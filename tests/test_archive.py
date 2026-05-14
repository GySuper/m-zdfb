"""archive.cleanup_old_files 单元测试(M9)。

策略:
- 直接传 logs_dir / 保留天数,不构造完整 Settings(让函数纯一点更好测)
- 用 os.utime 把测试文件 mtime 改成几天前
- 验证:过期被删、未过期保留、空目录回收、缺失目录幂等
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path


def _set_age_days(path: Path, days: int) -> None:
    """把文件 mtime 改成 N 天前。"""
    target = (datetime.now() - timedelta(days=days)).timestamp()
    os.utime(path, (target, target))


def test_cleanup_removes_old_screenshot(tmp_path: Path) -> None:
    from wxsp.archive import cleanup_old_files

    logs_dir = tmp_path / "logs"
    shots = logs_dir / "screenshots" / "202602"
    shots.mkdir(parents=True)
    old = shots / "100_step.png"
    old.write_bytes(b"x" * 10)
    _set_age_days(old, 100)  # 超 90 天

    report = cleanup_old_files(
        logs_dir=logs_dir,
        log_retention_days=30,
        screenshot_retention_days=90,
    )

    assert not old.exists()
    assert report.screenshots_removed == 1


def test_cleanup_keeps_recent_screenshot(tmp_path: Path) -> None:
    from wxsp.archive import cleanup_old_files

    logs_dir = tmp_path / "logs"
    shots = logs_dir / "screenshots" / "202605"
    shots.mkdir(parents=True)
    recent = shots / "200_step.png"
    recent.write_bytes(b"y")
    _set_age_days(recent, 5)  # 5 天前

    report = cleanup_old_files(
        logs_dir=logs_dir,
        log_retention_days=30,
        screenshot_retention_days=90,
    )

    assert recent.exists()
    assert report.screenshots_removed == 0


def test_cleanup_removes_old_log_file(tmp_path: Path) -> None:
    from wxsp.archive import cleanup_old_files

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    old_log = logs_dir / "wxsp.2025-12-01.log"
    old_log.write_text("old")
    _set_age_days(old_log, 40)

    report = cleanup_old_files(
        logs_dir=logs_dir,
        log_retention_days=30,
        screenshot_retention_days=90,
    )

    assert not old_log.exists()
    assert report.logs_removed == 1


def test_cleanup_keeps_recent_log(tmp_path: Path) -> None:
    from wxsp.archive import cleanup_old_files

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    today_log = logs_dir / "wxsp.2026-05-14.log"
    today_log.write_text("now")
    _set_age_days(today_log, 1)

    report = cleanup_old_files(
        logs_dir=logs_dir,
        log_retention_days=30,
        screenshot_retention_days=90,
    )

    assert today_log.exists()
    assert report.logs_removed == 0


def test_cleanup_removes_zipped_log(tmp_path: Path) -> None:
    """loguru 滚动后会压缩成 .log.gz / .log.zip,过期也要清。"""
    from wxsp.archive import cleanup_old_files

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    zipped = logs_dir / "wxsp.2025-04-01.log.zip"
    zipped.write_bytes(b"PK\x03\x04")
    _set_age_days(zipped, 60)

    report = cleanup_old_files(
        logs_dir=logs_dir,
        log_retention_days=30,
        screenshot_retention_days=90,
    )

    assert not zipped.exists()
    assert report.logs_removed == 1


def test_cleanup_removes_empty_screenshot_subdir(tmp_path: Path) -> None:
    """所有截图都删干净后,空的 YYYYMM 子目录也要回收。"""
    from wxsp.archive import cleanup_old_files

    logs_dir = tmp_path / "logs"
    shots_subdir = logs_dir / "screenshots" / "202601"
    shots_subdir.mkdir(parents=True)
    old = shots_subdir / "1_step.png"
    old.write_bytes(b"x")
    _set_age_days(old, 200)

    cleanup_old_files(
        logs_dir=logs_dir,
        log_retention_days=30,
        screenshot_retention_days=90,
    )

    assert not old.exists()
    assert not shots_subdir.exists()
    # screenshots/ 本身保留(还是个稳定的根目录)
    assert (logs_dir / "screenshots").exists()


def test_cleanup_handles_missing_dirs_gracefully(tmp_path: Path) -> None:
    """没有 logs/ 或没有 screenshots/ 应该幂等,不抛。"""
    from wxsp.archive import cleanup_old_files

    nonexistent = tmp_path / "nope"
    report = cleanup_old_files(
        logs_dir=nonexistent,
        log_retention_days=30,
        screenshot_retention_days=90,
    )
    assert report.logs_removed == 0
    assert report.screenshots_removed == 0


def test_install_file_sink_writes_to_dated_log_file(tmp_path: Path) -> None:
    """loguru sink 安装后,后续日志能落到 wxsp.YYYY-MM-DD.log。"""
    from loguru import logger

    from wxsp.archive import install_file_sink

    logs_dir = tmp_path / "logs"
    sink_id = install_file_sink(logs_dir=logs_dir, retention_days=30)
    try:
        logger.info("smoke-msg-2026-05-14")
        logger.complete()  # 等 enqueue 队列冲刷
    finally:
        logger.remove(sink_id)

    log_files = list(logs_dir.glob("wxsp.*.log"))
    assert log_files, f"找不到 wxsp.*.log,目录内容: {list(logs_dir.iterdir())}"
    content = log_files[0].read_text(encoding="utf-8")
    assert "smoke-msg-2026-05-14" in content


def test_install_file_sink_idempotent_removes_prior_sink(tmp_path: Path) -> None:
    """重复装 sink 应卸旧装新,不留下两份 sink 重复写。"""
    from loguru import logger

    from wxsp.archive import install_file_sink

    logs_dir = tmp_path / "logs"
    install_file_sink(logs_dir=logs_dir, retention_days=30)
    second_id = install_file_sink(logs_dir=logs_dir, retention_days=30)

    try:
        logger.info("just-once")
        logger.complete()
    finally:
        logger.remove(second_id)

    log_file = next(iter(logs_dir.glob("wxsp.*.log")))
    occurrences = log_file.read_text(encoding="utf-8").count("just-once")
    assert occurrences == 1


def test_cleanup_does_not_touch_other_files(tmp_path: Path) -> None:
    """只清 *.log* 和 screenshots/**/*.png,其它(如 README.md)不动。"""
    from wxsp.archive import cleanup_old_files

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    weird = logs_dir / "notes.txt"
    weird.write_text("important manual notes")
    _set_age_days(weird, 365)

    cleanup_old_files(
        logs_dir=logs_dir,
        log_retention_days=30,
        screenshot_retention_days=90,
    )

    assert weird.exists()  # .txt 不在清理范围内
