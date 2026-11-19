"""飞书 Bitable 拉取与回写(M3)。

设计要点(详见 docs/superpowers/specs/2026-05-12-m3-feishu-sync.md):
  - 无状态函数 API:make_client / fetch_pending_rows / writeback_row / add_account_option
  - 4 次指数退避(1s/3s/10s)就近写在函数体内,不引入 M5 的 retry.py
  - BitableRow 只存 record_id + 原始 fields dict;字段语义解析交给 validator
  - 不用 lark-oapi SDK(88MB、一次导入 50+ 个服务,安装包里只用到多维表格几个接口):
    `FeishuClient` 用标准库直接调开放平台 HTTP 接口(2026-11-19)
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

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


_BASE_URL = "https://open.feishu.cn/open-apis"


class FeishuClient:
    """飞书开放平台最小客户端:自建应用 tenant_access_token(缓存到过期前 5 分钟)+ JSON 请求。

    只覆盖本项目用到的多维表格接口;响应 code != 0 / HTTP 错误一律抛 FeishuApiError。
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        timeout: float = _DEFAULT_HTTP_TIMEOUT_SECONDS,
        base_url: str = _BASE_URL,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self._token: str | None = None
        self._token_expire_at = 0.0

    def _http(
        self, method: str, url: str, *, body: Any = None, headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            method=method,
            headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw)
            except ValueError:
                raise FeishuApiError(f"飞书 HTTP {exc.code}") from exc
            raise FeishuApiError(
                f"飞书 API 错误 code={parsed.get('code')} msg={parsed.get('msg')}"
            ) from exc
        parsed = json.loads(raw)
        if parsed.get("code", 0) != 0:
            raise FeishuApiError(f"飞书 API 错误 code={parsed.get('code')} msg={parsed.get('msg')}")
        result: dict[str, Any] = parsed
        return result

    def _tenant_token(self) -> str:
        if self._token is None or time.monotonic() >= self._token_expire_at:
            r = self._http(
                "POST",
                f"{self.base_url}/auth/v3/tenant_access_token/internal",
                body={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            self._token = str(r["tenant_access_token"])
            self._token_expire_at = time.monotonic() + max(0, int(r.get("expire", 7200)) - 300)
        return self._token

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
    ) -> dict[str, Any]:
        """调开放平台接口,返回响应里的 data。失败时丢掉缓存 token(下次重试会重取)。"""
        query = "?" + urllib.parse.urlencode(params) if params else ""
        try:
            r = self._http(
                method,
                f"{self.base_url}{path}{query}",
                body=body,
                headers={"Authorization": f"Bearer {self._tenant_token()}"},
            )
        except Exception:
            self._token = None
            raise
        data: dict[str, Any] = r.get("data") or {}
        return data


def _table_path(app_token: str, table_id: str) -> str:
    return f"/bitable/v1/apps/{app_token}/tables/{table_id}"


def make_client(
    app_id: str,
    app_secret: str,
    *,
    timeout: float = _DEFAULT_HTTP_TIMEOUT_SECONDS,
) -> FeishuClient:
    """构建飞书客户端;无缓存,sync 启动时新建。

    timeout: 单次 HTTP 调用超时(秒)。默认 15s,避免 sync_now 在 Web UI 路由里卡死。
    """
    return FeishuClient(app_id, app_secret, timeout=timeout)


def fetch_pending_rows(
    client: FeishuClient,
    *,
    app_token: str,
    table_id: str,
    status_field: str,
    status_pending_value: str = "待入库",
) -> list[BitableRow]:
    """拉所有 status_field=status_pending_value 的行,自动翻页。

    内置 4 次指数退避(1s/3s/10s):前三次失败 sleep 后重试;第四次失败 → FeishuApiError。
    """
    rows: list[BitableRow] = []
    page_token: str | None = None
    body = {
        "filter": {
            "conjunction": "and",
            "conditions": [
                {"field_name": status_field, "operator": "is", "value": [status_pending_value]}
            ],
        }
    }
    while True:
        params: dict[str, Any] = {"page_size": 100}
        if page_token is not None:
            params["page_token"] = page_token
        data = _with_retry(
            "fetch",
            lambda params=params: client.request(
                "POST",
                f"{_table_path(app_token, table_id)}/records/search",
                params=params,
                body=body,
            ),
        )
        rows.extend(
            BitableRow(record_id=item["record_id"], fields=dict(item.get("fields") or {}))
            for item in data.get("items") or []
        )
        if not data.get("has_more"):
            return rows
        page_token = data.get("page_token")


def _with_retry(what: str, call: Any) -> dict[str, Any]:
    """4 次指数退避:前三次失败 sleep 后重试,第四次失败 → FeishuApiError。"""
    last_err: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            result: dict[str, Any] = call()
            return result
        except Exception as exc:
            last_err = exc
            if attempt < len(_RETRY_DELAYS):
                time.sleep(_RETRY_DELAYS[attempt])
    assert last_err is not None
    raise FeishuApiError(f"飞书 {what} 重试 {_RETRY_ATTEMPTS} 次仍失败: {last_err}") from last_err


def writeback_row(
    client: FeishuClient,
    *,
    app_token: str,
    table_id: str,
    record_id: str,
    fields: dict[str, Any],
) -> None:
    """回写指定 record 的指定字段。fields 已用飞书原字段名作 key。

    内置 4 次指数退避(1s/3s/10s);4 次都失败 → FeishuApiError。
    """
    _with_retry(
        "writeback",
        lambda: client.request(
            "PUT",
            f"{_table_path(app_token, table_id)}/records/{record_id}",
            body={"fields": fields},
        ),
    )


# ---------- 账号字段:单选选项追加 ----------


def add_account_option(
    client: FeishuClient,
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
    if field.get("type") not in _SELECT_FIELD_TYPES:
        raise FeishuApiError(
            f"字段 {account_field_name!r} 类型不是单选/多选(type={field.get('type')}),拒绝改 options"
        )

    existing_options = _parse_options(field.get("property"))
    if any(opt_name == option_name for opt_name, _ in existing_options):
        return "exists"
    new_options = [*existing_options, (option_name, None)]
    _update_field_options(
        client,
        app_token=app_token,
        table_id=table_id,
        field_id=field["field_id"],
        field_name=account_field_name,
        options=new_options,
    )
    return "added"


def _find_field_by_name(
    client: FeishuClient,
    *,
    app_token: str,
    table_id: str,
    field_name: str,
) -> dict[str, Any] | None:
    """list 全表字段,按 field_name 找;翻页拉完。"""
    page_token: str | None = None
    while True:
        params: dict[str, Any] = {"page_size": 100}
        if page_token is not None:
            params["page_token"] = page_token
        data = _with_retry(
            "list field",
            lambda params=params: client.request(
                "GET", f"{_table_path(app_token, table_id)}/fields", params=params
            ),
        )
        for item in data.get("items") or []:
            if item.get("field_name") == field_name:
                found: dict[str, Any] = item
                return found
        if not data.get("has_more"):
            return None
        page_token = data.get("page_token")


def _parse_options(prop: dict[str, Any] | None) -> list[tuple[str, str | None]]:
    """从字段 property 里抽 [(name, id_or_None), ...]。"""
    out: list[tuple[str, str | None]] = []
    for opt in (prop or {}).get("options") or []:
        name = opt.get("name")
        if not isinstance(name, str):
            continue
        oid = opt.get("id")
        out.append((name, oid if isinstance(oid, str) else None))
    return out


def _update_field_options(
    client: FeishuClient,
    *,
    app_token: str,
    table_id: str,
    field_id: str,
    field_name: str,
    options: list[tuple[str, str | None]],
) -> None:
    body = {
        "field_name": field_name,
        "type": _FEISHU_FIELD_TYPE_SINGLE_SELECT,
        "property": {
            "options": [{"name": name, **({"id": oid} if oid else {})} for name, oid in options]
        },
    }
    _with_retry(
        "update field",
        lambda: client.request(
            "PUT", f"{_table_path(app_token, table_id)}/fields/{field_id}", body=body
        ),
    )
