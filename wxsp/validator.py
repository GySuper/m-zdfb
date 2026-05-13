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
from typing import Protocol


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
