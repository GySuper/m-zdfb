# 拼多多平台接入设计 (pinduoduo)

> 日期:2026-07-22
> 新增平台 key:`pinduoduo`(中文名:拼多多)
> 目标平台:多多视频创作者中心发布页 `https://live.pinduoduo.com/n-creator/video/home`(首页即发布页)

本文档遵循 `platform_meta.py` 顶部 docstring 的加平台规则:写 `platforms/pinduoduo.py` +
`pinduoduo_selectors.py` + 在 `publisher._PUBLISHERS` 注册实例 + 在 `platform_meta` 加 1 条
`PlatformMeta`。与小红书接入同属**最干净的一类平台新增**:不改 config / notify / browser /
validator / setup / errors / models / feishu / TaskBundle,只新增 2 个平台文件 + 改 2 处登记。

**与所有现有平台的关键差异**:拼多多发布页 = 创作者中心首页(单页应用,无独立发布页 URL 切换);
无独立标题框(主文案是描述框,同快手);**内容声明为必填下拉**(拼多多特有);挂商品后才展开
完整表单(定时发布等选项在绑商品后才出现)。结构参考 `taobao_guanghe.py`(商品绑定 + 内容声明)。

---

## 1. 范围与已确认决策

| 维度 | 决策 | 依据/影响 |
|---|---|---|
| 发布入口 | **多多视频创作者中心**首页 `live.pinduoduo.com/n-creator/video/home` | 用户指定 SSO 登录链,登录后落地此页;首页内嵌上传区,即发布页 |
| 登录入口 | `mms.pinduoduo.com/login/sso?platform=live&accessType=auto&redirectUrl=...` | 用户提供的完整 SSO 链;扫码登录 |
| 内容类型 | 只发短视频 | 对齐其余五平台,不做直播/图文 |
| 反检测档位 | `needs_fingerprint=False`(跟抖音/快手/淘宝一致) | 用户确认「和抖音一样不注入指纹」;先 patchright persistent context,实测若一天触发风控再切 `use_real_chrome=True`(同小红书演进路线) |
| 标题 | **无独立标题框**,`has_title=False`(同快手) | 实测:发布页只有「添加视频描述」一个 contenteditable 框,无标题 input;`title_min=1`,描述框为必填核心字段,validator 改判「描述」必填 |
| 描述框 | `div[contenteditable=true].sabo-root`(DraftJS 风格),上限 500 字 | 实测可写入;字数计数器显示 `N/500` |
| 话题标签 | 描述框内键入 `#关键词` → 弹 `.caret-popover-root` 候选 → 点 `.sabo-hash-tag-item` 选中 | 实测:与抖音/小红书机制一致(输入#触发搜索下拉) |
| **内容声明(必填*)** | **拼多多特有**,下拉 6 选项,做成**飞书字段**供运营选择 | 用户确认「做成字段,运营可选」;默认「内容无需标注」;复用 TaskBundle.declaration + 淘宝 `_set_declaration` 模式 |
| **挂商品** | **需要**,参考淘宝光合 `_add_products` | 用户确认「需要挂商品,参考淘宝」;拼多多「商品ID」tab 输入 ID → 下一步直接绑定(比淘宝简单,无需勾选卡片) |
| 定时发布 | **支持**(绑商品后才出现的「发布设置」radio) | 实测:绑商品后表单展开「发布设置:立即发布/定时发布」radio;选定时后填时间。不绑商品时无此选项 |
| 平台特有飞书字段 | `declaration`(内容声明) + `product_ids`(商品ID) + `cover`(封面) | `field_map_defaults` 见 §3 |
| remote_url 抽取 | 不抽取 | 定时发布到点前无公开链接(同抖音/快手/淘宝) |
| 平台 key / 中文名 | `pinduoduo` / `拼多多`,配置文件 `config_pinduoduo.yaml` | 完整拼音 key,对齐 `taobao_guanghe`/`xiaohongshu` 命名惯例 |

### 已实测确认的页面结构(2026-07-22 对真实账号「九阳豆浆官方旗舰店」校验)

| 元素 | 定位 | 状态 |
|---|---|---|
| 发布页 URL | `live.pinduoduo.com/n-creator/video/home`(首页即发布页,非独立 URL) | ✅ |
| 上传 file input | `input[type=file][accept=".mp4,.wmv,.mov,.avi,.m4v"]`(multiple) | ✅ |
| 上传区父容器 | `div.no-video_wrap__XJ9r4` | ✅ |
| 上传成功判定 | 「视频上传成功」文案出现 | ✅ |
| 描述框 | `div[contenteditable=true].sabo-root` | ✅可写入验证 |
| 字数上限 | 500 字(`N/500` 计数器) | ✅ |
| 话题候选容器 | `.caret-popover-root`,单项 `.sabo-hash-tag-item` 内含 `.text-edit_topicItem__` | ✅ |
| 内容声明下拉 | textbox「请选择」,6 选项(内容无需标注/含AI生成内容/含虚构演绎内容/内容含营销信息/内容为转载/个人观点,仅供参考) | ✅ |
| 商品弹窗 | 「店铺商品」tab(列表+radio) / 「商品ID」tab(输入框+下一步) | ✅ |
| 商品ID 绑定 | 输入 ID → 点「下一步」→ 直接绑定(无需勾选) | ✅ |
| 绑商品后展开 | 展示悬浮窗 radio / 同步店铺动态 radio / 发布设置(立即/定时)radio | ✅ |
| 发布按钮 | button「发布」(主) | ✅ |
| 定时发布 | 绑商品后才出现「发布设置」radio + 时间输入 | ✅ |

### 仍未实测(实现时以 dry-run 对真实页校验,标注 TODO)

- 点「发布」后的**成功判定 URL/文案**(未真发,避免污染账号)
- 风控文案(未触发,沿用通用关键词)
- 定时发布时间输入框的具体 DOM(绑商品后选「定时发布」radio 才渲染,本次未深入)
- 封面上传弹窗(本次未点开「编辑封面」)

---

## 2. 改动清单

| 文件 | 改动 | 说明 |
|---|---|---|
| `wxsp/platforms/pinduoduo_selectors.py` | 新增 | URL、登录态判定、各步骤元素、`RISK_CONTROL_KEYWORDS`、`SUCCESS_INDICATORS`;标注未实跑项 |
| `wxsp/platforms/pinduoduo.py` | 新增 | 步骤函数 + `_pre_publish`/`_post_publish` + `PINDUODUO_SPEC` + `PinduoduoPublisher`(含 `login`) |
| `wxsp/platform_meta.py` | +1 条 `PlatformMeta` | 见 §3 |
| `wxsp/publisher.py` | +1 行 + import | `_PUBLISHERS["pinduoduo"] = PinduoduoPublisher()` |
| `config_pinduoduo.yaml` | 生成 | 由 `wxsp setup` 选「拼多多」生成;账号后续去 `/config` 加 |

**不改动**(全部从 `platform_meta.REGISTRY` 读):`config.py` / `notify.py` / `browser.py` /
`validator.py` / `api/routes_setup.py` / `api/routes_config.py` / `errors.py` / `models.py` /
`feishu.py` / `platforms/base.py`(TaskBundle 已含 declaration/product_ids_json 字段)。

**错误类型**:复用现有 `ProductNotFound`(淘宝已定义,商品ID搜不到时抛)、`CookieExpired` /
`UploadFailed` / `RiskControl` / `ElementNotFound` / `NetworkError`,无新错误 → `errors.py` 与
`notify._ERROR_TYPE_CN` 不动。**实现时核对步骤名 `product` 是否在 `notify._STEP_CN`**(淘宝已用,
应已登记);内容声明步骤复用淘宝的 `declaration` 步骤名。

---

## 3. PlatformMeta 登记

```python
"pinduoduo": PlatformMeta(
    key="pinduoduo",
    label="拼多多",
    title_min=1,               # 无标题框(同快手),title_min 仅作占位
    login_meta={
        "home_url": "https://live.pinduoduo.com/n-creator/video/home",
        "mode": "logged_in_url",
        "logged_in_fragment": "live.pinduoduo.com/n-creator/video/home",
        # SSO 登录链跳转多跳,未登录停在 mms.pinduoduo.com/login;登录成功落地 n-creator/video/home
    },
    field_map_defaults={
        "tags": "标签",
        "cover": "封面文件",
        "declaration": "内容声明",      # 拼多多特有(同淘宝)
        "product_ids": "商品ID",        # 拼多多特有(同淘宝)
    },
    needs_fingerprint=False,    # 跟抖音/快手/淘宝一致(用户确认)
    has_title=False,            # 无独立标题框(同快手)
),
```

`login_meta` 用 `logged_in_url` 模式(正向判定):SSO 登录链有多跳重定向,未登录停在
`mms.pinduoduo.com/login`,登录成功最终落地 `live.pinduoduo.com/n-creator/video/home`。
URL 含 `n-creator/video/home` = 登录成功的硬信号(对齐小红书 `logged_in_fragment` 思路)。

---

## 4. 选择器文件 `pinduoduo_selectors.py`

(实现时按 §1 实测结构填充,下面是骨架与关键选择器)

```python
"""拼多多多多视频发布页选择器 —— 改版时的唯一改动点。

2026-07-22 对真实账号(九阳豆浆官方旗舰店)逐项实测校验:登录态/上传/描述框/话题候选/
内容声明下拉/商品弹窗(店铺商品+商品ID两 tab)/绑商品后展开的发布设置均已命中。
**仍未实测**:点发布后成功判定 URL/文案(未真发)、定时发布时间输入框 DOM(选定时 radio
后才渲染,本次未深入)、封面弹窗(未点开编辑封面)。优先语义化定位(text=/role=),少用脆弱 class。
"""

# ---- 登录 URL ----
LOGIN_URL = "https://mms.pinduoduo.com/login/sso?platform=live&accessType=auto&redirectUrl=https://live.pinduoduo.com/login/checker%3FisNewCreatorFrom%3Dvideo%26referUrl%3D%252Fn-creator%252Fvideo%252Fhome%253Ffrom%253Dmms%2526msfrom%253Dmms_sidenav%26from%3Dmms"
HOME_URL = "https://live.pinduoduo.com/n-creator/video/home"  # 发布页 = 首页
LOGIN_URL_FRAGMENT = "mms.pinduoduo.com/login"  # 未登录停在 SSO 登录页
LOGGED_IN_URL_FRAGMENT = "live.pinduoduo.com/n-creator/video/home"  # 登录成功硬信号

# ---- 视频上传 ----
VIDEO_FILE_INPUT = 'input[type="file"][accept=".mp4,.wmv,.mov,.avi,.m4v"]'  # multiple
VIDEO_UPLOAD_AREA = "div.no-video_wrap__XJ9r4"  # 上传区父容器(物理点击用)
UPLOAD_DONE_MARKER = "text=视频上传成功"  # 上传成功文案

# ---- 描述(无标题框,主文案)----
DESC_EDITOR = 'div[contenteditable="true"].sabo-root'  # DraftJS 风格,实测可写入
DESC_MAX_LENGTH = 500

# ---- 话题 ----
TOPIC_POPOVER = ".caret-popover-root"  # 输入#关键词后弹出
TOPIC_ITEM = ".sabo-hash-tag-item"  # 候选项(点选才真正绑定)

# ---- 内容声明(必填下拉)----
DECLARATION_TRIGGER = 'div:has-text("内容声明")'  # *内容声明 区域
DECLARATION_DROPDOWN = '[data-testid="beast-core-select-htmlInput"]'  # 下拉触发(testId 稳定)
DECLARATION_OPTIONS = {
    "内容无需标注": "内容无需标注",
    "含AI生成内容": "含AI生成内容",
    "含虚构演绎内容": "含虚构演绎内容",
    "内容含营销信息": "内容含营销信息",
    "内容为转载": "内容为转载",
    "个人观点，仅供参考": "个人观点，仅供参考",
}

# ---- 挂商品 ----
PRODUCT_TRIGGER = "text=添加商品"
PRODUCT_DIALOG = 'div:has-text("添加商品")'  # 弹窗
PRODUCT_TAB_BY_ID = "text=商品ID"  # 商品ID tab(精确搜索)
PRODUCT_ID_INPUT = 'input[placeholder*="商品id"]'  # placeholder 含"商品id"
PRODUCT_NEXT_BUTTON = 'button:has-text("下一步")'  # 输入ID后点下一步直接绑定

# ---- 发布设置(绑商品后出现)----
SCHEDULE_RADIO = 'radio:has-text("定时发布")'  # 发布设置 radio

# ---- 发布 / 风控 / 成功 ----
PUBLISH_BUTTON = 'button:has-text("发布")'
RISK_CONTROL_KEYWORDS = ("操作频繁", "操作过于频繁", "请稍后", "账号异常", "违规")
SUCCESS_INDICATORS = ("发布成功",)  # 待真发校验补充
```

**注意**:拼多多样式 class 是 CSS-module hash(如 `no-video_wrap__XJ9r4`),hash 部分会随构建变。
实现时优先用 `text=`/`placeholder=`/`data-testid` 语义定位(拼多多用 beast-core 组件库,testId 较稳定),
class hash 仅在无语义锚点时用前缀模糊匹配(`div[class^="no-video_wrap"]`)。

---

## 5. adapter `pinduoduo.py` 步骤设计

`_pre_publish(page, bundle, staged, ctx)` 步骤序列:

| 步骤 | 函数 | 说明 |
|---|---|---|
| open_publish | `_open_publish_page` | goto `HOME_URL`(首页即发布页,无 URL 切换) |
| verify_login | `_verify_logged_in` | URL 含 `mms.pinduoduo.com/login` → CookieExpired;否则查上传区在 |
| upload | `_upload_video` | set_input_files + 等「视频上传成功」文案 |
| desc | `_fill_description` | 点 `sabo-root` → keyboard.type(描述[:500]) |
| tags | `_add_tags` | 逐个 type(`#tag`) → 等 `.caret-popover-root` → 点首项 |
| **products** | `_add_products` | 点「添加商品」→ 切「商品ID」tab → 逐个填 ID → 下一步绑定 |
| cover | `_set_cover` | best-effort(自定义封面,未实测弹窗,标 TODO) |
| schedule | `_set_schedule` | **绑商品后才有效**:选「定时发布」radio + 填时间(降级:未绑商品/无定时选项则跳过) |
| **declaration** | `_select_declaration` | 点内容声明下拉 → 选对应选项(默认「内容无需标注」) |
| risk | `_risk_control_probe` | 扫 body 文本命中风控关键词 |

`_post_publish(page, bundle, ctx)`:

| 步骤 | 函数 | 说明 |
|---|---|---|
| publish | `_click_publish` | 点「发布」按钮 |
| wait_success | `_wait_for_success` | 等成功文案/URL(待真发校验,标 TODO) |

**与淘宝的关键差异**:
- 拼多多无 iframe(淘宝全在 iframe 内),直接在 page 操作,更简单
- 拼多多商品绑定:输入 ID → 下一步,直接绑定(**无需勾选卡片**,淘宝要 hover+勾选)
- 拼多多内容声明:下拉选择(淘宝是 radio)
- 拼多多发布页 = 首页(无 URL 切换判定),淘宝/小红书有独立发布页

**APC 守门、`ctx.last_step` 更新、`bring_browser_to_front`**:对齐小红书/淘宝实现。

---

## 6. 成功判定的风险点(实现时重点)

拼多多有两个干扰因素,影响成功判定:
1. **上传未发布的残留**:取消发布后视频仍在,下次进首页提示「上次有N个视频上传成功但未发布」。
   不能用此文案判成功(它持续存在)。须用点「发布」后的跳转/「发布成功」文案。
2. **单页应用无 URL 切换**:发布页是 SPA,点发布后可能不跳独立成功页(待真发确认)。
   成功判定优先「发布成功」文案,辅以 URL 变化。

---

## 7. 验证计划

1. `ruff check` + `mypy --strict` 通过(对齐全平台标准)
2. `wxsp run --platform pinduoduo --task-id N --dry-run` 对真实页跑 dry-run,截图确认每步命中
3. 首次真发一条测试视频验证 `_post_publish` 成功判定,补全 `SUCCESS_INDICATORS`(标 TODO 的项)
4. 若 patchright 风控:切 `use_real_chrome=True`,同小红书演进路线
