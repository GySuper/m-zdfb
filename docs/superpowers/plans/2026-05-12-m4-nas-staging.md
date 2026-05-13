# M4 NAS Staging + Doctor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `wxsp/nas.py` 加 `stage_to_tmp` / `cleanup_tmp` 两个函数(M5 publisher 准备用),在 `wxsp/errors.py` 定义 `NasUnreachable` 异常,在 `wxsp/doctor.py` 加 `check_nas` 健康检查,并把 `wxsp doctor` CLI 的输出补上 NAS section。

**Architecture:** 纯函数 + 标量数据类。`stage_to_tmp` 用 symlink(零拷贝);任意 OSError 翻译为 `NasUnreachable` 让 M5 retry 装饰器统一处理。`cleanup_tmp` 用 `shutil.rmtree`,FileNotFoundError 静默实现幂等。`check_nas` 只读 stat,不写 marker。

**Tech Stack:** pathlib + shutil(标准库) + pytest tmp_path fixture + Typer CliRunner。**不引入新依赖**。

**Spec:** [docs/superpowers/specs/2026-05-12-m4-nas-staging.md](../specs/2026-05-12-m4-nas-staging.md)

---

## 项目背景速读(subagent 必看)

新接手的 subagent 在动手前必须了解:

- **项目根目录**:`/Users/zhaoguangyu/wechat-sph-upload`
- **包管理器**:`uv`,所有命令前缀 `uv run`(如 `uv run pytest`、`uv run pre-commit run --all-files`)
- **Python 风格**:`from __future__ import annotations`,所有公开函数 keyword-only 参数(`*` 分隔),pathlib 不允许字符串拼路径
- **TDD 强制**:每个交付物先 RED commit(失败测试)再 GREEN commit(实现),用 Conventional Commits。**禁止 `--no-verify`** 跳过 pre-commit
- **测试风格**:看 `tests/test_nas.py`(纯函数 + tmp_path)和 `tests/test_doctor.py`(`from wxsp.doctor import X` 写在测试函数内,模仿这个风格)
- **doctor 命名风格**:NamedTuple 数据类(参考已有的 `CookieStatusRow`)
- **错误翻译模式**:见 spec §4。validator 阶段 `find_video` 找不到走 `FileNotFoundError`(M3 已落地,不动),publisher 阶段 `stage_to_tmp` 任何 OSError 翻译为 `NasUnreachable`
- **不要改的文件**:`wxsp/feishu.py`、`wxsp/validator.py`、`wxsp/cli.py::sync`、`wxsp/models.py`、`wxsp/db.py`、`wxsp/nas.py::find_video/find_cover`
- **当前 git 状态**:HEAD 是 `735ac16 docs(m4): add spec for NAS staging + doctor`,工作树干净
- **Python 版本**:3.10+,使用 `X | Y` 联合类型语法

---

## 文件结构总览

| 文件 | 改动 | 责任 |
|------|------|------|
| `wxsp/errors.py` | 新增 `NasUnreachable` 类 | 项目统一错误类型表 |
| `wxsp/nas.py` | 在文件末尾追加 `stage_to_tmp` + `cleanup_tmp` | M5 publisher 取 NAS 视频的入口 |
| `wxsp/doctor.py` | 新增 `NasCheckRow` + `check_nas` | NAS 健康度的领域函数 |
| `wxsp/cli.py` | 改 `doctor` 命令,补 NAS 输出 + 任一失败 exit 1 | 用户运维命令 |
| `tests/test_nas.py` | 追加 stage/cleanup 测试 | 单元测试 |
| `tests/test_doctor.py` | 追加 check_nas 测试 | 单元测试 |
| `tests/test_cli_doctor.py` | 追加 NAS section 测试 | CLI 集成测试 |

任务总数:6 个 TDD 任务 + 1 个验收任务。预计 2-3 小时。

---

## Task 1: 定义 NasUnreachable 异常

**Files:**
- Modify: `wxsp/errors.py`(目前只有 docstring,加新类)
- Create: `tests/test_errors.py`

**Context:** 这是 M4 最简单的一步,作为后续 stage_to_tmp 测试的依赖。M5 retry 装饰器会针对此异常做 5 次指数退避。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_errors.py`:

```python
"""errors module unit tests."""

from __future__ import annotations


def test_nas_unreachable_is_exception_subclass() -> None:
    from wxsp.errors import NasUnreachable

    assert issubclass(NasUnreachable, Exception)


def test_nas_unreachable_can_be_raised_and_caught() -> None:
    from wxsp.errors import NasUnreachable

    try:
        raise NasUnreachable("stage failed")
    except NasUnreachable as exc:
        assert str(exc) == "stage failed"


def test_nas_unreachable_preserves_cause_via_from() -> None:
    """exception chaining via `raise ... from ...` 保留原始 OSError。"""
    from wxsp.errors import NasUnreachable

    original = PermissionError("permission denied")
    try:
        raise NasUnreachable("translated") from original
    except NasUnreachable as exc:
        assert exc.__cause__ is original
```

- [ ] **Step 2: 运行测试,确认失败**

```bash
uv run pytest tests/test_errors.py -v
```

Expected: 三个测试都 FAIL(`ImportError: cannot import name 'NasUnreachable' from 'wxsp.errors'`)。

- [ ] **Step 3: RED commit**

```bash
git add tests/test_errors.py
git commit -m "test: add failing tests for NasUnreachable exception"
```

- [ ] **Step 4: 写最小实现**

修改 `wxsp/errors.py` 整体替换为:

```python
"""错误类型 + 分类(M4 起逐步填充;M5 加重试装饰器)。"""

from __future__ import annotations


class NasUnreachable(Exception):
    """NAS 文件操作失败。

    在 nas.stage_to_tmp 抛任意 OSError(FileNotFoundError / PermissionError /
    TimeoutError / ConnectionError / 其他 OSError 子类)时统一翻译为此类型。
    M5 retry 装饰器对此异常做 5 次指数退避(5/10/20/40/80s)。
    """
```

- [ ] **Step 5: 运行测试,确认通过 + 跑 pre-commit**

```bash
uv run pytest tests/test_errors.py -v
uv run pre-commit run --files wxsp/errors.py tests/test_errors.py
```

Expected: 测试 3 passed;pre-commit 全绿(ruff / ruff-format / mypy 都过)。

- [ ] **Step 6: GREEN commit**

```bash
git add wxsp/errors.py tests/test_errors.py
git commit -m "feat(errors): define NasUnreachable exception"
```

---

## Task 2: stage_to_tmp happy path + 覆盖式

**Files:**
- Modify: `wxsp/nas.py`(在文件末尾追加,不动 `find_video` / `find_cover`)
- Modify: `tests/test_nas.py`(在文件末尾追加,不动现有 5 个测试)

**Context:** 这是 M4 主要交付物的第一步。函数签名 `stage_to_tmp(src, *, task_id, tmp_root) -> Path`,task_id 是 int(`Task.id` 是 int)。symlink 用 `Path.symlink_to`。同 task_id 重跑时旧 symlink 要被覆盖(`if link_path.exists() or link_path.is_symlink(): link_path.unlink()` 再 symlink_to)。

**spec 引用**:[2026-05-12-m4-nas-staging.md §3.1](../specs/2026-05-12-m4-nas-staging.md)

- [ ] **Step 1: 写失败测试(3 个 case)**

在 `tests/test_nas.py` 末尾追加(注意保留文件开头 `from wxsp.nas import find_cover, find_video`,这里追加 `stage_to_tmp`):

```python
# ============== stage_to_tmp happy path + 覆盖式 ==============


def test_stage_to_tmp_creates_symlink_pointing_to_src(tmp_path: Path) -> None:
    from wxsp.nas import stage_to_tmp

    src = tmp_path / "videos" / "国庆01.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"fake video bytes")

    tmp_root = tmp_path / "tmp"
    link = stage_to_tmp(src, task_id=42, tmp_root=tmp_root)

    assert link == tmp_root / "42" / "国庆01.mp4"
    assert link.is_symlink()
    assert link.resolve() == src.resolve()
    # 跟读 src 一样能读到 fake bytes
    assert link.read_bytes() == b"fake video bytes"


def test_stage_to_tmp_creates_parent_dirs(tmp_path: Path) -> None:
    """tmp_root 本身不存在时也要自动 mkdir(parents=True)。"""
    from wxsp.nas import stage_to_tmp

    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")

    tmp_root = tmp_path / "does" / "not" / "exist"
    link = stage_to_tmp(src, task_id=1, tmp_root=tmp_root)

    assert link.is_symlink()
    assert link.parent == tmp_root / "1"
    assert link.parent.is_dir()


def test_stage_to_tmp_overwrites_existing_symlink(tmp_path: Path) -> None:
    """同 task_id + 同 src.name 重跑时旧 symlink 被覆盖,不报错;指向变新。"""
    from wxsp.nas import stage_to_tmp

    src_a = tmp_path / "first" / "vid.mp4"
    src_a.parent.mkdir()
    src_a.write_bytes(b"a")
    src_b = tmp_path / "second" / "vid.mp4"
    src_b.parent.mkdir()
    src_b.write_bytes(b"b")

    tmp_root = tmp_path / "tmp"
    link1 = stage_to_tmp(src_a, task_id=7, tmp_root=tmp_root)
    link2 = stage_to_tmp(src_b, task_id=7, tmp_root=tmp_root)

    assert link1 == link2 == tmp_root / "7" / "vid.mp4"
    assert link2.resolve() == src_b.resolve()
    assert link2.read_bytes() == b"b"


def test_stage_to_tmp_preserves_filename_with_spaces_and_unicode(tmp_path: Path) -> None:
    from wxsp.nas import stage_to_tmp

    src = tmp_path / "国庆 短片 01.mov"
    src.write_bytes(b"x")

    link = stage_to_tmp(src, task_id=3, tmp_root=tmp_path / "tmp")

    assert link.name == "国庆 短片 01.mov"
    assert link.is_symlink()
```

- [ ] **Step 2: 运行测试,确认失败**

```bash
uv run pytest tests/test_nas.py -v
```

Expected: 4 个新测试都 FAIL(`ImportError: cannot import name 'stage_to_tmp' from 'wxsp.nas'`);原有 5 个 `find_*` 测试仍 PASS。

- [ ] **Step 3: RED commit**

```bash
git add tests/test_nas.py
git commit -m "test: add failing tests for nas.stage_to_tmp happy path"
```

- [ ] **Step 4: 写最小实现**

在 `wxsp/nas.py` 末尾追加(顶部 import 区也要追加 `from wxsp.errors import NasUnreachable`,但这一步还用不上,留到 Task 3 一起处理。本 Task **只写不抛错的版本**,先让 happy path 过):

把文件开头 import 区改成:

```python
"""NAS 文件检索 + stage_to_tmp + cleanup_tmp(M3 含 find_*,M4 补 stage/cleanup)。"""

from __future__ import annotations

from pathlib import Path

from wxsp.errors import NasUnreachable
```

在文件末尾追加:

```python
def stage_to_tmp(src: Path, *, task_id: int, tmp_root: Path) -> Path:
    """在 tmp_root/{task_id}/ 下建 symlink 指向 src,返回 symlink 路径。

    - tmp_root/{task_id}/ 不存在则自动 mkdir(parents=True, exist_ok=True)
    - symlink 名等于 src.name(保留原文件名,便于 debug)
    - 已存在同名 symlink/文件 → 先 unlink 再 symlink_to(覆盖式;同 task_id 重跑安全)
    - 任意 OSError → NasUnreachable(M5 retry 装饰器统一处理)
    """
    try:
        stage_dir = tmp_root / str(task_id)
        stage_dir.mkdir(parents=True, exist_ok=True)
        link_path = stage_dir / src.name
        if link_path.is_symlink() or link_path.exists():
            link_path.unlink()
        link_path.symlink_to(src)
        return link_path
    except OSError as exc:
        raise NasUnreachable(f"stage_to_tmp 失败 src={src!s} task_id={task_id}: {exc}") from exc
```

注:虽然 Task 2 happy path 不会触发 OSError 分支,但实现一次性写完是合理的(YAGNI 之外的另一条准则是"不要写半成品")。Task 3 会专门测错误翻译。

- [ ] **Step 5: 运行测试,确认通过 + 跑 pre-commit**

```bash
uv run pytest tests/test_nas.py -v
uv run pre-commit run --files wxsp/nas.py tests/test_nas.py
```

Expected: 9 个测试全 PASS(原 5 + 新 4);pre-commit 全绿。

- [ ] **Step 6: GREEN commit**

```bash
git add wxsp/nas.py tests/test_nas.py
git commit -m "feat(nas): add stage_to_tmp with symlink + overwrite"
```

---

## Task 3: stage_to_tmp 错误翻译

**Files:**
- Modify: `tests/test_nas.py`(在 Task 2 追加内容的下方再追加)

**Context:** Task 2 的实现已经包含 `try / except OSError → NasUnreachable`,但还没测试覆盖。这一步专门测错误翻译路径。注意:这里测的是 stage_to_tmp 的内部 OSError 翻译,与 validator 阶段 find_video 抛 FileNotFoundError 是不同语义。

- [ ] **Step 1: 写失败测试**

在 `tests/test_nas.py` 末尾追加:

```python
# ============== stage_to_tmp 错误翻译 ==============


def test_stage_to_tmp_translates_oserror_to_nas_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模拟 NAS 抖动:symlink_to 抛 PermissionError → 翻译为 NasUnreachable。"""
    from wxsp.errors import NasUnreachable
    from wxsp.nas import stage_to_tmp

    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")

    def boom(self: Path, target: Path | str, target_is_directory: bool = False) -> None:
        raise PermissionError("simulated NAS permission error")

    monkeypatch.setattr(Path, "symlink_to", boom)

    with pytest.raises(NasUnreachable) as exc_info:
        stage_to_tmp(src, task_id=99, tmp_root=tmp_path / "tmp")

    # 原始 OSError 通过 __cause__ 保留
    assert isinstance(exc_info.value.__cause__, PermissionError)
    assert "simulated NAS permission error" in str(exc_info.value.__cause__)


def test_stage_to_tmp_translates_mkdir_oserror_to_nas_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mkdir 阶段抛 OSError 也要被翻译(不只是 symlink_to 阶段)。"""
    from wxsp.errors import NasUnreachable
    from wxsp.nas import stage_to_tmp

    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")

    original_mkdir = Path.mkdir

    def crashing_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        if "tmp" in str(self):
            raise OSError("simulated disk full")
        original_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", crashing_mkdir)

    with pytest.raises(NasUnreachable):
        stage_to_tmp(src, task_id=1, tmp_root=tmp_path / "tmp")
```

- [ ] **Step 2: 运行测试,确认 happy path 已过 + 错误翻译测试**

```bash
uv run pytest tests/test_nas.py -v
```

Expected: 11 个测试全 PASS(原 5 + Task 2 的 4 + Task 3 的 2)。

注:因为 Task 2 已经一次性写出了 try/except 实现,Task 3 的 2 个测试应该直接通过 —— 这种情况下不需要单独的 RED commit,Task 3 直接合并到一个测试增补 commit。

- [ ] **Step 3: 跑 pre-commit**

```bash
uv run pre-commit run --files tests/test_nas.py
```

Expected: 全绿。

- [ ] **Step 4: 提交**

```bash
git add tests/test_nas.py
git commit -m "test(nas): cover stage_to_tmp OSError → NasUnreachable translation"
```

---

## Task 4: cleanup_tmp

**Files:**
- Modify: `wxsp/nas.py`(再追加一个函数)
- Modify: `tests/test_nas.py`(再追加测试)

**Context:** cleanup_tmp 不做错误翻译——它清理的是本地 tmp 目录,跟 NAS 没关系,本地 IO 失败抛原始 OSError 即可。FileNotFoundError 静默实现幂等(同 task_id 多次 cleanup 不报错)。

- [ ] **Step 1: 写失败测试**

在 `tests/test_nas.py` 末尾追加:

```python
# ============== cleanup_tmp ==============


def test_cleanup_tmp_removes_task_dir(tmp_path: Path) -> None:
    from wxsp.nas import cleanup_tmp, stage_to_tmp

    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    tmp_root = tmp_path / "tmp"

    stage_to_tmp(src, task_id=42, tmp_root=tmp_root)
    assert (tmp_root / "42").is_dir()

    cleanup_tmp(task_id=42, tmp_root=tmp_root)
    assert not (tmp_root / "42").exists()


def test_cleanup_tmp_is_idempotent_when_dir_missing(tmp_path: Path) -> None:
    """目录不存在 → 静默返回,不抛异常。"""
    from wxsp.nas import cleanup_tmp

    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    # 调用不存在的 task_id 不抛
    cleanup_tmp(task_id=999, tmp_root=tmp_root)


def test_cleanup_tmp_does_not_touch_sibling_task_dirs(tmp_path: Path) -> None:
    """只删自己的 task_id 目录,不动其他 task 的 stage。"""
    from wxsp.nas import cleanup_tmp, stage_to_tmp

    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    tmp_root = tmp_path / "tmp"

    stage_to_tmp(src, task_id=1, tmp_root=tmp_root)
    stage_to_tmp(src, task_id=2, tmp_root=tmp_root)

    cleanup_tmp(task_id=1, tmp_root=tmp_root)

    assert not (tmp_root / "1").exists()
    assert (tmp_root / "2").is_dir()  # 兄弟没动
```

- [ ] **Step 2: 运行测试,确认失败**

```bash
uv run pytest tests/test_nas.py -v
```

Expected: 3 个新测试 FAIL(`ImportError: cannot import name 'cleanup_tmp' from 'wxsp.nas'`);其他 11 个仍 PASS。

- [ ] **Step 3: RED commit**

```bash
git add tests/test_nas.py
git commit -m "test: add failing tests for nas.cleanup_tmp"
```

- [ ] **Step 4: 写最小实现**

修改 `wxsp/nas.py`,顶部 import 区追加 `import shutil`:

```python
"""NAS 文件检索 + stage_to_tmp + cleanup_tmp(M3 含 find_*,M4 补 stage/cleanup)。"""

from __future__ import annotations

import shutil
from pathlib import Path

from wxsp.errors import NasUnreachable
```

在文件末尾追加(`stage_to_tmp` 之后):

```python
def cleanup_tmp(*, task_id: int, tmp_root: Path) -> None:
    """删除 tmp_root/{task_id}/ 目录。

    - 目录不存在 → 静默返回(幂等,同 task_id 多次清理安全)
    - 其他 OSError → 抛出原始异常(本地文件系统问题,不翻译为 NasUnreachable)
    """
    stage_dir = tmp_root / str(task_id)
    try:
        shutil.rmtree(stage_dir)
    except FileNotFoundError:
        pass  # 幂等:已经被清过了
```

- [ ] **Step 5: 运行测试,确认通过 + 跑 pre-commit**

```bash
uv run pytest tests/test_nas.py -v
uv run pre-commit run --files wxsp/nas.py tests/test_nas.py
```

Expected: 14 个测试全 PASS;pre-commit 全绿。

- [ ] **Step 6: GREEN commit**

```bash
git add wxsp/nas.py
git commit -m "feat(nas): add cleanup_tmp with idempotent FileNotFoundError handling"
```

---

## Task 5: doctor.check_nas

**Files:**
- Modify: `wxsp/doctor.py`(在文件末尾追加,不动 `record_cookie_check` / `refresh_cookie_status` / `CookieStatusRow`)
- Modify: `tests/test_doctor.py`(在文件末尾追加)

**Context:** `check_nas` 返回固定 2 行 `NasCheckRow`(video → cover 顺序),每行带 `ok: bool` 和 `detail: str`。**只读 stat,不抛异常**(失败信息塞 `detail`)。

Settings 对象怎么构造?现有测试里没有共享 fixture,我们直接手工 build 一个最小的 Settings(类似 `tests/test_validator.py` 里 `_make_settings()`,但 check_nas 只用 `config.paths`,所以我们只需要 `PathsConfig`)。

**关键设计选择**:`check_nas` 签名传整个 `Settings` 还是只传 `PathsConfig`?**传 Settings** 跟现有 `refresh_cookie_status` 用 session 而非 Engine 的"接受领域对象"风格一致,且 CLI 调用方拿到的就是 Settings,接口对齐自然。

- [ ] **Step 1: 写失败测试**

在 `tests/test_doctor.py` 末尾追加:

```python
# ============== check_nas ==============


def _make_settings(video_root: Path, cover_root: Path) -> Any:
    """构造最小 Settings,只填 check_nas 用到的 paths.{video,cover}_search_root。"""
    from wxsp.config import (
        AppConfig,
        FeishuBitableConfig,
        FeishuConfig,
        MonitoringConfig,
        NotifiersConfig,
        PathsConfig,
        PublisherConfig,
        SchedulerConfig,
        Settings,
        WebUIConfig,
        WecomNotifierConfig,
    )

    return Settings(
        app=AppConfig(data_dir=Path("/tmp/d"), logs_dir=Path("/tmp/l"), timezone="Asia/Shanghai"),
        paths=PathsConfig(
            nas_root=video_root.parent,
            video_search_root=video_root,
            cover_search_root=cover_root,
        ),
        accounts={},
        scheduler=SchedulerConfig(),
        publisher=PublisherConfig(),
        feishu=FeishuConfig(
            enabled=False,
            app_id="x",
            app_secret="x",
            bitable=FeishuBitableConfig(app_token="x", table_id="x"),
        ),
        monitoring=MonitoringConfig(
            notifiers=NotifiersConfig(wecom=WecomNotifierConfig(enabled=False, webhook="")),
        ),
        webui=WebUIConfig(),
    )


def test_check_nas_both_paths_exist_and_are_dirs(tmp_path: Path) -> None:
    from wxsp.doctor import check_nas

    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    settings = _make_settings(video_root, cover_root)

    rows = check_nas(settings)

    assert len(rows) == 2
    assert [(r.label, r.ok) for r in rows] == [
        ("video_search_root", True),
        ("cover_search_root", True),
    ]
    assert rows[0].path == video_root
    assert rows[1].path == cover_root


def test_check_nas_video_root_missing(tmp_path: Path) -> None:
    from wxsp.doctor import check_nas

    video_root = tmp_path / "videos"  # 故意不创建
    cover_root = tmp_path / "covers"
    cover_root.mkdir()
    settings = _make_settings(video_root, cover_root)

    rows = check_nas(settings)

    assert rows[0].label == "video_search_root"
    assert rows[0].ok is False
    assert "不存在" in rows[0].detail
    assert rows[1].ok is True


def test_check_nas_path_is_file_not_dir(tmp_path: Path) -> None:
    from wxsp.doctor import check_nas

    video_root = tmp_path / "videos"
    video_root.mkdir()
    cover_root = tmp_path / "covers_file"  # 故意建成文件
    cover_root.write_text("oops")
    settings = _make_settings(video_root, cover_root)

    rows = check_nas(settings)

    assert rows[0].ok is True
    assert rows[1].ok is False
    assert "不是目录" in rows[1].detail


def test_check_nas_both_missing(tmp_path: Path) -> None:
    from wxsp.doctor import check_nas

    settings = _make_settings(tmp_path / "v", tmp_path / "c")  # 都不存在
    rows = check_nas(settings)

    assert all(not r.ok for r in rows)
```

- [ ] **Step 2: 运行测试,确认失败**

```bash
uv run pytest tests/test_doctor.py -v
```

Expected: 4 个新测试 FAIL(`ImportError: cannot import name 'check_nas' from 'wxsp.doctor'`)。现有 cookie 测试全 PASS。

- [ ] **Step 3: RED commit**

```bash
git add tests/test_doctor.py
git commit -m "test: add failing tests for doctor.check_nas"
```

- [ ] **Step 4: 写最小实现**

修改 `wxsp/doctor.py`。顶部 import 区追加(注意保留现有 import):

```python
"""健康检查命令实现(M2 cookie,M4 加 NAS)。

`record_cookie_check` 是写入 cookie 状态的唯一入口,被 `wxsp login` 和
`refresh_cookie_status` 共用。与 `db.transition_task` 一致:**不 commit**,
让调用方决定事务边界(login 成功后回写 + doctor 批量刷新都受益)。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from sqlmodel import Session, select

from wxsp.config import Settings
from wxsp.models import (
    COOKIE_STATUS_EXPIRED,
    COOKIE_STATUS_OK,
    COOKIE_STATUS_UNKNOWN,
    Account,
)
```

在文件末尾追加:

```python
# ============== NAS 健康检查(M4)==============


class NasCheckRow(NamedTuple):
    """`check_nas` 返回行,CLI 输出层用。"""

    path: Path
    label: str  # "video_search_root" | "cover_search_root"
    ok: bool
    detail: str


def check_nas(config: Settings) -> list[NasCheckRow]:
    """检查 video_search_root + cover_search_root 是否存在且为目录。

    无 IO 副作用,只读 stat。返回固定 2 行,按 video → cover 顺序。
    任何路径都不抛异常,失败信息塞 NasCheckRow.detail。
    """
    targets: list[tuple[str, Path]] = [
        ("video_search_root", config.paths.video_search_root),
        ("cover_search_root", config.paths.cover_search_root),
    ]
    rows: list[NasCheckRow] = []
    for label, path in targets:
        if path.is_dir():
            rows.append(NasCheckRow(path=path, label=label, ok=True, detail=f"OK ({path})"))
        elif path.exists():
            rows.append(
                NasCheckRow(path=path, label=label, ok=False, detail=f"不是目录: {path}")
            )
        else:
            rows.append(NasCheckRow(path=path, label=label, ok=False, detail=f"不存在: {path}"))
    return rows
```

- [ ] **Step 5: 运行测试,确认通过 + 跑 pre-commit**

```bash
uv run pytest tests/test_doctor.py -v
uv run pre-commit run --files wxsp/doctor.py tests/test_doctor.py
```

Expected: 全部 PASS;pre-commit 全绿。

- [ ] **Step 6: GREEN commit**

```bash
git add wxsp/doctor.py tests/test_doctor.py
git commit -m "feat(doctor): add check_nas for video/cover search roots"
```

---

## Task 6: CLI `wxsp doctor` 补 NAS section

**Files:**
- Modify: `wxsp/cli.py:161-180`(改 `doctor` 函数,在 cookie 输出后追加 NAS section)
- Modify: `tests/test_cli_doctor.py`(追加测试)

**Context:** 现有 `doctor` 函数只跑 cookie 检查。改造后流程:
1. 先 cookie 检查(逻辑不变)
2. 后 NAS 检查:`load_settings()` → `check_nas(settings)` → 打印 + 任一失败 exit 1
3. 退出码语义:cookie expired 退 1(已有);NAS 任一失败也退 1(新增)

测试要注意:CliRunner 没有 cwd 控制权,`load_settings()` 默认读 `config.yaml`,我们不希望测试用户家目录里的 `config.yaml`。**用 monkeypatch 替换 `cli` 模块里 `load_settings`** 注入测试用的 Settings(模仿现有的 `monkeypatch.setattr(cli_module, "check_cookie", ...)` 风格)。

- [ ] **Step 1: 写失败测试**

在 `tests/test_cli_doctor.py` 末尾追加:

```python
# ============== NAS section ==============


def _make_settings_for_cli(video_root: Path, cover_root: Path) -> Any:
    """复用 test_doctor 里的最小 Settings 构造。"""
    from wxsp.config import (
        AppConfig,
        FeishuBitableConfig,
        FeishuConfig,
        MonitoringConfig,
        NotifiersConfig,
        PathsConfig,
        PublisherConfig,
        SchedulerConfig,
        Settings,
        WebUIConfig,
        WecomNotifierConfig,
    )

    return Settings(
        app=AppConfig(data_dir=Path("/tmp/d"), logs_dir=Path("/tmp/l"), timezone="Asia/Shanghai"),
        paths=PathsConfig(
            nas_root=video_root.parent,
            video_search_root=video_root,
            cover_search_root=cover_root,
        ),
        accounts={},
        scheduler=SchedulerConfig(),
        publisher=PublisherConfig(),
        feishu=FeishuConfig(
            enabled=False,
            app_id="x",
            app_secret="x",
            bitable=FeishuBitableConfig(app_token="x", table_id="x"),
        ),
        monitoring=MonitoringConfig(
            notifiers=NotifiersConfig(wecom=WecomNotifierConfig(enabled=False, webhook="")),
        ),
        webui=WebUIConfig(),
    )


def test_doctor_prints_nas_section_when_all_ok(
    db_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _add_account(db_env, "account_a")

    def fake_check(path: Path, *, timeout_ms: int) -> bool:
        return True

    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    settings = _make_settings_for_cli(video_root, cover_root)

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", fake_check)
    monkeypatch.setattr(cli_module, "load_settings", lambda: settings)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "NAS" in result.output
    assert "video_search_root" in result.output
    assert "cover_search_root" in result.output


def test_doctor_exits_1_when_nas_path_missing(
    db_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _add_account(db_env, "account_a")

    def fake_check(path: Path, *, timeout_ms: int) -> bool:
        return True

    video_root = tmp_path / "missing_videos"  # 故意不 mkdir
    cover_root = tmp_path / "covers"
    cover_root.mkdir()
    settings = _make_settings_for_cli(video_root, cover_root)

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", fake_check)
    monkeypatch.setattr(cli_module, "load_settings", lambda: settings)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "不存在" in result.output


def test_doctor_nas_section_runs_even_without_accounts(
    db_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """没账号时早 return 不应该跳过 NAS 检查 —— NAS 是独立诊断项。"""
    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    settings = _make_settings_for_cli(video_root, cover_root)

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "load_settings", lambda: settings)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    # 即便没账号,NAS section 仍然要出现
    assert "无账号" in result.output
    assert "NAS" in result.output
```

测试文件顶部需要确保有 `from typing import Any` 的 import。如果当前没有,加上。检查现有 `tests/test_cli_doctor.py` 顶部 imports 并补全。

- [ ] **Step 2: 运行测试,确认失败**

```bash
uv run pytest tests/test_cli_doctor.py -v
```

Expected: 3 个新测试 FAIL(原因可能多种,关键是要看到 `NAS` 字符串没出现 / exit_code != 1)。

- [ ] **Step 3: RED commit**

```bash
git add tests/test_cli_doctor.py
git commit -m "test: add failing tests for cli doctor NAS section"
```

- [ ] **Step 4: 改 CLI doctor 实现**

修改 `wxsp/cli.py:19` 的 import:

```python
from wxsp.doctor import check_nas, record_cookie_check, refresh_cookie_status
```

修改 `wxsp/cli.py:161-180` 的 `doctor` 函数,整体替换为:

```python
@app.command("doctor")
def doctor() -> None:
    """健康检查:账号 / Cookie + NAS(M2 cookie,M4 NAS)。"""

    # cookie_checker 注入点:生产用 wxsp.browser.check_cookie(打开浏览器);测试可 monkeypatch
    def cookie_checker(user_data_dir: Path) -> bool:
        return check_cookie(user_data_dir, timeout_ms=15_000)

    cookie_failed = False

    with _open_session() as session:
        # 先看有没有账号 —— 没有就提示,但不 return,继续跑 NAS section
        if not session.exec(select(Account)).first():
            typer.echo("[wxsp] 无账号。先 `wxsp accounts add`,再 `wxsp login <id>` 扫码。")
        else:
            rows = refresh_cookie_status(session, cookie_checker=cookie_checker)
            typer.echo(f"{'ID':<14} {'Cookie':<10} {'最后活跃':<20}")
            for row in rows:
                last_active = (
                    row.last_active_at.strftime("%Y-%m-%d %H:%M") if row.last_active_at else "-"
                )
                typer.echo(f"{row.account_id:<14} {row.status:<10} {last_active:<20}")
                if row.status != "ok":
                    cookie_failed = True

    # NAS section
    typer.echo("")  # 空行分隔
    typer.echo("NAS:")
    settings = load_settings()
    nas_rows = check_nas(settings)
    nas_failed = False
    for nas_row in nas_rows:
        mark = "✅" if nas_row.ok else "❌"
        typer.echo(f"  {mark} {nas_row.label:<20} {nas_row.detail}")
        if not nas_row.ok:
            nas_failed = True

    if cookie_failed or nas_failed:
        raise typer.Exit(code=1)
```

注:这一步把"无账号时早 return"改成了"无账号时给提示但继续跑 NAS"。这是行为改动,但是设计上 NAS 跟账号是两个独立诊断项,合并展示更合理。这一行为改动**已经被 Task 6 的 `test_doctor_nas_section_runs_even_without_accounts` 测试 lock 住**。

⚠️ **注意现有测试 `test_doctor_no_accounts_shows_hint`**(test_cli_doctor.py:37-43)只断言 `"无账号" in result.output` 和 `exit_code == 0`。改动后无账号时 NAS section 还会跑,**如果 NAS 路径不存在(测试没 mock),exit_code 会变成 1**。需要给 `test_doctor_no_accounts_shows_hint` 也 monkeypatch `load_settings` 注入一个 NAS 都 OK 的 Settings,否则它会 break。

修改 `test_doctor_no_accounts_shows_hint`:

```python
def test_doctor_no_accounts_shows_hint(
    db_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    settings = _make_settings_for_cli(video_root, cover_root)

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "load_settings", lambda: settings)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "无账号" in result.output
```

另外现有的 `test_doctor_lists_each_account_with_status` / `test_doctor_persists_cookie_status_to_db` / `test_doctor_continues_after_one_account_browser_crash` 三个测试也会 break(因为它们没 mock load_settings,跑到 NAS section 会读真实 config.yaml 失败或 path 不存在)。三个都得加上 `monkeypatch.setattr(cli_module, "load_settings", lambda: settings)`。

请把这 3 个测试也补上 `_make_settings_for_cli` 注入(具体改法见 Step 4 末尾追加)。

- [ ] **Step 5: 修补现有 3 个测试**

下面三个现有测试都要加 `tmp_path` 参数 + `_make_settings_for_cli` + `monkeypatch.setattr(cli_module, "load_settings", lambda: settings)`,以保证 NAS section 用注入的 settings(否则会读真实 `config.yaml` 失败)。

**改动 1 — `test_doctor_lists_each_account_with_status`(test_cli_doctor.py:46-78)**:整体替换为:

```python
def test_doctor_lists_each_account_with_status(
    db_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _add_account(db_env, "account_a")
    _add_account(db_env, "account_b")

    calls: list[Path] = []

    def fake_check(path: Path, *, timeout_ms: int) -> bool:
        calls.append(path)
        assert timeout_ms <= 30_000, "doctor should use a short timeout (already-logged-in path)"
        return path.name == "account_a"  # only A is logged in

    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    settings = _make_settings_for_cli(video_root, cover_root)

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", fake_check)
    monkeypatch.setattr(cli_module, "load_settings", lambda: settings)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    # cookie account_b 是 expired,新 doctor 因为 cookie_failed 退 1。
    # 这个测试关心的是输出内容,不是退码,所以放宽 exit_code 断言。
    assert "account_a" in result.output
    assert "account_b" in result.output
    assert "ok" in result.output
    assert "expired" in result.output

    assert sorted(str(p) for p in calls) == [
        "/tmp/profiles/account_a",
        "/tmp/profiles/account_b",
    ]
```

**关键 diff**:删掉 `assert result.exit_code == 0, result.output`,因为 expired cookie 现在退 1。

**改动 2 — `test_doctor_persists_cookie_status_to_db`(test_cli_doctor.py:79-99)**:整体替换为:

```python
def test_doctor_persists_cookie_status_to_db(
    db_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _add_account(db_env, "account_a")

    def fake_check(path: Path, *, timeout_ms: int) -> bool:
        return True

    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    settings = _make_settings_for_cli(video_root, cover_root)

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", fake_check)
    monkeypatch.setattr(cli_module, "load_settings", lambda: settings)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0  # cookie OK + NAS OK → 退 0

    engine = get_engine(db_env)
    with Session(engine) as session:
        account = session.get(Account, "account_a")
        assert account is not None
        assert account.cookie_status == "ok"
        assert account.cookie_last_active_at is not None
```

**关键 diff**:加 tmp_path + settings monkeypatch,exit_code == 0 保留不变。

**改动 3 — `test_doctor_continues_after_one_account_browser_crash`(test_cli_doctor.py:101-126)**:整体替换为:

```python
def test_doctor_continues_after_one_account_browser_crash(
    db_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _add_account(db_env, "account_a")
    _add_account(db_env, "account_b")

    def fake_check(path: Path, *, timeout_ms: int) -> bool:
        if path.name == "account_a":
            raise RuntimeError("simulated crash")
        return True

    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    settings = _make_settings_for_cli(video_root, cover_root)

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", fake_check)
    monkeypatch.setattr(cli_module, "load_settings", lambda: settings)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    # account_a 是 unknown,会触发 cookie_failed=True 退 1。
    # 这个测试关心的是 DB 里的状态而不是退码。
    _ = result.exit_code

    engine = get_engine(db_env)
    with Session(engine) as session:
        a = session.get(Account, "account_a")
        b = session.get(Account, "account_b")
        assert a is not None and a.cookie_status == "unknown"
        assert b is not None and b.cookie_status == "ok"
```

**关键 diff**:删掉 `assert result.exit_code == 0`(因为 unknown cookie 现在退 1),状态断言不变。

- [ ] **Step 6: 运行测试,确认通过 + 跑 pre-commit**

```bash
uv run pytest tests/test_cli_doctor.py -v
uv run pre-commit run --files wxsp/cli.py tests/test_cli_doctor.py
```

Expected: 全部 PASS(原 4 个 + 新 3 个 = 7 个);pre-commit 全绿。

- [ ] **Step 7: 全量测试跑一遍**

```bash
uv run pytest -v
```

Expected: 全绿,无失败。

- [ ] **Step 8: GREEN commit**

```bash
git add wxsp/cli.py tests/test_cli_doctor.py
git commit -m "feat(cli): doctor 命令补 NAS section + 任一失败 exit 1"
```

---

## Task 7: 验收 + 标记 M4 完成

**Files:**
- Modify: `docs/superpowers/specs/2026-05-12-wxsp-design.md`(验收 checklist 打勾,见 §7)

- [ ] **Step 1: 全量 pytest**

```bash
uv run pytest -v
```

Expected: 全绿(M0-M3 已有测试 + M4 新增测试,总数应该是 123 + M4 新增 17 个左右 = ~140 个)。

- [ ] **Step 2: 全量 pre-commit**

```bash
uv run pre-commit run --all-files
```

Expected: 全绿(ruff / ruff-format / mypy / trailing-whitespace / end-of-file-fixer / large-file 全过)。

- [ ] **Step 3: 手动验收 doctor 命令**

```bash
uv run wxsp doctor
```

Expected: 输出包含 cookie 表格 + NAS section,两个 search_root 都是 ✅(假设用户的 `config.yaml/paths` 配置正确)。如果故意把 `cover_search_root` 改成不存在的路径再跑,应该看到 ❌ 不存在 + 退码 1。**这一步不写成 commit**,只验证人工冒烟通过。

- [ ] **Step 4: 检查 git log**

```bash
git log --oneline -15
```

Expected: 看到清晰的 RED/GREEN commit 交替,Conventional Commits 风格(`test:` / `feat:` / `docs:`)。所有 commit 都通过了 pre-commit(没有 `--no-verify` 痕迹)。

- [ ] **Step 5: 在整体设计文档里标记 M4 完成**

修改 `docs/superpowers/specs/2026-05-12-wxsp-design.md` 找到 M4 行(grep `M4 NAS`),把状态标记为已完成。如果文档里有 milestone 状态 emoji(如 ✅),按已有风格加;如果没有,新加一个"M4 验收完成"注释段落。具体格式跟 M3 完成时的写法保持一致(参考 `f354ac0 chore: mark M3 acceptance complete` 这个 commit 的 diff)。

- [ ] **Step 6: 验收 commit**

```bash
git add docs/superpowers/specs/2026-05-12-wxsp-design.md
git commit -m "chore: mark M4 acceptance complete"
```

---

## 自查清单(subagent 完成所有 Task 后给上层 controller 报)

完成所有 Task 后,在最终报告里包含:

1. ✅ 所有 RED commit 之前测试确认 FAIL
2. ✅ 所有 GREEN commit 后测试确认 PASS
3. ✅ pre-commit 在每次 commit 上都通过(无 --no-verify)
4. ✅ ruff + mypy 全绿
5. ✅ 全量 `uv run pytest` 通过
6. ✅ 没改 M3 已交付的 `feishu.py` / `validator.py` / `cli.py::sync`
7. ✅ 没引入新依赖(对照 `pyproject.toml` diff)
8. ✅ `nas.stage_to_tmp` 的实现使用 symlink(不是 copy / hardlink)
9. ✅ `nas.stage_to_tmp` 把任意 OSError 翻译为 `NasUnreachable`,并通过 `__cause__` 保留原始异常
10. ✅ `nas.cleanup_tmp` 对 FileNotFoundError 静默(幂等)
11. ✅ `doctor.check_nas` 返回 `list[NasCheckRow]`,顺序固定为 video → cover
12. ✅ `wxsp doctor` 任一 NAS 失败时退码 1
13. ✅ 在文档里标记 M4 完成

---

## 风险与回避

- **macOS symlink 没权限问题**:用户当前是 macOS,Path.symlink_to 不需要管理员。Windows 部署是 M10 范围,届时再加 fallback(或 copy)
- **测试副作用**:CliRunner 在隔离环境跑,monkeypatch 在 fixture 结束时自动恢复,无遗留
- **覆盖式 symlink 的竞态**:目前是单 worker 串行,不存在并发覆盖问题。M5 publisher 进入 worker 串行后这个假设继续成立
- **现有 cli_doctor 测试被改动**:Task 6 改了 4 个现有测试,需要确保改动只是"加 monkeypatch load_settings"和"放宽 exit_code 断言",**不删任何已有断言**

---

## 与 Spec 的对齐

- spec §1.1 交付物清单 → Task 1-6 一一对应 ✅
- spec §1.2 测试文件 → tests/test_nas.py / test_doctor.py / test_cli_doctor.py / test_errors.py 都覆盖 ✅
- spec §1.4 不做的事项(重试装饰器、封面 stage、写 NAS 验证、Windows symlink 权限) → 本 plan 全部规避 ✅
- spec §3.1 stage_to_tmp 实现要点(覆盖式 / mkdir parents+exist_ok / OSError 翻译) → Task 2-3 完整覆盖 ✅
- spec §3.2 NasUnreachable 定义 → Task 1 ✅
- spec §3.3 NasCheckRow + check_nas → Task 5 ✅
- spec §3.4 CLI doctor 输出格式 + 退码 → Task 6 ✅
- spec §4 错误模型表 → Task 2-4 通过单元测试验证 ✅
- spec §5 测试策略 → 每个 Task 的 Step 1 测试列表对应 ✅
- spec §6 决策记录(symlink / publisher 内联清 / 任意 OSError / 路径布局) → 代码实现一致 ✅
- spec §7 偏离记录(stage 方式 + 重试推到 M5) → Task 1-6 不引入重试装饰器 ✅
