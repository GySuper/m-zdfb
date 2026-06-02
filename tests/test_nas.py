"""nas.find_video / find_cover tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from wxsp.nas import (
    _apply_path_aliases,
    _is_absolute_path_input,
    find_cover,
    find_video,
)


def test_find_video_nas_drop_raises_nas_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NAS 检索中途掉线(rglob 抛 OSError)→ NasUnreachable,而不是裸 OSError 崩掉整次 sync。"""
    from wxsp.errors import NasUnreachable

    root = tmp_path / "videos"
    root.mkdir()

    def boom(self: Path, pattern: str):  # type: ignore[no-untyped-def]
        raise OSError("Stale file handle")

    monkeypatch.setattr(Path, "rglob", boom)
    with pytest.raises(NasUnreachable):
        find_video("x.mp4", search_root=root)


def test_find_video_missing_file_still_raises_filenotfound(tmp_path: Path) -> None:
    """真·文件不存在仍是 FileNotFoundError(校验失败),不能被误判成 NAS 不可达。"""
    root = tmp_path / "videos"
    root.mkdir()
    with pytest.raises(FileNotFoundError):
        find_video("does-not-exist.mp4", search_root=root)


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


# ============== 路径模式 + path_aliases ==============


def test_is_absolute_path_input_recognizes_common_forms() -> None:
    """覆盖三类绝对路径 + 裸文件名应判 False。"""
    assert _is_absolute_path_input("\\\\172.31.15.11\\share\\x.mp4") is True
    assert _is_absolute_path_input("//host/share/x.mp4") is True
    assert _is_absolute_path_input("/Volumes/nas/x.mp4") is True
    assert _is_absolute_path_input("C:\\videos\\x.mp4") is True
    assert _is_absolute_path_input("Z:/videos/x.mp4") is True
    assert _is_absolute_path_input("x.mp4") is False
    assert _is_absolute_path_input("子目录/x.mp4") is False
    assert _is_absolute_path_input("") is False


def test_apply_path_aliases_empty_dict_returns_unchanged() -> None:
    """aliases={} 等于禁用,不做任何替换。"""
    assert _apply_path_aliases("\\\\host\\share\\x.mp4", {}) == "\\\\host\\share\\x.mp4"
    assert _apply_path_aliases("/Volumes/nas/x.mp4", {}) == "/Volumes/nas/x.mp4"


def test_apply_path_aliases_no_prefix_match_returns_unchanged() -> None:
    """配了 alias 但前缀不匹配 → 原样返回(daemon 在同 OS 上跑就是这种情况)。"""
    aliases = {"\\\\host1\\share": "/Volumes/share"}
    assert _apply_path_aliases("/some/other/path.mp4", aliases) == "/some/other/path.mp4"


def test_apply_path_aliases_unc_to_posix_on_non_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS/Linux 下:UNC 前缀 → POSIX 前缀,残留反斜杠转正斜杠。"""
    monkeypatch.setattr("wxsp.nas.sys.platform", "darwin")
    aliases = {"\\\\172.31.15.11\\dianshang": "/Volumes/dianshang"}
    result = _apply_path_aliases("\\\\172.31.15.11\\dianshang\\sub\\zjn-0511.mp4", aliases)
    assert result == "/Volumes/dianshang/sub/zjn-0511.mp4"


def test_apply_path_aliases_posix_to_unc_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows 下:POSIX 前缀 → UNC 前缀,残留正斜杠转反斜杠。"""
    monkeypatch.setattr("wxsp.nas.sys.platform", "win32")
    aliases = {"\\\\172.31.15.11\\dianshang": "/Volumes/dianshang"}
    result = _apply_path_aliases("/Volumes/dianshang/sub/zjn-0511.mp4", aliases)
    assert result == "\\\\172.31.15.11\\dianshang\\sub\\zjn-0511.mp4"


def test_find_video_with_absolute_posix_path_returns_when_exists(tmp_path: Path) -> None:
    """飞书填完整 POSIX 路径,daemon 跑在 macOS 上 → 直接 Path.exists() 校验。"""
    target = tmp_path / "videos" / "absolute.mp4"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    # search_root 故意指向不存在的目录 —— 路径模式根本不会读它
    result = find_video(str(target), search_root=tmp_path / "nonexistent")
    assert result == target


def test_find_video_with_absolute_path_missing_raises(tmp_path: Path) -> None:
    """路径模式下,目标文件不存在 → FileNotFoundError(与按名搜的"未找到"语义对齐)。"""
    with pytest.raises(FileNotFoundError):
        find_video(str(tmp_path / "ghost.mp4"), search_root=tmp_path)


def test_find_video_with_unc_path_translated_via_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """运营在飞书填 Windows UNC 路径,daemon 跑在 macOS 上 →
    path_aliases 把 UNC 前缀翻成本地 mount,定位到真文件。
    """
    monkeypatch.setattr("wxsp.nas.sys.platform", "darwin")
    real_dir = tmp_path / "mount" / "sub"
    real_dir.mkdir(parents=True)
    real_file = real_dir / "zjn-0511.mp4"
    real_file.write_bytes(b"x")

    aliases = {"\\\\172.31.15.11\\dianshang": str(tmp_path / "mount")}
    input_path = "\\\\172.31.15.11\\dianshang\\sub\\zjn-0511.mp4"

    result = find_video(input_path, search_root=tmp_path / "ignored", path_aliases=aliases)
    assert result == real_file


def test_find_video_filename_mode_unaffected_when_aliases_set(tmp_path: Path) -> None:
    """裸文件名走老逻辑,即便配了 path_aliases 也不应受影响。"""
    root = tmp_path / "videos"
    root.mkdir()
    target = root / "国庆01.mp4"
    target.write_bytes(b"x")
    aliases = {"\\\\host\\share": "/Volumes/share"}

    result = find_video("国庆01.mp4", search_root=root, path_aliases=aliases)
    assert result == target


def test_find_cover_with_absolute_path_works_same_as_video(tmp_path: Path) -> None:
    """find_cover 路径模式语义同 find_video。"""
    target = tmp_path / "covers" / "absolute_cover.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    result = find_cover(str(target), search_root=tmp_path / "nonexistent")
    assert result == target


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


# ============== stage_to_tmp 错误翻译 ==============


def test_stage_to_tmp_falls_back_to_copy_when_symlink_lacks_privilege(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows 默认账户没 symlink 权限会抛 OSError(WinError 1314);
    必须 fallback 到 copy,产物是普通文件而不是 symlink。"""
    from wxsp.nas import stage_to_tmp

    src = tmp_path / "videos" / "v.mp4"
    src.parent.mkdir()
    src.write_bytes(b"real bytes")

    def winerr_1314(self: Path, target: Path | str, target_is_directory: bool = False) -> None:
        # 模拟 Windows 报错: A required privilege is not held by the client
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", winerr_1314)

    out = stage_to_tmp(src, task_id=42, tmp_root=tmp_path / "tmp")

    assert out == tmp_path / "tmp" / "42" / "v.mp4"
    assert not out.is_symlink()  # 走的是 copy 兜底
    assert out.is_file()
    assert out.read_bytes() == b"real bytes"


def test_stage_to_tmp_raises_nas_unreachable_when_both_symlink_and_copy_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """symlink 失败 + copy 也失败(真 NAS 不可达)→ 翻译为 NasUnreachable。"""
    import shutil as _shutil

    from wxsp.errors import NasUnreachable
    from wxsp.nas import stage_to_tmp

    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")

    def symlink_boom(self: Path, target: Path | str, target_is_directory: bool = False) -> None:
        raise PermissionError("symlink not allowed")

    def copy_boom(src_p: object, dst_p: object, *args: object, **kwargs: object) -> str:
        raise OSError("NAS unreachable during copy")

    monkeypatch.setattr(Path, "symlink_to", symlink_boom)
    monkeypatch.setattr(_shutil, "copy2", copy_boom)

    with pytest.raises(NasUnreachable) as exc_info:
        stage_to_tmp(src, task_id=99, tmp_root=tmp_path / "tmp")

    # __cause__ 保留 copy 的失败(最终决定性失败),便于排查
    assert isinstance(exc_info.value.__cause__, OSError)
    assert "NAS unreachable during copy" in str(exc_info.value.__cause__)


def test_stage_to_tmp_translates_mkdir_oserror_to_nas_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mkdir 阶段抛 OSError 也要被翻译(不只是 symlink_to 阶段)。"""
    from wxsp.errors import NasUnreachable
    from wxsp.nas import stage_to_tmp

    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")

    original_mkdir = Path.mkdir

    def crashing_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        if "tmp" in str(self):
            raise OSError("simulated disk full")
        original_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", crashing_mkdir)

    with pytest.raises(NasUnreachable):
        stage_to_tmp(src, task_id=1, tmp_root=tmp_path / "tmp")


# ============== cleanup_tmp ==============


def test_cleanup_tmp_removes_task_dir(tmp_path: Path) -> None:
    from wxsp.nas import cleanup_tmp, stage_to_tmp

    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    tmp_root = tmp_path / "tmp"

    stage_to_tmp(src, task_id=42, tmp_root=tmp_root)
    assert (tmp_root / "42").is_dir()

    cleanup_tmp(task_id=42, tmp_root=tmp_root)
    assert not (tmp_root / "42").exists()


def test_cleanup_tmp_is_idempotent_when_dir_missing(tmp_path: Path) -> None:
    """目录不存在 → 静默返回,不抛异常。"""
    from wxsp.nas import cleanup_tmp

    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    # 调用不存在的 task_id 不抛
    cleanup_tmp(task_id=999, tmp_root=tmp_root)


def test_cleanup_tmp_does_not_touch_sibling_task_dirs(tmp_path: Path) -> None:
    """只删自己的 task_id 目录,不动其他 task 的 stage。"""
    from wxsp.nas import cleanup_tmp, stage_to_tmp

    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    tmp_root = tmp_path / "tmp"

    stage_to_tmp(src, task_id=1, tmp_root=tmp_root)
    stage_to_tmp(src, task_id=2, tmp_root=tmp_root)

    cleanup_tmp(task_id=1, tmp_root=tmp_root)

    assert not (tmp_root / "1").exists()
    assert (tmp_root / "2").is_dir()  # 兄弟没动
