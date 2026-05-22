"""patchright context 工厂 + 视频号登录态轮询(M2)。

设计要点:
  - 每账号独立 `user_data_dir`(persistent context),cookie / localStorage 由
    persistent context 自动持久化,**不**再单独维护 cookie.json。
  - 视频号风控敏感:**永远** `headless=False`(默认值),即使是 doctor 的快速检查
    也开窗。CLAUDE.md 明确"严禁 headless 跑视频号"。
  - 选择器集中在本模块的 `LOGGED_IN_SELECTOR`;M5 publisher 的发布步骤选择器会
    集中到 `wxsp.selectors`(改版时唯一改动点)。
  - **per-account 指纹**:`browser_context(..., account_id=)` 触发 `wxsp.fingerprint`
    生成/读取该账号的设备指纹,作为 context options(UA / viewport / locale / tz)+
    init script 注入。让 4 个账号在视频号眼里像 4 台不同的电脑,绕过"同设备多账号"
    风控。**指纹生成是确定性的**(种子=account_id 的 MD5),同账号永远拿到同一套,
    避免 "同账号换设备" 反而触发踢登录。
  - `wxsp.stealth_js.INIT_SCRIPT` 是上一代静态 init script,**已被 fingerprint
    模块的 per-account init script 取代**,代码里保留供回滚。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from loguru import logger
from patchright.sync_api import Page, sync_playwright

from wxsp.config import get_user_data_dir, is_packaged

WECHAT_CHANNELS_HOME = "https://channels.weixin.qq.com"

# "已登录"标记:视频号主页/发布页登录后才出现的元素。任一可见 → 视为已登录。
# 选择器来自 social-auto-upload/uploader/tencent_uploader/main.py 的踩坑成果。
LOGGED_IN_SELECTOR = (
    'div:has-text("发表视频"), button:has-text("发表"), button:has-text("发布视频")'
)


def _chromium_root() -> Path | None:
    """打包模式返回内嵌 chromium 目录;开发模式返回 None 让 patchright 自己找。

    优先 PyInstaller 的 _MEIPASS(onedir/onefile 都有)。退化到 sys.executable 相邻目录
    兼容老 Nuitka --standalone 布局。
    """
    if not is_packaged():
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "chromium"
    exe = Path(sys.executable)
    if sys.platform == "darwin":
        return exe.parent.parent / "Resources" / "chromium"
    return exe.parent / "chromium"


def _fingerprint_storage_dir() -> Path:
    """指纹 JSON 落盘目录,跟 chrome-profiles / db.sqlite 同处 data 根。"""
    return get_user_data_dir() / "fingerprints"


@contextmanager
def browser_context(
    user_data_dir: Path,
    *,
    headless: bool = False,
    account_id: str | None = None,
) -> Iterator[Page]:
    """打开账号专属 persistent Chrome context。

    - `user_data_dir` 不存在会自动创建(适配 `wxsp accounts add` 后第一次 login)。
    - `account_id` 传入时,从 `wxsp.fingerprint` 拿/建该账号的指纹,把 UA/viewport
      /locale/timezone 应用到 context options,并注入 JS init script 覆写
      navigator / WebGL / Canvas / Audio / Client Hints。**每次开这个 profile 都
      用同一套指纹**(确定性),避免视频号把它当"同账号换设备"踢登录。
    - `account_id=None` 时退化为老行为(no_viewport=True,无指纹注入),保留给
      没拿到账号上下文的调用方做兜底。
    - persistent context 启动时会有一个默认 page(about:blank),直接复用。
    - 退出时 close context;cookie 已由 persistent context 写到 user_data_dir。
    """
    chromium_root = _chromium_root()
    if chromium_root is not None:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(chromium_root)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    launch_kwargs: dict[str, Any] = {
        "user_data_dir": str(user_data_dir),
        "headless": headless,
        "args": [
            "--disable-blink-features=AutomationControlled",
            # Chromium 内置 DNS 路径在企业网 / 某些 Windows DNS 配置下会整段失败
            # (ERR_NAME_NOT_RESOLVED 即使系统 nslookup 能解析)。把已知 DNS-related
            # feature 全关,强制走系统 thread-pool getaddrinfo;unknown feature 名
            # 会被 Chromium 忽略,所以多写几个版本名兼容不同 patchright Chromium 版。
            "--disable-features=AsyncDns,AsyncDnsResolver,DnsOverHttps,SecureDnsForFreshnessCheck",
            "--dns-prefetch-disable",
        ],
    }

    # 诊断 escape hatch:WXSP_DISABLE_FINGERPRINT 接受
    #   "1" / "true" / "all" → 全禁(等同 v0.7.2 行为)
    #   "context" / "contextopts" → 只禁 context options(UA / viewport / locale / tz / screen)
    #   "initscript" / "script" → 只禁 add_init_script 的 JS 覆写
    # 用来二分定位"指纹的哪一部分让 Chromium 内部 DNS 失败"。生产留空 = 全启用。
    _fp_disable = os.environ.get("WXSP_DISABLE_FINGERPRINT", "").strip().lower()
    disable_all = _fp_disable in ("1", "true", "yes", "all")
    disable_context = disable_all or _fp_disable in ("context", "contextopts", "options")
    disable_init = disable_all or _fp_disable in ("initscript", "script", "js")
    if account_id is not None and _fp_disable:
        logger.warning(
            f"[browser] WXSP_DISABLE_FINGERPRINT={_fp_disable!r},account={account_id} "
            f"context_disabled={disable_context} init_script_disabled={disable_init}"
        )

    fp_init_script: str | None = None
    if account_id is not None and not disable_all:
        try:
            from wxsp.fingerprint import (
                context_options as fp_context_options,
            )
            from wxsp.fingerprint import (
                get_or_create_fingerprint,
            )
            from wxsp.fingerprint import (
                init_script as fp_init_script_fn,
            )

            fp = get_or_create_fingerprint(account_id, _fingerprint_storage_dir())
            if not disable_context:
                launch_kwargs.update(fp_context_options(fp))
            else:
                launch_kwargs["no_viewport"] = True
            if not disable_init:
                fp_init_script = fp_init_script_fn(fp)
        except Exception as exc:
            # 指纹生成不该阻塞登录/发布;退化到 no_viewport 老路径,记日志告警。
            logger.warning(f"[browser] 指纹注入失败 account={account_id}: {exc};退化到无指纹模式")
            launch_kwargs["no_viewport"] = True
    else:
        launch_kwargs["no_viewport"] = True

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(**launch_kwargs)
        try:
            if fp_init_script is not None:
                context.add_init_script(fp_init_script)
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


def check_cookie(
    user_data_dir: Path,
    *,
    timeout_ms: int = 15_000,
    account_id: str | None = None,
) -> bool:
    """一站式:开浏览器 → 检查登录态 → 关浏览器。

    被 `wxsp doctor`(`refresh_cookie_status` 的 cookie_checker)和 `wxsp login`
    复用。`login` 传 `timeout_ms=300_000` 等扫码完成。

    `account_id` 传入时,本次启动会带上该账号的指纹(必须跟登录时的指纹一致,否则
    视频号会把它当 "异常设备登录" 直接踢);省略时退化到无指纹模式。
    """
    with browser_context(user_data_dir, account_id=account_id) as page:
        return wait_for_logged_in(page, timeout_ms=timeout_ms)
