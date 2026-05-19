"""09:00 cron + 手动 fire(无 polling)(M6)。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime, timedelta

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
    Event,
    Task,
    Video,
)
from wxsp.notify import NotifyEvent, error_type_cn, notify
from wxsp.publisher import AlreadyClaimed, publish
from wxsp.sync import sync_now


@dataclass
class AccountRunStat:
    """单个账号在本轮 run-today 中的统计。run_summary 通知按账号 breakdown 用。

    skipped:本轮被跳过(账号开局就 paused_until 或本轮被 halt_reason 中断)。
    halt_reason:本轮某条 task 触发了不可恢复账号级错误(登录态失效/风控),
      后续 task 不再尝试 —— 这个字段记中文原因,run_summary 文案里直接展示。
    """

    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    halt_reason: str | None = None


# 触发"本轮停掉该账号剩余 task"的错误类型。第一条命中后,后续直接 skip,不再
# 反复跑浏览器 + 不再重复推同一条告警。
_ACCOUNT_HALT_ERRORS: frozenset[str] = frozenset({"cookie_expired", "risk_control"})

# 触发"整轮停掉所有账号剩余 task"的错误类型。这些是全局问题(视频号改版 /
# NAS 掉线),其他账号跑也会挂同样的错;abort 整个 run 是为了 ① 不浪费时间
# ② 不让 publisher 把同一条告警推 N 次。
_GLOBAL_HALT_ERRORS: frozenset[str] = frozenset({"element_not_found", "nas_unreachable"})


@dataclass
class RunSummary:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped_paused: int = 0
    # 按账号统计:key 是 account_id;display_name 在生成通知文案时从 settings 查
    per_account: dict[str, AccountRunStat] = field(default_factory=dict)
    # 整轮被全局错误中断(如视频号改版 / NAS 掉线);set 后剩余 task 全跳过,
    # run_summary 文案头部高亮提示。
    global_halt_reason: str | None = None


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


_BACKLOG_COOLDOWN = timedelta(hours=24)

# "查冷却 + 写 Event" 不是 SQL 原子操作;同进程多线程(09:00 cron 与 Web UI
# "立即跑今天" 撞车)会在并发缝隙里都没查到 recent → 双推。用进程级 Lock 串行化。
# 跨进程并发(多 daemon)不在本工具部署范围内(CLAUDE.md 单 worker 串行)。
_BACKLOG_NOTIFY_LOCK = threading.Lock()


def maybe_warn_backlog(settings: Settings, *, session: Session) -> int:
    """积压超阈值 → 推一条 backlog_high 通知(企微 + Event 表)。返回当前积压数。

    阈值 = `monitoring.backlog_warn_threshold`(默认 20)。

    24h 冷却:查 Event 表最近 24h 有没有 backlog_high,有就跳(同时不写 Event)。
    daemon 反复重启 / 09:00 cron 多次触发都不会刷屏。运营修完积压自然进入下一轮。

    "查 + 写" 用进程级 Lock + Lock 内 commit 实现原子化:Lock 保证同进程串行,
    Lock 内 commit 保证第二个进入者能看到第一个写的 Event 行(SQLite 跨连接看不到
    未 commit 的写)。生产唯一调用方是 daemon 启动 1 次 + run_today_pending 中调
    用,本来就在 session_scope 里、调用前没未提交写,提早 commit 无副作用。
    """
    backlog = count_backlog(session)
    if backlog <= settings.monitoring.backlog_warn_threshold:
        return backlog

    with _BACKLOG_NOTIFY_LOCK:
        # 冷却检查:最近一条 backlog_high Event 不到 24h 就不推
        cutoff = datetime.now() - _BACKLOG_COOLDOWN
        recent = session.exec(
            select(Event).where(Event.type == "backlog_high").where(Event.ts > cutoff).limit(1)
        ).first()
        if recent is not None:
            logger.info(
                f"[scheduler] backlog_high 在冷却期内(最近一条 {recent.ts.isoformat()}),跳过推送"
            )
            return backlog

        notify(
            NotifyEvent(
                type="backlog_high",
                level="warn",
                title=f"历史积压 {backlog} 条",
                content=(
                    f"今天之前还有 {backlog} 条任务没跑完(待发布或已中断),"
                    f"超过阈值 {settings.monitoring.backlog_warn_threshold}。"
                    "建议到管理后台的「历史积压」视图里逐条处理。"
                ),
                context={"积压条数": backlog},
            ),
            session=session,
            settings=settings,
        )
        # Lock 内 commit:让第二个线程进入 Lock 时能查到这条 Event(否则
        # 跨 session 的 SELECT 看不到未 commit 的 INSERT,Lock 形同虚设)
        session.commit()
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


def _emit_run_summary(
    settings: Settings, *, summary: RunSummary, failed_task_ids: list[int]
) -> None:
    """跑完后推一条 run_summary:今日累计 + 按账号 breakdown(成功/失败/暂停 + 失败明细)。

    `attempted == 0 and skipped_paused == 0` 时直接返回(没动静不刷屏)。
    """
    if summary.attempted == 0 and summary.skipped_paused == 0:
        return

    engine = get_engine()
    # 收集每个失败 task 的可读信息,按 account_id 分组挂到 breakdown 行下面
    failures_by_account: dict[str, list[str]] = {}
    if failed_task_ids:
        with Session(engine) as session:
            for tid in failed_task_ids:
                task = session.get(Task, tid)
                if task is None:
                    continue
                video = session.get(Video, task.video_id)
                title = (video.title if video is not None else "?")[:20]
                etype_cn = error_type_cn(task.last_error_type)
                # last_error_msg 可能为空字符串或 None;splitlines("") = [] 别炸
                msg_lines = (task.last_error_msg or "").splitlines()
                msg = msg_lines[0][:60] if msg_lines else ""
                line = f"{etype_cn}:{title}(任务编号 {tid})"
                if msg:
                    line += f" —— {msg}"
                failures_by_account.setdefault(task.account_id, []).append(line)

    lines: list[str] = []
    if summary.global_halt_reason is not None:
        lines.append(
            f"⚠ **整轮中止** —— 原因:{summary.global_halt_reason}。"
            f"后续 {summary.skipped_paused} 条未尝试,请先排查后重试。"
        )
        lines.append("")
    lines.append(
        f"今日累计:成功 {summary.succeeded} 条 / 失败 {summary.failed} 条 / "
        f"暂停跳过 {summary.skipped_paused} 条(共尝试 {summary.attempted} 条)"
    )
    if summary.per_account:
        lines.append("")
        lines.append("**按账号**:")
        for aid, stat in summary.per_account.items():
            display = settings.accounts[aid].display_name if aid in settings.accounts else aid
            bits: list[str] = []
            if stat.succeeded:
                bits.append(f"成功 {stat.succeeded}")
            if stat.failed:
                bits.append(f"失败 {stat.failed}")
            if stat.skipped:
                if stat.halt_reason:
                    bits.append(f"后续 {stat.skipped} 条跳过(原因:{stat.halt_reason})")
                else:
                    bits.append(f"暂停跳过 {stat.skipped}")
            summary_bits = " / ".join(bits) if bits else "无"
            lines.append(f"- **{display}**:{summary_bits}")
            for failure_line in failures_by_account.get(aid, []):
                lines.append(f"    - {failure_line}")

    level = "warn" if summary.failed > 0 else "info"
    title = f"今日发布汇总:成功 {summary.succeeded} 条 / 失败 {summary.failed} 条"
    with session_scope(engine) as session:
        notify(
            NotifyEvent(
                type="run_summary",
                level=level,
                title=title,
                content="\n".join(lines),
                context={
                    "尝试": summary.attempted,
                    "成功": summary.succeeded,
                    "失败": summary.failed,
                    "暂停跳过": summary.skipped_paused,
                },
            ),
            session=session,
            settings=settings,
        )


def run_today_pending(settings: Settings, *, do_sync: bool = True) -> RunSummary:
    """worker 入口:① sync_now()(可选)② queue_today() ③ 串行跑(跳过 paused 账号)。

    do_sync=False 时假设调用方(如 Web UI 路由)已经做过 sync_now 校验过结果。
    do_sync=True 时(09:00 cron 默认路径)sync_now 失败只 warn 不打断,
    保证已入库的还能继续跑。

    任何一条 task 失败 / 抛 AlreadyClaimed 都不阻断后面的;细节走 publish 自己的回写。
    跑完后推一条 run_summary 汇总通知(attempted=0 时静默)。
    """
    if do_sync:
        try:
            sync_now(settings)
        except Exception as exc:
            logger.warning(f"[scheduler] sync_now 失败,继续跑已入库的: {exc}")

    summary = RunSummary()
    failed_task_ids: list[int] = []
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

    # 本轮中触发账号级不可恢复错误(登录态失效 / 风控)的账号 → 后续 task 全跳过。
    # 这么做有两个好处:① 不浪费时间反复开浏览器跑同一个会失败的账号;
    # ② publisher 的 notify 只会推第一条,避免企微群里 20 条一样的"登录态失效"刷屏。
    halted_accounts: dict[str, str] = {}

    for task_id, account_id in plan:
        # 即使账号被暂停 / 全局 halt 也建条 per_account 槽,文案里好展示"被跳过"
        acc_stat = summary.per_account.setdefault(account_id, AccountRunStat())

        # 整轮 abort:全局错误(改版/NAS)出现后,后面所有账号 task 全 skip
        if summary.global_halt_reason is not None:
            summary.skipped_paused += 1
            acc_stat.skipped += 1
            if acc_stat.halt_reason is None:
                acc_stat.halt_reason = summary.global_halt_reason
            logger.info(f"[scheduler] 跳过 task={task_id}:整轮已中止({summary.global_halt_reason})")
            continue

        if account_id in paused_accounts or account_id in halted_accounts:
            summary.skipped_paused += 1
            acc_stat.skipped += 1
            reason = halted_accounts.get(account_id) or "账号暂停中"
            logger.info(f"[scheduler] 跳过 task={task_id}:账号 {account_id} {reason}")
            continue
        summary.attempted += 1
        try:
            result = publish(task_id, dry_run=False, settings=settings)
        except AlreadyClaimed as exc:
            summary.failed += 1
            acc_stat.failed += 1
            failed_task_ids.append(task_id)
            logger.warning(f"[scheduler] task={task_id} 已被认领: {exc}")
            continue
        except Exception as exc:
            summary.failed += 1
            acc_stat.failed += 1
            failed_task_ids.append(task_id)
            logger.exception(f"[scheduler] task={task_id} 跑挂了: {exc}")
            continue
        if result.ok:
            summary.succeeded += 1
            acc_stat.succeeded += 1
        else:
            summary.failed += 1
            acc_stat.failed += 1
            failed_task_ids.append(task_id)
            reason_cn = error_type_cn(result.error_type)
            # 全局错误(平台改版/NAS 掉线)→ 立刻中止整轮,通知就 publisher 第一条
            if result.error_type in _GLOBAL_HALT_ERRORS:
                summary.global_halt_reason = reason_cn
                logger.warning(f"[scheduler] 整轮中止:遇到 {reason_cn},剩余 task 全跳过")
            # 账号级错误 → 仅停该账号剩余 task
            elif result.error_type in _ACCOUNT_HALT_ERRORS:
                halted_accounts[account_id] = reason_cn
                if acc_stat.halt_reason is None:
                    acc_stat.halt_reason = reason_cn
                logger.warning(
                    f"[scheduler] 账号 {account_id} 触发 {reason_cn},本轮剩余 task 全跳过"
                )

    try:
        _emit_run_summary(settings, summary=summary, failed_task_ids=failed_task_ids)
    except Exception as exc:
        logger.warning(f"[scheduler] 推送 run_summary 失败,忽略: {exc}")

    return summary


def make_scheduler(settings: Settings, *, blocking: bool = False) -> BaseScheduler:
    """构造 APScheduler;`scheduler.enabled=true` 时注册"每日 cron job"。

    `enabled=false`:返回空 scheduler(无 job),daemon 仍可启动,手动入口照旧。
    blocking=True 用于 daemon 进程(主线程 .start() 阻塞);
    blocking=False(默认)用于测试 / 后台模式。**调用方负责 shutdown**。
    """
    scheduler: BaseScheduler
    if blocking:
        scheduler = BlockingScheduler(timezone=settings.app.timezone)
    else:
        scheduler = BackgroundScheduler(timezone=settings.app.timezone)
    if not settings.scheduler.enabled:
        logger.info("[scheduler] scheduler.enabled=false,跳过注册每日 cron(手动入口仍可用)")
        return scheduler
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
    if settings.scheduler.enabled:
        logger.info(
            f"[scheduler] daemon 启动:每日 "
            f"{settings.scheduler.daily_cron_hour:02d}:{settings.scheduler.daily_cron_minute:02d} "
            f"({settings.app.timezone}) 跑 run_today_pending"
        )
    else:
        logger.info("[scheduler] daemon 启动:定时任务已关闭,只服务手动入口")
    scheduler.start()  # 阻塞
