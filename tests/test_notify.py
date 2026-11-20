"""notify.py(M7)单元测试 —— mock urllib.request.urlopen,不真发企微。"""

from __future__ import annotations

import io
import json
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlmodel import select

from tests.conftest import make_settings
from wxsp.db import get_engine, init_db, session_scope
from wxsp.models import Event
from wxsp.notify import (
    LarkNotifier,
    NotifyEvent,
    WecomNotifier,
    _format_markdown,
    _format_plain,
    build_notifiers_from_settings,
    describe_failure,
    lark_sign,
    notify,
    step_cn,
)


def test_step_cn_covers_all_platform_step_names() -> None:
    """各平台 adapter 写进 ctx.last_step 的步骤名都必须能翻成中文,
    否则 element_not_found 等告警标题会漏成英文(CLAUDE.md「告警里步骤名会漏成英文」)。

    历史漏网:open / login / wait_upload_area / products / declaration / ai / download。
    """
    expected = {
        "open": "打开发布页",
        "login": "验证登录",
        "entry_popups": "清理入口弹窗",
        "wait_upload_area": "等待上传区",
        "products": "绑定商品",
        "declaration": "创作者声明",
        "ai": "AI 优化",
        "download": "下载素材",
    }
    for key, cn in expected.items():
        result = step_cn(key)
        assert result == cn
        assert result != key  # 不能原样把英文 key 透出去


def _fake_response(payload: dict) -> object:
    """构造一个 urlopen() 上下文管理器返回值,resp.read() 给 JSON 字节。"""

    class _Ctx:
        def __enter__(self) -> _Ctx:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(payload).encode("utf-8")

    return _Ctx()


# ============== WecomNotifier ==============


def test_wecom_notifier_send_success_returns_true() -> None:
    notifier = WecomNotifier(webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fake")
    event = NotifyEvent(type="task_failed", level="error", title="任务失败", content="oops")
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["method"] = req.method
        captured["timeout"] = timeout
        return _fake_response({"errcode": 0, "errmsg": "ok"})

    with patch("wxsp.notify.urllib.request.urlopen", side_effect=fake_urlopen):
        assert notifier.send(event) is True

    body = json.loads(captured["data"])  # type: ignore[arg-type]
    assert body["msgtype"] == "markdown"
    assert "任务失败" in body["markdown"]["content"]
    assert captured["method"] == "POST"
    assert captured["timeout"] == 5  # default timeout


def test_wecom_notifier_send_errcode_nonzero_returns_false() -> None:
    notifier = WecomNotifier(webhook="https://x")
    event = NotifyEvent(type="task_failed", level="error", title="T", content="C")

    with patch(
        "wxsp.notify.urllib.request.urlopen",
        return_value=_fake_response({"errcode": 93000, "errmsg": "invalid webhook"}),
    ):
        assert notifier.send(event) is False


def test_wecom_notifier_send_network_error_returns_false() -> None:
    notifier = WecomNotifier(webhook="https://x")
    event = NotifyEvent(type="task_failed", level="error", title="T", content="C")
    with patch(
        "wxsp.notify.urllib.request.urlopen",
        side_effect=urllib.error.URLError("dns fail"),
    ):
        assert notifier.send(event) is False


def test_wecom_notifier_send_timeout_returns_false() -> None:
    notifier = WecomNotifier(webhook="https://x")
    event = NotifyEvent(type="task_failed", level="error", title="T", content="C")
    with patch("wxsp.notify.urllib.request.urlopen", side_effect=TimeoutError("slow")):
        assert notifier.send(event) is False


def test_wecom_notifier_send_invalid_json_response_returns_false() -> None:
    notifier = WecomNotifier(webhook="https://x")
    event = NotifyEvent(type="task_failed", level="error", title="T", content="C")

    class _BadCtx:
        def __enter__(self) -> _BadCtx:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def read(self) -> bytes:
            return b"<html>nope</html>"

    with patch("wxsp.notify.urllib.request.urlopen", return_value=_BadCtx()):
        assert notifier.send(event) is False


# ============== _format_markdown ==============


G = "\u3000"  # 字段名与值之间的全角空格


def test_format_markdown_title_status_fields_and_footer() -> None:
    event = NotifyEvent(
        type="risk_control",
        level="error",
        title="账号触发平台风控",
        status="需要处理",
        content="",
        context={"平台提示": "操作过于频繁", "处理": "已自动暂停 24 小时"},
        task_id=42,
        account_id="account_a",
        account_display_name="美食号",
    )
    lines = _format_markdown(event).splitlines()
    assert lines[0] == '**视频号 · 账号触发平台风控** <font color="warning">需要处理</font>'
    assert f"> 账号{G}美食号" in lines  # 显示中文名,不显示账号 ID
    assert f"> 平台提示{G}操作过于频繁" in lines
    assert lines[-1].startswith('<font color="comment">任务 #42 · ')
    md = "\n".join(lines)
    assert "account_a" not in md and "[错误]" not in md and "##" not in md


def test_format_markdown_falls_back_to_account_id_when_no_display_name() -> None:
    md = _format_markdown(
        NotifyEvent(type="x", level="info", title="t", content="", account_id="acc")
    )
    assert f"> 账号{G}acc" in md


def test_format_markdown_minimal_event_has_no_fields_or_task() -> None:
    md = _format_markdown(NotifyEvent(type="info", level="info", title="Hi", content="body"))
    assert md.splitlines()[:2] == ["**视频号 · Hi**", "body"]
    assert "> " not in md and "任务 #" not in md


def test_format_markdown_status_color_follows_level() -> None:
    ok = _format_markdown(
        NotifyEvent(type="x", level="info", title="t", content="", status="全部成功")
    )
    bad = _format_markdown(
        NotifyEvent(type="x", level="warn", title="t", content="", status="需要处理")
    )
    assert '<font color="info">全部成功</font>' in ok
    assert '<font color="warning">需要处理</font>' in bad


# ============== LarkNotifier ==============


def test_lark_sign_matches_feishu_algorithm() -> None:
    """飞书签名 = base64(hmac_sha256(key=f"{ts}\\n{secret}"))。固定向量防算法漂移。"""
    assert lark_sign("1609459200", "abc") == "wpTIl7bp+SN2odG93IcRqege1WiZoyogn3R1QomxBrs="


def test_format_plain_has_no_markdown_symbols() -> None:
    """飞书文本消息不渲染 markdown:同一份内容去掉颜色 / 加粗 / 引用符号。"""
    event = NotifyEvent(
        type="run_summary",
        level="warn",
        title="本轮发布完成",
        status="1 条需要处理",
        content='成功 <font color="info">1</font> · 失败 1\n> **1. 早餐豆浆**',
        task_id=7,
        account_id="acc_x",
        account_display_name="美食号",
    )
    text = _format_plain(event)
    assert "**" not in text and "<font" not in text and "\n> " not in text
    assert text.splitlines()[0] == "视频号 · 本轮发布完成 1 条需要处理"
    assert "成功 1 · 失败 1" in text
    assert "1. 早餐豆浆" in text
    assert f"账号{G}美食号" in text


# ============== describe_failure:原因 / 处理办法,不让代码穿透 ==============


def test_describe_failure_strips_step_prefix_and_gives_action() -> None:
    reason, action = describe_failure(
        "product_not_found", "step=products: 商品已下架(商品 ID 858612559443)"
    )
    assert reason == "商品问题:商品已下架(商品 ID 858612559443)"
    assert "待入库" in action


def test_describe_failure_names_the_step_for_page_changes() -> None:
    reason, action = describe_failure("element_not_found", "step=cover: 编辑封面未找到(等了 10 秒)")
    assert reason == "发布页可能改版,卡在「设置封面」:编辑封面未找到(等了 10 秒)"
    assert "技术" in action


@pytest.mark.parametrize(
    ("error_type", "msg", "expected"),
    [
        ("unknown", "step=upload: CdpError: Runtime.evaluate timed out", "未知问题"),
        ("network", "step=open_publish: Page.navigate net::ERR_TIMED_OUT", "网络不稳定"),
        ("brand_new_type", "boom", "未知问题"),
        (None, None, "未知问题"),
    ],
)
def test_describe_failure_never_leaks_code(
    error_type: str | None, msg: str | None, expected: str
) -> None:
    reason, _ = describe_failure(error_type, msg)
    assert reason == expected


def test_lark_notifier_send_success_text_no_sign() -> None:
    notifier = LarkNotifier(webhook="https://open.feishu.cn/open-apis/bot/v2/hook/fake")
    event = NotifyEvent(type="task_failed", level="error", title="任务失败", content="oops")
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
        captured["data"] = req.data
        captured["method"] = req.method
        captured["timeout"] = timeout
        return _fake_response({"code": 0, "msg": "success"})

    with patch("wxsp.notify.urllib.request.urlopen", side_effect=fake_urlopen):
        assert notifier.send(event) is True

    body = json.loads(captured["data"])  # type: ignore[arg-type]
    assert body["msg_type"] == "text"
    assert "任务失败" in body["content"]["text"]
    assert "sign" not in body and "timestamp" not in body  # 无 secret → 不签名
    assert captured["method"] == "POST"
    assert captured["timeout"] == 5


def test_lark_notifier_send_includes_sign_when_secret_set() -> None:
    notifier = LarkNotifier(webhook="https://x", secret="topsecret")
    event = NotifyEvent(type="task_failed", level="error", title="T", content="C")
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
        captured["data"] = req.data
        return _fake_response({"code": 0})

    with patch("wxsp.notify.urllib.request.urlopen", side_effect=fake_urlopen):
        assert notifier.send(event) is True

    body = json.loads(captured["data"])  # type: ignore[arg-type]
    assert body.get("sign")
    assert body["sign"] == lark_sign(body["timestamp"], "topsecret")


def test_lark_notifier_send_accepts_legacy_statuscode_zero() -> None:
    """飞书旧接口返回 StatusCode=0 也算成功。"""
    notifier = LarkNotifier(webhook="https://x")
    event = NotifyEvent(type="x", level="info", title="t", content="c")
    with patch(
        "wxsp.notify.urllib.request.urlopen",
        return_value=_fake_response({"StatusCode": 0, "StatusMessage": "success"}),
    ):
        assert notifier.send(event) is True


def test_lark_notifier_send_nonzero_code_returns_false() -> None:
    notifier = LarkNotifier(webhook="https://x")
    event = NotifyEvent(type="x", level="info", title="t", content="c")
    with patch(
        "wxsp.notify.urllib.request.urlopen",
        return_value=_fake_response({"code": 19021, "msg": "sign match fail"}),
    ):
        assert notifier.send(event) is False


def test_lark_notifier_send_network_error_returns_false() -> None:
    notifier = LarkNotifier(webhook="https://x")
    event = NotifyEvent(type="x", level="info", title="t", content="c")
    with patch(
        "wxsp.notify.urllib.request.urlopen",
        side_effect=urllib.error.URLError("dns fail"),
    ):
        assert notifier.send(event) is False


# ============== build_notifiers_from_settings ==============


def test_build_notifiers_returns_wecom_when_enabled(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, tmp_path)
    settings.monitoring.notifiers.wecom.enabled = True
    settings.monitoring.notifiers.wecom.webhook = "https://qyapi.fake"
    notifiers = build_notifiers_from_settings(settings)
    assert len(notifiers) == 1
    n = notifiers[0]
    assert isinstance(n, WecomNotifier)
    assert n.webhook == "https://qyapi.fake"


def test_build_notifiers_empty_when_wecom_disabled(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, tmp_path)
    # default disabled in make_settings(lark 默认也 disabled)
    assert build_notifiers_from_settings(settings) == []


def test_build_notifiers_returns_lark_when_enabled(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, tmp_path)
    settings.monitoring.notifiers.lark.enabled = True
    settings.monitoring.notifiers.lark.webhook = "https://open.feishu.cn/hook/fake"
    settings.monitoring.notifiers.lark.secret = "s3cr3t"
    notifiers = build_notifiers_from_settings(settings)
    assert len(notifiers) == 1
    n = notifiers[0]
    assert isinstance(n, LarkNotifier)
    assert n.webhook == "https://open.feishu.cn/hook/fake"
    assert n.secret == "s3cr3t"


def test_build_notifiers_returns_both_when_wecom_and_lark_enabled(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, tmp_path)
    settings.monitoring.notifiers.wecom.enabled = True
    settings.monitoring.notifiers.wecom.webhook = "https://qyapi.fake"
    settings.monitoring.notifiers.lark.enabled = True
    settings.monitoring.notifiers.lark.webhook = "https://feishu.fake"
    kinds = {type(n) for n in build_notifiers_from_settings(settings)}
    assert kinds == {WecomNotifier, LarkNotifier}


# ============== notify() dispatcher ==============


@dataclass
class _SpyNotifier:
    """测试用 fake notifier;记录调用,可控返回值/抛异常。"""

    name: str = "spy"
    return_value: bool = True
    raise_exc: Exception | None = None
    calls: list[NotifyEvent] = field(default_factory=list)

    def send(self, event: NotifyEvent) -> bool:
        self.calls.append(event)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.return_value


def test_notify_writes_event_row_even_when_type_not_in_notify_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    settings = make_settings(tmp_path, tmp_path)
    settings.monitoring.notify_on = []  # 空白名单
    spy = _SpyNotifier()
    event = NotifyEvent(type="cookie_expired", level="error", title="T", content="C")

    with session_scope(engine) as session:
        notify(event, session=session, settings=settings, notifiers=[spy])

    with session_scope(engine) as session:
        rows = list(session.exec(select(Event)).all())
        assert len(rows) == 1
        assert rows[0].type == "cookie_expired"
        assert rows[0].level == "error"
    assert spy.calls == []  # 不在白名单 → 不派发


def test_notify_dispatches_when_type_in_notify_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    settings = make_settings(tmp_path, tmp_path)
    settings.monitoring.notify_on = ["task_failed"]
    spy = _SpyNotifier()
    event = NotifyEvent(
        type="task_failed", level="error", title="任务失败", content="boom", task_id=7
    )

    with session_scope(engine) as session:
        notify(event, session=session, settings=settings, notifiers=[spy])

    assert len(spy.calls) == 1
    assert spy.calls[0].type == "task_failed"
    assert spy.calls[0].task_id == 7


def test_notify_one_notifier_failure_does_not_block_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    settings = make_settings(tmp_path, tmp_path)
    settings.monitoring.notify_on = ["risk_control"]
    spy_raise = _SpyNotifier(name="boom", raise_exc=RuntimeError("network down"))
    spy_ok = _SpyNotifier(name="ok", return_value=True)
    event = NotifyEvent(type="risk_control", level="error", title="T", content="C")

    with session_scope(engine) as session:
        # 不抛
        notify(event, session=session, settings=settings, notifiers=[spy_raise, spy_ok])

    assert len(spy_raise.calls) == 1
    assert len(spy_ok.calls) == 1  # 第一个挂了第二个依然被调

    # Event 行也写了
    with session_scope(engine) as session:
        rows = list(session.exec(select(Event)).all())
        assert len(rows) == 1


def test_notify_returning_false_logs_but_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    settings = make_settings(tmp_path, tmp_path)
    settings.monitoring.notify_on = ["task_failed"]
    spy = _SpyNotifier(return_value=False)
    event = NotifyEvent(type="task_failed", level="error", title="T", content="C")

    with session_scope(engine) as session:
        notify(event, session=session, settings=settings, notifiers=[spy])

    assert len(spy.calls) == 1  # 调过了,结果 False 也不影响


# 防止"io 未使用"导致 ruff 报警(_BadCtx 在 invalid_json 测试里手写,不依赖 io)
_ = io
