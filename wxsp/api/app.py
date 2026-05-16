"""FastAPI 入口:挂载路由 + 模板。

不做用户登录(本地单用户),不做 CORS(单进程同源)。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse

from wxsp.api.log_stream import log_stream
from wxsp.api.routes_accounts import router as accounts_router
from wxsp.api.routes_config import router as config_router
from wxsp.api.routes_dashboard import router as dashboard_router
from wxsp.api.routes_logs import router as logs_router
from wxsp.api.routes_plans import router as plans_router
from wxsp.api.routes_setup import router as setup_router
from wxsp.api.routes_tasks import router as tasks_router
from wxsp.config import get_config_path

_SETUP_PREFIX = "/setup"
_STATIC_PREFIX = "/static"


def _setup_required() -> bool:
    return not get_config_path().exists()


def create_app() -> FastAPI:
    app = FastAPI(title="wxsp Web UI", docs_url=None, redoc_url=None)
    log_stream.attach_to_loguru()
    # 启动横幅:让 Web UI Logs 页面打开时至少能看到一行(防止"页面空白"误以为坏了)
    log_stream.emit_for_test("Web UI 启动,日志流就绪。任务运行时会自动推送 logger 输出。")

    @app.middleware("http")
    async def setup_redirect(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if (
            _setup_required()
            and not path.startswith(_SETUP_PREFIX)
            and not path.startswith(_STATIC_PREFIX)
        ):
            return RedirectResponse(url="/setup/step/1", status_code=302)
        return await call_next(request)

    app.include_router(setup_router)
    app.include_router(dashboard_router)
    app.include_router(accounts_router)
    app.include_router(tasks_router)
    app.include_router(plans_router)
    app.include_router(config_router)
    app.include_router(logs_router)
    return app


app = create_app()
