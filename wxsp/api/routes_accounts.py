"""Accounts:列表 + 扫码登录 + 暂停/恢复 + 立即同步飞书。

设计要点:
- "扫码登录":POST 触发后台线程跑 check_cookie(),浏览器弹窗显示视频号二维码,
  用户在 patchright 窗口里扫;Web 不嵌入二维码(CDP 抓 canvas 复杂且不稳),
  接口立即返回"已弹出窗口,请扫码"提示,完成后 DB.cookie_status 自动刷新。
- "立即同步飞书":HTMX 同步触发 + 内联状态;失败用 HX-Trigger 推 opError 弹窗。
  原本走后台线程 + 跳转 + flash,但运营反馈"飞书挂了从 UI 看不出",改成在路由内
  阻塞跑 sync_now(),把结果直接渲染成片段 + 关键错误推弹窗。
- "暂停/恢复":同步改 DB.paused_until,直接 redirect 回列表。
- 大部分 POST 还是 303 redirect 回 /accounts;只有 sync 例外用 HTMX 片段。
"""

from __future__ import annotations

import json
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

# 并发触发 sync 没必要(浪费 API quota + 写库冲突)。HTMX 同步阻塞 + lock 即可,
# 抢不到锁返回'正在同步中'片段;sync_now 本身 1-5 秒。
_sync_lock = threading.Lock()

# 进程级 login 在飞 tracker:account_id → 仍在跑的 login Thread。
# 解决"运营点两次扫码登录"刷出两个 chromium 进程抢同一个 user_data_dir,导致后开的
# 那个抛 'Target page, context or browser has been closed' + DB cookie_status 被
# 覆盖成 unknown。Lock 保护 dict 读写;Thread.is_alive() 判断真实存活态。
_login_in_flight: dict[str, threading.Thread] = {}
_login_lock = threading.Lock()

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
    platform: str | None = None,
) -> HTMLResponse:
    db_rows = {a.id: a for a in session.exec(select(Account)).all()}
    # Collect available platforms for filter dropdown
    platforms_seen: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for aid, cfg in settings.accounts.items():
        a = db_rows.get(aid)
        p = getattr(cfg, "platform", "tencent_channel") or "tencent_channel"
        platforms_seen[p] = p
        if platform and p != platform:
            continue
        rows.append(
            {
                "id": aid,
                "display_name": cfg.display_name,
                "enabled": cfg.enabled,
                "daily_limit": cfg.daily_limit,
                "platform": p,
                "is_active": a.is_active if a else False,
                "cookie_status": a.cookie_status if a else "unknown",
                "cookie_last_active_at": a.cookie_last_active_at if a else None,
                "paused_until": a.paused_until if a else None,
                "in_db": a is not None,
            }
        )
    platform_options = sorted(platforms_seen.keys())
    return templates.TemplateResponse(
        request,
        "accounts.html",
        {
            "active": "accounts",
            "rows": rows,
            "flash": flash,
            "filter_platform": platform or "",
            "platform_options": platform_options,
        },
    )


def _redirect(flash: str, *, platform: str = "") -> RedirectResponse:
    qs = f"flash={flash}"
    if platform:
        qs += f"&platform={platform}"
    return RedirectResponse(url=f"/accounts?{qs}", status_code=303)


@router.post("/accounts/{account_id}/pause")
def pause_account(
    account_id: str,
    request: Request,
    hours: int = Form(24),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    row = session.get(Account, account_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 不存在")
    row.paused_until = datetime.now() + timedelta(hours=hours)
    session.add(row)
    plat = getattr(request.state, "current_platform", "") or ""
    return _redirect(f"已暂停 {account_id} {hours} 小时", platform=plat)


@router.post("/accounts/{account_id}/resume")
def resume_account(
    account_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    row = session.get(Account, account_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 不存在")
    row.paused_until = None
    session.add(row)
    plat = getattr(request.state, "current_platform", "") or ""
    return _redirect(f"已恢复 {account_id}", platform=plat)


@router.post("/accounts/{account_id}/login")
def login_account(
    account_id: str,
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """触发扫码登录:后台线程开 patchright,弹窗里显示平台二维码。

    去重:若该账号已有 login 线程在跑,**不再 spawn 第二个**,直接 redirect 提示
    去已弹出的窗口扫。原因:同一个 user_data_dir 不能被两个 chromium 同时打开,
    第二次 spawn 必然抛 'Target page, context or browser has been closed',且
    DB cookie_status 会被覆盖成 unknown,运营会困惑。
    """
    cfg = settings.accounts.get(account_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 未配置")
    if session.get(Account, account_id) is None:
        session.add(
            Account(
                id=account_id,
                display_name=cfg.display_name,
                user_data_dir=str(cfg.user_data_dir),
                daily_limit=cfg.daily_limit,
            )
        )
    plat = getattr(request.state, "current_platform", "") or ""
    # plat(请求上下文)优先于 AccountConfig.platform 的 Pydantic 默认值
    # (默认值 "tencent_channel" 对淘宝账号是错的)
    account_platform = plat or getattr(cfg, "platform", "") or "tencent_channel"
    with _login_lock:
        existing = _login_in_flight.get(account_id)
        if existing is not None and existing.is_alive():
            return _redirect(
                f"账号 {account_id} 已在扫码中,请到已弹出的浏览器窗口扫码;"
                "想取消可手动关掉那个窗口再重试",
                platform=plat,
            )
        thread = threading.Thread(
            target=_login_runner,
            args=(account_id, Path(cfg.user_data_dir), account_platform),
            daemon=True,
            name=f"web-login-{account_id}",
        )
        _login_in_flight[account_id] = thread
        thread.start()
    return _redirect(
        f"已弹出浏览器,请在窗口中扫码登录 {account_id}(完成后状态自动刷新)", platform=plat
    )


def _login_runner(account_id: str, user_data_dir: Path, platform: str = "tencent_channel") -> None:
    """_run_login 的薄包装:无论成功失败,finally 里把 _login_in_flight 清掉,
    让下次 POST 能起新线程。
    """
    try:
        _run_login(account_id, user_data_dir, platform=platform)
    finally:
        with _login_lock:
            _login_in_flight.pop(account_id, None)


@router.post("/accounts/sync", response_class=HTMLResponse)
def trigger_sync(request: Request, settings: Settings = Depends(get_settings)) -> HTMLResponse:
    """同步执行飞书 Bitable sync;返回 HTML 片段(HTMX 用)。

    设计:
    - 飞书未启用 → 200 + 灰色片段提示。
    - 锁占用 → 200 + 提示'正在同步中'(不重复触发)。
    - 成功 → 200 + 绿色片段 + 入库/拒绝计数。
    - 失败 → 200 + 红色片段 + HX-Trigger 头 opError 让前端弹 modal(因为 HTMX
      默认遇到非 2xx 会走 onResponseError 路径,我们用 200 + header 控更稳)。

    并发:_sync_lock 串行。抢不到 = 已有同步在跑,返回提示不重做。
    """
    platform = getattr(request.state, "current_platform", "tencent_channel") or "tencent_channel"
    if not settings.feishu.enabled:
        return HTMLResponse(_fragment("warn", "飞书未启用,跳过同步"))
    if not _sync_lock.acquire(blocking=False):
        return HTMLResponse(_fragment("warn", "正在同步中,请等当前同步完成"))
    try:
        from wxsp.sync import sync_now

        result = sync_now(settings, platform=platform)
    except Exception as exc:
        logger.exception(f"[web/feishu-sync] {exc}")
        msg = f"飞书同步失败:{exc}"
        return HTMLResponse(
            _fragment("error", msg),
            headers={
                "HX-Trigger": json.dumps({"opError": {"title": "飞书同步失败", "detail": str(exc)}})
            },
        )
    finally:
        _sync_lock.release()
    parts = [f"新入库 {result.accepted} 条"]
    if result.rejected:
        wb_note = ""
        if result.writeback_failed > 0:
            wb_note = f"(回写失败 {result.writeback_failed} 行,未同步到飞书)"
        else:
            wb_note = "(已回写飞书)"
        parts.append(f"校验失败 {result.rejected} 条{wb_note}")
    if result.skipped_existing:
        parts.append(f"已存在 {result.skipped_existing} 条跳过")
    if result.skipped_incomplete:
        parts.append(f"未填完 {result.skipped_incomplete} 条等下次")
    if result.writeback_failed > 0 and result.rejected == 0:
        parts.append(f"⚠ 回写失败 {result.writeback_failed} 行")
    level = "warn" if result.writeback_failed > 0 else "ok"
    return HTMLResponse(_fragment(level, "飞书同步完成:" + "、".join(parts)))


def _fragment(level: str, text: str) -> str:
    """渲染一个 flash 片段。level: ok | warn | error。"""
    safe = (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
    return f'<div class="flash {level}">{safe}</div>'


# ---------- 后台 worker(不持任何 request 资源) ----------


def _run_login(account_id: str, user_data_dir: Path, *, platform: str = "tencent_channel") -> None:
    """后台跑扫码:check_cookie 同步阻塞最长 5 分钟,完成后写 DB。"""
    from datetime import datetime as _dt

    from wxsp.browser import check_cookie
    from wxsp.db import get_engine, session_scope
    from wxsp.doctor import record_cookie_check

    try:
        is_logged_in: bool | None = check_cookie(
            user_data_dir, timeout_ms=300_000, account_id=account_id, platform=platform
        )
    except Exception as exc:
        logger.exception(f"[web/login] {account_id} 浏览器异常: {exc}")
        is_logged_in = None
    engine = get_engine()
    with session_scope(engine) as s:
        record_cookie_check(s, account_id, is_logged_in=is_logged_in, now=_dt.now())
