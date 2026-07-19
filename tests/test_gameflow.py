import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gameflow.config import ConfigError, load_config
from gameflow.engine import Engine, WorkflowManager
from gameflow.mailer import send_daily_screenshots
from gameflow.store import Store
from gameflow.web import PAGE
from gameflow.runners import (RUNNERS, Result, RunContext, _decode_process_output, _naruto_visual_metrics, run_alas_gui,
                              run_adb, run_baas_gui, run_ba_reward_verify, run_gumballs_gui,
                              run_log_gui_daily, run_maa_gui, run_maaend_gui, run_mumu_wait)


class GameFlowTests(unittest.TestCase):
    def test_web_dashboard_keeps_controls_and_anime_theme(self):
        for required in ("id=\"tasks\"", "id=\"parallel\"", "id=\"startDaily\"",
                         "id=\"logView\"", "GameFlow 次元作战终端", "class=\"gacha-card\"",
                         "UI_AvatarIcon_", "drawCharacter()"):
            self.assertIn(required, PAGE)
        self.assertIn("葬送的芙莉莲", PAGE)
        self.assertIn("我推的孩子", PAGE)
        self.assertIn("POOL 50", PAGE)
        self.assertIn("交给诺艾尔吧，我会把每一项都妥善完成。", PAGE)
        self.assertNotIn("characteristicQuote", PAGE)
        self.assertNotIn("loadExpandedCharacterPool", PAGE)
        self.assertNotIn("我的英雄学院", PAGE)
        self.assertIn("mousedown", PAGE)
        self.assertIn("mousemove", PAGE)
        self.assertIn("drop-before", PAGE)
        self.assertIn("按住拖拽调整顺序", PAGE)
        self.assertNotIn("枫原万叶", PAGE)

    def test_windows_command_output_decoding(self):
        message = "成功: 已终止进程。"
        self.assertEqual(_decode_process_output(message.encode("gb18030")), message)
        self.assertEqual(_decode_process_output(message.encode("utf-8")), message)

    def test_command_timeout_does_not_wait_for_inherited_output_pipe(self):
        if os.name != "nt":
            self.skipTest("Windows process-tree cleanup regression")
        with tempfile.TemporaryDirectory() as tmp:
            ctx = RunContext(Path(tmp), {}, lambda _: None, threading.Event())
            script = ("import subprocess,sys,time; "
                      "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); "
                      "time.sleep(30)")
            started = time.monotonic()
            result = ctx.command([sys.executable, "-c", script], 1)
            elapsed = time.monotonic() - started
        self.assertFalse(result.success)
        self.assertIn("执行超时", result.message)
        self.assertLess(elapsed, 8)

    def test_config_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"; path.write_text("{}", encoding="utf-8")
            with self.assertRaises(ConfigError): load_config(path)

    def test_pc_daily_workflows_share_exclusive_group(self):
        config = load_config(Path(__file__).resolve().parents[1] / "config" / "workflow.json")
        names = ("endfield_daily", "zenless_daily", "star_rail_daily")
        self.assertTrue(all(name in config["workflows"] for name in names))
        self.assertEqual({config["workflows"][name].get("exclusive_group") for name in names},
                         {"pc_hoyoverse_daily"})
        self.assertEqual(config["workflows"]["zenless_daily"]["steps"][0]["button_names"],
                         ["启动一条龙"])
        self.assertEqual(config["workflows"]["star_rail_daily"]["steps"][0]["button_names"],
                         ["完整运行"])

    def test_azur_lane_and_naruto_startup_guards_are_configured(self):
        config = load_config(Path(__file__).resolve().parents[1] / "config" / "workflow.json")
        azur_steps = config["workflows"]["azur_lane_daily"]["steps"]
        self.assertEqual([step["id"] for step in azur_steps[:2]],
                         ["start_azur_lane_emulator", "run_azur_lane_script"])
        self.assertEqual(azur_steps[0]["runner"], "mumu_wait")
        self.assertFalse(config["workflows"]["azur_lane_daily"].get("exclusive_with"))
        naruto = config["workflows"]["naruto_daily"]["steps"][2]
        self.assertTrue(naruto["shadow_open_confirm"])
        self.assertEqual(naruto["shadow_open_confirm_point"], [360, 936])

    def test_successful_workflow_and_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); store = Store(root / "db.sqlite")
            config = {"workflows": {"test": {"steps": [
                {"id": "wait", "runner": "delay", "seconds": 0.01},
                {"id": "command", "runner": "command", "command": "python", "args": ["--version"], "timeout": 5}
            ]}}}
            engine = Engine(root, config, store)
            ok, _ = engine.start("test")
            self.assertTrue(ok)
            engine._thread.join(5)
            self.assertEqual(engine.state()["last_status"], "success")
            self.assertEqual(store.recent()[0]["status"], "success")

    def test_failure_still_runs_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); marker = root / "cleanup.txt"; store = Store(root / "db.sqlite")
            config = {"workflows": {"test": {"steps": [
                {"id": "fail", "runner": "command", "command": "definitely_missing_program_xyz", "timeout": 1},
                {"id": "cleanup", "runner": "command", "command": "python", "args": ["-c", f"open(r'{marker}','w').write('ok')"], "run_always": True}
            ]}}}
            engine = Engine(root, config, store); engine.start("test"); engine._thread.join(5)
            self.assertEqual(engine.state()["last_status"], "failed")
            self.assertEqual(marker.read_text(), "ok")

    def test_ignored_cleanup_failure_does_not_overwrite_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); store = Store(root / "db.sqlite")
            config = {"workflows": {"test": {"steps": [
                {"id": "main", "runner": "fake_ok"},
                {"id": "cleanup", "runner": "fake_fail", "run_always": True,
                 "continue_on_error": True}
            ]}}}
            with patch.dict(RUNNERS, {
                    "fake_ok": lambda step, ctx: Result(True, "done"),
                    "fake_fail": lambda step, ctx: Result(False, "process already gone")}):
                engine = Engine(root, config, store)
                engine.start("test"); engine.join(2)
            self.assertEqual(engine.state()["last_status"], "success")
            self.assertEqual(store.recent()[0]["status"], "success")

    def test_adb_wait_requires_android_and_package_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); messages = []
            ctx = RunContext(root, {"tools": {"adb": "adb"}}, messages.append,
                             threading.Event())

            def command(args, timeout, cwd=None, env=None):
                joined = " ".join(args)
                if "wait-for-device" in joined:
                    return Result(True, "connected")
                if "sys.boot_completed" in joined:
                    return Result(True, "ready", {"output": "1"})
                if "pm path" in joined:
                    return Result(True, "ready", {"output": "package:/data/app/test.apk"})
                if "resolve-activity" in joined:
                    return Result(True, "ready", {"output": "com.test/.MainActivity"})
                return Result(False, "unexpected")

            ctx.command = command
            result = run_adb({"action": "wait", "device": "emulator-5554",
                              "package": "com.test", "require_launcher": True,
                              "settle_seconds": 0, "timeout": 2}, ctx)
            self.assertTrue(result.success)
            self.assertTrue(result.details["boot_completed"])

    def test_baas_runner_follows_new_log_content(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "baas.exe"; exe.write_bytes(b"")
            logs = root / "logs"; logs.mkdir()
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            def write_marker():
                time.sleep(0.1)
                (logs / "today_baas1.log").write_text(
                    "模拟器连接成功\n开始执行【工作任务】\n任务全部执行成功", encoding="utf-8")

            threading.Thread(target=write_marker, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "*_baas1.log"),
                    "completion_marker": "任务全部执行成功", "timeout": 3,
                    "completion_quiet_seconds": 0.1,
                    "clean_existing": False, "close_on_complete": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()):
                result = run_baas_gui(step, ctx)
            self.assertTrue(result.success)

    def test_blue_archive_reward_verification(self):
        import cv2
        import numpy as np

        class Done:
            def __init__(self, stdout=b"", stderr=b"", returncode=0):
                self.stdout, self.stderr, self.returncode = stdout, stderr, returncode

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = cv2.imread(r"E:\baas\baas-pro\_internal\assets\images\cn\work_task\menu.png")
            self.assertIsNotNone(work)
            home = np.zeros((720, 1280, 3), dtype=np.uint8)
            h, w = work.shape[:2]
            home[100:100+h, 200:200+w] = work
            task_page = np.full((720, 1280, 3), 80, dtype=np.uint8)
            home_png = cv2.imencode(".png", home)[1].tobytes()
            page_png = cv2.imencode(".png", task_page)[1].tobytes()
            calls = [Done(home_png), Done(), Done(page_png)]
            step = {
                "device": "test", "use_fixed_entry": True,
                "entry_x_ratio": 0.052, "entry_y_ratio": 0.326,
                "evidence_path": str(root / "evidence.png"), "page_wait": 0, "threshold": 0.8
            }
            ctx = RunContext(root, {"tools": {"adb": "adb"}}, lambda _: None, threading.Event())
            with patch("gameflow.runners.subprocess.run", side_effect=calls):
                result = run_ba_reward_verify(step, ctx)
            self.assertTrue(result.success)
            self.assertTrue((root / "evidence.png").exists())

    def test_baas_runner_waits_until_last_completed_task_is_work_task(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "baas.exe"; exe.write_bytes(b"")
            logs = root / "logs"; logs.mkdir(); log = logs / "today_baas1.log"
            messages = []
            ctx = RunContext(root, {}, messages.append, threading.Event())

            def write_tasks():
                time.sleep(0.1)
                log.write_text(
                    "模拟器连接成功\n开始执行【重启设置】\n执行完成【重启设置】\n"
                    "任务全部执行成功\n", encoding="utf-8")
                time.sleep(1.3)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("开始执行【工作任务】\n执行完成【工作任务】\n")

            threading.Thread(target=write_tasks, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "*_baas1.log"),
                    "completion_marker": "任务全部执行成功", "required_last_task": "工作任务",
                    "required_task_check_interval": 0.2, "completion_quiet_seconds": 0.1,
                    "timeout": 5, "clean_existing": False, "close_on_complete": False,
                    "gui_click_fallback": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()):
                result = run_baas_gui(step, ctx)
            self.assertTrue(result.success)
            self.assertEqual(result.details["last_completed_task"], "工作任务")
            self.assertTrue(any("不是“工作任务”" in message for message in messages))

    def test_baas_recoverable_atx_restart_does_not_abort(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "baas.exe"; exe.write_bytes(b"")
            logs = root / "logs"; logs.mkdir(); log = logs / "today_baas1.log"
            log.write_text("", encoding="utf-8")
            messages = []
            ctx = RunContext(root, {}, messages.append, threading.Event())

            def write_recovery():
                time.sleep(0.1)
                log.write_text("Traceback (most recent call last):\n"
                               "RestartTaskException: ATX卡死，重启任务\n", encoding="utf-8")
                time.sleep(1.1)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("模拟器连接成功\n开始执行【工作任务】\n"
                                 "执行完成【工作任务】\n任务全部执行成功\n")

            threading.Thread(target=write_recovery, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "*_baas1.log"),
                    "completion_quiet_seconds": 0.05, "recoverable_retry_seconds": 0.05,
                    "timeout": 4, "clean_existing": False, "close_on_complete": False,
                    "gui_click_fallback": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()):
                result = run_baas_gui(step, ctx)
            self.assertTrue(result.success)
            self.assertEqual(result.details["recoverable_events"], 1)
            self.assertTrue(any("可恢复异常" in message for message in messages))

    def test_baas_restarts_offline_emulator_and_resumes(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "baas.exe"; exe.write_bytes(b"")
            logs = root / "logs"; logs.mkdir(); log = logs / "today_baas1.log"
            log.write_text("", encoding="utf-8")
            commands = []
            ctx = RunContext(root, {"tools": {"ldconsole": "ldconsole.exe"}},
                             lambda _: None, threading.Event())
            def command(args, timeout, cwd=None, env=None):
                commands.append(args)
                return Result(True, "ready", {"output": ""})
            ctx.command = command

            def write_recovery():
                time.sleep(0.1)
                log.write_text("USB device emulator-5560 is offline\n", encoding="utf-8")
                time.sleep(0.3)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("模拟器连接成功\n开始执行【工作任务】\n"
                                 "执行完成【工作任务】\n任务全部执行成功\n")

            threading.Thread(target=write_recovery, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "*_baas1.log"),
                    "device": "emulator-5560", "emulator_instance": 3,
                    "max_emulator_restarts": 1, "poll_seconds": 0.02,
                    "emulator_restart_delay": 0, "completion_quiet_seconds": 0.05,
                    "timeout": 3, "clean_existing": False, "close_on_complete": False,
                    "gui_click_fallback": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners.run_adb", return_value=Result(True, "ready")):
                result = run_baas_gui(step, ctx)
            self.assertTrue(result.success)
            self.assertEqual(result.details["emulator_restarts"], 1)
            self.assertIn(["ldconsole.exe", "quit", "--index", "3"], commands)
            self.assertIn(["ldconsole.exe", "launch", "--index", "3"], commands)

    def test_baas_retries_start_click_until_fresh_log_appears(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "baas.exe"; exe.write_bytes(b"")
            logs = root / "logs"; logs.mkdir(); log = logs / "today_baas1.log"
            log.write_text("", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())
            clicks = 0

            def click(*args):
                nonlocal clicks
                clicks += 1
                if clicks == 2:
                    log.write_text("模拟器连接成功\n开始执行【工作任务】\n"
                                   "执行完成【工作任务】\n任务全部执行成功\n",
                                   encoding="utf-8")
                return True, "clicked"

            step = {"executable": str(exe), "log_glob": str(logs / "*_baas1.log"),
                    "gui_fallback_after": 0, "gui_fallback_retry_seconds": 0.05,
                    "startup_timeout": 2, "max_start_clicks": 4, "poll_seconds": 0.02,
                    "completion_quiet_seconds": 0.05, "timeout": 2,
                    "clean_existing": False, "close_on_complete": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners._click_baas_start_button", side_effect=click):
                result = run_baas_gui(step, ctx)
            self.assertTrue(result.success)
            self.assertEqual(clicks, 2)

    def test_maa_gui_follows_successor_after_update_exit(self):
        class FakeProcess:
            pid = 123456789
            returncode = 0
            def poll(self): return 0
            def terminate(self): pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "MAA.exe"; exe.write_bytes(b"")
            debug = root / "debug"; debug.mkdir()
            asst = debug / "asst.log"; asst.write_text("", encoding="utf-8")
            gui = debug / "gui.log"; gui.write_text("", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            def update_and_finish():
                time.sleep(0.1)
                gui.write_text("Pending update package detected\n", encoding="utf-8")
                time.sleep(0.1)
                asst.write_text("AllTasksCompleted\n", encoding="utf-8")

            threading.Thread(target=update_and_finish, daemon=True).start()
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners._process_path_exists", return_value=True), \
                    patch("gameflow.runners.subprocess.run"):
                result = run_maa_gui({"executable": str(exe), "log_path": str(asst),
                                      "gui_log_path": str(gui), "timeout": 3,
                                      "restart_grace_seconds": 1}, ctx)
            self.assertTrue(result.success)

    def test_naruto_completion_and_reward_visuals(self):
        import cv2
        import numpy as np

        home_hsv = np.zeros((720, 1280, 3), dtype=np.uint8)
        home_hsv[:, :, 2] = 60
        home_hsv[100:340, 250:900] = (108, 180, 100)
        home = cv2.cvtColor(home_hsv, cv2.COLOR_HSV2BGR)
        self.assertTrue(_naruto_visual_metrics(home)["home_page"])

        popup = np.full((720, 1280, 3), 30, dtype=np.uint8)
        popup[120:600, 40:1240] = 255
        self.assertTrue(_naruto_visual_metrics(popup)["completion_popup"])

        portrait_dialog = np.full((1280, 720, 3), 30, dtype=np.uint8)
        portrait_dialog[210:990, 45:675] = 255
        portrait_metrics = _naruto_visual_metrics(portrait_dialog)
        self.assertTrue(portrait_metrics["portrait_white_dialog"])
        self.assertFalse(portrait_metrics["completion_popup"])

        privacy_hsv = np.zeros((1280, 720, 3), dtype=np.uint8)
        privacy_hsv[:, :, 2] = 30
        privacy_hsv[210:1100, 20:700] = (0, 0, 240)
        privacy_hsv[1152:1254, 374:684] = (15, 220, 240)
        privacy = cv2.cvtColor(privacy_hsv, cv2.COLOR_HSV2BGR)
        self.assertTrue(_naruto_visual_metrics(privacy)["game_privacy_consent"])

        reward_hsv = np.zeros((720, 1280, 3), dtype=np.uint8)
        reward_hsv[:, :, 2] = 70
        reward_hsv[550:590, 420:1200] = (12, 220, 220)
        for center_x in (510, 730, 1015, 1175):
            reward_hsv[475:570, center_x - 55:center_x + 55] = (15, 180, 180)
        reward = cv2.cvtColor(reward_hsv, cv2.COLOR_HSV2BGR)
        metrics = _naruto_visual_metrics(reward)
        self.assertTrue(metrics["reward_page"])
        self.assertTrue(metrics["all_chests_claimed"])

    def test_azur_lane_runner_waits_for_scheduler_idle(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "Alas.exe"; exe.write_bytes(b"")
            logs = root / "log"; logs.mkdir()
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            log = logs / "today_alas.txt"
            log.write_text("", encoding="utf-8")

            def write_log():
                time.sleep(0.18)
                log.write_text(
                    "Scheduler: Start task `Reward`\nScheduler: End task `Reward`\nNo task pending",
                    encoding="utf-8")

            threading.Thread(target=write_log, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "*_alas.txt"),
                    "timeout": 3, "completion_quiet_seconds": 0.05,
                    "log_retry_interval": 0.05,
                    "clean_existing": False, "close_on_complete": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners._click_alas_start_button",
                          return_value=(True, "clicked")) as click:
                result = run_alas_gui(step, ctx)
            self.assertTrue(result.success)
            self.assertGreaterEqual(click.call_count, 1)

    def test_azur_lane_human_takeover_stops_without_repeated_clicks(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "Alas.exe"; exe.write_bytes(b"")
            logs = root / "log"; logs.mkdir(); log = logs / "today_alas.txt"
            log.write_text("", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            def write_error():
                time.sleep(0.1)
                log.write_text('No emulator with serial "127.0.0.1:16384" found\n'
                               'Request human takeover\n', encoding="utf-8")

            threading.Thread(target=write_error, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "*_alas.txt"),
                    "timeout": 2, "log_retry_interval": 0.05,
                    "clean_existing": False, "close_on_complete": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners._click_alas_start_button",
                          return_value=(True, "clicked")) as click:
                result = run_alas_gui(step, ctx)
            self.assertFalse(result.success)
            self.assertIn("No emulator with serial", result.message)
            self.assertLessEqual(click.call_count, 2)

    def test_azur_lane_start_clicks_are_capped(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "Alas.exe"; exe.write_bytes(b"")
            logs = root / "log"; logs.mkdir(); (logs / "today_alas.txt").write_text("", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())
            step = {"executable": str(exe), "log_glob": str(logs / "*_alas.txt"),
                    "timeout": 2, "log_retry_interval": 0.05, "max_start_clicks": 3,
                    "startup_timeout": 1, "clean_existing": False, "close_on_complete": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners._click_alas_start_button",
                          return_value=(True, "clicked")) as click:
                result = run_alas_gui(step, ctx)
            self.assertFalse(result.success)
            self.assertEqual(click.call_count, 3)
            self.assertIn("3 次", result.message)

    def test_mumu_wait_accepts_already_started_instance(self):
        class Done:
            returncode = 0
            stdout = b'{"index":"0","is_android_started":true,"is_process_started":true}'

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); manager = root / "MuMuManager.exe"; manager.write_bytes(b"")
            (root / "adb.exe").write_bytes(b"")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())
            calls = []
            def command(args, timeout, cwd=None, env=None):
                calls.append(args)
                return Result(True, "ready", {"output": "1"})
            ctx.command = command
            with patch("gameflow.runners.subprocess.run", return_value=Done()):
                result = run_mumu_wait({"executable": str(manager), "instance": 0,
                                        "timeout": 1, "settle_seconds": 0}, ctx)
            self.assertTrue(result.success)
            self.assertTrue(result.details["adb_connected"])
            self.assertTrue(result.details["boot_completed"])
            self.assertTrue(any(call[1:3] == ["connect", "127.0.0.1:16384"] for call in calls))
            self.assertFalse(any("MuMuManager.exe" in call[0] and "adb" in call for call in calls))

    def test_gumballs_runner_clicks_and_completes_two_rounds(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "MFAAvalonia.exe"; exe.write_bytes(b"")
            logs = root / "logs"; logs.mkdir()
            log = logs / "log-test.log"
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            def write_rounds():
                time.sleep(0.1)
                log.write_text("用户操作：启动任务\n任务：WeeklyRaid 识别失败, 跳过该任务\n"
                               "任务已全部完成！\n", encoding="utf-8")
                time.sleep(1.2)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("用户操作：启动任务\n任务已全部完成！\n")

            threading.Thread(target=write_rounds, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "log-*.log"),
                    "initial_click_delay": 0, "between_round_delay": 0,
                    "round_start_timeout": 3, "timeout": 5,
                    "clean_existing": False, "close_on_complete": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners._click_gumballs_start_button",
                          return_value=(True, "clicked")) as click:
                result = run_gumballs_gui(step, ctx)
            self.assertTrue(result.success)
            self.assertEqual(click.call_count, 2)
            self.assertEqual(result.details["rounds"], 2)
            self.assertEqual(result.details["skipped_tasks"], ["WeeklyRaid"])

    def test_maaend_runner_waits_for_primary_daily_completion(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "MaaEnd.exe"; exe.write_bytes(b"")
            logs = root / "debug"; logs.mkdir()
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            def write_log():
                time.sleep(0.1)
                (logs / "2026-07-13-1.log").write_text(
                    "更新检查完成: 最新版本=v2.19.0, 有更新=false\n"
                    "实例 全套日常: 开始执行任务, 数量: 5, 分段: primary:4, trailing:1\n"
                    "实例 全套日常: 收尾段切换为 Dummy Controller\n"
                    "实例 全套日常: 任务已提交, task_ids: [1,2,3,4,5]\n", encoding="utf-8")

            threading.Thread(target=write_log, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "20??-??-??-*.log"),
                    "timeout": 3, "clean_existing": False, "close_on_complete": False,
                    "gui_click_fallback": False, "bring_game_to_front": True,
                    "game_process_image": "Endfield.exe", "game_title_contains": "Endfield",
                    "game_foreground_timeout": 0.1,
                    "screenshot_path": str(root / "endfield.png")}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners._bring_game_window_to_front",
                          return_value=Result(True, "foreground")) as foreground, \
                    patch("gameflow.runners.run_window_screenshot",
                          return_value=Result(True, "saved")) as screenshot:
                result = run_maaend_gui(step, ctx)
            self.assertTrue(result.success)
            self.assertEqual(result.message, "终末地每日任务正常结束")
            self.assertEqual(result.details["submitted_total"], 5)
            foreground.assert_called_once()
            screenshot.assert_called_once()

    def test_maaend_daily_error_becomes_abnormal_end(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "MaaEnd.exe"; exe.write_bytes(b"")
            logs = root / "debug"; logs.mkdir()
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            def write_log():
                time.sleep(0.05)
                (logs / "2026-07-15-2.log").write_text(
                    "实例 全套日常: 开始执行任务, 数量: 5, 分段: primary:4, trailing:1\n"
                    "实例 全套日常: 任务执行失败\n", encoding="utf-8")

            threading.Thread(target=write_log, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "20??-??-??-*.log"),
                    "timeout": 2, "clean_existing": False, "close_on_complete": False,
                    "gui_click_fallback": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()):
                result = run_maaend_gui(step, ctx)
            self.assertFalse(result.success)
            self.assertIn("异常结束", result.message)

    def test_maaend_framework_failure_cannot_be_reported_as_success(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "MaaEnd.exe"; exe.write_bytes(b"")
            logs = root / "debug"; logs.mkdir()
            high = logs / "2026-07-18-1.log"; high.write_text("", encoding="utf-8")
            framework = logs / "maafw.log"; framework.write_text("", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            def write_failure():
                time.sleep(0.1)
                high.write_text(
                    "实例 全套日常: 开始执行任务, 数量: 5, 分段: primary:4, trailing:1\n"
                    "实例 全套日常: 任务已提交, task_ids: [1,2,3,4,5]\n"
                    "实例 全套日常: 收尾段切换为 Dummy Controller\n", encoding="utf-8")
                framework.write_text(
                    '!!!OnEventNotify!!! [msg=Tasker.Task.Failed] '
                    '[details={"entry":"DailyRewardStart","task_id":4}]\n', encoding="utf-8")

            threading.Thread(target=write_failure, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "20??-??-??-*.log"),
                    "framework_log_path": str(framework), "timeout": 3,
                    "clean_existing": False, "close_on_complete": False,
                    "gui_click_fallback": False, "game_process_image": "Endfield.exe",
                    "screenshot_path": str(root / "endfield.png")}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners.run_window_screenshot",
                          return_value=Result(True, "saved")):
                result = run_maaend_gui(step, ctx)
            self.assertFalse(result.success)
            self.assertIn("DailyRewardStart", result.message)

    def test_maaend_update_marker_becomes_needs_update(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "MaaEnd.exe"; exe.write_bytes(b"")
            logs = root / "debug"; logs.mkdir()
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            def write_log():
                time.sleep(0.05)
                (logs / "2026-07-15-1.log").write_text(
                    "更新检查完成: 最新版本=v9.9.9, 有更新=true\n", encoding="utf-8")

            threading.Thread(target=write_log, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "20??-??-??-*.log"),
                    "timeout": 2, "clean_existing": False, "close_on_complete": False,
                    "gui_click_fallback": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()):
                result = run_maaend_gui(step, ctx)
            self.assertFalse(result.success)
            self.assertEqual(result.status, "needs_update")

    def test_engine_propagates_needs_update_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); store = Store(root / "db.sqlite")
            config = {"workflows": {"daily": {"steps": [
                {"id": "version", "runner": "fake_update"}
            ]}}}
            with patch.dict(RUNNERS, {"fake_update": lambda step, ctx: Result(
                    False, "游戏需要更新", {"marker": "update"}, "needs_update")}):
                engine = Engine(root, config, store)
                engine.start("daily")
                engine.join(2)
            self.assertEqual(engine.state()["last_status"], "needs_update")
            self.assertEqual(store.recent()[0]["status"], "needs_update")

    def test_log_gui_daily_retries_click_and_captures_reward(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "Launcher.exe"; exe.write_bytes(b"")
            log = root / "daily.log"; log.write_text("", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            def write_log():
                time.sleep(0.3)
                log.write_text("RUN START\nREWARD PAGE\nALL DONE\n", encoding="utf-8")

            threading.Thread(target=write_log, daemon=True).start()
            step = {"display_name": "测试每日", "executable": str(exe),
                    "log_glob": str(log), "process_images": ["Launcher.exe"],
                    "button_names": ["完整运行"], "title_hints": ["Test"],
                    "log_retry_interval": 0.05, "start_markers": ["RUN START"],
                    "completion_markers": ["ALL DONE"],
                    "screenshot_markers": ["REWARD PAGE"],
                    "screenshot_path": str(root / "reward.png"),
                    "bring_game_to_front": True, "game_process_image": "Game.exe",
                    "completion_quiet_seconds": 0.05,
                    "timeout": 2, "clean_existing": False, "close_on_complete": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners._click_named_gui_button",
                          return_value=(True, "clicked")) as click, \
                    patch("gameflow.runners._bring_game_window_to_front",
                          return_value=Result(True, "foreground")) as foreground, \
                    patch("gameflow.runners.run_window_screenshot",
                          return_value=Result(True, "saved")) as screenshot:
                result = run_log_gui_daily(step, ctx)
            self.assertTrue(result.success)
            self.assertGreaterEqual(click.call_count, 2)
            foreground.assert_called_once()
            screenshot.assert_called_once()

    def test_log_gui_daily_stalled_state_fails_with_current_screenshot(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            wrote = False
            def poll(self):
                if not self.wrote:
                    self.wrote = True
                    log.write_text(
                        "RUN START\n指令[ 出战 ] 节点 检测游戏窗口 -> 出战 返回状态 按钮-出战\n",
                        encoding="utf-8")
                return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "Launcher.exe"; exe.write_bytes(b"")
            log = root / "daily.log"; log.write_text("", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())
            step = {"display_name": "测试每日", "executable": str(exe),
                    "log_glob": str(log), "process_images": ["Launcher.exe"],
                    "start_markers": ["RUN START"], "log_retry_interval": 0.01,
                    "state_watchdog_regex": r"指令\[\s*([^\]]+)\s*\]\s*节点\s*(.*?)\s*->.*?返回状态\s*(.*)$",
                    "state_stall_seconds": 0.05, "state_recovery_seconds": 0.05,
                    "game_process_image": "Game.exe", "screenshot_path": str(root / "failure.png"),
                    "initial_click_delay": 10,
                    "timeout": 3, "clean_existing": False, "close_on_complete": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners._bring_game_window_to_front",
                          return_value=Result(True, "foreground")), \
                    patch("gameflow.runners.run_window_screenshot",
                          return_value=Result(True, "saved")) as screenshot:
                result = run_log_gui_daily(step, ctx)
            self.assertFalse(result.success)
            self.assertIn("按钮-出战", result.message)
            screenshot.assert_called()

    def test_exclusive_daily_group_never_runs_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); store = Store(root / "db.sqlite")
            config = {"workflows": {
                "endfield": {"exclusive_group": "pc_group", "steps": [
                    {"id": "endfield", "runner": "record"}]},
                "zenless": {"exclusive_group": "pc_group", "steps": [
                    {"id": "zenless", "runner": "record"}]},
                "starrail": {"exclusive_group": "pc_group", "steps": [
                    {"id": "starrail", "runner": "record"}]},
            }}
            intervals = {}

            def record(step, ctx):
                intervals[step["id"]] = [time.monotonic(), None]
                time.sleep(0.08)
                intervals[step["id"]][1] = time.monotonic()
                return Result(True, "done")

            with patch.dict(RUNNERS, {"record": record}):
                manager = WorkflowManager(root, config, store)
                ok, _ = manager.start_daily(["endfield", "zenless", "starrail"], 2)
                self.assertTrue(ok)
                manager.join(3)
            ordered = sorted(intervals.values(), key=lambda item: item[0])
            self.assertEqual(len(ordered), 3)
            self.assertTrue(all(ordered[index][1] <= ordered[index + 1][0]
                                for index in range(2)))

    def test_manual_start_respects_exclusive_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {"workflows": {
                "endfield": {"exclusive_group": "pc_group", "steps": [
                    {"id": "wait", "runner": "delay", "seconds": 0.3}]},
                "zenless": {"exclusive_group": "pc_group", "steps": [
                    {"id": "wait", "runner": "delay", "seconds": 0.01}]},
            }}
            manager = WorkflowManager(root, config, Store(root / "db.sqlite"))
            self.assertTrue(manager.start("endfield")[0])
            ok, message = manager.start("zenless")
            self.assertFalse(ok)
            self.assertIn("不能同时执行", message)
            manager.stop(); manager.join(2)

    def test_explicit_exclusive_with_is_symmetric(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {"workflows": {
                "alas": {"exclusive_with": ["naruto"], "steps": [
                    {"id": "wait", "runner": "delay", "seconds": 0.3}]},
                "naruto": {"steps": [
                    {"id": "wait", "runner": "delay", "seconds": 0.01}]},
            }}
            manager = WorkflowManager(root, config, Store(root / "db.sqlite"))
            self.assertTrue(manager.start("naruto")[0])
            ok, message = manager.start("alas")
            self.assertFalse(ok)
            self.assertIn("不能同时执行", message)
            manager.stop(); manager.join(2)

    def test_daily_batch_parallel_and_serial(self):
        def run_batch(parallel):
            tmp = tempfile.TemporaryDirectory()
            root = Path(tmp.name)
            config = {"workflows": {
                "a": {"steps": [{"id": "a", "runner": "delay", "seconds": 0.35}]},
                "b": {"steps": [{"id": "b", "runner": "delay", "seconds": 0.35}]}
            }}
            manager = WorkflowManager(root, config, Store(root / "db.sqlite"))
            started = time.monotonic()
            ok, _ = manager.start_daily(["a", "b"], parallel)
            self.assertTrue(ok)
            manager.join(5)
            elapsed = time.monotonic() - started
            completed = manager.state()["batch"]["completed"]
            tmp.cleanup()
            return elapsed, completed

        serial_time, serial_done = run_batch(1)
        parallel_time, parallel_done = run_batch(2)
        self.assertGreater(serial_time, 0.6)
        self.assertLess(parallel_time, serial_time * 0.8)
        self.assertEqual([x["workflow"] for x in serial_done], ["a", "b"])
        self.assertTrue(all(x["status"] == "success" for x in parallel_done))

    def test_daily_batch_selection_and_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {"workflows": {
                "a": {"steps": [{"id": "a", "runner": "delay", "seconds": 0.05}]},
                "b": {"steps": [{"id": "b", "runner": "delay", "seconds": 0.05}]},
                "self_test": {"steps": [{"id": "x", "runner": "delay", "seconds": 0.05}]}
            }}
            manager = WorkflowManager(root, config, Store(root / "db.sqlite"))
            ok, _ = manager.start_daily(["b", "self_test"], 9)
            self.assertTrue(ok)
            manager.join(5)
            batch = manager.state()["batch"]
            self.assertEqual(batch["max_parallel"], 1)
            self.assertEqual([x["workflow"] for x in batch["completed"]], ["b"])

    def test_email_report_prepares_fresh_screenshot_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shot = root / "shot.png"
            shot.write_bytes(b"png")
            config = {"email_report": {
                "enabled": True, "recipient": "test@example.com",
                "screenshots": {"game": str(shot)},
                "user_env": "TEST_SMTP_USER", "password_env": "TEST_SMTP_PASSWORD"
            }, "workflows": {"game": {"display_name": "Game"}}}
            with patch.dict("os.environ", {}, clear=True):
                ok, message = send_daily_screenshots(
                    root, config, ["game"], [{"workflow": "game", "status": "success"}], 0)
            self.assertFalse(ok)
            self.assertIn("尚未设置", message)


if __name__ == "__main__": unittest.main()
