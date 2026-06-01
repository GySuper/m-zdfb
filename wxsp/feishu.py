"""飞书 Bitable 拉取与回写(M3)。

设计要点(详见 docs/superpowers/specs/2026-05-12-m3-feishu-sync.md):
  - 无状态函数 API:make_client / fetch_pending_rows / writeback_row
  - 4 次指数退避(1s/3s/10s)就近写在函数体内,不引入 M5 的 retry.py
  - BitableRow 只存 record_id + 原始 fields dict;字段语义解析交给 validator
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import lark_oapi as lark  # type: ignore[import-untyped]
from lark_oapi.api.bitable.v1 import (  # type: ignore[import-untyped]
    AppTableField,
    AppTableFieldProperty,
    AppTableFieldPropertyOption,
    AppTableRecord,
    Condition,
    FilterInfo,
    ListAppTableFieldRequest,
    SearchAppTableRecordRequest,
    SearchAppTableRecordRequestBody,
    UpdateAppTableFieldRequest,
    UpdateAppTableRecordRequest,
)

# 飞书 Bitable 字段类型码:3 = 单选(SingleSelect)。Bitable 文档:
# https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/bitable-v1/app-table-field/guide
_FEISHU_FIELD_TYPE_SINGLE_SELECT = 3
_FEISHU_FIELD_TYPE_MULTI_SELECT = 4
_SELECT_FIELD_TYPES = {3, 4}  # 单选和多选都支持加 options

# 指数退避序列:第 1 次失败等 1s,第 2 次等 3s,第 3 次等 10s,第 4 次失败直接抛。
# 历史 (1.0, 2.0)/3 次 在飞书侧抖动时被实测打穿(运营反馈:一批 task 跑完最后一条
# writeback 未上传 → DB success / 飞书还是"已计划")。退避总时长 14s 配 API 15s
# timeout,最坏 ~75s 内决定成败,远小于一次 publish 的 1-5min 耗时,不会拖慢主流程。
_RETRY_DELAYS = (1.0, 3.0, 10.0)
_RETRY_ATTEMPTS = len(_RETRY_DELAYS) + 1  # 共尝试 4 次

# 单次 HTTP 调用超时(秒):lark-oapi 默认无超时,飞书侧抖动会让 Web UI 路由卡死。
# 15s 足够正常调用;真挂了配合 4 次退避最多 ~75s 就抛 FeishuApiError 走 opError 弹窗。
_DEFAULT_HTTP_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class BitableRow:
    """单行飞书 Bitable 记录的最小封装。fields 用飞书原字段中文名作 key。"""

    record_id: str
    fields: dict[str, Any]


class FeishuApiError(Exception):
    """飞书 API 在 4 次指数退避后仍失败时抛出。"""


def make_client(
    app_id: str,
    app_secret: str,
    *,
    timeout: float = _DEFAULT_HTTP_TIMEOUT_SECONDS,
) -> lark.Client:
    """构建 lark-oapi 客户端;无缓存,sync 启动时新建。

    timeout: 单次 HTTP 调用超时(秒)。默认 15s,避免 sync_now 在 Web UI 路由里卡死。
    """
    return lark.Client.builder().app_id(app_id).app_secret(app_secret).timeout(timeout).build()


def fetch_pending_rows(
    client: lark.Client,
    *,
    app_token: str,
    table_id: str,
    status_field: str,
    status_pending_value: str = "待入库",
) -> list[BitableRow]:
    """拉所有 status_field=status_pending_value 的行,自动翻页。

    内置 4 次指数退避(1s/3s/10s):前三次失败 sleep 后重试;第四次失败 → FeishuApiError。
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
    for attempt in range(_RETRY_ATTEMPTS):
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
            if attempt < len(_RETRY_DELAYS):
                time.sleep(_RETRY_DELAYS[attempt])
    assert last_err is not None
    raise FeishuApiError(f"飞书 fetch 重试 {_RETRY_ATTEMPTS} 次仍失败: {last_err}") from last_err


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


def writeback_row(
    client: lark.Client,
    *,
    app_token: str,
    table_id: str,
    record_id: str,
    fields: dict[str, Any],
) -> None:
    """回写指定 record 的指定字段。fields 已用飞书原字段名作 key。

    内置 4 次指数退避(1s/3s/10s);4 次都失败 → FeishuApiError。
    """
    last_err: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            response = client.bitable.v1.app_table_record.update(
                _build_update_request(
                    app_token=app_token,
                    table_id=table_id,
                    record_id=record_id,
                    fields=fields,
                )
            )
            if not response.success():
                raise FeishuApiError(f"飞书 update 错误 code={response.code} msg={response.msg}")
            return
        except Exception as exc:
            last_err = exc
            if attempt < len(_RETRY_DELAYS):
                time.sleep(_RETRY_DELAYS[attempt])
    assert last_err is not None
    raise FeishuApiError(
        f"飞书 writeback 重试 {_RETRY_ATTEMPTS} 次仍失败: {last_err}"
    ) from last_err


def _build_update_request(
    *,
    app_token: str,
    table_id: str,
    record_id: str,
    fields: dict[str, Any],
) -> UpdateAppTableRecordRequest:
    body = AppTableRecord.builder().fields(fields).build()
    return (
        UpdateAppTableRecordRequest.builder()
        .app_token(app_token)
        .table_id(table_id)
        .record_id(record_id)
        .request_body(body)
        .build()
    )


# ---------- 账号字段:单选选项追加 ----------


def add_account_option(
    client: lark.Client,
    *,
    app_token: str,
    table_id: str,
    account_field_name: str,
    option_name: str,
) -> str:
    """把 option_name 追加到飞书 'account_field_name' 单选字段的选项列表。

    返回:
      "added"  - 选项不存在,已新增
      "exists" - 选项已存在,无操作(幂等)

    抛 FeishuApiError:
      - 找不到该字段 / 字段不是单选
      - API 三次重试仍失败

    设计:
      - 飞书 update field 是"property 整体替换",所以要先 list 拿到完整 options
        (含 id),append 新项后整体回写,保留旧选项的 id(否则历史记录引用的
        option 会被当成"已删除"清空)。
      - 字段类型校验防呆:防止用户把 field_map.account 配到一个文本字段把它误
        改成单选。
    """
    field = _find_field_by_name(
        client, app_token=app_token, table_id=table_id, field_name=account_field_name
    )
    if field is None:
        raise FeishuApiError(f"找不到字段 {account_field_name!r}(检查 feishu.field_map.account)")
    if field.type not in _SELECT_FIELD_TYPES:
        raise FeishuApiError(
            f"字段 {account_field_name!r} 类型不是单选/多选(type={field.type}),拒绝改 options"
        )

    existing_options = _parse_options(field.property)
    if any(opt_name == option_name for opt_name, _ in existing_options):
        return "exists"
    new_options = [*existing_options, (option_name, None)]
    _update_field_options(
        client,
        app_token=app_token,
        table_id=table_id,
        field_id=field.field_id,
        field_name=account_field_name,
        options=new_options,
    )
    return "added"


def _find_field_by_name(
    client: lark.Client,
    *,
    app_token: str,
    table_id: str,
    field_name: str,
) -> AppTableField | None:
    """list 全表字段,按 field_name 找;翻页拉完。"""
    page_token: str | None = None
    while True:
        response = _list_fields_with_retry(
            client, app_token=app_token, table_id=table_id, page_token=page_token
        )
        for item in response.data.items or []:
            if item.field_name == field_name:
                return item
        if not response.data.has_more:
            return None
        page_token = response.data.page_token


def _list_fields_with_retry(
    client: lark.Client,
    *,
    app_token: str,
    table_id: str,
    page_token: str | None,
) -> Any:
    builder = (
        ListAppTableFieldRequest.builder().app_token(app_token).table_id(table_id).page_size(100)
    )
    if page_token is not None:
        builder = builder.page_token(page_token)
    req = builder.build()
    last_err: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            response = client.bitable.v1.app_table_field.list(req)
            if not response.success():
                raise FeishuApiError(
                    f"飞书 list field 错误 code={response.code} msg={response.msg}"
                )
            return response
        except Exception as exc:
            last_err = exc
            if attempt < len(_RETRY_DELAYS):
                time.sleep(_RETRY_DELAYS[attempt])
    assert last_err is not None
    raise FeishuApiError(
        f"飞书 list field 重试 {_RETRY_ATTEMPTS} 次仍失败: {last_err}"
    ) from last_err


def _parse_options(prop: AppTableFieldProperty | None) -> list[tuple[str, str | None]]:
    """从字段 property 里抽 [(name, id_or_None), ...]。"""
    if prop is None or not getattr(prop, "options", None):
        return []
    out: list[tuple[str, str | None]] = []
    for opt in prop.options:
        name = getattr(opt, "name", None)
        if not isinstance(name, str):
            continue
        oid = getattr(opt, "id", None)
        out.append((name, oid if isinstance(oid, str) else None))
    return out


def _update_field_options(
    client: lark.Client,
    *,
    app_token: str,
    table_id: str,
    field_id: str,
    field_name: str,
    options: list[tuple[str, str | None]],
) -> None:
    option_objs = []
    for name, oid in options:
        b = AppTableFieldPropertyOption.builder().name(name)
        if oid is not None:
            b = b.id(oid)
        option_objs.append(b.build())
    prop = AppTableFieldProperty.builder().options(option_objs).build()
    body = (
        AppTableField.builder()
        .field_name(field_name)
        .type(_FEISHU_FIELD_TYPE_SINGLE_SELECT)
        .property(prop)
        .build()
    )
    req = (
        UpdateAppTableFieldRequest.builder()
        .app_token(app_token)
        .table_id(table_id)
        .field_id(field_id)
        .request_body(body)
        .build()
    )
    last_err: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            response = client.bitable.v1.app_table_field.update(req)
            if not response.success():
                raise FeishuApiError(
                    f"飞书 update field 错误 code={response.code} msg={response.msg}"
                )
            return
        except Exception as exc:
            last_err = exc
            if attempt < len(_RETRY_DELAYS):
                time.sleep(_RETRY_DELAYS[attempt])
    assert last_err is not None
    raise FeishuApiError(
        f"飞书 update field 重试 {_RETRY_ATTEMPTS} 次仍失败: {last_err}"
    ) from last_err
