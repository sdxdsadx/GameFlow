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
    "æœ‰æ›´æ–°=true",
    '"has_update":true',
    '"is_compatible":false',
    "éœ€è¦æ›´æ–°åç»§ç»­",
    "è¯·æ›´æ–°å®¢æˆ·ç«¯",
    "å®¢æˆ·ç«¯ç‰ˆæœ¬è¿‡ä½",
    "èµ„æºç‰ˆæœ¬è¿‡ä½",
    "new version available",
    "update required",
)


def _find_update_marker(text: str, step: dict[str, Any]) -> str | None:
    """Return an explicit update-required marker without matching harmless update checks."""
    markers = [str(x) for x in step.get("update_markers", DEFAULT_UPDATE_MARKERS)]
    folded = text.casefold()
    return next((marker for marker in markers if marker.casefold() in folded), None)


def _needs_update(product: str, marker: str, **details: Any) -> Result:
    return Result(False, f"{product}éœ€è¦æ›´æ–°ï¼šæ£€æµ‹åˆ° {marker}",
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
        self.log(f"æ‰§è¡Œï¼š{shown}")
        try:
            proc = subprocess.Popen(args, cwd=cwd or str(self.root), env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except FileNotFoundError:
            return Result(False, f"æ‰¾ä¸åˆ°ç¨‹åºï¼š{args[0]}")
        started = time.monotonic()
        interrupted_message = None
        while proc.poll() is None:
            if self.stop_event.is_set():
                interrupted_message = "ä»»åŠ¡è¢«ç”¨æˆ·åœæ­¢"
                break
            if time.monotonic() - started > timeout:
                interrupted_message = f"æ‰§è¡Œè¶…æ—¶ï¼ˆ{timeout} ç§’ï¼‰"
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
        return Result(proc.returncode == 0, "å®Œæˆ" if proc.returncode == 0 else f"é€€å‡ºç  {proc.returncode}",
                      {"exit_code": proc.returncode, "output": output[-4000:]})


def run_command(step: dict[str, Any], ctx: RunContext) -> Result:
    command = expand(str(step.get("command", "")))
    if not command:
        return Result(False, "command runner ç¼ºå°‘ command")
    args = [command] + [expand(str(x)) for x in step.get("args", [])]
    env = os.environ.copy()
    env.update({str(k): expand(str(v)) for k, v in step.get("env", {}).items()})
    result = ctx.command(args, int(step.get("timeout", 3600)), step.get("cwd"), env)
    accepted_codes = {int(code) for code in step.get("success_exit_codes", [0])}
    exit_code = result.details.get("exit_code")
    if exit_code in accepted_codes and not result.success:
        result = Result(True, f"å®Œæˆï¼ˆé€€å‡ºç  {exit_code} å·²æŒ‰é…ç½®æ¥å—ï¼‰", result.details)
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
        return Result(False, f"æ‰¾ä¸åˆ° MAA GUIï¼š{exe}")
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
        return Result(False, f"æ— æ³•å¯åŠ¨ MAAï¼š{exc}")
    timeout = int(step.get("timeout", 3600))
    started = time.monotonic()
    ctx.log(f"MAA GUI å·²å¯åŠ¨ï¼Œç­‰å¾…å®Œæˆæ ‡è®°ï¼š{marker}")
    position = start_size
    tail = ""
    exit_seen_at = None
    successor_seen = False
    update_seen = False
    restart_grace = float(step.get("restart_grace_seconds", 120))
    try:
        while time.monotonic() - started <= timeout:
            if ctx.stop_event.wait(1):
                return Result(False, "ä»»åŠ¡è¢«ç”¨æˆ·åœæ­¢")
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
                        return _needs_update("MAA æˆ–æ˜æ—¥æ–¹èˆŸ", update_marker,
                                             log=str(log_path))
                    if marker in tail:
                        return Result(True, "MAA æ—¥å¸¸ä»»åŠ¡å…¨éƒ¨å®Œæˆ", {"marker": marker})
                    for error in error_markers:
                        if error in tail:
                            return Result(False, f"MAA æŠ¥å‘Šä»»åŠ¡é”™è¯¯ï¼š{error}", {"marker": error})
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
                            "Updater started", "å¼€å§‹æ›´æ–°")):
                        update_seen = True
                        ctx.log("æ£€æµ‹åˆ° MAA æ­£åœ¨äº¤æ¥è‡ªåŠ¨æ›´æ–°ï¼›ä¿ç•™æ¨¡æ‹Ÿå™¨å¹¶ç­‰å¾…æ›´æ–°åçš„è¿›ç¨‹ç»§ç»­")
            if proc.poll() is not None and marker not in tail:
                successor_running = _process_path_exists(exe)
                successor_seen = successor_seen or successor_running
                if successor_running:
                    exit_seen_at = None
                elif exit_seen_at is None:
                    exit_seen_at = time.monotonic()
                    reason = "è‡ªåŠ¨æ›´æ–°äº¤æ¥" if update_seen or proc.returncode == 0 else "è¿›ç¨‹é€€å‡º"
                    ctx.log(f"MAA åŸè¿›ç¨‹å·²{reason}ï¼ˆé€€å‡ºç  {proc.returncode}ï¼‰ï¼Œ"
                            f"ç­‰å¾…æœ€å¤š {restart_grace:g} ç§’æ¥ç»­è¿›ç¨‹")
                elif time.monotonic() - exit_seen_at >= restart_grace:
                    return Result(False, f"MAA é€€å‡ºå {restart_grace:g} ç§’å†…æœªæ¢å¤"
                                  f"ï¼ˆé€€å‡ºç  {proc.returncode}ï¼‰",
                                  {"update_seen": update_seen,
                                   "successor_seen": successor_seen})
        return Result(False, f"ç­‰å¾… MAA å®Œæˆè¶…æ—¶ï¼ˆ{timeout} ç§’ï¼‰")
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
        return Result(False, f"æ‰¾ä¸åˆ° BAASï¼š{exe}")
    log_glob = expand(str(step.get("log_glob") or exe.parent / "runtime" / "logs" / "*_baas1.log"))
    marker = str(step.get("completion_marker", "ä»»åŠ¡å…¨éƒ¨æ‰§è¡ŒæˆåŠŸ"))
    error_markers = [str(x) for x in step.get("error_markers", ["ä»»åŠ¡å…¨éƒ¨æ‰§è¡Œå¤±è´¥"])]
    recoverable_markers = [str(x) for x in step.get("recoverable_error_markers", [
        "RestartTaskException: ATXå¡æ­»ï¼Œé‡å¯ä»»åŠ¡", "ATXå¡æ­»ï¼Œå¼€å§‹é‡å¯ATX",
        "ä»»åŠ¡å¡æ­»ï¼Œå¼€å§‹é‡å¯ä»»åŠ¡", "é‡å¯ä»»åŠ¡ã€",
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
        return Result(False, f"æ— æ³•å¯åŠ¨ BAASï¼š{exc}")
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
        "USB device", "is offline", "æ¨¡æ‹Ÿå™¨è¿æ¥å¤±è´¥ï¼Œå¿…é¡»æ‰“å¼€æ¨¡æ‹Ÿå™¨",
    ])]
    start_markers = [str(x) for x in step.get(
        "start_markers", ["æ¨¡æ‹Ÿå™¨è¿æ¥æˆåŠŸ", "å¼€å§‹æ‰§è¡Œã€"])]

    def restart_emulator() -> Result:
        if emulator_instance is None:
            return Result(False, "BAAS æœªé…ç½®å¯æ¢å¤çš„é›·ç”µæ¨¡æ‹Ÿå™¨å®ä¾‹")
        ldconsole = ctx.tool("ldconsole")
        index = str(emulator_instance)
        ctx.command([ldconsole, "quit", "--index", index], 60)
        if ctx.stop_event.wait(float(step.get("emulator_restart_delay", 3))):
            return Result(False, "ä»»åŠ¡è¢«ç”¨æˆ·åœæ­¢")
        launched = ctx.command([ldconsole, "launch", "--index", index], 120)
        if not launched.success:
            return Result(False, f"é‡æ–°å¯åŠ¨ç¢§è“æ¡£æ¡ˆæ¨¡æ‹Ÿå™¨å¤±è´¥ï¼š{launched.message}")
        return run_adb({"action": "wait", "device": emulator_device,
                        "timeout": int(step.get("emulator_ready_timeout", 180)),
                        "poll_seconds": 2, "settle_seconds": 3}, ctx)

    ctx.log(f"BAAS å·²å¯åŠ¨ï¼Œç­‰å¾… baas1 å®Œæˆæ ‡è®°ï¼š{marker}")
    try:
        while time.monotonic() - starß}:âÚ$z{-®éÜj×'6¶—VE÷F6·2#¢6¶—VE÷F6·2À¢&F—6&ÆVE÷F6·2#¢F—6&ÆVE÷F6·2À¢'7FÆÆVE÷&WG&–W2#¢7FÆÆVE÷&WG&–W7Ò¢–b7G‚ç7F÷öWfVçBçv—B†&WGvVVå÷&÷VæEöFVÆ’“ ¢&WGW&â&W7VÇB„fÇ6RÂ.K»¾XªŠ*¾yJh‹~XÎjÚ""¢6V6öæBÒ6Æ–6µ÷7F'Bƒ"¢–bæ÷B6V6öæBç7V66W73 ¢&WGW&â6V6öæ@¢&÷VæEöçVÖ&W"Ò ¢&÷VæE÷7F'FVBÒfÇ6P¢6Æ–6µöBÒF–ÖRæÖöæ÷Föæ–2‚¢Æ7Eö'W6–æW75öBÒF–ÖRæÖöæ÷Föæ–2‚¢–b†æ÷B&÷VæE÷7F'FVBæB6Æ–6µöB—2æ÷BæöæRæ@¢F–ÖRæÖöæ÷Föæ–2‚’Ò6Æ–6µöBâ&÷VæE÷7F'E÷F–ÖV÷WB“ ¢&WGW&â&W7VÇB„fÇ6RÂb.x+X{¾Yâ·&÷VæE÷7F'E÷F–ÖV÷WC¦wÒzy.Xh^iÊ®j8kX¾X‹zÊÂ·&÷VæEöçVÖ&W'Ò‹ÚîY
şXªiz^[ùr"¢–b‡&÷VæE÷7F'FVBæB'W6–æW75÷6–ÆVæ6U÷F–ÖV÷WBâ ¢æBF–ÖRæÖöæ÷Föæ–2‚’ÒÆ7Eö'W6–æW75öBãÒ'W6–æW75÷6–ÆVæ6U÷F–ÖV÷WB“ ¢–b&÷VæEöçVÖ&W"ÓÒ"æB7FÆÆVE÷&WG&–W2ÂÖ…÷7FÆÆVE÷&WG&–W3 ¢7FÆÆVE÷&WG&–W2³Ò¢7G‚æÆör†b.KˆŞh	ŞŠêî‹û~Zê¾zÊÃ.‹ÚîYÊ(	Ç¶Æ7Eö'W6–æW75öÆ–æWŞ(	ŞYâ ¢b"¶'W6–æW75÷6–ÆVæ6U÷F–ÖV÷WC¦wÒzy.izK‰®Xª‹ù¾[^ûÉ² ¢b.˜xŞY
şˆI®iÊÎ[›n˜xŞŠù^zÊÃ.‹ÚîûÈ‡·7FÆÆVE÷&WG&–W7Ò÷¶Ö…÷7FÆÆVE÷&WG&–W7ŞûÈ’"¢–b÷2ææÖRÓÒ&çB# ¢7V'&ö6W72ç'Vâ…²'F6¶¶–ÆÂ"Â"ô”Ò"Â&ö6W75ö–ÖvRÂ"õB"Â"ôb%ÒÀ¢7FF÷WC×7V'&ö6W72äDUdåTÄÂÂ7FFW'#×7V'&ö6W72äDUdåTÄÂÀ¢7&VF–öæfÆw3×7V'&ö6W72ä5$TDUôäõõt”äDõr¢VÆ–b&ö2çöÆÂ‚’—2æöæS ¢&ö2çFW&Ö–æFR‚¢–b7G‚ç7F÷öWfVçBçv—B†fÆöB‡7FWævWB‚'7FÆÆVE÷&W7F'EöFVÆ’"ÂR’’“ ¢&WGW&â&W7VÇB„fÇ6RÂ.K»¾XªŠ*¾yJh‹~XÎjÚ""¢G'“ ¢&ö2Ò7V'&ö6W72å÷Vâ€¢·7G"†W†R•ÒÂ7vC×7G"†W†Rç&VçB’À¢7&VF–öæfÆw3×7V'&ö6W72ä5$TDUôäõõt”äDõr–b÷2ææÖRÓÒ&çB"VÇ6R¢W†6WBõ4W'&÷"2W†3 ¢&WGW&â&W7VÇB„fÇ6RÂb.˜xŞY
şKˆŞh	ŞŠêî‹û~Zê¾ˆI®iÊÎZK‹J^ûÉ§¶W†7Ò"¢–b7G‚ç7F÷öWfVçBçv—B†fÆöB‡7FWævWB‚'7FÆÆVE÷&VÆVæ6…÷v—B"ÂR’’“ ¢&WGW&â&W7VÇB„fÇ6RÂ.K»¾XªŠ*¾yJh‹~XÎjÚ""¢6V6öæBÒ6Æ–6µ÷7F'Bƒ"¢–bæ÷B6V6öæBç7V66W73 ¢&WGW&â6V6öæ@¢&÷VæE÷7F'FVBÒfÇ6P¢6Æ–6µöBÒF–ÖRæÖöæ÷Föæ–2‚¢Æ7Eö'W6–æW75öBÒF–ÖRæÖöæ÷Föæ–2‚¢Æ7Eö'W6–æW75öÆ–æRÒ.˜xŞŠù^zÊÃ.‹ÚîY
şXª‚ ¢VÇ6S ¢&WGW&â&W7VÇB„fÇ6RÂb.KˆŞh	ŞŠêî‹û~Zê¾zÊÂ·&÷VæEöçVÖ&W'Ò‹ÚîXÚYÊ(	Ç¶Æ7Eö'W6–æW75öÆ–æWŞ(	ŞYâ ¢b"¶'W6–æW75÷6–ÆVæ6U÷F–ÖV÷WC¦wÒzy.izK‰®Xª‹ù¾[R"À¢²'&÷VæB#¢&÷VæEöçVÖ&W"Â&Æ7Eö'W6–æW75öÆ–æR#¢Æ7Eö'W6–æW75öÆ–æRÀ¢'7FÆÆVE÷&WG&–W2#¢7FÆÆVE÷&WG&–W2À¢'6¶—VE÷F6·2#¢6¶—VE÷F6·2À¢&F—6&ÆVE÷F6·2#¢F—6&ÆVE÷F6·7Ò¢–b&ö2çöÆÂ‚’—2æ÷BæöæRæBæ÷B÷&ö6W75ö–ÖvUöW†—7G2…·&ö6W75ö–ÖvUÒ“ ¢&WGW&â&W7VÇB„fÇ6RÂb.KˆŞh	ŞŠêî‹û~Zê¾ˆI®iÊÎhùX˜Ş˜X{®ûÈ˜X{®z·&ö2ç&WGW&æ6öFWŞûÈ’"¢&WGW&â&W7VÇB„fÇ6RÂb.zØ[è^KˆŞh	ŞŠêî‹û~Zê¾KŠN‹ÚîK»¾XªZèÎh‰‹h^i{nûÈ‡·F–ÖV÷WGÒzy.ûÈ’"¢f–æÆÇ“ ¢–b7FWævWB‚&6Æ÷6Uööåö6ö×ÆWFR"ÂG'VR“ ¢–b÷2ææÖRÓÒ&çB# ¢7V'&ö6W72ç'Vâ…²'F6¶¶–ÆÂ"Â"ô”Ò"Â&ö6W75ö–ÖvRÂ"õB"Â"ôb%ÒÀ¢7FF÷WC×7V'&ö6W72äDUdåTÄÂÂ7FFW'#×7V'&ö6W72äDUdåTÄÂÀ¢7&VF–öæfÆw3×7V'&ö6W72ä5$TDUôäõõt”äDõr¢VÆ–b&ö2çöÆÂ‚’—2æöæS ¢&ö2çFW&Ö–æFR‚  ¦FVb'VåöÆGÆ–W"‡7FW¢F–7E·7G"Âç•ÒÂ7Gƒ¢'Vä6öçFW‡B’Óâ&W7VÇC ¢W†RÂ–æFW‚Ò7G‚çFööÂ‚&ÆF6öç6öÆR"’Â7G"‡7FWævWB‚&–ç7Fæ6R"Â’¢7F–öâÒ7FWævWB‚&7F–öâ"Â&ÆVæ6‚"¢Ö–ærÒ²&ÆVæ6‚#¢²&ÆVæ6‚"Â"ÒÖ–æFW‚"Â–æFW…ÒÂ'V—B#¢²'V—B"Â"ÒÖ–æFW‚"Â–æFW…ÒÀ¢'&V&ö÷B#¢²'&V&ö÷B"Â"ÒÖ–æFW‚"Â–æFW…×Ğ¢–b7F–öâæ÷B–âÖ–æs ¢&WGW&â&W7VÇB„fÇ6RÂb.KˆŞiJşhÈy¨N™»~yK^XªKÙÎûÉ§¶7F–öçÒ"¢&W7VÇBÒ7G‚æ6öÖÖæB…¶W†UÒ²Ö–æu¶7F–öåÒÂ–çB‡7FWævWB‚'F–ÖV÷WB"Â#’’¢–b&W7VÇBç7V66W72æB7F–öâ–â‚&ÆVæ6‚"Â'&V&ö÷B"“ ¢v—BÒ–çB‡7FWævWB‚'6WGFÆU÷6V6öæG2"ÂR’¢VæBÒF–ÖRæÖöæ÷Föæ–2‚’²v—@¢v†–ÆRF–ÖRæÖöæ÷Föæ–2‚’ÂVæC ¢–b7G‚ç7F÷öWfVçBçv—Bƒã"“ ¢&WGW&â&W7VÇB„fÇ6RÂ.K»¾XªŠ*¾yJh‹~XÎjÚ""¢&WGW&â&W7VÇ@  ¦FVb'Våö×V×U÷v—B‡7FW¢F–7E·7G"Âç•ÒÂ7Gƒ¢'Vä6öçFW‡B’Óâ&W7VÇC ¢""$ÆVæ6‚×T×R"–ç7Fæ6RæBv—BVçF–ÂæG&ö–B—27GVÆÇ’&VG’â"" ¢ÖævW"ÒF‚†W‡æB‡7G"‡7FWævWB‚&W†V7WF&ÆR"Â""’’’¢–bæ÷BÖævW"æW†—7G2‚“ ¢&WGW&â&W7VÇB„fÇ6RÂb.h›îKˆŞX‹×T×TÖævW.ûÉ§¶ÖævW'Ò"¢–æFW‚Ò7G"‡7FWævWB‚&–ç7Fæ6R"Â’¢FWf–6RÒ7G"‡7FWævWB‚&FWf–6R"Â##rããã£c3ƒB"’¢F%öW†V7WF&ÆRÒF‚†W‡æB‡7G"‡7FWævWB‚&F%öW†V7WF&ÆR"’÷"ÖævW"ç&VçBò&F"æW†R"’’¢F–ÖV÷WBÒfÆöB‡7FWævWB‚'F–ÖV÷WB"Âƒ’¢öÆÅ÷6V6öæG2ÒÖ‚ƒã"ÂfÆöB‡7FWævWB‚'öÆÅ÷6V6öæG2"Â"’’¢F%öVçbÒ÷2æVçf—&öâæ6÷’‚¢–b7FWævWB‚&F%÷6W'fW%÷÷'B"’—2æ÷BæöæS ¢F%öVçe²$äE$ô”EôD%õ4U%dU%õõ%B%ÒÒ7G"‡7FW²&F%÷6W'fW%÷÷'B%Ò ¢FVbVW'•ö–æfò‚’ÓâF–7E·7G"Âç•ÒÂæöæS ¢G'“ ¢FöæRÒ7V'&ö6W72ç'Vâ€¢·7G"†ÖævW"’Â&–æfò"Â"Ò×fÖ–æFW‚"Â–æFW…ÒÀ¢7FF÷WC×7V'&ö6W72å•RÂ7FFW'#×7V'&ö6W72å5DDõUBÀ¢F–ÖV÷WCÖÖ–âƒ#ÂÖ‚ƒÂ–çB‡F–ÖV÷WB’’’À¢7&VF–öæfÆw3×7V'&ö6W72ä5$TDUôäõõt”äDõr–b÷2ææÖRÓÒ&çB"VÇ6R¢W†6WB„õ4W'&÷"Â7V'&ö6W72åF–ÖV÷WDW‡—&VB“ ¢&WGW&âæöæP¢–bFöæRç&WGW&æ6öFRÒ ¢&WGW&âæöæP¢÷WGWBÒöFV6öFU÷&ö6W75ö÷WGWB†FöæRç7FF÷WB’ç7G&—‚¢G'“ ¢fÇVRÒ§6öâæÆöG2†÷WGWB¢&WGW&âfÇVR–b—6–ç7Fæ6R‡fÇVRÂF–7B’VÇ6RæöæP¢W†6WB§6öâä¥4ôäFV6öFTW'&÷# ¢&WGW&âæöæP ¢–æfòÒVW'•ö–æfò‚¢–bæ÷B–æfò÷"æ÷B–æfòævWB‚&—5öæG&ö–E÷7F'FVB"“ ¢ÆVæ6†VBÒ7G‚æ6öÖÖæB€¢·7G"†ÖævW"’Â&6öçG&öÂ"Â"Ò×fÖ–æFW‚"Â–æFW‚Â&ÆVæ6‚%ÒÀ¢Ö–âƒ#ÂÖ‚ƒÂ–çB‡F–ÖV÷WB’’’¢–bæ÷BÆVæ6†VBç7V66W73 ¢&WGW&â&W7VÇB„fÇ6RÂb.Y
şXª‚×T×RZéîKè²¶–æFW‡ÒZK‹J^ûÉ§¶ÆVæ6†VBæÖW76vWÒ"ÂÆVæ6†VBæFWF–Ç2 ¢7F'FVEöBÒF–ÖRæÖöæ÷Föæ–2‚¢v†–ÆRF–ÖRæÖöæ÷Föæ–2‚’Ò7F'FVEöBÃÒF–ÖV÷WC ¢–b7G‚ç7F÷öWfVçBæ—5÷6WB‚“ ¢&WGW&â&W7VÇB„fÇ6RÂ.K»¾XªŠ*¾yJh‹~XÎjÚ""¢–æfòÒVW'•ö–æfò‚¢–b–æfòæB–æfòævWB‚&—5öæG&ö–E÷7F'FVB"“ ¢–bæ÷BF%öW†V7WF&ÆRæW†—7G2‚“ ¢&WGW&â&W7VÇB„fÇ6RÂb.h›îKˆŞX‹×T×RD.ûÉ§¶F%öW†V7WF&ÆWÒ"¢F—&V7Eö6öææV7BÒ7G‚æ6öÖÖæB€¢·7G"†F%öW†V7WF&ÆR’Â&6öææV7B"ÂFWf–6UÒÂRÂVçcÖF%öVçb¢–bæ÷BF—&V7Eö6öææV7Bç7V66W73 ¢7G‚æÆör‚$×T×RæG&ö–B[{.Y
şXªûÈÎKØnxºÎz¸²D"˜	®˜>[	®iÊ®™˜NyØûÉ¾{º~{ºŞzØ[èR"¢–b7G‚ç7F÷öWfVçBçv—B‡öÆÅ÷6V6öæG2“ ¢&WGW&â&W7VÇB„fÇ6RÂ.K»¾XªŠ*¾yJh‹~XÎjÚ""¢6öçF–çVP¢&ö÷BÒ7G‚æ6öÖÖæB€¢·7G"†F%öW†V7WF&ÆR’Â"×2"ÂFWf–6RÂ'6†VÆÂ"À¢&vWG&÷"Â'7—2æ&ö÷Eö6ö×ÆWFVB%ÒÂRÂVçcÖF%öVçb¢&ö÷Eö÷WGWBÒ7G"†&ö÷BæFWF–Ç2ævWB‚&÷WGWB"Â""’’ç7G&—‚¢–b&ö÷Bç7V66W72æB&Rç6V&6‚‡""ƒó¥çÅÇ2“ƒó¥Ç7ÂB’"Â&ö÷Eö÷WGWB“ ¢6WGFÆRÒfÆöB‡7FWævWB‚'6WGFÆU÷6V6öæG2"Â2’¢–b7G‚ç7F÷öWfVçBçv—B‡6WGFÆR“ ¢&WGW&â&W7VÇB„fÇ6RÂ.K»¾XªŠ*¾yJh‹~XÎjÚ""¢&WGW&â&W7VÇB…G'VRÂb$×T×RZéîKè²¶–æFW‡Ò[{.Y
şXªûÈÄD"KˆâæG&ö–B{;¾{¹şYØ~[{.[{º¢"À¢²&–ç7Fæ6R#¢–æFW‚Â&–æfò#¢–æfòÀ¢&FWf–6R#¢FWf–6RÂ&F%ö6öææV7FVB#¢G'VRÀ¢&&ö÷Eö6ö×ÆWFVB#¢G'VWÒ¢7G‚æÆör‚$×T×RD"[{.‹ùîhê^ûÈÎKØbæG&ö–B{;¾{¹şK¸ŞYÊY
şXªûÉ¾{º~{ºŞzØ[è^{;¾{¹ş[{º¢"¢–b7G‚ç7F÷öWfVçBçv—B‡öÆÅ÷6V6öæG2“ ¢&WGW&â&W7VÇB„fÇ6RÂ.K»¾XªŠ*¾yJh‹~XÎjÚ""¢&WGW&â&W7VÇB„fÇ6RÂb.zØ[èR×T×RZéîKè²¶–æFW‡ÒY
şXª‹h^i{nûÈ‡·F–ÖV÷WC¦wÒzy.ûÈ’"  ¦FVb'VåöF"‡7FW¢F–7E·7G"Âç•ÒÂ7Gƒ¢'Vä6öçFW‡B’Óâ&W7VÇC ¢F"ÒW‡æB‡7G"‡7FWævWB‚&W†V7WF&ÆR"’÷"7G‚çFööÂ‚&F""’’¢FWf–6RÒ7G"‡7FWævWB‚&FWf–6R"’÷"7G‚æ6öæf–rævWB‚&FWf–6R"Â·Ò’ævWB‚&FG&W72"Â""’¢&Vf—‚Ò¶F%Ò²…²"×2"ÂFWf–6UÒ–bFWf–6RVÇ6RµÒ¢7F–öâÒ7FWævWB‚&7F–öâ"Â'v—B"¢F–ÖV÷WBÒ–çB‡7FWævWB‚'F–ÖV÷WB"Â#’¢VçbÒ÷2æVçf—&öâæ6÷’‚¢VçbçWFFR‡·7G"†²“¢W‡æB‡7G"‡b’’f÷"²Âb–â7FWævWB‚&Vçb"Â·Ò’æ—FV×2‚—Ò¢–b7FWævWB‚&F%÷6W'fW%÷÷'B"’—2æ÷BæöæS ¢Vçe²$äE$ô”EôD%õ4U%dU%õõ%B%ÒÒ7G"‡7FW²&F%÷6W'fW%÷÷'B%Ò¢–b7F–öâÓÒ'v—B# ¢FVFÆ–æRÒF–ÖRæÖöæ÷Föæ–2‚’²F–ÖV÷W@¢öÆÅ÷6V6öæG2ÒÖ‚ƒã"ÂfÆöB‡7FWævWB‚'öÆÅ÷6V6öæG2"Â"’’¢6¶vRÒ7G"‡7FWævWB‚'6¶vR"Â""’’ç7G&—‚¢&WV—&UöÆVæ6†W"Ò&ööÂ‡7FWævWB‚'&WV—&UöÆVæ6†W""ÂfÇ6R’¢Æ7EöÖW76vRÒ$D"ŠëîZH~[	®iÊ®[{º¢ ¢v†–ÆRF–ÖRæÖöæ÷Föæ–2‚’ÂFVFÆ–æS ¢–b7G‚ç7F÷öWfVçBæ—5÷6WB‚“ ¢&WGW&â&W7VÇB„fÇ6RÂ.K»¾XªŠ*¾yJh‹~XÎjÚ""¢&VÖ–æ–ærÒÖ‚ƒÂ–çB†FVFÆ–æRÒF–ÖRæÖöæ÷Föæ–2‚’’¢v—E÷&W7VÇBÒ7G‚æ6öÖÖæB‡&Vf—‚²²'v—BÖf÷"ÖFWf–6R%ÒÂÖ–âƒÂ&VÖ–æ–ær’ÂVçcÖVçb¢–bv—E÷&W7VÇBç7V66W73 ¢&ö÷BÒ7G‚æ6öÖÖæB‡&Vf—‚²²'6†VÆÂ"Â&vWG&÷"Â'7—2æ&ö÷Eö6ö×ÆWFVB%ÒÀ¢Ö–âƒÂ&VÖ–æ–ær’ÂVçcÖVçb¢&ö÷Eö÷WGWBÒ7G"†&ö÷BæFWF–Ç2ævWB‚&÷WGWB"Â""’’ç7G&—‚¢–b&ö÷Bç7V66W72æB&Rç6V&6‚‡""ƒó¥çÅÇ2“ƒó¥Ç7ÂB’"Â&ö÷Eö÷WGWB“ ¢6¶vU÷&VG’ÒG'VP¢–b6¶vS ¢6¶vU÷&W7VÇBÒ7G‚æ6öÖÖæB‡&Vf—‚²²'6†VÆÂ"Â'Ò"Â'F‚"Â6¶vUÒÀ¢Ö–âƒÂ&VÖ–æ–ær’ÂVçcÖVçb¢6¶vUö÷WGWBÒ7G"‡6¶vU÷&W7VÇBæFWF–Ç2ævWB‚&÷WGWB"Â""’¢6¶vU÷&VG’Ò6¶vU÷&W7VÇBç7V66W72æB'6¶vS¢"–â6¶vUö÷WGW@¢–b6¶vU÷&VG’æB&WV—&UöÆVæ6†W# ¢ÆVæ6†W"Ò7G‚æ6öÖÖæB€¢&Vf—‚²²'6†VÆÂ"Â&6ÖB"Â'6¶vR"Â'&W6öÇfRÖ7F—f—G’"À¢"ÒÖ'&–Vb"Â6¶vUÒÂÖ–âƒÂ&VÖ–æ–ær’ÂVçcÖVçb¢ÆVæ6†W%ö÷WGWBÒ7G"†ÆVæ6†W"æFWF–Ç2ævWB‚&÷WGWB"Â""’¢6¶vU÷&VG’Ò†ÆVæ6†W"ç7V66W72æB&ööÂ†ÆVæ6†W%ö÷WGWBç7G&—‚’¢æB$æò7F—f—G’f÷VæB"æ÷B–âÆVæ6†W%ö÷WGWB¢–bæ÷B6¶vU÷&VG“ ¢Æ7EöÖW76vRÒb$æG&ö–B[{.Y
şXªûÈÎKØn[©NyJ‚·6¶vWÒ[	®iÊ®XúşY
şXª‚ ¢–b6¶vU÷&VG“ ¢6WGFÆRÒÖ‚ƒãÂfÆöB‡7FWævWB‚'6WGFÆU÷6V6öæG2"Â’’¢–b7G‚ç7F÷öWfVçBçv—B‡6WGFÆR“ ¢&WGW&â&W7VÇB„fÇ6RÂ.K»¾XªŠ*¾yJh‹~XÎjÚ""¢&WGW&â&W7VÇB…G'VRÂ$D.8æG&ö–B{;¾{¹şKˆî[©NyJYØ~[{.[{º¢"–b6¶vP¢VÇ6R$D"KˆâæG&ö–B{;¾{¹şYØ~[{.[{º¢"À¢²&FWf–6R#¢FWf–6RÂ&&ö÷Eö6ö×ÆWFVB#¢G'VRÀ¢'6¶vR#¢6¶vR÷"æöæWÒ¢VÇ6S ¢Æ7EöÖW76vRÒ$D"[{.‹ùîhê^ûÈÎKØbæG&ö–B{;¾{¹şK¸ŞYÊY
şXª‚ ¢VÇ6S ¢Æ7EöÖW76vRÒv—E÷&W7VÇBæÖW76vP¢7G‚æÆör†b'¶Æ7EöÖW76vWŞûÉ¾{º~{ºŞzØ[èR"¢–b7G‚ç7F÷öWfVçBçv—B†Ö–â‡öÆÅ÷6V6öæG2ÂÖ‚ƒãÂFVFÆ–æRÒF–ÖRæÖöæ÷Föæ–2‚’’’“ ¢&WGW&â&W7VÇB„fÇ6RÂ.K»¾XªŠ*¾yJh‹~XÎjÚ""¢&WGW&â&W7VÇB„fÇ6RÂb.zØ[èRæG&ö–BZèÎXZY
şXª‹h^i{nûÈ‡·F–ÖV÷WGÒzy.ûÈûÉ§¶Æ7EöÖW76vWÒ"¢–b7F–öâÓÒ'67&VVç6†÷B# ¢÷WBÒF‚†W‡æB‡7G"‡7FWævWB‚'F‚"Â7G‚ç&ö÷Bò&Æöw2"ò'67&VVç6†÷Bçær"’’’¢÷WBç&VçBæÖ¶F—"‡&VçG3ÕG'VRÂW†—7Eöö³ÕG'VR¢G'“ ¢FöæRÒ7V'&ö6W72ç'Vâ‡&Vf—‚²²&W†V2Ö÷WB"Â'67&VVæ6"Â"×%ÒÀ¢7FF÷WC×7V'&ö6W72å•RÂ7FFW'#×7V'&ö6W72å•RÀ¢F–ÖV÷WC×F–ÖV÷WBÂVçcÖVçbÀ¢7&VF–öæfÆw3×7V'&ö6W72ä5$TDUôäõõt”äDõr–b÷2ææÖRÓÒ&çB"VÇ6R¢W†6WB„õ4W'&÷"Â7V'&ö6W72åF–ÖV÷WDW‡—&VB’2W†3 ¢&WGW&â&W7VÇB„fÇ6RÂb.jŠh¹şYšhŠ®Y»îZK‹J^ûÉ§¶W†7Ò"¢–bFöæRç&WGW&æ6öFRÒ÷"æ÷BFöæRç7FF÷WC ¢W'&÷"ÒöFV6öFU÷&ö6W75ö÷WGWB†FöæRç7FFW'"’ç7G&—‚¢&WGW&â&W7VÇB„fÇ6RÂb.jŠh¹şYšhŠ®Y»îZK‹J^ûÉ§¶W'&÷"÷"~iÊ®‹ùNY¹îY»îX8òwÒ"À¢²&W†—Eö6öFR#¢FöæRç&WGW&æ6öFWÒ¢G'“ ¢÷WBçw&—FUö'—FW2†FöæRç7FF÷WB¢W†6WBõ4W'&÷"2W†3 ¢&WGW&â&W7VÇB„fÇ6RÂb.KùŞZÙjŠh¹şYšhŠ®Y»îZK‹J^ûÉ§¶W†7Ò"¢&WGW&â&W7VÇB…G'VRÂ.jŠh¹şYšhŠ®Y»î[{.KùŞZÙ‚"Â²'F‚#¢7G"†÷WB’Â&'—FW2#¢ÆVâ†FöæRç7FF÷WB—Ò¢–b7F–öâÓÒ'7F'Eö# ¢6¶vRÒ7G"‡7FWævWB‚'6¶vR"Â""’¢&WGW&â7G‚æ6öÖÖæB‡&Vf—‚²²'6†VÆÂ"Â&Ööæ¶W’"Â"×"Â6¶vRÂ"Ö2"Â&æG&ö–Bæ–çFVçBæ6FVv÷'’äÄTä4„U""Â#%ÒÂF–ÖV÷WBÂVçcÖVçb¢–b7F–öâÓÒ'7F÷ö# ¢&WGW&â7G‚æ6öÖÖæB‡&Vf—‚²²'6†VÆÂ"Â&Ò"Â&f÷&6R×7F÷"Â7G"‡7FWævWB‚'6¶vR"Â""’•ÒÂF–ÖV÷WBÂVçcÖVçb¢–b7F–öâÓÒ'6†VÆÂ# ¢&WGW&â7G‚æ6öÖÖæB‡&Vf—‚²²'6†VÆÂ%Ò²·7G"‡‚’f÷"‚–â7FWævWB‚&&w2"ÂµÒ•ÒÂF–ÖV÷WBÂVçcÖVçb¢&WGW&â&W7VÇB„fÇ6RÂb.KˆŞiJşhÈy¨BD"XªKÙÎûÉ§¶7F–öçÒ"  ¦FVb'VåöFVÆ’‡7FW¢F–7E·7G"Âç•ÒÂ7Gƒ¢'Vä6öçFW‡B’Óâ&W7VÇC ¢6V6öæG2ÒfÆöB‡7FWævWB‚'6V6öæG2"Â’¢&WGW&â&W7VÇB†æ÷B7G‚ç7F÷öWfVçBçv—B‡6V6öæG2’Â.zØ[è^ZèÎh‰"–bæ÷B7G‚ç7F÷öWfVçBæ—5÷6WB‚’VÇ6R.K»¾XªŠ*¾yJh‹~XÎjÚ""  ¥%TääU%2Ò²&6öÖÖæB#¢'Våö6öÖÖæBÂ&Ö#¢'VåöÖÂ&ÖöwV’#¢'VåöÖöwV’À¢&&5öwV’#¢'Våö&5öwV’Â&&÷&Wv&E÷fW&–g’#¢'Våö&÷&Wv&E÷fW&–g’À¢&Æ5öwV’#¢'VåöÆ5öwV’Â&æ'WFõ÷6†F÷r#¢'Våöæ'WFõ÷6†F÷rÀ¢&æ'WFõ÷&Wv&E÷fW&–g’#¢'Våöæ'WFõ÷&Wv&E÷fW&–g’À¢&wVÖ&ÆÇ5öwV’#¢'VåöwVÖ&ÆÇ5öwV’Â&ÖVæEöwV’#¢'VåöÖVæEöwV’À¢&ÆöuöwV•öF–Ç’#¢'VåöÆöuöwV•öF–Ç’À¢'v–æF÷u÷67&VVç6†÷B#¢'Vå÷v–æF÷u÷67&VVç6†÷BÀ¢&ÆGÆ–W"#¢'VåöÆGÆ–W"Â&×V×U÷v—B#¢'Våö×V×U÷v—BÀ¢&F"#¢'VåöF"Â&FVÆ’#¢'VåöFVÆ—Ğ 