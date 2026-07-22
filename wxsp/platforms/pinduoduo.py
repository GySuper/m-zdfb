"""拼多多多多视频发布实现 —— patchright(sync)驱动,无 iframe。

只负责浏览器交互(打开页 → 上传 → 填描述/话题 → 绑商品 → 定时 → 内容声明 →
点发布)。claim / DB 状态机 / 通知 / 飞书回写等无差别 plumbing 全在
wxsp/platforms/runner.py 的共享编排器里。

步骤逻辑参考 taobao_guanghe.py(商品绑定 + 内容声明),但拼多多无 iframe、
首页即发布页、无标题框、商品绑定更简单(输入ID→下一步,无需勾选)。
决策:无指纹(patchright persistent context)/ 必挂商品 / 只走定时发布。
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
    ProductNotFound,
    RiskControl,
    UploadFailed,
)
from wxsp.human_input import (
    bring_browser_to_front,
    physical_click,
    physical_press,
    physical_select_all,
    physical_type,
    physical_upload,
)
from wxsp.models import Account
from wxsp.nas import stage_to_tmp
from wxsp.platforms import pinduoduo_selectors as sel
from wxsp.platforms.base import PlatformSpec, PublishContext, PublishResult, TaskBundle
from wxsp.platforms.runner import random_pause, run_publish, screenshot


def _wait(page: Page, min_ms: int = 1500, max_ms: int = 4000) -> None:
    """步间随机停顿 ms。"""
    page.wait_for_timeout(random.randint(min_ms, max_ms))


# ---------------------------------------------------------------------------
# step functions
# ---------------------------------------------------------------------------


def _open_publish_page(page: Page) -> None:
    # 发布页 = 首页(SPA,无独立 URL 切换)。goto 后未登录会被停在 mms/login。
    page.goto(sel.HOME_URL, wait_until="domcontentloaded")
    if sel.LOGIN_URL_FRAGMENT in page.url:
        return  # 交给 _verify_logged_in 判定
    try:
        page.wait_for_url(f"**{sel.LOGGED_IN_URL_FRAGMENT}**", timeout=30_000)
    except Exception as err:
        if sel.LOGIN_URL_FRAGMENT not in page.url:
            raise NetworkError("拼多多发布页加载超时") from err


def _verify_logged_in(page: Page) -> None:
    if sel.LOGIN_URL_FRAGMENT in page.url:
        raise CookieExpired("拼多多登录态失效(停在 SSO 登录页,需重新扫码)")
    # 正向确认:上传区 file input 必须在
    try:
        page.locator(sel.VIDEO_FILE_INPUT).first.wait_for(state="attached", timeout=8000)
    except Exception as err:
        raise CookieExpired("拼多多登录态失效(上传入口未出现,需重新扫码登录)") from err


def _upload_video(page: Page, file_path: Path, timeout_seconds: int = 600) -> None:
    # 混合上传:物理点击上传区(真实 click)→ set_input_files 注入文件路径
    upload_area = page.locator(sel.VIDEO_UPLOAD_AREA).first
    file_input = page.locator(sel.VIDEO_FILE_INPUT).first
    physical_upload(page, upload_area, file_input, str(file_path))
    try:
        page.locator(sel.UPLOAD_DONE_MARKER).first.wait_for(
            state="visible", timeout=timeout_seconds * 1000
        )
        logger.info("[pinduoduo] 视频上传完成")
    except Exception as err:
        raise UploadFailed("视频上传/处理超时") from err


def _fill_description(page: Page, description: str | None) -> None:
    if not description:
        return
    editor = page.locator(sel.DESC_EDITOR).first
    editor.wait_for(state="visible", timeout=10_000)
    physical_click(page, editor, click_count=2)
    _wait(page, 1500, 2500)
    physical_type(page, description[: sel.DESC_MAX_LENGTH])


def _add_tags(page: Page, tags: list[str]) -> None:
    if not tags:
        return
    editor = page.locator(sel.DESC_EDITOR).first
    physical_click(page, editor)
    for tag in tags:
        physical_type(page, "#" + tag)
        _wait(page)
        try:
            page.locator(sel.TOPIC_POPOVER).wait_for(state="visible", timeout=3000)
            page.locator(sel.TOPIC_ITEM).first.click()
            _wait(page, 1500, 3000)
        except Exception:
            # 候选框没弹,走空格收尾(避免 #tag 留成纯文本)
            physical_press("Space")
        _wait(page)


def _add_products(page: Page, product_ids: list[str]) -> None:
    if not product_ids:
        return
    # 点「添加商品」→ 切「商品ID」tab → 逐个填 ID → 下一步绑定
    page.locator(sel.PRODUCT_TRIGGER).first.click()
    _wait(page, 1000, 2000)
    page.locator(sel.PRODUCT_TAB_BY_ID).click()
    _wait(page, 800, 1500)
    # 拼多多商品ID搜索一次只绑一个;逐个绑定
    for pid in product_ids:
        page.locator(sel.PRODUCT_ID_INPUT).fill(pid)
        _wait(page, 300, 600)
        page.locator(sel.PRODUCT_NEXT_BUTTON).click()
        _wait(page, 2000, 3000)
    # 等绑定成功标志(删除商品按钮出现)
    try:
        page.locator(sel.PRODUCT_BOUND_MARKER).first.wait_for(state="visible", timeout=10_000)
    except Exception as err:
        raise ProductNotFound(f"商品绑定失败(ID: {product_ids})") from err
    logger.info(f"[pinduoduo] 绑定商品完成 ids={product_ids}")


def _set_cover(page: Page, cover_path: Path | None) -> None:
    """有自定义封面 → 走封面弹窗本地上传(可选,best-effort)。"""
    if cover_path is None:
        return
    try:
        page.locator(sel.COVER_EDIT_BUTTON).first.click()
        _wait(page, 1500, 2500)
        page.locator(sel.COVER_UPLOAD_TAB).click()
        _wait(page, 800, 1500)
        cover_input = page.locator(sel.COVER_FILE_INPUT).first
        cover_input.wait_for(state="attached", timeout=10_000)
        cover_input.set_input_files(str(cover_path))
        _wait(page, 1500, 2500)
        confirm = page.locator(sel.COVER_CONFIRM_BUTTON).first
        confirm.wait_for(state="visible", timeout=10_000)
        physical_click(page, confirm)
        logger.info("[pinduoduo] 自定义封面设置完成(best-effort)")
    except Exception as err:
        logger.warning(f"[pinduoduo] 封面设置失败(降级为平台默认封面): {err}")


def _set_schedule(page: Page, publish_at: datetime) -> None:
    # 选「定时发布」radio(绑商品后才有的发布设置)→ 填日期/时间 → 点确认
    # beast-core datePicker 的 fill() 会卡,走物理输入(对齐小红书定时策略)
    page.locator(sel.SCHEDULE_RADIO_CONTAINER).first.click()
    _wait(page, 1000, 2000)
    date_input = page.locator(sel.SCHEDULE_DATE_INPUT).first
    date_input.wait_for(state="visible", timeout=10_000)
    physical_click(page, date_input)
    _wait(page, 500, 1000)
    physical_select_all()
    physical_type(page, publish_at.strftime(sel.SCHEDULE_DATETIME_FORMAT))
    _wait(page, 500, 1000)
    physical_press("Enter")
    _wait(page, 1000, 2000)
    # 点日历确认按钮(只填日期不确认会丢失)
    try:
        page.locator(sel.SCHEDULE_CONFIRM_BUTTON).first.click()
        _wait(page, 1500, 2500)
    except Exception as err:
        logger.warning(f"[pinduoduo] 定时确认按钮未找到(可能改版): {err}")


def _select_declaration(page: Page, declaration: str | None) -> None:
    choice = declaration or sel.DECLARATION_DEFAULT
    page.locator(sel.DECLARATION_TRIGGER).click()
    _wait(page, 800, 1500)
    target = sel.DECLARATION_OPTIONS.get(choice, sel.DECLARATION_DEFAULT)
    page.get_by_text(target, exact=False).first.click()
    _wait(page, 500, 1000)
    logger.info(f"[pinduoduo] 内容声明已选: {choice}")


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

    def _btn_gone() -> bool:
        try:
            return not btn.is_visible()
        except Exception:
            return True

    physical_click(page, btn, delay_ms=80, verify=_btn_gone)
    _wait(page, 2500, 4500)


def _wait_for_success(page: Page, timeout: int = 60) -> None:
    # 真发实测(2026-07-22):点发布后跳转 mall-goods-video(无 toast)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sel.SUCCESS_URL_FRAGMENT in page.url:
            logger.info("[pinduoduo] 已跳转商家带货视频页,发布成功")
            return
        time.sleep(0.5)
    raise ElementNotFound("发布成功判定超时(URL 未跳转 mall-goods-video)")


# ---------------------------------------------------------------------------
# 平台步骤回调 + Spec + Publisher
# ---------------------------------------------------------------------------


def _pre_publish(page: Page, bundle: TaskBundle, staged: Path, ctx: PublishContext) -> None:
    """打开页 → 上传 → 填描述/话题 → 绑商品 → 封面 → 定时 → 内容声明 → 风控探测。"""
    step_pause = ctx.step_pause

    staged_cover = None
    if bundle.video_cover_path is not None:
        staged_cover = stage_to_tmp(
            bundle.video_cover_path, task_id=ctx.task_id, tmp_root=ctx.tmp_root
        )

    apc_passed = wxsp.apc.check_pass()
    bring_browser_to_front(page)

    ctx.last_step = "open_publish"
    _open_publish_page(page)
    random_pause(step_pause)

    ctx.last_step = "verify_login"
    _verify_logged_in(page)
    random_pause(step_pause)

    # APC 拒绝时装"等待上传区域超时"故障(对齐其他平台)
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

    ctx.last_step = "desc"
    _fill_description(page, description=bundle.description)
    random_pause(step_pause)

    ctx.last_step = "tags"
    _add_tags(page, tags=_json.loads(bundle.tags_json or "[]"))
    random_pause(step_pause)

    # 商品 ID:必挂(拼多多带货视频核心)
    ctx.last_step = "products"
    product_ids: list[str] = []
    if bundle.product_ids_json and bundle.product_ids_json != "[]":
        try:
            parsed = _json.loads(bundle.product_ids_json)
            if isinstance(parsed, list):
                product_ids = [str(p) for p in parsed if p]
        except (TypeError, _json.JSONDecodeError):
            logger.warning(
                f"[pinduoduo] 商品 ID JSON 解析失败 task_id={ctx.task_id}: {bundle.product_ids_json!r}"
            )
    if product_ids:
        _add_products(page, product_ids=product_ids)
    random_pause(step_pause)

    ctx.last_step = "cover"
    _set_cover(page, cover_path=staged_cover)
    random_pause(step_pause)

    ctx.last_step = "schedule"
    _set_schedule(page, publish_at=bundle.publish_at)
    random_pause(step_pause)

    ctx.last_step = "declaration"
    _select_declaration(page, declaration=bundle.declaration)
    random_pause(step_pause)

    ctx.last_step = "risk"
    _risk_control_probe(page)


def _post_publish(page: Page, bundle: TaskBundle, ctx: PublishContext) -> None:
    ctx.last_step = "publish"
    _click_publish(page)

    ctx.last_step = "wait_success"
    _wait_for_success(page)


PINDUODUO_SPEC = PlatformSpec(
    platform_key="pinduoduo",
    display_name="拼多多",
    pre_publish=_pre_publish,
    post_publish=_post_publish,
)


class PinduoduoPublisher:
    platform_key = "pinduoduo"

    def login(self, account: Account) -> bool:
        """开浏览器到拼多多 SSO 登录页等扫码。

        用正向判据:扫码成功后跳转到 n-creator/video/home,URL 含该片段 = 成功。
        cookie 由 browser_context 退出时落盘。
        """
        user_data_dir = Path(account.user_data_dir)
        logger.info(f"[pinduoduo] 开始登录 account={account.id}")
        try:
            with browser_context(
                user_data_dir,
                headless=False,
                account_id=account.id,
                platform="pinduoduo",
            ) as page:
                page.goto(sel.LOGIN_URL, wait_until="domcontentloaded")
                deadline = time.time() + 300
                while time.time() < deadline:
                    if sel.LOGGED_IN_URL_FRAGMENT in page.url:
                        logger.info(f"[pinduoduo] 登录成功 account={account.id}")
                        return True
                    time.sleep(2)
                logger.warning(f"[pinduoduo] 登录超时 account={account.id}")
                return False
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
