"""共享发布编排器:claim → 浏览器 → 平台步骤 → 状态机 → 通知 → 飞书回写。

平台模块只提供 `PlatformSpec`(pre_publish / post_publish 两段回调);
本模块统一处理所有平台无差别的"plumbing":
  - 幂等抢锁 + 加载快照
  - 启浏览器 + 失败时截图/HTML 存档
  - 错误分类(PublisherError / 未分类 / KeyboardInterrupt)
  - finally 状态机:cleanup tmp / task 终态 / 风控暂停 / cookie_status / 通知 / 飞书回写

对齐 `base.PlatformPublisher` 协议文档:"DB writes, notifications, and Feishu
callbacks are handled by the shared orchestrator"。新增平台时只写步骤回调,
不再复制这套状态机(避免历史上 tencent/taobao 各抄一份导致的漂移)。
"""

from __future__ import annotations

import json as _json
import random
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger
from patchright.sync_api import Page
from sqlalchemy.engine import Engine
from sqlmodel import Session

from wxsp.browser import browser_context
from wxsp.config import Settings
from wxsp.db import claim_task, get_engine, init_db, session_scope, transition_task
from wxsp.errors import PublisherError, classify
from wxsp.feishu import FeishuApiError, make_client, writeback_row
from wxsp.human_input import block_user_input, unblock_user_input
from wxsp.models import Account, Task, Video
from wxsp.nas import cleanup_tmp, stage_to_tmp
from wxsp.notify import NotifyEvent, error_type_cn, notify, step_cn
from wxsp.platforms.base import (
    AlreadyClaimed,
    PlatformSpec,
    PublishContext,
    PublishResult,
    TaskBundle,
)

# error_type → (notify type, level, 中文标题)。element_not_found 的标题在派发时
# 会用 step + 平台名重写,所以这里它的标题串只是占位(只取 type / level)。
_NOTIFY_BY_ERROR: dict[str, tuple[str, str, str]] = {
    "cookie_expired": ("cookie_expired", "error", "登录态失效,等待扫码"),
    "risk_control": ("risk_control", "error", "风控触发,账号已暂停 24 小时"),
    "element_not_found": ("element_not_found", "warn", "页面元素未找到,可能改版"),
    "nas_unreachable": ("nas_unreachable", "error", "存储不可达"),
}


# ============== 共享步骤辅助(供平台回调与编排器复用) ==============


def screenshot(
    page: Page,
    *,
    task_id: int,
    step: str,
    screenshots_root: Path,
    now: datetime | None = None,
) -> Path:
    """保存截图到 `screenshots_root/{YYYYMM}/{task_id}_{step}.png`,返回路径。

    `now` 注入用于测试,默认 `datetime.now()`。截图自身失败不抛(避免掩盖原始错误)。
    """
    now = now or datetime.now()
    month_dir = screenshots_root / now.strftime("%Y%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    path = month_dir / f"{task_id}_{step}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
    except Exception as exc:
        logger.warning(f"截图失败 task_id={task_id} step={step}: {exc}")
    return path


def random_pause(
    range_seconds: tuple[float, float],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """步骤间随机停顿(模拟人工);`sleep` 注入用于测试。"""
    low, high = range_seconds
    sleep(random.uniform(low, high))


def load_task_bundle(session: Session, task_id: int) -> TaskBundle:
    """在 session 内把 Task/Video/Account 取成 detached-safe 的快照超集。"""
    task = session.get(Task, task_id)
    if task is None:
        raise LookupError(f"Task id={task_id} 不存在")
    video = session.get(Video, task.video_id)
    account = session.get(Account, task.account_id)
    if video is None or account is None:
        raise LookupError(f"Task {task_id} 的 video/account 缺失")
    return TaskBundle(
        task_id=task_id,
        platform=task.platform,
        publish_at=task.publish_at,
        account_id=account.id,
        user_data_dir=Path(account.user_data_dir),
        video_record_id=video.id,
        video_file_path=Path(video.file_path),
        video_cover_path=Path(video.cover_path) if video.cover_path else None,
        title=video.title,
        description=video.description,
        topic=video.topic,
        original_claim=video.original_claim,
        tags_json=video.tags_json or "[]",
        product_ids_json=video.product_ids_json or "[]",
        declaration=getattr(video, "declaration", None),
        ai_optimize=getattr(video, "ai_optimize", False),
    )


def _capture_error_artifacts(page: Page, ctx: PublishContext) -> None:
    """趁 page 还活着:截图 err_{last_step}.png + 落一份同名 .html,失败不外抛。"""
    if not ctx.settings.publisher.screenshot_on_error:
        return
    try:
        shot = screenshot(
            page,
            task_id=ctx.task_id,
            step=f"err_{ctx.last_step}",
            screenshots_root=ctx.screenshots_root,
        )
        ctx.result.screenshots.append(str(shot))
        try:
            html_path = Path(str(shot)).with_suffix(".html")
            html_path.write_text(page.content(), encoding="utf-8")
        except Exception as html_exc:
            logger.warning(f"失败时存 HTML 失败 task_id={ctx.task_id}: {html_exc}")
    except Exception as ss_exc:
        logger.warning(f"失败时截图也失败 task_id={ctx.task_id}: {ss_exc}")


# ============== run_publish() 顶层编排 ==============


def run_publish(
    task_id: int,
    *,
    dry_run: bool,
    settings: Settings,
    spec: PlatformSpec,
) -> PublishResult:
    """跑一条任务的完整发布流程(平台无关骨架)。

    - 入口 `claim_task`(原子幂等锁),拿不到 → AlreadyClaimed。
    - 串行跑 spec.pre_publish;dry_run=True 在 gate 截图返回,不点发布。
    - 否则跑 spec.post_publish(点发布 / 等成功 / 抽取 remote)。
    - 任何 PublisherError / patchright Error → classify + 失败截图 + status=failed。
    - 风控 → 账号暂停 24h;cookie_expired → task 回退 pending + cookie_status=expired。
    - 不管成败,finally 里 cleanup tmp + 写 DB + 通知 + 飞书回写。
    """
    engine = get_engine()
    init_db(engine)
    screenshots_root = settings.app.logs_dir / "screenshots"
    tmp_root = settings.app.data_dir / "tmp"

    # 1. 幂等抢锁(claim_task 自己 commit)
    with Session(engine) as session:
        if not claim_task(session, task_id):
            raise AlreadyClaimed(f"Task {task_id} 不在 pending 或已被占用")

    # 2. 加载快照
    with Session(engine) as session:
        try:
            bundle = load_task_bundle(session, task_id)
        except Exception:
            # claim_task 已把 task 置 running;若加载快照失败(video/account 被并发删、
            # DB 抖动等),这里不释放锁,task 会卡在 running 直到 daemon 重启才回收。
            # 主动回退 pending + 清 lease,让下一轮调度能重新认领。
            session.rollback()
            try:
                transition_task(
                    session,
                    task_id,
                    status="pending",
                    lease_token=None,
                    lease_expires_at=None,
                    started_at=None,
                )
                session.commit()
            except Exception as rel_exc:
                logger.error(f"加载快照失败后释放 claim 也失败 task_id={task_id}: {rel_exc}")
            raise

    result = PublishResult(task_id=task_id, ok=False, dry_run=dry_run)
    ctx = PublishContext(
        task_id=task_id,
        result=result,
        dry_run=dry_run,
        step_pause=settings.publisher.step_pause_seconds,
        screenshots_root=screenshots_root,
        tmp_root=tmp_root,
        settings=settings,
    )

    try:
        if spec.platform_key == "tencent_channel" and settings.publisher.headless:
            ctx.last_step = "browser"
            raise RuntimeError("视频号禁止 headless 模式,请将 publisher.headless 设为 false")

        # [1] stage NAS → tmp
        ctx.last_step = "stage"
        staged = stage_to_tmp(bundle.video_file_path, task_id=task_id, tmp_root=tmp_root)

        # [2] 启浏览器(带账号指纹避免"同设备多账号"风控)
        ctx.last_step = "browser"
        with browser_context(
            bundle.user_data_dir,
            headless=settings.publisher.headless,
            account_id=bundle.account_id,
            platform=spec.platform_key,
        ) as page:
            # 禁用真实键盘/鼠标输入:浏览器已激活,物理操作(上传/填表/发布)开始前
            # 锁定,防人碰键盘鼠标打乱 pyautogui 屏幕坐标操作。锁不影响 pyautogui
            # (SendInput 注入不被 BlockInput 拦),任何路径都在 finally 解锁。
            block_user_input()
            try:
                spec.pre_publish(page, bundle, staged, ctx)

                # ★ DRY_RUN GATE
                if dry_run:
                    ctx.last_step = "dryrun_gate"
                    shot = screenshot(
                        page,
                        task_id=task_id,
                        step="dryrun_gate",
                        screenshots_root=screenshots_root,
                    )
                    result.screenshots.append(str(shot))
                    result.ok = True
                    return result

                spec.post_publish(page, bundle, ctx)
            except Exception:
                _capture_error_artifacts(page, ctx)
                raise

        result.ok = True
        return result

    except PublisherError as exc:
        result.error_type = classify(exc)
        result.error_msg = f"step={ctx.last_step}: {exc}"
        logger.error(result.error_msg)
        return result
    except KeyboardInterrupt:
        result.error_type = "interrupted"
        result.error_msg = f"step={ctx.last_step}: 用户中断(Ctrl-C)"
        logger.warning(result.error_msg)
        raise  # finally 写 DB 后,KeyboardInterrupt 继续向上传播
    except Exception as exc:
        result.error_type = classify(exc)
        result.error_msg = f"step={ctx.last_step}: {exc}"
        logger.exception(f"[{spec.platform_key}] publish 顶层未分类异常")
        return result
    finally:
        unblock_user_input()  # 任何路径(成功/失败/中断)都恢复输入,防锁死
        _finalize(engine, settings, ctx, bundle, spec)


def _finalize(
    engine: Engine,
    settings: Settings,
    ctx: PublishContext,
    bundle: TaskBundle,
    spec: PlatformSpec,
) -> None:
    """收尾状态机:cleanup tmp / task 终态 / 风控暂停 / cookie_status / 通知 / 飞书回写。"""
    result = ctx.result
    task_id = ctx.task_id
    account_id = bundle.account_id

    # cleanup tmp(本地文件操作,失败不影响后续 DB 写)
    try:
        cleanup_tmp(task_id=task_id, tmp_root=ctx.tmp_root)
    except Exception as exc:
        logger.warning(f"cleanup_tmp 失败 task_id={task_id}: {exc}")

    # 决定 task 最终状态
    if result.ok and not result.dry_run:
        new_status = "success"
    elif result.dry_run and result.ok:
        new_status = "pending"  # dry_run 没真发 → 回到可执行
    elif result.error_type == "interrupted":
        new_status = "interrupted"
    elif result.error_type == "cookie_expired":
        # 登录态失效不是 task 自身问题:回退 pending + 不回写飞书"失败",
        # 运营扫码后下轮 queue_today 自动重跑;cookie_status 仍写 expired。
        new_status = "pending"
    else:
        new_status = "failed"

    with session_scope(engine) as session:
        task_now = session.get(Task, task_id)
        assert task_now is not None
        task_now.status = new_status
        task_now.remote_url = result.remote_url
        task_now.remote_video_id = result.remote_video_id
        task_now.last_error_type = result.error_type
        task_now.last_error_msg = result.error_msg
        task_now.screenshots_json = _json.dumps(result.screenshots, ensure_ascii=False)

        if result.dry_run and result.ok:
            # dry_run 成功 → 抹掉 claim 痕迹,task 视作没跑过
            task_now.lease_token = None
            task_now.lease_expires_at = None
            task_now.started_at = None
            task_now.attempts = max(0, task_now.attempts - 1)
            task_now.finished_at = None
        elif result.error_type == "cookie_expired":
            # 清 lease 让下轮 queue_today/claim_task 能抢回来;attempts 保留(审计)。
            task_now.lease_token = None
            task_now.lease_expires_at = None
            task_now.started_at = None
            task_now.finished_at = None
        else:
            task_now.finished_at = datetime.now()

        # 风控 → 暂停账号 24h(CLAUDE.md 核心约束 §1)
        if result.error_type == "risk_control":
            account_now = session.get(Account, account_id)
            if account_now is not None:
                account_now.paused_until = datetime.now() + timedelta(hours=24)
                logger.warning(f"风控触发,账号 {account_id} 暂停 24h")

        # 登录态失效 → 回写 Account.cookie_status=expired(UI + scheduler pre-flight 用)
        if result.error_type == "cookie_expired":
            account_now = session.get(Account, account_id)
            if account_now is not None:
                account_now.cookie_status = "expired"
                logger.warning(f"登录态失效,账号 {account_id} cookie_status → expired")

        # 失败派 NotifyEvent(dry-run 不告警;同账号同类失败由 halt 机制去重)
        if not result.ok and not result.dry_run:
            error_type = result.error_type or "unknown"
            notify_type, level, title = _NOTIFY_BY_ERROR.get(
                error_type,
                ("task_failed", "error", f"任务失败:{error_type_cn(error_type)}"),
            )
            # element_not_found 给 title 拼上"在哪一步 + 平台名",运营一眼看到改版位置
            if error_type == "element_not_found":
                step_zh = step_cn(ctx.last_step)
                title = f"元素未找到 · {step_zh} —— {spec.display_name}可能改版"
            account_cfg = settings.accounts.get(account_id)
            display_name = account_cfg.display_name if account_cfg else None
            notify(
                NotifyEvent(
                    type=notify_type,
                    level=level,
                    title=title,
                    content=result.error_msg or "(无错误信息)",
                    context={
                        "错误类型": error_type_cn(error_type),
                        "最近步骤": step_cn(ctx.last_step),
                    },
                    task_id=task_id,
                    account_id=account_id,
                    account_display_name=display_name,
                    platform=bundle.platform,
                ),
                session=session,
                settings=settings,
            )

    # 终态回写飞书(HTTP 调用放 session_scope 之外,不持 DB 事务)
    _writeback_to_feishu(bundle.video_record_id, result, settings)


def _writeback_to_feishu(
    record_id: str,
    result: PublishResult,
    settings: Settings,
) -> None:
    """任务终态回写飞书 Bitable。

    跳过条件:dry-run / feishu disabled / write_back_enabled=False /
    cookie_expired(本轮没真给 task 机会,飞书状态保留'已计划'等扫码后重跑)。
    成功 → 状态=已发布 (+ 已发布链接,如能拿到);失败 → 状态=失败 + 错误信息。
    API 异常被吞(只 log),不能让飞书挂掉主流程。
    """
    if result.dry_run:
        return
    if not settings.feishu.enabled or not settings.feishu.sync.write_back_enabled:
        return
    if result.error_type == "cookie_expired":
        return

    fm = settings.feishu.field_map
    fields: dict[str, str] = {}
    if result.ok:
        fields[fm.status] = "已发布"
        if result.remote_url:
            fields[fm.remote_url] = result.remote_url
    else:
        fields[fm.status] = "失败"
        fields[fm.error_message] = result.error_msg or "(无错误信息)"

    try:
        client = make_client(settings.feishu.app_id, settings.feishu.app_secret)
        writeback_row(
            client,
            app_token=settings.feishu.bitable.app_token,
            table_id=settings.feishu.bitable.table_id,
            record_id=record_id,
            fields=fields,
        )
    except FeishuApiError as exc:
        logger.warning(f"[publisher] 飞书回写失败 task={result.task_id} record={record_id}: {exc}")
    except Exception as exc:
        logger.exception(f"[publisher] 飞书回写未预料异常 task={result.task_id}: {exc}")
