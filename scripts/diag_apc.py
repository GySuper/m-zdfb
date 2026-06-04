"""APC 凭据/验签一次性诊断 —— 排查"打包版被 APC 拦(伪装成上传超时)"。

读的是和 build 完全相同的那套 APC_* 环境变量(本地 .apc-env 或 GitHub secrets),
所以复刻的就是打包版烤进去的凭据 + 客户端验签逻辑。会真实调一次后端 session/init
(device_id=None,模拟首装),后台可能多出一条临时设备记录,跑完手动清理即可。

输出末尾的 `VERDICT:` 一行是结论(只含判断词,不含密钥值,不会被 CI 打码)。

跑法:
    本地:  . .\\.apc-env.ps1 ; uv run python scripts/diag_apc.py
    CI:    见 .github/workflows/diag-apc.yml(网页点 Run workflow)
"""

from __future__ import annotations

import os
import pathlib
import sys
import time

import jwt as pyjwt

from apc_sdk._http import fetch_session
from apc_sdk._types import ApcConfig
from apc_sdk.crypto import verify_jwt
from apc_sdk.pinning import build_httpx_client


def _need(key: str) -> str:
    val = os.environ.get(key)
    if val is None:
        print(f"[缺] 环境变量 {key} 没设 —— 先加载凭据再跑", file=sys.stderr)
        sys.exit(2)
    return val


def main() -> None:
    endpoint = _need("APC_ENDPOINT")
    app_id = _need("APC_APP_ID")
    app_secret = _need("APC_APP_SECRET")
    public_key = _need("APC_PUBLIC_KEY")
    cert_fp = os.environ.get("APC_CERT_FP") or None

    # 结论收集(只放不含密钥的判断词)
    fetch_state = "?"
    verify_state = "?"
    reason = "?"

    print("===== ① 凭据自检(密钥值会被 CI 打码,看判断词即可)=====")
    print("public_key 长度  :", len(public_key))
    has_real_newline = "\n" in public_key
    has_literal_bsn = "\\n" in public_key
    looks_pem = public_key.lstrip().startswith("-----BEGIN")
    print("PEM 头正确(应 True)        :", looks_pem)
    print("含真实换行(应 True)        :", has_real_newline)
    print("含字面 \\n 两字符(应 False) :", has_literal_bsn, " <- True=公钥被存成单行,验签必挂")
    pem_broken = (not looks_pem) or (not has_real_newline) or has_literal_bsn

    print("\n===== ② 调后端 session/init(device_id=None,模拟首装)=====")
    cfg = ApcConfig(
        endpoint=endpoint,
        app_id=app_id,
        app_secret=app_secret,
        public_key=public_key,
        cache_dir=pathlib.Path("."),
        cert_fingerprint=cert_fp,
        request_timeout_seconds=5.0,
        client_meta={"diag": "1"},
    )
    http = build_httpx_client(timeout=5.0, fingerprint=cert_fp)
    token = None
    try:
        token = fetch_session(http, cfg, device_id=None)
        fetch_state = "OK"
        print("fetch_session 成功,拿到 token,长度:", len(token))
    except Exception as exc:
        fetch_state = "FAIL"
        name = type(exc).__name__
        msg = str(exc)
        print(f"fetch_session 失败 → {name}: {msg}")
        if name == "ApcDenied":
            reason = "request-4xx(app_id/app_secret 签名不对,或后端拒这台设备)"
        else:
            reason = "network/TLS(连不上/超时/证书指纹不符)"

    if token is not None:
        print("\n===== ③ 不验签 dump token claims(看后端实际签了啥)=====")
        try:
            claims = pyjwt.decode(token, options={"verify_signature": False})
            now = int(time.time())
            print("sub(应=app_id)  :", claims.get("sub") == app_id, "(相等才对)")
            print(
                "exp - now (秒)   :",
                (claims.get("exp", 0) - now) if claims.get("exp") else "无 exp",
            )
            print("有 did 字段       :", "did" in claims)
            print("claims keys      :", sorted(claims.keys()))
        except Exception as exc:
            print("解码 token 失败:", exc)

        print("\n===== ④ 真验签(复刻客户端 verify_jwt)=====")
        try:
            verify_jwt(token, public_key=public_key, audience=app_id, expected_did=None)
            verify_state = "OK"
            reason = "none(首装验签通过;deny 来自当天缓存,删 session.json 重试即可)"
            print("verify_jwt 通过 ✅")
        except Exception as exc:
            verify_state = "FAIL"
            name = type(exc).__name__
            msg = str(exc).lower()
            print(f"verify_jwt 失败 ❌ {name}: {exc}")
            if "signature" in msg:
                reason = "signature-invalid(打包公钥≠后端签名私钥,换对公钥重打包)"
            elif "subject" in msg or "sub" in msg:
                reason = "subject-mismatch(打包 app_id≠后端签进 sub 的值)"
            elif "expired" in msg:
                reason = "jwt-expired(时钟或后端 exp 太短)"
            elif pem_broken:
                reason = "pem-broken(公钥 PEM 被存坏了,见①)"
            else:
                reason = "jwt-decode-failed(公钥格式坏 或 token 缺 exp/sub)"

    print("\n========================================")
    print(f"VERDICT: fetch={fetch_state} verify={verify_state} reason={reason}")
    print("========================================")


if __name__ == "__main__":
    main()
