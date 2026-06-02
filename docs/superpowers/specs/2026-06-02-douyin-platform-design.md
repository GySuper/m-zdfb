# 抖音平台接入设计 (douyin)

> 日期:2026-06-02
> 新增平台 key:`douyin`(中文名:抖音)
> 目标平台:抖音创作者中心 `https://creator.douyin.com/creator-micro/content/upload`

本文档遵循 `CLAUDE.md` 的「新增一个平台(操作手册)」5 步法。本次接入是**最干净的一类平台新增**:
不改 config / notify / browser / validator / setup / errors / models / feishu / TaskBundle,
只新增 2 个平台文件 + 改 2 处登记(platform_meta + publisher)。

---

## 1. 范围与已确认决策

| 维度 | 决策 | 影响 |
|---|---|---|
| 反检测档位 | `needs_fingerprint=False`(跟淘宝一致) | 用 `cookies.json` 显式持久化,不注入 per-account 指纹 |
| 发布策略 | 只支持定时发布(跟视频号一致) | 飞书「定时发布时间」必填,无立即发布分支 |
| 内容类型 | 只发视频 | 复用 Video 单文件 1:1 task,不碰图文/多图 |
| 平台特有飞书字段 | 无 | 只用共有字段 标题/描述/标签/封面,`field_map_defaults={}` |
| 平台 key / 中文名 | `douyin` / `抖音`,配置文件 `config_douyin.yaml` | 抖音创作者中心无品牌后缀,不加 |
| 参考实现的「第三方」开关 | **不实现(跳过)** | 用途不明的开关默认置 ON 有风险,守则不允许 |

### 参考实现

`_ref/social-auto-upload/uploader/douyin_uploader/main.py`(已 pull 到 dreammis/social-auto-upload 最新;
该文件本次 pull 未变动,即当前完整实现)。它是异步脚本式 + 原版 Playwright;我们**重写**成同步
patchright + adapter 模式,**保留它的选择器选择与等待策略**(踩坑成果)。

---

## 2. 改动清单

| 文件 | 改动 | 说明 |
|---|---|---|
| `wxsp/platforms/douyin_selectors.py` | 新增 | URL、登录态判定、各步骤元素、`RISK_CONTROL_KEYWORDS`、`SUCCESS_INDICATORS` |
| `wxsp/platforms/douyin.py` | 新增 | 步骤函数 + `_pre_publish`/`_post_publish` + `DOUYIN_SPEC` + `DouyinPublisher`(含 `login`) |
| `wxsp/platform_meta.py` | +1 条 `PlatformMeta` | 见 §3 |
| `wxsp/publisher.py` | +1 行 | `_PUBLISHERS["douyin"] = DouyinPublisher()` + import |
| `config_douyin.yaml` | 生成 | 由 `wxsp setup` 选「抖音」生成;账号后续去 `/config` 加 |

**不改动**(全部从 `platform_meta.REGISTRY` 读):`config.py` / `notify.py` / `browser.py` /
`validator.py` / `api/routes_setup.py` / `api/routes_config.py` / `errors.py` / `models.py` /
`feishu.py` / `platforms/base.py`(TaskBundle)。

---

## 3. platform_meta 登记表条目

```python
"douyin": PlatformMeta(
    key="douyin",
    label="抖音",
    title_min=1,              # 抖音标题无最小字数;上限 30,代码内 title[:30] 截断
    login_meta={
        "home_url": "https://creator.douyin.com/creator-micro/content/upload",
        "mode": "selector",   # 已登录 → 上传区可见;未登录 → 被登录页/遮罩挡住
        "selector": "<上传区/创作中心专属元素;实现时对真页面定稿>",
    },
    field_map_defaults={},    # 无平台特有字段
    needs_fingerprint=False,
),
```

`ALL_PLATFORMS` 自动派生,无需手改。回归保障 `tests/test_platform_meta_single_source.py` 注入假平台,
本条目加入后该测试仍应通过(各消费方都从 REGISTRY 读)。

### 登录态检测(login_meta)说明

- **`mode="selector"`**:`browser.wait_for_logged_in` 导航到 `home_url`,等 `selector` 可见即判已登录。
  抖音未登录时上传页被登录页/扫码遮罩挡住,登录态专属元素不可见 → 正确判未登录。
- 该 selector 的最终取值是 selectors 文件(易变文件)关注点,**实现时对真实页面验证定稿**;
  候选:上传拖拽区容器 / 创作中心侧栏专属入口。
- doctor 的快速 cookie 检查(`check_cookie`,timeout 15s)即走此 login_meta。

---

## 4. 发布流程

视频本体由共享编排器 `runner.run_publish` 已 `stage_to_tmp` 好,经 `staged` 传入 `_pre_publish`。
封面在 `_pre_publish` 内按需 `stage_to_tmp`(仅当配了封面),对齐 tencent 的做法。

步骤键(`ctx.last_step`)**全部使用 `notify._STEP_CN` 已存在的规范键**,确保告警不漏英文。

### `_pre_publish(page, bundle, staged, ctx)` — 止于 dry-run gate 之前

| 步骤键 | 动作 | 失败错误 |
|---|---|---|
| `open_publish` | 导航到 upload 页,等 URL 稳定(两种发布页 URL 变体都接受) | `NetworkError` |
| `verify_login` | 出现「扫码登录 / 手机号登录」→ 未登录 | `CookieExpired`(不重试) |
| `upload` | `set_input_files` 上传视频 → 轮询「重新上传」标记判完成 | `UploadFailed`(超时 `upload_timeout_seconds`) |
| `title` | 填标题输入框,`title[:30]` | — |
| `desc` | 填描述(contenteditable);无描述时回退用标题 | — |
| `tags` | 逐个 ` #tag` + Space(解析 `bundle.tags_json`) | — |
| `cover` | 有封面 → `stage_to_tmp` 后上传横版封面;无封面 → 选第一个「推荐封面」 | — |
| `schedule` | 点「定时发布」radio → 填 `publish_at.strftime("%Y-%m-%d %H:%M")` | `ElementNotFound` |
| `risk` | 扫 body 文本命中 `RISK_CONTROL_KEYWORDS` | `RiskControl`(账号暂停 24h) |

### `_pre_publish` 之后:DRY_RUN GATE(在 `runner` 内)

`--dry-run` 在此截图返回,不点发布。点发布只写在 `_post_publish`(dry-run 红线)。

### `_post_publish(page, bundle, ctx)` — dry-run gate 之后

| 步骤键 | 动作 | 失败错误 |
|---|---|---|
| `publish` | 点「发布」按钮 | `ElementNotFound` |
| `wait_success` | 等跳转到 `.../content/manage` 判成功 | `ElementNotFound`(判定超时) |

**remote_url**:抖音定时发布在到点前无公开链接 → 跟淘宝一致,**不抽取 remote_url**(留 `None`,非错误)。

---

## 5. 登录 + cookie

`DouyinPublisher.login(account)` 自写(参照 `TaobaoGuanghePublisher.login`):

```
开 browser_context(user_data_dir, headless=False, account_id=account.id, platform="douyin")
  → 导航 https://creator.douyin.com/(显示二维码)
  → 轮询(最长 ~300s)直到登录完成:URL 进 creator-micro/home 且无「扫码登录/手机号登录」文案
  → 成功 return True;超时 return False
```

cookie 由 `browser.browser_context` 的 `needs_fingerprint=False` 分支自动用 `cookies.json` 存取
(进入时 `add_cookies`,退出时 `context.cookies()` 落盘),**无需在 adapter 里写持久化代码**。

---

## 6. selectors 文件(douyin_selectors.py)要点

参照 taobao_selectors.py 的组织。优先语义化选择器(`text=` / `role=` / `placeholder=`),少用脆弱 CSS class。
从参考实现迁移并对真页面校验定稿:

- 发布页 URL(及两种发布页 URL 变体)、`creator-micro/content/manage` 成功页
- 登录态判定:`扫码登录` / `手机号登录` 文案、登录态专属元素 selector
- 视频文件 input、上传完成标记(`重新上传`)、上传失败标记
- 标题 input、描述 contenteditable
- 封面:`选择封面` 入口、上传 input、`推荐封面` / 「请设置封面后再发布」
- 定时:「定时发布」radio、`日期和时间` input
- 发布按钮
- `RISK_CONTROL_KEYWORDS`:保守集合(如「操作频繁」「请稍后再试」「账号异常」),实现时按实测微调
- `SUCCESS_INDICATORS`:跳转 `content/manage` 为主判据

---

## 7. 抖音定时发布约束

参考实现要求定时最早 = 当前时间 + **2 小时**(对比视频号 30 分钟)。**以页面校验为准,不在代码里硬编码**;
若飞书填的时间过近,抖音页面会拒绝设置 → 表现为 `schedule` 步骤 `ElementNotFound`,正常暴露并告警。

---

## 8. 明确不做(YAGNI)

- 图文 / 多图笔记(`DouYinNote`)
- 购物车商品链接(`productLink` / `productTitle`)
- 地理位置(`set_location`)
- 立即发布分支
- 参考实现里用途不明的「第三方」开关(默认不动)

如后续要加上述任一项,按 `CLAUDE.md`「需要全新飞书字段时」的整条链补字段。

---

## 9. 成功标准(验收)

1. `wxsp setup` 选「抖音」能生成 `config_douyin.yaml`。
2. `wxsp login <douyin_account>` 弹二维码,扫码后 cookie 落 `cookies.json`,`doctor` 显示登录态 ok。
3. `pytest` 全绿,含 `test_platform_meta_single_source.py`(单一信息源回归)。
4. `wxsp run --task-id <N> --dry-run` 对一条抖音任务跑通 `_pre_publish` 全步骤,在 dry-run gate 截图返回,
   **不**真发布;截图显示标题/描述/标签/封面/定时已正确填好。
5. Web UI 平台切换器出现「抖音」,各页面数据按 `?platform=douyin` 隔离。
6. 冒烟(发版前手动):测试号 + 预制视频跑非 dry-run,定时发布成功跳转 `content/manage`,飞书回写「已发布」。
