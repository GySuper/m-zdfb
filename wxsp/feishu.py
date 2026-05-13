"""飞书 Bitable 拉取与回写(M3)。

设计要点(详见 docs/superpowers/specs/2026-05-12-m3-feishu-sync.md):
  - 无状态函数 API:make_client / fetch_pending_rows / writeback_row
  - 3 次指数退避(1s/2s)就近写在函数体内,不引入 M5 的 retry.py
  - BitableRow 只存 record_id + 原始 fields dict;字段语义解析交给 validator
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import lark_oapi as lark  # type: ignore[import-untyped]
from lark_oapi.api.bitable.v1 import (  # type: ignore[import-untyped]
    AppTableRecord,
    Condition,
    FilterInfo,
    SearchAppTableRecordRequest,
    SearchAppTableRecordRequestBody,
)

# 指数退避序列:第 1 次失败等 1s,第 2 次失败等 2s,第 3 次失败直接抛
_RETRY_DELAYS = (1.0, 2.0)


@dataclass(frozen=True)
class BitableRow:
    """单行飞书 Bitable 记录的最小封装。fields 用飞书原字段中文名作 key。"""

    record_id: str
    fields: dict[str, Any]


class FeishuApiError(Exception):
    """飞书 API 在 3 次指数退避后仍失败时抛出。"""


def make_client(app_id: str, app_secret: str) -> lark.Client:
    """构建 lark-oapi 客户端;无缓存,sync 启动时新建。"""
    return lark.Client.builder().app_id(app_id).app_secret(app_secret).build()


def fetch_pending_rows(
    client: lark.Client,
    *,
    app_token: str,
    table_id: str,
    status_field: str,
    status_pending_value: str = "待入库",
) -> list[BitableRow]:
    """拉所有 status_field=status_pending_value 的行,自动翻页。

    内置 3 次指数退避(1s/2s):前两次失败 sleep 后重试;第三次失败 → FeishuApiError。
    response.code != 0 也按一次失败计。
    """
    rows: list[BitableRow] = []
    page_token: str | None = None
    while True:
        response = _search_with_retry(
            client,
            app_token=app_token,
            table_id=table_id,
            status_field=status_field,
            status_pending_value=status_pending_value,
            page_token=page_token,
        )
        rows.extend(_to_bitable_row(item) for item in (response.data.items or []))
        if not response.data.has_more:
            return rows
        page_token = response.data.page_token


def _search_with_retry(
    client: lark.Client,
    *,
    app_token: str,
    table_id: str,
    status_field: str,
    status_pending_value: str,
    page_token: str | None,
) -> Any:
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            response = client.bitable.v1.app_table_record.search(
                _build_search_request(
                    app_token=app_token,
                    table_id=table_id,
                    status_field=status_field,
                    status_pending_value=status_pending_value,
                    page_token=page_token,
                )
            )
            if not response.success():
                raise FeishuApiError(f"飞书 API 错误 code={response.code} msg={response.msg}")
            return response
        except Exception as exc:
            last_err = exc
            if attempt < 2:
                time.sleep(_RETRY_DELAYS[attempt])
    assert last_err is not None
    raise FeishuApiError(f"飞书 fetch 重试 3 次仍失败: {last_err}") from last_err


def _build_search_request(
    *,
    app_token: str,
    table_id: str,
    status_field: str,
    status_pending_value: str,
    page_token: str | None,
) -> SearchAppTableRecordRequest:
    body = (
        SearchAppTableRecordRequestBody.builder()
        .filter(
            FilterInfo.builder()
            .conjunction("and")
            .conditions(
                [
                    Condition.builder()
                    .field_name(status_field)
                    .operator("is")
                    .value([status_pending_value])
                    .build()
                ]
            )
            .build()
        )
        .build()
    )
    builder = (
        SearchAppTableRecordRequest.builder()
        .app_token(app_token)
        .table_id(table_id)
        .page_size(100)
        .request_body(body)
    )
    if page_token is not None:
        builder = builder.page_token(page_token)
    return builder.build()


def _to_bitable_row(item: AppTableRecord) -> BitableRow:
    return BitableRow(record_id=item.record_id, fields=dict(item.fields or {}))
