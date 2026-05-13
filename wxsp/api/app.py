"""FastAPI 入口:挂载路由 + 模板。

不做用户登录(本地单用户),不做 CORS(单进程同源)。
"""

from __future__ import annotations

from fastapi import FastAPI

from wxsp.api.log_stream import log_stream
from wxsp.api.routes_accounts import router as accounts_router
from wxsp.api.routes_config import router as config_router
from wxsp.api.routes_dashboard import router as dashboard_router
from wxsp.api.routes_logs import router as logs_router
from wxsp.api.routes_plans import router as plans_router
from wxsp.api.routes_tasks import router as tasks_router


def create_app() -> FastAPI:
    app = FastAPI(title="wxsp Web UI", docs_url=None, redoc_url=None)
    log_stream.attach_to_loguru()
    app.include_router(dashboard_router)
    app.include_router(accounts_router)
    app.include_router(tasks_router)
    app.include_router(plans_router)
    app.include_router(config_router)
    app.include_router(logs_router)
    return app


app = create_app()
