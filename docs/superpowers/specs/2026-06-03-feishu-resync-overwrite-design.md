# 飞书任务二次同步覆盖(re-sync overwrite)设计

> 日期:2026-06-03
> 模块:`wxsp/sync.py`(`sync_now`)
> 关联:CLAUDE.md「内容来源:飞书 Bitable」、`uq_one_task_per_video` 约束

## 背景与问题

运营在飞书填好任务、同步进库后,发现任务写错(如标题、视频文件、定时发布时间),
于是在飞书里改正,并把「状态」改回「待入库」,再点同步——结果改后的内容进不来。

根因:本地 `Video.id == 飞书 record_id`。一行任务一旦入库,
[sync.py](../../wxsp/sync.py) 的循环里:

```python
if session.get(Video, row.record_id) is not None:
    skipped.append(row.record_id)
    continue   # → 回写"已有历史任务,请在 Web UI 重试"
```

会无条件跳过并回写"已有历史任务"。这是当初为「杜绝重复入库 / 重复发布」加的护栏
(配合 `uq_one_task_per_video` 唯一约束)。本设计在**保住"不重复发布"安全前提**下,
放开"内容改正后可二次入库覆盖"。

## 目标

- 已入库的行,运营在飞书改正内容 + 状态改回「待入库」后,再同步能把改动覆盖进本地,
  并把任务重置为可重跑的 `pending`。
- **绝不导致重复发布**:已成功发布(本地 Task `status=success`)的任务不被覆盖重跑。
- 正在发布(`running`)的任务不被打断。

## 非目标

- 不改飞书拉取过滤逻辑(仍只拉「状态=待入库」的行)。
- 不引入新的飞书字段、不改数据库 schema(`uq_one_task_per_video` 保留)。
- 不做"已发布任务改正后自动重发"——这条路径走"在飞书新建一行"(对齐 1 视频→N 行约定)。

## 方案:原地 upsert

改 [sync.py](../../wxsp/sync.py) 主循环里 `session.get(Video, row.record_id) is not None`
这个分支:不再无条件 skip,而是查出该 Video 对应的 Task,先跑 `validate(...)`(与新行同一套),
再按"校验结果 + 本地 Task 状态"决定覆盖 / 拒绝 / 跳过。

**否决的备选**:

1. 删 Video+Task 重建 —— 会换 `task.id`、断掉 `Event.task_id` 外键引用、还要绕过
   `uq_one_task_per_video`,无谓复杂。
2. 去掉唯一约束、每次新建 Task —— 直接破坏"一视频一任务"不变量,且重新打开重复发布的口子。

原地 upsert 最小改动、保 `task.id` 稳定、Event 历史不断链。

## 核心流程(Video 已存在时)

Task 由 `select(Task).where(Task.video_id == record_id)` 查出(唯一约束保证 ≤1 条)。
先 `validate(...)`,再分支:

| 校验 / 本地 Task 状态 | 处理 | 飞书回写 |
|---|---|---|
| `incomplete`(4 核心字段又被清空) | 跳过,计 `skipped_incomplete` | 不回写(当草稿,留下次拉) |
| 校验失败(改坏了) | 计 `rejected` | `状态=失败` + 错因 |
| 通过 + 本地 `running` | 跳过,计 `skipped_existing` | 留 `待入库` 不动(跑完下轮再判) |
| 通过 + 本地 `success` | **拒绝**,计 `skipped_existing` | `状态=已发布` + 提示(见下) |
| 通过 + `pending`/`failed`/`skipped`/`interrupted` | **覆盖**,计 `updated` | `状态=已计划`,清空错误信息 |

### 覆盖时具体写什么

**Video**(原地刷新内容字段,主键 `id` 不变):
`file_path / title / description / tags_json / cover_path / topic / original_claim /
declaration / ai_optimize / product_ids_json`,以及 `ingested_at = now`。
即镜像现有"新建 Video"那段赋值,只是 update 而非 insert。

**Task**(原地重置为干净待跑):
- 刷新 `account_id / execute_date / publish_at / platform`(可能运营改了账号或发布时间);
- `status = "pending"`、`attempts = 0`;
- 清空 `lease_token / lease_expires_at / last_error_type / last_error_msg /
  started_at / finished_at / remote_video_id / remote_url`、`screenshots_json = "[]"`。

### 已发布(success)的拒绝回写

- **`状态` → `已发布`**(而非留在 `待入库`):否则该行一直落在 sync 的拉取过滤里,
  每次同步都重复拒绝 + 重复回写,产生噪音。改成「已发布」即移出待入库过滤。
- **`错误信息` → `"该任务已发布成功,不能改这一行重发;如需重新发布,请在飞书新建一行任务。"`**

### running 的跳过回写

留 `待入库` 不动、不回写状态。语义:它是瞬态——跑完后变 `success`(下轮被按已发布拒绝)
或 `failed`(下轮被覆盖重跑),交给下一次同步自然收敛。

## 报表 / 接口变化

- `SyncResult` 新增 `updated: int`(覆盖重入库的行数),与新建的 `accepted` 区分。
  `running` / `success` 拒绝仍计入现有 `skipped_existing`。
- CLI(`wxsp sync`)与 Web UI 的同步结果展示顺带显示 `updated`(若 0 可不显眼)。
- `_safe_writeback` 复用;新增的回写文案(已发布提示、覆盖清空错误信息)走同一函数。

## 数据流(ASCII)

```
飞书行(状态=待入库)
   │  fetch_pending_rows
   ▼
 validate(row)
   ├─ incomplete ───────────────► skip(不回写)
   ├─ 失败 ─────────────────────► rejected → 回写 状态=失败+错因
   └─ 通过
        │  session.get(Video, record_id)
        ├─ 不存在 ───────────────► 新建 Video+Task(pending) → accepted → 回写 已计划
        └─ 已存在 → 查 Task.status
              ├─ running ────────► skip(留待入库,不回写)
              ├─ success ────────► 拒绝 → 回写 状态=已发布 + 提示
              └─ 其余(pending/failed/skipped/interrupted)
                                  ► 覆盖 Video + 重置 Task(pending) → updated → 回写 已计划+清错误
```

## 错误处理

- 覆盖写库沿用现有 `session.begin_nested()` + `IntegrityError` 兜底
  (理论上原地 update 不会撞唯一约束,但保留同样的防御姿态,异常落 `skipped_existing`)。
- 飞书回写失败仍走 `_safe_writeback` 静默吞 + `logger.warning`,计入 `writeback_failed`,不打断整体 sync。

## 测试(`tests/test_sync.py` 增量)

绝不 mock 飞书行为之外的真实逻辑;沿用现有 `test_sync.py` 的假 NAS / 假飞书客户端套路:

1. **覆盖重置**:已存在 Video + Task(`pending`),飞书行改了标题/发布时间 → 同步后
   Video 标题更新、Task 仍 `pending` 且 `attempts=0`、`updated==1`。
2. **failed 也覆盖**:Task `failed` + 有 `last_error_*` → 同步后重置为 `pending`、错误字段清空、`updated==1`。
3. **success 拒绝**:Task `success` → Video 不变、Task 不变、`skipped_existing==1`,
   回写参数含 `状态=已发布` + 提示文案。
4. **running 跳过**:Task `running` → 不变、`skipped_existing==1`、不回写状态。
5. **改坏回写失败**:已存在行改成校验不过(如标题超长)→ `rejected==1`,回写 `状态=失败`。
6. **回归**:全新行仍走 `accepted`(`updated` 不受影响)。

## 验收标准

- [ ] 飞书改正已入库行 + 状态改回待入库 → 同步把改动覆盖进本地、任务重置 pending(对未发布的)。
- [ ] 已发布的行二次同步 → 本地 Task 不动,飞书回写「已发布」+ 提示,不重复发布。
- [ ] 正在跑的行二次同步 → 不被打断。
- [ ] `wxsp sync` 输出能看到 `updated` 计数。
- [ ] `pytest tests/test_sync.py` 全绿,含上述 6 个新增/回归用例。
