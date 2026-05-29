"""Tasks 列表 + TaskDetail + 重试按钮 + "跑今天"按钮。

设计要点:
- 列表:按 date / account_id / status 三个 query 过滤;默认 today + 全状态。
- 详情:展示 Task 字段 + Video 标题 + 关联 events + 截图缩略图占位。
- 重试:把 task 重置回 pending(清 last_error_*、lease、started/finished),
  然后 spawn publisher.publish() 后台线程跑;前提 status ∈ {failed, interrupted}。
- "跑今天":spawn scheduler.run_today_pending(),完成后刷新可见。
"""

from __future__ import annotations

import json
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

_run_today_lock = threading.Lock()
_run_today_running = False

RETRYABLE_STATUSES = {"failed", "interrupted"}
# 重新入队适用于"积压"的两类:还没跑(pending)+ 跑了一半挂了(interrupted)。
# 失败任务(failed)走"重试"按钮——重试会重新认领 + 后台 spawn 跑;
# 重新入队只改 execute_date,等下次手动 run-today / 09:00 cron 才跑。
REQUEUE_STATUSES = {TASK_STATUS_PENDING, TASK_STATUS_INTERRUPTED}

# UI 上按"场景"分组,默认进入"待处理"——出问题的任务一眼看到。
# 与单 status 过滤(?status=xxx)互斥;bucket 优先。
BUCKETS = {
    "attention": ["failed", "interrupted", "pending"],
    "running": ["running"],
    "done": ["success", "skipped"],
}


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
    bucket: str | None = None,
    platform: str | None = None,
    backlog: int | None = None,
    flash: str | None = None,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    today = _date.today()
    backlog_mode = bool(backlog)
    parsed_date = None if backlog_mode else (_parse_date(date) if date else today)

    # bucket 默认:无 bucket 也无 status 时进"待处理";单独传 status 时不走 bucket
    if bucket is None and status is None and not backlog_mode:
        bucket = "attention"
    if bucket and bucket not in BUCKETS and bucket != "all":
        bucket = "all"

    # 先算每个 tab 的计数(同日期 + 账号过滤,不受 status/bucket 限制)
    count_stmt = select(Task.status).where(
        Task.execute_date == parsed_date if parsed_date is not None else Task.id.isnot(None)  # type: ignore[union-attr]
    )
    if backlog_mode:
        count_stmt = select(Task.status).where(
            Task.execute_date < today,
            col(Task.status).in_([TASK_STATUS_PENDING, TASK_STATUS_INTERRUPTED]),
        )
    if account:
        count_stmt = count_stmt.where(Task.account_id == account)
    if platform:
        count_stmt = count_stmt.where(Task.platform == platform)
    status_counter: dict[str, int] = {}
    for s in session.exec(count_stmt).all():
        status_counter[s] = status_counter.get(s, 0) + 1
    bucket_counts = {
        "attention": sum(status_counter.get(s, 0) for s in BUCKETS["attention"]),
        "running": sum(status_counter.get(s, 0) for s in BUCKETS["running"]),
        "done": sum(status_counter.get(s, 0) for s in BUCKETS["done"]),
        "all": sum(status_counter.values()),
    }

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
    if platform:
        stmt = stmt.where(Task.platform == platform)
    if status:
        stmt = stmt.where(Task.status == status)
    elif bucket and bucket in BUCKETS:
        stmt = stmt.where(col(Task.status).in_(BUCKETS[bucket]))
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
                "platform": t.platform,
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
    # Collect available platform options for filter dropdown
    platform_set: set[str] = set()
    for t, _v in pairs:
        if t.platform:
            platform_set.add(t.platform)
    # Also include platforms from account configs
    for cfg in settings.accounts.values():
        p = getattr(cfg, "platform", "tencent_channel") or "tencent_channel"
        platform_set.add(p)
    platform_options = sorted(platform_set)
    return templates.TemplateResponse(
        request,
        "tasks.html",
        {
            "active": "tasks",
            "rows": rows,
            "filter_date": parsed_date,
            "filter_account": account or "",
            "filter_status": status or "",
            "filter_bucket": bucket or "",
            "filter_platform": platform or "",
            "bucket_counts": bucket_counts,
            "backlog_mode": backlog_mode,
            "account_options": account_options,
            "platform_options": platform_options,
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


@router.post("/tasks/run-today", response_class=HTMLResponse)
def run_today(request: Request, settings: Settings = Depends(get_settings)) -> HTMLResponse:
    """先在路由内同步飞书(失败弹窗 + 不发布),再 spawn 发布循环异步跑。

    设计:
    - sync 在路由内做(1-5s),失败 → 红色片段 + HX-Trigger opError 弹 modal,
      不进入发布阶段(避免静默继续跑旧数据)。
    - sync 成功 → spawn `run_today_pending(do_sync=False)`,发布循环异步跑,
      失败细节走 publisher 自己的截图/告警,UI 显示"已开始跑"。
    - _run_today_running 防并发触发;sync 还在跑时第二次点会被 lock 拒。
    """
    platform = getattr(request.state, "current_platform", "tencent_channel") or "tencent_channel"
    global _run_today_running
    with _run_today_lock:
        if _run_today_running:
            return HTMLResponse(_fragment("warn", "正在跑今天,请等当前一轮完成后再触发"))
        _run_today_running = True

    try:
        try:
            from wxsp.sync import sync_now

            sync_result = sync_now(settings, platform=platform) if settings.feishu.enabled else None
        except Exception as exc:
            logger.exception(f"[web/run-today] sync_now 挂了,放弃发布: {exc}")
            return HTMLResponse(
                _fragment("error", f"飞书同步失败,未启动发布:{exc}"),
                headers={
                    "HX-Trigger": json.dumps(
                        {"opError": {"title": "飞书同步失败", "detail": str(exc)}}
                    )
                },
            )

        def _run_and_release() -> None:
            global _run_today_running
            try:
                _run_today_pending(settings, platform=platform)
            finally:
                with _run_today_lock:
                    _run_today_running = False

        _spawn("run-today", _run_and_release)
        # 注意:这里把 sync 状态从 finally 中拿出来,不释放 _run_today_running——
        # 发布循环结束时它自己会释放。下面 return 之前不能进 finally。
    except Exception:
        with _run_today_lock:
            _run_today_running = False
        raise

    if sync_result is None:
        msg = "飞书未启用,直接跑今天 pending(已开始,完成后任务状态会变化)"
    else:
        bits = [f"飞书同步完成(新入库 {sync_result.accepted} 条"]
        if sync_result.rejected:
            bits.append(f",校验失败 {sync_result.rejected} 条")
        if sync_result.skipped_incomplete:
            bits.append(f",未填完 {sync_result.skipped_incomplete} 条等下次")
        bits.append("),发布循环已开始,等任务列表自动刷新")
        msg = "".join(bits)
    return HTMLResponse(_fragment("ok", msg))


def _fragment(level: str, text: str) -> str:
    """渲染一个 flash 片段(HTMX 用)。level: ok | warn | error。"""
    safe = (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
    return f'<div class="flash {level}">{safe}</div>'


# ---------- 后台 worker ----------


def _run_publish(task_id: int, settings: Settings) -> None:
    from wxsp.publisher import publish

    publish(task_id, settings=settings)


def _run_today_pending(settings: Settings, *, platform: str = "tencent_channel") -> None:
    """发布部分(sync 在路由里已经做过,这里跳过避免重复)。"""
    from wxsp.scheduler import run_today_pending

    run_today_pending(settings, do_sync=False, platform=platform)


def _parse_date(s: str) -> _date | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None
