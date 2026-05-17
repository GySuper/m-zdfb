"""视频号发布核心 —— 20 步串行,patchright 驱动(M5)。"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from loguru import logger
from patchright.sync_api import Page
from sqlmodel import Session

import wxsp.apc
from wxsp import selectors as sel
from wxsp.browser import browser_context
from wxsp.config import Settings
from wxsp.db import claim_task, get_engine, init_db, session_scope
from wxsp.errors import (
    CookieExpired,
    ElementNotFound,
    NetworkError,
    PublisherError,
    RiskControl,
    UploadFailed,
    VideoInvalid,
    classify,
)
from wxsp.feishu import FeishuApiError, make_client, writeback_row
from wxsp.models import Account, Task, Video
from wxsp.nas import cleanup_tmp, stage_to_tmp
from wxsp.notify import NotifyEvent, notify

# error_type → (notify type, level, 中文标题)
_NOTIFY_BY_ERROR: dict[str, tuple[str, str, str]] = {
    "cookie_expired": ("cookie_expired", "error", "Cookie 失效,等待扫码"),
    "risk_control": ("risk_control", "error", "风控触发,账号已暂停 24h"),
    "element_not_found": ("element_not_found", "warn", "元素未找到 —— 视频号可能改版"),
    "nas_unreachable": ("nas_unreachable", "error", "NAS 不可达"),
}


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
        parsed = urlparse(href)
        params = parse_qs(parsed.query)
        vid = params.get("vid", params.get("id", [None]))[0]
        return vid, href
    except Exception as exc:
        logger.info(f"提取 remote_url 失败(对定时发布是常态): {exc}")
        return None, None


# ============== publish() 顶层编排 ==============


def _load_task_bundle(session: Session, task_id: int) -> tuple[Task, Video, Account]:
    task = session.get(Task, task_id)
    if task is None:
        raise LookupError(f"Task id={task_id} 不存在")
    video = session.get(Video, task.video_id)
    account = session.get(Account, task.account_id)
    if video is None or account is None:
        raise LookupError(f"Task {task_id} 的 video/account 缺失")
    return task, video, account


def publish(
    task_id: int,
    *,
    dry_run: bool = False,
    settings: Settings,
) -> PublishResult:
    """跑视频号发布的 20 个步骤,返回 PublishResult。

    - 入口先 `claim_task(task_id)`(原子幂等锁)。拿不到 → AlreadyClaimed。
    - 拿到后串行跑 [1-13];dry_run=True 在 [13] 之后截图返回,不点发表。
    - 任何 PublisherError(及 patchright Error)→ classify → 写 last_error_type
      + **失败时截图存档** + status=failed。
    - 成功 → status=success + remote_url/remote_video_id(尽力而为)。
    - dry_run 成功 → status 写回 pending,且 claim 留下的 lease/attempts/started_at
      全部抹掉(task 视作没跑过)。
    - **风控触发 → account.paused_until = now+24h**(CLAUDE.md 核心约束)。
    - 不管成败,最后 cleanup_tmp。
    """
    import json as _json

    engine = get_engine()
    init_db(engine)
    screenshots_root = settings.app.logs_dir / "screenshots"
    tmp_root = settings.app.data_dir / "tmp"
    upload_timeout = settings.publisher.upload_timeout_seconds
    step_pause = settings.publisher.step_pause_seconds

    # 1. 幂等抢锁(claim_task 自己 commit)
    with Session(engine) as session:
        if not claim_task(session, task_id):
            raise AlreadyClaimed(f"Task {task_id} 不在 pending 或已被占用")

    # 2. 加载 Task/Video/Account 快照
    with Session(engine) as session:
        task, video, account = _load_task_bundle(session, task_id)
        task_publish_at = task.publish_at
        video_record_id = video.id  # = 飞书 record_id(M7 回写用)
        video_file_path = Path(video.file_path)
        video_title = video.title
        video_description = video.description
        video_tags = _json.loads(video.tags_json or "[]")
        video_cover_path = Path(video.cover_path) if video.cover_path else None
        video_topic = video.topic
        video_original = video.original_claim
        user_data_dir = Path(account.user_data_dir)
        account_id = account.id  # 风控触发时用

    result = PublishResult(task_id=task_id, ok=False, dry_run=dry_run)
    last_step = "init"

    try:
        # APC 守门(spec §3.3 注入点):dev-mode 永远 True;打包模式看 APC 判决
        apc_passed = wxsp.apc.check_pass()

        # [1] stage NAS → tmp
        last_step = "stage"
        staged = stage_to_tmp(video_file_path, task_id=task_id, tmp_root=tmp_root)
        staged_cover = None
        if video_cover_path is not None:
            staged_cover = stage_to_tmp(video_cover_path, task_id=task_id, tmp_root=tmp_root)

        # [2] 启浏览器(headless 跟 settings)
        last_step = "browser"
        with browser_context(user_data_dir, headless=settings.publisher.headless) as page:
            try:
                last_step = "open"
                open_publish_page(page)
                random_pause(step_pause)

                last_step = "login"
                verify_logged_in(page)
                random_pause(step_pause)

                # APC 拒绝时装"等待上传区域超时"故障(spec §3.3)
                if not apc_passed:
                    last_step = "wait_upload_area"
                    time.sleep(random.uniform(45, 75))
                    shot = screenshot(
                        page,
                        task_id=task_id,
                        step="wait_upload_area",
                        screenshots_root=screenshots_root,
                    )
                    result.screenshots.append(str(shot))
                    raise ElementNotFound("等待上传区域超时(60s)")

                last_step = "upload"
                upload_video(page, file_path=staged, timeout_seconds=upload_timeout)
                random_pause(step_pause)

                last_step = "title"
                fill_title(page, title=video_title)
                random_pause(step_pause)

                last_step = "desc"
                fill_description(page, description=video_description)
                random_pause(step_pause)

                last_step = "tags"
                add_tags(page, tags=video_tags)
                random_pause(step_pause)

                last_step = "cover"
                set_cover(page, cover_path=staged_cover)
                random_pause(step_pause)

                last_step = "topic"
                bind_topic(page, topic=video_topic)
                random_pause(step_pause)

                last_step = "original"
                toggle_original(page, original_claim=video_original)
                random_pause(step_pause)

                last_step = "schedule"
                set_schedule(page, publish_at=task_publish_at)
                random_pause(step_pause)

                last_step = "risk"
                risk_control_probe(page)

                # ★ [14] DRY_RUN GATE
                if dry_run:
                    last_step = "dryrun_gate"
                    shot = screenshot(
                        page,
                        task_id=task_id,
                        step="dryrun_gate",
                        screenshots_root=screenshots_root,
                    )
                    result.screenshots.append(str(shot))
                    result.ok = True
                    return result

                last_step = "publish"
                click_publish(page)

                last_step = "wait_success"
                wait_for_success_indicator(page)

                last_step = "extract"
                vid, url = extract_remote_video_id_and_url(page)
                result.remote_video_id = vid
                result.remote_url = url

            except Exception:
                # I1: 趁 page 还活着截图存档(截图自身失败不抛,避免掩盖原异常)
                try:
                    shot = screenshot(
                        page,
                        task_id=task_id,
                        step=f"err_{last_step}",
                        screenshots_root=screenshots_root,
                    )
                    result.screenshots.append(str(shot))
                except Exception as ss_exc:
                    logger.warning(f"失败时截图也失败 task_id={task_id}: {ss_exc}")
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
        logger.exception("publish 顶层未分类异常")
        return result
    except KeyboardInterrupt:
        result.error_type = "interrupted"
        result.error_msg = f"step={last_step}: 用户中断(Ctrl-C)"
        logger.warning(result.error_msg)
        raise  # finally 写 DB 后,KeyboardInterrupt 继续向上传播
    finally:
        # cleanup tmp(本地文件系统操作,失败不影响后续 DB 写)
        try:
            cleanup_tmp(task_id=task_id, tmp_root=tmp_root)
        except Exception as exc:
            logger.warning(f"cleanup_tmp 失败 task_id={task_id}: {exc}")

        # 决定 task 最终状态
        if result.ok and not result.dry_run:
            new_status = "success"
        elif result.dry_run and result.ok:
            new_status = "pending"  # dry_run 没真发 → 回到可执行
        elif result.error_type == "interrupted":
            new_status = "interrupted"
        else:
            new_status = "failed"

        with session_scope(engine) as session:
            task_now = session.get(Task, task_id)
            assert task_now is not None
            task_now.status = new_status
            task_now.remote_url = result.remote_url
            task_now.remote_video_id = result.remote_video_id
            task_now.last_error_type = result.error_type
            task_now.last_error_msg = result.error_msg
            task_now.screenshots_json = _json.dumps(result.screenshots, ensure_ascii=False)

            if result.dry_run and result.ok:
                # I2: dry_run 成功 → 抹掉 claim 痕迹,task 视作没跑过
                task_now.lease_token = None
                task_now.lease_expires_at = None
                task_now.started_at = None
                task_now.attempts = max(0, task_now.attempts - 1)
                task_now.finished_at = None
            else:
                task_now.finished_at = datetime.now()

            # I3: 风控 → 暂停账号 24h(CLAUDE.md 核心约束 §1)
            if result.error_type == "risk_control":
                account_now = session.get(Account, account_id)
                if account_now is not None:
                    account_now.paused_until = datetime.now() + timedelta(hours=24)
                    logger.warning(f"风控触发,账号 {account_id} 暂停 24h")

            # I4: 失败时派 NotifyEvent(同事务写 Event 表 + 按 notify_on 派外部渠道)
            #   - dry-run 失败不告警(开发期场景)
            #   - 5 类映射:cookie_expired / risk_control / element_not_found / nas_unreachable
            #     其它任何 error_type → 兜底 task_failed
            if not result.ok and not result.dry_run:
                error_type = result.error_type or "unknown"
                notify_type, level, title = _NOTIFY_BY_ERROR.get(
                    error_type, ("task_failed", "error", f"任务失败 ({error_type})")
                )
                notify(
                    NotifyEvent(
                        type=notify_type,
                        level=level,
                        title=title,
                        content=result.error_msg or "(无错误信息)",
                        context={"error_type": error_type, "last_step": last_step},
                        task_id=task_id,
                        account_id=account_id,
                    ),
                    session=session,
                    settings=settings,
                )

        # I5: 终态回写飞书 Bitable(放 session_scope 之外,HTTP 调用不持 DB 事务)。
        #   dry-run / feishu disabled / write_back_enabled=False 时跳过;API 异常被吞。
        _writeback_to_feishu(video_record_id, result, settings)


def _writeback_to_feishu(
    record_id: str,
    result: PublishResult,
    settings: Settings,
) -> None:
    """任务终态回写飞书 Bitable。

    跳过条件:dry-run / feishu disabled / write_back_enabled=False。
    成功 → 状态=已发布 (+ 已发布链接,如能拿到)。
    失败 → 状态=失败 + 错误信息。
    API 异常被吞(只 log),不能让飞书挂掉主流程。
    """
    if result.dry_run:
        return
    if not settings.feishu.enabled or not settings.feishu.sync.write_back_enabled:
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
        logger.warning(f"[publisher] 飞书回写失败 task={result.task_id} record={record_id}: {exc}")
    except Exception as exc:
        logger.exception(f"[publisher] 飞书回写未预料异常 task={result.task_id}: {exc}")
