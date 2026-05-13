"""nas.find_video / find_cover tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from wxsp.nas import find_cover, find_video


def test_find_video_single_match(tmp_path: Path) -> None:
    root = tmp_path / "videos"
    (root / "a").mkdir(parents=True)
    target = root / "a" / "国庆01.mp4"
    target.write_bytes(b"x")

    result = find_video("国庆01.mp4", search_root=root)
    assert result == target


def test_find_video_multi_match_returns_newest_mtime(tmp_path: Path) -> None:
    root = tmp_path / "videos"
    (root / "old").mkdir(parents=True)
    (root / "new").mkdir(parents=True)
    older = root / "old" / "dup.mp4"
    newer = root / "new" / "dup.mp4"
    older.write_bytes(b"x")
    newer.write_bytes(b"y")
    # 把 older 的 mtime 强制设到过去
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    result = find_video("dup.mp4", search_root=root)
    assert result == newer


def test_find_video_no_match_raises(tmp_path: Path) -> None:
    root = tmp_path / "videos"
    root.mkdir()
    with pytest.raises(FileNotFoundError):
        find_video("missing.mp4", search_root=root)


def test_find_cover_same_semantics(tmp_path: Path) -> None:
    root = tmp_path / "covers"
    (root / "deep" / "sub").mkdir(parents=True)
    target = root / "deep" / "sub" / "cover.jpg"
    target.write_bytes(b"x")

    result = find_cover("cover.jpg", search_root=root)
    assert result == target


def test_find_video_empty_root_raises(tmp_path: Path) -> None:
    # search_root 不存在 → rglob 抛 OSError 或返回空,统一表现为 FileNotFoundError
    root = tmp_path / "nonexistent"
    with pytest.raises(FileNotFoundError):
        find_video("any.mp4", search_root=root)
