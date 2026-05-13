"""视频号发布核心 —— 20 步串行,patchright 驱动(M5)。"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from loguru import logger
from patchright.sync_api import Page


class AlreadyClaimed(Exception):
    """task 已被其它 worker 占用 / 不在可执行状态 —— claim_task 返回 False。"""


@dataclass
class PublishResult:
    task_id: int
    ok: bool
    dry_run: bool
    remote_url: str | None = None
    remote_video_id: str | None = None
    error_type: str | None = None
    error_msg: str | None = None
    screenshots: list[str] = field(default_factory=list)


def screenshot(
    page: Page,
    *,
    task_id: int,
    step: str,
    screenshots_root: Path,
    now: datetime | None = None,
) -> Path:
    """保存截图到 `screenshots_root/{YYYYMM}/{task_id}_{step}.png`,返回路径。

    `screenshots_root` 通常是 `logs/screenshots`(由 settings.app.logs_dir 派生);
    `now` 注入用于测试,默认 `datetime.now()`。截图自身失败不抛(避免掩盖原始错误)。
    """
    now = now or datetime.now()
    month_dir = screenshots_root / now.strftime("%Y%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    path = month_dir / f"{task_id}_{step}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
    except Exception as exc:
        logger.warning(f"截图失败 task_id={task_id} step={step}: {exc}")
    return path


def random_pause(
    range_seconds: tuple[float, float],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """步骤间 1-3 秒随机停顿(模拟人工);`sleep` 注入用于测试。"""
    low, high = range_seconds
    sleep(random.uniform(low, high))
