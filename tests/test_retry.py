"""retry_on 装饰器单元测试。"""

from __future__ import annotations

import pytest

from wxsp.retry import retry_on


class A(Exception):
    pass


class B(Exception):
    pass


def test_retry_on_succeeds_first_try_no_sleep() -> None:
    sleeps: list[float] = []
    calls = [0]

    @retry_on((A,), max_attempts=3, base_delay=0.1, sleep=sleeps.append)
    def fn() -> str:
        calls[0] += 1
        return "ok"

    assert fn() == "ok"
    assert calls[0] == 1
    assert sleeps == []


def test_retry_on_retries_listed_exceptions_with_exponential_backoff() -> None:
    sleeps: list[float] = []
    calls = [0]

    @retry_on((A,), max_attempts=3, base_delay=2.0, sleep=sleeps.append)
    def fn() -> str:
        calls[0] += 1
        if calls[0] < 3:
            raise A("boom")
        return "ok"

    assert fn() == "ok"
    assert calls[0] == 3
    assert sleeps == [2.0, 4.0]  # 指数:base * 2^(attempt-1)


def test_retry_on_does_not_catch_other_exceptions() -> None:
    sleeps: list[float] = []
    calls = [0]

    @retry_on((A,), max_attempts=5, base_delay=0.1, sleep=sleeps.append)
    def fn() -> None:
        calls[0] += 1
        raise B("不重试")

    with pytest.raises(B):
        fn()
    assert calls[0] == 1
    assert sleeps == []


def test_retry_on_raises_after_exhausting_attempts() -> None:
    sleeps: list[float] = []
    calls = [0]

    @retry_on((A,), max_attempts=3, base_delay=1.0, sleep=sleeps.append)
    def fn() -> None:
        calls[0] += 1
        raise A("总是失败")

    with pytest.raises(A):
        fn()
    assert calls[0] == 3
    assert sleeps == [1.0, 2.0]  # 最后一次失败前 sleep 了 2 次
