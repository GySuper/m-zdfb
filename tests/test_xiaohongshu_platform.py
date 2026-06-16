"""小红书平台接入回归:REGISTRY 元数据 / 路由 / Spec 接线(纯结构,不碰浏览器)。"""

from __future__ import annotations


def test_xiaohongshu_registered_in_registry() -> None:
    from wxsp.platform_meta import ALL_PLATFORMS, get_meta

    m = get_meta("xiaohongshu")
    assert m.key == "xiaohongshu"
    assert m.label == "小红书"
    assert m.title_min == 1
    assert m.needs_fingerprint is False
    # 小红书用 tags(→话题标签)+ cover;这俩不在公共集里,放 field_map_defaults
    assert m.field_map_defaults == {"tags": "标签", "cover": "封面文件"}
    # 未登录访问发布页会跳 /login,故用 url 模式(同淘宝)
    assert m.login_meta["mode"] == "url"
    assert m.login_meta["login_fragment"] == "creator.xiaohongshu.com/login"
    assert "creator.xiaohongshu.com" in m.login_meta["home_url"]
    assert "xiaohongshu" in ALL_PLATFORMS


def test_xiaohongshu_title_min_via_validator() -> None:
    from wxsp.validator import _title_min_for

    assert _title_min_for("xiaohongshu") == 1


def test_xiaohongshu_field_map_has_fields_the_adapter_uses() -> None:
    from wxsp.api.routes_setup import _field_map_for

    fm = _field_map_for("xiaohongshu")
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


def test_xiaohongshu_routing_returns_xiaohongshu_publisher() -> None:
    from wxsp.platforms.xiaohongshu import XiaohongshuPublisher
    from wxsp.publisher import _get_publisher

    assert isinstance(_get_publisher("xiaohongshu"), XiaohongshuPublisher)


def test_xiaohongshu_spec_wiring() -> None:
    from wxsp.platforms.xiaohongshu import XIAOHONGSHU_SPEC, _post_publish, _pre_publish

    assert XIAOHONGSHU_SPEC.platform_key == "xiaohongshu"
    assert XIAOHONGSHU_SPEC.display_name == "小红书"
    assert XIAOHONGSHU_SPEC.pre_publish is _pre_publish
    assert XIAOHONGSHU_SPEC.post_publish is _post_publish
