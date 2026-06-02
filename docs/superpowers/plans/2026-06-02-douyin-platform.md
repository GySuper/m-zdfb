# 抖音平台接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给多平台发布工具加上抖音创作者中心(`creator.douyin.com`)的自动发布,沿用现有平台 adapter 模式。

**Architecture:** 新增 2 个平台文件(`douyin_selectors.py` 选择器 + `douyin.py` 步骤/Spec/Publisher),在 `platform_meta.REGISTRY` 加 1 条元数据、在 `publisher._PUBLISHERS` 注册 1 个实例。所有共享 plumbing(claim/状态机/通知/飞书回写)复用 `runner.run_publish`;config/notify/browser/validator/setup/CLI 全部从 REGISTRY 派生,无需改动。

**Tech Stack:** Python 3.10+,patchright(sync API),SQLModel,Typer,pytest。参考实现:`_ref/social-auto-upload/uploader/douyin_uploader/main.py`(异步脚本式,重写为同步 adapter)。

**Spec:** [docs/superpowers/specs/2026-06-02-douyin-platform-design.md](../specs/2026-06-02-douyin-platform-design.md)

---

## 关键约束(实现者必读)

- **决策已定**:无指纹(`needs_fingerprint=False`,用 cookies.json)、纯定时发布、只发视频、无平台特有飞书字段。
- **dry-run 红线**:点发布只能写在 `_post_publish`;`_pre_publish` 必须止于点发布之前。runner 在两者之间有 dry-run gate。
- **步骤键(`ctx.last_step`)只用 `wxsp/notify.py::_STEP_CN` 已存在的规范键**:`open_publish` / `verify_login` / `upload` / `title` / `desc` / `tags` / `cover` / `schedule` / `risk` / `publish` / `wait_success`。用别的字符串告警里会漏成英文。
- **选择器是"易变文件"**:`douyin_selectors.py` 的值从参考实现迁移,**最终对真实页面校验定稿**(Task 7 实操)。
- **绝不 mock 浏览器步骤逻辑**:步骤函数的正确性靠 Task 7 的 `--dry-run` 实跑验证,不写"mock page 然后断言"的假测试。自动化测试只覆盖 REGISTRY/路由/Spec 接线这类纯结构。

---

## 文件结构

| 文件 | 职责 | 改动 |
|---|---|---|
| `wxsp/platform_meta.py` | 平台静态元数据登记表 | +1 条 `PlatformMeta("douyin", ...)` |
| `wxsp/platforms/douyin_selectors.py` | 抖音选择器(唯一改版改动点) | 新建 |
| `wxsp/platforms/douyin.py` | 步骤函数 + `DOUYIN_SPEC` + `DouyinPublisher` | 新建 |
| `wxsp/publisher.py` | 薄路由层 | +import +1 行注册 |
| `tests/test_douyin_platform.py` | REGISTRY/路由/Spec 结构回归 | 新建 |
| `CLAUDE.md` | 文档"当前支持平台"+ 目录树 | 局部更新 |
| `config_douyin.yaml` | 抖音配置 | 由 `wxsp setup` 生成(Task 5) |

---

## Task 1: platform_meta 登记 douyin

**Files:**
- Create: `tests/test_douyin_platform.py`
- Modify: `wxsp/platform_meta.py`(在 `REGISTRY` 字典 `taobao_guanghe` 条目之后加一条)

- [ ] **Step 1: Write the failing test**

创建 `tests/test_douyin_platform.py`:

```python
"""抖音平台接入回归:REGISTRY 元数据 / 路由 / Spec 接线(纯结构,不碰浏览器)。"""

from __future__ import annotations


def test_douyin_registered_in_registry() -> None:
    from wxsp.platform_meta import ALL_PLATFORMS, get_meta

    m = get_meta("douyin")
    assert m.key == "douyin"
    assert m.label == "抖音"
    assert m.title_min == 1
    assert m.needs_fingerprint is False
    assert m.field_map_defaults == {}
    assert m.login_meta["mode"] == "selector"
    assert "creator.douyin.com" in m.login_meta["home_url"]
    assert "douyin" in ALL_PLATFORMS


def test_douyin_title_min_via_validator() -> None:
    from wxsp.validator import _title_min_for

    assert _title_min_for("douyin") == 1


def test_douyin_field_map_only_common_fields() -> None:
    from wxsp.api.routes_setup import _field_map_for

    fm = _field_map_for("douyin")
    assert fm["title"] == "标题"
    assert fm["video_file"] == "视频文件"
    assert fm["publish_at"] == "定时发布时间"
    # 抖音无平台特有字段:不应混入 topic / product_ids / declaration 等
    assert "product_ids" not in fm
    assert "topic" not in fm
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_douyin_platform.py -v`
Expected: FAIL —— `get_meta("douyin")` 当前回退到 tencent_channel,`label` 是「视频号」断言失败;`"douyin" not in ALL_PLATFORMS`。

- [ ] **Step 3: Add the REGISTRY entry**

在 `wxsp/platform_meta.py` 的 `REGISTRY` 字典里,`"taobao_guanghe": PlatformMeta(...)` 条目之后插入:

```python
    "douyin": PlatformMeta(
        key="douyin",
        label="抖音",
        title_min=1,
        login_meta={
            "home_url": "https://creator.douyin.com/creator-micro/content/upload",
            "mode": "selector",
            "selector": 'div[class^="container"] input',
        },
        field_map_defaults={},
        needs_fingerprint=False,
    ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_douyin_platform.py tests/test_platform_meta_single_source.py -v`
Expected: PASS(含既有 `test_existing_platforms_consistent_across_consumers` 自动覆盖 douyin)。

- [ ] **Step 5: Commit**

```bash
git add tests/test_douyin_platform.py wxsp/platform_meta.py
git commit -m "feat(douyin): 在 platform_meta 登记抖音平台元数据"
```

---

## Task 2: 抖音选择器文件

**Files:**
- Create: `wxsp/platforms/douyin_selectors.py`

> 无独立单测(纯常量)。Task 4 的 import 会顺带验证模块可加载;选择器值在 Task 7 对真页面定稿。

- [ ] **Step 1: Create the selectors module**

创建 `wxsp/platforms/douyin_selectors.py`(值迁移自参考实现 `douyin_uploader/main.py`):

```python
# ruff: noqa: RUF001
"""抖音创作者中心选择器 —— 抖音改版时的唯一改动点。

值迁移自 _ref/social-auto-upload/uploader/douyin_uploader/main.py,
对真实页面校验定稿。优先语义化(text= / role= / placeholder=),少用脆弱 CSS class。
"""

from __future__ import annotations

# ---- 页面 URL ----
UPLOAD_PAGE = "https://creator.douyin.com/creator-micro/content/upload"
HOME_URL = "https://creator.douyin.com/"
# 上传后进入的发布页(抖音有两种 URL 变体,任一命中即算进入)
PUBLISH_PAGE_URLS = (
    "https://creator.douyin.com/creator-micro/content/publish?enter_from=publish_page",
    "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page",
)
# 发布成功后跳转(glob)
MANAGE_URL_GLOB = "https://creator.douyin.com/creator-micro/content/manage**"

# ---- 登录态 ----
LOGIN_TEXT_MARKERS = ("扫码登录", "手机号登录")  # 任一出现 = 未登录
LOGGED_IN_HOME_PREFIX = "https://creator.douyin.com/creator-micro/home"
# platform_meta.login_meta 用的"已登录可见"指示元素(对真页面定稿)
LOGGED_IN_INDICATOR = 'div[class^="container"] input'

# ---- 视频上传 ----
VIDEO_FILE_INPUT = "div[class^='container'] input"
UPLOAD_DONE_MARKER = '[class^="long-card"] div:has-text("重新上传")'
UPLOAD_FAILED_MARKER = 'div.progress-div > div:has-text("上传失败")'
UPLOAD_RETRY_INPUT = 'div.progress-div [class^="upload-btn-input"]'

# ---- 标题 / 描述 ----
DESC_SECTION_ANCHOR = "作品描述"  # get_by_text(exact) → ancestor::div[2] → following-sibling::div[1]
TITLE_INPUT_IN_SECTION = 'input[type="text"]'
DESC_EDITOR_IN_SECTION = '.zone-container[contenteditable="true"]'
TITLE_MAX_LENGTH = 30

# ---- 封面 ----
COVER_ENTRY = 'text="选择封面"'
COVER_MODAL = 'div[id*="creator-content-modal"]'
COVER_UPLOAD_INPUT = "div[class^='semi-upload upload'] >> input.semi-upload-hidden-input"
COVER_DONE_BUTTON = 'button:visible:has-text("完成")'
COVER_EXTRACT_FOOTER = "div.extractFooter"
COVER_REQUIRED_HINT = "请设置封面后再发布"
COVER_RECOMMEND_FIRST = '[class^="recommendCover-"]'
COVER_CONFIRM_APPLY_TEXT = "是否确认应用此封面？"

# ---- 定时发布 ----
SCHEDULE_RADIO = "[class^='radio']:has-text('定时发布')"
SCHEDULE_DATETIME_INPUT = '.semi-input[placeholder="日期和时间"]'
SCHEDULE_DATETIME_FORMAT = "%Y-%m-%d %H:%M"

# ---- 发布 / 风控 / 成功 ----
PUBLISH_BUTTON_NAME = "发布"  # get_by_role("button", name=, exact=True)
RISK_CONTROL_KEYWORDS = ("操作频繁", "操作过于频繁", "请稍后再试", "账号异常")
SUCCESS_INDICATORS = ("发布成功",)
```

- [ ] **Step 2: Verify it imports**

Run: `uv run python -c "import wxsp.platforms.douyin_selectors as s; print(s.UPLOAD_PAGE, s.TITLE_MAX_LENGTH)"`
Expected: 打印 `https://creator.douyin.com/creator-micro/content/upload 30`,无异常。

- [ ] **Step 3: Commit**

```bash
git add wxsp/platforms/douyin_selectors.py
git commit -m "feat(douyin): 新增抖音创作者中心选择器"
```

---

## Task 3: 抖音发布 adapter

**Files:**
- Create: `wxsp/platforms/douyin.py`

> 步骤函数是浏览器交互逻辑(从参考实现翻译为同步),按项目约定**不写 mock 浏览器的单测**,正确性在 Task 7 用 `--dry-run` 实跑验证。本任务完成标准 = 模块通过 mypy/ruff 且可被 import(Task 4 接线时验证)。

- [ ] **Step 1: Create the adapter module**

创建 `wxsp/platforms/douyin.py`:

```python
# ruff: noqa: RUF001
"""抖音创作者中心发布实现 —— patchright(sync)驱动。

只负责浏览器交互(打开页 → 上传 → 填表 → 点发布)。claim / DB 状态机 / 通知 /
飞书回写等无差别 plumbing 全在 wxsp/platforms/runner.py 的共享编排器里。

步骤逻辑从 _ref/social-auto-upload/uploader/douyin_uploader/main.py(异步脚本式)
翻译为同步 + adapter 模式,保留其选择器与等待策略。决策:无指纹 / 纯定时 / 只发视频。
"""

from __future__ import annotations

import json as _json
import time
from datetime import datetime
from pathlib import Path

from loguru import logger
from patchright.sync_api import Locator, Page

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
from wxsp.platforms import douyin_selectors as sel
from wxsp.platforms.base import PlatformSpec, PublishContext, PublishResult, TaskBundle
from wxsp.platforms.runner import random_pause, run_publish


# ---------------------------------------------------------------------------
# step functions
# ---------------------------------------------------------------------------


def _open_publish_page(page: Page) -> None:
    page.goto(sel.UPLOAD_PAGE, wait_until="domcontentloaded")
    try:
        page.wait_for_url(sel.UPLOAD_PAGE, timeout=30_000)
    except Exception as err:
        raise NetworkError("抖音上传页加载超时") from err


def _verify_logged_in(page: Page) -> None:
    for marker in sel.LOGIN_TEXT_MARKERS:
        try:
            if page.get_by_text(marker, exact=True).count():
                raise CookieExpired("抖音登录态失效,需重新扫码登录")
        except CookieExpired:
            raise
        except Exception:
            pass


def _upload_video(page: Page, file_path: Path, timeout_seconds: int = 600) -> None:
    page.locator(sel.VIDEO_FILE_INPUT).set_input_files(str(file_path))

    # 等进入发布页(两种 URL 变体之一)
    on_publish = False
    appear_deadline = time.time() + 30
    while time.time() < appear_deadline:
        for url in sel.PUBLISH_PAGE_URLS:
            try:
                page.wait_for_url(url, timeout=3000)
                on_publish = True
                break
            except Exception:
                continue
        if on_publish:
            break
        time.sleep(0.5)
    if not on_publish:
        raise UploadFailed("上传后未进入发布页")

    # 等上传/转码完成("重新上传"标记出现 = 完成);"上传失败" → 重试一次
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            if page.locator(sel.UPLOAD_DONE_MARKER).count() > 0:
                logger.info("[douyin] 视频上传完成")
                return
            if page.locator(sel.UPLOAD_FAILED_MARKER).count() > 0:
                logger.warning("[douyin] 检测到上传失败,重新上传")
                page.locator(sel.UPLOAD_RETRY_INPUT).set_input_files(str(file_path))
        except Exception:
            pass
        time.sleep(2)
    raise UploadFailed("视频上传/处理超时")


def _desc_section(page: Page) -> Locator:
    """定位"作品描述"区块(标题 input + 描述编辑器都在里面)。"""
    return (
        page.get_by_text(sel.DESC_SECTION_ANCHOR, exact=True)
        .locator("xpath=ancestor::div[2]")
        .locator("xpath=following-sibling::div[1]")
    )


def _fill_title(page: Page, title: str) -> None:
    if not title:
        return
    inp = _desc_section(page).locator(sel.TITLE_INPUT_IN_SECTION).first
    inp.wait_for(state="visible", timeout=10_000)
    inp.fill(title[: sel.TITLE_MAX_LENGTH])


def _fill_description(page: Page, description: str | None, fallback_title: str) -> None:
    text = description or fallback_title
    if not text:
        return
    editor = _desc_section(page).locator(sel.DESC_EDITOR_IN_SECTION).first
    editor.wait_for(state="visible", timeout=10_000)
    editor.click()
    page.keyboard.press("Control+KeyA")
    page.keyboard.press("Delete")
    page.keyboard.type(text)


def _add_tags(page: Page, tags: list[str]) -> None:
    for tag in tags:
        page.keyboard.type(" #" + tag)
        page.keyboard.press("Space")


def _set_cover(page: Page, cover_path: Path | None) -> None:
    """有自定义封面 → 上传横版封面;无封面留给 _handle_auto_cover 在发布前兜底。"""
    if cover_path is None:
        return
    page.click(sel.COVER_ENTRY)
    modal = page.locator(sel.COVER_MODAL)
    modal.wait_for(timeout=10_000)
    page.wait_for_timeout(1000)
    modal.locator(sel.COVER_UPLOAD_INPUT).set_input_files(str(cover_path))
    page.wait_for_timeout(2000)
    modal.locator(sel.COVER_DONE_BUTTON).click()
    try:
        page.locator(sel.COVER_EXTRACT_FOOTER).wait_for(state="detached", timeout=10_000)
    except Exception:
        logger.warning("[douyin] 封面弹窗未按预期关闭,继续")
    logger.info("[douyin] 自定义封面设置完成")


def _handle_auto_cover(page: Page) -> None:
    """平台要求封面但未设自定义封面时,选第一个推荐封面(发布前兜底,失败不阻断)。"""
    try:
        if not page.get_by_text(sel.COVER_REQUIRED_HINT).first.is_visible():
            return
    except Exception:
        return
    rec = page.locator(sel.COVER_RECOMMEND_FIRST).first
    if not rec.count():
        return
    try:
        rec.click()
        page.wait_for_timeout(1000)
        if page.get_by_text(sel.COVER_CONFIRM_APPLY_TEXT).first.is_visible():
            page.get_by_role("button", name="确定").click()
            page.wait_for_timeout(1000)
        logger.info("[douyin] 已应用推荐封面")
    except Exception as exc:
        logger.warning(f"[douyin] 应用推荐封面失败: {exc}")


def _set_schedule(page: Page, publish_at: datetime) -> None:
    page.locator(sel.SCHEDULE_RADIO).click()
    page.wait_for_timeout(1000)
    inp = page.locator(sel.SCHEDULE_DATETIME_INPUT)
    inp.click()
    page.keyboard.press("Control+KeyA")
    page.keyboard.type(publish_at.strftime(sel.SCHEDULE_DATETIME_FORMAT))
    page.keyboard.press("Enter")
    page.wait_for_timeout(1000)


def _risk_control_probe(page: Page) -> None:
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return
    for kw in sel.RISK_CONTROL_KEYWORDS:
        if kw in body_text:
            raise RiskControl(f"页面命中风控关键词: {kw}")


def _click_publish(page: Page) -> None:
    btn = page.get_by_role("button", name=sel.PUBLISH_BUTTON_NAME, exact=True)
    btn.wait_for(state="visible", timeout=10_000)
    # 发布前若平台要求封面而未设 → 兜底选推荐封面,否则发布按钮点了也发不出去
    _handle_auto_cover(page)
    btn.click()


def _wait_for_success(page: Page, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            page.wait_for_url(sel.MANAGE_URL_GLOB, timeout=3000)
            logger.info("[douyin] 已跳转作品管理页,发布成功")
            return
        except Exception:
            _handle_auto_cover(page)
            time.sleep(0.5)
    raise ElementNotFound("发布成功判定超时")


# ---------------------------------------------------------------------------
# 平台步骤回调 + Spec + Publisher
# ---------------------------------------------------------------------------


def _pre_publish(page: Page, bundle: TaskBundle, staged: Path, ctx: PublishContext) -> None:
    """打开页 → 上传 → 填表 → 封面 → 定时 → 风控探测(止于 dry-run gate 之前)。"""
    step_pause = ctx.step_pause
    upload_timeout = ctx.settings.publisher.upload_timeout_seconds

    staged_cover = None
    if bundle.video_cover_path is not None:
        staged_cover = stage_to_tmp(
            bundle.video_cover_path, task_id=ctx.task_id, tmp_root=ctx.tmp_root
        )

    ctx.last_step = "open_publish"
    _open_publish_page(page)
    random_pause(step_pause)

    ctx.last_step = "verify_login"
    _verify_logged_in(page)
    random_pause(step_pause)

    ctx.last_step = "upload"
    _upload_video(page, file_path=staged, timeout_seconds=upload_timeout)
    random_pause(step_pause)

    ctx.last_step = "title"
    _fill_title(page, title=bundle.title)
    random_pause(step_pause)

    ctx.last_step = "desc"
    _fill_description(page, description=bundle.description, fallback_title=bundle.title)
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
    """点发布 → 等跳转判成功。抖音定时发布到点前无公开链接,不抽取 remote_url。"""
    ctx.last_step = "publish"
    _click_publish(page)

    ctx.last_step = "wait_success"
    _wait_for_success(page)


DOUYIN_SPEC = PlatformSpec(
    platform_key="douyin",
    display_name="抖音",
    pre_publish=_pre_publish,
    post_publish=_post_publish,
)


class DouyinPublisher:
    platform_key = "douyin"

    def login(self, account: Account) -> bool:
        """开浏览器导航到抖音创作者中心首页,等用户扫码登录。"""
        user_data_dir = Path(account.user_data_dir)
        logger.info(f"[douyin] 开始登录 account={account.id}")
        try:
            with browser_context(
                user_data_dir,
                headless=False,
                account_id=account.id,
                platform="douyin",
            ) as page:
                page.goto(sel.HOME_URL, wait_until="domcontentloaded")
                deadline = time.time() + 300
                while time.time() < deadline:
                    if page.url.startswith(sel.LOGGED_IN_HOME_PREFIX):
                        markers_visible = False
                        for m in sel.LOGIN_TEXT_MARKERS:
                            try:
                                if page.get_by_text(m, exact=True).first.is_visible():
                                    markers_visible = True
                                    break
                            except Exception:
                                pass
                        if not markers_visible:
                            logger.info(f"[douyin] 登录成功 account={account.id}")
                            return True
                    time.sleep(2)
                logger.warning(f"[douyin] 登录超时 account={account.id}")
                return False
        except Exception as exc:
            logger.error(f"[douyin] 登录异常 account={account.id}: {exc}")
            return False

    def publish_one(
        self,
        task_id: int,
        *,
        dry_run: bool = False,
        settings: Settings,
    ) -> PublishResult:
        return run_publish(task_id, dry_run=dry_run, settings=settings, spec=DOUYIN_SPEC)
```

- [ ] **Step 2: Verify it imports + type-checks**

Run: `uv run python -c "from wxsp.platforms.douyin import DOUYIN_SPEC, DouyinPublisher; print(DOUYIN_SPEC.platform_key, DOUYIN_SPEC.display_name)"`
Expected: 打印 `douyin 抖音`,无 ImportError。

Run: `uv run ruff check wxsp/platforms/douyin.py && uv run mypy wxsp/platforms/douyin.py`
Expected: ruff 无错;mypy 无错(若 mypy 因项目配置只能整包跑,改跑 `uv run mypy wxsp`)。

- [ ] **Step 3: Commit**

```bash
git add wxsp/platforms/douyin.py
git commit -m "feat(douyin): 新增抖音发布 adapter(步骤/Spec/Publisher)"
```

---

## Task 4: 注册到 publisher 路由

**Files:**
- Modify: `wxsp/publisher.py:13-21`(import + `_PUBLISHERS` 注册)
- Modify: `tests/test_douyin_platform.py`(追加路由 + Spec 接线测试)

- [ ] **Step 1: Add failing tests**

在 `tests/test_douyin_platform.py` 末尾追加:

```python
def test_douyin_routing_returns_douyin_publisher() -> None:
    from wxsp.platforms.douyin import DouyinPublisher
    from wxsp.publisher import _get_publisher

    assert isinstance(_get_publisher("douyin"), DouyinPublisher)


def test_douyin_spec_wiring() -> None:
    from wxsp.platforms.douyin import DOUYIN_SPEC, _post_publish, _pre_publish

    assert DOUYIN_SPEC.platform_key == "douyin"
    assert DOUYIN_SPEC.display_name == "抖音"
    assert DOUYIN_SPEC.pre_publish is _pre_publish
    assert DOUYIN_SPEC.post_publish is _post_publish
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_douyin_platform.py::test_douyin_routing_returns_douyin_publisher -v`
Expected: FAIL —— `_get_publisher("douyin")` 抛 `ValueError: Unknown platform: douyin`。

- [ ] **Step 3: Register the publisher**

在 `wxsp/publisher.py` import 区(`from wxsp.platforms.tencent_channel import ...` 旁)加:

```python
from wxsp.platforms.douyin import DouyinPublisher
```

并在 `_PUBLISHERS` 字典加一行:

```python
_PUBLISHERS: dict[str, PlatformPublisher] = {
    "tencent_channel": TencentChannelPublisher(),
    "taobao_guanghe": TaobaoGuanghePublisher(),
    "douyin": DouyinPublisher(),
}
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_douyin_platform.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add wxsp/publisher.py tests/test_douyin_platform.py
git commit -m "feat(douyin): 在 publisher 路由注册 DouyinPublisher"
```

---

## Task 5: 生成抖音配置 + 校验 setup/CLI 自动识别

**Files:**
- Create: `config_douyin.yaml`(产物,gitignore 视项目而定)

> setup 向导 / CLI / config 全从 `ALL_PLATFORMS` 派生,Task 1 之后已自动包含「抖音」。本任务只生成配置并验证。

- [ ] **Step 1: 确认 CLI 已列出抖音**

Run: `uv run python -c "from wxsp.config import ALL_PLATFORMS, platform_label; print([(p, platform_label(p)) for p in ALL_PLATFORMS])"`
Expected: 输出含 `('douyin', '抖音')`。

- [ ] **Step 2: 生成 config_douyin.yaml**

参照已有 `config_taobao_guanghe.yaml` 复制一份为 `config_douyin.yaml`,改 `feishu.field_map`(只保留共有字段:video_file/title/description/account/execute_date/publish_at/status/remote_url/error_message,删掉淘宝特有的 topic/product_ids/declaration/ai_optimize),清空 `accounts:`(账号后续去 Web UI `/config` 加),`publisher.headless` 保持 `false`。

校验配置可加载:
Run: `uv run python -c "from wxsp.config import load_settings; s = load_settings(platform='douyin'); print(s.feishu.field_map.title, s.publisher.headless)"`
Expected: 打印 `标题 False`,无校验异常。

- [ ] **Step 3: Commit(若 config_*.yaml 不在 gitignore)**

```bash
git add config_douyin.yaml 2>/dev/null; git commit -m "chore(douyin): 生成抖音平台配置模板" || echo "config 被 gitignore,跳过提交"
```

---

## Task 6: 更新 CLAUDE.md 文档

**Files:**
- Modify: `CLAUDE.md`(顶部"当前支持平台"行 + `platforms/` 目录树)

- [ ] **Step 1: 更新"当前支持平台"**

把 `CLAUDE.md` 顶部:
```
**当前支持平台**:视频号(tencent_channel)、淘宝光合(taobao_guanghe)
```
改为:
```
**当前支持平台**:视频号(tencent_channel)、淘宝光合(taobao_guanghe)、抖音(douyin)
```

- [ ] **Step 2: 在 platforms 目录树补两行**

在「平台架构」一节的 `wxsp/platforms/` 树里,`taobao_selectors.py` 之后补:
```
├── douyin.py                # 抖音步骤函数 + DOUYIN_SPEC
└── douyin_selectors.py      # 抖音选择器
```
(并把原本是 `└──` 的 taobao_selectors 行改成 `├──`,保持树形正确。)

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(douyin): CLAUDE.md 补充抖音平台"
```

---

## Task 7: 实跑验证 + 选择器定稿(手动集成)

> 这是把"翻译自参考实现的选择器"对真实抖音页面校验定稿的步骤。需要一个抖音测试号 + 一条预制视频任务。**CI 不跑**,发版前手动跑。任何选择器与真页面不符 → 回 `douyin_selectors.py` 修正后重跑(选择器是易变文件,允许迭代)。

- [ ] **Step 1: 全量单测回归**

Run: `uv run pytest -q`
Expected: 全绿(新平台未破坏既有测试)。

- [ ] **Step 2: 扫码登录**

在 `config_douyin.yaml` / Web UI `/config` 加一个抖音测试账号(指定 `user_data_dir`),然后:
Run: `uv run wxsp login <douyin_account_id>`
Expected: 弹出抖音二维码,手机扫码后终端报登录成功;`data/chrome-profiles/<id>/cookies.json` 生成。
若登录态检测有误(扫了码仍判超时)→ 回 `douyin_selectors.py` 调 `LOGIN_TEXT_MARKERS` / `LOGGED_IN_HOME_PREFIX`。

- [ ] **Step 3: doctor 验证登录态**

Run: `uv run wxsp doctor --format json`
Expected: 抖音账号 cookie 状态 ok。
若误判 → 调 `platform_meta` 的 `login_meta.selector`(选一个登录后才可见的元素)。

- [ ] **Step 4: dry-run 跑通发布全流程**

在飞书测试表建一条抖音任务(账号=测试号,执行日期=今天,定时发布时间=当前+至少 2 小时,标题/视频文件齐全),`wxsp sync` 入库后:
Run: `uv run wxsp run --task-id <N> --dry-run`
Expected: `_pre_publish` 跑完所有步骤,在 dry-run gate 截图返回,**不真发布**;查看 `logs/screenshots/<YYYYMM>/<N>_dryrun_gate.png`,确认标题/描述/标签/封面/定时都已正确填好。
任一步骤选择器超时 → 看 `logs/screenshots/<YYYYMM>/<N>_err_<step>.png` + 同名 `.html`,回 `douyin_selectors.py` 修对应选择器后重跑。

- [ ] **Step 5: (可选,发版前)真发布冒烟**

Run: `uv run wxsp run --task-id <N>`
Expected: 点发布后跳转 `content/manage`,task 终态 success,飞书回写「已发布」。

- [ ] **Step 6: 提交选择器定稿改动(如有)**

```bash
git add wxsp/platforms/douyin_selectors.py wxsp/platform_meta.py
git commit -m "fix(douyin): 选择器/登录态对真实页面定稿"
```

---

## Self-Review(写计划者已核对)

- **Spec 覆盖**:§2 改动清单 → Task 1-6;§3 platform_meta → Task 1;§4 发布流程 → Task 3;§5 登录+cookie → Task 3(login)+ Task 7;§6 selectors → Task 2 + Task 7 定稿;§7 定时约束 → Task 3 `_set_schedule`(不硬编码,页面校验);§8 不做项 → adapter 中无图文/商品/位置/立即发布/第三方开关;§9 成功标准 → Task 7 逐条。
- **占位扫描**:无 TBD/TODO;`login_meta.selector` 给了具体候选值 `div[class^="container"] input` 并在 Task 7 定稿,非占位。
- **类型一致**:`DOUYIN_SPEC` / `DouyinPublisher` / `_pre_publish` / `_post_publish` / `_get_publisher` / `_field_map_for` / `_title_min_for` 在各 Task 间命名一致;步骤键全部属于 `_STEP_CN` 既有键集。
