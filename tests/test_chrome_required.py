"""安装包不再自带浏览器:拼多多(插件版)要求电脑装了 Google Chrome。

没装时:体检标红并给下载地址;发布报「电脑上没装 Chrome」(整轮中止),汇总里是大白话。
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _no_chrome() -> Path:
    raise FileNotFoundError("未找到系统 Google Chrome")


def test_doctor_reports_chrome(monkeypatch: pytest.MonkeyPatch) -> None:
    from wxsp.doctor import check_chrome

    monkeypatch.setattr("wxsp.browser._system_chrome_path", lambda: Path("/Apps/Google Chrome"))
    assert check_chrome().ok
    monkeypatch.setattr("wxsp.browser._system_chrome_path", _no_chrome)
    row = check_chrome()
    assert not row.ok
    assert "google.com/chrome" in row.detail


def test_launch_without_chrome_raises_chrome_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wxsp import managed_chrome
    from wxsp.errors import ChromeMissing, classify

    monkeypatch.setattr(managed_chrome, "_system_chrome_path", _no_chrome)
    with pytest.raises(ChromeMissing) as exc:
        managed_chrome._launch(tmp_path, "about:blank")
    assert classify(exc.value) == "chrome_missing"


def test_chrome_missing_reads_plainly_and_stops_the_run() -> None:
    from wxsp.notify import describe_failure
    from wxsp.scheduler import _GLOBAL_HALT_ERRORS

    reason, action = describe_failure("chrome_missing", "step=browser: 电脑上没装 Google Chrome")
    assert reason.startswith("电脑上没装 Chrome")
    assert "安装" in action
    assert "chrome_missing" in _GLOBAL_HALT_ERRORS  # 每条都会失败,整轮停下只报一次
