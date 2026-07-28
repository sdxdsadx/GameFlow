from __future__ import annotations

import glob
import json
import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import expand


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)
    return cleaned.strip("._") or "unknown"


def _run(args: list[str], *, timeout: float = 8,
         env: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        done = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        output = done.stdout.decode("utf-8", errors="replace")
        if "�" in output:
            try:
                output = done.stdout.decode("gb18030")
            except UnicodeDecodeError:
                pass
        return {"command": subprocess.list2cmdline(args), "exit_code": done.returncode,
                "output": output[-20000:]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": subprocess.list2cmdline(args), "error": str(exc)}


def _workflow_devices(config: dict[str, Any], workflow: dict[str, Any],
                      failed_step: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    steps = [failed_step, *workflow.get("steps", [])]
    for item in steps:
        device = str(item.get("device", "")).strip()
        if not device:
            continue
        adb = str(item.get("adb_executable") or
                  (item.get("executable") if item.get("runner") == "adb" else "") or
                  config.get("tools", {}).get("adb", "adb"))
        port = item.get("adb_server_port")
        # One healthy ADB client per device/server is enough.  The failed step is
        # inspected first, so its vendor-specific executable wins over fallbacks.
        key = (device, str(port or ""))
        if any(entry["key"] == key for entry in candidates):
            continue
        candidates.append({"key": key, "device": device, "adb": expand(adb),
                           "server_port": port})
    return candidates


def _capture_adb_device(folder: Path, entry: dict[str, Any], index: int) -> dict[str, Any]:
    adb = entry["adb"]
    device = entry["device"]
    env = os.environ.copy()
    if entry.get("server_port"):
        env["ANDROID_ADB_SERVER_PORT"] = str(entry["server_port"])
    prefix = [adb, "-s", device]
    state: dict[str, Any] = {
        "device": device, "adb": adb, "server_port": entry.get("server_port"),
        "devices": _run([adb, "devices", "-l"], env=env),
        "get_state": _run(prefix + ["get-state"], env=env),
        "boot_completed": _run(prefix + ["shell", "getprop", "sys.boot_completed"], env=env),
    }
    window = _run(prefix + ["shell", "dumpsys", "window", "windows"], env=env)
    activity = _run(prefix + ["shell", "dumpsys", "activity", "activities"], env=env)
    focus_terms = ("mCurrentFocus", "mFocusedApp", "topResumedActivity",
                   "mResumedActivity", "ResumedActivity")
    for name, result in (("window_focus", window), ("activity_focus", activity)):
        output = str(result.get("output", ""))
        result["output"] = "\n".join(
            line.strip() for line in output.splitlines()
            if any(term in line for term in focus_terms)
        )[-8000:]
        state[name] = result
    screenshot_path = folder / f"emulator_{index}_{_safe_name(device)}.png"
    try:
        shot = subprocess.run(
            prefix + ["exec-out", "screencap", "-p"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=20, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if shot.returncode == 0 and shot.stdout.startswith(b"\x89PNG"):
            screenshot_path.write_bytes(shot.stdout)
            state["screenshot"] = str(screenshot_path)
        else:
            state["screenshot_error"] = shot.stderr.decode("utf-8", errors="replace")[-2000:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        state["screenshot_error"] = str(exc)
    return state


def _configured_process_names(workflow: dict[str, Any]) -> set[str]:
    names = {"dnplayer.exe", "LdVBoxHeadless.exe", "MuMuPlayer.exe",
             "MuMuVMMHeadless.exe", "adb.exe"}
    for step in workflow.get("steps", []):
        for key in ("executable", "command", "process_image", "game_process_image"):
            value = str(step.get(key, "")).strip()
            if value:
                names.add(Path(expand(value)).name)
        for key in ("process_images", "cleanup_process_images"):
            names.update(Path(str(value)).name for value in step.get(key, []) if value)
    return {name.casefold() for name in names if name}


def _process_and_window_state(workflow: dict[str, Any]) -> dict[str, Any]:
    wanted = _configured_process_names(workflow)
    processes: list[dict[str, Any]] = []
    relevant_pids: set[int] = set()
    try:
        import psutil
        for process in psutil.process_iter(
                ["pid", "name", "exe", "cmdline", "status", "create_time", "memory_info"]):
            try:
                name = str(process.info.get("name") or "")
                if name.casefold() not in wanted:
                    continue
                relevant_pids.add(int(process.info["pid"]))
                memory = process.info.get("memory_info")
                processes.append({
                    "pid": process.info["pid"], "name": name,
                    "exe": process.info.get("exe"), "cmdline": process.info.get("cmdline"),
                    "status": process.info.get("status"),
                    "create_time": process.info.get("create_time"),
                    "rss": getattr(memory, "rss", None),
                })
            except (psutil.Error, OSError):
                continue
    except ImportError:
        pass
    windows: list[dict[str, Any]] = []
    foreground: dict[str, Any] | None = None
    if os.name == "nt":
        try:
            import win32gui
            import win32process
            foreground_hwnd = win32gui.GetForegroundWindow()

            def collect(hwnd, _):
                nonlocal foreground
                if not win32gui.IsWindowVisible(hwnd):
                    return
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                title = win32gui.GetWindowText(hwnd).strip()
                if pid in relevant_pids:
                    windows.append({"pid": pid, "hwnd": int(hwnd), "title": title,
                                    "rect": list(win32gui.GetWindowRect(hwnd))})
                if hwnd == foreground_hwnd:
                    foreground = {"pid": pid, "hwnd": int(hwnd), "title": title}

            win32gui.EnumWindows(collect, None)
        except (ImportError, OSError):
            pass
    return {"processes": processes, "windows": windows, "foreground": foreground,
            "expected_process_names": sorted(wanted)}


def _log_patterns(workflow: dict[str, Any]) -> list[str]:
    patterns: list[str] = []
    for step in workflow.get("steps", []):
        for key in ("log_path", "gui_log_path", "framework_log_path", "log_glob"):
            value = str(step.get(key, "")).strip()
            if value and value not in patterns:
                patterns.append(expand(value))
    return patterns


def _save_log_tails(folder: Path, workflow: dict[str, Any]) -> list[str]:
    files: list[Path] = []
    for pattern in _log_patterns(workflow):
        matches = glob.glob(pattern)
        if not matches and Path(pattern).is_file():
            matches = [pattern]
        files.extend(Path(name) for name in matches if Path(name).is_file())
    # ALAS stores its strongest failure evidence below log/error rather than in
    # the configured text glob.  Keep the newest screenshot beside our report.
    for step in workflow.get("steps", []):
        exe = Path(expand(str(step.get("executable", ""))))
        if step.get("runner") == "alas_gui" and exe.parent.exists():
            error_root = exe.parent / "log" / "error"
            error_files = sorted(
                (path for path in error_root.rglob("*") if path.is_file()),
                key=lambda path: path.stat().st_mtime, reverse=True,
            )[:3] if error_root.exists() else []
            for index, source in enumerate(error_files, 1):
                target = folder / f"script_error_{index}_{_safe_name(source.name)}"
                try:
                    target.write_bytes(source.read_bytes())
                except OSError:
                    pass
    selected = sorted(set(files), key=lambda path: path.stat().st_mtime, reverse=True)[:8]
    sections: list[str] = []
    for path in selected:
        try:
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 200_000))
                data = handle.read()
            text = data.decode("utf-8", errors="replace")
            sections.append(f"\n===== {path} =====\n{text[-120000:]}")
        except OSError:
            continue
    if sections:
        (folder / "script_logs_tail.txt").write_text("".join(sections), encoding="utf-8")
    return [str(path) for path in selected]


def collect_failure_diagnostics(root: Path, config: dict[str, Any], workflow_id: str,
                                workflow: dict[str, Any], step: dict[str, Any],
                                attempt: int, message: str,
                                details: dict[str, Any]) -> dict[str, Any]:
    """Persist emulator, script, process and log state before cleanup/retry."""
    stamp = datetime.now().astimezone()
    folder = (root / "logs" / "error_diagnostics" / stamp.strftime("%Y-%m-%d") /
              f"{stamp.strftime('%H%M%S')}_{_safe_name(workflow_id)}_"
              f"{_safe_name(str(step.get('id', 'step')))}_attempt{attempt}")
    folder.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "captured_at": stamp.isoformat(), "workflow": workflow_id,
        "workflow_name": workflow.get("display_name"), "step": step.get("id"),
        "runner": step.get("runner"), "attempt": attempt, "message": message,
        "result_details": details,
        "process_state": _process_and_window_state(workflow),
    }
    devices = _workflow_devices(config, workflow, step)
    report["devices"] = [
        _capture_adb_device(folder, entry, index)
        for index, entry in enumerate(devices, 1)
    ]
    report["log_files"] = _save_log_tails(folder, workflow)

    # Capture any still-visible script/game windows.  Runners that already closed
    # their GUI remain represented in process_state and script_logs_tail.txt.
    window_images: list[str] = []
    try:
        from .runners import RunContext, run_window_screenshot
        context = RunContext(root, config, lambda _: None, threading.Event())
        images = []
        for item in workflow.get("steps", []):
            # Only explicit GUI process fields are safe here.  Treating every
            # executable as a window owner can accidentally target python.exe,
            # adb.exe or a command-line helper and make failure handling slow.
            for key in ("process_image", "game_process_image"):
                value = str(item.get(key, "")).strip()
                if value:
                    images.append(Path(expand(value)).name)
        for image in list(dict.fromkeys(images))[:6]:
            path = folder / f"window_{_safe_name(image)}.png"
            result = run_window_screenshot(
                {"process_image": image, "path": str(path), "wait_seconds": 0}, context)
            if result.success and path.exists():
                window_images.append(str(path))
    except Exception as exc:  # diagnostics must never mask the original failure
        report["window_capture_error"] = str(exc)
    report["window_images"] = window_images

    if any(item.get("runner") == "ldplayer" for item in workflow.get("steps", [])):
        report["ldplayer_list"] = _run([expand(str(config.get("tools", {}).get(
            "ldconsole", "ldconsole"))), "list2"])
    for item in workflow.get("steps", []):
        if item.get("runner") == "mumu_wait" and item.get("executable"):
            report["mumu_info"] = _run([
                expand(str(item["executable"])), "info", "--vmindex",
                str(item.get("instance", 0)),
            ])
            break
    report_path = folder / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                           encoding="utf-8")
    return {"path": str(folder), "report": str(report_path),
            "emulator_screenshots": [item.get("screenshot") for item in report["devices"]
                                     if item.get("screenshot")],
            "window_images": window_images}
