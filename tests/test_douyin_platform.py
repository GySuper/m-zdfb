"""抖音平台接入回归:REGISTRY 元数据 / 路由 / Spec 接线(纯结构,不碰浏览器)。"""

from __future__ import annotations


def test_douyin_registered_in_registry() -> None:
    from wxsp.platform_meta import ALL_PLATFORMS, get_meta

    m = get_meta("douyin")
    assert m.key == "douyin"
    assert m.label == "抖音"
    assert m.title_min == 1
    assert m.needs_fingerprint is False
    assert m.field_map_defaults == {}
    assert m.login_meta["mode"] == "selector"
    assert "creator.douyin.com" in m.login_meta["home_url"]
    assert "douyin" in ALL_PLATFORMS


def test_douyin_title_min_via_validator() -> None:
    from wxsp.validator import _title_min_for

    assert _title_min_for("douyin") == 1


def test_douyin_field_map_only_common_fields() -> None:
    from wxsp.api.routes_setup import _field_map_for

    fm = _field_map_for("douyin")
    assert fm["title"] == "标题"
    assert fm["video_file"] == "视频文件"
    assert fm["publish_at"] == "定时发布时间"
    # 抖音无平台特有字段:不应混入 topic / product_ids / declaration 等
    assert "product_ids" not in fm
    assert "topic" not in fm


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
