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

_TASK_NAME = "wxsp-daemon"
_LAUNCH_LABEL = "com.wxsp.daemon"

# 模板内联为字符串常量,避免 PyInstaller 打包时丢失 deploy/*.tmpl 文件导致运行时
# FileNotFoundError。deploy/ 目录下的 .tmpl 文件保留作为人类可读参考。
_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.wxsp.daemon</string>

    <key>WorkingDirectory</key>
    <string>__INSTALL_DIR__</string>

    <key>ProgramArguments</key>
    <array>
        <string>__WXSP_BIN__</string>
        <string>run</string>
        <string>--daemon</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>30</integer>

    <key>StandardOutPath</key>
    <string>__INSTALL_DIR__/logs/launchd.out.log</string>

    <key>StandardErrorPath</key>
    <string>__INSTALL_DIR__/logs/launchd.err.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>LANG</key>
        <string>zh_CN.UTF-8</string>
    </dict>
</dict>
</plist>
"""

_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>wxsp 微信视频号自动发布 daemon</Description>
    <URI>\\wxsp-daemon</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>__USERNAME__</UserId>
      <Delay>PT30S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>__USERNAME__</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>__WXSP_BIN__</Command>
      <Arguments>run --daemon</Arguments>
      <WorkingDirectory>__INSTALL_DIR__</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


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
    return _PLIST_TEMPLATE.replace("__INSTALL_DIR__", str(install_dir)).replace(
        "__WXSP_BIN__", str(wxsp_bin)
    )


def _render_windows_xml(*, install_dir: Path, wxsp_bin: Path, username: str) -> str:
    return (
        _XML_TEMPLATE.replace("__INSTALL_DIR__", str(install_dir))
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
