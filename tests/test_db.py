"""Tests for wxsp.db — engine / session / transition_task / claim_task."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, select

from wxsp.db import claim_task, get_engine, init_db, transition_task
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
        except BaseException as exc:  # — collect across threads
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
