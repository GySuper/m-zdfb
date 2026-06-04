"""测试共享 fixture / 工厂。"""

from __future__ import annotations

from pathlib import Path

import pytest

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


@pytest.fixture(autouse=True)
def _isolate_default_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离机器全局的 data/default_platform 文件,固定指向 tencent_channel。

    该文件存的是运营在 Web UI 选的「当前平台」(运行时状态,非配置)。API 路由测试
    不 chdir,platform_context 中间件会读到真实仓库根的这个文件;开发机若把默认平台
    切到 douyin/taobao,无 ?platform= 的 GET 就被重定向到那个平台,导致按平台过滤的
    渲染断言(账号卡片等)失配。固定到 tencent_channel 让测试与开发机的 UI 选择无关。
    """
    p = tmp_path / "_default_platform"
    p.write_text("tencent_channel", encoding="utf-8")
    monkeypatch.setattr("wxsp.config._default_platform_path", lambda: p)


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
