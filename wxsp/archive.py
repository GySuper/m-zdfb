"""日志/截图归档(M9)。

spec §6.3:
  - 日志:loguru 按天滚动,保留 30 天,zip 压缩(loguru 自己干)
  - 失败截图:保留 90 天(loguru 不管,由本模块清)
  - daemon 启动时调一次 `install_file_sink()` + `cleanup_old_files()`

不删 `screenshots/` 顶层目录本身(下次截图就少建一层目录,也避免和别的代码竞态)。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

# 模块级 sink id 列表:重复 install_file_sink 时先移除旧的,避免日志重复行。
_INSTALLED_SINK_IDS: list[int] = []


@dataclass(frozen=True)
class CleanupReport:
    """`cleanup_old_files` 的返回值,给 CLI / daemon 启动日志用。"""

    logs_removed: int = 0
    screenshots_removed: int = 0
    bytes_freed: int = 0


def cleanup_old_files(
    *,
    logs_dir: Path,
    log_retention_days: int,
    screenshot_retention_days: int,
) -> CleanupReport:
    """删 logs_dir 下过保留期的日志 + 截图。

    范围:
      - `logs_dir/*.log*`(原始 + zip 压缩文件)→ `log_retention_days`
      - `logs_dir/screenshots/**/*`(任意扩展名,目录递归)→ `screenshot_retention_days`

    判断:用 `Path.stat().st_mtime`。loguru 滚动后的 .log.zip 同样靠 mtime。
    缺失目录:幂等(直接 return 空报告)。
    """
    logs_removed, log_bytes = _cleanup_logs(logs_dir, log_retention_days)
    shots_removed, shot_bytes = _cleanup_screenshots(
        logs_dir / "screenshots", screenshot_retention_days
    )
    report = CleanupReport(
        logs_removed=logs_removed,
        screenshots_removed=shots_removed,
        bytes_freed=log_bytes + shot_bytes,
    )
    logger.info(
        f"[archive] 清理完成: 日志={report.logs_removed} 截图={report.screenshots_removed} "
        f"释放={report.bytes_freed} bytes"
    )
    return report


def install_file_sink(
    *,
    logs_dir: Path,
    retention_days: int,
    level: str = "INFO",
) -> int:
    """给 loguru 装一个按天滚动的文件 sink。

    spec §6.3:`wxsp.{YYYY-MM-DD}.log`,按天滚 + 保留 N 天 + zip 压缩。多次调用会先卸掉
    之前装的 sink(防止重复行)。返回新的 sink id。

    `level` 默认 INFO;debug 调试时调用方可传 "DEBUG" 提高细度。
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    # 卸旧 sink(重启 daemon、web 模式切 daemon 都会触发)
    for old_id in list(_INSTALLED_SINK_IDS):
        try:
            logger.remove(old_id)
        except ValueError:
            pass
    _INSTALLED_SINK_IDS.clear()

    sink_kwargs: dict[str, Any] = {
        "rotation": "00:00",  # 每天零点切
        "retention": f"{retention_days} days",
        "compression": "zip",
        "encoding": "utf-8",
        "level": level,
        "enqueue": True,  # 多线程/多进程友好
    }
    sink_id = logger.add(str(logs_dir / "wxsp.{time:YYYY-MM-DD}.log"), **sink_kwargs)
    _INSTALLED_SINK_IDS.append(sink_id)
    return sink_id


def _cleanup_logs(logs_dir: Path, retention_days: int) -> tuple[int, int]:
    """清 logs_dir 直属的 *.log* 文件(不递归,避免误删 screenshots/)。"""
    if not logs_dir.is_dir():
        return 0, 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    bytes_freed = 0
    for path in logs_dir.iterdir():
        if not path.is_file():
            continue
        # 只清日志类后缀:wxsp.YYYY-MM-DD.log / .log.gz / .log.zip
        if ".log" not in path.name:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime >= cutoff:
            continue
        try:
            path.unlink()
            removed += 1
            bytes_freed += stat.st_size
        except OSError as exc:
            logger.warning(f"[archive] 删除日志失败 {path}: {exc}")
    return removed, bytes_freed


def _cleanup_screenshots(screenshots_dir: Path, retention_days: int) -> tuple[int, int]:
    """递归清 screenshots_dir 下过期文件 + 空子目录,但保留 screenshots/ 本身。"""
    if not screenshots_dir.is_dir():
        return 0, 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    bytes_freed = 0
    # 用 rglob 找所有文件,删除后再回头扫空目录
    for path in screenshots_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime >= cutoff:
            continue
        try:
            path.unlink()
            removed += 1
            bytes_freed += stat.st_size
        except OSError as exc:
            logger.warning(f"[archive] 删除截图失败 {path}: {exc}")

    # 回收清空后的子目录(深度优先,避免 parent 还有 child 时先删 parent)
    for subdir in sorted(
        (p for p in screenshots_dir.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        try:
            subdir.rmdir()  # 只删空目录;非空会抛 OSError
        except OSError:
            pass
    return removed, bytes_freed
