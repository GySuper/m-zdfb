"""errors module unit tests."""

from __future__ import annotations

import pytest
from patchright.sync_api import TimeoutError as PWTimeoutError

from wxsp.errors import (
    CookieExpired,
    ElementNotFound,
    NasUnreachable,
    NetworkError,
    PublisherError,
    RiskControl,
    UnknownError,
    UploadFailed,
    VideoInvalid,
    classify,
)


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


def test_classify_known_publisher_errors_returns_their_kind() -> None:
    cases = [
        (CookieExpired("x"), "cookie_expired"),
        (RiskControl("x"), "risk_control"),
        (ElementNotFound("x"), "element_not_found"),
        (UploadFailed("x"), "upload_failed"),
        (NasUnreachable("x"), "nas_unreachable"),
        (NetworkError("x"), "network"),
        (VideoInvalid("x"), "video_invalid"),
    ]
    for exc, kind in cases:
        assert classify(exc) == kind


def test_classify_playwright_timeout_is_element_not_found() -> None:
    assert classify(PWTimeoutError("超时")) == "element_not_found"


def test_classify_unknown_exception_falls_back_to_unknown() -> None:
    assert classify(RuntimeError("boom")) == "unknown"


def test_all_publisher_errors_share_base_class() -> None:
    for cls in (
        CookieExpired,
        RiskControl,
        ElementNotFound,
        UploadFailed,
        NasUnreachable,
        NetworkError,
        VideoInvalid,
        UnknownError,
    ):
        assert issubclass(cls, PublisherError)
