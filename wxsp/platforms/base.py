"""PlatformPublisher protocol + shared types.

类型定义集中处(无业务逻辑);编排逻辑见 `wxsp/platforms/runner.py`。
依赖方向:base(类型) ← runner(编排) ← 各平台 adapter ← publisher(路由)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from wxsp.config import Settings
from wxsp.models import Account

if TYPE_CHECKING:
    from collections.abc import Callable

    from patchright.sync_api import Page


class AlreadyClaimed(Exception):
    """task 已被其它 worker 占用 / 不在可执行状态."""


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


@dataclass
class TaskBundle:
    """从 DB 加载的 Task/Video/Account 快照(在 session 内取值,detached 安全)。

    超集字段:不同平台各取所需(视频号读 tags/cover/original,淘宝读 product/declaration)。
    JSON 字段保留原始字符串,由各平台回调自行解析(保留各平台特有的兼容逻辑)。
    """

    task_id: int
    platform: str
    publish_at: datetime
    account_id: str
    user_data_dir: Path
    video_record_id: str
    video_file_path: Path
    video_cover_path: Path | None
    title: str
    description: str | None
    topic: str | None
    original_claim: bool
    tags_json: str
    product_ids_json: str
    declaration: str | None
    ai_optimize: bool


@dataclass
class PublishContext:
    """一次发布运行期的可变上下文,跨编排器与平台回调共享。"""

    task_id: int
    result: PublishResult
    dry_run: bool
    step_pause: tuple[float, float]
    screenshots_root: Path
    tmp_root: Path
    settings: Settings
    last_step: str = "init"


@dataclass
class PlatformSpec:
    """平台向共享编排器声明的接口:两段步骤回调 + 元信息。

    pre_publish:打开页 → 填表 → 风控探测(止于 dry-run gate 之前)。
    post_publish:点发布 → 等成功 →(可选)抽取 remote_url(写入 ctx.result)。
    display_name:中文平台名,用于"元素未找到"告警标题(如 "视频号" / "淘宝光合")。
    """

    platform_key: str
    display_name: str
    pre_publish: Callable[[Page, TaskBundle, Path, PublishContext], None]
    post_publish: Callable[[Page, TaskBundle, PublishContext], None]


class PlatformPublisher(Protocol):
    """Each platform's publish + login implementation.

    Platform implementations only handle browser interaction:
    "open browser → fill form → click publish".
    DB writes, notifications, and Feishu callbacks are handled by the shared
    orchestrator (`wxsp/platforms/runner.py::run_publish`).
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

    def login(self, account: Account) -> bool:
        """Open browser and let user log in. Returns True on success.

        登录只需打开浏览器到平台首页等扫码,不依赖 Settings(故不接 settings 参数)。
        """
        ...
