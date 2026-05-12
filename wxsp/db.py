"""SQLModel engine + session + 状态转换辅助 + 幂等锁(M1)。"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import update
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from wxsp.models import TASK_STATUS_PENDING, TASK_STATUS_RUNNING, Task

DEFAULT_DB_PATH = Path("data") / "db.sqlite"
ENV_DB_PATH = "WXSP_DB_PATH"


def get_engine(db_path: Path | None = None) -> Engine:
    """构造 SQLAlchemy engine。

    解析顺序:显式参数 > 环境变量 `WXSP_DB_PATH` > 默认 `data/db.sqlite`。
    `check_same_thread=False` 让 SQLAlchemy 连接池可跨线程派发(claim_task 并发测试需要)。
    """
    if db_path is None:
        env = os.environ.get(ENV_DB_PATH)
        db_path = Path(env) if env else DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{db_path}"
    return create_engine(url, connect_args={"check_same_thread": False})


def init_db(engine: Engine) -> None:
    """幂等地建表。SQLModel.metadata.create_all 重复调用安全。"""
    SQLModel.metadata.create_all(engine)


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
    """更新 Task 行的 status 和任意字段。调用方负责 commit。

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
