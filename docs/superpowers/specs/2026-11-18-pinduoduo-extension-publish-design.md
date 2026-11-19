# 拼多多插件化发布 —— 设计文档

> 日期:2026-11-18(P1 实现后修订:通信改走 CDP binding,见 §2.3) · 范围:只改拼多多(`pinduoduo`),其余 5 个平台不动
> 参考:`~/voc/tmall-review-collector`(MV3 插件 + chrome.debugger 可信输入 + CDP 注入插件)

## 1. 目标与非目标

**目标**
- 拼多多发布从「patchright 驱动的 patched chromium」改为「wxsp 托管的系统真实 Chrome + 自动注入的插件执行」,降低自动化特征。
- 业务零安装:插件由 wxsp 每次启动 Chrome 时自动注入,运营只需首次扫码。
- 插件逐步上报进度,工作台「正在发布」卡片显示当前步骤。
- 平台间可并行,同平台仍串行(改 CLAUDE.md §2.5)。

**非目标**
- 不改其他平台的发布方式;不做插件商店分发;不支持「运营日常 Chrome 里装插件」模式。
- 不改飞书同步、校验、DB 状态机、通知、回写(全部沿用 `runner.py`)。

**换来什么(实话)**:chrome.debugger 发的 `Input.dispatchMouseEvent` 与远程 CDP 是同一可信级别;真正的收益是 ① 品牌版 Chrome、无 `--enable-automation`、无 patchright/Playwright 的 `Runtime.enable` 等注入痕迹;② 步骤逻辑跑在页面旁边,不依赖远程 locator 轮询;③ 后台标签页执行,不抢焦点,为平台间并行铺路;④ 虚拟光标可视化,运营看得到在干什么。

## 2. 总体架构

```
 wxsp(daemon 或 CLI 进程)                            托管 Chrome(每个拼多多账号一个 profile)
 ┌──────────────────────────────┐   ① 启动 + CDP 注入插件   ┌───────────────────────────────┐
 │ runner.run_publish            │ ───────────────────────▶ │ 插件 service worker            │
 │   └ meta.driver="extension"   │                           │   ├ chrome.debugger(可信输入)  │
 │       └ extension_session     │ ── ② 附着插件 SW+binding ▶ │   ├ 后台标签页 live.pinduoduo  │
 │           (ExtensionSession)  │ ── ③ job / publish ─────▶ │   └ 虚拟光标 cursor.js         │
 │   (同一条 CDP 连接)          │ ◀── ④ step / gate / done ─ │                                │
 └──────────────────────────────┘      / error              └───────────────────────────────┘
```

### 2.1 托管 Chrome(`wxsp/managed_chrome.py`,新文件)
- 复用 `browser._system_chrome_path()` / `_reap_stale_profile_lock()`;启动参数对齐参考项目:
  `--user-data-dir=<account.user_data_dir> --remote-debugging-port=0 --no-first-run --no-default-browser-check --disable-background-timer-throttling --disable-renderer-backgrounding --disable-backgrounding-occluded-windows`。
- 读 `<profile>/DevToolsActivePort` 拿端口 → `/json/version` 拿 browser WS → `Extensions.uninstall`(忽略未安装)→ `Extensions.loadUnpacked(path=wxsp/extension)`。注入失败整段重试,20s 超时。
- 每条任务启动一次、结束关闭(与现有平台一致,不做常驻进程,先求简单)。
- profile 沿用账号现有 `user_data_dir`。旧 profile 是 patchright 的 Chromium 格式,macOS 上 cookie 加密钥匙串不同 → **升级后拼多多每个账号需重新扫码一次**。

### 2.2 插件(`wxsp/extension/`,随 wxsp 包一起打包)
- MV3,**纯 JS,无构建步骤**(wxsp 运行/打包都不依赖 Node)。
- `manifest.key` 固定公钥 → 固定扩展 ID(`managed_chrome.EXTENSION_ID`),注入前先按 ID uninstall。
- 权限:`debugger`、`tabs`、`scripting`;host:`https://*.pinduoduo.com/*`、`http://127.0.0.1/*`(selftest 本地页)。
- 每个标签页 attach 后先开 `Emulation.setFocusEmulationEnabled`,否则后台标签页里点击不生效(P0 实测)。
- 从参考项目移植(改写为纯 JS,不整包搬):`rpa-input`(点击/滚轮/按键三段序列 + 随机停顿)、`rpa-target`(视口外滚入 / 两帧稳定 / `elementFromPoint` 命中校验)、`cursor.js`(虚拟光标)。
- 文字输入:`Input.insertText` 分段 + 随机间隔(描述、话题、商品 ID)。
- 文件上传:`DOM.setFileInputFiles` 传 wxsp 已 stage 好的本地路径(见 §5 风险 1)。
- 截图:`Page.captureScreenshot`(后台标签页可用),base64 回传。
- 选择器从 `pinduoduo_selectors.py` 搬到 `wxsp/extension/pdd/selectors.js`,Python 侧旧文件在切换完成前保留。

### 2.3 通信协议(CDP binding,P1 实现时由 WebSocket 改来)
wxsp 注入插件时本来就握着这个 Chrome 的 browser 级 CDP 连接,P1 直接复用它通信,不再另起 WebSocket 服务:
- **wxsp → 插件**:附着插件 service worker,`Runtime.evaluate` 调 `globalThis.wxsp.*`:`startJob(job)` / `proceed("publish"|"abort")` / `screenshot()` / `html()`。
- **插件 → wxsp**:`Runtime.addBinding("wxspEmit")`,插件 `wxspEmit(JSON)` 回传,Python 收 `Runtime.bindingCalled`。插件 `console.*` 转进 wxsp 日志(debug 级)。
- 好处:没有端口/Origin/token/心跳;CDP 附着期间 service worker 不会被回收;`wxsp run` 命令行单独跑(没开 Web 服务)也能用。
- 消息(JSON):

| 方向 | 内容 |
|---|---|
| wxsp→插件 `startJob` | `platform` / `step_pause` / `video_path` / `halt_before`(APC 拒绝时为 `upload`);P2 补 `description` / `tags` / `product_ids` / `publish_at` / `declaration` / `cover_path` |
| 插件→wxsp `step` | `step`(沿用现有 step 名:`open_publish`/`verify_login`/`entry_popups`/`upload`/`desc`/`tags`/`products`/`cover`/`schedule`/`declaration`/`risk`/`publish`/`wait_success`) |
| 插件→wxsp `gate` | 填表完成,停在发布按钮前 |
| 插件→wxsp `halted` | 按 `halt_before` 在某步前停下 |
| wxsp→插件 `proceed("publish")` | **只有非 dry-run 才发** |
| 插件→wxsp `done` | 发布成功,可带 `remote_url` |
| 插件→wxsp `error` | `kind`(`cookie_expired`/`risk_control`/`element_not_found`/`upload_failed`/`product_not_found`/`network`/`unknown`)、`step`、`message`;截图与 HTML 由 runner 随后调 `screenshot()`/`html()` 取 |

- **dry-run 红线留在 Python**:插件到 `gate` 必须停下等指令。插件自己不知道也不判断 dry-run。
- 页面侧:插件只用 `chrome.scripting`(isolated world)只读 DOM,输入全走 chrome.debugger;Python 不附着任何页面 target。

### 2.4 接入 runner(改动最小化)
- 平台元数据 `PlatformMeta` 加 `driver: str = "patchright"`(拼多多 `extension`),runner 与 doctor 都从登记表读。`run_publish` 里唯一分支:`driver == "extension"` 时用 `managed_chrome.extension_session(...)` 代替 `browser_context(...)`,且不锁用户键鼠,其余流程(claim、stage、dry-run gate、`_finalize`、通知、飞书回写)一行不改。
- `ExtensionSession` 鸭子类型兼容现有 `screenshot()` / `_capture_error_artifacts()` 用到的 `page.screenshot(path=...)` / `page.content()`(返回插件最近一次上报的截图/HTML)。
- 拼多多新 `_pre_publish(session, ...)`:发 `job` → 循环收消息,`step` 写 `ctx.last_step`,`error` 按 `kind` 抛 `errors.py` 里对应异常,收到 `gate` 返回。`_post_publish`:发 `publish` → 等 `done`。超时一律按步骤给上限(上传沿用 `upload_timeout_seconds`)。
- 风控:插件扫 `RISK_CONTROL_KEYWORDS` 命中即 `error{kind: risk_control}`,runner 照旧暂停账号 24h。
- ~~回滚开关~~ P1 取消:两种驱动的 profile 互不兼容(切换都要重新扫码),运行时开关价值不大。回滚 = 回退代码 + 用 `data/profile-backups/` 里的旧 profile。功能分支在 P2 真发验证前不合并。

### 2.5 登录与健康检查
- `PinduoduoPublisher.login`:托管 Chrome 打开 SSO 登录页,经 CDP 轮询 URL 含 `live.pinduoduo.com/n-creator/video/home` 即成功。不需要插件。cookie 由真实 Chrome profile 自己持久化,不再写 `cookies.json`。
- `doctor` 的拼多多 cookie 检查同样改走托管 Chrome(`browser.check_cookie` 按 driver 分流,P1 已做)。

### 2.6 进度上报到工作台(顺带惠及全部平台)
- `publisher.py` 加进程内字典 `RUNNING_STEPS: dict[task_id, step]`;`PublishContext.last_step` 改为 property,赋值时同步写字典,任务结束清掉。
- 工作台「正在发布」卡片读它显示中文步骤(`notify.step_cn`)。不加 DB 列(避免迁移);代价是 `wxsp run --task-id` 这种独立 CLI 进程里的任务在 Web 上看不到步骤,只看到「发布中」。

### 2.7 平台间并行(改 §2.5 约束)
- `publisher._PUBLISH_LOCK` 全局锁 → 每平台一把锁:同平台串行、不同平台并行。
- **例外:小红书**用 pyautogui 物理输入,别的平台弹窗口会抢焦点打乱坐标 → 小红书运行时独占(读写锁:小红书拿写锁,其他平台拿读锁 + 本平台锁)。
- 风控上:同平台仍串行,不同平台各自风控体系,不构成「同 IP 多账号并发」特征。
- 放在最后一个阶段做,插件主线不依赖它。

## 3. 分阶段实施(每阶段有可验证标准)

| 阶段 | 内容 | 验收 |
|---|---|---|
| **P0 验证 ✅** | 脚本:启动本机 Chrome → `loadUnpacked` 最小插件 → WS `hello` → 在本地测试页用 chrome.debugger 点击按钮、`DOM.setFileInputFiles` 上传文件、`captureScreenshot` | 脚本跑通打印 4 个 ok;任一失败就回来改方案,不往下做 |
| **P1 骨架 ✅** | `managed_chrome.py`(含 CDP 客户端)、`ExtensionSession`、runner `driver` 分支、插件框架(步骤上报/错误上报/截图/可信输入/selftest 自检平台)、拼多多 `open_publish`→`verify_login`→`entry_popups`→`gate`、新 login | 单测:消息泵(步骤、error→异常映射、halted、超时);集成:selftest 真 Chrome 全通路;真机:`wxsp login` 扫码成功 → `wxsp run --task-id N --dry-run` 停在 gate 出截图 |
| **P2 全步骤(代码完成,待真机 dry-run)** | 上传、描述、话题、商品、封面、定时(日历切月同视频号修法,找不到日期就报错)、内容声明、风控探测、发布、成功判定 | 真机 dry-run 全流程截图核对;**经你同意后**真发 1 条;旧 patchright 路径仍可用开关切回 |
| **P3 收尾** | 全平台步骤上报到工作台、平台间并行锁、CLAUDE.md(§2.5、平台架构、新增平台手册)更新、打包脚本确认 `wxsp/extension` 被带上 | 单测:锁语义(同平台串行/跨平台并行/小红书独占);工作台卡片显示步骤 |

测试原则沿用 CLAUDE.md:Python 侧消息处理用回放消息的单测,不 mock 浏览器;插件行为只用真 Chrome 验证(`@pytest.mark.integration`)。

## 4. 文件清单

| 新增 | 修改 |
|---|---|
| `wxsp/managed_chrome.py` | `wxsp/platform_meta.py`(`driver`)、`wxsp/platforms/base.py`(回调类型放宽;P3 `last_step` property) |
| | `wxsp/platforms/runner.py`(一处 driver 分支) |
| `wxsp/extension/`(manifest、background.js、pdd/*.js、cursor.js) | `wxsp/platforms/pinduoduo.py`(新步骤走插件;旧实现留在开关后) |
| `tests/test_extension_protocol.py`、`tests/test_managed_chrome.py` | `wxsp/publisher.py`(RUNNING_STEPS、P3 的锁) |
| | `wxsp/browser.py`(check_cookie 分流)、`routes_dashboard.py` + `dashboard.html`(步骤显示)、`CLAUDE.md` |

## 5. 风险与待验证

1. ~~插件读本地文件~~ **P0 已验证**:插件经 chrome.debugger 调 `DOM.setFileInputFiles` 传本地路径可用,页面收到 `isTrusted=true` 的 change 事件,无需额外启动参数。
2. **调试提示条**:chrome.debugger 挂上后 Chrome 顶部会出现「xxx 已开始调试此浏览器」提示条。托管窗口只给自动化用,不影响运营;参考项目同样如此。
3. ~~`Extensions.loadUnpacked` 的开关要求~~ **P0 已验证**:Chrome 157(macOS)只用 `--remote-debugging-port=0` 即可注入。
4. **Windows**:路径、进程回收沿用 `browser.py` 已有的 Windows 处理;P2 结束后需要一台 Windows 机器验证一次。
5. **插件更新不生效(P2 实测踩到)**:注入的插件不跨重启保留(uninstall 报找不到),但 profile 里留着上次插件的 Service Worker 注册,它 import 的模块走安装时的脚本缓存,改了代码甚至改了版本号也照跑旧代码。解决:注入前借启动空白页的会话对插件源发 `Storage.clearDataForOrigin(service_workers)`(browser 目标上会报 Internal error;`chrome.runtime.reload()` 后 MV3 后台不自动起,也不行)。只清插件自己的源,不碰平台站点数据。
6. **测试不弹窗**:本地测试页类集成测试用 `--headless=new`(插件可用);真实平台登录/发布永远开窗。

## 6. P0 验证结论(2026-11-18,macOS · Chrome 157)

后台标签页内:CDP 注入插件 ✓;WS 握手 Origin 为固定扩展 ID ✓;`Input.dispatchMouseEvent` 点击 `isTrusted=true` ✓(需焦点仿真);`Input.insertText` 中文输入 ✓;`DOM.setFileInputFiles` 本地文件 ✓;`Page.captureScreenshot` ✓。
