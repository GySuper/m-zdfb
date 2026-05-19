"""Tasks 路由(M8)冒烟测试 + 重试关键路径。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date as _date
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

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
            display_name="美食号",
            daily_limit=20,
            user_data_dir=tmp_path / "a",
            video_search_root=tmp_path / "videos",
            cover_search_root=tmp_path / "covers",
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
    assert "上传失败" in r.text  # i18n: upload_failed → 上传失败
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
    assert "上传失败" in r.text  # i18n: upload_failed
    assert "任务失败" in r.text  # i18n: event type=task_failed
    assert "发布失败" in r.text  # event.message 原文


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
    """飞书 disabled fixture:sync 阶段跳过,直接 spawn 发布循环;返回 ok 片段。"""
    c, _ = client_with_data
    calls: list[str] = []
    monkeypatch.setattr(routes_tasks, "_spawn", lambda name, fn, *a, **kw: calls.append(name))
    r = c.post("/tasks/run-today", follow_redirects=False)
    assert r.status_code == 200
    assert 'class="flash ok"' in r.text
    assert "飞书未启用" in r.text
    assert calls == ["run-today"]
    # 释放全局 _run_today_running(下一个测试可能要再点)
    routes_tasks._run_today_running = False


def test_run_today_sync_failure_aborts_publish(
    client_with_data: tuple[TestClient, _date],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """飞书 enabled + sync_now 抛异常 → 不 spawn 发布,返回 error 片段 + HX-Trigger。"""
    c, _ = client_with_data
    # 把 fixture 里的 settings 的 feishu 打开
    settings = c.app.dependency_overrides[get_settings]()  # type: ignore[attr-defined]
    settings.feishu.enabled = True

    calls: list[str] = []
    monkeypatch.setattr(routes_tasks, "_spawn", lambda name, fn, *a, **kw: calls.append(name))

    def _boom(*a: object, **kw: object) -> object:
        raise RuntimeError("fake feishu down")

    monkeypatch.setattr("wxsp.sync.sync_now", _boom)

    r = c.post("/tasks/run-today", follow_redirects=False)
    assert r.status_code == 200
    assert 'class="flash error"' in r.text
    assert "fake feishu down" in r.text
    # 验证 HX-Trigger 是 htmx 可解的 JSON 形状(前端 e.detail.title /
    # e.detail.detail 才取得到),不光是 substring 包含 opError。
    hx_raw = r.headers.get("HX-Trigger", "")
    hx = json.loads(hx_raw)
    assert "opError" in hx
    assert isinstance(hx["opError"], dict)
    assert hx["opError"]["title"] == "飞书同步失败"
    assert "fake feishu down" in hx["opError"]["detail"]
    assert calls == []  # 发布循环没被 spawn
    routes_tasks._run_today_running = False


# ============== 重新入队 + backlog 过滤(M9) ==============


def _seed_backlog_task(
    engine: Any,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    *,
    task_id: int,
    video_id: str,
    title: str,
    exec_date: _date,
    status: str,
) -> None:
    """单独建一条积压任务,跟 client_with_data 里的 task #1 不冲突。"""
    with session_scope(engine) as s:
        s.add(
            Video(
                id=video_id,
                file_path=f"/x/{video_id}.mp4",
                title=title,
                description=None,
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
                id=task_id,
                video_id=video_id,
                account_id="account_a",
                execute_date=exec_date,
                publish_at=datetime.combine(exec_date, datetime.min.time()),
                status=status,
            )
        )


def test_tasks_backlog_filter_shows_only_past_pending_and_interrupted(
    client_with_data: tuple[TestClient, _date],
    tmp_path: Path,
) -> None:
    c, today = client_with_data
    engine = get_engine(tmp_path / "db.sqlite")
    yesterday = today - timedelta(days=1)
    two_days_ago = today - timedelta(days=2)
    _seed_backlog_task(
        engine,
        tmp_path,
        task_id=2,
        video_id="rec2",
        title="昨天pending",
        exec_date=yesterday,
        status="pending",
    )
    _seed_backlog_task(
        engine,
        tmp_path,
        task_id=3,
        video_id="rec3",
        title="前天interrupted",
        exec_date=two_days_ago,
        status="interrupted",
    )
    _seed_backlog_task(
        engine,
        tmp_path,
        task_id=4,
        video_id="rec4",
        title="昨天成功",
        exec_date=yesterday,
        status="success",
    )

    r = c.get("/tasks?backlog=1")
    assert r.status_code == 200
    assert "昨天pending" in r.text
    assert "前天interrupted" in r.text
    assert "昨天成功" not in r.text  # success 不算积压
    assert "国庆短片" not in r.text  # client_with_data 里 task #1 是 today 的 failed


def test_requeue_changes_execute_date_to_today_and_resets_to_pending(
    client_with_data: tuple[TestClient, _date],
    tmp_path: Path,
) -> None:
    c, today = client_with_data
    engine = get_engine(tmp_path / "db.sqlite")
    yesterday = today - timedelta(days=1)
    _seed_backlog_task(
        engine,
        tmp_path,
        task_id=2,
        video_id="rec2",
        title="t",
        exec_date=yesterday,
        status="interrupted",
    )

    r = c.post("/tasks/2/requeue", follow_redirects=False)
    assert r.status_code == 303

    with session_scope(engine) as s:
        t = s.get(Task, 2)
        assert t is not None
        assert t.execute_date == today
        assert t.status == "pending"
        assert t.lease_token is None
        assert t.lease_expires_at is None


def test_requeue_rejects_when_already_today(
    client_with_data: tuple[TestClient, _date],
    tmp_path: Path,
) -> None:
    c, today = client_with_data
    engine = get_engine(tmp_path / "db.sqlite")
    _seed_backlog_task(
        engine,
        tmp_path,
        task_id=2,
        video_id="rec2",
        title="t",
        exec_date=today,
        status="pending",
    )

    r = c.post("/tasks/2/requeue", follow_redirects=False)
    assert r.status_code == 303
    with session_scope(engine) as s:
        t = s.get(Task, 2)
        assert t is not None
        assert t.execute_date == today  # 没变,确认幂等


def test_requeue_rejects_non_backlog_status(
    client_with_data: tuple[TestClient, _date],
    tmp_path: Path,
) -> None:
    c, today = client_with_data
    engine = get_engine(tmp_path / "db.sqlite")
    yesterday = today - timedelta(days=1)
    _seed_backlog_task(
        engine,
        tmp_path,
        task_id=2,
        video_id="rec2",
        title="t",
        exec_date=yesterday,
        status="success",
    )

    r = c.post("/tasks/2/requeue", follow_redirects=False)
    assert r.status_code == 303
    with session_scope(engine) as s:
        t = s.get(Task, 2)
        assert t is not None
        assert t.execute_date == yesterday  # 不动
        assert t.status == "success"


def test_requeue_unknown_task_404(client_with_data: tuple[TestClient, _date]) -> None:
    c, _ = client_with_data
    assert c.post("/tasks/999/requeue").status_code == 404
