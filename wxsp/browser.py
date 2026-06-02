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

import json as _json
import os
import sys
import time as _time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from loguru import logger
from patchright.sync_api import Page, sync_playwright

from wxsp.config import get_user_data_dir, is_packaged

# 平台登录态检测配置:"url" 模式检查 URL 是否含 fragment,"selector" 模式轮询选择器
_PLATFORM_LOGIN_META: dict[str, dict[str, Any]] = {
    "tencent_channel": {
        "home_url": "https://channels.weixin.qq.com",
        "mode": "selector",
        "selector": 'div:has-text("发表视频"), button:has-text("发表"), button:has-text("发布视频")',
    },
    "taobao_guanghe": {
        "home_url": "https://creator.guanghe.taobao.com",
        "mode": "url",
        "login_fragment": "login.taobao.com",
    },
}

WECHAT_CHANNELS_HOME = "https://channels.weixin.qq.com"


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
    platform: str = "tencent_channel",
) -> Iterator[Page]:
    """打开账号专属 persistent Chrome context。

    - `user_data_dir` 不存在会自动创建。
    - 视频号:注入 per-account 指纹(UA/viewport/WebGL/Canvas 等)。
    - 淘宝等平台:跳过指纹,额外用 cookies.json 做显式持久化(patchright 的
      persistent context 对 session cookie 落盘不可靠)。
    """
    chromium_root = _chromium_root()
    if chromium_root is not None:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(chromium_root)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    # 显式 cookie 持久化:patchright 的 persistent context 在关闭时不一定能
    # 把 session cookie 写入 SQLite,导致淘宝登录后重开丢 cookie。
    # 这里额外保存/恢复到同目录的 cookies.json(platform 甄别,视频号走 fingerprint 不需要)。
    _use_explicit_cookies = platform != "tencent_channel"
    _cookie_file = user_data_dir / "cookies.json"

    if platform == "tencent_channel":
        _chrome_args = [
            "--disable-blink-features=AutomationControlled",
            "--window-position=0,0",
        ]
    else:
        _chrome_args = ["--window-position=0,0"]
    # DNS stability flags(企业网 / 某些 Windows DNS 配置下 Chromium 内置 DNS 会整段
    # 失败,ERR_NAME_NOT_RESOLVED 即使系统 nslookup 能解析)。所有平台统一加。
    _chrome_args.extend(
        [
            "--disable-features=AsyncDns,AsyncDnsResolver,DnsOverHttps,SecureDnsForFreshnessCheck",
            "--dns-prefetch-disable",
        ]
    )

    launch_kwargs: dict[str, Any] = {
        "user_data_dir": str(user_data_dir),
        "headless": headless,
        "args": _chrome_args,
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
    # 指纹只对视频号有效;淘宝等平台跳过(风控机制不同,不需要设备指纹模拟)
    if account_id is not None and not disable_all and platform == "tencent_channel":
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
            # init_script 先生成,成功后再应用 context options,避免 HTTP 层指纹
            # 已注入但 JS 层失败的不一致状态(比没指纹更危险)
            if not disable_init:
                fp_init_script = fp_init_script_fn(fp)
            if not disable_context:
                launch_kwargs.update(fp_context_options(fp))
            else:
                launch_kwargs["viewport"] = {"width": 1280, "height": 720}
        except Exception as exc:
            logger.warning(f"[browser] 指纹注入失败 account={account_id}: {exc};退化到无指纹模式")
            # CLAUDE.md:指纹生成失败 → 软回退到无指纹模式(no_viewport=True),用真实
            # 窗口尺寸,少一个 viewport 仿真指纹信号。先清掉可能半写入的 viewport 避免冲突。
            launch_kwargs.pop("viewport", None)
            launch_kwargs["no_viewport"] = True
    else:
        launch_kwargs["viewport"] = {"width": 1280, "height": 720}

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(**launch_kwargs)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            # 显式恢复 cookie(patchright persistent context 不可靠)
            if _use_explicit_cookies and _cookie_file.exists():
                try:
                    with open(_cookie_file) as f:
                        saved = _json.load(f)
                    context.add_cookies(saved)
                    logger.info(
                        f"[browser] {platform} 恢复 {len(saved)} 个 cookie" f" from {_cookie_file}"
                    )
                except Exception as exc:
                    logger.warning(f"[browser] 恢复 cookie 失败: {exc}")
            # 诊断:记录启动时已加载的 cookie 数量
            try:
                loaded = context.cookies()
                logger.info(
                    f"[browser] {platform} context 启动,"
                    f" user_data_dir={user_data_dir}, cookies_loaded={len(loaded)}"
                )
            except Exception:
                pass
            if fp_init_script is not None:
                # patchright 1.59.1 + 当前 Chromium 下,context.add_init_script /
                # page.add_init_script 一旦调用,后续 navigate 全部 ERR_CONNECTION_CLOSED
                # (本地复现:连 about:blank → baidu/google/example.com 全炸)。
                # 改用 framenavigated 事件,navigate 完成后立即对 frame evaluate
                # 同一段覆写脚本 —— 错过第一拍 server-side first GET 的 fingerprint
                # check(视频号实际是 navigate 后 JS 上报指纹,所以仍然有效)。
                def _inject_on_nav(frame: Any) -> None:
                    try:
                        frame.evaluate(fp_init_script)
                    except Exception:
                        # about:blank / 已 detach 的 frame / cross-origin iframe 都
                        # 可能 evaluate 失败,无所谓 —— 我们只关心主站和 wujie 子 frame。
                        pass

                page.on("framenavigated", _inject_on_nav)
            yield page
        finally:
            # 显式保存 cookie(淘宝等非视频号平台)
            if _use_explicit_cookies:
                try:
                    all_cookies = context.cookies()
                    with open(_cookie_file, "w") as f:
                        _json.dump(all_cookies, f, ensure_ascii=False)
                    logger.info(
                        f"[browser] {platform} 保存 {len(all_cookies)} 个 cookie"
                        f" to {_cookie_file}"
                    )
                except Exception as exc:
                    logger.warning(f"[browser] 保存 cookie 失败: {exc}")
            # 导到 about:blank 确保当前页面的 cookie 都写入,再关 context
            try:
                page.goto("about:blank")
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass  # 浏览器可能已经崩溃,close 失败不影响主流程


def wait_for_logged_in(page: Page, *, timeout_ms: int, platform: str = "tencent_channel") -> bool:
    """导航到平台首页,等待登录态确认。

    两种检测模式:
    - "selector":轮询选择器直到可见(视频号:发表视频按钮)
    - "url":检查 URL 是否不再含 login fragment(淘宝:login.taobao.com)

    返回 True = 已登录;False = 超时。
    """
    meta = _PLATFORM_LOGIN_META.get(platform, _PLATFORM_LOGIN_META["tencent_channel"])
    page.goto(meta["home_url"], wait_until="domcontentloaded")
    if meta["mode"] == "url":
        login_fragment = meta["login_fragment"]
        deadline = _time.monotonic() + timeout_ms / 1000.0
        while _time.monotonic() < deadline:
            if login_fragment not in page.url:
                # 登录成功:等足够时间让 cookie 全部落盘
                page.wait_for_timeout(5000)
                # 强制触发 cookie flush(写磁盘)
                try:
                    cookies = page.context.cookies()
                    logger.info(
                        f"[browser] {platform} 登录成功," f" cookies={len(cookies)} url={page.url}"
                    )
                except Exception:
                    pass
                # 再等 2 秒确保 SQLite WAL 同步到 disk
                page.wait_for_timeout(2000)
                return True
            page.wait_for_timeout(2000)
        return False
    try:
        page.wait_for_selector(meta["selector"], timeout=timeout_ms, state="visible")
        return True
    except Exception:
        return False


def check_cookie(
    user_data_dir: Path,
    *,
    timeout_ms: int = 15_000,
    account_id: str | None = None,
    platform: str = "tencent_channel",
) -> bool:
    """一站式:开浏览器 → 检查登录态 → 关浏览器。

    被 `wxsp doctor`(`refresh_cookie_status` 的 cookie_checker)和 `wxsp login`
    复用。`login` 传 `timeout_ms=300_000` 等扫码完成。

    `account_id` 传入时,本次启动会带上该账号的指纹(必须跟登录时的指纹一致,否则
    平台会把它当 "异常设备登录" 直接踢);省略时退化到无指纹模式。
    """
    with browser_context(user_data_dir, account_id=account_id, platform=platform) as page:
        return wait_for_logged_in(page, timeout_ms=timeout_ms, platform=platform)
