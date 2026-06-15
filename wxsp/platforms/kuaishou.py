"""快手创作者平台发布实现 —— patchright(sync)驱动。

只负责浏览器交互(打开页 → 上传 → 填表 → 点发布)。claim / DB 状态机 / 通知 /
飞书回写等无差别 plumbing 全在 wxsp/platforms/runner.py 的共享编排器里。

步骤逻辑从 _ref/social-auto-upload/uploader/ks_uploader/main.py(KSVideo,异步脚本式)
翻译为同步 + adapter 模式,保留其选择器与等待策略。决策:无指纹 / 纯定时 / 只发视频。
快手发布页无独立标题框,标题填进「描述」框(description 或回退 title)。
"""

from __future__ import annotations

import json as _json
import random
import time
from datetime import datetime
from pathlib import Path

from loguru import logger
from patchright.sync_api import Page

import wxsp.apc
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
from wxsp.platforms import kuaishou_selectors as sel
from wxsp.platforms.base import PlatformSpec, PublishContext, PublishResult, TaskBundle
from wxsp.platforms.runner import random_pause, run_publish, screenshot

# ---------------------------------------------------------------------------
# step functions
# ---------------------------------------------------------------------------


def _open_publish_page(page: Page) -> None:
    page.goto(sel.UPLOAD_PAGE, wait_until="domcontentloaded")
    try:
        page.wait_for_url(sel.UPLOAD_PAGE_GLOB, timeout=30_000)
    except Exception as err:
        # 未登录会被重定向到 passport,wait_for_url 超时是预期 —— 交给 _verify_logged_in 判 CookieExpired。
        # 其它 URL(既不在上传页也不在 passport)才是真的加载失败。
        if sel.LOGIN_URL_FRAGMENT not in page.url:
            raise NetworkError("快手上传页加载超时") from err


def _verify_logged_in(page: Page) -> None:
    if sel.LOGIN_URL_FRAGMENT in page.url:
        raise CookieExpired("快手登录态失效,需重新扫码登录")


def _dismiss_overlays(page: Page) -> None:
    """关掉首次进页面的「我知道了」提示 + Joyride 新手引导遮罩(都 best-effort,失败不阻断)。"""
    try:
        know = page.locator(sel.KNOW_BUTTON).first
        if know.count() and know.is_visible():
            know.click()
    except Exception:
        pass
    try:
        tooltip = page.locator(sel.JOYRIDE_TOOLTIP)
        if tooltip.count() and tooltip.first.is_visible():
            page.locator('div[role="alertdialog"]').locator(sel.JOYRIDE_CLOSE).click(force=True)
            tooltip.wait_for(state="hidden", timeout=5000)
    except Exception:
        pass


def _upload_video(page: Page, file_path: Path, timeout_seconds: int = 600) -> None:
    upload_button = page.locator(sel.UPLOAD_BUTTON)
    upload_button.wait_for(state="visible", timeout=10_000)
    with page.expect_file_chooser() as fc_info:
        upload_button.click()
    fc_info.value.set_files(str(file_path))

    # 给上传一点时间起步(否则下面 `上传中` 可能还没出现就误判完成),顺手关引导遮罩
    page.wait_for_timeout(2000)
    _dismiss_overlays(page)

    # 等上传/转码完成。先判 `上传失败`(它和 `上传中` 互斥,失败时 `上传中` 已消失,
    # 若先判完成会把失败误当成功)→ 重传一次仍失败则判失败;再判 `上传中` 消失 = 完成。
    deadline = time.time() + timeout_seconds
    retried = False
    while time.time() < deadline:
        try:
            if page.locator(sel.UPLOAD_FAILED_MARKER).count() > 0:
                if retried:
                    raise UploadFailed("视频上传失败(重传一次后仍失败)")
                logger.warning("[kuaishou] 检测到上传失败,重新上传(仅一次)")
                page.locator(sel.UPLOAD_RETRY_INPUT).set_input_files(str(file_path))
                retried = True
            elif page.locator(sel.UPLOADING_MARKER).count() == 0:
                logger.info("[kuaishou] 视频上传完成")
                return
        except UploadFailed:
            raise
        except Exception:
            pass
        time.sleep(2)
    raise UploadFailed("视频上传/处理超时")


def _fill_description(page: Page, description: str | None, fallback_title: str) -> None:
    text = description or fallback_title
    if not text:
        return
    # 快手发布页无独立标题框,「描述」框是主文案区。发布页为全新作品,描述框初始为空,
    # 无需 select-all 清空(且 Ctrl+A 在 macOS 不是全选)。click 聚焦后键盘输入。
    editor = page.get_by_text(sel.DESC_LABEL_TEXT).locator("xpath=following-sibling::div").first
    editor.wait_for(state="visible", timeout=10_000)
    editor.click()
    page.keyboard.type(text)


def _add_tags(page: Page, tags: list[str]) -> None:
    if not tags:
        return
    # 与描述同处一个可编辑区,先回车另起避免和描述粘连/触发 @ 下拉;每个话题之间停顿等下拉登记
    page.keyboard.press("Enter")
    for tag in tags[: sel.MAX_TAGS]:
        page.keyboard.type(f"#{tag} ")
        page.wait_for_timeout(2000)


def _set_cover(page: Page, cover_path: Path | None) -> None:
    """有自定义封面 → 走「封面设置」弹窗(可选,best-effort,未端到端实跑)。"""
    if cover_path is None:
        return
    cover_label = page.locator("span").filter(has_text=sel.COVER_LABEL_TEXT)
    cover_label.wait_for(state="visible", timeout=30_000)
    cover_label.locator("xpath=../following-sibling::div[1]").locator("div").first.click()
    modal = page.locator(sel.COVER_MODAL)
    modal.wait_for(state="visible", timeout=30_000)
    tab = modal.get_by_text(sel.COVER_UPLOAD_TAB_TEXT, exact=True)
    tab.wait_for(state="visible", timeout=10_000)
    tab.click()
    file_input = modal.locator('input[type="file"]')
    file_input.wait_for(state="attached", timeout=30_000)
    file_input.set_input_files(str(cover_path))
    page.wait_for_timeout(1000)
    confirm = modal.get_by_role("button", name=sel.COVER_CONFIRM_BUTTON_NAME, exact=True)
    confirm.wait_for(state="visible", timeout=10_000)
    confirm.click()
    modal.wait_for(state="hidden", timeout=30_000)
    logger.info("[kuaishou] 自定义封面设置完成(best-effort)")


# ant-design DatePicker 是 controlled component,必须 native value setter + 冒泡事件,普通 fill 无效。
# selector 由调用方传入(单一来源 = sel.SCHEDULE_DATETIME_INPUT),避免和选择器常量悄悄分叉。
_SCHEDULE_JS = """
(args) => {
    const [selector, newValue] = args;
    const input = document.querySelector(selector);
    if (!input) return false;
    const nativeSetter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    ).set;
    nativeSetter.call(input, newValue);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
}
"""


def _set_schedule(page: Page, publish_at: datetime) -> None:
    page.locator(sel.SCHEDULE_RADIO_WRAPPER).filter(has_text=sel.SCHEDULE_RADIO_TEXT).click()
    page.wait_for_timeout(2000)
    page.locator(sel.SCHEDULE_DATETIME_INPUT).click()
    page.wait_for_timeout(1000)
    ok = page.evaluate(
        _SCHEDULE_JS,
        [sel.SCHEDULE_DATETIME_INPUT, publish_at.strftime(sel.SCHEDULE_DATETIME_FORMAT)],
    )
    if not ok:
        raise ElementNotFound("找不到定时发布时间输入框")
    page.wait_for_timeout(1000)
    page.keyboard.press("Enter")
    page.wait_for_timeout(2000)


def _risk_control_probe(page: Page) -> None:
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return
    for kw in sel.RISK_CONTROL_KEYWORDS:
        if kw in body_text:
            raise RiskControl(f"页面命中风控关键词: {kw}")


def _click_publish(page: Page) -> None:
    btn = page.get_by_text(sel.PUBLISH_BUTTON_TEXT, exact=True)
    btn.wait_for(state="visible", timeout=10_000)
    btn.click()
    page.wait_for_timeout(1000)
    confirm = page.get_by_text(sel.PUBLISH_CONFIRM_TEXT)
    if confirm.count() > 0:
        confirm.first.click()


def _wait_for_success(page: Page, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            page.wait_for_url(sel.MANAGE_URL_GLOB, timeout=3000)
            logger.info("[kuaishou] 已跳转作品管理页,发布成功")
            return
        except Exception:
            # 跳转判据为主;个别情况只弹「发布成功」toast 不跳转,用文本兜底
            for kw in sel.SUCCESS_INDICATORS:
                try:
                    if page.get_by_text(kw).first.is_visible():
                        logger.info(f"[kuaishou] 命中成功文案「{kw}」,发布成功")
                        return
                except Exception:
                    pass
            time.sleep(0.5)
    raise ElementNotFound("发布成功判定超时")


# ---------------------------------------------------------------------------
# 平台步骤回调 + Spec + Publisher
# ---------------------------------------------------------------------------


def _pre_publish(page: Page, bundle: TaskBundle, staged: Path, ctx: PublishContext) -> None:
    """打开页 → 上传 → 填描述 → 标签 → 封面 → 定时 → 风控探测(止于 dry-run gate 之前)。"""
    step_pause = ctx.step_pause
    upload_timeout = ctx.settings.publisher.upload_timeout_seconds

    staged_cover = None
    if bundle.video_cover_path is not None:
        staged_cover = stage_to_tmp(
            bundle.video_cover_path, task_id=ctx.task_id, tmp_root=ctx.tmp_root
        )

    # APC 守门(对齐 tencent §3.3 注入点):dev-mode 永远 True;打包模式看 APC 判决
    apc_passed = wxsp.apc.check_pass()

    ctx.last_step = "open_publish"
    _open_publish_page(page)
    random_pause(step_pause)

    ctx.last_step = "verify_login"
    _verify_logged_in(page)
    random_pause(step_pause)

    # APC 拒绝时装"等待上传区域超时"故障(对齐 tencent §3.3 / douyin)
    if not apc_passed:
        ctx.last_step = "wait_upload_area"
        time.sleep(random.uniform(45, 75))
        shot = screenshot(
            page,
            task_id=ctx.task_id,
            step="wait_upload_area",
            screenshots_root=ctx.screenshots_root,
        )
        ctx.result.screenshots.append(str(shot))
        raise ElementNotFound("等待上传区域超时(60s)")

    ctx.last_step = "upload"
    _upload_video(page, file_path=staged, timeout_seconds=upload_timeout)
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
    """点发布 → 确认发布 → 等跳转判成功。定时发布到点前无公开链接,不抽取 remote_url。"""
    ctx.last_step = "publish"
    _click_publish(page)

    ctx.last_step = "wait_success"
    _wait_for_success(page)


KUAISHOU_SPEC = PlatformSpec(
    platform_key="kuaishou",
    display_name="快手",
    pre_publish=_pre_publish,
    post_publish=_post_publish,
)


class KuaishouPublisher:
    platform_key = "kuaishou"

    def login(self, account: Account) -> bool:
        """开浏览器到快手上传页(未登录会重定向到 passport 扫码),等 URL 离开 passport = 登录成功。"""
        user_data_dir = Path(account.user_data_dir)
        logger.info(f"[kuaishou] 开始登录 account={account.id}")
        try:
            with browser_context(
                user_data_dir,
                headless=False,
                account_id=account.id,
                platform="kuaishou",
            ) as page:
                page.goto(sel.UPLOAD_PAGE, wait_until="domcontentloaded")
                deadline = time.time() + 300
                while time.time() < deadline:
                    if sel.LOGIN_URL_FRAGMENT not in page.url:
                        logger.info(f"[kuaishou] 登录成功 account={account.id}")
                        return True
                    time.sleep(2)
                logger.warning(f"[kuaishou] 登录超时 account={account.id}")
                return False
        except Exception as exc:
            logger.error(f"[kuaishou] 登录异常 account={account.id}: {exc}")
            return False

    def publish_one(
        self,
        task_id: int,
        *,
        dry_run: bool = False,
        settings: Settings,
    ) -> PublishResult:
        return run_publish(task_id, dry_run=dry_run, settings=settings, spec=KUAISHOU_SPEC)
