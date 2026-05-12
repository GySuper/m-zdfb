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
