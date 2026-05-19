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
    NotifyEvent,
    WecomNotifier,
    _format_markdown,
    build_notifiers_from_settings,
    notify,
)


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


def test_format_markdown_includes_title_content_task_account_context() -> None:
    event = NotifyEvent(
        type="risk_control",
        level="error",
        title="风控触发",
        content="账号 a 命中关键词:请稍后",
        context={"关键词": "请稍后", "步骤": "风控探测"},
        task_id=42,
        account_id="account_a",
        account_display_name="美食号",
    )
    md = _format_markdown(event)
    assert "风控触发" in md
    assert "请稍后" in md
    # 字段名 + level 标签都是中文,账号显示 display_name 而非 ID
    assert "任务编号:42" in md
    assert "账号:美食号" in md
    assert "account_a" not in md
    assert "关键词" in md and "步骤" in md


def test_format_markdown_falls_back_to_account_id_when_no_display_name() -> None:
    """没传 display_name 时回退到 account_id —— 兼容老调用方。"""
    event = NotifyEvent(
        type="info",
        level="info",
        title="x",
        content="y",
        account_id="account_a",
    )
    md = _format_markdown(event)
    assert "账号:account_a" in md


def test_format_markdown_minimal_event_omits_optional_lines() -> None:
    event = NotifyEvent(type="info", level="info", title="Hi", content="body")
    md = _format_markdown(event)
    assert "Hi" in md
    assert "body" in md
    assert "任务编号" not in md
    assert "账号:" not in md


def test_format_markdown_includes_platform_tag_and_chinese_level() -> None:
    """所有推送的标题前面都要带 [视频号] + 中文 level 标签(信息/警告/错误)。"""
    event = NotifyEvent(type="info", level="info", title="今日发布汇总", content="x")
    md = _format_markdown(event)
    first_line = md.splitlines()[0]
    assert first_line.startswith("## [视频号] [信息]")
    assert "今日发布汇总" in first_line


def test_format_markdown_translates_level_warn_and_error() -> None:
    md_warn = _format_markdown(NotifyEvent(type="x", level="warn", title="t", content=""))
    md_err = _format_markdown(NotifyEvent(type="x", level="error", title="t", content=""))
    assert "[警告]" in md_warn and "WARN" not in md_warn
    assert "[错误]" in md_err and "ERROR" not in md_err


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
    # default disabled in make_settings
    assert build_notifiers_from_settings(settings) == []


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
