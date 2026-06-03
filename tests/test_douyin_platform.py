"""抖音平台接入回归:REGISTRY 元数据 / 路由 / Spec 接线(纯结构,不碰浏览器)。"""

from __future__ import annotations


def test_douyin_registered_in_registry() -> None:
    from wxsp.platform_meta import ALL_PLATFORMS, get_meta

    m = get_meta("douyin")
    assert m.key == "douyin"
    assert m.label == "抖音"
    assert m.title_min == 1
    assert m.needs_fingerprint is False
    # 抖音用 tags(→话题标签)+ cover;这俩不在公共集里,放 field_map_defaults
    assert m.field_map_defaults == {"tags": "标签", "cover": "封面文件"}
    # 扫码后抖音落到 creator-micro/home(非上传页),按 URL 片段 + 登录文案消失判定
    assert m.login_meta["mode"] == "logged_in_url"
    assert "creator-micro/" in m.login_meta["logged_in_fragment"]
    assert "creator.douyin.com" in m.login_meta["home_url"]
    assert "douyin" in ALL_PLATFORMS


def test_douyin_title_min_via_validator() -> None:
    from wxsp.validator import _title_min_for

    assert _title_min_for("douyin") == 1


def test_douyin_field_map_has_fields_the_adapter_uses() -> None:
    from wxsp.api.routes_setup import _field_map_for

    fm = _field_map_for("douyin")
    assert fm["title"] == "标题"
    assert fm["video_file"] == "视频文件"
    assert fm["publish_at"] == "定时发布时间"
    # adapter 用 tags(_add_tags)+ cover(_set_cover),字段映射必须带上
    assert fm["tags"] == "标签"
    assert fm["cover"] == "封面文件"
    # 但不该混入其它平台特有字段(视频号的合集/原创、淘宝的商品ID/声明等)
    assert "product_ids" not in fm
    assert "topic" not in fm
    assert "original_claim" not in fm


def test_douyin_routing_returns_douyin_publisher() -> None:
    from wxsp.platforms.douyin import DouyinPublisher
    from wxsp.publisher import _get_publisher

    assert isinstance(_get_publisher("douyin"), DouyinPublisher)


def test_douyin_spec_wiring() -> None:
    from wxsp.platforms.douyin import DOUYIN_SPEC, _post_publish, _pre_publish

    assert DOUYIN_SPEC.platform_key == "douyin"
    assert DOUYIN_SPEC.display_name == "抖音"
    assert DOUYIN_SPEC.pre_publish is _pre_publish
    assert DOUYIN_SPEC.post_publish is _post_publish
