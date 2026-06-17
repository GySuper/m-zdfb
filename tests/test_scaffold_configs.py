"""scaffold_missing_configs: 为已登记但无配置文件的平台生成空壳配置。

目标:更新新增平台后,启动自动补壳;每个平台页都能打开(不再 500),
凭证留空,由运营到 /config 填。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wxsp.config import (
    ALL_PLATFORMS,
    get_config_path,
    load_settings,
    scaffold_missing_configs,
)


def test_scaffold_creates_loadable_shell_for_every_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    created = scaffold_missing_configs()

    # 全新目录:所有已登记平台都应被补壳
    assert set(created) == set(ALL_PLATFORMS)
    for platform in ALL_PLATFORMS:
        assert get_config_path(platform).exists()
        # 空壳必须能通过 Settings 校验加载
        settings = load_settings(platform=platform)
        # 惰性:凭证为空、feishu/scheduler 禁用,不会拿空 token 去跑
        assert settings.feishu.enabled is False
        assert settings.feishu.app_id == ""
        assert settings.scheduler.enabled is False
        assert settings.accounts == {}


def test_scaffold_does_not_overwrite_existing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    target = ALL_PLATFORMS[0]
    existing = get_config_path(target)
    existing.write_text("# 真实配置,勿动\n", encoding="utf-8")

    created = scaffold_missing_configs()

    assert target not in created
    assert existing.read_text(encoding="utf-8") == "# 真实配置,勿动\n"


def test_scaffold_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    first = scaffold_missing_configs()
    second = scaffold_missing_configs()

    assert set(first) == set(ALL_PLATFORMS)
    assert second == []  # 第二次没有缺失的,啥也不建
