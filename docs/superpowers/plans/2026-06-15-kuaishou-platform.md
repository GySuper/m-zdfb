# 快手平台接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增快手平台(`kuaishou`),复用现有 adapter 架构,端到端跑通到 dry-run gate。

**Architecture:** 平台 = 一个 `PlatformSpec`(两段步骤回调)+ 步骤函数 + `login()`,只做浏览器交互;claim/状态机/通知/飞书回写全在共享 `runner.run_publish()`。新增 2 个平台文件 + 改 2 处登记(`platform_meta.REGISTRY` + `publisher._PUBLISHERS`),config/notify/browser/validator/setup/errors 一律不动(全从 `REGISTRY` 读)。决策档位对齐抖音:无指纹 / cookies.json 持久化 / 纯定时 / 仅视频。

**Tech Stack:** Python 3.10+,patchright(sync),pytest。步骤逻辑翻译自 `_ref/social-auto-upload/uploader/ks_uploader/main.py`(`KSVideo`)。

**测试策略:** 浏览器步骤函数**不做单测**(项目铁律「绝不 mock 浏览器」),靠 `--dry-run` 对真实页校验;自动化测试只覆盖结构/接线(REGISTRY 登记、路由、Spec wiring),仿照 `tests/test_douyin_platform.py`。

参考 spec:[docs/superpowers/specs/2026-06-15-kuaishou-platform-design.md](../specs/2026-06-15-kuaishou-platform-design.md)

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `wxsp/platforms/kuaishou_selectors.py` | 唯一易变点:URL、登录判据、各步骤元素、风控/成功关键词 | Create |
| `wxsp/platforms/kuaishou.py` | 步骤函数 + `_pre_publish`/`_post_publish` + `KUAISHOU_SPEC` + `KuaishouPublisher`(含 `login`) | Create |
| `wxsp/platform_meta.py` | `REGISTRY` 加 1 条 `PlatformMeta` | Modify |
| `wxsp/publisher.py` | import + `_PUBLISHERS["kuaishou"]` 加 1 行 | Modify |
| `tests/test_kuaishou_platform.py` | 结构/接线回归 | Create |

---

## Task 1: 选择器文件

**Files:**
- Create: `wxsp/platforms/kuaishou_selectors.py`

- [ ] **Step 1: 创建选择器文件**

```python
# ruff: noqa: RUF001
"""快手创作者平台选择器 —— 快手改版时的唯一改动点。

选择器移植自 _ref/social-auto-upload/uploader/ks_uploader/main.py(KSVideo),
**尚未对当前线上页实跑校验**;首次用 `wxsp run --task-id N --dry-run` 跑通后按真实页微调。
优先语义化(text= / role= / placeholder=),少用脆弱 CSS class。
"""

from __future__ import annotations

# ---- 页面 URL ----
UPLOAD_PAGE = "https://cp.kuaishou.com/article/publish/video"
UPLOAD_PAGE_GLOB = "**/article/publish/video**"
# 发布成功后跳转(glob)
MANAGE_URL_GLOB = "**/article/manage/video?status=2&from=publish**"

# ---- 登录态 ----
# 未登录访问上传页会重定向到 passport.kuaishou.com 扫码;URL 含该片段 = 未登录(同淘宝 url 模式)
LOGIN_URL_FRAGMENT = "passport.kuaishou.com"

# ---- 视频上传 ----
# 上传按钮(点击弹原生文件选择器);用 expect_file_chooser 接管
UPLOAD_BUTTON = "button[class^='_upload-btn']"
# 上传中标记:存在 = 还在传;count==0 = 完成
UPLOADING_MARKER = "text=上传中"
UPLOAD_FAILED_MARKER = "text=上传失败"
# 失败重传的隐藏 input
UPLOAD_RETRY_INPUT = 'div.progress-div [class^="upload-btn-input"]'
# 首次进页面的「我知道了」提示按钮
KNOW_BUTTON = 'button[type="button"] span:text("我知道了")'
# Joyride 新手引导遮罩
JOYRIDE_TOOLTIP = 'div[id^="react-joyride-step"] div[role="alertdialog"]'
JOYRIDE_CLOSE = '[aria-label="Skip"], [data-action="skip"], button[title="Skip"]'

# ---- 描述(快手无独立标题框;「描述」框是主文案区)----
DESC_LABEL_TEXT = "描述"
MAX_TAGS = 3  # 快手话题标签上限

# ---- 封面(可选;弹窗 best-effort,未端到端实跑)----
COVER_LABEL_TEXT = "封面设置"
COVER_MODAL = 'div[role="document"].ant-modal'
COVER_UPLOAD_TAB_TEXT = "上传封面"
COVER_CONFIRM_BUTTON_NAME = "确认"

# ---- 定时发布(ant-design DatePicker,controlled component,必须走 native setter)----
SCHEDULE_RADIO_WRAPPER = "label.ant-radio-wrapper"
SCHEDULE_RADIO_TEXT = "定时发布"
SCHEDULE_DATETIME_INPUT = 'input[placeholder="选择日期时间"]'
SCHEDULE_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# ---- 发布 / 风控 / 成功 ----
PUBLISH_BUTTON_TEXT = "发布"        # get_by_text(exact=True)
PUBLISH_CONFIRM_TEXT = "确认发布"
# 先沿用抖音那套,dry-run 时按快手实际文案补
RISK_CONTROL_KEYWORDS = ("操作频繁", "操作过于频繁", "请稍后再试", "账号异常")
SUCCESS_INDICATORS = ("发布成功",)
```

- [ ] **Step 2: 确认能 import(无语法错)**

Run: `uv run python -c "import wxsp.platforms.kuaishou_selectors as s; print(s.UPLOAD_PAGE)"`
Expected: 打印 `https://cp.kuaishou.com/article/publish/video`

- [ ] **Step 3: Commit**

```bash
git add wxsp/platforms/kuaishou_selectors.py
git commit -m "feat(kuaishou): 选择器文件(移植自 ks_uploader,待 dry-run 校验)"
```

---

## Task 2: 平台 adapter(步骤函数 + Spec + Publisher)

**Files:**
- Create: `wxsp/platforms/kuaishou.py`

> 浏览器代码,无单测(项目铁律)。本任务只保证「能 import + 结构正确」,行为靠 Task 5 的 dry-run 校验。

- [ ] **Step 1: 创建 adapter 文件**

```python
"""快手创作者平台发布实现 —— patchright(sync)驱动。

只负责浏览器交互(打开页 → 上传 → 填表 → 点发布)。claim / DB 状态机 / 通知 /
飞书回写等无差别 plumbing 全在 wxsp/platforms/runner.py 的共享编排器里。

步骤逻辑从 _ref/social-auto-upload/uploader/ks_uploader/main.py(KSVideo,异步脚本式)
翻译为同步 + adapter 模式,保留其选择器与等待策略。决策:无指纹 / 纯定时 / 只发视频。
快手发布页无独立标题框,标题填进「描述」框(description 或回退 title)。
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
    RiskControl,
    UploadFailed,
)
from wxsp.models import Account
from wxsp.nas import stage_to_tmp
from wxsp.platforms import kuaishou_selectors as sel
from wxsp.platforms.base import PlatformSpec, PublishContext, PublishResult, TaskBundle
from wxsp.platforms.runner import random_pause, run_publish, screenshot

# ---------------------------------------------------------------------------
# step functions
# ---------------------------------------------------------------------------


def _open_publish_page(page: Page) -> None:
    page.goto(sel.UPLOAD_PAGE, wait_until="domcontentloaded")
    try:
        page.wait_for_url(sel.UPLOAD_PAGE_GLOB, timeout=30_000)
    except Exception as err:
        # 未登录会被重定向到 passport,wait_for_url 超时是预期 —— 交给 _verify_logged_in 判 CookieExpired。
        # 其它 URL(既不在上传页也不在 passport)才是真的加载失败。
        if sel.LOGIN_URL_FRAGMENT not in page.url:
            raise NetworkError("快手上传页加载超时") from err


def _verify_logged_in(page: Page) -> None:
    if sel.LOGIN_URL_FRAGMENT in page.url:
        raise CookieExpired("快手登录态失效,需重新扫码登录")


def _dismiss_overlays(page: Page) -> None:
    """关掉首次进页面的「我知道了」提示 + Joyride 新手引导遮罩(都 best-effort,失败不阻断)。"""
    try:
        know = page.locator(sel.KNOW_BUTTON).first
        if know.count() and know.is_visible():
            know.click()
    except Exception:
        pass
    try:
        tooltip = page.locator(sel.JOYRIDE_TOOLTIP)
        if tooltip.count() and tooltip.first.is_visible():
            page.locator('div[role="alertdialog"]').locator(sel.JOYRIDE_CLOSE).click(force=True)
            tooltip.wait_for(state="hidden", timeout=5000)
    except Exception:
        pass


def _upload_video(page: Page, file_path: Path, timeout_seconds: int = 600) -> None:
    upload_button = page.locator(sel.UPLOAD_BUTTON)
    upload_button.wait_for(state="visible", timeout=10_000)
    with page.expect_file_chooser() as fc_info:
        upload_button.click()
    fc_info.value.set_files(str(file_path))

    # 给上传一点时间起步(否则下面 `上传中` 可能还没出现就误判完成),顺手关引导遮罩
    page.wait_for_timeout(2000)
    _dismiss_overlays(page)

    # 等上传/转码完成:`上传中` 消失 = 完成;`上传失败` → 重传一次仍失败则判失败
    deadline = time.time() + timeout_seconds
    retried = False
    while time.time() < deadline:
        try:
            if page.locator(sel.UPLOADING_MARKER).count() == 0:
                logger.info("[kuaishou] 视频上传完成")
                return
            if page.locator(sel.UPLOAD_FAILED_MARKER).count() > 0:
                if retried:
                    raise UploadFailed("视频上传失败(重传一次后仍失败)")
                logger.warning("[kuaishou] 检测到上传失败,重新上传(仅一次)")
                page.locator(sel.UPLOAD_RETRY_INPUT).set_input_files(str(file_path))
                retried = True
        except UploadFailed:
            raise
        except Exception:
            pass
        time.sleep(2)
    raise UploadFailed("视频上传/处理超时")


def _fill_description(page: Page, description: str | None, fallback_title: str) -> None:
    text = description or fallback_title
    if not text:
        return
    # 快手发布页无独立标题框,「描述」框是主文案区。发布页为全新作品,描述框初始为空,
    # 无需 select-all 清空(且 Ctrl+A 在 macOS 不是全选)。click 聚焦后键盘输入。
    editor = page.get_by_text(sel.DESC_LABEL_TEXT).locator("xpath=following-sibling::div").first
    editor.wait_for(state="visible", timeout=10_000)
    editor.click()
    page.keyboard.type(text)


def _add_tags(page: Page, tags: list[str]) -> None:
    if not tags:
        return
    # 与描述同处一个可编辑区,先回车另起避免和描述粘连/触发 @ 下拉;每个话题之间停顿等下拉登记
    page.keyboard.press("Enter")
    for tag in tags[: sel.MAX_TAGS]:
        page.keyboard.type(f"#{tag} ")
        page.wait_for_timeout(2000)


def _set_cover(page: Page, cover_path: Path | None) -> None:
    """有自定义封面 → 走「封面设置」弹窗(可选,best-effort,未端到端实跑)。"""
    if cover_path is None:
        return
    cover_label = page.locator("span").filter(has_text=sel.COVER_LABEL_TEXT)
    cover_label.wait_for(state="visible", timeout=30_000)
    cover_label.locator("xpath=../following-sibling::div[1]").locator("div").first.click()
    modal = page.locator(sel.COVER_MODAL)
    modal.wait_for(state="visible", timeout=30_000)
    tab = modal.get_by_text(sel.COVER_UPLOAD_TAB_TEXT, exact=True)
    tab.wait_for(state="visible", timeout=10_000)
    tab.click()
    file_input = modal.locator('input[type="file"]')
    file_input.wait_for(state="attached", timeout=30_000)
    file_input.set_input_files(str(cover_path))
    page.wait_for_timeout(1000)
    confirm = modal.get_by_role("button", name=sel.COVER_CONFIRM_BUTTON_NAME, exact=True)
    confirm.wait_for(state="visible", timeout=10_000)
    confirm.click()
    modal.wait_for(state="hidden", timeout=30_000)
    logger.info("[kuaishou] 自定义封面设置完成(best-effort)")


# ant-design DatePicker 是 controlled component,必须 native value setter + 冒泡事件,普通 fill 无效
_SCHEDULE_JS = """
(newValue) => {
    const input = document.querySelector('input[placeholder="选择日期时间"]');
    if (!input) return false;
    const nativeSetter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    ).set;
    nativeSetter.call(input, newValue);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
}
"""


def _set_schedule(page: Page, publish_at: datetime) -> None:
    page.locator(sel.SCHEDULE_RADIO_WRAPPER).filter(has_text=sel.SCHEDULE_RADIO_TEXT).click()
    page.wait_for_timeout(2000)
    page.locator(sel.SCHEDULE_DATETIME_INPUT).click()
    page.wait_for_timeout(1000)
    ok = page.evaluate(_SCHEDULE_JS, publish_at.strftime(sel.SCHEDULE_DATETIME_FORMAT))
    if not ok:
        raise ElementNotFound("找不到定时发布时间输入框")
    page.wait_for_timeout(1000)
    page.keyboard.press("Enter")
    page.wait_for_timeout(2000)


def _risk_control_probe(page: Page) -> None:
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return
    for kw in sel.RISK_CONTROL_KEYWORDS:
        if kw in body_text:
            raise RiskControl(f"页面命中风控关键词: {kw}")


def _click_publish(page: Page) -> None:
    btn = page.get_by_text(sel.PUBLISH_BUTTON_TEXT, exact=True)
    btn.wait_for(state="visible", timeout=10_000)
    btn.click()
    page.wait_for_timeout(1000)
    confirm = page.get_by_text(sel.PUBLISH_CONFIRM_TEXT)
    if confirm.count() > 0:
        confirm.first.click()


def _wait_for_success(page: Page, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            page.wait_for_url(sel.MANAGE_URL_GLOB, timeout=3000)
            logger.info("[kuaishou] 已跳转作品管理页,发布成功")
            return
        except Exception:
            # 跳转判据为主;个别情况只弹「发布成功」toast 不跳转,用文本兜底
            for kw in sel.SUCCESS_INDICATORS:
                try:
                    if page.get_by_text(kw).first.is_visible():
                        logger.info(f"[kuaishou] 命中成功文案「{kw}」,发布成功")
                        return
                except Exception:
                    pass
            time.sleep(0.5)
    raise ElementNotFound("发布成功判定超时")


# ---------------------------------------------------------------------------
# 平台步骤回调 + Spec + Publisher
# ---------------------------------------------------------------------------


def _pre_publish(page: Page, bundle: TaskBundle, staged: Path, ctx: PublishContext) -> None:
    """打开页 → 上传 → 填描述 → 标签 → 封面 → 定时 → 风控探测(止于 dry-run gate 之前)。"""
    step_pause = ctx.step_pause
    upload_timeout = ctx.settings.publisher.upload_timeout_seconds

    staged_cover = None
    if bundle.video_cover_path is not None:
        staged_cover = stage_to_tmp(
            bundle.video_cover_path, task_id=ctx.task_id, tmp_root=ctx.tmp_root
        )

    # APC 守门(对齐 tencent §3.3 注入点):dev-mode 永远 True;打包模式看 APC 判决
    apc_passed = wxsp.apc.check_pass()

    ctx.last_step = "open_publish"
    _open_publish_page(page)
    random_pause(step_pause)

    ctx.last_step = "verify_login"
    _verify_logged_in(page)
    random_pause(step_pause)

    # APC 拒绝时装"等待上传区域超时"故障(对齐 tencent §3.3 / douyin)
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
    """点发布 → 确认发布 → 等跳转判成功。定时发布到点前无公开链接,不抽取 remote_url。"""
    ctx.last_step = "publish"
    _click_publish(page)

    ctx.last_step = "wait_success"
    _wait_for_success(page)


KUAISHOU_SPEC = PlatformSpec(
    platform_key="kuaishou",
    display_name="快手",
    pre_publish=_pre_publish,
    post_publish=_post_publish,
)


class KuaishouPublisher:
    platform_key = "kuaishou"

    def login(self, account: Account) -> bool:
        """开浏览器到快手上传页(未登录会重定向到 passport 扫码),等 URL 离开 passport = 登录成功。"""
        user_data_dir = Path(account.user_data_dir)
        logger.info(f"[kuaishou] 开始登录 account={account.id}")
        try:
            with browser_context(
                user_data_dir,
                headless=False,
                account_id=account.id,
                platform="kuaishou",
            ) as page:
                page.goto(sel.UPLOAD_PAGE, wait_until="domcontentloaded")
                deadline = time.time() + 300
                while time.time() < deadline:
                    if sel.LOGIN_URL_FRAGMENT not in page.url:
                        logger.info(f"[kuaishou] 登录成功 account={account.id}")
                        return True
                    time.sleep(2)
                logger.warning(f"[kuaishou] 登录超时 account={account.id}")
                return False
        except Exception as exc:
            logger.error(f"[kuaishou] 登录异常 account={account.id}: {exc}")
            return False

    def publish_one(
        self,
        task_id: int,
        *,
        dry_run: bool = False,
        settings: Settings,
    ) -> PublishResult:
        return run_publish(task_id, dry_run=dry_run, settings=settings, spec=KUAISHOU_SPEC)
```

- [ ] **Step 2: 确认能 import(无语法/导入错)**

Run: `uv run python -c "from wxsp.platforms.kuaishou import KUAISHOU_SPEC, KuaishouPublisher; print(KUAISHOU_SPEC.platform_key, KuaishouPublisher().platform_key)"`
Expected: 打印 `kuaishou kuaishou`

- [ ] **Step 3: Commit**

```bash
git add wxsp/platforms/kuaishou.py
git commit -m "feat(kuaishou): 发布 adapter 步骤函数 + Spec + Publisher(含 login)"
```

---

## Task 3: 结构/接线回归测试(RED)

**Files:**
- Create: `tests/test_kuaishou_platform.py`

> 仿照 `tests/test_douyin_platform.py`。此时 kuaishou 尚未登记进 REGISTRY/_PUBLISHERS,
> 故 REGISTRY/路由/字段映射相关断言会失败 —— 这是预期的 RED;Task 4 登记后转 GREEN。

- [ ] **Step 1: 写测试**

```python
"""快手平台接入回归:REGISTRY 元数据 / 路由 / Spec 接线(纯结构,不碰浏览器)。"""

from __future__ import annotations


def test_kuaishou_registered_in_registry() -> None:
    from wxsp.platform_meta import ALL_PLATFORMS, get_meta

    m = get_meta("kuaishou")
    assert m.key == "kuaishou"
    assert m.label == "快手"
    assert m.title_min == 1
    assert m.needs_fingerprint is False
    # 快手用 tags(→话题标签)+ cover;这俩不在公共集里,放 field_map_defaults
    assert m.field_map_defaults == {"tags": "标签", "cover": "封面文件"}
    # 未登录访问上传页会重定向到 passport,按 URL 片段判定(同淘宝 url 模式)
    assert m.login_meta["mode"] == "url"
    assert m.login_meta["login_fragment"] == "passport.kuaishou.com"
    assert "cp.kuaishou.com" in m.login_meta["home_url"]
    assert "kuaishou" in ALL_PLATFORMS


def test_kuaishou_title_min_via_validator() -> None:
    from wxsp.validator import _title_min_for

    assert _title_min_for("kuaishou") == 1


def test_kuaishou_field_map_has_fields_the_adapter_uses() -> None:
    from wxsp.api.routes_setup import _field_map_for

    fm = _field_map_for("kuaishou")
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


def test_kuaishou_routing_returns_kuaishou_publisher() -> None:
    from wxsp.platforms.kuaishou import KuaishouPublisher
    from wxsp.publisher import _get_publisher

    assert isinstance(_get_publisher("kuaishou"), KuaishouPublisher)


def test_kuaishou_spec_wiring() -> None:
    from wxsp.platforms.kuaishou import KUAISHOU_SPEC, _post_publish, _pre_publish

    assert KUAISHOU_SPEC.platform_key == "kuaishou"
    assert KUAISHOU_SPEC.display_name == "快手"
    assert KUAISHOU_SPEC.pre_publish is _pre_publish
    assert KUAISHOU_SPEC.post_publish is _post_publish
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `uv run pytest tests/test_kuaishou_platform.py -v`
Expected: `test_kuaishou_spec_wiring` PASS(只依赖 Task 2 的 import);其余 4 个 FAIL/ERROR
(`get_meta("kuaishou")` 回退到 tencent_channel 故 `m.key` 不等于 `kuaishou`;`_get_publisher("kuaishou")` 抛 `ValueError`)。

- [ ] **Step 3: Commit**

```bash
git add tests/test_kuaishou_platform.py
git commit -m "test(kuaishou): 结构/接线回归(RED,待登记)"
```

---

## Task 4: 登记进 REGISTRY + publisher 路由(GREEN)

**Files:**
- Modify: `wxsp/platform_meta.py`(在 `douyin` 条目之后)
- Modify: `wxsp/publisher.py`(import + `_PUBLISHERS`)

- [ ] **Step 1: 在 `wxsp/platform_meta.py` 的 `REGISTRY` 里 `douyin` 条目之后加 kuaishou**

找到 `"douyin": PlatformMeta(...)` 整块的结尾(`needs_fingerprint=False,\n    ),`),在其后、`}` 之前插入:

```python
    "kuaishou": PlatformMeta(
        key="kuaishou",
        label="快手",
        title_min=1,
        login_meta={
            # 未登录访问上传页会重定向到 passport.kuaishou.com 扫码;URL 含该片段 = 未登录(同淘宝 url 模式)
            "home_url": "https://cp.kuaishou.com/article/publish/video",
            "mode": "url",
            "login_fragment": "passport.kuaishou.com",
        },
        # 快手用到的非公共字段:标签(→话题标签)、封面。其余公共字段在 base 集里。
        field_map_defaults={"tags": "标签", "cover": "封面文件"},
        needs_fingerprint=False,
    ),
```

- [ ] **Step 2: 在 `wxsp/publisher.py` 加 import**

把:

```python
from wxsp.platforms.douyin import DouyinPublisher
```

改为(其后加一行):

```python
from wxsp.platforms.douyin import DouyinPublisher
from wxsp.platforms.kuaishou import KuaishouPublisher
```

- [ ] **Step 3: 在 `wxsp/publisher.py` 的 `_PUBLISHERS` 字典里加一行**

把:

```python
_PUBLISHERS: dict[str, PlatformPublisher] = {
    "tencent_channel": TencentChannelPublisher(),
    "taobao_guanghe": TaobaoGuanghePublisher(),
    "douyin": DouyinPublisher(),
}
```

改为:

```python
_PUBLISHERS: dict[str, PlatformPublisher] = {
    "tencent_channel": TencentChannelPublisher(),
    "taobao_guanghe": TaobaoGuanghePublisher(),
    "douyin": DouyinPublisher(),
    "kuaishou": KuaishouPublisher(),
}
```

- [ ] **Step 4: 跑 kuaishou 测试确认 GREEN**

Run: `uv run pytest tests/test_kuaishou_platform.py -v`
Expected: 5 个全 PASS

- [ ] **Step 5: Commit**

```bash
git add wxsp/platform_meta.py wxsp/publisher.py
git commit -m "feat(kuaishou): 登记进 platform_meta REGISTRY + publisher 路由"
```

---

## Task 5: 全量回归 + 收尾

**Files:** 无新增

- [ ] **Step 1: 跑全量 pytest**

Run: `uv run pytest`
Expected: 全绿。重点关注:
- `tests/test_platform_meta_single_source.py`(注入假平台的回归)继续通过
- `tests/test_cli_run.py`(`len(calls) == len(ALL_PLATFORMS)`)自动把 kuaishou 算进去,仍通过

若某测试按 `ALL_PLATFORMS` 枚举且断言了平台总数/集合,补上 `kuaishou` 即可。

- [ ] **Step 2: 跑 lint/type(pre-commit 同款)**

Run: `uv run ruff check wxsp/platforms/kuaishou.py wxsp/platforms/kuaishou_selectors.py tests/test_kuaishou_platform.py && uv run ruff format --check wxsp/platforms/kuaishou.py wxsp/platforms/kuaishou_selectors.py tests/test_kuaishou_platform.py && uv run mypy wxsp/platforms/kuaishou.py`
Expected: 全部通过(无报错)

- [ ] **Step 3: 生成配置文件冒烟(可选,确认 setup 自动识别新平台)**

Run: `uv run wxsp setup --help` 或在 Web UI `/setup` 选「快手」生成 `config_kuaishou.yaml`
Expected: 平台列表里出现「快手」;能生成 `config_kuaishou.yaml`(field_map 含 标签/封面,不含 合集/原创/商品ID)

- [ ] **Step 4: dry-run 实跑校验(手动,需快手账号 + 一条 task)**

> 这一步**不是自动化 commit gate**,是上线前的人工验证(同抖音当初定稿方式)。
> 在飞书表造一条 execute_date=today、账号=快手账号、定时发布时间合法的 task,sync 进库后:

Run: `uv run wxsp login <kuaishou_account_id>` 扫码登录,再 `uv run wxsp run --task-id <N> --dry-run`
Expected: 浏览器开 → 上传 → 填描述/标签 →(封面)→ 定时 → 风控探测 → dry-run gate 截图返回,**不点发布**。
据真实页表现微调 `kuaishou_selectors.py`(描述框 sibling-div 定位、封面弹窗、DatePicker placeholder、风控文案最易漂移)。

- [ ] **Step 5: 整体收尾**

参照 `superpowers:finishing-a-development-branch` 决定合并/PR;或按需要把分支 `feat/kuaishou-platform` 推送。
```
