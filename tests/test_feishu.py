"""wxsp.feishu types and Bitable client wrappers."""

from __future__ import annotations

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
