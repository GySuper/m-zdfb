"""入库校验(纯函数)(M3)。

设计要点(详见 docs/superpowers/specs/2026-05-12-m3-feishu-sync.md):
  - 纯函数:validate(row, *, config, now, nas_finder, active_account_ids) -> ValidationResult
  - 字段独立校验,错误全部收集(不在第一个错就 return)
  - 时区:publish_at / execute_date 解析后落 naive Asia/Shanghai
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

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
    """validate() 的返回值。

    三种结果:
    - ok=True:校验通过,业务字段填充。
    - ok=False, incomplete=True:4 个核心字段(执行日期/定时发布时间/标题/视频文件)
      有一个为空 → 业务还没填完,sync 跳过该行,不回写飞书,不算失败。
    - ok=False, incomplete=False:字段填了但校验失败(标题超长、视频文件不存在、
      日期早于 now+30min 等)→ sync 算失败,回写飞书"失败 + 错因"。
    """

    ok: bool
    incomplete: bool = False
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
    # taobao-specific
    declaration: str | None = None
    ai_optimize: bool = False
    product_ids: list[str] = field(default_factory=list)


class NasFinder(Protocol):
    """validator 依赖的 NAS 检索接口。生产实现走 wxsp.nas;测试可直接造 stub。

    search_root 由 validator 根据 row 上的账号 ID 决定后传入(每账号路径可不同)。
    `path_aliases` 走"完整路径"分支(UNC ↔ POSIX 前缀互译);裸文件名时被 ignored。
    """

    def find_video(
        self,
        filename: str,
        *,
        search_root: Path,
        path_aliases: dict[str, str] | None = ...,
    ) -> Path: ...
    def find_cover(
        self,
        filename: str,
        *,
        search_root: Path,
        path_aliases: dict[str, str] | None = ...,
    ) -> Path: ...


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_TITLE_MAX = 30
_TAGS_MAX = 5


def _title_min_for(platform: str) -> int:
    """标题最小字数,取自 wxsp.platform_meta(视频号 16,淘宝光合无限制取 1)。"""
    from wxsp.platform_meta import get_meta

    return get_meta(platform).title_min


_VIDEO_EXTENSIONS = {".mp4", ".mov"}
_VIDEO_MAX_BYTES = 4 * 1024**3  # 4 GiB
_PUBLISH_MIN_DELTA = timedelta(minutes=30)
_PUBLISH_MAX_DELTA = timedelta(days=14)
_SHANGHAI = ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------


def validate(
    row: BitableRow,
    *,
    config: Settings,
    now: datetime,
    nas_finder: NasFinder,
    active_accounts: dict[str, str],
    platform: str = "tencent_channel",
) -> ValidationResult:
    """逐字段校验,错误全部收集。所有规则独立运行,不在第一个错就 return。

    active_accounts: {account_id: display_name},只含 DB 中 is_active 的账号。
    飞书"账号"字段允许写 account_id 或 display_name 任一形式,validator 反查。
    """
    fm = config.feishu.field_map

    # ① 完整性前置检查:4 个核心字段任一为空 → incomplete=True 返回。
    #    业务还没填完的草稿(运营在飞书慢慢补),sync 跳过且不回写"失败",等下次再拉。
    #    其他字段(描述/标签/封面/合集/原创)允许为空走默认值;**账号必填**
    #    (空 → _check_account 判失败回写"未指定",不做 round-robin 自动分配)。
    if _is_incomplete(row.fields, fm):
        return ValidationResult(ok=False, incomplete=True)

    errors: list[FieldError] = []

    title = _check_title(row.fields, fm.title, errors, platform=platform)
    tags = _check_tags(row.fields, fm.tags, errors)
    description = _get_str(row.fields.get(fm.description))
    topic = _get_str(row.fields.get(fm.topic))
    original_claim = bool(row.fields.get(fm.original_claim) or False)
    account_id = _check_account(row.fields, fm.account, active_accounts, errors)
    account_cfg = config.accounts.get(account_id) if account_id else None
    path_aliases = config.paths.path_aliases
    video_path = _check_video(
        row.fields, fm.video_file, nas_finder, account_cfg, errors, path_aliases
    )
    cover_path = _check_cover(row.fields, fm.cover, nas_finder, account_cfg, errors, path_aliases)
    execute_date = _check_execute_date(row.fields, fm.execute_date, errors)
    publish_at = _check_publish_at(row.fields, fm.publish_at, now, execute_date, errors)

    # Taobao-specific fields
    declaration_raw = row.fields.get(fm.declaration, "")
    declaration = _get_str(declaration_raw) if declaration_raw else "内容无需标注"
    if not declaration:
        declaration = "内容无需标注"
    valid_declarations = {
        "内容无需标注",
        "含AI生成内容",
        "含虚构演绎内容",
        "内容为转载",
        "个人观点，仅供参考",  # noqa: RUF001
        "内容含营销信息",
    }
    if declaration and declaration not in valid_declarations:
        errors.append(FieldError("创作者声明", f"'{declaration}' 不在有效选项中"))
        declaration = "内容无需标注"

    ai_str = row.fields.get(fm.ai_optimize, "")
    ai_optimize = (
        str(ai_str).strip().lower() in ("true", "是", "yes", "1", "✓") if ai_str else False
    )

    product_ids_str = (
        _get_str(row.fields.get(fm.product_ids)) if row.fields.get(fm.product_ids) else ""
    )
    product_ids: list[str] = []
    if product_ids_str:
        product_ids = [
            p.strip()
            for p in product_ids_str.replace("，", ",").split(",")  # noqa: RUF001
            if p.strip()
        ]
    if len(product_ids) > 6:
        errors.append(FieldError("商品ID", f"{len(product_ids)} 个商品(最多 6 个)"))
        product_ids = product_ids[:6]

    if errors:
        return ValidationResult(
            ok=False,
            errors=errors,
            declaration=declaration,
            ai_optimize=ai_optimize,
            product_ids=product_ids,
        )
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
        execute_date=execute_date,
        publish_at=publish_at,
        declaration=declaration,
        ai_optimize=ai_optimize,
        product_ids=product_ids,
    )


# ---------------------------------------------------------------------------
# 私有 helpers
# ---------------------------------------------------------------------------


def _is_incomplete(fields: dict[str, Any], fm: Any) -> bool:
    """业务尚未填完的判定:4 个核心字段任一缺 → True。

    判空规则:
      - 标题 / 视频文件:_get_str 返回 None(空字符串、rich-text 空数组都算空)
      - 执行日期 / 定时发布时间:原始值是 None(飞书日期字段空 → key 缺失或 None)
    """
    if _get_str(fields.get(fm.title)) is None:
        return True
    if _get_str(fields.get(fm.video_file)) is None:
        return True
    if fields.get(fm.execute_date) is None:
        return True
    if fields.get(fm.publish_at) is None:
        return True
    return False


def _check_title(
    fields: dict[str, Any],
    field_name: str,
    errors: list[FieldError],
    *,
    platform: str = "tencent_channel",
) -> str | None:
    raw = _get_str(fields.get(field_name))
    if not raw:
        errors.append(FieldError(field=field_name, message="未指定"))
        return None
    n = len(raw)
    title_min = _title_min_for(platform)
    if n < title_min or n > _TITLE_MAX:
        if title_min > 1:
            msg = f"{n} 字(要求 {title_min}-{_TITLE_MAX} 字)"
        else:
            msg = f"{n} 字(最多 {_TITLE_MAX} 字)"
        errors.append(FieldError(field=field_name, message=msg))
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
    active_accounts: dict[str, str],
    errors: list[FieldError],
) -> str | None:
    """先按 account_id 精确匹配,再按 display_name 反查;display_name 唯一由 Settings 保证。"""
    raw = fields.get(field_name)
    selected = _coerce_select(raw)
    if not selected:
        errors.append(FieldError(field=field_name, message="未指定"))
        return None
    if selected in active_accounts:
        return selected
    for aid, display_name in active_accounts.items():
        if display_name == selected:
            return aid
    errors.append(FieldError(field=field_name, message=f"{selected!r} 不存在或已停用"))
    return None


def _coerce_select(raw: Any) -> str | None:
    """单选字段可能返回 dict {'text': ...} 或字符串。"""
    if isinstance(raw, dict):
        text = raw.get("text")
        return text if isinstance(text, str) else None
    if isinstance(raw, str):
        return raw or None
    return None


def _get_str(raw: Any) -> str | None:
    """提取字符串。

    兼容飞书"单行/多行文本"字段的 rich-text array 格式:
    `[{"text": "...", "type": "text"}, ...]`。多段时按顺序拼接。
    """
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        joined = "".join(parts)
        return joined or None
    return None


def _check_video(
    fields: dict[str, Any],
    field_name: str,
    nas_finder: NasFinder,
    account_cfg: Any,
    errors: list[FieldError],
    path_aliases: dict[str, str],
) -> Path | None:
    raw = _get_str(fields.get(field_name))
    if not raw:
        errors.append(FieldError(field=field_name, message="未指定"))
        return None
    if account_cfg is None:
        errors.append(FieldError(field=field_name, message="账号未识别,无法选择视频检索路径"))
        return None
    # 运营常只填裸文件名(已知都是 mp4)→ 没扩展名时自动补 .mp4。
    # 注意:不能用 "." in raw 判定(文件名里的编号如 "4." 会误判),要看真后缀。
    # Path.suffix 对 UNC / POSIX 绝对路径同样能取到最后一个 `.` 后的扩展,两模式共用。
    lookup_name = raw if Path(raw).suffix.lower() in _VIDEO_EXTENSIONS else raw + ".mp4"
    try:
        path = nas_finder.find_video(
            lookup_name,
            search_root=account_cfg.video_search_root,
            path_aliases=path_aliases,
        )
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
    account_cfg: Any,
    errors: list[FieldError],
    path_aliases: dict[str, str],
) -> Path | None:
    raw = _get_str(fields.get(field_name))
    if not raw:
        return None  # 封面可空
    if account_cfg is None:
        errors.append(FieldError(field=field_name, message="账号未识别,无法选择封面检索路径"))
        return None
    try:
        return nas_finder.find_cover(
            raw,
            search_root=account_cfg.cover_search_root,
            path_aliases=path_aliases,
        )
    except FileNotFoundError:
        errors.append(FieldError(field=field_name, message=f"未在 NAS 下找到 {raw!r}"))
        return None


def _check_execute_date(
    fields: dict[str, Any],
    field_name: str,
    errors: list[FieldError],
) -> date | None:
    raw = fields.get(field_name)
    if raw is None:
        errors.append(FieldError(field=field_name, message="未指定"))
        return None
    if not isinstance(raw, int | float):
        errors.append(FieldError(field=field_name, message=f"无法解析 {raw!r}"))
        return None
    dt_utc = datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
    return dt_utc.astimezone(_SHANGHAI).date()


def _check_publish_at(
    fields: dict[str, Any],
    field_name: str,
    now: datetime,
    execute_date: date | None,
    errors: list[FieldError],
) -> datetime | None:
    raw = fields.get(field_name)
    if raw is None:
        errors.append(FieldError(field=field_name, message="未指定"))
        return None
    if not isinstance(raw, int | float):
        errors.append(FieldError(field=field_name, message=f"无法解析 {raw!r}"))
        return None
    dt_utc = datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
    publish_at = dt_utc.astimezone(_SHANGHAI).replace(tzinfo=None)
    min_allowed = now + _PUBLISH_MIN_DELTA
    max_allowed = now + _PUBLISH_MAX_DELTA
    if publish_at < min_allowed:
        errors.append(
            FieldError(
                field=field_name,
                message=f"{publish_at.strftime('%Y-%m-%d %H:%M')} 早于 now+30min",
            )
        )
        return None
    if publish_at > max_allowed:
        errors.append(
            FieldError(
                field=field_name,
                message=f"{publish_at.strftime('%Y-%m-%d %H:%M')} 超出 now+14d 上限",
            )
        )
        return None
    if execute_date is not None and publish_at.date() < execute_date:
        errors.append(
            FieldError(
                field=field_name,
                message=f"日期 {publish_at.date()} 早于执行日期 {execute_date}",
            )
        )
        return None
    return publish_at
