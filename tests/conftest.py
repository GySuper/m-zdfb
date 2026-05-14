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
    """构造一个最小可用 Settings。

    每账号都需要自己的 video/cover_search_root;但 make_settings 默认账号为空,
    单测里需要账号路径的(validator/sync)自己往 settings.accounts 加。
    保留 video_root/cover_root 参数兼容签名 —— 若需要,调用方手动注入到账号上。

    feishu/wecom 都 disabled;validator 测试有自己的薄工厂(`tests/test_validator.py::_make_settings`)。
    """
    _ = video_root, cover_root  # 旧签名兼容,实际不用(账号自己持路径)
    return Settings(
        app=AppConfig(data_dir=Path("/tmp/d"), logs_dir=Path("/tmp/l"), timezone="Asia/Shanghai"),
        paths=PathsConfig(nas_root=video_root.parent),
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
