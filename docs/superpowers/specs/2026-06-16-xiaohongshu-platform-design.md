# 小红书平台接入设计 (xiaohongshu)

> 日期:2026-06-16
> 新增平台 key:`xiaohongshu`(中文名:小红书)
> 目标平台:小红书创作者中心视频发布页 `https://creator.xiaohongshu.com/publish/publish?from=homepage&target=video`

本文档遵循 `CLAUDE.md` 的「新增一个平台(操作手册)」5 步法。与抖音/快手接入同属**最干净的一类平台新增**:
不改 config / notify / browser / validator / setup / errors / models / feishu / TaskBundle,
只新增 2 个平台文件 + 改 2 处登记(platform_meta + publisher)。结构照搬 `douyin.py`。

---

## 1. 范围与已确认决策

| 维度 | 决策 | 影响 |
|---|---|---|
| 内容类型 | **只发视频笔记**(`XiaoHongShuVideo` 流程) | 复用 Video 单文件 1:1 task,不做图文笔记(`XiaoHongShuNote`,需多图字段,属独立子项目) |
| 反检测档位 | `needs_fingerprint=False`(跟抖音/快手/淘宝一致) | 用 `cookies.json` 显式持久化,不注入 per-account 指纹;`browser_context` 已自动处理。参考实现也走 `storage_state` 路线 |
| 发布策略 | 只支持定时发布(跟视频号/抖音/快手一致) | 飞书「定时发布时间」必填,无立即发布分支;点「定时发布」按钮 |
| 平台特有飞书字段 | 标准短视频字段 | 复用共有 标题/描述/标签/封面;`field_map_defaults={"tags":"标签","cover":"封面文件"}`(同抖音/快手);**无合集、无原创** |
| 标题 | 小红书视频标题上限 20 字 | `_fill_title` 截断 `title[:20]`;`title_min=1`(平台无最小字数硬限) |
| 话题标签 | `#tag` 键入正文 + 从下拉选中才生效 | `_add_tags` 逐个 `type("#"+tag)` → 等 `#creator-editor-topic-container` → 点第一个候选项 |
| 平台 key / 中文名 | `xiaohongshu` / `小红书`,配置文件 `config_xiaohongshu.yaml` | 用完整拼音 key(对齐参考目录 `xiaohongshu_uploader`),非 `xhs`/`redbook` |
| remote_url 抽取 | 不抽取 | 定时发布到点前无公开链接(同抖音/快手) |
| headless | 跟随全局 `publisher.headless`(默认 false) | 不额外硬编码禁用(非视频号系强风控平台) |

### 参考实现

`_ref/social-auto-upload/uploader/xiaohongshu_uploader/main.py` 的 `XiaoHongShuVideo`(异步脚本式 + patchright)。
我们**重写**成同步 patchright + adapter 模式,**保留它的选择器选择与等待策略**(踩坑成果)。
图文 `XiaoHongShuNote`、立即发布分支、`set_location`(位置)不移植。

**选择器定稿方式**:参考项目的选择器对真实页校验过(约 2026-03),但**未在本仓库对当前线上页二次校验**。
本次**先按参考移植 + 标注「未实跑」**,之后用 `wxsp run --task-id N --dry-run` 对真实页校验微调
(与抖音/快手起步方式一致)。**完整 dry-run 实跑需要一个小红书测试号扫码登录**;在此之前选择器按
「已移植、未实测」对待(写进选择器文件头注释)。

---

## 2. 改动清单

| 文件 | 改动 | 说明 |
|---|---|---|
| `wxsp/platforms/xiaohongshu_selectors.py` | 新增 | URL、登录态判定、各步骤元素、`RISK_CONTROL_KEYWORDS`、`SUCCESS_INDICATORS`;标注未实跑 |
| `wxsp/platforms/xiaohongshu.py` | 新增 | 步骤函数 + `_pre_publish`/`_post_publish` + `XIAOHONGSHU_SPEC` + `XiaohongshuPublisher`(含 `login`) |
| `wxsp/platform_meta.py` | +1 条 `PlatformMeta` | 见 §3 |
| `wxsp/publisher.py` | +1 行 + import | `_PUBLISHERS["xiaohongshu"] = XiaohongshuPublisher()` |
| `config_xiaohongshu.yaml` | 生成 | 由 `wxsp setup` 选「小红书」生成;账号后续去 `/config` 加 |

**不改动**(全部从 `platform_meta.REGISTRY` 读):`config.py` / `notify.py` / `browser.py` /
`validator.py` / `api/routes_setup.py` / `api/routes_config.py` / `errors.py` / `models.py` /
`feishu.py` / `platforms/base.py`(TaskBundle)。

**错误类型 / 步骤名复用**:全程只用现有错误类型 `CookieExpired` / `UploadFailed` / `RiskControl` /
`ElementNotFound` / `NetworkError`,无新错误 → `errors.py` 与 `notify._ERROR_TYPE_CN` 不动。
步骤名 `open_publish` / `verify_login` / `upload` / `title` / `desc` / `tags` / `cover` /
`schedule` / `risk` / `publish` / `wait_success` 已全部在 `notify._STEP_CN` 中(实现时核对),不动 notify。

---

## 3. platform_meta 登记表条目

```python
"xiaohongshu": PlatformMeta(
    key="xiaohongshu",
    label="小红书",
    title_min=1,                 # 小红书视频标题无最小字数硬限(上限 20,在 adapter 截断)
    login_meta={
        # 未登录访问视频发布页 → 重定向到 .../login;URL 含该片段 = 未登录(同淘宝 url 模式)
        "home_url": "https://creator.xiaohongshu.com/publish/publish?from=homepage&target=video",
        "mode": "url",
        "login_fragment": "creator.xiaohongshu.com/login",
    },
    field_map_defaults={"tags": "标签", "cover": "封面文件"},
    needs_fingerprint=False,
),
```

---

## 4. 发布步骤(`xiaohongshu.py`,翻译自 `XiaoHongShuVideo`)

关键 URL(来自参考):
- 登录页 `https://creator.xiaohongshu.com/login`
- 视频发布页 `https://creator.xiaohongshu.com/publish/publish?from=homepage&target=video`
- 成功跳转 pattern `**/publish/success?**`

`_pre_publish`(止于 dry-run gate 之前):

1. `open_publish` — `goto` 视频发布页 + `wait_for_url`;超时 → `NetworkError`
2. `verify_login` — URL 落在 `creator.xiaohongshu.com/login` 或登录框 `div[class*='login-box']` 可见 → `CookieExpired`(不重试)
3. `upload` — `div[class^='upload-content'] input.upload-input` set 文件;轮询上传完成标记:
   预览区(`input.upload-input` 的 sibling `div.preview-new`)文本含 `上传成功`/`分辨率`/`重新上传`/`编辑封面`/`已上传`/`100%`,
   或标题框 `input[placeholder*="填写标题"]` 出现 = 完成;超时 → `UploadFailed`;失败标记 → 重传一次仍失败 → `UploadFailed`
4. `title` — `input[placeholder*="填写标题"]`,`fill(title[:20])`
5. `desc` — `p[data-placeholder*="输入正文描述"]`,click 聚焦后键入(参考做 Backspace+全选清空,我们发布页初始为空,click 后直接键入即可)
6. `tags` — 逐个 `type("#"+tag)` → 等 `#creator-editor-topic-container` 可见 → 点 `.item` 第一个候选(让话题真正绑定);无 tags 跳过
7. `cover` — `cover_path` 为空则跳过;否则走封面弹窗:`div.cover-plugin-title`(含「设置封面」)入口 → `div.d-modal.cover-modal` → `input[type="file"][accept*="image"]` set 文件 → `button.mojito-button`(含「确定」)→ 等 modal 隐藏(best-effort,首次用按需微调)
8. `schedule` — `.custom-switch-card`(含「定时发布」)→ `.d-switch` 点开 → `.d-datepicker-input-filter input.d-text` `fill("%Y-%m-%d %H:%M")`
9. `risk` — 扫 body 文本命中 `RISK_CONTROL_KEYWORDS`(先沿用通用:操作频繁/操作过于频繁/请稍后/账号异常,dry-run 时按小红书实际文案补)→ `RiskControl`(账号暂停 24h)

**★ DRY_RUN GATE ★**(由 `runner.run_publish` 统一截断)

`_post_publish`:

10. `publish` — 点「定时发布」按钮(`button:has-text("定时发布")`)
11. `wait_success` — 等跳转到 `**/publish/success?**`;超时 → `ElementNotFound`。不抽 remote_url

`login(account)`(不接 Settings):开浏览器(`browser_context(user_data_dir, headless=False, account_id, platform="xiaohongshu")`)`goto` 登录页 → 等扫码 → 轮询 300s,URL 离开 `creator.xiaohongshu.com/login`(且登录框消失)即成功;cookies.json 由 `browser_context` 退出时 flush(模仿 `taobao_guanghe.login` / `douyin.login`)。

---

## 5. 验证标准

1. `pytest` 全绿:
   - `test_platform_meta_single_source`(注入假平台的回归)继续通过
   - 任何断言「调用次数 == `len(ALL_PLATFORMS)`」的测试(如 `test_cli_run.py`)——`ALL_PLATFORMS` 自动含 `xiaohongshu`,会自动覆盖
   - 仿照 `test_kuaishou_platform.py` 加一个 `test_xiaohongshu_platform.py`:断言 `"xiaohongshu" in ALL_PLATFORMS`、`get_meta("xiaohongshu")` 字段正确、`publisher._PUBLISHERS["xiaohongshu"]` 存在且 `platform_key == "xiaohongshu"`
2. `wxsp setup` 选「小红书」能生成 `config_xiaohongshu.yaml`
3. `wxsp run --task-id N --dry-run` 跑通到 dry-run gate(开浏览器 → 上传 → 填表 → 定时 → 风控探测 → 截图返回),据此对真实页校验并微调选择器

---

## 6. 已知风险 / 待 dry-run 校验项

- 选择器全部未对当前线上页实跑,以参考实现为准(参考侧约 2026-03 校验);`.custom-switch-card` 定时开关、`div.cover-plugin-title`/`div.d-modal.cover-modal` 封面弹窗结构、`#creator-editor-topic-container` 话题下拉、上传完成判定文案最易随改版漂移
- 风控关键词为通用借用,小红书实际文案待 dry-run 补
- 封面弹窗流程(`set_thumbnail`)未端到端实跑,首次用自定义封面时按需微调
- 登录态判定用 `url` 模式(`login_fragment="creator.xiaohongshu.com/login"`):若小红书出现「已登录但仍弹登录框遮罩、URL 不变」的情况,需在 adapter 的 `verify_login` 步加登录框可见性兜底(参考实现已含此判据)
