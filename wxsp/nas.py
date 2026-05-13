"""NAS 文件检索 + stage_to_tmp + cleanup_tmp(M3 含 find_*,M4 补 stage/cleanup)。"""

from __future__ import annotations

from pathlib import Path


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
