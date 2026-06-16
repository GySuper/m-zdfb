# 小红书平台接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 wxsp 加一个新平台「小红书」(key=`xiaohongshu`),只发视频笔记,走定时发布,接入现有共享编排器。

**Architecture:** 完全按 `CLAUDE.md`「新增一个平台 5 步法」:新增 `xiaohongshu_selectors.py` + `xiaohongshu.py` 两个平台文件,在 `platform_meta.REGISTRY` 加 1 条、在 `publisher._PUBLISHERS` 加 1 行。不改 config/notify/browser/validator/setup/errors/models/feishu/TaskBundle —— 它们全从 `REGISTRY` 读。结构照搬 `kuaishou.py`/`douyin.py`。选择器移植自 `_ref/social-auto-upload/uploader/xiaohongshu_uploader/main.py`(`XiaoHongShuVideo`)。

**Tech Stack:** Python 3.10+,patchright(sync API),pytest。设计文档见 [docs/superpowers/specs/2026-06-16-xiaohongshu-platform-design.md](../specs/2026-06-16-xiaohongshu-platform-design.md)。

**前置:浏览器步骤无法自动化测试**(项目铁律:绝不 mock 浏览器;真实页校验走 `wxsp run --task-id N --dry-run` 手动跑)。所以自动化测试只覆盖**结构接线**(registry / 路由 / spec),浏览器交互留给 Task 5 的手动 dry-run。

---

## File Structure

| 文件 | 职责 | 创建/修改 |
|---|---|---|
| `wxsp/platforms/xiaohongshu_selectors.py` | 小红书改版时唯一改动点:URL / 登录态 / 上传 / 标题描述话题 / 封面 / 定时 / 风控 / 成功的选择器常量 | 创建 |
| `wxsp/platforms/xiaohongshu.py` | 步骤函数 + `_pre_publish`/`_post_publish` + `XIAOHONGSHU_SPEC` + `XiaohongshuPublisher`(含 `login`) | 创建 |
| `wxsp/platform_meta.py` | `REGISTRY` 加 `xiaohongshu` 条目 | 修改 |
| `wxsp/publisher.py` | import + `_PUBLISHERS` 加一行 | 修改 |
| `tests/test_xiaohongshu_platform.py` | 结构回归:registry/validator/setup/路由/spec | 创建 |

---

## Task 1: 选择器文件

**Files:**
- Create: `wxsp/platforms/xiaohongshu_selectors.py`

- [ ] **Step 1: 创建选择器文件**

```python
# ruff: noqa: RUF001
"""小红书创作者中心选择器 —— 小红书改版时的唯一改动点。

选择器移植自 _ref/social-auto-upload/uploader/xiaohongshu_uploader/main.py
(XiaoHongShuVideo,patchright)。参考侧约 2026-03 对真实页校验过,但**本仓库未对
当前线上页二次校验**(标注未实跑);定稿走 `wxsp run --task-id N --dry-run` 实测微调。
优先语义化(text= / role= / placeholder=),少用脆弱 CSS class。
"""

from __future__ import annotations

# ---- 页面 URL ----
LOGIN_URL = "https://creator.xiaohongshu.com/login"
PUBLISH_VIDEO_URL = "https://creator.xiaohongshu.com/publish/publish?from=homepage&target=video"
PUBLISH_VIDEO_URL_GLOB = "**/publish/publish**target=video**"
# 发布成功后跳转(glob)
SUCCESS_URL_GLOB = "**/publish/success?**"

# ---- 登录态 ----
# 未登录访问发布页会重定向到 .../login;URL 含该片段 = 未登录(同淘宝 url 模式)。
# platform_meta.login_meta 用 url 模式 + 本片段;adapter _verify_logged_in 额外兜底登录框可见性。
LOGIN_URL_FRAGMENT = "creator.xiaohongshu.com/login"
LOGIN_BOX_SELECTOR = "div[class*='login-box']"

# ---- 视频上传 ----
VIDEO_FILE_INPUT = "div[class^='upload-content'] input.upload-input"
# 上传/转码完成判据:预览区文本含任一关键词,或标题框出现(见 adapter _upload_video)
UPLOAD_PREVIEW = 'div[class*="preview-new"]'
UPLOAD_DONE_KEYWORDS = ("上传成功", "分辨率", "重新上传", "编辑封面", "已上传", "100%")

# ---- 标题 / 描述 / 话题 ----
TITLE_INPUT = 'input[placeholder*="填写标题"]'
TITLE_MAX_LENGTH = 20  # 小红书视频标题上限 20 字
DESC_EDITOR = 'p[data-placeholder*="输入正文描述"]'
# 话题:键入 #tag 后弹下拉,选第一个候选才真正绑定
TOPIC_CONTAINER = "#creator-editor-topic-container"
TOPIC_ITEM = "#creator-editor-topic-container .item"

# ---- 封面(可选;弹窗 best-effort,未端到端实跑)----
COVER_PLUGIN_TITLE = "div.cover-plugin-title"
COVER_PLUGIN_TITLE_TEXT = "设置封面"
COVER_PREVIEW_ANCESTOR_XPATH = "xpath=ancestor::div[contains(@class, 'cover-plugin-preview')]"
COVER_ENTRY_INNER = "div.cover > div.default"
COVER_MODAL = "div.d-modal.cover-modal"
COVER_FILE_INPUT = 'input[type="file"][accept*="image"]'
COVER_CONFIRM_BUTTON = "button.mojito-button"
COVER_CONFIRM_BUTTON_TEXT = "确定"

# ---- 定时发布 ----
SCHEDULE_SWITCH_CARD = ".custom-switch-card"
SCHEDULE_SWITCH_TEXT = "定时发布"
SCHEDULE_SWITCH = ".d-switch"
SCHEDULE_DATETIME_INPUT = ".d-datepicker-input-filter input.d-text"
SCHEDULE_DATETIME_FORMAT = "%Y-%m-%d %H:%M"

# ---- 发布 / 风控 / 成功 ----
# 始终定时发布(系统强制 publish_at,无立即发布分支),故点「定时发布」按钮
PUBLISH_BUTTON = 'button:has-text("定时发布")'
# 先沿用通用文案,dry-run 时按小红书实际文案补
RISK_CONTROL_KEYWORDS = ("操作频繁", "操作过于频繁", "请稍后", "账号异常", "违规")
SUCCESS_INDICATORS = ("发布成功",)
```

- [ ] **Step 2: 验证文件可导入**

Run: `python -c "import wxsp.platforms.xiaohongshu_selectors as s; print(s.PUBLISH_VIDEO_URL, s.TITLE_MAX_LENGTH)"`
Expected: 打印 `https://creator.xiaohongshu.com/publish/publish?from=homepage&target=video 20`,无报错。

- [ ] **Step 3: Commit**

```bash
git add wxsp/platforms/xiaohongshu_selectors.py
git commit -m "feat(xiaohongshu): 新增选择器常量(移植自参考,待 dry-run 实测)"
```

---

## Task 2: platform_meta 登记 + meta 侧回归测试

**Files:**
- Create: `tests/test_xiaohongshu_platform.py`
- Modify: `wxsp/platform_meta.py`(在 `REGISTRY` dict 末尾 `kuaishou` 条目之后插入)

- [ ] **Step 1: 写失败测试(meta 侧)**

创建 `tests/test_xiaohongshu_platform.py`:

```python
"""小红书平台接入回归:REGISTRY 元数据 / 路由 / Spec 接线(纯结构,不碰浏览器)。"""

from __future__ import annotations


def test_xiaohongshu_registered_in_registry() -> None:
    from wxsp.platform_meta import ALL_PLATFORMS, get_meta

    m = get_meta("xiaohongshu")
    assert m.key == "xiaohongshu"
    assert m.label == "小红书"
    assert m.title_min == 1
    assert m.needs_fingerprint is False
    # 小红书用 tags(→话题标签)+ cover;这俩不在公共集里,放 field_map_defaults
    assert m.field_map_defaults == {"tags": "标签", "cover": "封面文件"}
    # 未登录访问发布页会跳 /login,故用 url 模式(同淘宝)
    assert m.login_meta["mode"] == "url"
    assert m.login_meta["login_fragment"] == "creator.xiaohongshu.com/login"
    assert "creator.xiaohongshu.com" in m.login_meta["home_url"]
    assert "xiaohongshu" in ALL_PLATFORMS


def test_xiaohongshu_title_min_via_validator() -> None:
    from wxsp.validator import _title_min_for

    assert _title_min_for("xiaohongshu") == 1


def test_xiaohongshu_field_map_has_fields_the_adapter_uses() -> None:
    from wxsp.api.routes_setup import _field_map_for

    fm = _field_map_for("xiaohongshu")
    assert fm["title"] == "标题"
    assert fm["video_file"] == "视频文件"
    assert fm["publish_at"] == "定时发布时间"
    # adapter 用 tags(_add_tags)+ cover(_set_cover),字段映射必须带上
    assert fm["tags"] == "标签"
    assert fm["cover"] == "封面文件"
    # 不该混入其它平台特有字段(视频号的合集/原创、淘宝的商品ID/声明等)
    assert "product_ids" not in fm
    assert "topic" not in fm
    assert "original_claim" not in fm
```

- [ ] **Step 2: 运行测试,确认失败**

Run: `uv run pytest tests/test_xiaohongshu_platform.py -v`
Expected: FAIL —— `get_meta("xiaohongshu")` 回退到 tencent_channel(`m.key == "tencent_channel"`),断言不通过。

- [ ] **Step 3: 在 REGISTRY 加条目**

在 `wxsp/platform_meta.py` 的 `REGISTRY` dict 里,`"kuaishou": PlatformMeta(...)` 条目**之后**(闭合 `}` 前)插入:

```python
    "xiaohongshu": PlatformMeta(
        key="xiaohongshu",
        label="小红书",
        title_min=1,  # 小红书视频标题无最小字数硬限(上限 20,在 adapter 截断)
        login_meta={
            # 未登录访问视频发布页 → 重定向到 .../login;URL 含该片段 = 未登录(同淘宝 url 模式)
            "home_url": (
                "https://creator.xiaohongshu.com/publish/publish?from=homepage&target=video"
            ),
            "mode": "url",
            "login_fragment": "creator.xiaohongshu.com/login",
        },
        field_map_defaults={"tags": "标签", "cover": "封面文件"},
        needs_fingerprint=False,
    ),
```

- [ ] **Step 4: 运行测试,确认通过**

Run: `uv run pytest tests/test_xiaohongshu_platform.py -v`
Expected: 3 个测试 PASS(`test_xiaohongshu_registered_in_registry` / `..._title_min_via_validator` / `..._field_map_has_fields_the_adapter_uses`)。

- [ ] **Step 5: Commit**

```bash
git add wxsp/platform_meta.py tests/test_xiaohongshu_platform.py
git commit -m "feat(xiaohongshu): 登记 platform_meta + meta 侧回归测试"
```

---

## Task 3: 发布 adapter + publisher 路由

**Files:**
- Create: `wxsp/platforms/xiaohongshu.py`
- Modify: `wxsp/publisher.py`(import 区 + `_PUBLISHERS` dict)
- Modify: `tests/test_xiaohongshu_platform.py`(追加路由 + spec 测试)

- [ ] **Step 1: 追加失败测试(路由 + spec)**

在 `tests/test_xiaohongshu_platform.py` **末尾追加**:

```python
def test_xiaohongshu_routing_returns_xiaohongshu_publisher() -> None:
    from wxsp.platforms.xiaohongshu import XiaohongshuPublisher
    from wxsp.publisher import _get_publisher

    assert isinstance(_get_publisher("xiaohongshu"), XiaohongshuPublisher)


def test_xiaohongshu_spec_wiring() -> None:
    from wxsp.platforms.xiaohongshu import XIAOHONGSHU_SPEC, _post_publish, _pre_publish

    assert XIAOHONGSHU_SPEC.platform_key == "xiaohongshu"
    assert XIAOHONGSHU_SPEC.display_name == "小红书"
    assert XIAOHONGSHU_SPEC.pre_publish is _pre_publish
    assert XIAOHONGSHU_SPEC.post_publish is _post_publish
```

- [ ] **Step 2: 运行新测试,确认失败**

Run: `uv run pytest tests/test_xiaohongshu_platform.py::test_xiaohongshu_routing_returns_xiaohongshu_publisher -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'wxsp.platforms.xiaohongshu'`。

- [ ] **Step 3: 创建 adapter 文件**

创建 `wxsp/platforms/xiaohongshu.py`:

```python
"""小红书创作者中心视频发布实现 —— patchright(sync)驱动。

只负责浏览器交互(打开页 → 上传 → 填表 → 点发布)。claim / DB 状态机 / 通知 /
飞书回写等无差别 plumbing 全在 wxsp/platforms/runner.py 的共享编排器里。

步骤逻辑从 _ref/social-auto-upload/uploader/xiaohongshu_uploader/main.py(XiaoHongShuVideo,
异步脚本式)翻译为同步 + adapter 模式,保留其选择器与等待策略。
决策:无指纹(cookies.json)/ 纯定时 / 只发视频笔记。
"""

from __future__ import annotations

import json as _json
import time
from datetime import datetime
from pathlib import Path

from loguru import logger
from patchright.sync_api import Page

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
from wxsp.platforms import xiaohongshu_selectors as sel
from wxsp.platforms.base import PlatformSpec, PublishContext, PublishResult, TaskBundle
from wxsp.platforms.runner import random_pause, run_publish

# ---------------------------------------------------------------------------
# step functions
# ---------------------------------------------------------------------------


def _open_publish_page(page: Page) -> None:
    page.goto(sel.PUBLISH_VIDEO_URL, wait_until="domcontentloaded")
    try:
        page.wait_for_url(sel.PUBLISH_VIDEO_URL_GLOB, timeout=30_000)
    except Exception as err:
        # 未登录会被重定向到 /login,此时 wait_for_url 超时属正常,登录态留给 _verify_logged_in 判;
        # 不在 /login 又超时才是真的加载失败。
        if sel.LOGIN_URL_FRAGMENT not in page.url:
            raise NetworkError("小红书发布页加载超时") from err


def _verify_logged_in(page: Page) -> None:
    if sel.LOGIN_URL_FRAGMENT in page.url:
        raise CookieExpired("小红书登录态失效(被重定向到登录页,需重新扫码登录)")
    box = page.locator(sel.LOGIN_BOX_SELECTOR).first
    try:
        if box.count() and box.is_visible():
            raise CookieExpired("小红书登录态失效(登录框可见,需重新扫码登录)")
    except CookieExpired:
        raise
    except Exception:
        pass


def _upload_video(page: Page, file_path: Path, timeout_seconds: int = 600) -> None:
    page.locator(sel.VIDEO_FILE_INPUT).set_input_files(str(file_path))

    # 等上传/转码完成:预览区文本含完成关键词,或标题框出现(进入编辑态)= 完成。
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            preview = page.locator(sel.UPLOAD_PREVIEW).first
            if preview.count():
                txt = preview.inner_text(timeout=2000)
                if any(k in txt for k in sel.UPLOAD_DONE_KEYWORDS):
                    logger.info("[xiaohongshu] 视频上传完成")
                    return
            title_box = page.locator(sel.TITLE_INPUT).first
            if title_box.count() and title_box.is_visible():
                logger.info("[xiaohongshu] 视频上传完成(标题框已出现)")
                return
        except Exception:
            pass
        time.sleep(2)
    raise UploadFailed("视频上传/处理超时")


def _fill_title(page: Page, title: str) -> None:
    if not title:
        return
    inp = page.locator(sel.TITLE_INPUT).first
    inp.wait_for(state="visible", timeout=10_000)
    inp.fill(title[: sel.TITLE_MAX_LENGTH])


def _fill_description(page: Page, description: str | None) -> None:
    if not description:
        return
    # 发布页为全新笔记,正文区初始为空,click 聚焦后直接键入(光标留末尾,供 _add_tags 追加话题)。
    editor = page.locator(sel.DESC_EDITOR).first
    editor.wait_for(state="visible", timeout=10_000)
    editor.click()
    page.keyboard.type(description)


def _add_tags(page: Page, tags: list[str]) -> None:
    if not tags:
        return
    # 正文区若还没聚焦(无描述时)先点一下;话题需键入 #tag 后从下拉选第一个候选才真正绑定。
    page.locator(sel.DESC_EDITOR).first.click()
    for tag in tags:
        page.keyboard.type("#" + tag, delay=30)
        try:
            page.locator(sel.TOPIC_CONTAINER).wait_for(state="visible", timeout=3000)
            first_item = page.locator(sel.TOPIC_ITEM).first
            first_item.wait_for(state="visible", timeout=2000)
            first_item.click()
        except Exception:
            # 下拉没弹出(网络慢/无匹配话题)→ 退而求其次:敲空格让 #tag 以纯文本留在正文
            page.keyboard.press("Space")


def _set_cover(page: Page, cover_path: Path | None) -> None:
    """有自定义封面 → 走封面弹窗上传(可选,best-effort,未端到端实跑)。"""
    if cover_path is None:
        return
    cover_title = page.locator(sel.COVER_PLUGIN_TITLE).filter(has_text=sel.COVER_PLUGIN_TITLE_TEXT)
    entry = cover_title.locator(sel.COVER_PREVIEW_ANCESTOR_XPATH).locator(sel.COVER_ENTRY_INNER)
    entry.first.wait_for(state="visible", timeout=30_000)
    entry.first.click(force=True)

    modal = page.locator(sel.COVER_MODAL)
    modal.wait_for(state="visible", timeout=30_000)
    file_input = modal.locator(sel.COVER_FILE_INPUT).first
    file_input.wait_for(state="attached", timeout=10_000)
    file_input.set_input_files(str(cover_path))
    page.wait_for_timeout(2000)

    confirm = modal.locator(sel.COVER_CONFIRM_BUTTON).filter(
        has_text=sel.COVER_CONFIRM_BUTTON_TEXT
    ).first
    confirm.wait_for(state="visible", timeout=10_000)
    confirm.click()
    modal.wait_for(state="hidden", timeout=30_000)
    logger.info("[xiaohongshu] 自定义封面设置完成(best-effort)")


def _set_schedule(page: Page, publish_at: datetime) -> None:
    # 切「定时发布」开关 → 日期时间输入框出现,fill 一次性写入(避免 Ctrl/Cmd+A 跨平台差异)。
    page.locator(sel.SCHEDULE_SWITCH_CARD).filter(has_text=sel.SCHEDULE_SWITCH_TEXT).locator(
        sel.SCHEDULE_SWITCH
    ).click()
    page.wait_for_timeout(1000)
    inp = page.locator(sel.SCHEDULE_DATETIME_INPUT)
    inp.fill(publish_at.strftime(sel.SCHEDULE_DATETIME_FORMAT))
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
    btn = page.locator(sel.PUBLISH_BUTTON).first
    btn.wait_for(state="visible", timeout=10_000)
    btn.click()


def _wait_for_success(page: Page, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            page.wait_for_url(sel.SUCCESS_URL_GLOB, timeout=3000)
            logger.info("[xiaohongshu] 已跳转成功页,发布成功")
            return
        except Exception:
            for kw in sel.SUCCESS_INDICATORS:
                try:
                    if page.get_by_text(kw).first.is_visible():
                        logger.info(f"[xiaohongshu] 命中成功文案「{kw}」,发布成功")
                        return
                except Exception:
                    pass
            time.sleep(0.5)
    raise ElementNotFound("发布成功判定超时")


# ---------------------------------------------------------------------------
# 平台步骤回调 + Spec + Publisher
# ---------------------------------------------------------------------------


def _pre_publish(page: Page, bundle: TaskBundle, staged: Path, ctx: PublishContext) -> None:
    """打开页 → 上传 → 填标题/描述/话题 → 封面 → 定时 → 风控探测(止于 dry-run gate 之前)。"""
    step_pause = ctx.step_pause

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
    _upload_video(page, file_path=staged, timeout_seconds=ctx.settings.publisher.upload_timeout_seconds)
    random_pause(step_pause)

    ctx.last_step = "title"
    _fill_title(page, title=bundle.title)
    random_pause(step_pause)

    ctx.last_step = "desc"
    _fill_description(page, description=bundle.description)
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
    """点定时发布 → 等跳成功页。定时发布到点前无公开链接,不抽取 remote_url。"""
    ctx.last_step = "publish"
    _click_publish(page)

    ctx.last_step = "wait_success"
    _wait_for_success(page)


XIAOHONGSHU_SPEC = PlatformSpec(
    platform_key="xiaohongshu",
    display_name="小红书",
    pre_publish=_pre_publish,
    post_publish=_post_publish,
)


class XiaohongshuPublisher:
    platform_key = "xiaohongshu"

    def login(self, account: Account) -> bool:
        """开浏览器到小红书登录页等扫码。

        未登录时停在 .../login(可能默认手机号登录,用户在可见浏览器里自行点切到扫一扫)。
        扫完 URL 离开 /login 且登录框消失 = 成功;cookie 由 browser_context 退出时落盘。
        """
        user_data_dir = Path(account.user_data_dir)
        logger.info(f"[xiaohongshu] 开始登录 account={account.id}")
        try:
            with browser_context(
                user_data_dir,
                headless=False,
                account_id=account.id,
                platform="xiaohongshu",
            ) as page:
                page.goto(sel.LOGIN_URL, wait_until="domcontentloaded")
                deadline = time.time() + 300
                while time.time() < deadline:
                    if sel.LOGIN_URL_FRAGMENT not in page.url:
                        box = page.locator(sel.LOGIN_BOX_SELECTOR).first
                        box_visible = False
                        try:
                            box_visible = bool(box.count()) and box.is_visible()
                        except Exception:
                            box_visible = False
                        if not box_visible:
                            logger.info(f"[xiaohongshu] 登录成功 account={account.id}")
                            return True
                    time.sleep(2)
                logger.warning(f"[xiaohongshu] 登录超时 account={account.id}")
                return False
        except Exception as exc:
            logger.error(f"[xiaohongshu] 登录异常 account={account.id}: {exc}")
            return False

    def publish_one(
        self,
        task_id: int,
        *,
        dry_run: bool = False,
        settings: Settings,
    ) -> PublishResult:
        return run_publish(task_id, dry_run=dry_run, settings=settings, spec=XIAOHONGSHU_SPEC)
```

- [ ] **Step 4: 在 publisher 注册**

修改 `wxsp/publisher.py`。在 import 区(`from wxsp.platforms.tencent_channel import ...` 一带)加:

```python
from wxsp.platforms.xiaohongshu import XiaohongshuPublisher
```

在 `_PUBLISHERS` dict 里 `"kuaishou": KuaishouPublisher(),` 后加一行:

```python
    "xiaohongshu": XiaohongshuPublisher(),
```

- [ ] **Step 5: 运行测试,确认通过**

Run: `uv run pytest tests/test_xiaohongshu_platform.py -v`
Expected: 全部 5 个测试 PASS(含新加的 `..._routing_...` / `..._spec_wiring`)。

- [ ] **Step 6: Commit**

```bash
git add wxsp/platforms/xiaohongshu.py wxsp/publisher.py tests/test_xiaohongshu_platform.py
git commit -m "feat(xiaohongshu): 视频发布 adapter + publisher 路由接线"
```

---

## Task 4: 全量回归 + lint/type 检查

**Files:** 无新增,仅运行检查。

- [ ] **Step 1: 全量 pytest**

Run: `uv run pytest -q`
Expected: 全绿。重点关注任何断言「调用次数 == `len(ALL_PLATFORMS)`」或枚举平台的既有测试(如 `tests/test_cli_run.py`)—— `ALL_PLATFORMS` 自动含 `xiaohongshu`,这类测试应自动覆盖通过;若某测试**硬编码**了平台列表(没用 `ALL_PLATFORMS`),按它原有写法补上 `xiaohongshu` 再跑。

- [ ] **Step 2: ruff + mypy(pre-commit 会跑)**

Run: `uv run ruff check wxsp/platforms/xiaohongshu.py wxsp/platforms/xiaohongshu_selectors.py wxsp/platform_meta.py wxsp/publisher.py && uv run ruff format --check wxsp/platforms/xiaohongshu.py wxsp/platforms/xiaohongshu_selectors.py`
Expected: 全过。若 `ruff format --check` 报格式差异,跑 `uv run ruff format <文件>` 修正后重跑。

Run: `uv run mypy wxsp/platforms/xiaohongshu.py`
Expected: `Success: no issues found`。若报错按提示修(类型签名对齐 kuaishou.py)。

- [ ] **Step 3: 若 Step 1/2 有改动则 commit**

```bash
git add -A && git commit -m "test(xiaohongshu): 全量回归 + lint/type 修正"
```

(无改动则跳过此步。)

---

## Task 5: setup 生成配置 + dry-run 手动实测(需小红书测试号)

**Files:** 无代码改动;这是真实页校验环节,选择器据此定稿。

- [ ] **Step 1: 验证 setup 能生成 config_xiaohongshu.yaml**

通过 Web UI `wxsp web` → 设置向导选「小红书」生成 `config_xiaohongshu.yaml`(或在已有 setup 流程里选小红书)。确认生成的 `feishu.field_map` 含 `title/description/video_file/publish_at/execute_date/account/status/remote_url/error_message` + `tags/cover`,不含 `topic/original_claim/product_ids/declaration/ai_optimize`。

- [ ] **Step 2: 扫码登录测试号**

Run: `uv run wxsp login <xiaohongshu_account_id>`
Expected: 弹出浏览器到 `creator.xiaohongshu.com/login`,扫码后命令返回登录成功;`wxsp accounts list` 显示该号 cookie 状态 ok。

- [ ] **Step 3: dry-run 跑通到发布前**

准备一条飞书任务(执行日期=今天、定时发布时间、标题、视频文件),`wxsp sync` 入库后:

Run: `uv run wxsp run --task-id <N> --dry-run`
Expected: 浏览器走完 打开发布页 → 上传 → 填标题/描述/话题 →(封面)→ 定时 → 风控探测,在 dry-run gate 截断并截图返回 `DryRunPreview`,**不点发布**。

- [ ] **Step 4: 据实测微调选择器**

任一步骤超时/选错元素 → 对照真实页修 `xiaohongshu_selectors.py`(改版易漂移项见设计文档 §6:定时开关 `.custom-switch-card`、封面弹窗、话题下拉、上传完成判定、风控文案)。修完重跑 Step 3 直到跑通。把选择器文件头注释的「未实跑」更新为「已对真实页校验(日期)」。

- [ ] **Step 5: Commit(若有选择器微调)**

```bash
git add wxsp/platforms/xiaohongshu_selectors.py
git commit -m "fix(xiaohongshu): 按真实页 dry-run 校准选择器"
```

---

## Self-Review(写完计划后核对)

**Spec coverage(逐条对设计文档):**
- §1 决策(视频/无指纹/定时/字段/标题20/key)→ Task 1 选择器 + Task 2 meta + Task 3 adapter 全覆盖 ✅
- §2 改动清单(2 新文件 + 2 登记 + config 生成)→ Task 1/2/3/5 ✅
- §3 platform_meta 条目 → Task 2 Step 3 逐字给出 ✅
- §4 发布步骤(open→verify→upload→title→desc→tags→cover→schedule→risk→gate→publish→wait_success + login)→ Task 3 adapter 全部实现 ✅
- §5 验证标准(pytest / setup / dry-run)→ Task 2/3/4/5 ✅
- §6 已知风险 → Task 5 Step 4 校验项 ✅

**Placeholder scan:** 无 TBD/TODO;每个代码步给出完整代码;命令带预期输出。✅

**Type consistency:** `_pre_publish(page, bundle, staged, ctx)` / `_post_publish(page, bundle, ctx)` 签名对齐 `PlatformSpec`;`XIAOHONGSHU_SPEC` / `XiaohongshuPublisher.platform_key` / `publish_one` / `login` 与 base 协议 + 测试断言一致;选择器常量名(`PUBLISH_VIDEO_URL`、`TITLE_INPUT`、`SCHEDULE_DATETIME_INPUT` 等)在 selectors / adapter 两处用名一致。✅

**注**:`_pre_publish` 不做 APC 守门(kuaishou/douyin 有);小红书非视频号系强风控平台,设计文档未要求 APC,保持简洁不引入(YAGNI)。若后续要对齐 APC,照 kuaishou.py 的 `wxsp.apc.check_pass()` + `wait_upload_area` 故障段补即可。
