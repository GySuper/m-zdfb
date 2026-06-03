# 飞书任务二次同步覆盖 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让已入库的飞书行在被改正 + 状态改回「待入库」后,二次同步能覆盖未发布的本地任务并重置为可重跑,同时绝不覆盖已发布(success)/正在跑(running)的任务。

**Architecture:** 在 `wxsp/sync.py::sync_now` 的主循环里,把"Video 已存在就无条件 skip"改成原地 upsert:先 `validate`,再按"是否已存在 + 本地 Task 状态"分流为 新建 / 覆盖(updated) / 拒绝已发布(published_refused) / 跳过正在跑(running_skipped) / 跳过孤儿(skipped)。`SyncResult` 加 `updated` 计数,CLI 与两个 Web 路由顺带展示。

**Tech Stack:** Python 3.10+ / SQLModel / pytest + typer CliRunner(沿用 `tests/test_cli_sync.py` 的真 DB + 假飞书 harness)。

**实现说明(与 spec 的一处对齐):** spec 里写"在 `tests/test_sync.py` 增量",实际真正驱动 DB 的 sync 集成测试都在 `tests/test_cli_sync.py`(`sync_env` fixture / `_happy_row` / `_FakeClient` / `_FrozenDatetime` 都在那)。因此新测试落在 `tests/test_cli_sync.py`,与既有同类测试同处。

---

### Task 1: sync.py 二次同步 upsert 核心 + SyncResult.updated + CLI 计数

**Files:**
- Modify: `wxsp/sync.py`(`SyncResult` 加字段、主循环重构、writeback 段重构、新增常量 + 辅助函数)
- Modify: `wxsp/cli.py:335`(sync 命令输出加一行「覆盖更新」)
- Test: `tests/test_cli_sync.py`(新增 5 个测试 + 改写 1 个既有测试)

#### 背景:重构后 `sync_now` 主循环的目标形态

当前 [sync.py:105-156](../../wxsp/sync.py#L105-L156) 的循环把"Video 已存在"无条件 skip。重构后:先 `validate`,再分流。下面的步骤会逐块替换。

- [ ] **Step 1: 在 `tests/test_cli_sync.py` 顶部加一个测试常量 + 一个改写后的 Video 行助手**

在 `tests/test_cli_sync.py` 的 import 段之后(`_happy_row` 定义之前或之后均可),加:

```python
# 二次同步测试用:改正后的新标题(18~30 字,合规)
_RESYNC_NEW_TITLE = "改过的新标题视频内容编号十八个字以上凑数用"


def _edited_row(record_id: str, base: BitableRow, *, title: str | None = None) -> BitableRow:
    """复制一行飞书记录,可改标题,record_id 保持不变(模拟运营改同一行)。"""
    fields = dict(base.fields)
    if title is not None:
        fields["标题"] = title
    return BitableRow(record_id=record_id, fields=fields)
```

- [ ] **Step 2: 写失败测试 —— 覆盖未发布(pending)行**

加到 `tests/test_cli_sync.py` 末尾:

```python
def test_resync_overwrites_pending_row(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(0, now)]
    fake = _FakeClient(rows)
    monkeypatch.setattr("wxsp.sync.make_client", lambda app_id, app_secret: fake)
    monkeypatch.setattr("wxsp.sync.fetch_pending_rows", lambda client, **kw: list(rows))
    writebacks: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "wxsp.sync.writeback_row",
        lambda client, *, record_id, fields, **kw: writebacks.append((record_id, fields)),
    )
    monkeypatch.setattr("wxsp.sync.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    assert runner.invoke(app, ["sync"]).exit_code == 0

    # 运营改了标题,状态又被改回"待入库"(fake 始终返回待入库),再同步
    rows[:] = [_edited_row("rec_ok_0", _happy_row(0, now), title=_RESYNC_NEW_TITLE)]
    writebacks.clear()
    out = runner.invoke(app, ["sync"])
    assert out.exit_code == 0
    assert "覆盖更新: 1" in out.output

    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        videos = session.exec(select(Video)).all()
        tasks = session.exec(select(Task)).all()
    assert len(videos) == 1 and len(tasks) == 1
    assert videos[0].title == _RESYNC_NEW_TITLE
    assert tasks[0].status == "pending"
    assert tasks[0].attempts == 0
    # 回写:状态=已计划 且 清空错误信息
    assert any(
        f.get("状态") == "已计划" and f.get("错误信息") == "" for _, f in writebacks
    )
```

- [ ] **Step 3: 写失败测试 —— failed 行覆盖并重置干净**

```python
def test_resync_overwrites_failed_row_and_resets(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(0, now)]
    fake = _FakeClient(rows)
    monkeypatch.setattr("wxsp.sync.make_client", lambda app_id, app_secret: fake)
    monkeypatch.setattr("wxsp.sync.fetch_pending_rows", lambda client, **kw: list(rows))
    monkeypatch.setattr(
        "wxsp.sync.writeback_row", lambda client, *, record_id, fields, **kw: None
    )
    monkeypatch.setattr("wxsp.sync.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    assert runner.invoke(app, ["sync"]).exit_code == 0

    # 把 task 改成 failed + 带错误现场
    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        task = session.exec(select(Task)).one()
        task.status = "failed"
        task.attempts = 3
        task.last_error_type = "upload_failed"
        task.last_error_msg = "boom"
        task.finished_at = now
        session.add(task)
        session.commit()

    out = runner.invoke(app, ["sync"])  # 同一行,状态仍待入库
    assert out.exit_code == 0
    assert "覆盖更新: 1" in out.output

    with Session(engine) as session:
        task = session.exec(select(Task)).one()
    assert task.status == "pending"
    assert task.attempts == 0
    assert task.last_error_type is None
    assert task.last_error_msg is None
    assert task.finished_at is None
```

- [ ] **Step 4: 写失败测试 —— 已发布(success)被拒绝 + 回写"已发布"**

```python
def test_resync_refuses_published_row(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(0, now)]
    fake = _FakeClient(rows)
    monkeypatch.setattr("wxsp.sync.make_client", lambda app_id, app_secret: fake)
    monkeypatch.setattr("wxsp.sync.fetch_pending_rows", lambda client, **kw: list(rows))
    writebacks: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "wxsp.sync.writeback_row",
        lambda client, *, record_id, fields, **kw: writebacks.append((record_id, fields)),
    )
    monkeypatch.setattr("wxsp.sync.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    assert runner.invoke(app, ["sync"]).exit_code == 0
    original_title = _happy_row(0, now).fields["标题"]

    # 标记 success(已发布到平台)
    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        task = session.exec(select(Task)).one()
        task.status = "success"
        task.remote_url = "http://example.com/v1"
        session.add(task)
        session.commit()

    # 运营改了标题想重发,再同步
    rows[:] = [_edited_row("rec_ok_0", _happy_row(0, now), title=_RESYNC_NEW_TITLE)]
    writebacks.clear()
    out = runner.invoke(app, ["sync"])
    assert out.exit_code == 0
    assert "覆盖更新: 0" in out.output

    with Session(engine) as session:
        video = session.exec(select(Video)).one()
        task = session.exec(select(Task)).one()
    # 本地未被改动
    assert video.title == original_title
    assert task.status == "success"
    # 回写:状态=已发布 + 提示文案
    assert any(
        f.get("状态") == "已发布" and "已发布成功" in (f.get("错误信息") or "")
        for _, f in writebacks
    )
```

- [ ] **Step 5: 写失败测试 —— running 跳过且不回写**

```python
def test_resync_skips_running_row_without_writeback(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(0, now)]
    fake = _FakeClient(rows)
    monkeypatch.setattr("wxsp.sync.make_client", lambda app_id, app_secret: fake)
    monkeypatch.setattr("wxsp.sync.fetch_pending_rows", lambda client, **kw: list(rows))
    writebacks: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "wxsp.sync.writeback_row",
        lambda client, *, record_id, fields, **kw: writebacks.append((record_id, fields)),
    )
    monkeypatch.setattr("wxsp.sync.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    assert runner.invoke(app, ["sync"]).exit_code == 0
    original_title = _happy_row(0, now).fields["标题"]

    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        task = session.exec(select(Task)).one()
        task.status = "running"
        session.add(task)
        session.commit()

    rows[:] = [_edited_row("rec_ok_0", _happy_row(0, now), title=_RESYNC_NEW_TITLE)]
    writebacks.clear()
    out = runner.invoke(app, ["sync"])
    assert out.exit_code == 0

    with Session(engine) as session:
        video = session.exec(select(Video)).one()
        task = session.exec(select(Task)).one()
    assert video.title == original_title  # 未被覆盖
    assert task.status == "running"  # 未被打断
    assert all(rid != "rec_ok_0" for rid, _ in writebacks)  # 不回写
```

- [ ] **Step 6: 写失败测试 —— 改坏的已存在行回写「失败」,不动本地**

```python
def test_resync_rejects_now_invalid_row(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(0, now)]
    fake = _FakeClient(rows)
    monkeypatch.setattr("wxsp.sync.make_client", lambda app_id, app_secret: fake)
    monkeypatch.setattr("wxsp.sync.fetch_pending_rows", lambda client, **kw: list(rows))
    writebacks: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "wxsp.sync.writeback_row",
        lambda client, *, record_id, fields, **kw: writebacks.append((record_id, fields)),
    )
    monkeypatch.setattr("wxsp.sync.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    assert runner.invoke(app, ["sync"]).exit_code == 0
    original_title = _happy_row(0, now).fields["标题"]

    # 改成不合规(标题太短)
    rows[:] = [_edited_row("rec_ok_0", _happy_row(0, now), title="短")]
    writebacks.clear()
    out = runner.invoke(app, ["sync"])
    assert out.exit_code == 0

    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        video = session.exec(select(Video)).one()
        task = session.exec(select(Task)).one()
    assert video.title == original_title  # 校验失败 → 本地不动
    assert task.status == "pending"
    assert any(
        f.get("状态") == "失败" and (f.get("错误信息") or "") for _, f in writebacks
    )
```

- [ ] **Step 7: 改写既有测试 `test_sync_second_run_skips_existing` → 覆盖语义**

行为已变:第二遍拉到相同(仍待入库)的 pending 行,现在是覆盖(updated),不再 skip。把
[tests/test_cli_sync.py:200-236](../../tests/test_cli_sync.py#L200-L236) 整个函数替换为:

```python
def test_sync_second_run_overwrites_pending(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(i, now) for i in range(5)]

    fake = _FakeClient(rows)
    monkeypatch.setattr("wxsp.sync.make_client", lambda app_id, app_secret: fake)
    monkeypatch.setattr("wxsp.sync.fetch_pending_rows", lambda client, **kw: list(rows))

    writebacks: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "wxsp.sync.writeback_row",
        lambda client, *, record_id, fields, **kw: writebacks.append((record_id, fields)),
    )
    monkeypatch.setattr("wxsp.sync.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    # 第一遍:5 条新入库
    r1 = runner.invoke(app, ["sync"])
    assert r1.exit_code == 0
    assert len([w for w in writebacks if w[1].get("状态") == "已计划"]) == 5

    # 第二遍:5 条仍待入库且本地 pending → 覆盖(updated),不新增
    writebacks.clear()
    r2 = runner.invoke(app, ["sync"])
    assert r2.exit_code == 0
    assert "覆盖更新: 5" in r2.output
    assert len([w for w in writebacks if w[1].get("状态") == "已计划"]) == 5

    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        tasks = session.exec(select(Task)).all()
    assert len(tasks) == 5  # 没新增
```

- [ ] **Step 8: 运行新测试,确认全部失败(行为尚未实现)**

Run: `uv run pytest tests/test_cli_sync.py -k "resync or second_run_overwrites" -v`
Expected: 6 个测试全 FAIL(`覆盖更新` 输出不存在、覆盖未发生、success/running 仍被旧逻辑当 skip 回写"已有历史任务"等)。

- [ ] **Step 9: 实现 —— `wxsp/sync.py` 的 `SyncResult` 加 `updated` 字段**

把 [sync.py:27-35](../../wxsp/sync.py#L27-L35) 的 `SyncResult` 改为:

```python
@dataclass
class SyncResult:
    pulled: int = 0
    accepted: int = 0
    updated: int = 0  # 已存在行被改正后覆盖重入库(重置为 pending)的行数
    rejected: int = 0
    skipped_existing: int = 0
    skipped_incomplete: int = 0  # 业务还没填完(4 核心字段任一空)→ 跳过 + 不回写
    rejected_details: list[tuple[str, list[FieldError]]] = field(default_factory=list)
    writeback_failed: int = 0  # 飞书回写失败的行数
```

- [ ] **Step 10: 实现 —— 在 `wxsp/sync.py` 加常量 + `_apply_overwrite` 辅助函数**

在 `_format_errors`(文件末尾)之后追加:

```python
_PUBLISHED_REFUSE_MSG = (
    "该任务已发布成功,不能改这一行重发;如需重新发布,请在飞书新建一行任务。"
)


def _apply_overwrite(
    session: Any,
    video: Video,
    task: Task,
    record_id: str,
    v_result: Any,
    platform: str,
    now: datetime,
) -> None:
    """已存在且未发布的任务:原地刷新 Video 内容 + 把 Task 重置为干净 pending。"""
    video.file_path = str(v_result.video_path)
    video.title = v_result.title or ""
    video.description = v_result.description
    video.tags_json = json.dumps(v_result.tags, ensure_ascii=False)
    video.cover_path = str(v_result.cover_path) if v_result.cover_path else None
    video.topic = v_result.topic
    video.original_claim = v_result.original_claim
    video.declaration = v_result.declaration
    video.ai_optimize = v_result.ai_optimize
    video.product_ids_json = json.dumps(v_result.product_ids, ensure_ascii=False)
    video.ingested_at = now

    task.account_id = v_result.account_id or ""
    task.execute_date = v_result.execute_date
    task.publish_at = v_result.publish_at
    task.platform = platform
    task.status = "pending"
    task.attempts = 0
    task.lease_token = None
    task.lease_expires_at = None
    task.last_error_type = None
    task.last_error_msg = None
    task.started_at = None
    task.finished_at = None
    task.remote_video_id = None
    task.remote_url = None
    task.screenshots_json = "[]"

    session.add(video)
    session.add(task)
```

- [ ] **Step 11: 实现 —— 重构 `sync_now` 主循环**

把 [sync.py:86-89](../../wxsp/sync.py#L86-L89) 的桶声明 + [sync.py:105-156](../../wxsp/sync.py#L105-L156) 的循环,替换为下面整段(桶声明 + 循环)。即从 `accepted: list[str] = []` 到循环结束:

```python
    accepted: list[str] = []
    updated: list[str] = []
    rejected: list[tuple[str, list[FieldError]]] = []
    skipped: list[str] = []  # 孤儿 Video / 写库竞态 → 计 skipped_existing,不回写
    published_refused: list[str] = []  # 本地已发布 → 拒绝,回写"已发布"
    running_skipped: list[str] = []  # 本地正在跑 → 跳过,不回写
    skipped_incomplete: list[str] = []  # 4 核心字段空 → 跳过且不回写,等下次拉

    engine = get_engine()
    init_db(engine)
    with session_scope(engine) as session:
        active_account_ids: set[str] = {
            a.id
            for a in session.exec(select(Account).where(Account.is_active == True))  # noqa: E712
        }
        active_accounts: dict[str, str] = {
            aid: settings.accounts[aid].display_name
            for aid in active_account_ids
            if aid in settings.accounts
        }
        for row in rows:
            existing_video = session.get(Video, row.record_id)
            v_result = validate(
                row,
                config=settings,
                now=now,
                nas_finder=nas_finder,
                active_accounts=active_accounts,
                platform=platform,
            )
            if v_result.incomplete:
                skipped_incomplete.append(row.record_id)
                continue
            if not v_result.ok:
                rejected.append((row.record_id, v_result.errors))
                continue

            if existing_video is None:
                # 全新行:与原逻辑一致,新建 Video + Task
                if dry_run:
                    accepted.append(row.record_id)
                    continue
                video = Video(
                    id=row.record_id,
                    source="feishu",
                    file_path=str(v_result.video_path),
                    title=v_result.title or "",
                    description=v_result.description,
                    tags_json=json.dumps(v_result.tags, ensure_ascii=False),
                    cover_path=str(v_result.cover_path) if v_result.cover_path else None,
                    topic=v_result.topic,
                    original_claim=v_result.original_claim,
                    declaration=v_result.declaration,
                    ai_optimize=v_result.ai_optimize,
                    product_ids_json=json.dumps(v_result.product_ids, ensure_ascii=False),
                    ingested_at=now,
                )
                task = Task(
                    video_id=row.record_id,
                    account_id=v_result.account_id or "",
                    platform=platform,
                    execute_date=v_result.execute_date,
                    publish_at=v_result.publish_at,
                    status="pending",
                )
                try:
                    with session.begin_nested():
                        session.add(video)
                        session.add(task)
                except IntegrityError:
                    skipped.append(row.record_id)
                    continue
                accepted.append(row.record_id)
                continue

            # 已存在:按本地 Task 状态决定覆盖 / 拒绝 / 跳过
            existing_task = session.exec(
                select(Task).where(Task.video_id == row.record_id)
            ).first()
            if existing_task is None:
                # Video 无对应 Task(异常半状态)→ 防呆跳过,不覆盖不回写
                skipped.append(row.record_id)
                continue
            if existing_task.status == "running":
                running_skipped.append(row.record_id)
                continue
            if existing_task.status == "success":
                published_refused.append(row.record_id)
                continue
            # pending / failed / skipped / interrupted → 覆盖
            if dry_run:
                updated.append(row.record_id)
                continue
            _apply_overwrite(
                session, existing_video, existing_task, row.record_id, v_result, platform, now
            )
            updated.append(row.record_id)
```

- [ ] **Step 12: 实现 —— 结果汇总 + writeback 段重构**

把 [sync.py:158-186](../../wxsp/sync.py#L158-L186)(从 `result.accepted = len(accepted)` 到 writeback 段结束)替换为:

```python
    result.accepted = len(accepted)
    result.updated = len(updated)
    result.rejected = len(rejected)
    result.skipped_existing = len(skipped) + len(published_refused) + len(running_skipped)
    result.skipped_incomplete = len(skipped_incomplete)
    result.rejected_details = rejected

    if not dry_run and feishu_cfg.sync.write_back_enabled:
        fm = feishu_cfg.field_map
        wb_failed = 0
        for record_id in accepted:
            if not _safe_writeback(client, feishu_cfg, record_id, {fm.status: "已计划"}):
                wb_failed += 1
        for record_id in updated:
            # 覆盖成功:状态回"已计划"并清空旧错误信息
            if not _safe_writeback(
                client, feishu_cfg, record_id, {fm.status: "已计划", fm.error_message: ""}
            ):
                wb_failed += 1
        for record_id, errs in rejected:
            if not _safe_writeback(
                client,
                feishu_cfg,
                record_id,
                {fm.status: "失败", fm.error_message: _format_errors(errs)},
            ):
                wb_failed += 1
        for record_id in published_refused:
            # 已发布:移出"待入库"过滤,避免每次同步重复拒绝刷屏
            if not _safe_writeback(
                client,
                feishu_cfg,
                record_id,
                {fm.status: "已发布", fm.error_message: _PUBLISHED_REFUSE_MSG},
            ):
                wb_failed += 1
        # running_skipped / skipped:不回写(留待入库,下轮自然收敛)
        result.writeback_failed = wb_failed

    return result
```

- [ ] **Step 13: 实现 —— CLI sync 输出加「覆盖更新」一行**

把 [cli.py:335-337](../../wxsp/cli.py#L335-L337) 改为(在「入库」之后插一行):

```python
    typer.echo(f"  入库: {result.accepted}{' (dry-run)' if dry_run else ''}")
    typer.echo(f"  覆盖更新: {result.updated}{' (dry-run)' if dry_run else ''}")
    typer.echo(f"  拒绝: {result.rejected}{' (已回写)' if not dry_run else ''}")
    typer.echo(f"  已存在跳过: {result.skipped_existing}")
```

- [ ] **Step 14: 运行本任务全部测试,确认通过**

Run: `uv run pytest tests/test_cli_sync.py -v`
Expected: 全 PASS(含 6 个新/改测试 + 既有 happy/dry-run/disabled/api-error)。

- [ ] **Step 15: 运行 sync 相关全量 + 类型检查**

Run: `uv run pytest tests/test_sync.py tests/test_cli_sync.py -q && uv run mypy wxsp/sync.py wxsp/cli.py`
Expected: 测试全绿;mypy 无新增错误。

- [ ] **Step 16: Commit**

```bash
git add wxsp/sync.py wxsp/cli.py tests/test_cli_sync.py
git commit -m "feat(sync): 飞书任务二次同步覆盖未发布任务

已入库行改正+状态改回待入库后,二次同步覆盖 pending/failed/skipped/
interrupted 任务并重置为 pending;success 拒绝(回写已发布+提示),
running 跳过不打断。SyncResult 加 updated 计数,CLI 顺带展示。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Web UI 同步结果展示「覆盖更新」计数

**Files:**
- Modify: `wxsp/api/routes_accounts.py:293`(「立即同步」按钮结果片段)
- Modify: `wxsp/api/routes_tasks.py:359`(「跑今天」前置 sync 结果提示)

> 纯展示性 f-string 追加,无新业务逻辑。靠既有路由测试保持绿 + 手工核对。

- [ ] **Step 1: routes_accounts.py 加「覆盖更新」条目**

把 [routes_accounts.py:293](../../wxsp/api/routes_accounts.py#L293) 这行之后插入 `updated` 展示:

```python
    parts = [f"新入库 {result.accepted} 条"]
    if result.updated:
        parts.append(f"覆盖更新 {result.updated} 条")
    if result.rejected:
```

(即在 `parts = [...]` 与 `if result.rejected:` 之间插入那两行。)

- [ ] **Step 2: routes_tasks.py 加「覆盖更新」条目**

把 [routes_tasks.py:359-363](../../wxsp/api/routes_tasks.py#L359-L363) 改为:

```python
        bits = [f"飞书同步完成(新入库 {sync_result.accepted} 条"]
        if sync_result.updated:
            bits.append(f",覆盖更新 {sync_result.updated} 条")
        if sync_result.rejected:
            bits.append(f",校验失败 {sync_result.rejected} 条")
        if sync_result.skipped_incomplete:
            bits.append(f",未填完 {sync_result.skipped_incomplete} 条等下次")
```

- [ ] **Step 3: 运行 API + 全量测试,确认无回归**

Run: `uv run pytest tests/ -q`
Expected: 全 PASS。

- [ ] **Step 4: Commit**

```bash
git add wxsp/api/routes_accounts.py wxsp/api/routes_tasks.py
git commit -m "feat(webui): 同步结果展示覆盖更新计数

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- 二次同步覆盖未发布(pending/failed/skipped/interrupted)→ Task 1 Step 11 的"已存在"分支 + `_apply_overwrite`(Step 10),测试 Step 2/3/7。✅
- 已发布(success)拒绝 + 回写「已发布」+ 提示 → Step 11 `published_refused` 分支 + Step 12 writeback,测试 Step 4。✅
- 正在跑(running)不打断、不回写 → Step 11 `running_skipped` 分支(不进任何 writeback 循环),测试 Step 5。✅
- 改坏的已存在行回写「失败」→ Step 11 在分流前先 `validate`,失败即 `rejected`,测试 Step 6。✅
- `incomplete` 仍跳过不回写 → Step 11 保留该分支。✅(回归由既有 happy_pipeline 覆盖 multi_error 等)
- `SyncResult.updated` 计数 + CLI/Web 展示 → Step 9/13(CLI)、Task 2(Web)。✅
- 飞书回写失败仍 `_safe_writeback` 吞 + 计 `writeback_failed` → Step 12 沿用。✅
- 测试 6 项(覆盖 pending / failed 重置 / success 拒绝 / running 跳过 / 改坏回写失败 / 全新行回归)→ Step 2-7 + 既有 `test_sync_happy_pipeline` 作全新行回归。✅

**Placeholder scan:** 无 TBD/TODO;每个 code step 含完整代码;命令含预期输出。✅

**Type consistency:**
- `SyncResult.updated: int`(Step 9)被 `result.updated`(Step 12)、`result.output "覆盖更新"`(Step 13)、`result.updated`(Task 2)一致引用。✅
- `_apply_overwrite(session, video, task, record_id, v_result, platform, now)` 定义(Step 10)与调用(Step 11)签名一致。✅
- `_PUBLISHED_REFUSE_MSG` 定义(Step 10)与使用(Step 12)一致。✅
- 桶变量 `accepted/updated/rejected/skipped/published_refused/running_skipped/skipped_incomplete`(Step 11 声明)与汇总(Step 12)一一对应。✅
