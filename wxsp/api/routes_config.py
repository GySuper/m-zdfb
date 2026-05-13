"""Config:只读展示 config.yaml(敏感字段掩码)。

第一版不开放写入 —— 改配置请直接编辑 ./config.yaml 然后重启 `wxsp web`,
避免 Web UI 写入逻辑误删 ${ENV_VAR} 引用或写入 *** 占位符。
M10 部署阶段如需在线编辑,再加 POST + server-side merge 逻辑。
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from wxsp.api.deps import templates

router = APIRouter()

# 敏感行匹配:key 含这些子串时把 value 替换为 ***
_SENSITIVE_KEYS = ("app_secret", "webhook", "secret", "token", "password")
# 命中"  key: value" 形式;value 后保留行尾(可能有注释)
_KEY_VALUE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+?)\s*$")


def _mask_yaml(text: str) -> str:
    """对 key 含敏感词的行,把 value 替为 `***`(${ENV_VAR} 形式保留以便看到结构)。"""
    out: list[str] = []
    for line in text.splitlines():
        m = _KEY_VALUE.match(line)
        if not m:
            out.append(line)
            continue
        indent, key, value = m.group(1), m.group(2), m.group(3)
        if any(s in key.lower() for s in _SENSITIVE_KEYS):
            if value.startswith("${") and value.endswith("}"):
                # ${ENV_VAR} 引用本身不敏感(只是占位),保留
                out.append(line)
            else:
                out.append(f"{indent}{key}: '***'")
        else:
            out.append(line)
    return "\n".join(out)


@router.get("/config", response_class=HTMLResponse)
def config_page(request: Request) -> HTMLResponse:
    config_path = Path("config.yaml")
    if config_path.exists():
        raw = config_path.read_text(encoding="utf-8")
        masked = _mask_yaml(raw)
        exists = True
        abs_path = str(config_path.resolve())
    else:
        masked = "# 找不到 config.yaml —— 请基于 config.example.yaml 复制一份"
        exists = False
        abs_path = str(config_path.resolve())
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "active": "config",
            "yaml_text": masked,
            "abs_path": abs_path,
            "exists": exists,
        },
    )
