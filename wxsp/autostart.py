"""跨平台开机自启注册 / 反注册 / 查询(M11)。

- macOS: launchctl + LaunchAgent plist(必须 gui/ scope,不能 system/,否则没有桌面会话)
- Windows: schtasks + Task Scheduler XML(UTF-16 LE 编码,M10 已踩过坑)

仅打包模式(.app / .exe)使用本模块。开发模式继续手动复制 deploy/wxsp.plist。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from wxsp.config import get_user_data_dir

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLIST_TEMPLATE_PATH = _REPO_ROOT / "deploy" / "wxsp.plist.tmpl"
_XML_TEMPLATE_PATH = _REPO_ROOT / "deploy" / "wxsp-task.xml.tmpl"

_TASK_NAME = "wxsp-daemon"
_LAUNCH_LABEL = "com.wxsp.daemon"


class AutostartError(RuntimeError):
    """注册 / 反注册自启失败。"""


def _user_install_dir() -> Path:
    """daemon 工作目录(也是 launchd 写日志的根)。"""
    return get_user_data_dir().parent


def _wxsp_bin() -> Path:
    """打包后 wxsp 主可执行文件绝对路径。"""
    return Path(sys.executable)


def _launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCH_LABEL}.plist"


def _render_macos_plist(*, install_dir: Path, wxsp_bin: Path) -> str:
    tpl = _PLIST_TEMPLATE_PATH.read_text(encoding="utf-8")
    return tpl.replace("__INSTALL_DIR__", str(install_dir)).replace("__WXSP_BIN__", str(wxsp_bin))


def _render_windows_xml(*, install_dir: Path, wxsp_bin: Path, username: str) -> str:
    tpl = _XML_TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        tpl.replace("__INSTALL_DIR__", str(install_dir))
        .replace("__WXSP_BIN__", str(wxsp_bin))
        .replace("__USERNAME__", username)
    )


def enable_autostart() -> None:
    """注册开机自启。失败抛 AutostartError。"""
    if sys.platform == "darwin":
        _enable_macos()
    elif sys.platform == "win32":
        _enable_windows()
    else:
        raise AutostartError(f"开机自启不支持当前平台: {sys.platform}")


def disable_autostart() -> None:
    """反注册。已不存在视为成功(idempotent)。"""
    if sys.platform == "darwin":
        _disable_macos()
    elif sys.platform == "win32":
        _disable_windows()
    else:
        raise AutostartError(f"开机自启不支持当前平台: {sys.platform}")


def is_autostart_enabled() -> bool:
    if sys.platform == "darwin":
        return _is_enabled_macos()
    if sys.platform == "win32":
        return _is_enabled_windows()
    return False


def _enable_macos() -> None:
    plist = _render_macos_plist(install_dir=_user_install_dir(), wxsp_bin=_wxsp_bin())
    plist_path = _launch_agent_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist, encoding="utf-8")

    uid = os.getuid()
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and "already" not in result.stderr.lower():
        raise AutostartError(f"launchctl bootstrap 失败: {result.stderr}")


def _disable_macos() -> None:
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{_LAUNCH_LABEL}"],
        capture_output=True,
        text=True,
    )
    plist_path = _launch_agent_path()
    if plist_path.exists():
        plist_path.unlink()


def _is_enabled_macos() -> bool:
    uid = os.getuid()
    result = subprocess.run(
        ["launchctl", "print", f"gui/{uid}/{_LAUNCH_LABEL}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _enable_windows() -> None:
    xml = _render_windows_xml(
        install_dir=_user_install_dir(),
        wxsp_bin=_wxsp_bin(),
        username=os.getlogin(),
    )
    # schtasks 在中文版 Windows 接 UTF-8 偶尔报"参数错误",UTF-16 LE 最稳
    fd, tmp_str = tempfile.mkstemp(suffix="-wxsp-task.xml")
    tmp = Path(tmp_str)
    try:
        try:
            os.write(fd, b"\xff\xfe" + xml.encode("utf-16-le"))
        finally:
            os.close(fd)

        result = subprocess.run(
            ["schtasks", "/Create", "/TN", _TASK_NAME, "/XML", str(tmp), "/F"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise AutostartError(f"schtasks /Create 失败: {result.stderr or result.stdout}")
    finally:
        try:
            os.unlink(tmp_str)
        except OSError:
            pass


def _disable_windows() -> None:
    subprocess.run(
        ["schtasks", "/Delete", "/TN", _TASK_NAME, "/F"],
        capture_output=True,
        text=True,
    )


def _is_enabled_windows() -> bool:
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", _TASK_NAME],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0
