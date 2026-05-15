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
            license_jwt = self._fetch_session(
                self._get_http(), self._cfg, device_id=cache.get("device_id")
            )
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
    def _default_fetch_session(
        client: httpx.Client, cfg: ApcConfig, *, device_id: str | None
    ) -> str:
        return fetch_session(client, cfg, device_id=device_id)

    @staticmethod
    def _default_verify_jwt(token: str, **kwargs: Any) -> dict[str, Any]:
        return verify_jwt(token, **kwargs)
