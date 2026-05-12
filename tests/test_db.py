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
