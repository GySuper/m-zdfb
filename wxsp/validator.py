"""入库校验(纯函数)(M3)。

设计要点(详见 docs/superpowers/specs/2026-05-12-m3-feishu-sync.md):
  - 纯函数:validate(row, *, config, now, nas_finder, active_account_ids) -> ValidationResult
  - 字段独立校验,错误全部收集(不在第一个错就 return)
  - 时区:publish_at / execute_date 解析后落 naive Asia/Shanghai
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from wxsp.config import Settings
    from wxsp.feishu import BitableRow


@dataclass(frozen=True)
class FieldError:
    """单个字段的校验错误。field 填飞书原字段中文名(运营在飞书侧能直接对照)。"""

    field: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    """validate() 的返回值。ok=True 时业务字段填充,errors 为空;ok=False 时反之。"""

    ok: bool
    video_path: Path | None = None
    cover_path: Path | None = None
    title: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    topic: str | None = None
    original_claim: bool = False
    account_id: str | None = None
    execute_date: date | None = None
    publish_at: datetime | None = None
    errors: list[FieldError] = field(default_factory=list)


class NasFinder(Protocol):
    """validator 依赖的 NAS 检索接口。生产实现走 wxsp.nas;测试可直接造 stub。"""

    def find_video(self, filename: str) -> Path: ...
    def find_cover(self, filename: str) -> Path: ...


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_TITLE_MIN = 16
_TITLE_MAX = 30
_TAGS_MAX = 5
_VIDEO_EXTENSIONS = {".mp4", ".mov"}
_VIDEO_MAX_BYTES = 4 * 1024**3  # 4 GiB


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------


def validate(
    row: BitableRow,
    *,
    config: Settings,
    now: datetime,
    nas_finder: NasFinder,
    active_account_ids: set[str],
) -> ValidationResult:
    """逐字段校验,错误全部收集。所有规则独立运行,不在第一个错就 return。"""
    fm = config.feishu.field_map
    errors: list[FieldError] = []

    title = _check_title(row.fields, fm.title, errors)
    tags = _check_tags(row.fields, fm.tags, errors)
    description = _get_str(row.fields.get(fm.description))
    topic = _get_str(row.fields.get(fm.topic))
    original_claim = bool(row.fields.get(fm.original_claim) or False)
    account_id = _check_account(row.fields, fm.account, active_account_ids, errors)
    video_path = _check_video(row.fields, fm.video_file, nas_finder, errors)
    cover_path = _check_cover(row.fields, fm.cover, nas_finder, errors)

    if errors:
        return ValidationResult(ok=False, errors=errors)
    return ValidationResult(
        ok=True,
        title=title,
        description=description,
        tags=tags,
        topic=topic,
        original_claim=original_claim,
        account_id=account_id,
        video_path=video_path,
        cover_path=cover_path,
    )


# ---------------------------------------------------------------------------
# 私有 helpers
# ---------------------------------------------------------------------------


def _check_title(fields: dict[str, Any], field_name: str, errors: list[FieldError]) -> str | None:
    raw = fields.get(field_name)
    if not raw or not isinstance(raw, str):
        errors.append(FieldError(field=field_name, message="未指定"))
        return None
    n = len(raw)
    if n < _TITLE_MIN or n > _TITLE_MAX:
        errors.append(
            FieldError(field=field_name, message=f"{n} 字(要求 {_TITLE_MIN}-{_TITLE_MAX} 字)")
        )
        return None
    return raw


def _check_tags(fields: dict[str, Any], field_name: str, errors: list[FieldError]) -> list[str]:
    raw = fields.get(field_name) or []
    tags: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                tags.append(text)
        elif isinstance(item, str):
            tags.append(item)
    if len(tags) > _TAGS_MAX:
        errors.append(FieldError(field=field_name, message=f"{len(tags)} 个(最多 {_TAGS_MAX} 个)"))
    return tags


def _check_account(
    fields: dict[str, Any],
    field_name: str,
    active_account_ids: set[str],
    errors: list[FieldError],
) -> str | None:
    raw = fields.get(field_name)
    account_id = _coerce_select(raw)
    if not account_id:
        errors.append(FieldError(field=field_name, message="未指定"))
        return None
    if account_id not in active_account_ids:
        errors.append(FieldError(field=field_name, message=f"{account_id!r} 不存在或已停用"))
        return None
    return account_id


def _coerce_select(raw: Any) -> str | None:
    """单选字段可能返回 dict {'text': ...} 或字符串。"""
    if isinstance(raw, dict):
        text = raw.get("text")
        return text if isinstance(text, str) else None
    if isinstance(raw, str):
        return raw or None
    return None


def _get_str(raw: Any) -> str | None:
    if isinstance(raw, str) and raw:
        return raw
    return None


def _check_video(
    fields: dict[str, Any],
    field_name: str,
    nas_finder: NasFinder,
    errors: list[FieldError],
) -> Path | None:
    raw = _get_str(fields.get(field_name))
    if not raw:
        errors.append(FieldError(field=field_name, message="未指定"))
        return None
    try:
        path = nas_finder.find_video(raw)
    except FileNotFoundError:
        errors.append(FieldError(field=field_name, message=f"未在 NAS 下找到 {raw!r}"))
        return None
    if path.suffix.lower() not in _VIDEO_EXTENSIONS:
        errors.append(
            FieldError(
                field=field_name,
                message=f"不支持的扩展名 {path.suffix!r}(允许 .mp4/.mov)",
            )
        )
        return None
    size = path.stat().st_size
    if size > _VIDEO_MAX_BYTES:
        gib = size / 1024**3
        errors.append(FieldError(field=field_name, message=f"{gib:.2f} GiB 超出 4 GiB 上限"))
        return None
    return path


def _check_cover(
    fields: dict[str, Any],
    field_name: str,
    nas_finder: NasFinder,
    errors: list[FieldError],
) -> Path | None:
    raw = _get_str(fields.get(field_name))
    if not raw:
        return None  # 封面可空
    try:
        return nas_finder.find_cover(raw)
    except FileNotFoundError:
        errors.append(FieldError(field=field_name, message=f"未在 NAS 下找到 {raw!r}"))
        return None
