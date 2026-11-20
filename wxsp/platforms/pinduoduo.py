"""拼多多多多视频发布 —— 托管系统 Chrome + 自动注入的插件执行(driver="extension")。

页面步骤(打开页 → 上传 → 描述/话题 → 绑商品 → 封面 → 定时 → 内容声明 → 风控探测 →
点发布 → 成功判定)全部在 wxsp/extension/pinduoduo.js 里用 chrome.debugger 可信输入完成;
本模块只负责把任务数据交给插件、把插件消息翻译成 ctx.last_step / errors.py 异常。
claim / DB 状态机 / 通知 / 飞书回写等无差别 plumbing 在 runner.py 共享编排器里。
设计见 docs/superpowers/specs/2026-11-18-pinduoduo-extension-publish-design.md。
决策:必挂商品 / 只走定时发布 / 封面 best-effort。
"""

from __future__ import annotations

import json as _json
import random
import time
from pathlib import Path
from typing import Any

from loguru import logger

import wxsp.apc
from wxsp.config import Settings
from wxsp.errors import (
    CookieExpired,
    ElementNotFound,
    NetworkError,
    ProductNotFound,
    PublisherError,
    PublishUnconfirmed,
    RiskControl,
    UnknownError,
    UploadFailed,
    classify,
)
from wxsp.managed_chrome import ExtensionSession, wait_logged_in_url
from wxsp.models import Account
from wxsp.nas import stage_to_tmp
from wxsp.notify import step_cn
from wxsp.platforms import pinduoduo_selectors as sel
from wxsp.platforms.base import PlatformSpec, PublishContext, PublishResult, TaskBundle
from wxsp.platforms.runner import run_publish, screenshot

_EXT_ERRORS: dict[str, type[PublisherError]] = {
    "cookie_expired": CookieExpired,
    "risk_control": RiskControl,
    "element_not_found": ElementNotFound,
    "upload_failed": UploadFailed,
    "product_not_found": ProductNotFound,
    "network": NetworkError,
    "publish_unconfirmed": PublishUnconfirmed,
}

# 发布前(gate 之前)的暂时性错误自动重跑整轮:插件中止旧一轮、新标签页从头来,还没点发布所以安全。
# 次数对齐 CLAUDE.md 错误表;总轮数封顶。登录失效 / 风控 / 商品类错误重跑也没用,不重试。
# 点发布之后(_post_publish)绝不自动重试,避免重复发布。
_RERUN_BUDGET = {"network": 3, "upload_failed": 2, "element_not_found": 1, "unknown": 1}
_MAX_ROUNDS = 3


def _pump(
    session: ExtensionSession, ctx: PublishContext, until: str, timeout: float
) -> dict[str, Any]:
    """收插件消息直到 `until` 或 halted;step 同步到 ctx.last_step,error 转成 errors.py 异常。"""
    while True:
        try:
            msg = session.next_message(timeout=timeout)
        except TimeoutError as err:
            raise ElementNotFound(f"页面 {timeout / 60:.0f} 分钟没有任何进展") from err
        kind = msg.get("type")
        if kind == "relogin":
            ctx.result.relogged_in = True
            logger.info(f"[pinduoduo] task={ctx.task_id} 登录已过期,已用账号密码自动登录")
        elif kind == "step":
            ctx.last_step = msg["step"]
            logger.info(f"[pinduoduo] task={ctx.task_id} 步骤 {ctx.last_step}")
        elif kind == "error":
            ctx.last_step = msg.get("step") or ctx.last_step
            raise _EXT_ERRORS.get(msg.get("kind", ""), UnknownError)(msg.get("message", ""))
        elif kind in (until, "halted"):
            return msg


def _product_ids(bundle: TaskBundle, ctx: PublishContext) -> list[str]:
    try:
        parsed = _json.loads(bundle.product_ids_json or "[]")
    except (TypeError, _json.JSONDecodeError):
        logger.warning(
            f"[pinduoduo] 商品 ID JSON 解析失败 task_id={ctx.task_id}: {bundle.product_ids_json!r}"
        )
        return []
    return [str(p) for p in parsed if p] if isinstance(parsed, list) else []


def _login_credentials(ctx: PublishContext, account_id: str) -> dict[str, str] | None:
    cfg = ctx.settings.accounts.get(account_id)
    if cfg is None or not cfg.password_login or not cfg.login_username or not cfg.login_password:
        return None
    return {"username": cfg.login_username, "password": cfg.login_password}


def _pre_publish(
    session: ExtensionSession, bundle: TaskBundle, staged: Path, ctx: PublishContext
) -> None:
    # 必挂商品(拼多多带货视频核心):为空直接失败,不必先开页面上传
    product_ids = _product_ids(bundle, ctx)
    if not product_ids:
        ctx.last_step = "products"
        raise ProductNotFound("飞书里没填商品 ID(拼多多必须挂商品)")
    staged_cover = None
    if bundle.video_cover_path is not None:
        staged_cover = stage_to_tmp(
            bundle.video_cover_path, task_id=ctx.task_id, tmp_root=ctx.tmp_root
        )
    upload_timeout = ctx.settings.publisher.upload_timeout_seconds
    apc_passed = wxsp.apc.check_pass()
    job: dict[str, Any] = {
        "platform": "pinduoduo",
        "step_pause": list(ctx.step_pause),
        "upload_timeout_s": upload_timeout,
        # 必须绝对路径:Chrome 是另一个进程,data_dir 配成 ./data 时相对路径它找不到,
        # 页面收到空文件(拼多多提示「已过滤空文件」,2026-11-19 任务 58)
        "video_path": str(staged.absolute()),
        "cover_path": str(staged_cover.absolute()) if staged_cover else None,
        "description": bundle.description or "",
        "tags": _json.loads(bundle.tags_json or "[]"),
        "product_ids": product_ids,
        "publish_at": bundle.publish_at.strftime("%Y-%m-%d %H:%M:%S"),  # 与页面日期框同格式
        "declaration": bundle.declaration,
        "detail_float": bundle.detail_float,  # 商详悬浮窗;None = 保持页面默认
        "store_feed": bundle.store_feed,  # 同步店铺动态;None = 保持页面默认
        # 账密登录:登录过期时插件自动用账号密码登录;遇滑块/短信验证按登录失效处理
        "login": _login_credentials(ctx, bundle.account_id),
        "login_url": sel.LOGIN_URL,  # 带「账号登录」表单的 SSO 登录页(登录后跳回视频首页)
        # APC 拒绝:插件停在上传前,下面装"等待上传区域超时"故障(对齐其他平台)
        "halt_before": None if apc_passed else "upload",
    }
    used: dict[str, int] = {}
    for round_no in range(1, _MAX_ROUNDS + 1):
        session.start_job(job)
        try:
            # 上传期间插件不发消息,单条消息的等待上限按上传超时放宽
            _pump(session, ctx, "gate", timeout=upload_timeout + 120)
            break
        except PublisherError as exc:
            kind = classify(exc)
            if round_no == _MAX_ROUNDS or used.get(kind, 0) >= _RERUN_BUDGET.get(kind, 0):
                raise
            used[kind] = used.get(kind, 0) + 1
            shot = screenshot(
                session,
                task_id=ctx.task_id,
                step=f"retry{round_no}_{ctx.last_step}",
                screenshots_root=ctx.screenshots_root,
            )
            ctx.result.screenshots.append(str(shot))
            logger.warning(
                f"[pinduoduo] task={ctx.task_id} 第 {round_no} 轮在「{step_cn(ctx.last_step)}」失败"
                f"({kind}: {exc}),{5 * round_no}s 后开新标签页重跑"
            )
            time.sleep(5 * round_no)
    if not apc_passed:
        ctx.last_step = "wait_upload_area"
        time.sleep(random.uniform(45, 75))
        shot = screenshot(
            session,
            task_id=ctx.task_id,
            step="wait_upload_area",
            screenshots_root=ctx.screenshots_root,
        )
        ctx.result.screenshots.append(str(shot))
        raise ElementNotFound("等待上传区域超时(60s)")


def _post_publish(session: ExtensionSession, bundle: TaskBundle, ctx: PublishContext) -> None:
    session.proceed("publish")
    done = _pump(session, ctx, "done", timeout=120)
    if done.get("remote_url"):
        ctx.result.remote_url = done["remote_url"]


PINDUODUO_SPEC = PlatformSpec(
    platform_key="pinduoduo",
    display_name="拼多多",
    pre_publish=_pre_publish,
    post_publish=_post_publish,
)


class PinduoduoPublisher:
    platform_key = "pinduoduo"

    def login(self, account: Account) -> bool:
        """托管 Chrome 打开拼多多 SSO 登录页等扫码(不需要插件)。

        用正向判据:扫码成功后跳转到 n-creator/video/home,URL 含该片段 = 成功。
        cookie 由真实 Chrome profile 自己持久化,managed_chrome 退出时优雅关闭保证落盘。
        """
        user_data_dir = Path(account.user_data_dir)
        logger.info(f"[pinduoduo] 开始登录 account={account.id}")
        try:
            ok = wait_logged_in_url(
                user_data_dir,
                start_url=sel.LOGIN_URL,
                fragment=sel.LOGGED_IN_URL_FRAGMENT,
                timeout_s=300,
            )
            if ok:
                logger.info(f"[pinduoduo] 登录成功 account={account.id}")
            else:
                logger.warning(f"[pinduoduo] 登录超时 account={account.id}")
            return ok
        except Exception as exc:
            logger.error(f"[pinduoduo] 登录异常 account={account.id}: {exc}")
            return False

    def publish_one(
        self,
        task_id: int,
        *,
        dry_run: bool = False,
        settings: Settings,
    ) -> PublishResult:
        return run_publish(task_id, dry_run=dry_run, settings=settings, spec=PINDUODUO_SPEC)
