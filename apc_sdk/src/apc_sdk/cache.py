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
