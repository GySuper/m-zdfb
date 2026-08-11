"""拼多多平台接入回归:REGISTRY 元数据 / 路由 / Spec 接线 / field_map(纯结构,不碰浏览器)。

对齐 test_kuaishou_platform.py 的结构惯例。
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from wxsp.errors import ElementNotFound


def _schedule_page() -> tuple[MagicMock, MagicMock, MagicMock]:
    from wxsp.platforms import pinduoduo_selectors as sel

    page = MagicMock()
    radio = MagicMock()
    radio.first = radio
    page.get_by_text.return_value = radio

    date_input = MagicMock()
    date_input.first = date_input
    cell = MagicMock()
    cell_query = MagicMock()
    cell_filter = MagicMock()
    cell_query.filter.return_value = cell_filter
    cell_filter.first = cell

    time_input = MagicMock()
    time_input.first = time_input
    confirm = MagicMock()
    confirm.first = confirm

    def locator(selector: str) -> MagicMock:
        if selector == sel.SCHEDULE_DATE_INPUT:
            return date_input
        if selector.startswith('[role="date-cell"]'):
            return cell_query
        if selector == '[data-testid="beast-core-timePicker-input"]':
            return time_input
        if selector == sel.SCHEDULE_CONFIRM_BUTTON:
            return confirm
        raise AssertionError(f"unexpected selector: {selector}")

    page.locator.side_effect = locator
    page.evaluate.return_value = {
        "colCount": 3,
        "clicked": ["18", "30", "00"],
    }
    return page, cell, confirm


def test_pinduoduo_registered_in_registry() -> None:
    from wxsp.platform_meta import ALL_PLATFORMS, get_meta

    m = get_meta("pinduoduo")
    assert m.key == "pinduoduo"
    assert m.label == "拼多多"
    assert m.title_min == 1
    assert m.needs_fingerprint is False
    assert m.has_title is False
    # 拼多多用 tags + cover + declaration(内容声明)+ product_ids(商品ID)
    assert m.field_map_defaults == {
        "tags": "标签",
        "cover": "封面文件",
        "declaration": "内容声明",
        "product_ids": "商品ID",
    }
    # 登录态:goto SSO 入口,已登录跳 home(含 fragment),未登录停 mms/login → logged_in_url 正面判
    assert m.login_meta["mode"] == "logged_in_url"
    assert m.login_meta["logged_in_fragment"] == "live.pinduoduo.com/n-creator/video/home"
    assert "mms.pinduoduo.com/login/sso" in m.login_meta["home_url"]
    assert "pinduoduo" in ALL_PLATFORMS


def test_pinduoduo_title_min_via_validator() -> None:
    from wxsp.validator import _title_min_for

    assert _title_min_for("pinduoduo") == 1


def test_pinduoduo_field_map_has_fields_the_adapter_uses() -> None:
    from wxsp.api.routes_setup import _field_map_for

    fm = _field_map_for("pinduoduo")
    # 共有字段
    assert fm["title"] == "标题"
    assert fm["video_file"] == "视频文件"
    assert fm["publish_at"] == "定时发布时间"
    # adapter 用的平台特有字段:tags + cover + declaration + product_ids
    assert fm["tags"] == "标签"
    assert fm["cover"] == "封面文件"
    assert fm["declaration"] == "内容声明"
    assert fm["product_ids"] == "商品ID"
    # 不该混入其他平台特有字段(视频号的合集/原创、淘宝的 ai_optimize 等)
    assert "topic" not in fm
    assert "original_claim" not in fm
    assert "ai_optimize" not in fm


def test_pinduoduo_routing_returns_pinduoduo_publisher() -> None:
    from wxsp.platforms.pinduoduo import PinduoduoPublisher
    from wxsp.publisher import _get_publisher

    assert isinstance(_get_publisher("pinduoduo"), PinduoduoPublisher)


def test_pinduoduo_spec_wiring() -> None:
    from wxsp.platforms.pinduoduo import PINDUODUO_SPEC, _post_publish, _pre_publish

    assert PINDUODUO_SPEC.platform_key == "pinduoduo"
    assert PINDUODUO_SPEC.display_name == "拼多多"
    assert PINDUODUO_SPEC.pre_publish is _pre_publish
    assert PINDUODUO_SPEC.post_publish is _post_publish


def test_pinduoduo_schedule_date_failure_fails_closed() -> None:
    from wxsp.platforms.pinduoduo import _set_schedule

    page, cell, _confirm = _schedule_page()
    cell.click.side_effect = RuntimeError("date missing")

    with (
        patch("wxsp.platforms.pinduoduo._wait"),
        pytest.raises(ElementNotFound, match="日历"),
    ):
        _set_schedule(page, datetime(2026, 8, 12, 18, 30))


def test_pinduoduo_schedule_incomplete_time_fails_closed() -> None:
    from wxsp.platforms.pinduoduo import _set_schedule

    page, _cell, _confirm = _schedule_page()
    page.evaluate.return_value = {"colCount": 3, "clicked": ["18", "30"]}

    with (
        patch("wxsp.platforms.pinduoduo._wait"),
        pytest.raises(ElementNotFound, match="时间滚轮"),
    ):
        _set_schedule(page, datetime(2026, 8, 12, 18, 30))


def test_pinduoduo_schedule_confirm_failure_fails_closed() -> None:
    from wxsp.platforms.pinduoduo import _set_schedule

    page, _cell, confirm = _schedule_page()
    confirm.click.side_effect = RuntimeError("confirm missing")

    with (
        patch("wxsp.platforms.pinduoduo._wait"),
        pytest.raises(ElementNotFound, match="确认按钮"),
    ):
        _set_schedule(page, datetime(2026, 8, 12, 18, 30))
