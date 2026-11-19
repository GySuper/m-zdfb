"""托管 Chrome + 插件通路的真浏览器集成测试(本地测试页,不碰任何真实平台)。

会弹出真实 Chrome 窗口,默认跳过:`WXSP_CHROME_IT=1 pytest tests/test_managed_chrome.py`。
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from wxsp.managed_chrome import ExtensionSession, extension_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("WXSP_CHROME_IT"), reason="set WXSP_CHROME_IT=1 to run"),
]

PAGE = """<!doctype html><meta charset=utf-8>
<button id=btn style="margin:40px;padding:16px">点我</button>
<textarea id=ed></textarea><input type=file>
<div style="height:2400px"></div><button>发布</button><pre id=log></pre>
<script>
const log = (k, v) => { const o = JSON.parse(document.getElementById('log').textContent || '{}'); o[k] = v;
  document.getElementById('log').textContent = JSON.stringify(o); };
document.getElementById('btn').onclick = e => { log('click', e.isTrusted); document.getElementById('ed').focus(); };
document.getElementById('ed').oninput = e => log('typed', e.target.value);
document.querySelector('input[type=file]').onchange = e => { const f = e.target.files[0];
  log('file', [f.name, f.size, e.isTrusted]); };
document.querySelectorAll('button')[1].onclick = e => { window.published = true; log('published', e.isTrusted); };
</script>"""


@pytest.fixture
def page_url(tmp_path: Path) -> Iterator[str]:
    (tmp_path / "page.html").write_text(PAGE, encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/page.html"
    server.shutdown()


def _until(session: ExtensionSession, kind: str) -> list[dict[str, object]]:
    seen = []
    while True:
        msg = session.next_message(timeout=30)
        seen.append(msg)
        assert msg["type"] != "error", msg
        if msg["type"] == kind:
            return seen


def test_selftest_job_runs_through_gate_and_publish(tmp_path: Path, page_url: str) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\0" * 123_456)
    with extension_session(tmp_path / "profile") as session:
        session.start_job(
            {
                "platform": "selftest",
                "url": page_url,
                "text": "#你好 拼多多",
                "video_path": str(video),
                "step_pause": [0, 0],
            }
        )
        steps = [m["step"] for m in _until(session, "gate") if m["type"] == "step"]
        assert steps == ["open", "click", "type", "upload"]

        shot = tmp_path / "gate.png"
        session.screenshot(str(shot))
        assert shot.read_bytes()[:4] == b"\x89PNG"

        session.proceed("publish")
        done = _until(session, "done")[-1]
        assert done["remote_url"] == page_url

        state = json.loads(session.content().split('<pre id="log">')[1].split("</pre>")[0])
    assert state == {
        "click": True,
        "typed": "#你好 拼多多",
        "file": ["clip.mp4", 123_456, True],
        "published": True,  # 发布按钮在视口外,验证了先滚入再点
    }


def test_halt_before_stops_without_running_the_step(tmp_path: Path, page_url: str) -> None:
    with extension_session(tmp_path / "profile") as session:
        session.start_job(
            {
                "platform": "selftest",
                "url": page_url,
                "text": "x",
                "halt_before": "click",
                "step_pause": [0, 0],
            }
        )
        msgs = _until(session, "halted")
    assert [m["type"] for m in msgs] == ["step", "halted"]
    assert msgs[-1]["step"] == "click"
