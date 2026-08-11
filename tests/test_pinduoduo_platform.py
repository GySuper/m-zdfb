"""拼多多平台接入回归:REGISTRY 元数据 / 路由 / Spec 接线 / field_map(纯结构,不碰浏览器)。

对齐 test_kuaishou_platform.py 的结构惯例。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
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


def test_pinduoduo_add_tags_types_space_after_fixed_wait() -> None:
    from wxsp.platforms import pinduoduo_selectors as sel
    from wxsp.platforms.pinduoduo import _add_tags

    page = MagicMock()
    editor = MagicMock()
    editor.first = editor
    page.locator.return_value = editor
    events: list[tuple[str, str | int]] = []
    page.keyboard.type.side_effect = lambda value: events.append(("type", value))
    page.wait_for_timeout.side_effect = lambda value: events.append(("wait", value))
    page.keyboard.press.side_effect = lambda value: events.append(("press", value))

    _add_tags(page, ["早餐", "豆浆"])

    page.locator.assert_called_once_with(sel.DESC_EDITOR)
    editor.click.assert_called_once_with()
    assert events == [
        ("type", "#早餐"),
        ("wait", 1500),
        ("press", "Space"),
        ("type", "#豆浆"),
        ("wait", 1500),
        ("press", "Space"),
    ]


def _entry_dialog(*, has_close: bool = True) -> tuple[MagicMock, MagicMock]:
    dialog = MagicMock()
    close = MagicMock()
    close.first = close
    close.count.return_value = int(has_close)
    dialog.locator.return_value = close
    return dialog, close


def _entry_popup_page(*dialogs: MagicMock) -> MagicMock:
    from wxsp.platforms import pinduoduo_selectors as sel

    page = MagicMock()
    pending = list(dialogs)

    def locate(selector: str) -> MagicMock:
        matches = MagicMock()
        if selector == sel.ENTRY_DIALOG:
            if pending:
                matches.count.return_value = 1
                matches.last = pending.pop(0)
            else:
                matches.count.return_value = 0
        elif selector == sel.ENTRY_DIALOG_CLOSE:
            matches.count.return_value = 0
        else:
            raise AssertionError(f"unexpected selector: {selector}")
        return matches

    page.locator.side_effect = locate
    return page


def test_pinduoduo_dismisses_stacked_entry_popups_by_close_control() -> None:
    from wxsp.platforms import pinduoduo_selectors as sel
    from wxsp.platforms.pinduoduo import _dismiss_entry_popups

    first_dialog, first_close = _entry_dialog()
    second_dialog, second_close = _entry_dialog()
    page = _entry_popup_page(first_dialog, second_dialog)

    with patch("wxsp.platforms.pinduoduo._wait_for_entry_popup_count_drop", return_value=True):
        _dismiss_entry_popups(page)

    for dialog, close in ((first_dialog, first_close), (second_dialog, second_close)):
        dialog.locator.assert_called_once_with(sel.ENTRY_DIALOG_CLOSE)
        close.click.assert_called_once_with(timeout=2_000)
    page.keyboard.press.assert_not_called()


def test_pinduoduo_dismisses_entry_popup_with_escape_when_no_close() -> None:
    from wxsp.platforms.pinduoduo import _dismiss_entry_popups

    dialog, _close = _entry_dialog(has_close=False)
    page = _entry_popup_page(dialog)

    with patch("wxsp.platforms.pinduoduo._wait_for_entry_popup_count_drop", return_value=True):
        _dismiss_entry_popups(page)

    page.keyboard.press.assert_called_once_with("Escape")


def test_pinduoduo_entry_popup_fails_closed_when_escape_does_not_close() -> None:
    from wxsp.platforms.pinduoduo import _dismiss_entry_popups

    dialog, _close = _entry_dialog(has_close=False)
    page = _entry_popup_page(dialog)

    with (
        patch("wxsp.platforms.pinduoduo._wait_for_entry_popup_count_drop", return_value=False),
        pytest.raises(ElementNotFound, match="无法安全关闭"),
    ):
        _dismiss_entry_popups(page)

    page.keyboard.press.assert_called_once_with("Escape")


def test_pinduoduo_dismisses_standalone_promo_popup_from_real_dom() -> None:
    """真实商品推广浮层无 dialog 容器,仅有全局可见 beast close SVG。"""
    from wxsp.platforms import pinduoduo_selectors as sel
    from wxsp.platforms.pinduoduo import _dismiss_entry_popups

    page = MagicMock()
    close = MagicMock()
    visible = True

    def click(*_args, **_kwargs) -> None:
        nonlocal visible
        visible = False

    close.click.side_effect = click

    def locate(selector: str) -> MagicMock:
        matches = MagicMock()
        if selector == sel.ENTRY_DIALOG:
            matches.count.return_value = 0
        elif selector == sel.ENTRY_DIALOG_CLOSE:
            matches.count.return_value = int(visible)
            matches.last = close
        else:
            raise AssertionError(f"unexpected selector: {selector}")
        return matches

    page.locator.side_effect = locate
    _dismiss_entry_popups(page)

    assert 'data-testid="beast-core-icon-close"' in sel.ENTRY_DIALOG_CLOSE
    close.click.assert_called_once_with(timeout=2_000)
    page.keyboard.press.assert_not_called()


def test_pinduoduo_clears_entry_popups_before_upload() -> None:
    from wxsp.platforms.pinduoduo import _pre_publish

    bundle = MagicMock()
    bundle.video_cover_path = None
    bundle.description = None
    bundle.tags_json = "[]"
    bundle.product_ids_json = '["123"]'
    bundle.publish_at = datetime(2026, 8, 12, 18, 30)
    bundle.declaration = None

    ctx = MagicMock()
    ctx.step_pause = (0.0, 0.0)
    ctx.settings.publisher.upload_timeout_seconds = 60

    calls: list[str] = []
    fakes = {
        "_open_publish_page": lambda *a, **kw: calls.append("open"),
        "_verify_logged_in": lambda *a, **kw: calls.append("verify"),
        "_dismiss_entry_popups": lambda *a, **kw: calls.append("popups"),
        "_upload_video": lambda *a, **kw: calls.append("upload"),
        "_fill_description": lambda *a, **kw: None,
        "_add_tags": lambda *a, **kw: None,
        "_add_products": lambda *a, **kw: None,
        "_set_cover": lambda *a, **kw: None,
        "_set_schedule": lambda *a, **kw: None,
        "_select_declaration": lambda *a, **kw: None,
        "_risk_control_probe": lambda *a, **kw: None,
        "random_pause": lambda *a, **kw: None,
    }
    with (
        patch("wxsp.platforms.pinduoduo.wxsp.apc.check_pass", return_value=True),
        patch.multiple("wxsp.platforms.pinduoduo", **fakes),
    ):
        _pre_publish(MagicMock(), bundle, Path("video.mp4"), ctx)

    assert calls[:4] == ["open", "verify", "popups", "upload"]


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
