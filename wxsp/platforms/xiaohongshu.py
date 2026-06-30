"""小红书创作者中心视频发布实现 —— patchright(sync)驱动。

只负责浏览器交互(打开页 → 上传 → 填表 → 点发布)。claim / DB 状态机 / 通知 /
飞书回写等无差别 plumbing 全在 wxsp/platforms/runner.py 的共享编排器里。

步骤逻辑从 _ref/social-auto-upload/uploader/xiaohongshu_uploader/main.py(XiaoHongShuVideo,
异步脚本式)翻译为同步 + adapter 模式,保留其选择器与等待策略。
决策:无指纹(cookies.json)/ 纯定时 / 只发视频笔记。
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
from wxsp.platforms import xiaohongshu_selectors as sel
from wxsp.platforms.base import PlatformSpec, PublishContext, PublishResult, TaskBundle
from wxsp.platforms.runner import random_pause, run_publish, screenshot

# 小红书新版发布页底部「发布/暂存离开」按钮渲染在 <xhs-publish-btn> 的【闭合】shadow DOM 里,
# patchright 的 CSS/text/role 选择器都钻不进闭合 shadow(实测 2026-06-17)。导航前注入本脚本把
# attachShadow 强制改成 open,该 shadow 即可被 PUBLISH_BUTTON 选择器命中。仅本平台注入(不碰
# browser.py、不影响视频号等按指纹判定的强风控平台)。幂等:重复注入只生效一次。
_FORCE_OPEN_SHADOW_JS = """
(() => {
  const orig = Element.prototype.attachShadow;
  if (!orig || orig.__xhsForcedOpen) return;
  const patched = function (init) {
    return orig.call(this, Object.assign({}, init, { mode: 'open' }));
  };
  patched.__xhsForcedOpen = true;
  Element.prototype.attachShadow = patched;
})();
"""

# ---------------------------------------------------------------------------
# step functions
# ---------------------------------------------------------------------------


def _open_publish_page(page: Page) -> None:
    # 必须在 goto 之前注册:add_init_script 会在该次导航的页面脚本执行前先跑,
    # 才能赶在发布页 attachShadow 调用之前把闭合 shadow 改成 open(见 PUBLISH_BUTTON)。
    page.add_init_script(_FORCE_OPEN_SHADOW_JS)
    page.goto(sel.PUBLISH_VIDEO_URL, wait_until="domcontentloaded")
    # 未登录会被立即重定向到 /login:提前返回,免得 wait_for_url 白等满 30s
    # (登录态由紧随其后的 _verify_logged_in 判定并抛 CookieExpired)。
    if sel.LOGIN_URL_FRAGMENT in page.url:
        return
    try:
        page.wait_for_url(sel.PUBLISH_VIDEO_URL_GLOB, timeout=30_000)
    except Exception as err:
        # 仍可能在加载中被重定向到 /login(慢网),同样交给 _verify_logged_in;
        # 不在 /login 又超时才是真的加载失败。
        if sel.LOGIN_URL_FRAGMENT not in page.url:
            raise NetworkError("小红书发布页加载超时") from err


def _verify_logged_in(page: Page) -> None:
    # 失效判据(负向):被重定向到 /login,或登录框仍可见 → cookie 失效。
    # 发布页场景下 cookie 失效会被重定向到 .../login?redirectReason=401(URL 含 fragment),可靠。
    if sel.LOGIN_URL_FRAGMENT in page.url:
        raise CookieExpired("小红书登录态失效(被重定向到登录页,需重新扫码登录)")
    box = page.locator(sel.LOGIN_BOX_SELECTOR).first
    try:
        if box.count() and box.is_visible():
            raise CookieExpired("小红书登录态失效(登录框可见,需重新扫码登录)")
    except CookieExpired:
        raise
    except Exception:
        pass
    # 存活确认(正向):上传入口必须在(发布页一进就有,不依赖是否已上传视频);
    # 标题框要上传后才渲染,不能用。URL 没到 /login 但页面是异常中间态/空白页也算失效,
    # 避免「负面判据漏判 + 发布流程继续空跑」(与 login() 用 SIDEBAR_MARKER 的正向思路一致)。
    try:
        page.locator(sel.VIDEO_FILE_INPUT).first.wait_for(state="attached", timeout=8000)
    except Exception as err:
        raise CookieExpired("小红书登录态失效(发布页上传入口未出现,需重新扫码登录)") from err


def _upload_video(page: Page, file_path: Path, timeout_seconds: int = 600) -> None:
    page.locator(sel.VIDEO_FILE_INPUT).set_input_files(str(file_path))

    # 等上传/转码完成:预览区文本含完成关键词,或标题框出现(进入编辑态)= 完成。
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            preview = page.locator(sel.UPLOAD_PREVIEW).first
            if preview.count():
                txt = preview.inner_text(timeout=2000)
                if any(k in txt for k in sel.UPLOAD_DONE_KEYWORDS):
                    logger.info("[xiaohongshu] 视频上传完成")
                    return
            title_box = page.locator(sel.TITLE_INPUT).first
            if title_box.count() and title_box.is_visible():
                logger.info("[xiaohongshu] 视频上传完成(标题框已出现)")
                return
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


def _fill_description(page: Page, description: str | None) -> None:
    # 小红书有独立标题框,描述为空时直接跳过(不像抖音/快手回退到 title)。
    if not description:
        return
    # 发布页为全新笔记,正文区初始为空,click 聚焦后直接键入(光标留末尾,供 _add_tags 追加话题)。
    editor = page.locator(sel.DESC_EDITOR).first
    editor.wait_for(state="visible", timeout=10_000)
    editor.click()
    page.keyboard.type(description)


def _add_tags(page: Page, tags: list[str]) -> None:
    if not tags:
        return
    # 正文区若还没聚焦(无描述时)先点一下;话题需键入 #tag 后从下拉选第一个候选才真正绑定。
    page.locator(sel.DESC_EDITOR).first.click()
    for tag in tags:
        page.keyboard.type("#" + tag, delay=30)
        try:
            page.locator(sel.TOPIC_CONTAINER).wait_for(state="visible", timeout=3000)
            first_item = page.locator(sel.TOPIC_ITEM).first
            first_item.wait_for(state="visible", timeout=2000)
            first_item.click()
        except Exception:
            # 下拉没弹出(网络慢/无匹配话题)→ 退而求其次:敲空格让 #tag 以纯文本留在正文
            page.keyboard.press("Space")


def _set_cover(page: Page, cover_path: Path | None) -> None:
    """有自定义封面 → 走封面弹窗上传(可选,best-effort,未端到端实跑)。"""
    if cover_path is None:
        return
    cover_title = page.locator(sel.COVER_PLUGIN_TITLE).filter(has_text=sel.COVER_PLUGIN_TITLE_TEXT)
    entry = cover_title.locator(sel.COVER_PREVIEW_ANCESTOR_XPATH).locator(sel.COVER_ENTRY_INNER)
    entry.first.wait_for(state="visible", timeout=30_000)
    entry.first.click(force=True)

    modal = page.locator(sel.COVER_MODAL)
    modal.wait_for(state="visible", timeout=30_000)
    file_input = modal.locator(sel.COVER_FILE_INPUT).first
    file_input.wait_for(state="attached", timeout=10_000)
    file_input.set_input_files(str(cover_path))
    page.wait_for_timeout(2000)

    confirm = (
        modal.locator(sel.COVER_CONFIRM_BUTTON).filter(has_text=sel.COVER_CONFIRM_BUTTON_TEXT).first
    )
    confirm.wait_for(state="visible", timeout=10_000)
    confirm.click()
    modal.wait_for(state="hidden", timeout=30_000)
    logger.info("[xiaohongshu] 自定义封面设置完成(best-effort)")


def _set_schedule(page: Page, publish_at: datetime) -> None:
    # 切「定时发布」开关 → 日期时间输入框出现,fill 一次性写入(避免 Ctrl/Cmd+A 跨平台差异)。
    page.locator(sel.SCHEDULE_SWITCH_CARD).filter(has_text=sel.SCHEDULE_SWITCH_TEXT).locator(
        sel.SCHEDULE_SWITCH
    ).click()
    page.wait_for_timeout(1000)
    inp = page.locator(sel.SCHEDULE_DATETIME_INPUT)
    inp.fill(publish_at.strftime(sel.SCHEDULE_DATETIME_FORMAT))
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
    btn = page.locator(sel.PUBLISH_BUTTON).first
    btn.wait_for(state="visible", timeout=10_000)
    btn.click()


def _wait_for_success(page: Page, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            page.wait_for_url(sel.SUCCESS_URL_GLOB, timeout=3000)
            logger.info("[xiaohongshu] 已跳转成功页,发布成功")
            return
        except Exception:
            for kw in sel.SUCCESS_INDICATORS:
                try:
                    if page.get_by_text(kw).first.is_visible():
                        logger.info(f"[xiaohongshu] 命中成功文案「{kw}」,发布成功")
                        return
                except Exception:
                    pass
            time.sleep(0.5)
    raise ElementNotFound("发布成功判定超时")


# ---------------------------------------------------------------------------
# 平台步骤回调 + Spec + Publisher
# ---------------------------------------------------------------------------


def _pre_publish(page: Page, bundle: TaskBundle, staged: Path, ctx: PublishContext) -> None:
    """打开页 → 上传 → 填标题/描述/话题 → 封面 → 定时 → 风控探测(止于 dry-run gate 之前)。"""
    step_pause = ctx.step_pause

    staged_cover = None
    if bundle.video_cover_path is not None:
        staged_cover = stage_to_tmp(
            bundle.video_cover_path, task_id=ctx.task_id, tmp_root=ctx.tmp_root
        )

    # APC 守门(对齐 tencent/douyin/kuaishou):dev/非打包永远 True;打包模式看 APC 判决
    apc_passed = wxsp.apc.check_pass()

    ctx.last_step = "open_publish"
    _open_publish_page(page)
    random_pause(step_pause)

    ctx.last_step = "verify_login"
    _verify_logged_in(page)
    random_pause(step_pause)

    # APC 拒绝时装"等待上传区域超时"故障(对齐 tencent §3.3 / douyin / kuaishou)
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
    _upload_video(
        page, file_path=staged, timeout_seconds=ctx.settings.publisher.upload_timeout_seconds
    )
    random_pause(step_pause)

    ctx.last_step = "title"
    _fill_title(page, title=bundle.title)
    random_pause(step_pause)

    ctx.last_step = "desc"
    _fill_description(page, description=bundle.description)
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
    """点定时发布 → 等跳成功页。定时发布到点前无公开链接,不抽取 remote_url。"""
    ctx.last_step = "publish"
    _click_publish(page)

    ctx.last_step = "wait_success"
    _wait_for_success(page)


XIAOHONGSHU_SPEC = PlatformSpec(
    platform_key="xiaohongshu",
    display_name="小红书",
    pre_publish=_pre_publish,
    post_publish=_post_publish,
)


class XiaohongshuPublisher:
    platform_key = "xiaohongshu"

    def login(self, account: Account) -> bool:
        """开浏览器到小红书登录页等扫码。

        未登录时停在 .../login(可能默认手机号登录,用户在可见浏览器里自行点切到扫一扫)。
        用**正向判据**:扫码成功后小红书跳到创作者中心 /new/* 并渲染侧边栏「笔记管理」,
        两者同时满足才算成功。避免「URL 暂离 /login + 登录框未渲染」的加载中间态被误判。
        cookie 由 browser_context 退出时落盘。
        """
        user_data_dir = Path(account.user_data_dir)
        logger.info(f"[xiaohongshu] 开始登录 account={account.id}")
        try:
            with browser_context(
                user_data_dir,
                headless=False,
                account_id=account.id,
                platform="xiaohongshu",
            ) as page:
                page.goto(sel.LOGIN_URL, wait_until="domcontentloaded")
                deadline = time.time() + 300
                while time.time() < deadline:
                    if sel.LOGGED_IN_URL_FRAGMENT in page.url:
                        try:
                            sidebar = page.get_by_text(sel.SIDEBAR_MARKER_TEXT, exact=True).first
                            if sidebar.count() and sidebar.is_visible():
                                logger.info(f"[xiaohongshu] 登录成功 account={account.id}")
                                return True
                        except Exception:
                            pass
                    time.sleep(2)
                logger.warning(f"[xiaohongshu] 登录超时 account={account.id}")
                return False
        except Exception as exc:
            logger.error(f"[xiaohongshu] 登录异常 account={account.id}: {exc}")
            return False

    def publish_one(
        self,
        task_id: int,
        *,
        dry_run: bool = False,
        settings: Settings,
    ) -> PublishResult:
        return run_publish(task_id, dry_run=dry_run, settings=settings, spec=XIAOHONGSHU_SPEC)
