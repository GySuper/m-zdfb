"""wxsp 托管的系统真实 Chrome + 自动注入的发布插件(拼多多插件发布)。

设计见 docs/superpowers/specs/2026-11-18-pinduoduo-extension-publish-design.md。

  - 每条任务启动一次系统 Chrome(账号独立 profile),`--remote-debugging-port=0`
    拿调试端口;退出时 `Browser.close` 优雅关闭,cookie 由真实 Chrome profile 自己落盘。
  - 品牌版 Chrome ≥136 忽略 `--load-extension`,插件经 CDP `Extensions.loadUnpacked`
    注入;Chrome 会给 unpacked 插件留 staging 副本,先 uninstall 再 load 保证用当前代码。
  - 通信只走这一条 CDP 连接:Python 附着到插件 service worker,调它挂在
    `globalThis.wxsp` 上的函数;插件经 Runtime binding `wxspEmit` 回传消息
    (step / gate / halted / done / error)。不另起 WebSocket 服务,CLI 单独跑也能用。
  - 插件的所有页面操作(可信点击/输入/文件/截图)都在插件里用 chrome.debugger 完成,
    Python 不附着任何页面 target,页面侧看不到 Python 的自动化痕迹。
"""

from __future__ import annotations

import base64
import json
import queue
import subprocess
import threading
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger
from websockets.sync.client import connect

from wxsp.browser import (
    _profile_session,
    _reap_stale_profile_lock,
    _system_chrome_path,
    _wait_for_devtools_port,
)

EXTENSION_DIR = Path(__file__).resolve().parent / "extension"
# 由 extension/manifest.json 的 key(公钥)派生,改 key 必须同步改这里。
EXTENSION_ID = "kjpfbgfcnehdmhphconjddaolgecdcam"
_BINDING = "wxspEmit"


class CdpError(RuntimeError):
    """CDP 命令失败 / 超时 / 连接断开。"""


class CdpClient:
    """极简同步 CDP 客户端:一条 browser 级 WebSocket,flatten session。"""

    def __init__(self, ws_url: str) -> None:
        self._ws = connect(ws_url, max_size=None, open_timeout=10)
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self.events: queue.Queue[tuple[str | None, str, dict[str, Any]]] = queue.Queue()
        self.closed = threading.Event()
        threading.Thread(target=self._read, daemon=True, name="cdp-reader").start()

    def _read(self) -> None:
        try:
            for raw in self._ws:
                msg = json.loads(raw)
                if "id" in msg:
                    with self._lock:
                        waiter = self._pending.pop(msg["id"], None)
                    if waiter is not None:
                        waiter.put(msg)
                else:
                    self.events.put((msg.get("sessionId"), msg["method"], msg.get("params", {})))
        except Exception:
            pass
        finally:
            self.closed.set()
            with self._lock:
                waiters, self._pending = list(self._pending.values()), {}
            for waiter in waiters:
                waiter.put({"error": {"message": "CDP 连接已断开"}})

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        if self.closed.is_set():
            raise CdpError(f"{method}: CDP 连接已断开")
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            self._pending[msg_id] = waiter
        payload: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        self._ws.send(json.dumps(payload))
        try:
            msg = waiter.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._pending.pop(msg_id, None)
            raise CdpError(f"{method}: {timeout:.0f}s 内无响应") from None
        if "error" in msg:
            raise CdpError(f"{method}: {msg['error'].get('message')}")
        result: dict[str, Any] = msg.get("result", {})
        return result

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


@dataclass
class ManagedChrome:
    proc: subprocess.Popen[str]
    cdp: CdpClient

    def page_urls(self) -> list[str]:
        infos = self.cdp.send("Target.getTargets")["targetInfos"]
        return [t["url"] for t in infos if t["type"] == "page"]


def _browser_ws_url(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5) as resp:
        url: str = json.load(resp)["webSocketDebuggerUrl"]
    return url


def _launch(user_data_dir: Path, start_url: str) -> tuple[subprocess.Popen[str], int]:
    argv = [
        str(_system_chrome_path()),
        f"--user-data-dir={user_data_dir.resolve()}",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        # 插件在后台标签页里干活,不让 Chrome 节流后台定时器/渲染
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        start_url,
    ]
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    port = _wait_for_devtools_port(proc, timeout_seconds=20)
    if port is None:
        proc.kill()
        raise RuntimeError("Chrome 调试端口 20s 内未就绪")
    return proc, port


@contextmanager
def managed_chrome(
    user_data_dir: Path, *, start_url: str = "about:blank"
) -> Iterator[ManagedChrome]:
    """启动账号 profile 的系统 Chrome,退出时优雅关闭(保证 cookie 落盘)。"""
    with _profile_session(user_data_dir):
        _reap_stale_profile_lock(user_data_dir)
        proc, port = _launch(user_data_dir, start_url)
        cdp: CdpClient | None = None
        try:
            cdp = CdpClient(_browser_ws_url(port))
            yield ManagedChrome(proc, cdp)
        finally:
            if cdp is not None:
                try:
                    cdp.send("Browser.close", timeout=5)
                except CdpError:
                    pass
                cdp.close()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def wait_logged_in_url(
    user_data_dir: Path, *, start_url: str, fragment: str, timeout_s: float
) -> bool:
    """打开 start_url,轮询任一标签页 URL 含 fragment 即已登录(扫码登录 / doctor 共用)。"""
    with managed_chrome(user_data_dir, start_url=start_url) as chrome:
        deadline = time.monotonic() + timeout_s
        urls: list[str] = []
        while time.monotonic() < deadline:
            urls = chrome.page_urls()
            if any(fragment in u for u in urls):
                return True
            time.sleep(2)
        logger.warning(f"[managed_chrome] 等登录超时,窗口停在: {urls}")
    return False


class ExtensionSession:
    """插件会话。鸭子类型兼容 runner 截图/存 HTML 用到的 `screenshot()` / `content()`。"""

    def __init__(self, cdp: CdpClient) -> None:
        self._cdp = cdp
        self._sid = _attach_extension(cdp)

    def call(self, fn: str, *args: Any, timeout: float = 30.0) -> Any:
        expression = f"wxsp.{fn}(...{json.dumps(list(args), ensure_ascii=False)})"
        res = self._cdp.send(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            session_id=self._sid,
            timeout=timeout,
        )
        if "exceptionDetails" in res:
            detail = res["exceptionDetails"]
            text = detail.get("exception", {}).get("description") or detail.get("text")
            raise CdpError(f"插件 wxsp.{fn} 出错: {text}")
        return res.get("result", {}).get("value")

    def start_job(self, job: dict[str, Any]) -> None:
        self.call("startJob", job)

    def proceed(self, action: str) -> None:
        self.call("proceed", action)

    def next_message(self, timeout: float) -> dict[str, Any]:
        """取插件下一条回传消息;超时抛 TimeoutError,插件后台被回收抛 CdpError。"""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"插件 {timeout:.0f}s 内无消息")
            try:
                sid, method, params = self._cdp.events.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if self._cdp.closed.is_set():
                    raise CdpError("Chrome 已关闭或 CDP 连接断开") from None
                continue
            if method == "Target.detachedFromTarget" and params.get("sessionId") == self._sid:
                raise CdpError("插件后台已被 Chrome 回收")
            if sid != self._sid:
                continue
            if method == "Runtime.bindingCalled" and params.get("name") == _BINDING:
                msg: dict[str, Any] = json.loads(params["payload"])
                return msg
            if method == "Runtime.consoleAPICalled":
                text = " ".join(
                    str(a.get("value", a.get("description", ""))) for a in params.get("args", [])
                )
                logger.debug(f"[extension] {text}")
            elif method == "Runtime.exceptionThrown":
                desc = params.get("exceptionDetails", {}).get("exception", {}).get("description")
                logger.warning(f"[extension] 未捕获异常: {desc}")

    def screenshot(self, path: str, full_page: bool = False) -> None:
        Path(path).write_bytes(base64.b64decode(self.call("screenshot")))

    def content(self) -> str:
        html: str = self.call("html")
        return html


def _attach_extension(cdp: CdpClient) -> str:
    """注入插件 → 附着它的 service worker → 挂回传 binding,返回 sessionId。"""
    try:
        cdp.send("Extensions.uninstall", {"id": EXTENSION_ID})
    except CdpError:
        pass  # 首次启动未安装,正常
    loaded = cdp.send("Extensions.loadUnpacked", {"path": str(EXTENSION_DIR)}, timeout=20)
    if loaded.get("id") != EXTENSION_ID:
        raise RuntimeError(f"插件 ID 不符:{loaded.get('id')}(manifest key 与 EXTENSION_ID 不一致)")
    prefix = f"chrome-extension://{EXTENSION_ID}/"
    deadline = time.monotonic() + 10
    while True:
        infos = cdp.send("Target.getTargets")["targetInfos"]
        sw = next(
            (t for t in infos if t["type"] == "service_worker" and t["url"].startswith(prefix)),
            None,
        )
        if sw is not None:
            break
        if time.monotonic() > deadline:
            raise RuntimeError("插件后台 10s 内未启动")
        time.sleep(0.2)
    sid: str = cdp.send("Target.attachToTarget", {"targetId": sw["targetId"], "flatten": True})[
        "sessionId"
    ]
    cdp.send("Runtime.enable", session_id=sid)
    cdp.send("Runtime.addBinding", {"name": _BINDING}, session_id=sid)
    return sid


@contextmanager
def extension_session(user_data_dir: Path) -> Iterator[ExtensionSession]:
    with managed_chrome(user_data_dir) as chrome:
        yield ExtensionSession(chrome.cdp)
