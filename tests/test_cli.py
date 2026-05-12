"""Tests for wxsp.cli command skeleton."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from wxsp.cli import app

runner = CliRunner()


@pytest.mark.parametrize(
    "command",
    ["login", "accounts", "doctor", "sync", "run", "status", "logs", "web"],
)
def test_top_level_command_exists(command: str) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert command in result.stdout


@pytest.mark.parametrize("sub", ["list", "pause", "resume"])
def test_accounts_subcommand_exists(sub: str) -> None:
    result = runner.invoke(app, ["accounts", "--help"])
    assert result.exit_code == 0
    assert sub in result.stdout


def test_run_supports_today_flag() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--today" in result.stdout
    assert "--daemon" in result.stdout
    assert "--task-id" in result.stdout
    assert "--dry-run" in result.stdout


def test_doctor_runs() -> None:
    result = runner.invoke(app, ["doctor"])
    # 骨架阶段允许打印"未实现"提示并以 exit code 0 退出
    assert result.exit_code == 0
