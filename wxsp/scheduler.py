"""09:00 cron + 手动 fire(无 polling)(M6)。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore[import-untyped]
from apscheduler.schedulers.base import BaseScheduler  # type: ignore[import-untyped]
from apscheduler.schedulers.blocking import BlockingScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from loguru import logger
from sqlalchemy import update
from sqlmodel import Session, col, select

from wxsp.archive import cleanup_old_files, install_file_sink
from wxsp.config import Settings
from wxsp.db import get_engine, init_db, session_scope
from wxsp.models import (
    TASK_STATUS_INTERRUPTED,
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    Account,
    Task,
)
from wxsp.notify import NotifyEvent, notify
from wxsp.publisher import AlreadyClaimed, publish
from wxsp.sync import sync_now


@dataclass
class RunSummary:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped_paused: int = 0


def queue_today(session: Session, *, today: _date | None = None) -> list[int]:
    """返回今天的 pending task id 列表,按 publish_at 升序。

    调用方负责开 session;本函数只读。
    """
    if today is None:
        today = _date.today()
    stmt = (
        select(Task)
        .where(Task.execute_date == today)
        .where(Task.status == TASK_STATUS_PENDING)
        .order_by(col(Task.publish_at).asc())
    )
    return [task.id for task in session.exec(stmt) if task.id is not None]


def count_backlog(session: Session, *, today: _date | None = None) -> int:
    """spec §5.6 历史积压计数:execute_date < today AND status ∈ {pending, interrupted}。

    Daemon 启动用、积压告警判定用。本函数只读、不 commit。
    """
    if today is None:
        today = _date.today()
    stmt = select(Task).where(
        Task.execute_date < today,
        col(Task.status).in_([TASK_STATUS_PENDING, TASK_STATUS_INTERRUPTED]),
    )
    return len(list(session.exec(stmt).all()))


def maybe_warn_backlog(settings: Settings, *, session: Session) -> int:
    """积压超阈值 → 推一条 backlog_high 通知(企微 + Event 表)。返回当前积压数。

    阈值 = `monitoring.backlog_warn_threshold`(默认 20)。
    去重交给 notify() 自己 —— 这里每次调用都会落一条 Event,但只有 backlog_high
    在 `monitoring.notify_on` 里时才推外部渠道。重复推送由调用频率决定(目前每天 09:00
    cron + daemon 启动各一次),不在本函数里防抖。
    """
    backlog = count_backlog(session)
    if backlog > settings.monitoring.backlog_warn_threshold:
        notify(
            NotifyEvent(
                type="backlog_high",
                level="warn",
                title=f"历史积压 {backlog} 条",
                content=(
                    f"今天之前还有 {backlog} 条任务没跑完(pending/interrupted),"
                    f"超过阈值 {settings.monitoring.backlog_warn_threshold}。"
                    f"建议在 Web UI 的'历史积压'视图里逐条处理。"
                ),
                context={"backlog": backlog},
            ),
            session=session,
            settings=settings,
        )
    return backlog


def mark_stale_running_as_interrupted(session: Session, *, now: datetime | None = None) -> int:
    """启动时一次性把 `running` + lease 过期的 task 标为 `interrupted`。

    返回被改的行数。调用方负责开 session;本函数自己 commit(独立事务)。
    """
    if now is None:
        now = datetime.now()
    stmt = (
        update(Task)
        .where(Task.status == TASK_STATUS_RUNNING)  # type: ignore[arg-type]
        .where(col(Task.lease_expires_at).is_not(None))
        .where(col(Task.lease_expires_at) < now)
        .values(status=TASK_STATUS_INTERRUPTED)
    )
    result = session.execute(stmt)
    session.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def run_today_pending(settings: Settings) -> RunSummary:
    """worker 入口:① sync_now() ② queue_today() ③ 串行跑(跳过 paused 账号)。

    任何一条 task 失败 / 抛 AlreadyClaimed 都不阻断后面的;细节走 publish 自己的回写。
    """
    try:
        sync_now(settings)
    except Exception as exc:
        logger.warning(f"[scheduler] sync_now 失败,继续跑已入库的: {exc}")

    summary = RunSummary()
    engine = get_engine()
    init_db(engine)
    now = datetime.now()

    # 一次性把 (task_id, account_id) 读出来,后面循环里不再持 session 跑长 publish
    with Session(engine) as session:
        task_ids = queue_today(session)
        if not task_ids:
            return summary
        plan: list[tuple[int, str]] = []
        for tid in task_ids:
            t = session.get(Task, tid)
            if t is None:
                continue
            plan.append((tid, t.account_id))
        # 一次性拿 account.paused_until 快照
        paused_accounts: set[str] = set()
        for acc_id in {acc for _, acc in plan}:
            acc = session.get(Account, acc_id)
            if acc is not None and acc.paused_until is not None and acc.paused_until > now:
                paused_accounts.add(acc_id)

    for task_id, account_id in plan:
        if account_id in paused_accounts:
            summary.skipped_paused += 1
            logger.info(f"[scheduler] 跳过 task={task_id}:账号 {account_id} 暂停中")
            continue
        summary.attempted += 1
        try:
            result = publish(task_id, dry_run=False, settings=settings)
        except AlreadyClaimed as exc:
            summary.failed += 1
            logger.warning(f"[scheduler] task={task_id} 已被认领: {exc}")
            continue
        except Exception as exc:
            summary.failed += 1
            logger.exception(f"[scheduler] task={task_id} 跑挂了: {exc}")
            continue
        if result.ok:
            summary.succeeded += 1
        else:
            summary.failed += 1

    return summary


def make_scheduler(settings: Settings, *, blocking: bool = False) -> BaseScheduler:
    """构造 APScheduler 并注册"每日 09:00 cron job"。

    blocking=True 用于 daemon 进程(主线程 .start() 阻塞);
    blocking=False(默认)用于测试 / 后台模式。**调用方负责 shutdown**。
    """
    scheduler: BaseScheduler
    if blocking:
        scheduler = BlockingScheduler(timezone=settings.app.timezone)
    else:
        scheduler = BackgroundScheduler(timezone=settings.app.timezone)
    scheduler.add_job(
        run_today_pending,
        trigger=CronTrigger(
            hour=settings.scheduler.daily_cron_hour,
            minute=settings.scheduler.daily_cron_minute,
            timezone=settings.app.timezone,
        ),
        args=[settings],
        id="daily_run_today_pending",
        replace_existing=True,
    )
    return scheduler


def start_daemon(settings: Settings) -> None:
    """daemon 入口:启动时 (1) 标 interrupted (2) 清过期日志/截图 (3) 积压告警判定
    (4) 起 blocking scheduler。

    `BlockingScheduler.start()` 会阻塞当前线程,直到 SIGINT/SIGTERM。
    """
    # M9 文件 sink:必须在 cleanup_old_files 之前装,否则启动日志只在 stderr
    try:
        install_file_sink(
            logs_dir=settings.app.logs_dir,
            retention_days=settings.monitoring.log_retention_days,
        )
    except Exception as exc:
        logger.warning(f"[scheduler] 装日志 sink 失败,继续走 stderr: {exc}")

    engine = get_engine()
    init_db(engine)
    with session_scope(engine) as session:
        touched = mark_stale_running_as_interrupted(session)
    if touched:
        logger.warning(f"[scheduler] 启动时标 interrupted: {touched} 条僵尸 running")

    # M9 启动时一次性归档(spec §6.3),失败不影响 daemon
    try:
        cleanup_old_files(
            logs_dir=settings.app.logs_dir,
            log_retention_days=settings.monitoring.log_retention_days,
            screenshot_retention_days=settings.monitoring.screenshot_retention_days,
        )
    except Exception as exc:
        logger.warning(f"[scheduler] 启动归档失败,忽略: {exc}")

    # M9 启动时一次性积压告警(spec §5.6)
    with session_scope(engine) as session:
        try:
            backlog = maybe_warn_backlog(settings, session=session)
            logger.info(f"[scheduler] 启动时历史积压: {backlog} 条")
        except Exception as exc:
            logger.warning(f"[scheduler] 启动积压检查失败,忽略: {exc}")

    scheduler = make_scheduler(settings, blocking=True)
    logger.info(
        f"[scheduler] daemon 启动:每日 "
        f"{settings.scheduler.daily_cron_hour:02d}:{settings.scheduler.daily_cron_minute:02d} "
        f"({settings.app.timezone}) 跑 run_today_pending"
    )
    scheduler.start()  # 阻塞
