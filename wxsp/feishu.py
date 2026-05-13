"""飞书 Bitable 拉取与回写(M3)。

设计要点(详见 docs/superpowers/specs/2026-05-12-m3-feishu-sync.md):
  - 无状态函数 API:make_client / fetch_pending_rows / writeback_row
  - 3 次指数退避(1s/2s/4s)就近写在函数体内,不引入 M5 的 retry.py
  - BitableRow 只存 record_id + 原始 fields dict;字段语义解析交给 validator
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BitableRow:
    """单行飞书 Bitable 记录的最小封装。fields 用飞书原字段中文名作 key。"""

    record_id: str
    fields: dict[str, Any]


class FeishuApiError(Exception):
    """飞书 API 在 3 次指数退避后仍失败时抛出。"""
