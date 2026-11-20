"""安装向导第 1 步自检:安装包不再带 Chromium,改查电脑上的 Google Chrome。

2026-11-20 Windows 实装:向导报「Chromium 未找到: ...\\_internal\\chromium」,下一步被禁用卡死。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import wxsp.browser
from wxsp.api.routes_setup import _run_self_check


@pytest.fixture(autouse=True)
def _data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wxsp.config.get_user_data_dir", lambda: tmp_path)
    # 模拟打包环境:自带 Chromium 的目录不存在
    monkeypatch.setattr(
        wxsp.browser, "_chromium_root", lambda: tmp_path / "_internal" / "chromium", raising=False
    )


def test_passes_with_system_chrome_without_bundled_chromium(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chrome = tmp_path / "chrome.exe"
    monkeypatch.setattr(wxsp.browser, "_system_chrome_path", lambda: chrome)
    checks = _run_self_check()
    assert all(c["ok"] for c in checks), checks
    assert not any("Chromium" in c["name"] for c in checks)
    assert {"name": "Google Chrome", "ok": True, "detail": str(chrome)} in checks


def test_fails_with_install_hint_when_chrome_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> Path:
        raise FileNotFoundError

    monkeypatch.setattr(wxsp.browser, "_system_chrome_path", missing)
    chrome = next(c for c in _run_self_check() if c["name"] == "Google Chrome")
    assert chrome["ok"] is False
    assert "google.com/chrome" in chrome["detail"]
