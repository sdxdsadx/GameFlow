from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


DRIVE_PATH = re.compile(r"(?i)(?:^|[\"'])\s*[a-z]:\\")
NETWORK_URL = re.compile(r"(?i)https?://(?!127\.0\.0\.1(?::\d+)?(?:/|$))")
FORBIDDEN_CONFIG_KEYS = ("update", "announcement", "promotion", "email", "smtp")


def walk(value, path="root"):
    if isinstance(value, dict):
        for key, child in value.items():
            if any(part in key.casefold() for part in FORBIDDEN_CONFIG_KEYS):
                yield f"禁止的配置键：{path}.{key}"
            yield from walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, f"{path}[{index}]")
    elif isinstance(value, str):
        if DRIVE_PATH.search(value):
            yield f"绝对路径：{path}={value}"
        if NETWORK_URL.search(value):
            yield f"外部 URL：{path}={value}"


def walk_runtime_values(value, root: Path, path="root"):
    """Audit active vendor profiles without rejecting disabled schema fields."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk_runtime_values(child, root, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_runtime_values(child, root, f"{path}[{index}]")
    elif isinstance(value, str):
        if NETWORK_URL.search(value):
            yield f"活动工具配置含外部 URL：{path}={value}"
        if DRIVE_PATH.search(value):
            try:
                candidate = Path(value).resolve()
                candidate.relative_to(root)
            except (OSError, ValueError):
                yield f"活动工具配置含构建机路径：{path}={value}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    root = Path(args.root).resolve()
    config_path = root / "config" / "workflow.json"
    if not config_path.is_file():
        print(f"缺少配置：{config_path}")
        return 1
    config = json.loads(config_path.read_text(encoding="utf-8"))
    errors = list(walk(config))
    required = {
        "adb": root / "resources" / "runtime" / "adb.exe",
        "maa": root / "resources" / "tools" / "maa" / "MAA.exe",
        "baas": root / "resources" / "tools" / "baas" / "baas.exe",
        "alas": root / "resources" / "tools" / "alas" / "Alas.exe",
        "naruto": root / "resources" / "tools" / "naruto" / "MFAAvalonia.exe",
        "gumballs": root / "resources" / "tools" / "gumballs" / "MFAAvalonia.exe",
        "endfield": root / "resources" / "tools" / "endfield" / "MaaEnd.exe",
        "zenless": root / "resources" / "tools" / "zenless" / "OneDragon-Launcher.exe",
        "star_rail": root / "resources" / "tools" / "star_rail" / "March7th Launcher.exe",
        "qq_reader": root / "resources" / "tools" / "qq_reader" / "MaaQQReaderGUI.exe",
    }
    errors.extend(f"缺少内置入口：{name}={path}" for name, path in required.items()
                  if not path.is_file())
    active_json = (
        "resources/tools/maa/config/gui.new.json",
        "resources/tools/baas/_internal/web/static/baas.json",
        "resources/tools/naruto/config/instances/default.json",
        "resources/tools/gumballs/config/instances/default.json",
        "resources/tools/alas/config/alas.json",
        "resources/tools/qq_reader/dev/config/maa_pi_config.json",
        "resources/tools/endfield/config/mxu-MaaEnd.json",
    )
    for relative in active_json:
        path = root / relative
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8-sig"))
                errors.extend(walk_runtime_values(value, root, relative))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"活动工具配置无法读取：{relative}: {exc}")
    active_yaml = (
        "resources/tools/alas/config/deploy.yaml",
        "resources/tools/star_rail/config.yaml",
        "resources/tools/zenless/config/project.yml",
        "resources/tools/zenless/config/env.yml",
        "resources/tools/zenless/config/repository.yml",
    )
    for relative in active_yaml:
        path = root / relative
        if not path.is_file():
            continue
        lines = (line.split("#", 1)[0] for line in
                 path.read_text(encoding="utf-8-sig", errors="replace").splitlines())
        active_text = "\n".join(lines)
        if DRIVE_PATH.search(active_text):
            errors.append(f"活动工具配置含构建机路径：{relative}")
        if NETWORK_URL.search(active_text):
            errors.append(f"活动工具配置含外部 URL：{relative}")
    for relative in ("main.py", "gameflow/web.py", "launch_admin.ps1"):
        path = root / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if DRIVE_PATH.search(text):
            errors.append(f"源码含盘符绝对路径：{relative}")
        local_only_text = text.replace("http://127.0.0.1:", "")
        # The restored legacy dashboard intentionally loads its character-card
        # artwork from the original public image pool.
        if relative != "gameflow/web.py" and NETWORK_URL.search(local_only_text):
            errors.append(f"源码含非本机 URL：{relative}")
    if errors:
        print("\n".join(errors))
        return 1
    print(f"便携审计通过：{root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
