"""CLI `wxsp doctor` tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session
from typer.testing import CliRunner

from wxsp.cli import app
from wxsp.db import get_engine, init_db
from wxsp.models import Account


@pytest.fixture
def db_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    return db_path


def _add_account(db_path: Path, account_id: str) -> None:
    engine = get_engine(db_path)
    init_db(engine)
    with Session(engine) as session:
        session.add(
            Account(
                id=account_id,
                display_name=f"d-{account_id}",
                user_data_dir=f"/tmp/profiles/{account_id}",
            )
        )
        session.commit()


def test_doctor_no_accounts_shows_hint(db_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "无账号" in result.output


def test_doctor_lists_each_account_with_status(
    db_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_account(db_env, "account_a")
    _add_account(db_env, "account_b")

    calls: list[Path] = []

    def fake_check(path: Path, *, timeout_ms: int) -> bool:
        calls.append(path)
        assert timeout_ms <= 30_000, "doctor should use a short timeout (already-logged-in path)"
        return path.name == "account_a"  # only A is logged in

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", fake_check)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output

    # both accounts appear in output, with their status
    assert "account_a" in result.output
    assert "account_b" in result.output
    assert "ok" in result.output
    assert "expired" in result.output

    # checker was called once per account, with the right path
    assert sorted(str(p) for p in calls) == [
        "/tmp/profiles/account_a",
        "/tmp/profiles/account_b",
    ]


def test_doctor_persists_cookie_status_to_db(db_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _add_account(db_env, "account_a")

    def fake_check(path: Path, *, timeout_ms: int) -> bool:
        return True

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", fake_check)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0

    engine = get_engine(db_env)
    with Session(engine) as session:
        account = session.get(Account, "account_a")
        assert account is not None
        assert account.cookie_status == "ok"
        assert account.cookie_last_active_at is not None


def test_doctor_continues_after_one_account_browser_crash(
    db_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_account(db_env, "account_a")
    _add_account(db_env, "account_b")

    def fake_check(path: Path, *, timeout_ms: int) -> bool:
        if path.name == "account_a":
            raise RuntimeError("simulated crash")
        return True

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", fake_check)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0

    engine = get_engine(db_env)
    with Session(engine) as session:
        a = session.get(Account, "account_a")
        b = session.get(Account, "account_b")
        assert a is not None and a.cookie_status == "unknown"
        assert b is not None and b.cookie_status == "ok"
