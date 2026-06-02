"""平台元数据"单一信息源"回归测试。

收敛前 config / notify / validator / browser / setup 各自硬编码一份 per-platform map,
加平台要改 5 处、漏一处运行时才暴露。收敛后这些消费方都从 wxsp.platform_meta.REGISTRY 读。

本测试通过往 REGISTRY 注入一个假平台,断言消费方**调用时**就能反映出来 —— 证明它们
确实读 REGISTRY 而非各自的本地 map。收敛前会失败(本地 map 不认识假平台)。
"""

from __future__ import annotations

import pytest

from wxsp import platform_meta as pm

_FAKE = pm.PlatformMeta(
    key="fake_x",
    label="测试平台",
    title_min=7,
    login_meta={"home_url": "https://x.test", "mode": "url", "login_fragment": "login.x.test"},
    field_map_defaults={"topic": "假话题", "product_ids": "假商品"},
    needs_fingerprint=False,
)


@pytest.fixture
def fake_platform(monkeypatch: pytest.MonkeyPatch) -> pm.PlatformMeta:
    """临时把假平台塞进 REGISTRY,测完自动撤销。"""
    monkeypatch.setitem(pm.REGISTRY, "fake_x", _FAKE)
    return _FAKE


def test_config_label_reads_registry(fake_platform: pm.PlatformMeta) -> None:
    from wxsp.config import platform_label

    assert platform_label("fake_x") == "测试平台"


def test_notify_tag_reads_registry(fake_platform: pm.PlatformMeta) -> None:
    from wxsp.notify import _platform_tag

    assert _platform_tag("fake_x") == "测试平台"


def test_validator_title_min_reads_registry(fake_platform: pm.PlatformMeta) -> None:
    from wxsp.validator import _title_min_for

    assert _title_min_for("fake_x") == 7


def test_browser_login_meta_reads_registry(fake_platform: pm.PlatformMeta) -> None:
    from wxsp.browser import login_meta_for

    assert login_meta_for("fake_x") == _FAKE.login_meta


def test_setup_field_map_reads_registry(fake_platform: pm.PlatformMeta) -> None:
    from wxsp.api.routes_setup import _field_map_for

    fm = _field_map_for("fake_x")
    # 共有字段保留 + 平台特有默认值并入
    assert fm["title"] == "标题"
    assert fm["topic"] == "假话题"
    assert fm["product_ids"] == "假商品"


def test_i18n_platform_cn_reads_registry(fake_platform: pm.PlatformMeta) -> None:
    from wxsp.api.i18n import platform_cn

    assert platform_cn("fake_x") == "测试平台"


def test_config_form_field_keys_read_registry(fake_platform: pm.PlatformMeta) -> None:
    from wxsp.api.routes_config import _field_map_keys

    keys = _field_map_keys("fake_x")
    # 共有字段在前 + 平台特有默认值并入(保持 registry 声明顺序)
    assert ("title", "标题") in keys
    assert ("topic", "假话题") in keys
    assert ("product_ids", "假商品") in keys


def test_existing_platforms_consistent_across_consumers() -> None:
    """防漂移:已上线平台在 config / notify 两个消费方与 REGISTRY 标签一致。"""
    from wxsp.config import platform_label
    from wxsp.notify import _platform_tag

    for key, meta in pm.REGISTRY.items():
        assert platform_label(key) == meta.label
        assert _platform_tag(key) == meta.label
