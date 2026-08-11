"""小红书平台接入回归:REGISTRY 元数据 / 路由 / Spec 接线(纯结构,不碰浏览器)。"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from wxsp.errors import ElementNotFound


def test_xiaohongshu_registered_in_registry() -> None:
    from wxsp.platform_meta import ALL_PLATFORMS, get_meta

    m = get_meta("xiaohongshu")
    assert m.key == "xiaohongshu"
    assert m.label == "小红书"
    assert m.title_min == 1
    assert m.needs_fingerprint is False
    # 小红书用 tags(→话题标签)+ cover;这俩不在公共集里,放 field_map_defaults
    assert m.field_map_defaults == {"tags": "标签", "cover": "封面文件"}
    # 登录态用 logged_in_url 正向判定:goto 登录页,已登录跳创作者中心 /new/* = 成功
    # (旧 url 负面判定「URL 不含 /login」会误判,详见 platform_meta 注释)
    assert m.login_meta["mode"] == "logged_in_url"
    assert m.login_meta["logged_in_fragment"] == "creator.xiaohongshu.com/new/"
    assert m.login_meta["home_url"] == "https://creator.xiaohongshu.com/login"
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


def test_xiaohongshu_schedule_missing_input_fails_closed() -> None:
    from wxsp.platforms.xiaohongshu import _set_schedule

    page = MagicMock()
    switch_card = MagicMock()
    schedule_input = MagicMock()
    schedule_input.first = schedule_input
    schedule_input.wait_for.side_effect = RuntimeError("missing")
    page.locator.side_effect = [switch_card, schedule_input]

    with (
        patch("wxsp.platforms.xiaohongshu.physical_click"),
        patch("wxsp.platforms.xiaohongshu._wait_xhs"),
        pytest.raises(ElementNotFound, match="定时发布日期框"),
    ):
        _set_schedule(page, datetime(2026, 8, 12, 18, 0))
