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
                         "id=\"logView\"", "GameFlow æ¬¡å…ƒä½œæˆ˜ç»ˆç«¯", "class=\"gacha-card\"",
                         "UI_AvatarIcon_", "drawCharacter()"):
            self.assertIn(required, PAGE)
        self.assertIn("è‘¬é€çš„èŠ™è‰è²", PAGE)
        self.assertIn("æˆ‘æ¨çš„å­©å­", PAGE)
        self.assertIn("POOL 50", PAGE)
        self.assertIn("äº¤ç»™è¯ºè‰¾å°”å§ï¼Œæˆ‘ä¼šæŠŠæ¯ä¸€é¡¹éƒ½å¦¥å–„å®Œæˆã€‚", PAGE)
        self.assertNotIn("characteristicQuote", PAGE)
        self.assertNotIn("loadExpandedCharacterPool", PAGE)
        self.assertNotIn("æˆ‘çš„è‹±é›„å­¦é™¢", PAGE)
        self.assertIn("mousedown", PAGE)
        self.assertIn("mousemove", PAGE)
        self.assertIn("drop-before", PAGE)
        self.assertIn("æŒ‰ä½æ‹–æ‹½è°ƒæ•´é¡ºåº", PAGE)
        self.assertNotIn("æ«åŸä¸‡å¶", PAGE)

    def test_windows_command_output_decoding(self):
        message = "æˆåŠŸ: å·²ç»ˆæ­¢è¿›ç¨‹ã€‚"
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
        self.assertIn("æ‰§è¡Œè¶…æ—¶", result.message)
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
                         ["å¯åŠ¨ä¸€æ¡é¾™"])
        self.assertEqual(config["workflows"]["star_rail_daily"]["steps"][0]["button_names"],
                         ["å®Œæ•´è¿è¡Œ"])

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
                    "æ¨¡æ‹Ÿå™¨è¿æ¥æˆåŠŸ\nå¼€å§‹æ‰§è¡Œã€å·¥ä½œä»»åŠ¡ã€‘\nä»»åŠ¡å…¨éƒ¨æ‰§è¡ŒæˆåŠŸ", encoding="utf-8")

            threading.Thread(target=write_marker, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "*_baas1.log"),
                    "completion_marker": "ä»»åŠ¡å…¨éƒ¨æ‰§è¡ŒæˆåŠŸ", "timeout": 3,
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
                    "æ¨¡æ‹Ÿå™¨è¿æ¥æˆåŠŸ\nå¼€å§‹æ‰§è¡Œã€é‡å¯è®¾ç½®ã€‘\næ‰§è¡Œå®Œæˆã€é‡å¯è®¾ç½®ã€‘\n"
                    "ä»»åŠ¡å…¨éƒ¨æ‰§è¡ŒæˆåŠŸ\n", encoding="utf-8")
                time.sleep(1.3)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("å¼€å§‹æ‰§è¡Œã€å·¥ä½œä»»åŠ¡ã€‘\næ‰§è¡Œå®Œæˆã€å·¥ä½œä»»åŠ¡ã€‘\n")

            threading.Thread(target=write_tasks, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "*_baas1.log"),
                    "completion_marker": "ä»»åŠ¡å…¨éƒ¨æ‰§è¡ŒæˆåŠŸ", "required_last_task": "å·¥ä½œä»»åŠ¡",
                    "required_task_check_interval": 0.2, "completion_quiet_seconds": 0.1,
                    "timeout": 5, "clean_existing": False, "close_on_complete": False,
                    "gui_click_fallback": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()):
                result = run_baas_gui(step, ctx)
            self.assertTrue(result.success)
            self.assertEqual(result.details["last_completed_task"], "å·¥ä½œä»»åŠ¡")
            self.assertTrue(any("ä¸æ˜¯â€œå·¥ä½œä»»åŠ¡â€" in message for message in messages))

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
                               "RestartTaskException: ATXå¡æ­»ï¼Œé‡å¯ä»»åŠ¡\n", encoding="utf-8")
                time.sleep(1.1)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("æ¨¡æ‹Ÿå™¨è¿æ¥æˆåŠŸ\nå¼€å§‹æ‰§è¡Œã€å·¥ä½œä»»åŠ¡ã€‘\n"
                                 "æ‰§è¡Œå®Œæˆã€å·¥ä½œä»»åŠ¡ã€‘\nä»»åŠ¡å…¨éƒ¨æ‰§è¡ŒæˆåŠŸ\n")

            threading.Thread(target=write_recovery, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "*_baas1.log"),
                    "completion_quiet_seconds": 0.05, "recoverable_retry_seconds": 0.05,
                    "timeout": 4, "clean_existing": False, "close_on_complete": False,
                    "gui_click_fallback": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()):
                result = run_baas_gui(step, ctx)
            self.assertTrue(result.success)
            self.assertEqual(result.details["recoverable_events"], 1)
            self.assertTrue(any("å¯æ¢å¤å¼‚å¸¸" in message for message in messages))

    def test_baas_restarts_offline_emulator_and_resumes(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "baas.exe"; exe.write_bytes(b"")
            logs = root / "logs"; logs.mkdir(); log = logs / "today_baas1.log"
        ç¯v¶‰ËkºwµçP (€€€€€€€€€€€€€€€€€€€€‹–º{’ú,ƒ–£––_š^—–âàèƒ–ò–/š&Ÿ¢†3’îï–*„°ƒšVÃ¦<è€Ô°ƒ–"šºÔèÁÉ¥µ…ÉäèĞ°ÑÉ…¥±¥¹œèÅq¸ˆ(€€€€€€€€€€€€€€€€€€€€‹–º{’ú,ƒ–£––_š^—–âàèƒ’îï–*‡š&Ÿ¢†3–’Ç¢Ò•q¸ˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ¤((€€€€€€€€€€€Ñ¡É•…‘¥¹œ¹Q¡É•…¡Ñ…É•ĞõİÉ¥Ñ•}±½œ°‘…•µ½¸õQÉÕ”¤¹ÍÑ…ÉĞ ¤(€€€€€€€€€€€ÍÑ•À€ôì‰•á•ÕÑ…‰±”ˆèÍÑÈ¡•á”¤°€‰±½}±½ˆˆèÍÑÈ¡±½Ì€¼€ˆÈÀüü´üü´üü´¨¹±½œˆ¤°(€€€€€€€€€€€€€€€€€€€€‰Ñ¥µ•½ÕĞˆè€È°€‰±•…¹}•á¥ÍÑ¥¹œˆè…±Í”°€‰±½Í•}½¹}½µÁ±•Ñ”ˆè…±Í”°(€€€€€€€€€€€€€€€€€€€€‰Õ¥}±¥­}™…±±‰…¬ˆè…±Í•ô(€€€€€€€€€€€İ¥Ñ Á…Ñ  ‰…µ•™±½Ü¹ÉÕ¹¹•ÉÌ¹ÍÕ‰ÁÉ½•ÍÌ¹A½Á•¸ˆ°É•ÑÕÉ¹}Ù…±Õ”õ…­•AÉ½•ÍÌ ¤¤è(€€€€€€€€€€€€€€€É•ÍÕ±Ğ€ôÉÕ¹}µ……•¹‘}Õ¤¡ÍÑ•À°Ñà¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑ…±Í”¡É•ÍÕ±Ğ¹ÍÕ•ÍÌ¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑ%¸ ‹–ò–âãîOšv|ˆ°É•ÍÕ±Ğ¹µ•ÍÍ…”¤((€€€‘•˜Ñ•ÍÑ}µ……•¹‘}™É…µ•İ½É­}™…¥±ÕÉ•}…¹¹½Ñ}‰•}É•Á½ÉÑ•‘}…Í}ÍÕ•ÍÌ¡Í•±˜¤è(€€€€€€€±…ÍÌ…­•AÉ½•ÍÌè(€€€€€€€€€€€Á¥€ô€ÄÈÌĞÔØÜàä(€€€€€€€€€€€É•ÑÕÉ¹½‘”€ô9½¹”(€€€€€€€€€€€‘•˜Á½±°¡Í•±˜¤èÉ•ÑÕÉ¸9½¹”(€€€€€€€€€€€‘•˜Ñ•Éµ¥¹…Ñ”¡Í•±˜¤èÍ•±˜¹É•ÑÕÉ¹½‘”€ô€À((€€€€€€€İ¥Ñ Ñ•µÁ™¥±”¹Q•µÁ½É…Éå¥É•Ñ½Éä ¤…ÌÑµÀè(€€€€€€€€€€€É½½Ğ€ôA…Ñ ¡ÑµÀ¤ì•á”€ôÉ½½Ğ€¼€‰5……¹¹•á”ˆì•á”¹İÉ¥Ñ•}‰åÑ•Ì¡ˆˆˆ¤(€€€€€€€€€€€±½Ì€ôÉ½½Ğ€¼€‰‘•‰Õœˆì±½Ì¹µ­‘¥È ¤(€€€€€€€€€€€¡¥ €ô±½Ì€¼€ˆÈÀÈØ´ÀÜ´Äà´Ä¹±½œˆì¡¥ ¹İÉ¥Ñ•}Ñ•áĞ ˆˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ¤(€€€€€€€€€€€™É…µ•İ½É¬€ô±½Ì€¼€‰µ……™Ü¹±½œˆì™É…µ•İ½É¬¹İÉ¥Ñ•}Ñ•áĞ ˆˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ¤(€€€€€€€€€€€Ñà€ôIÕ¹½¹Ñ•áĞ¡É½½Ğ°íô°±…µ‰‘„|è9½¹”°Ñ¡É•…‘¥¹œ¹Ù•¹Ğ ¤¤((€€€€€€€€€€€‘•˜İÉ¥Ñ•}™…¥±ÕÉ” ¤è(€€€€€€€€€€€€€€€Ñ¥µ”¹Í±••À À¸Ä¤(€€€€€€€€€€€€€€€¡¥ ¹İÉ¥Ñ•}Ñ•áĞ (€€€€€€€€€€€€€€€€€€€€‹–º{’ú,ƒ–£––_š^—–âàèƒ–ò–/š&Ÿ¢†3’îï–*„°ƒšVÃ¦<è€Ô°ƒ–"šºÔèÁÉ¥µ…ÉäèĞ°ÑÉ…¥±¥¹œèÅq¸ˆ(€€€€€€€€€€€€€€€€€€€€‹–º{’ú,ƒ–£––_š^—–âàèƒ’îï–*‡–ŞËš>C’ê°Ñ…Í­}¥‘ÌèlÄ°È°Ì°Ğ°Õuq¸ˆ(€€€€€€€€€€€€€€€€€€€€‹–º{’ú,ƒ–£––_š^—–âàèƒšRÛ–Âûšº×–"š6‹’âèÕµµä½¹ÑÉ½±±•Éq¸ˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ¤(€€€€€€€€€€€€€€€™É…µ•İ½É¬¹İÉ¥Ñ•}Ñ•áĞ (€€€€€€€€€€€€€€€€€€€€œ„„…=¹Ù•¹Ñ9½Ñ¥™ä„„„mµÍœõQ…Í­•È¹Q…Í¬¹…¥±•‘t€œ(€€€€€€€€€€€€€€€€€€€€m‘•Ñ…¥±Ìõì‰•¹ÑÉäˆè‰…¥±åI•İ…É‘MÑ…ÉĞˆ°‰Ñ…Í­}¥ˆèÑõuq¸œ°•¹½‘¥¹œô‰ÕÑ˜´àˆ¤((€€€€€€€€€€€Ñ¡É•…‘¥¹œ¹Q¡É•…¡Ñ…É•ĞõİÉ¥Ñ•}™…¥±ÕÉ”°‘…•µ½¸õQÉÕ”¤¹ÍÑ…ÉĞ ¤(€€€€€€€€€€€ÍÑ•À€ôì‰•á•ÕÑ…‰±”ˆèÍÑÈ¡•á”¤°€‰±½}±½ˆˆèÍÑÈ¡±½Ì€¼€ˆÈÀüü´üü´üü´¨¹±½œˆ¤°(€€€€€€€€€€€€€€€€€€€€‰™É…µ•İ½É­}±½}Á…Ñ ˆèÍÑÈ¡™É…µ•İ½É¬¤°€‰Ñ¥µ•½ÕĞˆè€Ì°(€€€€€€€€€€€€€€€€€€€€‰±•…¹}•á¥ÍÑ¥¹œˆè…±Í”°€‰±½Í•}½¹}½µÁ±•Ñ”ˆè…±Í”°(€€€€€€€€€€€€€€€€€€€€‰Õ¥}±¥­}™…±±‰…¬ˆè…±Í”°€‰…µ•}ÁÉ½•ÍÍ}¥µ…”ˆè€‰¹‘™¥•±¹•á”ˆ°(€€€€€€€€€€€€€€€€€€€€‰ÍÉ••¹Í¡½Ñ}Á…Ñ ˆèÍÑÈ¡É½½Ğ€¼€‰•¹‘™¥•±¹Á¹œˆ¥ô(€€€€€€€€€€€İ¥Ñ Á…Ñ  ‰…µ•™±½Ü¹ÉÕ¹¹•ÉÌ¹ÍÕ‰ÁÉ½•ÍÌ¹A½Á•¸ˆ°É•ÑÕÉ¹}Ù…±Õ”õ…­•AÉ½•ÍÌ ¤¤°p(€€€€€€€€€€€€€€€€€€€Á…Ñ  ‰…µ•™±½Ü¹ÉÕ¹¹•ÉÌ¹ÉÕ¹}İ¥¹‘½İ}ÍÉ••¹Í¡½Ğˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¹}Ù…±Õ”õI•ÍÕ±Ğ¡QÉÕ”°€‰Í…Ù•ˆ¤¤è(€€€€€€€€€€€€€€€É•ÍÕ±Ğ€ôÉÕ¹}µ……•¹‘}Õ¤¡ÍÑ•À°Ñà¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑ…±Í”¡É•ÍÕ±Ğ¹ÍÕ•ÍÌ¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑ%¸ ‰…¥±åI•İ…É‘MÑ…ÉĞˆ°É•ÍÕ±Ğ¹µ•ÍÍ…”¤((€€€‘•˜Ñ•ÍÑ}µ……•¹‘}ÕÁ‘…Ñ•}µ…É­•É}‰•½µ•Í}¹••‘Í}ÕÁ‘…Ñ”¡Í•±˜¤è(€€€€€€€±…ÍÌ…­•AÉ½•ÍÌè(€€€€€€€€€€€Á¥€ô€ÄÈÌĞÔØÜàä(€€€€€€€€€€€É•ÑÕÉ¹½‘”€ô9½¹”(€€€€€€€€€€€‘•˜Á½±°¡Í•±˜¤èÉ•ÑÕÉ¸9½¹”(€€€€€€€€€€€‘•˜Ñ•Éµ¥¹…Ñ”¡Í•±˜¤èÍ•±˜¹É•ÑÕÉ¹½‘”€ô€À((€€€€€€€İ¥Ñ Ñ•µÁ™¥±”¹Q•µÁ½É…Éå¥É•Ñ½Éä ¤…ÌÑµÀè(€€€€€€€€€€€É½½Ğ€ôA…Ñ ¡ÑµÀ¤ì•á”€ôÉ½½Ğ€¼€‰5……¹¹•á”ˆì•á”¹İÉ¥Ñ•}‰åÑ•Ì¡ˆˆˆ¤(€€€€€€€€€€€±½Ì€ôÉ½½Ğ€¼€‰‘•‰Õœˆì±½Ì¹µ­‘¥È ¤(€€€€€€€€€€€Ñà€ôIÕ¹½¹Ñ•áĞ¡É½½Ğ°íô°±…µ‰‘„|è9½¹”°Ñ¡É•…‘¥¹œ¹Ù•¹Ğ ¤¤((€€€€€€€€€€€‘•˜İÉ¥Ñ•}±½œ ¤è(€€€€€€€€€€€€€€€Ñ¥µ”¹Í±••À À¸ÀÔ¤(€€€€€€€€€€€€€€€€¡±½Ì€¼€ˆÈÀÈØ´ÀÜ´ÄÔ´Ä¹±½œˆ¤¹İÉ¥Ñ•}Ñ•áĞ (€€€€€€€€€€€€€€€€€€€€‹šnÓšZÃšš~—–º3š"@èƒšršZÃ&#šr°õØä¸ä¸ä°ƒšr'šnÓšZÀõÑÉÕ•q¸ˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ¤((€€€€€€€€€€€Ñ¡É•…‘¥¹œ¹Q¡É•…¡Ñ…É•ĞõİÉ¥Ñ•}±½œ°‘…•µ½¸õQÉÕ”¤¹ÍÑ…ÉĞ ¤(€€€€€€€€€€€ÍÑ•À€ôì‰•á•ÕÑ…‰±”ˆèÍÑÈ¡•á”¤°€‰±½}±½ˆˆèÍÑÈ¡±½Ì€¼€ˆÈÀüü´üü´üü´¨¹±½œˆ¤°(€€€€€€€€€€€€€€€€€€€€‰Ñ¥µ•½ÕĞˆè€È°€‰±•…¹}•á¥ÍÑ¥¹œˆè…±Í”°€‰±½Í•}½¹}½µÁ±•Ñ”ˆè…±Í”°(€€€€€€€€€€€€€€€€€€€€‰Õ¥}±¥­}™…±±‰…¬ˆè…±Í•ô(€€€€€€€€€€€İ¥Ñ Á…Ñ  ‰…µ•™±½Ü¹ÉÕ¹¹•ÉÌ¹ÍÕ‰ÁÉ½•ÍÌ¹A½Á•¸ˆ°É•ÑÕÉ¹}Ù…±Õ”õ…­•AÉ½•ÍÌ ¤¤è(€€€€€€€€€€€€€€€É•ÍÕ±Ğ€ôÉÕ¹}µ……•¹‘}Õ¤¡ÍÑ•À°Ñà¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑ…±Í”¡É•ÍÕ±Ğ¹ÍÕ•ÍÌ¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡É•ÍÕ±Ğ¹ÍÑ…ÑÕÌ°€‰¹••‘Í}ÕÁ‘…Ñ”ˆ¤((€€€‘•˜Ñ•ÍÑ}•¹¥¹•}ÁÉ½Á……Ñ•Í}¹••‘Í}ÕÁ‘…Ñ•}ÍÑ…ÑÕÌ¡Í•±˜¤è(€€€€€€€İ¥Ñ Ñ•µÁ™¥±”¹Q•µÁ½É…Éå¥É•Ñ½Éä ¤…ÌÑµÀè(€€€€€€€€€€€É½½Ğ€ôA…Ñ ¡ÑµÀ¤ìÍÑ½É”€ôMÑ½É”¡É½½Ğ€¼€‰‘ˆ¹ÍÅ±¥Ñ”ˆ¤(€€€€€€€€€€€½¹™¥œ€ôì‰İ½É­™±½İÌˆèì‰‘…¥±äˆèì‰ÍÑ•ÁÌˆèl(€€€€€€€€€€€€€€€ì‰¥ˆè€‰Ù•ÉÍ¥½¸ˆ°€‰ÉÕ¹¹•Èˆè€‰™…­•}ÕÁ‘…Ñ”‰ô(€€€€€€€€€€€uõõô(€€€€€€€€€€€İ¥Ñ Á…Ñ ¹‘¥Ğ¡IU99IL°ì‰™…­•}ÕÁ‘…Ñ”ˆè±…µ‰‘„ÍÑ•À°ÑàèI•ÍÕ±Ğ (€€€€€€€€€€€€€€€€€€€…±Í”°€‹šâãš"?¦r¢ššnÓšZÀˆ°ì‰µ…É­•Èˆè€‰ÕÁ‘…Ñ”‰ô°€‰¹••‘Í}ÕÁ‘…Ñ”ˆ¥ô¤è(€€€€€€€€€€€€€€€•¹¥¹”€ô¹¥¹”¡É½½Ğ°½¹™¥œ°ÍÑ½É”¤(€€€€€€€€€€€€€€€•¹¥¹”¹ÍÑ…ÉĞ ‰‘…¥±äˆ¤(€€€€€€€€€€€€€€€•¹¥¹”¹©½¥¸ È¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡•¹¥¹”¹ÍÑ…Ñ” ¥l‰±…ÍÑ}ÍÑ…ÑÕÌ‰t°€‰¹••‘Í}ÕÁ‘…Ñ”ˆ¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡ÍÑ½É”¹É••¹Ğ ¥lÁul‰ÍÑ…ÑÕÌ‰t°€‰¹••‘Í}ÕÁ‘…Ñ”ˆ¤((€€€‘•˜Ñ•ÍÑ}±½}Õ¥}‘…¥±å}É•ÑÉ¥•Í}±¥­}…¹‘}…ÁÑÕÉ•Í}É•İ…É¡Í•±˜¤è(€€€€€€€±…ÍÌ…­•AÉ½•ÍÌè(€€€€€€€€€€€Á¥€ô€ÄÈÌĞÔØÜàä(€€€€€€€€€€€É•ÑÕÉ¹½‘”€ô9½¹”(€€€€€€€€€€€‘•˜Á½±°¡Í•±˜¤èÉ•ÑÕÉ¸9½¹”(€€€€€€€€€€€‘•˜Ñ•Éµ¥¹…Ñ”¡Í•±˜¤èÍ•±˜¹É•ÑÕÉ¹½‘”€ô€À((€€€€€€€İ¥Ñ Ñ•µÁ™¥±”¹Q•µÁ½É…Éå¥É•Ñ½Éä ¤…ÌÑµÀè(€€€€€€€€€€€É½½Ğ€ôA…Ñ ¡ÑµÀ¤ì•á”€ôÉ½½Ğ€¼€‰1…Õ¹¡•È¹•á”ˆì•á”¹İÉ¥Ñ•}‰åÑ•Ì¡ˆˆˆ¤(€€€€€€€€€€€±½œ€ôÉ½½Ğ€¼€‰‘…¥±ä¹±½œˆì±½œ¹İÉ¥Ñ•}Ñ•áĞ ˆˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ¤(€€€€€€€€€€€Ñà€ôIÕ¹½¹Ñ•áĞ¡É½½Ğ°íô°±…µ‰‘„|è9½¹”°Ñ¡É•…‘¥¹œ¹Ù•¹Ğ ¤¤((€€€€€€€€€€€‘•˜İÉ¥Ñ•}±½œ ¤è(€€€€€€€€€€€€€€€Ñ¥µ”¹Í±••À À¸Ì¤(€€€€€€€€€€€€€€€±½œ¹İÉ¥Ñ•}Ñ•áĞ ‰IU8MQIQq¹I]IAq¹10=9q¸ˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ¤((€€€€€€€€€€€Ñ¡É•…‘¥¹œ¹Q¡É•…¡Ñ…É•ĞõİÉ¥Ñ•}±½œ°‘…•µ½¸õQÉÕ”¤¹ÍÑ…ÉĞ ¤(€€€€€€€€€€€ÍÑ•À€ôì‰‘¥ÍÁ±…å}¹…µ”ˆè€‹šÖ/¢¾Wš¾?š^”ˆ°€‰•á•ÕÑ…‰±”ˆèÍÑÈ¡•á”¤°(€€€€€€€€€€€€€€€€€€€€‰±½}±½ˆˆèÍÑÈ¡±½œ¤°€‰ÁÉ½•ÍÍ}¥µ…•Ìˆèl‰1…Õ¹¡•È¹•á”‰t°(€€€€€€€€€€€€€€€€€€€€‰‰ÕÑÑ½¹}¹…µ•Ìˆèl‹–º3šVÓ¢şC¢†0‰t°€‰Ñ¥Ñ±•}¡¥¹ÑÌˆèl‰Q•ÍĞ‰t°(€€€€€€€€€€€€€€€€€€€€‰±½}É•ÑÉå}¥¹Ñ•ÉÙ…°ˆè€À¸ÀÔ°€‰ÍÑ…ÉÑ}µ…É­•ÉÌˆèl‰IU8MQIP‰t°(€€€€€€€€€€€€€€€€€€€€‰½µÁ±•Ñ¥½¹}µ…É­•ÉÌˆèl‰10=9‰t°(€€€€€€€€€€€€€€€€€€€€‰ÍÉ••¹Í¡½Ñ}µ…É­•ÉÌˆèl‰I]IA‰t°(€€€€€€€€€€€€€€€€€€€€‰ÍÉ••¹Í¡½Ñ}Á…Ñ ˆèÍÑÈ¡É½½Ğ€¼€‰É•İ…É¹Á¹œˆ¤°(€€€€€€€€€€€€€€€€€€€€‰‰É¥¹}…µ•}Ñ½}™É½¹ĞˆèQÉÕ”°€‰…µ•}ÁÉ½•ÍÍ}¥µ…”ˆè€‰…µ”¹•á”ˆ°(€€€€€€€€€€€€€€€€€€€€‰½µÁ±•Ñ¥½¹}ÅÕ¥•Ñ}Í•½¹‘Ìˆè€À¸ÀÔ°(€€€€€€€€€€€€€€€€€€€€‰Ñ¥µ•½ÕĞˆè€È°€‰±•…¹}•á¥ÍÑ¥¹œˆè…±Í”°€‰±½Í•}½¹}½µÁ±•Ñ”ˆè…±Í•ô(€€€€€€€€€€€İ¥Ñ Á…Ñ  ‰…µ•™±½Ü¹ÉÕ¹¹•ÉÌ¹ÍÕ‰ÁÉ½•ÍÌ¹A½Á•¸ˆ°É•ÑÕÉ¹}Ù…±Õ”õ…­•AÉ½•ÍÌ ¤¤°p(€€€€€€€€€€€€€€€€€€€Á…Ñ  ‰…µ•™±½Ü¹ÉÕ¹¹•ÉÌ¹}±¥­}¹…µ•‘}Õ¥}‰ÕÑÑ½¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¹}Ù…±Õ”ô¡QÉÕ”°€‰±¥­•ˆ¤¤…Ì±¥¬°p(€€€€€€€€€€€€€€€€€€€Á…Ñ  ‰…µ•™±½Ü¹ÉÕ¹¹•ÉÌ¹}‰É¥¹}…µ•}İ¥¹‘½İ}Ñ½}™É½¹Ğˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¹}Ù…±Õ”õI•ÍÕ±Ğ¡QÉÕ”°€‰™½É•É½Õ¹ˆ¤¤…Ì™½É•É½Õ¹°p(€€€€€€€€€€€€€€€€€€€Á…Ñ  ‰…µ•™±½Ü¹ÉÕ¹¹•ÉÌ¹ÉÕ¹}İ¥¹‘½İ}ÍÉ••¹Í¡½Ğˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¹}Ù…±Õ”õI•ÍÕ±Ğ¡QÉÕ”°€‰Í…Ù•ˆ¤¤…ÌÍÉ••¹Í¡½Ğè(€€€€€€€€€€€€€€€É•ÍÕ±Ğ€ôÉÕ¹}±½}Õ¥}‘…¥±ä¡ÍÑ•À°Ñà¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡É•ÍÕ±Ğ¹ÍÕ•ÍÌ¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÉ•…Ñ•ÉÅÕ…°¡±¥¬¹…±±}½Õ¹Ğ°€È¤(€€€€€€€€€€€™½É•É½Õ¹¹…ÍÍ•ÉÑ}…±±•‘}½¹” ¤(€€€€€€€€€€€ÍÉ••¹Í¡½Ğ¹…ÍÍ•ÉÑ}…±±•‘}½¹” ¤((€€€‘•˜Ñ•ÍÑ}±½}Õ¥}‘…¥±å}ÍÑ…±±•‘}ÍÑ…Ñ•}™…¥±Í}İ¥Ñ¡}ÕÉÉ•¹Ñ}ÍÉ••¹Í¡½Ğ¡Í•±˜¤è(€€€€€€€±…ÍÌ…­•AÉ½•ÍÌè(€€€€€€€€€€€Á¥€ô€ÄÈÌĞÔØÜàä(€€€€€€€€€€€É•ÑÕÉ¹½‘”€ô9½¹”(€€€€€€€€€€€İÉ½Ñ”€ô…±Í”(€€€€€€€€€€€‘•˜Á½±°¡Í•±˜¤è(€€€€€€€€€€€€€€€¥˜¹½ĞÍ•±˜¹İÉ½Ñ”è(€€€€€€€€€€€€€€€€€€€Í•±˜¹İÉ½Ñ”€ôQÉÕ”(€€€€€€€€€€€€€€€€€€€±½œ¹İÉ¥Ñ•}Ñ•áĞ (€€€€€€€€€€€€€€€€€€€€€€€€‰IU8MQIQq»š2’î‘lƒ–ëš"`tƒ¢*
äƒššÖ/šâãš"?ª_–>Œ€´øƒ–ëš"`ƒ¢şS–n{*Ûšƒš2'¦J¸·–ëš"aq¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€•¹½‘¥¹œô‰ÕÑ˜´àˆ¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€€€€€‘•˜Ñ•Éµ¥¹…Ñ”¡Í•±˜¤èÍ•±˜¹É•ÑÕÉ¹½‘”€ô€À((€€€€€€€İ¥Ñ Ñ•µÁ™¥±”¹Q•µÁ½É…Éå¥É•Ñ½Éä ¤…ÌÑµÀè(€€€€€€€€€€€É½½Ğ€ôA…Ñ ¡ÑµÀ¤ì•á”€ôÉ½½Ğ€¼€‰1…Õ¹¡•È¹•á”ˆì•á”¹İÉ¥Ñ•}‰åÑ•Ì¡ˆˆˆ¤(€€€€€€€€€€€±½œ€ôÉ½½Ğ€¼€‰‘…¥±ä¹±½œˆì±½œ¹İÉ¥Ñ•}Ñ•áĞ ˆˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ¤(€€€€€€€€€€€Ñà€ôIÕ¹½¹Ñ•áĞ¡É½½Ğ°íô°±…µ‰‘„|è9½¹”°Ñ¡É•…‘¥¹œ¹Ù•¹Ğ ¤¤(€€€€€€€€€€€ÍÑ•À€ôì‰‘¥ÍÁ±…å}¹…µ”ˆè€‹šÖ/¢¾Wš¾?š^”ˆ°€‰•á•ÕÑ…‰±”ˆèÍÑÈ¡•á”¤°(€€€€€€€€€€€€€€€€€€€€‰±½}±½ˆˆèÍÑÈ¡±½œ¤°€‰ÁÉ½•ÍÍ}¥µ…•Ìˆèl‰1…Õ¹¡•È¹•á”‰t°(€€€€€€€€€€€€€€€€€€€€‰ÍÑ…ÉÑ}µ…É­•ÉÌˆèl‰IU8MQIP‰t°€‰±½}É•ÑÉå}¥¹Ñ•ÉÙ…°ˆè€À¸ÀÄ°(€€€€€€€€€€€€€€€€€€€€‰ÍÑ…Ñ•}İ…Ñ¡‘½}É••àˆèÈ‹š2’î‘qmqÌ¨¡myqut¬¥qÌ©quqÌ«¢*
åqÌ¨ ¸¨ü¥qÌ¨´ø¸¨ÿ¢şS–n{*ÛšqÌ¨ ¸¨¤ˆ°(€€€€€€€€€€€€€€€€€€€€‰ÍÑ…Ñ•}ÍÑ…±±}Í•½¹‘Ìˆè€À¸ÀÔ°€‰ÍÑ…Ñ•}É•½Ù•Éå}Í•½¹‘Ìˆè€À¸ÀÔ°(€€€€€€€€€€€€€€€€€€€€‰…µ•}ÁÉ½•ÍÍ}¥µ…”ˆè€‰…µ”¹•á”ˆ°€‰ÍÉ••¹Í¡½Ñ}Á…Ñ ˆèÍÑÈ¡É½½Ğ€¼€‰™…¥±ÕÉ”¹Á¹œˆ¤°(€€€€€€€€€€€€€€€€€€€€‰¥¹¥Ñ¥…±}±¥­}‘•±…äˆè€ÄÀ°(€€€€€€€€€€€€€€€€€€€€‰Ñ¥µ•½ÕĞˆè€Ì°€‰±•…¹}•á¥ÍÑ¥¹œˆè…±Í”°€‰±½Í•}½¹}½µÁ±•Ñ”ˆè…±Í•ô(€€€€€€€€€€€İ¥Ñ Á…Ñ  ‰…µ•™±½Ü¹ÉÕ¹¹•ÉÌ¹ÍÕ‰ÁÉ½•ÍÌ¹A½Á•¸ˆ°É•ÑÕÉ¹}Ù…±Õ”õ…­•AÉ½•ÍÌ ¤¤°p(€€€€€€€€€€€€€€€€€€€Á…Ñ  ‰…µ•™±½Ü¹ÉÕ¹¹•ÉÌ¹}‰É¥¹}…µ•}İ¥¹‘½İ}Ñ½}™É½¹Ğˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¹}Ù…±Õ”õI•ÍÕ±Ğ¡QÉÕ”°€‰™½É•É½Õ¹ˆ¤¤°p(€€€€€€€€€€€€€€€€€€€Á…Ñ  ‰…µ•™±½Ü¹ÉÕ¹¹•ÉÌ¹ÉÕ¹}İ¥¹‘½İ}ÍÉ••¹Í¡½Ğˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¹}Ù…±Õ”õI•ÍÕ±Ğ¡QÉÕ”°€‰Í…Ù•ˆ¤¤…ÌÍÉ••¹Í¡½Ğè(€€€€€€€€€€€€€€€É•ÍÕ±Ğ€ôÉÕ¹}±½}Õ¥}‘…¥±ä¡ÍÑ•À°Ñà¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑ…±Í”¡É•ÍÕ±Ğ¹ÍÕ•ÍÌ¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑ%¸ ‹š2'¦J¸·–ëš"`ˆ°É•ÍÕ±Ğ¹µ•ÍÍ…”¤(€€€€€€€€€€€ÍÉ••¹Í¡½Ğ¹…ÍÍ•ÉÑ}…±±• ¤((€€€‘•˜Ñ•ÍÑ}•á±ÕÍ¥Ù•}‘…¥±å}É½ÕÁ}¹•Ù•É}ÉÕ¹Í}Ñ½•Ñ¡•È¡Í•±˜¤è(€€€€€€€İ¥Ñ Ñ•µÁ™¥±”¹Q•µÁ½É…Éå¥É•Ñ½Éä ¤…ÌÑµÀè(€€€€€€€€€€€É½½Ğ€ôA…Ñ ¡ÑµÀ¤ìÍÑ½É”€ôMÑ½É”¡É½½Ğ€¼€‰‘ˆ¹ÍÅ±¥Ñ”ˆ¤(€€€€€€€€€€€½¹™¥œ€ôì‰İ½É­™±½İÌˆèì(€€€€€€€€€€€€€€€€‰•¹‘™¥•±ˆèì‰•á±ÕÍ¥Ù•}É½ÕÀˆè€‰Á}É½ÕÀˆ°€‰ÍÑ•ÁÌˆèl(€€€€€€€€€€€€€€€€€€€ì‰¥ˆè€‰•¹‘™¥•±ˆ°€‰ÉÕ¹¹•Èˆè€‰É•½É‰õuô°(€€€€€€€€€€€€€€€€‰é•¹±•ÍÌˆèì‰•á±ÕÍ¥Ù•}É½ÕÀˆè€‰Á}É½ÕÀˆ°€‰ÍÑ•ÁÌˆèl(€€€€€€€€€€€€€€€€€€€ì‰¥ˆè€‰é•¹±•ÍÌˆ°€‰ÉÕ¹¹•Èˆè€‰É•½É‰õuô°(€€€€€€€€€€€€€€€€‰ÍÑ…ÉÉ…¥°ˆèì‰•á±ÕÍ¥Ù•}É½ÕÀˆè€‰Á}É½ÕÀˆ°€‰ÍÑ•ÁÌˆèl(€€€€€€€€€€€€€€€€€€€ì‰¥ˆè€‰ÍÑ…ÉÉ…¥°ˆ°€‰ÉÕ¹¹•Èˆè€‰É•½É‰õuô°(€€€€€€€€€€€õô(€€€€€€€€€€€¥¹Ñ•ÉÙ…±Ì€ôíô((€€€€€€€€€€€‘•˜É•½É¡ÍÑ•À°Ñà¤è(€€€€€€€€€€€€€€€¥¹Ñ•ÉÙ…±ÍmÍÑ•Ál‰¥‰ut€ômÑ¥µ”¹µ½¹½Ñ½¹¥Œ ¤°9½¹•t(€€€€€€€€€€€€€€€Ñ¥µ”¹Í±••À À¸Àà¤(€€€€€€€€€€€€€€€¥¹Ñ•ÉÙ…±ÍmÍÑ•Ál‰¥‰uulÅt€ôÑ¥µ”¹µ½¹½Ñ½¹¥Œ ¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸I•ÍÕ±Ğ¡QÉÕ”°€‰‘½¹”ˆ¤((€€€€€€€€€€€İ¥Ñ Á…Ñ ¹‘¥Ğ¡IU99IL°ì‰É•½ÉˆèÉ•½É‘ô¤è(€€€€€€€€€€€€€€€µ…¹…•È€ô]½É­™±½İ5…¹…•È¡É½½Ğ°½¹™¥œ°ÍÑ½É”¤(€€€€€€€€€€€€€€€½¬°|€ôµ…¹…•È¹ÍÑ…ÉÑ}‘…¥±ä¡l‰•¹‘™¥•±ˆ°€‰é•¹±•ÍÌˆ°€‰ÍÑ…ÉÉ…¥°‰t°€È¤(€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡½¬¤(€€€€€€€€€€€€€€€µ…¹…•È¹©½¥¸ Ì¤(€€€€€€€€€€€½É‘•É•€ôÍ½ÉÑ•¡¥¹Ñ•ÉÙ…±Ì¹Ù…±Õ•Ì ¤°­•äõ±…µ‰‘„¥Ñ•´è¥Ñ•µlÁt¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡±•¸¡½É‘•É•¤°€Ì¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡…±°¡½É‘•É•‘m¥¹‘•áulÅt€ğô½É‘•É•‘m¥¹‘•à€¬€ÅulÁt(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€™½È¥¹‘•à¥¸É…¹” È¤¤¤((€€€‘•˜Ñ•ÍÑ}µ…¹Õ…±}ÍÑ…ÉÑ}É•ÍÁ•ÑÍ}•á±ÕÍ¥Ù•}É½ÕÀ¡Í•±˜¤è(€€€€€€€İ¥Ñ Ñ•µÁ™¥±”¹Q•µÁ½É…Éå¥É•Ñ½Éä ¤…ÌÑµÀè(€€€€€€€€€€€É½½Ğ€ôA…Ñ ¡ÑµÀ¤(€€€€€€€€€€€½¹™¥œ€ôì‰İ½É­™±½İÌˆèì(€€€€€€€€€€€€€€€€‰•¹‘™¥•±ˆèì‰•á±ÕÍ¥Ù•}É½ÕÀˆè€‰Á}É½ÕÀˆ°€‰ÍÑ•ÁÌˆèl(€€€€€€€€€€€€€€€€€€€ì‰¥ˆè€‰İ…¥Ğˆ°€‰ÉÕ¹¹•Èˆè€‰‘•±…äˆ°€‰Í•½¹‘Ìˆè€À¸Íõuô°(€€€€€€€€€€€€€€€€‰é•¹±•ÍÌˆèì‰•á±ÕÍ¥Ù•}É½ÕÀˆè€‰Á}É½ÕÀˆ°€‰ÍÑ•ÁÌˆèl(€€€€€€€€€€€€€€€€€€€ì‰¥ˆè€‰İ…¥Ğˆ°€‰ÉÕ¹¹•Èˆè€‰‘•±…äˆ°€‰Í•½¹‘Ìˆè€À¸ÀÅõuô°(€€€€€€€€€€€õô(€€€€€€€€€€€µ…¹…•È€ô]½É­™±½İ5…¹…•È¡É½½Ğ°½¹™¥œ°MÑ½É”¡É½½Ğ€¼€‰‘ˆ¹ÍÅ±¥Ñ”ˆ¤¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡µ…¹…•È¹ÍÑ…ÉĞ ‰•¹‘™¥•±ˆ¥lÁt¤(€€€€€€€€€€€½¬°µ•ÍÍ…”€ôµ…¹…•È¹ÍÑ…ÉĞ ‰é•¹±•ÍÌˆ¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑ…±Í”¡½¬¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑ%¸ ‹’â7¢÷–B3š^Ûš&Ÿ¢†0ˆ°µ•ÍÍ…”¤(€€€€€€€€€€€µ…¹…•È¹ÍÑ½À ¤ìµ…¹…•È¹©½¥¸ È¤((€€€‘•˜Ñ•ÍÑ}•áÁ±¥¥Ñ}•á±ÕÍ¥Ù•}İ¥Ñ¡}¥Í}Íåµµ•ÑÉ¥Œ¡Í•±˜¤è(€€€€€€€İ¥Ñ Ñ•µÁ™¥±”¹Q•µÁ½É…Éå¥É•Ñ½Éä ¤…ÌÑµÀè(€€€€€€€€€€€É½½Ğ€ôA…Ñ ¡ÑµÀ¤(€€€€€€€€€€€½¹™¥œ€ôì‰İ½É­™±½İÌˆèì(€€€€€€€€€€€€€€€€‰…±…Ìˆèì‰•á±ÕÍ¥Ù•}İ¥Ñ ˆèl‰¹…ÉÕÑ¼‰t°€‰ÍÑ•ÁÌˆèl(€€€€€€€€€€€€€€€€€€€ì‰¥ˆè€‰İ…¥Ğˆ°€‰ÉÕ¹¹•Èˆè€‰‘•±…äˆ°€‰Í•½¹‘Ìˆè€À¸Íõuô°(€€€€€€€€€€€€€€€€‰¹…ÉÕÑ¼ˆèì‰ÍÑ•ÁÌˆèl(€€€€€€€€€€€€€€€€€€€ì‰¥ˆè€‰İ…¥Ğˆ°€‰ÉÕ¹¹•Èˆè€‰‘•±…äˆ°€‰Í•½¹‘Ìˆè€À¸ÀÅõuô°(€€€€€€€€€€€õô(€€€€€€€€€€€µ…¹…•È€ô]½É­™±½İ5…¹…•È¡É½½Ğ°½¹™¥œ°MÑ½É”¡É½½Ğ€¼€‰‘ˆ¹ÍÅ±¥Ñ”ˆ¤¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡µ…¹…•È¹ÍÑ…ÉĞ ‰¹…ÉÕÑ¼ˆ¥lÁt¤(€€€€€€€€€€€½¬°µ•ÍÍ…”€ôµ…¹…•È¹ÍÑ…ÉĞ ‰…±…Ìˆ¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑ…±Í”¡½¬¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑ%¸ ‹’â7¢÷–B3š^Ûš&Ÿ¢†0ˆ°µ•ÍÍ…”¤(€€€€€€€€€€€µ…¹…•È¹ÍÑ½À ¤ìµ…¹…•È¹©½¥¸ È¤((€€€‘•˜Ñ•ÍÑ}‘…¥±å}‰…Ñ¡}Á…É…±±•±}…¹‘}Í•É¥…°¡Í•±˜¤è(€€€€€€€‘•˜ÉÕ¹}‰…Ñ ¡Á…É…±±•°¤è(€€€€€€€€€€€ÑµÀ€ôÑ•µÁ™¥±”¹Q•µÁ½É…Éå¥É•Ñ½Éä ¤(€€€€€€€€€€€É½½Ğ€ôA…Ñ ¡ÑµÀ¹¹…µ”¤(€€€€€€€€€€€½¹™¥œ€ôì‰İ½É­™±½İÌˆèì(€€€€€€€€€€€€€€€€‰„ˆèì‰ÍÑ•ÁÌˆèmì‰¥ˆè€‰„ˆ°€‰ÉÕ¹¹•Èˆè€‰‘•±…äˆ°€‰Í•½¹‘Ìˆè€À¸ÌÕõuô°(€€€€€€€€€€€€€€€€‰ˆˆèì‰ÍÑ•ÁÌˆèmì‰¥ˆè€‰ˆˆ°€‰ÉÕ¹¹•Èˆè€‰‘•±…äˆ°€‰Í•½¹‘Ìˆè€À¸ÌÕõuô(€€€€€€€€€€€õô(€€€€€€€€€€€µ…¹…•È€ô]½É­™±½İ5…¹…•È¡É½½Ğ°½¹™¥œ°MÑ½É”¡É½½Ğ€¼€‰‘ˆ¹ÍÅ±¥Ñ”ˆ¤¤(€€€€€€€€€€€ÍÑ…ÉÑ•€ôÑ¥µ”¹µ½¹½Ñ½¹¥Œ ¤(€€€€€€€€€€€½¬°|€ôµ…¹…•È¹ÍÑ…ÉÑ}‘…¥±ä¡l‰„ˆ°€‰ˆ‰t°Á…É…±±•°¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡½¬¤(€€€€€€€€€€€µ…¹…•È¹©½¥¸ Ô¤(€€€€€€€€€€€•±…ÁÍ•€ôÑ¥µ”¹µ½¹½Ñ½¹¥Œ ¤€´ÍÑ…ÉÑ•(€€€€€€€€€€€½µÁ±•Ñ•€ôµ…¹…•È¹ÍÑ…Ñ” ¥l‰‰…Ñ ‰ul‰½µÁ±•Ñ•‰t(€€€€€€€€€€€ÑµÀ¹±•…¹ÕÀ ¤(€€€€€€€€€€€É•ÑÕÉ¸•±…ÁÍ•°½µÁ±•Ñ•((€€€€€€€Í•É¥…±}Ñ¥µ”°Í•É¥…±}‘½¹”€ôÉÕ¹}‰…Ñ  Ä¤(€€€€€€€Á…É…±±•±}Ñ¥µ”°Á…É…±±•±}‘½¹”€ôÉÕ¹}‰…Ñ  È¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÉ•…Ñ•È¡Í•É¥…±}Ñ¥µ”°€À¸Ø¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑ1•ÍÌ¡Á…É…±±•±}Ñ¥µ”°Í•É¥…±}Ñ¥µ”€¨€À¸à¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡mál‰İ½É­™±½Ü‰t™½Èà¥¸Í•É¥…±}‘½¹•t°l‰„ˆ°€‰ˆ‰t¤(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡…±°¡ál‰ÍÑ…ÑÕÌ‰t€ôô€‰ÍÕ•ÍÌˆ™½Èà¥¸Á…É…±±•±}‘½¹”¤¤((€€€‘•˜Ñ•ÍÑ}‘…¥±å}‰…Ñ¡}Í•±•Ñ¥½¹}…¹‘}±¥µ¥Ğ¡Í•±˜¤è(€€€€€€€İ¥Ñ Ñ•µÁ™¥±”¹Q•µÁ½É…Éå¥É•Ñ½Éä ¤…ÌÑµÀè(€€€€€€€€€€€É½½Ğ€ôA…Ñ ¡ÑµÀ¤(€€€€€€€€€€€½¹™¥œ€ôì‰İ½É­™±½İÌˆèì(€€€€€€€€€€€€€€€€‰„ˆèì‰ÍÑ•ÁÌˆèmì‰¥ˆè€‰„ˆ°€‰ÉÕ¹¹•Èˆè€‰‘•±…äˆ°€‰Í•½¹‘Ìˆè€À¸ÀÕõuô°(€€€€€€€€€€€€€€€€‰ˆˆèì‰ÍÑ•ÁÌˆèmì‰¥ˆè€‰ˆˆ°€‰ÉÕ¹¹•Èˆè€‰‘•±…äˆ°€‰Í•½¹‘Ìˆè€À¸ÀÕõuô°(€€€€€€€€€€€€€€€€‰Í•±™}Ñ•ÍĞˆèì‰ÍÑ•ÁÌˆèmì‰¥ˆè€‰àˆ°€‰ÉÕ¹¹•Èˆè€‰‘•±…äˆ°€‰Í•½¹‘Ìˆè€À¸ÀÕõuô(€€€€€€€€€€€õô(€€€€€€€€€€€µ…¹…•È€ô]½É­™±½İ5…¹…•È¡É½½Ğ°½¹™¥œ°MÑ½É”¡É½½Ğ€¼€‰‘ˆ¹ÍÅ±¥Ñ”ˆ¤¤(€€€€€€€€€€€½¬°|€ôµ…¹…•È¹ÍÑ…ÉÑ}‘…¥±ä¡l‰ˆˆ°€‰Í•±™}Ñ•ÍĞ‰t°€ä¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡½¬¤(€€€€€€€€€€€µ…¹…•È¹©½¥¸ Ô¤(€€€€€€€€€€€‰…Ñ €ôµ…¹…•È¹ÍÑ…Ñ” ¥l‰‰…Ñ ‰t(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡‰…Ñ¡l‰µ…á}Á…É…±±•°‰t°€Ä¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡mál‰İ½É­™±½Ü‰t™½Èà¥¸‰…Ñ¡l‰½µÁ±•Ñ•‰ut°l‰ˆ‰t¤((€€€‘•˜Ñ•ÍÑ}•µ…¥±}É•Á½ÉÑ}ÁÉ•Á…É•Í}™É•Í¡}ÍÉ••¹Í¡½Ñ}İ¥Ñ¡½ÕÑ}É•‘•¹Ñ¥…±Ì¡Í•±˜¤è(€€€€€€€İ¥Ñ Ñ•µÁ™¥±”¹Q•µÁ½É…Éå¥É•Ñ½Éä ¤…ÌÑµÀè(€€€€€€€€€€€É½½Ğ€ôA…Ñ ¡ÑµÀ¤(€€€€€€€€€€€Í¡½Ğ€ôÉ½½Ğ€¼€‰Í¡½Ğ¹Á¹œˆ(€€€€€€€€€€€Í¡½Ğ¹İÉ¥Ñ•}‰åÑ•Ì¡ˆ‰Á¹œˆ¤(€€€€€€€€€€€½¹™¥œ€ôì‰•µ…¥±}É•Á½ÉĞˆèì(€€€€€€€€€€€€€€€€‰•¹…‰±•ˆèQÉÕ”°€‰É•¥Á¥•¹Ğˆè€‰Ñ•ÍÑ•á…µÁ±”¹½´ˆ°(€€€€€€€€€€€€€€€€‰ÍÉ••¹Í¡½ÑÌˆèì‰…µ”ˆèÍÑÈ¡Í¡½Ğ¥ô°(€€€€€€€€€€€€€€€€‰ÕÍ•É}•¹Øˆè€‰QMQ}M5QA}UMHˆ°€‰Á…ÍÍİ½É‘}•¹Øˆè€‰QMQ}M5QA}AMM]=Iˆ(€€€€€€€€€€€ô°€‰İ½É­™±½İÌˆèì‰…µ”ˆèì‰‘¥ÍÁ±…å}¹…µ”ˆè€‰…µ”‰õõô(€€€€€€€€€€€İ¥Ñ Á…Ñ ¹‘¥Ğ ‰½Ì¹•¹Ù¥É½¸ˆ°íô°±•…ÈõQÉÕ”¤è(€€€€€€€€€€€€€€€½¬°µ•ÍÍ…”€ôÍ•¹‘}‘…¥±å}ÍÉ••¹Í¡½ÑÌ (€€€€€€€€€€€€€€€€€€€É½½Ğ°½¹™¥œ°l‰…µ”‰t°mì‰İ½É­™±½Üˆè€‰…µ”ˆ°€‰ÍÑ…ÑÕÌˆè€‰ÍÕ•ÍÌ‰õt°€À¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑ…±Í”¡½¬¤(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑ%¸ ‹–Âkšr«¢ºûö¸ˆ°µ•ÍÍ…”¤(()¥˜}}¹…µ•}|€ôô€‰}}µ…¥¹}|ˆèÕ¹¥ÑÑ•ÍĞ¹µ…¥¸ ¤(