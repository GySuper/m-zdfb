"""scheduler.py(M6)单元测试。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, select

from tests.conftest import make_settings
from wxsp.db import get_engine, init_db, session_scope
from wxsp.models import Account, Task, Video
from wxsp.publisher import PublishResult
from wxsp.scheduler import (
    RunSummary,
    mark_stale_running_as_interrupted,
    queue_today,
    run_today_pending,
)


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
        statuses = {t.video_id: t.status for t in session.exec(select(Task)).all()}
    assert statuses["v_running_expired"] == "interrupted"
    assert statuses["v_running_fresh"] == "running"
    assert statuses["v_pending"] == "pending"
    assert statuses["v_success"] == "success"


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

    monkeypatch.setattr("wxsp.scheduler.sync_now", lambda settings: None)
    monkeypatch.setattr("wxsp.scheduler.publish", fake_publish)

    summary: RunSummary = run_today_pending(settings)

    with Session(engine) as session:
        v2_task = session.exec(select(Task).where(Task.video_id == "v2")).first()
        v1_task = session.exec(select(Task).where(Task.video_id == "v1")).first()
        assert v2_task is not None and v1_task is not None
    assert call_order == [v2_task.id, v1_task.id]
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
        session.add(
            Account(
                id="a",
                display_name="A",
                user_data_dir="/tmp",
                daily_limit=20,
                paused_until=now + timedelta(hours=24),
            )
        )
        session.add(Video(id="v1", file_path="/tmp/v.mp4", title="x" * 16, ingested_at=now))
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

    def fake_publish(task_id, *, dry_run, settings):
        publish_calls.append(task_id)
        return PublishResult(task_id=task_id, ok=True, dry_run=False)

    monkeypatch.setattr("wxsp.scheduler.publish", fake_publish)

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
        first_id = calls[0]
        is_first = task_id == first_id
        return PublishResult(
            task_id=task_id,
            ok=not is_first,
            dry_run=False,
            error_type="network" if is_first else None,
            error_msg="boom" if is_first else None,
        )

    monkeypatch.setattr("wxsp.scheduler.publish", fake_publish)

    summary = run_today_pending(settings)

    assert len(calls) == 2
    assert summary.attempted == 2
    assert summary.succeeded == 1
    assert summary.failed == 1
