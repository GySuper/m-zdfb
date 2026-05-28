# 淘宝光合平台 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 淘宝光合平台 video publishing to wxsp, with per-platform config/scheduler/feishu/notifier, and refactor publisher into PlatformPublisher protocol.

**Architecture:** Create `wxsp/platforms/` package with a `PlatformPublisher` protocol. Move existing 视频号 code into `platforms/tencent_channel.py`. Add `platforms/taobao_guanghe.py`. `publisher.py` becomes a thin router. Config/scheduler/feishu/notifier all become platform-aware.

**Tech Stack:** Same as existing — Python 3.10+, patchright, SQLModel, FastAPI, Jinja2+HTMX

---

## Phase 1: Data Model + Config Foundation

### Task 1: Add `platform` to models

**Files:**
- Modify: `wxsp/models.py`

- [ ] **Step 1: Add `platform` to Account**

```python
class Account(SQLModel, table=True):
    id: str = Field(primary_key=True)
    display_name: str
    platform: str = "tencent_channel"          # NEW: "tencent_channel" | "taobao_guanghe"
    user_data_dir: str
    daily_limit: int = 20
    is_active: bool = True
    paused_until: datetime | None = None
    cookie_status: str = "unknown"
    cookie_last_checked_at: datetime | None = None
    cookie_last_active_at: datetime | None = None
```

- [ ] **Step 2: Add `platform` to Task with index**

```python
class Task(SQLModel, table=True):
    ...
    platform: str = Field(default="tencent_channel", index=True)  # NEW
    ...
```

- [ ] **Step 3: Add `PLATFORM_TENCENT_CHANNEL` and `PLATFORM_TAOBAO_GUANGHE` constants**

At top of models.py after existing constants:

```python
PLATFORM_TENCENT_CHANNEL = "tencent_channel"
PLATFORM_TAOBAO_GUANGHE = "taobao_guanghe"
```

- [ ] **Step 4: Run tests to verify no existing tests break**

Run: `pytest tests/ -v --tb=short`

- [ ] **Step 5: Commit**

```bash
git add wxsp/models.py
git commit -m "feat(models): add platform field to Account and Task

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task 2: Refactor config.py for per-platform settings

**Files:**
- Modify: `wxsp/config.py`

- [ ] **Step 1: Add platform-level Config models**

After `WebUIConfig` class, add:

```python
class PlatformSchedulerConfig(BaseModel):
    daily_cron_hour: int = 9
    daily_cron_minute: int = 0


class PlatformPublisherConfig(BaseModel):
    headless: bool = False
    upload_timeout_seconds: int = 600
    step_pause_seconds: tuple[float, float] = (1.0, 3.0)
    max_concurrent_accounts: int = 1


class PlatformFeishuConfig(BaseModel):
    enabled: bool = True
    app_id: str
    app_secret: str
    bitable: FeishuBitableConfig
    field_map: FeishuFieldMap = Field(default_factory=FeishuFieldMap)
    sync: FeishuSyncConfig = Field(default_factory=FeishuSyncConfig)


class PlatformMonitoringConfig(BaseModel):
    cookie_warn_days: float = 1.5
    notifiers: NotifiersConfig
    notify_on: list[str] = Field(default_factory=list)


class PlatformConfig(BaseModel):
    scheduler: PlatformSchedulerConfig = Field(default_factory=PlatformSchedulerConfig)
    publisher: PlatformPublisherConfig = Field(default_factory=PlatformPublisherConfig)
    feishu: PlatformFeishuConfig | None = None
    monitoring: PlatformMonitoringConfig | None = None
```

- [ ] **Step 2: Add `platforms` to Settings, make old top-level fields optional with validators**

```python
class Settings(BaseModel):
    app: AppConfig
    paths: PathsConfig
    accounts: dict[str, AccountConfig]
    platforms: dict[str, PlatformConfig] = Field(default_factory=dict)  # NEW
    # Old flat fields — made Optional for backward compat
    scheduler: SchedulerConfig | None = None
    publisher: PublisherConfig | None = None
    feishu: FeishuConfig | None = None
    monitoring: MonitoringConfig | None = None
    webui: WebUIConfig
```

- [ ] **Step 3: Add `@model_validator` to resolve platform config from old flat format**

Add after `_check_display_name_uniqueness`:

```python
    @model_validator(mode="after")
    def _resolve_platform_configs(self) -> Settings:
        """If platforms dict is empty, derive tencent_channel platform from old flat fields."""
        if self.platforms:
            return self
        # Backward compat: old config.yaml without platforms key
        if self.feishu and self.scheduler and self.publisher and self.monitoring:
            self.platforms = {
                "tencent_channel": PlatformConfig(
                    scheduler=PlatformSchedulerConfig(
                        daily_cron_hour=self.scheduler.daily_cron_hour,
                        daily_cron_minute=self.scheduler.daily_cron_minute,
                    ),
                    publisher=PlatformPublisherConfig(
                        headless=self.publisher.headless,
                        upload_timeout_seconds=self.publisher.upload_timeout_seconds,
                        step_pause_seconds=self.publisher.step_pause_seconds,
                        max_concurrent_accounts=self.publisher.max_concurrent_accounts,
                    ),
                    feishu=self.feishu,
                    monitoring=self.monitoring,
                )
            }
        return self

    def get_platform_config(self, platform: str) -> PlatformConfig:
        """Get config for a specific platform. Falls back to tencent_channel."""
        if platform in self.platforms:
            return self.platforms[platform]
        # Fallback: use tencent_channel config for unknown platforms
        return self.platforms.get("tencent_channel", PlatformConfig())

    def get_feishu_config(self, platform: str) -> PlatformFeishuConfig | None:
        cfg = self.get_platform_config(platform)
        return cfg.feishu

    def get_monitoring_config(self, platform: str) -> PlatformMonitoringConfig | None:
        cfg = self.get_platform_config(platform)
        return cfg.monitoring

    def get_publisher_config(self, platform: str) -> PlatformPublisherConfig:
        cfg = self.get_platform_config(platform)
        return cfg.publisher

    def get_scheduler_config(self, platform: str) -> PlatformSchedulerConfig:
        cfg = self.get_platform_config(platform)
        return cfg.scheduler
```

- [ ] **Step 4: Add `platform` to AccountConfig**

```python
class AccountConfig(BaseModel):
    display_name: str
    enabled: bool = True
    daily_limit: int
    platform: str = "tencent_channel"          # NEW
    user_data_dir: Path
    video_search_root: Path
    cover_search_root: Path
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/ -v --tb=short`

- [ ] **Step 6: Commit**

```bash
git add wxsp/config.py
git commit -m "feat(config): add per-platform config with backward compat

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Phase 2: Refactor publisher into platforms/ package

### Task 3: Create `wxsp/platforms/` package with base protocol

**Files:**
- Create: `wxsp/platforms/__init__.py`
- Create: `wxsp/platforms/base.py`

- [ ] **Step 1: Create `wxsp/platforms/__init__.py`**

```python
"""Platform adapter package — each platform implements the PlatformPublisher protocol."""
```

- [ ] **Step 2: Create `wxsp/platforms/base.py`**

Move `PublishResult` and define `PlatformPublisher` protocol:

```python
"""PlatformPublisher protocol + shared types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from wxsp.config import Settings
from wxsp.models import Account


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


class PlatformPublisher(Protocol):
    """Each platform's publish + login implementation."""

    platform_key: str

    def publish_one(
        self,
        task_id: int,
        *,
        dry_run: bool = False,
        settings: Settings,
    ) -> PublishResult:
        """Execute publishing steps for one task. Only handles browser interaction.
        DB writes, notifications, and Feishu callbacks are handled by the caller (publisher.py).
        """
        ...

    def login(self, account: Account, settings: Settings) -> bool:
        """Open browser and let user log in. Returns True on success."""
        ...
```

- [ ] **Step 3: Commit**

```bash
git add wxsp/platforms/
git commit -m "feat(platforms): create platforms package with base protocol

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task 4: Move 视频号 publisher into platforms/tencent_channel.py

**Files:**
- Create: `wxsp/platforms/tencent_channel.py`
- Create: `wxsp/platforms/tencent_selectors.py`
- Modify: `wxsp/publisher.py`

- [ ] **Step 1: Move selectors**

Copy `wxsp/selectors.py` → `wxsp/platforms/tencent_selectors.py` (exact copy, no changes).

- [ ] **Step 2: Create `wxsp/platforms/tencent_channel.py`**

Copy the `publish()` function from `wxsp/publisher.py` (lines 432-630) into this file, renamed to `publish_one()`. Also copy all the step functions (upload_video, fill_title, etc.) and helpers (screenshot, random_pause, _load_task_bundle, _NOTIFY_BY_ERROR). The function signature:

```python
class TencentChannelPublisher:
    platform_key = "tencent_channel"

    def publish_one(self, task_id: int, *, dry_run: bool, settings: Settings) -> PublishResult:
        # ... existing publish() body, unchanged
        ...

    def login(self, account: Account, settings: Settings) -> bool:
        # Delegate to browser.check_cookie
        from wxsp.browser import check_cookie
        from pathlib import Path
        return check_cookie(
            Path(account.user_data_dir),
            timeout_ms=300_000,
            account_id=account.id,
        )
```

Update imports: change `from wxsp import selectors as sel` → `from wxsp.platforms import tencent_selectors as sel`.

- [ ] **Step 3: Rewrite `wxsp/publisher.py` as thin routing layer**

Keep `PublishResult` import from `platforms/base.py`. Extract the DB-write logic from the original `publish()` and make it the new router:

```python
"""Publisher router — delegates to platform-specific publisher based on task.platform."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime
from loguru import logger
from sqlmodel import Session

from wxsp.platforms.base import PublishResult
from wxsp.platforms.tencent_channel import TencentChannelPublisher
from wxsp.config import Settings
from wxsp.db import claim_task, get_engine, init_db, session_scope
from wxsp.models import Account, Task, Video
from wxsp.nas import cleanup_tmp
from wxsp.errors import classify, PublisherError


class AlreadyClaimed(Exception):
    """task 已被其它 worker 占用."""


_PUBLISHERS = {
    "tencent_channel": TencentChannelPublisher(),
}


def _get_publisher(platform: str):
    if platform not in _PUBLISHERS:
        raise ValueError(f"Unknown platform: {platform}")
    return _PUBLISHERS[platform]


def publish(task_id: int, *, dry_run: bool = False, settings: Settings) -> PublishResult:
    """Route to correct platform publisher based on task.platform."""
    engine = get_engine()
    init_db(engine)

    # Load task to determine platform
    with Session(engine) as session:
        task = session.get(Task, task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        platform = getattr(task, "platform", "tencent_channel")

    pub = _get_publisher(platform)
    return pub.publish_one(task_id, dry_run=dry_run, settings=settings)
```

- [ ] **Step 4: Verify imports and run tests**

Run: `pytest tests/ -v --tb=short -x`
Fix any import errors.

- [ ] **Step 5: Verify video号 still works end-to-end**

Run: `wxsp run --task-id <existing_task_id> --dry-run` (if a task exists) or verify no import errors on `wxsp doctor`.

- [ ] **Step 6: Commit**

```bash
git add wxsp/platforms/tencent_channel.py wxsp/platforms/tencent_selectors.py wxsp/publisher.py
git commit -m "refactor(publisher): move 视频号 publisher into platforms/tencent_channel

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task 5: Update all imports throughout codebase

**Files:**
- Modify: All files importing from `wxsp.publisher` or `wxsp.selectors`

- [ ] **Step 1: Find all files importing from `wxsp.publisher`**

Run: `grep -rn "from wxsp.publisher import\|from wxsp import publisher\|import wxsp.publisher" wxsp/ tests/`

- [ ] **Step 2: Update each import**

For `wxsp/cli.py`:
```python
# Change:
from wxsp.publisher import AlreadyClaimed, publish
# To:
from wxsp.publisher import AlreadyClaimed, publish
# (no change needed — publisher.py still exports these)
```

For `wxsp/scheduler.py`:
```python
# Change:
from wxsp.publisher import AlreadyClaimed, publish
# To:
from wxsp.publisher import AlreadyClaimed, publish
# (no change needed — publisher.py still exports these)
```

- [ ] **Step 3: Find all imports from `wxsp.selectors`**

Run: `grep -rn "from wxsp import selectors\|import wxsp.selectors" wxsp/ tests/`

These should only be in `wxsp/publisher.py` (original) and `wxsp/browser.py` (for LOGGED_IN_SELECTOR). The `browser.py` import is separate from platform selectors and should be left alone.

- [ ] **Step 4: Run full test suite**

Run: `pytest tests/ -v --tb=short`

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: update imports after publisher refactor

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Phase 3: Taobao Platform Implementation

### Task 6: Create taobao_selectors.py

**Files:**
- Create: `wxsp/platforms/taobao_selectors.py`

- [ ] **Step 1: Write selectors file**

```python
"""淘宝光合平台发布页选择器集中管理。

发布页: https://creator.guanghe.taobao.com/page/pubNew/video
注意: 表单在 huodong.taobao.com 的 iframe 内，所有选择器需先 frame_locator(iframe)。
"""

from __future__ import annotations

# ============== URL ==============
PUBLISH_PAGE_URL = "https://creator.guanghe.taobao.com/page/pubNew/video"
LOGIN_URL_FRAGMENT = "login.taobao.com"
CREATOR_HOME = "https://creator.guanghe.taobao.com"

# ============== iframe ==============
IFRAME_SELECTOR = 'iframe[title="发布器"]'

# ============== [4] 登录态判定 ==============
# 若页面 URL 包含 login.taobao.com → 未登录
# 若发布表单可见 → 已登录
LOGGED_IN_INDICATOR = 'text=发布视频'

# ============== [5] 视频上传 ==============
FILE_INPUT = 'input[type="file"]'
# 上传区域(点击触发文件选择)
UPLOAD_AREA = 'text=点击上传视频，或将视频拖放到此处'
# 上传中 / 处理中的等待: 等待封面区域不再是"等待视频上传..."
COVER_WAITING_TEXT = "等待视频上传"
COVER_READY_INDICATOR = "视频封面"  # 封面区域已生成

# ============== [7] 标题 ==============
TITLE_INPUT = 'textbox[placeholder="加个标题让内容更吸引人"]'
TITLE_MAX_LENGTH = 30

# ============== [8] 描述 ==============
DESCRIPTION_EDITOR = '[contenteditable="true"]'
# 描述区域: "展开说说，你写的文字我们都喜欢:)"
DESCRIPTION_AREA = 'text=展开说说'

# ============== [9] 话题活动 ==============
TOPIC_TRIGGER = 'text=参与话题活动'
TOPIC_CLICK_AREA = 'text=点击添加话题'
TOPIC_DIALOG_HEADING = 'heading "话题选择"'
TOPIC_SEARCH_INPUT = 'textbox[placeholder="输入关键词搜索"]'
TOPIC_SEARCH_BUTTON = 'button "搜索"'
TOPIC_CONFIRM_BUTTON = 'button "确认提交"'
TOPIC_CANCEL_BUTTON = 'button "取消"'
TOPIC_CLOSE_BUTTON = 'button "关闭"'

# ============== [10] 关联商品 ==============
PRODUCT_TRIGGER = 'text=添加商品'
PRODUCT_DIALOG_HEADING = 'heading "关联商品"'
PRODUCT_SEARCH_INPUT = 'searchbox "搜索"'
PRODUCT_SEARCH_BUTTON = 'button "搜索"'
PRODUCT_CONFIRM_BUTTON = 'button "确定"'
PRODUCT_CANCEL_BUTTON = 'button "取消"'
PRODUCT_CLOSE_BUTTON = 'button "关闭"'
PRODUCT_TAB_SHOP = 'tab "本店商品"'
PRODUCT_TAB_RECOMMEND = 'tab "本店推荐"'

# ============== [11] 定时发布 ==============
SCHEDULE_RADIO = 'text=定时发布'
SCHEDULE_DATE_INPUT = 'textbox "YYYY/MM/DD"'
SCHEDULE_TIME_INPUT = 'textbox "HH:mm"'
SCHEDULE_CALENDAR_GRID = 'grid'
SCHEDULE_CONFIRM_BUTTON = 'button "确定"'
# 启用定时发布后，提交按钮文案变为 "定时发布"
SUBMIT_BUTTON_SCHEDULED = 'button "定时发布"'
SUBMIT_BUTTON_IMMEDIATE = 'button "立即发布"'

# ============== [12] 创作者声明 ==============
DECLARATION_RADIO_MAP = {
    "内容无需标注": 'radio "内容无需标注"',
    "含AI生成内容": 'radio "含AI生成内容"',
    "含虚构演绎内容": 'radio "含虚构演绎内容"',
    "内容为转载": 'radio "内容为转载"',
    "个人观点，仅供参考": 'radio "个人观点，仅供参考"',
    "内容含营销信息": 'radio "内容含营销信息"',
}

# ============== [13] AI优化 ==============
AI_TOGGLE_SWITCH = "switch"

# ============== [14] 允许下载 ==============
DOWNLOAD_CHECKBOX = 'radio "允许下载"'

# ============== 风控文案 ==============
RISK_CONTROL_KEYWORDS = (
    "请稍后",
    "系统繁忙",
    "操作过于频繁",
    "账号异常",
    "内容不符合",
)

# ============== 成功判定 ==============
SUCCESS_INDICATORS = (
    "发布成功",
    "定时发布成功",
    "已保存",
)
```

- [ ] **Step 2: Commit**

```bash
git add wxsp/platforms/taobao_selectors.py
git commit -m "feat(taobao): add taobao guanghe selectors

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task 7: Create platforms/taobao_guanghe.py — skeleton + login

**Files:**
- Create: `wxsp/platforms/taobao_guanghe.py`

- [ ] **Step 1: Write skeleton with login method**

```python
"""淘宝光合平台发布实现 — 18 步，patchright 驱动，iframe 内操作。"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from loguru import logger
from patchright.sync_api import FrameLocator, Page
from sqlmodel import Session

from wxsp import platforms
from wxsp.browser import browser_context
from wxsp.config import Settings
from wxsp.db import get_engine, init_db
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
from wxsp.models import Account, Task, Video
from wxsp.nas import cleanup_tmp, stage_to_tmp
from wxsp.platforms import taobao_selectors as sel
from wxsp.platforms.base import PlatformPublisher, PublishResult


class TaobaoGuanghePublisher:
    platform_key = "taobao_guanghe"

    def login(self, account: Account, settings: Settings) -> bool:
        """Open browser, navigate to creator home, wait for user to log in."""
        from pathlib import Path

        user_data_dir = Path(account.user_data_dir)
        logger.info(f"[taobao] 开始登录 account={account.id}")
        try:
            with browser_context(
                user_data_dir,
                headless=False,
                account_id=account.id,
            ) as page:
                page.goto(sel.CREATOR_HOME, wait_until="domcontentloaded")
                # Wait for user to log in or for page to show logged-in state
                deadline = time.time() + 300  # 5 min timeout
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
        """Execute Taobao Guanghe publishing steps [0-18]."""
        # TODO: Implement in next task
        raise NotImplementedError("Taobao publisher not yet implemented")
```

- [ ] **Step 2: Register in platforms/__init__.py and publisher.py**

In `wxsp/platforms/__init__.py`:
```python
"""Platform adapter package."""
from wxsp.platforms.base import PlatformPublisher, PublishResult

__all__ = ["PlatformPublisher", "PublishResult"]
```

In `wxsp/publisher.py`, add:
```python
from wxsp.platforms.taobao_guanghe import TaobaoGuanghePublisher

_PUBLISHERS = {
    "tencent_channel": TencentChannelPublisher(),
    "taobao_guanghe": TaobaoGuanghePublisher(),
}
```

- [ ] **Step 3: Add new error types to errors.py**

In `wxsp/errors.py`:

```python
class ProductNotFound(PublisherError):
    """飞书填的商品ID在弹窗搜索无结果。"""


class TopicNotFound(PublisherError):
    """飞书填的话题名搜索无结果。"""


class LoginRequired(PublisherError):
    """cookie 过期，需要重新登录。"""


# Update _KIND_BY_TYPE:
_KIND_BY_TYPE: dict[type[Exception], str] = {
    ...
    ProductNotFound: "product_not_found",
    TopicNotFound: "topic_not_found",
    LoginRequired: "login_required",
}
```

- [ ] **Step 4: Verify imports**

Run: `python -c "from wxsp.platforms.taobao_guanghe import TaobaoGuanghePublisher; print('OK')"`

- [ ] **Step 5: Commit**

```bash
git add wxsp/platforms/__init__.py wxsp/platforms/taobao_guanghe.py wxsp/errors.py wxsp/publisher.py
git commit -m "feat(taobao): add taobao guanghe publisher skeleton + login

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task 8: Implement taobao publish_one() — full publishing flow

**Files:**
- Modify: `wxsp/platforms/taobao_guanghe.py`

- [ ] **Step 1: Add helper functions**

```python
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
    """Save screenshot for debugging."""
    now = now or datetime.now()
    month_dir = screenshots_root / now.strftime("%Y%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    path = month_dir / f"{task_id}_{step}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
    except Exception:
        logger.warning(f"screenshot failed step={step}")
    return path


def _iframe(page: Page) -> FrameLocator:
    """Get the publisher iframe."""
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
```

- [ ] **Step 2: Implement step functions [3-4]: open page + verify login**

```python
def _open_publish_page(page: Page) -> None:
    page.goto(sel.PUBLISH_PAGE_URL, wait_until="domcontentloaded")
    try:
        _iframe(page).locator(sel.LOGGED_IN_INDICATOR).wait_for(timeout=30_000)
    except Exception:
        raise NetworkError("发布页加载超时")


def _verify_logged_in(page: Page) -> None:
    if sel.LOGIN_URL_FRAGMENT in page.url:
        raise CookieExpired("淘宝登录态失效，需重新登录")
    # Verify publish form is visible
    try:
        _iframe(page).locator(sel.LOGGED_IN_INDICATOR).wait_for(timeout=5_000)
    except Exception:
        raise CookieExpired("发布表单不可见，登录态可能失效")
```

- [ ] **Step 3: Implement [5-6]: upload video + wait cover**

```python
def _upload_video(page: Page, file_path: Path, timeout_seconds: int = 600) -> None:
    iframe = _iframe(page)
    # Use file input
    file_input = iframe.locator(sel.FILE_INPUT)
    file_input.set_input_files(str(file_path))
    # Wait for cover to generate (no longer shows "等待视频上传")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            cover_text = iframe.locator(f'text="{sel.COVER_WAITING_TEXT}"')
            if not cover_text.count():
                logger.info("[taobao] 封面生成完成")
                return
        except Exception:
            pass
        time.sleep(3)
    raise UploadFailed("视频上传/处理超时")


def _wait_cover_generated(page: Page) -> None:
    # Already handled in _upload_video; double-check
    iframe = _iframe(page)
    try:
        iframe.locator(f'text="{sel.COVER_READY_INDICATOR}"').wait_for(timeout=10_000)
    except Exception:
        logger.warning("[taobao] 封面区域未按预期出现，继续")
```

- [ ] **Step 4: Implement [7-8]: fill title + description**

```python
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
    # Click the description area first
    desc_area = iframe.locator(sel.DESCRIPTION_AREA)
    desc_area.click()
    # Type into the contenteditable
    editor = iframe.locator(sel.DESCRIPTION_EDITOR)
    editor.fill(description[:1000])
```

- [ ] **Step 5: Implement [9]: add topic**

```python
def _add_topic(page: Page, topic_name: str | None) -> None:
    if not topic_name:
        return
    iframe = _iframe(page)
    # Click "参与话题活动" area
    iframe.locator(sel.TOPIC_CLICK_AREA).click()
    time.sleep(1)
    # Wait for dialog
    iframe.locator(sel.TOPIC_DIALOG_HEADING).wait_for(timeout=5_000)
    # Search
    iframe.locator(sel.TOPIC_SEARCH_INPUT).fill(topic_name)
    iframe.locator(sel.TOPIC_SEARCH_BUTTON).click()
    time.sleep(2)
    # Click first matching topic card
    first_card = iframe.locator(f'text="{topic_name}"').first
    try:
        first_card.wait_for(timeout=5_000)
        first_card.click()
    except Exception:
        # Close dialog and raise
        iframe.locator(sel.TOPIC_CLOSE_BUTTON).click()
        from wxsp.errors import TopicNotFound
        raise TopicNotFound(f"话题 '{topic_name}' 搜索无结果")
    # Confirm
    iframe.locator(sel.TOPIC_CONFIRM_BUTTON).click()
    time.sleep(1)
```

- [ ] **Step 6: Implement [10]: add products**

```python
def _add_products(page: Page, product_ids: str | None) -> None:
    if not product_ids:
        return
    ids = [pid.strip() for pid in product_ids.split(",") if pid.strip()]
    if not ids:
        return
    iframe = _iframe(page)
    # Click "添加商品"
    iframe.locator(sel.PRODUCT_TRIGGER).click()
    time.sleep(1)
    iframe.locator(sel.PRODUCT_DIALOG_HEADING).wait_for(timeout=5_000)
    from wxsp.errors import ProductNotFound

    for pid in ids:
        iframe.locator(sel.PRODUCT_SEARCH_INPUT).fill(pid)
        iframe.locator(sel.PRODUCT_SEARCH_BUTTON).click()
        time.sleep(2)
        # Find and check matching product
        try:
            product_checkbox = iframe.locator(f'text="{pid}"').first
            product_checkbox.wait_for(timeout=5_000)
            # Click the checkbox near the product
            checkbox = iframe.locator(f'text="{pid}"').locator("..").locator('checkbox')
            if checkbox.count():
                checkbox.check()
            else:
                raise ProductNotFound(f"商品ID '{pid}' 搜索无结果")
        except Exception as e:
            if isinstance(e, ProductNotFound):
                iframe.locator(sel.PRODUCT_CLOSE_BUTTON).click()
                raise
            raise ProductNotFound(f"商品ID '{pid}' 搜索无结果")
    # Confirm
    iframe.locator(sel.PRODUCT_CONFIRM_BUTTON).click()
    time.sleep(1)
```

- [ ] **Step 7: Implement [11]: set schedule**

```python
def _set_schedule(page: Page, publish_at: datetime) -> None:
    iframe = _iframe(page)
    # Click "定时发布" radio
    iframe.locator(sel.SCHEDULE_RADIO).click()
    time.sleep(0.5)
    # Fill date: YYYY/MM/DD
    date_str = publish_at.strftime("%Y/%m/%d")
    date_input = iframe.locator(sel.SCHEDULE_DATE_INPUT)
    date_input.click()
    date_input.fill(date_str)
    # Fill time: HH:mm
    time_str = publish_at.strftime("%H:%M")
    time_input = iframe.locator(sel.SCHEDULE_TIME_INPUT)
    time_input.click()
    time_input.fill(time_str)
    # Confirm calendar
    iframe.locator(sel.SCHEDULE_CONFIRM_BUTTON).click()
    time.sleep(0.5)
```

- [ ] **Step 8: Implement [12-14]: declaration, AI toggle, disable download**

```python
_DECLARATION_CHOICES = {
    "内容无需标注": sel.DECLARATION_RADIO_MAP["内容无需标注"],
    "含AI生成内容": sel.DECLARATION_RADIO_MAP["含AI生成内容"],
    "含虚构演绎内容": sel.DECLARATION_RADIO_MAP["含虚构演绎内容"],
    "内容为转载": sel.DECLARATION_RADIO_MAP["内容为转载"],
    "个人观点，仅供参考": sel.DECLARATION_RADIO_MAP["个人观点，仅供参考"],
    "内容含营销信息": sel.DECLARATION_RADIO_MAP["内容含营销信息"],
}


def _set_declaration(page: Page, declaration: str | None) -> None:
    """Set creator declaration radio. Default: 内容无需标注."""
    iframe = _iframe(page)
    choice = declaration or "内容无需标注"
    selector = _DECLARATION_CHOICES.get(choice)
    if selector is None:
        logger.warning(f"[taobao] 未知创作者声明 '{choice}'，使用默认")
        selector = _DECLARATION_CHOICES["内容无需标注"]
    iframe.locator(selector).click()


def _toggle_ai_optimize(page: Page, on: bool) -> None:
    """Toggle AI optimization switch."""
    if not on:
        return  # Default is off
    iframe = _iframe(page)
    switch = iframe.locator(sel.AI_TOGGLE_SWITCH)
    switch.click()


def _disable_download(page: Page) -> None:
    """Uncheck '允许下载' checkbox."""
    iframe = _iframe(page)
    checkbox = iframe.locator(sel.DOWNLOAD_CHECKBOX)
    if checkbox.is_checked():
        checkbox.click()
```

- [ ] **Step 9: Implement [15-16]: click publish + wait success**

```python
def _click_publish(page: Page) -> None:
    iframe = _iframe(page)
    # Button text depends on whether schedule is set
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
    # Timed publish might not show explicit "success" - check for redirect
    if "pubNew/video" not in page.url:
        logger.info("[taobao] 页面已跳转，视为发布成功")
        return
    raise ElementNotFound("发布成功判定超时")
```

- [ ] **Step 10: Implement publish_one() main flow**

```python
    def publish_one(
        self,
        task_id: int,
        *,
        dry_run: bool = False,
        settings: Settings,
    ) -> PublishResult:
        import json as _json

        engine = get_engine()
        init_db(engine)
        screenshots_root = settings.app.logs_dir / "screenshots"
        tmp_root = settings.app.data_dir / "tmp"
        pub_cfg = settings.get_publisher_config("taobao_guanghe")
        upload_timeout = pub_cfg.upload_timeout_seconds
        step_pause = pub_cfg.step_pause_seconds

        # Load task bundle
        with Session(engine) as session:
            task, video, account = _load_task_bundle(session, task_id)
            video_file_path = Path(video.file_path)
            video_title = video.title
            video_description = video.description
            video_topic = video.topic
            video_product_ids = _json.loads(video.tags_json or "[]")  # reuse tags_json for product_ids
            task_publish_at = task.publish_at
            user_data_dir = Path(account.user_data_dir)
            account_id = account.id

        result = PublishResult(task_id=task_id, ok=False, dry_run=dry_run)
        last_step = "init"

        try:
            # [1] stage NAS → tmp
            last_step = "stage"
            staged = stage_to_tmp(video_file_path, task_id=task_id, tmp_root=tmp_root)

            # [2] launch browser
            last_step = "browser"
            with browser_context(user_data_dir, headless=pub_cfg.headless, account_id=account_id) as page:
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

                    # [10] add products (from tags_json, comma-separated IDs)
                    last_step = "products"
                    _add_products(page, product_ids=",".join(product_ids) if isinstance(product_ids, list) else None)
                    _random_pause(step_pause)

                    # [11] set schedule
                    last_step = "schedule"
                    _set_schedule(page, publish_at=task_publish_at)
                    _random_pause(step_pause)

                    # [12] set declaration
                    last_step = "declaration"
                    _set_declaration(page, declaration=None)  # TODO: read from video fields
                    _random_pause(step_pause)

                    # [13] toggle AI optimize
                    last_step = "ai"
                    _toggle_ai_optimize(page, on=False)  # TODO: read from video fields
                    _random_pause(step_pause)

                    # [14] disable download
                    last_step = "download"
                    _disable_download(page)
                    _random_pause(step_pause)

                    # ★ DRY_RUN GATE
                    if dry_run:
                        last_step = "dryrun_gate"
                        shot = _screenshot(page, task_id=task_id, step="dryrun_gate", screenshots_root=screenshots_root)
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
                        shot = _screenshot(page, task_id=task_id, step=f"err_{last_step}", screenshots_root=screenshots_root)
                        result.screenshots.append(str(shot))
                    except Exception as ss_exc:
                        logger.warning(f"screenshot failed: {ss_exc}")
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
```

- [ ] **Step 11: Note — declaration and ai_optimize need new Video fields**

The `_set_declaration` and `_toggle_ai_optimize` currently use hardcoded defaults. The design calls for these to come from Feishu. We'll add `declaration` and `ai_optimize` fields to the Video model in the validator task (Task 11).

- [ ] **Step 12: Commit**

```bash
git add wxsp/platforms/taobao_guanghe.py
git commit -m "feat(taobao): implement full 18-step taobao guanghe publisher

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Phase 4: Feishu + Validator for Taobao

### Task 9: Add taobao Video fields (declaration, ai_optimize, product_ids)

**Files:**
- Modify: `wxsp/models.py`

- [ ] **Step 1: Add fields to Video model**

```python
class Video(SQLModel, table=True):
    ...
    # NEW: taobao-specific fields
    declaration: str | None = None       # 创作者声明
    ai_optimize: bool = False             # AI优化开关
    product_ids_json: str = "[]"          # 商品ID列表(JSON array)
```

- [ ] **Step 2: Commit**

```bash
git add wxsp/models.py
git commit -m "feat(models): add taobao-specific fields to Video

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task 10: Update validator.py for taobao validation

**Files:**
- Modify: `wxsp/validator.py`

- [ ] **Step 1: Add taobao-specific fields to ValidationResult**

```python
@dataclass(frozen=True)
class ValidationResult:
    ...
    # NEW: taobao-specific
    declaration: str | None = None
    ai_optimize: bool = False
    product_ids: list[str] = field(default_factory=list)
```

- [ ] **Step 2: Add taobao validation logic in validate()**

In the `validate()` function, after existing checks, add conditional taobao validation:

```python
    # Taobao-specific fields (only validated for taobao platform)
    platform = account_id_to_platform.get(result.account_id, "tencent_channel") if result.account_id else "tencent_channel"
    if platform == "taobao_guanghe":
        # declaration: required for taobao
        declaration = row.get("declaration", "").strip()
        valid_declarations = {
            "内容无需标注", "含AI生成内容", "含虚构演绎内容",
            "内容为转载", "个人观点，仅供参考", "内容含营销信息",
        }
        if not declaration:
            # missing → incomplete
            result = ValidationResult(ok=False, incomplete=True, errors=[...])
            return result
        if declaration not in valid_declarations:
            errors.append(FieldError("创作者声明", f"'{declaration}' 不在有效选项中"))
        else:
            result = replace(result, declaration=declaration)

        # ai_optimize: checkbox
        ai_str = row.get("ai_optimize", "")
        ai_optimize = ai_str.strip().lower() in ("true", "是", "yes", "1", "✓")
        result = replace(result, ai_optimize=ai_optimize)

        # product_ids: optional, comma-separated
        product_ids_str = row.get("product_ids", "").strip()
        if product_ids_str:
            product_ids = [p.strip() for p in product_ids_str.split(",") if p.strip()]
            result = replace(result, product_ids=product_ids)
```

Note: we need `account_id_to_platform` mapping. This comes from account config in settings. The validate function already receives active accounts context.

- [ ] **Step 3: Commit**

```bash
git add wxsp/validator.py
git commit -m "feat(validator): add taobao-specific field validation

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task 11: Update feishu.py for multi-table support

**Files:**
- Modify: `wxsp/feishu.py`
- Modify: `wxsp/sync.py`

- [ ] **Step 1: Update FeishuFieldMap to support taobao fields**

```python
class FeishuFieldMap(BaseModel):
    # shared
    video_file: str = "视频文件"
    title: str = "标题"
    description: str = "描述"
    account: str = "账号"
    execute_date: str = "执行日期"
    publish_at: str = "定时发布时间"
    status: str = "状态"
    remote_url: str = "已发布链接"
    error_message: str = "错误信息"
    # tencent_channel specific
    tags: str = "标签"
    cover: str = "封面文件"
    topic: str = "合集"
    original_claim: str = "原创"
    # taobao_guanghe specific
    declaration: str = "创作者声明"
    ai_optimize: str = "AI优化"
    product_ids: str = "商品ID"
```

- [ ] **Step 2: Update sync_now() to accept platform parameter**

```python
def sync_now(settings: Settings, *, dry_run: bool = False, platform: str = "tencent_channel") -> SyncResult:
    result = SyncResult()
    feishu_cfg = settings.get_feishu_config(platform)
    if feishu_cfg is None or not feishu_cfg.enabled:
        return result

    client = make_client(feishu_cfg.app_id, feishu_cfg.app_secret)
    rows = fetch_pending_rows(
        client,
        app_token=feishu_cfg.bitable.app_token,
        table_id=feishu_cfg.bitable.table_id,
        status_field=feishu_cfg.field_map.status,
    )
    # ... rest uses feishu_cfg instead of settings.feishu
```

- [ ] **Step 3: Update all references from settings.feishu to settings.get_feishu_config(platform)**

In sync.py, validator.py, scheduler.py, and API routes.

- [ ] **Step 4: Commit**

```bash
git add wxsp/feishu.py wxsp/sync.py
git commit -m "feat(feishu): support per-platform feishu table config

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Phase 5: Scheduler + Notify Updates

### Task 12: Update scheduler.py for per-platform cron

**Files:**
- Modify: `wxsp/scheduler.py`

- [ ] **Step 1: Update start_daemon() to register per-platform cron jobs**

```python
def start_daemon(settings: Settings) -> None:
    scheduler = BlockingScheduler(timezone=settings.app.timezone)
    _register_lifecycle(scheduler, settings)

    for platform_key, platform_cfg in settings.platforms.items():
        sched_cfg = settings.get_scheduler_config(platform_key)
        cron_kwargs = {
            "hour": sched_cfg.daily_cron_hour,
            "minute": sched_cfg.daily_cron_minute,
        }
        scheduler.add_job(
            lambda p=platform_key: _daily_cron(p, settings),
            CronTrigger(**cron_kwargs),
            id=f"daily_{platform_key}",
            name=f"[{platform_key}] daily sync + run",
        )

    scheduler.start()
```

- [ ] **Step 2: Update run_today_pending() to accept platform**

```python
def run_today_pending(settings: Settings, platform: str | None = None) -> RunSummary:
    platforms_to_run = [platform] if platform else list(settings.platforms.keys())
    ...
    for p in platforms_to_run:
        _run_platform_today(p, settings, summary)
    ...
```

- [ ] **Step 3: Scope halt to platform**

Update `_ACCOUNT_HALT_ERRORS` logic: halt only affects tasks on the same platform.

- [ ] **Step 4: Commit**

```bash
git add wxsp/scheduler.py
git commit -m "feat(scheduler): per-platform cron jobs and halt scoping

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task 13: Update notify.py for per-platform notifier + platform tag

**Files:**
- Modify: `wxsp/notify.py`

- [ ] **Step 1: Make _PLATFORM_TAG dynamic**

Remove the hardcoded `_PLATFORM_TAG = "视频号"`. Update `_format_markdown()` to accept platform:

```python
def _platform_tag(platform: str | None) -> str:
    return {"tencent_channel": "视频号", "taobao_guanghe": "淘宝光合"}.get(platform or "", "视频号")


def _format_markdown(event: NotifyEvent, platform: str | None = None) -> str:
    tag_name = _platform_tag(platform)
    ...
    lines = [f"## [{tag_name}] {tag} {event.title}", "", event.content]
    ...
```

- [ ] **Step 2: Update build_notifiers_from_settings() for platform**

```python
def build_notifiers_from_settings(settings: Settings, platform: str = "tencent_channel") -> list[Notifier]:
    monitoring_cfg = settings.get_monitoring_config(platform)
    if monitoring_cfg is None:
        return []
    notifiers: list[Notifier] = []
    if monitoring_cfg.notifiers.wecom.enabled:
        notifiers.append(WecomNotifier(webhook=monitoring_cfg.notifiers.wecom.webhook))
    return notifiers
```

- [ ] **Step 3: Update notify() to check platform's notify_on**

```python
def notify(
    event: NotifyEvent,
    *,
    session: Session,
    settings: Settings,
    platform: str = "tencent_channel",
    notifiers: list[Notifier] | None = None,
) -> None:
    monitoring_cfg = settings.get_monitoring_config(platform)
    if monitoring_cfg is None:
        return
    ...
    if event.type not in monitoring_cfg.notify_on:
        return
    if notifiers is None:
        notifiers = build_notifiers_from_settings(settings, platform=platform)
    ...
```

- [ ] **Step 4: Update all callers to pass platform**

In publisher.py, scheduler.py, doctor.py, cli.py — add `platform=` to every `notify()` call.

- [ ] **Step 5: Commit**

```bash
git add wxsp/notify.py wxsp/publisher.py wxsp/scheduler.py wxsp/doctor.py wxsp/cli.py
git commit -m "feat(notify): per-platform notifier config and platform tag

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Phase 6: CLI Updates

### Task 14: Update CLI for multi-platform support

**Files:**
- Modify: `wxsp/cli.py`

- [ ] **Step 1: Update `login` command to handle taobao**

```python
@app.command("login")
def login(account_id: str = typer.Argument(..., help="账号 ID")) -> None:
    with _open_session() as session:
        account = session.get(Account, account_id)
        if account is None:
            typer.echo(f"[wxsp] 账号 {account_id!r} 不存在。")
            raise typer.Exit(code=1)
        platform = getattr(account, "platform", "tencent_channel")

    if platform == "taobao_guanghe":
        from wxsp.platforms.taobao_guanghe import TaobaoGuanghePublisher
        pub = TaobaoGuanghePublisher()
        ok = pub.login(account, load_settings())
    else:
        # existing tencent_channel login flow
        user_data_dir = Path(account.user_data_dir)
        typer.echo(f"[wxsp] 打开浏览器,请在弹出窗口中扫码登录 {account_id}...")
        ok = check_cookie(user_data_dir, timeout_ms=300_000, account_id=account_id)

    # ... rest of login flow (record_cookie_check, etc.)
```

- [ ] **Step 2: Update `sync` command with --platform flag**

```python
@app.command("sync")
def sync(
    dry_run: bool = typer.Option(False, "--dry-run"),
    platform: str = typer.Option("tencent_channel", "--platform", help="平台: tencent_channel | taobao_guanghe"),
) -> None:
    settings = load_settings()
    feishu_cfg = settings.get_feishu_config(platform)
    if feishu_cfg is None or not feishu_cfg.enabled:
        typer.echo(f"[wxsp] 平台 {platform} 飞书未启用。")
        return
    ...
```

- [ ] **Step 3: Update `run` command with --platform flag**

```python
@app.command("run")
def run(
    ...
    platform: str = typer.Option(None, "--platform", help="平台: tencent_channel | taobao_guanghe"),
) -> None:
    ...
    if today:
        summary = run_today_pending(settings, platform=platform)
        ...
```

- [ ] **Step 4: Update `accounts add` to accept --platform**

```python
@accounts_app.command("add")
def accounts_add(
    ...
    platform: str = typer.Option("tencent_channel", "--platform", help="平台"),
) -> None:
    with _open_session() as session:
        session.add(Account(..., platform=platform))
```

- [ ] **Step 5: Commit**

```bash
git add wxsp/cli.py
git commit -m "feat(cli): add --platform flag to login/sync/run/accounts

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Phase 7: Web UI

### Task 15: Update Web UI for multi-platform

**Files:**
- Modify: `wxsp/api/routes_accounts.py`
- Modify: `wxsp/api/routes_tasks.py`
- Modify: `wxsp/api/routes_dashboard.py`
- Modify: `wxsp/api/routes_config.py`
- Modify: `wxsp/templates/*.html`

- [ ] **Step 1: Add platform filter to accounts page**
- [ ] **Step 2: Add platform filter to tasks page**
- [ ] **Step 3: Update dashboard to show per-platform stats**
- [ ] **Step 4: Update config page with platform tabs**
- [ ] **Step 5: Commit**

```bash
git add wxsp/api/ wxsp/templates/
git commit -m "feat(webui): add platform filter to all pages

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Phase 8: Integration & Testing

### Task 16: Full integration test — taobao dry-run

**Files:**
- Create: `tests/test_taobao_guanghe.py` (if test infrastructure exists)

- [ ] **Step 1: Write integration test skeleton**
- [ ] **Step 2: Run full test suite**

Run: `pytest tests/ -v --tb=short`

- [ ] **Step 3: Fix any issues and commit**

```bash
git add tests/
git commit -m "test(taobao): add integration test skeleton

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Task 17: Update config.example.yaml

**Files:**
- Modify: `config.example.yaml`

- [ ] **Step 1: Add platforms section to example config**
- [ ] **Step 2: Commit**

```bash
git add config.example.yaml
git commit -m "docs(config): add taobao platform example to config template

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```
