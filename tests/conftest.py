"""测试共享 fixture / 工厂。"""

from __future__ import annotations

from pathlib import Path

from wxsp.config import (
    AppConfig,
    FeishuBitableConfig,
    FeishuConfig,
    MonitoringConfig,
    NotifiersConfig,
    PathsConfig,
    PublisherConfig,
    SchedulerConfig,
    Settings,
    WebUIConfig,
    WecomNotifierConfig,
)


def make_settings(video_root: Path, cover_root: Path) -> Settings:
    """构造一个最小可用 Settings(只关心 paths.{video,cover}_search_root)。

    feishu/wecom 都 disabled,doctor / cli_doctor 测试不需要它们;validator
    测试有自己的薄工厂(`tests/test_validator.py::_make_settings`),不走这里。
    """
    return Settings(
        app=AppConfig(data_dir=Path("/tmp/d"), logs_dir=Path("/tmp/l"), timezone="Asia/Shanghai"),
        paths=PathsConfig(
            nas_root=video_root.parent,
            video_search_root=video_root,
            cover_search_root=cover_root,
        ),
        accounts={},
        scheduler=SchedulerConfig(),
        publisher=PublisherConfig(),
        feishu=FeishuConfig(
            enabled=False,
            app_id="x",
            app_secret="x",
            bitable=FeishuBitableConfig(app_token="x", table_id="x"),
        ),
        monitoring=MonitoringConfig(
            notifiers=NotifiersConfig(wecom=WecomNotifierConfig(enabled=False, webhook="")),
        ),
        webui=WebUIConfig(),
    )
