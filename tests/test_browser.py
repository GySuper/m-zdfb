"""Smoke tests on the browser module's public surface.

Browser-touching code (`browser_context`, `wait_for_logged_in`) is exercised
manually in M2 acceptance (Task 8) and by `wxsp login` against the test account
— launching real Chromium under pytest is too heavy and not CI-safe.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch


def test_wechat_channels_home_constant_is_https():
    from wxsp.browser import WECHAT_CHANNELS_HOME

    assert WECHAT_CHANNELS_HOME.startswith("https://")
    assert "channels.weixin.qq.com" in WECHAT_CHANNELS_HOME


def test_platform_login_meta_has_tencent_channel():
    from wxsp.browser import login_meta_for

    tc = login_meta_for("tencent_channel")
    assert tc["mode"] == "selector"
    # 选择器必须提到仅登录后出现的文本
    assert "发表" in tc["selector"] or "发布" in tc["selector"]


def test_platform_login_meta_has_taobao_guanghe():
    from wxsp.browser import login_meta_for

    tb = login_meta_for("taobao_guanghe")
    assert tb["mode"] == "url"
    assert "login.taobao.com" in tb["login_fragment"]


def test_platform_login_meta_has_douyin():
    from wxsp.browser import login_meta_for

    dy = login_meta_for("douyin")
    # 扫码后抖音落到 creator-micro/home(非上传页),故用 URL 片段 + 登录文案消失判定,
    # 不能用上传页专属按钮的 selector 模式。
    assert dy["mode"] == "logged_in_url"
    assert "creator-micro/" in dy["logged_in_fragment"]
    assert "扫码登录" in dy["login_markers"]


# ---- wait_for_logged_in 的 "logged_in_url" 检测回路(用假 page,不开真浏览器)----


class _FakeLocator:
    def __init__(self, visible: bool) -> None:
        self._visible = visible

    @property
    def first(self) -> _FakeLocator:
        return self

    def is_visible(self) -> bool:
        return self._visible


class _FakeContext:
    def cookies(self) -> list:
        return []


class _FakePage:
    """按脚本化的 (url, 可见登录文案) 序列模拟轮询;每次 wait_for_timeout 前进一步,末步重复。"""

    def __init__(self, steps: list) -> None:
        self._steps = steps
        self._i = 0
        self.context = _FakeContext()
        self.goto_url: str | None = None

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self.goto_url = url

    def _cur(self) -> tuple:
        return self._steps[min(self._i, len(self._steps) - 1)]

    @property
    def url(self) -> str:
        return self._cur()[0]

    def get_by_text(self, text: str, exact: bool = False) -> _FakeLocator:
        return _FakeLocator(text in self._cur()[1])

    def wait_for_timeout(self, ms: int) -> None:
        self._i += 1


def test_wait_for_logged_in_logged_in_url_detects_after_scan_redirect():
    """扫码后从登录页跳到 creator-micro/home(无上传页按钮)也要判为已登录。"""
    from wxsp.browser import wait_for_logged_in

    login = ("https://creator.douyin.com/", {"扫码登录", "验证码登录"})
    home = ("https://creator.douyin.com/creator-micro/home", set())
    page = _FakePage([login, login, home])
    assert wait_for_logged_in(page, timeout_ms=100_000, platform="douyin") is True
    assert page.goto_url == "https://creator.douyin.com/"


def test_wait_for_logged_in_logged_in_url_false_while_markers_persist():
    """URL 含 creator-micro 但登录文案还在(登录浮层)→ 不能误判为已登录。"""
    from wxsp.browser import wait_for_logged_in

    overlay = ("https://creator.douyin.com/creator-micro/content/upload", {"扫码登录"})
    page = _FakePage([overlay])
    assert wait_for_logged_in(page, timeout_ms=80, platform="douyin") is False


def test_wait_for_logged_in_logged_in_url_times_out_when_never_logged_in():
    from wxsp.browser import wait_for_logged_in

    login = ("https://creator.douyin.com/", {"扫码登录", "验证码登录"})
    page = _FakePage([login])
    assert wait_for_logged_in(page, timeout_ms=80, platform="douyin") is False


def test_public_callables_importable():
    from wxsp.browser import browser_context, check_cookie, wait_for_logged_in

    assert callable(browser_context)
    assert callable(check_cookie)
    assert callable(wait_for_logged_in)


def test_check_cookie_passes_user_data_dir_through_to_browser_context(tmp_path, monkeypatch):
    """`check_cookie` is a thin wrapper: hand `user_data_dir` to `browser_context`,
    call `wait_for_logged_in`, return its bool.

    We monkeypatch `browser_context` and `wait_for_logged_in` to record their args.
    Real chromium launch is exercised manually in Task 8 acceptance, not under pytest.
    """
    from contextlib import contextmanager

    from wxsp import browser as browser_mod

    udd = tmp_path / "chrome-profiles" / "test_account"
    seen_dirs: list = []
    seen_timeouts: list = []
    sentinel_page = object()

    @contextmanager
    def fake_context(user_data_dir, *, headless=False, account_id=None, platform="tencent_channel"):
        seen_dirs.append(user_data_dir)
        yield sentinel_page

    def fake_wait(page, *, timeout_ms, platform="tencent_channel"):
        assert page is sentinel_page
        seen_timeouts.append(timeout_ms)
        return True

    monkeypatch.setattr(browser_mod, "browser_context", fake_context)
    monkeypatch.setattr(browser_mod, "wait_for_logged_in", fake_wait)

    result = browser_mod.check_cookie(udd, timeout_ms=1234)
    assert result is True
    assert seen_dirs == [udd]
    assert seen_timeouts == [1234]


def test_check_cookie_returns_false_when_wait_returns_false(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from wxsp import browser as browser_mod

    @contextmanager
    def fake_context(user_data_dir, *, headless=False, account_id=None, platform="tencent_channel"):
        yield object()

    def fake_wait(page, *, timeout_ms, platform="tencent_channel"):
        return False

    monkeypatch.setattr(browser_mod, "browser_context", fake_context)
    monkeypatch.setattr(browser_mod, "wait_for_logged_in", fake_wait)

    result = browser_mod.check_cookie(tmp_path / "udd", timeout_ms=1000)
    assert result is False


def test_chromium_root_dev_mode_returns_none(monkeypatch) -> None:
    """开发模式不动 PLAYWRIGHT_BROWSERS_PATH,patchright 走默认查找。"""
    from wxsp.browser import _chromium_root

    monkeypatch.delenv("WXSP_DEV_MODE", raising=False)
    # 默认 pytest 不是 Nuitka 编译,is_packaged() False
    assert _chromium_root() is None


def test_chromium_root_packaged_mac(monkeypatch) -> None:
    """打包模式 mac:返回 sys.executable 上溯 2 级 + Resources/chromium。"""
    from wxsp import browser

    main_module = sys.modules["__main__"]
    fake_exec = Path("/Applications/wxsp.app/Contents/MacOS/wxsp")
    with patch.object(main_module, "__compiled__", True, create=True):
        monkeypatch.delenv("WXSP_DEV_MODE", raising=False)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(sys, "executable", str(fake_exec))
        result = browser._chromium_root()
        assert result == Path("/Applications/wxsp.app/Contents/Resources/chromium")


def test_chromium_root_packaged_windows(monkeypatch) -> None:
    """打包模式 win:返回 sys.executable 同级 chromium 目录。"""
    from wxsp import browser

    main_module = sys.modules["__main__"]
    fake_exec = Path("C:/Program Files/wxsp/wxsp.exe")
    with patch.object(main_module, "__compiled__", True, create=True):
        monkeypatch.delenv("WXSP_DEV_MODE", raising=False)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(sys, "executable", str(fake_exec))
        result = browser._chromium_root()
        assert result == Path("C:/Program Files/wxsp/chromium")


def test_system_chrome_path_mac(monkeypatch) -> None:
    """mac:候选路径存在则返回它(第一个存在的候选)。"""
    from wxsp import browser

    monkeypatch.setattr(sys, "platform", "darwin")
    expected = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    # 让只有目标候选"存在",其余不存在
    monkeypatch.setattr(Path, "exists", lambda self: self == expected)
    assert browser._system_chrome_path() == expected


def test_system_chrome_path_windows(monkeypatch, tmp_path) -> None:
    """win:查 %ProgramFiles%/Google/Chrome/Application/chrome.exe。"""
    from wxsp import browser

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    expected = tmp_path / "Google/Chrome/Application/chrome.exe"
    monkeypatch.setattr(Path, "exists", lambda self: self == expected)
    assert browser._system_chrome_path() == expected


def test_system_chrome_path_not_found(monkeypatch) -> None:
    """找不到系统 Chrome → 抛 FileNotFoundError(带引导文案)。"""
    from wxsp import browser

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "exists", lambda self: False)  # 所有候选都不存在
    import pytest

    with pytest.raises(FileNotFoundError, match="Google Chrome"):
        browser._system_chrome_path()
