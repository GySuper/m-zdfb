"""抖音创作者中心发布实现 —— patchright(sync)驱动。

只负责浏览器交互(打开页 → 上传 → 填表 → 点发布)。claim / DB 状态机 / 通知 /
飞书回写等无差别 plumbing 全在 wxsp/platforms/runner.py 的共享编排器里。

步骤逻辑从 _ref/social-auto-upload/uploader/douyin_uploader/main.py(异步脚本式)
翻译为同步 + adapter 模式,保留其选择器与等待策略。决策:无指纹 / 纯定时 / 只发视频。
"""

from __future__ import annotations

import json as _json
import time
from datetime import datetime
from pathlib import Path

from loguru import logger
from patchright.sync_api import Page

from wxsp.browser import browser_context
from wxsp.config import Settings
from wxsp.errors import (
    CookieExpired,
    ElementNotFound,
    NetworkError,
    RiskControl,
    UploadFailed,
)
from wxsp.models import Account
from wxsp.nas import stage_to_tmp
from wxsp.platforms import douyin_selectors as sel
from wxsp.platforms.base import PlatformSpec, PublishContext, PublishResult, TaskBundle
from wxsp.platforms.runner import random_pause, run_publish

# ---------------------------------------------------------------------------
# step functions
# ---------------------------------------------------------------------------


def _open_publish_page(page: Page) -> None:
    page.goto(sel.UPLOAD_PAGE, wait_until="domcontentloaded")
    try:
        page.wait_for_url(sel.UPLOAD_PAGE, timeout=30_000)
    except Exception as err:
        raise NetworkError("抖音上传页加载超时") from err


def _verify_logged_in(page: Page) -> None:
    for marker in sel.LOGIN_TEXT_MARKERS:
        try:
            if page.get_by_text(marker, exact=True).count():
                raise CookieExpired("抖音登录态失效,需重新扫码登录")
        except CookieExpired:
            raise
        except Exception:
            pass


def _upload_video(page: Page, file_path: Path, timeout_seconds: int = 600) -> None:
    page.locator(sel.VIDEO_FILE_INPUT).set_input_files(str(file_path))

    # 等进入发布页(两种 URL 变体之一)
    on_publish = False
    appear_deadline = time.time() + 30
    while time.time() < appear_deadline:
        for url in sel.PUBLISH_PAGE_URLS:
            try:
                page.wait_for_url(url, timeout=3000)
                on_publish = True
                break
            except Exception:
                continue
        if on_publish:
            break
        time.sleep(0.5)
    if not on_publish:
        raise UploadFailed("上传后未进入发布页")

    # 等上传/转码完成("重新上传"标记出现 = 完成);"上传失败" → 重试一次
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            if page.locator(sel.UPLOAD_DONE_MARKER).count() > 0:
                logger.info("[douyin] 视频上传完成")
                return
            if page.locator(sel.UPLOAD_FAILED_MARKER).count() > 0:
                logger.warning("[douyin] 检测到上传失败,重新上传")
                page.locator(sel.UPLOAD_RETRY_INPUT).set_input_files(str(file_path))
        except Exception:
            pass
        time.sleep(2)
    raise UploadFailed("视频上传/处理超时")


def _fill_title(page: Page, title: str) -> None:
    if not title:
        return
    inp = page.locator(sel.TITLE_INPUT).first
    inp.wait_for(state="visible", timeout=10_000)
    inp.fill(title[: sel.TITLE_MAX_LENGTH])


def _fill_description(page: Page, description: str | None, fallback_title: str) -> None:
    text = description or fallback_title
    if not text:
        return
    # 发布页是全新作品,简介编辑器初始为空,无需 select-all 清空(且 Ctrl+A 在 macOS
    # 不是全选)。click 聚焦后键盘输入,光标留在末尾,供随后的 _add_tags 追加话题标签。
    editor = page.locator(sel.DESC_EDITOR).first
    editor.wait_for(state="visible", timeout=10_000)
    editor.click()
    page.keyboard.type(text)


def _add_tags(page: Page, tags: list[str]) -> None:
    for tag in tags:
        page.keyboard.type(" #" + tag)
        page.keyboard.press("Space")


def _set_cover(page: Page, cover_path: Path | None) -> None:
    """有自定义封面 → 走封面弹窗上传(可选功能)。

    抖音封面弹窗较复杂(横/竖/AI 封面 + 裁剪),入口与「完成」按钮可能各有多个,
    一律取 .first。裁剪/应用全流程未端到端实跑校验,首次用自定义封面时按需微调;
    无封面时 douyin 会自动生成 AI 封面,此步整体跳过。
    """
    if cover_path is None:
        return
    page.locator(sel.COVER_ENTRY).first.click()
    modal = page.locator(sel.COVER_MODAL)
    modal.wait_for(timeout=10_000)
    page.wait_for_timeout(1000)
    modal.locator(sel.COVER_UPLOAD_INPUT).first.set_input_files(str(cover_path))
    page.wait_for_timeout(2000)
    modal.locator(sel.COVER_DONE_BUTTON).first.click()
    page.wait_for_timeout(1000)
    logger.info("[douyin] 自定义封面设置完成(best-effort)")


def _handle_auto_cover(page: Page) -> None:
    """平台要求封面但未设自定义封面时,选第一个推荐封面(发布前兜底,失败不阻断)。"""
    try:
        if not page.get_by_text(sel.COVER_REQUIRED_HINT).first.is_visible():
            return
    except Exception:
        return
    rec = page.locator(sel.COVER_RECOMMEND_FIRST).first
    if not rec.count():
        return
    try:
        rec.click()
        page.wait_for_timeout(1000)
        if page.get_by_text(sel.COVER_CONFIRM_APPLY_TEXT).first.is_visible():
            page.get_by_role("button", name="确定").click()
            page.wait_for_timeout(1000)
        logger.info("[douyin] 已应用推荐封面")
    except Exception as exc:
        logger.warning(f"[douyin] 应用推荐封面失败: {exc}")


def _set_schedule(page: Page, publish_at: datetime) -> None:
    # 切到「定时发布」→ 日期时间输入框出现(默认预填当前+2h,故必须整体替换)。
    # 用 fill() 一次性清空并写入,避免 Ctrl/Cmd+A 全选的跨平台差异(macOS 是 Cmd+A)。
    page.locator(sel.SCHEDULE_RADIO).click()
    page.wait_for_timeout(1000)
    inp = page.locator(sel.SCHEDULE_DATETIME_INPUT)
    inp.fill(publish_at.strftime(sel.SCHEDULE_DATETIME_FORMAT))
    inp.press("Enter")
    page.wait_for_timeout(1000)


def _risk_control_probe(page: Page) -> None:
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return
    for kw in sel.RISK_CONTROL_KEYWORDS:
        if kw in body_text:
            raise RiskControl(f"页面命中风控关键词: {kw}")


def _click_publish(page: Page) -> None:
    btn = page.get_by_role("button", name=sel.PUBLISH_BUTTON_NAME, exact=True)
    btn.wait_for(state="visible", timeout=10_000)
    # 发布前若平台要求封面而未设 → 兜底选推荐封面,否则发布按钮点了也发不出去
    _handle_auto_cover(page)
    btn.click()


def _wait_for_success(page: Page, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            page.wait_for_url(sel.MANAGE_URL_GLOB, timeout=3000)
            logger.info("[douyin] 已跳转作品管理页,发布成功")
            return
        except Exception:
            # 跳转判据为主;个别情况只弹「发布成功」toast 不跳转,用文本兜底
            for kw in sel.SUCCESS_INDICATORS:
                try:
                    if page.get_by_text(kw).first.is_visible():
                        logger.info(f"[douyin] 命中成功文案「{kw}」,发布成功")
                        return
                except Exception:
                    pass
            _handle_auto_cover(page)
            time.sleep(0.5)
    raise ElementNotFound("发布成功判定超时")


# ---------------------------------------------------------------------------
# 平台步骤回调 + Spec + Publisher
# ---------------------------------------------------------------------------


def _pre_publish(page: Page, bundle: TaskBundle, staged: Path, ctx: PublishContext) -> None:
    """打开页 → 上传 → 填表 → 封面 → 定时 → 风控探测(止于 dry-run gate 之前)。"""
    step_pause = ctx.step_pause
    upload_timeout = ctx.settings.publisher.upload_timeout_seconds

    staged_cover = None
    if bundle.video_cover_path is not None:
        staged_cover = stage_to_tmp(
            bundle.video_cover_path, task_id=ctx.task_id, tmp_root=ctx.tmp_root
        )

    ctx.last_step = "open_publish"
    _open_publish_page(page)
    random_pause(step_pause)

    ctx.last_step = "verify_login"
    _verify_logged_in(page)
    random_pause(step_pause)

    ctx.last_step = "upload"
    _upload_video(page, file_path=staged, timeout_seconds=upload_timeout)
    random_pause(step_pause)

    ctx.last_step = "title"
    _fill_title(page, title=bundle.title)
    random_pause(step_pause)

    ctx.last_step = "desc"
    _fill_description(page, description=bundle.description, fallback_title=bundle.title)
    random_pause(step_pause)

    ctx.last_step = "tags"
    _add_tags(page, tags=_json.loads(bundle.tags_json or "[]"))
    random_pause(step_pause)

    ctx.last_step = "cover"
    _set_cover(page, cover_path=staged_cover)
    random_pause(step_pause)

    ctx.last_step = "schedule"
    _set_schedule(page, publish_at=bundle.publish_at)
    random_pause(step_pause)

    ctx.last_step = "risk"
    _risk_control_probe(page)


def _post_publish(page: Page, bundle: TaskBundle, ctx: PublishContext) -> None:
    """点发布 → 等跳转判成功。抖音定时发布到点前无公开链接,不抽取 remote_url。"""
    ctx.last_step = "publish"
    _click_publish(page)

    ctx.last_step = "wait_success"
    _wait_for_success(page)


DOUYIN_SPEC = PlatformSpec(
    platform_key="douyin",
    display_name="抖音",
    pre_publish=_pre_publish,
    post_publish=_post_publish,
)


class DouyinPublisher:
    platform_key = "douyin"

    def login(self, account: Account) -> bool:
        """开浏览器导航到抖音创作者中心首页,等用户扫码登录。"""
        user_data_dir = Path(account.user_data_dir)
        logger.info(f"[douyin] 开始登录 account={account.id}")
        try:
            with browser_context(
                user_data_dir,
                headless=False,
                account_id=account.id,
                platform="douyin",
            ) as page:
                page.goto(sel.HOME_URL, wait_until="domcontentloaded")
                deadline = time.time() + 300
                while time.time() < deadline:
                    if page.url.startswith(sel.LOGGED_IN_HOME_PREFIX):
                        markers_visible = False
                        for m in sel.LOGIN_TEXT_MARKERS:
                            try:
                                if page.get_by_text(m, exact=True).first.is_visible():
                                    markers_visible = True
                                    break
                            except Exception:
                                pass
                        if not markers_visible:
                            logger.info(f"[douyin] 登录成功 account={account.id}")
                            return True
                    time.sleep(2)
                logger.warning(f"[douyin] 登录超时 account={account.id}")
                return False
        except Exception as exc:
            logger.error(f"[douyin] 登录异常 account={account.id}: {exc}")
            return False

    def publish_one(
        self,
        task_id: int,
        *,
        dry_run: bool = False,
        settings: Settings,
    ) -> PublishResult:
        return run_publish(task_id, dry_run=dry_run, settings=settings, spec=DOUYIN_SPEC)
