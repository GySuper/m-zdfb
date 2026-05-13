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

from wxsp.config import Settings
from wxsp.db import get_engine, init_db, session_scope
from wxsp.models import (
    TASK_STATUS_INTERRUPTED,
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    Account,
    Task,
)
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
    """daemon 入口:启动时 (1) 标 interrupted (2) 起 blocking scheduler。

    `BlockingScheduler.start()` 会阻塞当前线程,直到 SIGINT/SIGTERM。
    """
    engine = get_engine()
    init_db(engine)
    with session_scope(engine) as session:
        touched = mark_stale_running_as_interrupted(session)
    if touched:
        logger.warning(f"[scheduler] 启动时标 interrupted: {touched} 条僵尸 running")

    scheduler = make_scheduler(settings, blocking=True)
    logger.info(
        f"[scheduler] daemon 启动:每日 "
        f"{settings.scheduler.daily_cron_hour:02d}:{settings.scheduler.daily_cron_minute:02d} "
        f"({settings.app.timezone}) 跑 run_today_pending"
    )
    scheduler.start()  # 阻塞
