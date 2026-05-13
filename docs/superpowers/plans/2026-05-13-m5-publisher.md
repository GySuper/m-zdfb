# M5 视频号发布核心 (publisher.py) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 patchright 把视频号发布流程拆成 20 个步骤,实现 `wxsp run --task-id N [--dry-run]`。dry-run 跑到"发布"按钮前停下并截图;正式发布走完后回写 DB(remote_url 尽力而为);重复触发由 `db.claim_task` 幂等锁挡住,只发一次。

**Architecture:**
- **3 个底层支撑模块**:`errors.py`(异常类型 + classify)、`retry.py`(`@retry_on` 指数退避装饰器)、`selectors.py`(集中所有页面选择器,改版唯一改动点)
- **核心**:`publisher.py`,每个步骤一个小函数(5-20 行),由 `publish(task_id, *, dry_run)` 顶层串起。失败时 `screenshot()` + 由 `classify(exc)` 转成 `last_error_type` 写回 Task
- **数据流**:`db.claim_task` → 加载 Task/Video/Account → `nas.stage_to_tmp` → `browser.browser_context` → 步骤 [3-13] → dry_run gate → 步骤 [15-17] → cleanup + writeback
- **测试策略**:errors/retry 单元测试;publisher 顶层用 mock Page 单元测试(失败分类、dry_run gate、AlreadyClaimed);**完整流程靠 `@pytest.mark.integration` 手动跑真账号 + 真视频**

**Tech Stack:** patchright (sync_api) · SQLModel · Typer · pytest

参考实现:`/Users/zhaoguangyu/_ref/social-auto-upload/uploader/tencent_uploader/main.py`(async,我们改 sync;选择器和等待策略可借鉴)

---

## File Structure

| 文件 | 创建/修改 | 职责 |
|---|---|---|
| `wxsp/errors.py` | **modify** | 加 8 个异常类 + `classify()` 函数;保留已有 `NasUnreachable` |
| `wxsp/retry.py` | **modify** | `@retry_on(error_types, max_attempts, backoff)` 装饰器,sync 版 |
| `wxsp/selectors.py` | **modify** | 所有视频号页面选择器常量;改版时唯一改动点 |
| `wxsp/publisher.py` | **modify** | 20 步函数 + `publish(task_id, *, dry_run)` 顶层 + `PublishResult` dataclass |
| `wxsp/cli.py` | modify | `run --task-id` 子命令接 `publisher.publish` |
| `tests/test_errors.py` | **modify** | `classify()` 单元测试 |
| `tests/test_retry.py` | create | `@retry_on` 单元测试 |
| `tests/test_publisher.py` | create | publish() 顶层逻辑(mock Page)+ AlreadyClaimed |
| `tests/test_publisher_integration.py` | create | `@pytest.mark.integration`,真账号 + dry_run 烟雾测试 |
| `tests/test_cli_run.py` | create | `wxsp run --task-id` 走通(stub publisher) |

**不动**:`browser.py`(M2 已就绪,直接复用 `browser_context`)、`nas.py`(M4 已就绪,直接复用 `stage_to_tmp/cleanup_tmp`)、`db.py`(`claim_task/transition_task` 已就绪)。

---

## Task 1: 错误类型 + classify

**Files:**
- Modify: `wxsp/errors.py`
- Modify: `tests/test_errors.py`

- [ ] **Step 1.1: 写失败测试**

把测试 append 到 `tests/test_errors.py`:

```python
import pytest
from patchright.sync_api import TimeoutError as PWTimeoutError

from wxsp.errors import (
    CookieExpired,
    ElementNotFound,
    NasUnreachable,
    NetworkError,
    PublisherError,
    RiskControl,
    UnknownError,
    UploadFailed,
    VideoInvalid,
    classify,
)


def test_classify_known_publisher_errors_returns_their_kind() -> None:
    cases = [
        (CookieExpired("x"), "cookie_expired"),
        (RiskControl("x"), "risk_control"),
        (ElementNotFound("x"), "element_not_found"),
        (UploadFailed("x"), "upload_failed"),
        (NasUnreachable("x"), "nas_unreachable"),
        (NetworkError("x"), "network"),
        (VideoInvalid("x"), "video_invalid"),
    ]
    for exc, kind in cases:
        assert classify(exc) == kind


def test_classify_playwright_timeout_is_element_not_found() -> None:
    assert classify(PWTimeoutError("超时")) == "element_not_found"


def test_classify_unknown_exception_falls_back_to_unknown() -> None:
    assert classify(RuntimeError("boom")) == "unknown"


def test_all_publisher_errors_share_base_class() -> None:
    for cls in (CookieExpired, RiskControl, ElementNotFound, UploadFailed,
                NasUnreachable, NetworkError, VideoInvalid, UnknownError):
        assert issubclass(cls, PublisherError)
```

- [ ] **Step 1.2: 跑测试,确认失败**

Run: `uv run pytest tests/test_errors.py -v`
Expected: FAIL,缺类。

- [ ] **Step 1.3: 实现 `wxsp/errors.py`**

把现有 `errors.py` 整个替换为:

```python
"""错误类型 + 分类(M5)。"""

from __future__ import annotations

from patchright.sync_api import Error as PWError
from patchright.sync_api import TimeoutError as PWTimeoutError


class PublisherError(Exception):
    """所有 publisher 业务异常的基类。"""


class NasUnreachable(PublisherError):
    """NAS 文件访问失败(M4)。"""


class CookieExpired(PublisherError):
    """登录失效:页面跳到了扫码页或检测到未登录标记。"""


class RiskControl(PublisherError):
    """页面出现风控文案(请稍后/系统繁忙/操作过于频繁/账号异常)。"""


class ElementNotFound(PublisherError):
    """目标元素在超时内未出现 —— 可能视频号改版。"""


class UploadFailed(PublisherError):
    """视频上传中断 / 页面提示上传失败。"""


class NetworkError(PublisherError):
    """网络/导航失败,可重试。"""


class VideoInvalid(PublisherError):
    """视频文件本身有问题(损坏/格式/大小);validator 该拦未拦下的兜底。"""


class UnknownError(PublisherError):
    """兜底类型,classify 返回 'unknown' 时的占位。"""


_KIND_BY_TYPE: dict[type[Exception], str] = {
    CookieExpired: "cookie_expired",
    RiskControl: "risk_control",
    ElementNotFound: "element_not_found",
    UploadFailed: "upload_failed",
    NasUnreachable: "nas_unreachable",
    NetworkError: "network",
    VideoInvalid: "video_invalid",
}


def classify(exc: BaseException) -> str:
    """把异常实例映射到 Task.last_error_type 用的字符串。

    - PublisherError 子类查表
    - patchright TimeoutError → 'element_not_found'(等待元素超时是最常见模式)
    - 其它 patchright Error → 'network'(导航/连接类)
    - 其它任何异常 → 'unknown'
    """
    for cls, kind in _KIND_BY_TYPE.items():
        if isinstance(exc, cls):
            return kind
    if isinstance(exc, PWTimeoutError):
        return "element_not_found"
    if isinstance(exc, PWError):
        return "network"
    return "unknown"
```

- [ ] **Step 1.4: 跑测试,确认通过**

Run: `uv run pytest tests/test_errors.py -v`
Expected: 4 个测试全 PASS。

- [ ] **Step 1.5: Commit**

```bash
git add wxsp/errors.py tests/test_errors.py
git commit -m "feat(errors): add publisher exception hierarchy + classify()"
```

---

## Task 2: 重试装饰器 `@retry_on`

**Files:**
- Modify: `wxsp/retry.py`
- Create: `tests/test_retry.py`

- [ ] **Step 2.1: 写失败测试**

新建 `tests/test_retry.py`:

```python
from __future__ import annotations

import pytest

from wxsp.retry import retry_on


class A(Exception):
    pass


class B(Exception):
    pass


def test_retry_on_succeeds_first_try_no_sleep() -> None:
    sleeps: list[float] = []
    calls = [0]

    @retry_on((A,), max_attempts=3, base_delay=0.1, sleep=sleeps.append)
    def fn() -> str:
        calls[0] += 1
        return "ok"

    assert fn() == "ok"
    assert calls[0] == 1
    assert sleeps == []


def test_retry_on_retries_listed_exceptions_with_exponential_backoff() -> None:
    sleeps: list[float] = []
    calls = [0]

    @retry_on((A,), max_attempts=3, base_delay=2.0, sleep=sleeps.append)
    def fn() -> str:
        calls[0] += 1
        if calls[0] < 3:
            raise A("boom")
        return "ok"

    assert fn() == "ok"
    assert calls[0] == 3
    assert sleeps == [2.0, 4.0]  # 指数:base * 2^(attempt-1)


def test_retry_on_does_not_catch_other_exceptions() -> None:
    sleeps: list[float] = []
    calls = [0]

    @retry_on((A,), max_attempts=5, base_delay=0.1, sleep=sleeps.append)
    def fn() -> None:
        calls[0] += 1
        raise B("不重试")

    with pytest.raises(B):
        fn()
    assert calls[0] == 1
    assert sleeps == []


def test_retry_on_raises_after_exhausting_attempts() -> None:
    sleeps: list[float] = []
    calls = [0]

    @retry_on((A,), max_attempts=3, base_delay=1.0, sleep=sleeps.append)
    def fn() -> None:
        calls[0] += 1
        raise A("总是失败")

    with pytest.raises(A):
        fn()
    assert calls[0] == 3
    assert sleeps == [1.0, 2.0]  # 最后一次失败前 sleep 了 2 次
```

- [ ] **Step 2.2: 跑测试确认失败**

Run: `uv run pytest tests/test_retry.py -v`
Expected: FAIL,`retry_on` 还没实现。

- [ ] **Step 2.3: 实现 `wxsp/retry.py`**

替换整个文件:

```python
"""重试装饰器:指数退避 + 异常类型白名单(M5)。"""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import wraps
from typing import TypeVar

T = TypeVar("T")


def retry_on(
    error_types: tuple[type[BaseException], ...],
    *,
    max_attempts: int,
    base_delay: float,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """异常白名单 + 指数退避的同步重试装饰器。

    - 只重试 `error_types` 元组里的异常;其它直接传出去
    - 第 N 次重试前 sleep `base_delay * 2 ** (N-1)` 秒
    - `max_attempts` 用完仍失败 → 抛最后一次的异常
    - `sleep` 参数注入用于测试(默认 `time.sleep`)
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapper(*args: object, **kwargs: object) -> T:
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except error_types as exc:
                    last_exc = exc
                    if attempt == max_attempts:
                        raise
                    sleep(base_delay * (2 ** (attempt - 1)))
            assert last_exc is not None  # unreachable
            raise last_exc

        return wrapper

    return decorator
```

- [ ] **Step 2.4: 跑测试确认通过**

Run: `uv run pytest tests/test_retry.py -v`
Expected: 4 PASS。

- [ ] **Step 2.5: Commit**

```bash
git add wxsp/retry.py tests/test_retry.py
git commit -m "feat(retry): add @retry_on decorator with exponential backoff"
```

---

## Task 3: 选择器集中模块

**Files:**
- Modify: `wxsp/selectors.py`

视频号选择器全部集中在这里,从 `_ref/social-auto-upload/uploader/tencent_uploader/main.py` 提炼。**只有常量,无逻辑**。

- [ ] **Step 3.1: 写 `wxsp/selectors.py`**

替换整个文件:

```python
"""视频号发布页选择器集中管理 —— 改版时唯一的改动点(M5)。

参考 social-auto-upload/uploader/tencent_uploader/main.py 的踩坑成果,
按发布流程的 20 步分组列出。所有定位优先用语义化选择器(text=/role=)。
"""

from __future__ import annotations

# ============== URL ==============
PUBLISH_PAGE_URL = "https://channels.weixin.qq.com/platform/post/create"
POST_LIST_URL = "https://channels.weixin.qq.com/platform/post/list"
# URL 包含此片段视为发布成功后跳转(submit_publish 等待此 URL)
POST_LIST_URL_FRAGMENT = "/post/list"

# ============== [4] 登录态判定 ==============
# 任一可见即认为已登录(扫码框存在时反向证明未登录)
LOGGED_IN_SELECTORS = (
    'div:has-text("发表视频")',
    'button:has-text("发表")',
    'button:has-text("保存草稿")',
)
LOGIN_QRCODE_SELECTORS = (
    "div.login-qrcode-wrap",
    "div.qrcode-wrap",
    "img.qrcode",
)

# ============== [5] 视频上传 ==============
FILE_INPUT = 'input[type="file"]'
# 上传完成判定:发表按钮的 class 不含 weui-desktop-btn_disabled
UPLOAD_PUBLISH_BUTTON_ROLE = ("button", "发表")
UPLOAD_DISABLED_CLASS = "weui-desktop-btn_disabled"
UPLOAD_FAILED_INDICATOR = "div.status-msg.error"
UPLOAD_DELETE_TAG = 'div.media-status-content div.tag-inner:has-text("删除")'

# ============== [6][7][8] 标题 / 描述 / 标签 ==============
# 视频号发布页:标题 + 描述 + tag 全部进 .input-editor 富文本,用键盘 type
TITLE_EDITOR = "div.input-editor"
SHORT_TITLE_LABEL_TEXT = "短标题"

# ============== [9] 封面 ==============
COVER_ENTRY_SELECTORS = (
    'div.vertical-cover-wrap:has-text("个人主页卡片"):has-text("3:4")',
    'div.vertical-cover-wrap:has-text("3:4")',
    'div.vertical-cover-wrap:has-text("个人主页卡片")',
)
COVER_DIALOG_HAS_TEXT = "编辑个人主页卡片"
COVER_FILE_INPUT = '.single-cover-uploader-wrap input[type="file"]'
COVER_CROP_DIALOG_HAS_TEXT = "裁剪封面图"
COVER_CROP_CONFIRM = (
    'div.weui-desktop-dialog__ft button.weui-desktop-btn_primary:has-text("确定")'
)
COVER_CONFIRM = (
    'div.weui-desktop-dialog__ft button.weui-desktop-btn_primary:has-text("确认")'
)

# ============== [10] 合集 ==============
COLLECTION_LABEL_TEXT = "添加到合集"

# ============== [11] 原创 ==============
ORIGINAL_CHECKBOX_LABEL = "视频为原创"
ORIGINAL_TERMS_LABEL = "我已阅读并同意 《视频号原创声明使用条款》"
ORIGINAL_DECLARE_BUTTON = "声明原创"

# ============== [12] 定时发布 ==============
SCHEDULE_RADIO_LABEL_HAS_TEXT = "定时"
SCHEDULE_DATE_INPUT = 'input[placeholder="请选择发表时间"]'
SCHEDULE_MONTH_LABEL = 'span.weui-desktop-picker__panel__label:has-text("月")'
SCHEDULE_NEXT_MONTH_BTN = "button.weui-desktop-btn__icon__right"
SCHEDULE_DAY_TABLE = "table.weui-desktop-picker__table a"
SCHEDULE_DAY_DISABLED_CLASS = "weui-desktop-picker__disabled"
SCHEDULE_TIME_INPUT = 'input[placeholder="请选择时间"]'

# ============== [13] 风控文案 ==============
# 任一在页面 body 文本中出现 → RiskControl
RISK_CONTROL_KEYWORDS = (
    "请稍后",
    "系统繁忙",
    "操作过于频繁",
    "账号异常",
)

# ============== [15] 提交 ==============
SUBMIT_PUBLISH_BUTTON = 'div.form-btns button:has-text("发表")'

# ============== [17] 提取已发布 URL(尽力而为) ==============
# 视频号定时发布提交后,页面跳到 post/list,新条目在最顶。
# 这个选择器返回首行视频卡的链接;找不到就 None(对定时发布是常态)。
LIST_FIRST_ITEM_LINK = "div.post-feeds-card-wrap a.feed-card-cover-wrap"
```

- [ ] **Step 3.2: Smoke 验证模块能 import**

Run: `uv run python -c "from wxsp import selectors; print(selectors.PUBLISH_PAGE_URL)"`
Expected: 打印 `https://channels.weixin.qq.com/platform/post/create`,无异常。

- [ ] **Step 3.3: Commit**

```bash
git add wxsp/selectors.py
git commit -m "feat(selectors): centralize wechat-channels publish page selectors"
```

---

## Task 4: publisher 数据类 + 辅助函数

**Files:**
- Modify: `wxsp/publisher.py`
- Create: `tests/test_publisher.py`

本任务搭骨架:`PublishResult` dataclass、`screenshot()` 辅助、`random_pause()` 步骤间停顿。

- [ ] **Step 4.1: 写 publisher 的初版骨架 + 辅助**

替换 `wxsp/publisher.py`:

```python
"""视频号发布核心 —— 20 步串行,patchright 驱动(M5)。"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from loguru import logger
from patchright.sync_api import Page


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
    `now` 注入用于测试,默认 `datetime.now()`。
    """
    now = now or datetime.now()
    month_dir = screenshots_root / now.strftime("%Y%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    path = month_dir / f"{task_id}_{step}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
    except Exception as exc:  # 截图失败不该掩盖原始错误
        logger.warning(f"截图失败 task_id={task_id} step={step}: {exc}")
    return path


def random_pause(
    range_seconds: tuple[float, float],
    *,
    sleep: callable = time.sleep,  # type: ignore[valid-type]
) -> None:
    """步骤间 1-3 秒随机停顿(模拟人工);`sleep` 注入用于测试。"""
    low, high = range_seconds
    sleep(random.uniform(low, high))
```

- [ ] **Step 4.2: 写 screenshot / random_pause 的单元测试**

新建 `tests/test_publisher.py`:

```python
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

from wxsp.publisher import PublishResult, random_pause, screenshot


def test_screenshot_writes_to_yyyymm_subdir(tmp_path: Path) -> None:
    page = MagicMock()
    out = screenshot(
        page,
        task_id=42,
        step="upload",
        screenshots_root=tmp_path,
        now=datetime(2026, 5, 13, 10, 30),
    )
    assert out == tmp_path / "202605" / "42_upload.png"
    assert out.parent.is_dir()
    page.screenshot.assert_called_once_with(path=str(out), full_page=False)


def test_screenshot_does_not_propagate_page_errors(tmp_path: Path) -> None:
    page = MagicMock()
    page.screenshot.side_effect = RuntimeError("浏览器已关")
    # 不抛,因为截图失败不该掩盖业务异常
    out = screenshot(page, task_id=1, step="x", screenshots_root=tmp_path,
                     now=datetime(2026, 1, 1))
    assert out.name == "1_x.png"  # 路径仍返回


def test_random_pause_uses_injected_sleep_within_range() -> None:
    sleeps: list[float] = []
    random_pause((1.0, 3.0), sleep=sleeps.append)
    assert len(sleeps) == 1
    assert 1.0 <= sleeps[0] <= 3.0


def test_publish_result_defaults() -> None:
    r = PublishResult(task_id=1, ok=False, dry_run=True)
    assert r.remote_url is None
    assert r.screenshots == []
```

- [ ] **Step 4.3: 跑测试**

Run: `uv run pytest tests/test_publisher.py -v`
Expected: 4 PASS。

- [ ] **Step 4.4: Commit**

```bash
git add wxsp/publisher.py tests/test_publisher.py
git commit -m "feat(publisher): add PublishResult, screenshot(), random_pause() helpers"
```

---

## Task 5: 步骤 [3-13] —— 浏览器交互函数

**Files:**
- Modify: `wxsp/publisher.py`

每个 step 一个小函数。**全部接受 `page: Page` + 显式参数,只读 Task/Video 字段(不动 DB)。** 由 Task 7 的 `publish()` 串起来 + 包 try/except。

> ⚠️ 这些函数依赖真实 patchright Page,**不写单元测试** —— Task 9 的集成测试用真浏览器跑一遍 dry_run 全流程覆盖。本任务靠手动 import smoke + 集成测试兜底。

- [ ] **Step 5.1: 给 publisher.py 追加步骤函数**

在 `wxsp/publisher.py` 文件末尾追加(保持现有 import + helper 不变):

```python
# ============== 步骤函数(每个 1 个小函数) ==============
# 设计原则:
#   - 函数体 ≤ 20 行;复杂多选 fallback 用循环 + try/except continue
#   - 选择器全部从 wxsp.selectors 取(改版时唯一改动点)
#   - 失败抛 PublisherError 子类,让 publish() 顶层 classify + 截图

from datetime import datetime as _dt  # noqa: E402
from datetime import timedelta as _td  # noqa: E402

from wxsp import selectors as sel  # noqa: E402
from wxsp.errors import (  # noqa: E402
    CookieExpired,
    ElementNotFound,
    NetworkError,
    RiskControl,
    UploadFailed,
)


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
    while time.monotonic() < deadline:
        # 上传失败兜底
        if (page.locator(sel.UPLOAD_FAILED_INDICATOR).count()
                and page.locator(sel.UPLOAD_DELETE_TAG).count()):
            raise UploadFailed("页面提示上传失败")
        # 发表按钮 class 不含 disabled → 上传完成
        publish_button = page.get_by_role(*sel.UPLOAD_PUBLISH_BUTTON_ROLE)
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
    dialog = page.locator("div.weui-desktop-dialog").filter(has_text=sel.COVER_DIALOG_HAS_TEXT).first
    if not dialog.count():
        # 没弹窗就跳过(参考实现的容错)
        return
    dialog.wait_for(state="visible", timeout=5000)

    file_input = dialog.locator(sel.COVER_FILE_INPUT).first
    file_input.wait_for(state="attached", timeout=10_000)
    file_input.set_input_files(str(cover_path))
    page.wait_for_timeout(1000)

    crop_dialog = page.locator("div.weui-desktop-dialog").filter(
        has_text=sel.COVER_CROP_DIALOG_HAS_TEXT
    ).first
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

    参考 apply_collection:展开下拉 → 点第一个(若 topic 显式指定,后续可扩展按名匹配)。
    """
    if not topic:
        return
    label = page.get_by_text(sel.COLLECTION_LABEL_TEXT)
    options_wrap = label.locator("xpath=following-sibling::div")
    options = options_wrap.locator(".option-list-wrap > div")
    if options.count() <= 1:
        return  # 平台没合集可选
    options_wrap.click()
    # 简化:点名字匹配的;找不到点第一个(原参考行为)
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

    校验:publish_at ∈ [now+30min, now+14d];超出 raise NetworkError(其实是校验失败,
    但 validator 已挡了一道,这里只是兜底,触发 fatal 即可)。
    """
    now = _dt.now()
    if publish_at < now + _td(minutes=30) or publish_at > now + _td(days=14):
        from wxsp.errors import VideoInvalid
        raise VideoInvalid(
            f"publish_at={publish_at} 超出 [now+30min, now+14d](validator 该挡未挡)"
        )

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
```

- [ ] **Step 5.2: Smoke import**

Run: `uv run python -c "from wxsp import publisher; print([n for n in dir(publisher) if not n.startswith('_')])"`
Expected: 列表包含 `open_publish_page`, `verify_logged_in`, `upload_video`, `fill_title`, `fill_description`, `add_tags`, `set_cover`, `bind_topic`, `toggle_original`, `set_schedule`, `risk_control_probe`,无 ImportError。

- [ ] **Step 5.3: 跑全量单测**

Run: `uv run pytest -q`
Expected: 全绿(新代码暂未被覆盖,但不破坏现有)。

- [ ] **Step 5.4: Commit**

```bash
git add wxsp/publisher.py
git commit -m "feat(publisher): add step functions [3-13] (open/login/upload/content/schedule/risk)"
```

---

## Task 6: 步骤 [15-17] —— 提交 + 提取 URL

**Files:**
- Modify: `wxsp/publisher.py`

- [ ] **Step 6.1: 在 publisher.py 末尾追加**

```python
def click_publish(page: Page, *, timeout_ms: int = 10_000) -> None:
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
        # 视频号详情页 URL 形如 .../platform/post/finderNewLifeData?vid=xxx 或 ?id=xxx
        # 不严格解析,直接拿 href 末段当 video_id
        vid = href.rsplit("=", 1)[-1] if "=" in href else None
        return vid, href
    except Exception as exc:
        logger.info(f"提取 remote_url 失败(对定时发布是常态): {exc}")
        return None, None
```

- [ ] **Step 6.2: Smoke import**

Run: `uv run python -c "from wxsp.publisher import click_publish, wait_for_success_indicator, extract_remote_video_id_and_url"`
Expected: 无 ImportError。

- [ ] **Step 6.3: Commit**

```bash
git add wxsp/publisher.py
git commit -m "feat(publisher): add step functions [15-17] (submit + extract remote url)"
```

---

## Task 7: `publish()` 顶层编排 + dry_run gate

**Files:**
- Modify: `wxsp/publisher.py`
- Modify: `tests/test_publisher.py`

顶层 `publish(task_id, *, dry_run, settings)` 把所有步骤串起来,异常分类 + 截图 + DB 回写。**这是 M5 的入口**,Task 8 的 CLI 直接调它。

- [ ] **Step 7.1: 先写 publish() 的单元测试 —— 用 fake 步骤函数验证编排**

把测试 append 到 `tests/test_publisher.py`:

```python
from datetime import date as _date
from datetime import datetime as _dt
from unittest.mock import patch

import pytest
from sqlalchemy import event
from sqlmodel import Session

from wxsp.db import claim_task, get_engine, init_db
from wxsp.models import Account, Task, Video
from wxsp.publisher import AlreadyClaimed, publish


@pytest.fixture()
def db_with_pending_task(tmp_path, monkeypatch):
    db_path = tmp_path / "test.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    with Session(engine) as session:
        session.add(Account(id="a", display_name="A", user_data_dir=str(tmp_path / "p"),
                            daily_limit=20))
        session.add(Video(id="v1", file_path=str(tmp_path / "v.mp4"), title="标题" * 5,
                          ingested_at=_dt.now()))
        # 写一个 v.mp4 占位文件(stage_to_tmp 需要文件存在)
        (tmp_path / "v.mp4").write_bytes(b"fake")
        session.add(Task(video_id="v1", account_id="a", execute_date=_date.today(),
                         publish_at=_dt.now() + _td(hours=2), status="pending"))
        session.commit()
        task_id = session.exec(__import__("sqlmodel").select(Task)).first().id
    return engine, task_id, tmp_path


def test_publish_dry_run_short_circuits_before_click_publish(db_with_pending_task,
                                                              make_settings):
    """dry_run=True:跑到 risk_control_probe 后停下,截图,不点发表。"""
    engine, task_id, tmp_path = db_with_pending_task
    settings = make_settings(tmp_path, tmp_path)

    call_log = []

    def fake_step(name):
        def _impl(*args, **kwargs):
            call_log.append(name)
        return _impl

    with patch("wxsp.publisher.browser_context") as bc:
        bc.return_value.__enter__.return_value = object()  # fake page
        with patch.multiple(
            "wxsp.publisher",
            open_publish_page=fake_step("open"),
            verify_logged_in=fake_step("login"),
            upload_video=fake_step("upload"),
            fill_title=fake_step("title"),
            fill_description=fake_step("desc"),
            add_tags=fake_step("tags"),
            set_cover=fake_step("cover"),
            bind_topic=fake_step("topic"),
            toggle_original=fake_step("orig"),
            set_schedule=fake_step("sched"),
            risk_control_probe=fake_step("risk"),
            click_publish=fake_step("publish"),
            wait_for_success_indicator=fake_step("wait"),
            extract_remote_video_id_and_url=lambda page: (None, None),
            screenshot=lambda *a, **kw: tmp_path / "shot.png",
            random_pause=lambda *a, **kw: None,
        ):
            result = publish(task_id, dry_run=True, settings=settings)

    assert result.ok is True
    assert result.dry_run is True
    assert "publish" not in call_log  # 关键:没点发表
    assert "wait" not in call_log
    assert call_log[-1] == "risk"  # 最后一步是 risk_control_probe


def test_publish_already_claimed_raises_if_task_in_running(db_with_pending_task,
                                                            make_settings):
    """先调一次 claim_task 抢占 → 第二次 publish 抛 AlreadyClaimed。"""
    engine, task_id, tmp_path = db_with_pending_task
    settings = make_settings(tmp_path, tmp_path)
    with Session(engine) as session:
        assert claim_task(session, task_id) is True

    with pytest.raises(AlreadyClaimed):
        publish(task_id, dry_run=True, settings=settings)
```

> 注意:`make_settings` 是 `tests/conftest.py` 已有的 fixture(M4 抽出),不需要再定义。

- [ ] **Step 7.2: 跑测试,确认失败**

Run: `uv run pytest tests/test_publisher.py -v`
Expected: 4 个 helper 测试 PASS,2 个 publish() 测试 FAIL(publish 未实现)。

- [ ] **Step 7.3: 在 publisher.py 末尾追加 `publish()` + 必要的 import**

```python
# === publish() 顶层编排(放在文件最末尾) =====================================

from sqlmodel import Session  # noqa: E402

from wxsp.browser import browser_context  # noqa: E402
from wxsp.config import Settings  # noqa: E402
from wxsp.db import claim_task, get_engine, init_db, session_scope, transition_task  # noqa: E402
from wxsp.errors import PublisherError, classify  # noqa: E402
from wxsp.models import Account, Task, Video  # noqa: E402
from wxsp.nas import cleanup_tmp, stage_to_tmp  # noqa: E402


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
      + 截图 + status=failed。
    - 成功 → status=success + remote_url/remote_video_id(尽力而为)。
    - 不管成败,最后 cleanup_tmp。
    """
    import json as _json

    engine = get_engine()
    init_db(engine)
    screenshots_root = settings.app.logs_dir / "screenshots"
    tmp_root = settings.app.data_dir / "tmp"
    upload_timeout = settings.publisher.upload_timeout_seconds
    step_pause = settings.publisher.step_pause_seconds

    # 1. 幂等抢锁(自己 commit)
    with Session(engine) as session:
        if not claim_task(session, task_id):
            raise AlreadyClaimed(f"Task {task_id} 不在 pending 或已被占用")

    # 2. 加载 Task/Video/Account 快照
    with Session(engine) as session:
        task, video, account = _load_task_bundle(session, task_id)
        task_publish_at = task.publish_at
        video_file_path = Path(video.file_path)
        video_title = video.title
        video_description = video.description
        video_tags = _json.loads(video.tags_json or "[]")
        video_cover_path = Path(video.cover_path) if video.cover_path else None
        video_topic = video.topic
        video_original = video.original_claim
        user_data_dir = Path(account.user_data_dir)

    result = PublishResult(task_id=task_id, ok=False, dry_run=dry_run)
    last_step = "init"

    try:
        # [1] stage NAS → tmp
        last_step = "stage"
        staged = stage_to_tmp(video_file_path, task_id=task_id, tmp_root=tmp_root)
        # 封面同样 stage(失败也是 NasUnreachable)
        staged_cover = None
        if video_cover_path is not None:
            staged_cover = stage_to_tmp(video_cover_path, task_id=task_id, tmp_root=tmp_root)

        # [2] 启浏览器(headless 跟 settings)
        last_step = "browser"
        with browser_context(user_data_dir, headless=settings.publisher.headless) as page:
            last_step = "open"
            open_publish_page(page)
            random_pause(step_pause)

            last_step = "login"
            verify_logged_in(page)
            random_pause(step_pause)

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
                shot = screenshot(page, task_id=task_id, step="dryrun_gate",
                                  screenshots_root=screenshots_root)
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

        result.ok = True
        return result

    except PublisherError as exc:
        # 业务异常 + patchright Error 都走 classify
        kind = classify(exc)
        result.error_type = kind
        result.error_msg = f"step={last_step}: {exc}"
        logger.error(result.error_msg)
        return result
    except Exception as exc:  # noqa: BLE001
        kind = classify(exc)
        result.error_type = kind
        result.error_msg = f"step={last_step}: {exc}"
        logger.exception("publish 顶层未分类异常")
        return result
    finally:
        # 不管成败都清理 tmp 和回写 DB
        try:
            cleanup_tmp(task_id=task_id, tmp_root=tmp_root)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"cleanup_tmp 失败 task_id={task_id}: {exc}")
        with session_scope(engine) as session:
            new_status = "success" if result.ok and not result.dry_run else (
                "pending" if result.dry_run else "failed"
            )
            # dry_run 成功 → 回写 pending,等正式触发;失败 → failed
            transition_task(
                session,
                task_id,
                status=new_status,
                finished_at=_dt.now(),
                remote_url=result.remote_url,
                remote_video_id=result.remote_video_id,
                last_error_type=result.error_type,
                last_error_msg=result.error_msg,
                screenshots_json=_json.dumps(result.screenshots, ensure_ascii=False),
            )
```

> **dry_run 状态机注**:dry_run 成功时把 task 改回 `pending`(因为没真发,后续正式 run 还要跑)。dry_run 失败也是 `failed`(失败就是失败)。

- [ ] **Step 7.4: 跑 publisher 测试**

Run: `uv run pytest tests/test_publisher.py -v`
Expected: 全部 PASS(含两个新的 publish 测试)。

- [ ] **Step 7.5: 跑全量单测,确保不破坏**

Run: `uv run pytest -q`
Expected: 全绿。

- [ ] **Step 7.6: Commit**

```bash
git add wxsp/publisher.py tests/test_publisher.py
git commit -m "feat(publisher): wire publish() top-level with dry_run gate + DB writeback"
```

---

## Task 8: CLI `wxsp run --task-id N [--dry-run]`

**Files:**
- Modify: `wxsp/cli.py`
- Create: `tests/test_cli_run.py`

- [ ] **Step 8.1: 写失败测试**

新建 `tests/test_cli_run.py`:

```python
from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from wxsp.cli import app
from wxsp.publisher import AlreadyClaimed, PublishResult


def test_run_task_id_success_prints_ok_and_exits_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("WXSP_DB_PATH", str(tmp_path / "db.sqlite"))
    fake_result = PublishResult(task_id=7, ok=True, dry_run=False,
                                remote_url="https://x", remote_video_id="vid")
    with patch("wxsp.cli.publish", return_value=fake_result) as p, \
         patch("wxsp.cli.load_settings", return_value=MagicMock()):
        result = CliRunner().invoke(app, ["run", "--task-id", "7"])
    assert result.exit_code == 0, result.stdout
    assert "task 7" in result.stdout.lower() or "成功" in result.stdout
    p.assert_called_once()
    args, kwargs = p.call_args
    assert kwargs.get("dry_run") is False or args[1] is False or 7 in args


def test_run_task_id_dry_run_passes_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("WXSP_DB_PATH", str(tmp_path / "db.sqlite"))
    fake_result = PublishResult(task_id=7, ok=True, dry_run=True,
                                screenshots=["/x/y.png"])
    with patch("wxsp.cli.publish", return_value=fake_result) as p, \
         patch("wxsp.cli.load_settings", return_value=MagicMock()):
        result = CliRunner().invoke(app, ["run", "--task-id", "7", "--dry-run"])
    assert result.exit_code == 0, result.stdout
    _, kwargs = p.call_args
    assert kwargs["dry_run"] is True


def test_run_task_id_failed_exits_one(tmp_path, monkeypatch):
    monkeypatch.setenv("WXSP_DB_PATH", str(tmp_path / "db.sqlite"))
    fake_result = PublishResult(task_id=7, ok=False, dry_run=False,
                                error_type="cookie_expired",
                                error_msg="step=login: cookie")
    with patch("wxsp.cli.publish", return_value=fake_result), \
         patch("wxsp.cli.load_settings", return_value=MagicMock()):
        result = CliRunner().invoke(app, ["run", "--task-id", "7"])
    assert result.exit_code == 1
    assert "cookie_expired" in result.stdout


def test_run_already_claimed_exits_one(tmp_path, monkeypatch):
    monkeypatch.setenv("WXSP_DB_PATH", str(tmp_path / "db.sqlite"))
    with patch("wxsp.cli.publish", side_effect=AlreadyClaimed("已占用")), \
         patch("wxsp.cli.load_settings", return_value=MagicMock()):
        result = CliRunner().invoke(app, ["run", "--task-id", "7"])
    assert result.exit_code == 1
    assert "已占用" in result.stdout or "claimed" in result.stdout.lower()
```

- [ ] **Step 8.2: 跑测试确认失败**

Run: `uv run pytest tests/test_cli_run.py -v`
Expected: FAIL(cli 还在 `_not_implemented` 占位)。

- [ ] **Step 8.3: 改 `wxsp/cli.py::run`**

替换文件里 `@app.command("run")` 装饰的整个函数:

```python
@app.command("run")
def run(
    daemon: bool = typer.Option(False, "--daemon", help="启动 daemon(09:00 cron + FastAPI)"),
    today: bool = typer.Option(False, "--today", help="立即跑今天所有 pending 任务"),
    task_id: int | None = typer.Option(None, "--task-id", help="跑指定单条任务"),
    dry_run: bool = typer.Option(False, "--dry-run", help="发布步骤跑到点'发布'前停下"),
) -> None:
    """执行任务(M5: --task-id;M6 实现 --daemon/--today)。"""
    if task_id is None:
        _not_implemented(
            f"run --daemon={daemon} --today={today} --task-id={task_id} --dry-run={dry_run}"
        )
        return

    from wxsp.publisher import AlreadyClaimed, publish

    settings = load_settings()
    typer.echo(f"[wxsp] 跑 task {task_id}{' (dry-run)' if dry_run else ''}...")
    try:
        result = publish(task_id, dry_run=dry_run, settings=settings)
    except AlreadyClaimed as exc:
        typer.echo(f"[wxsp] ✗ {exc}")
        raise typer.Exit(code=1) from exc

    if result.ok:
        typer.echo(f"[wxsp] ✓ task {task_id} {'dry-run 完成' if dry_run else '发布成功'}")
        if result.remote_url:
            typer.echo(f"        remote_url: {result.remote_url}")
        if result.screenshots:
            typer.echo(f"        screenshots: {', '.join(result.screenshots)}")
    else:
        typer.echo(f"[wxsp] ✗ task {task_id} 失败: {result.error_type}")
        typer.echo(f"        {result.error_msg}")
        raise typer.Exit(code=1)
```

- [ ] **Step 8.4: 跑测试**

Run: `uv run pytest tests/test_cli_run.py -v`
Expected: 4 PASS。

- [ ] **Step 8.5: 跑全量**

Run: `uv run pytest -q && uv run mypy wxsp && uv run ruff check wxsp tests`
Expected: 全绿。如果 mypy/ruff 报错,修干净再 commit。

- [ ] **Step 8.6: Commit**

```bash
git add wxsp/cli.py tests/test_cli_run.py
git commit -m "feat(cli): wire `wxsp run --task-id N [--dry-run]` to publisher.publish"
```

---

## Task 9: 集成测试(@pytest.mark.integration,手动跑)

**Files:**
- Create: `tests/test_publisher_integration.py`
- Modify: `pyproject.toml`(加 marker)

集成测试不在 CI 跑,留给用户在真账号 + 真视频文件上 smoke。

- [ ] **Step 9.1: 注册 pytest marker**

打开 `pyproject.toml`,在 `[tool.pytest.ini_options]`(若没有就新建)加:

```toml
[tool.pytest.ini_options]
markers = [
  "integration: real browser + real account; run manually, skip in CI",
]
```

- [ ] **Step 9.2: 写集成测试**

新建 `tests/test_publisher_integration.py`:

```python
"""手动集成测试 —— 用真账号 + 真视频文件 + dry_run 跑一遍 M5 全流程。

跑法:
  1. config.yaml 配好 account_a 的 user_data_dir + 视频/封面搜索根
  2. NAS 上放一个测试视频(例如 m5_test.mp4)
  3. 飞书侧新建一行(账号=account_a, 视频文件=m5_test.mp4, 定时发布时间=now+2h,
     执行日期=today, 状态=待入库) → `uv run wxsp sync`
  4. 找到入库后的 task_id (`uv run wxsp accounts list` 看不到 task,需查 DB):
       sqlite3 data/db.sqlite "SELECT id, video_id, status FROM task ORDER BY id DESC LIMIT 1;"
  5. 跑:`uv run pytest tests/test_publisher_integration.py -v -m integration -s`
"""

from __future__ import annotations

import os

import pytest

from wxsp.config import load_settings
from wxsp.publisher import publish

TASK_ID_ENV = "WXSP_M5_TEST_TASK_ID"


@pytest.mark.integration
def test_dry_run_full_flow_on_real_account() -> None:
    task_id_str = os.environ.get(TASK_ID_ENV)
    if not task_id_str:
        pytest.skip(f"set ${TASK_ID_ENV}=<pending task id> to run")
    task_id = int(task_id_str)

    settings = load_settings()
    result = publish(task_id, dry_run=True, settings=settings)

    assert result.ok, f"dry_run 失败: {result.error_type} {result.error_msg}"
    assert result.dry_run is True
    assert result.screenshots, "dry_run gate 必须截图"
    # 截图文件实际存在
    from pathlib import Path
    for p in result.screenshots:
        assert Path(p).exists(), f"截图文件缺失: {p}"
```

- [ ] **Step 9.3: 确认 marker 注册生效(无警告)**

Run: `uv run pytest --collect-only tests/test_publisher_integration.py 2>&1 | head -20`
Expected: 看到测试被 collect,没有 `PytestUnknownMarkWarning`。

- [ ] **Step 9.4: 默认运行不跑集成测试**

Run: `uv run pytest -q`
Expected: 集成测试被自然跳过(因为 ENV 没设)→ skipped 1。

- [ ] **Step 9.5: Commit**

```bash
git add tests/test_publisher_integration.py pyproject.toml
git commit -m "test(publisher): add integration smoke test for full dry-run flow"
```

---

## Task 10: M5 验收 + 标记完成

**Files:**
- Modify: `docs/superpowers/specs/2026-05-12-wxsp-design.md`

- [ ] **Step 10.1: 手动跑 M5 验收 §7 的三条**

按下面逐项打钩 —— 这是 M5 是否 done 的判据,不通过不能进 M6。

| # | 验收标准 | 怎么跑 | 期望 |
|---|---|---|---|
| 1 | dry-run 跑到 publish 按钮前停下 | 按 Task 9 步骤 1-3 准备 task,然后 `WXSP_M5_TEST_TASK_ID=<id> uv run pytest -m integration -v -s` | 测试 PASS;`logs/screenshots/202605/<id>_dryrun_gate.png` 存在 |
| 2 | 真发一条成功,DB 写入 remote_url | 同上准备,直接 `uv run wxsp run --task-id <id>` | 命令 exit 0;`sqlite3 data/db.sqlite "SELECT status, remote_url FROM task WHERE id=<id>"` → `success` |
| 3 | 重复触发幂等只发一次 | 上面成功后再 `uv run wxsp run --task-id <id>` | exit 1,提示"不在 pending 或已被占用"(因为 status=success) |

> ⚠️ 验收 2 真发布会消耗一个测试号配额。**先用一个不重要的视频号 + 一段废视频 + 定时到 +30min**,跑通后回飞书把测试行的"状态"改回"待入库"或删了。

- [ ] **Step 10.2: 标记 M5 完成**

在 `docs/superpowers/specs/2026-05-12-wxsp-design.md` 的 milestone 表里,找到 M5 行,**在结尾追加** `| **M5 验收完成 (2026-05-XX)**`(参考 M4 的写法)。

- [ ] **Step 10.3: Commit**

```bash
git add docs/superpowers/specs/2026-05-12-wxsp-design.md
git commit -m "chore: mark M5 acceptance complete"
```

---

## Self-review 复盘

**Spec 覆盖检查**(对照 CLAUDE.md §5.1 的 20 步 + design doc §7 M5 三条验收):
- 步骤 [0] claim_task → Task 7 publish() 入口 ✓
- 步骤 [1] stage_to_tmp → Task 7 publish() 调 `nas.stage_to_tmp` ✓
- 步骤 [2-13] → Task 5(浏览器 + 内容 + 调度 + 风控)✓
- 步骤 [14] dry_run gate → Task 7 publish() ✓
- 步骤 [15-17] → Task 6 ✓
- 步骤 [18] close_browser → Task 7 publish() 的 `with browser_context` 出作用域时自动 ✓
- 步骤 [19] cleanup_tmp → Task 7 publish() 的 finally ✓
- 步骤 [20] commit_task_success + writeback → Task 7 publish() 的 finally(飞书回写 M7 接,M5 只写 DB)✓
- 错误分类 + 重试 → Task 1 + Task 2 ✓(注:M5 publish() 顶层只 classify 不主动 retry;`@retry_on` 装饰器留给 M6 调度层按错误类型决定重试,或在 Task 5 个别步骤显式加)
- selectors.py 集中 → Task 3 ✓
- CLI `wxsp run --task-id N [--dry-run]` → Task 8 ✓
- 三条验收 → Task 10 ✓

**红线扫描**:
- 无 "TBD" / "implement later" / "适当处理" 字样
- 每一步都给出可执行命令或完整代码
- 函数签名一致(`publish(task_id, *, dry_run, settings)` 全文统一)

**已知简化 / 留给后续**:
- `@retry_on` 装饰器实现了但 publish() 暂未在每个步骤上贴 —— 视频号步骤间错误大多是改版/风控/cookie(都不该自动重试),`nas_unreachable` 已在 Task 7 publish() 的 try/except 里被分类。M6 调度层可基于 `last_error_type` 判定是否把 task 重置为 pending 重新入队。
- `set_schedule` 不处理跨月场景以外的复杂 picker(2 个月翻页)—— validator 已挡 14d 上限。
- `extract_remote_video_id_and_url` 对定时发布常返回 (None, None) —— 这是预期行为,M9 容量监控阶段可加"已发布后异步回填"。
- 飞书回写 status/remote_url/error_message —— 属于 M7 通知 + 回写,M5 只写 DB。

---

## 执行选项

**Plan complete, saved to `docs/superpowers/plans/2026-05-13-m5-publisher.md`. Two execution options:**

**1. Subagent-Driven(推荐)** — 每个 Task 起一个独立 subagent,我在每个 Task 之间帮你 review,迭代快、上下文干净
**2. Inline Execution** — 在当前会话里直接连续跑完,checkpoint 之间停下让你 review

哪种?
