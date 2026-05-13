"""Notifier 协议 + WecomNotifier(M7)。

设计要点:
- NotifyEvent:type/level/title/content + 可选 task_id/account_id/context
- Notifier Protocol:任何渠道(企微/飞书/钉钉)实现 send(event) -> bool
- WecomNotifier:POST 企微机器人 webhook,Markdown 卡片(stdlib urllib,无新依赖)
- build_notifiers_from_settings:按 Settings.monitoring.notifiers 构造 enabled notifier
- notify(event, *, session, settings, notifiers=None):一站式入口
    1. 无条件写 Event 表(审计 / Web UI 时间线)
    2. 只有 event.type ∈ settings.monitoring.notify_on 才派发到外部渠道
    3. 单 notifier 抛异常 / 返回 False,只 log,不影响其它渠道也不传给业务
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from loguru import logger
from sqlmodel import Session

from wxsp.config import Settings
from wxsp.models import Event


@dataclass
class NotifyEvent:
    """业务侧发出的通知事件。type 与 monitoring.notify_on 的字符串保持一致。"""

    type: str
    level: str  # "info" | "warn" | "error"
    title: str
    content: str
    context: dict[str, Any] = field(default_factory=dict)
    task_id: int | None = None
    account_id: str | None = None


class Notifier(Protocol):
    """任意通知渠道都实现 send() —— 返回 True 表示已送达。"""

    name: str

    def send(self, event: NotifyEvent) -> bool: ...


@dataclass
class WecomNotifier:
    """企微机器人 webhook(Markdown 卡片)。"""

    webhook: str
    name: str = "wecom"
    timeout_seconds: int = 5

    def send(self, event: NotifyEvent) -> bool:
        payload = json.dumps(
            {"msgtype": "markdown", "markdown": {"content": _format_markdown(event)}}
        ).encode("utf-8")
        req = urllib.request.Request(
            self.webhook,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                body = resp.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            logger.warning(f"[notify] 企微推送失败(network): {exc}")
            return False
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            logger.warning(f"[notify] 企微响应非 JSON: {body[:200]}")
            return False
        errcode = data.get("errcode", -1)
        if errcode != 0:
            logger.warning(f"[notify] 企微 errcode={errcode} errmsg={data.get('errmsg')}")
            return False
        return True


def _format_markdown(event: NotifyEvent) -> str:
    """渲染 Markdown:头部 + level 标签 + title + 正文 + 可选 task/account/context。"""
    tag = {"info": "[INFO]", "warn": "[WARN]", "error": "[ERROR]"}.get(event.level, "[*]")
    lines = [f"## {tag} {event.title}", "", event.content]
    if event.task_id is not None:
        lines.append(f"> task_id: `{event.task_id}`")
    if event.account_id is not None:
        lines.append(f"> account: `{event.account_id}`")
    if event.context:
        lines.append("")
        for k, v in event.context.items():
            lines.append(f"- **{k}**: {v}")
    return "\n".join(lines)


def build_notifiers_from_settings(settings: Settings) -> list[Notifier]:
    """从 Settings.monitoring.notifiers 构造 enabled notifier 列表。

    第一版只有 wecom;接入飞书/钉钉时在此 append 即可。
    """
    notifiers: list[Notifier] = []
    if settings.monitoring.notifiers.wecom.enabled:
        notifiers.append(WecomNotifier(webhook=settings.monitoring.notifiers.wecom.webhook))
    return notifiers


def notify(
    event: NotifyEvent,
    *,
    session: Session,
    settings: Settings,
    notifiers: list[Notifier] | None = None,
) -> None:
    """统一入口:写 Event 审计 + 按 notify_on 过滤后派发到外部渠道。

    任何渠道异常 / 写 Event 异常都只 log,不抛给调用方(避免告警链路打挂主流程)。
    """
    # ① Event 表落地(无论 type 是否在 notify_on)
    try:
        ev = Event(
            ts=datetime.now(),
            level=event.level,
            task_id=event.task_id,
            account_id=event.account_id,
            type=event.type,
            message=f"{event.title}\n{event.content}",
            context_json=json.dumps(event.context, ensure_ascii=False, default=str),
        )
        session.add(ev)
    except Exception as exc:
        logger.exception(f"[notify] 写 Event 表失败 type={event.type}: {exc}")

    # ② 派发外部渠道(白名单 + 单渠道失败不影响其它)
    if event.type not in settings.monitoring.notify_on:
        return
    if notifiers is None:
        notifiers = build_notifiers_from_settings(settings)
    for n in notifiers:
        try:
            ok = n.send(event)
            if not ok:
                logger.warning(f"[notify] {n.name} 返回 False, type={event.type}")
        except Exception as exc:
            logger.exception(f"[notify] {getattr(n, 'name', '?')} 抛异常 type={event.type}: {exc}")
