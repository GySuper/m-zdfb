from __future__ import annotations

from unittest.mock import MagicMock

from wxsp import human_input


def test_physical_click_verifies_success_after_retry(monkeypatch) -> None:
    page = MagicMock()
    locator = MagicMock()
    monkeypatch.setattr(human_input, "_box_center_screen", lambda *_: (100.0, 200.0))
    monkeypatch.setattr(human_input.time, "sleep", lambda *_: None)
    monkeypatch.setattr(human_input.random, "uniform", lambda a, _b: a)
    monkeypatch.setattr(human_input.pyautogui, "moveTo", MagicMock())
    mouse_down = MagicMock()
    mouse_up = MagicMock()
    monkeypatch.setattr(human_input.pyautogui, "mouseDown", mouse_down)
    monkeypatch.setattr(human_input.pyautogui, "mouseUp", mouse_up)

    checks = iter([False, True])
    human_input.physical_click(page, locator, verify=lambda: next(checks), retries=2)

    assert mouse_down.call_count == 2
    assert mouse_up.call_count == 2
