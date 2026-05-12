"""Tests for wxsp accounts add/list/pause/resume CLI commands."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlmodel import Session, select
from typer.testing import CliRunner

from wxsp.cli import app
from wxsp.db import get_engine, init_db
from wxsp.models import Account

runner = CliRunner()


@pytest.fixture()
def db_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "cli.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    return db_path


def test_accounts_add_creates_row(db_env: Path):
    result = runner.invoke(
        app,
        [
            "accounts",
            "add",
            "account_a",
            "--display-name",
            "美食号",
            "--user-data-dir",
            "./data/chrome-profiles/account_a",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "account_a" in result.stdout

    engine = get_engine(db_env)
    init_db(engine)
    with Session(engine) as session:
        row = session.exec(select(Account).where(Account.id == "account_a")).one()
    assert row.display_name == "美食号"
    assert row.daily_limit == 20  # default
    assert row.is_active is True


def test_accounts_add_honors_daily_limit_option(db_env: Path):
    result = runner.invoke(
        app,
        [
            "accounts",
            "add",
            "account_b",
            "--display-name",
            "健身号",
            "--user-data-dir",
            "./profiles/b",
            "--daily-limit",
            "30",
        ],
    )
    assert result.exit_code == 0

    engine = get_engine(db_env)
    init_db(engine)
    with Session(engine) as session:
        row = session.exec(select(Account).where(Account.id == "account_b")).one()
    assert row.daily_limit == 30


def test_accounts_add_duplicate_id_fails(db_env: Path):
    first = runner.invoke(
        app,
        [
            "accounts",
            "add",
            "account_a",
            "--display-name",
            "美食号",
            "--user-data-dir",
            "./profiles/a",
        ],
    )
    assert first.exit_code == 0

    second = runner.invoke(
        app,
        [
            "accounts",
            "add",
            "account_a",
            "--display-name",
            "另一个",
            "--user-data-dir",
            "./profiles/a2",
        ],
    )
    assert second.exit_code != 0
    assert "已存在" in second.stdout or "exists" in second.stdout.lower()


def test_accounts_list_empty(db_env: Path):
    result = runner.invoke(app, ["accounts", "list"])
    assert result.exit_code == 0
    assert "无账号" in result.stdout or "no account" in result.stdout.lower()


def test_accounts_list_shows_rows(db_env: Path):
    runner.invoke(
        app,
        [
            "accounts",
            "add",
            "account_a",
            "--display-name",
            "美食号",
            "--user-data-dir",
            "./profiles/a",
        ],
    )
    runner.invoke(
        app,
        [
            "accounts",
            "add",
            "account_b",
            "--display-name",
            "健身号",
            "--user-data-dir",
            "./profiles/b",
        ],
    )

    result = runner.invoke(app, ["accounts", "list"])
    assert result.exit_code == 0
    assert "account_a" in result.stdout
    assert "account_b" in result.stdout
    assert "美食号" in result.stdout
    assert "健身号" in result.stdout


def test_accounts_pause_sets_paused_until(db_env: Path):
    runner.invoke(
        app,
        [
            "accounts",
            "add",
            "account_a",
            "--display-name",
            "美食号",
            "--user-data-dir",
            "./profiles/a",
        ],
    )

    before = datetime.now()
    result = runner.invoke(app, ["accounts", "pause", "account_a", "--hours", "2"])
    assert result.exit_code == 0

    engine = get_engine(db_env)
    init_db(engine)
    with Session(engine) as session:
        row = session.exec(select(Account).where(Account.id == "account_a")).one()
    assert row.paused_until is not None
    delta = (row.paused_until - before).total_seconds()
    # 允许 60s 漂移,但必须在 2h ± 60s 之间
    assert 7140 <= delta <= 7260, f"paused_until delta = {delta}s"


def test_accounts_pause_missing_account_fails(db_env: Path):
    result = runner.invoke(app, ["accounts", "pause", "no_such_id", "--hours", "1"])
    assert result.exit_code != 0
    assert "no_such_id" in result.stdout


def test_accounts_resume_clears_paused_until(db_env: Path):
    runner.invoke(
        app,
        [
            "accounts",
            "add",
            "account_a",
            "--display-name",
            "美食号",
            "--user-data-dir",
            "./profiles/a",
        ],
    )
    runner.invoke(app, ["accounts", "pause", "account_a", "--hours", "5"])

    result = runner.invoke(app, ["accounts", "resume", "account_a"])
    assert result.exit_code == 0

    engine = get_engine(db_env)
    init_db(engine)
    with Session(engine) as session:
        row = session.exec(select(Account).where(Account.id == "account_a")).one()
    assert row.paused_until is None


def test_accounts_resume_missing_account_fails(db_env: Path):
    result = runner.invoke(app, ["accounts", "resume", "no_such_id"])
    assert result.exit_code != 0
