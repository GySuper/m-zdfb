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
from sqlmodel import Session, col, select

from wxsp.api.deps import get_session, get_settings, templates
from wxsp.config import Settings
from wxsp.models import (
    TASK_STATUS_INTERRUPTED,
    TASK_STATUS_PENDING,
    Account,
    Event,
    Task,
    Video,
)

router = APIRouter()

RETRYABLE_STATUSES = {"failed", "interrupted"}
# 重新入队适用于"积压"的两类:还没跑(pending)+ 跑了一半挂了(interrupted)。
# 失败任务(failed)走"重试"按钮——重试会重新认领 + 后台 spawn 跑;
# 重新入队只改 execute_date,等下次手动 run-today / 09:00 cron 才跑。
REQUEUE_STATUSES = {TASK_STATUS_PENDING, TASK_STATUS_INTERRUPTED}


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
    backlog: int | None = None,
    flash: str | None = None,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    today = _date.today()
    backlog_mode = bool(backlog)
    parsed_date = None if backlog_mode else (_parse_date(date) if date else today)

    stmt = select(Task, Video).join(Video, Task.video_id == Video.id)  # type: ignore[arg-type]
    if backlog_mode:
        # spec §5.6 "历史积压":execute_date < today AND status ∈ {pending, interrupted}
        stmt = stmt.where(
            Task.execute_date < today,
            col(Task.status).in_([TASK_STATUS_PENDING, TASK_STATUS_INTERRUPTED]),
        )
    elif parsed_date is not None:
        stmt = stmt.where(Task.execute_date == parsed_date)
    if account:
        stmt = stmt.where(Task.account_id == account)
    if status:
        stmt = stmt.where(Task.status == status)
    stmt = stmt.order_by(Task.execute_date.asc(), Task.publish_at.asc())  # type: ignore[attr-defined]

    pairs = list(session.exec(stmt).all())
    rows: list[dict[str, Any]] = []
    for t, v in pairs:
        is_backlog_row = t.execute_date < today and t.status in REQUEUE_STATUSES
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
                "requeueable": is_backlog_row,
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
            "backlog_mode": backlog_mode,
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

    today = _date.today()
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
            "requeueable": task.execute_date < today and task.status in REQUEUE_STATUSES,
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


@router.post("/tasks/{task_id}/requeue")
def requeue_task(
    task_id: int,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """把积压(execute_date<today + pending/interrupted)的 task 改 execute_date=today。

    spec §5.6:不做自动 rollover,运营在 Dashboard 看到积压后手动决定要不要拉到今天。
    本路由只改日期 + 重置到 pending(清错误/lease);需要运营再点"立即跑今天"才会真跑。
    """
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} 不存在")
    today = _date.today()
    if task.status not in REQUEUE_STATUSES:
        return RedirectResponse(
            url=f"/tasks/{task_id}?flash=当前状态不能重新入队: {task.status}",
            status_code=303,
        )
    if task.execute_date >= today:
        return RedirectResponse(
            url=f"/tasks/{task_id}?flash=执行日期已是今天或更晚,无需重新入队",
            status_code=303,
        )
    task.execute_date = today
    task.status = "pending"
    task.lease_token = None
    task.lease_expires_at = None
    task.started_at = None
    task.finished_at = None
    task.last_error_type = None
    task.last_error_msg = None
    session.add(task)
    return RedirectResponse(
        url=f"/tasks/{task_id}?flash=已重新入队到今天(还需点'立即跑今天'才会真跑)",
        status_code=303,
    )


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
