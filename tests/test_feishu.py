"""wxsp.feishu types and Bitable client wrappers."""

from __future__ import annotations

from typing import Any

import pytest

from wxsp.feishu import BitableRow, FeishuApiError


def test_bitable_row_is_frozen_dataclass() -> None:
    row = BitableRow(record_id="rec123", fields={"标题": "abc"})
    assert row.record_id == "rec123"
    assert row.fields == {"标题": "abc"}
    with pytest.raises(Exception):  # noqa: B017  # FrozenInstanceError or AttributeError
        row.record_id = "other"  # type: ignore[misc]


def test_feishu_api_error_is_exception() -> None:
    err = FeishuApiError("bitable timeout")
    assert isinstance(err, Exception)
    assert str(err) == "bitable timeout"


# ---------------------------------------------------------------------------
# Task 4: make_client + fetch_pending_rows
# ---------------------------------------------------------------------------

from wxsp.feishu import fetch_pending_rows, make_client  # noqa: E402


class _FakeResponse:
    """模拟 lark.Client 返回的 response 对象。"""

    def __init__(
        self,
        items: list[dict[str, Any]],
        has_more: bool,
        page_token: str | None = None,
        code: int = 0,
        msg: str = "",
    ) -> None:
        self.data = _FakeData(items, has_more, page_token)
        self.code = code
        self.msg = msg

    def success(self) -> bool:
        return self.code == 0


class _FakeData:
    def __init__(self, items: list[dict[str, Any]], has_more: bool, page_token: str | None) -> None:
        self.items = [_FakeRecord(r["record_id"], r["fields"]) for r in items]
        self.has_more = has_more
        self.page_token = page_token


class _FakeRecord:
    def __init__(self, record_id: str, fields: dict[str, Any]) -> None:
        self.record_id = record_id
        self.fields = fields


class _FakeClient:
    """模拟 lark.Client,捕获请求并按预设响应序列返回。"""

    def __init__(self, responses: list[_FakeResponse | Exception]) -> None:
        self._responses = list(responses)
        self.search_calls: list[Any] = []
        # client.bitable.v1.app_table_record.search(request) 这个调用链
        self.bitable = self
        self.v1 = self
        self.app_table_record = self

    def search(self, request: Any) -> _FakeResponse:
        self.search_calls.append(request)
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def test_make_client_builds_client() -> None:
    """make_client 应能返回一个 lark.Client 实例(我们只验证它不爆)。"""
    client = make_client("cli_app_id", "secret_value")
    assert client is not None
    # 不深入断言 builder 内部状态,只确保返回值有 bitable.v1.app_table_record.search 调用链
    assert hasattr(client, "bitable")


def test_fetch_pending_rows_single_page() -> None:
    fake = _FakeClient(
        [
            _FakeResponse(
                items=[
                    {"record_id": "rec1", "fields": {"标题": "a"}},
                    {"record_id": "rec2", "fields": {"标题": "b"}},
                ],
                has_more=False,
            ),
        ]
    )
    rows = fetch_pending_rows(
        fake,
        app_token="tbl_token",
        table_id="tblxxx",
        status_field="状态",
    )
    assert len(rows) == 2
    assert rows[0].record_id == "rec1"
    assert rows[0].fields == {"标题": "a"}
    assert rows[1].record_id == "rec2"
    assert len(fake.search_calls) == 1


def test_fetch_pending_rows_paginates() -> None:
    fake = _FakeClient(
        [
            _FakeResponse(
                items=[{"record_id": "rec1", "fields": {}}],
                has_more=True,
                page_token="cursor_2",
            ),
            _FakeResponse(
                items=[{"record_id": "rec2", "fields": {}}],
                has_more=False,
            ),
        ]
    )
    rows = fetch_pending_rows(
        fake,
        app_token="t",
        table_id="t",
        status_field="状态",
    )
    assert [r.record_id for r in rows] == ["rec1", "rec2"]
    assert len(fake.search_calls) == 2


def test_fetch_pending_rows_retries_on_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("wxsp.feishu.time.sleep", lambda s: sleeps.append(s))

    fake = _FakeClient(
        [
            RuntimeError("transient 1"),
            RuntimeError("transient 2"),
            _FakeResponse(items=[{"record_id": "rec1", "fields": {}}], has_more=False),
        ]
    )
    rows = fetch_pending_rows(
        fake,
        app_token="t",
        table_id="t",
        status_field="状态",
    )
    assert len(rows) == 1
    # 两次失败后第三次成功 → 应该 sleep 过 2 次(指数退避 1s/2s)
    assert sleeps == [1.0, 2.0]


def test_fetch_pending_rows_raises_after_3_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("wxsp.feishu.time.sleep", lambda s: None)

    fake = _FakeClient(
        [
            RuntimeError("fail 1"),
            RuntimeError("fail 2"),
            RuntimeError("fail 3"),
        ]
    )
    with pytest.raises(FeishuApiError) as exc_info:
        fetch_pending_rows(
            fake,
            app_token="t",
            table_id="t",
            status_field="状态",
        )
    assert "fail 3" in str(exc_info.value) or "3 次" in str(exc_info.value)


def test_fetch_pending_rows_raises_on_api_error_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("wxsp.feishu.time.sleep", lambda s: None)

    fake = _FakeClient(
        [
            _FakeResponse(items=[], has_more=False, code=999, msg="forbidden"),
            _FakeResponse(items=[], has_more=False, code=999, msg="forbidden"),
            _FakeResponse(items=[], has_more=False, code=999, msg="forbidden"),
        ]
    )
    with pytest.raises(FeishuApiError):
        fetch_pending_rows(
            fake,
            app_token="t",
            table_id="t",
            status_field="状态",
        )
