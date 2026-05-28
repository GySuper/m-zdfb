# 淘宝光合平台 — 多平台扩展设计

> 2026-05-28，brainstorm 产出，待用户审阅后进入 implementation plan。

## 1. 目标

在现有视频号发布工具（wxsp）基础上，新增**淘宝光合平台**的视频发布能力。两个平台由不同运营团队独立使用，共用同一套系统但配置/调度/通知各自独立。

## 2. 架构决策：PlatformPublisher 协议 + platforms/ 包

选择方案 A：定义 `PlatformPublisher` 协议，将现有 publisher.py 拆分后移入 `platforms/` 包。

### 2.1 目录结构

```
wxsp/
├── platforms/                          # 新包
│   ├── __init__.py
│   ├── base.py                         # PlatformPublisher 协议 + PublishResult
│   ├── tencent_channel.py              # 现有 publisher.py 移入（只做浏览器交互）
│   ├── tencent_selectors.py            # 现有 selectors.py 移入
│   ├── taobao_guanghe.py               # 淘宝光合发布实现
│   └── taobao_selectors.py             # 淘宝光合选择器
├── publisher.py                        # 薄路由层：读 task.platform → 调对应 platform
├── login.py                            # 统一登录入口（视频号扫码 + 淘宝手动登录）
├── config.py                           # 新增 platform 级配置段
├── scheduler.py                        # 每平台独立 cron
├── feishu.py                           # 支持多表（每平台一张）
├── validator.py                        # 支持淘宝字段校验
└── models.py                           # Task/Account 新增 platform 字段
```

### 2.2 PlatformPublisher 协议

```python
class PlatformPublisher(Protocol):
    platform_key: str

    def publish_one(self, task_id: int, *, dry_run: bool, settings: Settings) -> PublishResult: ...
    def login(self, account: Account, settings: Settings) -> bool: ...
```

**核心原则**：platform 实现只管"打开浏览器 → 填表单 → 点发布"，不碰 DB/通知/飞书回写。publish 顶层的 DB 写回、截图存档、异常分类、飞书回写、通知仍在 `publisher.py`，platform 只负责浏览器交互。

### 2.3 视频号存量

`publisher.py` 和 `selectors.py` 的代码移入 `platforms/tencent_channel.py` 和 `platforms/tencent_selectors.py`，**逻辑完全不动，只搬家**。`publisher.py` 变为薄路由层。

## 3. 数据模型变更

### 3.1 Account

```python
class Account(SQLModel, table=True):
    ...
    platform: str = "tencent_channel"  # "tencent_channel" | "taobao_guanghe"
```

### 3.2 Task

```python
class Task(SQLModel, table=True):
    ...
    platform: str = Field(index=True)  # 从关联的 Account 冗余，便于按平台查询
```

### 3.3 Video

不变。`Video.id` 仍为飞书 record_id，不同平台的飞书表 record_id 天然不冲突。

## 4. 配置系统

### 4.1 共享配置（不改动）

`app.*`、`paths.*`、`webui.*`、`monitoring.log_retention_days`、`monitoring.screenshot_retention_days`、`monitoring.backlog_warn_threshold`

### 4.2 平台级配置

每个平台独立配置调度、发布参数、飞书表、通知渠道：

```yaml
platforms:
  tencent_channel:
    scheduler:
      daily_cron_hour: 9
      daily_cron_minute: 0
    publisher:
      headless: false
      upload_timeout_seconds: 600
      step_pause_seconds: [1, 3]
      max_concurrent_accounts: 1
    feishu:
      enabled: true
      app_id: cli_xxxxxxxxxx
      app_secret: ${FEISHU_APP_SECRET}
      bitable:
        app_token: xxxxxxxxxxxx
        table_id: tbl_xxxxxxxx
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
    monitoring:
      cookie_warn_days: 1.5
      notifiers:
        wecom:
          enabled: true
          webhook: ${WECOM_BOT_WEBHOOK_SPH}
      notify_on:
        - cookie_expired
        - cookie_warning
        - risk_control
        - task_failed
        - element_not_found
        - nas_unreachable
        - backlog_high

  taobao_guanghe:
    scheduler:
      daily_cron_hour: 10
      daily_cron_minute: 0
    publisher:
      headless: false
      upload_timeout_seconds: 600
      step_pause_seconds: [1, 3]
      max_concurrent_accounts: 1
    feishu:
      enabled: true
      app_id: cli_yyyyyyyyyy
      app_secret: ${FEISHU_APP_SECRET_TAOBAO}
      bitable:
        app_token: yyyyyyyyyyyy
        table_id: tbl_yyyyyyyy
      field_map:
        video_file: "视频文件"
        title: "标题"
        description: "描述"
        topic: "话题活动"
        product_ids: "商品ID"
        execute_date: "执行日期"
        publish_at: "定时发布时间"
        declaration: "创作者声明"
        ai_optimize: "AI优化"
        account: "账号"
        status: "状态"
        remote_url: "已发布链接"
        error_message: "错误信息"
    monitoring:
      cookie_warn_days: 1.5
      notifiers:
        wecom:
          enabled: true
          webhook: ${WECOM_BOT_WEBHOOK_TAOBAO}
      notify_on:
        - cookie_expired
        - cookie_warning
        - risk_control
        - task_failed
        - element_not_found
        - nas_unreachable
        - backlog_high
```

### 4.3 账号配置

```yaml
accounts:
  account_a:
    platform: tencent_channel
    display_name: "美食号"
    enabled: true
    daily_limit: 20
    user_data_dir: ./data/chrome-profiles/account_a
    video_search_root: "{nas_root}/videos/account_a"
    cover_search_root: "{nas_root}/covers/account_a"

  taobao_a1:
    platform: taobao_guanghe
    display_name: "淘宝号A"
    enabled: true
    daily_limit: 10
    user_data_dir: ./data/chrome-profiles/taobao_a1
    video_search_root: "{nas_root}/videos/taobao_a"
    cover_search_root: "{nas_root}/covers/taobao_a"
```

## 5. 淘宝光合发布页结构

### 5.1 URL

- 发布页：`https://creator.guanghe.taobao.com/page/pubNew/video`
- 登录页：`https://login.taobao.com/`
- 注意：发布表单在 iframe 内（域名 `huodong.taobao.com`），所有选择器需 `page.frame_locator(iframe)` 后定位

### 5.2 表单字段

| 字段 | 必填 | 约束 | 自动化方式 |
|------|------|------|-----------|
| 视频上传 | ✓ | mp4、9:16、1080P+、≤30min、≤1.5GB | `input[type="file"]` |
| 封面 | ✓ | 视频上传后平台自动生成 | 等待生成完成即可 |
| 标题 | — | max 30 chars | textbox 填入 |
| 描述 | — | max 1000 chars，富文本 | 富文本填入 |
| 话题活动 | ✗ | 搜索 → 点击选中 → 确认提交 | 搜索框输入 → 点搜索结果卡片 → 确认提交 |
| 推荐音乐 | ✗ | — | **跳过，不处理** |
| 关联商品 | ✗ | 最多 6 个，商品和店铺互斥 | 搜索商品ID → 勾选 → 确定 |
| 定时发布 | ✗ | YYYY/MM/DD + HH:mm + 日历 | radio 启用 → 填日期 → 填时间 → 确定 |
| 创作者声明 | ✓ | 6 选 1，默认"无需标注" | radio 选择 |
| AI优化 | ✗ | toggle 开关 | 飞书字段控制 |
| 允许下载 | ✗ | checkbox，默认勾选 | **取消勾选** |

### 5.3 话题选择弹窗

- 触发：点击"参与话题活动"区域
- 弹窗结构：搜索框 + 搜索/重置按钮 + 话题卡片列表 + 确认提交/取消
- 流程：`fill(关键词)` → `click("搜索")` → `click(匹配的话题卡片)` → `click("确认提交")`
- 左侧有分类 tab（推荐/时尚/美妆等 16 个），搜索后直接出结果，不依赖分类导航

### 5.4 商品选择弹窗

- 触发：点击"添加商品"
- 弹窗结构：tab（本店商品/本店推荐）+ 搜索框 + 商品列表（checkbox）+ 已选商品 + 确定/取消
- 流程：`fill(商品ID)` → `click("搜索")` → `check(匹配商品)` → `click("确定")`
- 约束：最多 6 个商品，商品和店铺互斥

### 5.5 定时发布

- 启用：点击"定时发布" radio
- 日期选择器弹窗：YYYY/MM/DD 文本框 + HH:mm 文本框 + 日历 grid + 选择时间/确定按钮
- 启用后提交按钮从"立即发布"变为"定时发布"

## 6. 淘宝发布流程（~18 步）

```
[0]  claim_task(task.id)
[1]  stage_video_to_tmp()
[2]  launch_browser(account)           # 独立 user_data_dir + per-account 指纹
[3]  open_publish_page()              # 操作在 iframe 内
[4]  verify_logged_in()               # 检测是否跳到 login.taobao.com
[5]  upload_video(file)               # 等上传 + 处理完成
[6]  wait_cover_generated()           # 等平台自动生成封面
[7]  fill_title()                     # max 30 chars
[8]  fill_description()              # 富文本, max 1000 chars
[9]  add_topic(topic_name)            # 搜索话题 → 选中 → 确认
[10] add_products(product_ids)        # 搜索商品ID → 勾选 → 确定
[11] set_schedule(publish_at)         # 定时发布 radio → 日历选择器
[12] set_declaration(type)            # 创作者声明 radio（6选1）
[13] toggle_ai_optimize(on/off)       # AI优化 switch
[14] disable_download()               # 取消"允许下载" checkbox
─────── ★ DRY_RUN GATE ★ ──────
[15] click_publish()                  # 按钮文案可能为"定时发布"
[16] wait_for_success_indicator()
[17] close_browser()
[18] cleanup_tmp()
```

每步失败截图到 `logs/screenshots/{YYYYMM}/{task_id}_{step}.png`。

## 7. 错误分类

与视频号共用同一套错误类型（`errors.py`），新增淘宝特有：

| 错误类型 | 含义 | 重试 |
|---------|------|------|
| `product_not_found` | 飞书填的商品ID在弹窗搜索无结果 | 不重试 |
| `topic_not_found` | 飞书填的话题名搜索无结果 | 不重试 |
| `login_required` | 淘宝登录页出现（cookie 过期） | 不重试，等同 cookie_expired |

## 8. 登录

与视频号相同模式：开浏览器 → 用户自己登录 → 检测登录态 → 关浏览器。

```python
def _login_taobao_guanghe(account, settings):
    # 1. 开 browser，导航到发布页
    # 2. 若跳转到 login.taobao.com → 等待用户完成登录（手动）
    # 3. 检测到已进入发布页（未跳转 login）→ 登录成功
    # 4. cookie 由 persistent context 自动保存到 user_data_dir
```

CLI：`wxsp login <account_id>`（和视频号一样）

## 9. 调度器

- 每平台独立 cron job（各自配置 `scheduler.daily_cron_*`）
- 各自 sync 自己的飞书表 → 扫各自的 pending task → 按 `publish_at` 升序串行跑
- **账号级 halt**：仅限同平台同账号（淘宝被风控不影响视频号）
- **全局 halt**（`element_not_found / nas_unreachable`）：仅限同平台（淘宝改版不影响视频号）
- `wxsp run --today --platform taobao_guanghe` 可指定只跑淘宝

## 10. 飞书表

### 10.1 淘宝飞书字段（运营填写）

| 飞书字段名 | 类型 | 必填 | 说明 |
|----------|------|-----|------|
| 视频文件 | 单行文本 | 是 | 裸文件名 或 完整 NAS 路径 |
| 标题 | 单行文本 | 是 | max 30 字 |
| 描述 | 多行文本 | 否 | max 1000 字 |
| 话题活动 | 单行文本 | 否 | 话题名，程序搜索匹配 |
| 商品ID | 单行文本 | 否 | 淘宝商品ID，逗号分隔最多 6 个 |
| 执行日期 | 日期 | 是 | daemon 只跑 execute_date=today |
| 定时发布时间 | 日期时间 | 是 | 光合页面填的发布时刻 |
| 创作者声明 | 单选 | 是 | 6 选 1 |
| AI优化 | 复选框 | 否 | 开/关 |
| 账号 | 单选 | 否 | 空 → round-robin |
| 状态 | 单选 | 否 | 工具回写 |
| 已发布链接 | URL | 否 | 工具回写 |
| 错误信息 | 多行文本 | 否 | 工具回写 |

### 10.2 校验规则

- `execute_date ≤ date(定时发布时间)`
- 标题/视频文件/执行日期/定时发布时间/创作者声明 任一为空 → 跳过不回写（草稿）
- 商品ID 搜索无结果 → `product_not_found` 回写
- 话题名搜索无结果 → `topic_not_found` 回写

## 11. Web UI

### 11.1 配置页

飞书表/调度/通知配置按**平台 Tab** 切换（"视频号" Tab / "淘宝光合" Tab），每 Tab 下编辑各自的配置。全局共享配置（app/paths/monitoring 通用部分）保持单面板。

### 11.2 其他页面

- **Dashboard**：按平台显示（Tab 切换或并排卡片），各自显示今日进度和积压
- **Accounts**：增加 `platform` 筛选列
- **Tasks**：增加 `platform` 筛选条件
- **Logs**：SSE 流增加 `platform` 过滤

## 12. 回滚策略

- `publisher.py` 重命名为 `platforms/tencent_channel.py` 时，原文件改为 import + 透传（兼容旧引用），确认一切正常后再删旧文件
- `config.yaml` 同时支持旧 flat 格式和新 `platforms.*` 嵌套格式，`config.py` 优先读新格式，fallback 旧格式
- 每步改动 solo commit，便于 git bisect

## 13. 不做的

- 不做淘宝直播发布（本次只做短视频）
- 不做淘宝图文发布
- 不做商品自动推荐/选品策略
- 不做淘宝数据分析
- 不做"光合要下线了"的应对（用户确认平台继续运营）
