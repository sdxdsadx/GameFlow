from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import sys
import time
import webbrowser
from ctypes import wintypes
from pathlib import Path

from gameflow.config import (DEFAULT_CONFIG, ConfigError, load_config,
                             validate_runtime_config)
from gameflow.engine import WorkflowManager
from gameflow.runners import RUNNERS
from gameflow.settings import (HostSettings, HostSettingsError, RuntimeSettings,
                               apply_host_settings, apply_workflow_device_endpoint,
                               apply_workflow_device_port,
                               sync_bundled_workflow_endpoint,
                               sync_bundled_tool_profiles)
from gameflow.store import Store
from gameflow.web import serve


ROOT = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent)
STATE_ROOT = (ROOT.parent / "GameFlow.state"
              if getattr(sys, "frozen", False) else ROOT)


def build_identity(root: Path = ROOT) -> dict[str, object]:
    info_path = root / "build_info.json"
    try:
        info = json.loads(info_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        info = {"build_id": "source"}
    elevated = False
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            advapi32 = ctypes.windll.advapi32
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            advapi32.OpenProcessToken.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
            advapi32.OpenProcessToken.restype = wintypes.BOOL
            advapi32.GetTokenInformation.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
            advapi32.GetTokenInformation.restype = wintypes.BOOL
            token = wintypes.HANDLE()
            if advapi32.OpenProcessToken(
                    kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
                value = wintypes.DWORD()
                size = wintypes.DWORD()
                if advapi32.GetTokenInformation(
                    token, 20, ctypes.byref(value), ctypes.sizeof(value),
                    ctypes.byref(size)):
                    elevated = bool(value.value)
                kernel32.CloseHandle(token)
        except (AttributeError, OSError, TypeError, ValueError,
                ctypes.ArgumentError):
            elevated = False
    else:
        elevated = bool(getattr(os, "geteuid", lambda: 0)() == 0)
    return {
        "build_id": str(info.get("build_id", "source")),
        "built_at": str(info.get("built_at", "")),
        "root": str(root.resolve()),
        "executable": str(Path(sys.executable if getattr(sys, "frozen", False)
                               else __file__).resolve()),
        "pid": os.getpid(),
        "elevated": elevated,
    }


def build_engine(config_path: str) -> WorkflowManager:
    config = load_config(config_path)
    log_dir = STATE_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(log_dir / "gameflow.log", encoding="utf-8"),
                                  logging.StreamHandler()])
    default_endpoint = str(config.get("device", {}).get("address", "127.0.0.1:5555"))
    try:
        default_port = int(default_endpoint.rsplit(":", 1)[-1])
    except ValueError:
        default_port = 5555
    settings = RuntimeSettings(STATE_ROOT / "data" / "runtime_settings.json", default_port)
    config["_runtime_settings"] = settings
    for workflow_name in config.get("workflows", {}):
        saved_endpoint = settings.endpoint(workflow_name)
        if saved_endpoint:
            apply_workflow_device_endpoint(config, workflow_name, saved_endpoint)
    host_settings = HostSettings(STATE_ROOT / "data" / "host_settings.json")
    apply_host_settings(config, host_settings.resolve())
    validate_runtime_config(config, RUNNERS)
    sync_bundled_tool_profiles(ROOT, config)
    manager = WorkflowManager(ROOT, config, Store(STATE_ROOT / "data" / "gameflow.db"))
    manager.runtime_settings = settings
    manager.host_settings = host_settings
    manager.build_identity = build_identity()
    return manager


def main() -> int:
    parser = argparse.ArgumentParser(description="GameFlow 游戏每日自动化编排器")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="action")
    web = sub.add_parser("web", help="启动本地控制面板")
    web.add_argument("--no-browser", action="store_true")
    run = sub.add_parser("run", help="运行一个工作流")
    run.add_argument("workflow")
    run.add_argument("--force", action="store_true")
    run.add_argument("--device-port", type=int)
    configure = sub.add_parser("configure-host", help="配置本机模拟器和游戏安装路径")
    configure.add_argument("--ldplayer", help="雷电 LDPlayer9 目录或 ldconsole.exe")
    configure.add_argument("--mumu", help="MuMu 12 shell 目录或 MuMuManager.exe")
    configure.add_argument("--endfield", help="终末地游戏目录或 Endfield.exe")
    sub.add_parser("status", help="显示最近运行记录")
    args = parser.parse_args()
    if args.action == "configure-host":
        settings = HostSettings(STATE_ROOT / "data" / "host_settings.json")
        try:
            if any((args.ldplayer, args.mumu, args.endfield)):
                paths = settings.configure(
                    ldplayer=args.ldplayer, mumu=args.mumu,
                    endfield=args.endfield)
            else:
                paths = settings.resolve()
        except HostSettingsError as exc:
            print(f"本机路径配置错误：{exc}", file=sys.stderr)
            return 2
        if not paths:
            print("未自动发现外部程序，请使用 --ldplayer、--mumu 或 --endfield 配置")
            return 1
        for key, value in paths.items():
            print(f"{key}={value}")
        return 0
    try:
        manager = build_engine(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    if args.action == "run":
        if args.device_port is not None:
            try:
                port, endpoint = apply_workflow_device_port(
                    manager.config, args.workflow, args.device_port)
                workflow_config = manager.config["workflows"][args.workflow]
                first_emulator = next((step for step in workflow_config["steps"]
                                       if step.get("emulator_kind")), {})
                kind = str(first_emulator.get("emulator_kind", "")).casefold()
                instance = first_emulator.get(
                    "emulator_instance", first_emulator.get("instance", 0))
                validate_runtime_config(manager.config, RUNNERS)
                manager.runtime_settings.save_port(port)
                manager.runtime_settings.save_endpoint(
                    args.workflow, kind, instance, endpoint)
                sync_bundled_workflow_endpoint(ROOT, args.workflow, endpoint)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
        ok, message = manager.start(args.workflow, "command", args.force)
        if not ok:
            print(message)
            return 1
        while manager.state()["running"]:
            manager.join(0.5)
        state = manager.engines[args.workflow].state()
        print(state["message"])
        return 0 if state.get("last_status") == "success" else 1
    if args.action == "status":
        for row in manager.store.recent():
            print(row)
        return 0
    # The control surface is intentionally local-only.
    host = "127.0.0.1"
    port = int(manager.config.get("server", {}).get("port", 8765))
    url = f"http://127.0.0.1:{port}/"
    print(f"GameFlow 已启动：{url}  按 Ctrl+C 停止")
    if not getattr(args, "no_browser", False):
        webbrowser.open(url)
    try:
        serve(manager, host, port)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
