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
