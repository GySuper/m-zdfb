"""Logs SSE 路由(M8)冒烟测试。

只测可独立验证的部分:
- /logs HTML 页面渲染
- LogStream 历史回放 / 订阅推送(纯单元)
SSE 长连接端点(/api/logs/stream)在 TestClient 同步 iter_lines 下会卡住,
靠 wxsp web 起来后人工肉眼验证(浏览器 EventSource 自动断/续)。
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from wxsp.api.app import create_app
from wxsp.api.log_stream import LogStream


def test_logs_page_renders() -> None:
    app = create_app()
    with TestClient(app) as c:
        r = c.get("/logs")
        assert r.status_code == 200
        assert "实时日志" in r.text
        assert "/api/logs/stream" in r.text


def test_log_stream_history_replays_to_new_subscriber() -> None:
    stream = LogStream(history=10)
    stream.emit_for_test("hello-A")
    stream.emit_for_test("hello-B")

    async def consume() -> list[str]:
        loop = asyncio.get_running_loop()
        with stream.subscribe(loop) as q:
            return [
                await asyncio.wait_for(q.get(), timeout=1.0),
                await asyncio.wait_for(q.get(), timeout=1.0),
            ]

    received = asyncio.run(consume())
    assert any("hello-A" in s for s in received)
    assert any("hello-B" in s for s in received)


def test_log_stream_live_emit_pushed_to_subscriber() -> None:
    """订阅之后再 emit,subscriber 队列应该立刻拿到。"""
    stream = LogStream(history=10)

    async def consume_one() -> str:
        loop = asyncio.get_running_loop()
        with stream.subscribe(loop) as q:
            stream.emit_for_test("live-line-Q")
            return await asyncio.wait_for(q.get(), timeout=1.0)

    line = asyncio.run(consume_one())
    assert "live-line-Q" in line
