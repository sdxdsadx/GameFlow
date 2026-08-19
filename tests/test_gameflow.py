import json
import inspect
import os
import sys
import tempfile
import threading
import time
import http.client
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, call, patch

from gameflow.config import ConfigError, load_config
from gameflow.engine import Engine, WorkflowManager
from gameflow.daily import operational_day
from gameflow.diagnostics import collect_failure_diagnostics
from gameflow.mailer import send_daily_screenshots
from gameflow.store import Store
from gameflow.web import PAGE, UiPreferences, handler_for
from gameflow.runners import (RUNNERS, EmulatorBlackScreenWatchdog, Result, RunContext,
                              _click_named_gui_button, _decode_process_output,
                              _focus_window_before_click,
                              _parse_baas_queue_count,
                              _naruto_lobby_icon_metrics,
                              _naruto_visual_metrics, _screen_is_black,
                              _screen_looks_like_blue_archive_update,
                              _screen_looks_like_blue_archive_title,
                              _screen_looks_like_blue_archive_update_progress,
                              run_alas_gui,
                              run_adb, run_baas_gui, run_ba_reward_verify, run_gumballs_gui,
                              run_log_gui_daily, run_maa_gui, run_maaend_gui, run_mumu_wait,
                              run_naruto_shadow, run_qq_reader_trial)


class GameFlowTests(unittest.TestCase):
    def test_failure_diagnostics_writes_local_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = {"display_name": "测试流程", "steps": []}
            step = {"id": "broken", "runner": "command"}
            with patch("gameflow.diagnostics._process_and_window_state",
                       return_value={"processes": [], "windows": [], "foreground": None}), \
                    patch("gameflow.diagnostics._save_log_tails", return_value=[]), \
                    patch("gameflow.diagnostics._workflow_devices", return_value=[]):
                evidence = collect_failure_diagnostics(
                    root, {"tools": {}}, "test", workflow, step, 2,
                    "simulated failure", {"marker": "TEST"})
            report = Path(evidence["report"])
            self.assertTrue(report.exists())
            payload = __import__("json").loads(report.read_text(encoding="utf-8"))
            self.assertEqual(payload["workflow"], "test")
            self.assertEqual(payload["step"], "broken")
            self.assertEqual(payload["attempt"], 2)
            self.assertEqual(payload["result_details"]["marker"], "TEST")

    def test_operational_day_changes_at_four_am(self):
        china = timezone(timedelta(hours=8))
        self.assertEqual(operational_day(
            datetime(2026, 7, 20, 3, 59, 59, tzinfo=china)), "2026-07-19")
        self.assertEqual(operational_day(
            datetime(2026, 7, 20, 4, 0, 0, tzinfo=china)), "2026-07-20")

    def test_completed_today_uses_four_am_boundary(self):
        china = timezone(timedelta(hours=8))
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            db = store._connect()
            try:
                db.execute(
                    "INSERT INTO runs(workflow,status,trigger_name,started_at,finished_at) "
                    "VALUES(?,?,?,?,?)",
                    ("daily", "success", "manual", "2026-07-20T03:30:00+08:00",
                     "2026-07-20T03:31:00+08:00"))
                db.commit()
            finally:
                db.close()
            self.assertTrue(store.completed_today(
                "daily", datetime(2026, 7, 20, 3, 59, tzinfo=china)))
            self.assertFalse(store.completed_today(
                "daily", datetime(2026, 7, 20, 4, 0, tzinfo=china)))

    def test_daily_run_statuses_keep_today_completed_after_failed_rerun(self):
        china = timezone(timedelta(hours=8))
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            db = store._connect()
            try:
                db.executemany(
                    "INSERT INTO runs(workflow,status,trigger_name,started_at,finished_at,message) "
                    "VALUES(?,?,?,?,?,?)",
                    [
                        ("daily", "success", "manual", "2026-07-20T06:00:00+08:00",
                         "2026-07-20T06:30:00+08:00", "done"),
                        ("daily", "failed", "manual", "2026-07-20T07:00:00+08:00",
                         "2026-07-20T07:01:00+08:00", "rerun failed"),
                        ("other", "failed", "manual", "2026-07-20T08:00:00+08:00",
                         "2026-07-20T08:01:00+08:00", "failed"),
                    ])
                db.commit()
            finally:
                db.close()
            statuses = store.daily_run_statuses(
                ["daily", "other", "pending"],
                datetime(2026, 7, 20, 9, 0, tzinfo=china))
            self.assertEqual(statuses["daily"]["status"], "success")
            self.assertTrue(statuses["daily"]["completed"])
            self.assertEqual(statuses["other"]["status"], "failed")
            self.assertFalse(statuses["pending"]["completed"])

    def test_workflow_flags_persist_and_are_exposed_to_gui(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite")
            marker = {"marked_day": "2026-07-23", "wait_seconds": 600}
            store.set_workflow_flag("azur_lane_daily", "update_before_next_day", marker)
            self.assertEqual(store.get_workflow_flag(
                "azur_lane_daily", "update_before_next_day"), marker)
            manager = WorkflowManager(root, {"workflows": {
                "azur_lane_daily": {"steps": []},
            }}, store)
            state = manager.state()["workflows"]["azur_lane_daily"]
            self.assertTrue(state["tomorrow_update"])
            store.delete_workflow_flag("azur_lane_daily", "update_before_next_day")
            self.assertIsNone(store.get_workflow_flag(
                "azur_lane_daily", "update_before_next_day"))

    def test_finished_batch_badges_clear_after_operational_day_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = WorkflowManager(root, {"workflows": {
                "daily": {"steps": []},
            }}, Store(root / "db.sqlite"))
            manager._batch.update(running=False, completed=[{
                "workflow": "daily", "status": "skipped", "message": "done",
            }], operational_day="2000-01-01")
            snapshot = manager.state()["batch"]
            self.assertEqual(snapshot["completed"], [])
            self.assertEqual(snapshot["operational_day"], operational_day())

    def test_manager_exposes_live_daily_readiness_per_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); store = Store(root / "db.sqlite")
            run_id = store.start_run("done", "manual")
            store.finish_run(run_id, "success", "done")
            manager = WorkflowManager(root, {"workflows": {
                "done": {"steps": []}, "queued": {"steps": []},
            }}, store)
            manager._batch.update(running=True, queue=["queued"], active=[], completed=[])
            states = manager.state()["workflows"]
            self.assertEqual(states["done"]["today_status"], "success")
            self.assertTrue(states["done"]["today_completed"])
            self.assertEqual(states["queued"]["today_status"], "queued")

    def test_ui_preferences_persist_task_switches_order_and_parallelism(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "ui_preferences.json"
            preferences = UiPreferences(path, ["daily_game", "naruto_daily", "self_test"])
            saved = preferences.update({
                "order": ["naruto_daily", "unknown", "daily_game"],
                "max_parallel": 9,
                "workflows": {
                    "daily_game": {"enabled": False},
                    "naruto_daily": {"enabled": True},
                    "unknown": {"enabled": False},
                },
            })
            self.assertEqual(saved["order"], ["naruto_daily", "daily_game"])
            self.assertEqual(saved["max_parallel"], 2)
            self.assertFalse(saved["workflows"]["daily_game"]["enabled"])
            self.assertNotIn("self_test", saved["workflows"])

            reloaded = UiPreferences(path, ["daily_game", "naruto_daily", "self_test"]).get()
            self.assertEqual(reloaded, saved)

    def test_web_ui_exposes_clickable_skipped_and_persistent_task_switches(self):
        self.assertIn("SKIPPED", PAGE)
        self.assertIn("toggleWorkflow", PAGE)
        self.assertIn("/api/preferences", PAGE)
        self.assertIn("saveBeforeExit", PAGE)

    def test_web_ui_exposes_per_task_cancel_button(self):
        self.assertIn("async function cancelOne(id)", PAGE)
        self.assertIn("/api/cancel?workflow=", PAGE)
        self.assertIn("只取消这个任务，不影响其他任务", PAGE)
        self.assertIn("cancellableTasks", PAGE)

    def test_web_ui_exposes_live_daily_readiness_bar(self):
        for required in ("readiness-bar", "today_status", "今日已完成",
                         "今日未完成", "等待执行"):
            self.assertIn(required, PAGE)
        self.assertIn("明日更新", PAGE)

    def test_star_rail_config_rejects_incomplete_daily_training(self):
        root = Path(__file__).resolve().parents[1]
        config = load_config(root / "config" / "workflow.json")
        step = config["workflows"]["star_rail_daily"]["steps"][0]
        self.assertIn("每日实训未完成", step["error_markers"])
        self.assertIn("每日实训已完成", step["completion_markers"])
        self.assertNotIn("每日实训奖励完成", step["completion_markers"])

    def test_black_screen_classifier_and_five_minute_watchdog(self):
        import cv2
        import numpy as np

        black_png = cv2.imencode(".png", np.full((120, 200, 3), 28, dtype=np.uint8))[1].tobytes()
        normal_png = cv2.imencode(".png", np.full((120, 200, 3), 90, dtype=np.uint8))[1].tobytes()
        self.assertTrue(_screen_is_black(black_png, {})[0])
        self.assertFalse(_screen_is_black(normal_png, {})[0])

        with tempfile.TemporaryDirectory() as tmp:
            ctx = RunContext(Path(tmp), {}, lambda _: None, threading.Event())
            step = {"id": "watchdog_test", "black_screen_watchdog": True,
                    "black_screen_check_interval": 30, "black_screen_timeout": 300,
                    "black_screen_screenshot_path": str(Path(tmp) / "latest.png")}
            watchdog = EmulatorBlackScreenWatchdog(step, ctx, "测试模拟器")
            captured = SimpleNamespace(returncode=0, stdout=black_png, stderr=b"")
            with patch("gameflow.runners.subprocess.run", return_value=captured), \
                    patch("gameflow.runners.time.monotonic", side_effect=[0.0, 301.0]):
                self.assertIsNone(watchdog.poll(force=True))
                detected = watchdog.poll(force=True)
        self.assertIsNotNone(detected)
        self.assertTrue(detected.details["black_screen_restart"])
        self.assertGreaterEqual(detected.details["black_seconds"], 300)

    def test_blue_archive_update_screen_classifier(self):
        import cv2
        import numpy as np

        image = np.full((720, 1280, 3), 70, dtype=np.uint8)
        image[137:576, 358:922] = (238, 238, 238)
        image[468:548, 640:896] = (255, 200, 30)
        ok, metrics = _screen_looks_like_blue_archive_update(
            cv2.imencode(".png", image)[1].tobytes(), {})
        self.assertTrue(ok, metrics)

        initialization = np.full((720, 1280, 3), 235, dtype=np.uint8)
        initialization[680:710, 28:58] = (255, 170, 30)
        initialization[680:710, 68:270] = (238, 238, 238)
        false_update, _ = _screen_looks_like_blue_archive_update(
            cv2.imencode(".png", initialization)[1].tobytes(), {})
        self.assertFalse(false_update)

        locked_stage_notice = np.full((720, 1280, 3), 70, dtype=np.uint8)
        locked_stage_notice[137:576, 358:922] = (238, 238, 238)
        locked_stage_notice[468:548, 519:759] = (255, 200, 30)
        false_update, _ = _screen_looks_like_blue_archive_update(
            cv2.imencode(".png", locked_stage_notice)[1].tobytes(), {})
        self.assertFalse(false_update)

    def test_blue_archive_title_and_update_progress_are_distinct(self):
        import cv2
        import numpy as np

        title = np.full((720, 1280, 3), 235, dtype=np.uint8)
        title[0:158, 0:384] = (255, 190, 25)
        title[598:655, 320:960] = (240, 240, 240)
        title_png = cv2.imencode(".png", title)[1].tobytes()
        self.assertTrue(_screen_looks_like_blue_archive_title(title_png, {})[0])
        self.assertFalse(
            _screen_looks_like_blue_archive_update_progress(title_png, {})[0])

        progress = np.full((720, 1280, 3), 235, dtype=np.uint8)
        progress[652:662, 26:1254] = (95, 95, 95)
        progress_png = cv2.imencode(".png", progress)[1].tobytes()
        self.assertTrue(
            _screen_looks_like_blue_archive_update_progress(progress_png, {})[0])
        self.assertFalse(_screen_looks_like_blue_archive_title(progress_png, {})[0])

        launcher = np.full((720, 1280, 3), 52, dtype=np.uint8)
        launcher[652:662, 26:1254] = (45, 45, 45)
        launcher_png = cv2.imencode(".png", launcher)[1].tobytes()
        self.assertFalse(
            _screen_looks_like_blue_archive_update_progress(launcher_png, {})[0])

    def test_black_screen_restarts_emulator_then_retries_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite")
            calls = []

            def runner(step, ctx):
                calls.append("run")
                if len(calls) == 1:
                    return Result(False, "连续黑屏", {"black_screen_restart": True})
                return Result(True, "恢复后完成")

            config = {"workflows": {"test": {"steps": [{
                "id": "watched", "runner": "watched",
                "max_black_screen_restarts": 1, "retry_delay": 0}]}}}
            with patch.dict(RUNNERS, {"watched": runner}), patch(
                    "gameflow.engine._restart_configured_emulator",
                    return_value=Result(True, "模拟器已恢复")) as restart:
                engine = Engine(root, config, store)
                engine.start("test")
                engine._thread.join(5)
            self.assertEqual(calls, ["run", "run"])
            restart.assert_called_once()
            self.assertEqual(engine.state()["last_status"], "success")

    def test_global_failure_policy_runs_exactly_two_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite")
            calls = []

            def always_fails(step, ctx):
                calls.append("run")
                return Result(False, "still broken")

            config = {
                "failure_retry": {
                    "enabled": True,
                    "max_attempts": 2,
                    "retry_delay": 0,
                },
                "workflows": {"test": {"steps": [{
                    "id": "flaky",
                    "runner": "always_fails",
                    "retry": 99,
                    "max_black_screen_restarts": 99,
                }]}},
            }
            with patch.dict(RUNNERS, {"always_fails": always_fails}):
                engine = Engine(root, config, store)
                engine.start("test")
                engine.join(3)
            self.assertEqual(calls, ["run", "run"])
            self.assertEqual(engine.state()["last_status"], "failed")

    def test_skipped_step_is_not_retried_and_sets_next_day_update_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite")
            calls = []

            def maintenance(step, ctx):
                calls.append("run")
                return Result(False, "服务器维护", {
                    "defer_update_next_day": True,
                    "next_day_update_wait": 600,
                }, status="skipped")

            config = {"workflows": {"azur_lane_daily": {"steps": [{
                "id": "alas", "runner": "maintenance", "retry": 3,
            }]}}}
            with patch.dict(RUNNERS, {"maintenance": maintenance}):
                engine = Engine(root, config, store)
                engine.start("azur_lane_daily")
                engine._thread.join(5)
            self.assertEqual(calls, ["run"])
            self.assertEqual(engine.state()["last_status"], "skipped")
            marker = store.get_workflow_flag(
                "azur_lane_daily", "update_before_next_day")
            self.assertEqual(marker["wait_seconds"], 600)

    def test_next_day_update_wait_is_injected_into_baas(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite")
            seen = {}
            store.set_workflow_flag("blue_archive_daily", "update_before_next_day",
                                    {"marked_day": "2000-01-01",
                                     "wait_seconds": 600,
                                     "reason": "update"})

            def baas(step, ctx):
                seen.update(step)
                step["_startup_update_wait_consumed"] = True
                return Result(True, "ok")

            config = {"workflows": {"blue_archive_daily": {"steps": [{
                "id": "baas", "runner": "baas_gui",
            }]}}}
            with patch.dict(RUNNERS, {"baas_gui": baas}):
                engine = Engine(root, config, store)
                engine.start("blue_archive_daily")
                engine._thread.join(5)
            self.assertEqual(seen["startup_update_wait"], 600)
            self.assertIsNone(store.get_workflow_flag(
                "blue_archive_daily", "update_before_next_day"))

    def test_next_day_update_flag_remains_until_runner_consumes_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite")
            seen = {}
            marker = {"marked_day": "2000-01-01", "wait_seconds": 600,
                      "reason": "update"}
            store.set_workflow_flag(
                "blue_archive_daily", "update_before_next_day", marker)

            def baas(step, ctx):
                seen.update(step)
                return Result(True, "runner exited before consuming wait")

            config = {"workflows": {"blue_archive_daily": {"steps": [{
                "id": "baas", "runner": "baas_gui",
            }]}}}
            with patch.dict(RUNNERS, {"baas_gui": baas}):
                engine = Engine(root, config, store)
                engine.start("blue_archive_daily")
                engine._thread.join(5)

            self.assertEqual(seen["startup_update_wait"], 600)
            self.assertEqual(store.get_workflow_flag(
                "blue_archive_daily", "update_before_next_day"), marker)

    def test_script_gui_is_confirmed_foreground_before_click(self):
        hwnd = 2468
        win32gui = Mock()
        win32gui.IsWindow.return_value = True
        win32gui.GetForegroundWindow.return_value = hwnd
        win32con = SimpleNamespace(
            SW_RESTORE=9,
            SWP_NOMOVE=2,
            SWP_NOSIZE=1,
            SWP_SHOWWINDOW=64,
            HWND_TOPMOST=-1,
            HWND_NOTOPMOST=-2,
        )
        with patch("gameflow.runners.os.name", "nt"), patch.dict(
                sys.modules, {"win32gui": win32gui, "win32con": win32con}):
            focused, message = _focus_window_before_click(
                hwnd, "测试脚本", timeout=0.1, settle_seconds=0)

        self.assertTrue(focused)
        self.assertIn("置于最前端", message)
        win32gui.ShowWindow.assert_called_once_with(hwnd, win32con.SW_RESTORE)
        win32gui.BringWindowToTop.assert_called_once_with(hwnd)
        win32gui.SetForegroundWindow.assert_called_once_with(hwnd)
        self.assertEqual(win32gui.SetWindowPos.call_args_list[-1].args[1],
                         win32con.HWND_NOTOPMOST)

    def test_script_gui_uses_attached_input_fallback_when_windows_denies_focus(self):
        hwnd = 9753
        win32gui = Mock()
        win32gui.IsWindow.return_value = True
        win32gui.SetForegroundWindow.side_effect = OSError("foreground denied")
        win32gui.GetForegroundWindow.return_value = 111
        win32con = SimpleNamespace(
            SW_RESTORE=9, SWP_NOMOVE=2, SWP_NOSIZE=1, SWP_SHOWWINDOW=64,
            HWND_TOPMOST=-1, HWND_NOTOPMOST=-2)
        with patch("gameflow.runners.os.name", "nt"), patch.dict(
                sys.modules, {"win32gui": win32gui, "win32con": win32con}), patch(
                    "gameflow.runners._force_foreground_window", return_value=True) as fallback:
            focused, _ = _focus_window_before_click(
                hwnd, "测试脚本", timeout=0.1, settle_seconds=0)

        self.assertTrue(focused)
        fallback.assert_called_once_with(hwnd)

    def test_named_gui_button_rechecks_foreground_immediately_before_click(self):
        class PsutilError(Exception):
            pass

        hwnd = 8642
        pid = 4321
        win32gui = Mock()
        win32gui.IsWindowVisible.return_value = True
        win32gui.GetWindowText.return_value = "March7th Launcher"
        win32gui.GetWindowRect.return_value = (100, 100, 1100, 800)
        win32gui.EnumWindows.side_effect = lambda callback, value: callback(hwnd, value)
        win32process = SimpleNamespace(
            GetWindowThreadProcessId=lambda _: (77, pid))
        psutil = SimpleNamespace(
            Error=PsutilError,
            Process=lambda _: SimpleNamespace(children=lambda recursive: []),
            process_iter=lambda _: [SimpleNamespace(
                info={"pid": pid, "name": "March7th Launcher.exe"})])
        app = Mock()
        app.connect.return_value = app
        window = Mock()
        window.descendants.return_value = []
        app.window.return_value = window
        pywinauto = SimpleNamespace(Application=Mock(return_value=app))
        pyautogui = Mock()

        with patch("gameflow.runners.os.name", "nt"), patch.dict(sys.modules, {
                "psutil": psutil, "win32con": SimpleNamespace(),
                "win32gui": win32gui, "win32process": win32process,
                "pywinauto": pywinauto, "pyautogui": pyautogui}), patch(
                    "gameflow.runners._focus_window_before_click",
                    return_value=(True, "focused")) as focus:
            clicked, _ = _click_named_gui_button(
                pid, ["March7th Launcher.exe"], ["March7th"], ["完整运行"],
                0.165, 0.82)

        self.assertTrue(clicked)
        self.assertEqual(focus.call_count, 2)
        pyautogui.click.assert_called_once_with(265, 674)

    def test_named_gui_button_accepts_detached_window_with_strong_title(self):
        class PsutilError(Exception):
            pass

        hwnd = 9753
        detached_pid = 2468
        win32gui = Mock()
        win32gui.IsWindowVisible.return_value = True
        win32gui.GetWindowText.return_value = "绝区零 一条龙 01"
        win32gui.GetWindowRect.return_value = (0, 0, 1200, 800)
        win32gui.EnumWindows.side_effect = lambda callback, value: callback(hwnd, value)
        win32process = SimpleNamespace(
            GetWindowThreadProcessId=lambda _: (77, detached_pid))
        psutil = SimpleNamespace(
            Error=PsutilError,
            Process=lambda _: SimpleNamespace(children=lambda recursive: []),
            process_iter=lambda _: [])
        app = Mock(); app.connect.return_value = app
        window = Mock(); window.descendants.return_value = []
        app.window.return_value = window
        pywinauto = SimpleNamespace(Application=Mock(return_value=app))
        pyautogui = Mock()

        with patch("gameflow.runners.os.name", "nt"), patch.dict(sys.modules, {
                "psutil": psutil, "win32con": SimpleNamespace(),
                "win32gui": win32gui, "win32process": win32process,
                "pywinauto": pywinauto, "pyautogui": pyautogui}), patch(
                    "gameflow.runners._focus_window_before_click",
                    return_value=(True, "focused")):
            clicked, message = _click_named_gui_button(
                1111, ["OneDragon-Launcher.exe"], ["绝区零 一条龙"],
                ["启动一条龙"], 0.88, 0.92)

        self.assertTrue(clicked)
        self.assertIn("绝区零 一条龙 01", message)
        pyautogui.click.assert_called_once_with(1056, 736)

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
        self.assertLess(PAGE.index('id="startDaily"'), PAGE.index('<section class="hero">'))
        self.assertIn('id="qqTrial"', PAGE)
        self.assertIn("runQQTrial()", PAGE)
        self.assertIn("!x.test_only", PAGE)
        self.assertIn("每日自动化总控", PAGE)

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
        self.assertTrue(config["workflows"]["blue_archive_daily"]["steps"][2]["require_admin"])
        blue_start = config["workflows"]["blue_archive_daily"]["steps"][0]
        self.assertTrue(blue_start["verify_started"])
        self.assertGreaterEqual(blue_start["verify_timeout"], 60)
        blue_runner = config["workflows"]["blue_archive_daily"]["steps"][2]
        self.assertEqual(
            [blue_runner["repeated_probe_tap_x"], blue_runner["repeated_probe_tap_y"]],
            [930, 540])
        naruto = config["workflows"]["naruto_daily"]["steps"][2]
        self.assertTrue(naruto["shadow_open_confirm"])
        self.assertEqual(naruto["shadow_continue_point"], [520, 1118])
        self.assertGreaterEqual(naruto["shadow_continue_wait"], 5)
        self.assertGreaterEqual(naruto["enable_retry_interval"], 8)
        self.assertGreaterEqual(naruto["enable_confirm_timeout"], 10)
        self.assertGreaterEqual(naruto["enable_confirm_stable_checks"], 2)
        self.assertIn("功能运行中", naruto["enable_success_labels"])
        self.assertGreaterEqual(naruto["start_retry_interval"], 12)
        self.assertGreaterEqual(naruto["float_stop_confirm_timeout"], 10)
        self.assertGreaterEqual(naruto["float_stop_stable_checks"], 2)
        self.assertEqual(naruto["orientation_points"]["landscape"]["game_consent_point"],
                         [817, 600])
        self.assertIn("float_start_x", naruto["orientation_points"]["portrait"])
        self.assertIn("float_start_x", naruto["orientation_points"]["landscape"])
        self.assertGreaterEqual(naruto["stage1_wrong_foreground_checks"], 5)
        self.assertEqual(
            naruto["orientation_points"]["portrait"]["popup_confirm_point"],
            [360, 935])
        self.assertEqual(
            naruto["orientation_points"]["landscape"]["popup_confirm_point"],
            [640, 515])
        self.assertEqual(naruto["shadow_update_grace_versions"], 3)
        self.assertEqual(naruto["shadow_update_confirm_point"], [760, 500])
        self.assertGreaterEqual(naruto["shadow_update_popup_close_wait"], 10)
        self.assertGreaterEqual(naruto["lobby_icon_min_matches"], 3)
        self.assertGreaterEqual(len(naruto["lobby_reference_paths"]), 2)
        zenless = config["workflows"]["zenless_daily"]["steps"][0]
        self.assertEqual(zenless["retry"], 1)
        self.assertGreaterEqual(zenless["state_stall_overrides"]["^空洞操作器 \\|"], 900)
        self.assertTrue(any(
            "游戏窗口未就绪" in marker
            for marker in zenless["update_failure_markers"]))
        self.assertEqual(config["failure_retry"]["max_attempts"], 2)
        maa = config["workflows"]["daily_game"]["steps"][2]
        self.assertEqual(maa["log_stall_seconds"], 600)
        self.assertTrue(maa["gui_start_click"])
        self.assertEqual(maa["max_start_clicks"], 6)
        self.assertEqual(
            config["workflows"]["daily_game"]["steps"][1]["settle_seconds"], 60)
        self.assertTrue(
            config["workflows"]["blue_archive_daily"]["steps"][2]
            .get("queue_only_completion", True))
        star_rail = config["workflows"]["star_rail_daily"]["steps"][0]
        self.assertEqual(star_rail["max_start_clicks"], 20)
        self.assertEqual(star_rail["launch_args"], ["main", "-e"])
        self.assertEqual(star_rail["startup_ack_button_names"], ["我已知晓", "好的"])
        self.assertEqual(star_rail["max_startup_ack_clicks"], 3)
        self.assertIn("GitHub 发现新版本", star_rail["update_markers"])
        self.assertTrue(star_rail["dismiss_update_notice"])
        self.assertEqual(star_rail["update_ack_button_names"], ["好的"])
        self.assertAlmostEqual(star_rail["update_ack_x_ratio"], 0.366)
        self.assertAlmostEqual(star_rail["update_ack_y_ratio"], 0.912)
        watched = (("daily_game", 2), ("blue_archive_daily", 2),
                   ("azur_lane_daily", 1), ("naruto_daily", 2),
                   ("gumballs_daily", 2))
        for workflow, index in watched:
            step = config["workflows"][workflow]["steps"][index]
            self.assertTrue(step["black_screen_watchdog"])
            self.assertEqual(step["black_screen_timeout"], 300)

    def test_naruto_retry_loops_check_optional_continue_before_clicking(self):
        source = inspect.getsource(run_naruto_shadow)

        enable_loop = source[source.index("enable_attempt = 0"):
                             source.index("start_deadline =")]
        self.assertLess(
            enable_loop.index("click_shadow_continue_if_visible"),
            enable_loop.index("enable_attempt += 1"),
        )
        self.assertIn("if continued.details.get(\"clicked\"):", enable_loop)
        self.assertIn("重新开始本轮", enable_loop)
        self.assertIn("confirm_shadow_enabled(enable_confirm_timeout)", enable_loop)
        self.assertIn("本次不视为启动成功", enable_loop)
        self.assertIn("ensure_package_foreground(shadow_package)", enable_loop)
        self.assertIn("find_ui_text_point_exact(\"启动功能\")", enable_loop)

        float_loop = source[source.index("while time.monotonic() < start_deadline"):
                            source.index("if not game_started:")]
        self.assertLess(
            float_loop.index("click_shadow_continue_if_visible"),
            float_loop.index("start_attempt += 1"),
        )
        self.assertIn("重新识别红色浮窗", float_loop)
        self.assertIn("inspect_float_primary_control(confirm_point)", float_loop)
        self.assertIn("未识别到方形终止符", float_loop)
        self.assertIn("confirm_game_foreground()", float_loop)
        self.assertIn("按游戏启动成功处理", float_loop)
        self.assertNotIn("if game_focused:", float_loop)
        self.assertIn("locate_float_icon_details()", float_loop)
        self.assertIn("handle_game_blocking_page()", float_loop)
        self.assertIn("classify_collapsed_float_control(icon_point)", float_loop)
        self.assertIn("全程未点击该方形符", float_loop)
        self.assertIn('side_label = "左侧"', float_loop)
        self.assertIn('else "右侧"', float_loop)
        self.assertIn("半缩在{side_label}边缘", float_loop)
        self.assertIn("浮窗点击前检测到其他应用位于前台", float_loop)
        self.assertIn("取消本次坐标点击", float_loop)
        self.assertNotIn("未知，按校准坐标尝试", float_loop)

        stage_one = source[source.index("# Stage 1:"):
                           source.index("# Stage 2:")]
        self.assertIn("stage1_wrong_foreground_checks", stage_one)
        self.assertIn("is_login_method_page(image)", stage_one)
        self.assertIn("handle_game_blocking_page()", stage_one)
        self.assertIn('"emulator_restart_requested": True', stage_one)

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

    def test_cancelled_workflow_gives_run_always_cleanup_a_live_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite")
            cleanup_stop_states = []

            def cancel_run(step, ctx):
                ctx.stop_event.set()
                return Result(False, "cancelled by user", status="cancelled")

            def cleanup(step, ctx):
                cleanup_stop_states.append(ctx.stop_event.is_set())
                return Result(True, "cleanup completed")

            config = {"workflows": {"test": {"steps": [
                {"id": "main", "runner": "cancel_run"},
                {"id": "cleanup", "runner": "cleanup", "run_always": True},
            ]}}}
            with patch.dict(RUNNERS, {
                    "cancel_run": cancel_run, "cleanup": cleanup}):
                engine = Engine(root, config, store)
                engine.start("test")
                engine.join(2)

            self.assertEqual(cleanup_stop_states, [False])
            self.assertEqual(engine.state()["last_status"], "cancelled")

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
            self.assertTrue(result.success, result.message)
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

    def test_baas_queue_label_parser_accepts_current_and_legacy_labels(self):
        self.assertEqual(_parse_baas_queue_count(["队列中  (0)"]), 0)
        self.assertEqual(_parse_baas_queue_count(["队列 (7)"]), 7)
        self.assertEqual(_parse_baas_queue_count(["队列中", "（12）"]), 12)
        self.assertIsNone(_parse_baas_queue_count(["等待中 (8)", "闲置中"]))

    def test_baas_runner_finishes_only_after_queue_is_empty_and_idle(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "baas.exe"; exe.write_bytes(b"")
            logs = root / "logs"; logs.mkdir(); log = logs / "today_baas1.log"
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            def write_start():
                time.sleep(0.05)
                log.write_text("开始执行【工作任务】\n", encoding="utf-8")

            threading.Thread(target=write_start, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "*_baas1.log"),
                    "require_empty_queue": True, "queue_check_interval": 0.05,
                    "queue_empty_confirm_seconds": 0.05, "poll_seconds": 0.02,
                    "log_stall_seconds": 2, "timeout": 3,
                    "clean_existing": False, "close_on_complete": False,
                    "gui_click_fallback": False}
            states = [(2, False, "running"), (0, False, "last task running"),
                      (0, True, "idle"), (0, True, "idle")]

            def queue_state(_pid):
                return states.pop(0) if len(states) > 1 else states[0]

            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), patch(
                    "gameflow.runners._read_baas_queue_count", side_effect=queue_state):
                result = run_baas_gui(step, ctx)
            self.assertTrue(result.success)
            self.assertEqual(result.details["queue_count"], 0)

    def test_baas_initial_empty_queue_without_current_run_evidence_is_not_success(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "baas.exe"; exe.write_bytes(b"")
            logs = root / "logs"; logs.mkdir()
            ctx = RunContext(root, {}, lambda _: None, threading.Event())
            step = {
                "executable": str(exe),
                "log_glob": str(logs / "*_baas1.log"),
                "require_empty_queue": True,
                "queue_check_interval": 0.01,
                "queue_empty_confirm_seconds": 0,
                "startup_timeout": 0.12,
                "poll_seconds": 0.01,
                "timeout": 1,
                "clean_existing": False,
                "close_on_complete": False,
                "gui_click_fallback": False,
            }
            with patch("gameflow.runners.subprocess.Popen",
                       return_value=FakeProcess()), patch(
                    "gameflow.runners._read_baas_queue_count",
                    return_value=(0, True, "idle")):
                result = run_baas_gui(step, ctx)

            self.assertFalse(result.success)
            self.assertIn("仍无任务启动日志", result.message)
            self.assertNotIn("work_evidence", result.details)

    def test_baas_queue_zero_and_success_log_finish_when_idle_state_is_unreadable(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "baas.exe"; exe.write_bytes(b"")
            logs = root / "logs"; logs.mkdir(); log = logs / "today_baas1.log"
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            def write_success():
                time.sleep(0.05)
                log.write_text("开始执行【工作任务】\n任务全部执行成功\n", encoding="utf-8")

            threading.Thread(target=write_success, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "*_baas1.log"),
                    "require_empty_queue": True, "queue_check_interval": 0.03,
                    "queue_empty_confirm_seconds": 0, "completion_quiet_seconds": 1,
                    "poll_seconds": 0.01, "log_stall_seconds": 2, "timeout": 3,
                    "clean_existing": False, "close_on_complete": False,
                    "gui_click_fallback": False}
            states = [(2, False, "running"), (0, False, "finishing"),
                      (None, None, "temporarily unreadable")]

            def queue_state(_pid):
                return states.pop(0) if len(states) > 1 else states[0]

            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), patch(
                    "gameflow.runners._read_baas_queue_count", side_effect=queue_state):
                result = run_baas_gui(step, ctx)
            self.assertTrue(result.success)
            self.assertEqual(result.details["completion_mode"], "queue_zero_success_marker")

    def test_baas_successful_queue_click_arms_stall_watchdog(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "baas.exe"; exe.write_bytes(b"")
            logs = root / "logs"; logs.mkdir()
            ctx = RunContext(root, {}, lambda _: None, threading.Event())
            step = {"executable": str(exe), "log_glob": str(logs / "*_baas1.log"),
                    "require_empty_queue": True, "queue_check_interval": 0.02,
                    "poll_seconds": 0.01, "log_stall_seconds": 0.08,
                    "startup_timeout": 2, "timeout": 2,
                    "clean_existing": False, "close_on_complete": False,
                    "gui_click_fallback": False, "gui_fallback_retry_seconds": 1}

            queue_states = [(10, True, "idle"), (10, False, "running")]

            def queue_state(_pid):
                return queue_states.pop(0) if len(queue_states) > 1 else queue_states[0]

            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), patch(
                    "gameflow.runners._read_baas_queue_count",
                    side_effect=queue_state), patch(
                    "gameflow.runners._click_baas_start_button",
                    return_value=(True, "clicked")):
                result = run_baas_gui(step, ctx)
            self.assertFalse(result.success)
            self.assertTrue(result.details["log_stalled"])

    def test_baas_ticket_shortage_is_nonfatal(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "baas.exe"; exe.write_bytes(b"")
            logs = root / "logs"; logs.mkdir(); messages = []
            ctx = RunContext(root, {}, messages.append, threading.Event())

            def write_marker():
                time.sleep(0.1)
                (logs / "today_baas1.log").write_text(
                    "模拟器连接成功\n开始执行【战术对抗赛】\n入场券不足\n"
                    "开始执行【工作任务】\n执行完成【工作任务】\n任务全部执行成功\n",
                    encoding="utf-8")

            threading.Thread(target=write_marker, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "*_baas1.log"),
                    "completion_marker": "任务全部执行成功",
                    "error_markers": ["任务全部执行失败"],
                    "ignored_error_markers": ["入场券不足"],
                    "required_last_task": "工作任务", "timeout": 3,
                    "completion_quiet_seconds": 0.05,
                    "clean_existing": False, "close_on_complete": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()):
                result = run_baas_gui(step, ctx)
            self.assertTrue(result.success)
            self.assertTrue(any("非致命提示：入场券不足" in message for message in messages))

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
            task_page[670:700, 510:856] = (255, 200, 0)
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

    def test_blue_archive_reward_verification_rejects_gray_buttons_when_progress_incomplete(self):
        import cv2
        import numpy as np

        class Done:
            def __init__(self, stdout=b"", stderr=b"", returncode=0):
                self.stdout, self.stderr, self.returncode = stdout, stderr, returncode

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = np.zeros((720, 1280, 3), dtype=np.uint8)
            task_page = np.full((720, 1280, 3), 80, dtype=np.uint8)
            task_page[670:700, 510:807] = (255, 200, 0)
            calls = [
                Done(cv2.imencode(".png", home)[1].tobytes()),
                Done(),
                Done(cv2.imencode(".png", task_page)[1].tobytes()),
            ]
            step = {
                "device": "test", "entry_x_ratio": 0.052, "entry_y_ratio": 0.326,
                "evidence_path": str(root / "evidence.png"), "page_wait": 0,
            }
            ctx = RunContext(root, {"tools": {"adb": "adb"}}, lambda _: None,
                             threading.Event())
            with patch("gameflow.runners.subprocess.run", side_effect=calls):
                result = run_ba_reward_verify(step, ctx)
            self.assertFalse(result.success)
            self.assertIn("进度未满", result.message)
            self.assertLess(result.details["progress"]["fill_ratio"], 0.94)

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
                    "log_stall_seconds": 0.05,
                    "timeout": 5, "clean_existing": False, "close_on_complete": False,
                    "gui_click_fallback": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()):
                result = run_baas_gui(step, ctx)
            self.assertTrue(result.success)
            self.assertEqual(result.details["last_completed_task"], "工作任务")
            self.assertTrue(any("不是“工作任务”" in message for message in messages))

    def test_baas_detects_log_stall_only_after_current_run_started(self):
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

            def start_then_stall():
                # The pre-start wait is deliberately longer than the stall threshold.
                # It must remain governed by startup_timeout, not the running watchdog.
                time.sleep(0.02)
                log.write_text("日志初始化成功\n开始检查ATX\n", encoding="utf-8")
                time.sleep(0.1)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("开始执行【咖啡厅】\n")

            threading.Thread(target=start_then_stall, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "*_baas1.log"),
                    "start_markers": ["日志初始化成功", "开始执行【"],
                    "log_stall_start_markers": ["开始执行【"],
                    "log_stall_seconds": 0.05, "startup_timeout": 0.5,
                    "poll_seconds": 0.01, "timeout": 1,
                    "clean_existing": False, "close_on_complete": False,
                    "gui_click_fallback": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()):
                result = run_baas_gui(step, ctx)
            self.assertFalse(result.success)
            self.assertTrue(result.details["log_stalled"])
            self.assertTrue(result.details["retry_step"])
            self.assertEqual(result.details["last_run_task"], "咖啡厅")
            self.assertGreaterEqual(result.details["log_stall_seconds"], 0.05)

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

    def test_baas_repeated_update_prompt_click_is_capped(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "baas.exe"; exe.write_bytes(b"")
            logs = root / "logs"; logs.mkdir(); log = logs / "today_baas1.log"
            shot = root / "latest.png"; shot.write_bytes(b"png")
            log.write_text("", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            step = {"executable": str(exe), "log_glob": str(logs / "*_baas1.log"),
                    "black_screen_screenshot_path": str(shot),
                    "update_screen_detection": True, "next_day_update_wait": 600,
                    "update_screen_check_interval": 0.05,
                    "update_screen_grace_seconds": 0,
                    "update_screen_confirm_count": 1,
                    "max_update_confirm_clicks": 1,
                    "poll_seconds": 0.01, "startup_timeout": 1, "timeout": 1,
                    "clean_existing": False, "close_on_complete": False,
                    "gui_click_fallback": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners.EmulatorBlackScreenWatchdog.poll",
                          return_value=None), \
                    patch("gameflow.runners._screen_looks_like_blue_archive_update",
                          return_value=(True, {
                              "ba_update_dialog_light_ratio": 0.9,
                              "ba_update_confirm_cyan_ratio": 0.8})), \
                    patch("gameflow.runners._screen_looks_like_blue_archive_title",
                          return_value=(False, {})), \
                    patch("gameflow.runners._screen_looks_like_blue_archive_update_progress",
                          return_value=(False, {})), \
                    patch("gameflow.runners.subprocess.run",
                          return_value=SimpleNamespace(
                              returncode=0, stdout=b"", stderr=b"")):
                result = run_baas_gui(step, ctx)
            self.assertFalse(result.success)
            self.assertEqual(result.status, "needs_update")
            self.assertTrue(result.details["defer_update_next_day"])
            self.assertEqual(result.details["next_day_update_wait"], 600)

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

            def click(*args, **kwargs):
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

    def test_baas_does_not_fallback_click_while_queue_is_running(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "baas.exe"; exe.write_bytes(b"")
            logs = root / "logs"; logs.mkdir(); (logs / "today_baas1.log").write_text(
                "RestartTaskException: ATX卡死，重启任务\n", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())
            clicks = 0

            def click(*args):
                nonlocal clicks
                clicks += 1
                return True, "clicked"

            step = {"executable": str(exe), "log_glob": str(logs / "*_baas1.log"),
                    "require_empty_queue": True, "gui_fallback_after": 0,
                    "gui_fallback_retry_seconds": 0.02, "startup_timeout": 0.12,
                    "max_start_clicks": 4, "poll_seconds": 0.01,
                    "queue_check_interval": 0.01, "timeout": 0.4,
                    "clean_existing": False, "close_on_complete": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners._read_baas_queue_count",
                          return_value=(7, False, "running")), \
                    patch("gameflow.runners._click_baas_start_button", side_effect=click):
                result = run_baas_gui(step, ctx)
            self.assertFalse(result.success)
            self.assertEqual(clicks, 0)

    def test_maa_gui_follows_successor_after_launcher_exit(self):
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
            messages = []
            ctx = RunContext(root, {}, messages.append, threading.Event())

            def update_and_finish():
                time.sleep(0.1)
                gui.write_text(
                    "Pending update package detected\n"
                    "开始任务: 基建换班\n"
                    "当前选择的推理加速 GPU 存在兼容性问题\n",
                    encoding="utf-8")
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
            self.assertIn("MAA 开始任务：基建换班", messages)
            self.assertTrue(any("GPU 存在兼容性问题" in message
                                for message in messages))

    def test_maa_gui_fails_after_fresh_log_stalls(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "MAA.exe"
            exe.write_bytes(b"")
            asst = root / "asst.log"
            asst.write_text("", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            def write_once():
                time.sleep(0.05)
                asst.write_text("task started\n", encoding="utf-8")

            threading.Thread(target=write_once, daemon=True).start()
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()):
                result = run_maa_gui({
                    "executable": str(exe),
                    "log_path": str(asst),
                    "log_stall_seconds": 0.05,
                    "timeout": 4,
                    "close_on_complete": False,
                }, ctx)
            self.assertFalse(result.success)
            self.assertIn("日志已停滞", result.message)
            self.assertTrue(result.details["retry_step"])

    def test_maa_gui_clicks_start_after_emulator_settles(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "MAA.exe"
            exe.write_bytes(b"")
            asst = root / "asst.log"
            asst.write_text("", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            def click(*args):
                asst.write_text("task started\nAllTasksCompleted\n", encoding="utf-8")
                return True, "clicked"

            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners._click_named_gui_button",
                          side_effect=click) as start_click:
                result = run_maa_gui({
                    "executable": str(exe),
                    "log_path": str(asst),
                    "gui_start_click": True,
                    "initial_start_click_delay": 0,
                    "start_click_interval": 0.01,
                    "max_start_clicks": 2,
                    "timeout": 3,
                    "close_on_complete": False,
                }, ctx)
            self.assertTrue(result.success, result.message)
            start_click.assert_called_once()
            self.assertEqual(start_click.call_args.args[3], ["开始任务", "开始"])

    def test_maa_exact_updater_marker_returns_needs_update_without_waiting(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "MAA.exe"
            exe.write_bytes(b"")
            asst = root / "asst.log"
            asst.write_text("", encoding="utf-8")
            gui = root / "gui.log"
            gui.write_text("", encoding="utf-8")
            stop = Mock()
            stop.wait.return_value = False
            stop.is_set.return_value = False
            ctx = RunContext(root, {}, lambda _: None, stop)
            exact_marker = "MAA.Updater started (C++ external updater)."

            def launch(*args, **kwargs):
                gui.write_text(exact_marker + "\n", encoding="utf-8")
                return FakeProcess()

            started = time.monotonic()
            with patch("gameflow.runners.subprocess.Popen",
                       side_effect=launch), patch(
                    "gameflow.runners._process_image_exists",
                    return_value=False):
                result = run_maa_gui({
                    "executable": str(exe),
                    "log_path": str(asst),
                    "gui_log_path": str(gui),
                    "update_markers": [exact_marker],
                    "timeout": 30,
                    "close_on_complete": False,
                }, ctx)
            elapsed = time.monotonic() - started

            self.assertFalse(result.success)
            self.assertEqual(result.status, "needs_update")
            self.assertEqual(result.details["marker"], exact_marker)
            self.assertLess(elapsed, 0.5)

    def test_naruto_completion_and_reward_visuals(self):
        import cv2
        import numpy as np

        home_hsv = np.zeros((720, 1280, 3), dtype=np.uint8)
        home_hsv[:, :, 2] = 60
        home_hsv[0:101, 192:1088] = (108, 180, 100)
        home_hsv[100:340, 250:900] = (108, 180, 100)
        home = cv2.cvtColor(home_hsv, cv2.COLOR_HSV2BGR)
        self.assertTrue(_naruto_visual_metrics(home)["home_page"])

        battle_prep_hsv = home_hsv.copy()
        battle_prep_hsv[0:101, 192:1088] = (150, 80, 150)
        battle_prep = cv2.cvtColor(battle_prep_hsv, cv2.COLOR_HSV2BGR)
        battle_metrics = _naruto_visual_metrics(battle_prep)
        self.assertGreaterEqual(battle_metrics["home_blue_fraction"], 0.60)
        self.assertFalse(battle_metrics["home_page"])

        popup = np.full((720, 1280, 3), 30, dtype=np.uint8)
        popup[120:600, 40:1240] = 255
        popup_metrics = _naruto_visual_metrics(popup)
        self.assertTrue(popup_metrics["completion_popup"])
        self.assertFalse(popup_metrics["shadow_mandatory_update_popup"])

        mandatory_update = np.full((720, 1280, 3), 30, dtype=np.uint8)
        mandatory_update[78:646, 48:1232] = 255
        for x1, x2 in ((410, 625), (655, 870)):
            cv2.rectangle(mandatory_update, (x1, 450), (x2, 560),
                          (190, 190, 190), thickness=-1)
            cv2.rectangle(mandatory_update, (x1 + 5, 455), (x2 - 5, 555),
                          (255, 255, 255), thickness=-1)
        update_metrics = _naruto_visual_metrics(mandatory_update)
        self.assertTrue(update_metrics["shadow_mandatory_update_popup"])
        self.assertGreaterEqual(update_metrics["popup_white_bottom_fraction"], 0.87)

        portrait_dialog = np.full((1280, 720, 3), 30, dtype=np.uint8)
        portrait_dialog[210:990, 45:675] = 255
        portrait_metrics = _naruto_visual_metrics(portrait_dialog)
        self.assertTrue(portrait_metrics["portrait_white_dialog"])
        self.assertFalse(portrait_metrics["completion_popup"])
        self.assertFalse(portrait_metrics["shadow_update_choice_popup"])

        portrait_update = portrait_dialog.copy()
        for x1, x2 in ((150, 330), (390, 570)):
            cv2.rectangle(portrait_update, (x1, 780), (x2, 875),
                          (190, 190, 190), thickness=-1)
            cv2.rectangle(portrait_update, (x1 + 5, 785), (x2 - 5, 870),
                          (255, 255, 255), thickness=-1)
        self.assertTrue(
            _naruto_visual_metrics(portrait_update)["shadow_update_choice_popup"])

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

    def test_naruto_lobby_uses_fixed_entry_icons_not_background_color(self):
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory() as tmp:
            reference = np.full((720, 1280, 3), (80, 35, 20), dtype=np.uint8)
            candidate = np.full((720, 1280, 3), (20, 90, 150), dtype=np.uint8)
            regions = ((.02, .83, .12, .99), (.11, .83, .21, .99),
                       (.18, .83, .29, .99), (.28, .83, .39, .99))
            for index, (x1, y1, x2, y2) in enumerate(regions):
                for image in (reference, candidate):
                    h, w = image.shape[:2]
                    left, top = round(w * x1), round(h * y1)
                    right, bottom = round(w * x2), round(h * y2)
                    cv2.rectangle(image, (left + 8, top + 8),
                                  (right - 8, bottom - 8), (255, 255, 255), 3)
                    cv2.putText(image, str(index), (left + 25, bottom - 25),
                                cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 0), 4)
            reference_path = Path(tmp) / "lobby.png"
            cv2.imwrite(str(reference_path), reference)

            metrics = _naruto_lobby_icon_metrics(
                candidate, [str(reference_path)], threshold=.5, min_matches=3)
            self.assertTrue(metrics["home_page"])
            self.assertGreaterEqual(metrics["lobby_icon_match_count"], 3)

            false_page = np.full((720, 1280, 3), (120, 80, 20), dtype=np.uint8)
            false_metrics = _naruto_lobby_icon_metrics(
                false_page, [str(reference_path)], threshold=.5, min_matches=3)
            self.assertFalse(false_metrics["home_page"])

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

    def test_azur_lane_idle_cleanup_logs_do_not_cancel_completion(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "Alas.exe"; exe.write_bytes(b"")
            logs = root / "log"; logs.mkdir()
            log = logs / "today_alas.txt"
            log.write_text("", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            def write_log():
                time.sleep(0.1)
                log.write_text(
                    "Scheduler: Start task `Main`\n"
                    "Scheduler: End task `Main`\n"
                    "No task pending\n",
                    encoding="utf-8")
                time.sleep(0.04)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write(
                        "[Task] Main (Enable, tomorrow)\n"
                        "Wait until tomorrow for task `Main`\n"
                        "Goto main page during wait\n")

            threading.Thread(target=write_log, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "*_alas.txt"),
                    "timeout": 2, "completion_quiet_seconds": 0.12,
                    "log_retry_interval": 0.02,
                    "clean_existing": False, "close_on_complete": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners._click_alas_start_button",
                          return_value=(True, "clicked")):
                result = run_alas_gui(step, ctx)
            self.assertTrue(result.success, result.message)

    def test_azur_lane_update_wait_is_once_and_does_not_consume_startup_timeout(self):
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

            def write_log():
                time.sleep(0.65)
                log.write_text("Scheduler: Start task\nNo task pending", encoding="utf-8")

            threading.Thread(target=write_log, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "*_alas.txt"),
                    "startup_update_wait": 0.5, "startup_timeout": 0.4,
                    "timeout": 2, "completion_quiet_seconds": 0.01,
                    "log_retry_interval": 0.02, "clean_existing": False,
                    "close_on_complete": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners._click_alas_start_button",
                          return_value=(True, "clicked")):
                result = run_alas_gui(step, ctx)
            self.assertTrue(result.success, result.message)
            self.assertNotIn("startup_update_wait", step)

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

    def test_maaend_framework_failure_waits_for_remaining_primary_tasks(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "MaaEnd.exe"; exe.write_bytes(b"")
            logs = root / "debug"; logs.mkdir()
            high = logs / "2026-07-20-1.log"; high.write_text("", encoding="utf-8")
            framework = logs / "maafw.log"; framework.write_text("", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())
            remaining_finished = threading.Event()

            def write_failure_then_finish():
                time.sleep(0.04)
                high.write_text(
                    "实例 全套日常: 开始执行任务, 数量: 5, 分段: primary:4, trailing:1\n"
                    "实例 全套日常: 前段任务已提交, task_ids: [1,2,3,4]\n", encoding="utf-8")
                framework.write_text(
                    '!!!OnEventNotify!!! [msg=Tasker.Task.Succeeded] '
                    '[details={"entry":"DijiangRewards","task_id":1}]\n'
                    '!!!OnEventNotify!!! [msg=Tasker.Task.Succeeded] '
                    '[details={"entry":"SellProductSchedule","task_id":2}]\n'
                    '!!!OnEventNotify!!! [msg=Tasker.Task.Failed] '
                    '[details={"entry":"CreditShoppingMain","task_id":3}]\n', encoding="utf-8")
                time.sleep(0.12)
                with framework.open("a", encoding="utf-8") as handle:
                    handle.write('!!!OnEventNotify!!! [msg=Tasker.Task.Succeeded] '
                                 '[details={"entry":"DailyRewardStart","task_id":4}]\n')
                with high.open("a", encoding="utf-8") as handle:
                    handle.write("实例 全套日常: 收尾段切换为 Dummy Controller\n"
                                 "实例 全套日常: 任务已提交, task_ids: [1,2,3,4,5]\n")
                remaining_finished.set()

            threading.Thread(target=write_failure_then_finish, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "20??-??-??-*.log"),
                    "framework_log_path": str(framework), "timeout": 2,
                    "poll_interval": 0.01, "failed_settle_seconds": 0.3,
                    "clean_existing": False, "close_on_complete": False,
                    "gui_click_fallback": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()):
                result = run_maaend_gui(step, ctx)
            self.assertTrue(remaining_finished.is_set())
            self.assertFalse(result.success)
            self.assertIn("CreditShoppingMain", result.message)
            self.assertEqual(result.details["terminal_primary"], [1, 2, 3, 4])

    def test_maaend_recovers_stalled_credit_shop_menu_with_direct_game_click(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "MaaEnd.exe"; exe.write_bytes(b"")
            logs = root / "debug"; logs.mkdir()
            high = logs / "2026-07-20-2.log"; high.write_text("", encoding="utf-8")
            framework = logs / "maafw.log"; framework.write_text("", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            def write_stall_then_success():
                time.sleep(0.03)
                high.write_text(
                    "实例 全套日常: 开始执行任务, 数量: 4, 分段: primary:4, trailing:0\n"
                    "实例 全套日常: 前段任务已提交, task_ids: [1,2,3,4]\n", encoding="utf-8")
                framework.write_text(
                    '!!!OnEventNotify!!! [msg=Node.PipelineNode.Starting] '
                    '[details={"name":"__ScenePrivateWorldEnterMenuList","task_id":3}]\n',
                    encoding="utf-8")
                time.sleep(0.12)
                with framework.open("a", encoding="utf-8") as handle:
                    for task_id, entry in enumerate(("DijiangRewards", "SellProductSchedule",
                                                     "CreditShoppingMain", "DailyRewardStart"), 1):
                        handle.write('!!!OnEventNotify!!! [msg=Tasker.Task.Succeeded] '
                                     f'[details={{"entry":"{entry}","task_id":{task_id}}}]\n')
                with high.open("a", encoding="utf-8") as handle:
                    handle.write("实例 全套日常: 收尾段切换为 Dummy Controller\n"
                                 "实例 全套日常: 任务已提交, task_ids: [1,2,3,4]\n")

            threading.Thread(target=write_stall_then_success, daemon=True).start()
            step = {"executable": str(exe), "log_glob": str(logs / "20??-??-??-*.log"),
                    "framework_log_path": str(framework), "timeout": 2,
                    "poll_interval": 0.01, "credit_menu_recovery_after": 0.02,
                    "credit_menu_recovery_interval": 0.02, "credit_menu_recovery_max": 1,
                    "clean_existing": False, "close_on_complete": False,
                    "gui_click_fallback": False, "game_process_image": "Endfield.exe",
                    "game_title_contains": "Endfield"}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners._click_game_client_ratio",
                          return_value=(True, "clicked")) as click:
                result = run_maaend_gui(step, ctx)
            self.assertTrue(result.success)
            click.assert_called_once_with("Endfield.exe", "Endfield", 0.970, 0.056)

    def test_log_gui_daily_retries_click_and_captures_reward(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "Launcher.exe"; exe.write_bytes(b"")
            log = root / "daily.log"; log.write_text("OLD\n", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            def write_log():
                time.sleep(0.3)
                log.write_text("RUN START\nREWARD PAGE\nALL DONE\n", encoding="utf-8")

            threading.Thread(target=write_log, daemon=True).start()
            step = {"display_name": "测试每日", "executable": str(exe),
                    "log_glob": str(log), "process_images": ["Launcher.exe"],
                    "launch_args": ["main"],
                    "button_names": ["完整运行"], "title_hints": ["Test"],
                    "log_retry_interval": 0.05, "start_markers": ["RUN START"],
                    "completion_markers": ["ALL DONE"],
                    "screenshot_markers": ["REWARD PAGE"],
                    "screenshot_path": str(root / "reward.png"),
                    "bring_game_to_front": True, "game_process_image": "Game.exe",
                    "completion_quiet_seconds": 0.05,
                    "cleanup_process_images": [],
                    "timeout": 2, "clean_existing": False, "close_on_complete": True}
            with patch("gameflow.runners.subprocess.Popen",
                       return_value=FakeProcess()) as launch, \
                    patch("gameflow.runners._click_named_gui_button",
                          return_value=(True, "clicked")) as click, \
                    patch("gameflow.runners._bring_game_window_to_front",
                          return_value=Result(True, "foreground")) as foreground, \
                    patch("gameflow.runners.run_window_screenshot",
                          return_value=Result(True, "saved")) as screenshot, \
                    patch("gameflow.runners._close_matching_script_gui",
                          return_value=(1, "closed")) as close_gui:
                result = run_log_gui_daily(step, ctx)
            self.assertTrue(result.success)
            self.assertEqual(launch.call_args.args[0], [str(exe), "main"])
            self.assertGreaterEqual(click.call_count, 2)
            foreground.assert_called_once()
            screenshot.assert_called_once()
            close_gui.assert_called_once_with(FakeProcess.pid, ["Launcher.exe"], ["Test"])

    def test_log_gui_daily_caps_start_button_clicks(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "Launcher.exe"
            exe.write_bytes(b"")
            log = root / "daily.log"
            log.write_text("", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())
            step = {
                "display_name": "星穹铁道完整运行",
                "executable": str(exe),
                "log_glob": str(log),
                "process_images": ["Launcher.exe"],
                "initial_click_delay": 0,
                "log_retry_interval": 0.01,
                "max_start_clicks": 3,
                "timeout": 2,
                "clean_existing": False,
                "close_on_complete": False,
            }
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners._click_named_gui_button",
                          return_value=(True, "clicked")) as click:
                result = run_log_gui_daily(step, ctx)
            self.assertFalse(result.success)
            self.assertIn("3 次启动点击上限", result.message)
            self.assertEqual(result.details["start_clicks"], 3)
            self.assertEqual(click.call_count, 3)

    def test_log_gui_daily_reports_update_without_retryable_failure(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "Launcher.exe"
            exe.write_bytes(b"")
            log = root / "daily.log"
            log.write_text("", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            def write_update():
                time.sleep(0.05)
                log.write_text("GitHub 发现新版本: v2026.7.26\n", encoding="utf-8")

            threading.Thread(target=write_update, daemon=True).start()
            step = {
                "display_name": "星穹铁道完整运行",
                "executable": str(exe),
                "log_glob": str(log),
                "process_images": ["Launcher.exe"],
                "update_markers": ["GitHub 发现新版本"],
                "initial_click_delay": 10,
                "log_retry_interval": 0.01,
                "timeout": 2,
                "clean_existing": False,
                "close_on_complete": False,
            }
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()):
                result = run_log_gui_daily(step, ctx)
            self.assertFalse(result.success)
            self.assertEqual(result.status, "needs_update")
            self.assertIn("需要更新", result.message)

    def test_log_gui_daily_reports_game_update_when_confirm_is_followed_by_exit(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "Launcher.exe"
            exe.write_bytes(b"")
            log = root / "daily.log"
            log.write_text("", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            def write_update_exit():
                time.sleep(0.05)
                log.write_text(
                    "指令[ 一条龙 ] 节点 检测游戏窗口 返回状态 未打开游戏窗口\n"
                    "指令[ 进入游戏 ] 节点 检测游戏窗口 -> 画面识别 返回状态 确定\n"
                    "RuntimeError: 游戏窗口未就绪\n"
                    "指令[ 一条龙 ] 执行失败 返回状态 异常\n",
                    encoding="utf-8")

            threading.Thread(target=write_update_exit, daemon=True).start()
            step = {
                "display_name": "绝区零一条龙",
                "executable": str(exe),
                "log_glob": str(log),
                "process_images": ["Launcher.exe"],
                "start_markers": ["指令[ 一条龙 ] 节点 检测游戏窗口"],
                "error_markers": ["指令[ 一条龙 ] 执行失败"],
                "state_watchdog_regex":
                    r"指令\[\s*([^\]]+)\s*\]\s*节点\s*(.*?)\s*->.*?返回状态\s*(.*)$",
                "update_failure_state_regex": r"^进入游戏 \| 检测游戏窗口 \| 确定$",
                "update_failure_markers": ["RuntimeError: 游戏窗口未就绪"],
                "initial_click_delay": 10,
                "log_retry_interval": 0.01,
                "timeout": 2,
                "clean_existing": False,
                "close_on_complete": False,
            }
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()):
                result = run_log_gui_daily(step, ctx)
            self.assertFalse(result.success)
            self.assertEqual(result.status, "needs_update")
            self.assertEqual(result.details["reason_code"], "game_update_required")
            self.assertEqual(result.details["marker"], "RuntimeError: 游戏窗口未就绪")

    def test_log_gui_daily_update_marker_wins_even_when_dismiss_is_enabled(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "Launcher.exe"
            exe.write_bytes(b"")
            log = root / "daily.log"
            log.write_text("", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())
            def write_update():
                time.sleep(0.03)
                log.write_text("GitHub 发现新版本: v2026.7.26\n", encoding="utf-8")

            threading.Thread(target=write_update, daemon=True).start()
            step = {
                "display_name": "星穹铁道完整运行",
                "executable": str(exe),
                "log_glob": str(log),
                "process_images": ["Launcher.exe"],
                "button_names": ["完整运行"],
                "update_markers": ["GitHub 发现新版本"],
                "dismiss_update_notice": True,
                "skip_on_update": False,
                "update_ack_button_names": ["好的"],
                "update_ack_x_ratio": 0.366,
                "update_ack_y_ratio": 0.912,
                "update_ack_settle_seconds": 0.01,
                "update_ack_retry_interval": 0.2,
                "initial_click_delay": 0.2,
                "log_retry_interval": 0.01,
                "start_markers": ["开始运行"],
                "completion_markers": ["每日实训已完成"],
                "completion_quiet_seconds": 0.01,
                "timeout": 2,
                "clean_existing": False,
                "close_on_complete": False,
            }
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners._click_named_gui_button",
                          return_value=(True, "clicked")) as click:
                result = run_log_gui_daily(step, ctx)
            self.assertFalse(result.success)
            self.assertEqual(result.status, "needs_update")
            self.assertIn("GitHub 发现新版本", result.details["marker"])
            click.assert_not_called()

    def test_log_gui_daily_irrelevant_prestart_logs_do_not_postpone_click(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "Launcher.exe"
            exe.write_bytes(b"")
            log = root / "daily.log"
            log.write_text("", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            def emit_irrelevant_logs():
                for index in range(20):
                    time.sleep(0.01)
                    with log.open("a", encoding="utf-8") as handle:
                        handle.write(f"background heartbeat {index}\n")

            def click(*args):
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("RUN START\nALL DONE\n")
                return True, "clicked"

            emitter = threading.Thread(target=emit_irrelevant_logs, daemon=True)
            emitter.start()
            step = {
                "display_name": "测试每日",
                "executable": str(exe),
                "log_glob": str(log),
                "process_images": ["Launcher.exe"],
                "button_names": ["完整运行"],
                "initial_click_delay": 0,
                "log_retry_interval": 0.05,
                "startup_timeout": 0.12,
                "max_start_clicks": 1,
                "start_markers": ["RUN START"],
                "completion_markers": ["ALL DONE"],
                "completion_quiet_seconds": 0,
                "timeout": 1,
                "clean_existing": False,
                "close_on_complete": False,
            }
            with patch("gameflow.runners.subprocess.Popen",
                       return_value=FakeProcess()), patch(
                    "gameflow.runners._click_named_gui_button",
                    side_effect=click) as start_click:
                result = run_log_gui_daily(step, ctx)

            emitter.join(1)
            self.assertTrue(result.success, result.message)
            start_click.assert_called_once()

    def test_log_gui_daily_acknowledges_startup_modal_before_start(self):
        class FakeProcess:
            pid = 123456789
            returncode = None
            def poll(self): return None
            def terminate(self): self.returncode = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "Launcher.exe"
            exe.write_bytes(b"")
            log = root / "daily.log"
            log.write_text("", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())
            calls = []

            def click(root_pid, process_images, title_hints, names, x_ratio, y_ratio):
                calls.append((list(names), x_ratio, y_ratio))
                if names == ["我已知晓", "好的"]:
                    with log.open("a", encoding="utf-8") as handle:
                        handle.write("开始运行\n每日实训已完成\n")
                return True, "clicked"

            step = {
                "display_name": "星穹铁道完整运行",
                "executable": str(exe),
                "log_glob": str(log),
                "process_images": ["Launcher.exe"],
                "button_names": ["完整运行"],
                "startup_ack_button_names": ["我已知晓", "好的"],
                "startup_ack_x_ratio": 0.28,
                "startup_ack_y_ratio": 0.865,
                "startup_ack_after": 0,
                "startup_ack_settle_seconds": 0.01,
                "max_startup_ack_clicks": 1,
                "initial_click_delay": 1,
                "log_retry_interval": 0.01,
                "start_markers": ["开始运行"],
                "completion_markers": ["每日实训已完成"],
                "completion_quiet_seconds": 0.01,
                "timeout": 2,
                "clean_existing": False,
                "close_on_complete": False,
            }
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners._click_named_gui_button",
                          side_effect=click):
                result = run_log_gui_daily(step, ctx)
            self.assertTrue(result.success, result.message)
            self.assertEqual(calls, [(["我已知晓", "好的"], 0.28, 0.865)])
            self.assertEqual(result.details["startup_ack_clicks"], 1)

    def test_log_gui_daily_waits_for_detached_gui_after_launcher_exits(self):
        class ExitedLauncher:
            pid = 123456789
            returncode = 0
            def poll(self): return 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "Launcher.exe"; exe.write_bytes(b"")
            log = root / "daily.log"; log.write_text("", encoding="utf-8")
            messages = []
            ctx = RunContext(root, {}, messages.append, threading.Event())

            def write_log():
                time.sleep(0.2)
                log.write_text("RUN START\nALL DONE\n", encoding="utf-8")

            threading.Thread(target=write_log, daemon=True).start()
            step = {"display_name": "绝区零一条龙", "executable": str(exe),
                    "log_glob": str(log), "process_images": ["Launcher.exe"],
                    "button_names": ["启动一条龙"], "title_hints": ["绝区零 一条龙"],
                    "initial_click_delay": 0, "log_retry_interval": 0.05,
                    "gui_ready_timeout": 1, "start_markers": ["RUN START"],
                    "completion_markers": ["ALL DONE"], "completion_quiet_seconds": 0.05,
                    "timeout": 2, "clean_existing": False, "close_on_complete": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=ExitedLauncher()), patch(
                    "gameflow.runners._click_named_gui_button",
                    return_value=(False, "GUI loading")), patch(
                    "gameflow.runners._process_image_exists", return_value=False), patch(
                    "gameflow.runners._matching_visible_window_exists", return_value=False):
                result = run_log_gui_daily(step, ctx)

            self.assertTrue(result.success)
            self.assertTrue(any("真正 GUI 会由独立进程延迟创建" in item
                                for item in messages))

    def test_log_gui_daily_stops_querying_detached_gui_after_log_start(self):
        class ExitedLauncher:
            pid = 123456789
            returncode = 0
            def poll(self): return 0
            def terminate(self): pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); exe = root / "Launcher.exe"; exe.write_bytes(b"")
            log = root / "daily.log"; log.write_text("", encoding="utf-8")
            ctx = RunContext(root, {}, lambda _: None, threading.Event())

            def write_log():
                time.sleep(0.03)
                log.write_text(
                    "RUN START\n"
                    "指令[ 一条龙 ] 节点 运行 -> 结束 返回状态 进行中\n",
                    encoding="utf-8")
                time.sleep(0.15)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("ALL DONE\n")

            threading.Thread(target=write_log, daemon=True).start()
            step = {"display_name": "绝区零一条龙", "executable": str(exe),
                    "log_glob": str(log), "process_images": ["Launcher.exe"],
                    "title_hints": ["绝区零 一条龙"], "start_markers": ["RUN START"],
                    "completion_markers": ["ALL DONE"], "completion_quiet_seconds": 0.01,
                    "log_retry_interval": 0.01, "initial_click_delay": 10,
                    "timeout": 2, "clean_existing": False, "close_on_complete": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=ExitedLauncher()), \
                    patch("gameflow.runners._matching_visible_window_exists",
                          return_value=True) as window_probe:
                result = run_log_gui_daily(step, ctx)
            self.assertTrue(result.success)
            self.assertLessEqual(window_probe.call_count, 5)

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
                    "state_stall_seconds": 0.08, "state_recovery_seconds": 0.05,
                    "targeted_state_recovery_regex": r"^出战 \| 检测游戏窗口 \| 按钮-出战$",
                    "targeted_state_recovery_after": 0.02,
                    "targeted_state_recovery_interval": 0.02,
                    "targeted_state_recovery_max": 1,
                    "targeted_state_recovery_clicks": [[0.48, 0.14, 0],
                                                       [0.906, 0.957, 0]],
                    "game_process_image": "Game.exe", "screenshot_path": str(root / "failure.png"),
                    "initial_click_delay": 10,
                    "timeout": 3, "clean_existing": False, "close_on_complete": False}
            with patch("gameflow.runners.subprocess.Popen", return_value=FakeProcess()), \
                    patch("gameflow.runners._bring_game_window_to_front",
                          return_value=Result(True, "foreground")), \
                    patch("gameflow.runners._click_game_client_ratio",
                          return_value=(True, "clicked")) as targeted_click, \
                    patch("gameflow.runners.run_window_screenshot",
                          return_value=Result(True, "saved")) as screenshot:
                result = run_log_gui_daily(step, ctx)
            self.assertFalse(result.success)
            self.assertIn("按钮-出战", result.message)
            self.assertEqual(targeted_click.call_args_list, [
                call("Game.exe", "", 0.48, 0.14),
                call("Game.exe", "", 0.906, 0.957),
            ])
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
                "trial": {"test_only": True,
                          "steps": [{"id": "trial", "runner": "delay", "seconds": 0.05}]},
                "self_test": {"steps": [{"id": "x", "runner": "delay", "seconds": 0.05}]}
            }}
            manager = WorkflowManager(root, config, Store(root / "db.sqlite"))
            ok, _ = manager.start_daily(["b", "trial", "self_test"], 9)
            self.assertTrue(ok)
            manager.join(5)
            batch = manager.state()["batch"]
            self.assertEqual(batch["max_parallel"], 1)
            self.assertEqual([x["workflow"] for x in batch["completed"]], ["b"])

    def test_daily_batch_retries_only_failed_workflows_once(self):
        attempts = {"flaky": 0, "good": 0}

        def flaky_runner(step, ctx):
            attempts["flaky"] += 1
            if attempts["flaky"] == 1:
                return Result(False, "first failure")
            return Result(True, "retry success")

        def good_runner(step, ctx):
            attempts["good"] += 1
            return Result(True, "first success")

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                "gameflow.engine.RUNNERS",
                {"flaky_test": flaky_runner, "good_test": good_runner}):
            root = Path(tmp)
            config = {
                "daily_retry": {"enabled": True,
                                "statuses": ["failed", "interrupted"]},
                "workflows": {
                    "flaky": {"steps": [{"id": "run", "runner": "flaky_test"}]},
                    "good": {"steps": [{"id": "run", "runner": "good_test"}]},
                },
            }
            manager = WorkflowManager(root, config, Store(root / "db.sqlite"))
            self.assertTrue(manager.start_daily(["flaky", "good"], 2)[0])
            manager.join(5)
            completed = {item["workflow"]: item
                         for item in manager.state()["batch"]["completed"]}
        self.assertEqual(attempts, {"flaky": 2, "good": 1})
        self.assertEqual(completed["flaky"]["first_status"], "failed")
        self.assertEqual(completed["flaky"]["status"], "success")
        self.assertTrue(completed["flaky"]["retried"])
        self.assertEqual(completed["good"]["status"], "success")
        self.assertNotIn("retried", completed["good"])

    def test_daily_batch_does_not_retry_skip_update_or_cancel(self):
        attempts = {"update": 0, "skip": 0}

        def update_runner(step, ctx):
            attempts["update"] += 1
            return Result(False, "game update", status="needs_update")

        def skip_runner(step, ctx):
            attempts["skip"] += 1
            return Result(False, "maintenance", status="skipped")

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                "gameflow.engine.RUNNERS",
                {"update_test": update_runner, "skip_test": skip_runner}):
            root = Path(tmp)
            config = {"workflows": {
                "update": {"steps": [{"id": "run", "runner": "update_test"}]},
                "skip": {"steps": [{"id": "run", "runner": "skip_test"}]},
            }}
            manager = WorkflowManager(root, config, Store(root / "db.sqlite"))
            self.assertTrue(manager.start_daily(["update", "skip"], 2)[0])
            manager.join(5)
            completed = manager.state()["batch"]["completed"]
        self.assertEqual(attempts, {"update": 1, "skip": 1})
        self.assertTrue(all(item.get("round") == 1 for item in completed))

    def test_qq_reader_trial_restores_saved_gui_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "maa_gui_config.json"
            original = {"tasks": [{"name": "normal", "enabled": True,
                                     "minutes": 99, "count": 33}]}
            settings.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
            step = {
                "gui_settings_path": str(settings),
                "trial_tasks": [{"name": "trial", "enabled": True,
                                  "minutes": 1, "count": 1}],
            }
            ctx = RunContext(Path(tmp), {}, lambda _: None, threading.Event())
            with patch("gameflow.runners.run_log_gui_daily",
                       return_value=Result(True, "ok")) as delegated:
                result = run_qq_reader_trial(step, ctx)
                active = json.loads(settings.read_text(encoding="utf-8"))
            self.assertTrue(result.success)
            delegated.assert_called_once_with(step, ctx)
            self.assertEqual(active, original)

    def test_cancel_standalone_stops_only_requested_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {"workflows": {
                "a": {"steps": [{"id": "wait", "runner": "delay", "seconds": 0.5}]},
                "b": {"steps": [{"id": "wait", "runner": "delay", "seconds": 0.15}]},
            }}
            manager = WorkflowManager(root, config, Store(root / "db.sqlite"))
            self.assertTrue(manager.start("a")[0])
            self.assertTrue(manager.start("b")[0])
            ok, _ = manager.cancel("a")
            self.assertTrue(ok)
            manager.join(2)
            states = manager.workflow_states()
            self.assertEqual(states["a"].get("last_status"), "cancelled")
            self.assertEqual(states["b"].get("last_status"), "success")

    def test_cancel_pending_batch_item_removes_and_records_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {"workflows": {
                "a": {"steps": [{"id": "wait", "runner": "delay", "seconds": 0.2}]},
                "b": {"steps": [{"id": "wait", "runner": "delay", "seconds": 0.2}]},
            }}
            manager = WorkflowManager(root, config, Store(root / "db.sqlite"))
            self.assertTrue(manager.start_daily(["a", "b"], 1)[0])
            deadline = time.monotonic() + 2
            while "b" not in manager.state()["batch"]["queue"] and time.monotonic() < deadline:
                time.sleep(0.01)
            ok, _ = manager.cancel("b")
            self.assertTrue(ok)
            snapshot = manager.state()["batch"]
            self.assertNotIn("b", snapshot["queue"])
            self.assertTrue(any(item["workflow"] == "b" and item["status"] == "cancelled"
                                for item in snapshot["completed"]))
            manager.join(3)
            completed = manager.state()["batch"]["completed"]
            self.assertEqual(sum(item["workflow"] == "b" for item in completed), 1)
            self.assertEqual(next(item["status"] for item in completed
                                  if item["workflow"] == "b"), "cancelled")
            self.assertEqual(next(item["status"] for item in completed
                                  if item["workflow"] == "a"), "success")

    def test_cancel_active_batch_item_keeps_other_engine_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {"workflows": {
                "a": {"steps": [{"id": "wait", "runner": "delay", "seconds": 0.5}]},
                "b": {"steps": [{"id": "wait", "runner": "delay", "seconds": 0.15}]},
            }}
            manager = WorkflowManager(root, config, Store(root / "db.sqlite"))
            self.assertTrue(manager.start_daily(["a", "b"], 2)[0])
            deadline = time.monotonic() + 2
            while "a" not in manager.state()["batch"]["active"] and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(manager.cancel("a")[0])
            manager.join(3)
            completed = {item["workflow"]: item["status"]
                         for item in manager.state()["batch"]["completed"]}
            self.assertEqual(completed["a"], "cancelled")
            self.assertEqual(completed["b"], "success")

    def test_cancel_api_cancels_named_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {"workflows": {
                "a": {"steps": [{"id": "wait", "runner": "delay", "seconds": 0.5}]},
            }}
            manager = WorkflowManager(root, config, Store(root / "db.sqlite"))
            self.assertTrue(manager.start("a")[0])
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(manager))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                connection.request("POST", "/api/cancel?workflow=a")
                response = connection.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                self.assertTrue(payload["ok"])
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
            manager.join(2)
            self.assertEqual(manager.workflow_states()["a"].get("last_status"), "cancelled")

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
