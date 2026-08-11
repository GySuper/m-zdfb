# ruff: noqa: RUF001
"""淘宝光合平台发布实现 — patchright 驱动,iframe 内操作。

只负责浏览器交互(打开页 → 填表 → 点发布)。claim / DB 状态机 / 通知 / 飞书回写
等无差别 plumbing 全在 `wxsp/platforms/runner.py` 的共享编排器里。
"""

from __future__ import annotations

import json as _json
import random
import time
from datetime import datetime
from pathlib import Path

from loguru import logger
from patchright.sync_api import FrameLocator, Page, expect

import wxsp.apc
from wxsp.browser import browser_context
from wxsp.config import Settings
from wxsp.errors import (
    CookieExpired,
    ElementNotFound,
    NetworkError,
    ProductNotFound,
    RiskControl,
    TopicNotFound,
    UploadFailed,
)
from wxsp.models import Account
from wxsp.platforms import taobao_selectors as sel
from wxsp.platforms.base import PlatformSpec, PublishContext, PublishResult, TaskBundle
from wxsp.platforms.runner import random_pause, run_publish, screenshot


def _iframe(page: Page) -> FrameLocator:
    return page.frame_locator(sel.IFRAME_SELECTOR)


# ---------------------------------------------------------------------------
# step functions [3]-[16]
# ---------------------------------------------------------------------------


def _open_publish_page(page: Page) -> None:
    # 必须模拟用户操作路径:首页 → hover"发布作品" → 点"发视频"
    # 直接跳到 pubNew/video 不带 pub_url 参数会报"URL不合法"
    page.goto(sel.CREATOR_HOME, wait_until="domcontentloaded")
    if sel.LOGIN_URL_FRAGMENT in page.url:
        raise CookieExpired("淘宝登录态失效,需重新登录")
    # hover "发布作品" 出现下拉菜单
    page.get_by_text(sel.PUBLISH_DROPDOWN_TRIGGER).hover()
    time.sleep(0.5)
    page.get_by_role("menuitem", name=sel.PUBLISH_VIDEO_MENU_ITEM).click()
    # 等待发布页 iframe 加载
    try:
        _iframe(page).locator(sel.LOGGED_IN_INDICATOR).wait_for(timeout=30_000)
    except Exception as err:
        raise NetworkError("发布页加载超时") from err


def _verify_logged_in(page: Page) -> None:
    if sel.LOGIN_URL_FRAGMENT in page.url:
        raise CookieExpired("淘宝登录态失效，需重新登录")
    try:
        _iframe(page).locator(sel.LOGGED_IN_INDICATOR).wait_for(timeout=5_000)
    except Exception as err:
        raise CookieExpired("发布表单不可见，登录态可能失效") from err


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

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if failed.count():
            raise UploadFailed("视频上传失败(平台拒绝该文件)")
        if not waiting.count() and done.count():
            logger.info("[taobao] 视频上传/处理完成")
            return
        time.sleep(3)
    raise UploadFailed("视频上传/处理超时")


def _wait_cover_generated(page: Page) -> None:
    """上传成功后给封面图 10s 渲染时间(best-effort)。"""
    iframe = _iframe(page)
    try:
        iframe.locator(sel.COVER_IMG_PREVIEW).wait_for(timeout=10_000)
    except Exception:
        logger.warning("[taobao] 封面区域未按预期出现，继续")


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
    # 苍颉编辑器:点击 data-cangjie-key 容器激活,用 keyboard.type 输入
    editor = iframe.locator(sel.DESCRIPTION_EDITOR).first
    editor.click()
    time.sleep(0.3)
    page.keyboard.type(description[:1000])


def _add_topic(page: Page, topic_name: str | None) -> None:
    if not topic_name:
        return
    iframe = _iframe(page)
    iframe.locator(sel.TOPIC_CLICK_AREA).click()
    time.sleep(1)
    iframe.locator(sel.TOPIC_DIALOG_HEADING).wait_for(timeout=5_000)
    iframe.locator(sel.TOPIC_SEARCH_INPUT).fill(topic_name)
    iframe.locator(sel.TOPIC_SEARCH_BUTTON).click()
    time.sleep(2)
    try:
        first_card = iframe.locator(f'text="{topic_name}"').first
        first_card.wait_for(timeout=5_000)
        first_card.click()
    except Exception as err:
        try:
            iframe.locator(sel.TOPIC_CLOSE_BUTTON).click()
        except Exception:
            pass
        raise TopicNotFound(f"话题 '{topic_name}' 搜索无结果") from err
    iframe.locator(sel.TOPIC_CONFIRM_BUTTON).click()
    time.sleep(1)


_MAX_PRODUCTS = 6


def _add_products(page: Page, product_ids: list[str]) -> None:
    if not product_ids:
        return
    ids = product_ids[:_MAX_PRODUCTS]
    if len(product_ids) > _MAX_PRODUCTS:
        logger.warning(
            f"[taobao] 商品数 {len(product_ids)} 超出上限 {_MAX_PRODUCTS},"
            f" 仅取前 {_MAX_PRODUCTS} 个: {ids}"
        )
    iframe = _iframe(page)
    iframe.locator(sel.PRODUCT_TRIGGER).click()
    time.sleep(1)
    iframe.locator(sel.PRODUCT_DIALOG_HEADING).wait_for(timeout=5_000)
    # 等默认商品列表渲染完:弹窗外壳出现时默认列表还在加载,此刻去搜索会撞上
    # 列表重排 → card.hover() actionability 不满足 → 30s 超时(间歇性,看网络)。
    # 等首个商品 link 出现说明列表已就绪,再开始按 ID 搜索。
    iframe.locator(sel.PRODUCT_ITEM_LINK_ANY).first.wait_for(timeout=10_000)
    for pid in ids:
        search = iframe.locator(sel.PRODUCT_SEARCH_INPUT)
        search.fill(pid)
        search.press("Enter")

        # 结果以商品卡片呈现(不是每商品一个可见 checkbox)。按卡片标题链接 href 里的
        # 商品ID 精确定位 —— 搜不到 → 链接永不出现 → 判 ProductNotFound。
        link = iframe.locator(sel.PRODUCT_ITEM_LINK_BY_ID.format(pid=pid)).first
        try:
            link.wait_for(state="visible", timeout=8_000)
        except Exception as err:
            raise ProductNotFound(f"商品ID '{pid}' 搜索无结果") from err

        # 上溯到卡片容器,勾选卡片内"商品选择"复选框(hover 才显,区别于顶部"筛选"复选框)。
        # 先 hover 卡片让复选框显出,再直接点 input(opacity:0 盖在方框上,是真正接收点击的元素)。
        card = link.locator(sel.PRODUCT_ITEM_CARD_ANCESTOR)
        card.hover()
        checkbox = card.locator(sel.PRODUCT_ITEM_SELECT_CHECKBOX_INPUT).first
        checkbox.click()

        # 校验真实 checkbox 已选中,不依赖 Fusion 组件可能变化的 class。
        try:
            expect(checkbox).to_be_checked(timeout=3_000)
        except Exception as err:
            raise ProductNotFound(f"商品ID '{pid}' 已搜到但勾选未生效") from err
        logger.info(f"[taobao] 选中商品 pid={pid}")
    iframe.locator(sel.PRODUCT_CONFIRM_BUTTON).click()
    time.sleep(1)
    # 等弹窗关闭,否则 overlay 挡住后续步骤
    try:
        iframe.locator(sel.PRODUCT_DIALOG_HEADING).wait_for(state="hidden", timeout=10_000)
    except Exception:
        pass


def _set_schedule(page: Page, publish_at: datetime) -> None:
    iframe = _iframe(page)
    # ① 点"定时发布"radio,combobox 从 disabled 变为 enabled
    schedule_radio = iframe.locator(sel.SCHEDULE_RADIO).locator("..").locator('input[type="radio"]')
    schedule_radio.click(force=True)
    time.sleep(0.5)

    # ② 点 combobox 打开 DatePicker
    combo = iframe.locator(sel.SCHEDULE_COMBOBOX)
    combo.click(force=True)
    time.sleep(1)

    # ③ 直接填日期/时间输入框,绕开点日历格子(见 selectors 注释:格子里本月+下月
    #    日号重复,.first 会点到本月同号 → 跨月任务排错月份)。
    #    先填日期会把时间重置成 00:00,所以时间必须在日期之后填。
    picker = iframe.locator(sel.SCHEDULE_PICKER_OVERLAY)
    date_input = iframe.locator(sel.SCHEDULE_DATE_INPUT)
    date_input.fill(publish_at.strftime("%Y/%m/%d"))
    date_input.press("Enter")
    time.sleep(0.3)

    # ④ 填时间 HH:mm
    time_input = iframe.locator(sel.SCHEDULE_TIME_INPUT)
    time_input.fill(publish_at.strftime("%H:%M"))
    time_input.press("Enter")
    time.sleep(0.3)

    # ⑤ 点"确定"关闭 DatePicker
    picker.locator(sel.SCHEDULE_CONFIRM).click()
    time.sleep(0.5)

    # ⑥ 确认按钮已变成"定时发布"
    try:
        iframe.locator(sel.SUBMIT_BUTTON_SCHEDULED).wait_for(state="visible", timeout=5_000)
    except Exception:
        raise ElementNotFound("定时发布按钮未出现,时间可能未填写成功") from None


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
    iframe.locator(radio_sel).locator("..").locator('input[type="radio"]').click()


def _toggle_ai_optimize(page: Page, on: bool) -> None:
    """把"AI优化"开关设为目标状态。

    平台默认**开启**,不能靠盲点 —— 必须先读当前 aria-checked,只在与目标不一致时才点
    (盲点会把默认开的关掉,或把该关的留在开)。开关切换失败只 warn 不阻断发布
    (非关键设置,对齐 disable_location 的处理)。
    """
    iframe = _iframe(page)
    switch = iframe.locator(sel.AI_TOGGLE_SWITCH)
    try:
        switch.wait_for(state="visible", timeout=5_000)
    except Exception:
        logger.warning("[taobao] 未找到 AI优化开关,跳过(可能改版)")
        return
    if (switch.get_attribute("aria-checked") == "true") == on:
        return  # 已是目标态
    switch.click()
    # 轮询确认切到目标态(Fusion next-switch 的 aria-checked 立即反映)
    deadline = time.time() + 2
    while time.time() < deadline:
        if (switch.get_attribute("aria-checked") == "true") == on:
            return
        time.sleep(0.2)
    logger.warning(f"[taobao] AI优化开关点击后仍非目标态 on={on},继续发布")


def _disable_download(page: Page) -> None:
    """取消'允许下载':radio 默认选中,点击即取消。"""
    iframe = _iframe(page)
    label = iframe.locator(sel.DOWNLOAD_RADIO)
    radio = label.locator("..").locator('input[type="radio"]')
    if radio.is_checked():
        label.click()


def _click_publish(page: Page) -> None:
    iframe = _iframe(page)
    scheduled = iframe.locator(sel.SUBMIT_BUTTON_SCHEDULED)
    # 只点"定时发布",不 fallback 到"立即发布"(防止时间没填就发出去)
    scheduled.wait_for(state="visible", timeout=5_000)
    scheduled.click()


def _wait_for_success_indicator(page: Page, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        # 只接受发布后的作品管理页,登录/错误/风控等其它跳转不能算成功。
        if sel.SUCCESS_URL_FRAGMENT in page.url:
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
    if sel.SUCCESS_URL_FRAGMENT in page.url:
        logger.info("[taobao] 已跳转作品管理页，发布成功")
        return
    raise ElementNotFound("发布成功判定超时")


def _risk_control_probe(page: Page) -> None:
    """扫页面 body 文本,任一风控关键词命中 → RiskControl。"""
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return
    for kw in sel.RISK_CONTROL_KEYWORDS:
        if kw in body_text:
            raise RiskControl(f"页面命中风控关键词: {kw}")


# ---------------------------------------------------------------------------
# 平台步骤回调 + Spec + Publisher
# ---------------------------------------------------------------------------


def _pre_publish(page: Page, bundle: TaskBundle, staged: Path, ctx: PublishContext) -> None:
    """[3]-[15] 打开页 → 上传 → 填表 → 商品 → 定时 → 声明 → 风控探测。

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
    _open_publish_page(page)
    random_pause(step_pause)

    ctx.last_step = "login"
    _verify_logged_in(page)
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
    _fill_title(page, title=bundle.title)
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

    ctx.last_step = "schedule"
    _set_schedule(page, publish_at=bundle.publish_at)
    random_pause(step_pause)

    ctx.last_step = "declaration"
    _set_declaration(page, declaration=bundle.declaration)
    random_pause(step_pause)

    ctx.last_step = "ai"
    _toggle_ai_optimize(page, on=bool(bundle.ai_optimize))
    random_pause(step_pause)

    ctx.last_step = "download"
    _disable_download(page)
    random_pause(step_pause)

    ctx.last_step = "risk"
    _risk_control_probe(page)


def _post_publish(page: Page, bundle: TaskBundle, ctx: PublishContext) -> None:
    """[16]-[17] 点定时发布 → 等跳转判成功。淘宝不抽取 remote_url(到点前无公开链接)。"""
    ctx.last_step = "publish"
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
