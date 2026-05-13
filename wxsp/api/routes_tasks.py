"""Tasks 列表 + TaskDetail + 重试按钮 + "跑今天"按钮。

设计要点:
- 列表:按 date / account_id / status 三个 query 过滤;默认 today + 全状态。
- 详情:展示 Task 字段 + Video 标题 + 关联 events + 截图缩略图占位。
- 重试:把 task 重置回 pending(清 last_error_*、lease、started/finished),
  然后 spawn publisher.publish() 后台线程跑;前提 status ∈ {failed, interrupted}。
- "跑今天":spawn scheduler.run_today_pending(),完成后刷新可见。
"""

from __future__ import annotations

import threading
from datetime import date as _date
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger
from sqlmodel import Session, select

from wxsp.api.deps import get_session, get_settings, templates
from wxsp.config import Settings
from wxsp.models import Account, Event, Task, Video

router = APIRouter()

RETRYABLE_STATUSES = {"failed", "interrupted"}


def _spawn(name: str, fn: Any, *args: Any, **kwargs: Any) -> None:
    def runner() -> None:
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            logger.exception(f"[web/{name}] {exc}")

    threading.Thread(target=runner, daemon=True, name=f"web-{name}").start()


@router.get("/tasks", response_class=HTMLResponse)
def tasks_page(
    request: Request,
    date: str | None = None,
    account: str | None = None,
    status: str | None = None,
    flash: str | None = None,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    parsed_date = _parse_date(date) if date else _date.today()

    stmt = select(Task, Video).join(Video, Task.video_id == Video.id)  # type: ignore[arg-type]
    if parsed_date is not None:
        stmt = stmt.where(Task.execute_date == parsed_date)
    if account:
        stmt = stmt.where(Task.account_id == account)
    if status:
        stmt = stmt.where(Task.status == status)
    stmt = stmt.order_by(Task.publish_at.asc())  # type: ignore[attr-defined]

    pairs = list(session.exec(stmt).all())
    rows: list[dict[str, Any]] = []
    for t, v in pairs:
        rows.append(
            {
                "id": t.id,
                "title": v.title,
                "account_id": t.account_id,
                "execute_date": t.execute_date,
                "publish_at": t.publish_at,
                "status": t.status,
                "attempts": t.attempts,
                "last_error_type": t.last_error_type,
                "remote_url": t.remote_url,
                "retryable": t.status in RETRYABLE_STATUSES,
            }
        )

    account_options = list(settings.accounts.keys())
    return templates.TemplateResponse(
        request,
        "tasks.html",
        {
            "active": "tasks",
            "rows": rows,
            "filter_date": parsed_date,
            "filter_account": account or "",
            "filter_status": status or "",
            "account_options": account_options,
            "status_options": ["pending", "running", "success", "failed", "skipped", "interrupted"],
            "flash": flash,
        },
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(
    request: Request,
    task_id: int,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} 不存在")
    video = session.get(Video, task.video_id)
    account = session.get(Account, task.account_id)
    events = list(
        session.exec(
            select(Event).where(Event.task_id == task_id).order_by(Event.id.desc())  # type: ignore[union-attr]
        ).all()
    )
    import json as _json

    try:
        screenshots = _json.loads(task.screenshots_json)
        if not isinstance(screenshots, list):
            screenshots = []
    except Exception:
        screenshots = []

    return templates.TemplateResponse(
        request,
        "task_detail.html",
        {
            "active": "tasks",
            "task": task,
            "video": video,
            "account": account,
            "events": events,
            "screenshots": screenshots,
            "retryable": task.status in RETRYABLE_STATUSES,
        },
    )


@router.post("/tasks/{task_id}/retry")
def retry_task(
    task_id: int,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} 不存在")
    if task.status not in RETRYABLE_STATUSES:
        return RedirectResponse(
            url=f"/tasks/{task_id}?flash={'当前状态不可重试: ' + task.status}",
            status_code=303,
        )
    # 重置回 pending(claim_task 才会生效)。attempts 保留累加,审计用。
    task.status = "pending"
    task.lease_token = None
    task.lease_expires_at = None
    task.started_at = None
    task.finished_at = None
    task.last_error_type = None
    task.last_error_msg = None
    session.add(task)
    _spawn("retry", _run_publish, task_id, settings)
    return RedirectResponse(url=f"/tasks/{task_id}?flash=已加入队列重试", status_code=303)


@router.post("/tasks/run-today")
def run_today(settings: Settings = Depends(get_settings)) -> RedirectResponse:
    _spawn("run-today", _run_today_pending, settings)
    today = _date.today().isoformat()
    return RedirectResponse(
        url=f"/tasks?date={today}&flash=已触发跑今天,完成后刷新", status_code=303
    )


# ---------- 后台 worker ----------


def _run_publish(task_id: int, settings: Settings) -> None:
    from wxsp.publisher import publish

    publish(task_id, settings=settings)


def _run_today_pending(settings: Settings) -> None:
    from wxsp.scheduler import run_today_pending

    run_today_pending(settings)


def _parse_date(s: str) -> _date | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None
