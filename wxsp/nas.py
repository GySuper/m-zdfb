"""NAS 文件检索 + stage_to_tmp + cleanup_tmp(M3 含 find_*,M4 补 stage/cleanup)。"""

from __future__ import annotations

import shutil
from pathlib import Path

from loguru import logger

from wxsp.errors import NasUnreachable


def _find(filename: str, search_root: Path) -> Path:
    """通用实现:在 search_root 下递归找 filename,多匹配取 mtime 最新。"""
    if not search_root.exists():
        raise FileNotFoundError(filename)
    matches = list(search_root.rglob(filename))
    if not matches:
        raise FileNotFoundError(filename)
    return max(matches, key=lambda p: p.stat().st_mtime)


def find_video(filename: str, *, search_root: Path) -> Path:
    """在 search_root 下递归找视频文件;多匹配取 mtime 最新;0 匹配 raise FileNotFoundError。"""
    return _find(filename, search_root)


def find_cover(filename: str, *, search_root: Path) -> Path:
    """在 search_root 下递归找封面文件;语义同 find_video。"""
    return _find(filename, search_root)


def stage_to_tmp(src: Path, *, task_id: int, tmp_root: Path) -> Path:
    """在 tmp_root/{task_id}/ 下暂存 src(优先 symlink,失败 fallback 到 copy)。

    - tmp_root/{task_id}/ 不存在则自动 mkdir(parents=True, exist_ok=True)
    - 暂存文件名等于 src.name(保留原文件名,便于 debug)
    - 已存在同名 symlink/文件 → 先 unlink 再写(覆盖式;同 task_id 重跑安全)
    - 优先 symlink_to;Windows 默认账户没 SeCreateSymbolicLinkPrivilege 会抛 OSError
      → fallback 到 shutil.copy2;log 一条 warn 提示用户考虑开发者模式
    - 连 copy 都失败(真 NAS 抖动)→ NasUnreachable,M5 retry 装饰器统一处理
    """
    stage_dir = tmp_root / str(task_id)
    try:
        stage_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise NasUnreachable(f"stage_to_tmp 失败 src={src!s} task_id={task_id}: {exc}") from exc

    out_path = stage_dir / src.name
    if out_path.is_symlink() or out_path.exists():
        out_path.unlink()

    try:
        out_path.symlink_to(src)
        return out_path
    except OSError as symlink_exc:
        # Windows 上无权限 (WinError 1314) 是最常见原因;不区分细分类,统一兜底
        logger.warning(
            f"[nas] symlink 失败,fallback 到 copy(src={src.name} task_id={task_id}): "
            f"{symlink_exc}"
        )
        try:
            shutil.copy2(src, out_path)
            return out_path
        except OSError as copy_exc:
            raise NasUnreachable(
                f"stage_to_tmp 失败(symlink + copy 都失败) src={src!s} task_id={task_id}: "
                f"{copy_exc}"
            ) from copy_exc


def cleanup_tmp(*, task_id: int, tmp_root: Path) -> None:
    """删除 tmp_root/{task_id}/ 目录。

    - 目录不存在 → 静默返回(幂等,同 task_id 多次清理安全)
    - 其他 OSError → 抛出原始异常(本地文件系统问题,不翻译为 NasUnreachable)
    """
    stage_dir = tmp_root / str(task_id)
    try:
        shutil.rmtree(stage_dir)
    except FileNotFoundError:
        pass  # 幂等:已经被清过了
