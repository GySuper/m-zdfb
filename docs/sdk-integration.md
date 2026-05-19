# apc_sdk Python 接入指南(给 AI 看)

> 目标读者:接到 "把 APC 接进 X 项目" 任务的 AI agent。读完应直接知道改哪些文件、贴哪些代码、用什么命令验证。
> 源码:同仓 `apc_sdk/`。本文不解释协议,只讲怎么用。

---

## 0. SDK 是什么 + 接入的最小工作量

**一句话**:`apc_sdk` 让你的桌面程序每次启动调一次远程 APC 控制服务,拿到今日 `PASS` / `DENY` 判决;APC 后台可远程禁用某台设备或整个项目。

**SDK 不做**:不做用户登录、不做内容拉取、不做日志上报、不做后台轮询。**全程同步阻塞**,一次调用 ~100ms(命中本地缓存 <1ms)。

**接入的最小代价**:
1. 在 `pyproject.toml` 加 1 行依赖
2. 写 1 个 `your_app/apc.py` glue 文件(~30 行,见 §4)
3. 在 1 个业务热点(发布/启动/main entry)前调 `if not apc.check_pass(): exit/skip`
4. 把 5 个凭据塞进环境变量或打包注入(见 §5)

完事。**不要**为 SDK 包一层异步、线程池、缓存(SDK 内部已做);**不要**自己解析 JWT。

---

## 1. 公开 API 表面(只有这些)

```python
from apc_sdk import (
    ApcClient,      # 唯一 client 类
    ApcConfig,      # 不可变配置 dataclass
    Verdict,        # Enum: PASS | DENY
    ApcError,       # 异常基类
    ApcConfigError, # 配置非法
    ApcDenied,      # 服务端 4xx(check 内部已转 Verdict.DENY,业务一般不会看到)
    ApcNetworkError,# 连不上/超时/TLS 失败/5xx(check 内部已走 grace,业务一般不会看到)
)
```

```python
class ApcConfig:
    endpoint: str                          # 必填。"https://1.2.3.4:8443" 末尾不带 /
    app_id: str                            # 必填。"ap_xxxxx..."
    app_secret: str                        # 必填。HMAC 用,从 env 读或打包注入
    public_key: str                        # 必填。JWT 校验用 PEM(含 BEGIN/END 头尾)
    cache_dir: Path                        # 必填。SDK 写 session.json 的目录
    cert_fingerprint: str | None = None    # 自签证书才填(小写 hex,无冒号);公网 CA 留 None
    grace_days: int = 7                    # 网络挂了之后允许 fail-open 多久
    request_timeout_seconds: float = 5.0
    client_meta: dict[str, str] = {}       # 上报给后台用于区分设备(平台/版本/hostname 等)

class ApcClient:
    def __init__(self, cfg: ApcConfig): ...
    def check(self) -> Verdict: ...        # 业务调这个。同步阻塞。
    @property
    def device_id(self) -> str | None: ...  # 首次成功 check 后才有值
    def close(self) -> None: ...           # 关 httpx client;长进程退出前可调

class Verdict(str, Enum):
    PASS = "pass"
    DENY = "deny"
```

**调用契约**:
- `check()` 内部已经吃掉所有异常,只返回 `Verdict.PASS` 或 `Verdict.DENY`
- 当日同一进程再调 `check()` 不会触网(命中 today_verdict)
- 跨日第一次调会触网;失败时按 `last_success_at` + `grace_days` 判:7 天内 → PASS,超 7 天 → DENY
- **唯一会从 `check()` 漏出来的异常**:`ApcConfigError`(构造时配置非法)。其他 `ApcError` 子类都被吞了

---

## 2. 安装

SDK 内部全是 `from apc_sdk.xxx import ...` 绝对导入,所以**只要顶层包名仍叫 `apc_sdk`,无论用哪种方式接入,业务侧 `from apc_sdk import ApcClient` 都不变**。

### 选哪种

| 想要 | 选 |
|---|---|
| 跟主仓一起演进,偶尔 `cd apc_sdk && git pull` 同步 | §2.1 整目录 vendoring |
| SDK 就是新项目源码的一部分,以后独立改不回流 | §2.2 扁平 vendoring |
| 多个项目共用,改一次大家拉新版 | §2.3 git+ssh 远程引用 |
| monorepo 内同仓引用 | §2.4 path source |

### 2.1 整目录 vendoring(推荐:最像"直接拿过去用")

把整个 `apc_sdk/` 目录(含它自己的 `pyproject.toml` / `src/` / `tests/`)拷到新项目根:
```bash
cp -R /path/to/wechat-sph-upload/apc_sdk /path/to/new_project/
```

新项目 `pyproject.toml`:
```toml
[project]
dependencies = ["apc-sdk-python"]

[tool.uv.sources]
apc-sdk-python = { path = "./apc_sdk", editable = true }
```

`uv sync` 会自动装 SDK 的 3 个运行时依赖(httpx / pyjwt[crypto] / cryptography),不需要在新项目里手动写。

升级:把 `apc_sdk/` 目录覆盖一下就行;SDK 自带的 tests 也跟着过来,可以单独跑。

### 2.2 扁平 vendoring(只要源码,不要 SDK 自己的 pyproject)

只把 `src/apc_sdk/` 拷到新项目根,当成新项目自带的顶层包:
```bash
cp -R /path/to/wechat-sph-upload/apc_sdk/src/apc_sdk /path/to/new_project/
```

新项目 `pyproject.toml` 手动加 3 行运行时依赖:
```toml
[project]
dependencies = [
    "httpx>=0.27",
    "pyjwt[crypto]>=2.8",
    "cryptography>=42",
    # ...你项目原有依赖
]
```

不需要 `[tool.uv.sources]` 配置——`apc_sdk` 就是新项目源码的一部分。

适用:新项目体积要小、不想要 SDK 自带的测试/pyproject、确信不会回流改动。

### 2.3 git+ssh 远程引用(多项目共用)

`pyproject.toml`:
```toml
[project]
dependencies = [
    "apc-sdk-python @ git+ssh://git@github.com/GySuper/m-zdfb.git#subdirectory=apc_sdk",
]
```

或 uv 风格 sources:
```toml
[project]
dependencies = ["apc-sdk-python"]

[tool.uv.sources]
apc-sdk-python = { git = "ssh://git@github.com/GySuper/m-zdfb.git", subdirectory = "apc_sdk" }
```

需要 SSH key 能 clone 私仓。

### 2.4 monorepo / 同仓引用

`pyproject.toml`:
```toml
[project]
dependencies = ["apc-sdk-python"]

[tool.uv.sources]
apc-sdk-python = { path = "./apc_sdk", editable = true }
```

(跟 §2.1 一样的语法,只是不需要先 `cp` 一份——SDK 本来就在仓库里。)

### 2.5 验证安装

```bash
uv sync
uv run python -c "from apc_sdk import ApcClient, ApcConfig, Verdict; print('ok')"
```

输出 `ok` 即成功。SDK 只依赖 `httpx`、`pyjwt[crypto]`、`cryptography`,不会拉 Pydantic / loguru / typer。

---

## 3. 最小可跑示例

```python
import os
import sys
from pathlib import Path
from apc_sdk import ApcClient, ApcConfig, Verdict

client = ApcClient(ApcConfig(
    endpoint="https://1.2.3.4:8443",                 # 从运维拿
    app_id="ap_xxxxxxxx",                            # 从 APC 管理后台建项目时拿
    app_secret=os.environ["APC_APP_SECRET"],         # 同上,只显示一次
    public_key=Path("license_public.pem").read_text(),  # 从运维拿,内嵌在代码或资源里
    cache_dir=Path.home() / ".cache" / "myapp" / "apc",
    cert_fingerprint=os.environ.get("APC_CERT_FP"),  # 自签证书才填;公网 CA 留 None
))

if client.check() == Verdict.PASS:
    main()
else:
    sys.exit(1)
```

---

## 4. 推荐的 glue 文件(直接复制到 `your_app/apc.py`)

每个接入项目都应该写这一层薄包装。**不要在业务代码里直接 `from apc_sdk import ApcClient`**——你需要 dev-mode 旁路 + fail-open 兜底。

```python
"""your_app 私有 APC 粘合层。dev-mode 旁路 + fail-open 兜底。"""

from __future__ import annotations

import os
import socket
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from loguru import logger  # 换成你项目的 logger

from apc_sdk import ApcClient, ApcConfig, Verdict
from your_app.apc_config import (   # 见 §5
    APC_APP_ID,
    APC_APP_SECRET,
    APC_CERT_FP,
    APC_ENDPOINT,
    APC_PUBLIC_KEY,
)

_client_singleton: ApcClient | None = None


def is_dev_mode() -> bool:
    """开发模式 = 未打包。打包后强制走 APC。
    根据你的项目改判断逻辑;PyInstaller 看 sys.frozen,Nuitka 看 __compiled__。
    """
    if os.environ.get("YOUR_APP_DEV_MODE") == "1":
        return True
    return not getattr(sys, "frozen", False)


def _client_meta() -> dict[str, str]:
    """上报给 APC 后台,用于在管理界面区分该开还是关哪台。"""
    try:
        app_version = version("your-app")
    except PackageNotFoundError:
        app_version = "unknown"
    return {
        "app_version": app_version,
        "platform": sys.platform,
        "hostname": socket.gethostname(),
    }


def _client() -> ApcClient:
    """惰性单例。"""
    global _client_singleton
    if _client_singleton is None:
        cache_dir = Path.home() / ".cache" / "your_app" / "apc"
        cache_dir.mkdir(parents=True, exist_ok=True)
        _client_singleton = ApcClient(
            ApcConfig(
                endpoint=APC_ENDPOINT,
                app_id=APC_APP_ID,
                app_secret=APC_APP_SECRET,
                public_key=APC_PUBLIC_KEY,
                cache_dir=cache_dir,
                cert_fingerprint=APC_CERT_FP or None,  # 空字符串 → None
                grace_days=7,
                request_timeout_seconds=5.0,
                client_meta=_client_meta(),
            )
        )
    return _client_singleton


def check_pass() -> bool:
    """业务调这个。True=放行;False=拒绝。

    - dev-mode 永远 True(开发跑本地不触网,无需配 APC)
    - 打包模式:SDK 内部 raise 视作放行 + log warning,避免 SDK bug 干瘫你的程序
    """
    if is_dev_mode():
        return True
    try:
        return _client().check() == Verdict.PASS
    except Exception as exc:
        logger.warning(f"[apc] check 异常,fail-open: {exc!r}")
        return True
```

**业务侧使用**:
```python
import your_app.apc

def run_business():
    if not your_app.apc.check_pass():
        logger.error("APC 拒绝,跳过本次执行")
        return
    # 正常业务
```

调用时机:**只在业务真正会"产生外部副作用"的入口前调**。对桌面工具一般是:
- 每次定时任务触发前
- 每次手动点"立即运行"前
- 长驻进程的 main entry / Web UI 启动时

**不要每个 HTTP 请求都调**——SDK 内部当日命中缓存虽然 <1ms,但仍是文件 IO,业务热点没必要。

---

## 5. 凭据怎么进二进制(两种模式)

### 5.1 模式 A — env 驱动(开发/服务端/Docker)

`your_app/apc_config.py`:
```python
"""APC 凭据 from env。"""
import os

APC_ENDPOINT   = os.environ["APC_ENDPOINT"]
APC_APP_ID     = os.environ["APC_APP_ID"]
APC_APP_SECRET = os.environ["APC_APP_SECRET"]
APC_PUBLIC_KEY = os.environ["APC_PUBLIC_KEY"]
APC_CERT_FP    = os.environ.get("APC_CERT_FP", "")
```

适用:CI / Docker / systemd / 你能控制运行环境的部署。**不适用**:打包成 .app / .exe 发给终端用户(用户机器上没这些 env)。

### 5.2 模式 B — 打包占位符 + 构建期注入(分发给用户的二进制)

**这是 wxsp 用的模式,推荐**。源码进 git 永远是占位符,打包脚本临时替换 → PyInstaller/Nuitka 编译 → EXIT trap 恢复占位符 → git 工作树永远干净。攻击者拿到二进制需要反编译 .pyc / .so 才能拿到 secret。

`your_app/apc_config.py`(进 git 的版本):
```python
"""APC 凭据(打包时被 build 脚本替换;源码状态是占位符)。"""

APC_ENDPOINT = "__APC_ENDPOINT__"
APC_APP_ID = "__APC_APP_ID__"
APC_APP_SECRET = "__APC_APP_SECRET__"
APC_PUBLIC_KEY = "__APC_PUBLIC_KEY__"
APC_CERT_FP = "__APC_CERT_FP__"
```

打包脚本片段(macOS,Linux 同理;Windows 见 wxsp `scripts/build_windows.ps1`):
```bash
# 必填 env 校验
: "${APC_ENDPOINT:?APC_ENDPOINT env var 必填}"
: "${APC_APP_ID:?APC_APP_ID env var 必填}"
: "${APC_APP_SECRET:?APC_APP_SECRET env var 必填}"
: "${APC_PUBLIC_KEY:?APC_PUBLIC_KEY env var 必填}"
${APC_CERT_FP+:} false || { echo "APC_CERT_FP 必填(无自签证书则设为空字符串)" >&2; exit 1; }

# 任何退出路径都恢复源码占位符(成功 / 失败 / Ctrl-C)
trap 'git checkout -- your_app/apc_config.py 2>/dev/null || true' EXIT

# 注入(用 repr() 处理多行 PEM 和特殊字符)
python - <<'PYEOF'
import os, pathlib
p = pathlib.Path("your_app/apc_config.py")
content = p.read_text()
for key in ("APC_ENDPOINT", "APC_APP_ID", "APC_APP_SECRET", "APC_PUBLIC_KEY", "APC_CERT_FP"):
    content = content.replace(f'"__{key}__"', repr(os.environ[key]))
p.write_text(content)
PYEOF

# 然后正常 PyInstaller/Nuitka 编译
# pyinstaller --onedir --collect-all apc_sdk ...
```

**关键**:`--collect-all apc_sdk` 必须加,PyInstaller 才会把 SDK 全部模块打进去。

凭据来源:本地构建 → `.apc-env` 文件(gitignored)`source` 一下;CI → GitHub Actions Secrets。

### 5.3 在哪儿放 `.apc-env`(本地构建)

```bash
# .apc-env(必须在 .gitignore 里!)
export APC_ENDPOINT="https://1.2.3.4:8443"
export APC_APP_ID="ap_xxxxxxxx"
export APC_APP_SECRET="xxxxxxxxxxxxxxx"
export APC_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A...
-----END PUBLIC KEY-----"
export APC_CERT_FP="abcdef0123...0f1e"  # 见 §6;公网 CA 留空字符串
```

用法:
```bash
source .apc-env && bash scripts/build_macos.sh
```

`.gitignore` 必加 `.apc-env`。

---

## 6. `cert_fingerprint` — 自签证书 pinning

**只有 APC 服务端用自签证书时才需要**(常见于 `https://<IP>:8443` 部署)。公网域名 + Let's Encrypt 留 `None` 即可,httpx 走默认 CA 链。

### 6.1 算指纹

运维方在 APC 服务器跑:
```bash
openssl x509 -in /path/to/server.crt -noout -fingerprint -sha256
# sha256 Fingerprint=AB:CD:EF:01:23:...:0F:1E
```

或客户端从远端取:
```bash
echo | openssl s_client -connect <IP>:8443 -servername <IP> 2>/dev/null \
  | openssl x509 -outform DER \
  | openssl dgst -sha256
```

去冒号、转小写后塞进 `APC_CERT_FP`:`abcdef012345...0f1e`。

SDK 内部 `_normalize_fingerprint` 自动去冒号转小写,所以 `'AB:CD:...'` 和 `'abcd...'` 都接受;但**建议 env 里就存归一化形式**,跨平台脚本不容易踩坑。

### 6.2 指纹滚动

证书一旦换(过期、IP 变了、被迫 rotate),所有装机的二进制同时断连。所以:
- 指纹通过 env 注入,不要硬编码字面量
- 证书过期前提前通知接入方,客户端发新版本

SDK 目前**只支持单指纹**。需要双指纹平滑过渡的话改 `pinning.py`(几行)。

---

## 7. 缓存与状态

SDK 在 `cache_dir / session.json` 写一个 JSON,字段:
```json
{
  "schema_version": 1,
  "device_id": "dev_xxxxx",
  "last_success_at": "2026-05-15T07:00:00+00:00",
  "today_date": "2026-05-15",
  "today_verdict": "pass",
  "license_jwt": "<JWT>"
}
```

| 行为 | 触发条件 |
|---|---|
| 当日命中缓存,不触网 | `today_date == today AND today_verdict in {pass, deny}` |
| 跨日,触网调 `/api/v2/session/init` | 上述不满足 |
| 网络/超时失败,grace 内放行 | `now - last_success_at <= grace_days` |
| 网络/超时失败,grace 已过 | 写 `today_verdict=deny`,以后当日都拒绝 |
| 服务端 4xx(明确拒绝) | 写 `today_verdict=deny` |
| 服务端首装签发 device_id | 通过 JWT `did` claim 回传,SDK 落到 `session.json` |

**"换设备" = 删 `session.json`**。下次 `check()` 会以 `device_id=None` 调 init,服务端视为新设备,占新名额或重新进审批队列。

`cache_dir` 选址建议:
- macOS:`~/Library/Application Support/<your_app>/.apc` 或 `~/Library/Application Support/<your_app>/data/.apc`
- Windows:`%APPDATA%\<your_app>\.apc`
- Linux:`~/.config/<your_app>/.apc`
- 用 `platformdirs.user_data_dir("<your_app>")` 一行解决

**不要**放进项目目录或 `/tmp`:前者打包后路径只读,后者重启就清。

---

## 8. 失败模式速查

| 现象 | 原因 | 排查 |
|---|---|---|
| `ApcConfigError`,启动就 crash | `endpoint` / `app_id` / `app_secret` / `public_key` 空字符串或格式错 | 打印 `cfg`,确认 5 个字段都非空;`public_key` 含 BEGIN/END 头尾 |
| 一直 `Verdict.DENY` | (a) 服务端禁用了该项目/设备;(b) JWT 校验失败(公钥不匹配);(c) HMAC 签名错(app_secret 错) | 看 SDK 内部 log(自己加 `loguru.enable("apc_sdk")`);手动 curl 一下 init endpoint |
| 一直 `Verdict.PASS` 但实际后台已禁 | 当日缓存还在,要跨日才会重查 | 删 `session.json` 或等到第二天 |
| `cert fingerprint mismatch` | 证书换了 / 指纹算错 | 重新 §6.1 取一次指纹 |
| 完全连不上,httpx 超时 | (a) endpoint 错;(b) `:8443` 端口漏了;(c) 防火墙;(d) 自签证书没配指纹 | `curl -k https://<IP>:8443/api/v2/health`(若服务端有 health 端点) |
| `JWT subject mismatch` | `app_id` 跟 token 的 `sub` claim 不一致——通常是 endpoint 指向错环境(测试/生产混了) | 确认 `endpoint` 和 `app_id` 来自同一个 APC 部署 |
| `JWT did mismatch` | 缓存里的 `device_id` 和服务端记录不一致(手动改了 / 跨机器拷贝缓存) | 删 `session.json` 重新签发 |

**SDK 内部不打 log**(为了不污染接入方的 logger)。要看内部细节,在 glue 层的 `except` 里把 exc 打出来,或者用 httpx 自带的 transport hook 抓 HTTP。

---

## 9. 不接 APC 也能跑(开发态)

模式 A(env 驱动)+ 模式 B(打包占位符)的 glue 都有 dev-mode 短路(§4 的 `is_dev_mode()`),开发态永远返回 PASS。开发跑本地 `uv run your_app`,完全不需要 APC 服务运行。

也可以在测试里直接 mock:
```python
import your_app.apc

def test_business_with_apc_deny(monkeypatch):
    monkeypatch.setattr(your_app.apc, "check_pass", lambda: False)
    # 业务应当跳过
```

---

## 10. 常见踩坑

1. **`endpoint` 末尾带 `/`** → SDK 拼出来变成 `//api/v2/...`,有些反代会 400。配置里**绝不**带尾 slash。
2. **`public_key` 缺 BEGIN/END 头尾** → `pyjwt` 解析失败,所有 check 都 DENY。直接读 PEM 文件,**不要**手撕中间 base64。
3. **`app_secret` 末尾带空格 / 换行** → HMAC 签名 mismatch,服务端返 401,Verdict.DENY。env 里值用引号包好,`source` 时注意复制粘贴的尾随空白。
4. **多线程同时 `check()`** → SessionStore 内部不加锁,会出现 race。SDK 设计就是单实例 + 同步调用,业务侧别开多线程同时调。需要的话外面加一把 `threading.Lock`。
5. **PyInstaller 漏打 apc_sdk** → 运行时 `ModuleNotFoundError: apc_sdk._http`。`--collect-all apc_sdk` 必加。
6. **缓存放到只读路径** → 写 `session.json` 失败,每次 check 都重新触网 + 拿不到本地 grace。`cache_dir` 必须可写。
7. **CI 里 secrets 落进 build artifact** → 一定加 EXIT trap `git checkout -- your_app/apc_config.py`,验证产物里 `grep __APC` 应该为空、源码里应该恢复占位符。
8. **dev-mode 判断写错** → 打包后还认为是 dev,APC 永远短路,等于没接。打包后跑一次 `your_app.apc.is_dev_mode()` 应为 False。

---

## 11. 验收清单(接完跑一遍)

每个新项目接完 SDK,跑这 6 条:

1. `uv run python -c "from apc_sdk import ApcClient; print('ok')"` → ok
2. 开发态启动业务 → `your_app.apc.check_pass()` 返回 True 且不触网(看 httpx 没发请求)
3. 打包产物启动 → `session.json` 出现在 `cache_dir`,有 `device_id`、`today_verdict=pass`
4. 在 APC 管理后台**禁用该项目** → 删 `session.json` → 再启动 → `Verdict.DENY`,业务退出
5. 启用项目 → 删 `session.json` → 再启动 → `Verdict.PASS`,业务正常
6. 断网启动 → 在 7 天内 `Verdict.PASS`(grace),改系统时间到 8 天后再启动 → `Verdict.DENY`

第 4 / 5 / 6 步是 APC 的核心价值,**不跑等于没接**。

---

## 12. 参考实现

- 完整接入例子:同仓 [`wxsp/apc.py`](../wxsp/apc.py) + [`wxsp/apc_config.py`](../wxsp/apc_config.py) + [`scripts/build_macos.sh`](../scripts/build_macos.sh)(注入段) + [`scripts/build_windows.ps1`](../scripts/build_windows.ps1)
- SDK 源码:[`apc_sdk/src/apc_sdk/`](../apc_sdk/src/apc_sdk/)(总共 ~470 行,可整体读完)
- 单元测试模式参考:[`apc_sdk/tests/`](../apc_sdk/tests/)(尤其 `test_smoke.py` 走一次真实 httpx 流程的方法)
