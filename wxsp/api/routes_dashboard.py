"""Dashboard:今日总览(账号卡片 + 进度 + 最近事件 + 历史积压)。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, col, select

from wxsp.api.deps import get_session, get_settings, templates
from wxsp.config import Settings
from wxsp.models import TASK_STATUS_INTERRUPTED, TASK_STATUS_PENDING, Account, Event, Task

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    platform: str = "tencent_channel",
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    today = date.today()

    accounts = list(session.exec(select(Account).where(Account.platform == platform)).all())
    accounts_by_id = {a.id: a for a in accounts}

    # 今日任务计数(按状态) — 只统计当前平台
    today_tasks = list(
        session.exec(
            select(Task).where(Task.execute_date == today, Task.platform == platform)
        ).all()
    )
    counts: dict[str, int] = {
        "pending": 0,
        "running": 0,
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "interrupted": 0,
    }
    per_account: dict[str, dict[str, int]] = {a.id: dict.fromkeys(counts, 0) for a in accounts}
    for t in today_tasks:
        counts[t.status] = counts.get(t.status, 0) + 1
        bucket = per_account.setdefault(t.account_id, dict.fromkeys(counts, 0))
        bucket[t.status] = bucket.get(t.status, 0) + 1

    # 历史积压 — 只统计当前平台
    backlog = len(
        list(
            session.exec(
                select(Task).where(
                    Task.execute_date < today,
                    Task.platform == platform,
                    col(Task.status).in_([TASK_STATUS_PENDING, TASK_STATUS_INTERRUPTED]),
                )
            ).all()
        )
    )

    # 最近 10 个事件 — 只统计当前平台账号
    account_ids_for_platform = [a.id for a in accounts]
    recent_events_query = select(Event).order_by(Event.id.desc()).limit(10)  # type: ignore[union-attr]
    if account_ids_for_platform:
        recent_events_query = recent_events_query.where(
            col(Event.account_id).in_(account_ids_for_platform)
        )
    recent_events = list(session.exec(recent_events_query).all())

    # 账号卡片 — 只包含当前平台的配置
    account_cards: list[dict[str, Any]] = []
    for aid, cfg in settings.accounts.items():
        if getattr(cfg, "platform", "tencent_channel") != platform:
            continue
        a = accounts_by_id.get(aid)
        bucket = per_account.get(aid, dict.fromkeys(counts, 0))
        account_cards.append(
            {
                "id": aid,
                "display_name": cfg.display_name,
                "enabled": cfg.enabled,
                "daily_limit": cfg.daily_limit,
                "platform": getattr(cfg, "platform", "tencent_channel") or "tencent_channel",
                "is_active": a.is_active if a else False,
                "cookie_status": a.cookie_status if a else "unknown",
                "cookie_last_active_at": a.cookie_last_active_at if a else None,
                "paused_until": a.paused_until if a else None,
                "today": bucket,
            }
        )

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active": "dashboard",
            "today": today,
            "now": datetime.now(),
            "account_cards": account_cards,
            "counts": counts,
            "backlog": backlog,
            "recent_events": recent_events,
        },
    )
