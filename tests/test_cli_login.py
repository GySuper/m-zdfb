"""CLI `wxsp login <account_id>` tests.

Browser is stubbed via monkeypatch — we never launch real Chromium under pytest.
The CLI is the orchestration layer; what we verify here is:
  - account-not-found path → exit 1, no DB write
  - successful login → cookie_status='ok' + cookie_last_active_at bumped
  - failed login → cookie_status='expired' + exit 1
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session
from typer.testing import CliRunner

from wxsp.cli import app
from wxsp.db import get_engine, init_db
from wxsp.models import (
    COOKIE_STATUS_EXPIRED,
    COOKIE_STATUS_OK,
    COOKIE_STATUS_UNKNOWN,
    Account,
)


@pytest.fixture
def db_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    return db_path


def _add_account(db_path: Path, account_id: str = "account_a") -> None:
    engine = get_engine(db_path)
    init_db(engine)
    with Session(engine) as session:
        session.add(
            Account(
                id=account_id,
                display_name=f"display-{account_id}",
                user_data_dir=f"/tmp/profiles/{account_id}",
            )
        )
        session.commit()


def test_login_unknown_account_exits_with_error(db_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["login", "missing"])

    assert result.exit_code != 0
    assert "不存在" in result.output


def test_login_success_marks_cookie_ok_and_bumps_last_active(
    db_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_account(db_env)
    captured: list[Path] = []

    def fake_check(
        path: Path,
        *,
        timeout_ms: int,
        account_id: str | None = None,
        platform: str = "tencent_channel",
    ) -> bool:
        captured.append(path)
        assert timeout_ms >= 60_000, "login must use a generous timeout for QR scan"
        return True

    # login 经 publisher → TencentChannelPublisher.login → wxsp.browser.check_cookie
    monkeypatch.setattr("wxsp.browser.check_cookie", fake_check)

    runner = CliRunner()
    result = runner.invoke(app, ["login", "account_a"])
    assert result.exit_code == 0, result.output

    # browser path was the str-converted user_data_dir
    assert captured == [Path("/tmp/profiles/account_a")]

    # DB state
    engine = get_engine(db_env)
    with Session(engine) as session:
        account = session.get(Account, "account_a")
        assert account is not None
        assert account.cookie_status == COOKIE_STATUS_OK
        assert account.cookie_last_active_at is not None
        assert account.cookie_last_checked_at is not None


def test_login_failure_marks_cookie_expired_and_exits_nonzero(
    db_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_account(db_env)

    def fake_check(
        path: Path,
        *,
        timeout_ms: int,
        account_id: str | None = None,
        platform: str = "tencent_channel",
    ) -> bool:
        return False  # simulated scan timeout

    # login 经 publisher → TencentChannelPublisher.login → wxsp.browser.check_cookie
    monkeypatch.setattr("wxsp.browser.check_cookie", fake_check)

    runner = CliRunner()
    result = runner.invoke(app, ["login", "account_a"])
    assert result.exit_code != 0

    engine = get_engine(db_env)
    with Session(engine) as session:
        account = session.get(Account, "account_a")
        assert account is not None
        assert account.cookie_status == COOKIE_STATUS_EXPIRED
        assert account.cookie_last_active_at is None
        assert account.cookie_last_checked_at is not None


def test_login_browser_crash_marks_cookie_unknown(
    db_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_account(db_env)

    def crashing_check(
        path: Path,
        *,
        timeout_ms: int,
        account_id: str | None = None,
        platform: str = "tencent_channel",
    ) -> bool:
        raise RuntimeError("simulated patchright crash")

    monkeypatch.setattr("wxsp.browser.check_cookie", crashing_check)

    runner = CliRunner()
    result = runner.invoke(app, ["login", "account_a"])
    assert result.exit_code != 0

    engine = get_engine(db_env)
    with Session(engine) as session:
        account = session.get(Account, "account_a")
        assert account is not None
        assert account.cookie_status == COOKIE_STATUS_UNKNOWN
        assert account.cookie_last_active_at is None


def test_login_outputs_chinese_success_message(
    db_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_account(db_env)

    def fake_check(
        path: Path,
        *,
        timeout_ms: int,
        account_id: str | None = None,
        platform: str = "tencent_channel",
    ) -> bool:
        return True

    # login 经 publisher → TencentChannelPublisher.login → wxsp.browser.check_cookie
    monkeypatch.setattr("wxsp.browser.check_cookie", fake_check)

    runner = CliRunner()
    result = runner.invoke(app, ["login", "account_a"])
    assert result.exit_code == 0
    assert "account_a" in result.output
    assert "登录成功" in result.output
