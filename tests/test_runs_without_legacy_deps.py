"""安装包里不带 patchright(和它的 Chromium)也不带飞书 SDK:应用和拼多多(插件版)路径必须照常导入运行。

被锁定的旧平台(视频号等 patchright 实现)仍在仓库里作改版参考,只在开发环境可用。
"""

from __future__ import annotations

import subprocess
import sys

_PROBE = """
import sys
sys.modules["patchright"] = None   # 模拟安装包里没有 patchright
sys.modules["lark_oapi"] = None    # 也没有飞书 SDK
from wxsp.api.app import create_app
create_app()
from wxsp.publisher import _get_publisher
from wxsp.platforms.pinduoduo import PinduoduoPublisher
assert isinstance(_get_publisher("pinduoduo"), PinduoduoPublisher)
import wxsp.feishu, wxsp.doctor, wxsp.cli, wxsp.scheduler, wxsp.managed_chrome
from wxsp.errors import classify
assert classify(ValueError("x")) == "unknown"
print("OK")
"""


def test_app_and_pinduoduo_import_without_patchright_or_lark() -> None:
    r = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip().endswith("OK")
