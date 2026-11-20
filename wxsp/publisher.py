"""Publisher router — delegates to platform-specific publisher based on task.platform."""

from __future__ import annotations

import importlib
import threading

from wxsp import platform_meta
from wxsp.config import Settings, load_settings
from wxsp.db import get_engine, init_db
from wxsp.models import Account, Task
from wxsp.platforms.base import (  # re-export for callers
    AlreadyClaimed,
    PlatformPublisher,
    PublishResult,
)

__all__ = ["AlreadyClaimed", "PublishResult", "login", "publish"]

# 平台 → (模块, 类名),用到时才导入:被锁定的旧平台(patchright 实现,留作插件改版参考)
# 只在装了 patchright 的开发环境可用;安装包不带 patchright,拼多多(插件版)不受影响。
_PUBLISHER_CLASSES: dict[str, tuple[str, str]] = {
    "tencent_channel": ("wxsp.platforms.tencent_channel", "TencentChannelPublisher"),
    "taobao_guanghe": ("wxsp.platforms.taobao_guanghe", "TaobaoGuanghePublisher"),
    "douyin": ("wxsp.platforms.douyin", "DouyinPublisher"),
    "kuaishou": ("wxsp.platforms.kuaishou", "KuaishouPublisher"),
    "xiaohongshu": ("wxsp.platforms.xiaohongshu", "XiaohongshuPublisher"),
    "pinduoduo": ("wxsp.platforms.pinduoduo", "PinduoduoPublisher"),
}
_PUBLISHERS: dict[str, PlatformPublisher] = {}

# CLI / cron / Web retry 最终都经过这个边界。进程内只允许一个发布浏览器运行,
# 避免 pyautogui、BlockInput 和同 IP 多账号并发互相干扰。
_PUBLISH_LOCK = threading.Lock()


def _get_publisher(platform: str) -> PlatformPublisher:
    if platform not in _PUBLISHER_CLASSES:
        raise ValueError(f"Unknown platform: {platform}")
    if platform not in _PUBLISHERS:
        module, cls = _PUBLISHER_CLASSES[platform]
        _PUBLISHERS[platform] = getattr(importlib.import_module(module), cls)()
    return _PUBLISHERS[platform]


def publish(
    task_id: int,
    *,
    dry_run: bool = False,
    settings: Settings | None = None,
) -> PublishResult:
    """Route to correct platform publisher based on task.platform.

    The platform publisher handles browser interaction + DB writes + notifications + Feishu.
    """
    engine = get_engine()
    init_db(engine)

    from sqlmodel import Session

    # Determine platform from task
    with Session(engine) as session:
        task = session.get(Task, task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        platform = getattr(task, "platform", "tencent_channel")
        if not platform_meta.is_available(platform):
            label = platform_meta.get_meta(platform).label
            raise ValueError(f"{label}已锁定({platform_meta.LOCKED_HINT}),不发布 Task {task_id}")
        account = session.get(Account, task.account_id)
        if account is None or not account.is_active:
            raise ValueError(f"Task {task_id} 的账号不存在或已禁用")
        account_id = account.id

    if settings is None or settings.source_platform not in (None, platform):
        settings = load_settings(platform=platform)
    account_cfg = settings.accounts.get(account_id)
    if settings.source_platform is not None and (account_cfg is None or not account_cfg.enabled):
        raise ValueError(f"Task {task_id} 的账号已从平台配置删除或禁用")
    if settings.publisher.max_concurrent_accounts != 1:
        raise ValueError("当前发布器只支持单 worker: max_concurrent_accounts 必须为 1")

    pub = _get_publisher(platform)
    with _PUBLISH_LOCK:
        return pub.publish_one(task_id, dry_run=dry_run, settings=settings)


def login(account: Account) -> bool:
    """Route to the correct platform publisher's login based on account.platform."""
    platform = getattr(account, "platform", "tencent_channel")
    with _PUBLISH_LOCK:
        return _get_publisher(platform).login(account)
