# M1 数据层 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 SQLite 数据层(SQLModel 4 表 + 幂等锁 `claim_task` + 状态转换辅助 `transition_task`),并把 `wxsp accounts add/list/pause/resume` 4 个 CLI 命令从骨架占位实现为真实的 DB CRUD。

**Architecture:**
- 单一 DB 文件 `data/db.sqlite`(路径由 `WXSP_DB_PATH` 环境变量覆盖,默认相对 CWD)
- `wxsp/models.py` 定义 4 张表:`Account` / `Video` / `Task` / `Event` —— 模型即 schema 即 Pydantic 校验
- `wxsp/db.py` 提供 `get_engine` / `init_db` / `transition_task` / `claim_task`,业务模块拿到 ORM 对象后只读,状态变更走唯一加锁点 `claim_task`
- `Task` 表 `UniqueConstraint(video_id)` + `Index(status, execute_date)`,杜绝重复入库 + 加速调度查询
- `claim_task` = 单条原子 `UPDATE ... WHERE id=? AND status='pending' AND execute_date<=today`,以"影响行数=1"为加锁成功语义,SQLite 写入串行化天然保证两个并发线程只有一个赢
- CLI `accounts` 子命令直接走 DB(不读 `config.yaml`),通过 `_open_session()` 共享会话生命周期

**Tech Stack:**
- 已有:typer 0.12+ / pydantic 2.6+ / pyyaml 6.0+ / loguru 0.7+ / pytest 8.0+ / ruff 0.4+ / mypy 1.10+(实际通过 `uv run` 解析到 ≥ 2.x)
- 新增:**`sqlmodel>=0.0.21`**(自动带入 SQLAlchemy 2.x + Pydantic 2)
- 标准库:`sqlite3` / `threading` / `uuid` / `datetime`

**M1 验收(每条都要打勾)**
1. **`unique(video_id)` 约束生效**:同一个 `video_id` 插入两条 `Task` 时第二条抛 `IntegrityError`
2. **并发抢同一任务只有一个赢**:两个线程在 `threading.Barrier` 同步后同时调 `claim_task(task_id=N)`,精确一个返回 `True`,另一个返回 `False`,DB 中该任务 `status='running'` 且 `attempts=1`
3. **CLI 端到端**:`wxsp accounts add account_a --display-name "美食号" --user-data-dir ./data/chrome-profiles/account_a` 写入 DB → `wxsp accounts list` 输出该行 → `wxsp accounts pause account_a --hours 1` 设置 `paused_until` → `wxsp accounts resume account_a` 清空 `paused_until`
4. **质量门**:`pytest -v` 全绿(M0 19 个 + M1 新增至少 12 个)+ `pre-commit run --all-files` 全绿

---

## File Structure(M1 涉及的文件)

```
wxsp/
├── models.py       # M1 实现: 4 个 SQLModel 表 + 状态常量
├── db.py           # M1 实现: get_engine / init_db / session_scope / transition_task / claim_task
└── cli.py          # M1 修改: 替换 accounts list/pause/resume 占位 + 新增 accounts add

tests/
├── test_models.py  # M1 新建: schema 测试 + unique 约束
├── test_db.py      # M1 新建: engine / transition_task / claim_task(含并发)
└── test_cli_accounts.py  # M1 新建: 4 个 accounts 子命令的 CLI 集成测试
```

**不动的文件**:`config.py` / 其他 stub 模块 / pyproject.toml(只增 1 行依赖) / pre-commit / README。

---

## Task 1: 添加 sqlmodel 依赖

**Files:**
- Modify: `pyproject.toml`(`[project] dependencies`)

- [ ] **Step 1: 修改 `pyproject.toml` 添加 sqlmodel 依赖**

把 `pyproject.toml` 的 `[project] dependencies` 块改成:

```toml
[project]
name = "wxsp"
version = "0.0.1"
description = "微信视频号自动发布工具"
readme = "README.md"
requires-python = ">=3.10"
dependencies = [
    "typer>=0.12.0",
    "pydantic>=2.6.0",
    "pydantic-settings>=2.2.0",
    "pyyaml>=6.0",
    "loguru>=0.7.2",
    "sqlmodel>=0.0.21",
]
```

- [ ] **Step 2: 同步依赖**

Run: `uv sync --extra dev`
Expected: 成功安装 `sqlmodel`、`sqlalchemy` 等;`uv.lock` 自动更新。

- [ ] **Step 3: 验证 sqlmodel 可导入**

Run: `uv run python -c "import sqlmodel; print(sqlmodel.__version__)"`
Expected: 打印版本号(类似 `0.0.21` 或更高),无 ImportError。

- [ ] **Step 4: 提交**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add sqlmodel dependency for data layer"
```

---

## Task 2: 写 SQLModel 表的失败测试(RED)

**Files:**
- Create: `tests/test_models.py`

- [ ] **Step 1: 创建 `tests/test_models.py`**

把以下内容完整写入 `tests/test_models.py`:

```python
"""Tests for wxsp.models — SQLModel table definitions."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from wxsp.models import (
    TASK_STATUS_PENDING,
    Account,
    Event,
    Task,
    Video,
)


@pytest.fixture()
def engine():
    """In-memory SQLite engine with schema created."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _make_account(account_id: str = "account_a") -> Account:
    return Account(
        id=account_id,
        display_name="美食号",
        user_data_dir="./data/chrome-profiles/account_a",
        daily_limit=20,
    )


def _make_video(video_id: str = "rec_001") -> Video:
    return Video(
        id=video_id,
        file_path="/Volumes/NAS/wxsp/videos/test.mp4",
        title="测试标题十六字以上以满足校验长度要求",
        description="desc",
        tags_json="[]",
        ingested_at=datetime.now(),
    )


def _make_task(*, video_id: str = "rec_001", account_id: str = "account_a") -> Task:
    return Task(
        video_id=video_id,
        account_id=account_id,
        execute_date=date.today(),
        publish_at=datetime.now() + timedelta(hours=1),
        status=TASK_STATUS_PENDING,
    )


def test_account_round_trip(engine):
    with Session(engine) as session:
        session.add(_make_account())
        session.commit()
    with Session(engine) as session:
        row = session.exec(select(Account).where(Account.id == "account_a")).one()
    assert row.display_name == "美食号"
    assert row.is_active is True
    assert row.cookie_status == "unknown"
    assert row.paused_until is None


def test_video_round_trip(engine):
    with Session(engine) as session:
        session.add(_make_video())
        session.commit()
    with Session(engine) as session:
        row = session.exec(select(Video).where(Video.id == "rec_001")).one()
    assert row.source == "feishu"
    assert row.original_claim is False


def test_task_round_trip(engine):
    with Session(engine) as session:
        session.add(_make_account())
        session.add(_make_video())
        session.add(_make_task())
        session.commit()
    with Session(engine) as session:
        row = session.exec(select(Task)).one()
    assert row.id == 1
    assert row.status == TASK_STATUS_PENDING
    assert row.attempts == 0
    assert row.screenshots_json == "[]"


def test_task_unique_video_id_constraint(engine):
    """同一 video_id 只能存在一个 Task —— 杜绝重复入库。"""
    with Session(engine) as session:
        session.add(_make_account())
        session.add(_make_video())
        session.add(_make_task())
        session.commit()
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(_make_task())
        session.commit()


def test_event_round_trip(engine):
    with Session(engine) as session:
        session.add(_make_account())
        session.add(
            Event(
                ts=datetime.now(),
                level="info",
                type="manual_test",
                message="hello",
                account_id="account_a",
            )
        )
        session.commit()
    with Session(engine) as session:
        row = session.exec(select(Event)).one()
    assert row.type == "manual_test"
    assert row.context_json == "{}"


def test_task_status_constants_exist():
    """模块导出全部 6 个状态常量,且互不重复。"""
    from wxsp import models

    statuses = {
        models.TASK_STATUS_PENDING,
        models.TASK_STATUS_RUNNING,
        models.TASK_STATUS_SUCCESS,
        models.TASK_STATUS_FAILED,
        models.TASK_STATUS_SKIPPED,
        models.TASK_STATUS_INTERRUPTED,
    }
    assert statuses == {"pending", "running", "success", "failed", "skipped", "interrupted"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_models.py -v`
Expected: 全部失败,失败原因是 `ImportError: cannot import name 'Account' from 'wxsp.models'`(因为 `wxsp/models.py` 当前只有 docstring)。

- [ ] **Step 3: 提交 RED**

```bash
git add tests/test_models.py
git commit -m "test: add failing tests for SQLModel tables"
```

---

## Task 3: 实现 wxsp/models.py(GREEN)

**Files:**
- Modify: `wxsp/models.py`(从 1 行 docstring 扩展到完整实现)

- [ ] **Step 1: 把 `wxsp/models.py` 整体替换为以下内容**

```python
"""SQLModel 表定义:Account / Video / Task / Event(M1)。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel

# Task.status 状态机
TASK_STATUS_PENDING = "pending"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_SKIPPED = "skipped"
TASK_STATUS_INTERRUPTED = "interrupted"


class Account(SQLModel, table=True):
    id: str = Field(primary_key=True)
    display_name: str
    user_data_dir: str
    daily_limit: int = 20
    is_active: bool = True
    paused_until: datetime | None = None
    cookie_status: str = "unknown"
    cookie_last_checked_at: datetime | None = None
    cookie_last_active_at: datetime | None = None


class Video(SQLModel, table=True):
    id: str = Field(primary_key=True)
    source: str = "feishu"
    file_path: str
    title: str
    description: str | None = None
    tags_json: str = "[]"
    cover_path: str | None = None
    topic: str | None = None
    original_claim: bool = False
    file_hash: str | None = None
    ingested_at: datetime


class Task(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    video_id: str = Field(foreign_key="video.id", index=True)
    account_id: str = Field(foreign_key="account.id", index=True)
    execute_date: date = Field(index=True)
    publish_at: datetime
    status: str = Field(index=True)
    attempts: int = 0
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    last_error_type: str | None = None
    last_error_msg: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    remote_video_id: str | None = None
    remote_url: str | None = None
    screenshots_json: str = "[]"

    __table_args__ = (
        UniqueConstraint("video_id", name="uq_one_task_per_video"),
        Index("ix_status_execute_date", "status", "execute_date"),
    )


class Event(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    ts: datetime
    level: str
    task_id: int | None = Field(default=None, foreign_key="task.id", index=True)
    account_id: str | None = Field(default=None, foreign_key="account.id", index=True)
    type: str
    message: str
    context_json: str = "{}"
```

- [ ] **Step 2: 运行测试确认全部通过**

Run: `uv run pytest tests/test_models.py -v`
Expected: 6 个测试全部 PASSED。

- [ ] **Step 3: 跑 mypy + ruff 自检**

Run: `uv run mypy wxsp/models.py && uv run ruff check wxsp/models.py tests/test_models.py`
Expected: 都无报错(若 ruff 报 `I` 排序或 `UP` 升级提示,运行 `uv run ruff check --fix` 并 `uv run ruff format wxsp/models.py tests/test_models.py`)。

- [ ] **Step 4: 提交 GREEN**

```bash
git add wxsp/models.py
git commit -m "feat(models): implement 4 SQLModel tables with constraints + status constants"
```

---

## Task 4: 写 db.py 基础设施的失败测试(RED:engine + init_db + transition_task)

**Files:**
- Create: `tests/test_db.py`(后续 Task 6 会扩充,本任务先写一半)

- [ ] **Step 1: 创建 `tests/test_db.py`**

把以下内容完整写入 `tests/test_db.py`:

```python
"""Tests for wxsp.db — engine / session / transition_task / claim_task."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, select

from wxsp.db import get_engine, init_db, transition_task
from wxsp.models import (
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    Account,
    Task,
    Video,
)


@pytest.fixture()
def engine(tmp_path: Path):
    db_path = tmp_path / "wxsp-test.sqlite"
    engine = get_engine(db_path)
    init_db(engine)
    yield engine
    engine.dispose()


def _seed_pending_task(engine, *, task_id_expected: int = 1) -> int:
    with Session(engine) as session:
        session.add(
            Account(
                id="account_a",
                display_name="美食号",
                user_data_dir="./profiles/a",
                daily_limit=20,
            )
        )
        session.add(
            Video(
                id="rec_001",
                file_path="/x.mp4",
                title="测试标题十六字以上以满足校验长度要求",
                tags_json="[]",
                ingested_at=datetime.now(),
            )
        )
        task = Task(
            video_id="rec_001",
            account_id="account_a",
            execute_date=date.today(),
            publish_at=datetime.now() + timedelta(hours=1),
            status=TASK_STATUS_PENDING,
        )
        session.add(task)
        session.commit()
        assert task.id == task_id_expected
        return task.id


def test_get_engine_uses_explicit_path(tmp_path: Path):
    db_path = tmp_path / "explicit.sqlite"
    engine = get_engine(db_path)
    init_db(engine)
    assert db_path.exists()
    engine.dispose()


def test_get_engine_honors_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "from-env.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine()
    init_db(engine)
    assert db_path.exists()
    engine.dispose()


def test_init_db_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "twice.sqlite"
    engine = get_engine(db_path)
    init_db(engine)
    init_db(engine)  # second call must not raise
    engine.dispose()


def test_transition_task_updates_status_and_fields(engine):
    task_id = _seed_pending_task(engine)
    with Session(engine) as session:
        transition_task(
            session,
            task_id,
            status=TASK_STATUS_FAILED,
            last_error_type="network",
            last_error_msg="timeout",
        )
        session.commit()
    with Session(engine) as session:
        row = session.exec(select(Task).where(Task.id == task_id)).one()
    assert row.status == TASK_STATUS_FAILED
    assert row.last_error_type == "network"
    assert row.last_error_msg == "timeout"


def test_transition_task_rejects_unknown_field(engine):
    task_id = _seed_pending_task(engine)
    with Session(engine) as session, pytest.raises(AttributeError):
        transition_task(session, task_id, status=TASK_STATUS_RUNNING, not_a_field=42)


def test_transition_task_missing_task_raises(engine):
    with Session(engine) as session, pytest.raises(LookupError):
        transition_task(session, task_id=9999, status=TASK_STATUS_FAILED)
```

- [ ] **Step 2: 运行测试确认全部失败**

Run: `uv run pytest tests/test_db.py -v`
Expected: 全部失败,因为 `wxsp/db.py` 目前只有 docstring,`from wxsp.db import get_engine, init_db, transition_task` 会 ImportError。

- [ ] **Step 3: 提交 RED**

```bash
git add tests/test_db.py
git commit -m "test: add failing tests for db engine/init/transition_task"
```

---

## Task 5: 实现 db.py 基础(GREEN:get_engine + init_db + session_scope + transition_task)

**Files:**
- Modify: `wxsp/db.py`(从 1 行 docstring 扩展)

- [ ] **Step 1: 把 `wxsp/db.py` 整体替换为以下内容**

```python
"""SQLModel engine + session + 状态转换辅助 + 幂等锁(M1)。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from wxsp.models import Task

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
```

- [ ] **Step 2: 运行测试**

Run: `uv run pytest tests/test_db.py -v`
Expected: 6 个测试全部 PASSED(claim_task 的测试还没写,会在 Task 6 加)。

- [ ] **Step 3: mypy + ruff**

Run: `uv run mypy wxsp/db.py && uv run ruff check wxsp/db.py tests/test_db.py`
Expected: 无报错。若 ruff 报问题,跑 `uv run ruff check --fix` + `uv run ruff format`。

- [ ] **Step 4: 提交 GREEN**

```bash
git add wxsp/db.py
git commit -m "feat(db): implement engine, session_scope, init_db, transition_task"
```

---

## Task 6: 写 claim_task 的失败测试(RED:含并发场景)

**Files:**
- Modify: `tests/test_db.py`(追加)

- [ ] **Step 1: 在 `tests/test_db.py` 末尾追加以下内容**

需要先在文件顶部把 `from wxsp.db import get_engine, init_db, transition_task` 改成:

```python
from wxsp.db import claim_task, get_engine, init_db, transition_task
```

(claim_task 还没实现,这一改会让"导入"层面失败,正是 RED 的目的。)

然后在 `tests/test_db.py` 文件末尾追加:

```python
def test_claim_task_succeeds_for_pending(engine):
    task_id = _seed_pending_task(engine)
    with Session(engine) as session:
        won = claim_task(session, task_id, lease_seconds=1800)
    assert won is True
    with Session(engine) as session:
        row = session.exec(select(Task).where(Task.id == task_id)).one()
    assert row.status == TASK_STATUS_RUNNING
    assert row.attempts == 1
    assert row.lease_token is not None
    assert row.lease_expires_at is not None
    assert row.started_at is not None


def test_claim_task_fails_when_not_pending(engine):
    task_id = _seed_pending_task(engine)
    with Session(engine) as session:
        first = claim_task(session, task_id)
    assert first is True
    with Session(engine) as session:
        again = claim_task(session, task_id)
    assert again is False


def test_claim_task_fails_when_execute_date_in_future(engine):
    """execute_date > today 不允许 claim(09:00 cron 只跑当天或过期)。"""
    with Session(engine) as session:
        session.add(
            Account(
                id="account_a",
                display_name="美食号",
                user_data_dir="./profiles/a",
                daily_limit=20,
            )
        )
        session.add(
            Video(
                id="rec_tomorrow",
                file_path="/x.mp4",
                title="测试标题十六字以上以满足校验长度要求",
                tags_json="[]",
                ingested_at=datetime.now(),
            )
        )
        task = Task(
            video_id="rec_tomorrow",
            account_id="account_a",
            execute_date=date.today() + timedelta(days=1),
            publish_at=datetime.now() + timedelta(days=1, hours=1),
            status=TASK_STATUS_PENDING,
        )
        session.add(task)
        session.commit()
        future_task_id = task.id
    assert future_task_id is not None
    with Session(engine) as session:
        won = claim_task(session, future_task_id)
    assert won is False


def test_claim_task_missing_returns_false(engine):
    with Session(engine) as session:
        assert claim_task(session, task_id=9999) is False


def test_claim_task_concurrent_only_one_wins(engine):
    """两个线程在 Barrier 同步后同时调 claim_task,精确一个赢。"""
    import threading

    task_id = _seed_pending_task(engine)

    results: list[bool] = [False, False]
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker(idx: int) -> None:
        try:
            with Session(engine) as session:
                barrier.wait(timeout=5)
                results[idx] = claim_task(session, task_id)
        except BaseException as exc:  # noqa: BLE001 — collect across threads
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"worker threads raised: {errors!r}"
    assert sum(results) == 1, f"expected exactly one winner, got {results!r}"

    with Session(engine) as session:
        row = session.exec(select(Task).where(Task.id == task_id)).one()
    assert row.status == TASK_STATUS_RUNNING
    assert row.attempts == 1
```

- [ ] **Step 2: 运行测试确认全部失败**

Run: `uv run pytest tests/test_db.py -v`
Expected: 整个 `test_db.py` 因为 `ImportError: cannot import name 'claim_task' from 'wxsp.db'` 全部报错。

- [ ] **Step 3: 提交 RED**

```bash
git add tests/test_db.py
git commit -m "test: add failing tests for claim_task (sequential + concurrent)"
```

---

## Task 7: 实现 claim_task(GREEN)

**Files:**
- Modify: `wxsp/db.py`(在文件末尾追加 + 顶部增加 import)

- [ ] **Step 1: 修改 `wxsp/db.py` 顶部 import**

把 `wxsp/db.py` 现有的 import 块替换为:

```python
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
```

- [ ] **Step 2: 在 `wxsp/db.py` 文件末尾追加 `claim_task` 实现**

```python


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
            Task.id == task_id,
            Task.status == TASK_STATUS_PENDING,
            Task.execute_date <= date.today(),
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
    return bool(result.rowcount == 1)
```

- [ ] **Step 3: 运行测试**

Run: `uv run pytest tests/test_db.py -v`
Expected: 全部 11 个测试 PASSED(6 基础 + 5 claim_task)。

- [ ] **Step 4: mypy + ruff**

Run: `uv run mypy wxsp/db.py && uv run ruff check wxsp/db.py tests/test_db.py`
Expected: 无报错。

> 若 mypy 报 `Task.attempts + 1` 不能被 update 接受,这是 SQLAlchemy ORM column 表达式,运行时正确。如果 strict mode 抱怨,加 `# type: ignore[arg-type]` 到该单行,并在 commit message 中说明。

- [ ] **Step 5: 提交 GREEN**

```bash
git add wxsp/db.py
git commit -m "feat(db): implement claim_task atomic UPDATE with concurrent-safe semantics"
```

---

## Task 8: 写 CLI accounts 子命令的失败测试(RED)

**Files:**
- Create: `tests/test_cli_accounts.py`

- [ ] **Step 1: 创建 `tests/test_cli_accounts.py`**

```python
"""Tests for wxsp accounts add/list/pause/resume CLI commands."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlmodel import Session, select
from typer.testing import CliRunner

from wxsp.cli import app
from wxsp.db import get_engine, init_db
from wxsp.models import Account

runner = CliRunner()


@pytest.fixture()
def db_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "cli.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    return db_path


def test_accounts_add_creates_row(db_env: Path):
    result = runner.invoke(
        app,
        [
            "accounts",
            "add",
            "account_a",
            "--display-name",
            "美食号",
            "--user-data-dir",
            "./data/chrome-profiles/account_a",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "account_a" in result.stdout

    engine = get_engine(db_env)
    init_db(engine)
    with Session(engine) as session:
        row = session.exec(select(Account).where(Account.id == "account_a")).one()
    assert row.display_name == "美食号"
    assert row.daily_limit == 20  # default
    assert row.is_active is True


def test_accounts_add_honors_daily_limit_option(db_env: Path):
    result = runner.invoke(
        app,
        [
            "accounts",
            "add",
            "account_b",
            "--display-name",
            "健身号",
            "--user-data-dir",
            "./profiles/b",
            "--daily-limit",
            "30",
        ],
    )
    assert result.exit_code == 0

    engine = get_engine(db_env)
    init_db(engine)
    with Session(engine) as session:
        row = session.exec(select(Account).where(Account.id == "account_b")).one()
    assert row.daily_limit == 30


def test_accounts_add_duplicate_id_fails(db_env: Path):
    first = runner.invoke(
        app,
        [
            "accounts",
            "add",
            "account_a",
            "--display-name",
            "美食号",
            "--user-data-dir",
            "./profiles/a",
        ],
    )
    assert first.exit_code == 0

    second = runner.invoke(
        app,
        [
            "accounts",
            "add",
            "account_a",
            "--display-name",
            "另一个",
            "--user-data-dir",
            "./profiles/a2",
        ],
    )
    assert second.exit_code != 0
    assert "已存在" in second.stdout or "exists" in second.stdout.lower()


def test_accounts_list_empty(db_env: Path):
    result = runner.invoke(app, ["accounts", "list"])
    assert result.exit_code == 0
    assert "无账号" in result.stdout or "no account" in result.stdout.lower()


def test_accounts_list_shows_rows(db_env: Path):
    runner.invoke(
        app,
        [
            "accounts",
            "add",
            "account_a",
            "--display-name",
            "美食号",
            "--user-data-dir",
            "./profiles/a",
        ],
    )
    runner.invoke(
        app,
        [
            "accounts",
            "add",
            "account_b",
            "--display-name",
            "健身号",
            "--user-data-dir",
            "./profiles/b",
        ],
    )

    result = runner.invoke(app, ["accounts", "list"])
    assert result.exit_code == 0
    assert "account_a" in result.stdout
    assert "account_b" in result.stdout
    assert "美食号" in result.stdout
    assert "健身号" in result.stdout


def test_accounts_pause_sets_paused_until(db_env: Path):
    runner.invoke(
        app,
        [
            "accounts",
            "add",
            "account_a",
            "--display-name",
            "美食号",
            "--user-data-dir",
            "./profiles/a",
        ],
    )

    before = datetime.now()
    result = runner.invoke(app, ["accounts", "pause", "account_a", "--hours", "2"])
    assert result.exit_code == 0

    engine = get_engine(db_env)
    init_db(engine)
    with Session(engine) as session:
        row = session.exec(select(Account).where(Account.id == "account_a")).one()
    assert row.paused_until is not None
    delta = (row.paused_until - before).total_seconds()
    # 允许 60s 漂移,但必须在 2h ± 60s 之间
    assert 7140 <= delta <= 7260, f"paused_until delta = {delta}s"


def test_accounts_pause_missing_account_fails(db_env: Path):
    result = runner.invoke(app, ["accounts", "pause", "no_such_id", "--hours", "1"])
    assert result.exit_code != 0
    assert "no_such_id" in result.stdout


def test_accounts_resume_clears_paused_until(db_env: Path):
    runner.invoke(
        app,
        [
            "accounts",
            "add",
            "account_a",
            "--display-name",
            "美食号",
            "--user-data-dir",
            "./profiles/a",
        ],
    )
    runner.invoke(app, ["accounts", "pause", "account_a", "--hours", "5"])

    result = runner.invoke(app, ["accounts", "resume", "account_a"])
    assert result.exit_code == 0

    engine = get_engine(db_env)
    init_db(engine)
    with Session(engine) as session:
        row = session.exec(select(Account).where(Account.id == "account_a")).one()
    assert row.paused_until is None


def test_accounts_resume_missing_account_fails(db_env: Path):
    result = runner.invoke(app, ["accounts", "resume", "no_such_id"])
    assert result.exit_code != 0
```

- [ ] **Step 2: 运行测试确认全部失败**

Run: `uv run pytest tests/test_cli_accounts.py -v`
Expected: 大多数测试失败 —— `accounts add` 子命令不存在(M0 骨架没有它),其它 3 个子命令的 `_not_implemented` 占位虽然 exit_code=0 但输出里没有所需 token,断言会失败。

- [ ] **Step 3: 提交 RED**

```bash
git add tests/test_cli_accounts.py
git commit -m "test: add failing tests for accounts add/list/pause/resume CLI"
```

---

## Task 9: 实现 CLI accounts 子命令(GREEN)

**Files:**
- Modify: `wxsp/cli.py`(替换 4 个 accounts 子命令的实现,保留其它命令)

- [ ] **Step 1: 把 `wxsp/cli.py` 整体替换为以下内容**

```python
"""Typer CLI 入口(M0 骨架 + M1 accounts 子命令,后续 milestone 逐步实现其它命令)。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

import typer
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from wxsp.db import get_engine, init_db, session_scope
from wxsp.models import Account

app = typer.Typer(
    name="wxsp",
    help="微信视频号自动发布工具",
    no_args_is_help=True,
    add_completion=False,
)

accounts_app = typer.Typer(help="账号管理", no_args_is_help=True)
app.add_typer(accounts_app, name="accounts")


def _not_implemented(name: str) -> None:
    typer.echo(f"[wxsp] 命令 `{name}` 还未实现(M0 骨架阶段)。")


@contextmanager
def _open_session() -> Iterator[Session]:
    """CLI 共用:取 engine → 建表 → 开 session(成功 commit,异常 rollback)。"""
    engine = get_engine()
    init_db(engine)
    with session_scope(engine) as session:
        yield session


@app.command("login")
def login(account_id: str = typer.Argument(..., help="账号 ID")) -> None:
    """扫码登录指定账号,刷新 Cookie(M2 实现)。"""
    _not_implemented(f"login {account_id}")


@accounts_app.command("add")
def accounts_add(
    account_id: str = typer.Argument(..., help="账号 ID,如 account_a"),
    display_name: str = typer.Option(..., "--display-name", help="账号显示名,如 美食号"),
    user_data_dir: str = typer.Option(
        ..., "--user-data-dir", help="Chrome profile 目录,每账号独立"
    ),
    daily_limit: int = typer.Option(20, "--daily-limit", help="每日发布上限"),
) -> None:
    """新增账号到 DB。"""
    with _open_session() as session:
        existing = session.get(Account, account_id)
        if existing is not None:
            typer.echo(f"[wxsp] 账号 {account_id!r} 已存在。")
            raise typer.Exit(code=1)
        session.add(
            Account(
                id=account_id,
                display_name=display_name,
                user_data_dir=user_data_dir,
                daily_limit=daily_limit,
            )
        )
        try:
            session.flush()
        except IntegrityError as exc:
            typer.echo(f"[wxsp] 写入账号 {account_id!r} 失败:{exc}")
            raise typer.Exit(code=1) from exc
    typer.echo(f"[wxsp] 已新增账号 {account_id} ({display_name})。")


@accounts_app.command("list")
def accounts_list() -> None:
    """列出所有账号及其 Cookie 状态。"""
    with _open_session() as session:
        rows = session.exec(select(Account).order_by(Account.id)).all()
    if not rows:
        typer.echo("[wxsp] 无账号。")
        return
    typer.echo(f"{'ID':<14} {'显示名':<12} {'状态':<10} {'Cookie':<10} {'暂停至':<20}")
    for row in rows:
        active = "active" if row.is_active else "inactive"
        paused = row.paused_until.strftime("%Y-%m-%d %H:%M") if row.paused_until else "-"
        typer.echo(
            f"{row.id:<14} {row.display_name:<12} {active:<10} "
            f"{row.cookie_status:<10} {paused:<20}"
        )


@accounts_app.command("pause")
def accounts_pause(
    account_id: str = typer.Argument(..., help="账号 ID"),
    hours: int = typer.Option(24, "--hours", "-h", help="暂停小时数"),
) -> None:
    """暂停指定账号 N 小时。"""
    with _open_session() as session:
        row = session.get(Account, account_id)
        if row is None:
            typer.echo(f"[wxsp] 账号 {account_id!r} 不存在。")
            raise typer.Exit(code=1)
        row.paused_until = datetime.now() + timedelta(hours=hours)
        session.add(row)
    typer.echo(f"[wxsp] 已暂停账号 {account_id} {hours} 小时。")


@accounts_app.command("resume")
def accounts_resume(account_id: str = typer.Argument(..., help="账号 ID")) -> None:
    """恢复指定账号(清空 paused_until)。"""
    with _open_session() as session:
        row = session.get(Account, account_id)
        if row is None:
            typer.echo(f"[wxsp] 账号 {account_id!r} 不存在。")
            raise typer.Exit(code=1)
        row.paused_until = None
        session.add(row)
    typer.echo(f"[wxsp] 已恢复账号 {account_id}。")


@app.command("doctor")
def doctor() -> None:
    """健康检查:账号 / Cookie / NAS / 飞书 API(M2-M4 实现)。"""
    _not_implemented("doctor")


@app.command("sync")
def sync() -> None:
    """立即拉一次飞书 Bitable,不跑任务(M3 实现)。"""
    _not_implemented("sync")


@app.command("run")
def run(
    daemon: bool = typer.Option(False, "--daemon", help="启动 daemon(09:00 cron + FastAPI)"),
    today: bool = typer.Option(False, "--today", help="立即跑今天所有 pending 任务"),
    task_id: int | None = typer.Option(None, "--task-id", help="跑指定单条任务"),
    dry_run: bool = typer.Option(False, "--dry-run", help="发布步骤跑到点'发布'前停下"),
) -> None:
    """执行任务(M5-M6 实现)。"""
    _not_implemented(
        f"run --daemon={daemon} --today={today} --task-id={task_id} --dry-run={dry_run}"
    )


@app.command("status")
def status(
    date: str | None = typer.Option(None, "--date", help="日期 YYYY-MM-DD,默认今天"),
) -> None:
    """查看任务状态汇总(M1 实现)。"""
    _not_implemented(f"status --date {date}")


@app.command("logs")
def logs(
    task_id: int | None = typer.Option(None, "--task-id", help="按 task 过滤"),
    follow: bool = typer.Option(False, "--follow", "-f", help="持续 tail"),
) -> None:
    """查看日志(M7 实现)。"""
    _not_implemented(f"logs --task-id {task_id} --follow {follow}")


@app.command("web")
def web(port: int = typer.Option(8765, "--port", "-p", help="Web UI 端口")) -> None:
    """启动 Web UI(M8 实现)。"""
    _not_implemented(f"web --port {port}")


if __name__ == "__main__":
    app()
```

- [ ] **Step 2: 运行测试**

Run: `uv run pytest tests/test_cli_accounts.py -v`
Expected: 10 个测试全部 PASSED。

- [ ] **Step 3: 运行全量测试**

Run: `uv run pytest -v`
Expected: 全部 PASSED(M0 19 + M1 新增 ≥ 27)。

- [ ] **Step 4: mypy + ruff**

Run: `uv run mypy wxsp && uv run ruff check wxsp tests`
Expected: 无报错。若有,跑 `uv run ruff check --fix wxsp tests && uv run ruff format wxsp tests` 修复。

- [ ] **Step 5: 提交 GREEN**

```bash
git add wxsp/cli.py
git commit -m "feat(cli): implement accounts add/list/pause/resume with real DB CRUD"
```

---

## Task 10: M1 最终验收 + 端到端冒烟

**Files:**
- 无新文件;只跑验收脚本 + 收尾 commit(如果有 pre-commit 自动改动)。

- [ ] **Step 1: 跑 pre-commit 全套**

Run: `uv run pre-commit run --all-files`
Expected: 全部 PASSED。若 ruff-format / end-of-file-fixer 自动改了内容,把改动 `git add` + `git commit -m "style: pre-commit auto-fixes"`。

- [ ] **Step 2: 跑全量 pytest**

Run: `uv run pytest -v`
Expected: 全部 PASSED,且数量 ≥ 31(M0 19 + Task 2 的 6 + Task 4-6 的 11 + Task 8 的 10)。

- [ ] **Step 3: 端到端冒烟 CLI(用临时 DB,不污染 ./data/db.sqlite)**

```bash
export WXSP_DB_PATH=/tmp/wxsp-m1-smoke.sqlite
rm -f "$WXSP_DB_PATH"
uv run wxsp accounts list                       # → 无账号
uv run wxsp accounts add account_a --display-name 美食号 --user-data-dir ./data/chrome-profiles/account_a
uv run wxsp accounts add account_b --display-name 健身号 --user-data-dir ./data/chrome-profiles/account_b --daily-limit 25
uv run wxsp accounts list                       # → 两行,account_a daily 20, account_b daily 25
uv run wxsp accounts pause account_a --hours 2
uv run wxsp accounts list                       # → account_a 暂停至 显示两小时后
uv run wxsp accounts resume account_a
uv run wxsp accounts list                       # → account_a 暂停至 显示 "-"
uv run wxsp accounts add account_a --display-name dup --user-data-dir ./x
echo "exit=$?"                                  # → exit=1
unset WXSP_DB_PATH
rm -f /tmp/wxsp-m1-smoke.sqlite
```

Expected: 每步输出符合预期,duplicate add 返回 exit 1。

- [ ] **Step 4: 验收三线总览(粘贴到 commit message)**

逐项打勾后 commit:
- ✅ `unique(video_id)` 约束:`tests/test_models.py::test_task_unique_video_id_constraint` PASSED
- ✅ 并发 claim_task 只一个赢:`tests/test_db.py::test_claim_task_concurrent_only_one_wins` PASSED
- ✅ CLI 端到端冒烟(Step 3)成功
- ✅ `pytest -v` 全绿(数量 ≥ 31)
- ✅ `pre-commit run --all-files` 全绿

- [ ] **Step 5: 标记 M1 完成的收尾 commit(只在 Step 1 有自动改动时需要)**

```bash
# 仅当 pre-commit 在 Step 1 自动改了文件时,提交一次清理
git status
git add -A
git commit -m "chore: m1 acceptance — data layer green"
```

---

## Self-Review(plan 作者已跑,工程师可跳过)

**Spec coverage**(对照 CLAUDE.md M1 + design doc §3.2/§4):
- 4 张 SQLModel 表 → Task 2-3 ✓
- `db.py` engine + session + `transition_task` + `claim_task` 原子锁 → Task 4-7 ✓
- `accounts add/list/pause/resume` 4 个 CLI → Task 8-9 ✓
- `unique(video_id)` 约束 → Task 2 测试 + Task 3 schema ✓
- 并发抢同一 task 只一个赢 → Task 6 测试 + Task 7 实现 ✓
- 跨平台:全用 `pathlib.Path` + `sqlite:///` URL,自动跨平台 ✓

**No placeholders**:每一步都有完整代码块或确切命令,无 TBD / TODO。

**Type consistency**:`transition_task(session, task_id, *, status, **fields)` 与 `claim_task(session, task_id, *, lease_seconds=1800)` 在 Task 4/5/6/7 一致;`get_engine(db_path: Path | None = None)` 在 Task 4/5/8/9 一致;`Account.id: str` / `Task.id: int` 等贯穿测试与实现一致。

**Not covered in M1(故意推后)**:
- FK 约束启用(`PRAGMA foreign_keys=ON`):YAGNI,M5/M6 真用到级联删除时再加
- `transition_task` 的状态机校验(只能从 X 转到 Y):M5 publisher 真需要时加
- daemon 启动扫 `running → interrupted`:M6 实现,M1 只提供原子锁原语
- `Account` 与 `config.yaml` 的同步:M3/M6 再做
