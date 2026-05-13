"""重试装饰器:指数退避 + 异常类型白名单(M5)。"""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import wraps
from typing import TypeVar

T = TypeVar("T")


def retry_on(
    error_types: tuple[type[BaseException], ...],
    *,
    max_attempts: int,
    base_delay: float,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """异常白名单 + 指数退避的同步重试装饰器。

    - 只重试 `error_types` 元组里的异常;其它直接传出去
    - 第 N 次重试前 sleep `base_delay * 2 ** (N-1)` 秒
    - `max_attempts` 用完仍失败 → 抛最后一次的异常
    - `sleep` 参数注入用于测试(默认 `time.sleep`)
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapper(*args: object, **kwargs: object) -> T:
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except error_types as exc:
                    last_exc = exc
                    if attempt == max_attempts:
                        raise
                    sleep(base_delay * (2 ** (attempt - 1)))
            assert last_exc is not None  # unreachable
            raise last_exc

        return wrapper

    return decorator
