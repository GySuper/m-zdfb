"""托管 Chrome + 插件通路的真浏览器集成测试(本地测试页,不碰任何真实平台)。

无窗口(--headless=new)运行;需本机装 Chrome,默认跳过:`WXSP_CHROME_IT=1 pytest tests/test_managed_chrome.py`。
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from wxsp import managed_chrome
from wxsp.managed_chrome import CdpError, ExtensionSession, extension_session

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
log('webdriver', navigator.webdriver);
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
    with extension_session(tmp_path / "profile", headless=True) as session:
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
        "webdriver": False,  # 带调试端口启动不能暴露自动化标记(平台会上滑块/拒发验证码)
        "click": True,
        "typed": "#你好 拼多多",
        "file": ["clip.mp4", 123_456, True],
        "published": True,  # 发布按钮在视口外,验证了先滚入再点
    }


def test_halt_before_stops_without_running_the_step(tmp_path: Path, page_url: str) -> None:
    with extension_session(tmp_path / "profile", headless=True) as session:
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


# ---- 拼多多入口弹窗(插件 entryPopups 跑在本地夹具页上)----

POPUP_PAGES = {
    # 叠加两个弹窗,各自有关闭按钮
    "stacked": """<div role=dialog style="width:300px;height:100px">A
      <button aria-label="关闭" onclick="this.parentNode.remove()">x</button></div>
      <div role=dialog style="width:300px;height:100px">B
      <button aria-label="关闭" onclick="this.parentNode.remove()">x</button></div>""",
    # 弹窗没有关闭按钮,只能 Escape
    "escape": """<div role=dialog style="width:300px;height:100px">A</div>
      <script>addEventListener('keydown', e => { if (e.key === 'Escape')
        document.querySelector('[role=dialog]')?.remove(); });</script>""",
    # 关不掉:必须报错,绝不能去点业务按钮
    "stuck": """<div role=dialog style="width:300px;height:100px">A <button>立即参与</button></div>""",
    # 2026-11-18 真机:上传完成后弹「立即推广 冲顶交易额」(beast-core modal + 关闭图标 + 去推广)
    "promo_modal": """<div data-testid="beast-core-modal" class="MDL_outerWrapper_5-180-0 MDL_modal_5-180-0"
        style="width:400px;height:300px">
      <svg data-testid="beast-core-icon-close" class="goods_popularize_pop_up_closeBtn__6KE37"
        width="24" height="24" onclick="this.parentNode.remove()"><rect width="24" height="24"/></svg>
      <button onclick="document.body.dataset.promoted = 1">去推广</button></div>""",
    # 2026-11-18 真机:上传后表单里的内嵌提示条带关闭图标,不是弹窗,必须放过
    "notice_bar": """<div data-testid="beast-core-noticeBar" style="margin-top:1600px">
      该视频未匹配到有效商品<svg data-testid="beast-core-icon-close" width="16" height="16"
        onclick="document.body.dataset.promoted = 1"><rect width="16" height="16"/></svg></div>""",
    # 真实商品推广浮层:无 dialog 容器,只有全局可见的 beast close 图标
    "promo": """<div style="width:300px;height:100px">推广</div>
      <svg data-testid="beast-core-icon-close" width="24" height="24"
        onclick="this.remove()"><rect width="24" height="24"/></svg>""",
}


def _run_popups(tmp_path: Path, name: str) -> dict[str, object]:
    (tmp_path / f"{name}.html").write_text(
        f"<!doctype html><meta charset=utf-8><body>{POPUP_PAGES[name]}</body>", encoding="utf-8"
    )
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with extension_session(tmp_path / "profile", headless=True) as session:
            session.start_job(
                {
                    "platform": "selftest_pdd_popups",
                    "url": f"http://127.0.0.1:{server.server_address[1]}/{name}.html",
                    "step_pause": [0, 0],
                }
            )
            while True:
                msg = session.next_message(timeout=30)
                if msg["type"] in ("gate", "error"):
                    if msg["type"] == "gate":
                        left = session.call("html")
                        msg["dialogs_left"] = left.count('role="dialog"') + left.count(
                            "beast-core-icon-close"
                        )
                        msg["promoted"] = "data-promoted" in left
                    return msg
    finally:
        server.shutdown()


@pytest.mark.parametrize("name", ["stacked", "escape", "promo", "promo_modal"])
def test_entry_popups_are_closed_safely(tmp_path: Path, name: str) -> None:
    msg = _run_popups(tmp_path, name)
    assert msg["type"] == "gate", msg
    assert msg["dialogs_left"] == 0
    assert not msg["promoted"]  # 只点关闭,绝不点「去推广」等业务按钮


def test_inline_notice_bar_is_left_alone(tmp_path: Path) -> None:
    msg = _run_popups(tmp_path, "notice_bar")
    assert msg["type"] == "gate", msg
    assert not msg["promoted"]  # 提示条的关闭图标没被点


def test_entry_popup_that_cannot_be_closed_fails_closed(tmp_path: Path) -> None:
    msg = _run_popups(tmp_path, "stuck")
    assert msg["type"] == "error"
    assert msg["kind"] == "element_not_found"
    assert "无法安全关闭" in str(msg["message"])


def test_relaunch_on_same_profile_runs_current_extension_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """profile 里留着上次的插件后台和脚本缓存;插件代码一改,下次启动必须跑新代码。"""
    ext = tmp_path / "extension"
    shutil.copytree(managed_chrome.EXTENSION_DIR, ext)
    monkeypatch.setattr(managed_chrome, "EXTENSION_DIR", ext)
    selftest = ext / "selftest.js"
    prev = "open"
    for step in ("open", "open_v2", "open_v3"):
        if step != prev:  # 模拟插件升级:改步骤名
            code = selftest.read_text(encoding="utf-8")
            selftest.write_text(code.replace(f'["{prev}"', f'["{step}"', 1), encoding="utf-8")
            prev = step
        with extension_session(tmp_path / "profile", headless=True) as session:
            session.start_job({"platform": "selftest", "halt_before": step, "step_pause": [0, 0]})
            assert session.next_message(timeout=10) == {"type": "halted", "step": step}


def test_debug_channel_reads_page_and_refuses_publish(tmp_path: Path, page_url: str) -> None:
    with extension_session(tmp_path / "profile", headless=True) as session:
        session.call("debug.open", {"url": page_url, "step_pause": [0, 0]})
        assert session.call("debug.eval", "document.querySelector('#btn').innerText") == "点我"
        assert session.call("debug.click", {"sel": "#btn"}) == "点我"
        with pytest.raises(CdpError, match="禁止点击「发布」"):
            session.call("debug.click", {"sel": "button", "text": "发布", "exact": True})
        with pytest.raises(CdpError, match="禁止点击「发布」"):  # 不给 text 也会在命中点拦下
            session.call("debug.click", {"sel": "button", "pick": "last"})
        state = session.call("debug.eval", "document.getElementById('log').textContent")
    assert "published" not in state


def test_click_scrolls_inside_a_clipping_list(tmp_path: Path) -> None:
    """时间滚轮式的列:目标在列里被裁掉时,要在列上滚动再点(2026-11-18 真机踩到)。"""
    items = "".join(
        f'<li onclick="document.body.dataset.picked={i}">{i:02d}</li>' for i in range(60)
    )
    (tmp_path / "list.html").write_text(
        # 同拼多多 TimePicker:平时 overflow:hidden,悬停才 overflow-y:auto
        "<!doctype html><meta charset=utf-8><style>ul{overflow:hidden} ul:hover{overflow-y:auto}"
        "</style><body style='height:3000px'>"
        f"<ul style='margin-top:300px;height:196px;width:60px'>{items}</ul></body>",
        encoding="utf-8",
    )
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with extension_session(tmp_path / "profile", headless=True) as session:
            session.call(
                "debug.open", {"url": f"http://127.0.0.1:{server.server_address[1]}/list.html"}
            )
            session.call("debug.click", {"sel": "li", "text": "45", "exact": True})
            assert session.call("debug.eval", "document.body.dataset.picked") == "45"
    finally:
        server.shutdown()


# ---- 拼多多账密登录(插件 passwordLogin 跑在仿真登录页上)----

LOGIN_PAGE = """<!doctype html><meta charset=utf-8>
<div><span>扫码登录</span> | <span onclick="document.getElementById('f').style.display='block'">账号登录</span></div>
<div id=f style="display:none">
  <input id=usernameId placeholder="请输入账号名/手机号"><input id=passwordId type=password>
  <button onclick="go()">登录</button></div>
<script>
function go() {
  const u = document.getElementById('usernameId').value, p = document.getElementById('passwordId').value;
  if (u === '13800000000' && p === 'pa$$ 中文') { location.href = '/home.html'; return; }
  const d = document.createElement('div'); d.textContent = '账号或密码错误'; document.body.appendChild(d);
}
</script>"""


def _run_login(tmp_path: Path, password: str) -> tuple[dict[str, object], str]:
    (tmp_path / "login.html").write_text(LOGIN_PAGE, encoding="utf-8")
    (tmp_path / "home.html").write_text("<meta charset=utf-8>首页", encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with extension_session(tmp_path / "profile", headless=True) as session:
            session.start_job(
                {
                    "platform": "selftest_pdd_login",
                    "url": f"http://127.0.0.1:{server.server_address[1]}/login.html",
                    "login": {"username": "13800000000", "password": password},
                    "step_pause": [0, 0],
                }
            )
            while True:
                msg = session.next_message(timeout=60)
                if msg["type"] in ("gate", "error"):
                    return msg, session.call("debug.url")
    finally:
        server.shutdown()


def test_password_login_fills_form_and_reaches_home(tmp_path: Path) -> None:
    msg, url = _run_login(tmp_path, "pa$$ 中文")
    assert msg["type"] == "gate", msg
    assert url.endswith("/home.html")


def test_password_login_failure_is_cookie_expired_with_page_hint(tmp_path: Path) -> None:
    msg, _ = _run_login(tmp_path, "wrong-secret")
    assert msg["type"] == "error"
    assert msg["kind"] == "cookie_expired"
    assert "账号或密码错误" in str(msg["message"])
    assert "wrong-secret" not in str(msg["message"])  # 密码绝不进报错
