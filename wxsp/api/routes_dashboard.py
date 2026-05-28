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
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    today = date.today()

    accounts = list(session.exec(select(Account)).all())
    accounts_by_id = {a.id: a for a in accounts}

    # 今日任务计数(按状态)
    today_tasks = list(session.exec(select(Task).where(Task.execute_date == today)).all())
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

    # 历史积压:execute_date < today 且 status ∈ {pending, interrupted}
    # spec §5.6:pending(还没跑)+ interrupted(跑了一半挂了)都算积压;
    # failed/success/skipped 不计(需要走重试或已经走完了)。
    backlog = len(
        list(
            session.exec(
                select(Task).where(
                    Task.execute_date < today,
                    col(Task.status).in_([TASK_STATUS_PENDING, TASK_STATUS_INTERRUPTED]),
                )
            ).all()
        )
    )

    # 最近 10 个事件(倒序)
    recent_events = list(
        session.exec(select(Event).order_by(Event.id.desc()).limit(10)).all()  # type: ignore[union-attr]
    )

    # 把账号 + 当日计数 + 配置里的 daily_limit 合并
    account_cards: list[dict[str, Any]] = []
    for aid, cfg in settings.accounts.items():
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
