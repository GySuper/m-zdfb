"""快手平台接入回归:REGISTRY 元数据 / 路由 / Spec 接线(纯结构,不碰浏览器)。"""

from __future__ import annotations


def test_kuaishou_registered_in_registry() -> None:
    from wxsp.platform_meta import ALL_PLATFORMS, get_meta

    m = get_meta("kuaishou")
    assert m.key == "kuaishou"
    assert m.label == "快手"
    assert m.title_min == 1
    assert m.needs_fingerprint is False
    # 快手用 tags(→话题标签)+ cover;这俩不在公共集里,放 field_map_defaults
    assert m.field_map_defaults == {"tags": "标签", "cover": "封面文件"}
    # 未登录访问上传页会重定向到 passport,按 URL 片段判定(同淘宝 url 模式)
    assert m.login_meta["mode"] == "url"
    assert m.login_meta["login_fragment"] == "passport.kuaishou.com"
    assert "cp.kuaishou.com" in m.login_meta["home_url"]
    assert "kuaishou" in ALL_PLATFORMS


def test_kuaishou_title_min_via_validator() -> None:
    from wxsp.validator import _title_min_for

    assert _title_min_for("kuaishou") == 1


def test_kuaishou_field_map_has_fields_the_adapter_uses() -> None:
    from wxsp.api.routes_setup import _field_map_for

    fm = _field_map_for("kuaishou")
    assert fm["title"] == "标题"
    assert fm["video_file"] == "视频文件"
    assert fm["publish_at"] == "定时发布时间"
    # adapter 用 tags(_add_tags)+ cover(_set_cover),字段映射必须带上
    assert fm["tags"] == "标签"
    assert fm["cover"] == "封面文件"
    # 不该混入其它平台特有字段(视频号的合集/原创、淘宝的商品ID/声明等)
    assert "product_ids" not in fm
    assert "topic" not in fm
    assert "original_claim" not in fm


def test_kuaishou_routing_returns_kuaishou_publisher() -> None:
    from wxsp.platforms.kuaishou import KuaishouPublisher
    from wxsp.publisher import _get_publisher

    assert isinstance(_get_publisher("kuaishou"), KuaishouPublisher)


def test_kuaishou_spec_wiring() -> None:
    from wxsp.platforms.kuaishou import KUAISHOU_SPEC, _post_publish, _pre_publish

    assert KUAISHOU_SPEC.platform_key == "kuaishou"
    assert KUAISHOU_SPEC.display_name == "快手"
    assert KUAISHOU_SPEC.pre_publish is _pre_publish
    assert KUAISHOU_SPEC.post_publish is _post_publish
