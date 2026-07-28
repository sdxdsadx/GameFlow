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


def _windows_is_admin() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


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


def _screen_is_black(data: bytes, step: dict[str, Any]) -> tuple[bool, dict[str, float]]:
    """Classify an ADB PNG while tolerating small navigation bars and status overlays."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return False, {"mean_brightness": -1.0, "dark_ratio": 0.0}
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None or image.size == 0:
        return False, {"mean_brightness": -1.0, "dark_ratio": 0.0}
    height, width = image.shape[:2]
    margin_x = max(0, round(width * 0.02))
    margin_y = max(0, round(height * 0.02))
    sample = image[margin_y:height - margin_y or height,
                   margin_x:width - margin_x or width]
    dark_threshold = float(step.get("black_screen_pixel_threshold", 45))
    mean_limit = float(step.get("black_screen_mean_threshold", 35))
    ratio_limit = float(step.get("black_screen_dark_ratio", 0.95))
    mean_brightness = float(np.mean(sample))
    dark_ratio = float(np.mean(sample <= dark_threshold))
    return (mean_brightness <= mean_limit and dark_ratio >= ratio_limit,
            {"mean_brightness": round(mean_brightness, 2),
             "dark_ratio": round(dark_ratio, 4)})


class EmulatorBlackScreenWatchdog:
    """Periodically save emulator screenshots and report a continuous black screen."""

    def __init__(self, step: dict[str, Any], ctx: RunContext, label: str):
        self.step = step
        self.ctx = ctx
        self.label = label
        self.enabled = bool(step.get("black_screen_watchdog", False))
        self.interval = max(1.0, float(step.get("black_screen_check_interval", 30)))
        self.timeout = max(self.interval, float(step.get("black_screen_timeout", 300)))
        self.next_check_at = 0.0
        self.black_since: float | None = None
        self.last_black_log_at = 0.0
        self.capture_count = 0
        slug = re.sub(r"[^0-9A-Za-z_-]+", "_", str(step.get("id", label))).strip("_")
        default_path = ctx.root / "logs" / "emulator_watchdog" / f"{slug or 'emulator'}_latest.png"
        self.latest_path = Path(expand(str(step.get("black_screen_screenshot_path", default_path))))

    def poll(self, force: bool = False) -> Result | None:
        if not self.enabled:
            return None
        now = time.monotonic()
        if not force and now < self.next_check_at:
            return None
        self.next_check_at = now + self.interval
        adb = expand(str(self.step.get("adb_executable") or self.ctx.tool("adb")))
        device = str(self.step.get("device", "")).strip()
        env = os.environ.copy()
        env.update({str(k): expand(str(v)) for k, v in self.step.get("env", {}).items()})
        if self.step.get("adb_server_port") is not None:
            env["ANDROID_ADB_SERVER_PORT"] = str(self.step["adb_server_port"])
        args = [adb] + (["-s", device] if device else []) + ["exec-out", "screencap", "-p"]
        try:
            done = subprocess.run(
                args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=int(self.step.get("black_screen_capture_timeout", 20)), env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.black_since = None
            self.ctx.log(f"{self.label}黑屏看门狗截图失败，本次不计入黑屏时长：{exc}")
            return None
        if done.returncode != 0 or not done.stdout:
            self.black_since = None
            error = _decode_process_output(done.stderr).strip() or "ADB 未返回图像"
            self.ctx.log(f"{self.label}黑屏看门狗截图失败，本次不计入黑屏时长：{error}")
            return None

        self.latest_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.latest_path.write_bytes(done.stdout)
            self.capture_count += 1
            if self.capture_count == 1:
                self.ctx.log(f"{self.label}看门狗截图已启用：{self.latest_path}")
        except OSError as exc:
            self.ctx.log(f"{self.label}看门狗截图保存失败：{exc}")
        black, metrics = _screen_is_black(done.stdout, self.step)
        if not black:
            if self.black_since is not None:
                elapsed = now - self.black_since
                self.ctx.log(f"{self.label}画面已恢复，之前连续黑屏 {elapsed:g} 秒")
            self.black_since = None
            return None

        if self.black_since is None:
            self.black_since = now
            self.last_black_log_at = now
            self.ctx.log(f"{self.label}检测到黑屏，开始累计；超过 {self.timeout:g} 秒将重启模拟器和脚本")
            return None
        elapsed = now - self.black_since
        if elapsed < self.timeout:
            if now - self.last_black_log_at >= 60:
                self.last_black_log_at = now
                self.ctx.log(f"{self.label}仍为黑屏，已持续 {elapsed:g}/{self.timeout:g} 秒")
            return None

        timestamp = time.strftime("%Y%m%d-%H%M%S")
        evidence = self.latest_path.with_name(f"{self.latest_path.stem}_black_{timestamp}.png")
        try:
            evidence.write_bytes(done.stdout)
        except OSError:
            evidence = self.latest_path
        return Result(
            False,
            f"{self.label}连续黑屏 {elapsed:g} 秒，准备重启模拟器和脚本",
            {"black_screen_restart": True, "retry_step": True,
             "black_seconds": elapsed, "evidence": str(evidence), **metrics})


def _wait_with_emulator_watchdog(seconds: float, watchdog: EmulatorBlackScreenWatchdog,
                                  ctx: RunContext) -> Result | None:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        detected = watchdog.poll()
        if detected is not None:
            return detected
        remaining = max(0.0, deadline - time.monotonic())
        if ctx.stop_event.wait(min(1.0, remaining)):
            return Result(False, "任务被用户停止")
    return None


def _restart_configured_emulator(step: dict[str, Any], ctx: RunContext) -> Result:
    """Restart the emulator configured on a script step after that script has closed."""
    kind = str(step.get("emulator_kind", "")).strip().casefold()
    instance = str(step.get("emulator_instance", step.get("instance", ""))).strip()
    delay = float(step.get("emulator_restart_delay", 3))
    if kind == "ldplayer":
        if not instance:
            return Result(False, "黑屏恢复缺少雷电模拟器实例编号")
        ldconsole = expand(str(step.get("emulator_executable") or ctx.tool("ldconsole")))
        ctx.command([ldconsole, "quit", "--index", instance], 60)
        if ctx.stop_event.wait(delay):
            return Result(False, "任务被用户停止")
        launched = ctx.command([ldconsole, "launch", "--index", instance], 120)
        if not launched.success:
            return Result(False, f"黑屏后重启雷电实例 {instance} 失败：{launched.message}")
        return run_adb({
            "action": "wait", "executable": step.get("adb_executable"),
            "device": step.get("device", ""),
            "timeout": int(step.get("emulator_ready_timeout", 180)),
            "poll_seconds": 2, "settle_seconds": 3,
            "adb_server_port": step.get("adb_server_port")}, ctx)
    if kind == "mumu":
        manager = expand(str(step.get("emulator_manager", "")))
        if not manager or not instance:
            return Result(False, "黑屏恢复缺少 MuMuManager 地址或实例编号")
        ctx.command([manager, "control", "--vmindex", instance, "shutdown"], 60)
        if ctx.stop_event.wait(delay):
            return Result(False, "任务被用户停止")
        return run_mumu_wait({
            "executable": manager,
            "adb_executable": step.get("adb_executable", ""),
            "adb_server_port": step.get("adb_server_port"),
            "device": step.get("device", "127.0.0.1:16384"),
            "instance": instance,
            "timeout": int(step.get("emulator_ready_timeout", 180)),
            "poll_seconds": 2, "settle_seconds": 3}, ctx)
    return Result(False, "黑屏恢复未配置 emulator_kind（ldplayer 或 mumu）")


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
    return result


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
    marker = str(step.get("completion_marker", "AllTasksCompleted"))
    error_markers = [str(x) for x in step.get("error_markers", ["TaskChainError", "AllTasksError"])]
    start_size = log_path.stat().st_size if log_path.exists() else 0
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
    restart_grace = float(step.get("restart_grace_seconds", 120))
    log_stall_seconds = max(0.0, float(step.get("log_stall_seconds", 0)))
    last_log_at = started
    log_activity_seen = False
    watchdog = EmulatorBlackScreenWatchdog(step, ctx, "明日方舟模拟器")
    try:
        while time.monotonic() - started <= timeout:
            if ctx.stop_event.wait(1):
                return Result(False, "任务被用户停止")
            black_screen = watchdog.poll()
            if black_screen is not None:
                return black_screen
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
                    last_log_at = time.monotonic()
                    log_activity_seen = True
                    tail = (tail + text_chunk)[-12000:]
                    if marker in tail:
                        return Result(True, "MAA 日常任务全部完成", {"marker": marker})
                    for error in error_markers:
                        if error in tail:
                            return Result(False, f"MAA 报告任务错误：{error}", {"marker": error})
            if (log_activity_seen and log_stall_seconds > 0
                    and time.monotonic() - last_log_at >= log_stall_seconds):
                stalled_for = time.monotonic() - last_log_at
                return Result(
                    False,
                    f"MAA 任务日志已停滞 {stalled_for:.0f} 秒，结束第一轮并准备重新运行",
                    {"log_stall_seconds": stalled_for, "retry_step": True})
            if proc.poll() is not None and marker not in tail:
                successor_running = _process_path_exists(exe)
                successor_seen = successor_seen or successor_running
                if successor_running:
                    exit_seen_at = None
                elif exit_seen_at is None:
                    exit_seen_at = time.monotonic()
                    ctx.log(f"MAA 原进程已退出（退出码 {proc.returncode}），"
                            f"等待最多 {restart_grace:g} 秒接续进程")
                elif time.monotonic() - exit_seen_at >= restart_grace:
                    return Result(False, f"MAA 退出后 {restart_grace:g} 秒内未恢复"
                                  f"（退出码 {proc.returncode}）",
                                  {"successor_seen": successor_seen})
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
    if step.get("require_admin", False) and not _windows_is_admin():
        return Result(
            False,
            "BAAS 要求管理员权限；请通过“启动.lnk”、start.bat 或对应的每日流程批处理启动 GameFlow",
            {"requires_admin": True})
    log_glob = expand(str(step.get("log_glob") or exe.parent / "runtime" / "logs" / "*_baas1.log"))
    marker = str(step.get("completion_marker", "任务全部执行成功"))
    error_markers = [str(x) for x in step.get("error_markers", ["任务全部执行失败"])]
    ignored_error_markers = [str(x) for x in step.get("ignored_error_markers", [])]
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
    require_empty_queue = bool(step.get("require_empty_queue", False))
    queue_check_interval = max(0.05, float(step.get("queue_check_interval", 5)))
    queue_empty_confirm_seconds = max(
        0.0, float(step.get("queue_empty_confirm_seconds", 10)))
    next_queue_check_at = started
    queue_seen_nonempty = False
    queue_empty_at = None
    queue_zero_at = None
    last_queue_count = None
    last_queue_idle = None
    last_queue_probe_message = None
    next_queue_idle_click_at = started
    required_last_task = str(step.get("required_last_task", "")).strip()
    required_check_interval = float(step.get("required_task_check_interval", 600))
    completion_seen = False
    next_required_check_at = None
    last_run_task = None
    last_completed_task = None
    recoverable_events = 0
    reported_ignored_errors: set[str] = set()
    last_recoverable_at = None
    max_recoverable_events = max(1, int(step.get("max_recoverable_events", 3)))
    poll_seconds = max(0.05, float(step.get("poll_seconds", 1)))
    log_stall_seconds = max(0.0, float(step.get("log_stall_seconds", 300)))
    log_stall_start_markers = [str(x) for x in step.get(
        "log_stall_start_markers", ["开始执行【"])]
    log_stall_armed = False
    last_log_at = None
    last_log_name = None
    emulator_instance = step.get("emulator_instance")
    emulator_device = str(step.get("device", "emulator-5560"))
    emulator_restarts = 0
    max_emulator_restarts = max(0, int(step.get("max_emulator_restarts", 1)))
    emulator_offline_markers = [str(x) for x in step.get("emulator_offline_markers", [
        "USB device", "is offline", "模拟器连接失败，必须打开模拟器",
    ])]
    start_markers = [str(x) for x in step.get(
        "start_markers", ["模拟器连接成功", "开始执行【"])]
    watchdog = EmulatorBlackScreenWatchdog(step, ctx, "碧蓝档案模拟器")
    next_network_retry_at = started
    network_retry_attempts = 0
    repeated_probe_target = None
    repeated_probe_count = 0
    repeated_probe_recoveries = 0
    repeated_probe_last_recovery_at = None
    repeated_probe_recovery_targets = {str(value) for value in step.get(
        "repeated_probe_recovery_targets", ["normal_task_scan-confirm"])}
    repeated_probe_threshold = max(3, int(step.get("repeated_probe_threshold", 20)))
    repeated_probe_recovery_interval = max(
        5.0, float(step.get("repeated_probe_recovery_interval", 20)))
    repeated_probe_recovery_max = max(0, int(step.get("repeated_probe_recovery_max", 5)))

    def retry_game_network_dialog() -> Result | None:
        """Detect Blue Archive's 101404 modal and tap its dark Retry button."""
        nonlocal next_network_retry_at, network_retry_attempts
        if not step.get("network_retry_enabled", step.get("require_admin", False)):
            return None
        now = time.monotonic()
        if now < next_network_retry_at:
            return None
        next_network_retry_at = now + max(5.0, float(step.get("network_retry_interval", 15)))
        adb = ctx.tool("adb")
        try:
            import cv2
            import numpy as np
            captured = subprocess.run(
                [adb, "-s", emulator_device, "exec-out", "screencap", "-p"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if captured.returncode != 0 or not captured.stdout:
                return None
            image = cv2.imdecode(np.frombuffer(captured.stdout, dtype=np.uint8),
                                 cv2.IMREAD_COLOR)
            if image is None:
                return None
            height, width = image.shape[:2]
            modal = image[round(height * 0.27):round(height * 0.73),
                          round(width * 0.275):round(width * 0.725)]
            retry = image[round(height * 0.52):round(height * 0.62),
                          round(width * 0.515):round(width * 0.65)]
            if modal.size == 0 or retry.size == 0:
                return None
            modal_hsv = cv2.cvtColor(modal, cv2.COLOR_BGR2HSV)
            retry_hsv = cv2.cvtColor(retry, cv2.COLOR_BGR2HSV)
            modal_light = float(np.mean(modal_hsv[:, :, 2] >= 190))
            retry_dark = float(np.mean(retry_hsv[:, :, 2] <= 100))
            retry_yellow = float(np.mean(
                (retry_hsv[:, :, 0] >= 18) & (retry_hsv[:, :, 0] <= 42) &
                (retry_hsv[:, :, 1] >= 100) & (retry_hsv[:, :, 2] >= 120)))
            if not (modal_light >= 0.45 and retry_dark >= 0.35 and retry_yellow >= 0.005):
                return None
            max_attempts = max(1, int(step.get("network_retry_max_attempts", 6)))
            if network_retry_attempts >= max_attempts:
                return Result(False, f"碧蓝档案持续出现网络异常，自动重试已达到 {max_attempts} 次",
                              {"network_retry_attempts": network_retry_attempts})
            network_retry_attempts += 1
            x = round(width * float(step.get("network_retry_x_ratio", 744 / 1280)))
            y = round(height * float(step.get("network_retry_y_ratio", 410 / 720)))
            tapped = subprocess.run(
                [adb, "-s", emulator_device, "shell", "input", "tap", str(x), str(y)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if tapped.returncode == 0:
                ctx.log(f"检测到碧蓝档案 101404 网络异常弹窗，已第 {network_retry_attempts} 次点击“重试”：({x}, {y})")
            return None
        except (ImportError, OSError, subprocess.TimeoutExpired):
            return None

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

    def stop_baas_processes() -> None:
        if os.name == "nt":
            for image_name in dict.fromkeys(process_images):
                subprocess.run(["taskkill", "/IM", image_name, "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        elif proc.poll() is None:
            proc.terminate()

    if require_empty_queue:
        ctx.log("BAAS 已启动，将以“本轮已开始、baas1 队列为 0 且状态为闲置中”"
                "稳定保持作为结束标准")
    else:
        ctx.log(f"BAAS 已启动，等待 baas1 完成标记：{marker}")
    try:
        while time.monotonic() - started <= timeout:
            if ctx.stop_event.wait(poll_seconds):
                return Result(False, "任务被用户停止")
            black_screen = watchdog.poll()
            if black_screen is not None:
                return black_screen
            network_retry = retry_game_network_dialog()
            if network_retry is not None:
                return network_retry
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
                    chunk_at = time.monotonic()
                    chunk_starts_run = any(start_marker in content for start_marker in start_markers)
                    chunk_arms_stall = any(item in content for item in log_stall_start_markers)
                    if log_stall_armed or chunk_arms_stall:
                        last_log_at = chunk_at
                        last_log_name = name
                    if chunk_arms_stall:
                        log_stall_armed = True
                    if completion_at is not None:
                        if required_last_task:
                            completion_at = time.monotonic()
                            completion_log = name
                            ctx.log("BAAS 在关闭观察期仍有新日志，重新计算稳定时间")
                        else:
                            ctx.log("BAAS 在成功标记后仍有新日志，继续等待真正结束")
                            completion_at = None
                            completion_log = None
                    if chunk_starts_run:
                        run_started = True
                    for line in content.splitlines():
                        repeated_probe = re.search(
                            r"开始第\s*\d+/\d+\s*次图片检索\s+end:([^\s]+)", line)
                        if repeated_probe:
                            target = repeated_probe.group(1).strip("()'\"")
                            if target == repeated_probe_target:
                                repeated_probe_count += 1
                            else:
                                repeated_probe_target = target
                                repeated_probe_count = 1
                            recovery_now = time.monotonic()
                            can_recover = (
                                target in repeated_probe_recovery_targets
                                and repeated_probe_count >= repeated_probe_threshold
                                and repeated_probe_recoveries < repeated_probe_recovery_max
                                and (repeated_probe_last_recovery_at is None
                                     or recovery_now - repeated_probe_last_recovery_at
                                     >= repeated_probe_recovery_interval)
                            )
                            if can_recover:
                                tap_x = int(step.get("repeated_probe_tap_x", 640))
                                tap_y = int(step.get("repeated_probe_tap_y", 630))
                                tapped = subprocess.run(
                                    [ctx.tool("adb"), "-s", emulator_device, "shell", "input",
                                     "tap", str(tap_x), str(tap_y)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                                    creationflags=(subprocess.CREATE_NO_WINDOW
                                                   if os.name == "nt" else 0))
                                repeated_probe_recoveries += 1
                                repeated_probe_last_recovery_at = recovery_now
                                repeated_probe_count = 0
                                if tapped.returncode == 0:
                                    ctx.log(f"BAAS 连续识别“{target}”无业务进展，"
                                            f"第 {repeated_probe_recoveries} 次点击游戏“继续”："
                                            f"({tap_x}, {tap_y})")
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
                                if require_empty_queue:
                                    ctx.log("检测到 BAAS 成功日志，继续等待界面队列清空")
                                elif required_last_task:
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
                        log_stall_armed = False
                        last_log_at = None
                        last_log_name = None
                        queue_seen_nonempty = False
                        queue_empty_at = None
                        queue_zero_at = None
                        last_queue_count = None
                        last_queue_idle = None
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
                        log_stall_armed = False
                        last_log_at = None
                        last_log_name = None
                        queue_seen_nonempty = False
                        queue_empty_at = None
                        queue_zero_at = None
                        last_queue_count = None
                        last_queue_idle = None
                        startup_reference = time.monotonic()
                        next_fallback_at = time.monotonic() + float(
                            step.get("emulator_recovered_click_delay", 10))
                        ctx.log("碧蓝档案模拟器已恢复，继续等待 BAAS 并重新点击启动")
                    for error in error_markers:
                        if error in content:
                            return Result(False, f"BAAS 报告任务错误：{error}", {"log": name, "marker": error})
                    for warning in ignored_error_markers:
                        if warning in content and warning not in reported_ignored_errors:
                            reported_ignored_errors.add(warning)
                            ctx.log(f"BAAS 报告非致命提示：{warning}；继续等待完整每日流程结束")
            now = time.monotonic()
            if (require_empty_queue and now >= next_queue_check_at):
                queue_count, queue_idle, probe_message = _read_baas_queue_count(proc.pid)
                next_queue_check_at = now + queue_check_interval
                if queue_count is None:
                    queue_empty_at = None
                    if probe_message != last_queue_probe_message:
                        ctx.log(f"BAAS 队列检测：{probe_message}")
                        last_queue_probe_message = probe_message
                else:
                    last_queue_probe_message = None
                    if queue_count != last_queue_count or queue_idle != last_queue_idle:
                        shown_status = "闲置中" if queue_idle else "执行中"
                        ctx.log(f"BAAS 当前队列任务数：{queue_count}（{shown_status}）")
                        last_queue_count = queue_count
                        last_queue_idle = queue_idle
                    if queue_count > 0:
                        queue_seen_nonempty = True
                        queue_empty_at = None
                        queue_zero_at = None
                        if queue_idle and now >= next_queue_idle_click_at:
                            if click_count >= max_start_clicks:
                                return Result(
                                    False,
                                    f"BAAS 队列仍有 {queue_count} 项，但已达到 "
                                    f"{click_count} 次启动点击上限",
                                    {"queue_count": queue_count,
                                     "start_clicks": click_count})
                            click_count += 1
                            clicked, click_message = _click_baas_start_button(
                                proc.pid, float(step.get("gui_click_x_ratio", 0.374)),
                                float(step.get("gui_click_y_ratio", 0.108)),
                                float(step.get("gui_profile_x_ratio", 0.04)),
                                float(step.get("gui_profile_y_ratio", 0.15)))
                            ctx.log(f"BAAS 上一批已结束但队列仍有 {queue_count} 项；"
                                    f"第 {click_count} 次继续启动：{click_message}")
                            next_queue_idle_click_at = now + float(
                                step.get("gui_fallback_retry_seconds", 30))
                            completion_seen = False
                            completion_log = None
                            run_started = False
                            log_stall_armed = False
                            last_log_at = None
                            last_log_name = None
                            startup_reference = now
                            next_fallback_at = now + float(
                                step.get("gui_fallback_retry_seconds", 30))
                            if not clicked:
                                ctx.log("BAAS 继续启动未成功，将按启动重试间隔再次点击")
                    elif queue_idle and log_stall_armed:
                        if queue_zero_at is None:
                            queue_zero_at = now
                        if queue_empty_at is None:
                            queue_empty_at = now
                            ctx.log(f"BAAS 队列已空且状态为“闲置中”，继续确认 "
                                    f"{queue_empty_confirm_seconds:g} 秒避免瞬时误判")
                    else:
                        if queue_count == 0 and queue_zero_at is None:
                            queue_zero_at = now
                        queue_empty_at = None
            if (required_last_task and not require_empty_queue
                    and completion_seen and next_required_check_at is not None
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
            required_task_matches = (
                not required_last_task
                or (last_run_task == required_last_task
                    and last_completed_task == required_last_task)
            )
            if (require_empty_queue and queue_empty_at is not None
                    and now - queue_empty_at >= queue_empty_confirm_seconds
                    and not required_task_matches):
                if next_required_check_at is None or now >= next_required_check_at:
                    shown = last_completed_task or "尚无完成记录"
                    ctx.log(f"BAAS 队列虽已清空，但最后完成任务为“{shown}”，不是“{required_last_task}”；"
                            f"保持 BAAS 与模拟器运行，{required_check_interval:g} 秒后重新检测")
                    next_required_check_at = now + required_check_interval
                # Keep requiring a fresh stable-empty interval after the scheduler
                # eventually adds and finishes the required final task.
                queue_empty_at = now
            if (require_empty_queue and queue_empty_at is not None
                    and now - queue_empty_at >= queue_empty_confirm_seconds
                    and required_task_matches):
                return Result(
                    True,
                    (f"BAAS baas1 队列已清空，且最后完成任务已确认为“{required_last_task}”"
                     if required_last_task else
                     "BAAS baas1 队列已清空且脚本已闲置，本轮日常任务完成"),
                    {"log": completion_log, "marker": marker if completion_seen else None,
                     "queue_count": 0, "queue_seen_nonempty": queue_seen_nonempty,
                     "queue_empty_confirm_seconds": queue_empty_confirm_seconds,
                     "last_run_task": last_run_task,
                     "last_completed_task": last_completed_task,
                     "recoverable_events": recoverable_events,
                     "emulator_restarts": emulator_restarts,
                     "start_clicks": click_count})
            if (require_empty_queue and completion_seen and queue_seen_nonempty
                    and last_queue_count == 0 and queue_zero_at is not None
                    and last_log_at is not None
                    and now - last_log_at >= quiet_seconds
                    and required_task_matches):
                return Result(
                    True,
                    "BAAS 成功日志已出现、队列已从非空降至 0 且日志保持静默；"
                    "GUI 闲置状态暂不可读，按等价条件确认本轮完成",
                    {"log": completion_log, "marker": marker,
                     "queue_count": 0, "queue_seen_nonempty": True,
                     "queue_idle": last_queue_idle,
                     "quiet_seconds": quiet_seconds,
                     "last_run_task": last_run_task,
                     "last_completed_task": last_completed_task,
                     "recoverable_events": recoverable_events,
                     "emulator_restarts": emulator_restarts,
                     "start_clicks": click_count,
                     "completion_mode": "queue_zero_log_quiet"})
            if (not require_empty_queue and completion_at is not None
                    and time.monotonic() - completion_at >= quiet_seconds):
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
            if (log_stall_armed and last_log_at is not None and log_stall_seconds > 0
                    and not completion_seen
                    and now - last_log_at >= log_stall_seconds):
                stalled_for = now - last_log_at
                return Result(
                    False,
                    f"BAAS 本轮启动后日志已 {stalled_for:g} 秒无更新，判定脚本停滞",
                    {"log": last_log_name, "log_stalled": True,
                     "retry_step": True, "log_stall_seconds": stalled_for,
                     "last_run_task": last_run_task,
                     "last_completed_task": last_completed_task})
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
                    elapsed = time.monotonic() - startup_reference
                    window_timeout = float(step.get("gui_window_timeout", 120))
                    if elapsed >= window_timeout:
                        return Result(False, f"等待 BAAS 窗口 {window_timeout:g} 秒后仍无法点击启动：{click_message}")
                    ctx.log(f"BAAS 窗口仍在启动，{retry_seconds:g} 秒后重试备用点击")
            if proc.poll() is not None and not _process_image_exists(process_images):
                return Result(False, f"BAAS 提前退出（退出码 {proc.returncode}）")
        return Result(False, f"等待 BAAS 完成超时（{timeout} 秒）")
    finally:
        if step.get("close_on_complete", True):
            stop_baas_processes()


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


def _matching_visible_window_exists(title_hints: list[str]) -> bool:
    """Return whether a substantial visible window matches a configured title."""
    if os.name != "nt":
        return False
    folded_hints = [hint.strip().casefold() for hint in title_hints if hint.strip()]
    if not folded_hints:
        return False
    try:
        import win32gui
    except ImportError:
        return False
    found = False

    def collect(hwnd, _):
        nonlocal found
        if found or not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd).strip().casefold()
        if not any(hint in title for hint in folded_hints):
            return
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        found = max(0, right - left) * max(0, bottom - top) > 10000

    try:
        win32gui.EnumWindows(collect, None)
    except Exception:
        return False
    return found


def _force_foreground_window(hwnd: int) -> bool:
    """Use attached Windows input queues when SetForegroundWindow is access-restricted."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        current_thread = int(kernel32.GetCurrentThreadId())
        foreground = int(user32.GetForegroundWindow())
        target_thread = int(user32.GetWindowThreadProcessId(hwnd, None))
        foreground_thread = (int(user32.GetWindowThreadProcessId(foreground, None))
                             if foreground else 0)
        attached: list[int] = []
        try:
            # A brief Alt key cycle lets Windows legally transfer foreground ownership.
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 0x0002, 0)
            user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
            for thread_id in dict.fromkeys((foreground_thread, target_thread)):
                if thread_id and thread_id != current_thread:
                    if user32.AttachThreadInput(current_thread, thread_id, True):
                        attached.append(thread_id)
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SwitchToThisWindow(hwnd, True)
            user32.SetActiveWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            for thread_id in reversed(attached):
                user32.AttachThreadInput(current_thread, thread_id, False)
        return int(user32.GetForegroundWindow()) == int(hwnd)
    except Exception:
        return False


def _focus_window_before_click(hwnd: int, window_name: str = "脚本窗口",
                               timeout: float = 1.5,
                               settle_seconds: float = 0.12) -> tuple[bool, str]:
    """Restore a desktop window and verify it owns the foreground before clicking it."""
    if os.name != "nt":
        return False, "GUI 窗口置前只支持 Windows"
    try:
        import win32con
        import win32gui
    except ImportError as exc:
        return False, f"缺少 Windows 窗口控制组件：{exc}"
    if not win32gui.IsWindow(hwnd):
        return False, f"{window_name}窗口已经失效，无法置前"

    # Full-screen Unity windows can reject BringWindowToTop with ERROR_INVALID_PARAMETER
    # even though they own the foreground; the final foreground-handle check remains
    # authoritative even when one individual Windows API call fails.
    deadline = time.monotonic() + max(0.1, timeout)
    last_error = "Windows 未授予前台焦点"
    flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW
    while True:
        made_topmost = False
        focused = False
        try:
            try:
                # Calling SW_RESTORE on a visible borderless/full-screen Unity window
                # can make its Win32 handle disappear briefly.  Only restore windows
                # that Windows actually reports as minimized.
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
                made_topmost = True
            except Exception as exc:
                last_error = str(exc)
            # Keep the fallbacks independent: some Unity full-screen windows reject
            # BringWindowToTop but still accept SetForegroundWindow (or vice versa).
            try:
                win32gui.BringWindowToTop(hwnd)
            except Exception as exc:
                last_error = str(exc)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception as exc:
                last_error = str(exc)
            focused = win32gui.GetForegroundWindow() == hwnd
        finally:
            if made_topmost:
                try:
                    win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
                except Exception:
                    pass
        if not focused:
            focused = _force_foreground_window(hwnd)
        if focused:
            if settle_seconds > 0:
                time.sleep(settle_seconds)
            return True, f"已将{window_name}置于最前端并取得焦点"
        if time.monotonic() >= deadline:
            return False, f"无法将{window_name}置于最前端，已取消点击：{last_error}"
        time.sleep(0.05)


_DESKTOP_GUI_CLICK_LOCK = threading.Lock()


def _serialized_desktop_click(function):
    """Prevent parallel workflows from stealing foreground focus during a GUI click."""
    def locked(*args, **kwargs):
        with _DESKTOP_GUI_CLICK_LOCK:
            return function(*args, **kwargs)
    return locked


@_serialized_desktop_click
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
    focused, focus_message = _focus_window_before_click(hwnd, title or "BAAS")
    if not focused:
        return False, focus_message
    rect = win32gui.GetWindowRect(hwnd)
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


def _parse_baas_queue_count(texts: list[str]) -> int | None:
    """Extract the active BAAS queue count from UI Automation text."""
    cleaned = [str(value).strip() for value in texts if str(value).strip()]
    pattern = re.compile(r"(?:^|[\s/])队列(?:中)?\s*[\(（]\s*(\d+)\s*[\)）](?:$|\s)")

    # Prefer the smallest control containing the complete label. Web views often
    # expose both a precise text node and a huge parent document with the same text.
    matches: list[tuple[int, int]] = []
    for value in cleaned:
        found = pattern.search(value)
        if found:
            matches.append((len(value), int(found.group(1))))
    if matches:
        return min(matches)[1]

    # Some UIA providers split "队列" and "(7)" into adjacent text controls.
    for index, value in enumerate(cleaned[:-1]):
        if value.rstrip("：:").strip() not in ("队列", "队列中"):
            continue
        found = re.fullmatch(r"[\(（]?\s*(\d+)\s*[\)）]?", cleaned[index + 1])
        if found:
            return int(found.group(1))
    return None


def _read_baas_queue_count(root_pid: int) -> tuple[int | None, bool | None, str]:
    """Read BAAS' queue count and idle status without stealing foreground focus."""
    if os.name != "nt":
        return None, None, "队列界面检测仅支持 Windows"
    try:
        import psutil
        import win32gui
        import win32process
        from pywinauto import Application
    except ImportError as exc:
        return None, None, f"缺少队列界面检测组件：{exc}"

    pids = {root_pid}
    try:
        pids.update(child.pid for child in psutil.Process(root_pid).children(recursive=True))
    except (psutil.Error, OSError):
        pass
    candidates: list[tuple[int, int, int, str]] = []

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        title = win32gui.GetWindowText(hwnd)
        upper_title = title.upper()
        if (pid not in pids and "BAAS" not in upper_title
                and "BLUEARCHIVEAUTOSCRIPT" not in upper_title):
            return
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        area = max(0, right - left) * max(0, bottom - top)
        if area <= 10000:
            return
        priority = (10 if "BAAS PRO" in upper_title
                    else 8 if "BLUEARCHIVEAUTOSCRIPT" in upper_title else 1)
        candidates.append((priority, area, hwnd, title))

    try:
        win32gui.EnumWindows(collect, None)
    except Exception as exc:
        return None, None, f"枚举 BAAS 窗口失败：{exc}"
    if not candidates:
        return None, None, "BAAS 尚未产生可见窗口"

    errors = []
    for _, _, hwnd, title in sorted(candidates, reverse=True):
        try:
            window = Application(backend="uia").connect(handle=hwnd).window(handle=hwnd)
            controls = [window, *window.descendants()]
            texts: list[str] = []
            idle = False
            for control in controls:
                for value_getter in (
                    lambda item=control: item.window_text(),
                    lambda item=control: item.element_info.name,
                ):
                    try:
                        value = str(value_getter() or "").strip()
                    except Exception:
                        continue
                    if value and value not in texts:
                        texts.append(value)
                    if value == "闲置中":
                        idle = True
                    try:
                        if (value == "启动"
                                and control.element_info.control_type == "Button"
                                and control.is_enabled()):
                            idle = True
                    except Exception:
                        pass
            count = _parse_baas_queue_count(texts)
            if count is not None:
                status = "闲置中" if idle else "执行中"
                return count, idle, f"已从 BAAS 窗口读取队列数 {count}（{status}）"
            errors.append(f"{title or hwnd}: 未找到‘队列中（数字）’文本")
        except Exception as exc:
            errors.append(f"{title or hwnd}: {exc}")
    return None, None, "；".join(errors[-2:])


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
    # This runtime flag belongs to the workflow run, not every retry attempt.
    update_wait = max(0.0, float(step.pop("startup_update_wait", 0)))
    timeout = int(step.get("timeout", 7200))
    quiet_seconds = float(step.get("completion_quiet_seconds", 20))
    start_markers = [str(x) for x in step.get("start_markers", ["Scheduler: Start task"])]
    completion_marker = str(step.get("completion_marker", "No task pending"))
    error_markers = [str(x) for x in step.get(
        "error_markers", ["No emulator with serial", "无法连接至ADB服务",
                          "Request human takeover", "RequestHumanTakeover",
                          "ScriptError", "GameNotRunningError"])]
    restart_markers = [str(x) for x in step.get(
        "restart_emulator_markers", ["No emulator with serial", "无法连接至ADB服务",
                                     "Request human takeover"])]
    service_unavailable_markers = [str(x) for x in step.get(
        "service_unavailable_markers", ["GameTooManyClickError"])]
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
    watchdog = EmulatorBlackScreenWatchdog(step, ctx, "碧蓝航线模拟器")

    def recover_updated_login_page() -> tuple[bool, str]:
        """Tap the relocated PRESS TO START control used by the current event skin."""
        adb = expand(str(step.get("adb_executable") or ctx.tool("adb")))
        device = str(step.get("device", "")).strip()
        point = step.get("service_recovery_tap", [1120, 535])
        if not device or not isinstance(point, list) or len(point) != 2:
            return False, "未配置登录页恢复设备或坐标"
        env = os.environ.copy()
        if step.get("adb_server_port"):
            env["ANDROID_ADB_SERVER_PORT"] = str(step["adb_server_port"])
        try:
            done = subprocess.run(
                [adb, "-s", device, "shell", "input", "tap",
                 str(int(point[0])), str(int(point[1]))],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=15, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        output = done.stdout.decode("utf-8", errors="replace").strip()
        if done.returncode != 0:
            return False, output or f"ADB 退出码 {done.returncode}"
        return True, f"已点击改版登录页 PRESS TO START：({point[0]}, {point[1]})"

    ctx.log(f"AzurLaneAutoScript 已启动；若日志 {log_retry_interval:g} 秒无更新，"
            "将循环点击“启动”")
    try:
        if update_wait:
            ctx.log(f"检测到昨日维护标志，已启动 ALAS，先等待 {update_wait:g} 秒自动更新")
            if ctx.stop_event.wait(update_wait):
                return Result(False, "自动更新等待期间任务被用户停止")
            # Update time must not consume the ordinary startup timeout.
            started_at = time.monotonic()
            next_log_retry_at = started_at + log_retry_interval
            ctx.log("ALAS 自动更新等待结束，重新开始计算启动超时并检测启动按钮")
        while time.monotonic() - started_at <= timeout:
            if ctx.stop_event.wait(min(1.0, log_retry_interval)):
                return Result(False, "任务被用户停止")
            black_screen = watchdog.poll()
            if black_screen is not None:
                return black_screen
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
                started_in_content = any(marker in content for marker in start_markers)
                if completion_at is not None and started_in_content:
                    ctx.log("ALAS 在空闲标记后又启动了新任务，继续等待调度器稳定")
                    completion_at = None
                    completion_log = None
                if started_in_content:
                    run_started = True
                folded_content = content.casefold()
                service_error = next((marker for marker in service_unavailable_markers
                                      if marker.casefold() in folded_content), None)
                if service_error:
                    service_error_count = int(step.get("_service_error_count", 0)) + 1
                    step["_service_error_count"] = service_error_count
                    recovered, recovery_message = recover_updated_login_page()
                    if service_error_count >= int(step.get(
                            "maintenance_confirm_count", 2)):
                        wait_seconds = float(step.get("next_day_update_wait", 600))
                        return Result(
                            False,
                            (f"游戏持续显示服务器不可用，判断今日维护并跳过；"
                             f"已标记明日启动后先等待 {wait_seconds:g} 秒自动更新"),
                            {"log": name, "marker": service_error,
                             "server_unavailable": True,
                             "defer_update_next_day": True,
                             "next_day_update_wait": wait_seconds,
                             "service_error_count": service_error_count,
                             "login_recovery_message": recovery_message,
                             "evidence": (str(watchdog.latest_path)
                                          if watchdog.latest_path.exists() else None)},
                            status="skipped",
                        )
                    delay = float(step.get(
                        "service_recovery_retry_delay", 60) if recovered else
                        step.get("service_unavailable_retry_delay", 600))
                    return Result(
                        False,
                        (f"AzurLaneAutoScript 报告 {service_error}；{recovery_message}；"
                         f"将保留模拟器并等待 {delay:g} 秒后重试脚本"),
                        {"log": name, "marker": service_error,
                         "server_unavailable": True, "retry_step": True,
                         "service_error_count": service_error_count,
                         "login_recovery_success": recovered,
                         "login_recovery_message": recovery_message,
                         "retry_delay_override": delay,
                         "evidence": (str(watchdog.latest_path)
                                      if watchdog.latest_path.exists() else None)},
                    )
                restart_error = next((marker for marker in restart_markers
                                      if marker.casefold() in folded_content), None)
                if restart_error:
                    return Result(False, f"AzurLaneAutoScript 报告需要重启模拟器的错误：{restart_error}",
                                  {"log": name, "marker": restart_error,
                                   "emulator_restart_requested": True,
                                   "retry_step": True,
                                   "evidence": (str(watchdog.latest_path)
                                                if watchdog.latest_path.exists() else None)})
                for error in error_markers:
                    if error.casefold() in folded_content:
                        restart_requested = any(marker.casefold() in folded_content
                                                for marker in restart_markers)
                        return Result(False, f"AzurLaneAutoScript 报告错误：{error}",
                                      {"log": name, "marker": error,
                                       "emulator_restart_requested": restart_requested,
                                       "retry_step": restart_requested,
                                       "evidence": (str(watchdog.latest_path)
                                                    if watchdog.latest_path.exists() else None)})
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
    # The landscape privacy agreement places its yellow consent button in the
    # lower-right quarter of the white panel (not at the very bottom edge).
    consent_button = (area(0.52, 0.74, 0.82, 0.88) if landscape
                      else area(0.52, 0.90, 0.95, 0.98))
    consent_orange_fraction = float(np.mean(
        (consent_button[:, :, 0] >= 15) & (consent_button[:, :, 0] <= 40) &
        (consent_button[:, :, 1] > 100) & (consent_button[:, :, 2] > 150)))

    home_center = area(0.195, 0.139, 0.703, 0.472)
    home_blue_fraction = float(np.mean(
        (home_center[:, :, 0] >= 90) & (home_center[:, :, 0] <= 125) &
        (home_center[:, :, 1] > 60) & (home_center[:, :, 2] > 35)))
    # The real lobby keeps the blue sky/HUD strip across most of the top edge.
    # Battle preparation pages can have a similarly blue centre, which caused
    # stage 1 to start its 90-minute timer while still inside Elite Dungeon.
    home_top = area(0.15, 0.0, 0.85, 0.14)
    home_top_blue_fraction = float(np.mean(
        (home_top[:, :, 0] >= 90) & (home_top[:, :, 0] <= 125) &
        (home_top[:, :, 1] > 60) & (home_top[:, :, 2] > 35)))
    home_top_orange_fraction = float(np.mean(
        (home_top[:, :, 0] < 35) & (home_top[:, :, 1] > 120) &
        (home_top[:, :, 2] > 100)))

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
                                 and consent_orange_fraction >= 0.25),
        "consent_orange_fraction": consent_orange_fraction,
        "completion_popup": landscape and popup_white_fraction >= 0.60,
        "popup_white_fraction": popup_white_fraction,
        "home_page": (home_blue_fraction >= 0.60
                      and home_top_blue_fraction >= 0.65
                      and home_top_orange_fraction <= 0.13),
        "home_blue_fraction": home_blue_fraction,
        "home_top_blue_fraction": home_top_blue_fraction,
        "home_top_orange_fraction": home_top_orange_fraction,
        "reward_page": reward_page,
        "progress_orange_fraction": progress_orange_fraction,
        "hundred_orange_fraction": hundred_orange_fraction,
        "chest_saturation": chest_saturation,
        "chest_lid_saturation": chest_lid_saturation,
        "all_chests_claimed": all_chests_claimed,
    }


def _naruto_lobby_icon_metrics(image, reference_paths, *,
                               threshold: float = 0.50,
                               min_matches: int = 3) -> dict[str, Any]:
    """Match fixed lobby entry icons instead of the time-varying sky/background."""
    import cv2
    import numpy as np

    # 忍者、天赋、装备、通灵、秘卷，以及右上角活动。
    regions = (
        (0.02, 0.83, 0.12, 0.99),
        (0.11, 0.83, 0.21, 0.99),
        (0.18, 0.83, 0.29, 0.99),
        (0.28, 0.83, 0.39, 0.99),
        (0.38, 0.82, 0.50, 0.99),
        (0.91, 0.00, 0.99, 0.16),
    )
    names = ("忍者", "天赋", "装备", "通灵", "秘卷", "活动")
    scores = [0.0] * len(regions)
    height, width = image.shape[:2]

    def crop_edges(source, region):
        h, w = source.shape[:2]
        x1, y1, x2, y2 = region
        crop = source[round(h * y1):round(h * y2),
                      round(w * x1):round(w * x2)]
        if crop.size == 0:
            return None
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        return cv2.Canny(gray, 70, 160)

    current_edges = [crop_edges(image, region) for region in regions]
    for value in reference_paths or []:
        reference = cv2.imread(expand(str(value)))
        if reference is None or reference.shape[1] <= reference.shape[0]:
            continue
        for index, region in enumerate(regions):
            template = crop_edges(reference, region)
            current = current_edges[index]
            if template is None or current is None:
                continue
            current = cv2.resize(current, (template.shape[1], template.shape[0]))
            if (float(np.mean(template > 0)) < 0.01
                    or float(np.mean(current > 0)) < 0.01):
                continue
            score = float(cv2.matchTemplate(
                template, current, cv2.TM_CCOEFF_NORMED)[0, 0])
            if np.isfinite(score):
                scores[index] = max(scores[index], score)

    matches = [name for name, score in zip(names, scores) if score >= threshold]
    return {
        "lobby_icon_scores": dict(zip(names, scores)),
        "lobby_icon_matches": matches,
        "lobby_icon_match_count": len(matches),
        "home_page": len(matches) >= min_matches,
    }


def run_naruto_shadow(step: dict[str, Any], ctx: RunContext) -> Result:
    """Keep starting Shadow Clone until it actually hands control to Naruto."""
    adb = ctx.tool("adb")
    device = str(step.get("device", "emulator-5554"))
    shadow_package = str(step.get("shadow_package", "com.yy.yfs"))
    game_package = str(step.get("game_package", "com.tencent.KiHan"))
    timeout = int(step.get("timeout", 7200))
    watchdog = EmulatorBlackScreenWatchdog(step, ctx, "火影忍者模拟器")

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

    def capture_image():
        """Capture and decode the emulator once for all Naruto visual checks."""
        try:
            import cv2
            import numpy as np
            done = subprocess.run([adb, "-s", device, "exec-out", "screencap", "-p"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if done.returncode != 0:
                return None
            return cv2.imdecode(np.frombuffer(done.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
        except (ImportError, OSError, subprocess.TimeoutExpired):
            return None

    def current_orientation() -> str:
        image = capture_image()
        if image is None:
            return "portrait"
        return "landscape" if image.shape[1] > image.shape[0] else "portrait"

    def oriented_value(name: str, fallback):
        orientation = current_orientation()
        settings = step.get("orientation_points", {}).get(orientation, {})
        return settings.get(name, step.get(name, fallback))

    def capture_metrics():
        image = capture_image()
        if image is None:
            return None
        metrics = _naruto_visual_metrics(image)
        metrics["_height"], metrics["_width"] = image.shape[:2]
        return metrics

    def locate_float_icon_details():
        """Locate Shadow Clone's red-cloud overlay in its half-hidden or full state.

        Desktop icons can contain much larger red areas (the Kuaishou icon was a
        real false positive).  The overlay's red cloud is a compact component less
        than one floating-ball width from the right edge.  Prefer the rightmost
        size-qualified component instead of the largest red component on screen.
        """
        try:
            import cv2
            image = capture_image()
            if image is None:
                return None
            height, width = image.shape[:2]
            hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
            red = (((hsv[:, :, 0] <= 12) | (hsv[:, :, 0] >= 165)) &
                   (hsv[:, :, 1] >= 100) & (hsv[:, :, 2] >= 70)).astype("uint8")
            red[:round(height * 0.08), :] = 0
            red[round(height * 0.92):, :] = 0
            edge_band = max(80, round(width * 0.07))
            red[:, :max(0, width - edge_band)] = 0
            count, _, stats, centers = cv2.connectedComponentsWithStats(red, 8)
            candidates = []
            for index in range(1, count):
                x = int(stats[index, cv2.CC_STAT_LEFT])
                y = int(stats[index, cv2.CC_STAT_TOP])
                box_width = int(stats[index, cv2.CC_STAT_WIDTH])
                box_height = int(stats[index, cv2.CC_STAT_HEIGHT])
                area = int(stats[index, cv2.CC_STAT_AREA])
                center_x, center_y = centers[index]
                right_gap = width - (x + box_width)
                if not (150 <= area <= 1800 and 12 <= box_width <= 72
                        and 24 <= box_height <= 76 and right_gap <= edge_band):
                    continue
                state = "half_hidden" if right_gap <= 7 or width - center_x <= 30 else "full"
                candidates.append((right_gap, -area, int(round(center_x)),
                                   int(round(center_y)), state,
                                   (x, y, box_width, box_height)))
            if not candidates:
                return None
            _, _, click_x, click_y, state, bounds = min(candidates)
            return {"point": (click_x, click_y), "state": state, "bounds": bounds}
        except (ImportError, OSError, subprocess.TimeoutExpired):
            return None

    def locate_float_icon():
        details = locate_float_icon_details()
        return details["point"] if details is not None else None

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

    def ensure_package_foreground(package: str, *, relaunch: bool = True,
                                  wait_seconds: float = 12) -> tuple[bool, str]:
        """Make coordinate taps conditional on the intended Android app being frontmost."""
        if focused_on(package):
            return True, f"前台应用已是 {package}"
        if not relaunch:
            return False, f"前台应用不是 {package}"
        launched = command(["shell", "monkey", "-p", package,
                            "-c", "android.intent.category.LAUNCHER", "1"])
        if isinstance(launched, Exception) or launched.returncode != 0:
            return False, f"重新切回 {package} 失败：{launched}"
        deadline = time.monotonic() + max(2.0, wait_seconds)
        while time.monotonic() < deadline:
            if focused_on(package):
                return True, f"已重新切回 {package}"
            if ctx.stop_event.wait(1):
                return False, "任务被用户停止"
        return False, f"重新拉起后仍未确认 {package} 位于前台"

    def read_ui_xml() -> str:
        remote_ui = "/sdcard/gameflow-ui.xml"
        dumped = command(["shell", "uiautomator", "dump", remote_ui], seconds=20)
        if isinstance(dumped, Exception) or dumped.returncode != 0:
            return ""
        xml = command(["shell", "cat", remote_ui], seconds=20)
        if isinstance(xml, Exception) or xml.returncode != 0:
            return ""
        return xml.stdout

    def find_ui_text_point(*labels: str) -> tuple[int, int] | None:
        """Return the centre of a visible Android control matching one of ``labels``."""
        xml = read_ui_xml()
        if not xml:
            return None
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
            return x, y
        return None

    def find_ui_text_point_exact(*labels: str) -> tuple[int, int] | None:
        """Match a complete label, used where substring matching is unsafe."""
        xml = read_ui_xml()
        if not xml:
            return None
        wanted = {label.strip().casefold() for label in labels}
        for node in re.findall(r"<node\b[^>]*>", xml):
            shown_values = {value.strip().casefold() for value in
                            re.findall(r'(?:text|content-desc)="([^"]*)"', node)
                            if value.strip()}
            bounds = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
            if not bounds or not wanted.intersection(shown_values):
                continue
            return ((int(bounds.group(1)) + int(bounds.group(3))) // 2,
                    (int(bounds.group(2)) + int(bounds.group(4))) // 2)
        return None

    def visible_ui_texts() -> set[str]:
        """Return the exact visible labels from the current Shadow Clone page."""
        xml = read_ui_xml()
        if not xml:
            return set()
        return {value.strip() for value in
                re.findall(r'(?:text|content-desc)="([^"]*)"', xml)
                if value.strip()}

    def tap_ui_text(*labels: str) -> bool:
        """Tap a visible Android control by text, avoiding stale fixed coordinates."""
        point = find_ui_text_point(*labels)
        if point is None:
            return False
        done = tap(*point)
        return not isinstance(done, Exception) and done.returncode == 0

    def click_shadow_continue_if_visible(*, log_missing: bool = True) -> Result:
        """Close the current update-log dialog once, without blind repeated taps.

        Recent Shadow Clone builds renamed the old ``确定`` action to ``继续`` and
        moved it to the lower-right of the dialog.  Only use the calibrated point
        after UIAutomator has positively found that label; otherwise the caller
        immediately attempts ``启动功能`` as requested.
        """
        # Exact matching is essential: Naruto's privacy button says “同意并继续”.
        # Treating that substring as Shadow Clone's update-log button traps startup
        # in an endless series of taps at the old portrait coordinate.
        detected = find_ui_text_point_exact("继续")
        if detected is None:
            if log_missing:
                ctx.log("未识别到影分身“继续”按钮，直接执行当前循环的原操作")
            return Result(True, "未发现影分身继续按钮", {"clicked": False})
        point = tuple(oriented_value("shadow_continue_point", [520, 1118]))
        done = tap(*point)
        if isinstance(done, Exception) or done.returncode != 0:
            return Result(False, f"点击影分身“继续”失败：{done}")
        ctx.log(f"识别到影分身更新日志“继续”，已点击新位置：{point}")
        if ctx.stop_event.wait(max(5.0, float(step.get("shadow_continue_wait", 8)))):
            return Result(False, "任务被用户停止")
        return Result(True, "影分身更新日志已继续", {"clicked": True})

    launch_deadline = time.monotonic() + float(step.get("app_launch_timeout", 90))
    launch = None
    while time.monotonic() < launch_deadline:
        black_screen = watchdog.poll()
        if black_screen is not None:
            return black_screen
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
        continued = click_shadow_continue_if_visible()
        if not continued.success:
            return continued
    command(["shell", "input", "swipe", "360", "1050", "360", "350", "600"])
    ctx.stop_event.wait(1)
    enable_point = tuple(oriented_value("enable_point", [360, 1215]))
    enable_deadline = time.monotonic() + float(step.get("enable_retry_timeout", 90))
    enable_confirm_timeout = max(6.0, float(step.get("enable_confirm_timeout", 15)))
    enable_confirm_poll = max(1.0, float(step.get("enable_confirm_poll_seconds", 2)))
    enable_stable_checks = max(2, int(step.get("enable_confirm_stable_checks", 2)))
    enable_icon_tolerance = max(8, int(step.get("enable_icon_tolerance", 40)))
    enable_success_labels = {str(value) for value in step.get(
        "enable_success_labels", ["停止功能", "功能运行中", "已启动", "关闭功能"])}

    def confirm_shadow_enabled(confirm_timeout: float) -> tuple[str, str]:
        """Require repeatable UI evidence instead of trusting a successful tap command.

        ADB only tells us that Android accepted the tap.  Shadow Clone is considered
        enabled when its status text changes, Naruto receives focus, or the floating
        icon remains at nearly the same position in consecutive fresh screenshots.
        The update-log Continue dialog takes precedence over red-pixel detection so
        its decoration cannot be mistaken for the floating icon.
        """
        deadline = time.monotonic() + max(enable_confirm_poll, confirm_timeout)
        previous_icon: tuple[int, int] | None = None
        stable_count = 0
        while time.monotonic() < deadline:
            if ctx.stop_event.is_set():
                return "stopped", "任务被用户停止"
            texts = visible_ui_texts()
            if "继续" in texts:
                return "continue", "检测到更新日志“继续”弹窗"
            matched = sorted(enable_success_labels.intersection(texts))
            if matched:
                return "enabled", f"界面状态文字：{matched[0]}"
            if focused_on(game_package):
                return "enabled", "火影忍者已进入前台"
            icon = locate_float_icon()
            if icon is None:
                previous_icon = None
                stable_count = 0
            elif (previous_icon is not None
                  and abs(icon[0] - previous_icon[0]) <= enable_icon_tolerance
                  and abs(icon[1] - previous_icon[1]) <= enable_icon_tolerance):
                stable_count += 1
            else:
                previous_icon = icon
                stable_count = 1
            if stable_count >= enable_stable_checks:
                return "enabled", (f"红色浮窗连续 {stable_count} 次稳定出现"
                                   f"（位置约 {icon}）")
            if ctx.stop_event.wait(enable_confirm_poll):
                return "stopped", "任务被用户停止"
        return "timeout", "未检测到状态文字、游戏前台或稳定红色浮窗"

    enable_attempt = 0
    while time.monotonic() < enable_deadline:
        if ctx.stop_event.is_set():
            return Result(False, "任务被用户停止")
        black_screen = watchdog.poll()
        if black_screen is not None:
            return black_screen
        # The update-log dialog can be shown again after any previous tap.  Check it at
        # the start of every retry, then give Shadow Clone enough time to redraw before
        # beginning a fresh iteration.  A missing optional button never blocks startup.
        if step.get("shadow_open_confirm", True):
            continued = click_shadow_continue_if_visible(log_missing=enable_attempt == 0)
            if not continued.success:
                return continued
            if continued.details.get("clicked"):
                ctx.log("启动功能循环检测到“继续”；等待界面刷新后重新开始本轮")
                continue
        # Avoid toggling an already enabled service off.  Even this pre-click check
        # requires two consistent observations rather than one red-pixel match.
        state, evidence = confirm_shadow_enabled(
            float(step.get("enable_precheck_timeout", 4)))
        if state == "stopped":
            return Result(False, evidence)
        if state == "continue":
            continued = click_shadow_continue_if_visible(log_missing=False)
            if not continued.success:
                return continued
            ctx.log("启动前确认阶段检测到“继续”；关闭弹窗后重新检查启动功能")
            continue
        if state == "enabled":
            ctx.log(f"已确认影分身启动功能原本已生效：{evidence}（共点击 {enable_attempt} 次）")
            break
        foreground_ok, foreground_message = ensure_package_foreground(shadow_package)
        if not foreground_ok:
            ctx.log(f"启动功能点击已拦截：{foreground_message}；稍后重新确认前台应用")
            if ctx.stop_event.wait(max(3.0, float(step.get("enable_retry_interval", 10)))):
                return Result(False, "任务被用户停止")
            continue
        live_enable_point = find_ui_text_point_exact("启动功能") or enable_point
        enable_attempt += 1
        done = tap(*live_enable_point)
        if isinstance(done, Exception) or done.returncode != 0:
            return Result(False, f"点击影分身“启动功能”失败：{done}")
        ctx.log(f"第 {enable_attempt} 次点击影分身“启动功能”：{live_enable_point}；"
                f"点击前已确认前台包 {shadow_package}")
        state, evidence = confirm_shadow_enabled(enable_confirm_timeout)
        if state == "stopped":
            return Result(False, evidence)
        if state == "continue":
            continued = click_shadow_continue_if_visible(log_missing=False)
            if not continued.success:
                return continued
            ctx.log(f"第 {enable_attempt} 次点击后出现“继续”，本次不视为启动成功；重新点击启动功能")
            continue
        if state == "enabled":
            ctx.log(f"已确认第 {enable_attempt} 次点击启动功能成功：{evidence}")
            break
        ctx.log(f"第 {enable_attempt} 次点击尚未确认生效：{evidence}；等待后重试")
        if ctx.stop_event.wait(max(8.0, float(step.get("enable_retry_interval", 10)))):
            return Result(False, "任务被用户停止")
    else:
        return Result(False, (f"循环点击影分身“启动功能” {enable_attempt} 次后仍未确认成功"
                              "（未检测到状态文字、游戏前台或稳定红色浮窗）"))
    start_deadline = time.monotonic() + float(step.get(
        "start_retry_timeout", step.get("game_focus_timeout", 180)))
    retry_interval = max(12.0, float(step.get("start_retry_interval", 15)))
    menu_wait = max(1.5, float(step.get("float_menu_wait", 2)))
    after_click_wait = max(4.0, float(step.get("after_start_click_wait", 5)))
    start_attempt = 0
    game_started = False
    privacy_accepted = False

    def confirm_game_foreground() -> bool:
        """Accept a stable Naruto foreground when the overlay hides after launch.

        The stop square remains the preferred confirmation.  Some Shadow Clone
        builds hide their floating overlay as soon as Naruto gains focus, so the
        square becomes impossible to inspect even though the game is running.
        Two fresh foreground probes are required for this fallback.
        """
        required = max(2, int(step.get("game_focus_stable_checks", 2)))
        interval = max(0.5, float(step.get("game_focus_confirm_interval", 2)))
        stable = 0
        for _ in range(required + 1):
            if focused_on(game_package):
                stable += 1
                if stable >= required:
                    return True
            else:
                stable = 0
            if ctx.stop_event.wait(interval):
                return False
        return False

    def classify_float_primary_control(icon_point: tuple[int, int]) -> str:
        """Classify the expanded menu's first control as Start (triangle) or Stop (square)."""
        try:
            import cv2
            import numpy as np
            image = capture_image()
            if image is None:
                return "unknown"
            center_x = int(oriented_value("float_start_x", 293))
            center_y = int(icon_point[1])
            radius = max(20, int(step.get("float_control_radius", 30)))
            top, bottom = max(0, center_y - radius), min(image.shape[0], center_y + radius)
            left, right = max(0, center_x - radius), min(image.shape[1], center_x + radius)
            crop = image[top:bottom, left:right]
            if crop.size == 0 or min(crop.shape[:2]) < 20:
                return "unknown"
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            white = (((hsv[:, :, 1] <= int(step.get("float_symbol_max_saturation", 80))) &
                      (hsv[:, :, 2] >= int(step.get("float_symbol_min_value", 190))))
                     .astype(np.uint8) * 255)
            count, labels, stats, _ = cv2.connectedComponentsWithStats(white, 8)
            candidates = []
            for index in range(1, count):
                x, y, width, height, area = (int(value) for value in stats[index])
                if area < int(step.get("float_symbol_min_area", 120)):
                    continue
                component = (labels == index).astype(np.uint8)
                contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL,
                                               cv2.CHAIN_APPROX_SIMPLE)
                if not contours:
                    continue
                contour = max(contours, key=cv2.contourArea)
                perimeter = cv2.arcLength(contour, True)
                vertices = len(cv2.approxPolyDP(contour, 0.06 * perimeter, True))
                fill = area / max(1, width * height)
                candidates.append((area, vertices, fill, width, height))
            if not candidates:
                return "unknown"
            _, vertices, fill, width, height = max(candidates)
            if vertices == 3:
                return "start"
            if (3 <= vertices <= 6 and fill >= float(step.get("float_stop_min_fill", 0.68))
                    and 0.65 <= width / max(1, height) <= 1.35):
                return "stop"
            return "unknown"
        except (ImportError, OSError, subprocess.TimeoutExpired):
            return "unknown"

    def inspect_float_primary_control(icon_point: tuple[int, int]) -> str:
        """Open the overlay, require two identical classifications, then collapse it."""
        opened = tap(*icon_point)
        if isinstance(opened, Exception) or opened.returncode != 0:
            return "unknown"
        if ctx.stop_event.wait(menu_wait):
            return "unknown"
        deadline = time.monotonic() + max(4.0, float(step.get(
            "float_stop_confirm_timeout", 12)))
        poll = max(1.0, float(step.get("float_stop_poll_seconds", 2)))
        required = max(2, int(step.get("float_stop_stable_checks", 2)))
        stable = 0
        previous = "unknown"
        result = "unknown"
        while time.monotonic() < deadline:
            if ctx.stop_event.is_set():
                break
            current = classify_float_primary_control(icon_point)
            stable = stable + 1 if current != "unknown" and current == previous else 1
            previous = current
            if stable >= required:
                result = current
                break
            if ctx.stop_event.wait(poll):
                break
        tap(*icon_point)
        ctx.stop_event.wait(min(1.0, menu_wait))
        return result

    # The ordinary red overlay is the boundary between the two startup stages.  From
    # here on, success means that exact overlay changes to Shadow Clone's stop symbol;
    # merely focusing Naruto or accepting an ADB tap is not sufficient evidence.
    while time.monotonic() < start_deadline:
        if ctx.stop_event.is_set():
            return Result(False, "任务被用户停止")
        black_screen = watchdog.poll()
        if black_screen is not None:
            return black_screen
        # “继续” is optional, but it may reappear while the red floating-window loop is
        # running.  Detect it before every attempt; after a click the helper waits and we
        # restart the loop instead of immediately stacking another tap on the new page.
        if step.get("shadow_open_confirm", True):
            continued = click_shadow_continue_if_visible(log_missing=start_attempt == 0)
            if not continued.success:
                return continued
            if continued.details.get("clicked"):
                ctx.log("浮窗启动循环检测到“继续”；等待界面刷新后重新识别红色浮窗")
                continue
        if not (focused_on(shadow_package) or focused_on(game_package)):
            foreground_ok, foreground_message = ensure_package_foreground(shadow_package)
            ctx.log(f"浮窗点击前检测到其他应用位于前台：{foreground_message}")
            if not foreground_ok:
                if ctx.stop_event.wait(retry_interval):
                    return Result(False, "任务被用户停止")
                continue
        start_attempt += 1
        icon_details = locate_float_icon_details()
        if icon_details is None:
            if (not step.get("require_float_stop_confirmation", True)
                    and start_attempt > 1 and confirm_game_foreground()):
                game_started = True
                ctx.log(f"第 {start_attempt} 次浮窗已隐藏，但连续确认火影忍者位于前台，"
                        "按游戏启动成功处理")
                break
            ctx.log(f"第 {start_attempt} 次未识别到红色浮窗，不执行盲点，稍后重试")
            if ctx.stop_event.wait(retry_interval):
                return Result(False, "任务被用户停止")
            continue
        icon_point = icon_details["point"]
        state_label = "半缩在右侧边缘" if icon_details["state"] == "half_hidden" else "完整显示"
        ctx.log(f"第 {start_attempt} 次识别到影分身红色浮窗（{state_label}）："
                f"点击点 {icon_point}，红云范围 {icon_details['bounds']}")
        opened = tap(*icon_point)
        if isinstance(opened, Exception) or opened.returncode != 0:
            ctx.log(f"第 {start_attempt} 次点击悬浮窗失败，稍后重新识别：{opened}")
            if ctx.stop_event.wait(retry_interval):
                return Result(False, "任务被用户停止")
            continue
        if ctx.stop_event.wait(menu_wait):
            return Result(False, "任务被用户停止")
        primary_control = classify_float_primary_control(icon_point)
        if primary_control == "stop":
            tap(*icon_point)
            game_started = True
            ctx.log(f"第 {start_attempt} 次展开悬浮菜单时已识别到方形终止符，脚本原本就在运行")
            break
        if primary_control != "start":
            tap(*icon_point)
            ctx.log(f"第 {start_attempt} 次悬浮菜单首按钮无法确认是三角启动符，"
                    "已收起菜单并取消本次坐标点击")
            if ctx.stop_event.wait(retry_interval):
                return Result(False, "任务被用户停止")
            continue
        ctx.log(f"第 {start_attempt} 次悬浮菜单首按钮已明确识别为三角形启动符")
        orientation = current_orientation()
        orientation_settings = step.get("orientation_points", {}).get(orientation, {})
        start_x = int(orientation_settings.get(
            "float_start_x", step.get("float_start_x", 293)))
        start_y_offset = float(orientation_settings.get(
            "float_start_y_offset", step.get("float_start_y_offset", 49)))
        start_y = int(icon_point[1] + start_y_offset)
        started = tap(start_x, start_y)
        if isinstance(started, Exception) or started.returncode != 0:
            ctx.log(f"第 {start_attempt} 次点击影分身“启动”失败，稍后重试：{started}")
            if ctx.stop_event.wait(retry_interval):
                return Result(False, "任务被用户停止")
            continue
        ctx.log(f"第 {start_attempt} 次点击影分身“启动”：({start_x}, {start_y})")
        if ctx.stop_event.wait(after_click_wait):
            return Result(False, "任务被用户停止")
        start_metrics = capture_metrics()
        consent_control = (find_ui_text_point("同意并继续", "接受并继续")
                           if not privacy_accepted else None)
        if ((consent_control is not None or
             (start_metrics and start_metrics.get("game_privacy_consent")))
                and not privacy_accepted):
            clicked_text = False
            if consent_control is not None:
                consent_done = tap(*consent_control)
                clicked_text = (not isinstance(consent_done, Exception)
                                and consent_done.returncode == 0)
            consent_point = tuple(oriented_value("game_consent_point", [530, 1200]))
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
        if focused_on(shadow_package):
            continued = click_shadow_continue_if_visible(log_missing=False)
            if not continued.success:
                return continued
            if continued.details.get("clicked"):
                ctx.log(f"第 {start_attempt} 次启动后检测到“继续”；重新开始浮窗识别")
                continue
            if tap_ui_text("立即开始", "允许"):
                ctx.log(f"第 {start_attempt} 次按界面文字点击影分身授权页按钮")
                if ctx.stop_event.wait(menu_wait):
                    return Result(False, "任务被用户停止")

        confirm_point = locate_float_icon()
        confirmed_control = (inspect_float_primary_control(confirm_point)
                             if confirm_point is not None else "unknown")
        if confirmed_control == "stop":
            game_started = True
            ctx.log(f"第 {start_attempt} 次重新展开悬浮菜单，连续识别到方形终止符，影分身正常启动")
            break
        if (not step.get("require_float_stop_confirmation", True)
                and confirm_point is None and confirm_game_foreground()):
            game_started = True
            ctx.log(f"第 {start_attempt} 次点击启动后浮窗已隐藏，且连续确认火影忍者位于前台，"
                    "按游戏启动成功处理")
            break
        if ctx.stop_event.is_set():
            return Result(False, "任务被用户停止")
        control_label = "三角形启动符仍存在" if confirmed_control == "start" else "首按钮无法确认"
        ctx.log(f"第 {start_attempt} 次未识别到方形终止符（{control_label}），"
                "重新点击悬浮窗及“启动”")
        if ctx.stop_event.wait(retry_interval):
            return Result(False, "任务被用户停止")

    if not game_started:
        return Result(False, f"循环点击悬浮窗和影分身“启动” {start_attempt} 次后仍未识别到终止符")
    ctx.log(f"已根据悬浮窗终止符确认影分身开始运行（共点击 {start_attempt} 次）")

    try:
        import cv2
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
        image = capture_image()
        if image is None:
            return None, None
        metrics = _naruto_visual_metrics(image)
        icon_metrics = _naruto_lobby_icon_metrics(
            image, step.get("lobby_reference_paths", []),
            threshold=float(step.get("lobby_icon_threshold", 0.50)),
            min_matches=int(step.get("lobby_icon_min_matches", 3)))
        metrics.update(icon_metrics)
        return image, metrics

    def wait_poll():
        return ctx.stop_event.wait(poll_seconds)

    def dismiss_lobby_blocking_dialog(image, metrics) -> bool:
        """Dismiss update/completion notices that cover the Naruto lobby.

        Shadow Clone occasionally opens Naruto behind a mandatory-looking update
        notice.  It is a Unity canvas, so UIAutomator usually exposes no button
        text; use the already validated landscape white-dialog classifier and the
        calibrated centre of the visible 确定/继续 button instead.
        """
        if not metrics or not metrics.get("completion_popup"):
            return False
        privacy_prompt = bool(metrics.get("game_privacy_consent"))
        evidence_key = "privacy_evidence_path" if privacy_prompt else "update_evidence_path"
        evidence_default = (ctx.root / "logs" / "naruto_privacy_prompt.png"
                            if privacy_prompt else
                            ctx.root / "logs" / "naruto_update_prompt.png")
        update_evidence = Path(expand(str(step.get(evidence_key, evidence_default))))
        if image is not None:
            try:
                cv2.imwrite(str(update_evidence), image)
            except OSError:
                pass
        point = tuple(oriented_value(
            "game_consent_point" if privacy_prompt else "popup_confirm_point",
            [817, 600] if privacy_prompt else [640, 565]))
        tap(*point)
        prompt_name = "游戏隐私协议" if privacy_prompt else "更新/结束提示"
        button_name = "同意" if privacy_prompt else "确定/继续"
        ctx.log(f"阶段1检测到遮挡大厅的{prompt_name}，已保存截图并点击“{button_name}”：{point}")
        return True

    # Stage 1: establish that the script has really reached the Naruto lobby.
    stage_started = time.monotonic()
    lobby_at = None
    ctx.log(f"阶段1：每隔 {poll_seconds:g} 秒截图，等待识别火影忍者大厅")
    wrong_foreground_checks = 0
    max_wrong_foreground_checks = max(
        2, int(step.get("stage1_wrong_foreground_checks", 2)))
    while time.monotonic() - stage_started <= initial_lobby_timeout:
        image, metrics = capture_frame()
        black_screen = watchdog.poll()
        if black_screen is not None:
            return black_screen
        if dismiss_lobby_blocking_dialog(image, metrics):
            if ctx.stop_event.wait(float(step.get("popup_close_wait", 5))):
                return Result(False, "任务被用户停止")
            continue
        if metrics and metrics["home_page"]:
            lobby_at = time.monotonic()
            ctx.log(f"阶段1完成：已识别火影忍者大厅，开始 {timer_label} 计时")
            break
        if metrics and metrics.get("lobby_icon_match_count", 0):
            ctx.log(
                "阶段1大厅入口图标尚未匹配完整："
                f"{metrics.get('lobby_icon_matches', [])} "
                f"({metrics.get('lobby_icon_match_count', 0)}/"
                f"{int(step.get('lobby_icon_min_matches', 3))})")
        if focused_on(game_package):
            wrong_foreground_checks = 0
        else:
            wrong_foreground_checks += 1
            current_name = ("影分身" if focused_on(shadow_package)
                            else "其他页面或桌面")
            ctx.log(f"阶段1：火影未在前台，当前为{current_name}（连续 "
                    f"{wrong_foreground_checks}/{max_wrong_foreground_checks} 次）")
            if wrong_foreground_checks >= max_wrong_foreground_checks:
                return Result(
                    False,
                    f"影分身启动后火影连续 {wrong_foreground_checks} 次未处于前台，"
                    "请求重启模拟器和脚本",
                    {"emulator_restart_requested": True,
                     "retry_step": True,
                     "wrong_foreground_checks": wrong_foreground_checks})
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
    waiting = _wait_with_emulator_watchdog(remaining, watchdog, ctx)
    if waiting is not None:
        return waiting
    if step.get("close_after_lobby_timer", False):
        return Result(True, f"大厅识别后已运行 {one_hour_seconds:g} 秒，准备截图并关闭模拟器",
                      {"completion_mode": "lobby_timer", "timer_seconds": one_hour_seconds})

    cycle = 0
    while time.monotonic() - stage_started <= timeout:
        cycle += 1
        image, metrics = capture_frame()
        black_screen = watchdog.poll()
        if black_screen is not None:
            return black_screen
        if metrics and metrics["completion_popup"]:
            ctx.log("阶段2：识别到横屏结束标志，点击“确定”并继续等待大厅")
            tap(*oriented_value("popup_confirm_point", [640, 515]))
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


@_serialized_desktop_click
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
    focused, focus_message = _focus_window_before_click(hwnd, "ALAS")
    if not focused:
        return False, focus_message
    rect = win32gui.GetWindowRect(hwnd)
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


@_serialized_desktop_click
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
    focused, focus_message = _focus_window_before_click(hwnd, title or "MaaEnd")
    if not focused:
        return False, focus_message
    rect = win32gui.GetWindowRect(hwnd)
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


@_serialized_desktop_click
def _click_game_client_ratio(process_image: str, title_contains: str,
                             x_ratio: float, y_ratio: float) -> tuple[bool, str]:
    """Focus a game and click a position relative to its render client area."""
    if os.name != "nt":
        return False, "游戏窗口恢复点击只支持 Windows"
    try:
        import psutil
        import win32gui
        import win32process
        import pyautogui
    except ImportError as exc:
        return False, f"缺少 Windows 窗口点击组件：{exc}"

    image_folded = process_image.strip().casefold()
    title_folded = title_contains.strip().casefold()
    matching_pids: set[int] = set()
    if image_folded:
        for process in psutil.process_iter(["pid", "name"]):
            try:
                if (process.info.get("name") or "").casefold() == image_folded:
                    matching_pids.add(int(process.info["pid"]))
            except (psutil.Error, TypeError, ValueError):
                continue
    candidates: list[tuple[int, int, str]] = []

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd).strip()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if matching_pids and pid not in matching_pids:
            return
        if not matching_pids and title_folded and title_folded not in title.casefold():
            return
        client = win32gui.GetClientRect(hwnd)
        area = max(0, client[2] - client[0]) * max(0, client[3] - client[1])
        if area > 10000:
            candidates.append((area, hwnd, title))

    try:
        win32gui.EnumWindows(collect, None)
    except Exception as exc:
        return False, f"枚举游戏窗口失败：{exc}"
    if not candidates:
        return False, f"未找到可点击的游戏窗口：{process_image or title_contains}"
    _, hwnd, title = max(candidates)
    focused, message = _focus_window_before_click(hwnd, title or "游戏")
    if not focused:
        return False, message
    try:
        left, top = win32gui.ClientToScreen(hwnd, (0, 0))
        client = win32gui.GetClientRect(hwnd)
        width = max(1, client[2] - client[0])
        height = max(1, client[3] - client[1])
        x = left + round(width * min(max(x_ratio, 0.0), 1.0))
        y = top + round(height * min(max(y_ratio, 0.0), 1.0))
        pyautogui.click(x, y)
        return True, f"已置前游戏并点击客户区坐标（{x}, {y}）"
    except Exception as exc:
        return False, f"游戏窗口恢复点击失败：{exc}"


@_serialized_desktop_click
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
        # Launchers such as OneDragon intentionally detach the real Qt GUI from
        # their original process tree. A strong configured title is therefore
        # authoritative even when the GUI PID belongs to uv/python.
        if pid in pids or title_match:
            rect = win32gui.GetWindowRect(hwnd)
            area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
            if area > 10000:
                candidates.append((20 if title_match else 1, area, hwnd, title, rect))

    win32gui.EnumWindows(collect, None)
    if not candidates:
        related = len(pids)
        suffix = f"（已发现 {related} 个相关进程）" if related else "（尚无相关进程）"
        return False, "脚本尚未产生匹配的可见窗口" + suffix
    _, _, hwnd, title, rect = max(candidates)
    focused, focus_message = _focus_window_before_click(hwnd, title or "每日脚本")
    if not focused:
        return False, focus_message
    rect = win32gui.GetWindowRect(hwnd)
    folded_names = [name.casefold() for name in button_names]
    try:
        from pywinauto import Application
        window = Application(backend="uia").connect(handle=hwnd, timeout=3).window(handle=hwnd)
        for control in window.descendants(control_type="Button"):
            name = control.window_text().strip()
            if name and any(target == name.casefold() or target in name.casefold()
                            for target in folded_names):
                focused, focus_message = _focus_window_before_click(
                    hwnd, title or "每日脚本", timeout=3.0)
                if not focused:
                    return False, focus_message
                control.click_input()
                return True, f"已通过 GUI 控件点击“{name}”"
    except Exception:
        pass
    focused, focus_message = _focus_window_before_click(
        hwnd, title or "每日脚本", timeout=3.0)
    if not focused:
        return False, focus_message
    rect = win32gui.GetWindowRect(hwnd)
    left, top, right, bottom = rect
    x = left + round((right - left) * min(max(x_ratio, 0.0), 1.0))
    y = top + round((bottom - top) * min(max(y_ratio, 0.0), 1.0))
    try:
        import pyautogui
        pyautogui.click(x, y)
        return True, f"已在“{title}”窗口点击启动位置（{x}, {y}）"
    except Exception as exc:
        return False, f"GUI 点击失败：{exc}"


def _close_matching_script_gui(root_pid: int, process_images: list[str],
                               title_hints: list[str]) -> tuple[int, str]:
    """Close assistant windows matched by their configured strong title."""
    if os.name != "nt":
        return 0, "脚本窗口清理只支持 Windows"
    folded_hints = [hint.strip().casefold() for hint in title_hints if hint.strip()]
    if not folded_hints:
        return 0, "未配置脚本窗口标题，跳过按窗口清理"
    try:
        import win32con
        import win32gui
        import win32process
    except ImportError as exc:
        return 0, f"缺少 Windows 窗口清理组件：{exc}"

    windows: list[tuple[int, int, str]] = []

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd).strip()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        title_match = any(hint in title.casefold() for hint in folded_hints)
        if title_match:
            windows.append((hwnd, pid, title))

    win32gui.EnumWindows(collect, None)
    if not windows:
        return 0, "未发现需要关闭的脚本 GUI 窗口"

    for hwnd, _, _ in windows:
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if all(not win32gui.IsWindow(hwnd) for hwnd, _, _ in windows):
            break
        time.sleep(0.1)

    remaining_pids = {pid for hwnd, pid, _ in windows if win32gui.IsWindow(hwnd)}
    for pid in remaining_pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    titles = "、".join(dict.fromkeys(title or str(pid) for _, pid, title in windows))
    return len(windows), f"已关闭脚本 GUI：{titles}"


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
            focused, message = _focus_window_before_click(
                hwnd, title or process_image, timeout=min(3.0, max(0.1, timeout)))
            if focused:
                return Result(True, f"已恢复并置前“{title or process_image}”")
            return Result(False, f"找到游戏窗口但置前失败：{message}")
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
    update_markers = [str(x) for x in step.get("update_markers", [])]
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
    max_start_clicks = max(1, int(step.get("max_start_clicks", 20)))
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
    state_stall_overrides = {
        str(pattern): float(seconds)
        for pattern, seconds in step.get("state_stall_overrides", {}).items()
    }
    state_recovery_seconds = float(step.get("state_recovery_seconds", 120))
    targeted_pattern_text = str(step.get("targeted_state_recovery_regex", "")).strip()
    targeted_pattern = re.compile(targeted_pattern_text) if targeted_pattern_text else None
    targeted_after_seconds = float(step.get("targeted_state_recovery_after", 45))
    targeted_interval = float(step.get("targeted_state_recovery_interval", 45))
    targeted_max = int(step.get("targeted_state_recovery_max", 3))
    targeted_clicks = list(step.get("targeted_state_recovery_clicks", []))
    targeted_recovery_count = 0
    targeted_recovery_next_at = None
    last_state_key = None
    last_state_change_at = time.monotonic()
    state_recovery_at = None
    launcher_exit_at = None
    launcher_exit_code = None
    gui_ready_timeout = max(1.0, float(step.get("gui_ready_timeout", 300)))
    ctx.log(f"{display_name}已打开；日志每沉默 {retry_interval:g} 秒便重新点击启动，"
            f"最多点击 {max_start_clicks} 次")

    def current_state_stall_seconds() -> float:
        for pattern, seconds in state_stall_overrides.items():
            try:
                if last_state_key and re.search(pattern, last_state_key):
                    return seconds
            except re.error:
                continue
        return state_stall_seconds

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
                            targeted_recovery_count = 0
                            targeted_recovery_next_at = (last_state_change_at
                                                         + targeted_after_seconds)
                            if step.get("publish_state_updates", True):
                                ctx.log(f"{display_name}当前状态：{state_key}")
                for marker in update_markers:
                    if marker in content:
                        capture_reward(force=True)
                        return Result(
                            False,
                            f"{display_name}检测到需要更新：{marker}",
                            {"log": name, "marker": marker,
                             "start_clicks": click_count,
                             "screenshot": screenshot_path if screenshot_saved else None},
                            status="needs_update")
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
            effective_stall_seconds = current_state_stall_seconds()
            if (run_started and state_pattern is not None and last_state_key
                    and effective_stall_seconds > 0
                    and time.monotonic() - last_state_change_at >= effective_stall_seconds):
                if state_recovery_at is None:
                    state_recovery_at = time.monotonic()
                    foreground = _bring_game_window_to_front(
                        game_process, game_title,
                        float(step.get("game_foreground_timeout", 180)), ctx.stop_event)
                    ctx.log(f"{display_name}状态“{last_state_key}”持续"
                            f" {effective_stall_seconds:g} 秒未变化；重新置前游戏：{foreground.message}")
                elif time.monotonic() - state_recovery_at >= state_recovery_seconds:
                    return failure(f"{display_name}卡在“{last_state_key}”超过"
                                   f" {effective_stall_seconds + state_recovery_seconds:g} 秒",
                                   stalled_state=last_state_key)
            if (run_started and targeted_pattern is not None and last_state_key
                    and targeted_pattern.search(last_state_key)
                    and targeted_recovery_next_at is not None
                    and time.monotonic() >= targeted_recovery_next_at
                    and targeted_recovery_count < targeted_max):
                targeted_recovery_count += 1
                results = []
                for index, click in enumerate(targeted_clicks, 1):
                    try:
                        x_ratio = float(click[0])
                        y_ratio = float(click[1])
                        delay_after = float(click[2]) if len(click) > 2 else 0.8
                    except (TypeError, ValueError, IndexError):
                        results.append(f"第 {index} 个坐标配置无效")
                        continue
                    clicked, message = _click_game_client_ratio(
                        game_process, game_title, x_ratio, y_ratio)
                    results.append(message)
                    if not clicked:
                        break
                    if delay_after > 0 and ctx.stop_event.wait(delay_after):
                        return failure("任务被用户停止")
                ctx.log(f"{display_name}针对状态“{last_state_key}”执行第"
                        f" {targeted_recovery_count} 次恢复：{'；'.join(results)}")
                targeted_recovery_next_at = time.monotonic() + targeted_interval
            # A detached OneDragon Qt window can stop servicing Win32 title
            # queries while its worker is busy.  Once fresh logs prove the run
            # started, querying that GUI is both unnecessary and capable of
            # blocking this monitor until the whole daily run finishes.
            if proc.poll() is not None and not run_started:
                related_running = (_process_image_exists(process_images)
                                   or _matching_visible_window_exists(title_hints))
                if related_running:
                    launcher_exit_at = None
                else:
                    if launcher_exit_at is None:
                        launcher_exit_at = time.monotonic()
                        launcher_exit_code = proc.returncode
                        ctx.log(f"{display_name}启动器已退出（退出码 {proc.returncode}）；"
                                f"真正 GUI 会由独立进程延迟创建，继续等待"
                                f" {gui_ready_timeout:g} 秒")
                    elif time.monotonic() - launcher_exit_at >= gui_ready_timeout:
                        return failure(
                            f"{display_name}启动器退出后 {gui_ready_timeout:g} 秒内"
                            "仍未出现匹配 GUI 或任务启动日志",
                            launcher_exit_code=launcher_exit_code,
                            gui_ready_timeout=gui_ready_timeout)
            if not run_started and time.monotonic() >= next_click_at:
                if click_count >= max_start_clicks:
                    return failure(
                        f"{display_name}已达到 {max_start_clicks} 次启动点击上限，"
                        "日志仍未进入任务状态",
                        start_clicks=click_count,
                        max_start_clicks=max_start_clicks)
                click_count += 1
                clicked, message = _click_named_gui_button(
                    proc.pid, process_images, title_hints, button_names,
                    float(step.get("gui_click_x_ratio", 0.5)),
                    float(step.get("gui_click_y_ratio", 0.9)))
                ctx.log(f"{display_name}第 {click_count} 次点击启动：{message}")
                next_click_at = time.monotonic() + retry_interval
                if not clicked:
                    continue
        return failure(f"等待 {display_name}完成超时（{timeout:g} 秒）")
    finally:
        if step.get("close_on_complete", True):
            if os.name == "nt":
                try:
                    _, close_message = _close_matching_script_gui(
                        proc.pid, process_images, title_hints)
                    ctx.log(close_message)
                except Exception as exc:
                    ctx.log(f"清理 {display_name} GUI 时发生异常，继续执行进程清理：{exc}")
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
    failed_settle_seconds = float(step.get("failed_settle_seconds", 45))
    all_primary_terminal_at: float | None = None
    credit_menu_wait_started_at: float | None = None
    credit_menu_next_recovery_at: float | None = None
    credit_menu_recovery_count = 0
    daily_failure_recovery_count = 0
    credit_menu_recovery_after = float(step.get("credit_menu_recovery_after", 6))
    credit_menu_recovery_interval = float(step.get("credit_menu_recovery_interval", 5))
    credit_menu_recovery_max = int(step.get("credit_menu_recovery_max", 3))
    rotated_glob = expand(str(step.get(
        "framework_rotated_glob",
        framework_log.parent / "maafw.bak.*.log" if framework_log is not None else "")))
    known_rotated_logs = set(glob.glob(rotated_glob)) if rotated_glob else set()

    def record_framework_events(content: str) -> None:
        nonlocal last_primary_terminal_at, credit_menu_wait_started_at
        nonlocal credit_menu_next_recovery_at
        nonlocal daily_failure_recovery_count
        for line in content.splitlines():
            if re.search(
                    r'msg=Node\.PipelineNode\.Starting.*?'
                    r'"name":"__ScenePrivateWorldEnterMenuList"', line):
                if credit_menu_wait_started_at is None:
                    credit_menu_wait_started_at = time.monotonic()
                    credit_menu_next_recovery_at = (credit_menu_wait_started_at
                                                    + credit_menu_recovery_after)
            if re.search(
                    r'msg=Node\.PipelineNode\.(Succeeded|Failed).*?'
                    r'"name":"__ScenePrivateWorldEnterMenuList"', line):
                credit_menu_wait_started_at = None
                credit_menu_next_recovery_at = None
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
                # MaaEnd v2.20 can miss the enabled bottom-right action button on
                # crafting/gear pages after a UI refresh.  The error page remains
                # open after DailyRewardStart becomes terminal, so advance it once;
                # the workflow retry then verifies the daily task from fresh state.
                if (entry == "DailyRewardStart"
                        and step.get("daily_action_failure_recovery", False)
                        and daily_failure_recovery_count
                        < int(step.get("daily_action_recovery_max", 1))):
                    daily_failure_recovery_count += 1
                    clicked, message = _click_game_client_ratio(
                        game_process, game_title,
                        float(step.get("daily_action_x_ratio", 0.893)),
                        float(step.get("daily_action_y_ratio", 0.917)))
                    ctx.log(f"终末地每日动作按钮失败恢复第 "
                            f"{daily_failure_recovery_count} 次：{message}")
            if entry == "CreditShoppingMain":
                credit_menu_wait_started_at = None
                credit_menu_next_recovery_at = None
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
            if ctx.stop_event.wait(float(step.get("poll_interval", 1))):
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
                for line in content.splitlines():
                    if any(marker in line for marker in start_markers):
                        run_started = True
                        ctx.log("已确认 MaaEnd“全套日常”开始执行")
                    task_plan = re.search(
                        r"实例 全套日常: 开始执行任务, 数量:\s*(\d+),\s*分段:\s*primary:(\d+),\s*trailing:(\d+)",
                        line)
                    if task_plan:
                        expected_total = int(task_plan.group(1))
                        expected_primary = int(task_plan.group(2))
                        trailing = int(task_plan.group(3))
                        ctx.log(f"终末地本轮规定任务：主要 {expected_primary} 项，"
                                f"收尾 {trailing} 项，共 {expected_total} 项")
                        # Do not manipulate a Unity full-screen window while MaaEnd is
                        # still creating its Win32 controller.  This marker is emitted
                        # only after the controller and resources are ready.
                        if (step.get("bring_game_to_front", False)
                                and not game_foreground_attempted):
                            game_foreground_attempted = True
                            foreground = _bring_game_window_to_front(
                                game_process, game_title,
                                float(step.get("game_foreground_timeout", 180)),
                                ctx.stop_event)
                            ctx.log(f"终末地游戏窗口置前：{foreground.message}")
                            game_foregrounded = foreground.success
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

            now = time.monotonic()
            if (credit_menu_next_recovery_at is not None
                    and now >= credit_menu_next_recovery_at
                    and credit_menu_recovery_count < credit_menu_recovery_max):
                credit_menu_recovery_count += 1
                clicked, message = _click_game_client_ratio(
                    game_process, game_title,
                    float(step.get("credit_menu_x_ratio", 0.970)),
                    float(step.get("credit_menu_y_ratio", 0.056)))
                ctx.log(f"终末地大世界菜单恢复第 {credit_menu_recovery_count} 次：{message}")
                credit_menu_next_recovery_at = now + credit_menu_recovery_interval

            primary_ids = (submitted_ids[:expected_primary]
                           if expected_primary is not None else [])
            failed_primary = [task_id for task_id in primary_ids if task_id in framework_failed]
            pending_primary = [task_id for task_id in primary_ids
                               if (task_id not in framework_succeeded
                                   and task_id not in framework_failed)]
            if primary_ids and not pending_primary:
                if all_primary_terminal_at is None:
                    all_primary_terminal_at = time.monotonic()
            else:
                all_primary_terminal_at = None
            if (failed_primary and all_primary_terminal_at is not None
                    and ((completion_seen and expected_total is not None
                          and len(submitted_ids) >= expected_total)
                         or time.monotonic() - all_primary_terminal_at
                         >= failed_settle_seconds)):
                capture_endfield(force=True)
                task_id = failed_primary[0]
                return Result(False, f"终末地规定任务失败：{framework_failed[task_id]}",
                              {"task_id": task_id, "framework_log": str(framework_log),
                               "terminal_primary": sorted(
                                   framework_succeeded.union(framework_failed)
                                   .intersection(primary_ids)),
                               "screenshot": screenshot_path if screenshot_saved else None})
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
        if framework_failed:
            task_id = next(iter(framework_failed))
            return Result(False, f"终末地规定任务失败：{framework_failed[task_id]}",
                          {"task_id": task_id, "framework_log": str(framework_log),
                           "screenshot": screenshot_path if screenshot_saved else None})
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


@_serialized_desktop_click
def _click_gumballs_start_button(root_pid: int, x_ratio: float, y_ratio: float) -> tuple[bool, str]:
    """Click MFAAvalonia's Start Task button, preferring its UIA control."""
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
    focused, focus_message = _focus_window_before_click(hwnd, title or "不思议迷宫脚本")
    if not focused:
        return False, focus_message
    # Avalonia exposes the toolbar action through UI Automation on current MFA
    # builds.  Invoking that control is resilient to window position, DPI and a
    # game window briefly overlapping the script GUI.  Keep the calibrated
    # coordinate only for older builds that do not expose an accessible button.
    try:
        from pywinauto import Desktop
        window = Desktop(backend="uia").window(handle=hwnd)
        for control in window.descendants(control_type="Button"):
            name = (control.window_text() or "").strip()
            if name in ("开始任务", "启动任务", "开始", "Start Tasks", "Start"):
                try:
                    control.invoke()
                except Exception:
                    control.click_input()
                return True, f"已置前“{title}”并通过 GUI 控件点击“{name}”"
    except Exception:
        pass
    rect = win32gui.GetWindowRect(hwnd)
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
    first_click_at = None
    start_click_attempts = 0
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
    watchdog = EmulatorBlackScreenWatchdog(step, ctx, "不思议迷宫模拟器")

    def click_start(number: int) -> Result:
        ready_timeout = max(1.0, float(step.get("gui_ready_timeout", 60)))
        retry_interval = max(0.5, float(step.get("gui_ready_retry_interval", 2)))
        deadline = time.monotonic() + ready_timeout
        last_message = "不思议迷宫脚本窗口尚未出现"
        while time.monotonic() <= deadline:
            clicked, message = _click_gumballs_start_button(
                proc.pid, float(step.get("gui_click_x_ratio", 0.963)),
                float(step.get("gui_click_y_ratio", 0.136)))
            last_message = message
            if clicked:
                ctx.log(message)
                return Result(True, message, {"round": number})
            if proc.poll() is not None and not _process_image_exists([process_image]):
                break
            if ctx.stop_event.wait(retry_interval):
                return Result(False, "任务被用户停止", {"round": number})
        ctx.log(f"等待脚本 GUI {ready_timeout:g} 秒后仍无法点击：{last_message}")
        return Result(False, last_message, {"round": number,
                                            "gui_ready_timeout": ready_timeout})

    ctx.log(f"不思议迷宫脚本已启动，将在 {initial_delay:g} 秒后点击第一轮“开始任务”")
    try:
        waiting = _wait_with_emulator_watchdog(initial_delay, watchdog, ctx)
        if waiting is not None:
            return waiting
        first = click_start(1)
        if not first.success:
            return first
        round_number = 1
        click_at = time.monotonic()
        first_click_at = click_at
        start_click_attempts = 1

        while time.monotonic() - started_at <= timeout:
            if ctx.stop_event.wait(1):
                return Result(False, "任务被用户停止")
            black_screen = watchdog.poll()
            if black_screen is not None:
                return black_screen
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
                        first_click_at = click_at
                        start_click_attempts = 1
                        last_business_at = time.monotonic()
            if not round_started and click_at is not None:
                now = time.monotonic()
                retry_interval = max(5.0, float(step.get("start_click_retry_interval", 30)))
                max_attempts = max(1, int(step.get("max_start_click_attempts", 3)))
                if (start_click_attempts < max_attempts
                        and now - click_at >= retry_interval):
                    start_click_attempts += 1
                    ctx.log(f"不思议迷宫第 {round_number} 轮点击后尚无启动日志，"
                            f"第 {start_click_attempts} 次重新置前并点击“开始任务”")
                    retry_click = click_start(round_number)
                    if not retry_click.success:
                        return retry_click
                    click_at = time.monotonic()
                if (first_click_at is not None
                        and now - first_click_at > round_start_timeout):
                    return Result(False, f"点击后 {round_start_timeout:g} 秒内未检测到第 {round_number} 轮启动日志",
                                  {"start_click_attempts": start_click_attempts})
            if (round_started and business_silence_timeout > 0
                    and time.monotonic() - last_business_at >= business_silence_timeout):
                if stalled_retries < max_stalled_retries:
                    stalled_retries += 1
                    ctx.log(f"不思议迷宫第{round_number}轮在“{last_business_line}”后"
                            f" {business_silence_timeout:g} 秒无业务进展；"
                            f"重启脚本并重试本轮（{stalled_retries}/{max_stalled_retries}）")
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
                    retry_round = click_start(round_number)
                    if not retry_round.success:
                        return retry_round
                    round_started = False
                    click_at = time.monotonic()
                    first_click_at = click_at
                    start_click_attempts = 1
                    last_business_at = time.monotonic()
                    last_business_line = f"重试第{round_number}轮启动"
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
        if step.get("verify_started", False):
            verify_timeout = max(10.0, float(step.get("verify_timeout", 60)))
            restart_after = max(5.0, float(step.get("restart_if_stuck_after", 20)))
            deadline = time.monotonic() + verify_timeout
            verify_started = time.monotonic()
            restarted = False
            last_state = "未读取到实例状态"
            while time.monotonic() < deadline:
                if ctx.stop_event.is_set():
                    return Result(False, "任务被用户停止")
                try:
                    listed = subprocess.run(
                        [exe, "list2"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        timeout=15,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    rows = _decode_process_output(listed.stdout).splitlines()
                    row = next((value for value in rows
                                if value.split(",", 1)[0].strip() == index), "")
                    fields = row.split(",")
                    if len(fields) >= 7:
                        state, player_pid, vm_pid = fields[4:7]
                        last_state = (f"状态={state}，窗口进程={player_pid}，"
                                      f"虚拟机进程={vm_pid}")
                        if state.strip() == "1" and int(player_pid) > 0 and int(vm_pid) > 0:
                            return Result(True, f"雷电实例 {index} 已完整启动（{last_state}）",
                                          {"instance": index, "restarted": restarted})
                except (OSError, subprocess.TimeoutExpired, ValueError):
                    pass
                if (not restarted and time.monotonic() - verify_started >= restart_after):
                    restarted = True
                    ctx.log(f"雷电实例 {index} 出现假启动（{last_state}），自动关闭后重新启动")
                    ctx.command([exe, "quit", "--index", index], 60)
                    if ctx.stop_event.wait(float(step.get("stuck_restart_delay", 5))):
                        return Result(False, "任务被用户停止")
                    relaunched = ctx.command([exe, "launch", "--index", index], 120)
                    if not relaunched.success:
                        return Result(False, f"雷电实例 {index} 自动重启失败：{relaunched.message}")
                if ctx.stop_event.wait(2):
                    return Result(False, "任务被用户停止")
            return Result(False, f"雷电实例 {index} 启动状态异常：{last_state}")
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
