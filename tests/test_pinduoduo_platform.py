"""拼多多平台接入回归:REGISTRY 元数据 / 路由 / Spec 接线 / field_map(纯结构,不碰浏览器)。

对齐 test_kuaishou_platform.py 的结构惯例。
"""

from __future__ import annotations

from unittest.mock import MagicMock

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
    from wxsp.platform_meta import get_meta
    from wxsp.platforms.pinduoduo import PINDUODUO_SPEC, _post_publish, _pre_publish

    assert PINDUODUO_SPEC.platform_key == "pinduoduo"
    assert PINDUODUO_SPEC.display_name == "拼多多"
    assert get_meta("pinduoduo").driver == "extension"
    assert PINDUODUO_SPEC.pre_publish is _pre_publish
    assert PINDUODUO_SPEC.post_publish is _post_publish


class _ScriptedSession:
    """按顺序回放插件消息;耗尽后模拟插件无响应。"""

    def __init__(self, *msgs: dict[str, object]) -> None:
        self.msgs = list(msgs)

    def next_message(self, timeout: float) -> dict[str, object]:
        if not self.msgs:
            raise TimeoutError
        return self.msgs.pop(0)


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.task_id = 7
    ctx.last_step = "browser"
    return ctx


def test_pump_tracks_steps_until_gate() -> None:
    from wxsp.platforms.pinduoduo import _pump

    ctx = _ctx()
    session = _ScriptedSession(
        {"type": "step", "step": "open_publish"},
        {"type": "step", "step": "verify_login"},
        {"type": "gate"},
        {"type": "step", "step": "never_read"},
    )
    assert _pump(session, ctx, "gate", timeout=1) == {"type": "gate"}  # type: ignore[arg-type]
    assert ctx.last_step == "verify_login"


@pytest.mark.parametrize(
    ("kind", "exc_name"),
    [
        ("cookie_expired", "CookieExpired"),
        ("risk_control", "RiskControl"),
        ("element_not_found", "ElementNotFound"),
        ("upload_failed", "UploadFailed"),
        ("product_not_found", "ProductNotFound"),
        ("network", "NetworkError"),
        ("something_new", "UnknownError"),
    ],
)
def test_pump_maps_extension_errors_to_publisher_errors(kind: str, exc_name: str) -> None:
    import wxsp.errors
    from wxsp.platforms.pinduoduo import _pump

    ctx = _ctx()
    session = _ScriptedSession({"type": "error", "kind": kind, "step": "upload", "message": "坏了"})
    with pytest.raises(getattr(wxsp.errors, exc_name), match="坏了"):
        _pump(session, ctx, "gate", timeout=1)  # type: ignore[arg-type]
    assert ctx.last_step == "upload"


def test_pump_returns_on_halted_and_times_out_when_silent() -> None:
    from wxsp.platforms.pinduoduo import _pump

    halted = {"type": "halted", "step": "upload"}
    assert _pump(_ScriptedSession(halted), _ctx(), "gate", timeout=1) == halted  # type: ignore[arg-type]
    with pytest.raises(ElementNotFound, match="无进展"):
        _pump(_ScriptedSession(), _ctx(), "gate", timeout=1)  # type: ignore[arg-type]
