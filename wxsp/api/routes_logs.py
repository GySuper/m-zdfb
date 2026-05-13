"""Logs:HTML 页面 + SSE 实时流端点。

- GET /logs 渲染壳子(空 <pre>,前端 EventSource 订阅 /api/logs/stream 往里灌)
- GET /api/logs/stream 返回 text/event-stream,每条日志一条 SSE event
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from wxsp.api.deps import templates
from wxsp.api.log_stream import log_stream

router = APIRouter()


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "logs.html", {"active": "logs"})


@router.get("/api/logs/stream")
async def logs_stream(request: Request) -> StreamingResponse:
    """SSE 长连接:从 LogStream 拿日志,以 SSE event 形式推送。

    断开:客户端关掉 EventSource → starlette 触发 cancel → 我们 unsubscribe。
    心跳:每 15s 发一条 `:ping` 注释帧,避中间代理掐断空闲连接。
    """
    loop = asyncio.get_running_loop()

    async def event_generator() -> AsyncIterator[str]:
        with log_stream.subscribe(loop) as queue:
            yield ": connected\n\n"  # 启动一个握手注释
            while True:
                if await request.is_disconnected():
                    break
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                # SSE 协议:多行 data 要 prefix 每行,这里 line 已经被 rstrip 过且无换行。
                yield f"data: {line}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 让 nginx 不要缓冲(本地直连也无害)
        },
    )
