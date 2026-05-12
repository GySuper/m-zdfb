"""Tests for wxsp.config."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from wxsp.config import Settings, load_settings


@pytest.fixture
def minimal_yaml() -> str:
    """A minimal valid config yaml as a string."""
    return dedent(
        """
        app:
          data_dir: ./data
          logs_dir: ./logs
          timezone: Asia/Shanghai
        paths:
          nas_root: /Volumes/NAS/wxsp
          video_search_root: "{nas_root}/videos"
          cover_search_root: "{nas_root}/covers"
        accounts:
          account_a:
            display_name: 测试号
            enabled: true
            daily_limit: 20
            user_data_dir: ./data/chrome-profiles/account_a
        scheduler:
          daily_cron_hour: 9
          daily_cron_minute: 0
          strategy: round-robin
        publisher:
          headless: false
          upload_timeout_seconds: 600
          step_pause_seconds: [1, 3]
          screenshot_on_error: true
          max_concurrent_accounts: 1
        feishu:
          enabled: true
          app_id: cli_xxx
          app_secret: ${TEST_FEISHU_SECRET}
          bitable:
            app_token: tok_xxx
            table_id: tbl_xxx
          field_map:
            video_file: 视频文件
            title: 标题
            description: 描述
            tags: 标签
            cover: 封面文件
            topic: 合集
            original_claim: 原创
            account: 账号
            execute_date: 执行日期
            publish_at: 定时发布时间
            status: 状态
            remote_url: 已发布链接
            error_message: 错误信息
          sync:
            write_back_enabled: true
        monitoring:
          cookie_warn_days: 1.5
          notifiers:
            wecom:
              enabled: true
              webhook: ${TEST_WECOM_WEBHOOK}
          notify_on:
            - task_failed
            - risk_control
        webui:
          host: 127.0.0.1
          port: 8765
          open_browser_on_start: true
        """
    ).strip()


def test_load_valid_yaml(
    tmp_path: Path, minimal_yaml: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_FEISHU_SECRET", "secret_value")
    monkeypatch.setenv("TEST_WECOM_WEBHOOK", "https://example.com/wecom")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(minimal_yaml, encoding="utf-8")

    settings = load_settings(config_path)

    assert isinstance(settings, Settings)
    assert settings.app.timezone == "Asia/Shanghai"
    assert settings.publisher.max_concurrent_accounts == 1
    assert "account_a" in settings.accounts
    assert settings.accounts["account_a"].display_name == "测试号"


def test_env_var_substitution(
    tmp_path: Path, minimal_yaml: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_FEISHU_SECRET", "expanded_secret")
    monkeypatch.setenv("TEST_WECOM_WEBHOOK", "https://expanded.example.com")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(minimal_yaml, encoding="utf-8")

    settings = load_settings(config_path)

    assert settings.feishu.app_secret == "expanded_secret"
    assert settings.monitoring.notifiers.wecom.webhook == "https://expanded.example.com"


def test_nas_root_template_substitution(
    tmp_path: Path, minimal_yaml: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_FEISHU_SECRET", "x")
    monkeypatch.setenv("TEST_WECOM_WEBHOOK", "x")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(minimal_yaml, encoding="utf-8")

    settings = load_settings(config_path)

    assert str(settings.paths.video_search_root) == "/Volumes/NAS/wxsp/videos"
    assert str(settings.paths.cover_search_root) == "/Volumes/NAS/wxsp/covers"


def test_missing_env_var_raises(
    tmp_path: Path, minimal_yaml: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_FEISHU_SECRET", raising=False)
    monkeypatch.delenv("TEST_WECOM_WEBHOOK", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(minimal_yaml, encoding="utf-8")

    with pytest.raises(ValueError, match="TEST_FEISHU_SECRET"):
        load_settings(config_path)


def test_invalid_yaml_raises_clear_error(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("not a valid yaml: : :", encoding="utf-8")

    with pytest.raises(yaml.YAMLError):
        load_settings(config_path)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "nonexistent.yaml")
