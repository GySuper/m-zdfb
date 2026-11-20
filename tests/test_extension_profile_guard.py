"""插件版平台(拼多多)的 profile 属于系统 Chrome:patchright 不能碰,账号页「打开浏览器」走系统 Chrome。

2026-11-19:「打开浏览器」用 patchright Chromium 147 打开了 Chrome 157 的 profile,
解不开 Chrome 的 cookie → 显示未登录,还可能弄坏真实 Chrome 的登录态。
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_browser_context_refuses_extension_platform_profile(tmp_path: Path) -> None:
    from wxsp.browser import browser_context

    with pytest.raises(RuntimeError, match="系统 Chrome"):
        with browser_context(tmp_path / "profile", platform="pinduoduo"):
            pass
    assert not (tmp_path / "profile").exists()  # 拒绝发生在启动浏览器之前


def test_open_browser_uses_system_chrome_for_extension_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wxsp.api.routes_accounts as ra
    import wxsp.managed_chrome as mc

    calls: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        mc,
        "open_for_manual_use",
        lambda user_data_dir, *, start_url, max_seconds: calls.append((user_data_dir, start_url)),
    )
    ra._run_open_browser("account_x", tmp_path / "profile", platform="pinduoduo")
    assert calls and calls[0][0] == tmp_path / "profile"
    assert "pinduoduo.com" in calls[0][1]
