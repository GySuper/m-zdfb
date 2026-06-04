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
    """业务侧发出的通知事件。type 与 monitoring.notify_on 的字符串保持一致。

    account_display_name:运营在飞书 / Web UI 看到的中文名(如"美食号")。
    渲染时优先用它替代 account_id(英文 ID 对运营无意义)。发起方按需填。

    platform:发送该事件的平台 key,用于渲染平台标签(如 "[视频号]" / "[淘宝光合]")
    以及查对应平台的 monitoring.notify_on 白名单。
    """

    type: str
    level: str  # "info" | "warn" | "error"
    title: str
    content: str
    context: dict[str, Any] = field(default_factory=dict)
    task_id: int | None = None
    account_id: str | None = None
    account_display_name: str | None = None
    platform: str = "tencent_channel"


# 通知里出现的英文术语 → 中文映射。运营看的全中文,且与 UI 侧 api.i18n 不依赖
# (UI 翻译 + 通知翻译可能各自微调措辞,故各放各的)
_ERROR_TYPE_CN: dict[str, str] = {
    "cookie_expired": "登录态失效",
    "risk_control": "风控触发",
    "element_not_found": "页面元素未找到(可能改版)",
    "task_failed": "任务失败",
    "nas_unreachable": "存储不可达",
    "network": "网络异常",
    "video_invalid": "视频非法",
    "upload_failed": "上传失败",
    "feishu_api_error": "飞书接口错误",
    "topic_not_found": "话题未找到",
    "product_not_found": "商品未找到",
    "unknown": "未知错误",
    "cookie_warning": "登录态即将过期",
    "backlog_high": "历史积压超阈值",
    "login_required": "需重新登录",
}

_STEP_CN: dict[str, str] = {
    "claim": "认领任务",
    "stage": "暂存视频",
    "browser": "启动浏览器",
    "open": "打开发布页",
    "open_publish": "打开发布页",
    "login": "验证登录",
    "verify_login": "验证登录",
    "wait_upload_area": "等待上传区",
    "upload": "上传视频",
    "title": "填标题",
    "desc": "填描述",
    "tags": "加标签",
    "cover": "设置封面",
    "topic": "绑合集",
    "products": "绑定商品",
    "original": "声明原创",
    "declaration": "创作者声明",
    "ai": "AI 优化",
    "location": "选位置",
    "schedule": "定时发布",
    "download": "下载素材",
    "risk": "风控探测",
    "dryrun_gate": "测试模式拦截",
    "publish": "点击发表",
    "wait_success": "等待成功",
    "extract": "抓取链接",
}


def error_type_cn(value: str | None) -> str:
    """英文 error_type → 中文。未知键回退到中文兜底,不让英文穿透到通知里。"""
    if not value:
        return "未知错误"
    return _ERROR_TYPE_CN.get(value, "未知错误")


def step_cn(value: str | None) -> str:
    """英文 step 名 → 中文。未知键直接返回原值(尽量不丢信息)。"""
    if not value:
        return ""
    return _STEP_CN.get(value, value)


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


def _platform_tag(platform: str | None) -> str:
    """平台 key → 中文标签,用于通知 markdown 头部。

    取自 wxsp.platform_meta(单一信息源);未知/None 回退视频号(历史默认)。
    """
    from wxsp.platform_meta import get_meta

    return get_meta(platform).label


def _format_markdown(event: NotifyEvent) -> str:
    """渲染 Markdown:平台标识 + 中文 level 标签 + title + 正文 + 可选 任务编号/账号/上下文。

    平台标识按 event.platform 取中文名(如"[视频号]" / "[淘宝光合]")。
    全中文输出 —— 运营收到企微推送一眼看明白,不出现英文 level / 字段名 / 账号 ID。
    """
    platform_label = _platform_tag(event.platform)
    tag = {"info": "[信息]", "warn": "[警告]", "error": "[错误]"}.get(event.level, "[通知]")
    lines = [f"## [{platform_label}] {tag} {event.title}", "", event.content]
    if event.task_id is not None:
        lines.append(f"> 任务编号:{event.task_id}")
    if event.account_id is not None:
        # 优先 display_name(中文友好名),没有时回退到 account_id 字符串
        shown = event.account_display_name or event.account_id
        lines.append(f"> 账号:{shown}")
    if event.context:
        lines.append("")
        for k, v in event.context.items():
            lines.append(f"- **{k}**:{v}")
    return "\n".join(lines)


def build_notifiers_from_settings(
    settings: Settings, *, platform: str = "tencent_channel"
) -> list[Notifier]:
    """从平台 monitoring 配置构造 enabled notifier 列表。

    使用 settings.monitoring 取对应平台的 notifiers 配置。
    第一版只有 wecom;接入飞书/钉钉时在此 append 即可。
    """
    monitoring_cfg = settings.monitoring
    notifiers: list[Notifier] = []
    if monitoring_cfg is not None and monitoring_cfg.notifiers.wecom.enabled:
        notifiers.append(WecomNotifier(webhook=monitoring_cfg.notifiers.wecom.webhook))
    return notifiers


def notify(
    event: NotifyEvent,
    *,
    session: Session,
    settings: Settings,
    platform: str = "tencent_channel",
    notifiers: list[Notifier] | None = None,
) -> None:
    """统一入口:写 Event 审计 + 按 notify_on 过滤后派发到外部渠道。

    任何渠道异常 / 写 Event 异常都只 log,不抛给调用方(避免告警链路打挂主流程)。

    platform 用于查对应平台的 monitoring.notify_on 白名单;当 event 已有 platform
    字段时优先使用 event.platform(否则用参数兜底)。
    """
    effective_platform = event.platform or platform
    monitoring_cfg = settings.monitoring
    notify_on: list[str] = monitoring_cfg.notify_on if monitoring_cfg is not None else []

    # ① Event 表落地(无论 type 是否在 notify_on)
    try:
        ev = Event(
            ts=datetime.now(),
            level=event.level,
            task_id=event.task_id,
            account_id=event.account_id,
            platform=effective_platform,
            type=event.type,
            message=f"{event.title}\n{event.content}",
            context_json=json.dumps(event.context, ensure_ascii=False, default=str),
        )
        session.add(ev)
    except Exception as exc:
        logger.exception(f"[notify] 写 Event 表失败 type={event.type}: {exc}")

    # ② 派发外部渠道(白名单 + 单渠道失败不影响其它)
    if event.type not in notify_on:
        return
    if notifiers is None:
        notifiers = build_notifiers_from_settings(settings, platform=effective_platform)
    for n in notifiers:
        try:
            ok = n.send(event)
            if not ok:
                logger.warning(f"[notify] {n.name} 返回 False, type={event.type}")
        except Exception as exc:
            logger.exception(f"[notify] {getattr(n, 'name', '?')} 抛异常 type={event.type}: {exc}")
