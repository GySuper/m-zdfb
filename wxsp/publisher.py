"""Publisher router — delegates to platform-specific publisher based on task.platform."""

from __future__ import annotations

from wxsp.config import Settings
from wxsp.db import get_engine, init_db
from wxsp.models import Task
from wxsp.platforms.base import (  # re-export for callers
    AlreadyClaimed,
    PlatformPublisher,
    PublishResult,
)
from wxsp.platforms.taobao_guanghe import TaobaoGuanghePublisher
from wxsp.platforms.tencent_channel import TencentChannelPublisher

__all__ = ["AlreadyClaimed", "PublishResult", "publish"]

_PUBLISHERS: dict[str, PlatformPublisher] = {
    "tencent_channel": TencentChannelPublisher(),
    "taobao_guanghe": TaobaoGuanghePublisher(),
}


def _get_publisher(platform: str) -> PlatformPublisher:
    if platform not in _PUBLISHERS:
        raise ValueError(f"Unknown platform: {platform}")
    return _PUBLISHERS[platform]


def publish(
    task_id: int,
    *,
    dry_run: bool = False,
    settings: Settings,
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

    pub = _get_publisher(platform)
    return pub.publish_one(task_id, dry_run=dry_run, settings=settings)
