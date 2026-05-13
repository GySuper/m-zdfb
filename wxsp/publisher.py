"""视频号发布核心 —— 20 步串行,patchright 驱动(M5)。"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger
from patchright.sync_api import Page

from wxsp import selectors as sel
from wxsp.errors import (
    CookieExpired,
    ElementNotFound,
    NetworkError,
    RiskControl,
    UploadFailed,
    VideoInvalid,
)


class AlreadyClaimed(Exception):
    """task 已被其它 worker 占用 / 不在可执行状态 —— claim_task 返回 False。"""


@dataclass
class PublishResult:
    task_id: int
    ok: bool
    dry_run: bool
    remote_url: str | None = None
    remote_video_id: str | None = None
    error_type: str | None = None
    error_msg: str | None = None
    screenshots: list[str] = field(default_factory=list)


def screenshot(
    page: Page,
    *,
    task_id: int,
    step: str,
    screenshots_root: Path,
    now: datetime | None = None,
) -> Path:
    """保存截图到 `screenshots_root/{YYYYMM}/{task_id}_{step}.png`,返回路径。

    `screenshots_root` 通常是 `logs/screenshots`(由 settings.app.logs_dir 派生);
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
    """步骤间 1-3 秒随机停顿(模拟人工);`sleep` 注入用于测试。"""
    low, high = range_seconds
    sleep(random.uniform(low, high))


# ============== 步骤函数(每个 1 个小函数) ==============
# 设计原则:
#   - 函数体 ≤ 20 行;复杂多选 fallback 用循环 + try/except continue
#   - 选择器全部从 wxsp.selectors 取(改版时唯一改动点)
#   - 失败抛 PublisherError 子类,让 publish() 顶层 classify + 截图


def open_publish_page(page: Page, *, timeout_ms: int = 30_000) -> None:
    """[3] 打开发布页,等待 DOM ready。"""
    try:
        page.goto(sel.PUBLISH_PAGE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        raise NetworkError(f"打开发布页失败: {exc}") from exc


def verify_logged_in(page: Page, *, timeout_ms: int = 15_000) -> None:
    """[4] 任一登录标记可见 → 通过;否则 → CookieExpired。"""
    # 先看是否有扫码框(强信号:未登录)
    for selector in sel.LOGIN_QRCODE_SELECTORS:
        if page.locator(selector).first.is_visible():
            raise CookieExpired("发布页跳出扫码二维码,cookie 已失效")
    # 再看登录后元素
    joined = ", ".join(sel.LOGGED_IN_SELECTORS)
    try:
        page.wait_for_selector(joined, timeout=timeout_ms, state="visible")
    except Exception as exc:
        raise CookieExpired(f"未找到登录后标记元素: {exc}") from exc


def upload_video(page: Page, *, file_path: Path, timeout_seconds: int) -> None:
    """[5] set_input_files + 轮询发表按钮变为可点击。

    timeout_seconds 通常取 settings.publisher.upload_timeout_seconds(默认 600)。
    """
    try:
        page.locator(sel.FILE_INPUT).set_input_files(str(file_path))
    except Exception as exc:
        raise UploadFailed(f"set_input_files 失败: {exc}") from exc

    deadline = time.monotonic() + timeout_seconds
    role_name, role_text = sel.UPLOAD_PUBLISH_BUTTON_ROLE
    while time.monotonic() < deadline:
        # 上传失败兜底
        if (
            page.locator(sel.UPLOAD_FAILED_INDICATOR).count()
            and page.locator(sel.UPLOAD_DELETE_TAG).count()
        ):
            raise UploadFailed("页面提示上传失败")
        # 发表按钮 class 不含 disabled → 上传完成
        publish_button = page.get_by_role(role_name, name=role_text)  # type: ignore[arg-type]
        cls = publish_button.get_attribute("class") if publish_button.count() else None
        if cls and sel.UPLOAD_DISABLED_CLASS not in cls:
            return
        time.sleep(2)
    raise UploadFailed(f"上传 {timeout_seconds}s 后仍未完成")


def fill_title(page: Page, *, title: str) -> None:
    """[6] 点 .input-editor → type 标题 → 回车换行。"""
    editor = page.locator(sel.TITLE_EDITOR)
    editor.click()
    page.keyboard.type(title)
    page.keyboard.press("Enter")


def fill_description(page: Page, *, description: str | None) -> None:
    """[7] 接着 [6] 的换行继续 type description。"""
    if not description:
        return
    page.keyboard.type(description)


def add_tags(page: Page, *, tags: list[str]) -> None:
    """[8] 每个 tag 前加 # + 后加空格,继续在 .input-editor 输入。"""
    for tag in tags:
        page.keyboard.type(f"#{tag}")
        page.keyboard.press("Space")


def set_cover(page: Page, *, cover_path: Path | None) -> None:
    """[9] 设置自定义封面。cover_path is None → 跳过。

    参考 tencent_uploader/main.py::set_thumbnail,简化版:
    选 cover entry → 等弹窗 → 上传 → 裁剪确认 → 主弹窗确认。
    """
    if cover_path is None:
        return

    # 点 cover entry(三个选择器 fallback)
    entry_clicked = False
    for selector in sel.COVER_ENTRY_SELECTORS:
        locator = page.locator(selector).first
        if not locator.count():
            continue
        try:
            locator.wait_for(state="visible", timeout=3000)
            locator.click()
            entry_clicked = True
            break
        except Exception:
            continue
    if not entry_clicked:
        raise ElementNotFound("没找到可点击的封面入口")

    page.wait_for_timeout(500)
    dialog = (
        page.locator("div.weui-desktop-dialog").filter(has_text=sel.COVER_DIALOG_HAS_TEXT).first
    )
    if not dialog.count():
        # 没弹窗就跳过(参考实现的容错)
        return
    dialog.wait_for(state="visible", timeout=5000)

    file_input = dialog.locator(sel.COVER_FILE_INPUT).first
    file_input.wait_for(state="attached", timeout=10_000)
    file_input.set_input_files(str(cover_path))
    page.wait_for_timeout(1000)

    crop_dialog = (
        page.locator("div.weui-desktop-dialog")
        .filter(has_text=sel.COVER_CROP_DIALOG_HAS_TEXT)
        .first
    )
    if crop_dialog.count():
        try:
            crop_dialog.wait_for(state="visible", timeout=10_000)
            confirm = crop_dialog.locator(sel.COVER_CROP_CONFIRM).first
            if confirm.count():
                confirm.wait_for(state="visible", timeout=5000)
                confirm.click()
                page.wait_for_timeout(1000)
        except Exception as exc:
            logger.warning(f"封面裁剪确认失败,继续主弹窗: {exc}")

    main_confirm = dialog.locator(sel.COVER_CONFIRM).first
    main_confirm.wait_for(state="visible", timeout=10_000)
    main_confirm.click()


def bind_topic(page: Page, *, topic: str | None) -> None:
    """[10] 绑定合集。topic is None → 跳过。

    展开下拉 → 优先按名字匹配;找不到点第一个(参考实现行为)。
    """
    if not topic:
        return
    label = page.get_by_text(sel.COLLECTION_LABEL_TEXT)
    options_wrap = label.locator("xpath=following-sibling::div")
    options = options_wrap.locator(".option-list-wrap > div")
    if options.count() <= 1:
        return  # 平台没合集可选
    options_wrap.click()
    matched = options.filter(has_text=topic).first
    target = matched if matched.count() else options.first
    target.click()


def toggle_original(page: Page, *, original_claim: bool) -> None:
    """[11] 勾"视频为原创" + 条款 + 声明按钮(若可见)。"""
    if not original_claim:
        return
    if page.get_by_label(sel.ORIGINAL_CHECKBOX_LABEL).count():
        page.get_by_label(sel.ORIGINAL_CHECKBOX_LABEL).check()
    try:
        terms_visible = page.locator(f'label:has-text("{sel.ORIGINAL_TERMS_LABEL}")').is_visible()
    except Exception:
        terms_visible = False
    if terms_visible:
        page.get_by_label(sel.ORIGINAL_TERMS_LABEL).check()
        page.get_by_role("button", name=sel.ORIGINAL_DECLARE_BUTTON).click()


def set_schedule(page: Page, *, publish_at: datetime) -> None:
    """[12] 切到"定时" → 选日期 → 输入小时数(分钟交给平台 0)。

    publish_at ∈ [now+30min, now+14d];超出 raise VideoInvalid
    (validator 已挡了一道,这里只是兜底)。
    """
    now = datetime.now()
    if publish_at < now + timedelta(minutes=30) or publish_at > now + timedelta(days=14):
        raise VideoInvalid(f"publish_at={publish_at} 超出 [now+30min, now+14d](validator 该挡未挡)")

    # 切到"定时"
    radio = page.locator("label").filter(has_text=sel.SCHEDULE_RADIO_LABEL_HAS_TEXT).nth(1)
    radio.click()
    page.click(sel.SCHEDULE_DATE_INPUT)

    # 翻月(只翻到目标月,不处理跨年场景 —— validator 14d 上限本来就在 1 个月内可达)
    target_month = publish_at.strftime("%m月")
    page_month = page.inner_text(sel.SCHEDULE_MONTH_LABEL)
    if page_month != target_month:
        page.click(sel.SCHEDULE_NEXT_MONTH_BTN)

    # 选日
    for element in page.query_selector_all(sel.SCHEDULE_DAY_TABLE):
        if sel.SCHEDULE_DAY_DISABLED_CLASS in (element.evaluate("el => el.className") or ""):
            continue
        if (element.inner_text() or "").strip() == str(publish_at.day):
            element.click()
            break

    # 输入小时(平台默认分钟=00,参考实现也是只输小时)
    page.click(sel.SCHEDULE_TIME_INPUT)
    page.keyboard.press("Control+KeyA")
    page.keyboard.type(publish_at.strftime("%H"))
    page.locator(sel.TITLE_EDITOR).click()  # 点别处收起时间选择器


def risk_control_probe(page: Page) -> None:
    """[13] 扫页面 body 文本,任一风控关键词命中 → RiskControl。"""
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return  # 拿不到文本就跳过(谨慎)
    for kw in sel.RISK_CONTROL_KEYWORDS:
        if kw in body_text:
            raise RiskControl(f"页面命中风控关键词: {kw}")


def click_publish(page: Page) -> None:
    """[15] 点"发表"按钮。"""
    button = page.locator(sel.SUBMIT_PUBLISH_BUTTON)
    if not button.count():
        raise ElementNotFound("发表按钮未找到")
    button.click()


def wait_for_success_indicator(page: Page, *, timeout_ms: int = 30_000) -> None:
    """[16] 等 URL 跳到 /post/list,认为发布成功。"""
    try:
        page.wait_for_url(lambda url: sel.POST_LIST_URL_FRAGMENT in url, timeout=timeout_ms)
    except Exception as exc:
        # 兜底:看 URL 是否已经包含
        if sel.POST_LIST_URL_FRAGMENT not in page.url:
            raise NetworkError(f"发布后未跳到 post/list: {exc}") from exc


def extract_remote_video_id_and_url(page: Page) -> tuple[str | None, str | None]:
    """[17] 尽力从 post/list 首条提取 remote_video_id + URL。

    定时发布的视频在到点前**不会**有公开 URL,拿不到是常态 —— 返回 (None, None)
    让上层把 remote_url 留 None,不是错误。
    """
    try:
        link = page.locator(sel.LIST_FIRST_ITEM_LINK).first
        if not link.count():
            return None, None
        href = link.get_attribute("href", timeout=2000)
        if not href:
            return None, None
        # 视频号详情页 URL 形如 .../platform/post/finderNewLifeData?vid=xxx 或 ?id=xxx
        # 不严格解析,直接拿 href 末段当 video_id
        vid = href.rsplit("=", 1)[-1] if "=" in href else None
        return vid, href
    except Exception as exc:
        logger.info(f"提取 remote_url 失败(对定时发布是常态): {exc}")
        return None, None
