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


def _matching_marker(content: str, markers: list[str]) -> str | None:
    """Return the configured marker exactly as written, using case-insensitive matching."""
    folded = content.casefold()
    return next((marker for marker in markers
                 if marker and marker.casefold() in folded), None)


def _update_skip_result(label: str, step: dict[str, Any], marker: str,
                        details: dict[str, Any] | None = None,
                        *, reason_code: str = "script_or_game_update") -> Result:
    """Stop the current run immediately when a mandatory update is detected."""
    payload = dict(details or {})
    payload.update({
        "marker": marker,
        "reason_code": reason_code,
        "retryable": False,
    })
    if step.get("defer_update_next_day_on_update", True):
        wait_seconds = float(step.get("next_day_update_wait", 600))
        payload.update({
            "defer_update_next_day": True,
            "next_day_update_wait": wait_seconds,
        })
    return Result(
        False,
        f"{label}检测到游戏或脚本需要更新，已跳过本次执行：{marker}",
        payload,
        status="needs_update",
    )


def _maintenance_skip_result(label: str, step: dict[str, Any], marker: str,
                             details: dict[str, Any] | None = None) -> Result:
    """Treat an authoritative maintenance signal as a non-retryable daily skip."""
    payload = dict(details or {})
    payload.update({
        "marker": marker,
        "reason_code": "maintenance",
        "retryable": False,
    })
    if step.get("defer_update_next_day_on_maintenance", True):
        wait_seconds = float(step.get("next_day_update_wait", 600))
        payload.update({
            "defer_update_next_day": True,
            "next_day_update_wait": wait_seconds,
        })
    return Result(
        False,
        f"{label}检测到服务器维护，已跳过今日任务：{marker}",
        payload,
        status="skipped",
    )


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


def _screen_looks_like_blue_archive_update(data: bytes,
                                           step: dict[str, Any]) -> tuple[bool, dict[str, float]]:
    """Detect the real in-game download confirmation dialog.

    Blue Archive's ordinary initialization and title screens also contain a
    cyan spinner and pale text in the lower-left corner.  Those signals caused
    the old classifier to mistake “点击任意区域进入游戏” for a mandatory update.
    Require both the large central light dialog and its cyan right-hand button.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return False, {"ba_update_dialog_light_ratio": -1.0,
                       "ba_update_confirm_cyan_ratio": -1.0}
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        return False, {"ba_update_dialog_light_ratio": -1.0,
                       "ba_update_confirm_cyan_ratio": -1.0}
    height, width = image.shape[:2]
    dialog = image[round(height * 0.19):round(height * 0.80),
                   round(width * 0.28):round(width * 0.72)]
    confirm = image[round(height * 0.65):round(height * 0.76),
                    round(width * 0.50):round(width * 0.70)]
    if dialog.size == 0 or confirm.size == 0:
        return False, {"ba_update_dialog_light_ratio": -1.0,
                       "ba_update_confirm_cyan_ratio": -1.0}
    dialog_hsv = cv2.cvtColor(dialog, cv2.COLOR_BGR2HSV)
    confirm_hsv = cv2.cvtColor(confirm, cv2.COLOR_BGR2HSV)
    dialog_light = (
        (dialog_hsv[:, :, 1] <= 70) & (dialog_hsv[:, :, 2] >= 180)
    )
    confirm_cyan = (
        (confirm_hsv[:, :, 0] >= 80) & (confirm_hsv[:, :, 0] <= 105) &
        (confirm_hsv[:, :, 1] >= 80) & (confirm_hsv[:, :, 2] >= 140)
    )
    dialog_light_ratio = float(np.mean(dialog_light))
    confirm_cyan_ratio = float(np.mean(confirm_cyan))
    metrics = {
        "ba_update_dialog_light_ratio": round(dialog_light_ratio, 5),
        "ba_update_confirm_cyan_ratio": round(confirm_cyan_ratio, 5),
    }
    return (
        dialog_light_ratio >= float(
            step.get("ba_update_dialog_light_ratio", 0.75))
        and confirm_cyan_ratio >= float(
            step.get("ba_update_confirm_cyan_ratio", 0.60)),
        metrics,
    )


def _screen_looks_like_blue_archive_title(data: bytes,
                                          step: dict[str, Any]) -> tuple[bool, dict[str, float]]:
    """Detect the title page that asks the user to click any area."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return False, {"ba_title_logo_cyan_ratio": -1.0,
                       "ba_title_strip_light_ratio": -1.0,
                       "ba_title_strip_gray_ratio": -1.0}
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        return False, {"ba_title_logo_cyan_ratio": -1.0,
                       "ba_title_strip_light_ratio": -1.0,
                       "ba_title_strip_gray_ratio": -1.0}
    height, width = image.shape[:2]
    logo = image[0:round(height * 0.22), 0:round(width * 0.30)]
    strip = image[round(height * 0.83):round(height * 0.91),
                  round(width * 0.25):round(width * 0.75)]
    if logo.size == 0 or strip.size == 0:
        return False, {"ba_title_logo_cyan_ratio": -1.0,
                       "ba_title_strip_light_ratio": -1.0,
                       "ba_title_strip_gray_ratio": -1.0}
    logo_hsv = cv2.cvtColor(logo, cv2.COLOR_BGR2HSV)
    strip_hsv = cv2.cvtColor(strip, cv2.COLOR_BGR2HSV)
    logo_cyan = (
        (logo_hsv[:, :, 0] >= 80) & (logo_hsv[:, :, 0] <= 110) &
        (logo_hsv[:, :, 1] >= 70) & (logo_hsv[:, :, 2] >= 120)
    )
    strip_light = (
        (strip_hsv[:, :, 1] <= 80) & (strip_hsv[:, :, 2] >= 170)
    )
    strip_gray = (
        (strip_hsv[:, :, 1] <= 80) &
        (strip_hsv[:, :, 2] >= 60) & (strip_hsv[:, :, 2] <= 230)
    )
    logo_cyan_ratio = float(np.mean(logo_cyan))
    strip_light_ratio = float(np.mean(strip_light))
    strip_gray_ratio = float(np.mean(strip_gray))
    metrics = {
        "ba_title_logo_cyan_ratio": round(logo_cyan_ratio, 5),
        "ba_title_strip_light_ratio": round(strip_light_ratio, 5),
        "ba_title_strip_gray_ratio": round(strip_gray_ratio, 5),
    }
    return (
        logo_cyan_ratio >= float(step.get("ba_title_logo_cyan_ratio", 0.40))
        and strip_light_ratio >= float(
            step.get("ba_title_strip_light_ratio", 0.55))
        and strip_gray_ratio <= float(
            step.get("ba_title_strip_gray_ratio", 0.25)),
        metrics,
    )


def _screen_looks_like_blue_archive_update_progress(
        data: bytes, step: dict[str, Any]) -> tuple[bool, dict[str, float]]:
    """Detect the thin full-width download progress bar at the bottom."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return False, {"ba_update_progress_dark_ratio": -1.0}
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        return False, {"ba_update_progress_dark_ratio": -1.0}
    height, width = image.shape[:2]
    bar = image[round(height * 0.905):round(height * 0.918),
                round(width * 0.02):round(width * 0.98)]
    if bar.size == 0:
        return False, {"ba_update_progress_dark_ratio": -1.0}
    hsv = cv2.cvtColor(bar, cv2.COLOR_BGR2HSV)
    dark_ratio = float(np.mean(hsv[:, :, 2] <= 150))
    full_hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    bright_ratio = float(np.mean(full_hsv[:, :, 2] >= 185))
    metrics = {
        "ba_update_progress_dark_ratio": round(dark_ratio, 5),
        "ba_update_progress_bright_ratio": round(bright_ratio, 5),
    }
    return (
        dark_ratio >= float(step.get("ba_update_progress_dark_ratio", 0.65))
        and bright_ratio >= float(
            step.get("ba_update_progress_bright_ratio", 0.35)),
        metrics,
    )


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
    gui_log_value = str(step.get("gui_log_path", "")).strip()
    gui_log_path = Path(expand(gui_log_value)) if gui_log_value else None
    marker = str(step.get("completion_marker", "AllTasksCompleted"))
    error_markers = [str(x) for x in step.get("error_markers", ["TaskChainError", "AllTasksError"])]
    update_markers = [str(x) for x in step.get("update_markers", [])]
    maintenance_markers = [str(x) for x in step.get("maintenance_markers", [])]
    asst_start_markers = [str(x) for x in step.get(
        "asst_start_markers", ['"taskchain":', "Start Task Chain"])]
    update_process_images = [str(x) for x in step.get(
        "update_process_images", ["MAA.Updater.exe"])]
    start_size = log_path.stat().st_size if log_path.exists() else 0
    gui_start_size = (gui_log_path.stat().st_size
                      if gui_log_path is not None and gui_log_path.exists() else 0)
    try:
        proc = subprocess.Popen([str(exe)], cwd=str(exe.parent),
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError as exc:
        return Result(False, f"无法启动 MAA：{exc}")
    timeout = int(step.get("timeout", 3600))
    started = time.monotonic()
    ctx.log(f"MAA GUI 已启动，等待完成标记：{marker}")
    position = start_size
    gui_position = gui_start_size
    tail = ""
    last_gui_event = None
    gpu_warning_seen = False
    exit_seen_at = None
    successor_seen = False
    restart_grace = float(step.get("restart_grace_seconds", 120))
    log_stall_seconds = max(0.0, float(step.get("log_stall_seconds", 0)))
    startup_timeout = max(1.0, float(step.get("startup_timeout", 300)))
    last_log_at = started
    log_activity_seen = False
    gui_start_click = bool(step.get("gui_start_click", False))
    start_button_names = [
        str(value) for value in step.get("start_button_names", ["开始任务", "开始"])
    ]
    start_click_count = 0
    max_start_clicks = max(1, int(step.get("max_start_clicks", 6)))
    start_click_interval = max(1.0, float(step.get("start_click_interval", 10)))
    next_start_click_at = started + max(
        0.0, float(step.get("initial_start_click_delay", 5)))
    run_started = False
    update_wait = max(0.0, float(step.pop("startup_update_wait", 0)))
    watchdog = EmulatorBlackScreenWatchdog(step, ctx, "明日方舟模拟器")
    try:
        if update_wait:
            ctx.log(f"检测到昨日更新标志，MAA 启动后先等待 {update_wait:g} 秒完成自动更新")
            waited = _wait_with_emulator_watchdog(update_wait, watchdog, ctx)
            if waited is not None:
                return waited
            step["_startup_update_wait_consumed"] = True
            started = time.monotonic()
            position = log_path.stat().st_size if log_path.exists() else 0
            gui_position = (gui_log_path.stat().st_size
                            if gui_log_path is not None and gui_log_path.exists() else 0)
            last_log_at = started
            log_activity_seen = False
            tail = ""
            next_start_click_at = started + max(
                0.0, float(step.get("initial_start_click_delay", 5)))
            ctx.log("MAA 更新等待结束，忽略等待期间日志并重新开始本轮启动检测")
        while time.monotonic() - started <= timeout:
            if ctx.stop_event.wait(1):
                return Result(False, "任务被用户停止")
            black_screen = watchdog.poll()
            if black_screen is not None:
                return black_screen
            update_process = next(
                (name for name in update_process_images
                 if _process_image_exists([name])), None)
            if update_process is not None:
                return _update_skip_result(
                    "MAA", step, f"检测到更新进程 {update_process}",
                    {"process_image": update_process,
                     "evidence": (str(watchdog.latest_path)
                                  if watchdog.latest_path.exists() else None)},
                    reason_code="script_update")
            if gui_log_path is not None and gui_log_path.exists():
                gui_size = gui_log_path.stat().st_size
                if gui_size < gui_position:
                    gui_position = 0
                if gui_size > gui_position:
                    with gui_log_path.open("rb") as handle:
                        handle.seek(gui_position)
                        gui_chunk = handle.read().decode("utf-8", errors="replace")
                    gui_position = gui_size
                    update_marker = _matching_marker(gui_chunk, update_markers)
                    if update_marker:
                        return _update_skip_result(
                            "MAA", step, update_marker,
                            {"log": str(gui_log_path)},
                            reason_code="script_update")
                    maintenance_marker = _matching_marker(
                        gui_chunk, maintenance_markers)
                    if maintenance_marker:
                        return _maintenance_skip_result(
                            "明日方舟", step, maintenance_marker,
                            {"log": str(gui_log_path)})
                    for line in gui_chunk.splitlines():
                        task_event = re.search(r"(开始任务|完成任务):\s*(.+?)\s*$", line)
                        if task_event:
                            event = (task_event.group(1), task_event.group(2))
                            if event != last_gui_event:
                                last_gui_event = event
                                ctx.log(f"MAA {event[0]}：{event[1]}")
                            if event[0] == "开始任务":
                                run_started = True
                        if (not gpu_warning_seen
                                and "推理加速 GPU" in line
                                and "兼容性问题" in line):
                            gpu_warning_seen = True
                            ctx.log("MAA 警告：当前推理加速 GPU 存在兼容性问题，"
                                    "建议关闭 GPU 加速以避免识别停滞")
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
                    if any(value in text_chunk for value in asst_start_markers):
                        run_started = True
                    tail = (tail + text_chunk)[-12000:]
                    update_marker = _matching_marker(text_chunk, update_markers)
                    if update_marker:
                        return _update_skip_result(
                            "MAA", step, update_marker,
                            {"log": str(log_path)},
                            reason_code="script_update")
                    maintenance_marker = _matching_marker(
                        text_chunk, maintenance_markers)
                    if maintenance_marker:
                        return _maintenance_skip_result(
                            "明日方舟", step, maintenance_marker,
                            {"log": str(log_path)})
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
            if not run_started and time.monotonic() - started >= startup_timeout:
                return Result(
                    False,
                    f"MAA 启动后 {startup_timeout:g} 秒仍无明确任务开始日志",
                    {"start_clicks": start_click_count,
                     "startup_timeout": startup_timeout})
            if (gui_start_click and not run_started
                    and time.monotonic() >= next_start_click_at):
                if start_click_count >= max_start_clicks:
                    return Result(
                        False,
                        f"MAA 已达到 {max_start_clicks} 次“开始任务”点击上限，"
                        "仍未产生本轮任务日志",
                        {"start_clicks": start_click_count})
                start_click_count += 1
                clicked, message = _click_named_gui_button(
                    proc.pid,
                    [str(value) for value in step.get("process_images", [exe.name])],
                    [str(value) for value in step.get("title_hints", ["MAA"])],
                    start_button_names,
                    float(step.get("gui_click_x_ratio", 0.84)),
                    float(step.get("gui_click_y_ratio", 0.93)))
                ctx.log(f"MAA 第 {start_click_count} 次点击“开始任务”：{message}")
                next_start_click_at = time.monotonic() + start_click_interval
                if not clicked:
                    ctx.log("MAA 窗口尚未就绪，将在启动间隔后重新置前并点击")
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
    update_markers = [str(x) for x in step.get("update_markers", [])]
    maintenance_markers = [str(x) for x in step.get("maintenance_markers", [])]
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
    update_wait = max(0.0, float(step.pop("startup_update_wait", 0)))
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
    last_queue_sample_at = None
    last_queue_probe_message = None
    next_queue_idle_click_at = started
    required_last_task = str(step.get("required_last_task", "")).strip()
    if require_empty_queue and step.get("queue_only_completion", False):
        # Queue-empty mode is the newer BAAS completion contract. A legacy
        # required_last_task value must not keep the workflow alive after
        # BAAS has already drained the whole queue.
        required_last_task = ""
    required_check_interval = float(step.get("required_task_check_interval", 600))
    completion_seen = False
    next_required_check_at = None
    last_run_task = None
    last_completed_task = None
    recoverable_events = 0
    reported_ignored_errors: set[str] = set()
    last_recoverable_at = None
    max_recoverable_events = max(1, int(step.get("max_recoverable_events", 3)))
    recoverable_event_debounce_seconds = max(
        0.0, float(step.get("recoverable_event_debounce_seconds", 5)))
    verify_after_recoverable_limit = bool(
        step.get("verify_rewards_after_recoverable_limit", False))
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
    soft_limit_markers = [str(value) for value in step.get(
        "soft_limit_markers", [])]
    soft_limit_threshold = max(1, int(step.get("soft_limit_threshold", 60)))
    soft_limit_counts: dict[str, int] = {}
    fallback_suppressed_while_running = False
    queue_state_fresh_seconds = max(
        queue_check_interval * 3,
        float(step.get("queue_state_fresh_seconds", 15)))
    update_screen_interval = max(
        0.05, float(step.get("update_screen_check_interval", 30)))
    update_screen_grace = max(
        0.0, float(step.get("update_screen_grace_seconds", 60)))
    update_screen_confirm_count = max(
        1, int(step.get("update_screen_confirm_count", 3)))
    next_update_screen_check_at = started + update_screen_grace
    update_screen_matches = 0
    max_update_confirm_clicks = max(
        1, int(step.get("max_update_confirm_clicks", 3)))
    update_confirm_clicks = 0
    game_update_started_at = None
    game_update_timeout = max(
        60.0, float(step.get("game_update_timeout", 1800)))
    update_progress_reported = False
    title_screen_click_interval = max(
        5.0, float(step.get("title_screen_click_interval", 30)))
    max_title_screen_clicks = max(
        1, int(step.get("max_title_screen_clicks", 4)))
    title_screen_clicks = 0
    next_title_screen_click_at = started + update_screen_grace
    start_click_protection_seconds = max(
        0.0, float(step.get("start_click_protection_seconds", 60)))
    start_click_protection_until = 0.0

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

    def inspect_game_startup_screen() -> dict[str, Any] | None:
        if not step.get("update_screen_detection", False):
            return None
        watchdog.poll(force=True)
        if not watchdog.latest_path.exists():
            return None
        try:
            data = watchdog.latest_path.read_bytes()
        except OSError:
            return None
        update_prompt, update_metrics = _screen_looks_like_blue_archive_update(
            data, step)
        title_screen, title_metrics = _screen_looks_like_blue_archive_title(
            data, step)
        update_progress, progress_metrics = (
            _screen_looks_like_blue_archive_update_progress(data, step))
        return {
            "evidence": str(watchdog.latest_path),
            "update_prompt": update_prompt,
            "title_screen": title_screen,
            "update_progress": update_progress,
            **update_metrics,
            **title_metrics,
            **progress_metrics,
        }

    def tap_game_ratio(x_ratio: float, y_ratio: float) -> tuple[bool, str]:
        try:
            x = round(1280 * min(max(x_ratio, 0.0), 1.0))
            y = round(720 * min(max(y_ratio, 0.0), 1.0))
            tapped = subprocess.run(
                [ctx.tool("adb"), "-s", emulator_device, "shell", "input",
                 "tap", str(x), str(y)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                creationflags=(subprocess.CREATE_NO_WINDOW
                               if os.name == "nt" else 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        if tapped.returncode != 0:
            return False, tapped.stderr.decode(
                "utf-8", errors="replace").strip() or f"ADB 退出码 {tapped.returncode}"
        return True, f"({x}, {y})"

    if require_empty_queue:
        ctx.log("BAAS 已启动，将以“本轮已开始、baas1 队列为 0 且状态为闲置中”"
                "稳定保持作为结束标准")
        if not required_last_task:
            ctx.log("BAAS 已显式启用纯队列完成模式，本轮不再校验最后任务名称")
    else:
        ctx.log(f"BAAS 已启动，等待 baas1 完成标记：{marker}")
    try:
        if update_wait:
            ctx.log(f"检测到昨日更新标志，已启动 BAAS，先等待 {update_wait:g} 秒让游戏自动更新/初始化")
            waited = _wait_with_emulator_watchdog(update_wait, watchdog, ctx)
            if waited is not None:
                return waited
            step["_startup_update_wait_consumed"] = True
            existing = {
                name: Path(name).stat().st_size
                for name in glob.glob(log_glob)
                if Path(name).exists()
            }
            tails.clear()
            started = time.monotonic()
            startup_reference = started
            next_fallback_at = started + float(step.get("gui_fallback_after", 45))
            next_queue_check_at = started
            next_queue_idle_click_at = started
            next_update_screen_check_at = started + update_screen_grace
            update_screen_matches = 0
            next_title_screen_click_at = started + update_screen_grace
            ctx.log("碧蓝档案更新等待结束，重新开始启动检测")
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
                    update_marker = _matching_marker(content, update_markers)
                    if update_marker:
                        return _update_skip_result(
                            "碧蓝档案", step, update_marker,
                            {"log": name, "evidence_type": "log"},
                            reason_code="game_or_script_update")
                    maintenance_marker = _matching_marker(content, maintenance_markers)
                    if maintenance_marker:
                        return _maintenance_skip_result(
                            "碧蓝档案", step, maintenance_marker,
                            {"log": name, "evidence_type": "log"})
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
                        game_update_started_at = None
                        update_progress_reported = False
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
                        for soft_marker in soft_limit_markers:
                            if soft_marker not in line:
                                continue
                            soft_limit_counts[soft_marker] = (
                                soft_limit_counts.get(soft_marker, 0) + 1)
                            count = soft_limit_counts[soft_marker]
                            if count in (1, soft_limit_threshold):
                                ctx.log(
                                    f"BAAS 检测到可跳过型重复提示“{soft_marker}”"
                                    f"（{count}/{soft_limit_threshold}）")
                            if count >= soft_limit_threshold:
                                if verify_after_recoverable_limit:
                                    return Result(
                                        True,
                                        "BAAS 子任务连续提示条件不足，已转入每日奖励核验",
                                        {"log": name, "marker": soft_marker,
                                         "soft_limit_count": count,
                                         "completion_mode": "verify_after_soft_limit",
                                         "last_run_task": last_run_task,
                                         "last_completed_task": last_completed_task})
                                return Result(
                                    False,
                                    f"BAAS 子任务连续提示条件不足：{soft_marker}",
                                    {"log": name, "marker": soft_marker,
                                     "soft_limit_count": count,
                                     "last_run_task": last_run_task,
                                     "last_completed_task": last_completed_task})
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
                        if (last_recoverable_at is None
                                or now - last_recoverable_at >= recoverable_event_debounce_seconds):
                            recoverable_events += 1
                            last_recoverable_at = now
                        ctx.log(f"BAAS 检测到可恢复异常（第 {recoverable_events} 次）：{recoverable}；"
                                "保留模拟器并等待 BAAS 自恢复")
                        if recoverable_events > max_recoverable_events:
                            screen_state = inspect_game_startup_screen()
                            if (screen_state is not None and any(
                                    screen_state.get(key) for key in (
                                        "update_prompt", "update_progress",
                                        "title_screen"))):
                                recoverable_events = 0
                                next_update_screen_check_at = 0
                                ctx.log(
                                    "BAAS 自恢复达到上限，但游戏仍处于标题、"
                                    "更新确认或下载画面；改由游戏画面恢复逻辑继续处理")
                                continue
                            if verify_after_recoverable_limit:
                                ctx.log(
                                    "BAAS 连续自恢复已到上限；先结束 BAAS，"
                                    "转入游戏内每日奖励页核验，避免把可领取状态误判为脚本失败")
                                return Result(
                                    True,
                                    "BAAS 自恢复到达上限，已转入每日奖励核验",
                                    {"log": name, "marker": recoverable,
                                     "recoverable_events": recoverable_events,
                                     "completion_mode": "verify_after_recoverable_limit",
                                     "last_run_task": last_run_task,
                                     "last_completed_task": last_completed_task})
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
                        last_queue_sample_at = None
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
                        last_queue_sample_at = None
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
            if (step.get("update_screen_detection", False)
                    and now >= next_update_screen_check_at
                    and (not run_started or recoverable_events > 0
                         or game_update_started_at is not None)):
                next_update_screen_check_at = now + update_screen_interval
                screen_state = inspect_game_startup_screen()
                if screen_state is None:
                    update_screen_matches = 0
                elif screen_state.get("update_prompt"):
                    update_progress_reported = False
                    update_screen_matches += 1
                    ctx.log(
                        "碧蓝档案检测到真正的更新下载确认框"
                        f"（{update_screen_matches}/{update_screen_confirm_count}）")
                    if update_screen_matches >= update_screen_confirm_count:
                        if update_confirm_clicks >= max_update_confirm_clicks:
                            return _update_skip_result(
                                "碧蓝档案", step, "更新确认框重复出现",
                                {"update_confirm_clicks": update_confirm_clicks,
                                 **screen_state},
                                reason_code="game_update_confirmation_stuck")
                        clicked, click_message = tap_game_ratio(
                            float(step.get("update_confirm_x_ratio", 766 / 1280)),
                            float(step.get("update_confirm_y_ratio", 505 / 720)))
                        if not clicked:
                            return Result(
                                False,
                                f"点击碧蓝档案更新“确认”失败：{click_message}",
                                {"retry_step": True, **screen_state})
                        update_confirm_clicks += 1
                        update_screen_matches = 0
                        game_update_started_at = now
                        startup_reference = now
                        log_stall_armed = False
                        last_log_at = None
                        ctx.log(
                            f"已第 {update_confirm_clicks} 次点击碧蓝档案"
                            f"更新“确认”：{click_message}；最多等待 "
                            f"{game_update_timeout:g} 秒完成下载")
                elif screen_state.get("update_progress"):
                    update_screen_matches = 0
                    if game_update_started_at is None:
                        game_update_started_at = now
                        startup_reference = now
                    if not update_progress_reported:
                        update_progress_reported = True
                        ctx.log(
                            f"碧蓝档案正在下载游戏更新，最多等待 "
                            f"{game_update_timeout:g} 秒，不执行启动按钮误点")
                elif (screen_state.get("title_screen")
                      and now >= next_title_screen_click_at):
                    update_screen_matches = 0
                    update_progress_reported = False
                    if title_screen_clicks >= max_title_screen_clicks:
                        return Result(
                            False,
                            f"碧蓝档案标题页已点击 {title_screen_clicks} 次，"
                            "仍未进入游戏",
                            {"title_screen_clicks": title_screen_clicks,
                             "retry_step": True, **screen_state})
                    clicked, click_message = tap_game_ratio(
                        float(step.get("title_screen_x_ratio", 0.5)),
                        float(step.get("title_screen_y_ratio", 0.875)))
                    if not clicked:
                        return Result(
                            False,
                            f"点击碧蓝档案标题页失败：{click_message}",
                            {"retry_step": True, **screen_state})
                    title_screen_clicks += 1
                    next_title_screen_click_at = now + title_screen_click_interval
                    startup_reference = now
                    game_update_started_at = None
                    recoverable_events = 0
                    ctx.log(
                        f"检测到“点击任意区域进入游戏”标题页，"
                        f"已第 {title_screen_clicks} 次点击：{click_message}")
                else:
                    update_screen_matches = 0
            if (game_update_started_at is not None
                    and now - game_update_started_at >= game_update_timeout):
                screen_state = inspect_game_startup_screen() or {}
                return _update_skip_result(
                    "碧蓝档案", step, "游戏更新下载超时",
                    {"game_update_seconds": now - game_update_started_at,
                     "update_confirm_clicks": update_confirm_clicks,
                     **screen_state},
                    reason_code="game_update_timeout")
            if (require_empty_queue and now >= next_queue_check_at):
                queue_count, queue_idle, probe_message = _read_baas_queue_count(proc.pid)
                next_queue_check_at = now + queue_check_interval
                if queue_count is None:
                    queue_empty_at = None
                    if probe_message != last_queue_probe_message:
                        ctx.log(f"BAAS 队列检测：{probe_message}")
                        last_queue_probe_message = probe_message
                else:
                    last_queue_sample_at = now
                    last_queue_probe_message = None
                    if (queue_idle and now < start_click_protection_until
                            and not completion_seen):
                        queue_idle = False
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
                                float(step.get("gui_profile_y_ratio", 0.15)),
                                str(ctx.root / "logs" / "baas_gui_latest.png"),
                                allow_coordinate_fallback=True)
                            ctx.log(f"BAAS 上一批已结束但队列仍有 {queue_count} 项；"
                                    f"第 {click_count} 次继续启动：{click_message}")
                            next_queue_idle_click_at = now + float(
                                step.get("gui_fallback_retry_seconds", 30))
                            completion_seen = False
                            completion_log = None
                            run_started = False
                            # A successful click is the earliest reliable sign
                            # that BAAS has been asked to work. If the game
                            # hangs on its loading screen, "开始执行【" never
                            # appears, so arm the stall watchdog here.
                            log_stall_armed = clicked
                            last_log_at = now if clicked else None
                            last_log_name = None
                            if clicked:
                                last_queue_idle = False
                                last_queue_sample_at = now
                                start_click_protection_until = (
                                    now + start_click_protection_seconds)
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
            work_evidence = bool(
                completion_seen or queue_seen_nonempty or last_completed_task)
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
                    and required_task_matches and work_evidence):
                return Result(
                    True,
                    (f"BAAS baas1 队列已清空，且最后完成任务已确认为“{required_last_task}”"
                     if required_last_task else
                     "BAAS baas1 队列已清空且脚本已闲置，本轮日常任务完成"),
                    {"log": completion_log, "marker": marker if completion_seen else None,
                     "queue_count": 0, "queue_seen_nonempty": queue_seen_nonempty,
                     "work_evidence": work_evidence,
                     "queue_empty_confirm_seconds": queue_empty_confirm_seconds,
                     "last_run_task": last_run_task,
                     "last_completed_task": last_completed_task,
                     "recoverable_events": recoverable_events,
                     "emulator_restarts": emulator_restarts,
                     "start_clicks": click_count})
            if (require_empty_queue and completion_seen and queue_seen_nonempty
                    and last_queue_count == 0 and queue_zero_at is not None
                    and now - queue_zero_at >= queue_empty_confirm_seconds
                    and required_task_matches):
                return Result(
                    True,
                    "BAAS 成功日志已出现，且队列已从非空降至 0 并稳定保持；"
                    "无需继续等待暂时不可读的 GUI 闲置状态",
                    {"log": completion_log, "marker": marker,
                     "queue_count": 0, "queue_seen_nonempty": True,
                     "queue_idle": last_queue_idle,
                     "queue_empty_confirm_seconds": queue_empty_confirm_seconds,
                     "last_run_task": last_run_task,
                     "last_completed_task": last_completed_task,
                     "recoverable_events": recoverable_events,
                     "emulator_restarts": emulator_restarts,
                     "start_clicks": click_count,
                     "completion_mode": "queue_zero_success_marker"})
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
                    and not completion_seen and game_update_started_at is None
                    and now - last_log_at >= log_stall_seconds):
                stalled_for = now - last_log_at
                return Result(
                    False,
                    f"BAAS 本轮启动后日志已 {stalled_for:g} 秒无更新，判定脚本停滞",
                    {"log": last_log_name, "log_stalled": True,
                     "retry_step": True, "log_stall_seconds": stalled_for,
                     "last_run_task": last_run_task,
                     "last_completed_task": last_completed_task})
            if (not run_started and game_update_started_at is None
                    and time.monotonic() - startup_reference >= startup_timeout):
                return Result(False, f"BAAS 打开后 {startup_timeout:g} 秒仍无任务启动日志",
                              {"start_clicks": click_count})
            if (step.get("gui_click_fallback", True) and not run_started
                    and time.monotonic() >= next_fallback_at):
                retry_seconds = float(step.get("gui_fallback_retry_seconds", 30))
                queue_state_is_fresh = (
                    last_queue_sample_at is not None
                    and time.monotonic() - last_queue_sample_at
                    <= queue_state_fresh_seconds)
                if require_empty_queue and (
                        time.monotonic() < start_click_protection_until
                        or last_queue_idle is False or not queue_state_is_fresh):
                    next_fallback_at = time.monotonic() + retry_seconds
                    if not fallback_suppressed_while_running:
                        reason = ("队列仍在执行中" if last_queue_idle is False
                                  else ("启动点击仍在保护期"
                                        if time.monotonic()
                                        < start_click_protection_until
                                        else "没有最近的明确闲置状态"))
                        ctx.log(f"BAAS {reason}，暂停坐标备用启动点击，避免误点停止")
                        fallback_suppressed_while_running = True
                    continue
                fallback_suppressed_while_running = False
                if click_count >= max_start_clicks:
                    return Result(False, f"BAAS 已循环点击启动 {click_count} 次，仍无任务启动日志",
                                  {"start_clicks": click_count})
                click_count += 1
                clicked, click_message = _click_baas_start_button(
                    proc.pid, float(step.get("gui_click_x_ratio", 0.374)),
                    float(step.get("gui_click_y_ratio", 0.108)),
                    float(step.get("gui_profile_x_ratio", 0.04)),
                    float(step.get("gui_profile_y_ratio", 0.15)),
                    str(ctx.root / "logs" / "baas_gui_latest.png"),
                    allow_coordinate_fallback=(
                        not require_empty_queue
                        or (queue_state_is_fresh and last_queue_idle is True)))
                ctx.log(f"BAAS 第 {click_count} 次启动点击：{click_message}")
                next_fallback_at = time.monotonic() + retry_seconds
                if clicked and require_empty_queue:
                    last_queue_idle = False
                    last_queue_sample_at = time.monotonic()
                    start_click_protection_until = (
                        time.monotonic() + start_click_protection_seconds)
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
                             profile_y_ratio: float = 0.15,
                             debug_path: str = "",
                             allow_coordinate_fallback: bool = True) -> tuple[bool, str]:
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
    topmost_fallback = False
    if not focused:
        # Electron/WebView windows occasionally reject SetForegroundWindow
        # with ERROR_ACCESS_DENIED even when GameFlow and BAAS are both
        # elevated. Keep the real BAAS window visibly on top, then let the
        # first physical click activate it and the second click start work.
        try:
            flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
            topmost_fallback = True
            time.sleep(0.2)
        except Exception:
            return False, focus_message

    def release_topmost() -> None:
        if not topmost_fallback:
            return
        try:
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE |
                win32con.SWP_SHOWWINDOW)
        except Exception:
            pass

    rect = win32gui.GetWindowRect(hwnd)
    if debug_path:
        try:
            from PIL import ImageGrab
            output = Path(debug_path)
            output.parent.mkdir(parents=True, exist_ok=True)
            ImageGrab.grab(bbox=rect, all_screens=True).save(output)
        except Exception:
            pass
    try:
        from pywinauto import Application
        app = Application(backend="uia").connect(handle=hwnd, timeout=3)
        window = app.window(handle=hwnd)
        controls = list(window.descendants(control_type="Button"))
        names = [control.window_text().strip() for control in controls]
        running_names = [
            name for name in names
            if any(word in name for word in ("停止", "终止", "暂停", "执行中"))
        ]
        if running_names:
            release_topmost()
            return False, (
                f"检测到 BAAS 正在运行控件“{running_names[0]}”，"
                "拒绝执行坐标备用点击")
        for control, name in zip(controls, names):
            if name in ("启动", "开始", "运行") or "启动" in name:
                control.click_input()
                release_topmost()
                return True, f"已通过 GUI 控件点击 BAAS“{name}”按钮"
    except Exception:
        pass
    if not allow_coordinate_fallback:
        release_topmost()
        return False, "未确认 BAAS 最近处于闲置状态，仅尝试控件点击并拒绝盲坐标点击"
    left, top, right, bottom = rect
    profile_x = left + round((right - left) * min(max(profile_x_ratio, 0.0), 1.0))
    profile_y = top + round((bottom - top) * min(max(profile_y_ratio, 0.0), 1.0))
    x = left + round((right - left) * min(max(x_ratio, 0.0), 1.0))
    y = top + round((bottom - top) * min(max(y_ratio, 0.0), 1.0))
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        cursor = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(cursor))

        def native_click(click_x: int, click_y: int) -> None:
            user32.SetCursorPos(click_x, click_y)
            user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
            user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP

        native_click(profile_x, profile_y)
        time.sleep(0.5)
        native_click(x, y)
        user32.SetCursorPos(cursor.x, cursor.y)
        release_topmost()
        focus_note = ("（Windows 拒绝焦点，已临时置顶后点击）"
                      if topmost_fallback else "")
        return True, (f"已点击 BAAS 的 baas1（{profile_x}, {profile_y}）和启动按钮"
                      f"（{x}, {y}）{focus_note}")
    except Exception as exc:
        try:
            user32.SetCursorPos(cursor.x, cursor.y)
        except Exception:
            pass
        release_topmost()
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
    if step.get("return_home_before_verify", True):
        home_x_ratio = float(step.get("home_x_ratio", 1233 / 1280))
        home_y_ratio = float(step.get("home_y_ratio", 11 / 720))
        attempts = max(0, int(step.get("return_home_attempts", 3)))
        wait_seconds = max(0.0, float(step.get("return_home_wait", 1.2)))
        for attempt in range(1, attempts + 1):
            home_point = (
                round(home.shape[1] * home_x_ratio),
                round(home.shape[0] * home_y_ratio),
            )
            ctx.log(f"奖励核验前尝试返回碧蓝档案主页（{attempt}/{attempts}）：{home_point}")
            click_home = subprocess.run(
                [adb, "-s", device, "shell", "input", "tap", str(home_point[0]), str(home_point[1])],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if click_home.returncode != 0:
                return Result(False, "点击主页按钮失败")
            time.sleep(wait_seconds)
            refreshed_result, refreshed_home = capture()
            if refreshed_result.success and refreshed_home is not None:
                home = refreshed_home
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
        if step.get("allow_static_reward_page", True):
            ctx.log(
                f"点击后画面变化较小（{change:.2f}），继续用每日进度条与奖励按钮状态核验，"
                "避免已在目标页时误判")
        else:
            return Result(False, "点击后未能确认进入每日任务页",
                          {"evidence": str(evidence), "page_change": change})
    def roi_bounds(image, roi):
        return (
            round(image.shape[1] * float(roi[0])),
            round(image.shape[0] * float(roi[1])),
            round(image.shape[1] * float(roi[2])),
            round(image.shape[0] * float(roi[3])),
        )

    def gray_stats(image, roi):
        x1, y1, x2, y2 = roi_bounds(image, roi)
        area = image[y1:y2, x1:x2]
        if area.size == 0:
            return {"sat_mean": 255.0, "high_sat_fraction": 1.0, "roi": [x1, y1, x2, y2]}
        hsv = cv2.cvtColor(area, cv2.COLOR_BGR2HSV)
        saturation = hsv[:, :, 1]
        return {"sat_mean": float(np.mean(saturation)),
                "high_sat_fraction": float(np.mean(saturation > 60)),
                "roi": [x1, y1, x2, y2]}

    def progress_stats(image):
        roi = step.get("daily_progress_roi", [0.397, 0.922, 0.671, 0.979])
        x1, y1, x2, y2 = roi_bounds(image, roi)
        area = image[y1:y2, x1:x2]
        if area.size == 0:
            return {"fill_ratio": 0.0, "full": False, "roi": [x1, y1, x2, y2]}
        hsv = cv2.cvtColor(area, cv2.COLOR_BGR2HSV)
        cyan = ((hsv[:, :, 0] >= int(step.get("progress_hue_min", 80))) &
                (hsv[:, :, 0] <= int(step.get("progress_hue_max", 110))) &
                (hsv[:, :, 1] >= int(step.get("progress_saturation_min", 90))) &
                (hsv[:, :, 2] >= int(step.get("progress_value_min", 100))))
        column_fraction = np.mean(cyan, axis=0)
        active_columns = np.flatnonzero(
            column_fraction >= float(step.get("progress_column_fraction_min", 0.20)))
        fill_ratio = (
            float(active_columns[-1] - active_columns[0] + 1) / float(cyan.shape[1])
            if active_columns.size else 0.0
        )
        minimum = float(step.get("daily_progress_full_min", 0.94))
        return {"fill_ratio": fill_ratio, "full": fill_ratio >= minimum,
                "minimum": minimum, "roi": [x1, y1, x2, y2]}

    def tap_ratio(x_ratio: float, y_ratio: float) -> Result:
        x = round(task_page.shape[1] * x_ratio)
        y = round(task_page.shape[0] * y_ratio)
        done = subprocess.run(
            [adb, "-s", device, "shell", "input", "tap", str(x), str(y)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if done.returncode != 0:
            return Result(False, f"奖励页点击失败：({x}, {y})")
        return Result(True, f"已点击奖励页：({x}, {y})")

    complete_roi = step.get("complete_button_roi", [0.722, 0.894, 0.805, 0.972])
    collect_roi = step.get("collect_button_roi", [0.817, 0.889, 0.983, 0.976])

    def evaluate(image):
        complete = gray_stats(image, complete_roi)
        collect = gray_stats(image, collect_roi)
        max_mean = float(step.get("gray_max_saturation_mean", 35))
        max_fraction = float(step.get("gray_max_high_saturation_fraction", 0.15))
        return (
            complete,
            collect,
            complete["sat_mean"] <= max_mean
            and complete["high_sat_fraction"] <= max_fraction,
            collect["sat_mean"] <= max_mean
            and collect["high_sat_fraction"] <= max_fraction,
        )

    progress = progress_stats(task_page)
    complete_stats, collect_stats, complete_gray, collect_gray = evaluate(task_page)
    max_mean = float(step.get("gray_max_saturation_mean", 35))
    max_fraction = float(step.get("gray_max_high_saturation_fraction", 0.15))
    details = {"evidence": str(evidence), "progress": progress,
               "complete_gray": complete_gray,
               "collect_gray": collect_gray, "complete_stats": complete_stats,
               "collect_stats": collect_stats, "page_change": change}
    if not progress["full"]:
        return Result(
            False,
            f"每日任务进度未满（进度条 {progress['fill_ratio']:.1%}，"
            f"要求至少 {progress['minimum']:.0%}），不能把灰色按钮视为已领取",
            details)

    if not complete_gray or not collect_gray:
        click_wait = max(0.0, float(step.get("reward_click_wait", 1.2)))
        dismiss_wait = max(0.0, float(step.get("reward_dismiss_wait", 0.8)))
        dismiss_x = float(step.get("reward_dismiss_x_ratio", 0.5))
        dismiss_y = float(step.get("reward_dismiss_y_ratio", 0.5))
        if not complete_gray:
            result = tap_ratio(
                (float(complete_roi[0]) + float(complete_roi[2])) / 2,
                (float(complete_roi[1]) + float(complete_roi[3])) / 2)
            if not result.success:
                return result
            time.sleep(click_wait)
            tap_ratio(dismiss_x, dismiss_y)
            time.sleep(dismiss_wait)
        if not collect_gray:
            result = tap_ratio(
                (float(collect_roi[0]) + float(collect_roi[2])) / 2,
                (float(collect_roi[1]) + float(collect_roi[3])) / 2)
            if not result.success:
                return result
            time.sleep(click_wait)
            tap_ratio(dismiss_x, dismiss_y)
            time.sleep(dismiss_wait)
        final_result, final_page = capture()
        if not final_result.success:
            return final_result
        task_page = final_page
        cv2.imwrite(str(evidence), task_page)
        progress = progress_stats(task_page)
        complete_stats, collect_stats, complete_gray, collect_gray = evaluate(task_page)
        details.update({
            "progress": progress,
            "complete_gray": complete_gray,
            "collect_gray": collect_gray,
            "complete_stats": complete_stats,
            "collect_stats": collect_stats,
            "fallback_collection_attempted": True,
        })
    if not complete_gray and not collect_gray:
        return Result(False, "“完成”和“一键领取”按钮均未变灰，今日任务尚未完成", details)
    if not complete_gray:
        return Result(False, "完成每日任务里程碑的“领取”按钮尚未变灰", details)
    if not collect_gray:
        return Result(False, "“一键领取”按钮尚未变灰，仍有奖励未领取", details)
    return Result(
        True,
        "已确认每日任务进度满格，且“领取”和“一键领取”按钮均为灰色",
        details)


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
    update_markers = [str(x) for x in step.get("update_markers", [])]
    maintenance_markers = [str(x) for x in step.get("maintenance_markers", [])]
    restart_markers = [str(x) for x in step.get(
        "restart_emulator_markers", ["No emulator with serial", "无法连接至ADB服务",
                                     "Request human takeover"])]
    # LOGIN_CHECK can break after a game UI redesign.  It is recoverable and is
    # not, by itself, evidence of maintenance.
    login_recovery_markers = [str(x) for x in step.get(
        "login_recovery_markers",
        step.get("service_unavailable_markers", ["GameTooManyClickError"]))]
    started_at = time.monotonic()
    run_started = False
    completion_at = None
    completion_log = None
    log_retry_interval = max(0.05, float(step.get("log_retry_interval", 30)))
    next_log_retry_at = started_at + log_retry_interval
    start_click_count = 0
    max_start_clicks = max(1, int(step.get("max_start_clicks", 5)))
    startup_timeout = max(log_retry_interval, float(step.get("startup_timeout", 300)))
    post_start_log_stall_seconds = max(
        0.0, float(step.get("post_start_log_stall_seconds", 600)))
    tails = {}
    last_log = None
    last_log_activity_at = started_at
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
            waited = _wait_with_emulator_watchdog(update_wait, watchdog, ctx)
            if waited is not None:
                return waited
            step["_startup_update_wait_consumed"] = True
            existing = {
                name: Path(name).stat().st_size
                for name in glob.glob(log_glob)
                if Path(name).exists()
            }
            tails.clear()
            # Update time must not consume the ordinary startup timeout.
            started_at = time.monotonic()
            last_log_activity_at = started_at
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
                last_log_activity_at = time.monotonic()
                next_log_retry_at = time.monotonic() + log_retry_interval
                update_marker = _matching_marker(content, update_markers)
                if update_marker:
                    return _update_skip_result(
                        "碧蓝航线", step, update_marker,
                        {"log": name,
                         "evidence": (str(watchdog.latest_path)
                                      if watchdog.latest_path.exists() else None)},
                        reason_code="game_update")
                maintenance_marker = _matching_marker(content, maintenance_markers)
                if maintenance_marker:
                    return _maintenance_skip_result(
                        "碧蓝航线", step, maintenance_marker,
                        {"log": name,
                         "evidence": (str(watchdog.latest_path)
                                      if watchdog.latest_path.exists() else None)})
                started_in_content = any(marker in content for marker in start_markers)
                if completion_at is not None and started_in_content:
                    ctx.log("ALAS 在空闲标记后又启动了新任务，继续等待调度器稳定")
                    completion_at = None
                    completion_log = None
                if started_in_content:
                    run_started = True
                folded_content = content.casefold()
                service_error = next((marker for marker in login_recovery_markers
                                      if marker.casefold() in folded_content), None)
                if service_error:
                    service_error_count = int(step.get("_service_error_count", 0)) + 1
                    step["_service_error_count"] = service_error_count
                    recovered, recovery_message = recover_updated_login_page()
                    delay = float(step.get(
                        "service_recovery_retry_delay", 60) if recovered else
                        step.get("service_unavailable_retry_delay", 600))
                    return Result(
                        False,
                        (f"AzurLaneAutoScript 登录页控件失配（{service_error}）；"
                         f"{recovery_message}；将保留模拟器并在 {delay:g} 秒后重试脚本"),
                        {"log": name, "marker": service_error,
                         "login_ui_mismatch": True, "retry_step": True,
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
            if (run_started and completion_at is None
                    and post_start_log_stall_seconds > 0
                    and time.monotonic() - last_log_activity_at
                    >= post_start_log_stall_seconds):
                stalled_for = time.monotonic() - last_log_activity_at
                return Result(
                    False,
                    f"ALAS 进入任务后日志已 {stalled_for:g} 秒无更新，判定脚本停滞",
                    {"log": last_log, "log_stalled": True,
                     "log_stall_seconds": stalled_for,
                     "retry_step": True,
                     "evidence": (str(watchdog.latest_path)
                                  if watchdog.latest_path.exists() else None)})
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
    light_mask = (((hsv[:, :, 1] < 35) & (hsv[:, :, 2] > 190))
                  .astype(np.uint8) * 255)
    popup_white_bottom_fraction = 0.0
    # Use broad white rows instead of a connected component.  On Naruto's
    # title screen the white report card can touch the logo through antialiased
    # pixels, making a component incorrectly extend to the top/bottom HUD.
    central_light_rows = np.mean(
        light_mask[:, round(width * 0.05):round(width * 0.95)] > 0, axis=1)
    broad_white_rows = np.flatnonzero(central_light_rows >= 0.65)
    if broad_white_rows.size:
        popup_white_bottom_fraction = float(broad_white_rows[-1] + 1) / height

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

    # Shadow Clone's licence screen is not exposed to UIAutomator.  It has a
    # stable orange title bar, a broad light body, and split Exit/Continue
    # controls along the bottom.  Recognise that layout so the right-hand
    # Continue action can be clicked without treating it as an update notice.
    auth_header = area(0.03, 0.13, 0.97, 0.28)
    auth_body = area(0.03, 0.28, 0.97, 0.87)
    auth_header_orange_fraction = float(np.mean(
        (auth_header[:, :, 0] >= 5) & (auth_header[:, :, 0] <= 30) &
        (auth_header[:, :, 1] > 100) & (auth_header[:, :, 2] > 100)))
    auth_body_light_fraction = float(np.mean(
        (auth_body[:, :, 1] < 45) & (auth_body[:, :, 2] > 175)))
    shadow_authorization_continue = (
        landscape and auth_header_orange_fraction >= 0.45
        and auth_body_light_fraction >= 0.58)

    def outlined_button_metrics(x1, y1, x2, y2):
        crop = area(x1, y1, x2, y2)
        gray = cv2.cvtColor(crop, cv2.COLOR_HSV2BGR)
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 60, 140)
        return (float(np.mean(edges > 0)),
                float(np.mean((gray > 140) & (gray < 230))))

    if landscape:
        update_left_button = outlined_button_metrics(0.32, 0.60, 0.49, 0.79)
        update_right_button = outlined_button_metrics(0.51, 0.60, 0.68, 0.79)
        update_choice_buttons = (
            min(update_left_button[0], update_right_button[0]) >= 0.030
            and min(update_left_button[1], update_right_button[1]) >= 0.050)
    else:
        update_left_button = outlined_button_metrics(0.20, 0.60, 0.46, 0.70)
        update_right_button = outlined_button_metrics(0.54, 0.60, 0.80, 0.70)
        update_choice_buttons = (
            min(update_left_button[0], update_right_button[0]) >= 0.035
            and min(update_left_button[1], update_right_button[1]) >= 0.050)
    # Portrait releases use a darker translucent page behind the white card,
    # so the card fraction is lower (about 0.57 in v6.2.3).  Keep the stricter
    # landscape threshold while accepting the known portrait range.
    update_card_threshold = 0.50 if not landscape else 0.60
    shadow_update_choice_popup = (
        popup_white_fraction >= update_card_threshold
        and consent_orange_fraction < 0.25 and update_choice_buttons)

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
        # Shadow Clone's mandatory manual-update announcement uses a visibly
        # taller white card than its ordinary completion/error report.  Its
        # text is drawn as an overlay and is absent from UIAutomator, so the
        # card geometry is the authoritative, language-independent signal.
        "shadow_mandatory_update_popup": (
            shadow_update_choice_popup and not shadow_authorization_continue),
        "shadow_update_choice_popup": shadow_update_choice_popup,
        "update_left_button_metrics": update_left_button,
        "update_right_button_metrics": update_right_button,
        "shadow_authorization_continue": shadow_authorization_continue,
        "auth_header_orange_fraction": auth_header_orange_fraction,
        "auth_body_light_fraction": auth_body_light_fraction,
        "popup_white_bottom_fraction": popup_white_bottom_fraction,
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


def _naruto_role_name_metrics(image, reference_paths, *,
                              threshold: float = 0.62) -> dict[str, Any]:
    """Verify the fixed top-left account name on the Naruto lobby.

    The sky/HUD changes during the day and is intentionally not used here.
    ``naruto_activity_check.png`` is a known-good lobby capture with the
    requested account name (南部清和).  Matching only the small name strip
    avoids depending on the character, currency, or animated background.
    """
    import cv2
    import numpy as np

    height, width = image.shape[:2]
    if width <= height:
        return {"role_name_match": False, "role_name_score": 0.0,
                "role_name_reference": None}

    # Relative crop around the six-character account name, excluding the
    # level and combat-power numbers immediately to its left/right.
    region = (0.145, 0.000, 0.235, 0.065)

    def crop(source):
        sh, sw = source.shape[:2]
        x1, y1, x2, y2 = region
        value = source[round(sh * y1):round(sh * y2),
                       round(sw * x1):round(sw * x2)]
        if value.size == 0:
            return None
        gray = cv2.cvtColor(value, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        return cv2.Canny(gray, 40, 140)

    current = crop(image)
    best_score = 0.0
    best_reference = None
    if current is not None:
        for value in reference_paths or []:
            reference = cv2.imread(expand(str(value)))
            if reference is None:
                continue
            template = crop(reference)
            if template is None:
                continue
            current_scaled = cv2.resize(current,
                                        (template.shape[1], template.shape[0]))
            if (float(np.mean(template > 0)) < 0.01 or
                    float(np.mean(current_scaled > 0)) < 0.01):
                continue
            score = float(cv2.matchTemplate(
                template, current_scaled, cv2.TM_CCOEFF_NORMED)[0, 0])
            if np.isfinite(score) and score > best_score:
                best_score = score
                best_reference = str(value)
    return {
        "role_name_match": best_score >= threshold,
        "role_name_score": round(best_score, 4),
        "role_name_reference": best_reference,
    }


def run_naruto_shadow(step: dict[str, Any], ctx: RunContext) -> Result:
    """Keep starting Shadow Clone until it actually hands control to Naruto."""
    adb = ctx.tool("adb")
    device = str(step.get("device", "emulator-5554"))
    shadow_package = str(step.get("shadow_package", "com.yy.yfs"))
    game_package = str(step.get("game_package", "com.tencent.KiHan"))
    timeout = int(step.get("timeout", 7200))
    update_wait = max(0.0, float(step.pop("startup_update_wait", 0)))
    update_ui_markers = [str(x) for x in step.get(
        "update_ui_markers", step.get("update_markers", []))]
    maintenance_ui_markers = [
        str(x) for x in step.get(
            "maintenance_ui_markers", step.get("maintenance_markers", []))]
    ignored_update_markers = [
        str(x) for x in step.get(
            "ignored_update_markers", ["当前软件不是最新版"])]
    reported_ignored_updates: set[str] = set()
    shadow_update_popup_dismissals = 0
    shadow_continue_clicks = 0
    game_anr_wait_clicks = 0
    game_qq_consent_clicks = 0
    max_shadow_continue_clicks = max(
        1, int(step.get("max_shadow_continue_clicks", 6)))
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

    def park_float_for_maa(details: dict[str, Any]) -> bool:
        """Move the right-edge overlay away from Naruto's top-right controls.

        A normal swipe only opens the Shadow Clone menu.  Android's explicit
        drag-and-drop gesture performs the required long press before moving;
        the overlay then snaps to the right edge at the chosen lower height.
        MAA can subsequently recognise and click it against a stable ROI.
        """
        if not bool(step.get("park_float_before_maa", True)):
            return False
        point = details.get("point")
        if (not point or details.get("side") != "right"
                or current_orientation() != "landscape"):
            return False
        if int(point[1]) >= int(step.get("parked_float_min_y", 400)):
            return True
        if details.get("state") == "half_hidden":
            opened = tap(*point)
            if isinstance(opened, Exception) or opened.returncode != 0:
                return False
            if ctx.stop_event.wait(float(step.get(
                    "parked_float_unfold_wait", 1.6))):
                return False
            refreshed = locate_float_icon_details()
            if refreshed is not None and refreshed.get("side") == "right":
                point = refreshed["point"]
        image = capture_image()
        if image is None or image.shape[1] <= image.shape[0]:
            return False
        height, width = image.shape[:2]
        target_x = int(step.get("parked_float_drag_x", round(width * 0.86)))
        target_y = int(step.get("parked_float_drag_y", round(height * 0.78)))
        dragged = command([
            "shell", "input", "touchscreen", "draganddrop",
            str(int(point[0])), str(int(point[1])), str(target_x), str(target_y),
            str(max(500, int(step.get("parked_float_drag_duration_ms", 1500)))),
        ])
        if isinstance(dragged, Exception) or dragged.returncode != 0:
            ctx.log(f"移动影分身悬浮球失败，将保留原位置交给 MAA：{dragged}")
            return False
        if ctx.stop_event.wait(float(step.get("parked_float_settle_wait", 1.2))):
            return False
        parked = locate_float_icon_details()
        parked_ok = bool(parked and parked.get("side") == "right"
                         and parked["point"][1] >= int(step.get(
                             "parked_float_min_y", 400)))
        if parked_ok:
            ctx.log(f"已将影分身悬浮球长按拖放到右下固定区域：{parked['point']}；"
                    "后续由 MAA 识别并点击")
        else:
            ctx.log("悬浮球拖放后未在右下固定区域确认，MAA 将同时检查原位和固定位置")
        return parked_ok

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
        than one floating-ball width from either screen edge.  Portrait mode
        normally leaves it on the right, while Naruto can rotate to landscape and
        move the same overlay to the left edge.  Prefer the component closest to an
        edge instead of the largest red component on screen.
        """
        try:
            import cv2
            image = capture_image()
            if image is None:
                return None
            height, width = image.shape[:2]
            hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
            # LDPlayer darkens the overlay on its launcher and behind dialogs;
            # the cloud then shifts from vivid red toward dark magenta.
            red = (((hsv[:, :, 0] <= 20) | (hsv[:, :, 0] >= 140)) &
                   (hsv[:, :, 1] >= 70) & (hsv[:, :, 2] >= 35)).astype("uint8")
            # In landscape mode the half-hidden ball can sit against the top-left
            # corner (its red component starts around y=8 on a 720p frame), so a
            # broad top crop would erase the very target we need to click.
            red[:round(height * 0.005), :] = 0
            red[round(height * 0.92):, :] = 0
            edge_band = max(80, round(width * 0.07))
            middle_left = min(width, edge_band)
            middle_right = max(0, width - edge_band)
            if middle_right > middle_left:
                red[:, middle_left:middle_right] = 0
            count, _, stats, centers = cv2.connectedComponentsWithStats(red, 8)
            candidates = []
            for index in range(1, count):
                x = int(stats[index, cv2.CC_STAT_LEFT])
                y = int(stats[index, cv2.CC_STAT_TOP])
                box_width = int(stats[index, cv2.CC_STAT_WIDTH])
                box_height = int(stats[index, cv2.CC_STAT_HEIGHT])
                area = int(stats[index, cv2.CC_STAT_AREA])
                center_x, center_y = centers[index]
                left_gap = x
                right_gap = width - (x + box_width)
                edge_gap = min(left_gap, right_gap)
                if not (20 <= area <= 1800 and 5 <= box_width <= 72
                        and 16 <= box_height <= 76 and edge_gap <= edge_band):
                    continue
                side = "left" if left_gap <= right_gap else "right"
                center_edge_gap = center_x if side == "left" else width - center_x
                state = "half_hidden" if edge_gap <= 7 or center_edge_gap <= 30 else "full"
                candidates.append((edge_gap, -area, int(round(center_x)),
                                   int(round(center_y)), state, side,
                                   (x, y, box_width, box_height)))
            if not candidates:
                # While the script is running the cloud artwork is replaced by
                # a white stop square inside a pink ball.  Re-entering the
                # workflow must recognise that state without tapping it.  Look
                # only in the extreme edge strip so white game HUD icons cannot
                # become stop-control false positives.
                # The game dims the entire frame behind modal dialogs, turning
                # the nominally white stop square grey (V is often 150-190).
                pale = (((hsv[:, :, 1] <= 95) & (hsv[:, :, 2] >= 140))
                        .astype("uint8"))
                pale[:round(height * 0.03), :] = 0
                pale[round(height * 0.92):, :] = 0
                stop_edge_band = max(38, round(width * 0.035))
                if width > stop_edge_band * 2:
                    pale[:, stop_edge_band:width - stop_edge_band] = 0
                stop_count, _, stop_stats, stop_centers = \
                    cv2.connectedComponentsWithStats(pale, 8)
                stop_candidates = []
                for index in range(1, stop_count):
                    x = int(stop_stats[index, cv2.CC_STAT_LEFT])
                    y = int(stop_stats[index, cv2.CC_STAT_TOP])
                    box_width = int(stop_stats[index, cv2.CC_STAT_WIDTH])
                    box_height = int(stop_stats[index, cv2.CC_STAT_HEIGHT])
                    area = int(stop_stats[index, cv2.CC_STAT_AREA])
                    center_x, center_y = stop_centers[index]
                    left_gap = x
                    right_gap = width - (x + box_width)
                    edge_gap = min(left_gap, right_gap)
                    if not (55 <= area <= 900 and 7 <= box_width <= 38
                            and 7 <= box_height <= 45 and edge_gap <= 8):
                        continue
                    side = "left" if left_gap <= right_gap else "right"
                    stop_candidates.append((edge_gap, -area,
                                            int(round(center_x)),
                                            int(round(center_y)), side,
                                            (x, y, box_width, box_height)))
                if not stop_candidates:
                    return None
                _, _, click_x, click_y, side, bounds = min(stop_candidates)
                return {"point": (click_x, click_y),
                        "state": "half_hidden", "side": side,
                        "bounds": bounds, "control_hint": "stop"}
            _, _, click_x, click_y, state, side, bounds = min(candidates)
            return {"point": (click_x, click_y), "state": state,
                    "side": side, "bounds": bounds}
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
        def report_failure(detail: str) -> None:
            if getattr(read_ui_xml, "_failure_reported", False):
                return
            read_ui_xml._failure_reported = True
            ctx.log(f"UIAutomator 读取失败（后续省略同类日志）：{detail}")

        remote_ui = "/sdcard/gameflow-ui.xml"
        dumped = command(["shell", "uiautomator", "dump", remote_ui], seconds=20)
        if isinstance(dumped, Exception):
            report_failure(f"dump 异常：{dumped}")
            return ""
        if dumped.returncode != 0:
            report_failure(
                f"dump 失败 rc={dumped.returncode}："
                f"{dumped.stderr.strip()[-300:]}")
            return ""
        xml = command(["shell", "cat", remote_ui], seconds=20)
        if isinstance(xml, Exception):
            report_failure(f"cat 异常：{xml}")
            return ""
        if xml.returncode != 0:
            report_failure(
                f"cat 失败 rc={xml.returncode}：{xml.stderr.strip()[-300:]}")
            return ""
        if not xml.stdout.strip():
            report_failure("cat 返回空内容")
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

    def visual_ui_ready() -> bool:
        """Screenshot fallback when Shadow Clone kills UIAutomator.

        After the service has been enabled once, ``uiautomator dump`` exits
        with rc=137 on some builds.  The real custom UI is still on screen, so
        match the stable tab strip and the big ``启动功能`` button by template
        in the current frame instead of reading the accessibility XML.
        """
        try:
            import cv2
            image = capture_image()
            if image is None:
                return False
            tabs_path = Path(expand(str(step.get(
                "shadow_ui_tabs_template",
                ctx.root / "maa_naruto" / "resource" / "image"
                / "shadow_tabs.png"))))
            enable_path = Path(expand(str(step.get(
                "shadow_ui_enable_template",
                ctx.root / "maa_naruto" / "resource" / "image"
                / "shadow_enable.png"))))
            tabs_template = cv2.imread(str(tabs_path), cv2.IMREAD_COLOR)
            enable_template = cv2.imread(str(enable_path), cv2.IMREAD_COLOR)
            if tabs_template is None or enable_template is None:
                return False
            tabs_threshold = float(step.get("shadow_ui_tabs_threshold", 0.82))
            enable_threshold = float(step.get(
                "shadow_ui_enable_threshold", 0.80))
            tabs_result = cv2.matchTemplate(
                image, tabs_template, cv2.TM_CCOEFF_NORMED)
            _, tabs_score, _, _ = cv2.minMaxLoc(tabs_result)
            if tabs_score < tabs_threshold:
                return False
            # 启动功能 button is the bottom black strip; restrict to that band
            # so other dark game art cannot produce false positives.
            enable_roi = image[1000:1280, 0:720]
            if enable_roi.size == 0:
                return False
            enable_result = cv2.matchTemplate(
                enable_roi, enable_template, cv2.TM_CCOEFF_NORMED)
            _, enable_score, _, _ = cv2.minMaxLoc(enable_result)
            return enable_score >= enable_threshold
        except (ImportError, OSError, subprocess.TimeoutExpired):
            return False

    def save_status_evidence(kind: str) -> str | None:
        default_name = (
            "naruto_maintenance_prompt.png"
            if kind == "maintenance" else "naruto_update_prompt.png")
        configured = (
            step.get("maintenance_evidence_path")
            if kind == "maintenance" else step.get("update_evidence_path"))
        output = Path(expand(str(
            configured or ctx.root / "logs" / default_name)))
        image = capture_image()
        if image is None:
            return None
        try:
            import cv2
            output.parent.mkdir(parents=True, exist_ok=True)
            if cv2.imwrite(str(output), image):
                return str(output)
        except (ImportError, OSError):
            pass
        return None

    def visible_special_result() -> Result | None:
        """Return only for authoritative update/maintenance text.

        The optional Shadow Clone message “当前软件不是最新版” is intentionally
        recorded and ignored; it must never turn an otherwise runnable daily
        flow into a needs-update loop.
        """
        texts = visible_ui_texts()
        folded_text = "\n".join(texts).casefold()
        ignored = next(
            (marker for marker in ignored_update_markers
             if marker and marker.casefold() in folded_text), None)
        if ignored and ignored not in reported_ignored_updates:
            reported_ignored_updates.add(ignored)
            evidence = save_status_evidence("update")
            ctx.log(
                f"检测到可忽略的影分身版本提示“{ignored}”，"
                f"已留存截图并继续运行{f'：{evidence}' if evidence else ''}")
        update_marker = next(
            (marker for marker in update_ui_markers
             if (marker and marker not in ignored_update_markers
                 and marker.casefold() in folded_text)), None)
        if update_marker:
            return _update_skip_result(
                "火影忍者/影分身", step, update_marker,
                {"evidence": save_status_evidence("update"),
                 "visible_texts": sorted(texts)},
                reason_code="mandatory_update_ui")
        maintenance_marker = next(
            (marker for marker in maintenance_ui_markers
             if marker and marker.casefold() in folded_text), None)
        if maintenance_marker:
            return _maintenance_skip_result(
                "火影忍者", step, maintenance_marker,
                {"evidence": save_status_evidence("maintenance"),
                 "visible_texts": sorted(texts)})
        nonlocal shadow_update_popup_dismissals
        visual = capture_metrics()
        if visual and visual.get("shadow_mandatory_update_popup"):
            marker = "影分身公告：必须手动更新"
            grace_versions = max(
                0, int(step.get("shadow_update_grace_versions", 3)))

            def parse_version(value: str | None):
                if not value:
                    return None
                match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", value)
                return (tuple(int(part) for part in match.groups())
                        if match else None)

            current_version = parse_version(str(
                step.get("shadow_current_version", "")))
            if current_version is None:
                package_info = command(
                    ["shell", "dumpsys", "package", shadow_package], 15)
                if not isinstance(package_info, Exception):
                    version_match = re.search(
                        r"versionName\s*=\s*([^\s]+)", package_info.stdout)
                    current_version = parse_version(
                        version_match.group(1) if version_match else None)
            latest_version = parse_version(str(
                step.get("shadow_latest_version", "")))
            if latest_version is None:
                candidates = [
                    parsed for parsed in
                    (parse_version(text) for text in texts)
                    if parsed is not None and parsed != current_version]
                latest_version = max(candidates) if candidates else None
            version_gap = None
            # Only a patch-number difference in the same major/minor line is
            # a provable count of skipped releases.  If the announcement hides
            # its version (as v6.2.3 currently does), do not claim it is >3.
            if (current_version and latest_version
                    and current_version[:2] == latest_version[:2]):
                version_gap = max(0, latest_version[2] - current_version[2])

            if version_gap is None or version_gap <= grace_versions:
                max_dismissals = max(
                    1, int(step.get("max_shadow_update_popup_dismissals", 3)))
                if shadow_update_popup_dismissals >= max_dismissals:
                    return Result(
                        False,
                        "影分身更新公告连续关闭后仍反复出现，停止空循环并请求重试",
                        {"popup_dismissals": shadow_update_popup_dismissals,
                         "max_popup_dismissals": max_dismissals,
                         "retry_step": True})
                evidence = save_status_evidence("update")
                image_width = int(visual.get("_width", 1280))
                image_height = int(visual.get("_height", 720))
                # The two-button notice asks whether to update.  Daily automation
                # must decline non-forced updates, otherwise tapping the left
                # “是” button starts the updater and the launch loop loses its
                # floating control.  The right button is “否” in both layouts.
                fallback_point = ([760, 500] if image_width >= image_height
                                  else [480, 828])
                point = tuple(oriented_value(
                    "shadow_update_confirm_point", fallback_point))
                tap(*point)
                shadow_update_popup_dismissals += 1
                ctx.log(
                    "影分身更新公告未能确认落后超过"
                    f" {grace_versions} 个版本"
                    f"（当前={current_version or '未知'}，最新={latest_version or '未知'}，"
                    f"差值={version_gap if version_gap is not None else '未知'}），"
                    "已关闭公告并继续运行："
                    f"{point}{f'；{evidence}' if evidence else ''}")
                ctx.stop_event.wait(float(step.get(
                    "shadow_update_popup_close_wait", 3)))
                return None
            return _update_skip_result(
                "火影忍者/影分身", step, marker,
                {"evidence": save_status_evidence("update"),
                 "popup_white_bottom_fraction": visual.get(
                     "popup_white_bottom_fraction"),
                 "current_version": current_version,
                 "latest_version": latest_version,
                 "version_gap": version_gap,
                 "grace_versions": grace_versions},
                reason_code="mandatory_shadow_update_popup")
        return None

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
        nonlocal shadow_continue_clicks
        detected = (find_ui_text_point_exact("继续")
                    or find_ui_text_point_exact("确定"))
        detected_by_visual = False
        if detected is None:
            visual = capture_metrics()
            if visual and visual.get("shadow_authorization_continue"):
                detected = oriented_value(
                    "shadow_authorization_continue_point", (922, 556))
                detected_by_visual = True
        if detected is None:
            if log_missing:
                ctx.log("未识别到影分身“继续”按钮，直接执行当前循环的原操作")
            return Result(True, "未发现影分身继续按钮", {"clicked": False})
        if shadow_continue_clicks >= max_shadow_continue_clicks:
            return Result(
                False,
                f"影分身“继续”弹窗已重复出现并达到 "
                f"{max_shadow_continue_clicks} 次点击上限",
                {"shadow_continue_clicks": shadow_continue_clicks,
                 "max_shadow_continue_clicks": max_shadow_continue_clicks})
        # Prefer the live UIAutomator centre.  Shadow Clone has alternated this
        # button between “继续” and “确定”, and its position differs between
        # portrait releases; a stale fixed point can hit the dialog body.
        point = detected
        done = tap(*point)
        if isinstance(done, Exception) or done.returncode != 0:
            return Result(False, f"点击影分身“继续”失败：{done}")
        shadow_continue_clicks += 1
        source = "授权界面视觉布局" if detected_by_visual else "实时控件位置"
        ctx.log(f"第 {shadow_continue_clicks} 次识别到影分身说明弹窗按钮，"
                f"已按{source}点击：{point}")
        if ctx.stop_event.wait(max(5.0, float(step.get("shadow_continue_wait", 8)))):
            return Result(False, "任务被用户停止")
        return Result(True, "影分身更新日志已继续", {"clicked": True})

    def handle_game_blocking_page() -> Result:
        """Recover from Android ANR and Tencent QQ authorization pages.

        These native pages can cover Naruto after Shadow Clone has launched it.
        Live UI text is used instead of coordinates.  This helper never touches
        the floating square/stop control.
        """
        nonlocal game_anr_wait_clicks, game_qq_consent_clicks
        texts = visible_ui_texts()
        folded = "\n".join(texts).casefold()
        is_anr = (("没有响应" in folded or "isn't responding" in folded)
                  and ("关闭应用" in texts or "等待" in texts))
        if is_anr:
            limit = max(1, int(step.get("max_game_anr_wait_clicks", 5)))
            if game_anr_wait_clicks >= limit:
                return Result(False, f"火影忍者连续 {limit} 次无响应，停止重复等待")
            point = find_ui_text_point_exact("等待", "Wait")
            if point is None:
                return Result(True, "检测到无响应弹窗但暂未找到等待按钮",
                              {"clicked": False})
            done = tap(*point)
            if isinstance(done, Exception) or done.returncode != 0:
                return Result(False, f"点击火影忍者无响应弹窗“等待”失败：{done}")
            game_anr_wait_clicks += 1
            ctx.log(f"检测到火影忍者无响应，已点击“等待”而非关闭应用：{point}")
            ctx.stop_event.wait(max(3.0, float(step.get("game_anr_wait_seconds", 8))))
            return Result(True, "已处理火影忍者无响应弹窗", {"clicked": True})

        qq_authorization = any(marker in folded for marker in (
            "你的qq头像、昵称和朋友关系", "同意授权登录", "申请使用"))
        if qq_authorization and "同意" in texts:
            limit = max(1, int(step.get("max_game_qq_consent_clicks", 3)))
            if game_qq_consent_clicks >= limit:
                return Result(False, f"火影忍者 QQ 授权页连续出现 {limit} 次")
            point = find_ui_text_point_exact("同意")
            if point is None:
                return Result(True, "检测到 QQ 授权页但暂未找到同意按钮",
                              {"clicked": False})
            done = tap(*point)
            if isinstance(done, Exception) or done.returncode != 0:
                return Result(False, f"点击火影忍者 QQ 授权页“同意”失败：{done}")
            game_qq_consent_clicks += 1
            ctx.log(f"检测到火影忍者 QQ 授权登录页，已按实时控件点击“同意”：{point}")
            ctx.stop_event.wait(max(3.0, float(step.get("game_qq_consent_wait", 8))))
            return Result(True, "已处理火影忍者 QQ 授权页", {"clicked": True})
        login_failed = ("登录失败" in folded and
                        ("网络异常" in folded or "稍后重试" in folded))
        if login_failed:
            point = find_ui_text_point_exact("确定")
            if point is not None:
                done = tap(*point)
                if isinstance(done, Exception) or done.returncode != 0:
                    return Result(False, f"关闭火影忍者临时登录失败弹窗失败：{done}")
                ctx.log(f"检测到火影忍者临时网络登录失败，已点击“确定”等待脚本重试：{point}")
                ctx.stop_event.wait(max(3.0, float(step.get(
                    "game_login_retry_wait", 12))))
                return Result(True, "已处理火影忍者临时登录失败", {"clicked": True})
        return Result(True, "未发现游戏阻塞页", {"clicked": False})

    def ensure_game_login_agreement() -> bool:
        """Tick Naruto's title-page agreement only when it is visibly unchecked.

        The game renders this control inside Unity, so UIAutomator cannot see
        it.  Shadow Clone can otherwise press a login provider, briefly reach
        the server page, and then fall back to the title page without ever
        entering its running state.  Identify the four-provider layout first,
        then inspect the checkbox core for the yellow checked mark.
        """
        try:
            import cv2
            import numpy as np
            image = capture_image()
            if image is None or image.shape[1] <= image.shape[0]:
                return False
            height, width = image.shape[:2]
            hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

            provider_strip = hsv[
                round(height * 0.70):round(height * 0.82),
                round(width * 0.10):round(width * 0.94)]
            green_ratio = float(np.mean(
                (provider_strip[:, :, 0] >= 35)
                & (provider_strip[:, :, 0] <= 85)
                & (provider_strip[:, :, 1] > 100)))
            cyan_ratio = float(np.mean(
                (provider_strip[:, :, 0] >= 85)
                & (provider_strip[:, :, 0] <= 115)
                & (provider_strip[:, :, 1] > 100)))
            if (green_ratio < float(step.get("game_login_green_ratio", 0.15))
                    or cyan_ratio < float(step.get(
                        "game_login_cyan_ratio", 0.15))):
                return False

            checkbox = hsv[
                round(height * 0.835):round(height * 0.885),
                round(width * 0.068):round(width * 0.097)]
            checked_yellow = float(np.mean(
                (checkbox[:, :, 0] >= 15) & (checkbox[:, :, 0] <= 42)
                & (checkbox[:, :, 1] > 90) & (checkbox[:, :, 2] > 120)))
            if checked_yellow >= float(step.get(
                    "game_login_checkbox_yellow_ratio", 0.08)):
                return False
            point = (round(width * float(step.get(
                         "game_login_checkbox_x_fraction", 0.083))),
                     round(height * float(step.get(
                         "game_login_checkbox_y_fraction", 0.858))))
            done = tap(*point)
            if isinstance(done, Exception) or done.returncode != 0:
                return False
            ctx.log(
                "检测到火影登录方式页且协议框未勾选，已先勾选游戏协议再启动影分身："
                f"{point}（green={green_ratio:.3f}, cyan={cyan_ratio:.3f}, "
                f"yellow={checked_yellow:.3f}）")
            ctx.stop_event.wait(max(1.0, float(step.get(
                "game_login_checkbox_wait", 2))))
            return True
        except (ImportError, OSError, subprocess.TimeoutExpired):
            return False

    def maa_naruto_click(entry: str) -> tuple[bool, str]:
        """Delegate one image-recognition click sequence to MaaFramework."""
        if not step.get("use_maa_naruto_start", True):
            return False, "MAA 火影启动已在配置中关闭"
        clicker = Path(expand(str(step.get(
            "maa_naruto_clicker",
            ctx.root / "maa_naruto" / "maa_naruto_click.py"))))
        runtime = Path(expand(str(step.get(
            "maa_runtime", r"G:\project_X\dev"))))
        python = Path(expand(str(step.get(
            "maa_python", r"D:\python\python.exe"))))
        if not clicker.exists():
            return False, f"MAA 火影点击器不存在：{clicker}"
        if not (runtime / "MaaFramework.dll").exists():
            return False, f"MAA 运行库不存在：{runtime}"
        args = [str(python), str(clicker), entry,
                "--runtime", str(runtime), "--adb", str(adb),
                "--device", device]
        try:
            done = subprocess.run(
                args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                timeout=max(10.0, float(step.get("maa_click_timeout", 35))),
                creationflags=(subprocess.CREATE_NO_WINDOW
                               if os.name == "nt" else 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"MAA 调用异常：{exc}"
        payload = None
        for line in reversed(done.stdout.splitlines()):
            try:
                candidate = json.loads(line.strip())
            except (ValueError, TypeError):
                continue
            if isinstance(candidate, dict) and "success" in candidate:
                payload = candidate
                break
        if done.returncode == 0 and payload and payload.get("success"):
            version = payload.get("framework_version", "未知版本")
            return True, f"MaaFramework {version} 已完成 {entry}"
        detail = ((payload or {}).get("error")
                  or (payload or {}).get("status")
                  or done.stderr.strip()[-500:]
                  or f"退出码 {done.returncode}")
        return False, f"MAA {entry} 未命中：{detail}"

    prelaunch_details = locate_float_icon_details()
    preexisting_running = bool(
        prelaunch_details and prelaunch_details.get("control_hint") == "stop")
    if (step.get("reset_stale_shadow_service", True)
            and not preexisting_running):
        # A red cloud can survive after Shadow Clone's task engine has died.
        # Reusing that overlay makes every later Start tap look valid but it
        # never changes to the stop square.  Clear only this stale (non-running)
        # state; an authoritative white stop square is always preserved.
        command(["shell", "am", "force-stop", shadow_package], 15)
        ctx.log("启动前未发现白色终止符，已清理影分身残留悬浮服务后重新启动")
        if ctx.stop_event.wait(float(step.get("shadow_reset_wait", 3))):
            return Result(False, "任务被用户停止")

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
    if update_wait:
        ctx.log(
            f"火影忍者检测到昨日更新标志，影分身启动后先等待 "
            f"{update_wait:g} 秒完成自动更新")
        waited = _wait_with_emulator_watchdog(update_wait, watchdog, ctx)
        if waited is not None:
            return waited
        step["_startup_update_wait_consumed"] = True
        ctx.log("火影忍者自动更新等待结束，继续执行影分身启动流程")
    special = visible_special_result()
    if special is not None:
        return special
    if step.get("shadow_open_confirm", True):
        continued = click_shadow_continue_if_visible()
        if not continued.success:
            return continued

    # Shadow Clone first renders a temporary placeholder page and loads the
    # script-specific controls asynchronously.  "启动功能" can already be
    # present on that placeholder, but tapping it creates a red overlay whose
    # Start command immediately falls back to the triangle.  Wait for the real
    # Naruto tabs before performing any coordinate or text-based start tap.
    ui_ready_deadline = time.monotonic() + max(
        15.0, float(step.get("shadow_ui_ready_timeout", 90)))
    ui_ready_markers = {str(value) for value in step.get(
        "shadow_ui_ready_markers",
        ["常用设置", "挂机任务", "任务设置", "其他设置"])}
    placeholder_markers = {str(value) for value in step.get(
        "shadow_ui_placeholder_markers", ["该脚本暂无定制界面"])}
    ready_required = max(1, int(step.get("shadow_ui_ready_required", 2)))
    placeholder_reported = False
    while time.monotonic() < ui_ready_deadline:
        if ctx.stop_event.is_set():
            return Result(False, "任务被用户停止")
        special = visible_special_result()
        if special is not None:
            return special
        texts = visible_ui_texts()
        visible_ready = sorted(ui_ready_markers.intersection(texts))
        text_ready = (len(visible_ready) >= ready_required
                      and "启动功能" in texts)
        visual_ready = False
        if not text_ready:
            # Shadow Clone kills uiautomator once its service has been enabled,
            # so the text probe can stay empty even though the real custom UI
            # is already on screen.  Confirm the same fact with the tab-strip
            # and Start-button templates before allowing the click.
            visual_ready = visual_ui_ready()
        if text_ready or visual_ready:
            ctx.log(
                "影分身定制界面已加载，允许点击启动功能："
                + ("、".join(visible_ready) if text_ready
                   else "截图模板已确认标签栏与启动功能按钮"))
            break
        visible_placeholder = sorted(placeholder_markers.intersection(texts))
        if visible_placeholder and not placeholder_reported:
            placeholder_reported = True
            ctx.log(
                "影分身仍显示“该脚本暂无定制界面”，等待脚本配置加载，"
                "此时不点击启动功能")
        if ctx.stop_event.wait(max(1.0, float(step.get(
                "shadow_ui_ready_poll_seconds", 3)))):
            return Result(False, "任务被用户停止")
    else:
        evidence = save_status_evidence("update")
        return Result(
            False,
            "影分身定制界面在等待期限内仍未加载，已停止本轮以避免误启动",
            {"retry_step": True,
             "placeholder_seen": placeholder_reported,
             "evidence": evidence})
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
    max_enable_clicks = max(1, int(step.get("max_enable_clicks", 12)))

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
        special = visible_special_result()
        if special is not None:
            return special
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
        if enable_attempt >= max_enable_clicks:
            return Result(
                False,
                f"影分身已达到 {max_enable_clicks} 次“启动功能”点击上限，"
                "仍未确认浮窗或运行状态",
                {"enable_clicks": enable_attempt,
                 "max_enable_clicks": max_enable_clicks})
        enable_attempt += 1
        maa_clicked, maa_message = maa_naruto_click(
            "NarutoClickShadowEnable")
        if maa_clicked:
            done = None
            click_source = maa_message
        else:
            if not step.get("maa_click_coordinate_fallback", True):
                return Result(False, maa_message,
                              {"retry_step": True, "maa_entry":
                               "NarutoClickShadowEnable"})
            done = tap(*live_enable_point)
            if isinstance(done, Exception) or done.returncode != 0:
                return Result(False, f"点击影分身“启动功能”失败：{done}")
            click_source = f"MAA 未命中后坐标备用：{maa_message}"
        ctx.log(f"第 {enable_attempt} 次点击影分身“启动功能”：{live_enable_point}；"
                f"点击前已确认前台包 {shadow_package}；{click_source}")
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
    max_float_start_attempts = max(
        1, int(step.get(
            "max_float_start_attempts",
            step.get("max_float_start_clicks",
                     step.get("max_start_clicks", 16)))))
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

    def classify_control_at(center_x: int, center_y: int,
                            source_image=None) -> str:
        """Classify a white triangle/square around one screen coordinate."""
        try:
            import cv2
            import numpy as np
            image = source_image if source_image is not None else capture_image()
            if image is None:
                return "unknown"
            radius = max(20, int(step.get("float_control_radius", 30)))
            top, bottom = max(0, center_y - radius), min(image.shape[0], center_y + radius)
            left, right = max(0, center_x - radius), min(image.shape[1], center_x + radius)
            crop = image[top:bottom, left:right]
            if crop.size == 0 or min(crop.shape[:2]) < 20:
                return "unknown"
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            # The real Start/Stop glyph is drawn inside a dark circular menu
            # button.  Bright Naruto splash artwork contains many white
            # triangles and previously produced false "start" matches while
            # the half-hidden red ball had merely slid fully onto the screen.
            dark_ratio = float(np.count_nonzero(hsv[:, :, 2] <= int(step.get(
                "float_control_dark_value", 120)))) / max(1, hsv.shape[0] * hsv.shape[1])
            if dark_ratio < float(step.get("float_control_min_dark_ratio", 0.35)):
                return "unknown"
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

    def classify_float_primary_control(icon_point: tuple[int, int]) -> str:
        """Classify the expanded menu's first control as Start or Stop."""
        # The menu expands inward from the red ball.  A fixed x=293 only works
        # for the portrait/right-ball layout; in landscape the same menu is at
        # x≈853 when the ball is on the right (and mirrored on the left).
        image = capture_image()
        if image is None:
            return "unknown"
        height, width = image.shape[:2]
        # Do not recapture the screen here.  The floating menu auto-collapses
        # quickly; the old implementation captured once for every grid probe
        # plus two more times for icon/orientation, so classification commonly
        # finished only after the menu had disappeared.
        effective_point = icon_point
        center_edge_gap = min(effective_point[0], width - effective_point[0])
        state = "full" if center_edge_gap > 30 else "half_hidden"
        default_gap = 380 if state == "full" else 410
        gap = int(step.get("float_menu_primary_gap_full" if state == "full"
                           else "float_menu_primary_gap", default_gap))
        if width and effective_point[0] > width // 2:
            center_x = int(effective_point[0] - gap)
        elif width:
            center_x = int(effective_point[0] + gap)
        else:
            center_x = int(oriented_value("float_start_x", 293))
        orientation = "landscape" if width > height else "portrait"
        orientation_settings = step.get("orientation_points", {}).get(
            orientation, {})
        center_y = int(effective_point[1] + float(orientation_settings.get(
            "float_start_y_offset", step.get("float_start_y_offset", 49))))
        # The red cloud component's centroid is not the same as the circular
        # ball centre (usually off by 10-30 px).  Probe a tight grid and only
        # accept symbols that also passed the dark-circle guard above.
        results = []
        for offset_x in (0, -30, 30):
            for offset_y in (0, -12, 12):
                kind = classify_control_at(center_x + offset_x,
                                           center_y + offset_y, image)
                if kind != "unknown":
                    results.append(kind)
        if "stop" in results:
            return "stop"
        if "start" in results:
            return "start"
        return "unknown"

    def locate_menu_start_button(
            icon_point: tuple[int, int],
            source_image=None) -> tuple[tuple[int, int], float] | None:
        """Locate the expanded menu's exact Start-button centre.

        The red-cloud centroid is not the circular ball centre, and the radial
        menu also curves slightly around the ball (the Start button sits ~24 px
        above a bottom-right ball but only ~16 px above a top-right ball).
        Fixed offsets therefore sometimes tap below the button.  Instead, cut a
        tight band beside the live ball and template-match the real button crop
        ``float_menu_start.png`` (captured at 720p from the same menu).
        """
        try:
            import cv2
            image = source_image if source_image is not None else capture_image()
            if image is None or image.shape[1] <= image.shape[0]:
                return None
            template_path = Path(expand(str(step.get(
                "float_menu_start_template",
                ctx.root / "maa_naruto" / "resource" / "image"
                / "float_menu_start.png"))))
            template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
            if template is None:
                return None
            height, width = image.shape[:2]
            template_height, template_width = template.shape[:2]
            ball_x, ball_y = int(icon_point[0]), int(icon_point[1])
            gap = max(template_width,
                      int(step.get("float_menu_start_search_gap", 430)))
            half_height = max(template_height,
                              int(step.get("float_menu_start_search_half_height", 80)))
            if ball_x > width // 2:
                x_start = max(0, ball_x - gap)
                x_end = min(width, ball_x)
            else:
                x_start = min(width - 1, ball_x)
                x_end = min(width, ball_x + gap)
            y_start = max(0, ball_y - half_height)
            y_end = min(height, ball_y + half_height)
            if x_end - x_start < template_width or y_end - y_start < template_height:
                return None
            crop = image[y_start:y_end, x_start:x_end]
            result = cv2.matchTemplate(crop, template, cv2.TM_CCOEFF_NORMED)
            _, score, _, point = cv2.minMaxLoc(result)
            threshold = float(step.get("float_menu_start_threshold", 0.72))
            if score < threshold:
                return None
            center = (x_start + int(point[0]) + template_width // 2,
                      y_start + int(point[1]) + template_height // 2)
            return center, float(score)
        except (ImportError, OSError, subprocess.TimeoutExpired):
            return None

    def classify_collapsed_float_control(icon_point: tuple[int, int]) -> str:
        """Inspect the floating ball itself without tapping it.

        New Shadow Clone builds show a white stop square inside the floating
        ball while running.  That square is authoritative evidence and must not
        be clicked, because doing so can terminate the script.
        """
        try:
            import cv2
            import numpy as np
            image = capture_image()
            if image is None:
                return "unknown"
            x, y = (int(icon_point[0]), int(icon_point[1]))
            radius = max(8, int(step.get("collapsed_stop_core_radius", 14)))
            crop = image[max(0, y - radius):min(image.shape[0], y + radius),
                         max(0, x - radius):min(image.shape[1], x + radius)]
            if crop.size == 0 or min(crop.shape[:2]) < radius:
                return "unknown"
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            pale = ((hsv[:, :, 1] <= int(step.get(
                        "collapsed_stop_max_saturation", 80))) &
                    (hsv[:, :, 2] >= int(step.get(
                        "collapsed_stop_min_value", 190))))
            ratio = float(np.count_nonzero(pale)) / max(1, pale.size)
            return ("stop" if ratio >= float(step.get(
                    "collapsed_stop_min_core_ratio", 0.75)) else "unknown")
        except (ImportError, OSError, subprocess.TimeoutExpired):
            return "unknown"

    def inspect_float_primary_control(icon_point: tuple[int, int]) -> str:
        """Open the overlay, require two identical classifications, then collapse it."""
        active_point = icon_point
        opened_menu = False
        # A half-hidden ball does not always react to the first tap.  Depending
        # on the current animation frame it can take one tap to expose the ball
        # and another to expand the menu.  Re-locate after every tap instead of
        # assuming a fixed two-tap sequence.
        for _ in range(max(2, int(step.get("float_menu_open_taps", 3)))):
            before = locate_float_icon_details()
            before_state = before.get("state") if before is not None else None
            if before is not None:
                active_point = before["point"]
            opened = tap(*active_point)
            if isinstance(opened, Exception) or opened.returncode != 0:
                return "unknown"
            if before_state == "half_hidden":
                # The first tap merely exposes the full ball.  Its auto-hide
                # delay is shorter than the normal menu classification wait,
                # so immediately locate and tap it a second time.
                if ctx.stop_event.wait(float(step.get(
                        "float_unfold_wait", 0.8))):
                    return "unknown"
                unfolded = locate_float_icon_details()
                if unfolded is not None:
                    active_point = unfolded["point"]
                    if unfolded.get("state") == "full":
                        second = tap(*active_point)
                        if isinstance(second, Exception) or second.returncode != 0:
                            return "unknown"
            if ctx.stop_event.wait(menu_wait):
                return "unknown"
            if classify_float_primary_control(active_point) != "unknown":
                opened_menu = True
                break
            live = locate_float_icon_details()
            if live is not None:
                active_point = live["point"]
        if not opened_menu:
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
            current = classify_float_primary_control(active_point)
            stable = stable + 1 if current != "unknown" and current == previous else 1
            previous = current
            if stable >= required:
                result = current
                break
            if ctx.stop_event.wait(poll):
                break
        live = locate_float_icon_details()
        collapse_point = live["point"] if live is not None else active_point
        tap(*collapse_point)
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
        special = visible_special_result()
        if special is not None:
            return special
        blocker = handle_game_blocking_page()
        if not blocker.success:
            return blocker
        if blocker.details.get("clicked"):
            ctx.log("已处理游戏阻塞页；重新识别悬浮窗状态，不叠加点击")
            continue
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
        if not focused_on(game_package):
            # The floating ball only exposes Start/Settings/Info/Exit over the
            # target game.  On Shadow Clone's own settings page, tapping the
            # same ball merely alternates between half-hidden and full states.
            foreground_ok, foreground_message = ensure_package_foreground(
                game_package, wait_seconds=float(step.get(
                    "game_foreground_wait", 15)))
            ctx.log(f"浮窗点击前火影游戏不在前台：{foreground_message}")
            if not foreground_ok:
                if ctx.stop_event.wait(retry_interval):
                    return Result(False, "任务被用户停止")
                continue
        # This Unity checkbox is not exposed in Android's accessibility tree.
        # Handle it immediately before every floating Start attempt, including
        # cases where the game has returned to its title page after a failed
        # automatic login.
        ensure_game_login_agreement()
        if start_attempt >= max_float_start_attempts:
            return Result(
                False,
                f"影分身已达到 {max_float_start_attempts} 次悬浮窗启动上限，"
                "仍未识别到方形终止符",
                {"float_start_attempts": start_attempt,
                 "max_float_start_attempts": max_float_start_attempts})
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
        if icon_details.get("control_hint") == "stop":
            game_started = True
            ctx.log(
                f"第 {start_attempt} 次在屏幕边缘直接识别到影分身白色方形终止符："
                f"{icon_point}；确认脚本已运行，未点击该终止符")
            break
        side_label = "左侧" if icon_details.get("side") == "left" else "右侧"
        state_label = (f"半缩在{side_label}边缘"
                       if icon_details["state"] == "half_hidden"
                       else f"在{side_label}完整显示")
        ctx.log(f"第 {start_attempt} 次识别到影分身红色浮窗（{state_label}）："
                f"点击点 {icon_point}，红云范围 {icon_details['bounds']}")
        collapsed_control = classify_collapsed_float_control(icon_point)
        if collapsed_control == "stop":
            game_started = True
            ctx.log(f"第 {start_attempt} 次在悬浮球原位识别到方形终止符；"
                    "确认影分身已运行，全程未点击该方形符")
            break
        park_float_for_maa(icon_details)
        # Preferred path: one MaaFramework process performs the whole sequence
        # (locate the collapsed ball, unfold it if needed, open the radial menu,
        # recognise the Start button and click it).  Spawning a fresh MAA process
        # only after the menu is already open is too slow -- the menu collapses
        # while resources load -- so the sequence must start from the collapsed
        # ball where timing is forgiving.
        maa_started = False
        maa_start_message = "MAA 完整悬浮窗序列尚未执行"
        if step.get("maa_float_start_sequence", True):
            sequence_details = locate_float_icon_details()
            sequence_point = (sequence_details["point"]
                              if sequence_details is not None else icon_point)
            sequence_side = (sequence_details.get("side")
                             if sequence_details is not None
                             else icon_details.get("side", "right"))
            sequence_top = int(sequence_point[1]) < 360
            if sequence_side == "right":
                sequence_entry = ("NarutoStartFromFloatTop" if sequence_top
                                  else "NarutoStartFromFloatBottom")
            else:
                # Left-edge mirrors are not covered by the fixed ROIs yet.
                sequence_entry = "NarutoStartFromFloat"
            maa_sequence_ok, maa_sequence_message = maa_naruto_click(
                sequence_entry)
            if maa_sequence_ok:
                maa_started = True
                maa_start_message = maa_sequence_message
                ctx.log(
                    f"第 {start_attempt} 次 MAA {sequence_entry} 已识别并点击"
                    f"悬浮窗启动按钮：{maa_sequence_message}")
            else:
                maa_start_message = (
                    f"MAA 完整悬浮窗序列 {sequence_entry} 未命中："
                    f"{maa_sequence_message}")
        # MaaFramework's AdbShell controller recognises the landscape overlay
        # correctly, but some Shadow Clone builds discard its synthetic click.
        # The sequence result above is therefore treated as an attempt; if it
        # missed, the local OpenCV path below re-opens the menu with paced ADB
        # taps and clicks the exact template centre.
        maa_menu_control = "unknown"
        maa_menu_details = locate_float_icon_details()
        if (not maa_started
                and bool(step.get("maa_preopen_fast_path", False))
                and maa_menu_details is not None):
            maa_menu_point = maa_menu_details["point"]
            first_menu_tap = tap(*maa_menu_point)
            if (not isinstance(first_menu_tap, Exception)
                    and first_menu_tap.returncode == 0):
                was_half_hidden = maa_menu_details.get("state") == "half_hidden"
                if was_half_hidden:
                    if ctx.stop_event.wait(float(step.get(
                            "maa_float_unfold_wait", 1.6))):
                        return Result(False, "任务被用户停止")
                    unfolded = locate_float_icon_details()
                    if unfolded is not None:
                        maa_menu_details = unfolded
                        maa_menu_point = unfolded["point"]
                    tap(*maa_menu_point)
                    # The first tap on the freshly expanded ball is often
                    # consumed by its expansion animation.  A second paced tap
                    # is required before the radial menu really appears.
                    if ctx.stop_event.wait(float(step.get(
                            "maa_full_ball_second_tap_wait", 1.0))):
                        return Result(False, "任务被用户停止")
                    refreshed = locate_float_icon_details()
                    if refreshed is not None:
                        maa_menu_details = refreshed
                        maa_menu_point = refreshed["point"]
                    tap(*maa_menu_point)
                if ctx.stop_event.wait(float(step.get(
                        "maa_menu_settle_wait", 3.5))):
                    return Result(False, "任务被用户停止")
                maa_recognized, maa_start_message = maa_naruto_click(
                    "NarutoRecognizeStart")
                if not maa_recognized:
                    maa_menu_control = classify_float_primary_control(
                        maa_menu_point)
                    if maa_menu_control == "stop":
                        game_started = True
                        ctx.log("悬浮菜单已显示方形终止符；影分身原本已启动")
                if maa_recognized:
                    maa_menu_control = "start"
                    located_start = locate_menu_start_button(maa_menu_point)
                    if located_start is not None:
                        start_point = located_start[0]
                    elif maa_menu_details.get("side") == "right":
                        # Keep the legacy estimate as a last resort, but the
                        # real button centre is ~16-24 px above the ball.
                        start_point = (
                            max(1, int(maa_menu_point[0]) - 384),
                            max(1, int(maa_menu_point[1]) - 24))
                    else:
                        start_point = (
                            min(1279, int(maa_menu_point[0]) + 384),
                            max(1, int(maa_menu_point[1]) - 24))
                    start_click = tap(*start_point)
                    maa_started = (not isinstance(start_click, Exception)
                                   and start_click.returncode == 0)
                    if maa_started:
                        maa_start_message = (
                            f"{maa_start_message}；已通过模拟器触摸通道点击"
                            f" {start_point}")
        if game_started:
            break
        if maa_started:
            ctx.log(
                f"第 {start_attempt} 次已由 MAA 模板连续识别并点击"
                f"悬浮窗及启动按钮：{maa_start_message}")
            if ctx.stop_event.wait(after_click_wait):
                return Result(False, "任务被用户停止")
            blocker = handle_game_blocking_page()
            if not blocker.success:
                return blocker
            if blocker.details.get("clicked"):
                ctx.log("MAA 启动后已处理游戏阻塞页；重新确认悬浮窗状态")
            special = visible_special_result()
            if special is not None:
                return special
            post_start_metrics = capture_metrics()
            if (post_start_metrics
                    and post_start_metrics.get("completion_popup")
                    and not post_start_metrics.get("game_privacy_consent")
                    and not post_start_metrics.get("shadow_update_choice_popup")):
                # Shadow Clone shows a one-button optional update notice
                # immediately after floating Start.  It is not the >3-version
                # forced-update condition and must not block the home-screen
                # confirmation below.
                evidence = save_status_evidence("update")
                image_width = int(post_start_metrics.get("_width", 1280))
                image_height = int(post_start_metrics.get("_height", 720))
                bottom = (float(post_start_metrics.get(
                    "popup_white_bottom_fraction", 0.0)) * image_height)
                confirm_y = (int(round(bottom - image_height * 0.11))
                             if bottom else int(round(image_height * 0.715)))
                confirm_y = max(int(image_height * 0.62),
                                min(int(image_height * 0.86), confirm_y))
                confirm_point = (image_width // 2, confirm_y)
                tap(*confirm_point)
                ctx.log(
                    "MAA 启动后检测到影分身单按钮版本公告；该公告不属于超过 "
                    "3 个版本的强制更新，已点击“确定”继续："
                    f"{confirm_point}{f'，截图：{evidence}' if evidence else ''}")
                if ctx.stop_event.wait(float(step.get(
                        "shadow_update_popup_close_wait", 3))):
                    return Result(False, "任务被用户停止")
            confirm_point = locate_float_icon()
            confirmed_control = (inspect_float_primary_control(confirm_point)
                                 if confirm_point is not None else "unknown")
            ctx.log(
                f"第 {start_attempt} 次 MAA 点击启动后的本地图形状态："
                f"{confirmed_control}")
            if confirmed_control == "stop":
                game_started = True
                ctx.log(
                    f"第 {start_attempt} 次已由 MAA 点击启动，并识别到方形终止符")
                break
            # Some Shadow Clone builds immediately move/hide the floating ball
            # after accepting Start, so the stop square is impossible to verify.
            # The MAA sequence above already recognised the real Start button at
            # template score ~0.99 and clicked it.  Give the game a short grace
            # period to reach its own home/completion screen before falling
            # back to another floating-ball attempt.
            home_deadline = time.monotonic() + max(
                15.0, float(step.get("maa_start_home_grace", 60)))
            while time.monotonic() < home_deadline:
                start_metrics = capture_metrics()
                if (start_metrics and start_metrics.get("home_page")
                        and not start_metrics.get("game_privacy_consent")):
                    game_started = True
                    ctx.log(
                        f"第 {start_attempt} 次 MAA 点击启动后识别到火影完成页/主页，"
                        "视同影分身已开始运行")
                    break
                if ctx.stop_event.wait(5):
                    return Result(False, "任务被用户停止")
            if game_started:
                break
            ctx.log(
                f"第 {start_attempt} 次 MAA 已点击启动，但终止符确认结果为"
                f" {confirmed_control}；等待下一轮重新识别，不执行盲点")
            if ctx.stop_event.wait(retry_interval):
                return Result(False, "任务被用户停止")
            continue
        ctx.log(
            f"第 {start_attempt} 次 MAA 模板启动未命中，"
            f"转入原有受控备用识别：{maa_start_message}")
        primary_control = "unknown"
        menu_taps = max(2, int(step.get("float_menu_open_taps", 3)))
        for menu_tap in range(1, menu_taps + 1):
            before_tap_details = locate_float_icon_details()
            before_tap_state = (before_tap_details.get("state")
                                if before_tap_details is not None else None)
            if before_tap_details is not None:
                icon_point = before_tap_details["point"]
            opened = tap(*icon_point)
            if isinstance(opened, Exception) or opened.returncode != 0:
                ctx.log(f"第 {start_attempt} 次点击悬浮窗失败，稍后重新识别：{opened}")
                break
            if before_tap_state == "half_hidden":
                # The exposed ball auto-hides after only a few seconds.  Do not
                # run the expensive menu classifier before the second tap.
                if ctx.stop_event.wait(float(step.get(
                        "float_unfold_wait", 0.8))):
                    return Result(False, "任务被用户停止")
                unfolded = locate_float_icon_details()
                if unfolded is not None and unfolded.get("state") == "full":
                    icon_point = unfolded["point"]
                    second = tap(*icon_point)
                    if isinstance(second, Exception) or second.returncode != 0:
                        ctx.log(f"第 {start_attempt} 次点击完整悬浮球失败：{second}")
                        break
                    ctx.log(
                        f"第 {start_attempt} 次已在完整悬浮球自动缩回前立即二次点击："
                        f"{icon_point}")
                else:
                    if unfolded is not None:
                        icon_point = unfolded["point"]
                    ctx.log(
                        f"第 {start_attempt} 次半缩悬浮球点击后尚未完整展开，"
                        f"第 {menu_tap} 次快速重试")
                    continue
            if ctx.stop_event.wait(menu_wait):
                return Result(False, "任务被用户停止")
            primary_control = classify_float_primary_control(icon_point)
            if primary_control != "unknown":
                break
            live_details = locate_float_icon_details()
            if live_details is not None:
                icon_point = live_details["point"]
                ctx.log(
                    f"第 {start_attempt} 次悬浮球第 {menu_tap} 次点击后菜单尚未展开，"
                    f"已重新识别实时位置并继续：{icon_point}"
                    f"（{live_details.get('state', 'unknown')}）")
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
        live_menu_details = locate_float_icon_details()
        menu_icon_point = (live_menu_details["point"] if live_menu_details is not None
                           else icon_point)
        menu_icon_state = (live_menu_details.get("state")
                           if live_menu_details is not None else "half_hidden")
        start_located = locate_menu_start_button(menu_icon_point)
        if start_located is not None:
            start_point, start_score = start_located
            ctx.log(
                f"第 {start_attempt} 次已按实时模板定位悬浮菜单启动按钮："
                f"{start_point}（score={start_score:.3f}，悬浮球={menu_icon_point}，"
                f"{menu_icon_state}）")
        else:
            # Keep a coordinate estimate only as a last resort.  The vertical
            # offset differs between the top and bottom positions because the
            # menu is slightly curved around the ball.
            image_for_menu = capture_image()
            width_for_menu = int(image_for_menu.shape[1]) if image_for_menu is not None else 0
            height_for_menu = int(image_for_menu.shape[0]) if image_for_menu is not None else 0
            default_menu_gap = 380 if menu_icon_state == "full" else 410
            gap_for_menu = int(step.get(
                "float_menu_primary_gap_full" if menu_icon_state == "full"
                else "float_menu_primary_gap", default_menu_gap))
            if width_for_menu and menu_icon_point[0] > width_for_menu // 2:
                start_x = int(menu_icon_point[0] - gap_for_menu)
            elif width_for_menu:
                start_x = int(menu_icon_point[0] + gap_for_menu)
            else:
                start_x = int(step.get("float_start_x", 293))
            bottom_menu = (height_for_menu > 0
                           and menu_icon_point[1] > height_for_menu * 0.55)
            vertical_bias = int(step.get(
                "float_menu_start_y_bias_bottom" if bottom_menu
                else "float_menu_start_y_bias_top", -24 if bottom_menu else -13))
            start_point = (start_x, int(menu_icon_point[1] + vertical_bias))
            ctx.log(
                f"第 {start_attempt} 次实时模板未定位到启动按钮，"
                f"使用修正后的估算坐标 {start_point}")
        # The local template just confirmed the expanded menu, so click its
        # exact centre immediately.  A second MAA process would only miss: by
        # the time it has loaded its resources the radial menu has collapsed.
        # ``maa_post_open_click`` remains available for slower devices.
        maa_clicked = False
        maa_click_message = ""
        started = None
        if step.get("maa_post_open_click", False):
            maa_clicked, maa_click_message = maa_naruto_click(
                "NarutoClickFloatStart")
        if maa_clicked:
            click_source = f"{maa_click_message}；MAA 已直接点击识别中心"
        else:
            started = tap(*start_point)
            if isinstance(started, Exception) or started.returncode != 0:
                ctx.log(f"第 {start_attempt} 次点击影分身“启动”失败，稍后重试：{started}")
                if ctx.stop_event.wait(retry_interval):
                    return Result(False, "任务被用户停止")
                continue
            click_source = f"本地实时模板点击 {start_point}"
            if maa_click_message:
                click_source += f"（未调用二次 MAA：{maa_click_message}）"
        ctx.log(
            f"第 {start_attempt} 次点击影分身“启动”："
            f"{'MAA 识别中心' if maa_clicked else start_point}；{click_source}")
        if ctx.stop_event.wait(after_click_wait):
            return Result(False, "任务被用户停止")
        blocker = handle_game_blocking_page()
        if not blocker.success:
            return blocker
        if blocker.details.get("clicked"):
            ctx.log(f"第 {start_attempt} 次启动后已处理游戏阻塞页；重新识别悬浮窗")
            continue
        # Mandatory Shadow Clone announcements are drawn outside the Android
        # accessibility tree and may appear only after the floating Start tap.
        # Check the visual card before spending a full stop-symbol timeout on
        # controls hidden behind that announcement.
        special = visible_special_result()
        if special is not None:
            return special
        start_metrics = capture_metrics()
        if (start_metrics and start_metrics.get("completion_popup")
                and not start_metrics.get("game_privacy_consent")
                and not start_metrics.get("shadow_update_choice_popup")):
            # Shadow Clone currently shows a one-button optional update notice
            # immediately after floating Start.  It is not the >3-version
            # forced-update condition and must not block stop-symbol checking.
            # Derive the centre from the live white panel because its height has
            # changed between releases.
            evidence = save_status_evidence("update")
            image_width = int(start_metrics.get("_width", 1280))
            image_height = int(start_metrics.get("_height", 720))
            bottom = (float(start_metrics.get(
                "popup_white_bottom_fraction", 0.0)) * image_height)
            confirm_y = (int(round(bottom - image_height * 0.11))
                         if bottom else int(round(image_height * 0.715)))
            confirm_y = max(int(image_height * 0.62),
                            min(int(image_height * 0.86), confirm_y))
            confirm_point = (image_width // 2, confirm_y)
            tap(*confirm_point)
            ctx.log(
                "启动后检测到影分身单按钮版本公告；该公告不属于超过 3 个版本的"
                f"强制更新，已保存截图并点击“确定”继续：{confirm_point}"
                f"{f'，截图：{evidence}' if evidence else ''}")
            if ctx.stop_event.wait(float(step.get(
                    "shadow_update_popup_close_wait", 3))):
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
        role_metrics = _naruto_role_name_metrics(
            image, step.get("lobby_reference_paths", []),
            threshold=float(step.get("lobby_role_name_threshold", 0.62)))
        metrics.update(role_metrics)
        return image, metrics

    def wait_poll():
        return ctx.stop_event.wait(poll_seconds)

    def is_login_method_page(image) -> bool:
        """Recognize Naruto's four-button login choice screen.

        The animated background changes, so only inspect the stable lower band:
        two green WeChat buttons and two blue QQ buttons.  This prevents a blind
        coordinate fallback on loading screens or in the lobby.
        """
        if image is None:
            return False
        try:
            import cv2
            import numpy as np
            height, width = image.shape[:2]
            if width < height:
                return False
            band = image[round(height * 0.68):round(height * 0.84), :]
            hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
            green = ((hsv[:, :, 0] >= 35) & (hsv[:, :, 0] <= 85) &
                     (hsv[:, :, 1] >= 100) & (hsv[:, :, 2] >= 80))
            blue = ((hsv[:, :, 0] >= 90) & (hsv[:, :, 0] <= 130) &
                    (hsv[:, :, 1] >= 100) & (hsv[:, :, 2] >= 80))
            return (np.count_nonzero(green) / max(1, green.size) >= 0.035 and
                    np.count_nonzero(blue) / max(1, blue.size) >= 0.035)
        except (ImportError, OSError):
            return False

    def locate_server_start_button(image) -> tuple[int, int] | None:
        """Find the post-login server selection ``开始游戏`` button.

        After Android QQ authorization the game does not enter the lobby
        directly: it shows a server row and a large cyan start button.  The
        previous stage-1 loop treated this as an unknown page and waited until
        timeout.  Unity exposes no useful accessibility text, so locate the
        stable cyan button by shape/color and click its centre.
        """
        if image is None:
            return None
        try:
            import cv2
            import numpy as np
            height, width = image.shape[:2]
            if width < height:
                return None
            hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv, np.array([82, 75, 90]),
                               np.array([115, 255, 255]))
            mask[:int(height * 0.55), :] = 0
            count, _, stats, centers = cv2.connectedComponentsWithStats(mask, 8)
            candidates = []
            for index in range(1, count):
                x = int(stats[index, cv2.CC_STAT_LEFT])
                y = int(stats[index, cv2.CC_STAT_TOP])
                w = int(stats[index, cv2.CC_STAT_WIDTH])
                h = int(stats[index, cv2.CC_STAT_HEIGHT])
                area = int(stats[index, cv2.CC_STAT_AREA])
                if not (180 <= w <= 520 and 45 <= h <= 150 and area >= 5000):
                    continue
                cx, cy = centers[index]
                # The server button is centred; exclude the right-side HUD.
                if not (width * 0.25 <= cx <= width * 0.75):
                    continue
                candidates.append((abs(cx - width / 2), -area,
                                   int(round(cx)), int(round(cy))))
            if not candidates:
                return None
            _, _, x, y = min(candidates)
            return x, y
        except (ImportError, OSError):
            return None

    def locate_network_disconnect_confirm(image) -> tuple[int, int] | None:
        """Locate the Unity ``网络异常断开，退出战斗`` orange confirm button."""
        if image is None:
            return None
        try:
            import cv2
            import numpy as np
            height, width = image.shape[:2]
            if width < height:
                return None
            hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
            orange = cv2.inRange(hsv, np.array([8, 120, 135]),
                                 np.array([35, 255, 255]))
            orange[:int(height * 0.48), :] = 0
            orange[int(height * 0.78):, :] = 0
            orange[:, :int(width * 0.25)] = 0
            orange[:, int(width * 0.75):] = 0
            count, _, stats, centers = cv2.connectedComponentsWithStats(orange, 8)
            candidates = []
            for index in range(1, count):
                x = int(stats[index, cv2.CC_STAT_LEFT])
                y = int(stats[index, cv2.CC_STAT_TOP])
                w = int(stats[index, cv2.CC_STAT_WIDTH])
                h = int(stats[index, cv2.CC_STAT_HEIGHT])
                area = int(stats[index, cv2.CC_STAT_AREA])
                cx, cy = centers[index]
                if not (130 <= w <= 320 and 35 <= h <= 110 and area >= 3500):
                    continue
                candidates.append((abs(cx - width / 2), -area,
                                   int(round(cx)), int(round(cy))))
            if not candidates:
                return None
            _, _, x, y = min(candidates)
            return x, y
        except (ImportError, OSError):
            return None

    def detect_shadow_auth_bind(image) -> bool:
        """Detect Shadow Clone's authorization-code-bound-to-other-device popup.

        This dialog is not dismissable by tapping 确定/继续.  Clicking 是 opens
        the unbind-link page and clicking 否 stops Shadow Clone; neither is safe
        for unattended automation.  Return true so the caller can stop with a
        precise, non-retryable error instead of looping on the white dialog.
        """
        if image is None:
            return False
        try:
            import cv2
            template_path = Path(expand(str(step.get(
                "shadow_auth_bind_template",
                ctx.root / "maa_naruto" / "resource" / "image"
                / "shadow_auth_bind.png"))))
            template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
            if template is None:
                return False
            if (image.shape[0] < template.shape[0]
                    or image.shape[1] < template.shape[1]):
                return False
            result = cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED)
            _, score, _, _ = cv2.minMaxLoc(result)
            return float(score) >= float(step.get(
                "shadow_auth_bind_threshold", 0.75))
        except (ImportError, OSError):
            return False

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
        shadow_update_prompt = bool(metrics.get("shadow_mandatory_update_popup"))
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
        if shadow_update_prompt:
            # Decline a non-forced Shadow Clone update.  Selecting the left
            # “是” button starts the updater and interrupts today's run.
            point = tuple(oriented_value(
                "shadow_update_confirm_point", [760, 500]))
        elif not privacy_prompt and image is not None:
            # The one-button manual-update/announcement dialog has changed
            # height between releases.  Derive the button centre from the
            # detected white panel bottom instead of the old fixed y=515;
            # this prevents the tap landing in the dialog body.
            height, width = image.shape[:2]
            bottom = float(metrics.get("popup_white_bottom_fraction", 0.0)) * height
            dynamic_y = int(round(bottom - height * 0.11)) if bottom else int(height * 0.78)
            dynamic_y = max(int(height * 0.68), min(int(height * 0.88), dynamic_y))
            point = (width // 2, dynamic_y)
        else:
            point = tuple(oriented_value(
                "game_consent_point" if privacy_prompt else "popup_confirm_point",
                [817, 600] if privacy_prompt else [640, 515]))
        tap(*point)
        prompt_name = ("游戏隐私协议" if privacy_prompt else
                       "非强制更新提示" if shadow_update_prompt else
                       "更新/结束提示")
        button_name = ("同意" if privacy_prompt else
                       "否" if shadow_update_prompt else "确定/继续")
        ctx.log(f"阶段1检测到遮挡大厅的{prompt_name}，已保存截图并点击“{button_name}”：{point}")
        # Do not blindly assume the fixed-coordinate tap worked.  The Naruto
        # completion dialog has changed height between Shadow Clone releases;
        # an older landscape coordinate landed just below the button and kept
        # this loop alive until the 30-minute lobby timeout.  Recheck promptly
        # and use the stable button centre as a recovery tap when necessary.
        if (not privacy_prompt and not shadow_update_prompt
                and not ctx.stop_event.wait(1.5)):
            check_image = capture_image()
            check_metrics = (_naruto_visual_metrics(check_image)
                             if check_image is not None else {})
            if check_metrics.get("completion_popup"):
                height, width = check_image.shape[:2]
                bottom = (float(check_metrics.get(
                    "popup_white_bottom_fraction", 0.0)) * height)
                if width >= height:
                    recovery_y = int(round(bottom - height * 0.11)) if bottom else 515
                    recovery_y = max(int(height * 0.68),
                                     min(int(height * 0.88), recovery_y))
                    recovery_point = (width // 2, recovery_y)
                else:
                    recovery_point = (width // 2, 935)
                tap(*recovery_point)
                ctx.log(
                    "阶段1确认弹窗首次点击后仍存在，已按当前横竖屏的按钮中心"
                    f"再次点击：{recovery_point}")
        return True

    # Stage 1: establish that the script has really reached the Naruto lobby.
    stage_started = time.monotonic()
    lobby_at = None
    ctx.log(f"阶段1：每隔 {poll_seconds:g} 秒截图，等待识别火影忍者大厅")
    wrong_foreground_checks = 0
    consecutive_popup_dismissals = 0
    max_popup_dismissals = max(
        1, int(step.get("max_stage1_popup_dismissals", 3)))
    max_wrong_foreground_checks = max(
        2, int(step.get("stage1_wrong_foreground_checks", 2)))
    login_method_clicks = 0
    server_start_clicks = 0
    network_confirm_clicks = 0
    max_server_start_clicks = max(1, int(step.get("max_server_start_clicks", 3)))
    while time.monotonic() - stage_started <= initial_lobby_timeout:
        special = visible_special_result()
        if special is not None:
            return special
        blocker = handle_game_blocking_page()
        if not blocker.success:
            return blocker
        if blocker.details.get("clicked"):
            ctx.log("阶段1已处理游戏阻塞页，等待登录或大厅页面刷新")
            if ctx.stop_event.wait(float(step.get("stage1_login_page_wait", 8))):
                return Result(False, "任务被用户停止")
            continue
        image, metrics = capture_frame()
        black_screen = watchdog.poll()
        if black_screen is not None:
            return black_screen
        if detect_shadow_auth_bind(image):
            evidence = Path(expand(str(step.get(
                "shadow_auth_bind_evidence_path",
                ctx.root / "logs" / "naruto_shadow_auth_bind.png"))))
            evidence.parent.mkdir(parents=True, exist_ok=True)
            try:
                cv2.imwrite(str(evidence), image)
            except OSError:
                pass
            return Result(
                False,
                "影分身授权码已绑定其他设备，需要先使用解绑码解绑；"
                "本次不自动点击“是/否”，请处理后重试",
                {"evidence": str(evidence),
                 "retry_step": False})
        if dismiss_lobby_blocking_dialog(image, metrics):
            consecutive_popup_dismissals += 1
            if consecutive_popup_dismissals >= max_popup_dismissals:
                return Result(
                    False,
                    "阶段1同一遮挡弹窗连续点击后仍未消失，停止空循环并请求重试",
                    {"popup_dismissals": consecutive_popup_dismissals,
                     "max_popup_dismissals": max_popup_dismissals,
                     "retry_step": True})
            if ctx.stop_event.wait(float(step.get("popup_close_wait", 5))):
                return Result(False, "任务被用户停止")
            continue
        consecutive_popup_dismissals = 0
        network_point = locate_network_disconnect_confirm(image)
        if network_point is not None and network_confirm_clicks < int(
                step.get("max_network_disconnect_confirms", 3)):
            done = tap(*network_point)
            if isinstance(done, Exception) or done.returncode != 0:
                return Result(False, f"点击火影网络异常弹窗“确定”失败：{done}")
            network_confirm_clicks += 1
            ctx.log(
                "阶段1识别到火影游戏内“网络异常断开”提示，已按实时橙色按钮"
                f"点击“确定”：{network_point}（第 {network_confirm_clicks} 次）")
            if ctx.stop_event.wait(float(step.get(
                    "network_disconnect_confirm_wait", 8))):
                return Result(False, "任务被用户停止")
            continue
        server_point = locate_server_start_button(image)
        if server_point is not None and server_start_clicks < max_server_start_clicks:
            done = tap(*server_point)
            if isinstance(done, Exception) or done.returncode != 0:
                return Result(False, f"点击火影服务器页“开始游戏”失败：{done}")
            server_start_clicks += 1
            ctx.log(f"阶段1识别到登录后的服务器选择页，已点击“开始游戏”：{server_point}（第 {server_start_clicks} 次）")
            if ctx.stop_event.wait(float(step.get("server_start_wait", 10))):
                return Result(False, "任务被用户停止")
            continue
        if (login_method_clicks < int(step.get("max_login_method_clicks", 2))
                and is_login_method_page(image)):
            height, width = image.shape[:2]
            agreement_point = (round(width * 0.082), round(height * 0.857))
            qq_android_point = (round(width * 0.836), round(height * 0.753))
            tap(*agreement_point)
            if ctx.stop_event.wait(1.5):
                return Result(False, "任务被用户停止")
            tap(*qq_android_point)
            login_method_clicks += 1
            ctx.log("阶段1识别到火影四按钮登录页，已勾选协议并选择安卓 QQ 登录："
                    f"{agreement_point} -> {qq_android_point}")
            if ctx.stop_event.wait(float(step.get("stage1_login_page_wait", 8))):
                return Result(False, "任务被用户停止")
            continue
        if metrics and metrics["home_page"]:
            if step.get("lobby_role_name_check", True) and not metrics.get(
                    "role_name_match", False):
                # A valid-looking lobby on another account is unsafe.  Return
                # a retryable result so the engine restarts LDPlayer/Shadow
                # Clone before checking the lobby again.
                mismatch_evidence = Path(expand(str(step.get(
                    "lobby_role_mismatch_evidence_path",
                    ctx.root / "logs" / "naruto_role_mismatch.png"))))
                mismatch_evidence.parent.mkdir(parents=True, exist_ok=True)
                if image is not None:
                    try:
                        cv2.imwrite(str(mismatch_evidence), image)
                    except OSError:
                        pass
                score = metrics.get("role_name_score", 0.0)
                ctx.log(
                    "阶段1检测到火影大厅，但左上角角色名不是“南部清和”"
                    f"（匹配度 {score:.3f}）；已保存截图并请求重启脚本")
                return Result(
                    False,
                    "火影大厅角色名校验失败：需要“南部清和”，已请求重启影分身和模拟器",
                    {"emulator_restart_requested": True,
                     "retry_step": True,
                     "role_name_expected": "南部清和",
                     "role_name_score": score,
                     "evidence": str(mismatch_evidence)})
            lobby_at = time.monotonic()
            ctx.log("阶段1完成：已识别火影忍者大厅且角色名确认是“南部清和”，"
                    f"开始 {timer_label} 计时")
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
            shadow_focused = focused_on(shadow_package)
            current_name = ("影分身" if shadow_focused else "其他页面或桌面")
            ctx.log(f"阶段1：火影未在前台，当前为{current_name}（连续 "
                    f"{wrong_foreground_checks}/{max_wrong_foreground_checks} 次）")
            if shadow_focused:
                continued = click_shadow_continue_if_visible(log_missing=False)
                if not continued.success:
                    return continued
                # The floating script is already confirmed running.  If its
                # own page still owns focus, explicitly open Naruto instead of
                # immediately restarting a healthy emulator/script pair.
                launch_game = command([
                    "shell", "monkey", "-p", game_package,
                    "-c", "android.intent.category.LAUNCHER", "1"])
                if (not isinstance(launch_game, Exception)
                        and launch_game.returncode == 0):
                    ctx.log("阶段1：影分身仍在前台，已主动打开火影忍者并继续观察")
                    if ctx.stop_event.wait(float(step.get(
                            "stage1_game_launch_wait", 12))):
                        return Result(False, "任务被用户停止")
                    continue
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
        special = visible_special_result()
        if special is not None:
            return special
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
                            y_ratio: float,
                            allow_coordinate_fallback: bool = True) -> tuple[bool, str]:
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
        pid_match = pid in pids
        if pid_match or title_match:
            rect = win32gui.GetWindowRect(hwnd)
            area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
            if area > 10000:
                # Prefer a window owned by the configured executable/process
                # tree over a title-only match.  A broad hint such as "MAA"
                # also matches MaaEnd, and previously caused the Arknights
                # starter to click that unrelated GUI when both were open.
                score = (100 if pid_match else 0) + (20 if title_match else 0)
                candidates.append((score, area, hwnd, title, rect))

    win32gui.EnumWindows(collect, None)
    if not candidates:
        related = len(pids)
        suffix = f"（已发现 {related} 个相关进程）" if related else "（尚无相关进程）"
        return False, "脚本尚未产生匹配的可见窗口" + suffix
    _, _, hwnd, title, rect = max(candidates)
    focused, focus_message = _focus_window_before_click(hwnd, title or "每日脚本")
    topmost_fallback = False
    if not focused:
        try:
            flags = (win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                     | win32con.SWP_SHOWWINDOW)
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
            topmost_fallback = True
            time.sleep(0.2)
        except Exception:
            return False, focus_message

    def release_topmost() -> None:
        if not topmost_fallback:
            return
        try:
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                | win32con.SWP_SHOWWINDOW)
        except Exception:
            pass

    rect = win32gui.GetWindowRect(hwnd)
    folded_names = [name.casefold() for name in button_names]
    try:
        from pywinauto import Application
        window = Application(backend="uia").connect(handle=hwnd, timeout=3).window(handle=hwnd)
        for control in window.descendants(control_type="Button"):
            name = control.window_text().strip()
            if name and any(target == name.casefold() or target in name.casefold()
                            for target in folded_names):
                if not topmost_fallback:
                    focused, focus_message = _focus_window_before_click(
                        hwnd, title or "每日脚本", timeout=3.0)
                    if not focused:
                        return False, focus_message
                control.click_input()
                release_topmost()
                return True, f"已通过 GUI 控件点击“{name}”"
    except Exception:
        pass
    if not allow_coordinate_fallback:
        release_topmost()
        names = "、".join(f"“{name}”" for name in button_names)
        return False, f"窗口中未发现可见按钮：{names}；未执行坐标备用点击"
    if not topmost_fallback:
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
        release_topmost()
        focus_note = ("（Windows 拒绝焦点，已临时置顶后点击）"
                      if topmost_fallback else "")
        return True, (f"已在“{title}”窗口点击启动位置（{x}, {y}）"
                      f"{focus_note}")
    except Exception as exc:
        release_topmost()
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


def _probe_gui_emulator_adb(step: dict[str, Any]) -> tuple[bool, str]:
    """Probe the emulator endpoint used by a desktop assistant GUI."""
    adb_value = str(step.get("adb_executable", "")).strip()
    device = str(step.get("adb_device", step.get("device", ""))).strip()
    if not adb_value or not device:
        return False, "未配置 QQ 阅读 GUI 的 adb_executable 或 adb_device"
    adb = expand(adb_value)
    if not Path(adb).exists():
        return False, f"找不到 QQ 阅读 GUI 使用的 ADB：{adb}"
    env = os.environ.copy()
    if step.get("adb_server_port") is not None:
        env["ANDROID_ADB_SERVER_PORT"] = str(step["adb_server_port"])
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        connect = subprocess.run(
            [adb, "connect", device], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
            env=env, creationflags=flags)
        probe = subprocess.run(
            [adb, "-s", device, "shell", "getprop", "sys.boot_completed"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, env=env, creationflags=flags)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"ADB 连接检查异常：{exc}"
    output = " ".join(filter(None, (connect.stdout, connect.stderr,
                                    probe.stdout, probe.stderr))).strip()
    if probe.returncode == 0 and probe.stdout.strip().endswith("1"):
        return True, f"ADB 已连接且 Android 已就绪（{device}）"
    return False, output or f"ADB 未就绪（退出码 {probe.returncode}）"


def _capture_gui_emulator_screen(step: dict[str, Any], path: Path) -> tuple[bool, bool, str]:
    """Capture the GUI's emulator frame and report whether it is effectively black."""
    adb_value = str(step.get("adb_executable", "")).strip()
    device = str(step.get("adb_device", step.get("device", ""))).strip()
    if not adb_value or not device:
        return False, False, "未配置 ADB 路径或设备地址"
    adb = expand(adb_value)
    env = os.environ.copy()
    if step.get("adb_server_port") is not None:
        env["ANDROID_ADB_SERVER_PORT"] = str(step["adb_server_port"])
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        shot = subprocess.run(
            [adb, "-s", device, "exec-out", "screencap", "-p"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
            env=env, creationflags=flags)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, False, f"截图异常：{exc}"
    data = shot.stdout
    if shot.returncode != 0 or len(data) < 100 or not data.startswith(b"\x89PNG"):
        detail = shot.stderr.decode("utf-8", errors="replace").strip()
        return False, False, detail or "ADB 未返回有效 PNG 截图"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as exc:
        return False, False, f"保存模拟器截图失败：{exc}"
    black = False
    try:
        from PIL import Image, ImageStat
        with Image.open(path) as image:
            rgb = image.convert("RGB")
            mean = sum(ImageStat.Stat(rgb).mean) / 3.0
            black = mean < float(step.get("emulator_black_mean", 8.0))
    except Exception:
        # PNG validity is still useful when Pillow is unavailable.
        black = False
    return True, black, f"模拟器截图已更新：{path}"


def _auto_connect_gui_emulator(step: dict[str, Any], proc: subprocess.Popen,
                               process_images: list[str], title_hints: list[str],
                               ctx: RunContext) -> tuple[bool, str]:
    """Click a GUI connection button and verify its configured ADB endpoint."""
    if not step.get("auto_connect_emulator", False):
        return True, "未启用 GUI 自动连接"
    names = [str(value) for value in step.get(
        "connect_button_names", ["连接模拟器", "检查连接", "连接", "Connect"])
             if str(value).strip()]
    if not names:
        return False, "已启用 GUI 自动连接，但未配置连接按钮名称"
    timeout = max(5.0, float(step.get("connect_timeout", 45)))
    retry = max(0.5, float(step.get("connect_retry_interval", 2)))
    settle = max(0.0, float(step.get("connect_settle_seconds", 1)))
    deadline = time.monotonic() + timeout
    last_message = "尚未找到连接按钮"
    click_count = 0
    while time.monotonic() < deadline:
        if ctx.stop_event.is_set():
            return False, "任务已被用户停止"
        adb_ok, adb_message = _probe_gui_emulator_adb(step)
        if not adb_ok:
            last_message = adb_message
            if ctx.stop_event.wait(min(retry, max(0.1, deadline - time.monotonic()))):
                return False, "任务已被用户停止"
            continue
        clicked, click_message = _click_named_gui_button(
            proc.pid, process_images, title_hints, names,
            float(step.get("connect_button_x_ratio", 0.5)),
            float(step.get("connect_button_y_ratio", 0.16)),
            allow_coordinate_fallback=bool(
                step.get("connect_button_coordinate_fallback", False)))
        click_count += 1
        last_message = click_message
        ctx.log(f"{step.get('display_name', '脚本')}自动连接模拟器第 {click_count} 次：{click_message}")
        if clicked:
            if ctx.stop_event.wait(settle):
                return False, "任务已被用户停止"
            verified, verify_message = _probe_gui_emulator_adb(step)
            if verified:
                return True, f"已点击连接按钮并确认模拟器可用：{verify_message}"
            last_message = verify_message
        if ctx.stop_event.wait(min(retry, max(0.1, deadline - time.monotonic()))):
            return False, "任务已被用户停止"
    return False, f"{timeout:g} 秒内未能完成 GUI 模拟器连接：{last_message}"


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
    launch_args = [str(value) for value in step.get("launch_args", [])]
    launch_env = os.environ.copy()
    if step.get("adb_server_port") is not None:
        launch_env["ANDROID_ADB_SERVER_PORT"] = str(step["adb_server_port"])
    try:
        proc = subprocess.Popen([str(exe), *launch_args], cwd=str(exe.parent),
                                env=launch_env,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError as exc:
        return Result(False, f"无法启动 {display_name}：{exc}")

    update_wait = max(0.0, float(step.pop("startup_update_wait", 0)))
    timeout = float(step.get("timeout", 10800))
    retry_interval = max(0.05, float(step.get("log_retry_interval", 30)))
    start_markers = [str(x) for x in step.get("start_markers", [])]
    completion_markers = [str(x) for x in step.get("completion_markers", [])]
    error_markers = [str(x) for x in step.get("error_markers", [])]
    update_markers = [str(x) for x in step.get("update_markers", [])]
    maintenance_markers = [str(x) for x in step.get("maintenance_markers", [])]
    dismiss_update_notice = bool(step.get("dismiss_update_notice", False))
    update_ack_button_names = [
        str(x) for x in step.get("update_ack_button_names", ["好的"])
    ]
    update_ack_x_ratio = float(step.get("update_ack_x_ratio", 0.366))
    update_ack_y_ratio = float(step.get("update_ack_y_ratio", 0.912))
    update_ack_settle_seconds = max(
        0.0, float(step.get("update_ack_settle_seconds", 2)))
    update_ack_retry_interval = max(
        0.05, float(step.get("update_ack_retry_interval", retry_interval)))
    max_update_ack_clicks = max(1, int(step.get("max_update_ack_clicks", 5)))
    startup_ack_button_names = [
        str(x) for x in step.get("startup_ack_button_names", [])
    ]
    startup_ack_x_ratio = float(step.get("startup_ack_x_ratio", 0.5))
    startup_ack_y_ratio = float(step.get("startup_ack_y_ratio", 0.5))
    startup_ack_after = max(
        0.0, float(step.get("startup_ack_after", 5)))
    startup_ack_settle_seconds = max(
        0.0, float(step.get("startup_ack_settle_seconds", 3)))
    startup_ack_retry_interval = max(
        0.05, float(step.get("startup_ack_retry_interval", retry_interval)))
    max_startup_ack_clicks = max(
        0, int(step.get("max_startup_ack_clicks", 1)))
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
    update_notice_marker = None
    update_ack_clicks = 0
    next_update_ack_at = None
    startup_ack_clicks = 0
    next_startup_ack_at = (
        started_at + startup_ack_after
        if startup_ack_button_names and max_startup_ack_clicks > 0 else None
    )
    screenshot_saved = False
    screenshot_evidence_seen = False
    game_foregrounded = False
    game_foreground_attempted = False
    worker_seen = False
    worker_exit_at = None
    failed_task_seen = False
    failed_task_logs: set[str] = set()
    quiet_seconds = float(step.get("completion_quiet_seconds", 3))
    state_pattern_text = str(step.get("state_watchdog_regex", "")).strip()
    state_pattern = re.compile(state_pattern_text) if state_pattern_text else None
    update_failure_state_text = str(
        step.get("update_failure_state_regex", "")).strip()
    update_failure_state_pattern = (
        re.compile(update_failure_state_text)
        if update_failure_state_text else None
    )
    update_failure_markers = [
        str(value) for value in step.get("update_failure_markers", [])
    ]
    update_failure_state_seen = False
    update_failure_state_key = None
    update_failure_marker_seen = None
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
    startup_timeout = max(
        retry_interval, float(step.get("startup_timeout", 600)))
    post_start_log_stall_seconds = max(
        0.0, float(step.get("post_start_log_stall_seconds", 900)))
    post_start_process_exit_grace = max(
        0.0, float(step.get("post_start_process_exit_grace", 30)))
    last_log_activity_at = started_at
    post_start_process_exit_at = None
    emulator_monitor_enabled = bool(step.get("monitor_emulator", False))
    emulator_monitor_interval = max(
        5.0, float(step.get("emulator_monitor_interval", 30)))
    monitor_default_name = re.sub(r"[^0-9A-Za-z_.-]+", "_", display_name).strip("_")
    emulator_monitor_path = Path(expand(str(step.get(
        "emulator_monitor_path",
        ctx.root / "logs" / f"{monitor_default_name}_emulator.png"))))
    next_emulator_monitor_at = started_at
    emulator_black_streak = 0
    emulator_black_limit = max(1, int(step.get("emulator_black_streak", 3)))
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
        if step.get("auto_connect_emulator", False):
            connected, connection_message = _auto_connect_gui_emulator(
                step, proc, process_images, title_hints, ctx)
            if not connected:
                return failure(f"{display_name} 启动前连接模拟器失败：{connection_message}",
                               connection_error=connection_message)
            ctx.log(f"{display_name} 启动前连接模拟器完成：{connection_message}")
        if update_wait:
            ctx.log(
                f"{display_name}检测到昨日更新标志，已启动脚本并等待 "
                f"{update_wait:g} 秒完成自动更新")
            deadline = time.monotonic() + update_wait
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if ctx.stop_event.wait(min(1.0, remaining)):
                    return failure("自动更新等待期间任务被用户停止")
            step["_startup_update_wait_consumed"] = True
            existing = {}
            for name in glob.glob(log_glob):
                try:
                    existing[name] = Path(name).stat().st_size
                except OSError:
                    continue
            started_at = time.monotonic()
            last_log_activity_at = started_at
            next_click_at = started_at + max(
                0.0, float(step.get("initial_click_delay", retry_interval)))
            next_startup_ack_at = (
                started_at + startup_ack_after
                if startup_ack_button_names and max_startup_ack_clicks > 0 else None
            )
            launcher_exit_at = None
            ctx.log(f"{display_name}自动更新等待结束，重新开始启动检测")
        while time.monotonic() - started_at <= timeout:
            if ctx.stop_event.wait(min(1.0, retry_interval)):
                return failure("任务被用户停止")
            now = time.monotonic()
            if (emulator_monitor_enabled and now >= next_emulator_monitor_at):
                captured, black, monitor_message = _capture_gui_emulator_screen(
                    step, emulator_monitor_path)
                if captured:
                    emulator_black_streak = emulator_black_streak + 1 if black else 0
                    ctx.log(f"{display_name}模拟器监视：{monitor_message}；"
                            f"画面状态={'黑屏' if black else '正常'}")
                    if emulator_black_streak >= emulator_black_limit:
                        ctx.log(f"{display_name}连续 {emulator_black_streak} 次检测到黑屏，"
                                "重新确认 ADB 与脚本连接")
                        if step.get("auto_connect_emulator", False):
                            connected, reconnect_message = _auto_connect_gui_emulator(
                                step, proc, process_images, title_hints, ctx)
                            ctx.log(f"{display_name}黑屏恢复：{reconnect_message}")
                            if connected:
                                emulator_black_streak = 0
                else:
                    # QQ Reader's ReaderPageActivity can set FLAG_SECURE.  In
                    # that state ADB returns a successful command with an
                    # empty frame; it is a protected page, not a disconnected
                    # emulator, so do not repeatedly click the GUI button.
                    adb_ok, adb_message = _probe_gui_emulator_adb(step)
                    if adb_ok:
                        ctx.log(f"{display_name}模拟器监视：{monitor_message}；"
                                f"ADB 正常（{adb_message}），暂不重连")
                    else:
                        ctx.log(f"{display_name}模拟器监视：{monitor_message}；尝试重新连接")
                    if not adb_ok and step.get("auto_connect_emulator", False):
                        connected, reconnect_message = _auto_connect_gui_emulator(
                            step, proc, process_images, title_hints, ctx)
                        ctx.log(f"{display_name}断线恢复：{reconnect_message}")
                next_emulator_monitor_at = now + emulator_monitor_interval
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
                # Some desktop assistants truncate-and-rewrite their log
                # instead of appending.  If the delta starts in the middle of
                # a marker (for example ``RUN START`` becomes ``START``),
                # rescan the complete file once so startup/completion markers
                # are not missed.
                marker_candidates = [
                    *start_markers, *completion_markers, *error_markers,
                    *update_markers, *maintenance_markers,
                ]
                start_seen_in_delta = any(marker and marker in content
                                          for marker in start_markers)
                needs_full_rescan = (
                    position > 0 and marker_candidates
                    and ((start_markers and not start_seen_in_delta)
                         or (not start_markers and not any(
                             marker and marker in content
                             for marker in marker_candidates))))
                if needs_full_rescan:
                    try:
                        whole = path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        whole = ""
                    if ((start_markers and any(marker and marker in whole
                                               for marker in start_markers))
                            or (not start_markers and any(
                                marker and marker in whole
                                for marker in marker_candidates))):
                        content = whole
                existing[name] = size
                last_log_activity_at = time.monotonic()
                failed_task_marker = str(step.get("failed_task_marker", ""))
                if failed_task_marker and failed_task_marker in content:
                    failed_task_seen = True
                    failed_task_logs.add(name)
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
                        if (update_failure_state_pattern is not None
                                and update_failure_state_pattern.search(state_key)):
                            update_failure_state_seen = True
                            update_failure_state_key = state_key
                        if state_key != last_state_key:
                            last_state_key = state_key
                            last_state_change_at = time.monotonic()
                            state_recovery_at = None
                            targeted_recovery_count = 0
                            targeted_recovery_next_at = (last_state_change_at
                                                         + targeted_after_seconds)
                            if step.get("publish_state_updates", True):
                                ctx.log(f"{display_name}当前状态：{state_key}")
                if update_failure_state_seen and update_failure_markers:
                    matched_update_failure = next(
                        (value for value in update_failure_markers if value in content),
                        None)
                    if matched_update_failure:
                        update_failure_marker_seen = matched_update_failure
                marker = _matching_marker(content, update_markers)
                if marker:
                    capture_reward(force=True)
                    return _update_skip_result(
                        display_name, step, marker,
                        {"log": name, "start_clicks": click_count,
                         "screenshot": (screenshot_path
                                        if screenshot_saved else None)})
                maintenance_marker = _matching_marker(content, maintenance_markers)
                if maintenance_marker:
                    capture_reward(force=True)
                    return _maintenance_skip_result(
                        display_name, step, maintenance_marker,
                        {"log": name, "start_clicks": click_count,
                         "screenshot": (screenshot_path
                                        if screenshot_saved else None)})
                for marker in error_markers:
                    if run_started and marker in content:
                        if (update_failure_state_seen
                                and update_failure_marker_seen is not None):
                            capture_reward(force=True)
                            return _update_skip_result(
                                display_name, step, update_failure_marker_seen,
                                {"log": name, "state": update_failure_state_key,
                                 "start_clicks": click_count,
                                 "screenshot": (screenshot_path
                                                if screenshot_saved else None)},
                                reason_code="game_update_required")
                        return failure(f"{display_name}异常结束：{marker}",
                                       log=name, marker=marker)
                for marker in completion_markers:
                    if run_started and marker in content:
                        completion_at = time.monotonic()
                        completion_log = name
                        ctx.log(f"{display_name}检测到完成日志，观察 {quiet_seconds:g} 秒")
            if completion_at is not None and time.monotonic() - completion_at >= quiet_seconds:
                failed_task_marker = str(step.get("failed_task_marker", ""))
                if (failed_task_marker and completion_log == "worker_process_exit"
                        and failed_task_seen):
                    return failure(
                        f"{display_name}完整清单已结束，但存在失败子任务："
                        f"{failed_task_marker}",
                        failed_logs=sorted(failed_task_logs))
                if not screenshot_saved and step.get("screenshot_at_completion", True):
                    capture_reward()
                return Result(True, f"{display_name}正常结束",
                              {"log": completion_log, "start_clicks": click_count,
                               "update_ack_clicks": update_ack_clicks,
                               "startup_ack_clicks": startup_ack_clicks,
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
            if (update_notice_marker is not None and not run_started
                    and next_update_ack_at is not None
                    and time.monotonic() >= next_update_ack_at):
                if update_ack_clicks >= max_update_ack_clicks:
                    capture_reward(force=True)
                    return _update_skip_result(
                        display_name, step, update_notice_marker,
                        {"update_ack_clicks": update_ack_clicks,
                         "start_clicks": click_count,
                         "screenshot": (screenshot_path
                                        if screenshot_saved else None)})
                update_ack_clicks += 1
                clicked, message = _click_named_gui_button(
                    proc.pid, process_images, title_hints,
                    update_ack_button_names,
                    update_ack_x_ratio, update_ack_y_ratio)
                ctx.log(f"{display_name}第 {update_ack_clicks} 次跳过更新公告：{message}")
                now = time.monotonic()
                next_update_ack_at = now + update_ack_retry_interval
                if clicked:
                    next_click_at = now + update_ack_settle_seconds
                else:
                    next_click_at = now + min(update_ack_retry_interval, 3)
                continue
            if (not run_started and next_startup_ack_at is not None
                    and time.monotonic() >= next_startup_ack_at
                    and startup_ack_clicks < max_startup_ack_clicks):
                startup_ack_clicks += 1
                clicked, message = _click_named_gui_button(
                    proc.pid, process_images, title_hints,
                    startup_ack_button_names,
                    startup_ack_x_ratio, startup_ack_y_ratio)
                ctx.log(
                    f"{display_name}第 {startup_ack_clicks} 次处理启动前弹窗：{message}")
                now = time.monotonic()
                next_startup_ack_at = now + startup_ack_retry_interval
                if clicked:
                    next_click_at = max(
                        next_click_at, now + startup_ack_settle_seconds)
                    continue
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
            now = time.monotonic()
            if not run_started and now - started_at >= startup_timeout:
                return failure(
                    f"{display_name}在 {startup_timeout:g} 秒内未进入任务状态",
                    start_clicks=click_count,
                    startup_timeout=startup_timeout)
            if (run_started and completion_at is None
                    and post_start_log_stall_seconds > 0
                    and now - last_log_activity_at >= post_start_log_stall_seconds):
                return failure(
                    f"{display_name}进入任务后日志已 "
                    f"{now - last_log_activity_at:g} 秒无更新，判定脚本停滞",
                    log_stalled=True,
                    log_stall_seconds=now - last_log_activity_at)
            if run_started and completion_at is None:
                related_running = (
                    _process_image_exists([*process_images, *worker_images])
                    or _matching_visible_window_exists(title_hints))
                if related_running:
                    post_start_process_exit_at = None
                elif post_start_process_exit_at is None:
                    post_start_process_exit_at = now
                    ctx.log(
                        f"{display_name}任务进程和 GUI 已消失，等待 "
                        f"{post_start_process_exit_grace:g} 秒确认完成日志")
                elif (now - post_start_process_exit_at
                      >= post_start_process_exit_grace
                      and now - last_log_activity_at
                      >= post_start_process_exit_grace):
                    return failure(
                        f"{display_name}任务开始后进程和 GUI 均已退出，"
                        "但未产生完成日志",
                        process_exit_after_start=True,
                        process_exit_grace=post_start_process_exit_grace)
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


def run_qq_reader_trial(step: dict[str, Any], ctx: RunContext) -> Result:
    """Run the neighbouring QQ Reader GUI with a bounded one-shot test plan.

    The QQ Reader GUI persists its task selection.  A user's normal profile may
    contain many repetitions, so the GameFlow test must never launch that profile
    by accident.  Install the configured one-shot plan before the GUI starts and
    restore the original file regardless of success, cancellation, or timeout.
    """
    settings_path = Path(expand(str(step.get(
        "gui_settings_path", r"G:\project_X\dev\config\maa_gui_config.json"))))
    trial_tasks = step.get("trial_tasks", [])
    if not settings_path.exists():
        return Result(False, f"找不到 QQ 阅读 GUI 配置：{settings_path}")
    if not isinstance(trial_tasks, list) or not trial_tasks:
        return Result(False, "QQ 阅读测试未配置一次性任务清单")

    original = settings_path.read_bytes()
    cli_config_path = Path(expand(str(step.get(
        "cli_config_path", r"G:\project_X\dev\config\maa_pi_config.json"))))
    cli_original = cli_config_path.read_bytes() if cli_config_path.exists() else None
    temporary = settings_path.with_suffix(settings_path.suffix + ".gameflow.tmp")
    try:
        temporary.write_text(
            json.dumps({"tasks": trial_tasks}, ensure_ascii=False, indent=4) + "\n",
            encoding="utf-8")
        os.replace(temporary, settings_path)
        ctx.log("QQ 阅读已切换为本轮执行配置；原 GUI 配置将在结束后恢复")
        return run_log_gui_daily(step, ctx)
    except (OSError, TypeError, ValueError) as exc:
        return Result(False, f"准备 QQ 阅读一次性测试配置失败：{exc}")
    finally:
        try:
            restore = settings_path.with_suffix(settings_path.suffix + ".gameflow.restore")
            restore.write_bytes(original)
            os.replace(restore, settings_path)
            temporary.unlink(missing_ok=True)
            if cli_original is not None:
                cli_restore = cli_config_path.with_suffix(
                    cli_config_path.suffix + ".gameflow.restore")
                cli_restore.write_bytes(cli_original)
                os.replace(cli_restore, cli_config_path)
            ctx.log("QQ 阅读原 GUI 配置已恢复")
        except OSError as exc:
            ctx.log(f"恢复 QQ 阅读 GUI 配置失败，请手动检查：{exc}")


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

    update_wait = max(0.0, float(step.pop("startup_update_wait", 0)))
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
    update_markers = [str(x) for x in step.get("update_markers", [])]
    script_update_markers = [str(x) for x in step.get(
        "script_update_markers", ["发现新版本:", "开始下载更新:"])]
    maintenance_markers = [str(x) for x in step.get("maintenance_markers", [])]
    fallback_after = float(step.get("gui_fallback_after", 45))
    fallback_retry_seconds = max(
        1.0, float(step.get("gui_fallback_retry_seconds", 30)))
    max_start_clicks = max(1, int(step.get("max_start_clicks", 5)))
    startup_timeout = max(
        fallback_after, float(step.get("startup_timeout", 600)))
    started_at = time.monotonic()
    run_started = False
    script_update_seen = False
    next_script_update_click_at = 0.0
    start_click_count = 0
    next_fallback_at = started_at + fallback_after
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
        if update_wait:
            ctx.log(
                f"终末地检测到昨日更新标志，MaaEnd 启动后先等待 "
                f"{update_wait:g} 秒完成自动更新")
            deadline = time.monotonic() + update_wait
            while time.monotonic() < deadline:
                if ctx.stop_event.wait(min(1.0, deadline - time.monotonic())):
                    return Result(False, "自动更新等待期间任务被用户停止")
            step["_startup_update_wait_consumed"] = True
            existing = {
                name: Path(name).stat().st_size
                for name in glob.glob(log_glob)
                if Path(name).exists()
            }
            if framework_log is not None and framework_log.exists():
                framework_position = framework_log.stat().st_size
            started_at = time.monotonic()
            next_fallback_at = started_at + fallback_after
            ctx.log("MaaEnd 自动更新等待结束，重新开始本轮启动检测")
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
                script_update_marker = _matching_marker(content, script_update_markers)
                if script_update_marker and not script_update_seen:
                    if step.get("skip_on_script_update", False):
                        capture_endfield(force=True)
                        return _update_skip_result(
                            "终末地", step, f"MaaEnd 脚本更新：{script_update_marker}",
                            {"log": name,
                             "screenshot": screenshot_path if screenshot_saved else None,
                             "reason_code": "script_update"},
                            reason_code="script_update")
                    if step.get("ignore_script_updates", False):
                        ctx.log(
                            f"检测到 MaaEnd 脚本更新（{script_update_marker}）；"
                            "已按配置忽略脚本更新提示，继续等待每日任务")
                        continue
                    script_update_seen = True
                    next_script_update_click_at = time.monotonic()
                    ctx.log(
                        f"检测到 MaaEnd 脚本更新（{script_update_marker}）；"
                        "等待下载、安装和重启，不暂停终末地每日流程")
                update_marker = _matching_marker(content, update_markers)
                if update_marker:
                    capture_endfield(force=True)
                    return _update_skip_result(
                        "终末地", step, update_marker,
                        {"log": name,
                         "screenshot": screenshot_path if screenshot_saved else None})
                maintenance_marker = _matching_marker(content, maintenance_markers)
                if maintenance_marker:
                    capture_endfield(force=True)
                    return _maintenance_skip_result(
                        "终末地", step, maintenance_marker,
                        {"log": name,
                         "screenshot": screenshot_path if screenshot_saved else None})
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
            if script_update_seen and now >= next_script_update_click_at:
                clicked, message = _click_named_gui_button(
                    proc.pid, [process_image], ["MaaEnd"],
                    ["立即安装", "安装更新", "安装并重启"],
                    float(step.get("script_update_x_ratio", 0.82)),
                    float(step.get("script_update_y_ratio", 0.92)),
                    allow_coordinate_fallback=False)
                ctx.log(f"MaaEnd 更新安装确认：{message}")
                next_script_update_click_at = now + float(
                    step.get("script_update_click_interval", 30))
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
            if not run_started and time.monotonic() - started_at >= startup_timeout:
                capture_endfield(force=True)
                return Result(
                    False,
                    f"MaaEnd 在 {startup_timeout:g} 秒内未启动“全套日常”",
                    {"start_clicks": start_click_count,
                     "startup_timeout": startup_timeout,
                     "screenshot": screenshot_path if screenshot_saved else None})
            if (step.get("gui_click_fallback", True) and not run_started
                    and not script_update_seen
                    and time.monotonic() >= next_fallback_at):
                if start_click_count >= max_start_clicks:
                    capture_endfield(force=True)
                    return Result(
                        False,
                        f"MaaEnd 已达到 {max_start_clicks} 次启动点击上限，"
                        "仍未产生任务开始日志",
                        {"start_clicks": start_click_count,
                         "screenshot": screenshot_path if screenshot_saved else None})
                start_click_count += 1
                clicked, message = _click_maaend_start_button(
                    proc.pid, float(step.get("gui_click_x_ratio", 0.682)),
                    float(step.get("gui_click_y_ratio", 0.971)))
                ctx.log(
                    f"MaaEnd 第 {start_click_count} 次点击“开始任务”：{message}")
                next_fallback_at = time.monotonic() + fallback_retry_seconds
                if not clicked:
                    ctx.log(
                        f"MaaEnd GUI 尚未就绪，{fallback_retry_seconds:g} 秒后重试")
            if proc.poll() is not None and not _process_image_exists([process_image]):
                capture_endfield(force=True)
                if script_update_seen:
                    return Result(
                        False,
                        "MaaEnd 更新安装后已退出，准备重新启动并继续每日流程",
                        {"retry_step": True,
                         "retry_delay_override": float(
                             step.get("script_update_restart_delay", 15))})
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

    update_wait = max(0.0, float(step.pop("startup_update_wait", 0)))
    timeout = int(step.get("timeout", 14400))
    initial_delay = float(step.get("initial_click_delay", 120))
    round_start_timeout = float(step.get("round_start_timeout", 180))
    between_round_delay = float(step.get("between_round_delay", 3))
    start_marker = str(step.get("start_marker", "用户操作：启动任务"))
    completion_marker = str(step.get("completion_marker", "任务已全部完成！"))
    update_markers = [str(x) for x in step.get("update_markers", [])]
    maintenance_markers = [str(x) for x in step.get("maintenance_markers", [])]
    max_rounds = max(1, int(step.get("max_rounds", 2)))
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
        if update_wait:
            ctx.log(
                f"不思议迷宫检测到昨日更新标志，脚本启动后先等待 "
                f"{update_wait:g} 秒完成自动更新")
            waited = _wait_with_emulator_watchdog(update_wait, watchdog, ctx)
            if waited is not None:
                return waited
            step["_startup_update_wait_consumed"] = True
            existing = {
                name: Path(name).stat().st_size
                for name in glob.glob(log_glob)
                if Path(name).exists()
            }
            started_at = time.monotonic()
            last_business_at = started_at
            ctx.log("不思议迷宫自动更新等待结束，重新开始本轮日志检测")
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
                update_marker = _matching_marker(content, update_markers)
                if update_marker:
                    return _update_skip_result(
                        "不思议迷宫", step, update_marker, {"log": name})
                maintenance_marker = _matching_marker(content, maintenance_markers)
                if maintenance_marker:
                    return _maintenance_skip_result(
                        "不思议迷宫", step, maintenance_marker, {"log": name})
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
                        if round_number >= max_rounds:
                            return Result(True, f"不思议迷宫 {max_rounds} 轮任务均已完成",
                                          {"rounds": max_rounds, "logs": completion_logs,
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
        return Result(
            False,
            f"等待不思议迷宫 {max_rounds} 轮任务完成超时（{timeout} 秒）")
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
        # MuMu 12 occasionally keeps reporting is_android_started=false even
        # after Android and its dedicated ADB endpoint are fully operational.
        # Treat boot_completed=1 from that endpoint as authoritative instead of
        # waiting for a stale manager flag until the whole step times out.
        if not adb_executable.exists():
            return Result(False, f"找不到 MuMu ADB：{adb_executable}")
        if (info and info.get("is_android_started")) or device:
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
                               "boot_completed": True,
                               "manager_confirmed": bool(
                                   info and info.get("is_android_started"))})
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
           "log_gui_daily": run_log_gui_daily, "qq_reader_trial": run_qq_reader_trial,
           "window_screenshot": run_window_screenshot,
           "ldplayer": run_ldplayer, "mumu_wait": run_mumu_wait,
           "adb": run_adb, "delay": run_delay}
