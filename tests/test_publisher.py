"""publisher 模块单元测试(helpers + publish() 顶层编排)。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

from wxsp.publisher import PublishResult, random_pause, screenshot


def test_screenshot_writes_to_yyyymm_subdir(tmp_path: Path) -> None:
    page = MagicMock()
    out = screenshot(
        page,
        task_id=42,
        step="upload",
        screenshots_root=tmp_path,
        now=datetime(2026, 5, 13, 10, 30),
    )
    assert out == tmp_path / "202605" / "42_upload.png"
    assert out.parent.is_dir()
    page.screenshot.assert_called_once_with(path=str(out), full_page=False)


def test_screenshot_does_not_propagate_page_errors(tmp_path: Path) -> None:
    page = MagicMock()
    page.screenshot.side_effect = RuntimeError("浏览器已关")
    # 不抛,因为截图失败不该掩盖业务异常
    out = screenshot(page, task_id=1, step="x", screenshots_root=tmp_path, now=datetime(2026, 1, 1))
    assert out.name == "1_x.png"  # 路径仍返回


def test_random_pause_uses_injected_sleep_within_range() -> None:
    sleeps: list[float] = []
    random_pause((1.0, 3.0), sleep=sleeps.append)
    assert len(sleeps) == 1
    assert 1.0 <= sleeps[0] <= 3.0


def test_publish_result_defaults() -> None:
    r = PublishResult(task_id=1, ok=False, dry_run=True)
    assert r.remote_url is None
    assert r.screenshots == []
