# M6 调度 daemon (scheduler.py) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 APScheduler 实现"每天 09:00 cron + 手动 fire,无 polling"的调度模型,同时实现 `wxsp run --daemon` / `wxsp run --today` / 启动时把僵尸 `running` 任务标 `interrupted`。

**Architecture:**
- **抽出 `wxsp/sync.py`**:把 `cli.py sync` 里的"飞书拉取 → 校验 → 写 DB → 回写飞书"逻辑提到一个 `sync_now(settings) -> SyncResult` 函数。CLI 和 scheduler 都调它,避免重复。
- **新增 `wxsp/scheduler.py`**:5 个函数 + 1 个 dataclass。`queue_today()` 扫今天 pending,`mark_stale_running_as_interrupted()` 启动时回收僵尸,`run_today_pending()` 顶层串"sync + queue + 跑",`make_scheduler()` 构造 APScheduler 实例并注册 09:00 cron,`start_daemon()` 阻塞跑。
- **单 worker 串行**:`run_today_pending()` 内 for-loop 顺序调 `publisher.publish(task_id, dry_run=False, settings=settings)`,每条之间不加额外停顿(`publisher.random_pause` 已经在步骤间停)。
- **跳过暂停账号**:循环时如果 `account.paused_until > now`,**留 task 在 pending**(下次 run --today 再试),不消费。
- **测试策略**:`sync_now` / `queue_today` / `mark_stale_running_as_interrupted` 用真 SQLite + 真 schema 单测;`run_today_pending` 用 mock `publish` 测顺序 + 跳过逻辑;`make_scheduler` 断言 trigger 配置,不真启动;`run --daemon` 不写自动测,M10 部署时人工验。

**Tech Stack:** APScheduler 3.x (`BlockingScheduler` + `CronTrigger`) · SQLModel · Typer · pytest

---

## File Structure

| 文件 | 创建/修改 | 职责 |
|---|---|---|
| `wxsp/sync.py` | **create** | `sync_now(settings) -> SyncResult`:把 cli.sync 的逻辑搬过来,无副作用打印 |
| `wxsp/scheduler.py` | **modify** | M6 实现:`queue_today` / `mark_stale_running_as_interrupted` / `run_today_pending` / `make_scheduler` / `start_daemon` |
| `wxsp/cli.py` | modify | `sync` 改调 `sync_now`;`run --today` / `run --daemon` 接 scheduler |
| `pyproject.toml` | modify | 加 `apscheduler>=3.10.0` 依赖 |
| `tests/test_sync.py` | **create** | `sync_now` 单测(基于现有 `tests/test_cli_sync.py` 的 mock 飞书 client 模式) |
| `tests/test_scheduler.py` | **create** | `queue_today` / `mark_stale_running_as_interrupted` / `run_today_pending` / `make_scheduler` 单测 |
| `tests/test_cli_run.py` | modify | 加 `run --today` 用例(stub `run_today_pending`) |
| `tests/test_cli_sync.py` | modify | 改成基于 `sync_now` 的 mock(原 mock fetch_pending_rows 那层) |
| `docs/superpowers/specs/2026-05-12-wxsp-design.md` | modify | M6 行尾加 "M6 完成 (2026-05-XX)" |

**不动**:`publisher.py`(M5 已就绪)、`feishu.py`(M3 已就绪)、`db.py`、`models.py`、`validator.py`、`nas.py`。

---

## Task 1: 抽出 `wxsp/sync.py`(纯函数 + `SyncResult` dataclass)

**Files:**
- Create: `wxsp/sync.py`
- Create: `tests/test_sync.py`

- [ ] **Step 1.1: 写失败测试 — sync_now happy path**

创建 `tests/test_sync.py`:

```python
"""sync.sync_now 单元测试:用 monkeypatch 替换 lark client 工厂 + fetch_pending_rows。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlmodel import Session, select

from tests.conftest import make_settings
from wxsp.config import (
    AccountConfig,
    FeishuBitableConfig,
    FeishuConfig,
    FeishuFieldMap,
    FeishuSyncConfig,
)
from wxsp.db import get_engine, init_db
from wxsp.feishu import BitableRow
from wxsp.models import Account, Task, Video
from wxsp.sync import SyncResult, sync_now


def _enable_feishu(settings, tmp_path: Path):
    """把 conftest.make_settings 出来的 settings 改成启用飞书 + 1 个账号。"""
    settings.accounts = {
        "a": AccountConfig(
            display_name="A",
            enabled=True,
            daily_limit=20,
            user_data_dir=tmp_path / "profile_a",
        ),
    }
    settings.feishu = FeishuConfig(
        enabled=True,
        app_id="cli_x",
        app_secret="secret",
        bitable=FeishuBitableConfig(app_token="appT", table_id="tblT"),
        field_map=FeishuFieldMap(),
        sync=FeishuSyncConfig(write_back_enabled=True),
    )
    return settings


def test_sync_now_writes_video_and_task_for_valid_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)

    # 视频文件
    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    (video_root / "v1.mp4").write_bytes(b"x")

    settings = _enable_feishu(make_settings(video_root, cover_root), tmp_path)

    with Session(engine) as session:
        session.add(
            Account(id="a", display_name="A", user_data_dir=str(tmp_path / "p_a"), daily_limit=20)
        )
        session.commit()

    pending_row = BitableRow(
        record_id="rec_001",
        fields={
            "视频文件": "v1.mp4",
            "标题": "我是一个合法的标题十二字够长",
            "账号": "a",
            "执行日期": int(datetime(2026, 5, 13).timestamp() * 1000),
            "定时发布时间": int(datetime(2026, 5, 13, 14, 0).timestamp() * 1000),
        },
    )

    monkeypatch.setattr("wxsp.sync.make_client", lambda *a, **kw: MagicMock())
    monkeypatch.setattr("wxsp.sync.fetch_pending_rows", lambda *a, **kw: [pending_row])
    writeback_calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "wxsp.sync.writeback_row",
        lambda client, *, app_token, table_id, record_id, fields: writeback_calls.append(
            (record_id, fields)
        ),
    )

    result: SyncResult = sync_now(settings)

    assert isinstance(result, SyncResult)
    assert result.pulled == 1
    assert result.accepted == 1
    assert result.rejected == 0
    assert result.skipped_existing == 0

    with Session(engine) as session:
        assert session.get(Video, "rec_001") is not None
        task = session.exec(select(Task)).first()
        assert task is not None
        assert task.account_id == "a"
        assert task.status == "pending"

    # 回写: 1 条接受 → "已计划"
    assert len(writeback_calls) == 1
    assert writeback_calls[0][0] == "rec_001"
    assert writeback_calls[0][1]["状态"] == "已计划"
```

- [ ] **Step 1.2: 跑测试验证 fail**

```bash
uv run pytest tests/test_sync.py -v
```
预期:`ModuleNotFoundError: No module named 'wxsp.sync'`

- [ ] **Step 1.3: 写 `wxsp/sync.py` 实现**

```python
"""按需调用的飞书 sync(M6):被 CLI 和 scheduler 共用。

把 M3 时落在 cli.py 里的"拉飞书 → 校验 → 写 DB → 回写"流程抽到这里,
打印交给调用方;sync_now 只负责返回 SyncResult。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from wxsp.config import Settings
from wxsp.db import get_engine, init_db, session_scope
from wxsp.feishu import FeishuApiError, fetch_pending_rows, make_client, writeback_row
from wxsp.models import Account, Task, Video
from wxsp.nas import find_cover, find_video
from wxsp.validator import FieldError, NasFinder, validate


@dataclass
class SyncResult:
    pulled: int = 0
    accepted: int = 0
    rejected: int = 0
    skipped_existing: int = 0
    rejected_details: list[tuple[str, list[FieldError]]] = field(default_factory=list)


class _NasFinderImpl:
    def __init__(self, video_root: Path, cover_root: Path) -> None:
        self._video_root = video_root
        self._cover_root = cover_root

    def find_video(self, filename: str) -> Path:
        return find_video(filename, search_root=self._video_root)

    def find_cover(self, filename: str) -> Path:
        return find_cover(filename, search_root=self._cover_root)


def sync_now(settings: Settings, *, dry_run: bool = False) -> SyncResult:
    """拉飞书一次,入库 + 回写。"""
    result = SyncResult()
    if not settings.feishu.enabled:
        return result

    client = make_client(settings.feishu.app_id, settings.feishu.app_secret)
    rows = fetch_pending_rows(
        client,
        app_token=settings.feishu.bitable.app_token,
        table_id=settings.feishu.bitable.table_id,
        status_field=settings.feishu.field_map.status,
    )
    result.pulled = len(rows)

    nas_finder: NasFinder = _NasFinderImpl(
        video_root=settings.paths.video_search_root,
        cover_root=settings.paths.cover_search_root,
    )
    now = datetime.now()
    accepted: list[str] = []
    rejected: list[tuple[str, list[FieldError]]] = []
    skipped: list[str] = []

    engine = get_engine()
    init_db(engine)
    with session_scope(engine) as session:
        active_account_ids: set[str] = {
            a.id
            for a in session.exec(select(Account).where(Account.is_active == True))  # noqa: E712
        }
        for row in rows:
            if session.get(Video, row.record_id) is not None:
                skipped.append(row.record_id)
                continue
            v_result = validate(
                row,
                config=settings,
                now=now,
                nas_finder=nas_finder,
                active_account_ids=active_account_ids,
            )
            if not v_result.ok:
                rejected.append((row.record_id, v_result.errors))
                continue
            if dry_run:
                accepted.append(row.record_id)
                continue
            video = Video(
                id=row.record_id,
                source="feishu",
                file_path=str(v_result.video_path),
                title=v_result.title or "",
                description=v_result.description,
                tags_json=json.dumps(v_result.tags, ensure_ascii=False),
                cover_path=str(v_result.cover_path) if v_result.cover_path else None,
                topic=v_result.topic,
                original_claim=v_result.original_claim,
                ingested_at=now,
            )
            task = Task(
                video_id=row.record_id,
                account_id=v_result.account_id or "",
                execute_date=v_result.execute_date,
                publish_at=v_result.publish_at,
                status="pending",
            )
            try:
                with session.begin_nested():
                    session.add(video)
                    session.add(task)
            except IntegrityError:
                skipped.append(row.record_id)
                continue
            accepted.append(row.record_id)

    result.accepted = len(accepted)
    result.rejected = len(rejected)
    result.skipped_existing = len(skipped)
    result.rejected_details = rejected

    if not dry_run and settings.feishu.sync.write_back_enabled:
        fm = settings.feishu.field_map
        for record_id in accepted:
            _safe_writeback(client, settings, record_id, {fm.status: "已计划"})
        for record_id, errs in rejected:
            _safe_writeback(
                client,
                settings,
                record_id,
                {fm.status: "失败", fm.error_message: _format_errors(errs)},
            )
        for record_id in skipped:
            _safe_writeback(
                client,
                settings,
                record_id,
                {fm.error_message: "已有历史任务,请在 Web UI 重试"},
            )

    return result


def _safe_writeback(
    client: Any, settings: Settings, record_id: str, fields: dict[str, Any]
) -> None:
    try:
        writeback_row(
            client,
            app_token=settings.feishu.bitable.app_token,
            table_id=settings.feishu.bitable.table_id,
            record_id=record_id,
            fields=fields,
        )
    except FeishuApiError:
        pass  # 单行回写失败不打断 sync;由调用方决定要不要 echo


def _format_errors(errs: list[FieldError]) -> str:
    bullet_lines = "\n".join(f"· {e.field}: {e.message}" for e in errs)
    return f'校验失败,请修复后将"状态"改回"待入库":\n{bullet_lines}'
```

- [ ] **Step 1.4: 跑测试验证 pass**

```bash
uv run pytest tests/test_sync.py -v
```
预期:1 passed

- [ ] **Step 1.5: 加一个 sync_now 跳过 feishu disabled 的测试**

追加到 `tests/test_sync.py`:

```python
def test_sync_now_returns_empty_when_feishu_disabled(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, tmp_path)
    # feishu 在 conftest.make_settings 里默认就是 enabled=False
    result = sync_now(settings)
    assert result == SyncResult()
```

跑:
```bash
uv run pytest tests/test_sync.py -v
```
预期:2 passed

- [ ] **Step 1.6: Commit**

```bash
git add wxsp/sync.py tests/test_sync.py
git commit -m "$(cat <<'EOF'
feat(sync): extract sync_now() from cli — shared by CLI and scheduler

M6 准备:scheduler 的 worker 入口需要在跑 task 前 sync 一次飞书,
原来 cli.sync 把流程和打印混在一起,先抽干净。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: CLI `sync` 改调 `sync_now`(纯重构,行为不变)

**Files:**
- Modify: `wxsp/cli.py`(替换 `sync` 函数体)
- Modify: `tests/test_cli_sync.py`(改 mock 点)

- [ ] **Step 2.1: 先看现有 test_cli_sync.py 的 mock 模式**

```bash
grep -n "monkeypatch.setattr\|fetch_pending_rows\|writeback_row" tests/test_cli_sync.py
```

理解现状:它在 mock `wxsp.cli.fetch_pending_rows` / `wxsp.cli.writeback_row`。Task 2 之后这些 mock 点要改成 `wxsp.sync.fetch_pending_rows` / `wxsp.sync.writeback_row`(因为代码搬家了),或者 mock 更高层 `wxsp.cli.sync_now`。

- [ ] **Step 2.2: 改 test_cli_sync.py 的 mock 点**

把所有 `monkeypatch.setattr("wxsp.cli.fetch_pending_rows", ...)` 改成 `monkeypatch.setattr("wxsp.sync.fetch_pending_rows", ...)`,`writeback_row` / `make_client` 同理。**保留全部既有断言不动**。

- [ ] **Step 2.3: 跑测试,看会怎样 fail**

```bash
uv run pytest tests/test_cli_sync.py -v
```
预期:目前应该是 pass(因为 cli.sync 还在),mock 点改了之后某些用例可能 still pass(因为打 patch 打到了一个没人调的模块);Step 2.5 改完 cli 后再确认。

- [ ] **Step 2.4: 改 `wxsp/cli.py`**

把现有的 `@app.command("sync")` 函数体整段替换成:

```python
@app.command("sync")
def sync(
    dry_run: bool = typer.Option(False, "--dry-run", help="走完流程但不写 DB 不回写飞书"),
) -> None:
    """立即拉一次飞书 Bitable,执行入库 / 错误回写。"""
    from wxsp.sync import sync_now

    settings = load_settings()
    if not settings.feishu.enabled:
        typer.echo("[wxsp] 飞书未启用,跳过 sync。")
        return

    typer.echo(
        f"[wxsp] 飞书同步开始: app_token={settings.feishu.bitable.app_token} "
        f"table_id={settings.feishu.bitable.table_id}"
    )
    try:
        result = sync_now(settings, dry_run=dry_run)
    except FeishuApiError as exc:
        typer.echo(f"[wxsp] 飞书 API 持续失败: {exc}")
        raise typer.Exit(code=70) from exc

    typer.echo("[wxsp] 飞书同步完成")
    typer.echo(f"  拉取: {result.pulled}")
    typer.echo(f"  入库: {result.accepted}{' (dry-run)' if dry_run else ''}")
    typer.echo(f"  拒绝: {result.rejected}{' (已回写)' if not dry_run else ''}")
    typer.echo(f"  已存在跳过: {result.skipped_existing}")
```

同时**删掉 `cli.py` 里**已经搬到 `sync.py` 的 import 和辅助函数:
- 删 `json` import(如果别处没用)
- 删 `_NasFinderImpl` 类
- 删 `_safe_writeback`
- 删 `_format_errors`
- 删 `Any`、`IntegrityError`、`FieldError`、`NasFinder`、`make_client`、`fetch_pending_rows`、`writeback_row`、`find_video`、`find_cover`、`Video`、`Task`、`validate` 的 import(grep 确认 cli.py 其他地方不再用)
- 保留 `Account`(`accounts` 子命令仍用)
- 保留 `FeishuApiError`(`sync` 函数还 catch)

- [ ] **Step 2.5: 跑测试**

```bash
uv run pytest tests/test_cli_sync.py -v
```
预期:全 pass。

- [ ] **Step 2.6: Commit**

```bash
git add wxsp/cli.py tests/test_cli_sync.py
git commit -m "$(cat <<'EOF'
refactor(cli): sync command delegates to sync.sync_now()

纯重构,行为和 echo 输出不变。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `scheduler.queue_today()` — 扫今天 pending,按 publish_at 升序

**Files:**
- Modify: `wxsp/scheduler.py`
- Create: `tests/test_scheduler.py`

- [ ] **Step 3.1: 写失败测试**

创建 `tests/test_scheduler.py`:

```python
"""scheduler.py(M6)单元测试。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session

from wxsp.db import get_engine, init_db, session_scope
from wxsp.models import Account, Task, Video
from wxsp.scheduler import queue_today


def _seed_account_video(session: Session, *, account_id: str = "a", video_id: str) -> None:
    if session.get(Account, account_id) is None:
        session.add(Account(id=account_id, display_name="A", user_data_dir="/tmp", daily_limit=20))
    session.add(
        Video(id=video_id, file_path="/tmp/v.mp4", title="x" * 16, ingested_at=datetime.now())
    )


def test_queue_today_returns_today_pending_ordered_by_publish_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    today = date.today()
    base = datetime.now()

    with session_scope(engine) as session:
        _seed_account_video(session, video_id="v1")
        _seed_account_video(session, video_id="v2")
        _seed_account_video(session, video_id="v3")
        # 倒序入库,看 queue 是否按 publish_at 排
        session.add(
            Task(
                video_id="v1",
                account_id="a",
                execute_date=today,
                publish_at=base + timedelta(hours=5),
                status="pending",
            )
        )
        session.add(
            Task(
                video_id="v2",
                account_id="a",
                execute_date=today,
                publish_at=base + timedelta(hours=2),
                status="pending",
            )
        )
        session.add(
            Task(
                video_id="v3",
                account_id="a",
                execute_date=today,
                publish_at=base + timedelta(hours=3),
                status="pending",
            )
        )

    with Session(engine) as session:
        ids = queue_today(session)

    # publish_at: v2 (h+2) < v3 (h+3) < v1 (h+5)
    assert len(ids) == 3
    # ids 是 task.id;publish_at 升序对应入库 v2 < v3 < v1
    with Session(engine) as session:
        ordered_video_ids = [session.get(Task, i).video_id for i in ids]
    assert ordered_video_ids == ["v2", "v3", "v1"]


def test_queue_today_excludes_other_dates_and_non_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    today = date.today()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)

    with session_scope(engine) as session:
        _seed_account_video(session, video_id="v_yesterday")
        _seed_account_video(session, video_id="v_today_pending")
        _seed_account_video(session, video_id="v_today_running")
        _seed_account_video(session, video_id="v_tomorrow")
        for vid, edate, status in [
            ("v_yesterday", yesterday, "pending"),
            ("v_today_pending", today, "pending"),
            ("v_today_running", today, "running"),
            ("v_tomorrow", tomorrow, "pending"),
        ]:
            session.add(
                Task(
                    video_id=vid,
                    account_id="a",
                    execute_date=edate,
                    publish_at=datetime.now(),
                    status=status,
                )
            )

    with Session(engine) as session:
        ids = queue_today(session)
        videos = [session.get(Task, i).video_id for i in ids]

    assert videos == ["v_today_pending"]
```

- [ ] **Step 3.2: 跑测试验证 fail**

```bash
uv run pytest tests/test_scheduler.py -v
```
预期:`ImportError: cannot import name 'queue_today'`

- [ ] **Step 3.3: 实现 `wxsp/scheduler.py`(只加 queue_today)**

替换文件内容:

```python
"""09:00 cron + 手动 fire(无 polling)(M6)。"""

from __future__ import annotations

from datetime import date as _date
from datetime import datetime

from sqlmodel import Session, select

from wxsp.models import TASK_STATUS_PENDING, Task


def queue_today(session: Session, *, today: _date | None = None) -> list[int]:
    """返回今天的 pending task id 列表,按 publish_at 升序。

    调用方负责开 session;本函数只读。
    """
    if today is None:
        today = _date.today()
    stmt = (
        select(Task)
        .where(Task.execute_date == today)
        .where(Task.status == TASK_STATUS_PENDING)
        .order_by(Task.publish_at.asc())  # type: ignore[union-attr]
    )
    return [task.id for task in session.exec(stmt) if task.id is not None]
```

- [ ] **Step 3.4: 跑测试验证 pass**

```bash
uv run pytest tests/test_scheduler.py -v
```
预期:2 passed

- [ ] **Step 3.5: Commit**

```bash
git add wxsp/scheduler.py tests/test_scheduler.py
git commit -m "$(cat <<'EOF'
feat(scheduler): queue_today() — pending tasks for today, ordered by publish_at

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `mark_stale_running_as_interrupted()` — daemon 启动时回收僵尸

**Files:**
- Modify: `wxsp/scheduler.py`
- Modify: `tests/test_scheduler.py`

- [ ] **Step 4.1: 写失败测试**

追加到 `tests/test_scheduler.py`:

```python
from wxsp.scheduler import mark_stale_running_as_interrupted


def test_mark_stale_running_as_interrupted_only_touches_expired_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    now = datetime(2026, 5, 13, 12, 0, 0)
    today = now.date()

    with session_scope(engine) as session:
        _seed_account_video(session, video_id="v_running_expired")
        _seed_account_video(session, video_id="v_running_fresh")
        _seed_account_video(session, video_id="v_pending")
        _seed_account_video(session, video_id="v_success")
        # 1) running + lease 已过期 → 应被标 interrupted
        session.add(
            Task(
                video_id="v_running_expired",
                account_id="a",
                execute_date=today,
                publish_at=now,
                status="running",
                lease_expires_at=now - timedelta(minutes=5),
            )
        )
        # 2) running + lease 没过期 → 不动
        session.add(
            Task(
                video_id="v_running_fresh",
                account_id="a",
                execute_date=today,
                publish_at=now,
                status="running",
                lease_expires_at=now + timedelta(minutes=15),
            )
        )
        # 3) pending → 不动
        session.add(
            Task(
                video_id="v_pending",
                account_id="a",
                execute_date=today,
                publish_at=now,
                status="pending",
            )
        )
        # 4) success → 不动
        session.add(
            Task(
                video_id="v_success",
                account_id="a",
                execute_date=today,
                publish_at=now,
                status="success",
            )
        )

    with session_scope(engine) as session:
        touched = mark_stale_running_as_interrupted(session, now=now)

    assert touched == 1

    with Session(engine) as session:
        statuses = {
            t.video_id: t.status
            for t in session.exec(select(Task)).all()
        }
    assert statuses["v_running_expired"] == "interrupted"
    assert statuses["v_running_fresh"] == "running"
    assert statuses["v_pending"] == "pending"
    assert statuses["v_success"] == "success"
```

- [ ] **Step 4.2: 跑测试验证 fail**

```bash
uv run pytest tests/test_scheduler.py::test_mark_stale_running_as_interrupted_only_touches_expired_running -v
```
预期:`ImportError`

- [ ] **Step 4.3: 实现**

在 `wxsp/scheduler.py` 顶部加 import,在文件末尾追加:

```python
from sqlalchemy import update

from wxsp.models import TASK_STATUS_INTERRUPTED, TASK_STATUS_RUNNING


def mark_stale_running_as_interrupted(session: Session, *, now: datetime | None = None) -> int:
    """启动时一次性把 `running` + lease 过期的 task 标为 `interrupted`。

    返回被改的行数。调用方负责开 session;本函数自己 commit(独立事务)。
    """
    if now is None:
        now = datetime.now()
    stmt = (
        update(Task)
        .where(Task.status == TASK_STATUS_RUNNING)  # type: ignore[arg-type]
        .where(Task.lease_expires_at.is_not(None))  # type: ignore[union-attr]
        .where(Task.lease_expires_at < now)  # type: ignore[arg-type, operator]
        .values(status=TASK_STATUS_INTERRUPTED)
    )
    result = session.execute(stmt)
    session.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
```

记得把 `TASK_STATUS_PENDING` 那行 import 改成:

```python
from wxsp.models import TASK_STATUS_INTERRUPTED, TASK_STATUS_PENDING, TASK_STATUS_RUNNING, Task
```

并把 `from sqlalchemy import update` 挪到顶部 import 区。

- [ ] **Step 4.4: 跑测试验证 pass**

```bash
uv run pytest tests/test_scheduler.py -v
```
预期:3 passed

- [ ] **Step 4.5: Commit**

```bash
git add wxsp/scheduler.py tests/test_scheduler.py
git commit -m "$(cat <<'EOF'
feat(scheduler): mark_stale_running_as_interrupted — daemon startup recovery

kill daemon 后,残留的 status=running 但 lease 已过期的 task 在下次启动时
被一次性标为 interrupted,等运营从 Web UI 决定。无 polling job。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `run_today_pending()` — 顶层串"sync + queue + 跑",跳过暂停账号

**Files:**
- Modify: `wxsp/scheduler.py`
- Modify: `tests/test_scheduler.py`

- [ ] **Step 5.1: 写失败测试 — happy path 顺序调 publish**

追加到 `tests/test_scheduler.py`:

```python
from unittest.mock import MagicMock, patch

from tests.conftest import make_settings
from wxsp.publisher import PublishResult
from wxsp.scheduler import RunSummary, run_today_pending


def test_run_today_pending_calls_publish_for_each_today_task_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    today = date.today()
    now = datetime.now()

    with session_scope(engine) as session:
        _seed_account_video(session, video_id="v1")
        _seed_account_video(session, video_id="v2")
        session.add(
            Task(
                video_id="v1",
                account_id="a",
                execute_date=today,
                publish_at=now + timedelta(hours=3),
                status="pending",
            )
        )
        session.add(
            Task(
                video_id="v2",
                account_id="a",
                execute_date=today,
                publish_at=now + timedelta(hours=1),  # 更早,应先跑
                status="pending",
            )
        )

    settings = make_settings(tmp_path, tmp_path)

    call_order: list[int] = []

    def fake_publish(task_id, *, dry_run, settings):
        call_order.append(task_id)
        return PublishResult(task_id=task_id, ok=True, dry_run=False)

    # 关键:让 sync_now 不真去调飞书
    monkeypatch.setattr("wxsp.scheduler.sync_now", lambda settings: None)
    monkeypatch.setattr("wxsp.scheduler.publish", fake_publish)

    summary: RunSummary = run_today_pending(settings)

    # v2.publish_at 更早,先跑
    with Session(engine) as session:
        ordered_v2_id = session.exec(select(Task).where(Task.video_id == "v2")).first().id
        ordered_v1_id = session.exec(select(Task).where(Task.video_id == "v1")).first().id
    assert call_order == [ordered_v2_id, ordered_v1_id]
    assert summary.attempted == 2
    assert summary.succeeded == 2
    assert summary.failed == 0
    assert summary.skipped_paused == 0


def test_run_today_pending_skips_tasks_when_account_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    today = date.today()
    now = datetime.now()

    with session_scope(engine) as session:
        # 账号 a 被暂停 24h
        session.add(
            Account(
                id="a",
                display_name="A",
                user_data_dir="/tmp",
                daily_limit=20,
                paused_until=now + timedelta(hours=24),
            )
        )
        session.add(
            Video(id="v1", file_path="/tmp/v.mp4", title="x" * 16, ingested_at=now)
        )
        session.add(
            Task(
                video_id="v1",
                account_id="a",
                execute_date=today,
                publish_at=now,
                status="pending",
            )
        )

    settings = make_settings(tmp_path, tmp_path)
    monkeypatch.setattr("wxsp.scheduler.sync_now", lambda settings: None)

    publish_calls: list[int] = []
    monkeypatch.setattr(
        "wxsp.scheduler.publish",
        lambda task_id, *, dry_run, settings: publish_calls.append(task_id) or PublishResult(
            task_id=task_id, ok=True, dry_run=False
        ),
    )

    summary = run_today_pending(settings)

    assert publish_calls == []
    assert summary.attempted == 0
    assert summary.skipped_paused == 1


def test_run_today_pending_continues_after_a_publish_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一条失败不应阻断后面的任务。"""
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    today = date.today()
    now = datetime.now()

    with session_scope(engine) as session:
        _seed_account_video(session, video_id="v1")
        _seed_account_video(session, video_id="v2")
        session.add(
            Task(
                video_id="v1",
                account_id="a",
                execute_date=today,
                publish_at=now,
                status="pending",
            )
        )
        session.add(
            Task(
                video_id="v2",
                account_id="a",
                execute_date=today,
                publish_at=now + timedelta(hours=1),
                status="pending",
            )
        )

    settings = make_settings(tmp_path, tmp_path)
    monkeypatch.setattr("wxsp.scheduler.sync_now", lambda settings: None)

    calls: list[int] = []

    def fake_publish(task_id, *, dry_run, settings):
        calls.append(task_id)
        # 第一条失败,第二条成功
        return PublishResult(
            task_id=task_id,
            ok=task_id != calls[0],
            dry_run=False,
            error_type=None if task_id != calls[0] else "network",
            error_msg=None if task_id != calls[0] else "boom",
        )

    monkeypatch.setattr("wxsp.scheduler.publish", fake_publish)

    summary = run_today_pending(settings)

    assert len(calls) == 2
    assert summary.attempted == 2
    assert summary.succeeded == 1
    assert summary.failed == 1
```

- [ ] **Step 5.2: 跑测试验证 fail**

```bash
uv run pytest tests/test_scheduler.py -v
```
预期:`ImportError: cannot import name 'RunSummary'` 等

- [ ] **Step 5.3: 实现 `run_today_pending` + `RunSummary`**

在 `wxsp/scheduler.py` 加 import 和函数:

```python
from dataclasses import dataclass

from loguru import logger

from wxsp.config import Settings
from wxsp.db import get_engine, init_db
from wxsp.models import Account
from wxsp.publisher import AlreadyClaimed, publish
from wxsp.sync import sync_now


@dataclass
class RunSummary:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped_paused: int = 0


def run_today_pending(settings: Settings) -> RunSummary:
    """worker 入口:① sync_now() ② queue_today() ③ 串行跑(跳过 paused 账号)。

    任何一条 task 失败 / 抛 AlreadyClaimed 都不阻断后面的;细节走 publish 自己的回写。
    """
    try:
        sync_now(settings)
    except Exception as exc:
        logger.warning(f"[scheduler] sync_now 失败,继续跑已入库的: {exc}")

    summary = RunSummary()
    engine = get_engine()
    init_db(engine)
    now = datetime.now()

    # 一次性把 (task_id, account_id) 读出来,后面循环里不再持 session 跑长 publish
    with Session(engine) as session:
        task_ids = queue_today(session)
        if not task_ids:
            return summary
        plan: list[tuple[int, str]] = []
        for tid in task_ids:
            t = session.get(Task, tid)
            if t is None:
                continue
            plan.append((tid, t.account_id))
        # 一次性拿 account.paused_until 快照
        paused_accounts: set[str] = set()
        for acc_id in {acc for _, acc in plan}:
            acc = session.get(Account, acc_id)
            if acc is not None and acc.paused_until is not None and acc.paused_until > now:
                paused_accounts.add(acc_id)

    for task_id, account_id in plan:
        if account_id in paused_accounts:
            summary.skipped_paused += 1
            logger.info(f"[scheduler] 跳过 task={task_id}:账号 {account_id} 暂停中")
            continue
        summary.attempted += 1
        try:
            result = publish(task_id, dry_run=False, settings=settings)
        except AlreadyClaimed as exc:
            summary.failed += 1
            logger.warning(f"[scheduler] task={task_id} 已被认领: {exc}")
            continue
        except Exception as exc:
            summary.failed += 1
            logger.exception(f"[scheduler] task={task_id} 跑挂了: {exc}")
            continue
        if result.ok:
            summary.succeeded += 1
        else:
            summary.failed += 1

    return summary
```

- [ ] **Step 5.4: 跑测试验证 pass**

```bash
uv run pytest tests/test_scheduler.py -v
```
预期:6 passed

- [ ] **Step 5.5: Commit**

```bash
git add wxsp/scheduler.py tests/test_scheduler.py
git commit -m "$(cat <<'EOF'
feat(scheduler): run_today_pending() — sync + queue + run serial

worker 入口:先 sync 飞书,再扫今天 pending,串行调 publisher.publish。
暂停的账号(paused_until > now)被跳过,task 留在 pending 等下次。
一条失败不阻断后面的;每条的 DB 回写仍由 publish 自己负责。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: APScheduler 依赖 + `make_scheduler()` 配 09:00 cron

**Files:**
- Modify: `pyproject.toml`
- Modify: `wxsp/scheduler.py`
- Modify: `tests/test_scheduler.py`

- [ ] **Step 6.1: 加 apscheduler 依赖**

修改 `pyproject.toml` 的 `dependencies`,在 `lark-oapi>=1.4.0` 后面加:

```toml
    "lark-oapi>=1.4.0",
    "apscheduler>=3.10.0",
]
```

跑:
```bash
uv sync
```

预期:apscheduler 装好。

- [ ] **Step 6.2: 写失败测试**

追加到 `tests/test_scheduler.py`:

```python
from apscheduler.triggers.cron import CronTrigger

from wxsp.scheduler import make_scheduler


def test_make_scheduler_registers_daily_cron_with_configured_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, tmp_path)
    settings.scheduler.daily_cron_hour = 9
    settings.scheduler.daily_cron_minute = 0
    settings.app.timezone = "Asia/Shanghai"

    sched = make_scheduler(settings)
    try:
        jobs = sched.get_jobs()
        assert len(jobs) == 1
        trigger = jobs[0].trigger
        assert isinstance(trigger, CronTrigger)
        # CronTrigger 的字段用 .fields 暴露,name -> hour/minute
        field_values = {f.name: str(f) for f in trigger.fields}
        assert field_values["hour"] == "9"
        assert field_values["minute"] == "0"
    finally:
        sched.shutdown(wait=False)
```

- [ ] **Step 6.3: 跑测试验证 fail**

```bash
uv run pytest tests/test_scheduler.py::test_make_scheduler_registers_daily_cron_with_configured_time -v
```
预期:`ImportError`

- [ ] **Step 6.4: 实现 `make_scheduler`**

在 `wxsp/scheduler.py` 顶部 import:

```python
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
```

在文件末尾加:

```python
def make_scheduler(settings: Settings, *, blocking: bool = False):
    """构造 APScheduler 并注册"每日 09:00 cron job"。

    blocking=True 用于 daemon 进程(主线程 .start() 阻塞);
    blocking=False(默认)用于测试 / 后台模式。**调用方负责 shutdown**。
    """
    scheduler_cls = BlockingScheduler if blocking else BackgroundScheduler
    scheduler = scheduler_cls(timezone=settings.app.timezone)
    scheduler.add_job(
        run_today_pending,
        trigger=CronTrigger(
            hour=settings.scheduler.daily_cron_hour,
            minute=settings.scheduler.daily_cron_minute,
            timezone=settings.app.timezone,
        ),
        args=[settings],
        id="daily_run_today_pending",
        replace_existing=True,
    )
    return scheduler
```

- [ ] **Step 6.5: 跑测试验证 pass**

```bash
uv run pytest tests/test_scheduler.py -v
```
预期:7 passed

- [ ] **Step 6.6: 补 `start_daemon`(daemon 入口,blocking)**

在 `wxsp/scheduler.py` 末尾再加:

```python
def start_daemon(settings: Settings) -> None:
    """daemon 入口:启动时 (1) 标 interrupted (2) 起 blocking scheduler。

    `BlockingScheduler.start()` 会阻塞当前线程,直到 SIGINT/SIGTERM。
    `--daemon` 模式不在测试中跑;由 `wxsp run --daemon` CLI 调用。
    """
    engine = get_engine()
    init_db(engine)
    with session_scope(engine) as session:
        touched = mark_stale_running_as_interrupted(session)
    if touched:
        logger.warning(f"[scheduler] 启动时标 interrupted: {touched} 条僵尸 running")

    scheduler = make_scheduler(settings, blocking=True)
    logger.info(
        f"[scheduler] daemon 启动:每日 "
        f"{settings.scheduler.daily_cron_hour:02d}:{settings.scheduler.daily_cron_minute:02d} "
        f"({settings.app.timezone}) 跑 run_today_pending"
    )
    scheduler.start()  # 阻塞
```

(不为 `start_daemon` 写测试 —— 它本质是 "组装 + 阻塞",组装部分由 `make_scheduler` 测,阻塞部分由 M10 部署人工验。)

- [ ] **Step 6.7: Commit**

```bash
git add pyproject.toml uv.lock wxsp/scheduler.py tests/test_scheduler.py
git commit -m "$(cat <<'EOF'
feat(scheduler): APScheduler 09:00 cron + start_daemon()

make_scheduler(settings) 注册每日 cron(配置可调),start_daemon()
组装"启动扫 interrupted + 阻塞跑 scheduler"两步,供 wxsp run --daemon 用。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: CLI 接线 — `run --today` + `run --daemon`

**Files:**
- Modify: `wxsp/cli.py`
- Modify: `tests/test_cli_run.py`

- [ ] **Step 7.1: 写失败测试 — `run --today`**

追加到 `tests/test_cli_run.py`:

```python
def test_run_today_invokes_run_today_pending_and_echoes_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`wxsp run --today` 调 scheduler.run_today_pending,正常输出 summary。"""
    from typer.testing import CliRunner

    from wxsp.cli import app
    from wxsp.scheduler import RunSummary

    captured: dict[str, object] = {}

    def fake_run(settings):
        captured["called"] = True
        return RunSummary(attempted=3, succeeded=2, failed=1, skipped_paused=0)

    monkeypatch.setattr("wxsp.cli.run_today_pending", fake_run)
    monkeypatch.setattr("wxsp.cli.load_settings", lambda: object())

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--today"])

    assert result.exit_code == 0, result.output
    assert captured.get("called") is True
    assert "attempted=3" in result.output
    assert "succeeded=2" in result.output
    assert "failed=1" in result.output
```

- [ ] **Step 7.2: 跑测试验证 fail**

```bash
uv run pytest tests/test_cli_run.py::test_run_today_invokes_run_today_pending_and_echoes_summary -v
```
预期:fail —— 当前 cli.run 在 `task_id=None` 走 `_not_implemented`。

- [ ] **Step 7.3: 改 `wxsp/cli.py` `run` 命令**

在顶部 import:

```python
from wxsp.scheduler import run_today_pending, start_daemon
```

替换 `@app.command("run")` 整个函数体:

```python
@app.command("run")
def run(
    daemon: bool = typer.Option(False, "--daemon", help="启动 daemon(09:00 cron + FastAPI)"),
    today: bool = typer.Option(False, "--today", help="立即跑今天所有 pending 任务"),
    task_id: int | None = typer.Option(None, "--task-id", help="跑指定单条任务"),
    dry_run: bool = typer.Option(False, "--dry-run", help="发布步骤跑到点'发布'前停下"),
) -> None:
    """执行任务。三选一:--task-id 单条 / --today 跑今天 / --daemon 起 cron。"""
    settings = load_settings()

    if task_id is not None:
        typer.echo(f"[wxsp] 跑 task {task_id}{' (dry-run)' if dry_run else ''}...")
        try:
            result = publish(task_id, dry_run=dry_run, settings=settings)
        except AlreadyClaimed as exc:
            typer.echo(f"[wxsp] ✗ {exc}")
            raise typer.Exit(code=1) from exc

        if result.ok:
            typer.echo(f"[wxsp] ✓ task {task_id} {'dry-run 完成' if dry_run else '发布成功'}")
            if result.remote_url:
                typer.echo(f"        remote_url: {result.remote_url}")
            if result.screenshots:
                typer.echo(f"        screenshots: {', '.join(result.screenshots)}")
            return
        typer.echo(f"[wxsp] ✗ task {task_id} 失败: {result.error_type}")
        typer.echo(f"        {result.error_msg}")
        raise typer.Exit(code=1)

    if today:
        typer.echo("[wxsp] 跑今天所有 pending 任务...")
        summary = run_today_pending(settings)
        typer.echo(
            f"[wxsp] 完成: attempted={summary.attempted} succeeded={summary.succeeded} "
            f"failed={summary.failed} skipped_paused={summary.skipped_paused}"
        )
        if summary.failed > 0:
            raise typer.Exit(code=1)
        return

    if daemon:
        typer.echo("[wxsp] 启动 daemon(按 Ctrl-C 退出)...")
        try:
            start_daemon(settings)
        except (KeyboardInterrupt, SystemExit):
            typer.echo("[wxsp] daemon 退出")
        return

    typer.echo("[wxsp] 请指定 --task-id N / --today / --daemon 之一")
    raise typer.Exit(code=2)
```

- [ ] **Step 7.4: 跑测试验证 pass + 已有 run --task-id 测试不退化**

```bash
uv run pytest tests/test_cli_run.py -v
```
预期:全 pass。

- [ ] **Step 7.5: Commit**

```bash
git add wxsp/cli.py tests/test_cli_run.py
git commit -m "$(cat <<'EOF'
feat(cli): wire run --today / --daemon to scheduler

--today 跑 run_today_pending,echo summary(failed>0 退出 1)
--daemon 跑 start_daemon(阻塞)
--task-id 行为不变(M5)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: 跑全量回归 + M6 验收打勾 + 文档更新

**Files:**
- Modify: `docs/superpowers/specs/2026-05-12-wxsp-design.md`

- [ ] **Step 8.1: 跑全量回归**

```bash
uv run pytest -v
```
预期:全 pass(integration 1 skip)。如有失败立刻修。

- [ ] **Step 8.2: 跑 lint/format**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy wxsp tests
```
预期:全绿;有问题就修。

- [ ] **Step 8.3: 手动验收 4 项**

逐项跑(不需要真账号,因为我们要测的是"调度连线",不是"真发"):

```bash
# 1) wxsp sync 只 sync 不跑任务(无 task 入库的情况)
WXSP_DB_PATH=/tmp/m6_sanity.sqlite uv run wxsp sync
# 预期:打印 [wxsp] 飞书未启用,跳过 sync。 (config.yaml 未填飞书的情况下)

# 2) wxsp run --today(空 DB):
WXSP_DB_PATH=/tmp/m6_sanity.sqlite uv run wxsp run --today
# 预期:attempted=0 succeeded=0 failed=0 skipped_paused=0

# 3) wxsp run --daemon 启动并被 Ctrl-C 优雅退出
# (新开终端,跑下面这条,然后 Ctrl-C)
WXSP_DB_PATH=/tmp/m6_sanity.sqlite uv run wxsp run --daemon
# 预期:打印"daemon 启动:每日 09:00 (Asia/Shanghai) 跑 run_today_pending",
# Ctrl-C 后打印 "daemon 退出"。
```

第 4 项("kill 后重启 running→interrupted")通过 unit test `test_mark_stale_running_as_interrupted_only_touches_expired_running` 已覆盖,不必再手动构造。

- [ ] **Step 8.4: 更新 design doc**

把 `docs/superpowers/specs/2026-05-12-wxsp-design.md` 中 M6 行末尾从

> | **M6** | ... | 1.5d |

改成:

> | **M6** | ... | 1.5d | **M6 完成 (2026-05-13)** |

(对照 M5 行的标注样式)

- [ ] **Step 8.5: Commit**

```bash
git add docs/superpowers/specs/2026-05-12-wxsp-design.md
git commit -m "$(cat <<'EOF'
chore: mark M6 acceptance complete

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**Spec coverage(M6 验收 §7):**

| 验收点 | 实现在 | Task |
|---|---|---|
| 09:00 cron 触发时先 sync 飞书再扫 today pending 入队 | `run_today_pending` + `make_scheduler` 注册它为 cron job | Task 5 + 6 |
| 手动 `wxsp run --today` 等效 | CLI `run --today` 直接调 `run_today_pending` | Task 7 |
| `wxsp sync` 只拉飞书不跑任务 | CLI `sync` 调 `sync_now`,不调 `run_today_pending` | Task 2 |
| kill 后重启 running→interrupted | `mark_stale_running_as_interrupted` 在 `start_daemon` 启动时跑 | Task 4 + 6 |
| 暂停账号自动跳过(CLAUDE.md §1 / 模型注释) | `run_today_pending` 读 `paused_until` 跳过 | Task 5 |
| 单 worker 串行 | `run_today_pending` 顺序 for-loop | Task 5 |
| 无 polling | 只用 `CronTrigger`,不用 `IntervalTrigger` | Task 6 |

**Placeholder scan:** 通读各 Task,所有代码块都是完整可粘贴的;没有"按 X 类比"、"添加适当 X"这种留白。

**Type consistency:**
- `SyncResult` 字段:`pulled / accepted / rejected / skipped_existing / rejected_details` —— 跨 Task 1/2 一致
- `RunSummary` 字段:`attempted / succeeded / failed / skipped_paused` —— Task 5/7/8 一致
- `queue_today(session)` 返回 `list[int]` —— Task 3 定义,Task 5 调用一致
- `mark_stale_running_as_interrupted(session, *, now)` —— Task 4 定义,Task 6 `start_daemon` 调用一致
- `run_today_pending(settings)` —— Task 5 定义,Task 7 CLI / Task 6 cron 调用一致
- `make_scheduler(settings, *, blocking=False)` —— Task 6 定义,Task 6 `start_daemon(blocking=True)` 调用一致

无遗漏。
