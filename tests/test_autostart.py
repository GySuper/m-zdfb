"""测试 autostart.py:模板渲染 + 平台 dispatch + subprocess 调用(mock)。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def test_render_macos_plist(tmp_path: Path) -> None:
    from wxsp.autostart import _render_macos_plist

    rendered = _render_macos_plist(
        install_dir=tmp_path,
        wxsp_bin=Path("/Applications/wxsp.app/Contents/MacOS/wxsp"),
    )
    assert "__INSTALL_DIR__" not in rendered
    assert "__WXSP_BIN__" not in rendered
    assert str(tmp_path) in rendered
    assert "/Applications/wxsp.app/Contents/MacOS/wxsp" in rendered
    # 关键:3 段 ProgramArguments,不是 5 段
    assert rendered.count("<string>") >= 5  # bin + run + --daemon + 其它 string 字段


def test_render_windows_xml(tmp_path: Path) -> None:
    from wxsp.autostart import _render_windows_xml

    rendered = _render_windows_xml(
        install_dir=tmp_path,
        wxsp_bin=Path("C:/Program Files/wxsp/wxsp.exe"),
        username="alice",
    )
    assert "__INSTALL_DIR__" not in rendered
    assert "__WXSP_BIN__" not in rendered
    assert "__USERNAME__" not in rendered
    assert str(tmp_path) in rendered
    assert "alice" in rendered
    assert "<Arguments>run --daemon</Arguments>" in rendered


def test_enable_autostart_macos_calls_launchctl(tmp_path, monkeypatch) -> None:
    from wxsp import autostart

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(autostart, "_user_install_dir", lambda: tmp_path)
    monkeypatch.setattr(autostart, "_wxsp_bin", lambda: Path("/fake/wxsp"))
    monkeypatch.setattr(autostart, "_launch_agent_path", lambda: tmp_path / "com.wxsp.daemon.plist")

    fake_uid = 501
    monkeypatch.setattr("os.getuid", lambda: fake_uid, raising=False)
    mock_run = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(subprocess, "run", mock_run)

    autostart.enable_autostart()

    plist_path = tmp_path / "com.wxsp.daemon.plist"
    assert plist_path.exists()
    assert "/fake/wxsp" in plist_path.read_text()
    mock_run.assert_called_once()
    call_args = mock_run.call_args[0][0]
    assert call_args[0] == "launchctl"
    assert call_args[1] == "bootstrap"
    assert f"gui/{fake_uid}" in call_args[2]


def test_enable_autostart_windows_calls_schtasks(tmp_path, monkeypatch) -> None:
    from wxsp import autostart

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(autostart, "_user_install_dir", lambda: tmp_path)
    monkeypatch.setattr(autostart, "_wxsp_bin", lambda: Path("C:/wxsp/wxsp.exe"))
    monkeypatch.setattr("os.getlogin", lambda: "alice")

    mock_run = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(subprocess, "run", mock_run)

    autostart.enable_autostart()

    # subprocess 至少被调用一次 schtasks /Create
    assert mock_run.called
    create_calls = [c for c in mock_run.call_args_list if "schtasks" in str(c.args[0][0]).lower()]
    assert len(create_calls) >= 1
    cmd = create_calls[0].args[0]
    assert "/Create" in cmd
    assert "wxsp-daemon" in cmd


def test_disable_autostart_macos(tmp_path, monkeypatch) -> None:
    from wxsp import autostart

    monkeypatch.setattr(sys, "platform", "darwin")
    plist_path = tmp_path / "com.wxsp.daemon.plist"
    plist_path.write_text("<plist/>")
    monkeypatch.setattr(autostart, "_launch_agent_path", lambda: plist_path)
    monkeypatch.setattr("os.getuid", lambda: 501, raising=False)
    monkeypatch.setattr(
        subprocess, "run", MagicMock(return_value=subprocess.CompletedProcess([], 0))
    )

    autostart.disable_autostart()

    assert not plist_path.exists()


def test_is_autostart_enabled_mac_query(monkeypatch) -> None:
    from wxsp import autostart

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("os.getuid", lambda: 501, raising=False)
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(return_value=subprocess.CompletedProcess([], 0)),
    )
    assert autostart.is_autostart_enabled() is True

    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(return_value=subprocess.CompletedProcess([], 1)),
    )
    assert autostart.is_autostart_enabled() is False


def test_enable_autostart_unsupported_platform(monkeypatch) -> None:
    from wxsp import autostart

    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(autostart.AutostartError, match="不支持"):
        autostart.enable_autostart()
