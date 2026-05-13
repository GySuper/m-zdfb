"""`wxsp run --task-id` CLI 测试 —— mock publisher.publish。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from wxsp.cli import app
from wxsp.publisher import AlreadyClaimed, PublishResult


def test_run_task_id_success_prints_ok_and_exits_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("WXSP_DB_PATH", str(tmp_path / "db.sqlite"))
    fake_result = PublishResult(
        task_id=7,
        ok=True,
        dry_run=False,
        remote_url="https://x",
        remote_video_id="vid",
    )
    with (
        patch("wxsp.cli.publish", return_value=fake_result) as p,
        patch("wxsp.cli.load_settings", return_value=MagicMock()),
    ):
        result = CliRunner().invoke(app, ["run", "--task-id", "7"])
    assert result.exit_code == 0, result.stdout
    assert "task 7" in result.stdout.lower() or "成功" in result.stdout
    p.assert_called_once()
    _args, kwargs = p.call_args
    assert kwargs.get("dry_run") is False


def test_run_task_id_dry_run_passes_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("WXSP_DB_PATH", str(tmp_path / "db.sqlite"))
    fake_result = PublishResult(task_id=7, ok=True, dry_run=True, screenshots=["/x/y.png"])
    with (
        patch("wxsp.cli.publish", return_value=fake_result) as p,
        patch("wxsp.cli.load_settings", return_value=MagicMock()),
    ):
        result = CliRunner().invoke(app, ["run", "--task-id", "7", "--dry-run"])
    assert result.exit_code == 0, result.stdout
    _, kwargs = p.call_args
    assert kwargs["dry_run"] is True


def test_run_task_id_failed_exits_one(tmp_path, monkeypatch):
    monkeypatch.setenv("WXSP_DB_PATH", str(tmp_path / "db.sqlite"))
    fake_result = PublishResult(
        task_id=7,
        ok=False,
        dry_run=False,
        error_type="cookie_expired",
        error_msg="step=login: cookie",
    )
    with (
        patch("wxsp.cli.publish", return_value=fake_result),
        patch("wxsp.cli.load_settings", return_value=MagicMock()),
    ):
        result = CliRunner().invoke(app, ["run", "--task-id", "7"])
    assert result.exit_code == 1
    assert "cookie_expired" in result.stdout


def test_run_already_claimed_exits_one(tmp_path, monkeypatch):
    monkeypatch.setenv("WXSP_DB_PATH", str(tmp_path / "db.sqlite"))
    with (
        patch("wxsp.cli.publish", side_effect=AlreadyClaimed("已占用")),
        patch("wxsp.cli.load_settings", return_value=MagicMock()),
    ):
        result = CliRunner().invoke(app, ["run", "--task-id", "7"])
    assert result.exit_code == 1
    assert "已占用" in result.stdout or "claimed" in result.stdout.lower()
