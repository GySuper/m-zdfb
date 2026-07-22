# 拼多多平台接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `pinduoduo` 平台 adapter,在多多视频创作者中心定时发布带货短视频(必挂商品)。

**Architecture:** 纯加法,遵循 `platform_meta.py` 顶部 docstring 的加平台规则:新增 `pinduoduo_selectors.py` + `pinduoduo.py`,改 `publisher.py`(1 行注册)+ `platform_meta.py`(1 条 PlatformMeta)。adapter 结构照搬 `taobao_guanghe.py`(有商品绑定 + 内容声明);无 iframe、首页即发布页、无标题框(同快手)。

**Tech Stack:** Python 3.10+ / patchright(sync) / pyautogui 物理输入(human_input.py) / Typer CLI / pytest

## Global Constraints

- **核心原则**:简洁优先(最少代码)/ 外科手术式改动(只动该动的)/ 目标驱动执行(每步可验证)
- 选择器全部来自 2026-07-22 真发实测(见 `docs/superpowers/specs/2026-07-22-pinduoduo-platform-design.md`)
- 不改 config / notify / browser / validator / setup / errors / models / feishu / base.py
- 错误类型复用现有:`ProductNotFound` / `CookieExpired` / `UploadFailed` / `RiskControl` / `ElementNotFound` / `NetworkError`(无新错误)
- 步骤名复用 notify._STEP_CN 已登记的:`open_publish`/`verify_login`/`upload`/`desc`/`tags`/`products`/`cover`/`schedule`/`declaration`/`risk`/`publish`/`wait_success`
- 优先语义化定位(testid/text=/placeholder=),少用脆弱 CSS-module hash class
- 质量门:ruff + mypy --strict + pytest 全绿

---

## File Structure

| 文件 | 责任 | 改动类型 |
|---|---|---|
| `wxsp/platforms/pinduoduo_selectors.py` | 拼多多发布页所有 URL + 选择器常量(改版唯一改动点) | 新增 |
| `wxsp/platforms/pinduoduo.py` | 发布步骤函数 + `_pre_publish`/`_post_publish` + `PINDUODUO_SPEC` + `PinduoduoPublisher` | 新增 |
| `wxsp/platform_meta.py` | +1 条 `PlatformMeta` 登记拼多多身份信息 | +8 行 |
| `wxsp/publisher.py` | +1 import + `_PUBLISHERS` +1 条 | +2 行 |
| `tests/test_pinduoduo_platform.py` | 平台接入回归(纯结构,不碰浏览器):REGISTRY/路由/Spec/field_map | 新增 |

---

## Task 1: 选择器文件 `pinduoduo_selectors.py`

**Files:**
- Create: `wxsp/platforms/pinduoduo_selectors.py`

**Interfaces:**
- Produces: 所有 `sel.XXX` 常量,供 Task 2 的 `pinduoduo.py` import 使用

- [ ] **Step 1: 创建选择器文件**

```python
# ruff: noqa: RUF001, RUF002
"""拼多多多多视频发布页选择器 —— 改版时的唯一改动点。

2026-07-22 对真实账号(九阳豆浆官方旗舰店)**真发实测**(含完整定时发布流程):
登录态/上传/描述框/话题候选/内容声明下拉/商品弹窗/绑商品后展开的发布设置/
定时发布日历/封面弹窗/点发布后跳转均已命中并验证。
**仍未实测**:风控文案(未触发,沿用通用关键词)。
优先语义化定位(testid/text=),少用脆弱 CSS-module hash class。
"""

from __future__ import annotations

# ---- 登录 URL ----
# SSO 登录链:mms 登录 → 多跳重定向 → 落地 n-creator/video/home。未登录停在 mms/login。
LOGIN_URL = (
    "https://mms.pinduoduo.com/login/sso?platform=live&accessType=auto"
    "&redirectUrl=https://live.pinduoduo.com/login/checker%3FisNewCreatorFrom%3Dvideo"
    "%26referUrl%3D%252Fn-creator%252Fvideo%252Fhome%253Ffrom%253Dmms"
    "%2526msfrom%253Dmms_sidenav%26from%3Dmms"
)
HOME_URL = "https://live.pinduoduo.com/n-creator/video/home"  # 发布页 = 首页(SPA,无独立发布页 URL)
LOGIN_URL_FRAGMENT = "mms.pinduoduo.com/login"  # 未登录停在此
LOGGED_IN_URL_FRAGMENT = "live.pinduoduo.com/n-creator/video/home"  # 登录成功硬信号

# ---- 视频上传 ----
# 隐藏 input(multiple,支持批量)。实测 accept 含 .mp4/.wmv/.mov/.avi/.m4v
VIDEO_FILE_INPUT = 'input[type="file"][accept*=".mp4"]'
VIDEO_UPLOAD_AREA = "div[class^='no-video_wrap']"  # 上传区父容器(物理点击用);class 含 hash,用前缀模糊
UPLOAD_DONE_MARKER = "text=视频上传成功"  # 上传成功文案(实测出现即完成)

# ---- 描述(无标题框,主文案;同快手)----
# DraftJS 风格 contenteditable(实测可写入)。sabo-root 是 sabo 编辑器根 class。
DESC_EDITOR = 'div[contenteditable="true"].sabo-root'
DESC_MAX_LENGTH = 500  # 页内计数器 N/500

# ---- 话题(描述框内输入 #关键词 弹候选)----
TOPIC_POPOVER = ".caret-popover-root"  # 输入 #关键词 后弹出
TOPIC_ITEM = ".sabo-hash-tag-item"  # 候选项(点选才真正绑定话题)

# ---- 内容声明(必填下拉;拼多多特有,同淘宝 declaration)----
# beast-core select 组件,下拉触发器 testId 稳定
DECLARATION_TRIGGER = 'input[data-testid="beast-core-select-htmlInput"]'
# 下拉选项文本(点开下拉后用 text= 定位)
DECLARATION_OPTIONS = {
    "内容无需标注": "内容无需标注",
    "含AI生成内容": "含AI生成内容",
    "含虚构演绎内容": "含虚构演绎内容",
    "内容含营销信息": "内容含营销信息",
    "内容为转载": "内容为转载",
    "个人观点，仅供参考": "个人观点，仅供参考",
}
DECLARATION_DEFAULT = "内容无需标注"

# ---- 挂商品(商品ID tab:输入 ID → 下一步直接绑定,无需勾选)----
PRODUCT_TRIGGER = "text=添加商品"
PRODUCT_TAB_BY_ID = "text=商品ID"  # 切到商品ID tab(精确搜索)
PRODUCT_ID_INPUT = 'input[placeholder*="商品id"]'  # placeholder 含"商品id"
PRODUCT_NEXT_BUTTON = 'button:has-text("下一步")'  # 输入ID后点下一步直接绑定
# 绑定成功的标志:出现"删除商品"/"更改商品"按钮
PRODUCT_BOUND_MARKER = 'button:has-text("删除商品")'

# ---- 发布设置(绑商品后才出现)----
SCHEDULE_RADIO_CONTAINER = "text=定时发布"  # 发布设置 radio 容器(label 文本)
SCHEDULE_DATE_INPUT = 'input[data-testid="beast-core-datePicker-htmlInput"]'  # 格式 YYYY-MM-DD HH:MM:SS(带秒)
SCHEDULE_TIME_INPUT = 'input[data-testid="beast-core-timePicker-html-input"]'  # 格式 HH:MM:SS(日历面板内)
SCHEDULE_CONFIRM_BUTTON = 'button:has-text("确认")'  # 日历确认(点格子后必须点确认才生效)
SCHEDULE_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"  # 日期框格式(带秒,区别于其他平台的 %Y-%m-%d %H:%M)

# ---- 封面(可选;best-effort)----
COVER_EDIT_BUTTON = 'button:has-text("编辑封面")'
COVER_MODAL_TITLE = "封面选择"
COVER_UPLOAD_TAB = "text=本地上传"
COVER_FILE_INPUT = 'input[type="file"][accept=".jpg"]'  # accept 含 .jpg/.jpeg/.png
COVER_CONFIRM_BUTTON = 'button:has-text("确定")'

# ---- 发布 / 风控 / 成功 ----
PUBLISH_BUTTON = 'button:has-text("发布")'
# 真发实测(2026-07-22):点发布后跳转 mall-goods-video(无 toast)
SUCCESS_URL_FRAGMENT = "n-creator/video/mall-goods-video"
RISK_CONTROL_KEYWORDS = ("操作频繁", "操作过于频繁", "请稍后", "账号异常", "违规")
```

- [ ] **Step 2: 验证可 import**

Run: `cd /Users/zhaoguangyu/wechat-sph-upload && python -c "from wxsp.platforms import pinduoduo_selectors as sel; print(sel.HOME_URL)"`
Expected: 打印 `https://live.pinduoduo.com/n-creator/video/home`,无报错

- [ ] **Step 3: ruff 检查**

Run: `cd /Users/zhaoguangyu/wechat-sph-upload && ruff check wxsp/platforms/pinduoduo_selectors.py`
Expected: 无报错

- [ ] **Step 4: Commit**

```bash
cd /Users/zhaoguangyu/wechat-sph-upload
git add wxsp/platforms/pinduoduo_selectors.py
git commit -m "feat(pinduoduo): 选择器文件(真发实测结构)"
```

---

## Task 2: adapter `pinduoduo.py`

**Files:**
- Create: `wxsp/platforms/pinduoduo.py`

**Interfaces:**
- Consumes: `from wxsp.platforms import pinduoduo_selectors as sel`(Task 1 全部常量)
- Consumes: `runner.run_publish`/`random_pause`/`screenshot`;`human_input.*`;`errors.*`;`nas.stage_to_tmp`;`browser.browser_context`;`base.{PlatformSpec,PublishContext,PublishResult,TaskBundle}`
- Produces: `PINDUODUO_SPEC`(`PlatformSpec`)、`PinduoduoPublisher`(有 `platform_key`/`login`/`publish_one`)

- [ ] **Step 1: 创建 adapter 文件(步骤函数 + Spec + Publisher)**

```python
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
        page.locator(sel.PRODUCT_BOUND_MARKER).first.wait_for(
            state="visible", timeout=10_000
        )
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
            page, task_id=ctx.task_id, step="wait_upload_area", screenshots_root=ctx.screenshots_root
        )
        ctx.result.screenshots.append(str(shot))
        raise ElementNotFound("等待上传区域超时(60s)")

    ctx.last_step = "upload"
    _upload_video(page, file_path=staged, timeout_seconds=ctx.settings.publisher.upload_timeout_seconds)
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
            logger.warning(f"[pinduoduo] 商品 ID JSON 解析失败 task_id={ctx.task_id}: {bundle.product_ids_json!r}")
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
```

- [ ] **Step 2: 验证可 import**

Run: `cd /Users/zhaoguangyu/wechat-sph-upload && python -c "from wxsp.platforms.pinduoduo import PinduoduoPublisher, PINDUODUO_SPEC; print(PINDUODUO_SPEC.platform_key)"`
Expected: 打印 `pinduoduo`,无报错

- [ ] **Step 3: ruff 检查**

Run: `cd /Users/zhaoguangyu/wechat-sph-upload && ruff check wxsp/platforms/pinduoduo.py`
Expected: 无报错

- [ ] **Step 4: mypy --strict 检查**

Run: `cd /Users/zhaoguangyu/wechat-sph-upload && mypy wxsp/platforms/pinduoduo.py --strict`
Expected: 无报错(如 human_input 缺类型注解导致 mypy 抱怨,沿用其他平台的处理)

- [ ] **Step 5: Commit**

```bash
cd /Users/zhaoguangyu/wechat-sph-upload
git add wxsp/platforms/pinduoduo.py
git commit -m "feat(pinduoduo): adapter 发布步骤(上传/描述/话题/商品/定时/声明)"
```

---

## Task 3: PlatformMeta 登记 + publisher 注册

**Files:**
- Modify: `wxsp/platform_meta.py`(REGISTRY +1 条)
- Modify: `wxsp/publisher.py`(+1 import + _PUBLISHERS +1 条)

**Interfaces:**
- Consumes: Task 1/2 的选择器和 adapter
- Produces: `platform_meta.REGISTRY["pinduoduo"]` + `publisher._PUBLISHERS["pinduoduo"]`(供 Task 4 测试)

- [ ] **Step 1: 在 platform_meta.py REGISTRY 加拼多多条目**

在 `xiaohongshu` 条目后(第 140 行 `},` 之后,`}` 闭合 REGISTRY 之前)插入:

```python
    "pinduoduo": PlatformMeta(
        key="pinduoduo",
        label="拼多多",
        title_min=1,  # 无标题框(同快手),title_min 仅占位
        login_meta={
            # SSO 登录链多跳重定向:未登录停 mms.pinduoduo.com/login;
            # 登录成功落地 live.pinduoduo.com/n-creator/video/home。
            "home_url": "https://live.pinduoduo.com/n-creator/video/home",
            "mode": "logged_in_url",
            "logged_in_fragment": "live.pinduoduo.com/n-creator/video/home",
        },
        field_map_defaults={
            "tags": "标签",
            "cover": "封面文件",
            "declaration": "内容声明",  # 拼多多特有(同淘宝)
            "product_ids": "商品ID",  # 拼多多特有(同淘宝)
        },
        needs_fingerprint=False,  # 跟抖音/快手/淘宝一致(用户确认)
        has_title=False,  # 无独立标题框(同快手)
    ),
```

- [ ] **Step 2: 在 publisher.py 注册**

在 import 区(第 17 行 `from wxsp.platforms.xiaohongshu import XiaohongshuPublisher` 后)加:

```python
from wxsp.platforms.pinduoduo import PinduoduoPublisher
```

在 `_PUBLISHERS` dict(第 26 行 `"xiaohongshu": XiaohongshuPublisher(),` 后)加:

```python
    "pinduoduo": PinduoduoPublisher(),
```

- [ ] **Step 3: 验证注册生效**

Run: `cd /Users/zhaoguangyu/wechat-sph-upload && python -c "from wxsp.publisher import _get_publisher; from wxsp.platforms.pinduoduo import PinduoduoPublisher; print(isinstance(_get_publisher('pinduoduo'), PinduoduoPublisher))"`
Expected: `True`

- [ ] **Step 4: ruff + mypy**

Run: `cd /Users/zhaoguangyu/wechat-sph-upload && ruff check wxsp/platform_meta.py wxsp/publisher.py && mypy wxsp/platform_meta.py wxsp/publisher.py --strict`
Expected: 无报错

- [ ] **Step 5: Commit**

```bash
cd /Users/zhaoguangyu/wechat-sph-upload
git add wxsp/platform_meta.py wxsp/publisher.py
git commit -m "feat(pinduoduo): PlatformMeta 登记 + publisher 注册"
```

---

## Task 4: 平台接入回归测试

**Files:**
- Create: `tests/test_pinduoduo_platform.py`

**Interfaces:**
- Consumes: Task 1-3 的全部产出
- Produces: 平台接入结构正确性的回归保护(REGISTRY/路由/Spec/field_map)

- [ ] **Step 1: 创建测试文件(纯结构测试,不碰浏览器)**

```python
"""拼多多平台接入回归:REGISTRY 元数据 / 路由 / Spec 接线 / field_map(纯结构,不碰浏览器)。

对齐 test_kuaishou_platform.py 的结构惯例。
"""

from __future__ import annotations


def test_pinduoduo_registered_in_registry() -> None:
    from wxsp.platform_meta import ALL_PLATFORMS, get_meta

    m = get_meta("pinduoduo")
    assert m.key == "pinduoduo"
    assert m.label == "拼多多"
    assert m.title_min == 1
    assert m.needs_fingerprint is False
    assert m.has_title is False
    # 拼多多用 tags + cover + declaration(内容声明)+ product_ids(商品ID)
    assert m.field_map_defaults == {
        "tags": "标签",
        "cover": "封面文件",
        "declaration": "内容声明",
        "product_ids": "商品ID",
    }
    # 登录态:SSO 登录链,登录成功落地 n-creator/video/home
    assert m.login_meta["mode"] == "logged_in_url"
    assert m.login_meta["logged_in_fragment"] == "live.pinduoduo.com/n-creator/video/home"
    assert "pinduoduo" in ALL_PLATFORMS


def test_pinduoduo_title_min_via_validator() -> None:
    from wxsp.validator import _title_min_for

    assert _title_min_for("pinduoduo") == 1


def test_pinduoduo_field_map_has_fields_the_adapter_uses() -> None:
    from wxsp.api.routes_setup import _field_map_for

    fm = _field_map_for("pinduoduo")
    # 共有字段
    assert fm["title"] == "标题"
    assert fm["video_file"] == "视频文件"
    assert fm["publish_at"] == "定时发布时间"
    # adapter 用的平台特有字段:tags + cover + declaration + product_ids
    assert fm["tags"] == "标签"
    assert fm["cover"] == "封面文件"
    assert fm["declaration"] == "内容声明"
    assert fm["product_ids"] == "商品ID"
    # 不该混入其他平台特有字段(视频号的合集/原创、淘宝的 ai_optimize 等)
    assert "topic" not in fm
    assert "original_claim" not in fm
    assert "ai_optimize" not in fm


def test_pinduoduo_routing_returns_pinduoduo_publisher() -> None:
    from wxsp.platforms.pinduoduo import PinduoduoPublisher
    from wxsp.publisher import _get_publisher

    assert isinstance(_get_publisher("pinduoduo"), PinduoduoPublisher)


def test_pinduoduo_spec_wiring() -> None:
    from wxsp.platforms.pinduoduo import PINDUODUO_SPEC, _post_publish, _pre_publish

    assert PINDUODUO_SPEC.platform_key == "pinduoduo"
    assert PINDUODUO_SPEC.display_name == "拼多多"
    assert PINDUODUO_SPEC.pre_publish is _pre_publish
    assert PINDUODUO_SPEC.post_publish is _post_publish
```

- [ ] **Step 2: 运行测试**

Run: `cd /Users/zhaoguangyu/wechat-sph-upload && pytest tests/test_pinduoduo_platform.py -v`
Expected: 5 passed

- [ ] **Step 3: 运行 platform_meta 单一信息源回归(确认拼多多已被消费方识别)**

Run: `cd /Users/zhaoguangyu/wechat-sph-upload && pytest tests/test_platform_meta_single_source.py -v`
Expected: 全部 passed(含 `test_existing_platforms_consistent_across_consumers`,拼多多自动纳入)

- [ ] **Step 4: 运行全量测试(确认无回归)**

Run: `cd /Users/zhaoguangyu/wechat-sph-upload && pytest -x -q`
Expected: 全绿

- [ ] **Step 5: ruff 检查测试文件**

Run: `cd /Users/zhaoguangyu/wechat-sph-upload && ruff check tests/test_pinduoduo_platform.py`
Expected: 无报错

- [ ] **Step 6: Commit**

```bash
cd /Users/zhaoguangyu/wechat-sph-upload
git add tests/test_pinduoduo_platform.py
git commit -m "test(pinduoduo): 平台接入回归(REGISTRY/路由/Spec/field_map)"
```

---

## Task 5: dry-run 对真实页校验(需测试号登录态)

> 此任务依赖拼多多账号 cookie 已登录(运行前先 `wxsp login` 扫码)。它是对真页的最后校验,
> 不产出代码,产出的是选择器微调(若有)。

**Files:**
- 可能 Modify: `wxsp/platforms/pinduoduo_selectors.py`(若 dry-run 截图发现选择器偏差)

- [ ] **Step 1: 确认拼多多账号已登录**

Run: `cd /Users/zhaoguangyu/wechat-sph-upload && wxsp login --platform pinduoduo`
(若已登录会立即返回成功)

- [ ] **Step 2: 在飞书表建一条拼多多测试任务,或用现有 task_id**

(运营侧操作,确认有一条 platform=pinduoduo 的 pending task)

- [ ] **Step 3: dry-run 运行**

Run: `cd /Users/zhaoguangyu/wechat-sph-upload && wxsp run --task-id <TASK_ID> --dry-run`
Expected: 截图显示每步命中(上传成功/描述填入/商品绑定/定时设置/内容声明选中),在 dry-run gate 处停止(不真发)

- [ ] **Step 4: 检查 dry-run 截图**

查看 `data/screenshots/` 下该 task 的截图,逐步确认:
- open_publish: 落地 n-creator/video/home
- upload: 「视频上传成功」文案
- desc: 描述框有内容
- products: 「删除商品」按钮出现(绑定成功)
- schedule: 定时 radio 选中
- declaration: 内容声明下拉显示选中项

- [ ] **Step 5: 若有选择器偏差,修正并重跑 dry-run**

(若全部命中,跳过此步)

- [ ] **Step 6: Commit(若有修正)**

```bash
cd /Users/zhaoguangyu/wechat-sph-upload
git add wxsp/platforms/pinduoduo_selectors.py
git commit -m "fix(pinduoduo): dry-run 校验后修正选择器"
```
