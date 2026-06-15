# 快手平台接入设计 (kuaishou)

> 日期:2026-06-15
> 新增平台 key:`kuaishou`(中文名:快手)
> 目标平台:快手创作者平台 `https://cp.kuaishou.com/article/publish/video`

本文档遵循 `CLAUDE.md` 的「新增一个平台(操作手册)」5 步法。与抖音接入同属**最干净的一类平台新增**:
不改 config / notify / browser / validator / setup / errors / models / feishu / TaskBundle,
只新增 2 个平台文件 + 改 2 处登记(platform_meta + publisher)。结构照搬 `douyin.py`。

---

## 1. 范围与已确认决策

| 维度 | 决策 | 影响 |
|---|---|---|
| 反检测档位 | `needs_fingerprint=False`(跟抖音/淘宝一致) | 用 `cookies.json` 显式持久化,不注入 per-account 指纹;`browser_context` 已自动处理 |
| 发布策略 | 只支持定时发布(跟视频号/抖音一致) | 飞书「定时发布时间」必填,无立即发布分支 |
| 内容类型 | **只发视频**(`KSVideo` 流程) | 复用 Video 单文件 1:1 task,不做快手图文(`KSNote`) |
| 平台特有飞书字段 | 标准短视频字段 | 复用共有 标题/描述/标签/封面;`field_map_defaults={"tags":"标签","cover":"封面文件"}`(同抖音);**无合集、无原创**(快手发布页本就没有) |
| 标题 | 快手发布页**无独立标题输入框** | `_fill_description` 把 `description or title` 填进「描述」框;不实现 `_fill_title` |
| 话题标签上限 | **≤ 3 个**(快手平台上限,沿用参考) | `_add_tags` 取 `tags[:3]`;抖音侧无此上限 |
| 平台 key / 中文名 | `kuaishou` / `快手`,配置文件 `config_kuaishou.yaml` | 快手创作者平台无品牌后缀 |
| remote_url 抽取 | 不抽取 | 定时发布到点前无公开链接(同抖音) |
| APC 守门 | 实现(对齐抖音/视频号注入点) | dev-mode 永远放行;拒绝时在 `verify_login` 后装「等待上传区超时」故障 |

### 参考实现

`_ref/social-auto-upload/uploader/ks_uploader/main.py` 的 `KSVideo`(异步脚本式 + patchright)。
我们**重写**成同步 patchright + adapter 模式,**保留它的选择器选择与等待策略**(踩坑成果)。
图文 `KSNote`、立即发布分支不移植。

**选择器定稿方式**:参考项目的选择器可能与当前线上页面有出入,本次**先按参考移植 + 标注"未实跑"**,
之后用 `wxsp run --task-id N --dry-run` 对真实页校验微调(与抖音起步方式一致,现在不需要快手账号)。

---

## 2. 改动清单

| 文件 | 改动 | 说明 |
|---|---|---|
| `wxsp/platforms/kuaishou_selectors.py` | 新增 | URL、登录态判定、各步骤元素、`RISK_CONTROL_KEYWORDS`、`SUCCESS_INDICATORS`;标注未实跑 |
| `wxsp/platforms/kuaishou.py` | 新增 | 步骤函数 + `_pre_publish`/`_post_publish` + `KUAISHOU_SPEC` + `KuaishouPublisher`(含 `login`) |
| `wxsp/platform_meta.py` | +1 条 `PlatformMeta` | 见 §3 |
| `wxsp/publisher.py` | +1 行 + import | `_PUBLISHERS["kuaishou"] = KuaishouPublisher()` |
| `config_kuaishou.yaml` | 生成 | 由 `wxsp setup` 选「快手」生成;账号后续去 `/config` 加 |

**不改动**(全部从 `platform_meta.REGISTRY` 读):`config.py` / `notify.py` / `browser.py` /
`validator.py` / `api/routes_setup.py` / `api/routes_config.py` / `errors.py` / `models.py` /
`feishu.py` / `platforms/base.py`(TaskBundle)。

**错误类型 / 步骤名复用**:全程只用现有错误类型 `CookieExpired` / `UploadFailed` / `RiskControl` /
`ElementNotFound` / `NetworkError`,无新错误 → `errors.py` 与 `notify._ERROR_TYPE_CN` 不动。
步骤名 `open_publish` / `verify_login` / `wait_upload_area` / `upload` / `desc` / `tags` / `cover` /
`schedule` / `risk` / `publish` / `wait_success` 已全部在 `notify._STEP_CN` 中(已核对),不动 notify。
**不使用** `title` 步骤(快手无标题框)。

---

## 3. platform_meta 登记表条目

```python
"kuaishou": PlatformMeta(
    key="kuaishou",
    label="快手",
    title_min=1,                 # 快手标题无最小字数(实际填进「描述」框)
    login_meta={
        # 未登录访问上传页 → 重定向到 passport.kuaishou.com 扫码;URL 含该片段 = 未登录(同淘宝 url 模式)
        "home_url": "https://cp.kuaishou.com/article/publish/video",
        "mode": "url",
        "login_fragment": "passport.kuaishou.com",
    },
    field_map_defaults={"tags": "标签", "cover": "封面文件"},
    needs_fingerprint=False,
),
```

---

## 4. 发布步骤(`kuaishou.py`,翻译自 `KSVideo`)

关键 URL(来自参考):
- 上传页 `https://cp.kuaishou.com/article/publish/video`(URL pattern `**/article/publish/video**`)
- 成功跳转 `https://cp.kuaishou.com/article/manage/video?status=2&from=publish`

`_pre_publish`(止于 dry-run gate 之前):

1. `open_publish` — `goto` 上传页,等 URL pattern;超时 → `NetworkError`
2. `verify_login` — URL 含 `passport.kuaishou.com` → `CookieExpired`(不重试)
3. **APC 守门** — `wxsp.apc.check_pass()`;拒绝 → `wait_upload_area` 步装 45–75s 故障后截图 + `raise ElementNotFound("等待上传区域超时")`(对齐抖音)
4. `upload` — 点 `button[class^='_upload-btn']` 走 `expect_file_chooser` 选文件;关闭「我知道了」按钮 + Joyride 引导遮罩;轮询 `text=上传中` count==0 = 完成,`text=上传失败` → 重传一次仍失败 → `UploadFailed`;超时 → `UploadFailed`
5. `desc` — 点「描述」框(`get_by_text("描述")` 的 sibling div),清空后输入 `description or title`,回车;**无独立标题步骤**
6. `tags` — `tags[:3]` 逐个 `type("#" + tag + " ")`
7. `cover` — `cover_path` 为空则跳过;否则走「封面设置」弹窗:上传封面 tab → file input → 确认(best-effort,首次用按需微调)
8. `schedule` — 切「定时发布」radio(`label.ant-radio-wrapper` 含「定时发布」)→ 点 `input[placeholder="选择日期时间"]` → 用 React 兼容方式(native value setter + `input`/`change` 冒泡事件)写入 `%Y-%m-%d %H:%M:%S` → 回车。ant-design DatePicker 是 controlled component,必须走 native setter
9. `risk` — 扫 body 文本命中 `RISK_CONTROL_KEYWORDS`(先沿用抖音:操作频繁/操作过于频繁/请稍后再试/账号异常,dry-run 时按快手实际文案补)→ `RiskControl`(账号暂停 24h)

**★ DRY_RUN GATE ★**(由 `runner.run_publish` 统一截断)

`_post_publish`:

10. `publish` — 点 `发布`(exact)→ 点 `确认发布`
11. `wait_success` — 等跳转到 manage URL pattern;超时 → `ElementNotFound`。不抽 remote_url

`login(account)`(不接 Settings):开浏览器(`browser_context(user_data_dir, headless=False, account_id, platform="kuaishou")`)`goto` 上传页 → 重定向到 passport 扫码 → 轮询 300s,URL 离开 `passport.kuaishou.com`(回到 `cp.kuaishou.com`)即成功;cookies.json 由 `browser_context` 退出时 flush(模仿 `taobao_guanghe.login`)。

---

## 5. 验证标准

1. `pytest` 全绿:
   - `test_platform_meta_single_source`(注入假平台的回归)继续通过
   - `test_cli_run.py::...` 断言 `len(calls) == len(ALL_PLATFORMS)`——`ALL_PLATFORMS` 自动含 `kuaishou`,会自动覆盖
   - 仿照 `test_douyin_platform.py` 加一个 `test_kuaishou_platform.py`:断言 `"kuaishou" in ALL_PLATFORMS`、`get_meta("kuaishou")` 字段正确、`publisher._PUBLISHERS["kuaishou"]` 存在且 `platform_key == "kuaishou"`
2. `wxsp setup` 选「快手」能生成 `config_kuaishou.yaml`
3. `wxsp run --task-id N --dry-run` 跑通到 dry-run gate(开浏览器 → 上传 → 填表 → 定时 → 风控探测 → 截图返回),据此对真实页校验并微调选择器

---

## 6. 已知风险 / 待 dry-run 校验项

- 选择器全部未对当前线上页实跑,以参考实现为准;`描述`框 sibling-div 定位、`封面设置`弹窗结构、ant DatePicker placeholder 文案最易随改版漂移
- 风控关键词为抖音借用,快手实际文案待补
- 封面弹窗流程(`set_thumbnail`)未端到端实跑,首次用自定义封面时按需微调
