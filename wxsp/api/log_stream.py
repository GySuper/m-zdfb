"""loguru → SSE 桥。

设计要点:
- 进程级单例 `LogStream`:启动时挂一个 loguru sink,把每条日志推给所有订阅者的队列。
- 每个 SSE 连接 `subscribe()` 拿到自己的 `asyncio.Queue`(maxsize 限,防慢客户端炸内存),
  断开时 `unsubscribe()` 摘掉。
- 同时保留最近 200 条 ring buffer,新连接进来先回放(防止"打开页面什么都没有")。
- 线程安全:loguru sink 在 worker 线程触发,asyncio.Queue 的 `put_nowait` 是线程安全的;
  跨事件循环用 `loop.call_soon_threadsafe(queue.put_nowait, msg)` 兜底。
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from loguru import logger


class LogStream:
    """进程级日志广播。一个进程持一个实例(单例方便接 wxsp 全模块的 loguru)。"""

    def __init__(self, *, history: int = 200) -> None:
        self._subscribers: list[tuple[asyncio.Queue[str], asyncio.AbstractEventLoop]] = []
        self._lock = threading.Lock()
        self._history: deque[str] = deque(maxlen=history)
        self._sink_id: int | None = None

    # ---------- 生命周期 ----------

    def attach_to_loguru(self) -> None:
        """挂 loguru sink。重复调用幂等(同一进程只挂一次)。"""
        if self._sink_id is not None:
            return
        self._sink_id = logger.add(
            self._sink,
            level="INFO",
            format="{time:HH:mm:ss} | {level: <5} | {name}:{line} - {message}",
            enqueue=False,
        )

    def detach(self) -> None:
        if self._sink_id is not None:
            logger.remove(self._sink_id)
            self._sink_id = None

    # ---------- 订阅 ----------

    @contextmanager
    def subscribe(self, loop: asyncio.AbstractEventLoop) -> Iterator[asyncio.Queue[str]]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        # 先回放历史(不阻塞 sink)
        with self._lock:
            for line in self._history:
                try:
                    queue.put_nowait(line)
                except asyncio.QueueFull:
                    break
            self._subscribers.append((queue, loop))
        try:
            yield queue
        finally:
            with self._lock:
                self._subscribers = [s for s in self._subscribers if s[0] is not queue]

    # ---------- loguru sink(被 worker 线程调用) ----------

    def _sink(self, message: Any) -> None:
        # message 已经按 add() 的 format 格式化好;loguru Message 对象 str() 即结果。
        # 末尾通常带 \n,SSE data 字段不要带换行,strip 掉。
        line = str(message).rstrip("\n")
        with self._lock:
            self._history.append(line)
            subs = list(self._subscribers)
        for queue, loop in subs:
            try:
                loop.call_soon_threadsafe(self._try_put, queue, line)
            except RuntimeError:
                # loop 已关闭(连接断开但还没 unsubscribe 完);忽略。
                pass

    @staticmethod
    def _try_put(queue: asyncio.Queue[str], line: str) -> None:
        try:
            queue.put_nowait(line)
        except asyncio.QueueFull:
            # 慢客户端,丢一条提示一下,继续。
            try:
                queue.put_nowait("[log_stream] (overflow, dropped)")
            except asyncio.QueueFull:
                pass

    # ---------- 测试/调试 ----------

    def emit_for_test(self, line: str) -> None:
        """绕过 loguru,直接推一条 —— 仅供测试 / 启动横幅用。"""
        prefixed = f"{datetime.now().strftime('%H:%M:%S')} | INFO  | test - {line}"
        self._sink(prefixed)


# 进程级单例
log_stream = LogStream()
