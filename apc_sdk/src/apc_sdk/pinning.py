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

    实现路径(httpcore 1.0 公开 API):
      response.extensions["network_stream"]  →  httpcore SyncStream
      .get_extra_info("ssl_object")          →  ssl.SSLObject
      .getpeercert(binary_form=True)         →  DER bytes
    """

    def __init__(self, *, expected_fingerprint: str, **kwargs: object) -> None:
        # 关掉 CA 校验,交由我们的 pin 手动验证
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs.setdefault("verify", ctx)
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._expected = _normalize_fingerprint(expected_fingerprint)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = super().handle_request(request)

        # extensions["network_stream"] 是 httpcore 1.0 的公开扩展字段,
        # 由 HTTP11Connection.handle_request 注入。
        network_stream = response.extensions.get("network_stream")
        if network_stream is None:
            raise ApcNetworkError("无法读取 network_stream,无法做 cert pinning")

        ssl_obj = network_stream.get_extra_info("ssl_object")
        if ssl_obj is None:
            raise ApcNetworkError("无法读取 ssl_object,无法做 cert pinning")

        try:
            # ssl_obj may be the internal _ssl._SSLSocket C type which only
            # accepts positional args — use positional True instead of keyword.
            der = ssl_obj.getpeercert(True)
        except Exception as exc:
            raise ApcNetworkError(f"无法读取 peer cert: {exc}") from exc

        if der is None:
            raise ApcNetworkError("peer cert 为空,无法做 cert pinning")

        actual = hashlib.sha256(der).hexdigest()
        if actual != self._expected:
            raise ApcNetworkError(
                f"cert fingerprint mismatch: got {actual}, expected {self._expected}"
            )
        return response


def build_httpx_client(*, timeout: float, fingerprint: str | None) -> httpx.Client:
    """构造 httpx.Client。

    - fingerprint=None:走默认 CA 校验(Let's Encrypt 等公网场景)
    - fingerprint=非空:关 CA 校验 + 握手后手动比对 sha256(peer DER cert)
    """
    if fingerprint is None:
        return httpx.Client(timeout=timeout)
    transport = _PinnedTransport(expected_fingerprint=fingerprint)
    return httpx.Client(timeout=timeout, transport=transport)
