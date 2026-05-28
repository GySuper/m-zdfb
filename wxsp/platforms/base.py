"""PlatformPublisher protocol + shared types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from wxsp.config import Settings
from wxsp.models import Account


@dataclass
class PublishResult:
    """发布一条任务的结果。"""

    task_id: int
    ok: bool
    dry_run: bool
    remote_url: str | None = None
    remote_video_id: str | None = None
    error_type: str | None = None
    error_msg: str | None = None
    screenshots: list[str] = field(default_factory=list)


class PlatformPublisher(Protocol):
    """Each platform's publish + login implementation.

    Platform implementations only handle browser interaction:
    "open browser → fill form → click publish".
    DB writes, notifications, and Feishu callbacks are handled by the caller (publisher.py).
    """

    platform_key: str

    def publish_one(
        self,
        task_id: int,
        *,
        dry_run: bool = False,
        settings: Settings,
    ) -> PublishResult:
        """Execute publishing steps for one task."""
        ...

    def login(self, account: Account, settings: Settings) -> bool:
        """Open browser and let user log in. Returns True on success."""
        ...
