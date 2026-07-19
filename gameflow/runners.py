from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import expand


@dataclass
class Result:
    success: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    status: str | None = None


DEFAULT_UPDATE_MARKERS = (
    "有更新=true",
    '"has_update":true',
    '"is_compatible":false',
    "需要更新后继续",
    "请更新客户端",
    "客户端版本过低",
    "资源版本过低",
    "new version available",
    "update required",
)


def _find_update_marker(text: str, step: dict[str, Any]) -> str | None:
    """Return an explicit update-required marker without matching harmless update checks."""
    markers = [str(x) for x in step.get("update_markers", DEFAULT_UPDATE_MARKERS)]
    folded = text.casefold()
    return next((marker for marker in markers if marker.casefold() in folded), None)


def _needs_update(product: str, marker: str, **details: Any) -> Result:
    return Result(False, f"{product}需要更新：检测到 {marker}",
                  {"marker": marker, **details}, "needs_update")


def _decode_process_output(data: bytes) -> str:
    """Decode output from UTF-8 tools and Chinese Windows console programs."""
    if not data:
        return ""
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class RunContext:
    def __init__(self, root: Path, config: dict[str, Any], log: Callable[[str], None], stop_event):
        self.root, self.config, self.log, self.stop_event = root, config, log, stop_event

    def tool(self, name: str) -> str:
        value = str(self.config.get("tools", {}).get(name, "")).strip()
        if value and Path(expand(value)).exists():
            return expand(value)
        patterns = self.config.get("discovery", {}).get(name, [])
        for pattern in patterns:
            matches = glob.glob(expand(pattern))
            if matches:
                return matches[0]
        return expand(value) if value else name

    def command(self, args: list[str], timeout: int, cwd: str | None = None,
                env: dict[str, str] | None = None) -> Result:
        shown = subprocess.list2cmdline(args)
        self.log(f"执行：{shown}")
        try:
            proc = subprocess.Popen(args, cwd=cwd or str(self.root), env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except FileNotFoundError:
            return Result(False, f"找不到程序：{args[0]}")
        started = time.monotonic()
        interrupted_message = None
        while proc.poll() is None:
            if self.stop_event.is_set():
                interrupted_message = "任务被用户停止"
                break
            if time.monotonic() - started > timeout:
                interrupted_message = f"执行超时（{timeout} 秒）"
                break
            time.sleep(0.2)
        if interrupted_message:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    proc.kill()
        try:
            output_bytes, _ = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                output_bytes, _ = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                output_bytes = b""
        output = _decode_process_output(output_bytes).strip()
        if output:
            self.log(output[-4000:])
        if interrupted_message:
            return Result(False, interrupted_message,
                          {"exit_code": proc.returncode, "output": output[-4000:]})
        return Result(proc.returncode == 0, "完成" if proc.returncode == 0 else f"退出码 {proc.returncode}",
                      {"exit_code": proc.returncode, "output": output[-4000:]})


def run_command(step: dict[str, Any], ctx: RunContext) -> Result:
    command = expand(str(step.get("command", "")))
    if not command:
        return Result(False, "command runner 缺少 command")
    args = [command] + [expand(str(x)) for x in step.get("args", [])]
    env = os.environ.copy()
    env.update({str(k): expand(str(v)) for k, v in step.get("env", {}).items()})
    result = ctx.command(args, int(step.get("timeout", 3600)), step.get("cwd"), env)
    accepted_codes = {int(code) for code in step.get("success_exit_codes", [0])}
    exit_code = result.details.get("exit_code")
    if exit_code in accepted_codes and not result.success:
        result = Result(True, f"完成（退出码 {exit_code} 已按配置接受）", result.details)
    update_marker = _find_update_marker(str(result.details.get("output", "")), step)
    return (_needs_update(Path(command).name, update_marker,
                          output=result.details.get("output", ""))
            if update_marker else result)


def run_maa(step: dict[str, Any], ctx: RunContext) -> Result:
    exe = ctx.tool("maa")
    task = str(step.get("task", "daily"))
    args = [exe, "run", task]
    if step.get("profile"):
        args += ["--profile", str(step["profile"])]
    return ctx.command(args, int(step.get("timeout", 3600)))


def run_maa_gui(step: dict[str, Any], ctx: RunContext) -> Result:
    """Start MAA GUI and wait for its authoritative completion marker."""
    exe = Path(expand(str(step.get("executable") or ctx.tool("maa_gui"))))
    if not exe.exists():
        return Result(False, f"找不到 MAA GUI：{exe}")
    log_path = Path(expand(str(step.get("log_path") or exe.parent / "debug" / "asst.log")))
    gui_log_path = Path(expand(str(step.get("gui_log_path") or exe.parent / "debug" / "gui.log")))
    marker = str(step.get("completion_marker", "AllTasksCompleted"))
    error_markers = [str(x) for x in step.get("error_markers", ["TaskChainError", "AllTasksError"])]
    start_size = log_path.stat().st_size if log_path.exists() else 0
    gui_position = gui_log_path.stat().st_size if gui_log_path.exists() else 0
    try:
        proc = subprocess.Popen([str(exe)], cwd=str(exe.parent),
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError as exc:
        return Result(False, f"无法启动 MAA：{exc}")
    timeout = int(step.get("timeout", 3600))
    started = time.monotonic()
    ctx.log(f"MAA GUI 已启动，等待完成标记：{marker}")
    position = start_size
    tail = ""
    exit_seen_at = None
    successor_seen = False
    update_seen = False
    restart_grace = float(step.get("restart_grace_seconds", 120))
    try:
        while time.monotonic() - started <= timeout:
            if ctx.stop_event.wait(1):
                return Result(False, "任务被用户停止")
            if log_path.exists():
                size = log_path.stat().st_size
                if size < position:
                    position = 0
                if size > position:
                    with log_path.open("rb") as handle:
                        handle.seek(position)
                        chunk = handle.read()
                    position = size
                    text_chunk = chunk.decode("utf-8", errors="replace")
                    tail = (tail + text_chunk)[-12000:]
                    update_marker = _find_update_marker(text_chunk, step)
                    if update_marker:
                        return _needs_update("MAA 或明日方舟", update_marker,
                                             log=str(log_path))
                    if marker in tail:
                        return Result(True, "MAA 日常任务全部完成", {"marker": marker})
                    for error in error_markers:
                        if error in tail:
                            return Result(False, f"MAA 报告任务错误：{error}", {"marker": error})
            if gui_log_path.exists():
                gui_size = gui_log_path.stat().st_size
                if gui_size < gui_position:
                    gui_position = 0
                if gui_size > gui_position:
                    with gui_log_path.open("rb") as handle:
                        handle.seek(gui_position)
                        gui_chunk = handle.read().decode("utf-8", errors="replace")
                    gui_position = gui_size
                    if any(text in gui_chunk for text in (
                            "Pending update package detected", "Update package detected",
                            "Updater started", "开始更新")):
                        update_seen = True
                        ctx.log("检测到 MAA 正在交接自动更新；保留模拟器并等待更新后的进程继续")
            if proc.poll() is not None and marker not in tail:
                successor_running = _process_path_exists(exe)
                successor_seen = successor_seen or successor_running
                if successor_running:
                    exit_seen_at = None
                elif exit_seen_at is None:
                    exit_seen_at = time.monotonic()
                    reason = "自动更新交接" if update_seen or proc.returncode == 0 else "进程退出"
                    ctx.log(f"MAA 原进程已{reason}（退出码 {proc.returncode}），"
                            f"等待最多 {restart_grace:g} 秒接续进程")
                elif time.monotonic() - exit_seen_at >= restart_grace:
                    return Result(False, f"MAA 退出后 {restart_grace:g} 秒内未恢复"
                                  f"（退出码 {proc.returncode}）",
                                  {"update_seen": update_seen,
                                   "successor_seen": successor_seen})
        return Result(False, f"等待 MAA 完成超时（{timeout} 秒）")
    finally:
        if step.get("close_on_complete", True):
            if proc.poll() is None:
                proc.terminate()
            elif os.name == "nt" and (successor_seen or _process_path_exists(exe)):
                subprocess.run(["taskkill", "/IM", exe.name, "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)


def run_baas_gui(step: dict[str, Any], ctx: RunContext) -> Result:
    """Start BAAS Pro and follow its rotating per-profile log until completion."""
    exe = Path(expand(str(step.get("executable") or ctx.tool("baas_gui"))))
    if not exe.exists():
        return Result(False, f"找不到 BAAS：{exe}")
    log_glob = expand(str(step.get("log_glob") or exe.parent / "runtime" / "logs" / "*_baas1.log"))
    marker = str(step.get("completion_marker", "任务全部执行成功"))
    error_markers = [str(x) for x in step.get("error_markers", ["任务全部执行失败"])]
    recoverable_markers = [str(x) for x in step.get("recoverable_error_markers", [
        "RestartTaskException: ATX卡死，重启任务", "ATX卡死，开始重启ATX",
        "任务卡死，开始重启任务", "重启任务【",
    ])]
    process_images = [str(x) for x in step.get("process_images", [exe.name, "baas.exe"])]
    if os.name == "nt" and step.get("clean_existing", True):
        for image_name in dict.fromkeys(process_images):
            subprocess.run(["taskkill", "/IM", image_name, "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(0.5)
    existing = {path: Path(path).stat().st_size for path in glob.glob(log_glob)}
    try:
        proc = subprocess.Popen([str(exe)], cwd=str(exe.parent),
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError as exc:
        return Result(False, f"无法启动 BAAS：{exc}")
    timeout = int(step.get("timeout", 3600))
    started, tails = time.monotonic(), {}
    startup_reference = started
    click_count = 0
    max_start_clicks = max(1, int(step.get("max_start_clicks", 6)))
    startup_timeout = float(step.get("startup_timeout", 300))
    next_fallback_at = started + float(step.get("gui_fallback_after", 45))
    run_started = False
    completion_at = None
    completion_log = None
    quiet_seconds = float(step.get("completion_quiet_seconds", 15))
    required_last_task = str(step.get("required_last_task", "")).strip()
    required_check_interval = float(step.get("required_task_check_interval", 600))
    completion_seen = False
    next_required_check_at = None
    last_run_task = None
    last_completed_task = None
    recoverable_events = 0
    last_recoverable_at = None
    max_recoverable_events = max(1, int(step.get("max_recoverable_events", 3)))
    poll_seconds = max(0.05, float(step.get("poll_seconds", 1)))
    emulator_instance = step.get("emulator_instance")
    emulator_device = str(step.get("device", "emulator-5560"))
    emulator_restarts = 0
    max_emulator_restarts = max(0, int(step.get("max_emulator_restarts", 1)))
    emulator_offline_markers = [str(x) for x in step.get("emulator_offline_markers", [
        "USB device", "is offline", "模拟器连接失败，必须打开模拟器",
    ])]
    start_markers = [str(x) for x in step.get(
        "start_markers", ["模拟器连接成功", "开始执行【"])]

    def restart_emulator() -> Result:
        if emulator_instance is None:
            return Result(False, "BAAS 未配置可恢复的雷电模拟器实例")
        ldconsole = ctx.tool("ldconsole")
        index = str(emulator_instance)
        ctx.command([ldconsole, "quit", "--index", index], 60)
        if ctx.stop_event.wait(float(step.get("emulator_restart_delay", 3))):
            return Result(False, "任务被用户停止")
        launched = ctx.command([ldconsole, "launch", "--index", index], 120)
        if not launched.success:
            return Result(False, f"重新启动碧蓝档案模拟器失败：{launched.message}")
        return run_adb({"action": "wait", "device": emulator_device,
                        "timeout": int(step.get("emulator_ready_timeout", 180)),
                        "poll_seconds": 2, "settle_seconds": 3}, ctx)

    ctx.log(f"BAAS 已启动，等待 baas1 完成标记：{marker}")
    try:
        while time.monotonic() - started <= timeout:
            if ctx.stop_event.wait(poll_seconds):
                return Result(False, "任务被用户停止")
            paths = glob.glob(log_glob)
            for name in paths:
                path = Path(name)
                position = existing.get(name, 0)
                size = path.stat().st_size
                if size < position:
                    position = 0
                if size > position:
                    with path.open("rb") as handle:
                        handle.seek(position)
                        chunk = handle.read()
                    existing[name] = size
                    content = chunk.decode("utf-8", errors="replace")
                    tails[name] = (tails.get(name, "") + content)[-16000:]
                    update_marker = _find_update_marker(content, step)
                    if update_marker:
                        return _needs_update("BAAS 或碧蓝档案", update_marker, log=name)
                    if completion_at is not None:
                        if required_last_task:
                            completion_at = time.monotonic()
                            completion_log = name
                            ctx.log("BAAS 在关闭观察期仍有新日志，重新计算稳定时间")
                        else:
                            ctx.log("BAAS 在成功标记后仍有新日志，继续等待真正结束")
                            completion_at = None
                            completion_log = None
                    if any(start_marker in content for start_marker in start_markers):
                        run_started = True
                    for line in content.splitlines():
                        started_task = re.search(r"开始执行【([^】]+)】", line)
                        if started_task:
                            last_run_task = started_task.group(1).strip()
                        completed = re.search(r"执行完成【([^】]+)】", line)
                        if completed:
                            last_completed_task = completed.group(1).strip()
                            last_run_task = last_completed_task
                            ctx.log(f"BAAS 本轮最后完成任务更新为“{last_completed_task}”")
                        if marker in line:
                            if run_started:
                                completion_seen = True
                                completion_log = name
                                if required_last_task:
                                    next_required_check_at = time.monotonic()
                                    ctx.log(f"检测到 BAAS 结束信号，检查最后完成任务是否为“{required_last_task}”")
                                else:
                                    completion_at = time.monotonic()
                                    ctx.log(f"检测到 BAAS 成功标记，继续观察 {quiet_seconds:g} 秒确认无后续任务")
                            else:
                                ctx.log("忽略未出现本次任务开始记录之前的 BAAS 成功标记")
                    if (required_last_task and completion_at is not None
                            and (last_run_task != required_last_task
                                 or last_completed_task != required_last_task)):
                        completion_at = None
                        next_required_check_at = time.monotonic() + required_check_interval
                        shown = last_completed_task or "尚无完成记录"
                        ctx.log(f"观察期最后完成任务变为“{shown}”；"
                                f"{required_check_interval:g} 秒后重新检测")
                    recoverable = next((item for item in recoverable_markers if item in content), None)
                    if recoverable:
                        now = time.monotonic()
                        if last_recoverable_at is None or now - last_recoverable_at >= 5:
                            recoverable_events += 1
                            last_recoverable_at = now
                        ctx.log(f"BAAS 检测到可恢复异常（第 {recoverable_events} 次）：{recoverable}；"
                                "保留模拟器并等待 BAAS 自恢复")
                        if recoverable_events > max_recoverable_events:
                            return Result(False, f"BAAS 连续自恢复超过 {max_recoverable_events} 次：{recoverable}",
                                          {"log": name, "marker": recoverable,
                                           "recoverable_events": recoverable_events})
                        run_started = False
                        startup_reference = time.monotonic()
                        next_fallback_at = (time.monotonic()
                                            + float(step.get("recoverable_retry_seconds", 30)))
                    offline = (next((item for item in emulator_offline_markers if item in content), None)
                               if "offline" in content.casefold() or "模拟器连接失败" in content else None)
                    if offline:
                        if emulator_restarts >= max_emulator_restarts:
                            return Result(False, f"BAAS 模拟器离线且已达到自动重启上限：{offline}",
                                          {"log": name, "marker": offline,
                                           "emulator_restarts": emulator_restarts})
                        emulator_restarts += 1
                        ctx.log(f"BAAS 检测到模拟器离线，自动重启雷电实例"
                                f" {emulator_instance}（{emulator_restarts}/{max_emulator_restarts}）")
                        recovered = restart_emulator()
                        if not recovered.success:
                            return Result(False, recovered.message,
                                          {"emulator_restarts": emulator_restarts})
                        run_started = False
                        startup_reference = time.monotonic()
                        next_fallback_at = time.monotonic() + float(
                            step.get("emulator_recovered_click_delay", 10))
                        ctx.log("碧蓝档案模拟器已恢复，继续等待 BAAS 并重新点击启动")
                    for error in error_markers:
                        if error in content:
                            return Result(False, f"BAAS 报告任务错误：{error}", {"log": name, "marker": error})
            now = time.monotonic()
            if (required_last_task and completion_seen and next_required_check_at is not None
                    and now >= next_required_check_at):
                if (last_run_task == required_last_task
                        and last_completed_task == required_last_task):
                    completion_at = now
                    next_required_check_at = None
                    ctx.log(f"已确认 BAAS 最后完成任务为“{required_last_task}”，观察 {quiet_seconds:g} 秒后关闭")
                else:
                    completion_at = None
                    next_required_check_at = now + required_check_interval
                    shown = last_completed_task or "尚无完成记录"
                    ctx.log(f"BAAS 最后完成任务为“{shown}”，不是“{required_last_task}”；"
                            f"{required_check_interval:g} 秒后重新检测，期间保持 BAAS 和模拟器运行")
            if completion_at is not None and time.monotonic() - completion_at >= quiet_seconds:
                message = (f"BAAS 最后完成任务已确认为“{required_last_task}”"
                           if required_last_task else "BAAS 日常任务全部完成且已停止输出")
                return Result(True, message,
                              {"log": completion_log, "marker": marker,
                               "quiet_seconds": quiet_seconds,
                                "last_run_task": last_run_task,
                                "last_completed_task": last_completed_task,
                                "recoverable_events": recoverable_events,
                                "emulator_restarts": emulator_restarts,
                                "start_clicks": click_count})
            if not run_started and time.monotonic() - startup_reference >= startup_timeout:
                return Result(False, f"BAAS 打开后 {startup_timeout:g} 秒仍无任务启动日志",
                              {"start_clicks": click_count})
            if (step.get("gui_click_fallback", True) and not run_started
                    and time.monotonic() >= next_fallback_at):
                if click_count >= max_start_clicks:
                    return Result(False, f"BAAS 已循环点击启动 {click_count} 次，仍无任务启动日志",
                                  {"start_clicks": click_count})
                click_count += 1
                clicked, click_message = _click_baas_start_button(
                    proc.pid, float(step.get("gui_click_x_ratio", 0.374)),
                    float(step.get("gui_click_y_ratio", 0.108)),
                    float(step.get("gui_profile_x_ratio", 0.04)),
                    float(step.get("gui_profile_y_ratio", 0.15)))
                ctx.log(f"BAAS 第 {click_count} 次启动点击：{click_message}")
                retry_seconds = float(step.get("gui_fallback_retry_seconds", 30))
                next_fallback_at = time.monotonic() + retry_seconds
                if not clicked:
                    elapsed = time.monotonic() - started
                    window_timeout = float(step.get("gui_window_timeout", 120))
                    if elapsed >= window_timeout:
                        return Result(False, f"等待 BAAS 窗口 {window_timeout:g} 秒后仍无法点击启动：{click_message}")
                    ctx.log(f"BAAS 窗口仍在启动，{retry_seconds:g} 秒后重试备用点击")
            if proc.poll() is not None and not _process_image_exists(process_images):
                return Result(False, f"BAAS 提前退出（退出码 {proc.returncode}）")
        return Result(False, f"等待 BAAS 完成超时（{timeout} 秒）")
    finally:
        if step.get("close_on_complete", True):
            if os.name == "nt":
                for image_name in dict.fromkeys(process_images):
                    subprocess.run(["taskkill", "/IM", image_name, "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
            elif proc.poll() is None:
                proc.terminate()


def _process_path_exists(executable: Path) -> bool:
    try:
        import psutil
        expected = os.path.normcase(str(executable.resolve()))
        for process in psutil.process_iter(["exe"]):
            try:
                if process.info["exe"] and os.path.normcase(str(Path(process.info["exe"]).resolve())) == expected:
                    return True
            except (psutil.Error, OSError):
                continue
    except ImportError:
        return False
    return False


def _process_image_exists(image_names: list[str]) -> bool:
    expected = {name.casefold() for name in image_names}
    try:
        import psutil
        for process in psutil.process_iter(["name"]):
            try:
                if process.info["name"] and process.info["name"].casefold() in expected:
                    return True
            except psutil.Error:
                continue
    except ImportError:
        return False
    return False


def _click_baas_start_button(root_pid: int, x_ratio: float, y_ratio: float,
                             profile_x_ratio: float = 0.04,
                             profile_y_ratio: float = 0.15) -> tuple[bool, str]:
    """Click BAAS Start using UI Automation, then window-relative coordinates as fallback."""
    if os.name != "nt":
        return False, "GUI 备用点击只支持 Windows"
    try:
        import psutil
        import win32con
        import win32gui
        import win32process
    except ImportError as exc:
        return False, f"缺少 Windows GUI 自动化组件：{exc}"
    pids = {root_pid}
    try:
        root = psutil.Process(root_pid)
        pids.update(child.pid for child in root.children(recursive=True))
    except psutil.Error:
        pass
    candidates = []

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        title = win32gui.GetWindowText(hwnd)
        if pid in pids or "BAAS" in title.upper() or "BLUEARCHIVEAUTOSCRIPT" in title.upper():
            rect = win32gui.GetWindowRect(hwnd)
            area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
            if area > 10000:
                upper_title = title.upper()
                priority = 10 if "BAAS PRO" in upper_title else (8 if "BLUEARCHIVEAUTOSCRIPT" in upper_title else 1)
                candidates.append((priority, area, hwnd, title, rect))

    win32gui.EnumWindows(collect, None)
    if not candidates:
        return False, "BAAS 未产生可见窗口，无法执行备用点击"
    _, _, hwnd, title, rect = max(candidates)
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    try:
        from pywinauto import Application
        app = Application(backend="uia").connect(handle=hwnd, timeout=3)
        window = app.window(handle=hwnd)
        for control in window.descendants(control_type="Button"):
            name = control.window_text().strip()
            if name in ("启动", "开始", "运行") or "启动" in name:
                control.click_input()
                return True, f"已通过 GUI 控件点击 BAAS“{name}”按钮"
    except Exception:
        pass
    left, top, right, bottom = rect
    profile_x = left + round((right - left) * min(max(profile_x_ratio, 0.0), 1.0))
    profile_y = top + round((bottom - top) * min(max(profile_y_ratio, 0.0), 1.0))
    x = left + round((right - left) * min(max(x_ratio, 0.0), 1.0))
    y = top + round((bottom - top) * min(max(y_ratio, 0.0), 1.0))
    try:
        import pyautogui
        pyautogui.click(profile_x, profile_y)
        time.sleep(0.5)
        pyautogui.click(x, y)
        return True, (f"已点击 BAAS 的 baas1（{profile_x}, {profile_y}）和启动按钮"
                      f"（{x}, {y}）")
    except Exception as exc:
        return False, f"BAAS GUI 备用点击失败：{exc}"


def run_ba_reward_verify(step: dict[str, Any], ctx: RunContext) -> Result:
    """Open Blue Archive's work-task page and verify no collect-all button remains."""
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        return Result(False, f"奖励验证需要 OpenCV：{exc}")
    adb = ctx.tool("adb")
    device = str(step.get("device") or "emulator-5560")
    timeout = int(step.get("timeout", 45))
    evidence = Path(expand(str(step.get("evidence_path", ctx.root / "logs" / "blue_archive_daily_check.png"))))
    evidence.parent.mkdir(parents=True, exist_ok=True)

    def capture() -> tuple[Result, Any | None]:
        try:
            done = subprocess.run([adb, "-s", device, "exec-out", "screencap", "-p"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Result(False, f"模拟器截图失败：{exc}"), None
        if done.returncode != 0:
            return Result(False, "模拟器截图失败：" + done.stderr.decode(errors="replace").strip()), None
        image = cv2.imdecode(np.frombuffer(done.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
        return (Result(True, "截图成功"), image) if image is not None else (Result(False, "无法解析模拟器截图"), None)

    def match(image, template_path: Path) -> tuple[float, tuple[int, int]]:
        template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
        if template is None or image.shape[0] < template.shape[0] or image.shape[1] < template.shape[1]:
            return 0.0, (0, 0)
        result = cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, point = cv2.minMaxLoc(result)
        return float(score), (point[0] + template.shape[1] // 2, point[1] + template.shape[0] // 2)

    first_result, home = capture()
    if not first_result.success:
        return first_result
    threshold = float(step.get("threshold", 0.82))
    entry_x_ratio = float(step.get("entry_x_ratio", 67 / 1280))
    entry_y_ratio = float(step.get("entry_y_ratio", 235 / 720))
    point = (round(home.shape[1] * entry_x_ratio), round(home.shape[0] * entry_y_ratio))
    work_score = None
    ctx.log(f"直接点击“工作任务”固定位置：{point}")
    click = subprocess.run([adb, "-s", device, "shell", "input", "tap", str(point[0]), str(point[1])],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if click.returncode != 0:
        return Result(False, "点击每日任务入口失败")
    time.sleep(float(step.get("page_wait", 3)))
    page_result, task_page = capture()
    if not page_result.success:
        return page_result
    cv2.imwrite(str(evidence), task_page)
    change = float(np.mean(cv2.absdiff(home, task_page)))
    if change < float(step.get("min_page_change", 8)):
        return Result(False, "点击后未能确认进入每日任务页",
                      {"evidence": str(evidence), "page_change": change})
    def gray_stats(roi):
        x1 = round(task_page.shape[1] * float(roi[0]))
        y1 = round(task_page.shape[0] * float(roi[1]))
        x2 = round(task_page.shape[1] * float(roi[2]))
        y2 = round(task_page.shape[0] * float(roi[3]))
        area = task_page[y1:y2, x1:x2]
        if area.size == 0:
            return {"sat_mean": 255.0, "high_sat_fraction": 1.0, "roi": [x1, y1, x2, y2]}
        hsv = cv2.cvtColor(area, cv2.COLOR_BGR2HSV)
        saturation = hsv[:, :, 1]
        return {"sat_mean": float(np.mean(saturation)),
                "high_sat_fraction": float(np.mean(saturation > 60)),
                "roi": [x1, y1, x2, y2]}

    complete_stats = gray_stats(step.get("complete_button_roi", [0.722, 0.894, 0.805, 0.972]))
    collect_stats = gray_stats(step.get("collect_button_roi", [0.817, 0.889, 0.983, 0.976]))
    max_mean = float(step.get("gray_max_saturation_mean", 35))
    max_fraction = float(step.get("gray_max_high_saturation_fraction", 0.15))
    complete_gray = (complete_stats["sat_mean"] <= max_mean and
                     complete_stats["high_sat_fraction"] <= max_fraction)
    collect_gray = (collect_stats["sat_mean"] <= max_mean and
                    collect_stats["high_sat_fraction"] <= max_fraction)
    details = {"evidence": str(evidence), "complete_gray": complete_gray,
               "collect_gray": collect_gray, "complete_stats": complete_stats,
               "collect_stats": collect_stats, "page_change": change}
    if not complete_gray and not collect_gray:
        return Result(False, "“完成”和“一键领取”按钮均未变灰，今日任务尚未完成", details)
    if not complete_gray:
        return Result(False, "完成9次以上每日任务的“完成”按钮尚未变灰", details)
    if not collect_gray:
        return Result(False, "“一键领取”按钮尚未变灰，仍有奖励未领取", details)
    return Result(True, "已确认“完成”和“一键领取”按钮均为灰色，今日任务完成", details)


def run_alas_gui(step: dict[str, Any], ctx: RunContext) -> Result:
    """Start ALAS and wait until its scheduler reaches a stable no-pending state."""
    exe = Path(expand(str(step.get("executable") or ctx.tool("alas_gui"))))
    if not exe.exists():
        return Result(False, f"找不到 AzurLaneAutoScript：{exe}")
    log_glob = expand(str(step.get("log_glob") or exe.parent / "log" / "*_alas.txt"))
    process_image = str(step.get("process_image", exe.name))
    if os.name == "nt" and step.get("clean_existing", True):
        subprocess.run(["taskkill", "/IM", process_image, "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(0.5)
    existing = {name: Path(name).stat().st_size for name in glob.glob(log_glob)}
    launch_env = os.environ.copy()
    launch_env.update({str(k): expand(str(v)) for k, v in step.get("env", {}).items()})
    try:
        proc = subprocess.Popen([str(exe)], cwd=str(exe.parent), env=launch_env,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError as exc:
        return Result(False, f"无法启动 AzurLaneAutoScript：{exc}")
    timeout = int(step.get("timeout", 7200))
    quiet_seconds = float(step.get("completion_quiet_seconds", 20))
    start_markers = [str(x) for x in step.get("start_markers", ["Scheduler: Start task"])]
    completion_marker = str(step.get("completion_marker", "No task pending"))
    error_markers = [str(x) for x in step.get(
        "error_markers", ["No emulator with serial", "无法连接至ADB服务",
                          "Request human takeover", "RequestHumanTakeover",
                          "ScriptError", "GameNotRunningError"])]
    started_at = time.monotonic()
    run_started = False
    completion_at = None
    completion_log = None
    log_retry_interval = max(0.05, float(step.get("log_retry_interval", 30)))
    next_log_retry_at = started_at + log_retry_interval
    start_click_count = 0
    max_start_clicks = max(1, int(step.get("max_start_clicks", 5)))
    startup_timeout = max(log_retry_interval, float(step.get("startup_timeout", 300)))
    tails = {}
    last_log = None
    ctx.log(f"AzurLaneAutoScript 已启动；若日志 {log_retry_interval:g} 秒无更新，"
            "将循环点击“启动”")
    try:
        while time.monotonic() - started_at <= timeout:
            if ctx.stop_event.wait(min(1.0, log_retry_interval)):
                return Result(False, "任务被用户停止")
            for name in glob.glob(log_glob):
                path = Path(name)
                position = existing.get(name, 0)
                size = path.stat().st_size
                if size < position:
                    position = 0
                if size <= position:
                    continue
                with path.open("rb") as handle:
                    handle.seek(position)
                    content = handle.read().decode("utf-8", errors="replace")
                existing[name] = size
                tails[name] = (tails.get(name, "") + content)[-20000:]
                last_log = name
                next_log_retry_at = time.monotonic() + log_retry_interval
                update_marker = _find_update_marker(content, step)
                if update_marker:
                    return _needs_update("ALAS 或碧蓝航线", update_marker, log=name)
                if completion_at is not None:
                    ctx.log("ALAS 在空闲标记后仍有新日志，继续等待调度器稳定")
                    completion_at = None
                    completion_log = None
                if any(marker in content for marker in start_markers):
                    run_started = True
                folded_content = content.casefold()
                for error in error_markers:
                    if error.casefold() in folded_content:
                        return Result(False, f"AzurLaneAutoScript 报告错误：{error}",
                                      {"log": name, "marker": error})
                if completion_marker in content:
                    if run_started:
                        completion_at = time.monotonic()
                        completion_log = name
                        ctx.log(f"ALAS 已无待执行任务，观察 {quiet_seconds:g} 秒确认调度器稳定")
                    else:
                        ctx.log("忽略本次任务启动前的 ALAS 空闲记录")
            if completion_at is not None and time.monotonic() - completion_at >= quiet_seconds:
                return Result(True, "AzurLaneAutoScript 本轮每日任务完成",
                              {"log": completion_log, "marker": completion_marker,
                               "quiet_seconds": quiet_seconds,
                               "start_clicks": start_click_count})
            elapsed = time.monotonic() - started_at
            if not run_started and elapsed >= startup_timeout:
                return Result(False, f"ALAS 在 {startup_timeout:g} 秒内未进入任务状态",
                              {"log": last_log, "start_clicks": start_click_count})
            if (step.get("gui_click_fallback", True) and not run_started
                    and time.monotonic() >= next_log_retry_at):
                if start_click_count >= max_start_clicks:
                    return Result(False, f"ALAS 点击“启动” {max_start_clicks} 次后日志仍未进入任务状态",
                                  {"log": last_log, "start_clicks": start_click_count})
                start_click_count += 1
                clicked, message = _click_alas_start_button(
                    proc.pid, float(step.get("gui_click_x_ratio", 0.43)),
                    float(step.get("gui_click_y_ratio", 0.105)))
                ctx.log(f"ALAS 日志未更新，第 {start_click_count} 次尝试启动：{message}")
                if not clicked:
                    return Result(False, message)
                next_log_retry_at = time.monotonic() + log_retry_interval
            if proc.poll() is not None and not _process_image_exists([process_image]):
                return Result(False, f"AzurLaneAutoScript 提前退出（退出码 {proc.returncode}）")
        return Result(False, f"等待 AzurLaneAutoScript 完成超时（{timeout} 秒）")
    finally:
        if step.get("close_on_complete", True):
            if os.name == "nt":
                subprocess.run(["taskkill", "/IM", process_image, "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            elif proc.poll() is None:
                proc.terminate()


def _naruto_visual_metrics(image) -> dict[str, Any]:
    """Measure the two stable Naruto completion screens at any 16:9 resolution."""
    import cv2
    import numpy as np

    height, width = image.shape[:2]
    landscape = width > height
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

    def area(x1, y1, x2, y2):
        return hsv[round(height * y1):round(height * y2),
                   round(width * x1):round(width * x2)]

    panel = area(0.031, 0.167, 0.969, 0.833)
    popup_white_fraction = float(np.mean((panel[:, :, 1] < 35) & (panel[:, :, 2] > 190)))
    consent_button = area(0.52, 0.90, 0.95, 0.98)
    consent_orange_fraction = float(np.mean(
        (consent_button[:, :, 0] < 25) & (consent_button[:, :, 1] > 120) &
        (consent_button[:, :, 2] > 120)))

    home_center = area(0.195, 0.139, 0.703, 0.472)
    home_blue_fraction = float(np.mean(
        (home_center[:, :, 0] >= 90) & (home_center[:, :, 0] <= 125) &
        (home_center[:, :, 1] > 60) & (home_center[:, :, 2] > 35)))

    progress = area(0.328, 0.764, 0.938, 0.819)
    orange = ((progress[:, :, 0] < 25) & (progress[:, :, 1] > 120) &
              (progress[:, :, 2] > 100))
    progress_orange_fraction = float(np.mean(orange))
    hundred = area(0.844, 0.771, 0.906, 0.812)
    hundred_orange_fraction = float(np.mean(
        (hundred[:, :, 0] < 25) & (hundred[:, :, 1] > 120) & (hundred[:, :, 2] > 100)))

    chest_saturation = []
    chest_lid_saturation = []
    for center_x in (0.398, 0.570, 0.793, 0.918):
        chest = area(center_x - 0.043, 0.660, center_x + 0.043, 0.792)
        lid = area(center_x - 0.043, 0.660, center_x + 0.043, 0.715)
        chest_saturation.append(float(np.mean(chest[:, :, 1] > 100)))
        chest_lid_saturation.append(float(np.mean(lid[:, :, 1] > 100)))

    reward_page = progress_orange_fraction >= 0.20 and min(chest_saturation) >= 0.40
    all_chests_claimed = (reward_page and hundred_orange_fraction >= 0.35 and
                          min(chest_saturation) >= 0.48 and min(chest_lid_saturation) >= 0.48)
    return {
        "landscape": landscape,
        "portrait_white_dialog": not landscape and popup_white_fraction >= 0.60,
        "game_privacy_consent": (popup_white_fraction >= 0.60
                                 and consent_orange_fraction >= 0.35),
        "consent_orange_fraction": consent_orange_fraction,
        "completion_popup": landscape and popup_white_fraction >= 0.60,
        "popup_white_fraction": popup_white_fraction,
        "home_page": home_blue_fraction >= 0.60,
        "home_blue_fraction": home_blue_fraction,
        "reward_page": reward_page,
        "progress_orange_fraction": progress_orange_fraction,
        "hundred_orange_fraction": hundred_orange_fraction,
        "chest_saturation": chest_saturation,
        "chest_lid_saturation": chest_lid_saturation,
        "all_chests_claimed": all_chests_claimed,
    }


def run_naruto_shadow(step: dict[str, Any], ctx: RunContext) -> Result:
    """Keep starting Shadow Clone until it actually hands control to Naruto."""
    adb = ctx.tool("adb")
    device = str(step.get("device", "emulator-5554"))
    shadow_package = str(step.get("shadow_package", "com.yy.yfs"))
    game_package = str(step.get("game_package", "com.tencent.KiHan"))
    timeout = int(step.get("timeout", 7200))

    def command(args, seconds=30):
        try:
            return subprocess.run([adb, "-s", device] + list(args), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                  errors="replace", timeout=seconds,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return exc

    def tap(x, y):
        return command(["shell", "input", "tap", str(x), str(y)])

    def capture_metrics():
        try:
            import cv2
            import numpy as np
            done = subprocess.run([adb, "-s", device, "exec-out", "screencap", "-p"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if done.returncode != 0:
                return None
            image = cv2.imdecode(np.frombuffer(done.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                return None
            metrics = _naruto_visual_metrics(image)
            metrics["_height"], metrics["_width"] = image.shape[:2]
            return metrics
        except (ImportError, OSError, subprocess.TimeoutExpired):
            return None

    def locate_float_icon():
        """Locate the movable red Shadow Clone overlay near the right screen edge."""
        try:
            import cv2
            import numpy as np
            done = subprocess.run([adb, "-s", device, "exec-out", "screencap", "-p"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            image = cv2.imdecode(np.frombuffer(done.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
            if done.returncode != 0 or image is None:
                return None
            height, width = image.shape[:2]
            hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
            red = (((hsv[:, :, 0] <= 12) | (hsv[:, :, 0] >= 165)) &
                   (hsv[:, :, 1] >= 100) & (hsv[:, :, 2] >= 70)).astype("uint8")
            red[:round(height * 0.12), :] = 0
            red[round(height * 0.80):, :] = 0
            red[:, :round(width * 0.84)] = 0
            count, _, stats, centers = cv2.connectedComponentsWithStats(red, 8)
            candidates = []
            for index in range(1, count):
                area = int(stats[index, cv2.CC_STAT_AREA])
                x, y = centers[index]
                if area >= 20:
                    candidates.append((area, int(round(x)), int(round(y))))
            if not candidates:
                return None
            _, x, y = max(candidates)
            return x, y
        except (ImportError, OSError, subprocess.TimeoutExpired):
            return None

    def focused_on(package: str) -> bool:
        target = package.casefold()
        probes = (
            (["shell", "dumpsys", "window", "windows"],
             ("mCurrentFocus", "mFocusedApp")),
            (["shell", "dumpsys", "activity", "activities"],
             ("topResumedActivity", "mResumedActivity", "ResumedActivity")),
        )
        for args, labels in probes:
            state = command(args)
            if isinstance(state, Exception) or state.returncode != 0:
                continue
            focus_lines = [line for line in state.stdout.splitlines()
                           if any(label in line for label in labels)]
            if target in "\n".join(focus_lines).casefold():
                return True
        return False

    def read_ui_xml() -> str:
        remote_ui = "/sdcard/gameflow-ui.xml"
        dumped = command(["shell", "uiautomator", "dump", remote_ui], seconds=20)
        if isinstance(dumped, Exception) or dumped.returncode != 0:
            return ""
        xml = command(["shell", "cat", remote_ui], seconds=20)
        if isinstance(xml, Exception) or xml.returncode != 0:
            return ""
        return xml.stdout

    def tap_ui_text(*labels: str) -> bool:
        """Tap a visible Android control by text, avoiding stale fixed coordinates."""
        xml = read_ui_xml()
        if not xml:
            return False
        wanted = {label.casefold() for label in labels}
        for node in re.findall(r"<node\b[^>]*>", xml):
            shown_values = [value.strip().casefold() for value in
                            re.findall(r'(?:text|content-desc)="([^"]*)"', node)
                            if value.strip()]
            bounds = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
            if not shown_values or not bounds:
                continue
            if not any(label == shown or label in shown
                       for label in wanted for shown in shown_values):
                continue
            x = (int(bounds.group(1)) + int(bounds.group(3))) // 2
            y = (int(bounds.group(2)) + int(bounds.group(4))) // 2
            done = tap(x, y)
            return not isinstance(done, Exception) and done.returncode == 0
        return False

    def dismiss_shadow_dialog() -> Result:
        attempts = max(1, int(step.get("tutorial_confirm_attempts", 5)))
        point = tuple(step.get("tutorial_confirm_point", [360, 936]))
        for attempt in range(1, attempts + 1):
            metrics = capture_metrics()
            if metrics is not None and not metrics.get("portrait_white_dialog"):
                return Result(True, "影分身功能说明弹窗已关闭", {"attempts": attempt - 1})
            if metrics is None:
                ctx.log(f"第 {attempt} 次无法取得影分身画面，稍后重新确认弹窗")
            else:
                done = tap(*point)
                if isinstance(done, Exception) or done.returncode != 0:
                    return Result(False, f"点击影分身“确定”失败：{done}")
                ctx.log(f"第 {attempt} 次点击影分身功能说明“确定”：{point}")
            if ctx.stop_event.wait(float(step.get("tutorial_close_wait", 2))):
                return Result(False, "任务被用户停止")
        metrics = capture_metrics()
        if metrics is not None and not metrics.get("portrait_white_dialog"):
            return Result(True, "影分身功能说明弹窗已关闭", {"attempts": attempts})
        return Result(False, f"循环点击“确定” {attempts} 次后功能说明弹窗仍未消失")

    launch_deadline = time.monotonic() + float(step.get("app_launch_timeout", 90))
    launch = None
    while time.monotonic() < launch_deadline:
        package_ready = command(["shell", "pm", "path", shadow_package])
        if (not isinstance(package_ready, Exception) and package_ready.returncode == 0
                and "package:" in package_ready.stdout):
            launch = command(["shell", "monkey", "-p", shadow_package,
                              "-c", "android.intent.category.LAUNCHER", "1"])
            if not isinstance(launch, Exception) and launch.returncode == 0:
                break
        if ctx.stop_event.wait(2):
            return Result(False, "任务被用户停止")
    if isinstance(launch, Exception) or launch is None or launch.returncode != 0:
        output = (getattr(launch, "stdout", "") + getattr(launch, "stderr", "")).strip()
        return Result(False, f"Android 已等待但仍无法启动影分身：{output or launch}")
    if ctx.stop_event.wait(float(step.get("app_wait", 5))):
        return Result(False, "任务被用户停止")
    if step.get("shadow_open_confirm", True):
        closed = dismiss_shadow_dialog()
        if not closed.success:
            return closed
    command(["shell", "input", "swipe", "360", "1050", "360", "350", "600"])
    ctx.stop_event.wait(1)
    enable_point = tuple(step.get("enable_point", [360, 1215]))
    enable_deadline = time.monotonic() + float(step.get("enable_retry_timeout", 90))
    enable_attempt = 0
    while time.monotonic() < enable_deadline:
        if ctx.stop_event.is_set():
            return Result(False, "任务被用户停止")
        metrics = capture_metrics()
        if metrics and metrics.get("portrait_white_dialog"):
            closed = dismiss_shadow_dialog()
            if not closed.success:
                return closed
        enable_attempt += 1
        done = tap(*enable_point)
        if isinstance(done, Exception) or done.returncode != 0:
            return Result(False, f"点击影分身“启动功能”失败：{done}")
        ctx.log(f"第 {enable_attempt} 次点击影分身“启动功能”：{enable_point}")
        if ctx.stop_event.wait(float(step.get("enable_retry_interval", 3))):
            return Result(False, "任务被用户停止")
        metrics = capture_metrics()
        if metrics and metrics.get("portrait_white_dialog"):
            ctx.log("“启动功能”弹出了功能说明；关闭后将再次点击启动功能")
            closed = dismiss_shadow_dialog()
            if not closed.success:
                return closed
            continue
        if locate_float_icon() is not None:
            ctx.log(f"已确认影分身启动功能生效（共点击 {enable_attempt} 次）")
            break
    else:
        return Result(False, f"循环点击影分身“启动功能” {enable_attempt} 次后仍未出现红色浮窗")
    start_deadline = time.monotonic() + float(step.get(
        "start_retry_timeout", step.get("game_focus_timeout", 180)))
    retry_interval = max(2.0, float(step.get("start_retry_interval", 8)))
    menu_wait = max(0.5, float(step.get("float_menu_wait", 1)))
    after_click_wait = max(1.0, float(step.get("after_start_click_wait", 2)))
    start_attempt = 0
    game_started = False
    privacy_accepted = False

    # Do not launch Naruto by package name here: only Shadow Clone bringing the game to the
    # foreground is accepted as proof that its Start action has taken effect.
    while time.monotonic() < start_deadline:
        if ctx.stop_event.is_set():
            return Result(False, "任务被用户停止")
        start_attempt += 1
        icon_point = locate_float_icon()
        if icon_point is None:
            metrics = capture_metrics()
            if metrics and metrics.get("portrait_white_dialog"):
                closed = dismiss_shadow_dialog()
                if not closed.success:
                    return closed
            ctx.log(f"第 {start_attempt} 次未识别到红色浮窗，不执行盲点，稍后重试")
            if ctx.stop_event.wait(retry_interval):
                return Result(False, "任务被用户停止")
            continue
        ctx.log(f"第 {start_attempt} 次识别到影分身红色浮窗：{icon_point}")
        tap(*icon_point)
        if ctx.stop_event.wait(menu_wait):
            return Result(False, "任务被用户停止")
        start_x = int(step.get("float_start_x", 293))
        start_y = int(icon_point[1] + float(step.get("float_start_y_offset", 49)))
        tap(start_x, start_y)
        ctx.log(f"第 {start_attempt} 次点击影分身“启动”：({start_x}, {start_y})")
        if ctx.stop_event.wait(after_click_wait):
            return Result(False, "任务被用户停止")
        game_focused = focused_on(game_package)
        start_metrics = capture_metrics()
        if (start_metrics and start_metrics.get("game_privacy_consent")
                and not privacy_accepted):
            clicked_text = tap_ui_text("同意", "接受并继续")
            consent_point = tuple(step.get("game_consent_point", [530, 1200]))
            width = int(start_metrics.get("_width", 720))
            height = int(start_metrics.get("_height", 1280))
            if consent_point[0] >= width or consent_point[1] >= height:
                consent_point = (round(width * 0.76), round(height * 0.94))
            if not clicked_text:
                tap(*consent_point)
            privacy_accepted = True
            ctx.log("检测到火影忍者《个人信息保护指引》，已按文字控件点击“同意”"
                    if clicked_text else
                    f"检测到火影忍者《个人信息保护指引》，已点击“同意”：{consent_point}")
            if ctx.stop_event.wait(float(step.get("game_consent_wait", 8))):
                return Result(False, "任务被用户停止")
            game_started = True
            break
        if game_focused:
            game_started = True
            break
        if focused_on(shadow_package):
            if tap_ui_text("继续", "立即开始", "允许"):
                ctx.log(f"第 {start_attempt} 次按界面文字点击影分身授权页按钮")

        attempt_deadline = min(start_deadline, time.monotonic() + retry_interval)
        while time.monotonic() < attempt_deadline:
            if focused_on(game_package):
                game_started = True
                break
            if ctx.stop_event.wait(min(1.0, max(0.0, attempt_deadline - time.monotonic()))):
                return Result(False, "任务被用户停止")
        if game_started:
            break
        ctx.log(f"第 {start_attempt} 次启动尚未生效，继续点击影分身“启动”")

    if not game_started:
        return Result(False, f"循环点击影分身“启动” {start_attempt} 次后仍未进入火影忍者")
    ctx.log(f"已确认影分身开始运行并进入火影忍者（共点击 {start_attempt} 次）")

    def detect_update_marker() -> str | None:
        xml = read_ui_xml()
        return _find_update_marker(xml, step) if xml else None

    update_marker = detect_update_marker()
    if update_marker:
        return _needs_update("影分身或火影忍者", update_marker)

    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        return Result(False, f"火影状态识别需要 OpenCV：{exc}")

    evidence = Path(expand(str(step.get(
        "evidence_path", ctx.root / "logs" / "naruto_activity_check.png"))))
    evidence.parent.mkdir(parents=True, exist_ok=True)
    poll_seconds = float(step.get("screenshot_interval", 60))
    one_hour_seconds = float(step.get("one_hour_seconds", 3600))
    timer_label = (f"{one_hour_seconds / 60:g} 分钟" if one_hour_seconds % 60 == 0
                   else f"{one_hour_seconds:g} 秒")
    initial_lobby_timeout = float(step.get("initial_lobby_timeout", 1800))
    page_wait = float(step.get("reward_page_wait", 8))

    def capture_frame():
        try:
            done = subprocess.run([adb, "-s", device, "exec-out", "screencap", "-p"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if done.returncode != 0:
                return None, None
            image = cv2.imdecode(np.frombuffer(done.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
            return (image, _naruto_visual_metrics(image)) if image is not None else (None, None)
        except (OSError, subprocess.TimeoutExpired):
            return None, None

    def wait_poll():
        return ctx.stop_event.wait(poll_seconds)

    # Stage 1: establish that the script has really reached the Naruto lobby.
    stage_started = time.monotonic()
    lobby_at = None
    ctx.log(f"阶段1：每隔 {poll_seconds:g} 秒截图，等待识别火影忍者大厅")
    while time.monotonic() - stage_started <= initial_lobby_timeout:
        image, metrics = capture_frame()
        update_marker = detect_update_marker()
        if update_marker:
            return _needs_update("影分身或火影忍者", update_marker)
        if metrics and metrics["home_page"]:
            lobby_at = time.monotonic()
            ctx.log(f"阶段1完成：已识别火影忍者大厅，开始 {timer_label} 计时")
            break
        if wait_poll():
            return Result(False, "任务被用户停止")
    if lobby_at is None:
        return Result(False, f"阶段1超时：{initial_lobby_timeout:g} 秒内未识别到火影忍者大厅")

    # Stage 2: do not assess completion until one hour after the initial lobby.
    remaining = max(0.0, lobby_at + one_hour_seconds - time.monotonic())
    if step.get("close_after_lobby_timer", False):
        ctx.log(f"退出计时：大厅识别后继续运行 {timer_label}，结束后直接关闭模拟器")
    else:
        ctx.log(f"阶段2：计时 {timer_label} 后开始检查大厅或结束标志")
    if ctx.stop_event.wait(remaining):
        return Result(False, "任务被用户停止")
    if step.get("close_after_lobby_timer", False):
        return Result(True, f"大厅识别后已运行 {one_hour_seconds:g} 秒，准备截图并关闭模拟器",
                      {"completion_mode": "lobby_timer", "timer_seconds": one_hour_seconds})

    cycle = 0
    while time.monotonic() - stage_started <= timeout:
        cycle += 1
        image, metrics = capture_frame()
        update_marker = detect_update_marker()
        if update_marker:
            return _needs_update("影分身或火影忍者", update_marker)
        if metrics and metrics["completion_popup"]:
            ctx.log("阶段2：识别到横屏结束标志，点击“确定”并继续等待大厅")
            tap(*step.get("popup_confirm_point", [640, 515]))
            if ctx.stop_event.wait(float(step.get("popup_close_wait", 5))):
                return Result(False, "任务被用户停止")
            metrics = None

        if metrics and metrics["home_page"]:
            ctx.log(f"阶段3：第 {cycle} 次检测到大厅，进入奖励页检查四个宝箱")
            reward_x = round(image.shape[1] * float(step.get("reward_x_ratio", 1230 / 1280)))
            reward_y = round(image.shape[0] * float(step.get("reward_y_ratio", 363 / 720)))
            tap(reward_x, reward_y)
            if ctx.stop_event.wait(page_wait):
                return Result(False, "任务被用户停止")
            reward_image, reward_metrics = capture_frame()
            if reward_image is not None:
                cv2.imwrite(str(evidence), reward_image)
            if reward_metrics and reward_metrics["reward_page"] and reward_metrics["all_chests_claimed"]:
                return Result(True, "已确认每日活跃度达到100，四个宝箱均已领取",
                              {"completion_mode": "reward_confirmed", "evidence": str(evidence),
                               "checks": cycle, **reward_metrics})

            if reward_metrics and reward_metrics["reward_page"]:
                ctx.log("阶段3：奖励尚未拿满，点击返回并回到阶段2继续每分钟检查")
                back_x = round(reward_image.shape[1] * float(step.get("reward_back_x_ratio", 80 / 1280)))
                back_y = round(reward_image.shape[0] * float(step.get("reward_back_y_ratio", 680 / 720)))
                tap(back_x, back_y)
                if ctx.stop_event.wait(float(step.get("reward_back_wait", 5))):
                    return Result(False, "任务被用户停止")
            else:
                ctx.log("阶段3：未进入奖励页，回到阶段2继续每分钟检查，不终止流程")
                command(["shell", "input", "keyevent", "4"])
                if ctx.stop_event.wait(float(step.get("reward_back_wait", 5))):
                    return Result(False, "任务被用户停止")
        else:
            ctx.log(f"阶段2：第 {cycle} 次截图未发现大厅或结束标志，继续等待")

        if wait_poll():
            return Result(False, "任务被用户停止")
    return Result(False, f"等待火影奖励全部领取超时（{timeout} 秒）")


def run_naruto_reward_verify(step: dict[str, Any], ctx: RunContext) -> Result:
    """Open Rewards at its fixed home coordinate and confirm all four daily chests are open."""
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        return Result(False, f"火影奖励验证需要 OpenCV：{exc}")
    adb = ctx.tool("adb")
    device = str(step.get("device", "emulator-5554"))
    timeout = int(step.get("timeout", 45))
    evidence = Path(expand(str(step.get(
        "evidence_path", ctx.root / "logs" / "naruto_activity_check.png"))))
    evidence.parent.mkdir(parents=True, exist_ok=True)

    def capture():
        try:
            done = subprocess.run([adb, "-s", device, "exec-out", "screencap", "-p"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Result(False, f"模拟器截图失败：{exc}"), None
        if done.returncode != 0:
            return Result(False, "模拟器截图失败：" + done.stderr.decode(errors="replace").strip()), None
        image = cv2.imdecode(np.frombuffer(done.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
        return (Result(True, "截图成功"), image) if image is not None else (Result(False, "无法解析模拟器截图"), None)

    shot_result, image = capture()
    if not shot_result.success:
        return shot_result
    metrics = _naruto_visual_metrics(image)
    if not metrics["landscape"]:
        cv2.imwrite(str(evidence), image)
        return Result(False, "火影奖励核验时仍停留在竖屏应用，影分身任务未正确进入游戏",
                      {"evidence": str(evidence), **metrics})
    if not metrics["reward_page"]:
        x = round(image.shape[1] * float(step.get("reward_x_ratio", 1230 / 1280)))
        y = round(image.shape[0] * float(step.get("reward_y_ratio", 363 / 720)))
        ctx.log(f"点击火影主页“奖励”固定位置：({x}, {y})")
        click = subprocess.run([adb, "-s", device, "shell", "input", "tap", str(x), str(y)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if click.returncode != 0:
            return Result(False, "点击火影主页“奖励”失败")
        if ctx.stop_event.wait(float(step.get("page_wait", 5))):
            return Result(False, "任务被用户停止")
        shot_result, image = capture()
        if not shot_result.success:
            return shot_result
        metrics = _naruto_visual_metrics(image)

    cv2.imwrite(str(evidence), image)
    details = {"evidence": str(evidence), **metrics}
    if not metrics["reward_page"]:
        return Result(False, "点击后未能确认进入火影每日奖励页", details)
    if not metrics["all_chests_claimed"]:
        return Result(False, "每日奖励的四个宝箱尚未全部领取", details)
    return Result(True, "已确认每日活跃度达到 100，四个日常宝箱均已领取", details)


def _click_alas_start_button(root_pid: int, x_ratio: float, y_ratio: float) -> tuple[bool, str]:
    """Click the ALAS scheduler Start button by UI name or calibrated window coordinates."""
    if os.name != "nt":
        return False, "ALAS GUI 备用点击只支持 Windows"
    try:
        import win32con
        import win32gui
        import win32process
    except ImportError as exc:
        return False, f"缺少 Windows GUI 自动化组件：{exc}"
    candidates = []

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd).strip()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if title.casefold() == "alas" or pid == root_pid:
            rect = win32gui.GetWindowRect(hwnd)
            area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
            if area > 10000:
                candidates.append((10 if title.casefold() == "alas" else 1, area, hwnd, title, rect))

    win32gui.EnumWindows(collect, None)
    if not candidates:
        return False, "ALAS 未产生可见窗口，无法点击“启动”"
    _, _, hwnd, _, rect = max(candidates)
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    try:
        from pywinauto import Application
        window = Application(backend="uia").connect(handle=hwnd, timeout=3).window(handle=hwnd)
        for control in window.descendants(control_type="Button"):
            name = control.window_text().strip()
            if name in ("启动", "Start"):
                control.click_input()
                return True, f"已通过 GUI 控件点击 ALAS“{name}”按钮"
    except Exception:
        pass
    left, top, right, bottom = rect
    x = left + round((right - left) * min(max(x_ratio, 0.0), 1.0))
    y = top + round((bottom - top) * min(max(y_ratio, 0.0), 1.0))
    try:
        import pyautogui
        pyautogui.click(x, y)
        return True, f"已在 ALAS 窗口点击“启动”按钮（{x}, {y}）"
    except Exception as exc:
        return False, f"ALAS GUI 备用点击失败：{exc}"


def _click_maaend_start_button(root_pid: int, x_ratio: float, y_ratio: float) -> tuple[bool, str]:
    """Click MaaEnd's Start Tasks button by UI name or calibrated coordinates."""
    if os.name != "nt":
        return False, "MaaEnd GUI 备用点击只支持 Windows"
    try:
        import win32con
        import win32gui
        import win32process
    except ImportError as exc:
        return False, f"缺少 Windows GUI 自动化组件：{exc}"
    candidates = []

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd).strip()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == root_pid or "MAAEND" in title.upper():
            rect = win32gui.GetWindowRect(hwnd)
            area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
            if area > 10000:
                candidates.append((10 if "MAAEND" in title.upper() else 1,
                                   area, hwnd, title, rect))

    win32gui.EnumWindows(collect, None)
    if not candidates:
        return False, "MaaEnd 未产生可见窗口，无法点击“开始任务”"
    _, _, hwnd, title, rect = max(candidates)
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    try:
        from pywinauto import Application
        window = Application(backend="uia").connect(handle=hwnd, timeout=3).window(handle=hwnd)
        for control in window.descendants(control_type="Button"):
            name = control.window_text().strip()
            if name in ("开始任务", "启动任务", "开始", "Start Tasks"):
                control.click_input()
                return True, f"已通过 GUI 控件点击 MaaEnd“{name}”按钮"
    except Exception:
        pass
    left, top, right, bottom = rect
    x = left + round((right - left) * min(max(x_ratio, 0.0), 1.0))
    y = top + round((bottom - top) * min(max(y_ratio, 0.0), 1.0))
    try:
        import pyautogui
        pyautogui.click(x, y)
        return True, f"已在“{title}”窗口点击“开始任务”（{x}, {y}）"
    except Exception as exc:
        return False, f"MaaEnd GUI 备用点击失败：{exc}"


def _click_named_gui_button(root_pid: int, process_images: list[str], title_hints: list[str],
                            button_names: list[str], x_ratio: float,
                            y_ratio: float) -> tuple[bool, str]:
    """Click a named desktop button, with a calibrated window-relative fallback."""
    if os.name != "nt":
        return False, "GUI 点击只支持 Windows"
    try:
        import psutil
        import win32con
        import win32gui
        import win32process
    except ImportError as exc:
        return False, f"缺少 Windows GUI 自动化组件：{exc}"
    pids = {root_pid}
    expected_images = {name.casefold() for name in process_images}
    try:
        pids.update(child.pid for child in psutil.Process(root_pid).children(recursive=True))
    except psutil.Error:
        pass
    for process in psutil.process_iter(["pid", "name"]):
        try:
            if (process.info.get("name") or "").casefold() in expected_images:
                pids.add(int(process.info["pid"]))
        except (psutil.Error, TypeError, ValueError):
            continue
    folded_hints = [hint.casefold() for hint in title_hints]
    candidates = []

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd).strip()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        title_match = any(hint in title.casefold() for hint in folded_hints)
        if pid in pids or title_match:
            rect = win32gui.GetWindowRect(hwnd)
            area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
            if area > 10000:
                candidates.append((10 if title_match else 1, area, hwnd, title, rect))

    win32gui.EnumWindows(collect, None)
    if not candidates:
        return False, "脚本尚未产生可见窗口"
    _, _, hwnd, title, rect = max(candidates)
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    folded_names = [name.casefold() for name in button_names]
    try:
        from pywinauto import Application
        window = Application(backend="uia").connect(handle=hwnd, timeout=3).window(handle=hwnd)
        for control in window.descendants(control_type="Button"):
            name = control.window_text().strip()
            if name and any(target == name.casefold() or target in name.casefold()
                            for target in folded_names):
                control.click_input()
                return True, f"已通过 GUI 控件点击“{name}”"
    except Exception:
        pass
    left, top, right, bottom = rect
    x = left + round((right - left) * min(max(x_ratio, 0.0), 1.0))
    y = top + round((bottom - top) * min(max(y_ratio, 0.0), 1.0))
    try:
        import pyautogui
        pyautogui.click(x, y)
        return True, f"已在“{title}”窗口点击启动位置（{x}, {y}）"
    except Exception as exc:
        return False, f"GUI 点击失败：{exc}"


def _bring_game_window_to_front(process_image: str, title_contains: str,
                                timeout: float, stop_event: threading.Event) -> Result:
    """Wait for a game window, restore it, and raise it without leaving it topmost."""
    if os.name != "nt":
        return Result(False, "游戏窗口置前只支持 Windows")
    process_image = process_image.strip().casefold()
    title_contains = title_contains.strip().casefold()
    if not process_image and not title_contains:
        return Result(False, "未配置游戏进程名或窗口标题")
    try:
        import psutil
        import win32con
        import win32gui
        import win32process
    except ImportError as exc:
        return Result(False, f"缺少 Windows 窗口控制组件：{exc}")

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        matching_pids = set()
        if process_image:
            for process in psutil.process_iter(["pid", "name"]):
                try:
                    if (process.info.get("name") or "").casefold() == process_image:
                        matching_pids.add(process.info["pid"])
                except psutil.Error:
                    continue
        candidates = []

        def collect(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd).strip()
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if process_image:
                if pid not in matching_pids:
                    return
            elif not title_contains or title_contains not in title.casefold():
                return
            rect = win32gui.GetWindowRect(hwnd)
            area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
            if area > 10000:
                candidates.append((area, hwnd, title))

        win32gui.EnumWindows(collect, None)
        if candidates:
            _, hwnd, title = max(candidates)
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW
                win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
                win32gui.BringWindowToTop(hwnd)
                win32gui.SetForegroundWindow(hwnd)
                time.sleep(0.15)
                win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
                return Result(True, f"已恢复并置前“{title or process_image}”")
            except Exception as exc:
                return Result(False, f"找到游戏窗口但置前失败：{exc}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return Result(False, f"等待游戏窗口超时：{process_image or title_contains}")
        if stop_event.wait(min(0.5, remaining)):
            return Result(False, "任务被用户停止")


def run_log_gui_daily(step: dict[str, Any], ctx: RunContext) -> Result:
    """Run a desktop daily assistant, retry its start button, and follow fresh logs."""
    display_name = str(step.get("display_name", "每日脚本"))
    exe = Path(expand(str(step.get("executable", ""))))
    if not exe.exists():
        return Result(False, f"找不到 {display_name}：{exe}")
    log_glob = expand(str(step.get("log_glob", "")))
    process_images = [str(x) for x in step.get("process_images", [exe.name])]
    cleanup_images = [str(x) for x in step.get("cleanup_process_images", process_images)]
    worker_images = [str(x) for x in step.get("completion_on_worker_exit", [])]
    if os.name == "nt" and step.get("clean_existing", True):
        for image_name in dict.fromkeys(cleanup_images):
            subprocess.run(["taskkill", "/IM", image_name, "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(0.5)
    existing = {}
    for name in glob.glob(log_glob):
        try:
            existing[name] = Path(name).stat().st_size
        except OSError:
            continue
    try:
        proc = subprocess.Popen([str(exe)], cwd=str(exe.parent),
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError as exc:
        return Result(False, f"无法启动 {display_name}：{exc}")

    timeout = float(step.get("timeout", 10800))
    retry_interval = max(0.05, float(step.get("log_retry_interval", 30)))
    start_markers = [str(x) for x in step.get("start_markers", [])]
    completion_markers = [str(x) for x in step.get("completion_markers", [])]
    error_markers = [str(x) for x in step.get("error_markers", [])]
    screenshot_markers = [str(x) for x in step.get("screenshot_markers", [])]
    screenshot_path = expand(str(step.get("screenshot_path", "")))
    game_process = str(step.get("game_process_image", ""))
    game_title = str(step.get("game_title_contains", ""))
    title_hints = [str(x) for x in step.get("title_hints", [])]
    button_names = [str(x) for x in step.get("button_names", [])]
    started_at = time.monotonic()
    next_click_at = started_at + max(0.0, float(step.get("initial_click_delay", retry_interval)))
    run_started = False
    completion_at = None
    completion_log = None
    click_count = 0
    screenshot_saved = False
    screenshot_evidence_seen = False
    game_foregrounded = False
    game_foreground_attempted = False
    worker_seen = False
    worker_exit_at = None
    quiet_seconds = float(step.get("completion_quiet_seconds", 3))
    state_pattern_text = str(step.get("state_watchdog_regex", "")).strip()
    state_pattern = re.compile(state_pattern_text) if state_pattern_text else None
    state_stall_seconds = float(step.get("state_stall_seconds", 0))
    state_recovery_seconds = float(step.get("state_recovery_seconds", 120))
    last_state_key = None
    last_state_change_at = time.monotonic()
    state_recovery_at = None
    ctx.log(f"{display_name}已打开；日志每沉默 {retry_interval:g} 秒便重新点击启动")

    def capture_reward(force: bool = False) -> None:
        nonlocal screenshot_saved
        if (screenshot_saved and not force) or not screenshot_path or not game_process:
            return
        result = run_window_screenshot({"process_image": game_process,
                                        "title_contains": game_title,
                                        "path": screenshot_path,
                                        "wait_seconds": 0}, ctx)
        ctx.log(result.message)
        screenshot_saved = result.success

    def failure(message: str, **details: Any) -> Result:
        capture_reward(force=True)
        details.setdefault("screenshot", screenshot_path if screenshot_saved else None)
        return Result(False, message, details)

    try:
        while time.monotonic() - started_at <= timeout:
            if ctx.stop_event.wait(min(1.0, retry_interval)):
                return failure("任务被用户停止")
            for name in sorted(glob.glob(log_glob)):
                path = Path(name)
                position = existing.get(name, 0)
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size < position:
                    position = 0
                if size <= position:
                    continue
                try:
                    with path.open("rb") as handle:
                        handle.seek(position)
                        content = handle.read().decode("utf-8", errors="replace")
                except OSError:
                    continue
                existing[name] = size
                if run_started or click_count > 0:
                    next_click_at = time.monotonic() + retry_interval
                update_marker = _find_update_marker(content, step)
                if update_marker:
                    capture_reward(force=True)
                    return _needs_update(display_name, update_marker, log=name)
                if any(marker in content for marker in start_markers):
                    if not run_started:
                        ctx.log(f"{display_name}日志已更新，进入等待完成状态")
                    run_started = True
                    if step.get("bring_game_to_front", False) and not game_foreground_attempted:
                        game_foreground_attempted = True
                        foreground = _bring_game_window_to_front(
                            game_process, game_title,
                            float(step.get("game_foreground_timeout", 180)),
                            ctx.stop_event)
                        ctx.log(f"{display_name}游戏窗口置前：{foreground.message}")
                        game_foregrounded = foreground.success
                if any(marker in content for marker in screenshot_markers):
                    screenshot_evidence_seen = True
                    capture_reward()
                if state_pattern is not None:
                    for line in content.splitlines():
                        state_match = state_pattern.search(line)
                        if not state_match:
                            continue
                        state_key = " | ".join(part.strip() for part in state_match.groups())
                        if state_key != last_state_key:
                            last_state_key = state_key
                            last_state_change_at = time.monotonic()
                            state_recovery_at = None
                for marker in error_markers:
                    if run_started and marker in content:
                        return failure(f"{display_name}异常结束：{marker}",
                                       log=name, marker=marker)
                for marker in completion_markers:
                    if run_started and marker in content:
                        completion_at = time.monotonic()
                        completion_log = name
                        ctx.log(f"{display_name}检测到完成日志，观察 {quiet_seconds:g} 秒")
            if completion_at is not None and time.monotonic() - completion_at >= quiet_seconds:
                if not screenshot_saved and step.get("screenshot_at_completion", True):
                    capture_reward()
                return Result(True, f"{display_name}正常结束",
                              {"log": completion_log, "start_clicks": click_count,
                               "screenshot": screenshot_path if screenshot_saved else None})
            if worker_images:
                worker_running = _process_image_exists(worker_images)
                worker_seen = worker_seen or worker_running
                if run_started and worker_seen and not worker_running and completion_at is None:
                    if (step.get("worker_exit_requires_evidence", False)
                            and not screenshot_evidence_seen):
                        if worker_exit_at is None:
                            worker_exit_at = time.monotonic()
                            ctx.log(f"{display_name}工作进程已退出，等待奖励完成日志落盘")
                        elif time.monotonic() - worker_exit_at >= float(
                                step.get("worker_exit_log_grace", 10)):
                            return failure(f"{display_name}工作进程退出，但未检测到奖励完成日志")
                    else:
                        completion_at = time.monotonic()
                        completion_log = "worker_process_exit"
                        ctx.log(f"{display_name}工作进程已退出，观察 {quiet_seconds:g} 秒确认结束")
            if (run_started and state_pattern is not None and last_state_key
                    and state_stall_seconds > 0
                    and time.monotonic() - last_state_change_at >= state_stall_seconds):
                if state_recovery_at is None:
                    state_recovery_at = time.monotonic()
                    foreground = _bring_game_window_to_front(
                        game_process, game_title,
                        float(step.get("game_foreground_timeout", 180)), ctx.stop_event)
                    ctx.log(f"{display_name}状态“{last_state_key}”持续"
                            f" {state_stall_seconds:g} 秒未变化；重新置前游戏：{foreground.message}")
                elif time.monotonic() - state_recovery_at >= state_recovery_seconds:
                    return failure(f"{display_name}卡在“{last_state_key}”超过"
                                   f" {state_stall_seconds + state_recovery_seconds:g} 秒",
                                   stalled_state=last_state_key)
            if not run_started and time.monotonic() >= next_click_at:
                click_count += 1
                clicked, message = _click_named_gui_button(
                    proc.pid, process_images, title_hints, button_names,
                    float(step.get("gui_click_x_ratio", 0.5)),
                    float(step.get("gui_click_y_ratio", 0.9)))
                ctx.log(f"{display_name}第 {click_count} 次点击启动：{message}")
                next_click_at = time.monotonic() + retry_interval
                if not clicked:
                    continue
            if proc.poll() is not None and not _process_image_exists(process_images):
                return failure(f"{display_name}提前退出（退出码 {proc.returncode}）")
        return failure(f"等待 {display_name}完成超时（{timeout:g} 秒）")
    finally:
        if step.get("close_on_complete", True):
            if os.name == "nt":
                for image_name in dict.fromkeys(cleanup_images):
                    subprocess.run(["taskkill", "/IM", image_name, "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
            elif proc.poll() is None:
                proc.terminate()


def run_maaend_gui(step: dict[str, Any], ctx: RunContext) -> Result:
    """Start MaaEnd's full daily preset and wait for all primary tasks to finish."""
    exe = Path(expand(str(step.get("executable") or ctx.tool("maaend_gui"))))
    if not exe.exists():
        return Result(False, f"找不到 MaaEnd：{exe}")
    log_glob = expand(str(step.get("log_glob") or exe.parent / "debug" / "20??-??-??-*.log"))
    process_image = str(step.get("process_image", exe.name))
    if os.name == "nt" and step.get("clean_existing", True):
        subprocess.run(["taskkill", "/IM", process_image, "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(0.5)
    existing = {name: Path(name).stat().st_size for name in glob.glob(log_glob)}
    try:
        proc = subprocess.Popen([str(exe)], cwd=str(exe.parent),
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError as exc:
        return Result(False, f"无法启动 MaaEnd：{exc}")

    timeout = int(step.get("timeout", 7200))
    start_markers = [str(x) for x in step.get("start_markers", [
        "手动启动：激活配置并启动任务: 全套日常",
        "实例 全套日常: 开始执行任务",
    ])]
    completion_marker = str(step.get(
        "completion_marker", "实例 全套日常: 收尾段切换为 Dummy Controller"))
    final_submission_marker = str(step.get(
        "final_submission_marker", "实例 全套日常: 任务已提交, task_ids:"))
    error_markers = [str(x) for x in step.get(
        "error_markers", ["实例 全套日常: 连接失败", "实例 全套日常: 任务执行失败",
                          "实例 全套日常: 执行异常", "实例 全套日常: 任务失败"])]
    fallback_after = float(step.get("gui_fallback_after", 45))
    started_at = time.monotonic()
    run_started = False
    fallback_clicked = False
    expected_total = None
    expected_primary = None
    completion_seen = False
    game_foregrounded = False
    game_foreground_attempted = False
    screenshot_saved = False
    game_process = str(step.get("game_process_image", ""))
    game_title = str(step.get("game_title_contains", ""))
    screenshot_path = expand(str(step.get("screenshot_path", "")))
    framework_log_value = expand(str(step.get("framework_log_path", "")))
    framework_log = Path(framework_log_value) if framework_log_value else None
    framework_position = (framework_log.stat().st_size
                          if framework_log is not None and framework_log.exists() else 0)
    framework_succeeded: set[int] = set()
    framework_failed: dict[int, str] = {}
    submitted_ids: list[int] = []
    last_primary_terminal_at = time.monotonic()
    framework_stall_seconds = float(step.get("framework_stall_seconds", 1800))
    rotated_glob = expand(str(step.get(
        "framework_rotated_glob",
        framework_log.parent / "maafw.bak.*.log" if framework_log is not None else "")))
    known_rotated_logs = set(glob.glob(rotated_glob)) if rotated_glob else set()

    def record_framework_events(content: str) -> None:
        nonlocal last_primary_terminal_at
        for line in content.splitlines():
            terminal = re.search(
                r'msg=Tasker\.Task\.(Succeeded|Failed).*?"entry":"([^"]+)".*?"task_id":(\d+)',
                line)
            if not terminal:
                continue
            outcome, entry, task_id_text = terminal.groups()
            task_id = int(task_id_text)
            if outcome == "Succeeded":
                if task_id not in framework_succeeded:
                    ctx.log(f"终末地底层确认任务成功：{entry}（{task_id}）")
                framework_succeeded.add(task_id)
            else:
                if task_id not in framework_failed:
                    ctx.log(f"终末地底层确认任务失败：{entry}（{task_id}）")
                framework_failed[task_id] = entry
            last_primary_terminal_at = time.monotonic()

    def capture_endfield(force: bool = False) -> None:
        nonlocal screenshot_saved
        if (screenshot_saved and not force) or not screenshot_path or not game_process:
            return
        screenshot = run_window_screenshot({
            "process_image": game_process,
            "title_contains": game_title,
            "path": screenshot_path,
            "wait_seconds": float(step.get("screenshot_wait_seconds", 0)),
        }, ctx)
        ctx.log(screenshot.message)
        screenshot_saved = screenshot.success
    ctx.log("MaaEnd 已启动；等待自动运行“全套日常”，必要时点击“开始任务”")
    try:
        while time.monotonic() - started_at <= timeout:
            if ctx.stop_event.wait(1):
                return Result(False, "任务被用户停止")
            for name in sorted(glob.glob(log_glob)):
                path = Path(name)
                position = existing.get(name, 0)
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size < position:
                    position = 0
                if size <= position:
                    continue
                try:
                    with path.open("rb") as handle:
                        handle.seek(position)
                        content = handle.read().decode("utf-8", errors="replace")
                except OSError:
                    continue
                existing[name] = size
                update_marker = _find_update_marker(content, step)
                if update_marker:
                    capture_endfield(force=True)
                    return _needs_update("MaaEnd 或终末地", update_marker, log=name)
                for line in content.splitlines():
                    if any(marker in line for marker in start_markers):
                        run_started = True
                        ctx.log("已确认 MaaEnd“全套日常”开始执行")
                        if step.get("bring_game_to_front", False) and not game_foreground_attempted:
                            game_foreground_attempted = True
                            foreground = _bring_game_window_to_front(
                                game_process, game_title,
                                float(step.get("game_foreground_timeout", 180)),
                                ctx.stop_event)
                            ctx.log(f"终末地游戏窗口置前：{foreground.message}")
                            game_foregrounded = foreground.success
                    task_plan = re.search(
                        r"实例 全套日常: 开始执行任务, 数量:\s*(\d+),\s*分段:\s*primary:(\d+),\s*trailing:(\d+)",
                        line)
                    if task_plan:
                        expected_total = int(task_plan.group(1))
                        expected_primary = int(task_plan.group(2))
                        trailing = int(task_plan.group(3))
                        ctx.log(f"终末地本轮规定任务：主要 {expected_primary} 项，"
                                f"收尾 {trailing} 项，共 {expected_total} 项")
                    for error in error_markers:
                        if error in line:
                            capture_endfield(force=True)
                            return Result(False, f"终末地每日任务异常结束：{error}",
                                          {"log": name, "marker": error})
                    if completion_marker in line:
                        if run_started and expected_total is not None:
                            completion_seen = True
                            ctx.log("终末地主要任务执行完毕，等待日志确认全部规定任务已提交")
                            capture_endfield()
                        else:
                            ctx.log("忽略缺少本轮任务计划的 MaaEnd 收尾记录")
                    if final_submission_marker in line or "前段任务已提交, task_ids:" in line:
                        ids_match = re.search(r"task_ids:\s*\[([^\]]*)\]", line)
                        if ids_match:
                            parsed_ids = [int(x.strip()) for x in ids_match.group(1).split(",")
                                          if x.strip().isdigit()]
                            if len(parsed_ids) >= len(submitted_ids):
                                submitted_ids = parsed_ids
            if framework_log is not None and framework_log.exists():
                try:
                    framework_size = framework_log.stat().st_size
                    if framework_size < framework_position:
                        framework_position = 0
                    if framework_size > framework_position:
                        with framework_log.open("rb") as handle:
                            handle.seek(framework_position)
                            framework_chunk = handle.read().decode("utf-8", errors="replace")
                        framework_position = framework_size
                        record_framework_events(framework_chunk)
                except OSError:
                    pass
            if rotated_glob:
                for rotated_name in set(glob.glob(rotated_glob)) - known_rotated_logs:
                    known_rotated_logs.add(rotated_name)
                    try:
                        rotated_path = Path(rotated_name)
                        tail_bytes = max(1024, int(step.get("framework_rotated_tail_bytes", 2_000_000)))
                        with rotated_path.open("rb") as handle:
                            handle.seek(max(0, rotated_path.stat().st_size - tail_bytes))
                            record_framework_events(handle.read().decode("utf-8", errors="replace"))
                    except OSError:
                        continue

            primary_ids = (submitted_ids[:expected_primary]
                           if expected_primary is not None else [])
            failed_primary = [task_id for task_id in primary_ids if task_id in framework_failed]
            if failed_primary:
                capture_endfield(force=True)
                task_id = failed_primary[0]
                return Result(False, f"终末地规定任务失败：{framework_failed[task_id]}",
                              {"task_id": task_id, "framework_log": str(framework_log),
                               "screenshot": screenshot_path if screenshot_saved else None})
            pending_primary = [task_id for task_id in primary_ids
                               if task_id not in framework_succeeded]
            if (primary_ids and pending_primary and framework_stall_seconds > 0
                    and time.monotonic() - last_primary_terminal_at >= framework_stall_seconds):
                capture_endfield(force=True)
                return Result(False, f"终末地规定任务 {pending_primary[0]} 超过"
                              f" {framework_stall_seconds:g} 秒没有完成结果",
                              {"pending_primary": pending_primary,
                               "framework_log": str(framework_log),
                               "screenshot": screenshot_path if screenshot_saved else None})
            if (completion_seen and expected_total is not None
                    and len(submitted_ids) >= expected_total):
                if framework_log is not None and primary_ids:
                    if all(task_id in framework_succeeded for task_id in primary_ids):
                        return Result(True, "终末地每日任务正常结束",
                                      {"marker": final_submission_marker,
                                       "expected_total": expected_total,
                                       "expected_primary": expected_primary,
                                       "submitted_total": len(submitted_ids),
                                       "succeeded_primary": sorted(framework_succeeded.intersection(primary_ids)),
                                       "game_foregrounded": game_foregrounded,
                                       "screenshot": screenshot_path if screenshot_saved else None})
                elif framework_log is None:
                    return Result(True, "终末地每日任务正常结束",
                                  {"marker": final_submission_marker,
                                   "expected_total": expected_total,
                                   "expected_primary": expected_primary,
                                   "submitted_total": len(submitted_ids),
                                   "game_foregrounded": game_foregrounded,
                                   "screenshot": screenshot_path if screenshot_saved else None})
            if (step.get("gui_click_fallback", True) and not fallback_clicked and not run_started
                    and time.monotonic() - started_at >= fallback_after):
                fallback_clicked = True
                clicked, message = _click_maaend_start_button(
                    proc.pid, float(step.get("gui_click_x_ratio", 0.682)),
                    float(step.get("gui_click_y_ratio", 0.971)))
                ctx.log(message)
                if not clicked:
                    capture_endfield(force=True)
                    return Result(False, message)
            if proc.poll() is not None and not _process_image_exists([process_image]):
                capture_endfield(force=True)
                return Result(False, f"MaaEnd 提前退出（退出码 {proc.returncode}）")
        capture_endfield(force=True)
        return Result(False, f"等待 MaaEnd 全套日常完成超时（{timeout} 秒）")
    finally:
        if step.get("close_on_complete", True):
            if os.name == "nt":
                subprocess.run(["taskkill", "/IM", process_image, "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            elif proc.poll() is None:
                proc.terminate()


def run_window_screenshot(step: dict[str, Any], ctx: RunContext) -> Result:
    """Save a visible Windows game window before cleanup closes it."""
    if os.name != "nt":
        return Result(False, "窗口截图只支持 Windows")
    process_image = str(step.get("process_image", "")).strip().casefold()
    title_contains = str(step.get("title_contains", "")).strip().casefold()
    output = Path(expand(str(step.get("path", ctx.root / "logs" / "window.png"))))
    wait_seconds = float(step.get("wait_seconds", 1))
    try:
        import psutil
        import win32con
        import win32gui
        import win32process
        from PIL import ImageGrab
    except ImportError as exc:
        return Result(False, f"缺少窗口截图组件：{exc}")
    matching_pids = set()
    for process in psutil.process_iter(["pid", "name"]):
        try:
            name = (process.info.get("name") or "").casefold()
            if process_image and name == process_image:
                matching_pids.add(process.info["pid"])
        except psutil.Error:
            continue
    candidates = []

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd).strip()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if process_image:
            if pid not in matching_pids:
                return
        elif not title_contains or title_contains not in title.casefold():
            return
        rect = win32gui.GetWindowRect(hwnd)
        area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
        if area > 10000:
            candidates.append((area, hwnd, title, rect))

    win32gui.EnumWindows(collect, None)
    if not candidates:
        return Result(False, f"未找到可截图的游戏窗口：{process_image or title_contains}")
    _, hwnd, title, rect = max(candidates)
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    if ctx.stop_event.wait(wait_seconds):
        return Result(False, "任务被用户停止")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        image = ImageGrab.grab(bbox=rect, all_screens=True)
        image.save(output)
    except Exception as exc:
        return Result(False, f"保存游戏窗口截图失败：{exc}")
    return Result(True, f"已保存“{title or process_image}”最终截图", {"path": str(output)})


def _click_gumballs_start_button(root_pid: int, x_ratio: float, y_ratio: float) -> tuple[bool, str]:
    """Click MFAAvalonia's Start Task button using its calibrated window-relative position."""
    if os.name != "nt":
        return False, "不思议迷宫 GUI 点击只支持 Windows"
    try:
        import win32con
        import win32gui
        import win32process
    except ImportError as exc:
        return False, f"缺少 Windows GUI 自动化组件：{exc}"
    candidates = []

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid != root_pid:
            return
        rect = win32gui.GetWindowRect(hwnd)
        area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
        if area > 10000:
            candidates.append((area, hwnd, win32gui.GetWindowText(hwnd).strip(), rect))

    win32gui.EnumWindows(collect, None)
    if not candidates:
        return False, "不思议迷宫脚本未产生可见窗口，无法点击“开始任务”"
    _, hwnd, title, rect = max(candidates)
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    left, top, right, bottom = rect
    x = left + round((right - left) * min(max(x_ratio, 0.0), 1.0))
    y = top + round((bottom - top) * min(max(y_ratio, 0.0), 1.0))
    try:
        import pyautogui
        pyautogui.click(x, y)
        return True, f"已在“{title}”窗口点击“开始任务”（{x}, {y}）"
    except Exception as exc:
        return False, f"点击不思议迷宫“开始任务”失败：{exc}"


def run_gumballs_gui(step: dict[str, Any], ctx: RunContext) -> Result:
    """Run MFAAvalonia twice, manually clicking Start after the configured initial delay."""
    exe = Path(expand(str(step.get("executable") or ctx.tool("gumballs_gui"))))
    if not exe.exists():
        return Result(False, f"找不到不思议迷宫脚本：{exe}")
    log_glob = expand(str(step.get("log_glob") or exe.parent / "logs" / "log-*.log"))
    process_image = str(step.get("process_image", exe.name))
    if os.name == "nt" and step.get("clean_existing", True):
        subprocess.run(["taskkill", "/IM", process_image, "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(0.5)
    existing = {name: Path(name).stat().st_size for name in glob.glob(log_glob)}
    try:
        proc = subprocess.Popen([str(exe)], cwd=str(exe.parent),
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError as exc:
        return Result(False, f"无法启动不思议迷宫脚本：{exc}")

    timeout = int(step.get("timeout", 14400))
    initial_delay = float(step.get("initial_click_delay", 120))
    round_start_timeout = float(step.get("round_start_timeout", 180))
    between_round_delay = float(step.get("between_round_delay", 3))
    start_marker = str(step.get("start_marker", "用户操作：启动任务"))
    completion_marker = str(step.get("completion_marker", "任务已全部完成！"))
    started_at = time.monotonic()
    round_number = 0
    round_started = False
    click_at = None
    completion_logs = []
    skipped_tasks = []
    disabled_tasks = []
    business_silence_timeout = float(step.get("business_silence_timeout", 600))
    heartbeat_markers = [str(x) for x in step.get(
        "heartbeat_markers", ["内存管理", "MemoryManager", "memory cleanup"])]
    last_business_at = time.monotonic()
    last_business_line = "尚无业务日志"
    stalled_retries = 0
    max_stalled_retries = max(0, int(step.get("max_stalled_retries", 1)))

    def click_start(number: int) -> Result:
        clicked, message = _click_gumballs_start_button(
            proc.pid, float(step.get("gui_click_x_ratio", 0.963)),
            float(step.get("gui_click_y_ratio", 0.136)))
        ctx.log(message)
        return Result(clicked, message, {"round": number})

    ctx.log(f"不思议迷宫脚本已启动，将在 {initial_delay:g} 秒后点击第一轮“开始任务”")
    try:
        if ctx.stop_event.wait(initial_delay):
            return Result(False, "任务被用户停止")
        first = click_start(1)
        if not first.success:
            return first
        round_number = 1
        click_at = time.monotonic()

        while time.monotonic() - started_at <= timeout:
            if ctx.stop_event.wait(1):
                return Result(False, "任务被用户停止")
            for name in sorted(glob.glob(log_glob)):
                path = Path(name)
                position = existing.get(name, 0)
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size < position:
                    position = 0
                if size <= position:
                    continue
                try:
                    with path.open("rb") as handle:
                        handle.seek(position)
                        content = handle.read().decode("utf-8", errors="replace")
                except OSError:
                    continue
                existing[name] = size
                update_marker = _find_update_marker(content, step)
                if update_marker:
                    return _needs_update("不思议迷宫脚本或游戏", update_marker, log=name)
                for line in content.splitlines():
                    stripped = line.strip()
                    if stripped and not any(marker.casefold() in stripped.casefold()
                                            for marker in heartbeat_markers):
                        last_business_at = time.monotonic()
                        last_business_line = stripped[-500:]
                    skipped = re.search(
                        r"任务[：:]\s*([^\s]+)\s+(识别失败|已禁用)[,，]\s*跳过该任务", line)
                    if skipped and skipped.group(1) not in skipped_tasks:
                        skipped_tasks.append(skipped.group(1))
                        if skipped.group(2) == "已禁用":
                            disabled_tasks.append(skipped.group(1))
                            ctx.log(f"不思议迷宫配置：任务“{skipped.group(1)}”已禁用并跳过")
                        else:
                            ctx.log(f"不思议迷宫脚本警告：任务“{skipped.group(1)}”识别失败并被跳过")
                    if start_marker in line:
                        if not round_started:
                            round_started = True
                            ctx.log(f"已确认不思议迷宫第 {round_number} 轮开始")
                    if completion_marker in line and round_started:
                        completion_logs.append(name)
                        ctx.log(f"已确认不思议迷宫第 {round_number} 轮完成")
                        if round_number >= 2:
                            return Result(True, "不思议迷宫两轮任务均已完成",
                                          {"rounds": 2, "logs": completion_logs,
                                            "completion_marker": completion_marker,
                                            "skipped_tasks": skipped_tasks,
                                            "disabled_tasks": disabled_tasks,
                                            "stalled_retries": stalled_retries})
                        if ctx.stop_event.wait(between_round_delay):
                            return Result(False, "任务被用户停止")
                        second = click_start(2)
                        if not second.success:
                            return second
                        round_number = 2
                        round_started = False
                        click_at = time.monotonic()
                        last_business_at = time.monotonic()
            if (not round_started and click_at is not None and
                    time.monotonic() - click_at > round_start_timeout):
                return Result(False, f"点击后 {round_start_timeout:g} 秒内未检测到第 {round_number} 轮启动日志")
            if (round_started and business_silence_timeout > 0
                    and time.monotonic() - last_business_at >= business_silence_timeout):
                if round_number == 2 and stalled_retries < max_stalled_retries:
                    stalled_retries += 1
                    ctx.log(f"不思议迷宫第2轮在“{last_business_line}”后"
                            f" {business_silence_timeout:g} 秒无业务进展；"
                            f"重启脚本并重试第2轮（{stalled_retries}/{max_stalled_retries}）")
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/IM", process_image, "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                    elif proc.poll() is None:
                        proc.terminate()
                    if ctx.stop_event.wait(float(step.get("stalled_restart_delay", 5))):
                        return Result(False, "任务被用户停止")
                    try:
                        proc = subprocess.Popen(
                            [str(exe)], cwd=str(exe.parent),
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    except OSError as exc:
                        return Result(False, f"重启不思议迷宫脚本失败：{exc}")
                    if ctx.stop_event.wait(float(step.get("stalled_relaunch_wait", 5))):
                        return Result(False, "任务被用户停止")
                    second = click_start(2)
                    if not second.success:
                        return second
                    round_started = False
                    click_at = time.monotonic()
                    last_business_at = time.monotonic()
                    last_business_line = "重试第2轮启动"
                else:
                    return Result(False, f"不思议迷宫第 {round_number} 轮卡在“{last_business_line}”后"
                                  f" {business_silence_timeout:g} 秒无业务进展",
                                  {"round": round_number, "last_business_line": last_business_line,
                                   "stalled_retries": stalled_retries,
                                   "skipped_tasks": skipped_tasks,
                                   "disabled_tasks": disabled_tasks})
            if proc.poll() is not None and not _process_image_exists([process_image]):
                return Result(False, f"不思议迷宫脚本提前退出（退出码 {proc.returncode}）")
        return Result(False, f"等待不思议迷宫两轮任务完成超时（{timeout} 秒）")
    finally:
        if step.get("close_on_complete", True):
            if os.name == "nt":
                subprocess.run(["taskkill", "/IM", process_image, "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            elif proc.poll() is None:
                proc.terminate()


def run_ldplayer(step: dict[str, Any], ctx: RunContext) -> Result:
    exe, index = ctx.tool("ldconsole"), str(step.get("instance", 0))
    action = step.get("action", "launch")
    mapping = {"launch": ["launch", "--index", index], "quit": ["quit", "--index", index],
               "reboot": ["reboot", "--index", index]}
    if action not in mapping:
        return Result(False, f"不支持的雷电动作：{action}")
    result = ctx.command([exe] + mapping[action], int(step.get("timeout", 120)))
    if result.success and action in ("launch", "reboot"):
        wait = int(step.get("settle_seconds", 5))
        end = time.monotonic() + wait
        while time.monotonic() < end:
            if ctx.stop_event.wait(0.2):
                return Result(False, "任务被用户停止")
    return result


def run_mumu_wait(step: dict[str, Any], ctx: RunContext) -> Result:
    """Launch a MuMu 12 instance and wait until Android is actually ready."""
    manager = Path(expand(str(step.get("executable", ""))))
    if not manager.exists():
        return Result(False, f"找不到 MuMuManager：{manager}")
    index = str(step.get("instance", 0))
    device = str(step.get("device", "127.0.0.1:16384"))
    adb_executable = Path(expand(str(step.get("adb_executable") or manager.parent / "adb.exe")))
    timeout = float(step.get("timeout", 180))
    poll_seconds = max(0.2, float(step.get("poll_seconds", 2)))
    adb_env = os.environ.copy()
    if step.get("adb_server_port") is not None:
        adb_env["ANDROID_ADB_SERVER_PORT"] = str(step["adb_server_port"])

    def query_info() -> dict[str, Any] | None:
        try:
            done = subprocess.run(
                [str(manager), "info", "--vmindex", index],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=min(20, max(1, int(timeout))),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if done.returncode != 0:
            return None
        output = _decode_process_output(done.stdout).strip()
        try:
            value = json.loads(output)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None

    info = query_info()
    if not info or not info.get("is_android_started"):
        launched = ctx.command(
            [str(manager), "control", "--vmindex", index, "launch"],
            min(120, max(1, int(timeout))))
        if not launched.success:
            return Result(False, f"启动 MuMu 实例 {index} 失败：{launched.message}", launched.details)

    started_at = time.monotonic()
    while time.monotonic() - started_at <= timeout:
        if ctx.stop_event.is_set():
            return Result(False, "任务被用户停止")
        info = query_info()
        if info and info.get("is_android_started"):
            if not adb_executable.exists():
                return Result(False, f"找不到 MuMu ADB：{adb_executable}")
            direct_connect = ctx.command(
                [str(adb_executable), "connect", device], 15, env=adb_env)
            if not direct_connect.success:
                ctx.log("MuMu Android 已启动，但独立 ADB 通道尚未附着；继续等待")
                if ctx.stop_event.wait(poll_seconds):
                    return Result(False, "任务被用户停止")
                continue
            boot = ctx.command(
                [str(adb_executable), "-s", device, "shell",
                 "getprop", "sys.boot_completed"], 15, env=adb_env)
            boot_output = str(boot.details.get("output", "")).strip()
            if boot.success and re.search(r"(?:^|\s)1(?:\s|$)", boot_output):
                settle = float(step.get("settle_seconds", 3))
                if ctx.stop_event.wait(settle):
                    return Result(False, "任务被用户停止")
                return Result(True, f"MuMu 实例 {index} 已启动，ADB 与 Android 系统均已就绪",
                              {"instance": index, "info": info,
                               "device": device, "adb_connected": True,
                               "boot_completed": True})
            ctx.log("MuMu ADB 已连接，但 Android 系统仍在启动；继续等待系统就绪")
        if ctx.stop_event.wait(poll_seconds):
            return Result(False, "任务被用户停止")
    return Result(False, f"等待 MuMu 实例 {index} 启动超时（{timeout:g} 秒）")


def run_adb(step: dict[str, Any], ctx: RunContext) -> Result:
    adb = expand(str(step.get("executable") or ctx.tool("adb")))
    device = str(step.get("device") or ctx.config.get("device", {}).get("address", ""))
    prefix = [adb] + (["-s", device] if device else [])
    action = step.get("action", "wait")
    timeout = int(step.get("timeout", 120))
    env = os.environ.copy()
    env.update({str(k): expand(str(v)) for k, v in step.get("env", {}).items()})
    if step.get("adb_server_port") is not None:
        env["ANDROID_ADB_SERVER_PORT"] = str(step["adb_server_port"])
    if action == "wait":
        deadline = time.monotonic() + timeout
        poll_seconds = max(0.2, float(step.get("poll_seconds", 2)))
        package = str(step.get("package", "")).strip()
        require_launcher = bool(step.get("require_launcher", False))
        last_message = "ADB 设备尚未就绪"
        while time.monotonic() < deadline:
            if ctx.stop_event.is_set():
                return Result(False, "任务被用户停止")
            remaining = max(1, int(deadline - time.monotonic()))
            wait_result = ctx.command(prefix + ["wait-for-device"], min(10, remaining), env=env)
            if wait_result.success:
                boot = ctx.command(prefix + ["shell", "getprop", "sys.boot_completed"],
                                   min(10, remaining), env=env)
                boot_output = str(boot.details.get("output", "")).strip()
                if boot.success and re.search(r"(?:^|\s)1(?:\s|$)", boot_output):
                    package_ready = True
                    if package:
                        package_result = ctx.command(prefix + ["shell", "pm", "path", package],
                                                     min(10, remaining), env=env)
                        package_output = str(package_result.details.get("output", ""))
                        package_ready = package_result.success and "package:" in package_output
                        if package_ready and require_launcher:
                            launcher = ctx.command(
                                prefix + ["shell", "cmd", "package", "resolve-activity",
                                          "--brief", package], min(10, remaining), env=env)
                            launcher_output = str(launcher.details.get("output", ""))
                            package_ready = (launcher.success and bool(launcher_output.strip())
                                             and "No activity found" not in launcher_output)
                        if not package_ready:
                            last_message = f"Android 已启动，但应用 {package} 尚未可启动"
                    if package_ready:
                        settle = max(0.0, float(step.get("settle_seconds", 1)))
                        if ctx.stop_event.wait(settle):
                            return Result(False, "任务被用户停止")
                        return Result(True, "ADB、Android 系统与应用均已就绪" if package
                                      else "ADB 与 Android 系统均已就绪",
                                      {"device": device, "boot_completed": True,
                                       "package": package or None})
                else:
                    last_message = "ADB 已连接，但 Android 系统仍在启动"
            else:
                last_message = wait_result.message
            ctx.log(f"{last_message}；继续等待")
            if ctx.stop_event.wait(min(poll_seconds, max(0.0, deadline - time.monotonic()))):
                return Result(False, "任务被用户停止")
        return Result(False, f"等待 Android 完全启动超时（{timeout} 秒）：{last_message}")
    if action == "screenshot":
        out = Path(expand(str(step.get("path", ctx.root / "logs" / "screenshot.png"))))
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            done = subprocess.run(prefix + ["exec-out", "screencap", "-p"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=timeout, env=env,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Result(False, f"模拟器截图失败：{exc}")
        if done.returncode != 0 or not done.stdout:
            error = _decode_process_output(done.stderr).strip()
            return Result(False, f"模拟器截图失败：{error or '未返回图像'}",
                          {"exit_code": done.returncode})
        try:
            out.write_bytes(done.stdout)
        except OSError as exc:
            return Result(False, f"保存模拟器截图失败：{exc}")
        return Result(True, "模拟器截图已保存", {"path": str(out), "bytes": len(done.stdout)})
    if action == "start_app":
        package = str(step.get("package", ""))
        return ctx.command(prefix + ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"], timeout, env=env)
    if action == "stop_app":
        return ctx.command(prefix + ["shell", "am", "force-stop", str(step.get("package", ""))], timeout, env=env)
    if action == "shell":
        return ctx.command(prefix + ["shell"] + [str(x) for x in step.get("args", [])], timeout, env=env)
    return Result(False, f"不支持的 ADB 动作：{action}")


def run_delay(step: dict[str, Any], ctx: RunContext) -> Result:
    seconds = float(step.get("seconds", 1))
    return Result(not ctx.stop_event.wait(seconds), "等待完成" if not ctx.stop_event.is_set() else "任务被用户停止")


RUNNERS = {"command": run_command, "maa": run_maa, "maa_gui": run_maa_gui,
           "baas_gui": run_baas_gui, "ba_reward_verify": run_ba_reward_verify,
           "alas_gui": run_alas_gui, "naruto_shadow": run_naruto_shadow,
           "naruto_reward_verify": run_naruto_reward_verify,
           "gumballs_gui": run_gumballs_gui, "maaend_gui": run_maaend_gui,
           "log_gui_daily": run_log_gui_daily,
           "window_screenshot": run_window_screenshot,
           "ldplayer": run_ldplayer, "mumu_wait": run_mumu_wait,
           "adb": run_adb, "delay": run_delay}
