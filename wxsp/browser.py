"""patchright context 工厂 + 视频号登录态轮询(M2)。

设计要点:
  - 每账号独立 `user_data_dir`(persistent context),cookie / localStorage 由
    persistent context 自动持久化,**不**再单独维护 cookie.json。
  - 视频号风控敏感:**永远** `headless=False`(默认值),即使是 doctor 的快速检查
    也开窗。CLAUDE.md 明确"严禁 headless 跑视频号"。
  - 选择器集中在本模块的 `LOGGED_IN_SELECTOR`;M5 publisher 的发布步骤选择器会
    集中到 `wxsp.selectors`(改版时唯一改动点)。
  - **不默认注入 `wxsp.stealth_js.INIT_SCRIPT`**。视频号 2026 反爬升级后,init script
    里 `navigator.webdriver=undefined`(正常浏览器是 `false`)等"过度修改"特征反而
    会触发服务端在 TLS 层断连(`ERR_CONNECTION_CLOSED`)。patchright 自身的 CDP
    指纹修补已足够;`stealth_js.py` 的常量保留备用,M5 publisher 若有需要可显式
    `context.add_init_script(INIT_SCRIPT)` 注入。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from patchright.sync_api import Page, sync_playwright

from wxsp.config import is_packaged

WECHAT_CHANNELS_HOME = "https://channels.weixin.qq.com"

# "已登录"标记:视频号主页/发布页登录后才出现的元素。任一可见 → 视为已登录。
# 选择器来自 social-auto-upload/uploader/tencent_uploader/main.py 的踩坑成果。
LOGGED_IN_SELECTOR = (
    'div:has-text("发表视频"), button:has-text("发表"), button:has-text("发布视频")'
)


def _chromium_root() -> Path | None:
    """打包模式返回内嵌 chromium 目录;开发模式返回 None 让 patchright 自己找。"""
    if not is_packaged():
        return None
    exe = Path(sys.executable)
    if sys.platform == "darwin":
        # /Applications/wxsp.app/Contents/MacOS/wxsp → ../Resources/chromium
        return exe.parent.parent / "Resources" / "chromium"
    return exe.parent / "chromium"


@contextmanager
def browser_context(
    user_data_dir: Path,
    *,
    headless: bool = False,
) -> Iterator[Page]:
    """打开账号专属 persistent Chrome context。

    - `user_data_dir` 不存在会自动创建(适配 `wxsp accounts add` 后第一次 login)。
    - persistent context 启动时会有一个默认 page(about:blank),直接复用。
    - 退出时 close context;cookie 已由 persistent context 写到 user_data_dir。
    - 反检测靠 patchright 自身的 CDP 指纹修补 + `--disable-blink-features=
      AutomationControlled`,**不再注入** `stealth_js.INIT_SCRIPT`(见模块 docstring)。
    """
    chromium_root = _chromium_root()
    if chromium_root is not None:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(chromium_root)
    user_data_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=headless,
            no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            yield page
        finally:
            context.close()


def wait_for_logged_in(page: Page, *, timeout_ms: int) -> bool:
    """导航到视频号主页,轮询 `LOGGED_IN_SELECTOR` 直到可见或超时。

    - `timeout_ms` ≥ 视频号页面 DOM ready 时间(经验值:5-15s)+ 扫码等待时间。
    - login 场景:`timeout_ms=300_000`(5 分钟,够扫码)。
    - doctor 场景:`timeout_ms=15_000`(已登录的话主页几秒就出按钮)。

    返回 True = 找到登录标记;False = 超时(扫码未完成 / cookie 失效 / 网络问题)。
    """
    page.goto(WECHAT_CHANNELS_HOME, wait_until="domcontentloaded")
    try:
        page.wait_for_selector(LOGGED_IN_SELECTOR, timeout=timeout_ms, state="visible")
        return True
    except Exception:
        # patchright 超时抛 TimeoutError,网络/导航错误抛其它子类。任何异常都视为
        # "没找到登录标记" —— 真正可补救的错误(网络等)在 M5 publisher 层细分。
        return False


def check_cookie(user_data_dir: Path, *, timeout_ms: int = 15_000) -> bool:
    """一站式:开浏览器 → 检查登录态 → 关浏览器。

    被 `wxsp doctor`(`refresh_cookie_status` 的 cookie_checker)和 `wxsp login`
    复用。`login` 传 `timeout_ms=300_000` 等扫码完成。
    """
    with browser_context(user_data_dir) as page:
        return wait_for_logged_in(page, timeout_ms=timeout_ms)
