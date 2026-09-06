import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gameflow.config import (ConfigError, load_config, resolve_project_paths,
                             validate_runtime_config)
from gameflow.engine import Engine
from gameflow.settings import (HostSettings, HostSettingsError, PortError,
                               RuntimeSettings, apply_device_port,
                               apply_host_settings,
                               apply_workflow_device_port,
                               sync_bundled_tool_ports,
                               sync_bundled_tool_profiles, validate_port,
                               workflow_host_path_errors)
from gameflow.store import Store
from gameflow.web import PAGE


class PortableGameFlowTests(unittest.TestCase):
    def test_mutable_state_token_resolves_outside_release(self):
        release = Path("C:/portable/dist/GameFlow")
        state = Path("C:/portable/dist/GameFlow.state")
        resolved = resolve_project_paths(
            {"resource": "${GAMEFLOW_ROOT}/resources/tool.exe",
             "evidence": "${GAMEFLOW_STATE}/logs/final.png"},
            release, state)
        self.assertEqual(Path(resolved["resource"]),
                         release / "resources" / "tool.exe")
        self.assertEqual(Path(resolved["evidence"]), state / "logs" / "final.png")

    def test_legacy_dashboard_and_task_controls_are_preserved(self):
        for required in ('id="tasks"', 'id="parallel"', 'id="startDaily"',
                         'id="logView"', 'id="history"', 'onclick="stopAll()"'):
            self.assertIn(required, PAGE)
        self.assertIn("GameFlow 次元作战终端", PAGE)
        self.assertIn("toggleWorkflow", PAGE)
        self.assertIn("cancelOne", PAGE)

    def test_port_validation_accepts_only_integer_range(self):
        self.assertEqual(validate_port("1"), 1)
        self.assertEqual(validate_port(65535), 65535)
        for value in ("", "1.5", "abc", 0, 65536, True, "１２３"):
            with self.subTest(value=value), self.assertRaises(PortError):
                validate_port(value)

    def test_last_valid_port_is_persisted_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime_settings.json"
            settings = RuntimeSettings(path, 5555)
            self.assertEqual(settings.port, 5555)
            settings.save_port(16384)
            self.assertEqual(RuntimeSettings(path, 5555).port, 16384)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["emulator_port"], 16384)

    def test_host_paths_are_discovered_and_persisted_outside_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ldconsole = root / "leidian" / "LDPlayer9" / "ldconsole.exe"
            manager = (root / "Program Files" / "Netease" /
                       "MuMu Player 12" / "shell" / "MuMuManager.exe")
            endfield = (root / "Hypergryph Launcher" / "games" /
                        "Endfield Game" / "Endfield.exe")
            for executable in (ldconsole, manager, manager.parent / "adb.exe", endfield):
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_bytes(b"")
            path = root / "portable" / "data" / "host_settings.json"
            discovered = HostSettings(path, roots=[root], environ={}).resolve()
            self.assertEqual(Path(discovered["ldconsole"]), ldconsole)
            self.assertEqual(Path(discovered["mumu_manager"]), manager)
            self.assertEqual(Path(discovered["mumu_adb"]), manager.parent / "adb.exe")
            self.assertEqual(Path(discovered["endfield_game"]), endfield)
            self.assertEqual(HostSettings(path, roots=[], environ={}).resolve(), discovered)

    def test_manual_host_paths_survive_restart_and_reject_invalid_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shell = root / "relocated" / "mumu" / "shell"
            shell.mkdir(parents=True)
            (shell / "MuMuManager.exe").write_bytes(b"")
            (shell / "adb.exe").write_bytes(b"")
            path = root / "data" / "host_settings.json"
            configured = HostSettings(path, roots=[], environ={}).configure(mumu=str(shell))
            reloaded = HostSettings(path, roots=[], environ={}).resolve()
            self.assertEqual(reloaded, configured)
            with self.assertRaises(HostSettingsError):
                HostSettings(path, roots=[], environ={}).configure(
                    ldplayer=str(root / "missing"))

    def test_invalid_required_host_path_fails_before_runner_thread_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {
                "tools": {},
                "workflows": {"test": {
                    "required_host_tools": ["ldconsole"],
                    "steps": [{"id": "never", "runner": "delay", "seconds": 0}],
                }},
            }
            apply_host_settings(config, {"ldconsole": str(root / "missing.exe")})
            self.assertTrue(workflow_host_path_errors(config, "test"))
            engine = Engine(root, config, Store(root / "state.db"))
            ok, message = engine.start("test")
            self.assertFalse(ok)
            self.assertIn("雷电模拟器控制台", message)
            self.assertFalse(engine.state()["running"])

    def test_ui_port_reaches_every_adb_endpoint(self):
        config = {"device": {"address": "old"}, "workflows": {
            "a": {"steps": [{"device": "emulator-5554"},
                              {"adb_device": "127.0.0.1:16384",
                               "env": {"ANDROID_SERIAL": "old"}}]}}}
        apply_device_port(config, 24680)
        self.assertEqual(config["device"]["address"], "127.0.0.1:24680")
        steps = config["workflows"]["a"]["steps"]
        self.assertEqual(steps[0]["device"], "127.0.0.1:24680")
        self.assertEqual(steps[1]["adb_device"], "127.0.0.1:24680")
        self.assertEqual(steps[1]["env"]["ANDROID_SERIAL"], "127.0.0.1:24680")
        self.assertEqual(steps[0]["env"]["ANDROID_SERIAL"], "127.0.0.1:24680")

    def test_workflow_port_override_does_not_touch_other_emulators(self):
        config = {"workflows": {
            "ld": {"steps": [{"runner": "adb", "device": "emulator-5554",
                                "emulator_kind": "ldplayer", "emulator_instance": 0}]},
            "mumu": {"steps": [{"runner": "adb", "device": "127.0.0.1:16384",
                                  "emulator_kind": "mumu", "emulator_instance": 0}]},
        }}
        port, endpoint = apply_workflow_device_port(config, "ld", 5560)
        self.assertEqual((port, endpoint), (5560, "emulator-5560"))
        self.assertEqual(config["workflows"]["ld"]["steps"][0]["device"],
                         "emulator-5560")
        self.assertEqual(config["workflows"]["mumu"]["steps"][0]["device"],
                         "127.0.0.1:16384")

    def test_runtime_settings_persist_endpoint_per_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime_settings.json"
            settings = RuntimeSettings(path, 5555)
            settings.save_endpoint("azur_lane_daily", "mumu", 0,
                                   "127.0.0.1:16400")
            reloaded = RuntimeSettings(path, 5555)
            self.assertEqual(reloaded.endpoint("azur_lane_daily"),
                             "127.0.0.1:16400")
            self.assertEqual(reloaded.endpoint("daily_game"), "")

    def test_runtime_config_validation_rejects_runner_action_and_endpoint_conflict(self):
        config = {"workflows": {
            "a": {"steps": [{"id": "bad", "runner": "adb",
                                "action": "explode", "device": "same"}]},
            "b": {"steps": [{"id": "other", "runner": "adb",
                                "action": "wait", "device": "same"}]},
        }}
        with self.assertRaises(ConfigError) as caught:
            validate_runtime_config(config, {"adb"})
        self.assertIn("action=explode", str(caught.exception))
        self.assertIn("非互斥工作流", str(caught.exception))

    def test_ui_port_is_written_to_every_bundled_tool_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def write(relative, value):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value), encoding="utf-8")
                return path

            maa = write("resources/tools/maa/config/gui.new.json",
                        {"Configurations": [{"Address": "old"}]})
            baas = write("resources/tools/baas/_internal/web/static/baas.json",
                         {"device": {"serial": "old"}})
            naruto = write("resources/tools/naruto/config/instances/default.json",
                           {"AdbDevice": {"AdbSerial": "old", "AdbPath": "old"}})
            gumballs = write("resources/tools/gumballs/config/instances/default.json",
                             {"AdbDevice": {"AdbSerial": "old", "AdbPath": "old"}})
            alas = write("resources/tools/alas/config/alas.json",
                         {"Alas": {"Emulator": {"Serial": "old"},
                                   "EmulatorInfo": {"path": "old"}}})
            qq = write("resources/tools/qq_reader/dev/config/maa_pi_config.json",
                       {"adb": {"address": "old", "adb_path": "old",
                                "config": {"extras": {"mumu": {"enable": True}}}}})

            sync_bundled_tool_ports(root, 34567)
            endpoint = "127.0.0.1:34567"
            self.assertEqual(json.loads(maa.read_text(encoding="utf-8"))
                             ["Configurations"][0]["Address"], endpoint)
            maa_value = json.loads(maa.read_text(encoding="utf-8"))
            maa_connection = maa_value["Configurations"][0]["Gui"]["ConnectSettings"]
            self.assertEqual(Path(maa_connection["AdbPath"]),
                             root / "resources" / "runtime" / "adb.exe")
            self.assertFalse(maa_value["Update"]["CheckOnStartup"])
            self.assertTrue(maa_value["AnnouncementInfo"]["DoNotShow"])
            self.assertEqual(json.loads(baas.read_text(encoding="utf-8"))
                             ["device"]["serial"], endpoint)
            self.assertEqual(json.loads(naruto.read_text(encoding="utf-8"))
                             ["AdbDevice"]["AdbSerial"], endpoint)
            self.assertEqual(json.loads(gumballs.read_text(encoding="utf-8"))
                             ["AdbDevice"]["AdbSerial"], endpoint)
            self.assertEqual(Path(json.loads(naruto.read_text(encoding="utf-8"))
                                  ["AdbDevice"]["AdbPath"]),
                             root / "resources" / "runtime" / "adb.exe")
            alas_value = json.loads(alas.read_text(encoding="utf-8"))["Alas"]
            self.assertEqual(alas_value["Emulator"]["Serial"], endpoint)
            self.assertEqual(alas_value["Emulator"]["ScreenshotMethod"], "ADB")
            self.assertEqual(alas_value["EmulatorInfo"]["path"], "")
            qq_value = json.loads(qq.read_text(encoding="utf-8"))["adb"]
            self.assertEqual(qq_value["address"], endpoint)
            self.assertEqual(qq_value["name"], endpoint)
            self.assertEqual(Path(qq_value["adb_path"]),
                             root / "resources" / "runtime" / "adb.exe")
            self.assertFalse(qq_value["config"]["extras"]["mumu"]["enable"])

    @mock.patch("gameflow.settings.sync_bundled_tool_ports")
    def test_restored_dashboard_preserves_per_game_emulator_endpoints(self, sync):
        config = {"device": {"address": "127.0.0.1:5555"}, "workflows": {
            "daily_game": {"steps": [{"device": "emulator-5556"}]},
            "blue_archive_daily": {"steps": [{"device": "emulator-5560"}]},
            "azur_lane_daily": {"steps": [{"device": "127.0.0.1:16384"}]},
            "naruto_daily": {"steps": [{"device": "emulator-5554"}]},
            "gumballs_daily": {"steps": [{"device": "emulator-5558"}]},
            "qq_reader_trial": {"steps": [{"device": "127.0.0.1:16384"}]},
        }}
        sync_bundled_tool_profiles(Path("C:/portable"), config)
        endpoints = sync.call_args.args[2]
        self.assertEqual(endpoints["maa"], "emulator-5556")
        self.assertEqual(endpoints["baas"], "emulator-5560")
        self.assertEqual(endpoints["alas"], "127.0.0.1:16384")
        self.assertEqual(endpoints["naruto"], "emulator-5554")
        self.assertEqual(endpoints["gumballs"], "emulator-5558")
        self.assertEqual(endpoints["qq_reader"], "127.0.0.1:16384")

    def test_runtime_config_has_no_machine_paths_or_update_features(self):
        root = Path(__file__).resolve().parents[1]
        raw = (root / "config" / "workflow.json").read_text(encoding="utf-8")
        folded = raw.casefold()
        self.assertNotRegex(raw, r'(?i)[a-z]:\\')
        self.assertIn("${GAMEFLOW_STATE}\\\\logs", raw)
        self.assertNotIn("${GAMEFLOW_ROOT}\\\\logs", raw)
        for forbidden in ("smtp", "email_report", "update_markers",
                          "auto_update", "announcement", "promotion"):
            self.assertNotIn(forbidden, folded)
        config = load_config(root / "config" / "workflow.json")
        naruto = next(step for step in config["workflows"]["naruto_daily"]["steps"]
                      if step["id"] == "run_maa_auto_naruto")
        self.assertEqual(naruto["runner"], "log_gui_daily")
        self.assertIn("resources\\tools\\naruto", naruto["executable"])
        self.assertEqual(config["workflows"]["azur_lane_daily"]["steps"][0]["runner"],
                         "mumu_wait")

    def test_shared_gui_runners_do_not_kill_by_image_name(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "gameflow" / "runners.py").read_text(encoding="utf-8")
        generic = source.split("def run_log_gui_daily", 1)[1].split(
            "def run_qq_reader_trial", 1)[0]
        gumballs = source.split("def run_gumballs_gui", 1)[1].split(
            "def run_ldplayer", 1)[0]
        self.assertNotIn('["taskkill", "/IM"', generic)
        self.assertNotIn('["taskkill", "/IM"', gumballs)
        self.assertIn("_terminate_owned_processes(exe, proc.pid)", generic)
        self.assertIn("_terminate_owned_processes(exe, proc.pid)", gumballs)

    def test_launcher_checks_service_identity_and_resource_manifest_is_complete(self):
        root = Path(__file__).resolve().parents[1]
        launcher = (root / "launch_admin.ps1").read_text(encoding="utf-8")
        self.assertIn("api/identity", launcher)
        self.assertIn("identity.pid", launcher)
        manifest = json.loads((root / "config" / "resource_manifest.json")
                              .read_text(encoding="utf-8"))
        required = {"runtime"}
        self.assertEqual({item["source"] for item in manifest["includes"]}, required)

    def test_release_build_is_staged_self_checked_and_preserves_state(self):
        root = Path(__file__).resolve().parents[1]
        build = (root / "build.ps1").read_text(encoding="utf-8")
        swap = (root / "tools" / "swap_release.ps1").read_text(encoding="utf-8")
        ignored = (root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("release-staging", build)
        self.assertIn("GameFlow.exe') status", build)
        self.assertIn("bundled_resources", build)
        self.assertIn("GameFlow.state", swap)
        self.assertIn("@('data', 'logs')", swap)
        ignored_lines = ignored.splitlines()
        self.assertNotIn("*.png", ignored_lines)
        self.assertNotIn("*.zip", ignored_lines)


if __name__ == "__main__":
    unittest.main()
