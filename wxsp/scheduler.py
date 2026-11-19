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

from wxsp import platform_meta
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
from wxsp.notify import NotifyEvent, describe_failure, notify
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
    halt_reason: str | None = None  # 本轮跳过该账号的原因(大白话,汇总里直接展示)
    halt_action: str | None = None  # 对应的处理办法
    halt_actionable: bool = False  # 需要运营处理(登录失效 / 风控);人为暂停、停用不算
    global_skipped: int = 0  # 因整轮中止被跳过的条数(汇总在顶部统一说明,不进账号明细)


# 触发"本轮停掉该账号剩余 task"的错误类型。第一条命中后,后续直接 skip,不再
# 反复跑浏览器 + 不再重复推同一条告警。
#
# 注意:`cookie_expired` 不在此 set 里,它走独立的"软失败"分支(下方处理逻辑):
#   - publisher 已把 task 回退 pending(没真给它机会),scheduler 计 skipped 不计 failed
#   - halted_accounts 仍然标记,让后续 task 跳过
# 这样运营扫码续命后,这账号的所有 pending task 在下轮 queue_today 自动重跑,不用手动重试。
_ACCOUNT_HALT_ERRORS: frozenset[str] = frozenset({"risk_control"})

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
    global_halt_action: str | None = None
    global_skipped: int = 0


def queue_today(
    session: Session, *, today: _date | None = None, platform: str | None = None
) -> list[int]:
    """返回今天的 pending task id 列表,按 publish_at 升序。

    调用方负责开 session;本函数只读。
    `platform`:可选过滤,只返回该平台的任务(配合 per-platform cron)。
    """
    if today is None:
        today = _date.today()
    stmt = select(Task).where(Task.execute_date == today).where(Task.status == TASK_STATUS_PENDING)
    if platform is not None:
        stmt = stmt.where(Task.platform == platform)
    stmt = stmt.order_by(col(Task.publish_at).asc())
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


def maybe_warn_backlog(
    settings: Settings, *, session: Session, platform: str = "tencent_channel"
) -> int:
    """积压超阈值 → 推一条 backlog_high 通知(企微 + Event 表)。返回当前积压数。

    阈值 = `monitoring.backlog_warn_threshold`(默认 20),按平台独立配置。

    24h 冷却:查 Event 表最近 24h 有没有 backlog_high,有就跳(同时不写 Event)。
    daemon 反复重启 / 09:00 cron 多次触发都不会刷屏。运营修完积压自然进入下一轮。

    "查 + 写" 用进程级 Lock + Lock 内 commit 实现原子化:Lock 保证同进程串行,
    Lock 内 commit 保证第二个进入者能看到第一个写的 Event 行(SQLite 跨连接看不到
    未 commit 的写)。生产唯一调用方是 daemon 启动 1 次 + run_today_pending 中调
    用,本来就在 session_scope 里、调用前没未提交写,提早 commit 无副作用。
    """
    monitoring_cfg = settings.monitoring
    threshold = monitoring_cfg.backlog_warn_threshold if monitoring_cfg is not None else 20
    backlog = count_backlog(session)
    if backlog <= threshold:
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
                title=f"有 {backlog} 条任务积压",
                status="提醒",
                content="",
                context={
                    "说明": "都是今天之前没发出去的任务(待发布或中断)",
                    "处理": "到「任务」页的「历史积压」里重新入队或取消",
                },
                platform=platform,
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


_LOGIN_ACTION = "到「账号」页扫码登录,登录后自动补发"
_FIELD_GAP = "\u3000"


def _video_name(video: Video | None, task_id: int) -> str:
    """汇总里怎么称呼这条视频:标题 → 描述开头(拼多多 / 快手无标题)→ 任务号。"""
    text = ((video.title or video.description or "") if video else "").strip().splitlines()
    name = text[0] if text else ""
    if not name:
        return f"任务 #{task_id}"
    return name if len(name) <= 24 else name[:24] + "…"


def _run_summary_body(
    settings: Settings, summary: RunSummary, failed_task_ids: list[int]
) -> tuple[str, int]:
    """汇总正文(企微 markdown)+ 需要处理的条数。

    结构:数字一行 → 整轮中止说明 → 需要处理(每条失败:视频 / 账号 + 定时 / 原因 / 处理;
    每个被跳过的账号一块)→ 跳过(人为暂停 / 停用,只说明)→ 各账号(多账号时)。
    """
    names = {aid: cfg.display_name for aid, cfg in settings.accounts.items()}
    lines = [
        f'成功 <font color="info">{summary.succeeded}</font> · 失败 {summary.failed}'
        f" · 跳过 {summary.skipped_paused}"
    ]
    if summary.global_halt_reason is not None:
        lines += [
            "",
            f"> 原因{_FIELD_GAP}{summary.global_halt_reason}",
            f"> 影响{_FIELD_GAP}剩下 {summary.global_skipped} 条任务已暂停,没有发出",
            f"> 处理{_FIELD_GAP}{summary.global_halt_action}",
        ]

    blocks: list[list[str]] = []
    failures: list[tuple[datetime, list[str]]] = []
    if failed_task_ids:
        with Session(get_engine()) as session:
            for tid in failed_task_ids:
                task = session.get(Task, tid)
                if task is None:
                    continue
                reason, action = describe_failure(task.last_error_type, task.last_error_msg)
                account = names.get(task.account_id, task.account_id)
                failures.append(
                    (
                        task.publish_at,
                        [
                            f"{_video_name(session.get(Video, task.video_id), tid)}",
                            f"账号{_FIELD_GAP}{account} · 定时 {task.publish_at:%m-%d %H:%M}"
                            f" · 任务 #{tid}",
                            f"原因{_FIELD_GAP}{reason}",
                            f"处理{_FIELD_GAP}{action}",
                        ],
                    )
                )
    blocks += [b for _, b in sorted(failures, key=lambda x: x[0])]
    notes: list[str] = []
    needs = len(failures)
    for aid, stat in summary.per_account.items():
        skipped_here = stat.skipped - stat.global_skipped
        if not skipped_here or stat.halt_reason is None:
            continue
        name = names.get(aid, aid)
        if stat.halt_actionable:
            needs += skipped_here
            blocks.append(
                [
                    f"{name}:{skipped_here} 条没跑",
                    f"原因{_FIELD_GAP}{stat.halt_reason}",
                    f"处理{_FIELD_GAP}{stat.halt_action}",
                ]
            )
        else:
            notes.append(f"{name} {skipped_here} 条:{stat.halt_reason},{stat.halt_action}")

    if blocks:
        lines += ["", "**需要处理**"]
        for i, block in enumerate(blocks, 1):
            lines.append(f"> **{i}. {block[0]}**")
            lines += [f"> {line}" for line in block[1:]]
            lines.append("")
        lines.pop()
    if notes:
        lines += ["", "**跳过**", *notes]
    if len(summary.per_account) > 1:
        lines += ["", "**各账号**"]
        for aid, stat in summary.per_account.items():
            bits = [f"成功 {stat.succeeded}"]
            if stat.failed:
                bits.append(f"失败 {stat.failed}")
            if stat.skipped:
                bits.append(f"跳过 {stat.skipped}")
            lines.append(f"{names.get(aid, aid)} {' · '.join(bits)}")
    return "\n".join(lines), needs


def _emit_run_summary(
    settings: Settings,
    *,
    summary: RunSummary,
    failed_task_ids: list[int],
    platform: str = "tencent_channel",
) -> None:
    """每轮结束只发这一条汇总(单条失败不再单独推送,避免多个失败刷屏)。"""
    if summary.attempted == 0 and summary.skipped_paused == 0:
        return
    body, needs = _run_summary_body(settings, summary, failed_task_ids)
    halted = summary.global_halt_reason is not None
    if halted:
        status = "需要处理"
    elif needs:
        status = f"{needs} 条需要处理"
    elif summary.skipped_paused:
        status = "完成"
    else:
        status = "全部成功"
    with session_scope(get_engine()) as session:
        notify(
            NotifyEvent(
                type="run_summary",
                level="warn" if halted or needs else "info",
                title="本轮发布中止" if halted else "本轮发布完成",
                status=status,
                content=body,
                platform=platform,
            ),
            session=session,
            settings=settings,
        )


def run_today_pending(
    settings: Settings,
    *,
    do_sync: bool = True,
    platform: str | None = None,
    task_ids: list[int] | None = None,
) -> RunSummary:
    """worker 入口:① sync_now()(可选)② queue_today() ③ 串行跑(跳过 paused 账号)。

    do_sync=False 时假设调用方(如 Web UI 路由)已经做过 sync_now 校验过结果。
    do_sync=True 时(09:00 cron 默认路径)sync_now 失败只 warn 不打断,
    保证已入库的还能继续跑。

    platform=None 时跑所有平台(兼容旧版命令行 `wxsp run --today`);
    platform="tencent_channel" 时只跑视频号任务(per-platform cron 路径)。

    task_ids 给定时跳过 queue_today 的"今天所有 pending"扫描,只跑这批指定 task
    (Web UI "一键重试今天失败" 用:调用方已把 failed 重置回 pending 并按 publish_at 排序),
    串行/账号预检/halt/run_summary 全程复用,不波及同日其它 pending。

    任何一条 task 失败 / 抛 AlreadyClaimed 都不阻断后面的;细节走 publish 自己的回写。
    跑完后推一条 run_summary 汇总通知(attempted=0 时静默)。
    """
    if platform is not None and not platform_meta.is_available(platform):
        logger.info(
            f"[scheduler] [{platform}] 平台已锁定({platform_meta.LOCKED_HINT}),不同步不发布"
        )
        return RunSummary()
    if do_sync and platform_meta.is_available(platform or "tencent_channel"):
        try:
            sync_now(settings, platform=platform or "tencent_channel")
        except Exception as exc:
            logger.warning(f"[scheduler] sync_now 失败,继续跑已入库的: {exc}")

    summary = RunSummary()
    failed_task_ids: list[int] = []
    engine = get_engine()
    init_db(engine)
    now = datetime.now()
    effective_platform = platform or "tencent_channel"

    # 一次性把 (task_id, account_id) 读出来,后面循环里不再持 session 跑长 publish
    with Session(engine) as session:
        ids = task_ids if task_ids is not None else queue_today(session, platform=platform)
        if not ids:
            return summary
        plan: list[tuple[int, str]] = []
        for tid in ids:
            t = session.get(Task, tid)
            if t is None:
                continue
            # Per-platform:skip tasks not matching the target platform
            if platform is not None and t.platform != platform:
                continue
            if not platform_meta.is_available(t.platform):  # platform=None 跑全部时也不碰锁定平台
                continue
            plan.append((tid, t.account_id))
        # 一次性拿账号快照:paused_until + cookie_status,避免发布时才发现 cookie 没登录
        paused_accounts: dict[str, datetime] = {}  # acc_id → 暂停到期时间
        disabled_accounts: set[str] = set()
        unlogged_accounts: dict[str, str] = {}  # acc_id → 中文跳过原因
        for acc_id in {acc for _, acc in plan}:
            acc = session.get(Account, acc_id)
            account_cfg = settings.accounts.get(acc_id)
            if acc is None or not acc.is_active or account_cfg is None or not account_cfg.enabled:
                disabled_accounts.add(acc_id)
                continue
            # Per-platform:skip accounts not belonging to this platform
            if platform is not None and acc.platform != platform:
                # This shouldn't happen normally (task platform == account platform),
                # but handle defensively.
                continue
            if acc.paused_until is not None and acc.paused_until > now:
                paused_accounts[acc_id] = acc.paused_until
                continue  # 暂停优先级高,不再判 cookie
            # cookie_status != ok → 跳过本账号所有 task。expired = login 时检测到未登录;
            # unknown = login 时浏览器异常(用户关窗 / 启动崩了),user_data_dir 里没拿到 cookie。
            # 这俩状态下开 publisher 必然 verify_logged_in 失败,且会写一条误导性"登录态失效"
            # 告警(实际从来就没登录过)。预检在这,task 直接 skip,提醒运营先扫码。
            if account_cfg.password_login:
                pass  # 开了账密登录:登录失效也照跑,插件会自动用账号密码登录
            elif acc.cookie_status == "expired":
                unlogged_accounts[acc_id] = "账号登录失效"
            elif acc.cookie_status == "unknown":
                unlogged_accounts[acc_id] = "账号还没扫码登录"

    # 本轮中触发账号级不可恢复错误(登录态失效 / 风控)的账号 → 后续 task 全跳过。
    # 这么做有两个好处:① 不浪费时间反复开浏览器跑同一个会失败的账号;
    # ② publisher 的 notify 只会推第一条,避免企微群里 20 条一样的"登录态失效"刷屏。
    halted_accounts: dict[str, tuple[str, str]] = {}  # acc_id → (原因, 处理办法)

    for task_id, account_id in plan:
        # 即使账号被暂停 / 全局 halt 也建条 per_account 槽,文案里好展示"被跳过"
        acc_stat = summary.per_account.setdefault(account_id, AccountRunStat())

        # 整轮 abort:全局错误(改版/NAS)出现后,后面所有账号 task 全 skip
        if summary.global_halt_reason is not None:
            summary.skipped_paused += 1
            summary.global_skipped += 1
            acc_stat.skipped += 1
            acc_stat.global_skipped += 1
            logger.info(f"[scheduler] 跳过 task={task_id}:整轮已中止({summary.global_halt_reason})")
            continue

        if (
            account_id in paused_accounts
            or account_id in disabled_accounts
            or account_id in halted_accounts
            or account_id in unlogged_accounts
        ):
            summary.skipped_paused += 1
            acc_stat.skipped += 1
            actionable = account_id in unlogged_accounts or account_id in halted_accounts
            if account_id in unlogged_accounts:
                reason, action = unlogged_accounts[account_id], _LOGIN_ACTION
            elif account_id in halted_accounts:
                reason, action = halted_accounts[account_id]
            elif account_id in disabled_accounts:
                reason, action = "账号已停用或已从配置删除", "要发的话到「配置」页重新启用"
            else:
                until = paused_accounts[account_id]
                reason = f"账号暂停中(到 {until:%m-%d %H:%M} 恢复)"
                action = "到期自动恢复;要提前发,到「账号」页点「恢复」"
            # 跳过原因 + 处理办法写进账号统计,汇总里给运营看
            if acc_stat.halt_reason is None:
                acc_stat.halt_reason, acc_stat.halt_action = reason, action
                acc_stat.halt_actionable = actionable
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
        elif result.error_type == "cookie_expired":
            # 软失败:publisher 已把 task 回退成 pending(本轮没真给机会),所以这里
            # 不计 failed 计 skipped,但仍然 halt 该账号(后续 task 不再开浏览器,
            # 也避免反复推 cookie_expired 告警)。运营扫码后下轮自动重跑。
            summary.skipped_paused += 1
            acc_stat.skipped += 1
            reason_cn, action = describe_failure(result.error_type, result.error_msg)
            halted_accounts[account_id] = (reason_cn, action)
            if acc_stat.halt_reason is None:
                acc_stat.halt_reason, acc_stat.halt_action = reason_cn, action
                acc_stat.halt_actionable = True
            logger.warning(
                f"[scheduler] 账号 {account_id} task={task_id} 触发 {reason_cn};"
                "task 回退 pending 等扫码后重跑,本轮剩余 task 全跳过"
            )
        else:
            summary.failed += 1
            acc_stat.failed += 1
            failed_task_ids.append(task_id)
            reason_cn, action = describe_failure(result.error_type, result.error_msg)
            # 全局错误(平台改版/NAS 掉线)→ 立刻中止整轮,剩余 task 全跳过,汇总顶部说明
            if result.error_type in _GLOBAL_HALT_ERRORS:
                summary.global_halt_reason, summary.global_halt_action = reason_cn, action
                logger.warning(f"[scheduler] 整轮中止:遇到 {reason_cn},剩余 task 全跳过")
            # 账号级错误(risk_control 等)→ 仅停该账号剩余 task
            elif result.error_type in _ACCOUNT_HALT_ERRORS:
                halted_accounts[account_id] = (reason_cn, action)
                logger.warning(
                    f"[scheduler] 账号 {account_id} 触发 {reason_cn},本轮剩余 task 全跳过"
                )

    try:
        _emit_run_summary(
            settings, summary=summary, failed_task_ids=failed_task_ids, platform=effective_platform
        )
    except Exception as exc:
        logger.warning(f"[scheduler] 推送 run_summary 失败,忽略: {exc}")

    return summary


def _daily_cron(platform: str, settings: Settings) -> None:
    """Per-platform daily cron:tick → sync → run.

    隔离在独立函数里方便 cron 注册;内部任何异常只 log 不抛(apscheduler 会吞,
    但显式 catch 方便在日志里记账)。
    """
    logger.info(f"[scheduler] [{platform}] daily cron triggered")
    try:
        from wxsp.archive import install_file_sink

        monitoring_cfg = settings.monitoring
        retention = monitoring_cfg.log_retention_days if monitoring_cfg is not None else 30
        install_file_sink(
            logs_dir=settings.app.logs_dir,
            retention_days=retention,
        )
    except Exception as exc:
        logger.warning(f"[scheduler] 装日志 sink 失败,继续走 stderr: {exc}")
    try:
        sync_now(settings, platform=platform)
    except Exception as exc:
        logger.exception(f"[scheduler] [{platform}] sync 失败: {exc}")
    run_today_pending(settings, platform=platform)


def make_scheduler(settings: Settings, *, blocking: bool = False) -> BaseScheduler:
    """构造 APScheduler;per-platform cron job 由 start_daemon() 注册。

    `blocking=True` 用于 daemon 进程(主线程 .start() 阻塞);
    `blocking=False`(默认)用于测试 / 后台模式。**调用方负责 shutdown**。
    """
    scheduler: BaseScheduler
    if blocking:
        scheduler = BlockingScheduler(timezone=settings.app.timezone)
    else:
        scheduler = BackgroundScheduler(timezone=settings.app.timezone)
    if not settings.scheduler.enabled:
        logger.info("[scheduler] scheduler.enabled=false,跳过注册每日 cron(手动入口仍可用)")
    return scheduler


def _register_daily_crons(scheduler: BaseScheduler, settings: Settings) -> list[str]:
    """按平台注册每日 cron(各平台用自己的 config),返回实际注册的平台 key 列表。

    抽成独立函数便于测试:start_daemon 末尾是阻塞的 scheduler.start(),不好直接测。
    """
    from wxsp.config import ALL_PLATFORMS, load_settings
    from wxsp.config import get_config_path as _get_cfg_path

    platform_keys = [p for p in ALL_PLATFORMS if _get_cfg_path(p).exists()]
    registered: list[str] = []
    for platform_key in platform_keys:
        if not platform_meta.is_available(platform_key):
            logger.info(
                f"[scheduler] 平台 [{platform_key}] 已锁定({platform_meta.LOCKED_HINT}),不注册 cron"
            )
            continue
        try:
            plat_settings = load_settings(platform=platform_key)
        except Exception:
            plat_settings = settings
        sched_cfg = plat_settings.scheduler
        if not sched_cfg.enabled:
            logger.info(f"[scheduler] 平台 [{platform_key}] scheduler.enabled=false,跳过注册 cron")
            continue
        scheduler.add_job(
            lambda p=platform_key: _daily_cron(p, load_settings(platform=p)),
            CronTrigger(
                hour=sched_cfg.daily_cron_hour,
                minute=sched_cfg.daily_cron_minute,
                timezone=settings.app.timezone,
            ),
            id=f"daily_{platform_key}",
            name=f"[{platform_key}] daily sync + run",
        )
        logger.info(
            f"[scheduler] 平台 [{platform_key}] cron:每日 "
            f"{sched_cfg.daily_cron_hour:02d}:{sched_cfg.daily_cron_minute:02d} "
            f"({settings.app.timezone}) sync + run_today_pending"
        )
        registered.append(platform_key)
    return registered


def start_daemon(settings: Settings) -> None:
    """daemon 入口:启动时 (1) 标 interrupted (2) 清过期日志/截图 (3) 积压告警判定
    (4) 起 blocking scheduler + 注册 per-platform cron job。

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
    registered = _register_daily_crons(scheduler, settings)
    if not registered:
        logger.info("[scheduler] daemon 启动:没有启用的平台 cron(手动入口仍可用)")

    scheduler.start()  # 阻塞
