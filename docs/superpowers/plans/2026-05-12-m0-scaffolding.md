# M0 脚手架 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 wxsp 项目骨架 —— pyproject.toml(uv 管理)、ruff/mypy/pre-commit、扁平 `wxsp/` 包、Typer CLI 命令骨架、完整的 Pydantic config 加载器、`config.example.yaml` 模板,通过 `wxsp --help` 和 `pre-commit run --all-files` 两条验收线。

**Architecture:** 顶层扁平包结构(`wxsp/*.py` 单文件单职责),Typer 作为 CLI 框架,Pydantic Settings + 自写 `${ENV_VAR}` 展开 + `{nas_root}` 路径模板。所有 M1-M10 用得到的模块文件先创建为 stub(空模块或函数占位),保证后续 milestone 不必频繁挪文件。

**Tech Stack:** Python 3.10+,uv,Typer,Pydantic v2 + Pydantic Settings,PyYAML,loguru,pytest,ruff,mypy,pre-commit。

---

## File Structure

新建文件清单(全部新建,无需修改既有源码):

| 文件 | 责任 |
|------|------|
| `pyproject.toml` | uv 项目元数据 + 依赖 + 入口点 + ruff/mypy 配置 |
| `.pre-commit-config.yaml` | pre-commit hook 集合 |
| `config.example.yaml` | 配置模板(用户复制为 `config.yaml`) |
| `wxsp/__init__.py` | 包标识 + `__version__` |
| `wxsp/cli.py` | Typer app + 全部 CLI 命令骨架 |
| `wxsp/config.py` | Pydantic Settings + `load_settings()` 加载函数 |
| `wxsp/db.py` | M1 占位 |
| `wxsp/models.py` | M1 占位 |
| `wxsp/feishu.py` | M3 占位 |
| `wxsp/validator.py` | M3 占位 |
| `wxsp/scheduler.py` | M6 占位 |
| `wxsp/publisher.py` | M5 占位 |
| `wxsp/selectors.py` | M5 占位 |
| `wxsp/browser.py` | M2 占位 |
| `wxsp/stealth_js.py` | M2 占位 |
| `wxsp/errors.py` | M5 占位 |
| `wxsp/notify.py` | M7 占位 |
| `wxsp/doctor.py` | M2 占位 |
| `wxsp/nas.py` | M4 占位 |
| `wxsp/retry.py` | M5 占位 |
| `tests/__init__.py` | 空 |
| `tests/test_config.py` | config 加载单元测试 |
| `tests/test_cli.py` | CLI 命令骨架存在性测试 |
| `README.md` | 最简介绍(三行) |

`api/` 子包延后到 M8 再创建(YAGNI)。`data/`、`logs/` 已在 .gitignore,M0 不创建。

---

## Task 1: pyproject.toml + uv 项目初始化

**Files:**
- Create: `/Users/zhaoguangyu/wechat-sph-upload/pyproject.toml`

- [ ] **Step 1: 写 pyproject.toml**

```toml
[project]
name = "wxsp"
version = "0.0.1"
description = "微信视频号自动发布工具"
readme = "README.md"
requires-python = ">=3.10"
dependencies = [
    "typer>=0.12.0",
    "pydantic>=2.6.0",
    "pydantic-settings>=2.2.0",
    "pyyaml>=6.0",
    "loguru>=0.7.2",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "ruff>=0.4.0",
    "mypy>=1.10.0",
    "pre-commit>=3.7.0",
    "types-pyyaml>=6.0.12",
]

[project.scripts]
wxsp = "wxsp.cli:app"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["wxsp"]

[tool.ruff]
line-length = 100
target-version = "py310"

[tool.ruff.lint]
select = ["E", "W", "F", "I", "B", "UP", "RUF"]
ignore = ["E501"]  # line-length handled by formatter

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["B011"]  # allow assert False in tests

[tool.mypy]
python_version = "3.10"
strict = true
warn_unused_ignores = true
disallow_untyped_decorators = false  # typer decorators are untyped
plugins = ["pydantic.mypy"]

[[tool.mypy.overrides]]
module = "tests.*"
disallow_untyped_defs = false

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
```

- [ ] **Step 2: 创建虚拟环境并同步依赖**

Run: `cd /Users/zhaoguangyu/wechat-sph-upload && uv venv && uv pip install -e ".[dev]"`
Expected: `.venv/` 目录被创建,依赖安装无错误。

- [ ] **Step 3: 验证 uv 能跑 Python**

Run: `uv run python -c "import typer, pydantic, yaml, loguru; print('ok')"`
Expected: 输出 `ok`,没有 ImportError。

- [ ] **Step 4: 把 .venv 加进 .gitignore(若未加)**

读 `.gitignore` 确认含有 `.venv/`(已含,不动)。

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "chore: init pyproject.toml with uv + ruff/mypy/pre-commit deps"
```

---

## Task 2: 创建 wxsp 包骨架(空模块)

**Files:**
- Create: `wxsp/__init__.py`
- Create: `wxsp/cli.py`(空,Task 5 填充)
- Create: `wxsp/config.py`(空,Task 4 填充)
- Create: `wxsp/db.py` `wxsp/models.py` `wxsp/feishu.py` `wxsp/validator.py` `wxsp/scheduler.py` `wxsp/publisher.py` `wxsp/selectors.py` `wxsp/browser.py` `wxsp/stealth_js.py` `wxsp/errors.py` `wxsp/notify.py` `wxsp/doctor.py` `wxsp/nas.py` `wxsp/retry.py`

- [ ] **Step 1: 写 `wxsp/__init__.py`**

```python
"""wxsp - 微信视频号自动发布工具"""

__version__ = "0.0.1"
```

- [ ] **Step 2: 每个占位模块写一句 docstring**

对以下每个文件,写**只含模块 docstring 的占位文件**(不写函数/类,等对应 milestone 实现):

`wxsp/db.py`:
```python
"""SQLModel engine + session + 状态转换辅助 + 幂等锁(M1)。"""
```

`wxsp/models.py`:
```python
"""SQLModel 表定义:Account / Video / Task / Event(M1)。"""
```

`wxsp/feishu.py`:
```python
"""飞书 Bitable 拉取与回写(M3)。"""
```

`wxsp/validator.py`:
```python
"""入库校验(纯函数)(M3)。"""
```

`wxsp/scheduler.py`:
```python
"""09:00 cron + 手动 fire(无 polling)(M6)。"""
```

`wxsp/publisher.py`:
```python
"""视频号发布核心,基于 patchright(M5)。"""
```

`wxsp/selectors.py`:
```python
"""视频号页面选择器集中管理 —— 视频号改版时的唯一改动点(M5)。"""
```

`wxsp/browser.py`:
```python
"""patchright context 工厂 + stealth 注入(M2)。"""
```

`wxsp/stealth_js.py`:
```python
"""反检测 init script 常量(M2)。"""
```

`wxsp/errors.py`:
```python
"""错误类型 + 分类(M5)。"""
```

`wxsp/notify.py`:
```python
"""Notifier 协议 + WecomNotifier(M7)。"""
```

`wxsp/doctor.py`:
```python
"""健康检查命令实现(M2)。"""
```

`wxsp/nas.py`:
```python
"""NAS 文件检索 + stage_to_tmp + cleanup_tmp(M4)。"""
```

`wxsp/retry.py`:
```python
"""重试装饰器 / 指数退避(M5)。"""
```

`wxsp/cli.py`:
```python
"""Typer CLI 入口(M0 骨架,后续 milestone 逐步实现命令体)。"""
```

`wxsp/config.py`:
```python
"""Pydantic Settings + YAML/ENV 加载(M0)。"""
```

- [ ] **Step 3: 验证包能 import**

Run: `uv run python -c "import wxsp; print(wxsp.__version__)"`
Expected: 输出 `0.0.1`。

- [ ] **Step 4: Commit**

```bash
git add wxsp/
git commit -m "chore: scaffold wxsp package with stub modules"
```

---

## Task 3: 写 config 加载的失败测试

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: 创建 `tests/__init__.py`(空)**

```python
```

- [ ] **Step 2: 写 `tests/test_config.py`(全部失败,因为 `load_settings` 还没实现)**

```python
"""Tests for wxsp.config."""
from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent

import pytest

from wxsp.config import Settings, load_settings


@pytest.fixture
def minimal_yaml() -> str:
    """A minimal valid config yaml as a string."""
    return dedent(
        """
        app:
          data_dir: ./data
          logs_dir: ./logs
          timezone: Asia/Shanghai
        paths:
          nas_root: /Volumes/NAS/wxsp
          video_search_root: "{nas_root}/videos"
          cover_search_root: "{nas_root}/covers"
        accounts:
          account_a:
            display_name: 测试号
            enabled: true
            daily_limit: 20
            user_data_dir: ./data/chrome-profiles/account_a
        scheduler:
          daily_cron_hour: 9
          daily_cron_minute: 0
          strategy: round-robin
        publisher:
          headless: false
          upload_timeout_seconds: 600
          step_pause_seconds: [1, 3]
          screenshot_on_error: true
          max_concurrent_accounts: 1
        feishu:
          enabled: true
          app_id: cli_xxx
          app_secret: ${TEST_FEISHU_SECRET}
          bitable:
            app_token: tok_xxx
            table_id: tbl_xxx
          field_map:
            video_file: 视频文件
            title: 标题
            description: 描述
            tags: 标签
            cover: 封面文件
            topic: 合集
            original_claim: 原创
            account: 账号
            execute_date: 执行日期
            publish_at: 定时发布时间
            status: 状态
            remote_url: 已发布链接
            error_message: 错误信息
          sync:
            write_back_enabled: true
        monitoring:
          cookie_warn_days: 1.5
          notifiers:
            wecom:
              enabled: true
              webhook: ${TEST_WECOM_WEBHOOK}
          notify_on:
            - task_failed
            - risk_control
        webui:
          host: 127.0.0.1
          port: 8765
          open_browser_on_start: true
        """
    ).strip()


def test_load_valid_yaml(tmp_path: Path, minimal_yaml: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_FEISHU_SECRET", "secret_value")
    monkeypatch.setenv("TEST_WECOM_WEBHOOK", "https://example.com/wecom")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(minimal_yaml, encoding="utf-8")

    settings = load_settings(config_path)

    assert isinstance(settings, Settings)
    assert settings.app.timezone == "Asia/Shanghai"
    assert settings.publisher.max_concurrent_accounts == 1
    assert "account_a" in settings.accounts
    assert settings.accounts["account_a"].display_name == "测试号"


def test_env_var_substitution(tmp_path: Path, minimal_yaml: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_FEISHU_SECRET", "expanded_secret")
    monkeypatch.setenv("TEST_WECOM_WEBHOOK", "https://expanded.example.com")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(minimal_yaml, encoding="utf-8")

    settings = load_settings(config_path)

    assert settings.feishu.app_secret == "expanded_secret"
    assert settings.monitoring.notifiers.wecom.webhook == "https://expanded.example.com"


def test_nas_root_template_substitution(tmp_path: Path, minimal_yaml: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_FEISHU_SECRET", "x")
    monkeypatch.setenv("TEST_WECOM_WEBHOOK", "x")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(minimal_yaml, encoding="utf-8")

    settings = load_settings(config_path)

    assert str(settings.paths.video_search_root) == "/Volumes/NAS/wxsp/videos"
    assert str(settings.paths.cover_search_root) == "/Volumes/NAS/wxsp/covers"


def test_missing_env_var_raises(tmp_path: Path, minimal_yaml: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_FEISHU_SECRET", raising=False)
    monkeypatch.delenv("TEST_WECOM_WEBHOOK", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(minimal_yaml, encoding="utf-8")

    with pytest.raises(ValueError, match="TEST_FEISHU_SECRET"):
        load_settings(config_path)


def test_invalid_yaml_raises_clear_error(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("not a valid yaml: : :", encoding="utf-8")

    with pytest.raises(Exception):
        load_settings(config_path)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "nonexistent.yaml")
```

- [ ] **Step 3: 跑测试,确认失败**

Run: `uv run pytest tests/test_config.py -v`
Expected: 全部测试 FAIL,错误信息形如 `ImportError: cannot import name 'load_settings' from 'wxsp.config'`(或 `Settings` 同理)。

- [ ] **Step 4: Commit**

```bash
git add tests/__init__.py tests/test_config.py
git commit -m "test: add failing tests for config loading"
```

---

## Task 4: 实现 config.py 让测试通过 + 写 config.example.yaml

**Files:**
- Modify: `wxsp/config.py`(从占位升级为完整实现)
- Create: `config.example.yaml`

- [ ] **Step 1: 实现 `wxsp/config.py`**

```python
"""Pydantic Settings + YAML/ENV 加载(M0)。"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator


class AppConfig(BaseModel):
    data_dir: Path
    logs_dir: Path
    timezone: str


class PathsConfig(BaseModel):
    nas_root: Path
    video_search_root: Path
    cover_search_root: Path


class AccountConfig(BaseModel):
    display_name: str
    enabled: bool = True
    daily_limit: int
    user_data_dir: Path


class SchedulerConfig(BaseModel):
    daily_cron_hour: int = 9
    daily_cron_minute: int = 0
    strategy: str = "round-robin"


class PublisherConfig(BaseModel):
    headless: bool = False
    upload_timeout_seconds: int = 600
    step_pause_seconds: tuple[float, float] = (1.0, 3.0)
    screenshot_on_error: bool = True
    max_concurrent_accounts: int = 1


class FeishuFieldMap(BaseModel):
    video_file: str = "视频文件"
    title: str = "标题"
    description: str = "描述"
    tags: str = "标签"
    cover: str = "封面文件"
    topic: str = "合集"
    original_claim: str = "原创"
    account: str = "账号"
    execute_date: str = "执行日期"
    publish_at: str = "定时发布时间"
    status: str = "状态"
    remote_url: str = "已发布链接"
    error_message: str = "错误信息"


class FeishuBitableConfig(BaseModel):
    app_token: str
    table_id: str


class FeishuSyncConfig(BaseModel):
    write_back_enabled: bool = True


class FeishuConfig(BaseModel):
    enabled: bool = True
    app_id: str
    app_secret: str
    bitable: FeishuBitableConfig
    field_map: FeishuFieldMap = Field(default_factory=FeishuFieldMap)
    sync: FeishuSyncConfig = Field(default_factory=FeishuSyncConfig)


class WecomNotifierConfig(BaseModel):
    enabled: bool = True
    webhook: str


class NotifiersConfig(BaseModel):
    wecom: WecomNotifierConfig


class MonitoringConfig(BaseModel):
    cookie_warn_days: float = 1.5
    notifiers: NotifiersConfig
    notify_on: list[str] = Field(default_factory=list)


class WebUIConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser_on_start: bool = True


class Settings(BaseModel):
    app: AppConfig
    paths: PathsConfig
    accounts: dict[str, AccountConfig]
    scheduler: SchedulerConfig
    publisher: PublisherConfig
    feishu: FeishuConfig
    monitoring: MonitoringConfig
    webui: WebUIConfig

    @model_validator(mode="after")
    def _expand_nas_root_template(self) -> "Settings":
        nas_root_str = str(self.paths.nas_root)
        for field in ("video_search_root", "cover_search_root"):
            current = str(getattr(self.paths, field))
            if "{nas_root}" in current:
                expanded = current.replace("{nas_root}", nas_root_str)
                setattr(self.paths, field, Path(expanded))
        return self


_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _expand_env_vars(text: str) -> str:
    """Replace ${VAR} with os.environ[VAR]; raise ValueError if missing."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"环境变量 {name} 未设置,无法展开 config.yaml 中的 ${{{name}}}")
        return os.environ[name]

    return _ENV_PATTERN.sub(replace, text)


def load_settings(config_path: Path | None = None) -> Settings:
    """Load and validate config.yaml; expand ${ENV_VAR} and {nas_root}."""
    if config_path is None:
        config_path = Path("config.yaml")
    if not config_path.exists():
        raise FileNotFoundError(f"找不到配置文件: {config_path}")
    raw = config_path.read_text(encoding="utf-8")
    expanded = _expand_env_vars(raw)
    data: dict[str, Any] = yaml.safe_load(expanded)
    return Settings.model_validate(data)
```

- [ ] **Step 2: 跑测试,确认通过**

Run: `uv run pytest tests/test_config.py -v`
Expected: 6 个测试全 PASS。

- [ ] **Step 3: 写 `config.example.yaml`**

```yaml
# ============== 全局 ==============
app:
  data_dir: ./data
  logs_dir: ./logs
  timezone: Asia/Shanghai

# ============== 路径(NAS 友好,跨平台) ==============
# Windows 示例: nas_root: "Z:/wxsp" 或 "\\\\server\\share\\wxsp"
paths:
  nas_root: /Volumes/NAS/wxsp
  video_search_root: "{nas_root}/videos"
  cover_search_root: "{nas_root}/covers"

# ============== 账号 ==============
accounts:
  account_a:
    display_name: "美食号"
    enabled: true
    daily_limit: 20
    user_data_dir: ./data/chrome-profiles/account_a
  account_b:
    display_name: "健身号"
    enabled: true
    daily_limit: 20
    user_data_dir: ./data/chrome-profiles/account_b
  account_c:
    display_name: "旅游号"
    enabled: true
    daily_limit: 20
    user_data_dir: ./data/chrome-profiles/account_c
  account_d:
    display_name: "搞笑号"
    enabled: true
    daily_limit: 20
    user_data_dir: ./data/chrome-profiles/account_d

# ============== 调度 ==============
scheduler:
  daily_cron_hour: 9
  daily_cron_minute: 0
  strategy: round-robin

# ============== 发布器 ==============
publisher:
  headless: false                # 视频号必须 false
  upload_timeout_seconds: 600
  step_pause_seconds: [1, 3]
  screenshot_on_error: true
  max_concurrent_accounts: 1

# ============== 飞书集成 ==============
feishu:
  enabled: true
  app_id: cli_xxxxxxxxxx
  app_secret: ${FEISHU_APP_SECRET}
  bitable:
    app_token: xxxxxxxxxxxx
    table_id: tblxxxxxxxx
  field_map:
    video_file: "视频文件"
    title: "标题"
    description: "描述"
    tags: "标签"
    cover: "封面文件"
    topic: "合集"
    original_claim: "原创"
    account: "账号"
    execute_date: "执行日期"
    publish_at: "定时发布时间"
    status: "状态"
    remote_url: "已发布链接"
    error_message: "错误信息"
  sync:
    write_back_enabled: true

# ============== 告警 ==============
monitoring:
  cookie_warn_days: 1.5
  notifiers:
    wecom:
      enabled: true
      webhook: ${WECOM_BOT_WEBHOOK}
  notify_on:
    - cookie_expired
    - cookie_warning
    - risk_control
    - task_failed
    - element_not_found
    - nas_unreachable

# ============== Web UI ==============
webui:
  host: 127.0.0.1
  port: 8765
  open_browser_on_start: true
```

- [ ] **Step 4: 验证 example 文件能被加载**

Run:
```bash
FEISHU_APP_SECRET=dummy WECOM_BOT_WEBHOOK=dummy uv run python -c "
from wxsp.config import load_settings
from pathlib import Path
s = load_settings(Path('config.example.yaml'))
print('loaded:', s.app.timezone, len(s.accounts), 'accounts')
"
```
Expected: `loaded: Asia/Shanghai 4 accounts`

- [ ] **Step 5: Commit**

```bash
git add wxsp/config.py config.example.yaml
git commit -m "feat(config): implement settings loader with env + nas_root expansion"
```

---

## Task 5: 写 CLI 命令骨架的失败测试

**Files:**
- Create: `tests/test_cli.py`

- [ ] **Step 1: 写 `tests/test_cli.py`**

```python
"""Tests for wxsp.cli command skeleton."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from wxsp.cli import app

runner = CliRunner()


@pytest.mark.parametrize(
    "command",
    ["login", "accounts", "doctor", "sync", "run", "status", "logs", "web"],
)
def test_top_level_command_exists(command: str) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert command in result.stdout


@pytest.mark.parametrize("sub", ["list", "pause", "resume"])
def test_accounts_subcommand_exists(sub: str) -> None:
    result = runner.invoke(app, ["accounts", "--help"])
    assert result.exit_code == 0
    assert sub in result.stdout


def test_run_supports_today_flag() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--today" in result.stdout
    assert "--daemon" in result.stdout
    assert "--task-id" in result.stdout
    assert "--dry-run" in result.stdout


def test_doctor_runs() -> None:
    result = runner.invoke(app, ["doctor"])
    # 骨架阶段允许打印"未实现"提示并以 exit code 0 退出
    assert result.exit_code == 0
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 全部 FAIL,因为 `wxsp.cli.app` 还没有任何命令(只有占位 docstring)。

- [ ] **Step 3: Commit**

```bash
git add tests/test_cli.py
git commit -m "test: add failing tests for CLI command skeleton"
```

---

## Task 6: 实现 cli.py 骨架让测试通过

**Files:**
- Modify: `wxsp/cli.py`

- [ ] **Step 1: 实现 `wxsp/cli.py`**

```python
"""Typer CLI 入口(M0 骨架,后续 milestone 逐步实现命令体)。"""
from __future__ import annotations

import typer

app = typer.Typer(
    name="wxsp",
    help="微信视频号自动发布工具",
    no_args_is_help=True,
    add_completion=False,
)

accounts_app = typer.Typer(help="账号管理", no_args_is_help=True)
app.add_typer(accounts_app, name="accounts")


def _not_implemented(name: str) -> None:
    typer.echo(f"[wxsp] 命令 `{name}` 还未实现(M0 骨架阶段)。")


@app.command("login")
def login(account_id: str = typer.Argument(..., help="账号 ID")) -> None:
    """扫码登录指定账号,刷新 Cookie(M2 实现)。"""
    _not_implemented(f"login {account_id}")


@accounts_app.command("list")
def accounts_list() -> None:
    """列出所有账号及其 Cookie 状态(M1 实现)。"""
    _not_implemented("accounts list")


@accounts_app.command("pause")
def accounts_pause(
    account_id: str = typer.Argument(...),
    hours: int = typer.Option(24, "--hours", "-h", help="暂停小时数"),
) -> None:
    """暂停指定账号(M1 实现)。"""
    _not_implemented(f"accounts pause {account_id} --hours {hours}")


@accounts_app.command("resume")
def accounts_resume(account_id: str = typer.Argument(...)) -> None:
    """恢复指定账号(M1 实现)。"""
    _not_implemented(f"accounts resume {account_id}")


@app.command("doctor")
def doctor() -> None:
    """健康检查:账号 / Cookie / NAS / 飞书 API(M2-M4 实现)。"""
    _not_implemented("doctor")


@app.command("sync")
def sync() -> None:
    """立即拉一次飞书 Bitable,不跑任务(M3 实现)。"""
    _not_implemented("sync")


@app.command("run")
def run(
    daemon: bool = typer.Option(False, "--daemon", help="启动 daemon(09:00 cron + FastAPI)"),
    today: bool = typer.Option(False, "--today", help="立即跑今天所有 pending 任务"),
    task_id: int | None = typer.Option(None, "--task-id", help="跑指定单条任务"),
    dry_run: bool = typer.Option(False, "--dry-run", help="发布步骤跑到点'发布'前停下"),
) -> None:
    """执行任务(M5-M6 实现)。"""
    _not_implemented(
        f"run --daemon={daemon} --today={today} --task-id={task_id} --dry-run={dry_run}"
    )


@app.command("status")
def status(date: str | None = typer.Option(None, "--date", help="日期 YYYY-MM-DD,默认今天")) -> None:
    """查看任务状态汇总(M1 实现)。"""
    _not_implemented(f"status --date {date}")


@app.command("logs")
def logs(
    task_id: int | None = typer.Option(None, "--task-id", help="按 task 过滤"),
    follow: bool = typer.Option(False, "--follow", "-f", help="持续 tail"),
) -> None:
    """查看日志(M7 实现)。"""
    _not_implemented(f"logs --task-id {task_id} --follow {follow}")


@app.command("web")
def web(port: int = typer.Option(8765, "--port", "-p", help="Web UI 端口")) -> None:
    """启动 Web UI(M8 实现)。"""
    _not_implemented(f"web --port {port}")


if __name__ == "__main__":
    app()
```

- [ ] **Step 2: 跑 CLI 测试,确认通过**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 全部 PASS(13 个测试:8 个 top-level 命令 + 3 个 accounts 子命令 + 1 个 run flags + 1 个 doctor exit code)。

- [ ] **Step 3: 跑全量测试**

Run: `uv run pytest -v`
Expected: 全部 PASS(config 6 + cli 13 = 19)。

- [ ] **Step 4: 验证 `wxsp --help`**

Run: `uv run wxsp --help`
Expected: 输出列出 `login`、`accounts`、`doctor`、`sync`、`run`、`status`、`logs`、`web` 全部 8 个命令。

- [ ] **Step 5: Commit**

```bash
git add wxsp/cli.py
git commit -m "feat(cli): scaffold all Typer commands with placeholder bodies"
```

---

## Task 7: pre-commit 配置 + 全绿验收

**Files:**
- Create: `.pre-commit-config.yaml`
- Create: `README.md`

- [ ] **Step 1: 写 `.pre-commit-config.yaml`**

```yaml
repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.6.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-yaml
      - id: check-added-large-files
        args: ["--maxkb=1024"]
      - id: check-merge-conflict

  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.4.10
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format

  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.10.0
    hooks:
      - id: mypy
        additional_dependencies:
          - pydantic>=2.6.0
          - pydantic-settings>=2.2.0
          - typer>=0.12.0
          - types-pyyaml>=6.0.12
        args: [--config-file=pyproject.toml]
        files: ^wxsp/
```

- [ ] **Step 2: 写最小 README.md**

```markdown
# wxsp — 微信视频号自动发布工具

本地运行的视频号自动发布工具:飞书 Bitable 为单一任务源,每天 09:00 cron 触发跑当日任务,Web UI 做运维控制台。

详见 [CLAUDE.md](CLAUDE.md) 和 [docs/superpowers/specs/](docs/superpowers/specs/)。
```

- [ ] **Step 3: 安装 pre-commit hook**

Run: `uv run pre-commit install`
Expected: 输出 `pre-commit installed at .git/hooks/pre-commit`。

- [ ] **Step 4: 全量 pre-commit 跑一遍,逐项修复直到全绿**

Run: `uv run pre-commit run --all-files`

Expected on first run: 可能有 trailing-whitespace / end-of-file-fixer 自动修复(允许),或 ruff format 改动(允许)。重跑直到全部 hook 显示 `Passed`。

若 mypy 报错:逐条阅读错误信息,在对应文件加类型注解或修正。**禁止用 `# type: ignore` 绕过**,除非问题来自第三方库且无法解决。

若 ruff lint 报错:逐条阅读修正(通常是导入顺序、未用变量等小问题)。

预期最终输出形如:
```
trim trailing whitespace.................................................Passed
fix end of files.........................................................Passed
check yaml...............................................................Passed
check for added large files..............................................Passed
check for merge conflicts................................................Passed
ruff.....................................................................Passed
ruff-format..............................................................Passed
mypy.....................................................................Passed
```

- [ ] **Step 5: 跑全量测试再确认一次**

Run: `uv run pytest -v`
Expected: 19 个测试全 PASS。

- [ ] **Step 6: 验收命令 1 ——`wxsp --help` 列全命令**

Run: `uv run wxsp --help`
Expected: 输出包含 `login`、`accounts`、`doctor`、`sync`、`run`、`status`、`logs`、`web` 全部 8 个命令。

- [ ] **Step 7: 验收命令 2 ——`pre-commit run --all-files` 全绿**

Run: `uv run pre-commit run --all-files`
Expected: 所有 hook `Passed`。

- [ ] **Step 8: Commit**

```bash
git add .pre-commit-config.yaml README.md
git diff --cached  # 检查是否有 pre-commit 自动 fmt 的改动一并暂存
git commit -m "chore: configure pre-commit hooks + minimal README"
```

若 Step 4 中 pre-commit 修复了某些 wxsp/ 或 tests/ 下的文件,这些改动会在 `git status` 中显示为未暂存(因为 pre-commit 不会自动 add)。把它们也加进来:

```bash
git add -u
git commit --amend --no-edit
```

(`--amend --no-edit` 把 fmt 改动合到刚才那个 commit;若 pre-commit hook 在 amend 时又改了文件,继续 `git add -u && git commit --amend --no-edit`,直到 hook 一次性通过。)

---

## Task 8: 最终验收 + M0 完结

- [ ] **Step 1: `git status` 确认干净**

Run: `git status`
Expected: `nothing to commit, working tree clean`。

- [ ] **Step 2: `git log` 确认 commit 历史合理**

Run: `git log --oneline`
Expected: 从根 commit `0d484eb` 到当前,有 ~6 个 M0 commit(pyproject、stub modules、test+impl config、test+impl cli、pre-commit+readme)。

- [ ] **Step 3: 验收线 ①:`uv run wxsp --help` 列出全部 8 个命令**

Run: `uv run wxsp --help`
Expected: stdout 含 `login`、`accounts`、`doctor`、`sync`、`run`、`status`、`logs`、`web`。

- [ ] **Step 4: 验收线 ②:`uv run pre-commit run --all-files` 全绿**

Run: `uv run pre-commit run --all-files`
Expected: 所有 hook `Passed`。

- [ ] **Step 5: 验收线 ③ (额外):`uv run pytest` 全 PASS**

Run: `uv run pytest -v`
Expected: 19 个测试全 PASS。

- [ ] **Step 6: 给用户演示 + 等"OK,下一个"**

按 CLAUDE.md "每个 milestone 完成必做" 节,M0 验收标准逐项打钩之后:
- 不直接进 M1
- 跟用户展示三条验收命令的输出
- 等用户说"OK,下一个"才进 M1

---

## Self-Review

完成最后一次审视,对照 M0 验收标准:

| Design / CLAUDE.md 要求 | 本计划覆盖 |
|------------------------|----------|
| `pyproject.toml`(uv) | Task 1 |
| ruff + mypy + pre-commit | Task 1 配置 + Task 7 hook |
| 扁平 `wxsp/*.py` 目录 | Task 2(15 个 stub 模块) |
| Typer CLI 骨架 | Task 5-6(8 个 top-level + 3 个 accounts 子命令) |
| `config.py`(Pydantic Settings + env + 模板) | Task 3-4 |
| `config.example.yaml` | Task 4 |
| `uv run wxsp --help` 列全命令 | Task 8 Step 3 |
| `uv run pre-commit run --all` 全绿 | Task 8 Step 4 |
| 跨平台:用 `pathlib.Path` | config.py 全用 `Path` ✓ |
| `${ENV_VAR}` 展开 | config.py `_expand_env_vars` ✓ |
| `{nas_root}` 模板 | config.py `_expand_nas_root_template` ✓ |

无 placeholder("TBD"、"implement later"等)。每个 Step 都给出**完整可执行内容**(代码 / 命令 / 期望输出)。类型 / 命名跨任务一致(`load_settings`、`Settings`、`app` 始终如一)。

