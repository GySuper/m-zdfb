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
    from wxsp.browser import _PLATFORM_LOGIN_META

    tc = _PLATFORM_LOGIN_META["tencent_channel"]
    assert tc["mode"] == "selector"
    # 选择器必须提到仅登录后出现的文本
    assert "发表" in tc["selector"] or "发布" in tc["selector"]


def test_platform_login_meta_has_taobao_guanghe():
    from wxsp.browser import _PLATFORM_LOGIN_META

    tb = _PLATFORM_LOGIN_META["taobao_guanghe"]
    assert tb["mode"] == "url"
    assert "login.taobao.com" in tb["login_fragment"]


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
