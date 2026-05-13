"""NAS 文件检索 + stage_to_tmp + cleanup_tmp(M3 含 find_*,M4 补 stage/cleanup)。"""

from __future__ import annotations

from pathlib import Path

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
    """在 tmp_root/{task_id}/ 下建 symlink 指向 src,返回 symlink 路径。

    - tmp_root/{task_id}/ 不存在则自动 mkdir(parents=True, exist_ok=True)
    - symlink 名等于 src.name(保留原文件名,便于 debug)
    - 已存在同名 symlink/文件 → 先 unlink 再 symlink_to(覆盖式;同 task_id 重跑安全)
    - 任意 OSError → NasUnreachable(M5 retry 装饰器统一处理)
    """
    try:
        stage_dir = tmp_root / str(task_id)
        stage_dir.mkdir(parents=True, exist_ok=True)
        link_path = stage_dir / src.name
        if link_path.is_symlink() or link_path.exists():
            link_path.unlink()
        link_path.symlink_to(src)
        return link_path
    except OSError as exc:
        raise NasUnreachable(f"stage_to_tmp 失败 src={src!s} task_id={task_id}: {exc}") from exc
