# APC SDK 接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 APC(Application Control)远程许可服务接入 wxsp,打包版每天首次启动调一次 APC 拿"通过 / 拒绝"判决,拒绝时让所有发布 task 装成"等待上传区域超时"故障,运营无感知;SDK 部分抽成可复用子包 `apc_sdk`。

**Architecture:** monorepo 子目录:`apc_sdk/`(独立 pyproject + httpx/pyjwt 通用 SDK)+ `wxsp/apc.py`(私有粘合层)+ `wxsp/apc_config.py`(凭据占位符,build 时被 patch + EXIT trap revert)+ `publisher.publish()` 在 step [4] 之后注入 ElementNotFound。

**Tech Stack:** httpx 0.27 + pyjwt[crypto] 2.8 + cryptography 42(SDK 包);现有 wxsp 栈(patchright sync_api、loguru、SQLModel);PyInstaller `--collect-all apc_sdk` 收子包进 bundle。

**Spec:** [docs/superpowers/specs/2026-05-15-apc-sdk-integration-design.md](../specs/2026-05-15-apc-sdk-integration-design.md)

---

## 全局约定

- 测试运行:`uv run pytest <path>` 在仓库根目录跑。子包测试 `cd apc_sdk && uv run pytest`,但 plan 里所有命令都从仓库根目录写绝对路径。
- 提交风格:Conventional Commits 中文(对齐 `git log` 现状,如 `feat(apc): ...`)。
- pre-commit hook 强制 ruff + mypy + 部分测试。任何 commit 前提交前确认 `pre-commit run --all-files` 通过(否则修复后再 commit,**不要 `--no-verify`**)。
- 每个 task 结束时仓库工作树干净。

---

## Task 1: apc_sdk 包脚手架

**Files:**
- Create: `apc_sdk/pyproject.toml`
- Create: `apc_sdk/README.md`
- Create: `apc_sdk/src/apc_sdk/__init__.py`
- Create: `apc_sdk/src/apc_sdk/_types.py`
- Create: `apc_sdk/src/apc_sdk/exceptions.py`
- Create: `apc_sdk/tests/__init__.py`
- Create: `apc_sdk/tests/test_smoke.py`
- Modify: `pyproject.toml`(wxsp 根 — 加 path 依赖)

- [ ] **Step 1: 创建 apc_sdk 目录结构**

```bash
mkdir -p apc_sdk/src/apc_sdk apc_sdk/tests
```

- [ ] **Step 2: 写 apc_sdk/pyproject.toml**

```toml
[project]
name = "apc-sdk-python"
version = "0.1.0"
description = "Python SDK for APC (Application Control) — minimal, no Pydantic"
readme = "README.md"
requires-python = ">=3.10"
license = {text = "MIT"}
dependencies = [
    "httpx>=0.27",
    "pyjwt[crypto]>=2.8",
    "cryptography>=42",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "respx>=0.21.0",
    "pytest-httpserver>=1.0.0",
    "trustme>=1.1.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/apc_sdk"]
```

- [ ] **Step 3: 写 apc_sdk/src/apc_sdk/_types.py**

```python
"""SDK 类型 + Verdict + ApcConfig。故意不引入 Pydantic 以减少接入方冲突。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TypedDict


class Verdict(str, Enum):
    PASS = "pass"
    DENY = "deny"


@dataclass(frozen=True)
class ApcConfig:
    """ApcClient 构造参数。frozen=True 避免接入方实例化后误改字段。"""

    endpoint: str  # "https://203.0.113.5:8443" 末尾不带 /
    app_id: str
    app_secret: str
    public_key: str  # JWT 校验用 PEM 公钥(含 BEGIN/END 头尾)
    cache_dir: Path
    cert_fingerprint: str | None = None  # 自签时填(小写 hex,无冒号)
    grace_days: int = 7
    request_timeout_seconds: float = 5.0
    client_meta: dict[str, str] = field(default_factory=dict)


class SessionCache(TypedDict, total=False):
    """session.json 内部结构。total=False 让旧版本缓存少字段也能 load。"""

    schema_version: int
    device_id: str
    last_success_at: str  # ISO 8601 UTC
    today_date: str  # YYYY-MM-DD 本地时区
    today_verdict: str  # "pass" | "deny"
    license_jwt: str
```

- [ ] **Step 4: 写 apc_sdk/src/apc_sdk/exceptions.py**

```python
"""SDK 异常类型。"""


class ApcError(Exception):
    """基类,接入方可以一次 catch 所有 SDK 异常。"""


class ApcConfigError(ApcError):
    """构造时配置非法(空字符串、文件不存在等)。"""


class ApcDenied(ApcError):
    """APC 服务端明确拒绝(4xx)。client.check() 翻译成 Verdict.DENY。"""


class ApcNetworkError(ApcError):
    """网络问题:连不上 / 超时 / TLS 失败 / 5xx。client.check() 走 grace 逻辑。"""
```

- [ ] **Step 5: 写 apc_sdk/src/apc_sdk/__init__.py**

```python
"""APC SDK 公开接口。"""

from apc_sdk._types import ApcConfig, Verdict
from apc_sdk.exceptions import ApcConfigError, ApcDenied, ApcError, ApcNetworkError

__all__ = [
    "ApcConfig",
    "ApcConfigError",
    "ApcDenied",
    "ApcError",
    "ApcNetworkError",
    "Verdict",
]

# ApcClient 在 Task 7 加进来后,这里追加导出
```

- [ ] **Step 6: 写 apc_sdk/README.md**

```markdown
# apc-sdk-python

Python SDK for APC (Application Control).

最小依赖(httpx + pyjwt + cryptography);不引入 Pydantic / loguru / typer。

## 安装

仓库内:
```toml
[tool.uv.sources]
apc-sdk-python = { path = "./apc_sdk", editable = true }
```

外部项目:
```bash
pip install "git+ssh://git@github.com/GySuper/m-zdfb.git#subdirectory=apc_sdk"
```

## 使用

完整接口和 fail-open + 7 天 grace 语义见同仓 `docs/superpowers/specs/2026-05-15-apc-sdk-integration-design.md`。

最小例子(Task 7 完成后才能跑):

```python
from pathlib import Path
from apc_sdk import ApcClient, ApcConfig, Verdict

client = ApcClient(ApcConfig(
    endpoint="https://203.0.113.5:8443",
    app_id="ap_xxxxxxxx",
    app_secret=os.environ["APC_APP_SECRET"],
    public_key=Path("license_public.pem").read_text(),
    cache_dir=Path.home() / ".cache" / "myapp" / "apc",
    cert_fingerprint=os.environ.get("APC_CERT_FP"),
))

if client.check() == Verdict.PASS:
    run_business_logic()
else:
    sys.exit(1)
```
```

- [ ] **Step 7: 写 apc_sdk/tests/__init__.py + apc_sdk/tests/test_smoke.py**

`apc_sdk/tests/__init__.py` 空文件。

`apc_sdk/tests/test_smoke.py`:

```python
"""冒烟测试:确认包能 import,公开类型齐全。"""


def test_imports():
    from apc_sdk import (
        ApcConfig,
        ApcConfigError,
        ApcDenied,
        ApcError,
        ApcNetworkError,
        Verdict,
    )

    assert Verdict.PASS.value == "pass"
    assert Verdict.DENY.value == "deny"


def test_apc_config_construction():
    from pathlib import Path

    from apc_sdk import ApcConfig

    cfg = ApcConfig(
        endpoint="https://example.com",
        app_id="ap_x",
        app_secret="s",
        public_key="-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----",
        cache_dir=Path("/tmp/apc"),
    )
    assert cfg.grace_days == 7
    assert cfg.cert_fingerprint is None
```

- [ ] **Step 8: 改 wxsp 根 pyproject.toml,加 uv path 依赖**

在 `[project]` 的 `dependencies` 末尾(末尾的 `]` 之前)加一行:

```toml
    "apc-sdk-python",
```

然后在 `[build-system]` 之前加新区块:

```toml
[tool.uv.sources]
apc-sdk-python = { path = "./apc_sdk", editable = true }
```

- [ ] **Step 9: 跑 uv sync 装上 apc_sdk + 跑冒烟测试**

```bash
uv sync --all-extras
uv run pytest apc_sdk/tests/test_smoke.py -v
```

Expected:`uv sync` 把 `apc_sdk` 以 editable 装入 venv;`pytest` 输出 `2 passed`。

- [ ] **Step 10: Commit**

```bash
git add apc_sdk/ pyproject.toml uv.lock
git commit -m "$(cat <<'EOF'
feat(apc): apc_sdk 子包脚手架(Verdict / ApcConfig / 异常类型)

monorepo 子目录形式,wxsp 通过 [tool.uv.sources] path 依赖装入。
最小依赖:httpx + pyjwt[crypto] + cryptography,不引入 Pydantic 等。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: HMAC 签名 + 测试

**Files:**
- Create: `apc_sdk/src/apc_sdk/crypto.py`(本 task 只写 hmac_sign,JWT 留 Task 3)
- Create: `apc_sdk/tests/test_crypto_hmac.py`

- [ ] **Step 1: 写失败测试 apc_sdk/tests/test_crypto_hmac.py**

```python
"""HMAC 签名向量测试。预期值对齐 sdk-integration.md §2.3 的 Node 示例算法:
    canonical = `${method}\n${path}\n${ts}\n${bodySha}`
    bodySha   = sha256(body, utf8).hex()
    signature = hmac_sha256(secret, canonical).hex()
"""

import hashlib
import hmac


def _reference_sign(secret: str, method: str, path: str, ts: str, body: str) -> str:
    """独立参考实现,跟被测函数对算。"""
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    canonical = f"{method}\n{path}\n{ts}\n{body_sha}"
    return hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def test_hmac_sign_matches_reference_empty_body():
    from apc_sdk.crypto import hmac_sign

    expected = _reference_sign("s3cr3t", "POST", "/api/v2/session/init", "1715763600", "")
    assert hmac_sign("s3cr3t", "POST", "/api/v2/session/init", "1715763600", "") == expected


def test_hmac_sign_matches_reference_with_body():
    from apc_sdk.crypto import hmac_sign

    body = '{"device_id":"abc","client_meta":{}}'
    expected = _reference_sign("s3cr3t", "POST", "/api/v2/session/init", "1715763600", body)
    assert (
        hmac_sign("s3cr3t", "POST", "/api/v2/session/init", "1715763600", body) == expected
    )


def test_hmac_sign_different_secret_different_result():
    from apc_sdk.crypto import hmac_sign

    a = hmac_sign("secret_a", "POST", "/x", "1", "body")
    b = hmac_sign("secret_b", "POST", "/x", "1", "body")
    assert a != b


def test_hmac_sign_returns_hex_lowercase_64chars():
    from apc_sdk.crypto import hmac_sign

    sig = hmac_sign("s", "POST", "/x", "1", "")
    assert len(sig) == 64
    assert all(c in "0123456789abcdef" for c in sig)
```

- [ ] **Step 2: 跑测试确认全部 FAIL**

```bash
uv run pytest apc_sdk/tests/test_crypto_hmac.py -v
```

Expected:4 个 test 全 FAIL,错误是 `ModuleNotFoundError: No module named 'apc_sdk.crypto'`。

- [ ] **Step 3: 写 apc_sdk/src/apc_sdk/crypto.py(只 HMAC 部分)**

```python
"""HMAC 签名(本文件 Task 3 会追加 JWT 校验)。"""

from __future__ import annotations

import hashlib
import hmac


def hmac_sign(secret: str, method: str, path: str, ts: str, body: str) -> str:
    """对齐 sdk-integration.md §2.3 的签名算法。返回 hex 小写字符串。

    canonical = method + "\\n" + path + "\\n" + ts + "\\n" + sha256(body).hex()
    signature = hmac_sha256(secret, canonical)
    """
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    canonical = f"{method}\n{path}\n{ts}\n{body_sha}"
    return hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
```

- [ ] **Step 4: 跑测试确认 PASS**

```bash
uv run pytest apc_sdk/tests/test_crypto_hmac.py -v
```

Expected:`4 passed`。

- [ ] **Step 5: Commit**

```bash
git add apc_sdk/src/apc_sdk/crypto.py apc_sdk/tests/test_crypto_hmac.py
git commit -m "$(cat <<'EOF'
feat(apc): HMAC-SHA256 签名实现 + 向量测试

签名算法对齐 sdk-integration.md §2.3 Node 示例:
canonical = method\\n + path\\n + ts\\n + sha256(body).hex

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: JWT RS256 校验 + 测试

**Files:**
- Modify: `apc_sdk/src/apc_sdk/crypto.py`(追加 verify_jwt)
- Create: `apc_sdk/tests/test_crypto_jwt.py`

- [ ] **Step 1: 写失败测试 apc_sdk/tests/test_crypto_jwt.py**

```python
"""JWT RS256 校验。用 cryptography 现场签一个 token,然后让被测函数验证。"""

from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization


def _keypair() -> tuple[str, str]:
    """返回 (private_pem, public_pem)。"""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return private_pem, public_pem


def _make_jwt(private_pem: str, **claims) -> str:
    return pyjwt.encode(claims, private_pem, algorithm="RS256")


def test_verify_jwt_valid_token():
    from apc_sdk.crypto import verify_jwt

    priv, pub = _keypair()
    now = datetime.now(timezone.utc)
    token = _make_jwt(
        priv,
        aud="ap_test",
        did="dev_a",
        iat=int(now.timestamp()),
        exp=int((now + timedelta(hours=1)).timestamp()),
    )
    claims = verify_jwt(token, public_key=pub, audience="ap_test")
    assert claims["did"] == "dev_a"
    assert claims["aud"] == "ap_test"


def test_verify_jwt_wrong_audience_rejects():
    from apc_sdk.crypto import verify_jwt
    from apc_sdk.exceptions import ApcDenied

    priv, pub = _keypair()
    now = datetime.now(timezone.utc)
    token = _make_jwt(
        priv,
        aud="ap_other",
        iat=int(now.timestamp()),
        exp=int((now + timedelta(hours=1)).timestamp()),
    )
    with pytest.raises(ApcDenied, match="audience"):
        verify_jwt(token, public_key=pub, audience="ap_test")


def test_verify_jwt_expired_rejects():
    from apc_sdk.crypto import verify_jwt
    from apc_sdk.exceptions import ApcDenied

    priv, pub = _keypair()
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    token = _make_jwt(
        priv,
        aud="ap_test",
        iat=int(past.timestamp()),
        exp=int((past + timedelta(minutes=1)).timestamp()),
    )
    with pytest.raises(ApcDenied, match="expired"):
        verify_jwt(token, public_key=pub, audience="ap_test")


def test_verify_jwt_wrong_signing_key_rejects():
    """用另一对密钥签的 token,本端的 pub key 应当拒签。"""
    from apc_sdk.crypto import verify_jwt
    from apc_sdk.exceptions import ApcDenied

    _, our_pub = _keypair()
    attacker_priv, _ = _keypair()
    token = _make_jwt(
        attacker_priv,
        aud="ap_test",
        exp=int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
    )
    with pytest.raises(ApcDenied, match="signature"):
        verify_jwt(token, public_key=our_pub, audience="ap_test")


def test_verify_jwt_did_mismatch_when_expected():
    """指定了 expected_did,token did 不匹配 → 拒。"""
    from apc_sdk.crypto import verify_jwt
    from apc_sdk.exceptions import ApcDenied

    priv, pub = _keypair()
    token = _make_jwt(
        priv,
        aud="ap_test",
        did="actual_device",
        exp=int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
    )
    with pytest.raises(ApcDenied, match="did"):
        verify_jwt(token, public_key=pub, audience="ap_test", expected_did="other_device")


def test_verify_jwt_did_check_skipped_when_expected_is_none():
    """首次签发,本地还没 device_id → expected_did=None 跳过 did 校验。"""
    from apc_sdk.crypto import verify_jwt

    priv, pub = _keypair()
    token = _make_jwt(
        priv,
        aud="ap_test",
        did="anything",
        exp=int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
    )
    claims = verify_jwt(token, public_key=pub, audience="ap_test", expected_did=None)
    assert claims["did"] == "anything"
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
uv run pytest apc_sdk/tests/test_crypto_jwt.py -v
```

Expected:6 个 test 全 FAIL,错误是 `ImportError: cannot import name 'verify_jwt'`。

- [ ] **Step 3: 追加 verify_jwt 到 apc_sdk/src/apc_sdk/crypto.py**

把文件末尾追加:

```python
from typing import Any

import jwt as pyjwt

from apc_sdk.exceptions import ApcDenied


def verify_jwt(
    token: str,
    *,
    public_key: str,
    audience: str,
    expected_did: str | None = None,
) -> dict[str, Any]:
    """RS256 校验 + 必要 claim 检查。失败一律抛 ApcDenied。

    Args:
        token: 服务端返回的 license JWT。
        public_key: PEM 公钥(含 BEGIN/END 头尾)。
        audience: 期望 aud,通常 = app_id;不等抛 ApcDenied。
        expected_did: 期望 did(本地已有 device_id);为 None 时跳过(首次签发场景)。

    Returns:
        token claims dict。

    Raises:
        ApcDenied: 任何校验失败(过期、签名错、aud 错、did 错)。
    """
    try:
        claims = pyjwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=audience,
            options={"require": ["exp", "aud"]},
        )
    except pyjwt.ExpiredSignatureError as exc:
        raise ApcDenied(f"JWT expired: {exc}") from exc
    except pyjwt.InvalidAudienceError as exc:
        raise ApcDenied(f"JWT audience mismatch: {exc}") from exc
    except pyjwt.InvalidSignatureError as exc:
        raise ApcDenied(f"JWT signature invalid: {exc}") from exc
    except pyjwt.PyJWTError as exc:
        raise ApcDenied(f"JWT decode failed: {exc}") from exc

    if expected_did is not None:
        actual_did = claims.get("did")
        if actual_did != expected_did:
            raise ApcDenied(
                f"JWT did mismatch: token did={actual_did!r}, expected did={expected_did!r}"
            )

    return claims
```

- [ ] **Step 4: 跑测试确认 PASS**

```bash
uv run pytest apc_sdk/tests/test_crypto_jwt.py -v
```

Expected:`6 passed`。

- [ ] **Step 5: Commit**

```bash
git add apc_sdk/src/apc_sdk/crypto.py apc_sdk/tests/test_crypto_jwt.py
git commit -m "$(cat <<'EOF'
feat(apc): JWT RS256 校验(pyjwt) + 6 case 测试

校验签名 + exp + aud + did(可选)。失败一律 ApcDenied。
expected_did=None 跳过 did 检查(首次签发场景)。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Cache 模块(原子读写 + bootstrap)+ 测试

**Files:**
- Create: `apc_sdk/src/apc_sdk/cache.py`
- Create: `apc_sdk/tests/test_cache.py`

- [ ] **Step 1: 写失败测试 apc_sdk/tests/test_cache.py**

```python
"""SessionStore: session.json 原子读写 + 首装 bootstrap + 损坏 fallback。"""

import json

import pytest


def test_load_nonexistent_returns_bootstrapped(tmp_path):
    from apc_sdk.cache import SessionStore

    store = SessionStore(tmp_path / "session.json")
    cache = store.load_or_bootstrap()
    assert "device_id" in cache
    assert "last_success_at" in cache
    assert cache["schema_version"] == 1
    # 文件应该已经写入磁盘
    assert (tmp_path / "session.json").exists()


def test_load_or_bootstrap_idempotent(tmp_path):
    """两次调用返回相同的 device_id(第二次读已写入的文件,不重写)。"""
    from apc_sdk.cache import SessionStore

    store = SessionStore(tmp_path / "session.json")
    cache1 = store.load_or_bootstrap()
    cache2 = store.load_or_bootstrap()
    assert cache1["device_id"] == cache2["device_id"]
    assert cache1["last_success_at"] == cache2["last_success_at"]


def test_save_then_load(tmp_path):
    from apc_sdk.cache import SessionStore

    store = SessionStore(tmp_path / "session.json")
    store.save({
        "schema_version": 1,
        "device_id": "abc",
        "last_success_at": "2026-05-10T00:00:00+00:00",
        "today_date": "2026-05-15",
        "today_verdict": "pass",
    })
    cache = store.load_or_bootstrap()
    assert cache["device_id"] == "abc"
    assert cache["today_verdict"] == "pass"


def test_save_is_atomic(tmp_path):
    """save 后,临时文件不应残留;主文件是完整 JSON。"""
    from apc_sdk.cache import SessionStore

    store = SessionStore(tmp_path / "session.json")
    store.save({"schema_version": 1, "device_id": "x", "last_success_at": "..."})
    # 没有 .tmp 残留
    assert not (tmp_path / "session.json.tmp").exists()
    # 主文件可解析
    text = (tmp_path / "session.json").read_text()
    assert json.loads(text)["device_id"] == "x"


def test_load_corrupted_json_rebootstraps(tmp_path):
    """文件损坏(非 JSON) → 当作首装重建,不抛。"""
    from apc_sdk.cache import SessionStore

    path = tmp_path / "session.json"
    path.write_text("this is not json {{{")
    store = SessionStore(path)
    cache = store.load_or_bootstrap()
    assert "device_id" in cache
    # 文件被覆盖成合法 JSON
    assert json.loads(path.read_text())["device_id"] == cache["device_id"]


def test_load_missing_required_field_rebootstraps(tmp_path):
    """缺 device_id 也视作损坏,重建。"""
    from apc_sdk.cache import SessionStore

    path = tmp_path / "session.json"
    path.write_text(json.dumps({"schema_version": 1}))
    store = SessionStore(path)
    cache = store.load_or_bootstrap()
    assert "device_id" in cache
    assert "last_success_at" in cache


def test_bootstrap_last_success_at_is_now_utc(tmp_path):
    """首装时 last_success_at 写当前时间 UTC ISO,而不是其他时区。"""
    from datetime import datetime, timezone

    from apc_sdk.cache import SessionStore

    store = SessionStore(tmp_path / "session.json")
    before = datetime.now(timezone.utc)
    cache = store.load_or_bootstrap()
    after = datetime.now(timezone.utc)

    written = datetime.fromisoformat(cache["last_success_at"])
    assert written.tzinfo is not None  # 有时区
    assert before <= written <= after


def test_update_partial(tmp_path):
    """update 只改给的 key,不清空别的。"""
    from apc_sdk.cache import SessionStore

    store = SessionStore(tmp_path / "session.json")
    store.load_or_bootstrap()
    store.update(today_date="2026-05-15", today_verdict="pass")
    cache = store.load_or_bootstrap()
    assert cache["today_date"] == "2026-05-15"
    assert cache["today_verdict"] == "pass"
    assert "device_id" in cache  # 没被清掉
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
uv run pytest apc_sdk/tests/test_cache.py -v
```

Expected:8 个 test 全 FAIL with `ModuleNotFoundError: No module named 'apc_sdk.cache'`。

- [ ] **Step 3: 写 apc_sdk/src/apc_sdk/cache.py**

```python
"""session.json 原子读写 + 首装 bootstrap + 损坏 fallback。"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REQUIRED_FIELDS = ("device_id", "last_success_at")


class SessionStore:
    """单文件 session.json 的薄包装。线程不安全(SDK 整体单实例使用)。"""

    def __init__(self, path: Path):
        self.path = path
        self._tmp = path.with_suffix(path.suffix + ".tmp")

    def load_or_bootstrap(self) -> dict[str, Any]:
        """读 session.json;不存在 / 损坏 / 缺必需字段 → 首装重建。"""
        cache = self._read_safe()
        if cache is None:
            cache = self._bootstrap()
            self.save(cache)
        return cache

    def save(self, cache: dict[str, Any]) -> None:
        """原子写:写到 .tmp → rename。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(self._tmp, self.path)

    def update(self, **fields: Any) -> None:
        """读 + 浅合并 + 保存。"""
        cache = self.load_or_bootstrap()
        cache.update(fields)
        self.save(cache)

    # --- 内部 ---

    def _read_safe(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            cache = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
        if not isinstance(cache, dict):
            return None
        if not all(field in cache for field in _REQUIRED_FIELDS):
            return None
        return cache

    def _bootstrap(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "device_id": str(uuid.uuid4()),
            "last_success_at": datetime.now(timezone.utc).isoformat(),
        }
```

- [ ] **Step 4: 跑测试确认 PASS**

```bash
uv run pytest apc_sdk/tests/test_cache.py -v
```

Expected:`8 passed`。

- [ ] **Step 5: Commit**

```bash
git add apc_sdk/src/apc_sdk/cache.py apc_sdk/tests/test_cache.py
git commit -m "$(cat <<'EOF'
feat(apc): SessionStore 原子读写 + 首装 bootstrap

- save 走 .tmp → os.replace,半写入文件不会破坏主文件
- load 时 JSON 损坏 / 缺必需字段 → 静默重建(首装语义)
- bootstrap 写 last_success_at = now_utc,给 7 天试用

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Pinning 模块(httpx + cert fingerprint)+ 测试

**Files:**
- Create: `apc_sdk/src/apc_sdk/pinning.py`
- Create: `apc_sdk/tests/test_pinning.py`

- [ ] **Step 1: 写失败测试 apc_sdk/tests/test_pinning.py**

```python
"""TLS cert fingerprint pinning。用 pytest-httpserver + trustme 起本地 HTTPS server。"""

import hashlib

import pytest
import trustme
from pytest_httpserver import HTTPServer


@pytest.fixture
def ca_and_cert(tmp_path):
    """返回 (httpserver_ssl_context, fingerprint_hex_lower)。"""
    ca = trustme.CA()
    server_cert = ca.issue_cert("127.0.0.1")
    cert_pem = server_cert.cert_chain_pems[0].bytes()
    # 算 DER 形式的 sha256
    import ssl
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    parsed = x509.load_pem_x509_certificate(cert_pem)
    der = parsed.public_bytes(Encoding.DER)
    fingerprint = hashlib.sha256(der).hexdigest()

    # 写文件给 pytest-httpserver
    cert_path = tmp_path / "server.pem"
    key_path = tmp_path / "server.key"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(server_cert.private_key_pem.bytes())

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return ctx, fingerprint


@pytest.fixture
def httpserver_ssl_context(ca_and_cert):
    return ca_and_cert[0]


def test_correct_fingerprint_passes(httpserver: HTTPServer, ca_and_cert):
    from apc_sdk.pinning import build_httpx_client

    _, fingerprint = ca_and_cert
    httpserver.expect_request("/ping").respond_with_json({"ok": True})

    with build_httpx_client(timeout=5.0, fingerprint=fingerprint) as client:
        resp = client.get(httpserver.url_for("/ping"))
        assert resp.status_code == 200


def test_wrong_fingerprint_raises_network_error(httpserver: HTTPServer, ca_and_cert):
    from apc_sdk.exceptions import ApcNetworkError
    from apc_sdk.pinning import build_httpx_client

    httpserver.expect_request("/ping").respond_with_json({"ok": True})
    bad_fingerprint = "0" * 64

    with build_httpx_client(timeout=5.0, fingerprint=bad_fingerprint) as client:
        with pytest.raises(ApcNetworkError, match="fingerprint mismatch"):
            client.get(httpserver.url_for("/ping"))


def test_no_fingerprint_uses_default_verify():
    """fingerprint=None 时返回的客户端走默认 CA 校验,不做 pinning。"""
    from apc_sdk.pinning import build_httpx_client

    client = build_httpx_client(timeout=5.0, fingerprint=None)
    # 这个 client 应该对公网 HTTPS 正常,对自签会被 CA 链拦下
    # 我们只验证不 raise 在构造期
    assert client is not None
    client.close()


def test_fingerprint_with_colons_and_uppercase_normalized(httpserver: HTTPServer, ca_and_cert):
    """SDK 接受 'AB:CD:..' 大写带冒号格式,自动归一化。"""
    from apc_sdk.pinning import build_httpx_client

    _, fp_lower = ca_and_cert
    fp_with_colons = ":".join(fp_lower[i : i + 2] for i in range(0, 64, 2)).upper()

    httpserver.expect_request("/ping").respond_with_json({"ok": True})
    with build_httpx_client(timeout=5.0, fingerprint=fp_with_colons) as client:
        resp = client.get(httpserver.url_for("/ping"))
        assert resp.status_code == 200
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
uv run pytest apc_sdk/tests/test_pinning.py -v
```

Expected:4 个 test 全 FAIL with `ModuleNotFoundError: No module named 'apc_sdk.pinning'`。

- [ ] **Step 3: 写 apc_sdk/src/apc_sdk/pinning.py**

```python
"""httpx 客户端构造 + 自签证书 SHA-256 指纹校验。"""

from __future__ import annotations

import hashlib
import ssl

import httpx

from apc_sdk.exceptions import ApcNetworkError


def _normalize_fingerprint(fp: str) -> str:
    """'AB:CD:...' / 'abcd...' 统一成小写无冒号 hex。"""
    return fp.replace(":", "").lower()


class _PinnedTransport(httpx.HTTPTransport):
    """握手后立即比对 peer cert SHA-256;不匹配抛 ApcNetworkError。

    实现思路:httpx 不暴露 SSLObject,但底层 httpcore 在 socket 建立时会触发
    transport.connect。我们重写 handle_request,在拿到 response 之前 / 之后都
    无法读 cert,只能在 socket 建立时 hook ssl 上下文的 verify_callback —— 但
    callback 在 ssl 标准库里只能 set_verify_callback(已废弃)。更稳的做法:
    用 ssl.SSLContext.set_servername_callback 或直接在 connect 时手动 wrap。

    最简单且兼容性最好的实现:用 httpx 的 verify 参数传一个自定义 SSLContext,
    把它的 verify_mode=CERT_NONE,然后在 handle_request 里通过 stream 拿到
    底层 socket → SSLSocket.getpeercert(binary_form=True) → sha256 比对。
    """

    def __init__(self, *, expected_fingerprint: str, **kwargs):
        # 关闭 CA 链校验(自签证书);手动 pinning 替代
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs.setdefault("verify", ctx)
        super().__init__(**kwargs)
        self._expected = _normalize_fingerprint(expected_fingerprint)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = super().handle_request(request)
        # 从底层 stream 拿 peer cert
        try:
            stream = response.stream  # httpcore.SyncStream
            # httpcore 0.18+:stream._stream._network_stream._sock 暴露 SSLSocket
            # 但层级私有,稳健做法是用 sslobject 抽象
            sock = self._extract_socket(stream)
            der = sock.getpeercert(binary_form=True)
        except Exception as exc:
            raise ApcNetworkError(f"无法读取 peer cert 做 pinning: {exc}") from exc

        actual = hashlib.sha256(der).hexdigest()
        if actual != self._expected:
            raise ApcNetworkError(
                f"cert fingerprint mismatch: got {actual}, expected {self._expected}"
            )
        return response

    @staticmethod
    def _extract_socket(stream):
        """从 httpx response.stream 一路下钻到 SSLSocket。"""
        obj = stream
        # 跟 httpx/httpcore 内部结构(0.27 / 1.x)
        for attr in ("_stream", "_network_stream", "_sock"):
            obj = getattr(obj, attr, obj)
        if not isinstance(obj, ssl.SSLSocket):
            raise RuntimeError(f"未找到 SSLSocket(拿到 {type(obj).__name__})")
        return obj


def build_httpx_client(*, timeout: float, fingerprint: str | None) -> httpx.Client:
    """构造 httpx.Client。

    - fingerprint=None:走默认 CA 校验(Let's Encrypt 等公网场景)
    - fingerprint=非空:关 CA 校验 + 手动 SHA-256 pin
    """
    if fingerprint is None:
        return httpx.Client(timeout=timeout)
    transport = _PinnedTransport(expected_fingerprint=fingerprint)
    return httpx.Client(timeout=timeout, transport=transport)
```

- [ ] **Step 4: 跑测试**

```bash
uv run pytest apc_sdk/tests/test_pinning.py -v
```

Expected:`4 passed`。如果 httpx/httpcore 私有 attr 跟版本不对,会有失败 — 此时调整 `_extract_socket` 适配当前 httpx 版本。**接受小幅迭代**;一旦 4 个 case 全过,继续。

- [ ] **Step 5: Commit**

```bash
git add apc_sdk/src/apc_sdk/pinning.py apc_sdk/tests/test_pinning.py
git commit -m "$(cat <<'EOF'
feat(apc): httpx + 自签证书 SHA-256 指纹校验

fingerprint=None → 默认 CA 链(Let's Encrypt 路径)
fingerprint=有值 → CERT_NONE + 握手后手动比对 sha256(peer der)
测试用 trustme + pytest-httpserver 起本地自签 HTTPS server 验证 4 case。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: HTTP fetch_session(POST /api/v2/session/init)+ 测试

**Files:**
- Create: `apc_sdk/src/apc_sdk/_http.py`(把 HTTP 调用封到独立内部模块)
- Create: `apc_sdk/tests/test_http.py`

- [ ] **Step 1: 写失败测试 apc_sdk/tests/test_http.py**

```python
"""fetch_session: 拼 body + 签名 + POST + 解析响应。用 respx mock httpx。"""

import json

import httpx
import pytest
import respx


def _basic_config(tmp_path):
    from pathlib import Path

    from apc_sdk import ApcConfig

    return ApcConfig(
        endpoint="https://apc.example.com:8443",
        app_id="ap_test",
        app_secret="s3cr3t",
        public_key="ignored-for-this-test",
        cache_dir=tmp_path,
    )


@respx.mock
def test_fetch_session_success_returns_license(tmp_path):
    from apc_sdk._http import fetch_session

    cfg = _basic_config(tmp_path)
    route = respx.post("https://apc.example.com:8443/api/v2/session/init").mock(
        return_value=httpx.Response(200, json={"license": "eyJ.fake.jwt"})
    )
    client = httpx.Client()
    license_jwt = fetch_session(client, cfg, device_id="dev-1")
    assert license_jwt == "eyJ.fake.jwt"
    assert route.called

    # 检查请求体
    sent = route.calls.last.request
    body = json.loads(sent.content)
    assert body["device_id"] == "dev-1"
    assert sent.headers["X-Client-Id"] == "ap_test"
    assert "X-T" in sent.headers
    assert "X-Sig" in sent.headers


@respx.mock
def test_fetch_session_device_id_none_when_first_call(tmp_path):
    """首次签发,device_id=None 直接序列化成 null。"""
    from apc_sdk._http import fetch_session

    cfg = _basic_config(tmp_path)
    route = respx.post("https://apc.example.com:8443/api/v2/session/init").mock(
        return_value=httpx.Response(200, json={"license": "tok"})
    )
    client = httpx.Client()
    fetch_session(client, cfg, device_id=None)
    body = json.loads(route.calls.last.request.content)
    assert body["device_id"] is None


@respx.mock
def test_fetch_session_403_raises_denied(tmp_path):
    from apc_sdk._http import fetch_session
    from apc_sdk.exceptions import ApcDenied

    cfg = _basic_config(tmp_path)
    respx.post("https://apc.example.com:8443/api/v2/session/init").mock(
        return_value=httpx.Response(403, json={"error": "DEVICE_DISABLED"})
    )
    client = httpx.Client()
    with pytest.raises(ApcDenied):
        fetch_session(client, cfg, device_id="dev-1")


@respx.mock
def test_fetch_session_500_raises_network(tmp_path):
    """5xx 视为网络问题,不算明确拒绝。"""
    from apc_sdk._http import fetch_session
    from apc_sdk.exceptions import ApcNetworkError

    cfg = _basic_config(tmp_path)
    respx.post("https://apc.example.com:8443/api/v2/session/init").mock(
        return_value=httpx.Response(503, text="upstream down")
    )
    client = httpx.Client()
    with pytest.raises(ApcNetworkError):
        fetch_session(client, cfg, device_id="dev-1")


@respx.mock
def test_fetch_session_timeout_raises_network(tmp_path):
    from apc_sdk._http import fetch_session
    from apc_sdk.exceptions import ApcNetworkError

    cfg = _basic_config(tmp_path)
    respx.post("https://apc.example.com:8443/api/v2/session/init").mock(
        side_effect=httpx.ConnectTimeout("simulated")
    )
    client = httpx.Client()
    with pytest.raises(ApcNetworkError):
        fetch_session(client, cfg, device_id="dev-1")


@respx.mock
def test_fetch_session_200_missing_license_raises_network(tmp_path):
    """200 但响应里没 license 字段 → 视为协议错(走网络分支,不写 today_verdict)。"""
    from apc_sdk._http import fetch_session
    from apc_sdk.exceptions import ApcNetworkError

    cfg = _basic_config(tmp_path)
    respx.post("https://apc.example.com:8443/api/v2/session/init").mock(
        return_value=httpx.Response(200, json={"oops": "no license"})
    )
    client = httpx.Client()
    with pytest.raises(ApcNetworkError):
        fetch_session(client, cfg, device_id="dev-1")


@respx.mock
def test_fetch_session_includes_client_meta(tmp_path):
    """ApcConfig.client_meta 被注入到 body。"""
    from apc_sdk._http import fetch_session
    from apc_sdk import ApcConfig

    cfg = ApcConfig(
        endpoint="https://apc.example.com:8443",
        app_id="ap_test",
        app_secret="s3cr3t",
        public_key="ignored",
        cache_dir=tmp_path,
        client_meta={"app_version": "0.1.4", "platform": "darwin"},
    )
    route = respx.post("https://apc.example.com:8443/api/v2/session/init").mock(
        return_value=httpx.Response(200, json={"license": "tok"})
    )
    client = httpx.Client()
    fetch_session(client, cfg, device_id="dev-1")
    body = json.loads(route.calls.last.request.content)
    assert body["client_meta"]["app_version"] == "0.1.4"
    assert body["client_meta"]["platform"] == "darwin"
```

- [ ] **Step 2: 跑测试 FAIL**

```bash
uv run pytest apc_sdk/tests/test_http.py -v
```

Expected:7 个 test 全 FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: 写 apc_sdk/src/apc_sdk/_http.py**

```python
"""POST /api/v2/session/init 调用,封装签名 + 错误翻译。

私有模块,接入方走 ApcClient,不直接调这里。
"""

from __future__ import annotations

import json
import time

import httpx

from apc_sdk._types import ApcConfig
from apc_sdk.crypto import hmac_sign
from apc_sdk.exceptions import ApcDenied, ApcNetworkError

_PATH = "/api/v2/session/init"


def fetch_session(client: httpx.Client, cfg: ApcConfig, *, device_id: str | None) -> str:
    """调 APC,返回 license JWT 字符串。

    Args:
        client: httpx.Client(可能带 pinning transport)。
        cfg: ApcConfig。
        device_id: 本地已有的 device_id;None 表示首次签发。

    Returns:
        license JWT 字符串(未校验签名,留给 crypto.verify_jwt)。

    Raises:
        ApcDenied: 4xx 响应。
        ApcNetworkError: 5xx / 网络异常 / 响应缺 license 字段。
    """
    body_dict = {"device_id": device_id, "client_meta": dict(cfg.client_meta)}
    body_bytes = json.dumps(body_dict, separators=(",", ":")).encode("utf-8")
    ts = str(int(time.time()))
    sig = hmac_sign(cfg.app_secret, "POST", _PATH, ts, body_bytes.decode("utf-8"))

    url = cfg.endpoint.rstrip("/") + _PATH
    headers = {
        "Content-Type": "application/json",
        "X-Client-Id": cfg.app_id,
        "X-T": ts,
        "X-Sig": sig,
    }

    try:
        response = client.post(url, content=body_bytes, headers=headers)
    except httpx.HTTPError as exc:
        raise ApcNetworkError(f"APC 请求失败: {exc!r}") from exc

    if 400 <= response.status_code < 500:
        raise ApcDenied(f"APC 拒绝(HTTP {response.status_code}): {response.text[:200]}")
    if response.status_code >= 500:
        raise ApcNetworkError(f"APC 服务端错(HTTP {response.status_code})")

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise ApcNetworkError(f"APC 响应非 JSON: {exc}") from exc

    license_jwt = payload.get("license")
    if not isinstance(license_jwt, str) or not license_jwt:
        raise ApcNetworkError(f"APC 响应缺 license 字段: {payload!r}")
    return license_jwt
```

- [ ] **Step 4: 跑测试 PASS**

```bash
uv run pytest apc_sdk/tests/test_http.py -v
```

Expected:`7 passed`。

- [ ] **Step 5: Commit**

```bash
git add apc_sdk/src/apc_sdk/_http.py apc_sdk/tests/test_http.py
git commit -m "$(cat <<'EOF'
feat(apc): fetch_session(POST /api/v2/session/init)+ 7 case 测试

- 拼 body(device_id + client_meta)、HMAC 签名、X-T/X-Sig/X-Client-Id 头
- 4xx → ApcDenied;5xx / 超时 / 响应缺 license → ApcNetworkError

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: ApcClient.check() 状态机 + grace 逻辑测试

**Files:**
- Create: `apc_sdk/src/apc_sdk/client.py`
- Create: `apc_sdk/tests/test_grace_logic.py`
- Modify: `apc_sdk/src/apc_sdk/__init__.py`(导出 ApcClient)

- [ ] **Step 1: 写失败测试 apc_sdk/tests/test_grace_logic.py(spec §5.3 6 个 case)**

```python
"""ApcClient.check() 7 天 grace + fail-open 边界。

注入 frozen clock + mock fetch_session,避开真实 httpx/网络。
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _config(tmp_path: Path):
    from apc_sdk import ApcConfig

    return ApcConfig(
        endpoint="https://apc.example.com:8443",
        app_id="ap_test",
        app_secret="s",
        public_key="-----BEGIN PUBLIC KEY-----\nignored\n-----END PUBLIC KEY-----",
        cache_dir=tmp_path,
        grace_days=7,
    )


def _seed_cache(tmp_path: Path, last_success_at: datetime, device_id: str = "dev"):
    """预置 session.json,模拟非首装状态。"""
    import json

    (tmp_path / "session.json").write_text(
        json.dumps({
            "schema_version": 1,
            "device_id": device_id,
            "last_success_at": last_success_at.isoformat(),
        })
    )


class _FakeClock:
    def __init__(self, now: datetime):
        self.now = now

    def utcnow(self) -> datetime:
        return self.now


def _make_client(tmp_path, fake_now, *, fetch_result):
    """构造 ApcClient,注入 mock fetch_session + fake clock。"""
    from apc_sdk.client import ApcClient
    from apc_sdk.exceptions import ApcDenied, ApcNetworkError

    cfg = _config(tmp_path)
    client = ApcClient(cfg)
    # monkeypatch 内部依赖
    client._clock = _FakeClock(fake_now)

    def fake_fetch(*args, **kwargs):
        if isinstance(fetch_result, Exception):
            raise fetch_result
        return fetch_result

    client._fetch_session = fake_fetch  # type: ignore[attr-defined]

    # bypass JWT 校验:直接把 fetch 返回值当 device_id 来源
    def fake_verify(license_jwt: str, **_kwargs):
        return {"did": "dev", "aud": "ap_test"}

    client._verify_jwt = fake_verify  # type: ignore[attr-defined]
    return client


def test_G1_grace_6d_23h_59m_network_failure_returns_PASS(tmp_path):
    """边界 1:6d 23h 59m + 网络失败 → PASS,不写 today_verdict。"""
    from apc_sdk import Verdict
    from apc_sdk.exceptions import ApcNetworkError

    now = datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc)
    last_ok = now - timedelta(days=6, hours=23, minutes=59, seconds=59)
    _seed_cache(tmp_path, last_ok)

    client = _make_client(tmp_path, now, fetch_result=ApcNetworkError("simulated"))
    assert client.check() == Verdict.PASS

    # 没写 today_verdict
    import json

    cache = json.loads((tmp_path / "session.json").read_text())
    assert "today_verdict" not in cache


def test_G2_grace_7d_1s_network_failure_returns_DENY_and_caches(tmp_path):
    """边界 2:7d 1s + 网络失败 → DENY,且写入 today_verdict。"""
    from apc_sdk import Verdict
    from apc_sdk.exceptions import ApcNetworkError

    now = datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc)
    last_ok = now - timedelta(days=7, seconds=1)
    _seed_cache(tmp_path, last_ok)

    client = _make_client(tmp_path, now, fetch_result=ApcNetworkError("simulated"))
    assert client.check() == Verdict.DENY

    import json

    cache = json.loads((tmp_path / "session.json").read_text())
    assert cache["today_verdict"] == "deny"


def test_G3_200_resets_last_success_at(tmp_path):
    """边界 3:200 通过 → last_success_at 更新到 now。"""
    from apc_sdk import Verdict

    now = datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc)
    last_ok = now - timedelta(days=6)
    _seed_cache(tmp_path, last_ok)

    client = _make_client(tmp_path, now, fetch_result="fake.jwt.token")
    assert client.check() == Verdict.PASS

    import json

    cache = json.loads((tmp_path / "session.json").read_text())
    updated = datetime.fromisoformat(cache["last_success_at"])
    assert updated == now


def test_G4_403_does_not_reset_last_success_at(tmp_path):
    """边界 4:403 拒绝 → today_verdict=deny,last_success_at 不变。"""
    from apc_sdk import Verdict
    from apc_sdk.exceptions import ApcDenied

    now = datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc)
    last_ok = now - timedelta(days=6)
    _seed_cache(tmp_path, last_ok)

    client = _make_client(tmp_path, now, fetch_result=ApcDenied("forbidden"))
    assert client.check() == Verdict.DENY

    import json

    cache = json.loads((tmp_path / "session.json").read_text())
    assert cache["today_verdict"] == "deny"
    # last_success_at 不变
    assert datetime.fromisoformat(cache["last_success_at"]) == last_ok


def test_G5_over_grace_then_200_resets(tmp_path):
    """边界 5:已超 grace 后,APC 又能联上 → 200 → 重置 grace,放行。"""
    from apc_sdk import Verdict

    now = datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc)
    last_ok = now - timedelta(days=8)
    _seed_cache(tmp_path, last_ok)

    client = _make_client(tmp_path, now, fetch_result="fake.jwt.token")
    assert client.check() == Verdict.PASS

    import json

    cache = json.loads((tmp_path / "session.json").read_text())
    assert datetime.fromisoformat(cache["last_success_at"]) == now


def test_G6_first_install_no_cache_writes_now(tmp_path):
    """边界 6:无 session.json + 网络问题 → bootstrap 写 last_success_at=now → 在 grace 内 → PASS。"""
    from apc_sdk import Verdict
    from apc_sdk.exceptions import ApcNetworkError

    now = datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc)
    # 不预置任何 cache

    client = _make_client(tmp_path, now, fetch_result=ApcNetworkError("simulated"))
    assert client.check() == Verdict.PASS

    import json

    cache = json.loads((tmp_path / "session.json").read_text())
    # bootstrap 写的 last_success_at 在 fake_clock 下是 now
    # 注意:cache bootstrap 用 datetime.now,不走 _clock — 见 client._maybe_bootstrap
    # 因此这条只验证 verdict 是 PASS,不验证写入时刻精确等于 now
    assert "device_id" in cache


def test_today_cache_short_circuits_no_network_call(tmp_path):
    """当日已经判过(today_date == today) → 直接走缓存,不调 fetch。"""
    import json
    from datetime import date

    from apc_sdk import Verdict

    now = datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc)
    last_ok = now - timedelta(days=2)
    cache = {
        "schema_version": 1,
        "device_id": "dev",
        "last_success_at": last_ok.isoformat(),
        "today_date": "2026-05-15",
        "today_verdict": "deny",
    }
    (tmp_path / "session.json").write_text(json.dumps(cache))

    called = [False]

    def explode(*a, **kw):
        called[0] = True
        raise AssertionError("should not be called")

    from apc_sdk.client import ApcClient

    client = ApcClient(_config(tmp_path))
    client._clock = _FakeClock(now)
    client._fetch_session = explode  # type: ignore[attr-defined]

    assert client.check() == Verdict.DENY
    assert called[0] is False
```

- [ ] **Step 2: 跑测试 FAIL**

```bash
uv run pytest apc_sdk/tests/test_grace_logic.py -v
```

Expected:7 个 test 全 FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: 写 apc_sdk/src/apc_sdk/client.py**

```python
"""ApcClient:状态机 + 缓存协调。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

import httpx

from apc_sdk._http import fetch_session
from apc_sdk._types import ApcConfig, Verdict
from apc_sdk.cache import SessionStore
from apc_sdk.crypto import verify_jwt
from apc_sdk.exceptions import ApcDenied, ApcNetworkError
from apc_sdk.pinning import build_httpx_client


class _Clock(Protocol):
    def utcnow(self) -> datetime: ...


class _RealClock:
    def utcnow(self) -> datetime:
        return datetime.now(timezone.utc)


class ApcClient:
    """每天首次启动调一次的远程许可客户端。

    Public 接口:`__init__(cfg)` + `check() -> Verdict` + `device_id` 属性 + `close()`。
    """

    def __init__(self, cfg: ApcConfig):
        self._cfg = cfg
        self._cache = SessionStore(cfg.cache_dir / "session.json")
        self._clock: _Clock = _RealClock()
        self._http: httpx.Client | None = None
        # 测试可以覆盖:client._fetch_session = ... / client._verify_jwt = ...
        self._fetch_session = self._default_fetch_session
        self._verify_jwt = self._default_verify_jwt

    @property
    def device_id(self) -> str | None:
        return self._cache.load_or_bootstrap().get("device_id")

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def check(self) -> Verdict:
        """同步阻塞调用,返回今日判决。详见 spec §4.3。"""
        cache = self._cache.load_or_bootstrap()
        today = date.today().isoformat()

        # 1. 当日已判过 → 直接走缓存
        today_verdict = cache.get("today_verdict")
        if cache.get("today_date") == today and today_verdict in {"pass", "deny"}:
            return Verdict(today_verdict)

        # 2. 跨日 / 首次,调 APC
        try:
            license_jwt = self._fetch_session(self._get_http(), self._cfg, device_id=cache.get("device_id"))
            claims = self._verify_jwt(
                license_jwt,
                public_key=self._cfg.public_key,
                audience=self._cfg.app_id,
                expected_did=cache.get("device_id"),
            )
            new_device_id = claims.get("did") or cache.get("device_id")
            self._cache.update(
                device_id=new_device_id,
                license_jwt=license_jwt,
                last_success_at=self._clock.utcnow().isoformat(),
                today_date=today,
                today_verdict=Verdict.PASS.value,
            )
            return Verdict.PASS

        except ApcDenied:
            self._cache.update(today_date=today, today_verdict=Verdict.DENY.value)
            return Verdict.DENY

        except ApcNetworkError:
            last_success_str = cache.get("last_success_at")
            if last_success_str is None:
                # 异常情况:缓存丢字段。当作首装,放行。
                return Verdict.PASS
            last_success = datetime.fromisoformat(last_success_str)
            if last_success.tzinfo is None:
                last_success = last_success.replace(tzinfo=timezone.utc)
            elapsed = self._clock.utcnow() - last_success
            if elapsed > timedelta(days=self._cfg.grace_days):
                self._cache.update(today_date=today, today_verdict=Verdict.DENY.value)
                return Verdict.DENY
            return Verdict.PASS  # grace 内,不写 today_verdict

    # --- 内部 ---

    def _get_http(self) -> httpx.Client:
        if self._http is None:
            self._http = build_httpx_client(
                timeout=self._cfg.request_timeout_seconds,
                fingerprint=self._cfg.cert_fingerprint,
            )
        return self._http

    @staticmethod
    def _default_fetch_session(client: httpx.Client, cfg: ApcConfig, *, device_id: str | None) -> str:
        return fetch_session(client, cfg, device_id=device_id)

    @staticmethod
    def _default_verify_jwt(token: str, **kwargs: Any) -> dict[str, Any]:
        return verify_jwt(token, **kwargs)
```

- [ ] **Step 4: 改 apc_sdk/src/apc_sdk/__init__.py 导出 ApcClient**

把已有的 `__init__.py` 内容改成:

```python
"""APC SDK 公开接口。"""

from apc_sdk._types import ApcConfig, Verdict
from apc_sdk.client import ApcClient
from apc_sdk.exceptions import ApcConfigError, ApcDenied, ApcError, ApcNetworkError

__all__ = [
    "ApcClient",
    "ApcConfig",
    "ApcConfigError",
    "ApcDenied",
    "ApcError",
    "ApcNetworkError",
    "Verdict",
]
```

- [ ] **Step 5: 跑测试 PASS**

```bash
uv run pytest apc_sdk/tests/ -v
```

Expected:总计前面所有测试 + 本 task 7 个,全 PASS。

- [ ] **Step 6: Commit**

```bash
git add apc_sdk/src/apc_sdk/client.py apc_sdk/src/apc_sdk/__init__.py apc_sdk/tests/test_grace_logic.py
git commit -m "$(cat <<'EOF'
feat(apc): ApcClient.check() 状态机 + 7 day grace fail-open 测试

实现 spec §4.3 状态机:
- 当日缓存判决 → 短路返回
- 跨日/首次 → POST /session/init
  - 200 → PASS,更新 last_success_at,缓存今日 pass
  - 403 → DENY,缓存今日 deny,不更新 last_success_at
  - 网络问题:grace 内 PASS(不缓存),超 grace → DENY(缓存今日 deny)

7 case 覆盖 spec §5.3 G1-G6 + 当日缓存短路。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: wxsp 接入 — apc_config.py + apc.py 粘合层

**Files:**
- Create: `wxsp/apc_config.py`
- Create: `wxsp/apc.py`
- Create: `tests/test_wxsp_apc.py`

- [ ] **Step 1: 写 wxsp/apc_config.py(占位符,git tracked)**

```python
"""APC 凭据(打包时被 build 脚本替换;源码状态是占位符)。

打包脚本 scripts/build_macos.sh / build_windows.ps1 会用 EXIT trap 保证
patch 进 git 工作树后**总会** revert,源码克隆者永远看到占位符。
"""

APC_ENDPOINT = "__APC_ENDPOINT__"
APC_APP_ID = "__APC_APP_ID__"
APC_APP_SECRET = "__APC_APP_SECRET__"
APC_PUBLIC_KEY = "__APC_PUBLIC_KEY__"
APC_CERT_FP = "__APC_CERT_FP__"
```

- [ ] **Step 2: 写失败测试 tests/test_wxsp_apc.py**

```python
"""wxsp/apc.py 粘合层。重点验证 dev-mode 短路 + 异常 fail-open。"""

import os
from unittest.mock import MagicMock, patch


def test_is_dev_mode_when_not_packaged(monkeypatch):
    """开发模式 = 未打包 = is_dev_mode() True。"""
    monkeypatch.delenv("WXSP_DEV_MODE", raising=False)
    # 默认 pytest 跑在源码模式,is_packaged 应是 False
    from wxsp import apc

    assert apc.is_dev_mode() is True


def test_check_pass_dev_mode_returns_true_without_network(monkeypatch):
    """dev-mode 下 check_pass 直接 True,不应触发 ApcClient 实例化。"""
    monkeypatch.delenv("WXSP_DEV_MODE", raising=False)

    # 让 _client() 一旦被调到就爆炸,确保 dev-mode 不到这里
    with patch("wxsp.apc._client", side_effect=AssertionError("should not be called")):
        from wxsp import apc

        assert apc.check_pass() is True


def test_check_pass_packaged_calls_client(monkeypatch):
    """打包模式下调 ApcClient.check;返回 Verdict.PASS → True。"""
    monkeypatch.setattr("wxsp.config.is_packaged", lambda: True)

    fake_client = MagicMock()
    from apc_sdk import Verdict

    fake_client.check.return_value = Verdict.PASS

    with patch("wxsp.apc._client", return_value=fake_client):
        # 强制刷新 _client_singleton,因为之前 dev-mode 没建过
        import wxsp.apc as apc_mod

        apc_mod._client_singleton = None
        assert apc_mod.check_pass() is True
        fake_client.check.assert_called_once()


def test_check_pass_packaged_deny_returns_false(monkeypatch):
    monkeypatch.setattr("wxsp.config.is_packaged", lambda: True)

    fake_client = MagicMock()
    from apc_sdk import Verdict

    fake_client.check.return_value = Verdict.DENY

    with patch("wxsp.apc._client", return_value=fake_client):
        import wxsp.apc as apc_mod

        apc_mod._client_singleton = None
        assert apc_mod.check_pass() is False


def test_check_pass_packaged_exception_fail_open(monkeypatch, caplog):
    """SDK 内部 raise 时 fail-open(spec §3.2):返回 True + log warning。"""
    monkeypatch.setattr("wxsp.config.is_packaged", lambda: True)

    fake_client = MagicMock()
    fake_client.check.side_effect = RuntimeError("SDK internal bug")

    with patch("wxsp.apc._client", return_value=fake_client):
        import wxsp.apc as apc_mod

        apc_mod._client_singleton = None
        assert apc_mod.check_pass() is True
```

- [ ] **Step 3: 跑测试 FAIL**

```bash
uv run pytest tests/test_wxsp_apc.py -v
```

Expected:5 个 test 全 FAIL with `ModuleNotFoundError: No module named 'wxsp.apc'`。

- [ ] **Step 4: 写 wxsp/apc.py**

```python
"""wxsp 私有 APC 粘合层。封装 ApcClient 实例化 + dev-mode 旁路。

dev-mode(`is_packaged() = False`)永远放行,不触网。
打包模式 fail-open:SDK 内部任何 raise 都视作"放行 + log warning",
避免 SDK bug 把整个 wxsp 干瘫。
"""

from __future__ import annotations

from loguru import logger

from apc_sdk import ApcClient, ApcConfig, Verdict
from wxsp.apc_config import (
    APC_APP_ID,
    APC_APP_SECRET,
    APC_CERT_FP,
    APC_ENDPOINT,
    APC_PUBLIC_KEY,
)
from wxsp.config import get_user_data_dir, is_packaged

_client_singleton: ApcClient | None = None


def is_dev_mode() -> bool:
    """开发模式 = 未打包。打包后强制走 APC。"""
    return not is_packaged()


def _client() -> ApcClient:
    """惰性单例。dev-mode 不应调到这里(check_pass 短路了)。"""
    global _client_singleton
    if _client_singleton is None:
        cache_dir = get_user_data_dir() / ".apc"
        cache_dir.mkdir(parents=True, exist_ok=True)
        _client_singleton = ApcClient(ApcConfig(
            endpoint=APC_ENDPOINT,
            app_id=APC_APP_ID,
            app_secret=APC_APP_SECRET,
            public_key=APC_PUBLIC_KEY,
            cache_dir=cache_dir,
            cert_fingerprint=APC_CERT_FP or None,
            grace_days=7,
            request_timeout_seconds=5.0,
        ))
    return _client_singleton


def check_pass() -> bool:
    """业务侧调这个。返回 True = 允许跑;False = 装故障。

    dev-mode 永远 True(开发跑 uv run wxsp 不触网)。
    打包模式下 SDK 内部 raise 时 fail-open(避免内部 bug 干瘫 wxsp)。
    """
    if is_dev_mode():
        return True
    try:
        verdict = _client().check()
        return verdict == Verdict.PASS
    except Exception as exc:
        logger.warning(f"[apc] check 异常,fail-open: {exc!r}")
        return True
```

- [ ] **Step 5: 跑测试 PASS**

```bash
uv run pytest tests/test_wxsp_apc.py -v
```

Expected:`5 passed`。

- [ ] **Step 6: Commit**

```bash
git add wxsp/apc_config.py wxsp/apc.py tests/test_wxsp_apc.py
git commit -m "$(cat <<'EOF'
feat(apc): wxsp 粘合层(apc_config 占位符 + apc.check_pass)

- apc_config.py: 五个 __APC_*__ 占位符,build 脚本会 patch + revert
- apc.py: dev-mode 短路 + 打包模式调 ApcClient,SDK 异常时 fail-open

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: publisher.publish() 故障注入 + 测试

**Files:**
- Modify: `wxsp/publisher.py:435`(verify_logged_in 之后、upload_video 之前)
- Create: `tests/test_publisher_apc_injection.py`

- [ ] **Step 1: 写失败测试 tests/test_publisher_apc_injection.py**

风格对齐 `tests/test_publisher.py`:用 conftest 的 `make_settings`,fixture 模式参考 `pending_task`,helper 函数 `_fake_browser_ctx` / `_noop_steps` 直接 inline(避免动现有文件)。

```python
"""publisher.publish() 装故障注入。

`check_pass()=False` 时,publisher 在 step [4] 之后必须:
- sleep 45-75 秒(注入点用 monkeypatch 短路)
- 截图保存到 screenshots/{YYYYMM}/{task_id}_wait_upload_area.png
- raise ElementNotFound("等待上传区域超时(60s)")

风格对齐 tests/test_publisher.py:make_settings fixture from conftest,
WXSP_DB_PATH 经 monkeypatch 到 tmp_path,数据库 seed 在 fixture 里。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, select

from tests.conftest import make_settings
from wxsp.db import claim_task, get_engine, init_db
from wxsp.models import Account, Task, Video
from wxsp.publisher import publish


@pytest.fixture
def pending_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int, Path]:
    """最小可用 DB:1 account + 1 video + 1 pending task。复用 test_publisher.py 风格。"""
    db_path = tmp_path / "test.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)

    video_file = tmp_path / "v.mp4"
    video_file.write_bytes(b"fake-video")

    with Session(engine) as session:
        session.add(
            Account(
                id="a",
                display_name="A",
                user_data_dir=str(tmp_path / "profile"),
                daily_limit=20,
            )
        )
        session.add(
            Video(
                id="v1",
                file_path=str(video_file),
                title="标题" * 5,
                ingested_at=datetime.now(),
            )
        )
        session.add(
            Task(
                video_id="v1",
                account_id="a",
                execute_date=date.today(),
                publish_at=datetime.now() + timedelta(hours=2),
                status="pending",
            )
        )
        session.commit()
        task = session.exec(select(Task)).first()
        assert task is not None and task.id is not None
        task_id = task.id

    return task_id, tmp_path


def _fake_browser_ctx() -> MagicMock:
    fake_ctx = MagicMock()
    fake_ctx.__enter__.return_value = MagicMock(name="page")
    fake_ctx.__exit__.return_value = False
    return fake_ctx


def _noop_steps(**overrides):
    """所有步骤函数 mock 成 no-op;显式 override 用来注入异常 / 计数。"""
    fakes = {
        "open_publish_page": lambda *a, **kw: None,
        "verify_logged_in": lambda *a, **kw: None,
        "upload_video": lambda *a, **kw: None,
        "fill_title": lambda *a, **kw: None,
        "fill_description": lambda *a, **kw: None,
        "add_tags": lambda *a, **kw: None,
        "set_cover": lambda *a, **kw: None,
        "bind_topic": lambda *a, **kw: None,
        "toggle_original": lambda *a, **kw: None,
        "set_schedule": lambda *a, **kw: None,
        "risk_control_probe": lambda *a, **kw: None,
        "click_publish": lambda *a, **kw: None,
        "wait_for_success_indicator": lambda *a, **kw: None,
        "extract_remote_video_id_and_url": lambda page: (None, None),
        "random_pause": lambda *a, **kw: None,
    }
    fakes.update(overrides)
    return fakes


def test_publish_apc_pass_runs_full_pipeline(
    pending_task: tuple[int, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """check_pass=True 时,publisher 跑过 upload_video(并最终成功)。"""
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    monkeypatch.setattr("wxsp.apc.check_pass", lambda: True)

    upload_calls: list[bool] = []
    overrides = _noop_steps(
        upload_video=lambda *a, **kw: upload_calls.append(True),
        extract_remote_video_id_and_url=lambda page: ("rid", "https://channels/x"),
    )

    with (
        patch("wxsp.publisher.browser_context", return_value=_fake_browser_ctx()),
        patch("wxsp.publisher.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch("wxsp.publisher.cleanup_tmp"),
        patch("wxsp.publisher.screenshot", return_value=tmp_path / "shot.png"),
        patch.multiple("wxsp.publisher", **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is True, f"err={result.error_type} {result.error_msg}"
    assert upload_calls == [True]


def test_publish_apc_deny_injects_element_not_found(
    pending_task: tuple[int, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """check_pass=False 时:不调 upload / title / click_publish,error_type=element_not_found。"""
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    monkeypatch.setattr("wxsp.apc.check_pass", lambda: False)
    # 跳过真实 45-75s 等待
    monkeypatch.setattr("wxsp.publisher.time.sleep", lambda _s: None)
    monkeypatch.setattr("wxsp.publisher.random.uniform", lambda a, b: 0.0)

    upload_calls: list[bool] = []
    title_calls: list[bool] = []
    click_calls: list[bool] = []
    shot_steps: list[str] = []

    def fake_screenshot(page, *, task_id, step, screenshots_root, now=None):
        shot_steps.append(step)
        return tmp_path / f"{task_id}_{step}.png"

    overrides = _noop_steps(
        upload_video=lambda *a, **kw: upload_calls.append(True),
        fill_title=lambda *a, **kw: title_calls.append(True),
        click_publish=lambda *a, **kw: click_calls.append(True),
    )

    with (
        patch("wxsp.publisher.browser_context", return_value=_fake_browser_ctx()),
        patch("wxsp.publisher.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch("wxsp.publisher.cleanup_tmp"),
        patch("wxsp.publisher.screenshot", side_effect=fake_screenshot),
        patch.multiple("wxsp.publisher", **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is False
    assert result.error_type == "element_not_found"
    assert upload_calls == []
    assert title_calls == []
    assert click_calls == []
    assert "wait_upload_area" in shot_steps


def test_publish_dev_mode_no_apc_call(
    pending_task: tuple[int, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pytest 默认 is_packaged()=False → check_pass True,不触 ApcClient。"""
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    # 让 _client() 一旦被调到就爆炸(确认 dev-mode 短路)
    monkeypatch.setattr("wxsp.apc._client", lambda: (_ for _ in ()).throw(AssertionError("dev-mode 不应调网络")))
    monkeypatch.delenv("WXSP_DEV_MODE", raising=False)

    overrides = _noop_steps(
        extract_remote_video_id_and_url=lambda page: ("rid", "https://channels/x"),
    )
    with (
        patch("wxsp.publisher.browser_context", return_value=_fake_browser_ctx()),
        patch("wxsp.publisher.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch("wxsp.publisher.cleanup_tmp"),
        patch("wxsp.publisher.screenshot", return_value=tmp_path / "shot.png"),
        patch.multiple("wxsp.publisher", **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is True
```

- [ ] **Step 2: 跑测试 FAIL**

```bash
uv run pytest tests/test_publisher_apc_injection.py -v
```

Expected:3 个 test FAIL,因为 publisher.publish 还没注入 apc 逻辑(check_pass=False 时仍然跑 upload_video → mock 被调到 → assert_not_called 失败)。

- [ ] **Step 3: 改 wxsp/publisher.py 在 step [4] 之后注入故障**

`wxsp/publisher.py` 在文件顶部 imports 区域(line ~33 附近)加:

```python
import wxsp.apc
```

然后定位到 line 414 附近 `result = PublishResult(...)` 之后,在 `try:` 块的第一行**插入** apc check:

```python
        # APC 守门(spec §3.3 注入点):dev-mode 永远 True;打包模式看 APC 判决
        apc_passed = wxsp.apc.check_pass()
```

然后定位 `verify_logged_in(page)` 之后 + `random_pause(step_pause)` 之后(line 435 之后)+ `upload_video(...)` 之前(line 437 之前),**插入故障注入块**:

```python
                # APC 拒绝时装"等待上传区域超时"故障
                if not apc_passed:
                    last_step = "wait_upload_area"
                    time.sleep(random.uniform(45, 75))
                    screenshot_path = screenshot(
                        page,
                        task_id=task_id,
                        step="wait_upload_area",
                        screenshots_root=screenshots_root,
                    )
                    result.screenshots.append(str(screenshot_path))
                    raise ElementNotFound("等待上传区域超时(60s)")
```

(实现者:具体行号以打开 publisher.py 时为准;关键定位是"在 `verify_logged_in(page)` 紧跟 `random_pause(step_pause)` 之后,`last_step = "upload"` 之前"。)

- [ ] **Step 4: 跑测试 PASS**

```bash
uv run pytest tests/test_publisher_apc_injection.py -v
```

Expected:`3 passed`。

- [ ] **Step 5: 跑全量 wxsp 测试,确认没破坏其他**

```bash
uv run pytest -m "not integration" -v 2>&1 | tail -30
```

Expected:所有原有测试仍 PASS;新加 3 个也 PASS。

- [ ] **Step 6: Commit**

```bash
git add wxsp/publisher.py tests/test_publisher_apc_injection.py
git commit -m "$(cat <<'EOF'
feat(apc): publisher 在 step [4] 之后注入 ElementNotFound 故障

apc.check_pass()=False 时:
1. 不调 upload_video 及后续步骤
2. sleep 45-75 秒(模拟"等元素超时"耗时感)
3. 真截图(此时浏览器在视频号发布页)
4. raise ElementNotFound,走现有 retry + 截图 + 通知 + 飞书回写

dev-mode 永远 check_pass=True,publisher 行为零变化。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: build_macos.sh patch 凭据 + EXIT trap

**Files:**
- Modify: `scripts/build_macos.sh`

- [ ] **Step 1: 看现有 build_macos.sh 找 PyInstaller 调用位置**

```bash
grep -n "pyinstaller\|--collect-all" scripts/build_macos.sh
```

Expected 输出大致是:
```
40:uv run pyinstaller \
41:  --onedir \
...
```

- [ ] **Step 2: 在 PyInstaller 调用之前插入凭据 patch + trap**

打开 `scripts/build_macos.sh`,在 `echo "==> PyInstaller 打包"` 这行之前插入:

```bash
echo "==> 注入 APC 凭据"
: "${APC_ENDPOINT:?APC_ENDPOINT env var 必填(GitHub Actions secrets)}"
: "${APC_APP_ID:?APC_APP_ID env var 必填}"
: "${APC_APP_SECRET:?APC_APP_SECRET env var 必填}"
: "${APC_PUBLIC_KEY:?APC_PUBLIC_KEY env var 必填}"
: "${APC_CERT_FP:?APC_CERT_FP env var 必填(无自签证书时留空字符串)}"

# 任何退出路径都恢复源码占位符状态(成功 / 失败 / Ctrl-C)
trap 'git checkout -- wxsp/apc_config.py 2>/dev/null || true' EXIT

uv run python - <<'PYEOF'
import os, pathlib
p = pathlib.Path("wxsp/apc_config.py")
content = p.read_text()
for key in ("APC_ENDPOINT", "APC_APP_ID", "APC_APP_SECRET", "APC_PUBLIC_KEY", "APC_CERT_FP"):
    val = os.environ[key]
    # 用 repr 把字符串转成合法 Python 字面量,处理特殊字符 + 换行(PUBLIC_KEY 多行 PEM)
    content = content.replace(f'"__{key}__"', repr(val))
p.write_text(content)
print(f"==> 凭据已注入 wxsp/apc_config.py(打包后 trap 会 revert)")
PYEOF
```

- [ ] **Step 3: 在 PyInstaller `--collect-all wxsp` 后面加 `--collect-all apc_sdk`**

定位 PyInstaller 调用块,在 `--collect-all wxsp \` 后面新增一行 `  --collect-all apc_sdk \`:

```bash
uv run pyinstaller \
  --onedir \
  --windowed \
  --name wxsp \
  --osx-bundle-identifier com.wxsp.app \
  --collect-all wxsp \
  --collect-all apc_sdk \
  --collect-all jinja2 \
  ...
```

- [ ] **Step 4: 本地干跑(需要 5 个 env)**

实现者本地准备一个 stub env 跑(secret 用占位字符串):

```bash
export APC_ENDPOINT="https://example.com:8443"
export APC_APP_ID="ap_localtest"
export APC_APP_SECRET="local-test-secret"
export APC_PUBLIC_KEY="$(cat <<'EOF'
-----BEGIN PUBLIC KEY-----
LOCAL_TEST_KEY_FAKE
-----END PUBLIC KEY-----
EOF
)"
export APC_CERT_FP=""

bash scripts/build_macos.sh 2>&1 | head -20
```

只验证前 20 行(看到"凭据已注入"和"PyInstaller 打包"两个提示即可)。中途可以 Ctrl-C。

- [ ] **Step 5: 验证 trap 工作:`git status` 干净**

```bash
git status --short
```

Expected:**没有** `M  wxsp/apc_config.py`(trap 已经 revert)。如果有 → trap 没生效,检查 set -e 顺序。

- [ ] **Step 6: Commit**

```bash
git add scripts/build_macos.sh
git commit -m "$(cat <<'EOF'
feat(apc): build_macos.sh 注入 APC 凭据 + EXIT trap revert

打包前从 5 个 env(APC_ENDPOINT/APP_ID/APP_SECRET/PUBLIC_KEY/CERT_FP)
patch wxsp/apc_config.py 的占位符。trap 保证打包成败 / Ctrl-C 都
git checkout 还原成占位符,工作树干净。
PyInstaller 加 --collect-all apc_sdk 把子包收进 bundle。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: build_windows.ps1 patch 凭据

**Files:**
- Modify: `scripts/build_windows.ps1`

- [ ] **Step 1: 看现有 build_windows.ps1**

```bash
grep -n "pyinstaller\|--collect-all\|Write-Host" scripts/build_windows.ps1
```

- [ ] **Step 2: 在 PyInstaller 调用之前插入凭据 patch + try/finally**

PowerShell 没有 trap,用 try/finally 等价实现。打开 `scripts/build_windows.ps1`,定位 `Write-Host "==> PyInstaller 打包"` 之前,插入:

```powershell
Write-Host "==> 注入 APC 凭据"
foreach ($k in @("APC_ENDPOINT","APC_APP_ID","APC_APP_SECRET","APC_PUBLIC_KEY","APC_CERT_FP")) {
  if (-not (Test-Path env:$k)) {
    throw "$k env var 必填(GitHub Actions secrets;无自签证书时 APC_CERT_FP 设为空字符串)"
  }
}

uv run python -c @"
import os, pathlib
p = pathlib.Path('wxsp/apc_config.py')
content = p.read_text()
for key in ('APC_ENDPOINT','APC_APP_ID','APC_APP_SECRET','APC_PUBLIC_KEY','APC_CERT_FP'):
    val = os.environ[key]
    content = content.replace(f'\"__{key}__\"', repr(val))
p.write_text(content)
print('==> 凭据已注入')
"@
```

然后把剩余 PyInstaller + Inno Setup 调用整个**包裹在 try { ... } finally { git checkout ... }** 内。最小改动方式 — 在脚本最末尾加 `finally` 等价的 cleanup:

```powershell
# 在脚本末尾(最后一行 Write-Host 之后)加:
Write-Host "==> 恢复 apc_config.py 占位符"
git checkout -- wxsp/apc_config.py 2>$null
```

为了让 Ctrl-C / 异常也走这一行,需要把整个脚本主体包在 try/finally。最稳的实现:

把 `Write-Host "==> 注入 APC 凭据"` 之前所有内容(原本的清理 / launcher 写入 / 等等)保留不动。**从凭据注入开始**用 try/finally 包裹直到脚本末尾:

```powershell
try {
  Write-Host "==> 注入 APC 凭据"
  # ... 凭据注入 + PyInstaller + chromium 拷贝 + Inno Setup ...
} finally {
  Write-Host "==> 恢复 apc_config.py 占位符"
  git checkout -- wxsp/apc_config.py 2>$null | Out-Null
}
```

- [ ] **Step 3: 在 PyInstaller `--collect-all wxsp` 后面加 `--collect-all apc_sdk`**

定位 PowerShell 里的 PyInstaller 调用(用反引号 ` 行连续):

```powershell
uv run pyinstaller `
  --onedir `
  --console `
  --name wxsp `
  --collect-all wxsp `
  --collect-all apc_sdk `
  --collect-all jinja2 `
  ...
```

- [ ] **Step 4: 本地跳过(macOS 上无法直接跑 .ps1)**

无法本地干跑,实现者推送后看 CI 验。**直接进 Step 5 commit,跑 CI 时若失败再修**。

- [ ] **Step 5: Commit**

```bash
git add scripts/build_windows.ps1
git commit -m "$(cat <<'EOF'
feat(apc): build_windows.ps1 注入 APC 凭据 + try/finally revert

PowerShell 版本对齐 macOS:patch wxsp/apc_config.py,
try/finally 保证脚本失败 / Ctrl-C 都 git checkout 还原占位符。
PyInstaller 加 --collect-all apc_sdk。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: GitHub Actions 注入 APC secrets + 联调

**Files:**
- Modify: `.github/workflows/build.yml`

- [ ] **Step 1: 在两个 build job 的 "Build" step 加 env block**

打开 `.github/workflows/build.yml`,**`Build .dmg` step**(line 31 附近)的 `env:` 改成:

```yaml
      - name: Build .dmg
        env:
          WXSP_VERSION: ${{ github.ref_type == 'tag' && github.ref_name || '0.0.0-dev' }}
          APC_ENDPOINT:   ${{ secrets.APC_ENDPOINT }}
          APC_APP_ID:     ${{ secrets.APC_APP_ID }}
          APC_APP_SECRET: ${{ secrets.APC_APP_SECRET }}
          APC_PUBLIC_KEY: ${{ secrets.APC_PUBLIC_KEY }}
          APC_CERT_FP:    ${{ secrets.APC_CERT_FP }}
        run: bash scripts/build_macos.sh
```

同样改 **`Build setup.exe` step**(line 63 附近):

```yaml
      - name: Build setup.exe
        env:
          WXSP_VERSION: ${{ github.ref_type == 'tag' && github.ref_name || '0.0.0-dev' }}
          APC_ENDPOINT:   ${{ secrets.APC_ENDPOINT }}
          APC_APP_ID:     ${{ secrets.APC_APP_ID }}
          APC_APP_SECRET: ${{ secrets.APC_APP_SECRET }}
          APC_PUBLIC_KEY: ${{ secrets.APC_PUBLIC_KEY }}
          APC_CERT_FP:    ${{ secrets.APC_CERT_FP }}
        shell: pwsh
        run: |
          $OutputEncoding = [System.Text.UTF8Encoding]::new()
          [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
          .\scripts\build_windows.ps1
```

- [ ] **Step 2: 手工同步:用户在 GitHub 仓库 Settings → Secrets 配齐 5 个**

**这一步实现者本地无法跑,留作 user-actionable**:

```
Repository → Settings → Secrets and variables → Actions → New repository secret

APC_ENDPOINT     = https://<真实IP>:8443
APC_APP_ID       = ap_xxxxxxxx
APC_APP_SECRET   = <真实 secret>
APC_PUBLIC_KEY   = -----BEGIN PUBLIC KEY-----\n....\n-----END PUBLIC KEY-----
APC_CERT_FP      = <小写 hex 无冒号> (或空字符串)
```

实现者在 commit message 里**显式 @用户**:"⚠️ User action required: 在 GitHub Settings 配齐 5 个 secrets,见 README/spec §2.3"。

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/build.yml
git commit -m "$(cat <<'EOF'
feat(apc): GitHub Actions build job 注入 APC_* secrets

macOS + Windows 两个 build step 都加 5 个 APC env(从仓库 secrets 读)。
⚠️ User action required: 在 GitHub Settings → Secrets 配齐:
  APC_ENDPOINT / APC_APP_ID / APC_APP_SECRET / APC_PUBLIC_KEY / APC_CERT_FP

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 4: 联调 tag + 看 CI**

实现者按需打 dev tag 测一次 CI:

```bash
git tag v0.2.0-apc-dev
git push origin main v0.2.0-apc-dev
```

打开 GitHub Actions 看 build job 是否两个平台都成功。失败 → 看日志定位是 patch 步还是 PyInstaller / Inno Setup —— 此时**不要新发 tag**,在 main 上 fix-forward 再删旧 tag 重打。

---

## Task 13: 全量回归 + 手工验收准备

**Files:**
- Modify: `apc_sdk/README.md`(加一行链接到 wxsp spec)
- 无代码改动,纯验证 task

- [ ] **Step 1: 跑全量 SDK 测试**

```bash
uv run pytest apc_sdk/tests/ -v 2>&1 | tail -10
```

Expected:`32+ passed`(冒烟 2 + hmac 4 + jwt 6 + cache 8 + pinning 4 + http 7 + grace 7)。

- [ ] **Step 2: 跑全量 wxsp 测试(不含 integration)**

```bash
uv run pytest -m "not integration" -v 2>&1 | tail -20
```

Expected:所有原有测试 + 新增 wxsp_apc 5 个 + publisher_apc_injection 3 个全 PASS。

- [ ] **Step 3: 跑 ruff + mypy 确保通过**

```bash
uv run ruff check apc_sdk/ wxsp/apc.py wxsp/apc_config.py wxsp/publisher.py tests/test_wxsp_apc.py tests/test_publisher_apc_injection.py
uv run mypy apc_sdk/src wxsp/apc.py wxsp/apc_config.py
```

Expected:都 0 错误。

- [ ] **Step 4: 手工本地 .dmg 跑通 spec §7 验收 #4**

把 5 个 APC env 真实值导入,本地跑:

```bash
export APC_ENDPOINT="https://<真实IP>:8443"
export APC_APP_ID=...
export APC_APP_SECRET=...
export APC_PUBLIC_KEY="$(cat /path/to/license_public.pem)"
export APC_CERT_FP=...
bash scripts/build_macos.sh
```

产物在 `dist/wxsp-*.dmg`。装到测试机,先在 APC 后台 enable 该 device → 跑一条真实 task 看是否正常发布。

- [ ] **Step 5: 手工跑 spec §7 验收 #10**

在 APC 后台 disable 该 device,系统时间手动 +1 day(回拨设置 / `sudo date 0517...`),再启动 wxsp,跑一条 task。Web UI 看到 `task: failed, last_error_type=element_not_found, screenshot=.../wait_upload_area.png`。

- [ ] **Step 6: 在 apc_sdk/README.md 末尾加一行**

```markdown
---

完整接入设计、grace 语义、隐蔽性威胁模型见同仓 [`docs/superpowers/specs/2026-05-15-apc-sdk-integration-design.md`](../docs/superpowers/specs/2026-05-15-apc-sdk-integration-design.md)。
```

- [ ] **Step 7: Commit + 收尾**

```bash
git add apc_sdk/README.md
git commit -m "$(cat <<'EOF'
docs(apc): apc_sdk/README.md 加 wxsp spec 链接

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

至此 APC SDK 接入完成。验收 #4 #9 #10 是手工流程,实现者跑通后向 user 回报。
