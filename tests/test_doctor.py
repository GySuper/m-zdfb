"""doctor.record_cookie_check + doctor.refresh_cookie_status unit tests.

`refresh_cookie_status` accepts an injected `cookie_checker` callable so we
exercise it without ever launching a real browser.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine

from wxsp.models import (
    COOKIE_STATUS_EXPIRED,
    COOKIE_STATUS_OK,
    COOKIE_STATUS_UNKNOWN,
    Account,
)


@pytest.fixture
def engine() -> Any:
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine: Any) -> Any:
    with Session(engine) as s:
        yield s


def _add_account(session: Session, account_id: str = "account_a") -> Account:
    account = Account(
        id=account_id,
        display_name=f"display-{account_id}",
        user_data_dir=f"/tmp/profiles/{account_id}",
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def test_record_cookie_check_marks_ok_and_bumps_last_active(session: Session) -> None:
    from wxsp.doctor import record_cookie_check

    _add_account(session)
    now = datetime(2026, 5, 12, 14, 30, 0)

    record_cookie_check(session, "account_a", is_logged_in=True, now=now)
    session.commit()

    account = session.get(Account, "account_a")
    assert account is not None
    assert account.cookie_status == COOKIE_STATUS_OK
    assert account.cookie_last_checked_at == now
    assert account.cookie_last_active_at == now


def test_record_cookie_check_marks_expired_and_does_not_bump_last_active(session: Session) -> None:
    from wxsp.doctor import record_cookie_check

    earlier = datetime(2026, 5, 10, 12, 0, 0)
    account = _add_account(session)
    account.cookie_last_active_at = earlier
    session.add(account)
    session.commit()

    later = datetime(2026, 5, 12, 14, 30, 0)
    record_cookie_check(session, "account_a", is_logged_in=False, now=later)
    session.commit()

    account = session.get(Account, "account_a")
    assert account is not None
    assert account.cookie_status == COOKIE_STATUS_EXPIRED
    assert account.cookie_last_checked_at == later
    assert account.cookie_last_active_at == earlier  # unchanged


def test_record_cookie_check_marks_unknown_when_is_logged_in_is_none(session: Session) -> None:
    from wxsp.doctor import record_cookie_check

    _add_account(session)
    now = datetime(2026, 5, 12, 14, 30, 0)

    record_cookie_check(session, "account_a", is_logged_in=None, now=now)
    session.commit()

    account = session.get(Account, "account_a")
    assert account is not None
    assert account.cookie_status == COOKIE_STATUS_UNKNOWN
    assert account.cookie_last_checked_at == now
    assert account.cookie_last_active_at is None


def test_record_cookie_check_missing_account_raises(session: Session) -> None:
    from wxsp.doctor import record_cookie_check

    with pytest.raises(LookupError):
        record_cookie_check(session, "does_not_exist", is_logged_in=True, now=datetime.now())


def test_record_cookie_check_does_not_commit(session: Session) -> None:
    """Mirror transition_task: caller controls the transaction."""
    from wxsp.doctor import record_cookie_check

    _add_account(session)
    now = datetime(2026, 5, 12, 14, 30, 0)

    record_cookie_check(session, "account_a", is_logged_in=True, now=now)
    # rollback BEFORE caller commits — should undo
    session.rollback()

    account = session.get(Account, "account_a")
    assert account is not None
    assert account.cookie_status == "unknown"  # default, not "ok"
    assert account.cookie_last_active_at is None
