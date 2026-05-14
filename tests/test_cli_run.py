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


def test_run_today_invokes_run_today_pending_and_echoes_summary(tmp_path, monkeypatch):
    """`wxsp run --today` 调 scheduler.run_today_pending,正常输出 summary。"""
    from wxsp.scheduler import RunSummary

    captured: dict[str, object] = {}

    def fake_run(settings):
        captured["called"] = True
        return RunSummary(attempted=3, succeeded=2, failed=1, skipped_paused=0)

    with (
        patch("wxsp.cli.run_today_pending", side_effect=fake_run),
        patch("wxsp.cli.load_settings", return_value=MagicMock()),
    ):
        result = CliRunner().invoke(app, ["run", "--today"])

    # failed>0 → exit 1
    assert result.exit_code == 1, result.stdout
    assert captured.get("called") is True
    assert "attempted=3" in result.stdout
    assert "succeeded=2" in result.stdout
    assert "failed=1" in result.stdout


def test_run_today_all_success_exits_zero(tmp_path, monkeypatch):
    from wxsp.scheduler import RunSummary

    with (
        patch(
            "wxsp.cli.run_today_pending",
            return_value=RunSummary(attempted=2, succeeded=2, failed=0, skipped_paused=0),
        ),
        patch("wxsp.cli.load_settings", return_value=MagicMock()),
    ):
        result = CliRunner().invoke(app, ["run", "--today"])
    assert result.exit_code == 0, result.stdout


def test_run_no_flag_exits_two(tmp_path, monkeypatch):
    with patch("wxsp.cli.load_settings", return_value=MagicMock()):
        result = CliRunner().invoke(app, ["run"])
    assert result.exit_code == 2
    assert "--task-id" in result.stdout or "--today" in result.stdout


def test_run_daemon_in_packaged_mode_starts_web(monkeypatch) -> None:
    """打包模式下 `wxsp run --daemon` 应该额外开一个 uvicorn 线程。"""
    import sys
    from unittest.mock import MagicMock, patch

    from typer.testing import CliRunner

    main_module = sys.modules["__main__"]
    started_threads: list[str] = []

    real_thread = __import__("threading").Thread

    def fake_thread(*args, **kwargs):
        started_threads.append(kwargs.get("name", "unnamed"))
        t = real_thread(*args, **kwargs)
        return t

    monkeypatch.setattr("threading.Thread", fake_thread)
    monkeypatch.setattr("wxsp.cli.start_daemon", lambda s: None)
    monkeypatch.setattr(
        "wxsp.cli.load_settings",
        lambda: MagicMock(
            webui=MagicMock(host="127.0.0.1", port=8765, open_browser_on_start=False),
        ),
    )

    with patch.object(main_module, "__compiled__", True, create=True):
        monkeypatch.delenv("WXSP_DEV_MODE", raising=False)
        from wxsp.cli import app as cli_app

        runner = CliRunner()
        result = runner.invoke(cli_app, ["run", "--daemon"])
        assert result.exit_code == 0
        assert "web-ui" in started_threads


def test_run_daemon_in_dev_mode_does_not_start_web(monkeypatch) -> None:
    """开发模式下 `wxsp run --daemon` 不应该开 uvicorn 线程。"""
    from unittest.mock import MagicMock

    from typer.testing import CliRunner

    started_threads: list[str] = []
    real_thread = __import__("threading").Thread

    def fake_thread(*args, **kwargs):
        started_threads.append(kwargs.get("name", "unnamed"))
        return real_thread(*args, **kwargs)

    monkeypatch.setattr("threading.Thread", fake_thread)
    monkeypatch.setattr("wxsp.cli.start_daemon", lambda s: None)
    monkeypatch.setattr(
        "wxsp.cli.load_settings",
        lambda: MagicMock(
            webui=MagicMock(host="127.0.0.1", port=8765, open_browser_on_start=True),
        ),
    )
    monkeypatch.delenv("WXSP_DEV_MODE", raising=False)

    from wxsp.cli import app as cli_app

    runner = CliRunner()
    result = runner.invoke(cli_app, ["run", "--daemon"])
    assert result.exit_code == 0
    assert "web-ui" not in started_threads
