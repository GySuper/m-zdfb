"""SQLModel engine + session + 状态转换辅助 + 幂等锁(M1)。"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import inspect, text, update
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from wxsp.config import get_user_data_dir
from wxsp.models import TASK_STATUS_PENDING, TASK_STATUS_RUNNING, Task

ENV_DB_PATH = "WXSP_DB_PATH"


def get_engine(db_path: Path | None = None) -> Engine:
    """构造 SQLAlchemy engine。

    解析顺序:显式参数 > 环境变量 `WXSP_DB_PATH` > `get_user_data_dir()/db.sqlite`。
    默认走 `get_user_data_dir()` 是为了在 PyInstaller bundle 里 anchor 到
    `~/Library/Application Support/wxsp/data/`(macOS) / `%APPDATA%/wxsp/data/`(Win)。
    早先版本默认是相对路径 `data/db.sqlite`,双击 .app 时 cwd=/,会 OSError EROFS。
    `check_same_thread=False` 让 SQLAlchemy 连接池可跨线程派发(claim_task 并发测试需要)。
    """
    if db_path is None:
        env = os.environ.get(ENV_DB_PATH)
        db_path = Path(env) if env else get_user_data_dir() / "db.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{db_path}"
    return create_engine(url, connect_args={"check_same_thread": False})


def init_db(engine: Engine) -> None:
    """幂等地建表 + 老库补列。SQLModel.metadata.create_all 重复调用安全。"""
    SQLModel.metadata.create_all(engine)
    _migrate_add_platform_column(engine)
    _migrate_add_community_columns(engine)


def _migrate_add_platform_column(engine: Engine) -> None:
    """老库升级补列:多平台改造给 account/task/event 加了 platform 列,但
    create_all 只建缺失的表、不会给已存在的表加列。多平台之前建的老库缺这一列,
    会让所有按 platform 过滤的查询炸 'no such column: account.platform'。

    SQLite `ALTER TABLE ADD COLUMN ... NOT NULL DEFAULT` 给老行回填 'tencent_channel'
    (语义正确:多平台之前的数据都是视频号)。幂等:列已存在则跳过;表不存在(全新库
    由 create_all 建好,本就带 platform 列)也跳过。
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in ("account", "task", "event"):
            if table not in existing_tables:
                continue
            cols = {c["name"] for c in inspector.get_columns(table)}
            if "platform" in cols:
                continue
            conn.execute(
                text(
                    f'ALTER TABLE "{table}" ADD COLUMN platform VARCHAR '
                    "NOT NULL DEFAULT 'tencent_channel'"
                )
            )
            logger.info(f"[db] 老库迁移:{table} 补 platform 列(老行回填 tencent_channel)")


def _migrate_add_community_columns(engine: Engine) -> None:
    """老库升级补列:小红书社区站登录态字段。模式同 _migrate_add_platform_column。"""
    inspector = inspect(engine)
    if "account" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("account")}
    with engine.begin() as conn:
        if "community_status" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE account ADD COLUMN community_status VARCHAR "
                    "NOT NULL DEFAULT 'unknown'"
                )
            )
            logger.info("[db] 老库迁移:account 补 community_status 列")
        if "community_last_active_at" not in cols:
            conn.execute(text("ALTER TABLE account ADD COLUMN community_last_active_at DATETIME"))
            logger.info("[db] 老库迁移:account 补 community_last_active_at 列")


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Session 上下文:成功 commit,异常 rollback。"""
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def transition_task(session: Session, task_id: int, *, status: str, **fields: Any) -> None:
    """更新 Task 行的 status 和任意字段。**调用方负责 commit**。

    与 `claim_task` 不同,本函数刻意不 commit,允许调用方把状态变更与同一 session 中
    的其它写入合并为一个事务(例如 publisher 收尾时同时写 remote_url + status)。

    - `task_id` 不存在 → `LookupError`
    - `fields` 中出现非 Task 字段名 → `AttributeError`
    """
    task = session.get(Task, task_id)
    if task is None:
        raise LookupError(f"Task id={task_id} not found")
    task.status = status
    for key, value in fields.items():
        if not hasattr(task, key):
            raise AttributeError(f"Task has no field {key!r}")
        setattr(task, key, value)
    session.add(task)


def claim_task(session: Session, task_id: int, *, lease_seconds: int = 1800) -> bool:
    """原子地把一条 `pending` 任务标记为 `running`。

    SQL 等价:
        UPDATE task
        SET status='running',
            lease_token=?, lease_expires_at=?, started_at=?,
            attempts = attempts + 1
        WHERE id=? AND status='pending' AND execute_date <= today

    SQLite 写入是串行化的,两个并发调用只有一个会让影响行数=1。
    返回 True 表示本次调用拿到了执行权;False 表示别人在跑、状态非 pending、
    execute_date 在未来,或 task 不存在。

    **本函数内部 commit 是有意为之**:UPDATE 必须独占一次完整事务并提交,SQLite 写锁
    才会释放、status='running' 才能对其它 worker 可见。调用方不应把它包在更外层的事务
    里等待其它写入一起 commit —— 会让后续 worker 阻塞、defeating 并发互斥语义。
    """
    now = datetime.now()
    stmt = (
        update(Task)
        .where(
            Task.id == task_id,  # type: ignore[arg-type]
            Task.status == TASK_STATUS_PENDING,  # type: ignore[arg-type]
            Task.execute_date <= date.today(),  # type: ignore[arg-type]
        )
        .values(
            status=TASK_STATUS_RUNNING,
            lease_token=uuid.uuid4().hex,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            started_at=now,
            attempts=Task.attempts + 1,
        )
    )
    result = session.execute(stmt)
    session.commit()
    return bool(result.rowcount == 1)  # type: ignore[attr-defined]
