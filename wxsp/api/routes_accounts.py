"""Accounts:列表 + 扫码登录 + 暂停/恢复 + 立即同步飞书。

设计要点:
- "扫码登录":POST 触发后台线程跑 check_cookie(),浏览器弹窗显示视频号二维码,
  用户在 patchright 窗口里扫;Web 不嵌入二维码(CDP 抓 canvas 复杂且不稳),
  接口立即返回"已弹出窗口,请扫码"提示,完成后 DB.cookie_status 自动刷新。
- "立即同步飞书":后台线程跑 feishu.sync_now(),完成后回到列表页可见新任务。
- "暂停/恢复":同步改 DB.paused_until,直接 redirect 回列表。
- 所有变更通过 POST + 303 redirect 回 /accounts(无 HTMX hard reload),保证从
  CLI 改完刷新页面也能看到一致状态(避免 HTMX 缓存的乐观更新错位)。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger
from sqlmodel import Session, select

from wxsp.api.deps import get_session, get_settings, templates
from wxsp.config import Settings
from wxsp.models import Account

router = APIRouter()


def _spawn(name: str, fn: Any, *args: Any, **kwargs: Any) -> None:
    """启动 daemon 线程跑阻塞任务;异常 log 不抛。"""

    def runner() -> None:
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            logger.exception(f"[web/{name}] {exc}")

    threading.Thread(target=runner, daemon=True, name=f"web-{name}").start()


@router.get("/accounts", response_class=HTMLResponse)
def accounts_page(
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    flash: str | None = None,
) -> HTMLResponse:
    db_rows = {a.id: a for a in session.exec(select(Account)).all()}
    rows: list[dict[str, Any]] = []
    for aid, cfg in settings.accounts.items():
        a = db_rows.get(aid)
        rows.append(
            {
                "id": aid,
                "display_name": cfg.display_name,
                "enabled": cfg.enabled,
                "daily_limit": cfg.daily_limit,
                "is_active": a.is_active if a else False,
                "cookie_status": a.cookie_status if a else "unknown",
                "cookie_last_active_at": a.cookie_last_active_at if a else None,
                "paused_until": a.paused_until if a else None,
                "in_db": a is not None,
            }
        )
    return templates.TemplateResponse(
        request,
        "accounts.html",
        {"active": "accounts", "rows": rows, "flash": flash},
    )


def _redirect(flash: str) -> RedirectResponse:
    return RedirectResponse(url=f"/accounts?flash={flash}", status_code=303)


@router.post("/accounts/{account_id}/pause")
def pause_account(
    account_id: str,
    hours: int = Form(24),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    row = session.get(Account, account_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 不存在")
    row.paused_until = datetime.now() + timedelta(hours=hours)
    session.add(row)
    return _redirect(f"已暂停 {account_id} {hours} 小时")


@router.post("/accounts/{account_id}/resume")
def resume_account(
    account_id: str,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    row = session.get(Account, account_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 不存在")
    row.paused_until = None
    session.add(row)
    return _redirect(f"已恢复 {account_id}")


@router.post("/accounts/{account_id}/login")
def login_account(
    account_id: str,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """触发扫码登录:后台线程开 patchright,弹窗里显示视频号二维码。"""
    cfg = settings.accounts.get(account_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 未配置")
    # 确保 DB 里有这条记录(扫码完成后写 cookie 状态用)
    if session.get(Account, account_id) is None:
        session.add(
            Account(
                id=account_id,
                display_name=cfg.display_name,
                user_data_dir=str(cfg.user_data_dir),
                daily_limit=cfg.daily_limit,
            )
        )
    _spawn("login", _run_login, account_id, Path(cfg.user_data_dir))
    return _redirect(f"已弹出浏览器,请在窗口中扫码登录 {account_id}(完成后状态自动刷新)")


@router.post("/accounts/sync")
def trigger_sync(settings: Settings = Depends(get_settings)) -> RedirectResponse:
    """触发飞书 Bitable 同步(后台线程,完成后任务进库)。"""
    if not settings.feishu.enabled:
        return _redirect("飞书未启用,跳过同步")
    _spawn("feishu-sync", _run_sync, settings)
    return _redirect("已触发飞书同步,稍后刷新查看 Tasks")


# ---------- 后台 worker(不持任何 request 资源) ----------


def _run_login(account_id: str, user_data_dir: Path) -> None:
    """后台跑扫码:check_cookie 同步阻塞最长 5 分钟,完成后写 DB。"""
    from datetime import datetime as _dt

    from wxsp.browser import check_cookie
    from wxsp.db import get_engine, session_scope
    from wxsp.doctor import record_cookie_check

    try:
        is_logged_in: bool | None = check_cookie(user_data_dir, timeout_ms=300_000)
    except Exception as exc:
        logger.exception(f"[web/login] {account_id} 浏览器异常: {exc}")
        is_logged_in = None
    engine = get_engine()
    with session_scope(engine) as s:
        record_cookie_check(s, account_id, is_logged_in=is_logged_in, now=_dt.now())


def _run_sync(settings: Settings) -> None:
    """后台跑飞书 sync(sync_now 自管 session)。"""
    from wxsp.sync import sync_now

    sync_now(settings)
