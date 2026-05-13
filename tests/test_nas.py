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


# ============== stage_to_tmp happy path + 覆盖式 ==============


def test_stage_to_tmp_creates_symlink_pointing_to_src(tmp_path: Path) -> None:
    from wxsp.nas import stage_to_tmp

    src = tmp_path / "videos" / "国庆01.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"fake video bytes")

    tmp_root = tmp_path / "tmp"
    link = stage_to_tmp(src, task_id=42, tmp_root=tmp_root)

    assert link == tmp_root / "42" / "国庆01.mp4"
    assert link.is_symlink()
    assert link.resolve() == src.resolve()
    # 跟读 src 一样能读到 fake bytes
    assert link.read_bytes() == b"fake video bytes"


def test_stage_to_tmp_creates_parent_dirs(tmp_path: Path) -> None:
    """tmp_root 本身不存在时也要自动 mkdir(parents=True)。"""
    from wxsp.nas import stage_to_tmp

    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")

    tmp_root = tmp_path / "does" / "not" / "exist"
    link = stage_to_tmp(src, task_id=1, tmp_root=tmp_root)

    assert link.is_symlink()
    assert link.parent == tmp_root / "1"
    assert link.parent.is_dir()


def test_stage_to_tmp_overwrites_existing_symlink(tmp_path: Path) -> None:
    """同 task_id + 同 src.name 重跑时旧 symlink 被覆盖,不报错;指向变新。"""
    from wxsp.nas import stage_to_tmp

    src_a = tmp_path / "first" / "vid.mp4"
    src_a.parent.mkdir()
    src_a.write_bytes(b"a")
    src_b = tmp_path / "second" / "vid.mp4"
    src_b.parent.mkdir()
    src_b.write_bytes(b"b")

    tmp_root = tmp_path / "tmp"
    link1 = stage_to_tmp(src_a, task_id=7, tmp_root=tmp_root)
    link2 = stage_to_tmp(src_b, task_id=7, tmp_root=tmp_root)

    assert link1 == link2 == tmp_root / "7" / "vid.mp4"
    assert link2.resolve() == src_b.resolve()
    assert link2.read_bytes() == b"b"


def test_stage_to_tmp_preserves_filename_with_spaces_and_unicode(tmp_path: Path) -> None:
    from wxsp.nas import stage_to_tmp

    src = tmp_path / "国庆 短片 01.mov"
    src.write_bytes(b"x")

    link = stage_to_tmp(src, task_id=3, tmp_root=tmp_path / "tmp")

    assert link.name == "国庆 短片 01.mov"
    assert link.is_symlink()
