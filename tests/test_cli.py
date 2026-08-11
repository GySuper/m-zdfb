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


# ============== Windows emoji 兼容(_force_utf8_stdout)==============


def test_force_utf8_stdout_calls_reconfigure_when_available() -> None:
    """有 .reconfigure 的 stream(真 TextIOWrapper)应该被强制 UTF-8 + replace。"""
    import sys

    from wxsp.cli import _force_utf8_stdout

    captured: list[tuple[str, str]] = []

    class FakeStream:
        def reconfigure(self, *, encoding: str, errors: str) -> None:
            captured.append((encoding, errors))

    fake_out, fake_err = FakeStream(), FakeStream()
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = fake_out, fake_err  # type: ignore[assignment]
    try:
        _force_utf8_stdout()
    finally:
        sys.stdout, sys.stderr = real_out, real_err

    # stdout + stderr 各一次,编码都是 utf-8,errors=replace
    assert captured == [("utf-8", "replace"), ("utf-8", "replace")]


def test_force_utf8_stdout_silent_when_stream_has_no_reconfigure() -> None:
    """StringIO 之类无 .reconfigure 的流不应该让函数 crash。"""
    import sys
    from io import StringIO

    from wxsp.cli import _force_utf8_stdout

    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = StringIO(), StringIO()  # 无 reconfigure 方法
    try:
        _force_utf8_stdout()  # 不应该抛
    finally:
        sys.stdout, sys.stderr = real_out, real_err


def test_force_utf8_stdout_silent_when_reconfigure_raises() -> None:
    """流已关闭等场景 reconfigure 抛异常 → 兜住,不让 cli 启动挂掉。"""
    import sys

    from wxsp.cli import _force_utf8_stdout

    class CrashingStream:
        def reconfigure(self, *, encoding: str, errors: str) -> None:
            raise ValueError("stream is detached")

    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = CrashingStream(), CrashingStream()  # type: ignore[assignment]
    try:
        _force_utf8_stdout()  # 不应该抛
    finally:
        sys.stdout, sys.stderr = real_out, real_err


def test_run_supports_today_flag() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--today" in result.stdout
    assert "--daemon" in result.stdout
    assert "--task-id" in result.stdout
    assert "--dry-run" in result.stdout


def test_webui_bind_host_must_be_loopback() -> None:
    from wxsp.cli import _validate_webui_host

    assert _validate_webui_host("127.0.0.1") == "127.0.0.1"
    assert _validate_webui_host("localhost") == "localhost"
    with pytest.raises(ValueError, match="仅允许监听本机"):
        _validate_webui_host("0.0.0.0")


def test_doctor_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # doctor 现在会读 settings.paths.{video,cover}_search_root 并检查存在性,
    # 测试不依赖开发机 config.yaml,monkeypatch load_settings 注入临时目录。
    from tests.conftest import make_settings

    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    settings = make_settings(video_root, cover_root)

    monkeypatch.setenv("WXSP_DB_PATH", str(tmp_path / "db.sqlite"))
    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "load_settings", lambda platform=None: settings)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
