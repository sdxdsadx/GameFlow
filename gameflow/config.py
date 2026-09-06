from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent.parent)
DEFAULT_CONFIG = ROOT / "config" / "workflow.json"


class ConfigError(ValueError):
    pass


def expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def runtime_state_root(root: Path = ROOT) -> Path:
    """Keep mutable state beside, rather than inside, a frozen release."""
    return root.parent / "GameFlow.state" if getattr(sys, "frozen", False) else root


def resolve_project_paths(value: Any, root: Path = ROOT,
                          state_root: Path | None = None) -> Any:
    """Expand the immutable release-root and mutable state-root tokens."""
    state_root = state_root or runtime_state_root(root)
    if isinstance(value, dict):
        return {key: resolve_project_paths(child, root, state_root)
                for key, child in value.items()}
    if isinstance(value, list):
        return [resolve_project_paths(child, root, state_root) for child in value]
    if isinstance(value, str):
        return (value.replace("${GAMEFLOW_ROOT}", str(root))
                .replace("${GAMEFLOW_STATE}", str(state_root)))
    return value


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
    root = config_path.resolve().parent.parent
    state_root = runtime_state_root(root)
    data = resolve_project_paths(data, root, state_root)
    data["_path"] = str(config_path.resolve())
    data["_state_root"] = str(state_root.resolve())
    return data


RUNNER_ACTIONS = {
    "adb": {"wait", "screenshot", "start_app", "tap"},
    "ldplayer": {"launch", "quit", "reboot"},
    "mumu_wait": {"wait"},
    "mumu_stop": {"stop"},
}


def validate_runtime_config(config: dict[str, Any], runner_names) -> None:
    """Reject executable workflow mistakes before any process is started."""
    available = set(runner_names)
    endpoint_owners: dict[str, tuple[str, str]] = {}
    errors: list[str] = []
    executable_runners = {
        "maa_gui", "baas_gui", "alas_gui", "gumballs_gui", "maaend_gui",
        "log_gui_daily", "qq_reader_trial",
    }
    for workflow_name, workflow in config.get("workflows", {}).items():
        group = str(workflow.get("exclusive_group", "")).strip()
        for step in workflow.get("steps", []):
            prefix = f"{workflow_name}.{step.get('id', '?')}"
            runner = str(step.get("runner", ""))
            if runner not in available:
                errors.append(f"{prefix} 使用未注册 runner：{runner}")
                continue
            action = str(step.get("action", "")).strip()
            allowed = RUNNER_ACTIONS.get(runner)
            if allowed and action and action not in allowed:
                errors.append(f"{prefix} 的 action={action} 不受 {runner} 支持")
            if runner in executable_runners and not str(step.get("executable", "")).strip():
                errors.append(f"{prefix} 缺少 executable")
            if runner in {"ldplayer", "mumu_wait", "mumu_stop"}:
                if step.get("instance", step.get("mumu_instance")) is None:
                    errors.append(f"{prefix} 缺少模拟器 instance")
            if runner == "adb" and not str(step.get("device", "")).strip():
                errors.append(f"{prefix} 缺少 device")
            if runner == "process_stop" and not str(step.get("image", "")).strip():
                errors.append(f"{prefix} 缺少 image")
            device = str(step.get("device", "")).strip()
            if not device or runner not in {"adb", "mumu_wait"}:
                continue
            previous = endpoint_owners.get(device)
            if previous and previous[0] != workflow_name:
                previous_workflow, previous_group = previous
                if not group or group != previous_group:
                    errors.append(
                        f"ADB 端点 {device} 同时分配给非互斥工作流 "
                        f"{previous_workflow} 与 {workflow_name}")
            else:
                endpoint_owners[device] = (workflow_name, group)
    if errors:
        raise ConfigError("运行配置预检失败：" + "；".join(errors))
