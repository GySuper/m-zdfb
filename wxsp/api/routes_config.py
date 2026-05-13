"""配置:Web UI 编辑 config.yaml。

保存流程:
1. 校验提交的 YAML 语法
2. 把 *** 占位还原成磁盘旧文件里对应字段的原值(用户没改的敏感字段不会被吹掉)
3. 展开 ${ENV_VAR},Pydantic Settings 完整校验
4. 任一步失败:不写盘,回到表单显示用户提交的原始文本 + 红色错误
5. 全部通过:备份 config.yaml.bak,原子写新内容到 config.yaml(注释/格式都保留)
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import yaml
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from wxsp.api.deps import templates
from wxsp.config import Settings, _expand_env_vars

router = APIRouter()

# 行内 key: value 形式;value 可能被单/双引号包围,行尾可能带注释
_KEY_VALUE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+?)\s*$")
_MASKED_LINE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(['\"]?)\*\*\*\3\s*(#.*)?$")

_SENSITIVE_KEYS = ("app_secret", "webhook", "secret", "token", "password")
_CONFIG_PATH = Path("config.yaml")
_BACKUP_PATH = Path("config.yaml.bak")


def _is_sensitive(key: str) -> bool:
    return any(s in key.lower() for s in _SENSITIVE_KEYS)


def _mask_yaml(text: str) -> str:
    """对 key 含敏感词的行,把 value 替为 `***`(${ENV_VAR} 形式保留以便看到结构)。"""
    out: list[str] = []
    for line in text.splitlines():
        m = _KEY_VALUE.match(line)
        if not m:
            out.append(line)
            continue
        indent, key, value = m.group(1), m.group(2), m.group(3)
        if _is_sensitive(key):
            if value.startswith("${") and value.endswith("}"):
                # ${ENV_VAR} 引用本身不敏感(只是占位),保留
                out.append(line)
            else:
                out.append(f"{indent}{key}: '***'")
        else:
            out.append(line)
    return "\n".join(out)


def _restore_masked(new_text: str, old_text: str) -> str:
    """new_text 里所有 *** 占位,从 old_text 同 (缩进,key) 位置取原值还原。

    行级操作:保留用户的注释 / 顺序 / 空行;不经过 yaml load+dump。
    """
    old_lines = old_text.splitlines()
    out: list[str] = []
    for line in new_text.splitlines():
        m = _MASKED_LINE.match(line)
        if not m or not _is_sensitive(m.group(2)):
            out.append(line)
            continue
        indent, key, _, tail_comment = m.groups()
        restored: str | None = None
        # 旧文件中第一个 (indent, key) 匹配项
        key_re = re.compile(rf"^{re.escape(indent)}{re.escape(key)}\s*:\s*(.+?)\s*$")
        for ol in old_lines:
            om = key_re.match(ol)
            if om:
                restored = om.group(1)
                break
        if restored is None:
            # 旧文件里没有 —— 用户在 textarea 里新增了敏感字段但留着 ***,校验阶段会报
            out.append(line)
            continue
        suffix = f"  {tail_comment}" if tail_comment else ""
        out.append(f"{indent}{key}: {restored}{suffix}")
    trailing = "\n" if new_text.endswith("\n") else ""
    return "\n".join(out) + trailing


def _validate(text: str) -> list[str]:
    """返回错误列表;空列表 = 校验通过。"""
    errors: list[str] = []
    # 1. YAML 语法
    try:
        yaml.safe_load(text)
    except yaml.YAMLError as exc:
        errors.append(f"YAML 语法错误:{exc}")
        return errors
    # 2. ${ENV_VAR} 展开
    try:
        expanded = _expand_env_vars(text)
    except ValueError as exc:
        errors.append(str(exc))
        return errors
    # 3. Pydantic
    try:
        data = yaml.safe_load(expanded)
        Settings.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()))
            errors.append(f"{loc}: {err.get('msg', '')}")
    except (yaml.YAMLError, TypeError, ValueError) as exc:
        errors.append(str(exc))
    return errors


def _atomic_write(path: Path, text: str) -> None:
    """先写 .tmp 再 os.replace,避免写到一半进程被杀导致配置文件残缺。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@router.get("/config", response_class=HTMLResponse)
def config_page(
    request: Request,
    flash: str | None = None,
) -> HTMLResponse:
    if _CONFIG_PATH.exists():
        raw = _CONFIG_PATH.read_text(encoding="utf-8")
        masked = _mask_yaml(raw)
        exists = True
    else:
        masked = "# 找不到 config.yaml —— 请基于 config.example.yaml 复制一份"
        exists = False
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "active": "config",
            "yaml_text": masked,
            "abs_path": str(_CONFIG_PATH.resolve()),
            "exists": exists,
            "flash": flash,
            "errors": [],
        },
    )


@router.post("/config", response_class=HTMLResponse)
def config_save(
    request: Request,
    yaml_text: str = Form(...),
) -> HTMLResponse:
    if not _CONFIG_PATH.exists():
        return templates.TemplateResponse(
            request,
            "config.html",
            {
                "active": "config",
                "yaml_text": yaml_text,
                "abs_path": str(_CONFIG_PATH.resolve()),
                "exists": False,
                "flash": None,
                "errors": ["config.yaml 不存在,请先在磁盘上创建一份再来这里编辑"],
            },
            status_code=400,
        )

    old_text = _CONFIG_PATH.read_text(encoding="utf-8")
    restored = _restore_masked(yaml_text, old_text)
    errors = _validate(restored)

    if errors:
        return templates.TemplateResponse(
            request,
            "config.html",
            {
                "active": "config",
                "yaml_text": yaml_text,  # 保留用户提交的原始内容(含 ***)便于继续编辑
                "abs_path": str(_CONFIG_PATH.resolve()),
                "exists": True,
                "flash": None,
                "errors": errors,
            },
            status_code=400,
        )

    # 备份后原子写入。备份单文件(每次保存覆盖),够用。
    shutil.copy2(_CONFIG_PATH, _BACKUP_PATH)
    _atomic_write(_CONFIG_PATH, restored)

    masked = _mask_yaml(restored)
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "active": "config",
            "yaml_text": masked,
            "abs_path": str(_CONFIG_PATH.resolve()),
            "exists": True,
            "flash": f"已保存,旧版本备份在 {_BACKUP_PATH}",
            "errors": [],
        },
    )
