"""账号浏览器会话互斥:新会话不能清理仍在工作的浏览器。"""

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from wxsp import browser


def test_profile_session_is_exclusive_and_released_after_error(tmp_path: Path) -> None:
    profile = tmp_path / "account"
    with pytest.raises(ValueError, match="test failure"):
        with browser._profile_session(profile):
            with pytest.raises(RuntimeError, match="浏览器正在使用"):
                with browser._profile_session(profile):
                    pytest.fail("同账号的第二个会话不应进入")
            with browser._profile_session(tmp_path / "another-account"):
                pass
            raise ValueError("test failure")
    with browser._profile_session(profile):
        pass


def test_profile_session_is_exclusive_across_processes(tmp_path: Path) -> None:
    profile = tmp_path / "account"
    script = """
import sys
from pathlib import Path
from wxsp.browser import _profile_session
try:
    with _profile_session(Path(sys.argv[1])):
        sys.stdout.write('acquired')
except RuntimeError:
    sys.stdout.write('busy')
"""
    with browser._profile_session(profile):
        result = subprocess.run(
            [sys.executable, "-c", script, str(profile)],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert result.stdout == "busy"
    result = subprocess.run(
        [sys.executable, "-c", script, str(profile)],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout == "acquired"


@pytest.mark.integration
def test_second_context_preserves_live_browser(tmp_path: Path) -> None:
    profile = tmp_path / "account"
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    with browser.browser_context(profile, headless=True, platform="taobao_guanghe") as page:
        page.set_content('<p>publishing</p><input type="file">')
        errors: list[Exception] = []

        def open_second_context() -> None:
            try:
                with browser.browser_context(profile, headless=True, platform="taobao_guanghe"):
                    errors.append(AssertionError("同账号的第二个会话不应启动"))
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=open_second_context, daemon=True)
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert "浏览器正在使用" in str(errors[0])
        assert page.locator("p").inner_text() == "publishing"
        page.locator('input[type="file"]').set_input_files(str(video), timeout=2_000)
        assert page.locator("input").evaluate("input => input.files[0].name") == "video.mp4"
    with browser.browser_context(profile, headless=True, platform="taobao_guanghe") as page:
        page.set_content("<p>reopened</p>")
        assert page.locator("p").inner_text() == "reopened"
