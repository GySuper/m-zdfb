"""拼多多平台接入回归:REGISTRY 元数据 / 路由 / Spec 接线 / field_map(纯结构,不碰浏览器)。

对齐 test_kuaishou_platform.py 的结构惯例。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
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
        "detail_float": "商详悬浮窗",
        "store_feed": "同步店铺动态",
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


def test_pre_publish_hands_absolute_file_paths_to_the_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置里 data_dir 是相对路径(./data)时暂存路径也是相对的;Chrome 是另一个进程,
    按相对路径找不到文件,拼多多提示「已过滤空文件」(2026-11-19 任务 58)。"""
    from wxsp.platforms import pinduoduo

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("wxsp.apc.check_pass", lambda: True)
    monkeypatch.setattr(pinduoduo, "stage_to_tmp", lambda *a, **k: Path("data/tmp/58/c.png"))
    bundle = MagicMock(
        product_ids_json='["1"]',
        video_cover_path=Path("/nas/c.png"),
        tags_json="[]",
        description="d",
        publish_at=datetime(2026, 12, 1, 10, 30),
        declaration=None,
        detail_float=None,
        store_feed=None,
        account_id="a",
    )
    ctx = MagicMock()
    ctx.settings.accounts = {}
    ctx.settings.publisher.upload_timeout_seconds = 600
    ctx.step_pause = (0, 0)
    session = MagicMock()
    session.next_message.return_value = {"type": "gate"}

    pinduoduo._pre_publish(session, bundle, Path("data/tmp/58/v.mp4"), ctx)

    job = session.start_job.call_args[0][0]
    assert job["video_path"] == str(tmp_path / "data/tmp/58/v.mp4")
    assert job["cover_path"] == str(tmp_path / "data/tmp/58/c.png")


# ---- 发布前暂时性错误自动重跑整轮(新标签页从头来;点发布之后绝不重试)----


class _RoundsSession:
    """每次 start_job 开一轮,按轮回放插件消息。"""

    def __init__(self, *rounds: list[dict[str, object]]) -> None:
        self.rounds = list(rounds)
        self.jobs: list[dict[str, object]] = []
        self.msgs: list[dict[str, object]] = []

    def start_job(self, job: dict[str, object]) -> None:
        self.jobs.append(job)
        self.msgs = list(self.rounds.pop(0))

    def next_message(self, timeout: float) -> dict[str, object]:
        if not self.msgs:
            raise TimeoutError
        return self.msgs.pop(0)


def _err(kind: str) -> dict[str, object]:
    return {"type": "error", "kind": kind, "step": "upload", "message": "坏了"}


def _run_pre(monkeypatch: pytest.MonkeyPatch, session: _RoundsSession) -> MagicMock:
    from wxsp.platforms import pinduoduo

    monkeypatch.setattr("wxsp.apc.check_pass", lambda: True)
    monkeypatch.setattr(pinduoduo.time, "sleep", lambda s: None)
    monkeypatch.setattr(pinduoduo, "screenshot", lambda *a, **k: Path("shot.png"))
    bundle = MagicMock(
        product_ids_json='["1"]',
        video_cover_path=None,
        tags_json="[]",
        description="d",
        publish_at=datetime(2026, 12, 1, 10, 30),
        declaration=None,
        detail_float=None,
        store_feed=None,
        account_id="a",
    )
    ctx = MagicMock()
    ctx.settings.accounts = {}
    ctx.settings.publisher.upload_timeout_seconds = 600
    ctx.step_pause = (0, 0)
    ctx.result.screenshots = []
    pinduoduo._pre_publish(session, bundle, Path("/v.mp4"), ctx)  # type: ignore[arg-type]
    return ctx


def test_pre_publish_reruns_after_transient_error(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _RoundsSession([_err("network")], [{"type": "gate"}])
    ctx = _run_pre(monkeypatch, session)
    assert len(session.jobs) == 2
    assert ctx.result.screenshots == ["shot.png"]  # 失败那一轮留截图


@pytest.mark.parametrize("kind", ["cookie_expired", "risk_control", "product_not_found"])
def test_pre_publish_does_not_rerun_hopeless_errors(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from wxsp.errors import PublisherError

    session = _RoundsSession([_err(kind)], [{"type": "gate"}])
    with pytest.raises(PublisherError):
        _run_pre(monkeypatch, session)
    assert len(session.jobs) == 1


def test_pre_publish_rerun_budget_per_kind_and_total(monkeypatch: pytest.MonkeyPatch) -> None:
    from wxsp.errors import ElementNotFound, NetworkError

    # element_not_found 只重跑 1 次
    session = _RoundsSession(
        [_err("element_not_found")], [_err("element_not_found")], [{"type": "gate"}]
    )
    with pytest.raises(ElementNotFound):
        _run_pre(monkeypatch, session)
    assert len(session.jobs) == 2
    # 网络错误预算 3 次,但总共最多 3 轮
    session = _RoundsSession(*[[_err("network")]] * 4)
    with pytest.raises(NetworkError):
        _run_pre(monkeypatch, session)
    assert len(session.jobs) == 3


def test_publish_unconfirmed_is_its_own_error() -> None:
    from wxsp.errors import PublishUnconfirmed, classify
    from wxsp.platforms.pinduoduo import _pump

    session = _ScriptedSession(
        {
            "type": "error",
            "kind": "publish_unconfirmed",
            "step": "wait_success",
            "message": "可能已发出",
        }
    )
    with pytest.raises(PublishUnconfirmed) as exc:
        _pump(session, _ctx(), "done", timeout=1)  # type: ignore[arg-type]
    assert classify(exc.value) == "publish_unconfirmed"
