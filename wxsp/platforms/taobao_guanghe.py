# ruff: noqa: RUF001, RUF002
"""淘宝光合平台发布实现 — 18 步，patchright 驱动，iframe 内操作。"""

from __future__ import annotations

import json as _json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger
from patchright.sync_api import FrameLocator, Page
from sqlmodel import Session

from wxsp.browser import browser_context
from wxsp.config import Settings
from wxsp.db import claim_task, get_engine, init_db, session_scope
from wxsp.errors import (
    CookieExpired,
    ElementNotFound,
    NetworkError,
    ProductNotFound,
    PublisherError,
    RiskControl,
    TopicNotFound,
    UploadFailed,
    classify,
)
from wxsp.feishu import FeishuApiError, make_client, writeback_row
from wxsp.models import Account, Task, Video
from wxsp.nas import cleanup_tmp, stage_to_tmp
from wxsp.notify import NotifyEvent, error_type_cn, notify, step_cn
from wxsp.platforms import taobao_selectors as sel
from wxsp.platforms.base import PublishResult

# error_type → (notify type, level, 中文标题)
_NOTIFY_BY_ERROR: dict[str, tuple[str, str, str]] = {
    "cookie_expired": ("cookie_expired", "error", "登录态失效,等待扫码"),
    "risk_control": ("risk_control", "error", "风控触发,账号已暂停 24 小时"),
    "element_not_found": ("element_not_found", "warn", "页面元素未找到,淘宝光合可能改版"),
    "nas_unreachable": ("nas_unreachable", "error", "存储不可达"),
}

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _random_pause(step_pause: tuple[float, float]) -> None:
    time.sleep(random.uniform(*step_pause))


def _screenshot(
    page: Page,
    *,
    task_id: int,
    step: str,
    screenshots_root: Path,
    now: datetime | None = None,
) -> Path:
    now = now or datetime.now()
    month_dir = screenshots_root / now.strftime("%Y%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    path = month_dir / f"{task_id}_{step}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
    except Exception:
        logger.warning(f"[taobao] screenshot failed step={step}")
    return path


def _iframe(page: Page) -> FrameLocator:
    return page.frame_locator(sel.IFRAME_SELECTOR)


def _load_task_bundle(session: Session, task_id: int) -> tuple[Task, Video, Account]:
    task = session.get(Task, task_id)
    if task is None:
        raise ValueError(f"Task {task_id} not found")
    video = session.get(Video, task.video_id)
    if video is None:
        raise ValueError(f"Video {task.video_id} not found")
    account = session.get(Account, task.account_id)
    if account is None:
        raise ValueError(f"Account {task.account_id} not found")
    return task, video, account


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
    iframe = _iframe(page)
    file_input = iframe.locator(sel.FILE_INPUT)
    file_input.set_input_files(str(file_path))
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            cover_waiting = iframe.locator(f'text="{sel.COVER_WAITING_TEXT}"')
            if not cover_waiting.count():
                logger.info("[taobao] 封面生成完成")
                return
        except Exception:
            pass
        time.sleep(3)
    raise UploadFailed("视频上传/处理超时")


def _wait_cover_generated(page: Page) -> None:
    iframe = _iframe(page)
    try:
        iframe.locator(f'text="{sel.COVER_READY_INDICATOR}"').wait_for(timeout=10_000)
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
    for pid in ids:
        iframe.locator(sel.PRODUCT_SEARCH_INPUT).fill(pid)
        iframe.locator(sel.PRODUCT_SEARCH_INPUT).press("Enter")
        time.sleep(2)
        # 商品弹窗有 2 个 checkbox:第 1 个是筛选器,第 2 个起才是商品
        all_cbs = iframe.locator('input[type="checkbox"]').all()
        clicked = False
        for cb in all_cbs:
            try:
                if cb.is_visible():
                    if not clicked:
                        clicked = True
                        continue  # 跳过筛选器 checkbox
                    cb.check()
                    logger.info(f"[taobao] 选中商品 pid={pid}")
                    break
            except Exception:
                continue
        if not clicked:
            raise ProductNotFound(f"商品ID '{pid}' 搜索无结果")
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

    # ③ 点日历上的目标日期(day 数字)
    day_str = str(publish_at.day)
    picker = iframe.locator(".next-overlay-wrapper.opened")
    picker.get_by_text(day_str, exact=True).first.click(force=True)
    time.sleep(0.5)

    # ④ 填时间 HH:mm
    time_str = publish_at.strftime("%H:%M")
    time_input = iframe.locator('input[placeholder="HH:mm"]')
    time_input.fill(time_str)
    time.sleep(0.3)

    # ⑤ 点"确定"关闭 DatePicker
    picker.locator('button:has-text("确定")').click()
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
    if not on:
        return
    iframe = _iframe(page)
    switch = iframe.locator(sel.AI_TOGGLE_SWITCH)
    switch.click()


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
        # 发布后页面会跳到 /page/workspace/tb,第一时间检测 URL 变化
        if "pubNew/video" not in page.url:
            logger.info("[taobao] 页面已跳转，视为发布成功")
            return
        for indicator in sel.SUCCESS_INDICATORS:
            try:
                if page.locator(f'text="{indicator}"').count():
                    return
            except Exception:
                pass
            try:
                iframe = _iframe(page)
                if iframe.locator(f'text="{indicator}"').count():
                    return
            except Exception:
                pass
        time.sleep(2)
    if "pubNew/video" not in page.url:
        logger.info("[taobao] 页面已跳转，视为发布成功")
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
# publisher class
# ---------------------------------------------------------------------------


class TaobaoGuanghePublisher:
    platform_key = "taobao_guanghe"

    def login(self, account: Account, settings: Settings) -> bool:
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
        return _publish_one_body(task_id, dry_run=dry_run, settings=settings)


def _publish_one_body(
    task_id: int,
    *,
    dry_run: bool,
    settings: Settings,
) -> PublishResult:
    """跑淘宝光合发布的完整流程,含 claim/DB/通知/飞书回写。

    与视频号 publisher 架构一致:入口 claim_task → 串行跑步骤 → finally 写 DB
    + 通知 + 飞书回写 + 清理 tmp。
    """
    engine = get_engine()
    init_db(engine)
    screenshots_root = settings.app.logs_dir / "screenshots"
    tmp_root = settings.app.data_dir / "tmp"
    pub_cfg = settings.publisher
    upload_timeout = pub_cfg.upload_timeout_seconds
    step_pause = pub_cfg.step_pause_seconds

    # [0] 幂等抢锁
    with Session(engine) as session:
        from wxsp.publisher import AlreadyClaimed

        if not claim_task(session, task_id):
            raise AlreadyClaimed(f"Task {task_id} 不在 pending 或已被占用")

    # 加载 Task/Video/Account 快照
    with Session(engine) as session:
        task, video, account = _load_task_bundle(session, task_id)
        video_record_id = video.id
        video_file_path = Path(video.file_path)
        video_title = video.title
        video_description = video.description
        video_topic = video.topic
        product_ids_raw = (
            video.product_ids_json
            if video.product_ids_json and video.product_ids_json != "[]"
            else None
        )
        # 迁移兼容:旧版数据商品 ID 存在 tags_json 中(product_ids_json 为空时 fallback)
        if product_ids_raw is None:
            product_ids_raw = (
                video.tags_json if video.tags_json and video.tags_json != "[]" else None
            )
        task_publish_at = task.publish_at
        user_data_dir = Path(account.user_data_dir)
        account_id = account.id
        declaration = getattr(video, "declaration", None)
        ai_optimize = getattr(video, "ai_optimize", False)
        task_platform = task.platform

    result = PublishResult(task_id=task_id, ok=False, dry_run=dry_run)
    last_step = "init"

    try:
        # [1] stage NAS → tmp
        last_step = "stage"
        staged = stage_to_tmp(video_file_path, task_id=task_id, tmp_root=tmp_root)

        # [2] launch browser
        last_step = "browser"
        with browser_context(
            user_data_dir,
            headless=pub_cfg.headless,
            account_id=account_id,
            platform=task_platform,
        ) as page:
            try:
                # [3] open publish page
                last_step = "open"
                _open_publish_page(page)
                _random_pause(step_pause)

                # [4] verify login
                last_step = "login"
                _verify_logged_in(page)
                _random_pause(step_pause)

                # [5] upload video
                last_step = "upload"
                _upload_video(page, file_path=staged, timeout_seconds=upload_timeout)
                _random_pause(step_pause)

                # [6] wait cover
                last_step = "cover"
                _wait_cover_generated(page)
                _random_pause(step_pause)

                # [7] fill title
                last_step = "title"
                _fill_title(page, title=video_title)
                _random_pause(step_pause)

                # [8] fill description
                last_step = "desc"
                _fill_description(page, description=video_description)
                _random_pause(step_pause)

                # [9] add topic
                last_step = "topic"
                _add_topic(page, topic_name=video_topic)
                _random_pause(step_pause)

                # [10] add products
                last_step = "products"
                product_ids_list: list[str] = []
                if product_ids_raw:
                    try:
                        parsed = _json.loads(product_ids_raw)
                        if isinstance(parsed, list):
                            product_ids_list = [str(p) for p in parsed if p]
                    except (TypeError, _json.JSONDecodeError):
                        logger.warning(
                            f"[taobao] 商品 ID JSON 解析失败 task_id={task_id}:"
                            f" {product_ids_raw!r},跳过商品"
                        )
                if product_ids_list:
                    _add_products(page, product_ids=product_ids_list)
                _random_pause(step_pause)

                # [11] set schedule
                last_step = "schedule"
                _set_schedule(page, publish_at=task_publish_at)
                _random_pause(step_pause)

                # [12] set declaration
                last_step = "declaration"
                _set_declaration(page, declaration=declaration)
                _random_pause(step_pause)

                # [13] toggle AI optimize
                last_step = "ai"
                _toggle_ai_optimize(page, on=bool(ai_optimize))
                _random_pause(step_pause)

                # [14] disable download
                last_step = "download"
                _disable_download(page)
                _random_pause(step_pause)

                # [15] risk control probe
                last_step = "risk"
                _risk_control_probe(page)

                # ★ DRY_RUN GATE
                if dry_run:
                    last_step = "dryrun_gate"
                    shot = _screenshot(
                        page,
                        task_id=task_id,
                        step="dryrun_gate",
                        screenshots_root=screenshots_root,
                    )
                    result.screenshots.append(str(shot))
                    result.ok = True
                    return result

                # [16] click publish
                last_step = "publish"
                _click_publish(page)

                # [17] wait success
                last_step = "wait_success"
                _wait_for_success_indicator(page)

            except Exception:
                try:
                    shot = _screenshot(
                        page,
                        task_id=task_id,
                        step=f"err_{last_step}",
                        screenshots_root=screenshots_root,
                    )
                    result.screenshots.append(str(shot))
                    try:
                        html_path = Path(str(shot)).with_suffix(".html")
                        html_path.write_text(page.content(), encoding="utf-8")
                    except Exception as html_exc:
                        logger.warning(f"[taobao] 存 HTML 失败 task_id={task_id}: {html_exc}")
                except Exception as ss_exc:
                    logger.warning(f"[taobao] 截图失败 task_id={task_id}: {ss_exc}")
                raise

        result.ok = True
        return result

    except PublisherError as exc:
        kind = classify(exc)
        result.error_type = kind
        result.error_msg = f"step={last_step}: {exc}"
        logger.error(result.error_msg)
        return result
    except Exception as exc:
        kind = classify(exc)
        result.error_type = kind
        result.error_msg = f"step={last_step}: {exc}"
        logger.exception("[taobao] publish 顶层未分类异常")
        return result
    except KeyboardInterrupt:
        result.error_type = "interrupted"
        result.error_msg = f"step={last_step}: 用户中断(Ctrl-C)"
        logger.warning(result.error_msg)
        raise
    finally:
        # cleanup tmp
        try:
            cleanup_tmp(task_id=task_id, tmp_root=tmp_root)
        except Exception as exc:
            logger.warning(f"[taobao] cleanup_tmp 失败 task_id={task_id}: {exc}")

        # 决定 task 最终状态
        if result.ok and not result.dry_run:
            new_status = "success"
        elif result.dry_run and result.ok:
            new_status = "pending"
        elif result.error_type == "interrupted":
            new_status = "interrupted"
        elif result.error_type == "cookie_expired":
            new_status = "pending"
        else:
            new_status = "failed"

        with session_scope(engine) as session:
            task_now = session.get(Task, task_id)
            assert task_now is not None
            task_now.status = new_status
            task_now.last_error_type = result.error_type
            task_now.last_error_msg = result.error_msg
            task_now.screenshots_json = _json.dumps(result.screenshots, ensure_ascii=False)

            if result.dry_run and result.ok:
                task_now.lease_token = None
                task_now.lease_expires_at = None
                task_now.started_at = None
                task_now.attempts = max(0, task_now.attempts - 1)
                task_now.finished_at = None
            elif result.error_type == "cookie_expired":
                task_now.lease_token = None
                task_now.lease_expires_at = None
                task_now.started_at = None
                task_now.finished_at = None
            else:
                task_now.finished_at = datetime.now()

            # 风控 → 暂停账号 24h
            if result.error_type == "risk_control":
                account_now = session.get(Account, account_id)
                if account_now is not None:
                    account_now.paused_until = datetime.now() + timedelta(hours=24)
                    logger.warning(f"[taobao] 风控触发,账号 {account_id} 暂停 24h")

            # 登录态失效 → 写 cookie_status
            if result.error_type == "cookie_expired":
                account_now = session.get(Account, account_id)
                if account_now is not None:
                    account_now.cookie_status = "expired"
                    logger.warning(f"[taobao] 登录态失效,账号 {account_id} cookie_status → expired")

            # 失败通知
            if not result.ok and not result.dry_run:
                error_type = result.error_type or "unknown"
                notify_type, level, title = _NOTIFY_BY_ERROR.get(
                    error_type,
                    ("task_failed", "error", f"任务失败:{error_type_cn(error_type)}"),
                )
                if error_type == "element_not_found":
                    step_zh = step_cn(last_step)
                    title = f"元素未找到 · {step_zh} —— 淘宝光合可能改版"
                account_cfg = settings.accounts.get(account_id)
                display_name = account_cfg.display_name if account_cfg else None
                notify(
                    NotifyEvent(
                        type=notify_type,
                        level=level,
                        title=title,
                        content=result.error_msg or "(无错误信息)",
                        context={
                            "错误类型": error_type_cn(error_type),
                            "最近步骤": step_cn(last_step),
                        },
                        task_id=task_id,
                        account_id=account_id,
                        account_display_name=display_name,
                        platform=task_platform,
                    ),
                    session=session,
                    settings=settings,
                )

        # 终态回写飞书
        _writeback_to_feishu(video_record_id, result, settings)


def _writeback_to_feishu(
    record_id: str,
    result: PublishResult,
    settings: Settings,
) -> None:
    """任务终态回写飞书 Bitable。cookie_expired 时保留'已计划'等扫码后重跑。"""
    if result.dry_run:
        return
    if not settings.feishu.enabled or not settings.feishu.sync.write_back_enabled:
        return
    if result.error_type == "cookie_expired":
        return

    fm = settings.feishu.field_map
    fields: dict[str, str] = {}
    if result.ok:
        fields[fm.status] = "已发布"
        if result.remote_url:
            fields[fm.remote_url] = result.remote_url
    else:
        fields[fm.status] = "失败"
        fields[fm.error_message] = result.error_msg or "(无错误信息)"

    try:
        client = make_client(settings.feishu.app_id, settings.feishu.app_secret)
        writeback_row(
            client,
            app_token=settings.feishu.bitable.app_token,
            table_id=settings.feishu.bitable.table_id,
            record_id=record_id,
            fields=fields,
        )
    except FeishuApiError as exc:
        logger.warning(f"[taobao] 飞书回写失败 task={result.task_id} record={record_id}: {exc}")
    except Exception as exc:
        logger.exception(f"[taobao] 飞书回写未预料异常 task={result.task_id}: {exc}")
