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
        patch("wxsp.cli.load_settings", return_value=MagicMock()) as load_settings,
    ):
        result = CliRunner().invoke(app, ["run", "--task-id", "7"])
    assert result.exit_code == 0, result.stdout
    assert "task 7" in result.stdout.lower() or "成功" in result.stdout
    p.assert_called_once()
    _args, kwargs = p.call_args
    assert kwargs.get("dry_run") is False
    assert "settings" not in kwargs
    load_settings.assert_not_called()


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

    def fake_run(settings, **kwargs):
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


def test_run_today_no_platform_runs_each_configured_platform_with_own_settings(
    tmp_path, monkeypatch
):
    """`run --today` 不带 --platform 时,必须按平台各自的 settings 分别跑。

    回归 bug:旧代码用 load_settings(tencent) 这一份 settings 跑 queue_today(platform=None)
    返回的所有平台任务 → 淘宝任务用视频号配置(错的飞书表/账号)发布、回写。
    """
    import wxsp.config as cfg
    from wxsp.scheduler import RunSummary

    existing = tmp_path / "exists.yaml"
    existing.write_text("x")
    # 让"已配置平台"确定为全部(两个),与 cwd 是否有真配置文件解耦
    monkeypatch.setattr(cfg, "get_config_path", lambda p="tencent_channel": existing)

    calls: list[tuple[str | None, object]] = []

    def fake_run(settings, *, platform=None):
        calls.append((platform, settings))
        return RunSummary(attempted=1, succeeded=1, failed=0, skipped_paused=0)

    monkeypatch.setattr("wxsp.cli.run_today_pending", fake_run)
    monkeypatch.setattr(
        "wxsp.cli.load_settings", lambda platform="tencent_channel": f"S::{platform}"
    )

    result = CliRunner().invoke(app, ["run", "--today"])
    assert result.exit_code == 0, result.stdout
    # 每个平台各跑一次,且各用自己平台的 settings
    assert ("tencent_channel", "S::tencent_channel") in calls
    assert ("taobao_guanghe", "S::taobao_guanghe") in calls
    assert ("douyin", "S::douyin") in calls
    assert len(calls) == len(cfg.ALL_PLATFORMS)


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
    """打包模式下 `wxsp run --daemon` 应该用 uvicorn.run 主线程阻塞起 Web UI。

    cron 由 FastAPI lifespan 内的 BackgroundScheduler 注册(避免和独立的
    BlockingScheduler 重复),所以这里不再调用 start_daemon。
    """
    import sys
    from unittest.mock import MagicMock, patch

    from typer.testing import CliRunner

    main_module = sys.modules["__main__"]
    uvicorn_calls: list[tuple] = []

    def fake_uvicorn_run(*args, **kwargs):
        uvicorn_calls.append((args, kwargs))

    start_daemon_called: list[bool] = []
    monkeypatch.setattr("wxsp.cli.start_daemon", lambda s: start_daemon_called.append(True))
    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
    monkeypatch.setattr(
        "wxsp.cli.load_settings",
        lambda platform=None: MagicMock(
            webui=MagicMock(host="127.0.0.1", port=8765, open_browser_on_start=False),
        ),
    )

    with patch.object(main_module, "__compiled__", True, create=True):
        monkeypatch.delenv("WXSP_DEV_MODE", raising=False)
        from wxsp.cli import app as cli_app

        runner = CliRunner()
        result = runner.invoke(cli_app, ["run", "--daemon"])
        assert result.exit_code == 0
        assert len(uvicorn_calls) == 1
        assert uvicorn_calls[0][0] == ("wxsp.api.app:app",)
        assert uvicorn_calls[0][1]["host"] == "127.0.0.1"
        assert uvicorn_calls[0][1]["port"] == 8765
        # 不该再调 start_daemon(否则 BlockingScheduler 会和 lifespan 内的
        # BackgroundScheduler 重复注册 cron)
        assert start_daemon_called == []


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
        lambda platform=None: MagicMock(
            webui=MagicMock(host="127.0.0.1", port=8765, open_browser_on_start=True),
        ),
    )
    monkeypatch.delenv("WXSP_DEV_MODE", raising=False)

    from wxsp.cli import app as cli_app

    runner = CliRunner()
    result = runner.invoke(cli_app, ["run", "--daemon"])
    assert result.exit_code == 0
    assert "web-ui" not in started_threads
