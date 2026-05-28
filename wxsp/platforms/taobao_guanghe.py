# ruff: noqa: RUF001, RUF002
"""淘宝光合平台发布实现 — 18 步，patchright 驱动，iframe 内操作。"""

from __future__ import annotations

import random
import time
from datetime import datetime
from pathlib import Path

from loguru import logger
from patchright.sync_api import FrameLocator, Page
from sqlmodel import Session

from wxsp.browser import browser_context
from wxsp.config import Settings
from wxsp.db import get_engine, init_db
from wxsp.errors import (
    CookieExpired,
    ElementNotFound,
    NetworkError,
    ProductNotFound,
    PublisherError,
    TopicNotFound,
    UploadFailed,
    classify,
)
from wxsp.models import Account, Task, Video
from wxsp.nas import cleanup_tmp, stage_to_tmp
from wxsp.platforms import taobao_selectors as sel
from wxsp.platforms.base import PublishResult

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
    page.goto(sel.PUBLISH_PAGE_URL, wait_until="domcontentloaded")
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
    desc_area = iframe.locator(sel.DESCRIPTION_AREA)
    desc_area.click()
    time.sleep(0.3)
    editor = iframe.locator(sel.DESCRIPTION_EDITOR)
    editor.fill(description[:1000])


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


def _add_products(page: Page, product_ids: str | None) -> None:
    if not product_ids:
        return
    ids = [pid.strip() for pid in product_ids.split(",") if pid.strip()]
    if not ids:
        return
    iframe = _iframe(page)
    iframe.locator(sel.PRODUCT_TRIGGER).click()
    time.sleep(1)
    iframe.locator(sel.PRODUCT_DIALOG_HEADING).wait_for(timeout=5_000)
    for pid in ids:
        iframe.locator(sel.PRODUCT_SEARCH_INPUT).fill(pid)
        iframe.locator(sel.PRODUCT_SEARCH_BUTTON).click()
        time.sleep(2)
        try:
            # Find product by text match and check its checkbox
            product_row = iframe.locator(f'text="{pid}"').first
            product_row.wait_for(timeout=5_000)
            # The checkbox is typically a sibling element
            checkbox = (
                iframe.locator(f'text="{pid}"')
                .locator("..")
                .locator("..")
                .locator("checkbox")
                .first
            )
            if checkbox.count():
                checkbox.check()
            else:
                raise ProductNotFound(f"商品ID '{pid}' 搜索无结果")
        except ProductNotFound:
            try:
                iframe.locator(sel.PRODUCT_CLOSE_BUTTON).click()
            except Exception:
                pass
            raise
        except Exception as err:
            try:
                iframe.locator(sel.PRODUCT_CLOSE_BUTTON).click()
            except Exception:
                pass
            raise ProductNotFound(f"商品ID '{pid}' 搜索无结果") from err
    iframe.locator(sel.PRODUCT_CONFIRM_BUTTON).click()
    time.sleep(1)


def _set_schedule(page: Page, publish_at: datetime) -> None:
    iframe = _iframe(page)
    iframe.locator(sel.SCHEDULE_RADIO).click()
    time.sleep(0.5)
    date_str = publish_at.strftime("%Y/%m/%d")
    time_str = publish_at.strftime("%H:%M")
    date_input = iframe.locator(sel.SCHEDULE_DATE_INPUT)
    date_input.click()
    date_input.fill(date_str)
    time_input = iframe.locator(sel.SCHEDULE_TIME_INPUT)
    time_input.click()
    time_input.fill(time_str)
    iframe.locator(sel.SCHEDULE_CONFIRM_BUTTON).click()
    time.sleep(0.5)


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
    iframe.locator(radio_sel).click()


def _toggle_ai_optimize(page: Page, on: bool) -> None:
    if not on:
        return
    iframe = _iframe(page)
    switch = iframe.locator(sel.AI_TOGGLE_SWITCH)
    switch.click()


def _disable_download(page: Page) -> None:
    iframe = _iframe(page)
    checkbox = iframe.locator(sel.DOWNLOAD_CHECKBOX)
    if checkbox.is_checked():
        checkbox.click()


def _click_publish(page: Page) -> None:
    iframe = _iframe(page)
    scheduled = iframe.locator(sel.SUBMIT_BUTTON_SCHEDULED)
    immediate = iframe.locator(sel.SUBMIT_BUTTON_IMMEDIATE)
    if scheduled.count():
        scheduled.click()
    elif immediate.count():
        immediate.click()
    else:
        raise ElementNotFound("找不到发布按钮")


def _wait_for_success_indicator(page: Page, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for indicator in sel.SUCCESS_INDICATORS:
            try:
                if page.locator(f'text="{indicator}"').count():
                    return
            except Exception:
                pass
        time.sleep(2)
    if "pubNew/video" not in page.url:
        logger.info("[taobao] 页面已跳转，视为发布成功")
        return
    raise ElementNotFound("发布成功判定超时")


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
        """执行淘宝光合发布流程 [0]-[18]."""
        engine = get_engine()
        init_db(engine)
        screenshots_root = settings.app.logs_dir / "screenshots"
        tmp_root = settings.app.data_dir / "tmp"
        pub_cfg = settings.publisher
        upload_timeout = pub_cfg.upload_timeout_seconds
        step_pause = pub_cfg.step_pause_seconds

        with Session(engine) as session:
            task, video, account = _load_task_bundle(session, task_id)
            video_file_path = Path(video.file_path)
            video_title = video.title
            video_description = video.description
            video_topic = video.topic
            # product_ids stored in tags_json for now (will be separate field later)
            product_ids_raw = (
                video.tags_json if video.tags_json and video.tags_json != "[]" else None
            )
            task_publish_at = task.publish_at
            user_data_dir = Path(account.user_data_dir)
            account_id = account.id
            # taobao-specific fields (may be None until separate fields are added to Video)
            declaration = getattr(video, "declaration", None)
            ai_optimize = getattr(video, "ai_optimize", False)

        result = PublishResult(task_id=task_id, ok=False, dry_run=dry_run)
        last_step = "init"

        try:
            # [1] stage NAS → tmp
            last_step = "stage"
            staged = stage_to_tmp(video_file_path, task_id=task_id, tmp_root=tmp_root)

            # [2] launch browser
            last_step = "browser"
            with browser_context(
                user_data_dir, headless=pub_cfg.headless, account_id=account_id
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
                    if product_ids_raw:
                        _add_products(page, product_ids=product_ids_raw)
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

                    # DRY_RUN GATE
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

                    # [15] click publish
                    last_step = "publish"
                    _click_publish(page)

                    # [16] wait success
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
                    except Exception as ss_exc:
                        logger.warning(f"[taobao] screenshot failed: {ss_exc}")
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
        finally:
            try:
                cleanup_tmp(task_id=task_id, tmp_root=tmp_root)
            except Exception as exc:
                logger.warning(f"cleanup_tmp failed: {exc}")
