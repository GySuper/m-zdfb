"""视频号发布核心 —— patchright 驱动(M5)。

只负责浏览器交互(打开页 → 填表 → 点发布)。claim / DB 状态机 / 通知 / 飞书回写
等无差别 plumbing 全在 `wxsp/platforms/runner.py` 的共享编排器里。
"""

from __future__ import annotations

import json as _json
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from loguru import logger
from patchright.sync_api import Page

import wxsp.apc
from wxsp.config import Settings
from wxsp.errors import (
    CookieExpired,
    ElementNotFound,
    NetworkError,
    RiskControl,
    UploadFailed,
    VideoInvalid,
)
from wxsp.models import Account
from wxsp.nas import stage_to_tmp
from wxsp.platforms import tencent_selectors as sel
from wxsp.platforms.base import PlatformSpec, PublishContext, PublishResult, TaskBundle
from wxsp.platforms.runner import random_pause, run_publish, screenshot

# ============== 步骤函数(每个 1 个小函数) ==============
# 设计原则:
#   - 函数体 ≤ 20 行;复杂多选 fallback 用循环 + try/except continue
#   - 选择器全部从 tencent_selectors 取(改版时唯一改动点)
#   - 失败抛 PublisherError 子类,让 publish_one() 顶层 classify + 截图


def open_publish_page(page: Page, *, timeout_ms: int = 30_000) -> None:
    """[3] 打开发布页,等待 DOM ready。"""
    try:
        page.goto(sel.PUBLISH_PAGE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        raise NetworkError(f"打开发布页失败: {exc}") from exc


def verify_logged_in(page: Page, *, timeout_ms: int = 15_000) -> None:
    """[4] 任一登录标记可见 → 通过;否则 → CookieExpired。

    QR 命中时把 selector + 当前 page.url 一起写进异常 msg + 日志,
    便于事后判断这是真登录页还是发布页上某个无关 QR 元素被误判。
    """
    # 先看是否有扫码框(强信号:未登录)
    for selector in sel.LOGIN_QRCODE_SELECTORS:
        if page.locator(selector).first.is_visible():
            url = page.url
            logger.warning(
                f"[publisher] verify_logged_in 检测到 QR:selector={selector!r} url={url!r}"
            )
            raise CookieExpired(
                f"登录态已失效:发布页跳出了扫码二维码,请到管理后台账号页扫码续命"
                f"(命中 selector={selector}, 当前 url={url})"
            )
    # 再看登录后元素
    joined = ", ".join(sel.LOGGED_IN_SELECTORS)
    try:
        page.wait_for_selector(joined, timeout=timeout_ms, state="visible")
    except Exception as exc:
        # 异常细节进日志(loguru 自己抓),通知文本只给运营看的版本
        logger.warning(f"[publisher] verify_logged_in 等待登录元素超时 url={page.url!r}: {exc}")
        raise CookieExpired(
            f"登录态可能已失效:发布页没出现登录后该有的入口,请扫码续命(当前 url={page.url})"
        ) from exc


def upload_video(page: Page, *, file_path: Path, timeout_seconds: int) -> None:
    """[5] set_input_files + 轮询发表按钮变为可点击。

    timeout_seconds 通常取 settings.publisher.upload_timeout_seconds(默认 600)。
    """
    try:
        page.locator(sel.FILE_INPUT).set_input_files(str(file_path))
    except Exception as exc:
        logger.warning(f"[publisher] upload_video 提交文件失败: {exc}")
        raise UploadFailed("提交视频文件失败,可能是文件格式不支持或浏览器侧异常") from exc

    deadline = time.monotonic() + timeout_seconds
    role_name, role_text = sel.UPLOAD_PUBLISH_BUTTON_ROLE
    while time.monotonic() < deadline:
        # 上传失败兜底
        if (
            page.locator(sel.UPLOAD_FAILED_INDICATOR).count()
            and page.locator(sel.UPLOAD_DELETE_TAG).count()
        ):
            raise UploadFailed("视频号页面提示上传失败,请检查视频文件能否正常播放")
        # 发表按钮 class 不含 disabled → 上传完成
        publish_button = page.get_by_role(role_name, name=role_text)  # type: ignore[arg-type]
        cls = publish_button.get_attribute("class") if publish_button.count() else None
        if cls and sel.UPLOAD_DISABLED_CLASS not in cls:
            return
        time.sleep(2)
    raise UploadFailed(f"上传超过 {timeout_seconds} 秒仍未完成,可能视频过大或网络慢")


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


def disable_location(page: Page) -> None:
    """[11.5] 把"位置"显式选为"不显示位置"(写死,不接飞书字段)。

    视频号页面会记忆上次选过的位置,或按 IP 推荐附近位置 —— 不显式关掉的话,
    自动发布出去会带地理信息。这步打开位置面板 → 选"不显示位置"。

    所有失败(找不到入口/选项、改版、点击异常)只 warn,不抛 —— 视频该能发还能发,
    最差结果是带默认位置上线;改版时来调 selectors.py:LOCATION_TRIGGER_SELECTORS 即可。
    """
    trigger = None
    for sel_str in sel.LOCATION_TRIGGER_SELECTORS:
        cand = page.locator(sel_str).first
        try:
            if cand.count() and cand.is_visible():
                trigger = cand
                break
        except Exception:
            continue
    if trigger is None:
        logger.info("[publisher] 未见位置入口,跳过(可能视频号改版或本来就没有此控件)")
        return
    try:
        trigger.click()
        page.wait_for_timeout(500)
        not_show = page.get_by_text(sel.LOCATION_HIDE_OPTION_TEXT, exact=True).first
        if not_show.count():
            not_show.click()
            return
        logger.warning(
            f"[publisher] 位置面板打开,但找不到 {sel.LOCATION_HIDE_OPTION_TEXT!r} 选项 —— 跳过"
        )
    except Exception as exc:
        logger.warning(f"[publisher] disable_location 异常,跳过: {exc}")
    finally:
        # 兜底:popover 可能还在开着,Escape 关掉避免遮挡后面的"定时发布"控件
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass


def set_schedule(page: Page, *, publish_at: datetime) -> None:
    """[12] 切到"定时" → 选日期 → 在时分面板里点小时 li + 分钟 li。

    weui picker 是分栏 spinner:小时和分钟各一列 ``<li>``,**点 li 提交而非键盘**。
    早期 ``keyboard.type("%H")`` 靠的是 weui 数字快捷匹配,冒号被吞导致分钟列从不
    被更新 —— 飞书 "21:20" 永远被发布成 "21:00"。

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

    # 翻月(只翻到目标月,不处理跨年场景 —— validator 14d 上限本来就在 1 个月内可达)。
    # 页面月份标签实际渲染可能是 "6月" / "06月" / "2026年6月",不能直接和
    # strftime("%m月")="06月" 比字符串(个位数月份永不相等 → 误翻月、把任务排晚一个月)。
    # 抽出"N月"里的数字按月份号比较;抽不到数字(异常格式)就保守不翻,留在当前月。
    page_month = page.inner_text(sel.SCHEDULE_MONTH_LABEL)
    m = re.search(r"(\d{1,2})\s*月", page_month)
    if m and int(m.group(1)) != publish_at.month:
        page.click(sel.SCHEDULE_NEXT_MONTH_BTN)

    # 选日
    for element in page.query_selector_all(sel.SCHEDULE_DAY_TABLE):
        if sel.SCHEDULE_DAY_DISABLED_CLASS in (element.evaluate("el => el.className") or ""):
            continue
        if (element.inner_text() or "").strip() == str(publish_at.day):
            element.click()
            break

    # 点时间输入框展开时分面板,然后分别点小时 li + 分钟 li
    page.click(sel.SCHEDULE_TIME_INPUT)
    hour_text = f"{publish_at.hour:02d}"
    minute_text = f"{publish_at.minute:02d}"
    page.locator(sel.SCHEDULE_TIME_HOUR_LI).get_by_text(hour_text, exact=True).first.click()
    page.locator(sel.SCHEDULE_TIME_MINUTE_LI).get_by_text(minute_text, exact=True).first.click()
    page.locator(sel.TITLE_EDITOR).click()  # 点别处收起 picker


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
        parsed = urlparse(href)
        params = parse_qs(parsed.query)
        vid = params.get("vid", params.get("id", [None]))[0]
        return vid, href
    except Exception as exc:
        logger.info(f"提取 remote_url 失败(对定时发布是常态): {exc}")
        return None, None


# ============== 平台步骤回调 + Spec ==============


def _pre_publish(page: Page, bundle: TaskBundle, staged: Path, ctx: PublishContext) -> None:
    """[1]-[13] 打开页 → 上传 → 填表 → 风控探测(止于 dry-run gate 之前)。

    视频本体由编排器已 stage 好传进来;封面在此处按需 stage(仅当配了封面)。
    """
    step_pause = ctx.step_pause
    upload_timeout = ctx.settings.publisher.upload_timeout_seconds

    staged_cover = None
    if bundle.video_cover_path is not None:
        staged_cover = stage_to_tmp(
            bundle.video_cover_path, task_id=ctx.task_id, tmp_root=ctx.tmp_root
        )

    # APC 守门(spec §3.3 注入点):dev-mode 永远 True;打包模式看 APC 判决
    apc_passed = wxsp.apc.check_pass()

    ctx.last_step = "open"
    open_publish_page(page)
    random_pause(step_pause)

    ctx.last_step = "login"
    verify_logged_in(page)
    random_pause(step_pause)

    # APC 拒绝时装"等待上传区域超时"故障(spec §3.3)
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
    upload_video(page, file_path=staged, timeout_seconds=upload_timeout)
    random_pause(step_pause)

    ctx.last_step = "title"
    fill_title(page, title=bundle.title)
    random_pause(step_pause)

    ctx.last_step = "desc"
    fill_description(page, description=bundle.description)
    random_pause(step_pause)

    ctx.last_step = "tags"
    add_tags(page, tags=_json.loads(bundle.tags_json or "[]"))
    random_pause(step_pause)

    ctx.last_step = "cover"
    set_cover(page, cover_path=staged_cover)
    random_pause(step_pause)

    ctx.last_step = "topic"
    bind_topic(page, topic=bundle.topic)
    random_pause(step_pause)

    ctx.last_step = "original"
    toggle_original(page, original_claim=bundle.original_claim)
    random_pause(step_pause)

    ctx.last_step = "location"
    disable_location(page)
    random_pause(step_pause)

    ctx.last_step = "schedule"
    set_schedule(page, publish_at=bundle.publish_at)
    random_pause(step_pause)

    ctx.last_step = "risk"
    risk_control_probe(page)


def _post_publish(page: Page, bundle: TaskBundle, ctx: PublishContext) -> None:
    """[15]-[17] 点发表 → 等跳转到 post/list → 抽取 remote_video_id / url(尽力而为)。"""
    ctx.last_step = "publish"
    click_publish(page)

    ctx.last_step = "wait_success"
    wait_for_success_indicator(page)

    ctx.last_step = "extract"
    vid, url = extract_remote_video_id_and_url(page)
    ctx.result.remote_video_id = vid
    ctx.result.remote_url = url


TENCENT_SPEC = PlatformSpec(
    platform_key="tencent_channel",
    display_name="视频号",
    pre_publish=_pre_publish,
    post_publish=_post_publish,
)


class TencentChannelPublisher:
    platform_key = "tencent_channel"

    def publish_one(
        self,
        task_id: int,
        *,
        dry_run: bool = False,
        settings: Settings,
    ) -> PublishResult:
        """跑视频号发布的完整流程(共享编排器 + 视频号步骤)。"""
        return run_publish(task_id, dry_run=dry_run, settings=settings, spec=TENCENT_SPEC)

    def login(self, account: Account) -> bool:
        """扫码登录: 打开浏览器, 等用户在微信上扫码。"""
        from wxsp.browser import check_cookie

        return check_cookie(
            Path(account.user_data_dir),
            timeout_ms=300_000,
            account_id=account.id,
            platform="tencent_channel",
        )
