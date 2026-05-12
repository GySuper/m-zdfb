"""Sanity checks on the stealth init script content."""

from __future__ import annotations

from wxsp.stealth_js import INIT_SCRIPT


def test_init_script_is_non_empty_string():
    assert isinstance(INIT_SCRIPT, str)
    assert len(INIT_SCRIPT) > 100  # not just a stub


def test_init_script_patches_webdriver_flag():
    # The single most important anti-bot signal: navigator.webdriver should be hidden.
    assert "navigator" in INIT_SCRIPT
    assert "webdriver" in INIT_SCRIPT


def test_init_script_patches_chrome_runtime():
    assert "window.chrome" in INIT_SCRIPT or "chrome.runtime" in INIT_SCRIPT


def test_init_script_patches_plugins_and_languages():
    assert "plugins" in INIT_SCRIPT
    assert "languages" in INIT_SCRIPT
    assert "zh-CN" in INIT_SCRIPT  # we target Chinese locale


def test_init_script_patches_permissions_query():
    assert "permissions" in INIT_SCRIPT
    assert "query" in INIT_SCRIPT
