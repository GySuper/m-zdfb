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
    RiskControl,
    UnknownError,
    UploadFailed,
)
from wxsp.managed_chrome import ExtensionSession, wait_logged_in_url
from wxsp.models import Account
from wxsp.nas import stage_to_tmp
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
}


def _pump(
    session: ExtensionSession, ctx: PublishContext, until: str, timeout: float
) -> dict[str, Any]:
    """收插件消息直到 `until` 或 halted;step 同步到 ctx.last_step,error 转成 errors.py 异常。"""
    while True:
        try:
            msg = session.next_message(timeout=timeout)
        except TimeoutError as err:
            raise ElementNotFound(f"插件 {timeout:.0f}s 无进展") from err
        kind = msg.get("type")
        if kind == "step":
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


def _pre_publish(
    session: ExtensionSession, bundle: TaskBundle, staged: Path, ctx: PublishContext
) -> None:
    # 必挂商品(拼多多带货视频核心):为空直接失败,不必先开页面上传
    product_ids = _product_ids(bundle, ctx)
    if not product_ids:
        ctx.last_step = "products"
        raise ProductNotFound("拼多多必挂商品,但 product_ids 为空")
    staged_cover = None
    if bundle.video_cover_path is not None:
        staged_cover = stage_to_tmp(
            bundle.video_cover_path, task_id=ctx.task_id, tmp_root=ctx.tmp_root
        )
    upload_timeout = ctx.settings.publisher.upload_timeout_seconds
    apc_passed = wxsp.apc.check_pass()
    session.start_job(
        {
            "platform": "pinduoduo",
            "step_pause": list(ctx.step_pause),
            "upload_timeout_s": upload_timeout,
            "video_path": str(staged),
            "cover_path": str(staged_cover) if staged_cover else None,
            "description": bundle.description or "",
            "tags": _json.loads(bundle.tags_json or "[]"),
            "product_ids": product_ids,
            "publish_at": bundle.publish_at.strftime("%Y-%m-%d %H:%M:%S"),  # 与页面日期框同格式
            "declaration": bundle.declaration,
            "detail_float": bundle.detail_float,  # 商详悬浮窗;None = 保持页面默认
            "store_feed": bundle.store_feed,  # 同步店铺动态;None = 保持页面默认
            # APC 拒绝:插件停在上传前,下面装"等待上传区域超时"故障(对齐其他平台)
            "halt_before": None if apc_passed else "upload",
        }
    )
    # 上传期间插件不发消息,单条消息的等待上限按上传超时放宽
    _pump(session, ctx, "gate", timeout=upload_timeout + 120)
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
