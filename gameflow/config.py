from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "workflow.json"


class ConfigError(ValueError):
    pass


def expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def load_config(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    config_path = Path(path)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在：{config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件格式错误（第 {exc.lineno} 行）：{exc.msg}") from exc
    if not isinstance(data.get("workflows"), dict) or not data["workflows"]:
        raise ConfigError("配置必须包含非空的 workflows 对象")
    for name, workflow in data["workflows"].items():
        if not isinstance(workflow.get("steps"), list):
            raise ConfigError(f"工作流 {name} 缺少 steps 列表")
        ids: set[str] = set()
        for index, step in enumerate(workflow["steps"], 1):
            if not isinstance(step, dict) or not step.get("id") or not step.get("runner"):
                raise ConfigError(f"工作流 {name} 的第 {index} 步缺少 id 或 runner")
            if step["id"] in ids:
                raise ConfigError(f"工作流 {name} 存在重复步骤 id：{step['id']}")
            ids.add(step["id"])
    data["_path"] = str(config_path.resolve())
    return data
