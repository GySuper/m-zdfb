"""wxsp.validator types and rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from wxsp.validator import FieldError, NasFinder, ValidationResult


def test_field_error_is_frozen() -> None:
    err = FieldError(field="标题", message="12 字(要求 16-30 字)")
    assert err.field == "标题"
    assert err.message == "12 字(要求 16-30 字)"
    with pytest.raises(Exception):  # noqa: B017  # FrozenInstanceError or AttributeError
        err.field = "x"  # type: ignore[misc]


def test_validation_result_ok_shape() -> None:
    result = ValidationResult(ok=True, title="abc" * 6)
    assert result.ok is True
    assert result.errors == []  # 默认空 list


def test_validation_result_fail_shape() -> None:
    result = ValidationResult(
        ok=False,
        errors=[FieldError(field="标题", message="12 字")],
    )
    assert result.ok is False
    assert len(result.errors) == 1


def test_nas_finder_is_protocol() -> None:
    """NasFinder 是 Protocol,任何提供 find_video/find_cover 的对象都满足。"""

    class _Stub:
        def find_video(self, name: str) -> Path:
            return Path("/dev/null")

        def find_cover(self, name: str) -> Path:
            return Path("/dev/null")

    finder: NasFinder = _Stub()
    assert finder.find_video("x").as_posix() == "/dev/null"
