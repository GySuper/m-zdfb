"""NAS 文件检索 + stage_to_tmp + cleanup_tmp(M3 含 find_*,M4 补 stage/cleanup)。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from loguru import logger

from wxsp.errors import NasUnreachable


def _is_absolute_path_input(raw: str) -> bool:
    """判定运营填的串是"完整路径"而非"裸文件名"。
    覆盖:UNC `\\\\host\\share\\...`、Windows 盘符 `X:\\...`、POSIX 绝对 `/...`。
    任一命中走"路径模式":不走 rglob,直接 alias 翻译 + Path.exists()。
    """
    if not raw:
        return False
    if raw.startswith(("\\\\", "//")):
        return True
    if raw.startswith("/"):
        return True
    if len(raw) >= 3 and raw[0].isalpha() and raw[1] == ":" and raw[2] in ("\\", "/"):
        return True
    return False


def _apply_path_aliases(raw: str, aliases: dict[str, str]) -> str:
    """按当前 OS 把前缀互译:
    - macOS/Linux:命中 key(UNC/Windows 前缀) → 替换为 value(POSIX mount),并把残留反斜杠转正斜杠
    - Windows:命中 value(POSIX 前缀) → 替换为 key(UNC),并把残留正斜杠转反斜杠
    无命中返回原串。前缀以"是否 startswith"为唯一判定。
    """
    if not aliases:
        return raw
    is_windows = sys.platform == "win32"
    for win_prefix, posix_prefix in aliases.items():
        if is_windows:
            if posix_prefix and raw.startswith(posix_prefix):
                tail = raw[len(posix_prefix) :].replace("/", "\\")
                return win_prefix + tail
        else:
            if win_prefix and raw.startswith(win_prefix):
                tail = raw[len(win_prefix) :].replace("\\", "/")
                return posix_prefix + tail
    return raw


def _resolve_absolute(raw: str, aliases: dict[str, str]) -> Path:
    """路径模式:alias 翻译 + 存在性校验。
    不存在 → FileNotFoundError(与按名搜的"未找到"语义对齐,validator 错误消息可统一)。
    """
    translated = _apply_path_aliases(raw, aliases)
    path = Path(translated)
    if not path.exists():
        raise FileNotFoundError(translated)
    return path


def _find(filename: str, search_root: Path) -> Path:
    """通用实现:在 search_root 下递归找 filename,多匹配取 mtime 最新。"""
    if not search_root.exists():
        # 账号配置的检索根都不存在 → 几乎一定是 NAS 没挂上(而非"文件不存在"),
        # 归 nas_unreachable 走重试 + 告警,别误判成校验失败回写飞书(CLAUDE.md §5)。
        raise NasUnreachable(f"检索根不可达(NAS 未挂载?): {search_root}")
    try:
        matches = list(search_root.rglob(filename))
        if not matches:
            raise FileNotFoundError(filename)
        return max(matches, key=lambda p: p.stat().st_mtime)
    except FileNotFoundError:
        raise  # 真·没找到 = 校验失败,保持原语义
    except OSError as exc:
        # rglob / stat 中途抛 OSError(NAS 掉线 ESTALE/ENETDOWN 等)→ 归类 nas_unreachable,
        # 不能让裸 OSError 冒泡崩掉整次 sync(CLAUDE.md §5)。
        raise NasUnreachable(f"NAS 检索 {filename} 时不可达: {exc}") from exc


def find_video(
    filename: str,
    *,
    search_root: Path,
    path_aliases: dict[str, str] | None = None,
) -> Path:
    """检索视频文件。
    - 输入像绝对路径(UNC / 盘符 / POSIX 绝对) → 走"路径模式":alias 翻译 + Path.exists()
    - 否则走"按文件名搜索"模式:在 search_root 下递归找;多匹配取 mtime 最新
    任一模式未命中均 raise FileNotFoundError(translated 路径或文件名)。
    """
    if _is_absolute_path_input(filename):
        return _resolve_absolute(filename, path_aliases or {})
    return _find(filename, search_root)


def find_cover(
    filename: str,
    *,
    search_root: Path,
    path_aliases: dict[str, str] | None = None,
) -> Path:
    """检索封面文件;语义同 find_video(同样支持路径/文件名两种模式)。"""
    if _is_absolute_path_input(filename):
        return _resolve_absolute(filename, path_aliases or {})
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
        logger.warning(f"[nas] 创建暂存目录失败 src={src!s} task_id={task_id}: {exc}")
        raise NasUnreachable(f"暂存目录创建失败,可能本地磁盘满 / 没权限({stage_dir})") from exc

    out_path = stage_dir / src.name
    if out_path.is_symlink() or out_path.exists():
        out_path.unlink()

    try:
        out_path.symlink_to(src)
        return out_path
    except OSError as symlink_exc:
        # Windows 上无权限 (WinError 1314) 是最常见原因;不区分细分类,统一兜底
        logger.warning(
            f"[nas] symlink 失败,fallback 到 copy(src={src.name} task_id={task_id}): {symlink_exc}"
        )
        try:
            shutil.copy2(src, out_path)
            return out_path
        except OSError as copy_exc:
            logger.warning(f"[nas] copy2 也失败 src={src!s} task_id={task_id}: {copy_exc}")
            raise NasUnreachable(
                f"NAS 视频文件读取失败,NAS 可能掉线或文件已被删除({src.name})"
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
