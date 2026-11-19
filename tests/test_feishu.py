"""wxsp.feishu:标准库飞书客户端 + 多维表格拉取 / 回写 / 加单选项。

对着本地仿真的飞书开放平台(真 HTTP)测请求格式、翻页、token 缓存、错误码与重试,
不再模仿 lark-oapi SDK 的调用链(SDK 已移除)。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from wxsp.feishu import (
    BitableRow,
    FeishuApiError,
    FeishuClient,
    add_account_option,
    fetch_pending_rows,
    make_client,
    writeback_row,
)

TABLE = "/open-apis/bitable/v1/apps/app_tok/tables/tbl1"


class FakeFeishu:
    """仿真飞书:记录请求;`fail_next` 里的条目依次让后续请求失败(HTTP 状态码或业务 code)。"""

    def __init__(self) -> None:
        self.token_calls = 0
        self.requests: list[
            tuple[str, str, dict[str, Any], Any]
        ] = []  # (method, path, query, body)
        self.pages: list[dict[str, Any]] = []  # search 依次返回的 data
        self.fields: list[dict[str, Any]] = []
        self.updated_records: dict[str, Any] = {}
        self.updated_fields: dict[str, Any] = {}
        self.fail_next: list[int | str] = []  # int = HTTP 状态码;str = 业务错误 msg

    def handle(
        self, method: str, path: str, query: dict[str, Any], body: Any, auth: str
    ) -> tuple[int, dict[str, Any]]:
        if path == "/open-apis/auth/v3/tenant_access_token/internal":
            self.token_calls += 1
            assert body == {"app_id": "cli_x", "app_secret": "sec"}
            return 200, {"code": 0, "tenant_access_token": f"t-{self.token_calls}", "expire": 7200}
        self.requests.append((method, path, query, body))
        assert auth.startswith("Bearer t-"), auth
        if self.fail_next:
            f = self.fail_next.pop(0)
            if isinstance(f, int):
                return f, {"code": 99991400, "msg": "server busy"}
            return 200, {"code": 1254040, "msg": f}
        if path == f"{TABLE}/records/search":
            return 200, {"code": 0, "data": self.pages.pop(0)}
        if method == "PUT" and path.startswith(f"{TABLE}/records/"):
            self.updated_records[path.rsplit("/", 1)[1]] = body
            return 200, {"code": 0, "data": {}}
        if method == "GET" and path == f"{TABLE}/fields":
            return 200, {"code": 0, "data": {"items": self.fields, "has_more": False}}
        if method == "PUT" and path.startswith(f"{TABLE}/fields/"):
            self.updated_fields[path.rsplit("/", 1)[1]] = body
            return 200, {"code": 0, "data": {}}
        return 404, {"code": 404, "msg": "not found"}


@pytest.fixture
def feishu(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[FakeFeishu, FeishuClient]]:
    fake = FakeFeishu()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            query = {k: v[0] for k, v in parse_qs(u.query).items()}
            status, payload = fake.handle(
                self.command, u.path, query, body, self.headers.get("Authorization", "")
            )
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = _serve

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr("wxsp.feishu.time.sleep", lambda s: None)
    client = FeishuClient(
        "cli_x", "sec", base_url=f"http://127.0.0.1:{server.server_address[1]}/open-apis"
    )
    yield fake, client
    server.shutdown()


def _fetch(client: FeishuClient) -> list[BitableRow]:
    return fetch_pending_rows(client, app_token="app_tok", table_id="tbl1", status_field="状态")


# ---------- 基础类型 / 构造 ----------


def test_bitable_row_is_frozen_dataclass() -> None:
    row = BitableRow(record_id="rec123", fields={"标题": "abc"})
    assert row.record_id == "rec123"
    with pytest.raises(Exception):  # noqa: B017  # FrozenInstanceError
        row.record_id = "other"  # type: ignore[misc]


def test_make_client_has_timeout() -> None:
    """默认要有 HTTP 超时,避免飞书抖动时 Web UI 路由卡死;显式传入时透传。"""
    assert make_client("cli", "sec").timeout == 15.0
    assert make_client("cli", "sec", timeout=3.0).timeout == 3.0


# ---------- 拉取待入库 ----------


def test_fetch_pending_rows_sends_status_filter_and_paginates(
    feishu: tuple[FakeFeishu, FeishuClient],
) -> None:
    fake, client = feishu
    fake.pages = [
        {
            "items": [{"record_id": "rec1", "fields": {"标题": "a"}}],
            "has_more": True,
            "page_token": "p2",
        },
        {"items": [{"record_id": "rec2", "fields": {}}], "has_more": False},
    ]
    rows = _fetch(client)
    assert [(r.record_id, r.fields) for r in rows] == [("rec1", {"标题": "a"}), ("rec2", {})]
    (m1, p1, q1, b1), (_, _, q2, _) = fake.requests
    assert (m1, p1) == ("POST", f"{TABLE}/records/search")
    assert q1 == {"page_size": "100"} and q2 == {"page_size": "100", "page_token": "p2"}
    assert b1 == {
        "filter": {
            "conjunction": "and",
            "conditions": [{"field_name": "状态", "operator": "is", "value": ["待入库"]}],
        }
    }
    assert fake.token_calls == 1  # token 缓存复用


def test_fetch_pending_rows_retries_transient_http_error(
    feishu: tuple[FakeFeishu, FeishuClient],
) -> None:
    fake, client = feishu
    fake.fail_next = [500]
    fake.pages = [{"items": [{"record_id": "rec1", "fields": {}}], "has_more": False}]
    assert [r.record_id for r in _fetch(client)] == ["rec1"]
    assert len(fake.requests) == 2
    assert fake.token_calls == 2  # 失败后丢弃 token,重试时重新取


def test_fetch_pending_rows_raises_after_all_attempts(
    feishu: tuple[FakeFeishu, FeishuClient],
) -> None:
    fake, client = feishu
    fake.fail_next = [500, 500, 500, 500]
    with pytest.raises(FeishuApiError, match="重试 4 次仍失败"):
        _fetch(client)
    assert len(fake.requests) == 4


def test_fetch_pending_rows_business_error_code(feishu: tuple[FakeFeishu, FeishuClient]) -> None:
    fake, client = feishu
    fake.fail_next = ["NoPermission"] * 4
    with pytest.raises(FeishuApiError, match="NoPermission"):
        _fetch(client)


# ---------- 回写 ----------


def test_writeback_row_puts_fields(feishu: tuple[FakeFeishu, FeishuClient]) -> None:
    fake, client = feishu
    writeback_row(
        client, app_token="app_tok", table_id="tbl1", record_id="rec9", fields={"状态": "已发布"}
    )
    assert fake.updated_records == {"rec9": {"fields": {"状态": "已发布"}}}


def test_writeback_row_retries_then_raises(feishu: tuple[FakeFeishu, FeishuClient]) -> None:
    fake, client = feishu
    fake.fail_next = [502]
    writeback_row(client, app_token="app_tok", table_id="tbl1", record_id="r", fields={})
    assert len(fake.requests) == 2
    fake.fail_next = [502] * 4
    with pytest.raises(FeishuApiError):
        writeback_row(client, app_token="app_tok", table_id="tbl1", record_id="r", fields={})


# ---------- 账号单选加选项 ----------


def _account_field(type_: int = 3) -> dict[str, Any]:
    return {
        "field_id": "fld1",
        "field_name": "账号",
        "type": type_,
        "property": {"options": [{"id": "opt1", "name": "美食号", "color": 0}]},
    }


def test_add_account_option_keeps_existing_ids(feishu: tuple[FakeFeishu, FeishuClient]) -> None:
    fake, client = feishu
    fake.fields = [{"field_id": "f0", "field_name": "标题", "type": 1}, _account_field()]
    r = add_account_option(
        client,
        app_token="app_tok",
        table_id="tbl1",
        account_field_name="账号",
        option_name="健身号",
    )
    assert r == "added"
    assert fake.updated_fields["fld1"] == {
        "field_name": "账号",
        "type": 3,
        "property": {"options": [{"name": "美食号", "id": "opt1"}, {"name": "健身号"}]},
    }


def test_add_account_option_is_idempotent_and_checks_type(
    feishu: tuple[FakeFeishu, FeishuClient],
) -> None:
    fake, client = feishu
    fake.fields = [_account_field()]
    kw = {"app_token": "app_tok", "table_id": "tbl1", "account_field_name": "账号"}
    assert add_account_option(client, option_name="美食号", **kw) == "exists"
    assert fake.updated_fields == {}
    fake.fields = [_account_field(type_=1)]
    with pytest.raises(FeishuApiError, match="不是单选"):
        add_account_option(client, option_name="新号", **kw)
    fake.fields = []
    with pytest.raises(FeishuApiError, match="找不到字段"):
        add_account_option(client, option_name="新号", **kw)
