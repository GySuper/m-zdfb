"""CLI 层 autostart 子命令测试(monkeypatch autostart 模块,不触碰 launchctl/schtasks)。"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from wxsp.cli import app

runner = CliRunner()


def test_autostart_status_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wxsp.autostart.is_autostart_enabled", lambda: False)
    result = runner.invoke(app, ["autostart", "status"])
    assert result.exit_code == 1
    assert "未启用" in result.stdout


def test_autostart_status_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wxsp.autostart.is_autostart_enabled", lambda: True)
    result = runner.invoke(app, ["autostart", "status"])
    assert result.exit_code == 0
    assert "已启用" in result.stdout


def test_autostart_enable_calls_module(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr("wxsp.autostart.enable_autostart", lambda: called.append(True))
    result = runner.invoke(app, ["autostart", "enable"])
    assert result.exit_code == 0
    assert called == [True]
