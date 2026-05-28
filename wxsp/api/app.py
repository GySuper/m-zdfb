"""FastAPI 入口:挂载路由 + 模板。

不做用户登录(本地单用户),不做 CORS(单进程同源)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from apscheduler.schedulers.base import BaseScheduler  # type: ignore[import-untyped]
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from loguru import logger

from wxsp.api.log_stream import log_stream
from wxsp.api.routes_accounts import router as accounts_router
from wxsp.api.routes_config import router as config_router
from wxsp.api.routes_dashboard import router as dashboard_router
from wxsp.api.routes_logs import router as logs_router
from wxsp.api.routes_plans import router as plans_router
from wxsp.api.routes_setup import router as setup_router
from wxsp.api.routes_tasks import router as tasks_router
from wxsp.archive import cleanup_old_files
from wxsp.config import (
    ALL_PLATFORMS,
    get_config_path,
    get_default_platform,
    load_settings,
    platform_label,
)
from wxsp.db import get_engine, init_db, session_scope
from wxsp.scheduler import (
    make_scheduler,
    mark_stale_running_as_interrupted,
    maybe_warn_backlog,
)

_SETUP_PREFIX = "/setup"
_STATIC_PREFIX = "/static"


def _setup_required() -> bool:
    """setup 向导模式: 没有任何平台配置文件时启用。"""
    from wxsp.config import ALL_PLATFORMS
    from wxsp.config import get_config_path as _gcp

    return not any(_gcp(p).exists() for p in ALL_PLATFORMS)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时:跑 daemon 的副作用 + 起 BackgroundScheduler;关闭时 shutdown。

    `wxsp web`(以及打包后双击 .exe → launcher 默认追加 `web`)走这里,
    所以 09:00 cron 必须在这里注册,否则 Web UI 模式下定时任务永远不触发。
    setup 模式(config.yaml 缺失)只 yield,不起 scheduler,等向导写完 config
    用户重启进程后再起。
    """
    scheduler: BaseScheduler | None = None
    if _setup_required():
        logger.info("[api] config.yaml 缺失,跳过 scheduler 启动(走 setup 向导)")
    else:
        try:
            settings = load_settings()
        except Exception as exc:
            logger.warning(f"[api] 加载 settings 失败,跳过 scheduler 启动: {exc}")
            settings = None

        if settings is not None:
            engine = get_engine()
            init_db(engine)
            try:
                with session_scope(engine) as session:
                    touched = mark_stale_running_as_interrupted(session)
                if touched:
                    logger.warning(f"[api] 启动时标 interrupted: {touched} 条僵尸 running")
            except Exception as exc:
                logger.warning(f"[api] 启动 mark_stale 失败,忽略: {exc}")

            try:
                cleanup_old_files(
                    logs_dir=settings.app.logs_dir,
                    log_retention_days=settings.monitoring.log_retention_days,
                    screenshot_retention_days=settings.monitoring.screenshot_retention_days,
                )
            except Exception as exc:
                logger.warning(f"[api] 启动归档失败,忽略: {exc}")

            try:
                with session_scope(engine) as session:
                    backlog = maybe_warn_backlog(settings, session=session)
                logger.info(f"[api] 启动时历史积压: {backlog} 条")
            except Exception as exc:
                logger.warning(f"[api] 启动积压检查失败,忽略: {exc}")

            try:
                scheduler = make_scheduler(settings, blocking=False)
                scheduler.start()
                if settings.scheduler.enabled:
                    logger.info(
                        f"[api] 每日 cron 已注册:"
                        f"{settings.scheduler.daily_cron_hour:02d}:"
                        f"{settings.scheduler.daily_cron_minute:02d} "
                        f"({settings.app.timezone}) 跑 run_today_pending"
                    )
            except Exception as exc:
                logger.error(f"[api] scheduler 启动失败,定时任务将不会触发: {exc}")
                scheduler = None
    try:
        yield
    finally:
        if scheduler is not None:
            try:
                scheduler.shutdown(wait=False)
            except Exception as exc:
                logger.warning(f"[api] scheduler.shutdown 失败,忽略: {exc}")


def create_app() -> FastAPI:
    app = FastAPI(title="wxsp Web UI", docs_url=None, redoc_url=None, lifespan=_lifespan)
    log_stream.attach_to_loguru()
    # 启动横幅:让 Web UI Logs 页面打开时至少能看到一行(防止"页面空白"误以为坏了)
    log_stream.emit_for_test("Web UI 启动,日志流就绪。任务运行时会自动推送 logger 输出。")

    @app.middleware("http")
    async def platform_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Inject current platform into request.state. Redirect if missing ?platform=."""
        path = request.url.path
        if not path.startswith(_SETUP_PREFIX) and not path.startswith(_STATIC_PREFIX):
            # Build platform list
            platforms = {}
            for pkey in ALL_PLATFORMS:
                try:
                    configured = get_config_path(pkey).exists()
                except Exception:
                    configured = False
                platforms[pkey] = {
                    "key": pkey,
                    "label": platform_label(pkey),
                    "configured": configured,
                }
            # Resolve current platform
            default_p = get_default_platform()
            current = request.query_params.get("platform")
            if current and current in platforms:
                pass  # explicit ?platform= in URL
            else:
                # No valid platform in URL -> redirect to add it
                from fastapi.responses import RedirectResponse as _Redir

                qp = dict(request.query_params)
                qp["platform"] = default_p
                qs = "&".join(f"{k}={v}" for k, v in qp.items())
                target = f"{path}?{qs}" if qs else f"{path}?platform={default_p}"
                return _Redir(url=target, status_code=302)
            request.state.platforms = platforms
            request.state.current_platform = current
            request.state.has_multiple_platforms = len(platforms) > 1
        return await call_next(request)

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
