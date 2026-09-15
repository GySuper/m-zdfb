# ruff: noqa: RUF001
"""淘宝光合平台发布实现 — patchright 驱动,iframe 内操作。

只负责浏览器交互(打开页 → 填表 → 点发布)。claim / DB 状态机 / 通知 / 飞书回写
等无差别 plumbing 全在 `wxsp/platforms/runner.py` 的共享编排器里。
"""

from __future__ import annotations

import json as _json
import random
import re
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlsplit

from loguru import logger
from patchright.sync_api import Error as PWError
from patchright.sync_api import FrameLocator, Locator, Page, expect
from patchright.sync_api import TimeoutError as PWTimeoutError

import wxsp.apc
from wxsp.browser import browser_context
from wxsp.config import Settings
from wxsp.errors import (
    CookieExpired,
    ElementNotFound,
    NetworkError,
    ProductNotFound,
    ProductSelectionFailed,
    TopicNotFound,
    UploadFailed,
)
from wxsp.models import Account
from wxsp.platforms import taobao_selectors as sel
from wxsp.platforms.base import PlatformSpec, PublishContext, PublishResult, TaskBundle
from wxsp.platforms.runner import random_pause, run_publish, screenshot


def _iframe(page: Page) -> FrameLocator:
    return page.frame_locator(sel.IFRAME_SELECTOR)


_ELEMENT_RETRY_ATTEMPTS = 3
_ELEMENT_RETRY_DELAY_SECONDS = 1.5
_CONTROL_TIMEOUT_MS = 15_000
_COVER_WAIT_TIMEOUT_SECONDS = 180
_COVER_POLL_INTERVAL_SECONDS = 1
_PRODUCT_SELECT_TIMEOUT_SECONDS = 3
_SCHEDULE_OPEN_TIMEOUT_MS = 3_000
_T = TypeVar("_T")


def _with_element_retry(page: Page, step: str, action: Callable[[], _T]) -> _T:
    """只用于可重复执行的准备步骤,不关闭业务弹窗或重放提交。"""
    for attempt in range(1, _ELEMENT_RETRY_ATTEMPTS + 1):
        try:
            return action()
        except (ElementNotFound, PWTimeoutError, NetworkError) as exc:
            if attempt == _ELEMENT_RETRY_ATTEMPTS:
                raise
            logger.warning(
                f"[taobao] 步骤 {step} 元素暂不可用,准备重试 "
                f"({attempt}/{_ELEMENT_RETRY_ATTEMPTS - 1}): {exc}"
            )
            time.sleep(_ELEMENT_RETRY_DELAY_SECONDS)
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# step functions [3]-[15]
# ---------------------------------------------------------------------------


def _open_publish_page(page: Page) -> None:
    # 必须模拟用户操作路径:首页 → hover"发布作品" → 点"发视频"
    # 直接跳到 pubNew/video 不带 pub_url 参数会报"URL不合法"
    page.goto(sel.CREATOR_HOME, wait_until="domcontentloaded")
    if sel.LOGIN_URL_FRAGMENT in page.url:
        raise CookieExpired("淘宝登录态失效,需重新登录")
    # hover "发布作品" 出现下拉菜单
    page.get_by_text(sel.PUBLISH_DROPDOWN_TRIGGER).hover()
    page.get_by_role("menuitem", name=sel.PUBLISH_VIDEO_MENU_ITEM, exact=True).click(timeout=30_000)
    # 等待发布页 iframe 加载
    try:
        _iframe(page).locator(sel.LOGGED_IN_INDICATOR).wait_for(timeout=30_000)
    except PWTimeoutError as err:
        raise NetworkError("发布页加载超时") from err


def _verify_logged_in(page: Page) -> None:
    if sel.LOGIN_URL_FRAGMENT in page.url:
        raise CookieExpired("淘宝登录态失效，需重新登录")
    try:
        _iframe(page).locator(sel.LOGGED_IN_INDICATOR).wait_for(timeout=30_000)
    except PWTimeoutError as err:
        raise NetworkError("发布表单加载超时") from err


def _upload_video(page: Page, file_path: Path, timeout_seconds: int = 600) -> None:
    """上传视频并等待处理完成。

    实测 DOM 状态机(2026-07-10):
      - 上传中:body 含"等待视频上传..."(带省略号);"重新上传"按钮不存在
      - 成功:"等待视频上传..."消失,"重新上传"按钮出现
      - 失败:"视频上传失败"出现(此时"重新上传"也可能在,但失败文案优先)
    """
    iframe = _iframe(page)
    iframe.locator(sel.FILE_INPUT).set_input_files(str(file_path))
    waiting = iframe.locator(f'text="{sel.UPLOAD_WAITING_TEXT}"')
    failed = iframe.locator(f'text="{sel.UPLOAD_FAILED_TEXT}"')
    done = iframe.locator(f'text="{sel.COVER_READY_INDICATOR}"')

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if failed.is_visible():
            raise UploadFailed("视频上传失败(平台拒绝该文件)")
        if not waiting.is_visible() and done.is_visible():
            logger.info("[taobao] 视频上传/处理完成")
            return
        time.sleep(3)
    raise UploadFailed("视频上传/处理超时")


def _wait_cover_generated(page: Page) -> None:
    """等待主封面图片完成加载,最长 3 分钟。"""
    iframe = _iframe(page)
    cover = iframe.locator(sel.COVER_IMG_PREVIEW).first
    deadline = time.monotonic() + _COVER_WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            if cover.is_visible() and cover.evaluate(
                "img => Boolean(img.complete && img.naturalWidth > 0 && img.naturalHeight > 0)"
            ):
                logger.info("[taobao] 视频主封面已加载")
                return
        except PWTimeoutError:
            pass
        time.sleep(min(_COVER_POLL_INTERVAL_SECONDS, max(0, deadline - time.monotonic())))
    raise UploadFailed(f"视频封面生成或加载超时({_COVER_WAIT_TIMEOUT_SECONDS:g}s)")


def _fill_title(page: Page, title: str) -> None:
    if not title:
        return
    iframe = _iframe(page)
    inp = iframe.locator(sel.TITLE_INPUT)
    inp.click()
    inp.fill(title[: sel.TITLE_MAX_LENGTH])


def _fill_description(page: Page, description: str | None) -> None:
    if not description:
        return
    iframe = _iframe(page)
    # 苍颉不是 contenteditable;先确认专用 textarea 获得焦点再替换全文。
    editor = iframe.locator(sel.DESCRIPTION_EDITOR).first
    editor.click()
    try:
        expect(iframe.locator(sel.DESCRIPTION_INPUT)).to_be_focused(timeout=_CONTROL_TIMEOUT_MS)
    except AssertionError as err:
        raise ElementNotFound("描述编辑器未获得焦点") from err
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.press("Backspace")
    page.keyboard.type(description[:1000])


def _add_topic(page: Page, topic_name: str | None) -> None:
    if not topic_name:
        return
    iframe = _iframe(page)
    iframe.locator(sel.TOPIC_CLICK_AREA).click()
    dialog = iframe.locator(sel.TOPIC_DIALOG)
    dialog.wait_for(timeout=_CONTROL_TIMEOUT_MS)
    dialog.locator(sel.TOPIC_SEARCH_INPUT).fill(topic_name)
    dialog.locator(sel.TOPIC_SEARCH_BUTTON).click()
    card = dialog.locator(sel.TOPIC_CARD).filter(
        has=iframe.locator(sel.TOPIC_TITLE).filter(
            has_text=re.compile(f"^{re.escape(topic_name)}$")
        )
    )
    try:
        card.wait_for(timeout=_CONTROL_TIMEOUT_MS)
    except PWTimeoutError as err:
        raise TopicNotFound(f"话题 '{topic_name}' 搜索无结果") from err
    if sel.TOPIC_SELECTED_CLASS not in (card.get_attribute("class") or ""):
        card.click()
    try:
        expect(card).to_have_class(
            re.compile(sel.TOPIC_SELECTED_CLASS), timeout=_CONTROL_TIMEOUT_MS
        )
    except AssertionError as err:
        raise TopicNotFound(f"话题 '{topic_name}' 选择未生效") from err
    dialog.locator(sel.TOPIC_CONFIRM_BUTTON).click()
    dialog.wait_for(state="hidden", timeout=_CONTROL_TIMEOUT_MS)


_MAX_PRODUCTS = 6


def _is_product_checkbox_selected(checkbox: Locator) -> bool:
    """淘宝 Fusion 受控 checkbox 会在点击后替换 DOM 节点。"""
    return checkbox.is_checked() or checkbox.get_attribute("aria-checked") == "true"


def _add_products(page: Page, product_ids: list[str]) -> None:
    if not product_ids:
        return
    ids = list(dict.fromkeys(product_ids))[:_MAX_PRODUCTS]
    if len(product_ids) > _MAX_PRODUCTS:
        logger.warning(
            f"[taobao] 商品数 {len(product_ids)} 超出上限 {_MAX_PRODUCTS},"
            f" 仅取前 {_MAX_PRODUCTS} 个: {ids}"
        )
    iframe = _iframe(page)
    iframe.locator(sel.PRODUCT_TRIGGER).click()
    dialog = iframe.locator(sel.PRODUCT_DIALOG)
    dialog.wait_for(timeout=_CONTROL_TIMEOUT_MS)
    for pid in ids:
        if not pid.isascii() or not pid.isdigit():
            raise ProductNotFound(f"商品ID '{pid}' 必须为数字")
        search = dialog.locator(sel.PRODUCT_SEARCH_INPUT)
        search.fill(pid)
        dialog.locator(sel.PRODUCT_SEARCH_BUTTON).click()

        # 结果以商品卡片呈现(不是每商品一个可见 checkbox)。按卡片标题链接 href 里的
        # 商品ID 精确定位 —— 搜不到 → 链接永不出现 → 判 ProductNotFound。
        link = dialog.locator(sel.PRODUCT_ITEM_LINK_BY_ID.format(pid=pid)).first
        try:
            link.wait_for(state="visible", timeout=_CONTROL_TIMEOUT_MS)
        except PWTimeoutError as err:
            raise ProductNotFound(f"商品ID '{pid}' 搜索无结果") from err

        # 上溯到卡片容器,勾选卡片内"商品选择"复选框(hover 才显,区别于顶部"筛选"复选框)。
        # 先 hover 卡片让复选框显出,再直接点 input(opacity:0 盖在方框上,是真正接收点击的元素)。
        card = link.locator(sel.PRODUCT_ITEM_CARD_ANCESTOR)
        card.hover()
        checkbox = card.locator(sel.PRODUCT_ITEM_SELECT_CHECKBOX_INPUT).first

        # 不使用 locator.check(): Fusion 点击后会异步替换 input,check() 对旧节点做
        # 原生 checked 断言会误报失败。click 后通过 locator 重新解析当前节点状态。
        try:
            if not _is_product_checkbox_selected(checkbox):
                checkbox.click(timeout=_CONTROL_TIMEOUT_MS)
            deadline = time.monotonic() + _PRODUCT_SELECT_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if _is_product_checkbox_selected(checkbox):
                    break
                time.sleep(0.2)
            else:
                raise ProductSelectionFailed(f"商品ID '{pid}' 已搜到但勾选未生效")
        except PWError as err:
            raise ProductSelectionFailed(f"商品ID '{pid}' 已搜到但勾选失败") from err
        logger.info(f"[taobao] 选中商品 pid={pid}")
    dialog.locator(sel.PRODUCT_CONFIRM_BUTTON).click()
    dialog.wait_for(state="hidden", timeout=_CONTROL_TIMEOUT_MS)


def _open_schedule_picker(combo: Locator, picker: Locator) -> None:
    """打开日期面板,已展开或点击期间展开时都不重复点击。"""
    if combo.get_attribute("aria-expanded") != "true":
        try:
            combo.click(timeout=_SCHEDULE_OPEN_TIMEOUT_MS)
        except PWTimeoutError:
            # 点击期间 DatePicker 可能已经打开并遮住输入框,这种情况无需再点。
            if combo.get_attribute("aria-expanded") != "true":
                raise
    picker.wait_for(timeout=_CONTROL_TIMEOUT_MS)


def _set_schedule(page: Page, publish_at: datetime) -> None:
    iframe = _iframe(page)
    # radio 与提交按钮同名,直接定位 radio input,避免文本选择器歧义。
    iframe.locator(sel.SCHEDULE_RADIO).check(timeout=_CONTROL_TIMEOUT_MS)
    combo = iframe.locator(sel.SCHEDULE_COMBOBOX)
    picker = iframe.locator(sel.SCHEDULE_PICKER_OVERLAY)
    _open_schedule_picker(combo, picker)
    date_input = picker.locator(sel.SCHEDULE_DATE_INPUT)
    target_date = publish_at.strftime("%Y/%m/%d")
    target_time = publish_at.strftime("%H:%M")
    if not combo.input_value().startswith(f"{target_date} "):
        date_input.fill(target_date)
        date_input.press("Enter")
    # 日期提交会重置时间,等组合框反映目标日期后再填时间。
    try:
        expect(combo).to_have_value(re.compile(f"^{target_date} "), timeout=_CONTROL_TIMEOUT_MS)
    except AssertionError as err:
        raise ElementNotFound(f"定时日期未生效: {target_date}") from err
    time_input = picker.locator(sel.SCHEDULE_TIME_INPUT)
    time_input.fill(target_time)
    time_input.press("Enter")
    picker.locator(sel.SCHEDULE_CONFIRM).click()
    picker.wait_for(state="hidden", timeout=_CONTROL_TIMEOUT_MS)
    _verify_schedule(page, publish_at)


def _verify_schedule(page: Page, publish_at: datetime) -> None:
    """按钮文案不代表日期有效;提交前必须核对整个定时状态。"""
    iframe = _iframe(page)
    target = publish_at.strftime("%Y/%m/%d %H:%M")
    try:
        expect(iframe.locator(sel.SCHEDULE_RADIO)).to_be_checked(timeout=_CONTROL_TIMEOUT_MS)
        expect(iframe.locator(sel.SCHEDULE_COMBOBOX)).to_be_enabled(timeout=_CONTROL_TIMEOUT_MS)
        expect(iframe.locator(sel.SCHEDULE_COMBOBOX)).to_have_value(
            target, timeout=_CONTROL_TIMEOUT_MS
        )
        button = iframe.locator(sel.SUBMIT_BUTTON_SCHEDULED)
        expect(button).to_be_visible(timeout=_CONTROL_TIMEOUT_MS)
        expect(button).to_be_enabled(timeout=_CONTROL_TIMEOUT_MS)
    except AssertionError as err:
        raise ElementNotFound(f"定时发布状态未就绪,目标时间: {target}") from err


def _prepare_publish(page: Page, publish_at: datetime) -> None:
    try:
        _verify_schedule(page, publish_at)
    except ElementNotFound:
        logger.warning("[taobao] 提交前定时状态发生变化,重新设置")
        _set_schedule(page, publish_at)


_DECLARATION_SELECTORS = {
    "内容无需标注": sel.DECLARATION_RADIO_MAP["内容无需标注"],
    "含AI生成内容": sel.DECLARATION_RADIO_MAP["含AI生成内容"],
    "含虚构演绎内容": sel.DECLARATION_RADIO_MAP["含虚构演绎内容"],
    "内容为转载": sel.DECLARATION_RADIO_MAP["内容为转载"],
    "个人观点，仅供参考": sel.DECLARATION_RADIO_MAP["个人观点，仅供参考"],
    "内容含营销信息": sel.DECLARATION_RADIO_MAP["内容含营销信息"],
}


def _set_declaration(page: Page, declaration: str | None) -> None:
    iframe = _iframe(page)
    choice = declaration or "内容无需标注"
    radio_sel = _DECLARATION_SELECTORS.get(choice)
    if radio_sel is None:
        logger.warning(f"[taobao] 未知创作者声明 '{choice}'，使用默认")
        radio_sel = _DECLARATION_SELECTORS["内容无需标注"]
    # text= 选中 label 文字,需要点里面的 input[type=radio]
    iframe.locator(radio_sel).locator("..").locator('input[type="radio"]').check(
        timeout=_CONTROL_TIMEOUT_MS
    )


def _toggle_ai_optimize(page: Page, on: bool) -> None:
    """把"AI优化"开关设为目标状态。

    平台默认**开启**,不能靠盲点 —— 必须先读当前 aria-checked,只在与目标不一致时才点
    (盲点会把默认开的关掉,或把该关的留在开)。
    """
    iframe = _iframe(page)
    switch = iframe.locator(sel.AI_TOGGLE_SWITCH)
    switch.wait_for(state="visible", timeout=_CONTROL_TIMEOUT_MS)
    if (switch.get_attribute("aria-checked") == "true") == on:
        return  # 已是目标态
    switch.click()
    try:
        expect(switch).to_have_attribute(
            "aria-checked", str(on).lower(), timeout=_CONTROL_TIMEOUT_MS
        )
    except AssertionError as err:
        raise ElementNotFound(f"AI优化开关未切换到目标状态: {on}") from err


def _click_publish(page: Page) -> None:
    iframe = _iframe(page)
    scheduled = iframe.locator(sel.SUBMIT_BUTTON_SCHEDULED)
    # 只点"定时发布",不 fallback 到"立即发布"(防止时间没填就发出去)
    scheduled.click(timeout=_CONTROL_TIMEOUT_MS)


def _is_success_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.hostname == "creator.guanghe.taobao.com"
        and parsed.path.rstrip("/") == sel.SUCCESS_URL_FRAGMENT
    )


def _wait_for_success_indicator(page: Page, timeout: int = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # 只接受发布后的作品管理页,登录/错误/风控等其它跳转不能算成功。
        if _is_success_url(page.url):
            logger.info("[taobao] 已跳转作品管理页，发布成功")
            return
        for indicator in sel.SUCCESS_INDICATORS:
            try:
                if page.locator(f'text="{indicator}"').first.is_visible():
                    return
            except Exception:
                pass
            try:
                iframe = _iframe(page)
                if iframe.locator(f'text="{indicator}"').first.is_visible():
                    return
            except Exception:
                pass
        time.sleep(2)
    if _is_success_url(page.url):
        logger.info("[taobao] 已跳转作品管理页，发布成功")
        return
    raise ElementNotFound("发布成功判定超时")


# ---------------------------------------------------------------------------
# 平台步骤回调 + Spec + Publisher
# ---------------------------------------------------------------------------


def _pre_publish(page: Page, bundle: TaskBundle, staged: Path, ctx: PublishContext) -> None:
    """[3]-[14] 打开页 → 上传 → 填表 → 商品 → 声明/AI → 定时。

    视频本体由编排器已 stage 好传进来(淘宝无独立封面文件,封面由平台自动生成)。
    """
    step_pause = ctx.step_pause
    upload_timeout = ctx.settings.publisher.upload_timeout_seconds

    # 商品 ID:优先 product_ids_json;迁移兼容 —— 为空时回退旧版存放处 tags_json
    product_ids_raw = (
        bundle.product_ids_json
        if bundle.product_ids_json and bundle.product_ids_json != "[]"
        else None
    )
    if product_ids_raw is None:
        product_ids_raw = (
            bundle.tags_json if bundle.tags_json and bundle.tags_json != "[]" else None
        )

    # APC 守门(对齐 tencent §3.3 注入点):dev-mode 永远 True;打包模式看 APC 判决
    apc_passed = wxsp.apc.check_pass()

    ctx.last_step = "open"
    _with_element_retry(page, "open", lambda: _open_publish_page(page))
    random_pause(step_pause)

    ctx.last_step = "login"
    _with_element_retry(page, "login", lambda: _verify_logged_in(page))
    random_pause(step_pause)

    # APC 拒绝时装"等待上传区域超时"故障(对齐 tencent §3.3)
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

    ctx.last_step = "cover"
    _wait_cover_generated(page)
    random_pause(step_pause)

    ctx.last_step = "title"
    _with_element_retry(page, "title", lambda: _fill_title(page, title=bundle.title))
    random_pause(step_pause)

    ctx.last_step = "desc"
    _fill_description(page, description=bundle.description)
    random_pause(step_pause)

    ctx.last_step = "topic"
    _add_topic(page, topic_name=bundle.topic)
    random_pause(step_pause)

    ctx.last_step = "products"
    product_ids_list: list[str] = []
    if product_ids_raw:
        try:
            parsed = _json.loads(product_ids_raw)
            if isinstance(parsed, list):
                product_ids_list = [str(p) for p in parsed if p]
        except (TypeError, _json.JSONDecodeError):
            logger.warning(
                f"[taobao] 商品 ID JSON 解析失败 task_id={ctx.task_id}:"
                f" {product_ids_raw!r},跳过商品"
            )
    if product_ids_list:
        _add_products(page, product_ids=product_ids_list)
    random_pause(step_pause)

    ctx.last_step = "declaration"
    _with_element_retry(
        page,
        "declaration",
        lambda: _set_declaration(page, declaration=bundle.declaration),
    )
    random_pause(step_pause)

    ctx.last_step = "ai"
    _with_element_retry(
        page,
        "ai",
        lambda: _toggle_ai_optimize(page, on=bool(bundle.ai_optimize)),
    )
    random_pause(step_pause)

    ctx.last_step = "schedule"
    _with_element_retry(page, "schedule", lambda: _set_schedule(page, publish_at=bundle.publish_at))


def _post_publish(page: Page, bundle: TaskBundle, ctx: PublishContext) -> None:
    """[15]-[16] 点定时发布 → 等跳转判成功。淘宝不抽取 remote_url(到点前无公开链接)。"""
    ctx.last_step = "publish"
    _with_element_retry(page, "publish_prepare", lambda: _prepare_publish(page, bundle.publish_at))
    _click_publish(page)

    ctx.last_step = "wait_success"
    _wait_for_success_indicator(page)


TAOBAO_SPEC = PlatformSpec(
    platform_key="taobao_guanghe",
    display_name="淘宝光合",
    pre_publish=_pre_publish,
    post_publish=_post_publish,
)


class TaobaoGuanghePublisher:
    platform_key = "taobao_guanghe"

    def login(self, account: Account) -> bool:
        """开浏览器导航到淘宝光合首页, 等用户手动登录。"""
        user_data_dir = Path(account.user_data_dir)
        logger.info(f"[taobao] 开始登录 account={account.id}")
        try:
            with browser_context(
                user_data_dir,
                headless=False,
                account_id=account.id,
                platform="taobao_guanghe",
            ) as page:
                page.goto(sel.CREATOR_HOME, wait_until="domcontentloaded")
                deadline = time.time() + 300
                while time.time() < deadline:
                    if sel.LOGIN_URL_FRAGMENT not in page.url:
                        logger.info(f"[taobao] 登录成功 account={account.id}")
                        return True
                    time.sleep(2)
                logger.warning(f"[taobao] 登录超时 account={account.id}")
                return False
        except Exception as exc:
            logger.error(f"[taobao] 登录异常 account={account.id}: {exc}")
            return False

    def publish_one(
        self,
        task_id: int,
        *,
        dry_run: bool = False,
        settings: Settings,
    ) -> PublishResult:
        return run_publish(task_id, dry_run=dry_run, settings=settings, spec=TAOBAO_SPEC)
