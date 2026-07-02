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
import signal
import subprocess
import sys
import time as _time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from loguru import logger
from patchright.sync_api import Page, sync_playwright

from wxsp.config import get_user_data_dir, is_packaged
from wxsp.platform_meta import get_meta

WECHAT_CHANNELS_HOME = "https://channels.weixin.qq.com"


def login_meta_for(platform: str) -> dict[str, Any]:
    """平台登录态检测配置,取自 wxsp.platform_meta(单一信息源)。

    "url" 模式检查 URL 是否含 login_fragment,"selector" 模式轮询 selector。
    """
    return get_meta(platform).login_meta


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


def _system_chrome_path() -> Path:
    """按平台查找系统真实安装的 Google Chrome 可执行文件路径(CDP 模式用)。

    找不到抛 FileNotFoundError(带引导文案)。开发/打包模式逻辑一致 —— 系统 Chrome
    永远装在固定系统路径,与 wxsp 是否打包无关。
    """
    if sys.platform == "darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    elif sys.platform == "win32":
        candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
    else:  # Linux
        candidates = [Path("/usr/bin/google-chrome"), Path("/usr/bin/chromium-browser")]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(
        "未找到系统 Google Chrome(CDP 模式需要真实 Chrome,不能用 patchright 自带的 chromium)。"
        f"已尝试: {[str(c) for c in candidates]}。请从 https://www.google.com/chrome/ 安装 Google Chrome。"
    )


def _launch_real_chrome(
    user_data_dir: Path, *, headless: bool = False
) -> tuple[subprocess.Popen[str], int]:
    """用 subprocess 启动系统真实 Chrome(带调试端口 + 绑定 profile),返回 (proc, port)。

    --remote-debugging-port=0 让 Chrome 自选端口,从 stderr 的 "DevTools listening on" 解析。
    调用方负责在 finally 里 proc.terminate()(CDP 的 context.close() 只断连接不杀进程)。
    """
    chrome = _system_chrome_path()
    argv = [
        str(chrome),
        f"--user-data-dir={user_data_dir.resolve()}",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-position=0,0",
        "--disable-features=AsyncDns,AsyncDnsResolver,DnsOverHttps,SecureDnsForFreshnessCheck",
        "--dns-prefetch-disable",
    ]
    if headless:
        argv.append("--headless=new")
    # stderr=PIPE 读 DevTools 端口;stdout 不关心。text 模式方便解析。
    proc = subprocess.Popen(argv, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    # 轮询 stderr 直到出现 "DevTools listening on ws://127.0.0.1:<port>"(Chrome 自选端口)
    deadline = _time.monotonic() + 15
    port: int | None = None
    while _time.monotonic() < deadline and proc.poll() is None:
        line = proc.stderr.readline() if proc.stderr else ""
        if "DevTools listening on" in line:
            # 形如 ...ws://127.0.0.1:9222/devtools/browser/...
            import re

            m = re.search(r"127\.0\.0\.1:(\d+)", line)
            if m:
                port = int(m.group(1))
                break
    if port is None:
        proc.terminate()
        raise RuntimeError("Chrome DevTools 端口就绪超时(15s 内未监听)")
    return proc, port


def _fingerprint_storage_dir() -> Path:
    """指纹 JSON 落盘目录,跟 chrome-profiles / db.sqlite 同处 data 根。"""
    return get_user_data_dir() / "fingerprints"


def _process_cmdline_contains(pid: int, needle: str) -> bool:
    """进程 pid 的命令行是否含 needle(best-effort)。dead pid / 查不到 → False。"""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
        else:
            out = subprocess.run(
                ["ps", "-ww", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
        return needle in out
    except Exception:
        return False


def _reap_windows_profile_holders(user_data_dir: Path) -> None:
    """Windows 分支:杀掉命令行含本 profile 的残留 chrome,再删单例锁文件。

    Windows 的单例是命名互斥量,没有 posix 那种可 readlink 出 pid 的 SingletonLock
    symlink,只能反查:枚举 chrome.exe 进程命令行,含本 profile 的 --user-data-dir 即
    上次泄漏(单 worker 串行下,此刻还活着 = 上轮没干净退出),taskkill 之。wmic 列名
    按字母序输出(CommandLine 在前、ProcessId 在后),故 pid 取每行最后一个 token。
    """
    needle = str(user_data_dir).lower()
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='chrome.exe'", "get", "ProcessId,CommandLine"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception:
        out = ""
    for line in out.splitlines():
        if needle not in line.lower():
            continue
        pid_str = line.rsplit(None, 1)[-1].strip()
        if not pid_str.isdigit():
            continue
        try:
            subprocess.run(["taskkill", "/F", "/PID", pid_str], capture_output=True, timeout=5)
            logger.warning(
                f"[browser] 清理残留浏览器 pid={pid_str}:profile {user_data_dir.name}"
                " 被上次泄漏的会话占用,否则新会话会撞单例卡空白页"
            )
        except Exception as exc:
            logger.warning(f"[browser] 残留浏览器 pid={pid_str} 清理失败: {exc}")
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (user_data_dir / name).unlink()
        except OSError:
            pass


def _reap_stale_profile_lock(user_data_dir: Path) -> None:
    """launch 前清理占用该 profile 的残留 Chrome + 陈旧单例锁。

    单 worker 串行模型:即将为某账号启动浏览器时 profile 仍被占用 = 上次浏览器泄漏
    (典型:扫码续期开了 headed 浏览器、用户手动关窗口,patchright kill EPERM 没杀掉)。
    不清理则新 launch 撞 Chromium 单例 → 把请求交给旧会话并自身退出 → patchright
    TargetClosedError(表现为空白页卡住,告警里误标"网络异常")。

    posix 机制:Chromium 的 SingletonLock 是指向 "<host>-<pid>" 的 symlink。读出 pid,
    确认它确实是用着本 profile 的进程(命令行含该路径,防 PID 复用误杀),杀掉,再删锁。
    Windows 没有该 symlink(用命名互斥量),走 `_reap_windows_profile_holders` 反查进程。
    """
    if sys.platform == "win32":
        _reap_windows_profile_holders(user_data_dir)
        return
    try:
        target = os.readlink(user_data_dir / "SingletonLock")
    except OSError:
        return  # 无残留锁(正常路径,零开销)
    try:
        pid: int | None = int(target.rsplit("-", 1)[-1])
    except ValueError:
        pid = None
    if pid is not None and _process_cmdline_contains(pid, str(user_data_dir.resolve())):
        sig = getattr(signal, "SIGKILL", signal.SIGTERM)
        try:
            os.kill(pid, sig)
            logger.warning(
                f"[browser] 清理残留浏览器 pid={pid}:profile {user_data_dir.name}"
                " 被上次泄漏的会话占用(如扫码续期手动关窗口),否则新会话会撞单例卡空白页"
            )
        except OSError as exc:
            logger.warning(f"[browser] 残留浏览器 pid={pid} 清理失败: {exc}")
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (user_data_dir / name).unlink()
        except OSError:
            pass


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
    # 启动前清掉上次泄漏的浏览器 / 陈旧单例锁,否则会撞 Chromium 单例 → 卡空白页
    _reap_stale_profile_lock(user_data_dir)

    # 反检测档位由 platform_meta 声明:needs_fingerprint=True 的平台注入 per-account
    # 指纹 + AutomationControlled,靠 persistent context 持久化 cookie;False 的平台
    # 不注入指纹,改用 cookies.json 显式持久化(patchright persistent context 对
    # session cookie 落盘不可靠)。
    needs_fingerprint = get_meta(platform).needs_fingerprint

    _use_explicit_cookies = not needs_fingerprint
    _cookie_file = user_data_dir / "cookies.json"

    if needs_fingerprint:
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
    # 指纹只对 needs_fingerprint 平台启用;其它平台跳过(风控机制不同,不需要设备指纹模拟)
    if account_id is not None and not disable_all and needs_fingerprint:
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

    use_real_chrome = get_meta(platform).use_real_chrome

    # CDP 分支:subprocess 启系统真实 Chrome + connect_over_cdp(绕 patchright patched
    # chromium 风控指纹,小红书用)。小红书 needs_fingerprint=False,无指纹注入,故本分支
    # 不处理 fp_init_script;cookie 走 cookies.json 显式持久化(同 launch 分支)。
    if use_real_chrome:
        chrome_proc, chrome_port = _launch_real_chrome(user_data_dir, headless=headless)
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{chrome_port}", is_local=True
                )
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else context.new_page()
                if _cookie_file.exists():
                    try:
                        with open(_cookie_file) as f:
                            saved = _json.load(f)
                        context.add_cookies(saved)
                        logger.info(
                            f"[browser] {platform} 恢复 {len(saved)} 个 cookie from {_cookie_file}"
                        )
                    except Exception as exc:
                        logger.warning(f"[browser] 恢复 cookie 失败: {exc}")
                try:
                    loaded = context.cookies()
                    logger.info(
                        f"[browser] {platform} CDP context 启动(real chrome),"
                        f" user_data_dir={user_data_dir}, cookies_loaded={len(loaded)}, port={chrome_port}"
                    )
                except Exception:
                    pass
                yield page
            finally:
                if _use_explicit_cookies:
                    try:
                        all_cookies = context.cookies()
                        with open(_cookie_file, "w") as f:
                            _json.dump(all_cookies, f, ensure_ascii=False)
                        logger.info(
                            f"[browser] {platform} 保存 {len(all_cookies)} 个 cookie to {_cookie_file}"
                        )
                    except Exception as exc:
                        logger.warning(f"[browser] 保存 cookie 失败: {exc}")
                try:
                    page.goto("about:blank")
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
                # CDP 关键:context.close()/browser.close() 只断连接不杀进程,必须显式 terminate
                # 自己 Popen 的 Chrome,否则泄漏持有 SingletonLock → 下次撞单例(见 _reap_stale_profile_lock)。
                try:
                    chrome_proc.terminate()
                    chrome_proc.wait(timeout=5)
                except Exception:
                    try:
                        chrome_proc.kill()
                    except Exception:
                        pass
        return

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
                        f"[browser] {platform} 恢复 {len(saved)} 个 cookie from {_cookie_file}"
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
                        f"[browser] {platform} 保存 {len(all_cookies)} 个 cookie to {_cookie_file}"
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
    meta = login_meta_for(platform)
    page.goto(meta["home_url"], wait_until="domcontentloaded")
    if meta["mode"] == "url":
        login_fragment = meta["login_fragment"]
        deadline = _time.monotonic() + timeout_ms / 1000.0
        while _time.monotonic() < deadline:
            if login_fragment not in page.url:
                _settle_cookies(page, platform)
                return True
            page.wait_for_timeout(2000)
        return False
    if meta["mode"] == "logged_in_url":
        # 已登录判据:URL 进入 logged_in_fragment(如 creator-micro/*)且登录文案都消失。
        # 抖音扫码后落到 creator-micro/home(非上传页),故不能等上传页专属元素。
        fragment = meta["logged_in_fragment"]
        markers = [m for m in meta.get("login_markers", "").split(",") if m]
        deadline = _time.monotonic() + timeout_ms / 1000.0
        while _time.monotonic() < deadline:
            if fragment in page.url and not _login_markers_visible(page, markers):
                _settle_cookies(page, platform)
                return True
            page.wait_for_timeout(2000)
        return False
    try:
        page.wait_for_selector(meta["selector"], timeout=timeout_ms, state="visible")
        return True
    except Exception:
        return False


def _login_markers_visible(page: Page, markers: list[str]) -> bool:
    """登录文案(扫码登录/验证码登录)任一可见 = 还在登录页。"""
    for marker in markers:
        try:
            if page.get_by_text(marker, exact=True).first.is_visible():
                return True
        except Exception:
            pass
    return False


def _settle_cookies(page: Page, platform: str) -> None:
    """登录确认后等 cookie 落盘:douyin/taobao 用 cookies.json 持久化,关 context 前必须 flush。"""
    page.wait_for_timeout(5000)
    try:
        cookies = page.context.cookies()
        logger.info(f"[browser] {platform} 登录成功, cookies={len(cookies)} url={page.url}")
    except Exception:
        pass
    page.wait_for_timeout(2000)  # 再等 2 秒确保 SQLite WAL 同步到 disk


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
