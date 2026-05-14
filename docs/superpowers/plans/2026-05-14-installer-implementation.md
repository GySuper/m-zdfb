# M11 安装器 + 首次设置向导 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal**:把 wxsp 从"开发者风格 CLI 安装"换成"运营双击 .dmg / .exe → Web UI 走完 6 步向导 → 开机自启跑"的产品形态,且打包产物用 Nuitka 编译,业务代码不暴露源码。

**Architecture**:
1. **路径分层**:开发模式继续用 `./data` / `./logs` / `./config.yaml`;打包模式用 `platformdirs` 定位用户数据目录。通过 `__compiled__` 属性 + `WXSP_DEV_MODE` 环境变量切换。
2. **首次启动体验**:FastAPI startup hook 检测 config.yaml 不存在 → 进 setup 模式,所有非 `/setup/*` 路由 302 重定向。向导 6 页(欢迎自检 / 飞书 / NAS / 账号 / 告警 / 完成),最后写 yaml + 注册开机自启。
3. **跨平台自启**:`autostart.py` 模板渲染 + `subprocess` 调 `launchctl` / `schtasks`。模板文件 `deploy/wxsp.plist.tmpl` / `wxsp-task.xml.tmpl` 是打包模式专用(命令结构 `<wxsp_bin> run --daemon`,3 段,无 uv);现有 `deploy/wxsp.plist` / `wxsp-task.xml` 保留给开发模式手动注册。
4. **打包**:`wxsp/__main__.py` 入口 + Nuitka standalone → mac `.app` 拖进 `.dmg` / win `.exe` 由 Inno Setup 装成 setup 包;GitHub Actions(macos-latest + windows-latest)tag 触发出 release。

**Tech Stack**:Nuitka 2.x、`platformdirs` 4.x、`create-dmg`(brew)、Inno Setup 6、GitHub Actions、现有 FastAPI / Jinja2 / HTMX、`patchright` 内嵌 Chromium。

**Design ref**:[docs/superpowers/specs/2026-05-14-installer-design.md](../specs/2026-05-14-installer-design.md)

---

## 文件结构概览

**新增**:
- `wxsp/__main__.py` — Nuitka 入口
- `wxsp/autostart.py` — 跨平台自启注册/反注册
- `wxsp/api/routes_setup.py` — 向导后端
- `wxsp/templates/setup/{base,welcome,feishu,nas,accounts,notify,complete}.html` — 6 页向导
- `deploy/wxsp.plist.tmpl` — 打包模式 mac launchd 模板(3 段 ProgramArguments)
- `deploy/wxsp-task.xml.tmpl` — 打包模式 Windows 任务计划程序模板
- `scripts/build_macos.sh` — Nuitka + create-dmg
- `scripts/build_windows.ps1` — Nuitka + Inno Setup
- `scripts/setup.iss` — Inno Setup 脚本
- `.github/workflows/build.yml` — CI 出 release
- `tests/test_paths.py` / `test_autostart.py` / `test_routes_setup.py`

**修改**:
- `wxsp/config.py` — 加 `get_user_data_dir()` / `get_config_path()` 辅助 + 默认值逻辑
- `wxsp/api/app.py` — startup hook + setup 模式中间件 + 挂 setup 路由
- `wxsp/api/routes_config.py` — `_CONFIG_PATH` 改成调用 `get_config_path()`
- `wxsp/browser.py` — 打包模式设置 `PLAYWRIGHT_BROWSERS_PATH`
- `pyproject.toml` — `platformdirs>=4.0.0`
- `README.md` — 安装章节重写

**保留**(打开后不动):
- `wxsp/publisher.py` / `selectors.py` / `feishu.py` / `nas.py` / `scheduler.py`
- 现有 `deploy/wxsp.plist` / `deploy/wxsp-task.xml`(开发模式手动注册用)

---

## Task 1: platformdirs 接入 + 开发/打包模式检测

**Files:**
- Modify: `pyproject.toml`
- Modify: `wxsp/config.py`(新增辅助函数 + `load_settings` 默认路径切换)
- Modify: `wxsp/api/routes_config.py:32-33`(用 `get_config_path()` 替换硬编码)
- Test: `tests/test_paths.py`(新建)

- [ ] **Step 1: 加 platformdirs 依赖**

修改 `pyproject.toml` 第 20 行附近,在 `dependencies` 列表末尾添加(`"python-multipart>=0.0.9",` 后):

```toml
    "platformdirs>=4.0.0",
```

然后跑:

```bash
uv sync
```

预期:成功安装 platformdirs,`uv.lock` 更新。

- [ ] **Step 2: 写失败测试 — 路径解析**

新建 `tests/test_paths.py`:

```python
"""测试 config.py 的路径解析:打包模式 vs 开发模式。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from wxsp.config import (
    get_config_path,
    get_user_data_dir,
    get_user_logs_dir,
    is_packaged,
)


def test_is_packaged_default_dev_mode() -> None:
    """普通 pytest 运行下,__main__ 没有 __compiled__ 属性 → 开发模式。"""
    # 默认 pytest 运行环境就是开发模式
    assert is_packaged() is False


def test_is_packaged_when_nuitka_compiled() -> None:
    """Nuitka 会给 sys.modules['__main__'] 注入 __compiled__ → True。"""
    main_module = sys.modules["__main__"]
    with patch.object(main_module, "__compiled__", True, create=True):
        assert is_packaged() is True


def test_is_packaged_force_dev_via_env(monkeypatch) -> None:
    """WXSP_DEV_MODE=1 强制开发模式,即使被 Nuitka 编译。"""
    main_module = sys.modules["__main__"]
    monkeypatch.setenv("WXSP_DEV_MODE", "1")
    with patch.object(main_module, "__compiled__", True, create=True):
        assert is_packaged() is False


def test_user_data_dir_dev_mode() -> None:
    """开发模式下 user_data_dir 返回项目根的 ./data。"""
    assert get_user_data_dir() == Path("./data").resolve()


def test_user_data_dir_packaged_uses_platformdirs(monkeypatch) -> None:
    """打包模式走 platformdirs(mac: ~/Library/Application Support/wxsp)。"""
    main_module = sys.modules["__main__"]
    with patch.object(main_module, "__compiled__", True, create=True):
        monkeypatch.delenv("WXSP_DEV_MODE", raising=False)
        result = get_user_data_dir()
        # 不写死具体路径,验证落到 platformdirs 算出来的某个绝对路径
        assert result.is_absolute()
        assert result.name == "data"
        assert result.parent.name == "wxsp"


def test_user_logs_dir_dev_mode() -> None:
    assert get_user_logs_dir() == Path("./logs").resolve()


def test_config_path_dev_mode() -> None:
    """开发模式 config.yaml 在项目根。"""
    assert get_config_path() == Path("./config.yaml").resolve()


def test_config_path_packaged(monkeypatch) -> None:
    """打包模式 config.yaml 在用户数据目录(get_user_data_dir().parent)。"""
    main_module = sys.modules["__main__"]
    with patch.object(main_module, "__compiled__", True, create=True):
        monkeypatch.delenv("WXSP_DEV_MODE", raising=False)
        config_path = get_config_path()
        data_dir = get_user_data_dir()
        # config.yaml 和 data/ 同级,都在 user_data_dir("wxsp") 这一层
        assert config_path.parent == data_dir.parent
        assert config_path.name == "config.yaml"
```

- [ ] **Step 3: 跑测试,看到全失败**

```bash
uv run pytest tests/test_paths.py -v
```

预期:**全部失败**,`ImportError: cannot import name 'get_config_path' from 'wxsp.config'`。

- [ ] **Step 4: 实现 config.py 的路径辅助函数**

在 `wxsp/config.py` 顶部 import 区下方(`from pydantic import ...` 之后)插入:

```python
import sys

from platformdirs import user_data_dir as _platform_user_data_dir
from platformdirs import user_log_dir as _platform_user_log_dir


def is_packaged() -> bool:
    """判断当前是否运行在 Nuitka 编译的二进制里。

    Nuitka 会给 sys.modules['__main__'] 注入 __compiled__ 属性。
    WXSP_DEV_MODE=1 可以强制走开发模式(用于在打包产物里本地调试)。
    """
    if os.environ.get("WXSP_DEV_MODE") == "1":
        return False
    return hasattr(sys.modules.get("__main__"), "__compiled__")


def get_user_data_dir() -> Path:
    """返回 data/ 目录绝对路径。

    打包模式:平台规范位置 / wxsp / data
        mac: ~/Library/Application Support/wxsp/data
        win: %APPDATA%\\wxsp\\data
    开发模式:项目根 ./data
    """
    if is_packaged():
        return Path(_platform_user_data_dir("wxsp")) / "data"
    return Path("./data").resolve()


def get_user_logs_dir() -> Path:
    """返回 logs/ 目录绝对路径。打包模式走 platformdirs,开发模式 ./logs。"""
    if is_packaged():
        return Path(_platform_user_log_dir("wxsp"))
    return Path("./logs").resolve()


def get_config_path() -> Path:
    """返回 config.yaml 的绝对路径。

    打包模式:user_data_dir("wxsp")/config.yaml(与 data/ 同级)
    开发模式:./config.yaml
    """
    if is_packaged():
        return Path(_platform_user_data_dir("wxsp")) / "config.yaml"
    return Path("./config.yaml").resolve()
```

注意:`os` 已经在文件顶部 import 过,不需要重复。

- [ ] **Step 5: 跑测试,看到全过**

```bash
uv run pytest tests/test_paths.py -v
```

预期:**8 个测试全过**。

- [ ] **Step 6: 让 load_settings 用新路径**

修改 `wxsp/config.py` 的 `load_settings`:把签名第 1 行改成 fallback 到 `get_config_path()`,改后第 148-153 行附近:

```python
def load_settings(config_path: Path | None = None) -> Settings:
    """Load and validate config.yaml; expand ${ENV_VAR} and {nas_root}."""
    if config_path is None:
        config_path = get_config_path()
    if not config_path.exists():
        raise FileNotFoundError(f"找不到配置文件: {config_path}")
    ...
```

- [ ] **Step 7: 改 routes_config.py 用 get_config_path**

修改 `wxsp/api/routes_config.py` 第 28、32-33 行附近:

把:

```python
from wxsp.config import Settings, _expand_env_vars

router = APIRouter()

_CONFIG_PATH = Path("config.yaml")
_BACKUP_PATH = Path("config.yaml.bak")
```

改成:

```python
from wxsp.config import Settings, _expand_env_vars, get_config_path

router = APIRouter()


def _config_path() -> Path:
    return get_config_path()


def _backup_path() -> Path:
    return get_config_path().with_suffix(".yaml.bak")
```

然后在文件里所有引用 `_CONFIG_PATH` / `_BACKUP_PATH` 的地方改成 `_config_path()` / `_backup_path()` 调用:

```bash
grep -n "_CONFIG_PATH\|_BACKUP_PATH" wxsp/api/routes_config.py
```

把每一处替换成函数调用。如果原代码是 `_CONFIG_PATH.read_text(...)` 就改成 `_config_path().read_text(...)`,以此类推。

- [ ] **Step 8: 跑全量测试确认没破坏**

```bash
uv run pytest -m "not integration" -q
```

预期:全部 PASS。如果 `test_api_plans_config.py` 之类有 `Path("config.yaml")` 写死的 fixture,改成 monkeypatch `get_config_path` 返回 `tmp_path / "config.yaml"`。

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock wxsp/config.py wxsp/api/routes_config.py tests/test_paths.py
git commit -m "feat(config): platformdirs 接入 + is_packaged 检测(M11.1)"
```

---

## Task 2: Chromium 路径在打包模式定向到 app bundle

**Files:**
- Modify: `wxsp/browser.py:34-60`(`browser_context` 函数前加路径解析)
- Test: `tests/test_browser.py`(已存在,追加 case)

- [ ] **Step 1: 写失败测试**

在 `tests/test_browser.py` 文件末尾追加:

```python
import os
import sys
from pathlib import Path
from unittest.mock import patch


def test_chromium_root_dev_mode_returns_none(monkeypatch) -> None:
    """开发模式不动 PLAYWRIGHT_BROWSERS_PATH,patchright 走默认查找。"""
    from wxsp.browser import _chromium_root

    monkeypatch.delenv("WXSP_DEV_MODE", raising=False)
    # 默认 pytest 不是 Nuitka 编译,is_packaged() False
    assert _chromium_root() is None


def test_chromium_root_packaged_mac(monkeypatch) -> None:
    """打包模式 mac:返回 sys.executable 上溯 2 级 + Resources/chromium。"""
    from wxsp import browser

    main_module = sys.modules["__main__"]
    fake_exec = Path("/Applications/wxsp.app/Contents/MacOS/wxsp")
    with patch.object(main_module, "__compiled__", True, create=True):
        monkeypatch.delenv("WXSP_DEV_MODE", raising=False)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(sys, "executable", str(fake_exec))
        result = browser._chromium_root()
        assert result == Path("/Applications/wxsp.app/Contents/Resources/chromium")


def test_chromium_root_packaged_windows(monkeypatch) -> None:
    """打包模式 win:返回 sys.executable 同级 chromium 目录。"""
    from wxsp import browser

    main_module = sys.modules["__main__"]
    fake_exec = Path("C:/Program Files/wxsp/wxsp.exe")
    with patch.object(main_module, "__compiled__", True, create=True):
        monkeypatch.delenv("WXSP_DEV_MODE", raising=False)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(sys, "executable", str(fake_exec))
        result = browser._chromium_root()
        assert result == Path("C:/Program Files/wxsp/chromium")
```

- [ ] **Step 2: 跑测试,看到失败**

```bash
uv run pytest tests/test_browser.py -v -k chromium_root
```

预期:**3 个测试失败**,`AttributeError: module 'wxsp.browser' has no attribute '_chromium_root'`。

- [ ] **Step 3: 实现 `_chromium_root`**

修改 `wxsp/browser.py`,在 import 区域(第 17-23 行附近)加 sys:

```python
import os
import sys
```

然后在 `WECHAT_CHANNELS_HOME = ...` 行之前加:

```python
from wxsp.config import is_packaged


def _chromium_root() -> Path | None:
    """打包模式返回内嵌 chromium 目录;开发模式返回 None 让 patchright 自己找。"""
    if not is_packaged():
        return None
    exe = Path(sys.executable)
    if sys.platform == "darwin":
        # /Applications/wxsp.app/Contents/MacOS/wxsp → ../Resources/chromium
        return exe.parent.parent / "Resources" / "chromium"
    return exe.parent / "chromium"
```

- [ ] **Step 4: 在 browser_context 里应用 PLAYWRIGHT_BROWSERS_PATH**

修改 `wxsp/browser.py` 的 `browser_context` 函数(第 34-60 行附近),在 `user_data_dir.mkdir(...)` 之前插入:

```python
    chromium_root = _chromium_root()
    if chromium_root is not None:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(chromium_root)
```

完整改动后函数体首部:

```python
@contextmanager
def browser_context(
    user_data_dir: Path,
    *,
    headless: bool = False,
) -> Iterator[Page]:
    """..."""  # docstring 不动
    chromium_root = _chromium_root()
    if chromium_root is not None:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(chromium_root)
    user_data_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        # ... 原逻辑不变
```

- [ ] **Step 5: 跑测试,看到通过**

```bash
uv run pytest tests/test_browser.py -v -k chromium_root
```

预期:**3 个测试通过**。

- [ ] **Step 6: 跑 browser.py 的全量测试**

```bash
uv run pytest tests/test_browser.py -v -m "not integration"
```

预期:全部 PASS(已有非 integration 测试不应该被破坏)。

- [ ] **Step 7: Commit**

```bash
git add wxsp/browser.py tests/test_browser.py
git commit -m "feat(browser): 打包模式定向 chromium 到 app bundle 内嵌目录(M11.1)"
```

---

## Task 3: autostart.py + 打包模式专用模板

**Files:**
- Create: `deploy/wxsp.plist.tmpl`
- Create: `deploy/wxsp-task.xml.tmpl`
- Create: `wxsp/autostart.py`
- Test: `tests/test_autostart.py`(新建)

- [ ] **Step 1: 写打包模式 mac 模板**

新建 `deploy/wxsp.plist.tmpl`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!--
  wxsp 打包模式 launchd 模板(M11)。由 wxsp/autostart.py 自动渲染。
  与开发模式的 deploy/wxsp.plist 区别:ProgramArguments 是 3 段 (<wxsp_bin> run --daemon),
  没有 uv 前缀。__INSTALL_DIR__ 指用户数据目录(写日志用)。
-->
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
```

- [ ] **Step 2: 写打包模式 win 模板**

新建 `deploy/wxsp-task.xml.tmpl`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!--
  wxsp 打包模式任务计划程序模板(M11)。由 wxsp/autostart.py 自动渲染。
  与开发模式的 deploy/wxsp-task.xml 区别:<Arguments> 只有 'run --daemon'(没 uv 前缀)。
-->
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>wxsp 微信视频号自动发布 daemon</Description>
    <URI>\wxsp-daemon</URI>
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
```

- [ ] **Step 3: 写失败测试**

新建 `tests/test_autostart.py`:

```python
"""测试 autostart.py:模板渲染 + 平台 dispatch + subprocess 调用(mock)。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_render_macos_plist(tmp_path: Path) -> None:
    from wxsp.autostart import _render_macos_plist

    rendered = _render_macos_plist(
        install_dir=tmp_path,
        wxsp_bin=Path("/Applications/wxsp.app/Contents/MacOS/wxsp"),
    )
    assert "__INSTALL_DIR__" not in rendered
    assert "__WXSP_BIN__" not in rendered
    assert str(tmp_path) in rendered
    assert "/Applications/wxsp.app/Contents/MacOS/wxsp" in rendered
    # 关键:3 段 ProgramArguments,不是 5 段
    assert rendered.count("<string>") >= 5  # bin + run + --daemon + 其它 string 字段


def test_render_windows_xml(tmp_path: Path) -> None:
    from wxsp.autostart import _render_windows_xml

    rendered = _render_windows_xml(
        install_dir=tmp_path,
        wxsp_bin=Path("C:/Program Files/wxsp/wxsp.exe"),
        username="alice",
    )
    assert "__INSTALL_DIR__" not in rendered
    assert "__WXSP_BIN__" not in rendered
    assert "__USERNAME__" not in rendered
    assert str(tmp_path) in rendered
    assert "alice" in rendered
    assert "<Arguments>run --daemon</Arguments>" in rendered


def test_enable_autostart_macos_calls_launchctl(tmp_path, monkeypatch) -> None:
    from wxsp import autostart

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(autostart, "_user_install_dir", lambda: tmp_path)
    monkeypatch.setattr(autostart, "_wxsp_bin", lambda: Path("/fake/wxsp"))
    monkeypatch.setattr(
        autostart, "_launch_agent_path", lambda: tmp_path / "com.wxsp.daemon.plist"
    )

    fake_uid = 501
    monkeypatch.setattr("os.getuid", lambda: fake_uid, raising=False)
    mock_run = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(subprocess, "run", mock_run)

    autostart.enable_autostart()

    plist_path = tmp_path / "com.wxsp.daemon.plist"
    assert plist_path.exists()
    assert "/fake/wxsp" in plist_path.read_text()
    mock_run.assert_called_once()
    call_args = mock_run.call_args[0][0]
    assert call_args[0] == "launchctl"
    assert call_args[1] == "bootstrap"
    assert f"gui/{fake_uid}" in call_args[2]


def test_enable_autostart_windows_calls_schtasks(tmp_path, monkeypatch) -> None:
    from wxsp import autostart

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(autostart, "_user_install_dir", lambda: tmp_path)
    monkeypatch.setattr(autostart, "_wxsp_bin", lambda: Path("C:/wxsp/wxsp.exe"))
    monkeypatch.setattr("os.getlogin", lambda: "alice")

    mock_run = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(subprocess, "run", mock_run)

    autostart.enable_autostart()

    # subprocess 至少被调用一次 schtasks /Create
    assert mock_run.called
    create_calls = [
        c for c in mock_run.call_args_list if "schtasks" in str(c.args[0][0]).lower()
    ]
    assert len(create_calls) >= 1
    cmd = create_calls[0].args[0]
    assert "/Create" in cmd
    assert "wxsp-daemon" in cmd


def test_disable_autostart_macos(tmp_path, monkeypatch) -> None:
    from wxsp import autostart

    monkeypatch.setattr(sys, "platform", "darwin")
    plist_path = tmp_path / "com.wxsp.daemon.plist"
    plist_path.write_text("<plist/>")
    monkeypatch.setattr(autostart, "_launch_agent_path", lambda: plist_path)
    monkeypatch.setattr("os.getuid", lambda: 501, raising=False)
    monkeypatch.setattr(subprocess, "run", MagicMock(return_value=subprocess.CompletedProcess([], 0)))

    autostart.disable_autostart()

    assert not plist_path.exists()


def test_is_autostart_enabled_mac_query(monkeypatch) -> None:
    from wxsp import autostart

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("os.getuid", lambda: 501, raising=False)
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(return_value=subprocess.CompletedProcess([], 0)),
    )
    assert autostart.is_autostart_enabled() is True

    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(return_value=subprocess.CompletedProcess([], 1)),
    )
    assert autostart.is_autostart_enabled() is False


def test_enable_autostart_unsupported_platform(monkeypatch) -> None:
    from wxsp import autostart

    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(autostart.AutostartError, match="不支持"):
        autostart.enable_autostart()
```

- [ ] **Step 4: 跑测试,看到失败**

```bash
uv run pytest tests/test_autostart.py -v
```

预期:全失败,`ModuleNotFoundError: No module named 'wxsp.autostart'`。

- [ ] **Step 5: 实现 autostart.py**

新建 `wxsp/autostart.py`:

```python
"""跨平台开机自启注册 / 反注册 / 查询(M11)。

- macOS: launchctl + LaunchAgent plist(必须 gui/ scope,不能 system/,否则没有桌面会话)
- Windows: schtasks + Task Scheduler XML(UTF-16 LE 编码,M10 已踩过坑)

仅打包模式(.app / .exe)使用本模块。开发模式继续手动复制 deploy/wxsp.plist。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from wxsp.config import get_user_data_dir

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLIST_TEMPLATE_PATH = _REPO_ROOT / "deploy" / "wxsp.plist.tmpl"
_XML_TEMPLATE_PATH = _REPO_ROOT / "deploy" / "wxsp-task.xml.tmpl"

_TASK_NAME = "wxsp-daemon"
_LAUNCH_LABEL = "com.wxsp.daemon"


class AutostartError(RuntimeError):
    """注册 / 反注册自启失败。"""


# ---------- 内部辅助:路径解析 ----------

def _user_install_dir() -> Path:
    """daemon 工作目录(也是 launchd 写日志的根)。"""
    return get_user_data_dir().parent  # user_data_dir 是 .../wxsp/data,parent 是 .../wxsp


def _wxsp_bin() -> Path:
    """打包后 wxsp 主可执行文件绝对路径。"""
    return Path(sys.executable)


def _launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCH_LABEL}.plist"


# ---------- 模板渲染(纯字符串替换,易测) ----------

def _render_macos_plist(*, install_dir: Path, wxsp_bin: Path) -> str:
    tpl = _PLIST_TEMPLATE_PATH.read_text(encoding="utf-8")
    return tpl.replace("__INSTALL_DIR__", str(install_dir)).replace(
        "__WXSP_BIN__", str(wxsp_bin)
    )


def _render_windows_xml(*, install_dir: Path, wxsp_bin: Path, username: str) -> str:
    tpl = _XML_TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        tpl.replace("__INSTALL_DIR__", str(install_dir))
        .replace("__WXSP_BIN__", str(wxsp_bin))
        .replace("__USERNAME__", username)
    )


# ---------- 平台 dispatch ----------

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


# ---------- macOS 实现 ----------

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
    )  # 已不存在也无所谓
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


# ---------- Windows 实现 ----------

def _enable_windows() -> None:
    xml = _render_windows_xml(
        install_dir=_user_install_dir(),
        wxsp_bin=_wxsp_bin(),
        username=os.getlogin(),
    )
    # M10 踩坑:schtasks 在中文版 Windows 接 UTF-8 偶尔报"参数错误",UTF-16 LE 最稳
    tmp = Path(os.environ.get("TEMP", "C:/Windows/Temp")) / "wxsp-task.xml"
    tmp.write_bytes(b"\xff\xfe" + xml.encode("utf-16-le"))

    result = subprocess.run(
        ["schtasks", "/Create", "/TN", _TASK_NAME, "/XML", str(tmp), "/F"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AutostartError(f"schtasks /Create 失败: {result.stderr or result.stdout}")


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
```

- [ ] **Step 6: 跑测试,看到通过**

```bash
uv run pytest tests/test_autostart.py -v
```

预期:**7 个测试全过**。

- [ ] **Step 7: Commit**

```bash
git add deploy/wxsp.plist.tmpl deploy/wxsp-task.xml.tmpl wxsp/autostart.py tests/test_autostart.py
git commit -m "feat(autostart): 跨平台 launchctl/schtasks 自启注册(M11.1)"
```

---

## Task 4: wxsp/__main__.py 入口 + run --daemon 在打包模式同时起 FastAPI

**Files:**
- Create: `wxsp/__main__.py`
- Modify: `wxsp/cli.py:325-331`(`run --daemon` 分支,在打包模式同时启动 FastAPI)

**Why**:spec §2.1 要求 launchctl/schtasks 起的 `wxsp run --daemon` 同时跑 cron + FastAPI :8765。当前 `start_daemon()` 只跑 `BlockingScheduler`,所以浏览器会连不上。开发模式不影响(开发者继续两进程 `run --daemon` + `web`)。

- [ ] **Step 1: 写入口**

新建 `wxsp/__main__.py`:

```python
"""Nuitka 编译入口。等价于 `python -m wxsp`。

打包后 sys.modules['__main__'] 就是这个模块,Nuitka 会注入 __compiled__ = True,
config.is_packaged() 据此判断走 platformdirs 路径。
"""

from __future__ import annotations

from wxsp.cli import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 验证开发模式 entry point 还工作**

```bash
uv run python -m wxsp --help
```

预期:打印 typer 的 help,不报错。

- [ ] **Step 3: 修改 cli.py 的 run --daemon 分支,打包模式同时起 FastAPI**

修改 `wxsp/cli.py` 第 325-331 行附近(`if daemon:` 块),改成:

```python
    if daemon:
        from wxsp.config import is_packaged

        if is_packaged():
            # 打包模式:在后台线程起 uvicorn,主线程跑 BlockingScheduler
            import threading

            import uvicorn

            def _serve_web() -> None:
                uvicorn.run(
                    "wxsp.api.app:app",
                    host=settings.webui.host,
                    port=settings.webui.port,
                    log_level="info",
                )

            threading.Thread(target=_serve_web, daemon=True, name="web-ui").start()
            # 给 uvicorn 起 1 秒,再开浏览器
            if settings.webui.open_browser_on_start:
                import time
                import webbrowser

                def _open() -> None:
                    time.sleep(1.0)
                    try:
                        webbrowser.open(f"http://{settings.webui.host}:{settings.webui.port}/")
                    except Exception:
                        pass

                threading.Thread(target=_open, daemon=True, name="open-browser").start()

        typer.echo("[wxsp] 启动 daemon(按 Ctrl-C 退出)...")
        try:
            start_daemon(settings)
        except (KeyboardInterrupt, SystemExit):
            typer.echo("[wxsp] daemon 退出")
        return
```

注意:**开发模式分支不变**,开发者继续手动开两进程。

- [ ] **Step 4: 写测试覆盖打包模式分支**

在 `tests/test_cli_run.py` 末尾追加:

```python
def test_run_daemon_in_packaged_mode_starts_web(monkeypatch) -> None:
    """打包模式下 `wxsp run --daemon` 应该额外开一个 uvicorn 线程。"""
    import sys
    from unittest.mock import MagicMock, patch

    from typer.testing import CliRunner

    main_module = sys.modules["__main__"]
    started_threads: list[str] = []

    real_thread = __import__("threading").Thread

    def fake_thread(*args, **kwargs):
        started_threads.append(kwargs.get("name", "unnamed"))
        t = real_thread(*args, **kwargs)
        return t

    monkeypatch.setattr("threading.Thread", fake_thread)
    monkeypatch.setattr("wxsp.scheduler.start_daemon", lambda s: None)
    monkeypatch.setattr("wxsp.cli.load_settings", lambda: MagicMock(
        webui=MagicMock(host="127.0.0.1", port=8765, open_browser_on_start=False),
    ))

    with patch.object(main_module, "__compiled__", True, create=True):
        monkeypatch.delenv("WXSP_DEV_MODE", raising=False)
        from wxsp.cli import app as cli_app

        runner = CliRunner()
        result = runner.invoke(cli_app, ["run", "--daemon"])
        assert result.exit_code == 0
        assert "web-ui" in started_threads


def test_run_daemon_in_dev_mode_does_not_start_web(monkeypatch) -> None:
    """开发模式下 `wxsp run --daemon` 不应该开 uvicorn 线程。"""
    from unittest.mock import MagicMock

    from typer.testing import CliRunner

    started_threads: list[str] = []
    real_thread = __import__("threading").Thread

    def fake_thread(*args, **kwargs):
        started_threads.append(kwargs.get("name", "unnamed"))
        return real_thread(*args, **kwargs)

    monkeypatch.setattr("threading.Thread", fake_thread)
    monkeypatch.setattr("wxsp.scheduler.start_daemon", lambda s: None)
    monkeypatch.setattr("wxsp.cli.load_settings", lambda: MagicMock(
        webui=MagicMock(host="127.0.0.1", port=8765, open_browser_on_start=True),
    ))
    monkeypatch.delenv("WXSP_DEV_MODE", raising=False)
    # 默认 pytest 不是 packaged,不需要 patch __compiled__

    from wxsp.cli import app as cli_app

    runner = CliRunner()
    result = runner.invoke(cli_app, ["run", "--daemon"])
    assert result.exit_code == 0
    assert "web-ui" not in started_threads
```

- [ ] **Step 5: 跑测试**

```bash
uv run pytest tests/test_cli_run.py -v -k daemon
```

预期:2 个新测试 PASS,已有 `run --daemon` 测试不破。

- [ ] **Step 6: Commit**

```bash
git add wxsp/__main__.py wxsp/cli.py tests/test_cli_run.py
git commit -m "feat(cli): __main__ 入口 + 打包模式 run --daemon 同时起 FastAPI(M11.1)"
```

---

## Task 5: Setup 模式 startup hook + 重定向中间件

**Files:**
- Modify: `wxsp/api/app.py`(加 startup hook + 重定向)
- Modify: `wxsp/api/deps.py`(暴露 setup_mode 状态;若没有该字段则用 app.state)

- [ ] **Step 1: 写失败测试**

新建 `tests/test_setup_mode.py`:

```python
"""测试 setup 模式:config.yaml 不存在时,非 /setup 路由 302 → /setup/step/1。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_in_setup_mode(monkeypatch, tmp_path):
    """构造 config.yaml 不存在的环境,加载 fastapi app。"""
    monkeypatch.setattr("wxsp.config.get_config_path", lambda: tmp_path / "config.yaml")
    # 强制重新构造 app(import side effect)
    import importlib

    import wxsp.api.app as app_module

    importlib.reload(app_module)
    return app_module.app


def test_root_redirects_to_setup_when_no_config(app_in_setup_mode):
    client = TestClient(app_in_setup_mode, follow_redirects=False)
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/setup/step/1"


def test_accounts_redirects_to_setup_when_no_config(app_in_setup_mode):
    client = TestClient(app_in_setup_mode, follow_redirects=False)
    resp = client.get("/accounts")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/setup/step/1"


def test_setup_route_not_redirected(app_in_setup_mode):
    """/setup/* 本身不应该被重定向(否则死循环)。"""
    client = TestClient(app_in_setup_mode, follow_redirects=False)
    resp = client.get("/setup/step/1")
    # 此时 routes_setup 还没接入,200 / 404 都可以,但不应是 302 自指
    assert resp.headers.get("location") != "/setup/step/1"


def test_static_assets_not_redirected(app_in_setup_mode):
    """静态资源不重定向,否则 HTMX 拉不到。"""
    client = TestClient(app_in_setup_mode, follow_redirects=False)
    resp = client.get("/static/anything.css")
    # 不存在返 404,但不能 302 到 /setup
    assert resp.status_code != 302
```

- [ ] **Step 2: 跑测试,看到失败**

```bash
uv run pytest tests/test_setup_mode.py -v
```

预期:全部失败,`/` 应该 302 但实际 200 / 500。

- [ ] **Step 3: 改 app.py 加 setup 模式**

修改 `wxsp/api/app.py`:

```python
"""FastAPI 入口:挂载路由 + 模板。

不做用户登录(本地单用户),不做 CORS(单进程同源)。
首次启动 config.yaml 不存在时进入 setup 模式,所有非 /setup 路由 302 重定向。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from wxsp.api.log_stream import log_stream
from wxsp.api.routes_accounts import router as accounts_router
from wxsp.api.routes_config import router as config_router
from wxsp.api.routes_dashboard import router as dashboard_router
from wxsp.api.routes_logs import router as logs_router
from wxsp.api.routes_plans import router as plans_router
from wxsp.api.routes_tasks import router as tasks_router
from wxsp.config import get_config_path

_SETUP_PREFIX = "/setup"
_STATIC_PREFIX = "/static"


def _setup_required() -> bool:
    """config.yaml 不存在 → 需要走向导。"""
    return not get_config_path().exists()


def create_app() -> FastAPI:
    app = FastAPI(title="wxsp Web UI", docs_url=None, redoc_url=None)
    log_stream.attach_to_loguru()

    @app.middleware("http")
    async def setup_redirect(request: Request, call_next):
        path = request.url.path
        if (
            _setup_required()
            and not path.startswith(_SETUP_PREFIX)
            and not path.startswith(_STATIC_PREFIX)
        ):
            return RedirectResponse(url="/setup/step/1", status_code=302)
        return await call_next(request)

    app.include_router(dashboard_router)
    app.include_router(accounts_router)
    app.include_router(tasks_router)
    app.include_router(plans_router)
    app.include_router(config_router)
    app.include_router(logs_router)
    # routes_setup 在下一个 Task 中接入
    return app


app = create_app()
```

- [ ] **Step 4: 跑测试,部分通过**

```bash
uv run pytest tests/test_setup_mode.py -v
```

预期:
- `test_root_redirects_to_setup_when_no_config` PASS
- `test_accounts_redirects_to_setup_when_no_config` PASS
- `test_setup_route_not_redirected` PASS(404 不是 302)
- `test_static_assets_not_redirected` PASS

- [ ] **Step 5: 确认没破坏现有 web 测试**

```bash
uv run pytest tests/test_api_dashboard.py tests/test_api_accounts.py -v
```

预期:已有 test 应该都过 —— 它们 fixture 里建了真 config.yaml,setup mode 不会触发。

如果有失败:检查 fixture 是否设置了 `monkeypatch.setattr("wxsp.config.get_config_path", lambda: ...)` 指向一个**存在**的 config.yaml。

- [ ] **Step 6: Commit**

```bash
git add wxsp/api/app.py tests/test_setup_mode.py
git commit -m "feat(api): config.yaml 缺失时进入 setup 模式重定向(M11.2)"
```

---

## Task 6: routes_setup.py — 向导后端 6 个端点

**Files:**
- Create: `wxsp/api/routes_setup.py`
- Modify: `wxsp/api/app.py`(挂载 setup_router)
- Test: `tests/test_routes_setup.py`(新建)

- [ ] **Step 1: 写失败测试 — 向导每步 GET/POST + 完成写盘**

新建 `tests/test_routes_setup.py`:

```python
"""测试 routes_setup.py:6 步向导 + 校验 + 最终写 yaml。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient


@pytest.fixture
def fresh_app(monkeypatch, tmp_path):
    """干净环境:config.yaml 不存在,user_data_dir 在 tmp_path。"""
    config_path = tmp_path / "config.yaml"
    user_data = tmp_path / "data"
    user_data.mkdir()
    monkeypatch.setattr("wxsp.config.get_config_path", lambda: config_path)
    monkeypatch.setattr("wxsp.config.get_user_data_dir", lambda: user_data)
    monkeypatch.setattr("wxsp.config.get_user_logs_dir", lambda: tmp_path / "logs")
    import importlib

    import wxsp.api.app as app_module

    importlib.reload(app_module)
    return app_module.app, config_path


def test_step1_welcome_renders(fresh_app):
    app, _ = fresh_app
    client = TestClient(app)
    resp = client.get("/setup/step/1")
    assert resp.status_code == 200
    # 至少标题里有"欢迎"
    assert "欢迎" in resp.text or "wxsp" in resp.text


def test_step2_post_stores_feishu_and_advances(fresh_app):
    app, _ = fresh_app
    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        "/setup/step/2",
        data={
            "app_id": "cli_test_app",
            "app_secret": "secret_test",
            "app_token": "bascntest",
            "table_id": "tbltest",
        },
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/setup/step/3"


def test_step3_post_stores_nas_and_advances(fresh_app, tmp_path):
    app, _ = fresh_app
    client = TestClient(app, follow_redirects=False)
    nas_dir = tmp_path / "nas"
    nas_dir.mkdir()
    resp = client.post("/setup/step/3", data={"nas_root": str(nas_dir)})
    assert resp.status_code == 302
    assert resp.headers["location"] == "/setup/step/4"


def test_step4_post_validates_accounts(fresh_app):
    app, _ = fresh_app
    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        "/setup/step/4",
        data={
            "account_id[]": ["account_a", "account_b"],
            "display_name[]": ["美食号", "健身号"],
            "daily_limit[]": ["20", "20"],
        },
    )
    assert resp.status_code == 302


def test_step4_rejects_invalid_account_id(fresh_app):
    app, _ = fresh_app
    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        "/setup/step/4",
        data={
            "account_id[]": ["Account-A"],  # 大写 + 连字符 都非法
            "display_name[]": ["x"],
            "daily_limit[]": ["20"],
        },
    )
    assert resp.status_code == 200  # 渲染错误页,不 302
    assert "account_id" in resp.text.lower() or "格式" in resp.text


def test_complete_writes_config_yaml(fresh_app, tmp_path, monkeypatch):
    """整套 happy path 走完,POST /setup/complete 后 config.yaml 落盘。"""
    app, config_path = fresh_app
    # mock autostart,测试不真调 launchctl
    monkeypatch.setattr("wxsp.autostart.enable_autostart", lambda: None)
    client = TestClient(app, follow_redirects=False)

    # 走 step 2-5
    client.post(
        "/setup/step/2",
        data={
            "app_id": "cli_x",
            "app_secret": "s",
            "app_token": "bx",
            "table_id": "tbl1",
        },
    )
    nas_dir = tmp_path / "nas"
    nas_dir.mkdir()
    client.post("/setup/step/3", data={"nas_root": str(nas_dir)})
    client.post(
        "/setup/step/4",
        data={
            "account_id[]": ["account_a"],
            "display_name[]": ["美食号"],
            "daily_limit[]": ["20"],
        },
    )
    client.post("/setup/step/5", data={"webhook": ""})

    resp = client.post("/setup/complete", data={"enable_autostart": "on"})
    assert resp.status_code == 302
    assert resp.headers["location"] == "/accounts"
    assert config_path.exists()

    parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert parsed["feishu"]["app_id"] == "cli_x"
    assert parsed["feishu"]["app_secret"] == "s"
    assert parsed["paths"]["nas_root"] == str(nas_dir)
    assert "account_a" in parsed["accounts"]
    assert parsed["accounts"]["account_a"]["display_name"] == "美食号"
    assert parsed["monitoring"]["notifiers"]["wecom"]["enabled"] is False


def test_step_rejects_when_prior_step_missing(fresh_app):
    """没填飞书就跳到 step 3 提交,后端应该拒绝(redirect 回 step 2)。"""
    app, _ = fresh_app
    client = TestClient(app, follow_redirects=False)
    resp = client.post("/setup/step/3", data={"nas_root": "/tmp/nas"})
    # 缺前置数据,应该跳回最早未完成的 step
    assert resp.status_code == 302
    assert "/setup/step/2" in resp.headers["location"]
```

- [ ] **Step 2: 跑测试,看到失败**

```bash
uv run pytest tests/test_routes_setup.py -v
```

预期:全部 404 / 500,`routes_setup` 还没实现。

- [ ] **Step 3: 实现 routes_setup.py**

新建 `wxsp/api/routes_setup.py`:

```python
"""向导后端:6 页表单 → 写 config.yaml → 注册自启 → 跳 /accounts(M11)。

向导数据存在 app.state.wizard_data(进程内 dict),重启 daemon 数据丢失,运营要重来。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from wxsp import autostart
from wxsp.api.deps import templates
from wxsp.config import get_config_path, get_user_data_dir, get_user_logs_dir

router = APIRouter(prefix="/setup")

_ACCOUNT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_TOTAL_STEPS = 6
_STEP_KEYS = {
    2: "feishu",
    3: "nas",
    4: "accounts",
    5: "notify",
}


def _get_wizard_data(request: Request) -> dict[str, Any]:
    if not hasattr(request.app.state, "wizard_data"):
        request.app.state.wizard_data = {}
    return request.app.state.wizard_data


def _last_completed_step(data: dict[str, Any]) -> int:
    """返回数据里最后完成的 step 编号。"""
    completed = 1  # step 1 是欢迎页,GET 即视为完成
    for step, key in sorted(_STEP_KEYS.items()):
        if key in data:
            completed = step
        else:
            break
    return completed


def _required_step_or_redirect(
    request: Request, target_step: int
) -> RedirectResponse | None:
    """提交 step N 时,前置 step 数据缺失 → 重定向到下一个该填的 step。"""
    data = _get_wizard_data(request)
    last = _last_completed_step(data)
    if target_step > last + 1:
        return RedirectResponse(url=f"/setup/step/{last + 1}", status_code=302)
    return None


# ---------------- GET 每一步 ----------------

@router.get("/step/{step}", response_class=HTMLResponse)
def render_step(step: int, request: Request) -> HTMLResponse:
    if step < 1 or step > _TOTAL_STEPS:
        raise HTTPException(status_code=404)
    data = _get_wizard_data(request)
    template_name = {
        1: "setup/welcome.html",
        2: "setup/feishu.html",
        3: "setup/nas.html",
        4: "setup/accounts.html",
        5: "setup/notify.html",
        6: "setup/complete.html",
    }[step]

    ctx: dict[str, Any] = {
        "request": request,
        "step": step,
        "total_steps": _TOTAL_STEPS,
        "data": data,
    }
    if step == 1:
        ctx["data_dir"] = str(get_user_data_dir())
        ctx["logs_dir"] = str(get_user_logs_dir())
        ctx["self_check"] = _run_self_check()
    if step == 6:
        ctx["summary"] = _build_summary(data)
    return templates.TemplateResponse(template_name, ctx)


def _run_self_check() -> list[dict[str, Any]]:
    """欢迎页自检:用户数据目录可写 + chromium 就位。"""
    checks: list[dict[str, Any]] = []
    data_dir = get_user_data_dir()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".probe"
        probe.write_text("ok")
        probe.unlink()
        checks.append({"name": "用户数据目录可写", "ok": True, "detail": str(data_dir)})
    except Exception as exc:
        checks.append({"name": "用户数据目录可写", "ok": False, "detail": str(exc)})

    # chromium 检查:packaged 模式下应该有,开发模式宽容跳过
    from wxsp.browser import _chromium_root

    chromium = _chromium_root()
    if chromium is None:
        checks.append({"name": "Chromium", "ok": True, "detail": "开发模式由 patchright 自动管理"})
    elif chromium.exists():
        checks.append({"name": "Chromium", "ok": True, "detail": str(chromium)})
    else:
        checks.append({"name": "Chromium", "ok": False, "detail": f"未找到: {chromium}"})
    return checks


def _build_summary(data: dict[str, Any]) -> dict[str, Any]:
    feishu = data.get("feishu", {})
    accounts = data.get("accounts", [])
    notify = data.get("notify", {})
    return {
        "feishu_app_id_masked": feishu.get("app_id", "")[:6] + "..." if feishu.get("app_id") else "(未填)",
        "nas_root": data.get("nas", {}).get("nas_root", "(未填)"),
        "account_count": len(accounts),
        "wecom_enabled": bool(notify.get("webhook")),
    }


# ---------------- POST 每一步 ----------------

@router.post("/step/2")
def submit_feishu(
    request: Request,
    app_id: str = Form(...),
    app_secret: str = Form(...),
    app_token: str = Form(...),
    table_id: str = Form(...),
):
    redirect = _required_step_or_redirect(request, 2)
    if redirect:
        return redirect
    if not all([app_id.strip(), app_secret.strip(), app_token.strip(), table_id.strip()]):
        return templates.TemplateResponse(
            "setup/feishu.html",
            {"request": request, "step": 2, "total_steps": _TOTAL_STEPS, "error": "所有字段均必填"},
            status_code=200,
        )
    _get_wizard_data(request)["feishu"] = {
        "app_id": app_id.strip(),
        "app_secret": app_secret.strip(),
        "app_token": app_token.strip(),
        "table_id": table_id.strip(),
    }
    return RedirectResponse(url="/setup/step/3", status_code=302)


@router.post("/step/3")
def submit_nas(request: Request, nas_root: str = Form(...)):
    redirect = _required_step_or_redirect(request, 3)
    if redirect:
        return redirect
    nas_path = Path(nas_root.strip())
    if not nas_path.exists():
        return templates.TemplateResponse(
            "setup/nas.html",
            {
                "request": request,
                "step": 3,
                "total_steps": _TOTAL_STEPS,
                "error": f"路径不存在: {nas_path}",
                "data": _get_wizard_data(request),
            },
            status_code=200,
        )
    _get_wizard_data(request)["nas"] = {"nas_root": str(nas_path)}
    return RedirectResponse(url="/setup/step/4", status_code=302)


@router.post("/step/4")
async def submit_accounts(request: Request):
    redirect = _required_step_or_redirect(request, 4)
    if redirect:
        return redirect
    form = await request.form()
    ids = form.getlist("account_id[]")
    names = form.getlist("display_name[]")
    limits = form.getlist("daily_limit[]")

    if not ids or len(ids) != len(names) or len(ids) != len(limits):
        return templates.TemplateResponse(
            "setup/accounts.html",
            {"request": request, "step": 4, "total_steps": _TOTAL_STEPS, "error": "至少 1 个账号,字段数对不齐"},
            status_code=200,
        )

    accounts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for acc_id, name, limit in zip(ids, names, limits, strict=False):
        acc_id = acc_id.strip()
        if not _ACCOUNT_ID_RE.match(acc_id):
            return templates.TemplateResponse(
                "setup/accounts.html",
                {"request": request, "step": 4, "total_steps": _TOTAL_STEPS, "error": f"account_id 格式非法: {acc_id}(需小写字母开头,英文蛇形)"},
                status_code=200,
            )
        if acc_id in seen_ids:
            return templates.TemplateResponse(
                "setup/accounts.html",
                {"request": request, "step": 4, "total_steps": _TOTAL_STEPS, "error": f"account_id 重复: {acc_id}"},
                status_code=200,
            )
        seen_ids.add(acc_id)
        try:
            limit_int = int(limit)
            assert limit_int >= 1
        except (ValueError, AssertionError):
            return templates.TemplateResponse(
                "setup/accounts.html",
                {"request": request, "step": 4, "total_steps": _TOTAL_STEPS, "error": f"daily_limit 必须 ≥ 1: {limit}"},
                status_code=200,
            )
        accounts.append({"id": acc_id, "display_name": name.strip(), "daily_limit": limit_int})

    _get_wizard_data(request)["accounts"] = accounts
    return RedirectResponse(url="/setup/step/5", status_code=302)


@router.post("/step/5")
def submit_notify(request: Request, webhook: str = Form("")):
    redirect = _required_step_or_redirect(request, 5)
    if redirect:
        return redirect
    _get_wizard_data(request)["notify"] = {"webhook": webhook.strip()}
    return RedirectResponse(url="/setup/step/6", status_code=302)


# ---------------- 完成 ----------------

@router.post("/complete")
def complete(request: Request, enable_autostart: str = Form("")):
    data = _get_wizard_data(request)
    if _last_completed_step(data) < 5:
        return RedirectResponse(url=f"/setup/step/{_last_completed_step(data) + 1}", status_code=302)

    config = _render_config(data)
    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    if enable_autostart == "on":
        try:
            autostart.enable_autostart()
        except autostart.AutostartError as exc:
            # 不阻塞向导完成,但记一笔(后端 log 会有)
            from loguru import logger

            logger.warning(f"开机自启注册失败: {exc}")

    # 清掉向导内存,免得用户再开 /setup 看到旧数据
    request.app.state.wizard_data = {}
    return RedirectResponse(url="/accounts", status_code=302)


def _render_config(data: dict[str, Any]) -> dict[str, Any]:
    """把向导收集的 dict 渲染成完整的 config.yaml 结构。"""
    feishu = data["feishu"]
    nas_root = data["nas"]["nas_root"]
    accounts = data["accounts"]
    webhook = data.get("notify", {}).get("webhook", "")

    data_dir = get_user_data_dir()
    logs_dir = get_user_logs_dir()

    accounts_yaml: dict[str, Any] = {}
    for acc in accounts:
        acc_id = acc["id"]
        accounts_yaml[acc_id] = {
            "display_name": acc["display_name"],
            "enabled": True,
            "daily_limit": acc["daily_limit"],
            "user_data_dir": str(data_dir / "chrome-profiles" / acc_id),
            "video_search_root": f"{{nas_root}}/videos/{acc_id}",
            "cover_search_root": f"{{nas_root}}/covers/{acc_id}",
        }

    return {
        "app": {
            "data_dir": str(data_dir),
            "logs_dir": str(logs_dir),
            "timezone": "Asia/Shanghai",
        },
        "paths": {"nas_root": nas_root},
        "accounts": accounts_yaml,
        "scheduler": {"daily_cron_hour": 9, "daily_cron_minute": 0, "strategy": "round-robin"},
        "publisher": {
            "headless": False,
            "upload_timeout_seconds": 600,
            "step_pause_seconds": [1, 3],
            "screenshot_on_error": True,
            "max_concurrent_accounts": 1,
        },
        "feishu": {
            "enabled": True,
            "app_id": feishu["app_id"],
            "app_secret": feishu["app_secret"],
            "bitable": {"app_token": feishu["app_token"], "table_id": feishu["table_id"]},
            "sync": {"write_back_enabled": True},
        },
        "monitoring": {
            "cookie_warn_days": 1.5,
            "notifiers": {
                "wecom": {
                    "enabled": bool(webhook),
                    "webhook": webhook or "(留空)",
                }
            },
            "notify_on": [
                "cookie_expired",
                "cookie_warning",
                "risk_control",
                "task_failed",
                "element_not_found",
                "nas_unreachable",
                "backlog_high",
            ],
            "log_retention_days": 30,
            "screenshot_retention_days": 90,
            "backlog_warn_threshold": 20,
        },
        "webui": {"host": "127.0.0.1", "port": 8765, "open_browser_on_start": True},
    }
```

- [ ] **Step 4: 在 app.py 挂载 setup router**

修改 `wxsp/api/app.py`,在 `from wxsp.api.routes_tasks import router as tasks_router` 之后加一行:

```python
from wxsp.api.routes_setup import router as setup_router
```

在 `app.include_router(logs_router)` 之后加:

```python
    app.include_router(setup_router)
```

- [ ] **Step 5: 跑测试,看到大部分通过(模板还没有 → 200 渲染会失败)**

```bash
uv run pytest tests/test_routes_setup.py -v
```

预期:
- `test_step1_welcome_renders` FAIL(模板还没建)
- `test_step2_post_stores_feishu_and_advances` PASS(302 不需要模板)
- `test_step3_post_stores_nas_and_advances` PASS
- `test_step4_post_validates_accounts` PASS
- `test_step4_rejects_invalid_account_id` FAIL(渲染错误页需要模板)
- `test_complete_writes_config_yaml` PASS
- `test_step_rejects_when_prior_step_missing` PASS

5/7 通过即可,渲染相关 fail 的留给下一个 Task。

- [ ] **Step 6: 加 3 个测试按钮端点(spec §4.2/§4.3/§4.5)**

在 `wxsp/api/routes_setup.py` 文件末尾追加(在 `_render_config` 函数之前/后均可):

```python
# ---------------- 测试按钮(spec §4.2/§4.3/§4.5)----------------

@router.post("/test-feishu")
def test_feishu(
    app_id: str = Form(...),
    app_secret: str = Form(...),
    app_token: str = Form(...),
    table_id: str = Form(...),
) -> HTMLResponse:
    """点"测试连接":拿表单值实例化 LarkClient 拉一条记录。返回 ✓ 列字段名 / ✗ 错误。"""
    from wxsp.feishu import fetch_pending_rows, make_client

    try:
        client = make_client(app_id.strip(), app_secret.strip())
        rows = fetch_pending_rows(
            client,
            app_token=app_token.strip(),
            table_id=table_id.strip(),
            status_field="状态",
        )
        sample_fields = list(rows[0].fields.keys()) if rows else []
        return HTMLResponse(
            f'<div class="check-ok">✓ 连接成功,拉到 {len(rows)} 行。字段名: {", ".join(sample_fields[:8])}</div>'
        )
    except Exception as exc:
        return HTMLResponse(f'<div class="check-fail">✗ {exc}</div>', status_code=200)


@router.post("/probe-path")
def probe_path(path: str = Form(...)) -> HTMLResponse:
    """NAS 路径实时检测:存在 / 不存在。"""
    p = Path(path.strip())
    if p.exists() and p.is_dir():
        return HTMLResponse(f'<div class="check-ok">✓ 路径存在: {p}</div>')
    return HTMLResponse(f'<div class="check-fail">✗ 不可达: {p}</div>')


@router.post("/test-wecom")
def test_wecom(webhook: str = Form(...)) -> HTMLResponse:
    """企微告警按钮:发一条测试消息。"""
    from wxsp.notify import NotifyEvent, WecomNotifier

    if not webhook.strip():
        return HTMLResponse('<div class="check-fail">✗ webhook 为空</div>')
    notifier = WecomNotifier(webhook=webhook.strip())
    try:
        ok = notifier.send(
            NotifyEvent(
                type="setup_test",
                level="info",
                title="wxsp 安装测试",
                content="如果你看到这条消息,说明 webhook 配置正确。",
            )
        )
        return HTMLResponse(
            '<div class="check-ok">✓ 消息已发,看下群</div>'
            if ok
            else '<div class="check-fail">✗ 发送失败,看后端日志</div>'
        )
    except Exception as exc:
        return HTMLResponse(f'<div class="check-fail">✗ {exc}</div>')
```

加测试用例,在 `tests/test_routes_setup.py` 末尾追加:

```python
def test_probe_path_exists(fresh_app, tmp_path):
    app, _ = fresh_app
    client = TestClient(app)
    resp = client.post("/setup/probe-path", data={"path": str(tmp_path)})
    assert resp.status_code == 200
    assert "✓" in resp.text


def test_probe_path_missing(fresh_app):
    app, _ = fresh_app
    client = TestClient(app)
    resp = client.post("/setup/probe-path", data={"path": "/no/such/path/12345"})
    assert resp.status_code == 200
    assert "✗" in resp.text


def test_test_feishu_error_path(fresh_app, monkeypatch):
    """make_client 抛异常时 endpoint 返 ✗ 不抛 500。"""
    app, _ = fresh_app
    monkeypatch.setattr(
        "wxsp.feishu.make_client",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("fake")),
    )
    client = TestClient(app)
    resp = client.post(
        "/setup/test-feishu",
        data={"app_id": "x", "app_secret": "y", "app_token": "z", "table_id": "t"},
    )
    assert resp.status_code == 200
    assert "✗" in resp.text
```

- [ ] **Step 7: 跑测试**

```bash
uv run pytest tests/test_routes_setup.py -v
```

预期:新加 3 个 + 之前已 PASS 的共 8/10 PASS(渲染相关 fail 留给 Task 7 模板)。

- [ ] **Step 8: Commit**

```bash
git add wxsp/api/app.py wxsp/api/routes_setup.py tests/test_routes_setup.py
git commit -m "feat(setup): 6 步向导后端 + 测试按钮 + 完成写 yaml(M11.2)"
```

---

## Task 7: Setup 向导前端 — 6 个模板

**Files:**
- Create: `wxsp/templates/setup/base.html`
- Create: `wxsp/templates/setup/welcome.html`(step 1)
- Create: `wxsp/templates/setup/feishu.html`(step 2)
- Create: `wxsp/templates/setup/nas.html`(step 3)
- Create: `wxsp/templates/setup/accounts.html`(step 4)
- Create: `wxsp/templates/setup/notify.html`(step 5)
- Create: `wxsp/templates/setup/complete.html`(step 6)

- [ ] **Step 1: 写 setup base 模板**

新建 `wxsp/templates/setup/base.html`:

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{% block title %}wxsp 首次设置{% endblock %}</title>
  <script src="https://unpkg.com/htmx.org@1.9.12"></script>
  <style>
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif; background: #fafafa; color: #1a1a1a; }
    .wizard { max-width: 720px; margin: 40px auto; padding: 0 24px; }
    .progress { display: flex; gap: 4px; margin-bottom: 32px; }
    .progress .dot { flex: 1; height: 6px; background: #e0e0e0; border-radius: 3px; }
    .progress .dot.done { background: #16a34a; }
    .progress .dot.active { background: #0066cc; }
    .card { background: #fff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 32px; }
    h1 { margin: 0 0 8px 0; font-size: 22px; }
    .step-label { color: #777; font-size: 13px; margin-bottom: 24px; }
    .form-row { margin-bottom: 16px; }
    .form-row label { display: block; font-size: 14px; font-weight: 500; margin-bottom: 6px; }
    .form-row input[type=text], .form-row input[type=password], .form-row input[type=number] {
      width: 100%; padding: 8px 12px; border: 1px solid #d0d0d0; border-radius: 4px; font-size: 14px;
    }
    .form-row .hint { color: #999; font-size: 12px; margin-top: 4px; }
    .actions { margin-top: 28px; display: flex; gap: 12px; justify-content: flex-end; }
    button { padding: 8px 18px; border: 1px solid #d0d0d0; background: #fff; border-radius: 4px; cursor: pointer; font-size: 14px; }
    button.primary { background: #0066cc; color: #fff; border-color: #0066cc; }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    .error { background: #fee2e2; color: #dc2626; padding: 10px 14px; border-radius: 4px; margin-bottom: 16px; font-size: 14px; }
    .check-ok { color: #16a34a; }
    .check-fail { color: #dc2626; }
    table { width: 100%; border-collapse: collapse; }
    table th, table td { padding: 8px 12px; border-bottom: 1px solid #eee; font-size: 14px; text-align: left; }
    .account-row { display: grid; grid-template-columns: 1fr 1fr 100px 40px; gap: 8px; margin-bottom: 8px; align-items: center; }
    .summary-list { font-size: 14px; line-height: 1.8; }
    .summary-list strong { display: inline-block; min-width: 100px; color: #555; }
  </style>
</head>
<body>
  <div class="wizard">
    <div class="progress">
      {% for i in range(1, (total_steps or 6) + 1) %}
        <div class="dot {% if i < step %}done{% elif i == step %}active{% endif %}"></div>
      {% endfor %}
    </div>
    <div class="card">
      <div class="step-label">第 {{ step }} 步 / 共 {{ total_steps or 6 }} 步</div>
      {% if error %}<div class="error">{{ error }}</div>{% endif %}
      {% block content %}{% endblock %}
    </div>
  </div>
</body>
</html>
```

- [ ] **Step 2: 写 step 1 — 欢迎 + 自检**

新建 `wxsp/templates/setup/welcome.html`:

```html
{% extends "setup/base.html" %}
{% block content %}
<h1>欢迎使用 wxsp</h1>
<p>这是首次设置向导,大约 3 分钟,引导你配置飞书、NAS、账号、告警。</p>

<h2 style="font-size: 16px; margin-top: 24px;">环境自检</h2>
<table>
  {% for c in self_check %}
  <tr>
    <td style="width: 30px;">{% if c.ok %}<span class="check-ok">✓</span>{% else %}<span class="check-fail">✗</span>{% endif %}</td>
    <td style="width: 180px;">{{ c.name }}</td>
    <td style="color: #777;">{{ c.detail }}</td>
  </tr>
  {% endfor %}
</table>

<p class="step-label" style="margin-top: 20px;">配置和数据将写入: <code>{{ data_dir }}</code></p>

<form method="get" action="/setup/step/2">
  <div class="actions">
    <button type="submit" class="primary"
      {% if self_check|selectattr('ok', 'equalto', false)|list %}disabled{% endif %}>
      下一步:飞书配置 →
    </button>
  </div>
</form>
{% endblock %}
```

- [ ] **Step 3: 写 step 2 — 飞书**

新建 `wxsp/templates/setup/feishu.html`:

```html
{% extends "setup/base.html" %}
{% block content %}
<h1>飞书 Bitable 配置</h1>
<p>填飞书自建应用的 app_id / app_secret + 多维表格的 token / table_id。</p>

<form method="post" action="/setup/step/2" id="feishu-form">
  <div class="form-row">
    <label>App ID</label>
    <input type="text" name="app_id" required value="{{ data.feishu.app_id if data and data.feishu else '' }}">
    <div class="hint">飞书开放平台 → 自建应用 → 凭证,以 cli_ 开头</div>
  </div>
  <div class="form-row">
    <label>App Secret</label>
    <input type="password" name="app_secret" required>
    <div class="hint">和 App ID 同一处</div>
  </div>
  <div class="form-row">
    <label>Bitable App Token</label>
    <input type="text" name="app_token" required value="{{ data.feishu.app_token if data and data.feishu else '' }}">
    <div class="hint">飞书多维表 URL: /base/<strong>xxxxxxxx</strong>?table=tblxxx 中加粗那段</div>
  </div>
  <div class="form-row">
    <label>Table ID</label>
    <input type="text" name="table_id" required value="{{ data.feishu.table_id if data and data.feishu else '' }}">
    <div class="hint">同 URL 中 ?table=<strong>tblxxx</strong> 部分</div>
  </div>

  <div class="actions">
    <button type="button"
      hx-post="/setup/test-feishu"
      hx-include="#feishu-form"
      hx-target="#test-result"
      hx-swap="innerHTML">测试连接</button>
    <a href="/setup/step/1"><button type="button">← 上一步</button></a>
    <button type="submit" class="primary">下一步:NAS 路径 →</button>
  </div>
  <div id="test-result" style="margin-top: 12px;"></div>
</form>
{% endblock %}
```

- [ ] **Step 4: 写 step 3 — NAS**

新建 `wxsp/templates/setup/nas.html`:

```html
{% extends "setup/base.html" %}
{% block content %}
<h1>NAS 挂载路径</h1>
<p>填视频和封面文件的存储根目录,工具会在这下面按账号检索文件。</p>

<form method="post" action="/setup/step/3" id="nas-form">
  <div class="form-row">
    <label>NAS Root</label>
    <input type="text" name="nas_root" required
      placeholder="/Volumes/NAS/wxsp 或 Z:/wxsp"
      value="{{ data.nas.nas_root if data and data.nas else '' }}"
      hx-post="/setup/probe-path"
      hx-trigger="blur, keyup changed delay:600ms"
      hx-vals='js:{path: event.target.value}'
      hx-target="#path-probe"
      hx-swap="innerHTML">
    <div class="hint">
      mac 示例: <code>/Volumes/NAS/wxsp</code><br>
      win 示例: <code>Z:/wxsp</code> 或 UNC <code>\\server\share\wxsp</code>
    </div>
    <div id="path-probe" style="margin-top: 8px;"></div>
  </div>
  <div class="actions">
    <a href="/setup/step/2"><button type="button">← 上一步</button></a>
    <button type="submit" class="primary">下一步:账号 →</button>
  </div>
</form>
{% endblock %}
```

- [ ] **Step 5: 写 step 4 — 账号**

新建 `wxsp/templates/setup/accounts.html`:

```html
{% extends "setup/base.html" %}
{% block content %}
<h1>视频号账号</h1>
<p>每个账号会自动生成独立的浏览器 profile 目录,装完后到"账号"页扫码登录。</p>

<form method="post" action="/setup/step/4" id="accounts-form">
  <div id="accounts-list">
    {% set existing = (data.accounts if data and data.accounts else [{'id':'account_a','display_name':'美食号','daily_limit':20}]) %}
    {% for acc in existing %}
    <div class="account-row">
      <input type="text" name="account_id[]" placeholder="account_a" required pattern="^[a-z][a-z0-9_]*$" value="{{ acc.id }}">
      <input type="text" name="display_name[]" placeholder="美食号" required value="{{ acc.display_name }}">
      <input type="number" name="daily_limit[]" placeholder="20" required min="1" value="{{ acc.daily_limit }}">
      <button type="button" onclick="this.parentElement.remove()" {% if loop.first and existing|length == 1 %}style="visibility:hidden"{% endif %}>×</button>
    </div>
    {% endfor %}
  </div>
  <button type="button" onclick="addAccountRow()">+ 添加账号</button>

  <div class="hint" style="margin-top: 12px;">
    <strong>ID 规则</strong>:小写字母开头,英文蛇形,如 <code>account_a</code><br>
    <strong>每日上限</strong>:≥ 1,视频号风控敏感,建议 ≤ 20
  </div>

  <div class="actions">
    <a href="/setup/step/3"><button type="button">← 上一步</button></a>
    <button type="submit" class="primary">下一步:告警 →</button>
  </div>
</form>

<script>
function addAccountRow() {
  const list = document.getElementById('accounts-list');
  const row = document.createElement('div');
  row.className = 'account-row';
  row.innerHTML = `
    <input type="text" name="account_id[]" placeholder="account_x" required pattern="^[a-z][a-z0-9_]*$">
    <input type="text" name="display_name[]" placeholder="显示名" required>
    <input type="number" name="daily_limit[]" placeholder="20" required min="1" value="20">
    <button type="button" onclick="this.parentElement.remove()">×</button>
  `;
  list.appendChild(row);
}
</script>
{% endblock %}
```

- [ ] **Step 6: 写 step 5 — 告警**

新建 `wxsp/templates/setup/notify.html`:

```html
{% extends "setup/base.html" %}
{% block content %}
<h1>告警渠道(可选)</h1>
<p>失败、风控、cookie 失效会推送到企微群。留空则不启用告警。</p>

<form method="post" action="/setup/step/5" id="notify-form">
  <div class="form-row">
    <label>企微机器人 Webhook</label>
    <input type="password" name="webhook" placeholder="留空 = 不启用告警"
      value="{{ data.notify.webhook if data and data.notify else '' }}">
    <div class="hint">
      在企微群"群机器人 → 添加机器人 → 复制 webhook 地址",形如 <code>https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx</code>
    </div>
  </div>
  <div class="actions">
    <button type="button"
      hx-post="/setup/test-wecom"
      hx-include="#notify-form"
      hx-target="#wecom-test-result"
      hx-swap="innerHTML">测试推送</button>
    <a href="/setup/step/4"><button type="button">← 上一步</button></a>
    <button type="submit" class="primary">下一步:确认 →</button>
  </div>
  <div id="wecom-test-result" style="margin-top: 12px;"></div>
</form>
{% endblock %}
```

- [ ] **Step 7: 写 step 6 — 完成**

新建 `wxsp/templates/setup/complete.html`:

```html
{% extends "setup/base.html" %}
{% block content %}
<h1>确认并完成</h1>
<p>检查下面的摘要,确认无误后点完成。</p>

<div class="summary-list" style="margin: 20px 0;">
  <div><strong>飞书 app_id:</strong> {{ summary.feishu_app_id_masked }}</div>
  <div><strong>NAS 根:</strong> {{ summary.nas_root }}</div>
  <div><strong>账号数:</strong> {{ summary.account_count }}</div>
  <div><strong>企微告警:</strong> {{ '启用' if summary.wecom_enabled else '未启用' }}</div>
</div>

<form method="post" action="/setup/complete">
  <div class="form-row">
    <label>
      <input type="checkbox" name="enable_autostart" checked>
      开机自动启动(推荐)
    </label>
    <div class="hint">勾选则会注册 launchctl(mac) / 任务计划程序(win)</div>
  </div>
  <div class="actions">
    <a href="/setup/step/5"><button type="button">← 上一步</button></a>
    <button type="submit" class="primary">完成,进入主界面 →</button>
  </div>
</form>
{% endblock %}
```

- [ ] **Step 8: 跑全部 setup 测试,确认全过**

```bash
uv run pytest tests/test_routes_setup.py tests/test_setup_mode.py -v
```

预期:**全部 PASS**(11/11)。

- [ ] **Step 9: 跑全量非 integration 测试**

```bash
uv run pytest -m "not integration" -q
```

预期:全部 PASS,无回归。

- [ ] **Step 10: 手动浏览验证**

```bash
# 临时移走 config.yaml,模拟首次启动
mv config.yaml config.yaml.bak 2>/dev/null
uv run wxsp web --no-browser &
sleep 2
curl -i http://127.0.0.1:8765/ | head -5
# 应看到 302 → /setup/step/1
curl http://127.0.0.1:8765/setup/step/1 | head -20
# 应看到"欢迎使用 wxsp"

# 收尾
pkill -f "wxsp web"
mv config.yaml.bak config.yaml 2>/dev/null
```

- [ ] **Step 11: Commit**

```bash
git add wxsp/templates/setup/
git commit -m "feat(setup): 6 页向导前端模板 + base.html(M11.2)"
```

---

## Task 8: scripts/build_macos.sh — Nuitka + create-dmg

**Files:**
- Create: `scripts/build_macos.sh`
- Create: `assets/icon.icns`(可选,先用占位符)

- [ ] **Step 1: 准备占位图标**

```bash
mkdir -p assets
# 用 macOS 自带工具生成 1024x1024 蓝色方块作占位
# 没有 sips/iconutil 的话留空文件,Nuitka 报警可忽略,后期换真图标
touch assets/icon.icns
```

- [ ] **Step 2: 写 build 脚本**

新建 `scripts/build_macos.sh`:

```bash
#!/usr/bin/env bash
# wxsp macOS 打包脚本(M11)。Nuitka standalone → app bundle → .dmg。
# CI 用 macos-latest 跑;本地编译需要 brew install create-dmg。
set -euo pipefail

VERSION="${WXSP_VERSION:-0.1.0}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "==> 安装 nuitka(若缺)"
uv add --dev nuitka || true

echo "==> 清理旧产物"
rm -rf dist build

echo "==> Nuitka 编译"
uv run python -m nuitka \
  --standalone \
  --macos-create-app-bundle \
  --macos-app-name=wxsp \
  --macos-app-icon=assets/icon.icns \
  --include-package=wxsp \
  --include-package-data=wxsp \
  --include-data-dir=wxsp/templates=wxsp/templates \
  --include-data-files=deploy/wxsp.plist.tmpl=deploy/wxsp.plist.tmpl \
  --include-data-files=deploy/wxsp-task.xml.tmpl=deploy/wxsp-task.xml.tmpl \
  --output-dir=dist \
  --assume-yes-for-downloads \
  --remove-output \
  wxsp/__main__.py

APP_PATH="dist/__main__.app"
# Nuitka 默认按 __main__ 命名,改成 wxsp.app
mv "$APP_PATH" "dist/wxsp.app"
APP_PATH="dist/wxsp.app"

echo "==> 内嵌 patchright chromium"
CHROMIUM_SRC="$(uv run python -c "
import patchright, os
driver = os.path.join(os.path.dirname(patchright.__file__), 'driver')
candidates = [d for d in os.listdir(driver) if d.startswith('chromium')]
assert candidates, f'未在 {driver} 找到 chromium 目录'
print(os.path.join(driver, candidates[0]))
")"
echo "    chromium 源: $CHROMIUM_SRC"
mkdir -p "$APP_PATH/Contents/Resources/chromium"
cp -R "$CHROMIUM_SRC/." "$APP_PATH/Contents/Resources/chromium/"

echo "==> 修可执行位"
find "$APP_PATH/Contents/Resources/chromium" -name "Chromium" -exec chmod +x {} \; 2>/dev/null || true
find "$APP_PATH/Contents/Resources/chromium" -name "chrome" -exec chmod +x {} \; 2>/dev/null || true

echo "==> 用 create-dmg 打包"
if ! command -v create-dmg >/dev/null 2>&1; then
  echo "未装 create-dmg。本地: brew install create-dmg;CI: workflow 里已加。" >&2
  exit 1
fi

rm -f "dist/wxsp-${VERSION}.dmg"
create-dmg \
  --volname "wxsp Installer" \
  --window-size 600 400 \
  --app-drop-link 450 200 \
  --icon "wxsp.app" 150 200 \
  --hide-extension "wxsp.app" \
  "dist/wxsp-${VERSION}.dmg" \
  "$APP_PATH"

echo "==> 完成: dist/wxsp-${VERSION}.dmg"
ls -lh "dist/wxsp-${VERSION}.dmg"
```

赋可执行权限:

```bash
chmod +x scripts/build_macos.sh
```

- [ ] **Step 3: 本地试编译(只验证 Nuitka 部分)**

```bash
# 不要求装 create-dmg,跳过最后一步即可
WXSP_VERSION=0.1.0-dev bash -c '
  set -e
  cd .
  rm -rf dist build
  uv add --dev nuitka
  uv run python -m nuitka \
    --standalone \
    --macos-create-app-bundle \
    --include-package=wxsp \
    --include-package-data=wxsp \
    --include-data-dir=wxsp/templates=wxsp/templates \
    --include-data-files=deploy/wxsp.plist.tmpl=deploy/wxsp.plist.tmpl \
    --include-data-files=deploy/wxsp-task.xml.tmpl=deploy/wxsp-task.xml.tmpl \
    --output-dir=dist \
    --assume-yes-for-downloads \
    wxsp/__main__.py
' 2>&1 | tail -20
```

预期:Nuitka 跑通,生成 `dist/__main__.app`(或类似)。可能耗时 5-10 分钟。**这步是 smoke test,失败再调脚本**(Nuitka 兼容性是 M11 最大风险点,见 spec §9)。

如果跑通:

```bash
ls dist/
# 应该看到 __main__.app 或 wxsp.app 目录
```

- [ ] **Step 4: Commit(可不验证 dmg)**

```bash
git add scripts/build_macos.sh assets/icon.icns
git commit -m "feat(build): macOS Nuitka + create-dmg 脚本(M11.1)"
```

> 提示:不要 commit `dist/` 目录(默认应该被 `.gitignore`,如果没有,加一条)。

---

## Task 9: scripts/build_windows.ps1 + setup.iss

**Files:**
- Create: `scripts/build_windows.ps1`
- Create: `scripts/setup.iss`

- [ ] **Step 1: 写 PowerShell 编译脚本**

新建 `scripts/build_windows.ps1`:

```powershell
# wxsp Windows 打包(M11)。Nuitka standalone → Inno Setup → setup.exe
$ErrorActionPreference = "Stop"

$Version = if ($env:WXSP_VERSION) { $env:WXSP_VERSION } else { "0.1.0" }
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

Write-Host "==> 装 Nuitka(若缺)"
uv add --dev nuitka 2>&1 | Out-Null

Write-Host "==> 清理旧产物"
Remove-Item -Recurse -Force dist, build -ErrorAction SilentlyContinue

Write-Host "==> Nuitka 编译"
uv run python -m nuitka `
  --standalone `
  --windows-console-mode=disable `
  --include-package=wxsp `
  --include-package-data=wxsp `
  --include-data-dir=wxsp/templates=wxsp/templates `
  --include-data-files=deploy/wxsp.plist.tmpl=deploy/wxsp.plist.tmpl `
  --include-data-files=deploy/wxsp-task.xml.tmpl=deploy/wxsp-task.xml.tmpl `
  --output-dir=dist `
  --assume-yes-for-downloads `
  --remove-output `
  wxsp/__main__.py

# Nuitka 默认产物名 __main__.dist
$DistDir = "dist/__main__.dist"
if (-not (Test-Path $DistDir)) {
  throw "Nuitka 未生成 $DistDir"
}
Rename-Item $DistDir "wxsp.dist"
$DistDir = "dist/wxsp.dist"
Rename-Item "$DistDir/__main__.exe" "wxsp.exe"

Write-Host "==> 内嵌 patchright chromium"
$ChromiumSrc = uv run python -c "
import patchright, os
d = os.path.join(os.path.dirname(patchright.__file__), 'driver')
c = [x for x in os.listdir(d) if x.startswith('chromium')]
assert c, 'no chromium'
print(os.path.join(d, c[0]))
"
Write-Host "    chromium 源: $ChromiumSrc"
$ChromiumDst = "$DistDir/chromium"
New-Item -ItemType Directory -Force -Path $ChromiumDst | Out-Null
Copy-Item -Recurse -Force "$ChromiumSrc/*" $ChromiumDst

Write-Host "==> 运行 Inno Setup"
$InnoPath = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $InnoPath)) {
  throw "未找到 Inno Setup,装它: choco install innosetup -y"
}
& $InnoPath /Qp "/DAppVersion=$Version" "/DSourceDir=$ProjectRoot\dist\wxsp.dist" scripts/setup.iss

Write-Host "==> 完成"
Get-ChildItem "dist/*setup*.exe" | Format-Table Name, Length
```

- [ ] **Step 2: 写 Inno Setup 脚本**

新建 `scripts/setup.iss`:

```iss
; wxsp Windows 安装器(M11)。
; 用 ISCC.exe 编译;由 build_windows.ps1 调用,/DAppVersion + /DSourceDir 通过命令行传入。

#define AppName "wxsp"
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\wxsp.dist"
#endif

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=wxsp
DefaultDirName={autopf}\wxsp
DefaultGroupName=wxsp
OutputDir=..\dist
OutputBaseFilename={#AppName}-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
WizardStyle=modern
SetupIconFile=..\assets\icon.ico

[Languages]
Name: "chinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "开机自动启动 wxsp"; GroupDescription: "附加任务"
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\wxsp"; Filename: "{app}\wxsp.exe"
Name: "{group}\卸载 wxsp"; Filename: "{uninstallexe}"
Name: "{commondesktop}\wxsp"; Filename: "{app}\wxsp.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\wxsp.exe"; Parameters: "autostart enable"; Tasks: autostart; Flags: runhidden waituntilterminated
Filename: "{app}\wxsp.exe"; Description: "启动 wxsp"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\wxsp.exe"; Parameters: "autostart disable"; Flags: runhidden waituntilterminated
```

注意:这里需要先把 `wxsp autostart enable/disable` 这个 CLI 命令加上(下一步)。

- [ ] **Step 3: 在 cli.py 加 autostart 子命令**

修改 `wxsp/cli.py`,在文件末尾(`if __name__ == "__main__":` 之前)插入:

```python
autostart_app = typer.Typer(help="开机自启管理", no_args_is_help=True)
app.add_typer(autostart_app, name="autostart")


@autostart_app.command("enable")
def autostart_enable() -> None:
    """注册开机自启(mac launchctl / win 任务计划程序)。"""
    from wxsp.autostart import AutostartError, enable_autostart

    try:
        enable_autostart()
        typer.echo("[wxsp] ✓ 开机自启已注册")
    except AutostartError as exc:
        typer.echo(f"[wxsp] ✗ 注册失败:{exc}")
        raise typer.Exit(code=1) from exc


@autostart_app.command("disable")
def autostart_disable() -> None:
    """反注册开机自启。"""
    from wxsp.autostart import disable_autostart

    disable_autostart()
    typer.echo("[wxsp] ✓ 开机自启已反注册")


@autostart_app.command("status")
def autostart_status() -> None:
    """查询自启是否注册。"""
    from wxsp.autostart import is_autostart_enabled

    if is_autostart_enabled():
        typer.echo("[wxsp] 开机自启:已启用")
    else:
        typer.echo("[wxsp] 开机自启:未启用")
        raise typer.Exit(code=1)
```

- [ ] **Step 4: 写 CLI 测试**

在 `tests/test_cli.py` 末尾追加(若没有 `test_cli.py` 单独这部分,在 cli 测试文件里加):

```python
def test_autostart_status_when_disabled(monkeypatch) -> None:
    from typer.testing import CliRunner

    from wxsp.cli import app

    monkeypatch.setattr("wxsp.autostart.is_autostart_enabled", lambda: False)
    runner = CliRunner()
    result = runner.invoke(app, ["autostart", "status"])
    assert result.exit_code == 1
    assert "未启用" in result.stdout


def test_autostart_status_when_enabled(monkeypatch) -> None:
    from typer.testing import CliRunner

    from wxsp.cli import app

    monkeypatch.setattr("wxsp.autostart.is_autostart_enabled", lambda: True)
    runner = CliRunner()
    result = runner.invoke(app, ["autostart", "status"])
    assert result.exit_code == 0
    assert "已启用" in result.stdout


def test_autostart_enable_calls_module(monkeypatch) -> None:
    from typer.testing import CliRunner

    from wxsp.cli import app

    called = []
    monkeypatch.setattr("wxsp.autostart.enable_autostart", lambda: called.append(True))
    runner = CliRunner()
    result = runner.invoke(app, ["autostart", "enable"])
    assert result.exit_code == 0
    assert called == [True]
```

- [ ] **Step 5: 跑测试**

```bash
uv run pytest tests/test_cli.py -v -k autostart
```

预期:3/3 PASS。

- [ ] **Step 6: Commit**

```bash
git add scripts/build_windows.ps1 scripts/setup.iss wxsp/cli.py tests/test_cli.py
git commit -m "feat(build): Windows Nuitka + Inno Setup + CLI autostart 命令(M11.1)"
```

> Windows 脚本无法在 mac 本地验证,留给 GitHub Actions CI 跑通即视为可用(下个 Task)。

---

## Task 10: GitHub Actions CI workflow

**Files:**
- Create: `.github/workflows/build.yml`

- [ ] **Step 1: 写 workflow**

新建 `.github/workflows/build.yml`:

```yaml
name: Build installers

on:
  push:
    tags: ['v*']
  workflow_dispatch:

jobs:
  build-macos:
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - uses: astral-sh/setup-uv@v3
        with:
          enable-cache: true

      - name: uv sync
        run: uv sync --all-extras

      - name: Cache patchright chromium
        uses: actions/cache@v4
        with:
          path: ~/.cache/ms-playwright
          key: chromium-mac-${{ hashFiles('uv.lock') }}

      - name: Install chromium
        run: uv run patchright install chromium

      - name: Install create-dmg
        run: brew install create-dmg

      - name: Build .dmg
        env:
          WXSP_VERSION: ${{ github.ref_type == 'tag' && github.ref_name || '0.0.0-dev' }}
        run: bash scripts/build_macos.sh

      - uses: actions/upload-artifact@v4
        with:
          name: macos-dmg
          path: dist/*.dmg

  build-windows:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - uses: astral-sh/setup-uv@v3
        with:
          enable-cache: true

      - name: uv sync
        run: uv sync --all-extras

      - name: Cache patchright chromium
        uses: actions/cache@v4
        with:
          path: ~\AppData\Local\ms-playwright
          key: chromium-win-${{ hashFiles('uv.lock') }}

      - name: Install chromium
        run: uv run patchright install chromium

      - name: Install Inno Setup
        run: choco install innosetup -y --no-progress

      - name: Build setup.exe
        env:
          WXSP_VERSION: ${{ github.ref_type == 'tag' && github.ref_name || '0.0.0-dev' }}
        run: powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1

      - uses: actions/upload-artifact@v4
        with:
          name: windows-exe
          path: dist/*setup*.exe

  release:
    needs: [build-macos, build-windows]
    if: startsWith(github.ref, 'refs/tags/v')
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/download-artifact@v4
        with:
          path: artifacts

      - name: Create GitHub Release
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          GH_REPO: ${{ github.repository }}
        run: |
          gh release create "${{ github.ref_name }}" \
            artifacts/macos-dmg/*.dmg \
            artifacts/windows-exe/*setup*.exe \
            --generate-notes
```

- [ ] **Step 2: 验证 YAML 语法(本地 quick check)**

```bash
uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/build.yml'))"
```

预期:无输出 = 语法 OK。

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/build.yml
git commit -m "ci: GitHub Actions 出 mac.dmg + win.exe(M11.2)"
```

- [ ] **Step 4: 跑 CI(workflow_dispatch 手动触发)**

```bash
git push origin main
gh workflow run build.yml
```

观察一次完整跑通:

```bash
gh run watch
```

预期:macos-dmg 和 windows-exe 两个 artifact 都生成,~15 分钟。

如果 Nuitka 在 CI 上失败:
- mac 端最常见是 `--macos-app-icon` 找不到 → 删除该行或加占位 icns
- win 端最常见是 `--windows-console-mode=disable` 参数名变化 → 改成 `--disable-console`(老版本 Nuitka)或 `--windows-disable-console`

**这里不是 plan 的 hard fail point**:CI 跑不通要回到 Task 8/9 调脚本,plan 的工程目标"workflow 文件存在 + 本地脚本可调"已达成。

---

## Task 11: 验收冒烟 + README 重写 + CLAUDE.md 更新

**Files:**
- Modify: `README.md`(安装章节重写)
- Modify: `CLAUDE.md`("起步任务清单"加 M11)
- Test: 手工冒烟参照 spec §7

- [ ] **Step 1: 重写 README 安装章节**

替换 `README.md` 第 9-47 行(从 `## 系统要求` 到 `## 首次使用` 之前)为:

```markdown
---

## 安装

### 普通运营用户(推荐)

**macOS**:
1. 从 [Releases](https://github.com/your-org/wxsp/releases) 下载 `wxsp-x.y.z.dmg`
2. 双击挂载,把 `wxsp.app` 拖到 `/Applications`
3. **首次打开**:右键 `wxsp.app` → 「打开」→ 确认。普通双击会被 Gatekeeper 拦截(因为没苹果开发者签名)
4. 浏览器会自动弹到 http://127.0.0.1:8765/setup —— 走完 6 步向导

**Windows**:
1. 下载 `wxsp-x.y.z-setup.exe`
2. **首次打开**:SmartScreen 警告 → 「更多信息」→ 「仍要运行」
3. 安装向导默认勾选「开机自启」+ 「桌面快捷方式」,确认后安装
4. 装完自动启动,浏览器弹到 setup 向导

### 开发者(从源码)

```bash
git clone <repo-url> wechat-sph-upload
cd wechat-sph-upload
uv sync
uv run patchright install chromium
cp config.example.yaml config.yaml  # 然后编辑
export FEISHU_APP_SECRET='cli_xxx'
export WECOM_BOT_WEBHOOK='https://...'
uv run wxsp doctor                  # 应输出 配置✓ DB✓ NAS✓ 飞书✓
uv run wxsp web                     # 起 web UI
```

---

## 首次设置向导

普通用户装完打开就是向导,走 6 步:**欢迎/自检 → 飞书 → NAS → 账号 → 告警 → 完成**。完成后会自动跳到「账号」页扫码登录。

数据落盘位置:
- macOS: `~/Library/Application Support/wxsp/`
- Windows: `%APPDATA%\wxsp\`

迁机:把上面这个目录整个拷到新机的同位置,新机装好 app 后启动直接跳过向导进 Dashboard。

---
```

(保留剩余章节不动 — 首次使用、开机自启、常用命令、故障排查、项目结构、开发、安全 等。)

- [ ] **Step 2: 更新 CLAUDE.md 的 milestone 清单**

修改 `CLAUDE.md` 的 "起步任务清单(Milestones)" 那块,在 M10 后追加 M11 行,并更新依赖关系图:

替换 milestones 表里的 M10 行下方,加一行:

```
| M11 | 安装器 + 设置向导 | Nuitka 编译 + .dmg/.exe 出包 + Web UI 6 页向导 + `wxsp autostart enable/disable` |
```

依赖关系图末尾加箭头:

```
                                                                                                ↓
                                                                                       M10 部署+文档
                                                                                                │
                                                                                                ↓
                                                                                   M11 安装器+向导
```

合计工时改成 "**~22.5 工作日**"(原 15 + M11 7.5)。

- [ ] **Step 3: 提交 docs**

```bash
git add README.md CLAUDE.md
git commit -m "docs: M11 安装器章节重写 + milestones 更新"
```

- [ ] **Step 4: 手工冒烟 — macOS**

照着 spec §7.2 逐条打勾:

```
- [ ] 双击 .dmg 弹窗 / 拖 wxsp.app
- [ ] 右键 → 打开 → Gatekeeper 二次确认 → app 启动
- [ ] 浏览器自动开 :8765/setup/step/1
- [ ] 走完 6 步(造测试飞书表 + 测试 NAS 目录)
- [ ] ~/Library/Application Support/wxsp/config.yaml 已生成
- [ ] launchctl print gui/$(id -u)/com.wxsp.daemon 退出码 0
- [ ] 重启 mac → 登录 30s 内 daemon 自动起
- [ ] 拷贝用户数据目录到第二台机器 → 跳过向导直进 Dashboard
- [ ] strings /Applications/wxsp.app/Contents/MacOS/wxsp | grep -i "selectors\|publisher" 捞不到清晰业务串
```

任一失败:回到对应 Task 修。

- [ ] **Step 5: 手工冒烟 — Windows**

照着 spec §7.3 在 Windows 机器上逐条打勾(可用 VMware/Parallels 一台 Win11 客机)。

- [ ] **Step 6: 端到端验证**

照 spec §7.4:扫码登录 1 个账号 → 飞书表加一条今日任务 → 点 Web UI "立即同步 + 跑今天" → 浏览器弹出 → dry-run 成功。

- [ ] **Step 7: 最终 commit + tag**

```bash
git tag v0.1.0
git push origin v0.1.0
```

CI 出 release。下载 `.dmg` 和 `setup.exe`,挂到内部群分发。

---

## 完成检查表

跑完上面所有 Task 后,以下都应该成立:

- [ ] `uv run pytest -m "not integration"` 全过(含新增 test_paths / test_autostart / test_routes_setup / test_setup_mode)
- [ ] `uv run wxsp autostart status` 在装好 app 的机器上能正确报告状态
- [ ] 在干净环境 `wxsp web` 启动后,config.yaml 不存在则自动跳 /setup
- [ ] CI 跑 `workflow_dispatch` 出两个 artifact
- [ ] tag 推 `v*` 触发 release,挂上 .dmg + setup.exe
- [ ] mac / win 双端冒烟全过(spec §7)

如果 M11.1(Task 1-4 + 8-10 前两步)已先合并,M11.2(Task 5-7 + 10 后两步 + 11)单独 PR/branch 也合规 —— 见 spec §10 末尾的"中型可拆"建议。
