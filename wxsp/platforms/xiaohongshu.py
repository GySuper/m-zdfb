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
from typing import Any

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
from wxsp.human_input import (
    bring_browser_to_front,
    physical_click,
    physical_press,
    physical_scroll,
    physical_select_all,
    physical_type,
    physical_upload,
)
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
# 拟人节奏辅助(_wait_xhs 保留;打字/点击/轨迹已移到 wxsp.human_input 物理输入层)
# ---------------------------------------------------------------------------


def _get_random_int(min_: int, max_: int) -> int:
    """[min, max] 闭区间随机整数。"""
    return random.randint(min_, max_)


def _wait_xhs(page: Page, min_ms: int = 1500, max_ms: int = 4000) -> None:
    """步间随机停顿 ms。"""
    page.wait_for_timeout(_get_random_int(min_ms, max_ms))


# ---------------------------------------------------------------------------
# step functions
# ---------------------------------------------------------------------------


def _open_publish_page(page: Page) -> None:
    # 不能用 page.add_init_script:patchright 1.59.1 + 当前 Chromium 下,一旦调用
    # 后续 navigate 全部 ERR_CONNECTION_CLOSED(与 browser.py 指纹注入踩的是同一个坑,
    # 见 browser.py 第 297-299 行注释)。改用 framenavigated 事件:navigate 完成后立即
    # evaluate 覆写脚本。时序上 OK —— 发布按钮 <xhs-publish-btn> 要上传视频后才 mount,
    # goto 发布页那一刻 shadow 还没创建,framenavigated 后覆写 attachShadow 来得及。
    page.on("framenavigated", _inject_force_open_shadow)
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


def _inject_force_open_shadow(frame: Any) -> None:
    """framenavigated 回调:对主 frame 注入闭合 shadow 强开脚本(about:blank/cross-origin 忽略)。"""
    try:
        frame.evaluate(_FORCE_OPEN_SHADOW_JS)
    except Exception:
        pass


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
    # 混合上传:物理点击上传区(产生真实 click 事件 isTrusted=true)→ ESC 关系统文件
    # 对话框 → set_input_files 注入文件路径(不产生交互事件,避开对话框操作风险)。
    upload_area = page.locator(sel.VIDEO_UPLOAD_AREA).first
    file_input = page.locator(sel.VIDEO_FILE_INPUT).first
    physical_upload(page, upload_area, file_input, str(file_path))

    # 等上传/转码完成:「重新上传」按钮出现 = 完成(对齐抖音)。
    try:
        page.locator(sel.UPLOAD_DONE_MARKER).first.wait_for(
            state="visible", timeout=timeout_seconds * 1000
        )
        logger.info("[xiaohongshu] 视频上传完成")
    except Exception as err:
        raise UploadFailed("视频上传/处理超时") from err


def _fill_title(page: Page, title: str) -> None:
    if not title:
        return
    inp = page.locator(sel.TITLE_INPUT).first
    inp.wait_for(state="visible", timeout=10_000)
    # 物理三击全选 → 物理退格清空 → 物理逐字符输入(全部 isTrusted=true)
    physical_click(page, inp, click_count=3)
    physical_press("Backspace")
    title_text = title[: sel.TITLE_MAX_LENGTH]
    if title_text:
        physical_type(page, title_text)


def _fill_description(page: Page, description: str | None) -> None:
    if not description:
        return
    editor = page.locator(sel.DESC_EDITOR).first
    editor.wait_for(state="visible", timeout=10_000)
    # 物理双击聚焦 → 停顿 → 物理逐字符输入(全部 isTrusted=true)
    physical_click(page, editor, click_count=2)
    _wait_xhs(page, 1500, 2500)
    physical_type(page, description)


def _add_tags(page: Page, tags: list[str]) -> None:
    if not tags:
        return
    # 物理点击聚焦正文 → 物理逐字符输入 #tag → 物理回车选候选(全部 isTrusted=true)
    physical_click(page, page.locator(sel.DESC_EDITOR).first)
    for tag in tags:
        physical_type(page, "#" + tag)
        _wait_xhs(page)
        try:
            page.locator(sel.TOPIC_CONTAINER).wait_for(state="visible", timeout=3000)
            physical_press("Enter")
            _wait_xhs(page, 1500, 3000)
        except Exception:
            # 下拉没弹出 → 敲空格让 #tag 以纯文本留在正文
            physical_press("Space")
        _wait_xhs(page)


def _set_cover(page: Page, cover_path: Path | None) -> None:
    """有自定义封面 → 走封面弹窗上传(可选,best-effort)。"""
    if cover_path is None:
        return
    cover_title = page.locator(sel.COVER_PLUGIN_TITLE).filter(has_text=sel.COVER_PLUGIN_TITLE_TEXT)
    entry = cover_title.locator(sel.COVER_PREVIEW_ANCESTOR_XPATH).locator(sel.COVER_ENTRY_INNER)
    entry.first.wait_for(state="visible", timeout=30_000)
    # 物理点击封面入口(isTrusted=true)
    physical_click(page, entry.first)

    modal = page.locator(sel.COVER_MODAL)
    modal.wait_for(state="visible", timeout=30_000)
    file_input = modal.locator(sel.COVER_FILE_INPUT).first
    file_input.wait_for(state="attached", timeout=10_000)
    # 混合上传:物理点击触发对话框 → ESC 关 → set_input_files
    physical_upload(page, entry.first, file_input, str(cover_path))
    _wait_xhs(page, 1500, 2500)

    confirm = (
        modal.locator(sel.COVER_CONFIRM_BUTTON).filter(has_text=sel.COVER_CONFIRM_BUTTON_TEXT).first
    )
    confirm.wait_for(state="visible", timeout=10_000)
    physical_click(page, confirm)
    modal.wait_for(state="hidden", timeout=30_000)
    logger.info("[xiaohongshu] 自定义封面设置完成(best-effort)")


def _set_schedule(page: Page, publish_at: datetime) -> None:
    # 物理点击定时开关,verify=日期框出现(没点到就重试,防止人碰鼠标)→ 输入日期。
    switch_card = page.locator(sel.SCHEDULE_SWITCH_CARD).filter(
        has_text=sel.SCHEDULE_SWITCH_TEXT
    )
    switch = switch_card.locator(sel.SCHEDULE_SWITCH)
    inp = page.locator(sel.SCHEDULE_DATETIME_INPUT).first

    def _date_input_visible() -> bool:
        try:
            return inp.is_visible()
        except Exception:
            return False

    physical_click(page, switch, verify=_date_input_visible)
    _wait_xhs(page, 1500, 2500)

    try:
        inp.wait_for(state="visible", timeout=10_000)
    except Exception as err:
        logger.warning(f"[xiaohongshu] 定时发布日期框未找到(可能改版): {err}")
        return  # 定时设置失败不阻断发布(降级为即时发布)

    # 物理点击日期框聚焦 → Cmd/Ctrl+A 全选 → 物理输入覆盖
    physical_click(page, inp)
    _wait_xhs(page, 400, 800)
    physical_select_all()
    physical_type(page, publish_at.strftime(sel.SCHEDULE_DATETIME_FORMAT))
    _wait_xhs(page, 1500, 2500)


def _risk_control_probe(page: Page) -> None:
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return
    for kw in sel.RISK_CONTROL_KEYWORDS:
        if kw in body_text:
            raise RiskControl(f"页面命中风控关键词: {kw}")


def _click_publish(page: Page) -> None:
    # 物理点击发布按钮,verify=按钮消失(发布动作生效),没点到就重试。
    btn = page.locator(sel.PUBLISH_BUTTON).first
    btn.wait_for(state="visible", timeout=10_000)

    def _btn_gone() -> bool:
        try:
            return not btn.is_visible()
        except Exception:
            return True

    physical_click(page, btn, delay_ms=80, verify=_btn_gone)
    _wait_xhs(page, 2500, 4500)


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

    # pyautogui 物理输入要求窗口在前台:确保 Chrome 最前+最大化,否则屏幕坐标会点飞
    bring_browser_to_front(page)

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
    """点定时发布 → 等跳成功页 → 社区浏览 12-30s 再退出(打散"发完即走"机械模式)。"""
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
