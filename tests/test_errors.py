"""errors module unit tests."""

from __future__ import annotations

import pytest

from wxsp.errors import NasUnreachable


def test_nas_unreachable_is_exception_subclass() -> None:
    assert issubclass(NasUnreachable, Exception)


def test_nas_unreachable_can_be_raised_and_caught() -> None:
    with pytest.raises(NasUnreachable, match="stage failed"):
        raise NasUnreachable("stage failed")


def test_nas_unreachable_preserves_cause_via_from() -> None:
    """exception chaining via `raise ... from ...` 保留原始 OSError。"""
    original = PermissionError("permission denied")
    with pytest.raises(NasUnreachable) as exc_info:
        raise NasUnreachable("translated") from original
    assert exc_info.value.__cause__ is original
