# M4 NAS Staging + Doctor 设计文档

> 本文档是整体设计 [`2026-05-12-wxsp-design.md`](2026-05-12-wxsp-design.md) 的 M4 milestone 细化版,**对上层设计的偏离会在 §7 列明**。

**Goal**:在 `wxsp/nas.py` 上补 `stage_to_tmp / cleanup_tmp` 两个函数(为 M5 publisher 准备本地视频路径),在 `wxsp/doctor.py` 上加 NAS 可达性检查,在 `wxsp/errors.py` 上定义 `NasUnreachable` 异常。完成后 `wxsp doctor` 输出能告知 NAS 健康度,且 M5 publisher 调用 `stage_to_tmp` 失败时拿到的是统一的 `NasUnreachable`。

**Tech Stack**:`pathlib`(已是项目跨平台标准) + `shutil` + pytest + Typer(已就绪)。

---

## 1. M4 范围

### 1.1 交付物

| 文件 | 改动 |
|------|------|
| `wxsp/nas.py` | 新增 `stage_to_tmp(src, *, task_id, tmp_root) -> Path` + `cleanup_tmp(task_id, *, tmp_root) -> None`;`find_video / find_cover` 不动 |
| `wxsp/errors.py` | 定义 `class NasUnreachable(Exception)` |
| `wxsp/doctor.py` | 新增 `NasCheckRow` NamedTuple + `check_nas(config) -> list[NasCheckRow]`;`record_cookie_check / refresh_cookie_status` 不动 |
| `wxsp/cli.py` | `wxsp doctor` 命令补 NAS section 输出(账号/cookie section 已就绪) |

### 1.2 测试文件

- `tests/test_nas.py` — 已有 5 个 `find_*` 测试不动;新增 `stage_to_tmp` / `cleanup_tmp` 的若干 case(`tmp_path` 造目录树 + monkeypatch 模拟 OSError)
- `tests/test_doctor.py` — 已有 cookie 相关测试不动;新增 `check_nas` 的若干 case
- `tests/test_cli_doctor.py`(已有,M2 创建) — 补 NAS 输出的 assertion

### 1.3 不动的

- `wxsp/config.py` — `PathsConfig` 已有 `nas_root / video_search_root / cover_search_root`,直接用
- `wxsp/nas.py::find_video / find_cover` — M3 实现,本里程碑不动
- `wxsp/models.py` / `wxsp/db.py` / `wxsp/feishu.py` / `wxsp/validator.py` / `wxsp/cli.py::sync` — M3 已稳定,不碰
- `wxsp/doctor.py::record_cookie_check / refresh_cookie_status` — M2 实现,不动

### 1.4 不做的

- **重试装饰器(retry.py)**:M4 的 `stage_to_tmp` 单次失败就抛 `NasUnreachable`,**不内联重试**。5 次指数退避(5/10/20/40/80s)在 M5 写 publisher 时统一通过 retry 装饰器处理(详见 §6.A 决策)
- **封面 stage**:第一版只 stage 视频。封面是图片(几 MB),发布时直接给 NAS 路径,失败成本低;真要 stage 封面是后续 milestone 的事
- **写 NAS 验证**:`check_nas` 只 read-only 验 `exists() + is_dir()`,不写 marker(项目从不往 NAS 写)
- **Windows 上 symlink 权限处理**:macOS 上 symlink 不需要任何权限,Windows 需要管理员或开发者模式。Windows 部署属于 M10,届时再补 fallback

---

## 2. 数据流

### 2.1 stage_to_tmp(M5 publisher 将调用)

```
publisher.publish(task)
  ├─ [1] stage_to_tmp(video_path, task_id=task.id, tmp_root=config.app.data_dir / "tmp")
  │       └─ 在 {tmp_root}/{task_id}/ 下创建 symlink → src
  │       └─ 返回 symlink 路径(Path 对象,给 Playwright set_input_files 用)
  │       └─ 任意 OSError → raise NasUnreachable
  ├─ ... 发布步骤 [2-18] ...
  └─ [19] cleanup_tmp(task_id, tmp_root=...)
          └─ rmtree({tmp_root}/{task_id}/),失败抛 OSError(不翻译)
          └─ 成败都调用(finally 或 publisher 上下文管理)
```

### 2.2 check_nas(doctor 命令使用)

```
wxsp doctor
  ├─ ... 账号/cookie 检查(M2 已实现)...
  └─ NAS 检查:
        for path, label in [(video_search_root, "video_search_root"),
                            (cover_search_root, "cover_search_root")]:
            if path.exists() and path.is_dir():
                row.ok = True, detail = f"OK ({path})"
            elif path.exists():
                row.ok = False, detail = f"不是目录: {path}"
            else:
                row.ok = False, detail = f"不存在: {path}"
        return rows
```

---

## 3. 模块设计

### 3.1 `wxsp/nas.py`

```python
def stage_to_tmp(src: Path, *, task_id: int, tmp_root: Path) -> Path:
    """在 tmp_root/{task_id}/ 下建 symlink 指向 src,返回 symlink 路径。

    - `tmp_root/{task_id}/` 不存在则自动 mkdir(parents=True)
    - symlink 名等于 src.name(保留原文件名,便于 debug)
    - 已存在同名 symlink → 先 unlink 再 symlink_to(覆盖式)
    - 任意 OSError(symlink_to / mkdir / unlink 抛出)→ NasUnreachable
    """

def cleanup_tmp(task_id: int, *, tmp_root: Path) -> None:
    """删除 tmp_root/{task_id}/ 目录。

    - 目录不存在 → 静默返回(幂等)
    - 其他 OSError → 抛出原始异常(不翻译,这是本地文件系统问题)
    """
```

实现要点:
- `(tmp_root / str(task_id)).mkdir(parents=True, exist_ok=True)` —— exist_ok=True 保证幂等
- `link_path = tmp_root / str(task_id) / src.name`
- `if link_path.is_symlink() or link_path.exists(): link_path.unlink()` —— 覆盖式(同 task 重跑时不报错)
- `link_path.symlink_to(src)` —— Path.symlink_to 跨平台
- `try / except OSError as exc: raise NasUnreachable(...) from exc` 包整段
- `cleanup_tmp` 用 `shutil.rmtree(dir, ignore_errors=False)`,自己捕 FileNotFoundError 实现幂等

### 3.2 `wxsp/errors.py`

```python
"""错误类型 + 分类(M4 起逐步填充;M5 加重试装饰器)。"""

class NasUnreachable(Exception):
    """NAS 文件操作失败。

    在 nas.stage_to_tmp 抛任意 OSError(FileNotFoundError / PermissionError /
    TimeoutError / ConnectionError / 其他 OSError 子类)时统一翻译为此类型。
    M5 retry 装饰器对此异常做 5 次指数退避(5/10/20/40/80s)。
    """
```

### 3.3 `wxsp/doctor.py`

新增片段(顶部 import 区追加 `from wxsp.config import Settings` 即可):

```python
class NasCheckRow(NamedTuple):
    """`check_nas` 返回行,CLI 输出层用。"""

    path: Path
    label: str   # "video_search_root" | "cover_search_root"
    ok: bool
    detail: str

def check_nas(config: Settings) -> list[NasCheckRow]:
    """检查 video_search_root + cover_search_root 是否存在且为目录。

    无 IO 副作用,只读 stat。返回固定 2 行,按 video → cover 顺序。
    """
```

### 3.4 `wxsp/cli.py::doctor`

`doctor` 命令在已有的"账号/cookie"输出之后,追加一段:

```
NAS:
  ✅ video_search_root  OK (/Volumes/NAS/videos)
  ❌ cover_search_root  不存在: /Volumes/NAS/covers
```

具体打印用 Typer / `typer.echo`,与现有 cookie 风格保持一致。CLI 退出码规则:任一行 `ok=False` → `raise typer.Exit(code=1)`(与现有 cookie expired 退码一致)。

---

## 4. 错误模型

| 场景 | 函数 | 抛出 |
|------|------|------|
| validator 阶段 find_video 找不到文件 | `find_video` (M3) | `FileNotFoundError`(validator 走 video_invalid,**不**翻译 NasUnreachable) |
| publisher 阶段 stage_to_tmp 失败 | `stage_to_tmp` | `NasUnreachable`(任意 OSError 翻译) |
| publisher 阶段 cleanup_tmp 失败 | `cleanup_tmp` | 原始 `OSError` 子类(本地文件系统问题,不翻译) |
| doctor 检查 NAS | `check_nas` | **不抛**,失败信息塞 `NasCheckRow.detail` |

**关键区分**:`find_video` 和 `stage_to_tmp` 都可能抛 `OSError`(尤其 NAS 掉线时 `FileNotFoundError`),但语义不同:
- `find_video` 在 validator 阶段,搜不到就是"用户填错文件名 / NAS 真没这文件"——属于业务校验失败,回写飞书"文件不存在"
- `stage_to_tmp` 在 publisher 阶段,此时 validator 早已确认文件存在,再失败一定是 NAS 抖动——值得重试

这层区分通过**调用点不同**而非异常类型不同实现,逻辑上更直白(M3 validator 不需要捕 NasUnreachable,M5 publisher 也不需要捕 FileNotFoundError)。

---

## 5. 测试策略

### 5.1 `tests/test_nas.py`(新增 case)

| 测试 | 目的 |
|------|------|
| `test_stage_to_tmp_creates_symlink` | happy path:tmp_root 不存在 → 自动 mkdir,symlink 指向 src,返回路径正确 |
| `test_stage_to_tmp_preserves_original_filename` | symlink 文件名 = src.name(中文 / 空格 / `.mov` 都 OK) |
| `test_stage_to_tmp_overwrites_existing_symlink` | 同 task_id 重跑时旧 symlink 被覆盖 |
| `test_stage_to_tmp_translates_oserror_to_nas_unreachable` | monkeypatch `Path.symlink_to` 抛 PermissionError → 触发 NasUnreachable;chain 保留原 OSError |
| `test_cleanup_tmp_removes_task_dir` | happy path:rmtree 干净 |
| `test_cleanup_tmp_is_idempotent_when_dir_missing` | 目录不存在 → 静默不抛 |

### 5.2 `tests/test_doctor.py`(新增 case)

| 测试 | 目的 |
|------|------|
| `test_check_nas_both_exist_and_are_dirs` | happy path:两行 ok=True,顺序 video → cover |
| `test_check_nas_missing_dir` | video_search_root 不存在 → 该行 ok=False,detail 含 "不存在" |
| `test_check_nas_path_is_file_not_dir` | path 存在但是个文件 → ok=False,detail 含 "不是目录" |

### 5.3 `tests/test_cli_doctor.py`(扩展)

- `test_doctor_command_prints_nas_section` — stdout 含 "NAS:" 且两行都 ✅
- `test_doctor_command_exits_1_when_nas_missing` — video_search_root 不存在 → exit code 1

### 5.4 测试技巧

- 用 pytest `tmp_path` fixture 构造 fake nas_root / tmp_root
- monkeypatch 模拟 OSError:`monkeypatch.setattr(Path, "symlink_to", lambda *a, **kw: raise PermissionError(...))`
- 构造 `Settings` 对象:复用 M3 测试里已有的 `_make_settings()` helper(`tests/conftest.py` 或 `tests/test_validator.py` 里已有,M4 复用)

---

## 6. 决策记录

| # | 决策 | 选择 | 理由 |
|---|------|------|------|
| 1 | stage 方式 | **symlink** | 零拷贝、瞬间完成;用户当前 `nas_root` 指本地目录(`/Users/zhaoguangyu/test-1`),无 NAS 网络抖动风险。**遗留风险**:将来真挂 NAS 时浏览器上传期间直接读网络盘,网络抖动会导致 upload_failed;届时改为 `shutil.copy2` |
| 2 | cleanup 时机 | **publisher 发布完成(成败都清)** | 最简单,symlink 几乎不占空间。M5 publisher 必须用 try/finally 保证清理 |
| 3 | doctor 粒度 | **查两个 search_root 均 exists + is_dir** | 比 nas_root 单点检查更准(子目录单独配错也能识别),比写 marker 简单(read-only) |
| 4 | 错误边界 | **任意 OSError → NasUnreachable** | publisher 阶段 validator 已确认文件存在,再错就是网络问题。跨平台 errno 白名单实现成本高、收益低 |
| 5 | 路径布局 | **`./data/tmp/{task_id}/{原文件名}`** | 每 task 一个目录,cleanup 直接 rmtree;原文件名保留便于 debug;扩展性好 |
| A | 重试位置 | **M5 retry.py 装饰器统一处理,M4 不内联** | YAGNI:M4 没有 caller(publisher 是 M5);M3 内联重试是因为 sync CLI 同 milestone 调用 |

---

## 7. 与上层设计的偏离

**与 [`2026-05-12-wxsp-design.md`](2026-05-12-wxsp-design.md) 的差异**:

### 7.1 上层说 "stage 复制",本里程碑改 symlink

- 上层 §5.1 步骤 [1] 描述"复制视频 NAS → tmp/"
- 本里程碑用 symlink。用户当前测试环境 `nas_root` 是本地目录,symlink 等价 copy 但更快
- **生产风险记录**:挂真 NAS 后,若 upload_failed 高频出现,把 `stage_to_tmp` 内部改回 `shutil.copy2(src, link_path.with_suffix(""))` 即可,接口签名不变

### 7.2 上层说 M4 包含 5 次指数退避,本里程碑推到 M5

- 上层 §5.3 "nas_unreachable 5 次退避"位列错误重试表
- 本里程碑只交付错误类型 + stage_to_tmp 抛错;重试装饰器统一在 M5(写 publisher 时)做
- 影响:M4 验收时 NAS 断开 → 单次抛 NasUnreachable;5 次退避验收推到 M5

### 7.3 没有偏离的

- `find_video / find_cover` 语义、错误抛出方式(`FileNotFoundError`)与上层一致(M3 已落地)
- `doctor` 命令的 NAS section 输出是上层 §5.4(M2 验收标准节)的本里程碑增量
- `errors.py` 沿用上层 §5.3 的错误分类思路,只先填 NAS 相关一条
