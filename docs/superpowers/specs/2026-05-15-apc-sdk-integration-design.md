# APC SDK 接入 设计文档

> 接入的服务端文档见 [`/Users/zhaoguangyu/wechat-sph-upload/docs/sdk-integration.md`](../../sdk-integration.md)。
> 本文档定义把 APC(Application Control)接入 wxsp 的具体方案,**并把 SDK 部分抽成可被其他项目复用的独立 Python 包 `apc_sdk`**(monorepo 子目录形式)。

**Goal**:打包后的 wxsp(`.dmg` / `.exe`)启动后,每天首次调一次 APC 服务拿"通过 / 拒绝"判决;拒绝时让所有发布 task 装成"等待上传区域超时"故障,**运营无感知地把这台机器从运营队伍里踢掉**;开发模式(`uv run wxsp`)完全跳过 APC。

**Tech Stack**:`httpx`(HTTP 客户端 + 自签证书指纹校验) + `pyjwt` + `cryptography`(JWT RS256 校验) + 现有 `pathlib` / `platformdirs` / loguru 栈。**不**引入 Pydantic 到 `apc_sdk` 包(它要做成最小依赖,方便接入任意项目)。

---

## 1. 范围

### 1.1 交付物

| 文件 | 说明 |
|------|------|
| `apc_sdk/pyproject.toml` | **新文件**。独立子包,依赖 `httpx>=0.27`、`pyjwt[crypto]>=2.8`、`cryptography>=42`。包名 `apc-sdk-python`,导入名 `apc_sdk`。MIT License。 |
| `apc_sdk/src/apc_sdk/__init__.py` | **新文件**。导出 `ApcClient`、`Verdict`、`ApcConfig`、`ApcDenied`、`ApcNetworkError`。 |
| `apc_sdk/src/apc_sdk/client.py` | **新文件**。`ApcClient` 主入口 + `check()` 状态机(7 天 grace + fail-open 算法)。 |
| `apc_sdk/src/apc_sdk/crypto.py` | **新文件**。HMAC-SHA256 签名(对齐 SDK 文档 §2.3 的 `${method}\n${path}\n${ts}\n${bodySha}`)+ JWT RS256 校验。 |
| `apc_sdk/src/apc_sdk/cache.py` | **新文件**。原子读写 `session.json`(临时文件 + rename),JSON 损坏时安全 fallback。 |
| `apc_sdk/src/apc_sdk/pinning.py` | **新文件**。`build_httpx_client(verify, fingerprint)`,有指纹时校验 SHA-256;无指纹时走正常 CA 链。 |
| `apc_sdk/src/apc_sdk/exceptions.py` | **新文件**。`ApcDenied`(明确 4xx)、`ApcNetworkError`(网络 / 5xx / TLS 失败,统称"连不上")。 |
| `apc_sdk/src/apc_sdk/_types.py` | **新文件**。`Verdict` Enum(`PASS` / `DENY`)、`SessionCache` TypedDict、`ApcConfig` dataclass。 |
| `apc_sdk/README.md` | **新文件**。通用接入文档,讲在其他项目里怎么用 `ApcClient`,给一个 ~30 行的最小例子。 |
| `apc_sdk/tests/test_crypto.py` | **新文件**。HMAC 向量(对齐 §2.3 Node 版)+ JWT 篡改必须失败。 |
| `apc_sdk/tests/test_cache.py` | **新文件**。跨日清缓存 / 首装写 `last_success_at=now` / JSON 损坏 fallback。 |
| `apc_sdk/tests/test_client.py` | **新文件**。`respx` mock 200/403/超时,verdict 三态正确。 |
| `apc_sdk/tests/test_grace_logic.py` | **新文件**。7 天硬上限边界 4 个 case(§5.3)。 |
| `wxsp/apc.py` | **新文件**。wxsp 私有粘合层:`is_dev_mode()` / `check_pass()` / `_client()`。 |
| `wxsp/apc_config.py` | **新文件**。凭据占位符(`__APC_ENDPOINT__` 等),git tracked,build 脚本打包时 patch,trap 在 build 退出时 revert。 |
| `wxsp/publisher.py` | **修改**。`publish_task()` 入口调 `wxsp.apc.check_pass()`,失败时在 step `[4]` 后注入 `ElementNotFound` 故障(§4)。 |
| `scripts/build_macos.sh` | **修改**。PyInstaller 之前注入凭据 + EXIT trap 恢复;PyInstaller 命令加 `--collect-all apc_sdk`。 |
| `scripts/build_windows.ps1` | **修改**。同上 PowerShell 版本;加 `--collect-all apc_sdk`。 |
| `.github/workflows/build.yml` | **修改**。job env 注入 5 个 `APC_*` secrets。 |
| `pyproject.toml`(wxsp 根) | **修改**。`dependencies` 加 `apc-sdk-python @ file:./apc_sdk`(本地 path 依赖)。 |

### 1.2 不动的

- `wxsp/cli.py` — 守门完全交给 publisher,cli 入口不调 `apc.check_pass()`(§3 解释)
- `wxsp/doctor.py` / `wxsp/api/routes_accounts.py` / `wxsp/api/routes_dashboard.py` 等"查状态"代码 — 不卡 APC
- `wxsp/config.py` — APC 配置走独立的 `apc_config.py`,不进 `Settings`
- 现有 setup wizard(`wxsp/api/routes_setup.py`)— 不暴露 APC 配置项给运营

### 1.3 不做的(YAGNI)

- **后台心跳续签**:用户场景只需要"每天首次启动调一次",没有 SDK 文档 §5 那套状态机
- **`/session/refresh` 接口**:同上,不实现
- **设备审批(`queued`)流**:接入方设 `auto_approve_new_devices=true`,不走审批
- **GUI 配置 APC**:凭据嵌进二进制,运营无入口
- **多端点 / 多 backend**:一个 endpoint 写死
- **指纹双 pin 过渡**:文档 §4.5 提的运维优化,先单指纹,需要时再加

---

## 2. 凭据流(关键!)

### 2.1 源码状态

`wxsp/apc_config.py`(git tracked,**这就是仓库永久状态**):

```python
"""APC 凭据(打包时被 build 脚本替换;源码状态是占位符)。"""

APC_ENDPOINT = "__APC_ENDPOINT__"
APC_APP_ID = "__APC_APP_ID__"
APC_APP_SECRET = "__APC_APP_SECRET__"
APC_PUBLIC_KEY = "__APC_PUBLIC_KEY__"
APC_CERT_FP = "__APC_CERT_FP__"
```

源码版本永远是占位符 — git 仓库里不出现任何真实凭据。

### 2.2 build 时 patch

`scripts/build_macos.sh` 在 PyInstaller 调用**之前**加:

```bash
echo "==> 注入 APC 凭据"
: "${APC_ENDPOINT:?env required}"
: "${APC_APP_ID:?env required}"
: "${APC_APP_SECRET:?env required}"
: "${APC_PUBLIC_KEY:?env required}"
: "${APC_CERT_FP:?env required}"

# trap 保证打包成功 / 失败 / Ctrl-C 都恢复源码占位符状态
trap 'git checkout -- wxsp/apc_config.py 2>/dev/null || true' EXIT

uv run python -c "
import os, pathlib
p = pathlib.Path('wxsp/apc_config.py')
content = p.read_text()
for k in ['APC_ENDPOINT','APC_APP_ID','APC_APP_SECRET','APC_PUBLIC_KEY','APC_CERT_FP']:
    content = content.replace(f'__{k}__', os.environ[k])
p.write_text(content)
"
```

`scripts/build_windows.ps1` 同等 PowerShell 实现(用 `git checkout` 而非 `Restore-Item`,git 一定存在)。

### 2.3 GitHub Actions 注入

`.github/workflows/build.yml` 在 macOS + Windows build job 各自的 step env 注入:

```yaml
env:
  APC_ENDPOINT:   ${{ secrets.APC_ENDPOINT }}
  APC_APP_ID:     ${{ secrets.APC_APP_ID }}
  APC_APP_SECRET: ${{ secrets.APC_APP_SECRET }}
  APC_PUBLIC_KEY: ${{ secrets.APC_PUBLIC_KEY }}
  APC_CERT_FP:    ${{ secrets.APC_CERT_FP }}
```

仓库管理员在 GitHub Settings → Secrets 配齐 5 个值。

### 2.4 隐蔽性论证

- 源码克隆者 / PR reviewer:看到 `apc_config.py` 占位符,知道有 APC 但拿不到凭据
- 打包后 .app / .exe:`apc_config.py` 编译成 `.pyc`,凭据以字符串常量嵌入字节码;Python 3.11 字节码反编译工具(uncompyle6 / decompyle3)对 3.11 支持差,门槛 = "技术型运营 + 数小时尝试"
- 打包流水线泄漏:仅 GitHub Actions 主仓库管理员能读 secrets,运营拿不到

威胁模型对齐:**"拦运营 / 一般技术人员"**(M11 安装器文档原话)。专业逆向 / 二进制分析超出范围,且这种用户也不在自己人 2-3 台机器场景。

---

## 3. wxsp 接入位置

### 3.1 守门策略

| 命令 | 守门? | 理由 |
|------|--------|------|
| `wxsp run --daemon` / `wxsp web` | **不在启动时守门** | daemon / Web UI 必须正常起来;守门交给每个 task |
| `wxsp run --today` / `wxsp run --task-id N` | **task 入口守门** | 每个 task 进 `publish_task()` 时调一次 `check_pass()` |
| `wxsp login <account>` | **不守门** | 扫码登录是运营准备工具的前置动作,卡这一步太显眼;反正不让发布就达成 kill 目标 |
| `wxsp sync` | **不守门** | 飞书同步是只读 + 校验,即便同步成功 task 也跑不出来 |
| `wxsp doctor` / `accounts list/pause/resume` / `status` / `logs` / `cleanup` | **不守门** | 用户拍板"查状态命令不卡,照实输出" |

**为什么 cli 入口不守门、由 publisher 自己守门**:

1. daemon 必须能起来 — 这样 Web UI 看起来一切正常
2. 同一份逻辑在 `run --today` 和 `run --daemon`(cron tick)两条路径都生效
3. 装故障的位置必须在 publisher 里(故障是 publisher 的语义),守门和注入在同一个函数体内,避免跨模块传 flag

### 3.2 接入代码

`wxsp/apc.py`:

```python
"""wxsp 私有 APC 粘合层。封装 ApcClient 实例化 + dev-mode 旁路。"""

from __future__ import annotations
from pathlib import Path
from loguru import logger
from apc_sdk import ApcClient, Verdict, ApcConfig
from wxsp.apc_config import (
    APC_ENDPOINT, APC_APP_ID, APC_APP_SECRET, APC_PUBLIC_KEY, APC_CERT_FP,
)
from wxsp.config import is_packaged, get_user_data_dir


def is_dev_mode() -> bool:
    """开发模式 = 未打包。打包后强制走 APC。"""
    return not is_packaged()


_client_singleton: ApcClient | None = None


def _client() -> ApcClient:
    """惰性单例。dev-mode 不应该调到这里(check_pass 短路了)。"""
    global _client_singleton
    if _client_singleton is None:
        cache_dir = get_user_data_dir() / ".apc"
        cache_dir.mkdir(parents=True, exist_ok=True)
        _client_singleton = ApcClient(ApcConfig(
            endpoint=APC_ENDPOINT,
            app_id=APC_APP_ID,
            app_secret=APC_APP_SECRET,
            public_key=APC_PUBLIC_KEY,
            cert_fingerprint=APC_CERT_FP or None,
            cache_dir=cache_dir,
            grace_days=7,
            request_timeout_seconds=5.0,
        ))
    return _client_singleton


def check_pass() -> bool:
    """业务侧调这个。返回 True = 允许跑;False = 装故障。

    dev-mode 永远返回 True(开发跑 uv run wxsp 不会触网)。
    """
    if is_dev_mode():
        return True
    try:
        verdict = _client().check()
        return verdict == Verdict.PASS
    except Exception as exc:
        # 任何 SDK 内部 bug 都不应该让 wxsp 崩,fail-open
        logger.warning(f"[apc] check 异常,fail-open: {exc!r}")
        return True
```

**注意**:wxsp 视角下,`Verdict.PASS` = 跑、`Verdict.DENY` = 装故障。SDK 内部把"网络问题在 7 天 grace 内"翻译成 `PASS`,把"超过 grace"翻译成 `DENY` — wxsp 不操心。

### 3.3 publisher 接入

`wxsp/publisher.py`,`publish_task()` 改动:

```python
async def publish_task(task: Task, account: Account, settings: Settings) -> None:
    apc_passed = wxsp.apc.check_pass()   # 毫秒级,本地缓存查询为主

    # ───── 步骤 [0]–[4] 全部照常跑 ─────
    claim_task(task.id)                  # [0]
    staged_path = stage_video_to_tmp(task)  # [1]
    browser = await launch_browser(account)  # [2]
    page = await open_publish_page(browser)  # [3]
    await verify_logged_in(page)         # [4]

    if not apc_passed:
        # ───── 故障注入点 ─────
        # 真截图(此时浏览器还在视频号发布页),time 看起来像"等元素超时"。
        await asyncio.sleep(random.uniform(45, 75))
        screenshot_path = await save_screenshot(page, task, step="wait_upload_area")
        task.screenshots_json = json.dumps([str(screenshot_path)])
        raise ElementNotFound("等待上传区域超时(60s)")

    # ───── 步骤 [5]–[20] 照常跑 ─────
    await upload_video(page, staged_path)
    # ... 后续步骤
```

抛出的 `ElementNotFound` 走 `@retry_on` 装饰器的现有处理(retry 1 次再 fail)+ `errors.classify()` 标记 `element_not_found`,event 表 + 通知 + 飞书回写全部正常触发。

---

## 4. SDK 行为详解

### 4.1 公开接口

```python
@dataclass(frozen=True)
class ApcConfig:
    endpoint: str                  # 例 "https://203.0.113.5:8443"(末尾不带 /)
    app_id: str
    app_secret: str
    public_key: str                # JWT 校验 PEM 公钥
    cache_dir: Path
    cert_fingerprint: str | None = None    # 自签时填(小写 hex,无冒号);Let's Encrypt 留 None
    grace_days: int = 7
    request_timeout_seconds: float = 5.0
    client_meta: dict[str, str] | None = None  # 上报到 APC 后台,如 {"app_version": "0.1.4"}


class Verdict(str, Enum):
    PASS = "pass"
    DENY = "deny"


class ApcClient:
    def __init__(self, config: ApcConfig): ...
    def check(self) -> Verdict:
        """同步阻塞调用。返回判决。
        - 本地缓存的今日判决存在 → 直接返回
        - 跨日 → 调 /session/init
            - 200 + JWT 校验通过 → PASS,更新 last_success_at
            - 4xx → DENY
            - 网络问题 → 看 grace:在 grace 内返回 PASS,超 grace 返回 DENY(并写入今日缓存)
        """
```

### 4.2 缓存文件结构

存 `{cache_dir}/session.json`(wxsp 场景下 `cache_dir = ~/Library/Application Support/wxsp/.apc/`):

```json
{
  "schema_version": 1,
  "device_id": null,
  "last_success_at": "2026-05-15T08:23:00+08:00",
  "today_date": "2026-05-15",
  "today_verdict": "pass",
  "license_jwt": "eyJhbGc..."
}
```

- `device_id`:**首次 bootstrap 时为 `None`**;首次调 `/session/init` 时 SDK 把 `null` 送给后台,后台分配并通过 JWT 的 `did` claim 回传,SDK 存下来。后续启动 SDK 送已存的 did,后台 refresh 同一台(返回 did 不变)。SDK **不本地 uuid4 生成**,避免 APC 后台堆积"假设备"
- `last_success_at`:最近一次 `/session/init` 返回 200 + JWT 校验通过的时间;**首次安装写为 `now()`**(给 7 天试用)
- `today_date` + `today_verdict`:当日缓存判决,跨日清空
- `license_jwt`:留存最近一次成功签发的 JWT,**仅作 debug 参考,不参与判决**

原子写:写到 `session.json.tmp` → `os.replace()`,避免半写入文件导致下次启动 JSON 损坏。

### 4.3 状态机伪代码

```python
def check(self) -> Verdict:
    cache = self._cache.load()  # 损坏 → 当作首次安装重建
    today = date.today().isoformat()

    # 1. 当日已经判过
    if cache.get("today_date") == today:
        return Verdict(cache["today_verdict"])

    # 2. 跨日,清今日缓存,保留 last_success_at + device_id(可能为 None)
    try:
        # 首次启动 cache.get("device_id") 为 None,POST 时序列化成 "device_id": null
        license_jwt = self._fetch_session(cache.get("device_id"))
        claims = self._crypto.verify_jwt(license_jwt, self._config.public_key)
        # 后台始终把 did 写进 JWT;首次签发本地 None → 用后台给的,后续两边一致
        new_device_id = claims.get("did") or cache.get("device_id")
        self._cache.update(
            device_id=new_device_id,
            license_jwt=license_jwt,
            last_success_at=now_iso(),
            today_date=today,
            today_verdict=Verdict.PASS.value,
        )
        return Verdict.PASS

    except ApcDenied:
        # 4xx 拒绝,缓存今日 DENY,不更新 last_success_at
        self._cache.update(today_date=today, today_verdict=Verdict.DENY.value)
        return Verdict.DENY

    except ApcNetworkError:
        # 网络问题:检查 grace。注意不写 today_verdict,下次启动还会再试。
        last_success = parse_iso(cache.get("last_success_at"))
        if (now() - last_success) > timedelta(days=self._config.grace_days):
            # 超 grace,升级为 DENY 并缓存(避免每个 task 都重新调网络)
            self._cache.update(today_date=today, today_verdict=Verdict.DENY.value)
            return Verdict.DENY
        return Verdict.PASS  # grace 内,fail-open;不写缓存
```

**首次安装**(`cache.load()` 返回空字典):

```python
def _bootstrap_if_empty(self, cache: dict) -> dict:
    if not cache:
        cache = {
            "schema_version": 1,
            "device_id": None,           # 等 APC 后台首次签发时分配并通过 JWT.did 回传
            "last_success_at": now_iso(),  # 给 7 天试用
        }
        self._cache.save(cache)
    return cache
```

### 4.4 HTTP 调用细节

`POST {endpoint}/api/v2/session/init`

Headers:
- `Content-Type: application/json`
- `X-Client-Id: {app_id}`
- `X-T: {秒级 Unix 时间戳}`
- `X-Sig: HMAC-SHA256(secret, "POST\n/api/v2/session/init\n{ts}\n{sha256(body)}")`

Body(请求):
```json
{
  "device_id": null,
  "client_meta": {"channel": "视频号", "app_version": "0.1.4", "platform": "darwin", "hostname": "..."}
}
```

- 首次启动 `device_id=null`(SDK cache 里 bootstrap 时就是 `None`)
- 后续启动 SDK 送已存的 did;后台识别后 `_handle_known_device(did)`,返回同一台的新 JWT(refreshed,did 不变)

Body(响应,200):
```json
{
  "ok": true,
  "data": {
    "device_id": "845836f5-9e9a-44cc-ab79-622db70736ef",
    "token": "<JWT>",
    "expires_at": 1778918673,
    "ttl_seconds": 86400,
    "grace_period_seconds": 86400
  }
}
```

错误处理:
- `200` → 拿 `data.token` JWT,RS256 验签 → PASS
- `4xx`(含 401 / 403 / 404)→ raise `ApcDenied`(client.py 翻译成 DENY)
- `5xx` / 超时 / 连接失败 / TLS 失败 → raise `ApcNetworkError`(client.py 走 grace 逻辑)

JWT claims(实际):
```json
{
  "iss": "session",
  "sub": "<app_id>",
  "did": "<device_id>",
  "iat": 1778832273,
  "exp": 1778918673,
  "ttl": 86400,
  "grace": 86400
}
```

JWT 校验额外项:
- `exp` / `nbf`(`pyjwt` 自动)
- **`sub` 必须等于 `app_id`**(防 token 被错配到其他项目;APC 后台用 `sub` 不是 `aud`)
- `did` 必须等于本地存的 `device_id`(防 token 被搬到别的机器;首次签发时 local device_id 为 `None`,跳过这一检查,改用 JWT.did 写入本地)

### 4.5 自签证书 pinning(pinning.py)

```python
def build_httpx_client(timeout: float, fingerprint: str | None) -> httpx.Client:
    if fingerprint is None:
        return httpx.Client(timeout=timeout)  # 走默认 CA 链(Let's Encrypt 用)

    # 自签:关 CA 校验 + 手动比对 SHA-256
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    expected = fingerprint.lower().replace(":", "")

    transport = httpx.HTTPTransport(verify=ctx)
    # httpx 不直接暴露 cert.raw,需要在 connect 后从 socket 拿
    # 实现:wrap transport.handle_request,握手后从 SSLSocket.getpeercert(binary_form=True) 算 sha256
    return _PinnedHttpxClient(transport=transport, timeout=timeout, expected_fp=expected)
```

`_PinnedHttpxClient` 在每次连接建立后立刻读 peer cert,SHA-256 比对失败抛 `ssl.SSLError`(被 httpx 翻译成 `httpx.ConnectError`,被 client.py 翻译成 `ApcNetworkError`)。这一段是 ~30 行实现 + 1 个集成测试(用 `pytest_httpserver` 起自签 server)。

---

## 5. 关键边界与不变量

### 5.1 跨日时机

`today_date` 用本地时区的 `date.today()`。运营改本机时间到昨天 → SDK 仍按昨天算 → 今日缓存不失效;但 `last_success_at` 是 UTC ISO,改本机时间无法让 grace 倒退(grace 比较用 `datetime.now(timezone.utc) - parse_iso(last_success_at)`)。

**Threat model:运营改本机时间** — 改成将来 = 可能让 last_success 看起来"早 7 天" → 升级 DENY,等于自己关自己;改成过去 = 让 today_date 永远是昨天 → 缓存永不刷新,**但 `today_verdict` 一旦是 DENY 就会一直 DENY**(改时间没法绕过已经拒绝的判决)。改时间只能"延后或加速"被 kill,不能绕过。

### 5.2 缓存损坏

`session.json` 任何解析错误(`JSONDecodeError` / `KeyError` / 字段类型不对)→ 当作首次安装重建,写入 `last_success_at=now()`。**这意味着运营删除 session.json = 重置 7 天试用**,无解 — 但在我们威胁模型范围外(运营删 ~/Library 下隐藏目录的门槛 ≈ 拿 admin 跑 launchctl 卸载)。

### 5.3 关键 grace 测试用例(必须覆盖)

| ID | 前置 last_success_at | 调用结果 | 预期 verdict | 预期 last_success_at 更新? |
|----|---------------------|----------|--------------|---------------------------|
| G1 | now - 6d 23h 59m 59s | 网络超时 | PASS | 否 |
| G2 | now - 7d 0s 1ms      | 网络超时 | DENY | 否(写入今日 deny) |
| G3 | now - 6d             | 200 通过 | PASS | 是(更新到 now) |
| G4 | now - 6d             | 403 拒绝 | DENY | 否 |
| G5 | now - 8d(已超 grace) | 200 通过 | PASS | 是(grace 重新开始) |
| G6 | (无)首次安装       | 任何     | 看分支 | bootstrap 写入 now |

### 5.4 daemon 长跑

daemon 进程跨过 00:00 不重启 — 此时 `check_pass()` 第一次被调到时会发现 `today_date != today`,自动重新调 APC。也就是 daemon 不需要"定时唤醒检查",**它本身的每个 task 都是一次检查触发**。

如果运营那天没有任何 task(daily_limit=0 / 飞书表空)→ 那天不调 APC → `today_date` 不更新 → 但这不是问题:`last_success_at` 没有衰减压力,下一个有 task 的日子会调。

---

## 6. apc_sdk 包独立性

### 6.1 接口承诺

`apc_sdk` 对接入方暴露:
- `ApcClient(config: ApcConfig)`
- `ApcConfig` dataclass
- `Verdict` Enum
- `ApcDenied` / `ApcNetworkError` / `ApcConfigError` 异常

**不暴露** `cache.py` / `crypto.py` / `pinning.py` 内部接口(`_` 前缀私有)。其他项目改 SDK 内部不影响接入方。

### 6.2 依赖

`apc_sdk/pyproject.toml`:

```toml
[project]
name = "apc-sdk-python"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "httpx>=0.27",
    "pyjwt[crypto]>=2.8",
    "cryptography>=42",
]

[project.optional-dependencies]
dev = ["pytest>=8", "respx>=0.21", "pytest-httpserver>=1.0"]
```

**故意不引入** Pydantic / loguru / typer 等可能跟接入方冲突的库。日志走 `logging.getLogger("apc_sdk")`,接入方自己 hook。

### 6.3 wxsp 怎么依赖它

`pyproject.toml`(wxsp 根):

```toml
dependencies = [
    # ... 已有的
    "apc-sdk-python @ file:./apc_sdk",
]
```

`uv sync` 会以 editable + path 形式装,本地改 SDK 立刻在 wxsp 里生效。CI / 打包时同样有效(`file:./` 相对仓库根)。

### 6.4 其他项目怎么用

```bash
pip install "git+ssh://git@github.com/GySuper/m-zdfb.git#subdirectory=apc_sdk"
```

或在 `pyproject.toml`:

```toml
dependencies = [
    "apc-sdk-python @ git+ssh://git@github.com/GySuper/m-zdfb.git#subdirectory=apc_sdk",
]
```

接入示例(写在 `apc_sdk/README.md` 里):

```python
from pathlib import Path
from apc_sdk import ApcClient, ApcConfig, Verdict

client = ApcClient(ApcConfig(
    endpoint="https://203.0.113.5:8443",
    app_id="ap_xxxxxxxx",
    app_secret=os.environ["APC_APP_SECRET"],
    public_key=Path("/path/to/license_public.pem").read_text(),
    cache_dir=Path.home() / ".cache" / "myapp" / "apc",
    cert_fingerprint=os.environ.get("APC_CERT_FP"),
))

if client.check() == Verdict.PASS:
    run_business_logic()
else:
    sys.exit(1)
```

---

## 7. 验收标准(M-APC milestone)

| # | 标准 | 怎么验 |
|---|------|--------|
| 1 | `apc_sdk` 单测全绿 | `cd apc_sdk && uv run pytest` 全通过 |
| 2 | 7 天 grace 边界(§5.3 G1-G6)6 个 case 全通过 | 同上,`test_grace_logic.py` |
| 3 | 开发模式 `uv run wxsp run --task-id N` 不调网络 | mock httpx,断言 `httpx.Client.send` 未被调用 |
| 4 | 打包模式装 PASS → 正常发布 20 步 | 集成测试 `@pytest.mark.integration`,mock APC server 返回 200 |
| 5 | 打包模式装 DENY → step [4] 后 raise ElementNotFound,截图存在,event 写入 | 同上,mock 返回 403 |
| 6 | 打包模式 + 网络问题 + grace 内 → 正常发布 | mock httpx 超时,`last_success_at = now - 3d` |
| 7 | 打包模式 + 网络问题 + 超 grace → 装故障 | mock httpx 超时,`last_success_at = now - 8d` |
| 8 | build_macos.sh 跑完后 `git status` 干净(`apc_config.py` 已 revert) | 手工:`bash scripts/build_macos.sh && git diff --exit-code wxsp/apc_config.py` |
| 9 | 实际 .dmg 装到测试机,APC 后台 enable → 跑正常 task | 手工:跑 1 条真实 task,看视频号发布成功 |
| 10 | 实际 .dmg + APC 后台 disable 该 device → 次日 00:00 后启动,所有 task 装"等待上传区域超时" | 手工:回拨系统时间到次日,跑 1 条 task,看 Web UI 失败 |

每个标准都在 `apc_sdk/tests/` 或 `tests/test_apc_*.py` 有对应自动化测试(除标 "手工" 的几条)。

---

## 8. 偏离上层设计的点

| 项 | 上层 wxsp 设计原文 | 本设计 | 理由 |
|----|------------------|--------|------|
| 配置加载 | "所有配置走 Pydantic Settings (config.yaml + env)" | APC 凭据**不走** config.yaml,**不走** env(打包后),独立 `apc_config.py` 占位符 + build 时 patch | 隐蔽性硬约束:运营不能在 yaml 里看到 APC 字段 |
| 错误分类 | "element_not_found:1 次重试,截图,告警" | 装故障也走 element_not_found 路径,**复用现有重试 + 截图 + 告警** | 不可分辨,这是隐蔽性的目标 |
| 通知规则 | "task 失败 → 企微告警" | 装故障同样触发企微告警 | 不可分辨 |
| `Verdict.PASS` 跨日重新调网络 | 上层无规定 | 每自然日首次有 task 时调一次 | M11 已经做了打包 + 自启,daemon 长驻是常态 |

---

## 9. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 运营技术型,反编译 .pyc 拿到 app_secret | kill switch 失效 | 接受 — 威胁模型外 |
| APC 服务端长期挂(超过 7 天) | 所有打包版集体失效 | 运维责任。生产前监控 APC 服务可用性 |
| 用户 NAS 路径搜不到视频 + 同一天 APC 也拒绝 | 真故障被装故障掩盖 | doctor 不卡 APC,运营跑 `wxsp doctor` 会看到 NAS 错。但确实诊断变难,**接受** |
| `apc_config.py` 被运营改回占位符 | check_pass 失败 → fail-open(log warning)→ 全放行 | 打包后 `.pyc` 不可改;`.py` 即使被改也已经不被加载 |
| build 脚本中断时未 trap → 凭据进 git | secret 泄漏 | trap EXIT 兜底;pre-commit hook 加 `grep "ap_[a-z0-9]\{8,\}" wxsp/apc_config.py` 阻断提交 |
| `httpx` cert pinning 实现 bug → MITM 可能 | 服务端假签发 | 4 个 case 单测(指纹正确 / 错误 / 缺失 / TLS 错);手工跑 1 次假指纹必须失败 |

---

## 10. 实现顺序(给后续 plan 用)

1. **apc_sdk 骨架** — pyproject + 包结构 + 异常 + Verdict + ApcConfig
2. **apc_sdk crypto** — HMAC + JWT,纯函数,先写测试
3. **apc_sdk cache** — JSON 原子读写 + bootstrap,纯 IO,容易测
4. **apc_sdk pinning** — httpx + fingerprint(可后置,Let's Encrypt 路径先跑通)
5. **apc_sdk client** — 状态机串起来,用 respx mock 服务端
6. **apc_sdk grace_logic 测试** — 6 个 case
7. **wxsp/apc_config.py + wxsp/apc.py** — 粘合层,dev-mode 测试
8. **wxsp/publisher.py 接入** — `check_pass()` + `[4]` 后注入,集成测试
9. **build_macos.sh + build_windows.ps1 patch** — 凭据注入 + trap
10. **GitHub Actions** — 加 5 个 secrets
11. **手工验收** — 跑 §7 第 8-10 条

---

## 11. 联调修订(2026-05-15)

线上首次跟 APC 后台联调时发现的 contract 偏差,已对齐(commit `ece1714`、`89c9277`)。同期顺手修了 build 脚本的 chromium 拷贝 bug(commit `10cb9c1`)。本节记录上下文,避免后续人按旧版 spec 排查时绕路。

| 项 | 最初 spec | 实际 APC 后台 | 修复位置 |
|----|----------|---------------|---------|
| 响应里 JWT 字段 | `payload.license`(顶层) | `payload.data.token`(嵌一层) | `apc_sdk/_http.py` |
| JWT 标识 app 的 claim | `aud = app_id`(`pyjwt` 自动 audience 校验) | `sub = app_id`(无 `aud` claim) | `apc_sdk/crypto.py` |
| `device_id` 首次签发 | SDK `uuid.uuid4()` bootstrap,送给 APC | APC 不复用客户端送的 UUID,每收到陌生 did 就当新设备登记 → SDK 应送 `null`,等 APC 分配并通过 JWT.did 回传 | `apc_sdk/cache.py` |
| build_macos.sh 拷 chromium | `cp -R src dst/` 留版本目录 | BSD cp 把内容铺平,patchright 找不到浏览器 | `scripts/build_macos.sh` |

**回归测试覆盖**:
- `test_http.py::test_fetch_session_200_missing_data_raises_network` / `_data_without_token_raises_network` —— 响应壳错误必须走 NetworkError 分支(而不是静默放行)
- `test_crypto_jwt.py::test_verify_jwt_wrong_subject_rejects` —— `sub` mismatch 拒绝(原 `aud` 测试用例的对位)
- `test_cache.py::test_bootstrap_device_id_is_none` —— bootstrap 不再生成本地 UUID

**未来契约稳定性约定**:APC 后台同学如要改响应壳 / claim 名,**先提 issue 跟 SDK 这边对齐**。SDK 已写死按 `data.token` + `sub` 解析,改了直接挂。
