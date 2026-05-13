"""errors module unit tests."""

from __future__ import annotations


def test_nas_unreachable_is_exception_subclass() -> None:
    from wxsp.errors import NasUnreachable

    assert issubclass(NasUnreachable, Exception)


def test_nas_unreachable_can_be_raised_and_caught() -> None:
    from wxsp.errors import NasUnreachable

    try:
        raise NasUnreachable("stage failed")
    except NasUnreachable as exc:
        assert str(exc) == "stage failed"


def test_nas_unreachable_preserves_cause_via_from() -> None:
    """exception chaining via `raise ... from ...` 保留原始 OSError。"""
    from wxsp.errors import NasUnreachable

    original = PermissionError("permission denied")
    try:
        raise NasUnreachable("translated") from original
    except NasUnreachable as exc:
        assert exc.__cause__ is original
