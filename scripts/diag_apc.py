"""APC 凭据/验签一次性诊断 —— 排查"打包版被 APC 拦(伪装成上传超时)"。

两种凭据来源,自动选:
  - 环境变量 APC_*(CI 诊断用,读 GitHub secrets);
  - 都没设时,读打包烤进 wxsp/apc_config.py 的值(下载的 exe 在目标机上跑走这条,
    1:1 复刻真 app 的凭据 + 验签)。

会真实调一次后端 session/init(device_id=None,模拟首装),后台可能多出一条临时设备,
跑完手动清理即可。输出同时写到 exe 旁边的 apc_diag_output.txt。

末尾 `VERDICT:` 一行是结论;`④ 真验签` 里的报错原文就是那个被客户端吞掉的 deny 真因。
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import sys
import time

import jwt as pyjwt

from apc_sdk._http import fetch_session
from apc_sdk._types import ApcConfig
from apc_sdk.crypto import verify_jwt
from apc_sdk.pinning import build_httpx_client


class _Tee:
    """同时往多个流写(控制台 + 文件),任一失败不影响其它。"""

    def __init__(self, *streams: object) -> None:
        self._streams = streams

    def write(self, s: str) -> None:
        for st in self._streams:
            try:
                st.write(s)
            except Exception:
                pass

    def flush(self) -> None:
        for st in self._streams:
            try:
                st.flush()
            except Exception:
                pass


def _load_creds() -> tuple[str, str, str, str, str | None, str]:
    """ENV 优先(CI);env 缺则读打包烤入的 apc_config(目标机上的下载 exe 走这条)。"""
    if os.environ.get("APC_ENDPOINT"):
        return (
            os.environ["APC_ENDPOINT"],
            os.environ["APC_APP_ID"],
            os.environ["APC_APP_SECRET"],
            os.environ["APC_PUBLIC_KEY"],
            os.environ.get("APC_CERT_FP") or None,
            "env(环境变量 / GitHub secrets)",
        )
    from wxsp.apc_config import (
        APC_APP_ID,
        APC_APP_SECRET,
        APC_CERT_FP,
        APC_ENDPOINT,
        APC_PUBLIC_KEY,
    )

    return (
        APC_ENDPOINT,
        APC_APP_ID,
        APC_APP_SECRET,
        APC_PUBLIC_KEY,
        APC_CERT_FP or None,
        "baked(打包烤入 apc_config.py)",
    )


def _run(endpoint: str, app_id: str, app_secret: str, public_key: str, cert_fp: str | None) -> None:
    fetch_state = "?"
    verify_state = "?"
    reason = "?"

    print("===== ① 凭据自检(密钥值会被 CI 打码,看判断词即可)=====")
    print("public_key 长度  :", len(public_key))
    fp = hashlib.sha256(public_key.encode("utf-8")).hexdigest()[:12]
    print("public_key 指纹  :", fp, " <- 应与 CI 那次的 da6d1638dcb8 一致")
    has_real_newline = "\n" in public_key
    has_literal_bsn = "\\n" in public_key
    looks_pem = public_key.lstrip().startswith("-----BEGIN")
    print("PEM 头正确(应 True)        :", looks_pem)
    print("含真实换行(应 True)        :", has_real_newline)
    print("含字面 \\n 两字符(应 False) :", has_literal_bsn)
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
        print(f"fetch_session 失败 → {name}: {exc}")
        reason = (
            "request-4xx(app_id/app_secret 不对,或后端拒)"
            if name == "ApcDenied"
            else "network/TLS(连不上/超时/证书指纹不符)"
        )

    if token is not None:
        print("\n===== ③ 不验签 dump token claims(看后端实际签了啥)=====")
        try:
            claims = pyjwt.decode(token, options={"verify_signature": False})
            now = int(time.time())
            print("sub == app_id    :", claims.get("sub") == app_id)
            print("exp - now (秒)   :", (claims.get("exp", 0) - now) if claims.get("exp") else "无")
            print("iat - now (秒)   :", (claims.get("iat", 0) - now) if claims.get("iat") else "无")
            print("claims keys      :", sorted(claims.keys()))
        except Exception as exc:
            print("解码 token 失败:", exc)

        print("\n===== ④ 真验签(复刻客户端 verify_jwt)=====")
        try:
            verify_jwt(token, public_key=public_key, audience=app_id, expected_did=None)
            verify_state = "OK"
            reason = "none(验签通过)"
            print("verify_jwt 通过 ✅")
        except Exception as exc:
            verify_state = "FAIL"
            name = type(exc).__name__
            msg = str(exc).lower()
            print(f"verify_jwt 失败 ❌ {name}: {exc}")
            if "signature" in msg:
                reason = "signature-invalid(公钥≠后端签名私钥)"
            elif "subject" in msg or "sub" in msg:
                reason = "subject-mismatch(app_id≠sub)"
            elif "expired" in msg:
                reason = "jwt-expired(时钟/exp)"
            elif pem_broken:
                reason = "pem-broken(公钥格式坏,见①)"
            else:
                reason = "jwt-decode-failed(公钥坏 或 缺 exp/sub)"

    print("\n========================================")
    print(f"VERDICT: fetch={fetch_state} verify={verify_state} reason={reason}")
    print("========================================")


def main() -> None:
    # 冻结 exe 的 stdout 默认 cp1252,打印中文会 UnicodeEncodeError 崩。强制 UTF-8。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass

    # 输出同时落一份文件,方便目标机上把结果发回来。
    base = (
        pathlib.Path(sys.executable).parent if getattr(sys, "frozen", False) else pathlib.Path.cwd()
    )
    logf = None
    try:
        logf = open(base / "apc_diag_output.txt", "w", encoding="utf-8")
        sys.stdout = _Tee(sys.stdout, logf)  # type: ignore[assignment]
    except Exception:
        pass

    try:
        endpoint, app_id, app_secret, public_key, cert_fp, src = _load_creds()
        print("凭据来源:", src)
        print("结果同时写到:", str(base / "apc_diag_output.txt"))
        print()
        _run(endpoint, app_id, app_secret, public_key, cert_fp)
    finally:
        if logf is not None:
            try:
                logf.flush()
                logf.close()
            except Exception:
                pass

    # 双击运行时别闪退;CI / 管道里 isatty()=False 自动跳过,不会卡住。
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input("\n按回车键关闭本窗口...")
    except Exception:
        pass


if __name__ == "__main__":
    main()
