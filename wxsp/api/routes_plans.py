"""Plans:任意日期的任务清单(按 execute_date 查询;只读视图)。

Tasks 页和 Plans 页的区别:
- Tasks:默认 today + 有"重试 / 跑今天"按钮(可操作)
- Plans:任意 date + 按 account 分组展示,无操作按钮(纯日历视图)
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date as _date
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from wxsp.api.deps import get_session, templates
from wxsp.models import Task, Video

router = APIRouter()


@router.get("/plans", response_class=HTMLResponse)
def plans_page(
    request: Request,
    date: str | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    parsed = _parse_date(date) if date else _date.today()

    stmt = (
        select(Task, Video)
        .join(Video, Task.video_id == Video.id)  # type: ignore[arg-type]
        .where(Task.execute_date == parsed)
        .order_by(Task.account_id, Task.publish_at.asc())  # type: ignore[attr-defined]
    )
    pairs = list(session.exec(stmt).all())
    by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t, v in pairs:
        by_account[t.account_id].append(
            {
                "id": t.id,
                "title": v.title,
                "publish_at": t.publish_at,
                "status": t.status,
            }
        )
    groups = sorted(by_account.items(), key=lambda kv: kv[0])
    return templates.TemplateResponse(
        request,
        "plans.html",
        {
            "active": "plans",
            "filter_date": parsed,
            "groups": groups,
            "total": sum(len(v) for v in by_account.values()),
        },
    )


def _parse_date(s: str) -> _date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return _date.today()
