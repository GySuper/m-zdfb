"""Tests for wxsp.cli command skeleton."""

from __future__ import annotations

from pathlib import Path

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


def test_doctor_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # doctor 现在会读 settings.paths.{video,cover}_search_root 并检查存在性,
    # 测试不依赖开发机 config.yaml,monkeypatch load_settings 注入临时目录。
    from tests.test_cli_doctor import _make_settings_for_cli

    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    settings = _make_settings_for_cli(video_root, cover_root)

    monkeypatch.setenv("WXSP_DB_PATH", str(tmp_path / "db.sqlite"))
    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "load_settings", lambda: settings)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
