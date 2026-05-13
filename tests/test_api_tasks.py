"""Tasks 路由(M8)冒烟测试 + 重试关键路径。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date as _date
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.conftest import make_settings
from wxsp.api import routes_tasks
from wxsp.api.app import create_app
from wxsp.api.deps import get_session, get_settings
from wxsp.config import AccountConfig, Settings
from wxsp.db import get_engine, init_db, session_scope
from wxsp.models import Account, Event, Task, Video


def _settings(tmp_path: Path) -> Settings:
    s = make_settings(tmp_path, tmp_path)
    s.accounts = {
        "account_a": AccountConfig(
            display_name="美食号", daily_limit=20, user_data_dir=tmp_path / "a"
        ),
    }
    return s


@pytest.fixture
def client_with_data(tmp_path: Path) -> Iterator[tuple[TestClient, _date]]:
    db_path = tmp_path / "db.sqlite"
    engine = get_engine(db_path)
    init_db(engine)
    settings = _settings(tmp_path)
    today = _date.today()
    with session_scope(engine) as s:
        s.add(
            Account(
                id="account_a",
                display_name="美食号",
                user_data_dir=str(tmp_path / "a"),
                daily_limit=20,
            )
        )
        s.add(
            Video(
                id="rec1",
                file_path="/x/v1.mp4",
                title="国庆短片",
                description="d",
                tags_json="[]",
                cover_path=None,
                topic=None,
                original_claim=False,
                file_hash=None,
                ingested_at=datetime.now(),
            )
        )
        s.add(
            Task(
                id=1,
                video_id="rec1",
                account_id="account_a",
                execute_date=today,
                publish_at=datetime(today.year, today.month, today.day, 18, 0),
                status="failed",
                attempts=1,
                last_error_type="upload_failed",
                last_error_msg="boom",
            )
        )
        s.add(
            Event(
                ts=datetime.now(),
                level="error",
                task_id=1,
                account_id="account_a",
                type="task_failed",
                message="发布失败",
                context_json="{}",
            )
        )

    app = create_app()

    def fake_get_session() -> Iterator[Session]:
        with session_scope(engine) as s:
            yield s

    app.dependency_overrides[get_session] = fake_get_session
    app.dependency_overrides[get_settings] = lambda: settings

    with TestClient(app) as c:
        yield c, today


def test_tasks_list_renders_today_failed_row(
    client_with_data: tuple[TestClient, _date],
) -> None:
    c, today = client_with_data
    r = c.get(f"/tasks?date={today}")
    assert r.status_code == 200
    assert "国庆短片" in r.text
    assert "account_a" in r.text
    assert "upload_failed" in r.text
    assert "重试" in r.text  # failed → retryable 按钮可见


def test_tasks_filter_by_status_excludes_others(
    client_with_data: tuple[TestClient, _date],
) -> None:
    c, today = client_with_data
    r = c.get(f"/tasks?date={today}&status=success")
    assert r.status_code == 200
    assert "国庆短片" not in r.text  # 是 failed,被 status=success 过滤掉


def test_task_detail_shows_event_and_error(
    client_with_data: tuple[TestClient, _date],
) -> None:
    c, _ = client_with_data
    r = c.get("/tasks/1")
    assert r.status_code == 200
    assert "国庆短片" in r.text
    assert "upload_failed" in r.text
    assert "task_failed" in r.text  # event type
    assert "发布失败" in r.text


def test_task_detail_404(client_with_data: tuple[TestClient, _date]) -> None:
    c, _ = client_with_data
    assert c.get("/tasks/999").status_code == 404


def test_retry_resets_to_pending_and_spawns(
    client_with_data: tuple[TestClient, _date],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, _ = client_with_data
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        routes_tasks,
        "_spawn",
        lambda name, fn, *a, **kw: calls.append((name, a[0])) if a else None,
    )

    r = c.post("/tasks/1/retry", follow_redirects=False)
    assert r.status_code == 303
    assert calls == [("retry", 1)]

    engine = get_engine(tmp_path / "db.sqlite")
    with session_scope(engine) as s:
        t = s.get(Task, 1)
        assert t is not None
        assert t.status == "pending"
        assert t.last_error_type is None
        assert t.last_error_msg is None
        assert t.attempts == 1  # 不重置,审计累加


def test_retry_running_task_rejected(
    client_with_data: tuple[TestClient, _date],
    tmp_path: Path,
) -> None:
    c, _ = client_with_data
    engine = get_engine(tmp_path / "db.sqlite")
    with session_scope(engine) as s:
        t = s.get(Task, 1)
        assert t is not None
        t.status = "running"
        s.add(t)

    r = c.post("/tasks/1/retry", follow_redirects=False)
    assert r.status_code == 303
    # 跳回详情页 + flash,但状态仍是 running
    with session_scope(engine) as s:
        assert s.get(Task, 1).status == "running"  # type: ignore[union-attr]


def test_retry_unknown_task_404(client_with_data: tuple[TestClient, _date]) -> None:
    c, _ = client_with_data
    assert c.post("/tasks/999/retry").status_code == 404


def test_run_today_spawns_scheduler(
    client_with_data: tuple[TestClient, _date],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, _ = client_with_data
    calls: list[str] = []
    monkeypatch.setattr(routes_tasks, "_spawn", lambda name, fn, *a, **kw: calls.append(name))
    r = c.post("/tasks/run-today", follow_redirects=False)
    assert r.status_code == 303
    assert calls == ["run-today"]
