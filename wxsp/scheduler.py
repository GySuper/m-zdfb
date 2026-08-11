"""09:00 cron + 手动 fire(无 polling)(M6)。"""

from __future__ import annotations

import re
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
                title=f"历史积压 {backlog} 条",
                content=(
                    f"今天之前还有 {backlog} 条任务没跑完(待发布或已中断),"
                    f"超过阈值 {threshold}。"
                    "建议到管理后台的「历史积压」视图里逐条处理。"
                ),
                context={"积压条数": backlog},
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


# 失败消息里 publisher 拼的 "step=xxx: " 前缀对运营无意义,文案展示前剥掉。
_STEP_PREFIX_RE = re.compile(r"^step=\S+:\s*")


def _short_failure_reason(task: Task) -> str:
    """把 task.last_error_type + last_error_msg 压成一行运营能直接看懂的中文。

    格式:`错误类型 —— 简短描述`(描述为空时只显示错误类型)。
    msg 取首行 + 截到 50 字符,避免一条 publisher 长 traceback 撑爆通知。
    """
    etype_cn = error_type_cn(task.last_error_type)
    raw = (task.last_error_msg or "").strip()
    if not raw:
        return etype_cn
    first_line = raw.splitlines()[0]
    cleaned = _STEP_PREFIX_RE.sub("", first_line).strip()
    if not cleaned:
        return etype_cn
    if len(cleaned) > 50:
        cleaned = cleaned[:50] + "…"
    return f"{etype_cn} —— {cleaned}"


def _emit_run_summary(
    settings: Settings,
    *,
    summary: RunSummary,
    failed_task_ids: list[int],
    platform: str = "tencent_channel",
) -> None:
    """跑完后推一条 run_summary。

    `attempted == 0 and skipped_paused == 0` 时直接返回(没动静不刷屏)。

    文案示例:

        时间:2026-05-19 19:27:16
        汇总:4 个账号,成功 11,失败 2

        各账号明细:
        - [美食号] 新增 6,成功 5,失败 1
          失败原因:上传失败 —— 页面提示上传失败
        - [健身号] 新增 0,成功 0,失败 0,跳过 5
          跳过原因:登录态失效
        - [旅游号] 新增 4,成功 4,失败 0
        - [搞笑号] 新增 3,成功 2,失败 1
          失败原因:风控触发 —— 操作过于频繁

    整轮 halt 时顶部加一行 `⚠ 本轮中止 —— 原因:xxx`,标题前缀 `⚠`。
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
                failures_by_account.setdefault(task.account_id, []).append(
                    _short_failure_reason(task)
                )

    lines: list[str] = []
    if summary.global_halt_reason is not None:
        lines.append(f"⚠ 本轮中止 —— 原因:{summary.global_halt_reason}")
        lines.append("")
    lines.append(f"时间:{datetime.now():%Y-%m-%d %H:%M:%S}")
    skip_suffix = f",跳过 {summary.skipped_paused}" if summary.skipped_paused else ""
    lines.append(
        f"汇总:{len(summary.per_account)} 个账号,"
        f"成功 {summary.succeeded},失败 {summary.failed}{skip_suffix}"
    )
    if summary.per_account:
        lines.append("")
        lines.append("各账号明细:")
        for aid, stat in summary.per_account.items():
            display = settings.accounts[aid].display_name if aid in settings.accounts else aid
            # "新增" = 该账号实际启动 publish 的条数(=成功+失败),paused/halt 跳过的不算"新增"。
            new_count = stat.succeeded + stat.failed
            bits = [
                f"新增 {new_count}",
                f"成功 {stat.succeeded}",
                f"失败 {stat.failed}",
            ]
            if stat.skipped:
                bits.append(f"跳过 {stat.skipped}")
            lines.append(f"- [{display}] {','.join(bits)}")
            for failure_line in failures_by_account.get(aid, []):
                lines.append(f"  失败原因:{failure_line}")
            if stat.skipped and stat.halt_reason:
                lines.append(f"  跳过原因:{stat.halt_reason}")

    # halt_reason(cookie_expired / risk_control)需要运营干预,即使 failed=0 也用 ⚠;
    # paused_only(运营手动暂停)halt_reason 为 None,保持 ✓ 不打扰。
    has_halt = any(stat.halt_reason for stat in summary.per_account.values())
    needs_attention = summary.failed > 0 or summary.global_halt_reason is not None or has_halt
    level = "warn" if needs_attention else "info"
    title_prefix = "⚠" if needs_attention else "✓"
    from wxsp.notify import _platform_tag

    plat_tag = _platform_tag(platform)
    title = f"{title_prefix} {plat_tag}发布完成"
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
    if do_sync:
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
            plan.append((tid, t.account_id))
        # 一次性拿账号快照:paused_until + cookie_status,避免发布时才发现 cookie 没登录
        paused_accounts: set[str] = set()
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
                paused_accounts.add(acc_id)
                continue  # 暂停优先级高,不再判 cookie
            # cookie_status != ok → 跳过本账号所有 task。expired = login 时检测到未登录;
            # unknown = login 时浏览器异常(用户关窗 / 启动崩了),user_data_dir 里没拿到 cookie。
            # 这俩状态下开 publisher 必然 verify_logged_in 失败,且会写一条误导性"登录态失效"
            # 告警(实际从来就没登录过)。预检在这,task 直接 skip,提醒运营先扫码。
            if acc.cookie_status == "expired":
                unlogged_accounts[acc_id] = "登录态已失效,请重新扫码"
            elif acc.cookie_status == "unknown":
                unlogged_accounts[acc_id] = "未完成扫码登录(状态 unknown),请到账号页扫码"

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

        if (
            account_id in paused_accounts
            or account_id in disabled_accounts
            or account_id in halted_accounts
            or account_id in unlogged_accounts
        ):
            summary.skipped_paused += 1
            acc_stat.skipped += 1
            reason = (
                unlogged_accounts.get(account_id)
                or halted_accounts.get(account_id)
                or ("账号已禁用或配置已删除" if account_id in disabled_accounts else "账号暂停中")
            )
            # 未登录的也写进 halt_reason,让 run_summary 显示"跳过(原因:请扫码)"
            # 而不是只写"暂停跳过 N"(运营一头雾水)
            if (
                account_id in unlogged_accounts or account_id in disabled_accounts
            ) and acc_stat.halt_reason is None:
                acc_stat.halt_reason = reason
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
            reason_cn = error_type_cn(result.error_type)
            halted_accounts[account_id] = reason_cn
            if acc_stat.halt_reason is None:
                acc_stat.halt_reason = reason_cn
            logger.warning(
                f"[scheduler] 账号 {account_id} task={task_id} 触发 {reason_cn};"
                "task 回退 pending 等扫码后重跑,本轮剩余 task 全跳过"
            )
        else:
            summary.failed += 1
            acc_stat.failed += 1
            failed_task_ids.append(task_id)
            reason_cn = error_type_cn(result.error_type)
            # 全局错误(平台改版/NAS 掉线)→ 立刻中止整轮,通知就 publisher 第一条
            if result.error_type in _GLOBAL_HALT_ERRORS:
                summary.global_halt_reason = reason_cn
                logger.warning(f"[scheduler] 整轮中止:遇到 {reason_cn},剩余 task 全跳过")
            # 账号级错误(risk_control 等)→ 仅停该账号剩余 task
            elif result.error_type in _ACCOUNT_HALT_ERRORS:
                halted_accounts[account_id] = reason_cn
                if acc_stat.halt_reason is None:
                    acc_stat.halt_reason = reason_cn
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
