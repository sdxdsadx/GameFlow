from __future__ import annotations

import json
import os
import string
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence


DEFAULT_DEVICE_PORT = 5555


HOST_TOOL_LABELS = {
    "ldconsole": "雷电模拟器控制台",
    "mumu_manager": "MuMu 12 管理器",
    "mumu_adb": "MuMu 12 ADB",
    "endfield_game": "终末地游戏本体",
    # 各游戏自动化前端程序（剥离 resources/tools 后由用户在 GUI 手动指定路径）
    "maa_gui": "明日方舟 MAA 程序",
    "baas_gui": "碧蓝档案 BAA 程序",
    "alas_gui": "碧蓝航线 Alas 程序",
    "naruto_gui": "火影忍者 MFA 程序",
    "gumballs_gui": "不思议迷宫 MFA 程序",
    "maaend_gui": "终末地 MaaEnd 程序",
    "zenless_gui": "绝区零 OneDragon 程序",
    "star_rail_gui": "星穹铁道 March7th 程序",
    "qq_reader_gui": "QQ 阅读 MaaQQReader 程序",
}


class HostSettingsError(ValueError):
    """Raised when a manually supplied host installation path is invalid."""


def _windows_drive_roots() -> list[Path]:
    if os.name != "nt":
        return []
    return [Path(f"{letter}:/") for letter in string.ascii_uppercase
            if Path(f"{letter}:/").exists()]


class HostSettings:
    """Machine-local executable paths kept outside the portable workflow file."""

    _ENVIRONMENT = {
        "ldconsole": ("LDPLAYER_HOME", "LDPLAYER_PATH"),
        "mumu_manager": ("MUMU_HOME", "MUMU_PATH"),
        "endfield_game": ("ENDFIELD_HOME", "ENDFIELD_GAME_PATH"),
    }
    _RELATIVE_CANDIDATES = {
        "ldconsole": (
            "leidian/LDPlayer9/ldconsole.exe",
            "LDPlayer/LDPlayer9/ldconsole.exe",
            "Program Files/LDPlayer/LDPlayer9/ldconsole.exe",
            "Program Files (x86)/LDPlayer/LDPlayer9/ldconsole.exe",
        ),
        "mumu_manager": (
            "Program Files/Netease/MuMu Player 12/shell/MuMuManager.exe",
            "Program Files (x86)/Netease/MuMu Player 12/shell/MuMuManager.exe",
            "Netease/MuMu Player 12/shell/MuMuManager.exe",
            "MuMu Player 12/shell/MuMuManager.exe",
        ),
        "endfield_game": (
            "Hypergryph Launcher/games/Endfield Game/Endfield.exe",
            "Program Files/Hypergryph Launcher/games/Endfield Game/Endfield.exe",
            "Program Files (x86)/Hypergryph Launcher/games/Endfield Game/Endfield.exe",
        ),
    }
    _FILENAMES = {
        "ldconsole": "ldconsole.exe",
        "mumu_manager": "MuMuManager.exe",
        "mumu_adb": "adb.exe",
        "endfield_game": "Endfield.exe",
        "maa_gui": "MAA.exe",
        "baas_gui": "baas.exe",
        "alas_gui": "Alas.exe",
        "naruto_gui": "MFAAvalonia.exe",
        "gumballs_gui": "MFAAvalonia.exe",
        "maaend_gui": "MaaEnd.exe",
        "zenless_gui": "OneDragon-Launcher.exe",
        "star_rail_gui": "March7th Launcher.exe",
        "qq_reader_gui": "MaaQQReaderGUI.exe",
    }

    def __init__(self, path: Path, *, roots: Sequence[Path] | None = None,
                 environ: Mapping[str, str] | None = None):
        self.path = path
        self._roots = list(roots) if roots is not None else _windows_drive_roots()
        self._environ = dict(os.environ if environ is None else environ)
        self._lock = threading.Lock()
        self._paths = self._load()

    def _load(self) -> dict[str, str]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        raw = value.get("paths", value) if isinstance(value, dict) else {}
        if not isinstance(raw, dict):
            return {}
        return {str(key): str(path).strip() for key, path in raw.items()
                if key in HOST_TOOL_LABELS and str(path).strip()}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"paths": self._paths}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    @staticmethod
    def _from_installation(value: str | Path, filename: str) -> Path:
        candidate = Path(os.path.expandvars(os.path.expanduser(str(value).strip())))
        return candidate / filename if candidate.is_dir() else candidate

    def _environment_candidates(self, key: str) -> list[Path]:
        filename = self._FILENAMES[key]
        values: list[Path] = []
        for variable in self._ENVIRONMENT.get(key, ()):
            raw = self._environ.get(variable, "").strip()
            if raw:
                values.append(self._from_installation(raw, filename))
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            base = self._environ.get(variable, "").strip()
            if not base:
                continue
            base_path = Path(base)
            if key == "ldconsole":
                values.append(base_path / "LDPlayer" / "LDPlayer9" / filename)
            elif key == "mumu_manager":
                values.append(base_path / "Netease" / "MuMu Player 12" / "shell" / filename)
            elif key == "endfield_game":
                values.append(base_path / "Hypergryph Launcher" / "games" /
                              "Endfield Game" / filename)
        return values

    def _discover_one(self, key: str) -> str:
        candidates = self._environment_candidates(key)
        candidates.extend(root / relative for root in self._roots
                          for relative in self._RELATIVE_CANDIDATES.get(key, ()))
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate.resolve())
        return ""

    def resolve(self) -> dict[str, str]:
        """Return configured paths and persist newly discovered installations."""
        with self._lock:
            changed = False
            for key in ("ldconsole", "mumu_manager", "endfield_game"):
                # An explicit value remains authoritative even when invalid so
                # preflight can identify the exact stale field to the user.
                if not self._paths.get(key):
                    discovered = self._discover_one(key)
                    if discovered:
                        self._paths[key] = discovered
                        changed = True
            if not self._paths.get("mumu_adb") and self._paths.get("mumu_manager"):
                sibling = Path(self._paths["mumu_manager"]).parent / "adb.exe"
                self._paths["mumu_adb"] = str(sibling)
                changed = True
            if changed:
                self._write()
            return dict(self._paths)

    def configure(self, *, ldplayer: str | None = None,
                  mumu: str | None = None,
                  endfield: str | None = None) -> dict[str, str]:
        """Validate and persist installation directories or executable paths."""
        inputs = {
            "ldconsole": (ldplayer, "ldconsole.exe"),
            "mumu_manager": (mumu, "MuMuManager.exe"),
            "endfield_game": (endfield, "Endfield.exe"),
        }
        with self._lock:
            for key, (raw, filename) in inputs.items():
                if raw is None:
                    continue
                candidate = self._from_installation(raw, filename)
                if not candidate.is_file():
                    raise HostSettingsError(
                        f"{HOST_TOOL_LABELS[key]}路径无效：{candidate}")
                self._paths[key] = str(candidate.resolve())
                if key == "mumu_manager":
                    adb = candidate.parent / "adb.exe"
                    if not adb.is_file():
                        raise HostSettingsError(f"MuMu 12 ADB 路径无效：{adb}")
                    self._paths["mumu_adb"] = str(adb.resolve())
            self._write()
            return dict(self._paths)

    def set_path(self, key: str, raw: str | None) -> dict[str, str]:
        """Validate and persist a single host tool path (used by the config GUI).

        ``key`` must be one of ``HOST_TOOL_LABELS``. ``raw`` may be a directory
        containing the tool's executable, or the executable path itself.
        """
        if key not in HOST_TOOL_LABELS:
            raise HostSettingsError(f"未知主机工具：{key}")
        if raw is None or not str(raw).strip():
            raise HostSettingsError(f"{HOST_TOOL_LABELS[key]}路径不能为空")
        filename = self._FILENAMES.get(key, "")
        candidate = self._from_installation(raw, filename)
        if not candidate.is_file():
            raise HostSettingsError(
                f"{HOST_TOOL_LABELS[key]}路径无效：{candidate}")
        with self._lock:
            self._paths[key] = str(candidate.resolve())
            if key == "mumu_manager":
                adb = candidate.parent / "adb.exe"
                if not adb.is_file():
                    raise HostSettingsError(f"MuMu 12 ADB 路径无效：{adb}")
                self._paths["mumu_adb"] = str(adb.resolve())
            self._write()
            return dict(self._paths)


def apply_host_settings(config: dict[str, Any], paths: Mapping[str, str]) -> None:
    """Inject machine-local tools into an in-memory config only."""
    tools = config.setdefault("tools", {})
    for key in HOST_TOOL_LABELS:
        tools[key] = str(paths.get(key, "")).strip()
    config["_host_tool_labels"] = dict(HOST_TOOL_LABELS)


def workflow_host_path_errors(config: dict[str, Any], workflow: str) -> list[str]:
    """Validate only the host programs required by the selected workflow."""
    definition = config.get("workflows", {}).get(workflow, {})
    tools = config.get("tools", {})
    errors: list[str] = []
    for key in definition.get("required_host_tools", []):
        value = str(tools.get(key, "")).strip()
        label = config.get("_host_tool_labels", HOST_TOOL_LABELS).get(key, key)
        if not value:
            errors.append(f"{label}（{key}）未配置")
        elif not Path(value).is_file():
            errors.append(f"{label}（{key}）路径无效：{value}")
    return errors


class PortError(ValueError):
    """Raised when the emulator ADB port is outside the supported range."""


def validate_port(value: Any) -> int:
    if isinstance(value, bool):
        raise PortError("模拟器端口必须是 1–65535 的整数")
    text = str(value).strip()
    if not text or not text.isascii() or not text.isdigit():
        raise PortError("模拟器端口必须是 1–65535 的整数")
    port = int(text, 10)
    if not 1 <= port <= 65535:
        raise PortError("模拟器端口必须在 1–65535 之间")
    return port


class RuntimeSettings:
    """Persistent per-workflow endpoints; legacy port remains readable."""

    def __init__(self, path: Path, default_port: int = DEFAULT_DEVICE_PORT):
        self.path = path
        self.default_port = validate_port(default_port)
        self._lock = threading.Lock()
        self._port, self._endpoints = self._load()

    def _load(self) -> tuple[int, dict[str, dict[str, Any]]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            port = validate_port(value.get("emulator_port", self.default_port))
            endpoints = value.get("workflow_endpoints", {})
            if not isinstance(endpoints, dict):
                endpoints = {}
            return port, {
                str(name): dict(item) for name, item in endpoints.items()
                if isinstance(item, dict) and str(item.get("endpoint", "")).strip()
            }
        except (FileNotFoundError, OSError, json.JSONDecodeError,
                AttributeError, PortError):
            return self.default_port, {}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps({
            "emulator_port": self._port,
            "workflow_endpoints": self._endpoints,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)

    @property
    def port(self) -> int:
        with self._lock:
            return self._port

    def save_port(self, value: Any) -> int:
        port = validate_port(value)
        with self._lock:
            self._port = port
            self._write()
        return port

    def endpoint(self, workflow: str) -> str:
        with self._lock:
            return str(self._endpoints.get(workflow, {}).get("endpoint", "")).strip()

    def save_endpoint(self, workflow: str, emulator_kind: str,
                      instance: int | str, endpoint: str) -> str:
        endpoint = str(endpoint).strip()
        if not endpoint:
            raise PortError("模拟器端点不能为空")
        with self._lock:
            self._endpoints[str(workflow)] = {
                "emulator_kind": str(emulator_kind).strip().casefold(),
                "instance": int(instance),
                "endpoint": endpoint,
            }
            self._write()
        return endpoint


def apply_device_port(config: dict[str, Any], port: Any) -> int:
    """Legacy helper retained for callers outside GameFlow.

    GameFlow itself no longer uses this global operation because workflows can
    target different emulator instances concurrently.
    """

    resolved = validate_port(port)
    endpoint = f"127.0.0.1:{resolved}"
    config.setdefault("device", {})["address"] = endpoint

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in list(value.items()):
                if key in {"device", "adb_device", "serial"} and isinstance(child, str):
                    value[key] = endpoint
                elif key == "ANDROID_SERIAL":
                    value[key] = endpoint
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(config.get("workflows", {}))
    for workflow in config.get("workflows", {}).values():
        for step in workflow.get("steps", []):
            step.setdefault("env", {})["ANDROID_SERIAL"] = endpoint
    config["_device_port"] = resolved
    return resolved


def apply_workflow_device_port(config: dict[str, Any], workflow: str,
                               port: Any) -> tuple[int, str]:
    """Apply a manual port override to one workflow only."""
    resolved = validate_port(port)
    definition = config.get("workflows", {}).get(workflow)
    if not isinstance(definition, dict):
        raise PortError(f"不存在工作流：{workflow}")
    kinds = [str(step.get("emulator_kind", "")).casefold()
             for step in definition.get("steps", [])]
    kind = next((value for value in kinds if value in {"ldplayer", "mumu"}), "")
    endpoint = (f"emulator-{resolved}" if kind == "ldplayer"
                else f"127.0.0.1:{resolved}")
    apply_workflow_device_endpoint(config, workflow, endpoint)
    return resolved, endpoint


def apply_workflow_device_endpoint(config: dict[str, Any], workflow: str,
                                   endpoint: str) -> str:
    """Apply a detected endpoint to one workflow without touching its peers."""
    definition = config.get("workflows", {}).get(workflow)
    if not isinstance(definition, dict):
        raise PortError(f"不存在工作流：{workflow}")
    endpoint = str(endpoint).strip()
    if not endpoint:
        raise PortError("模拟器端点不能为空")
    for step in definition.get("steps", []):
        if "device" in step:
            step["device"] = endpoint
        if "adb_device" in step:
            step["adb_device"] = endpoint
        if isinstance(step.get("env"), dict) and "ANDROID_SERIAL" in step["env"]:
            step["env"]["ANDROID_SERIAL"] = endpoint
    return endpoint


def _rewrite_json(path: Path, change) -> None:
    if not path.is_file():
        return
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        change(value)
        temporary = path.with_suffix(path.suffix + ".gameflow.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        os.replace(temporary, path)
    except (OSError, json.JSONDecodeError, TypeError, AttributeError) as exc:
        raise PortError(f"无法同步内置工具端口 {path}: {exc}") from exc


def sync_bundled_tool_ports(
    root: Path,
    port: Any,
    tool_endpoints: dict[str, str] | None = None,
    *,
    host_paths: Mapping[str, str] | None = None,
    emulator_specs: Mapping[str, tuple[str, int]] | None = None,
) -> int:
    """Write the selected endpoint into every bundled tool's active profile."""
    resolved = validate_port(port)
    endpoint = f"127.0.0.1:{resolved}"
    endpoints = {name: endpoint for name in
                 ("maa", "baas", "alas", "naruto", "gumballs", "qq_reader")}
    if tool_endpoints:
        endpoints.update({key: value for key, value in tool_endpoints.items()
                          if isinstance(value, str) and value.strip()})
    tools = root / "resources" / "tools"

    def replace_addresses(value: Any, target: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.casefold() in {"address", "adbserial", "serial"} and isinstance(child, str):
                    value[key] = target
                elif key.casefold() == "addresses" and isinstance(child, list):
                    value[key] = [target]
                else:
                    replace_addresses(child, target)
        elif isinstance(value, list):
            for child in value:
                replace_addresses(child, target)

    bundled_adb = str(root / "resources" / "runtime" / "adb.exe")

    def change_maa(value: dict[str, Any]) -> None:
        target = endpoints["maa"]
        replace_addresses(value.get("Configurations", {}), target)
        profiles = value.get("Configurations", {})
        if isinstance(profiles, dict):
            profile_values = profiles.values()
        elif isinstance(profiles, list):
            profile_values = profiles
        else:
            profile_values = ()
        for profile in profile_values:
            if not isinstance(profile, dict):
                continue
            gui = profile.setdefault("Gui", {})
            connection = gui.setdefault("ConnectSettings", {})
            connection["Address"] = target
            connection["AddressHistory"] = [target]
            connection["AdbPath"] = bundled_adb
            extras = connection.setdefault("Extras", {})
            for name in ("LDPlayer", "MuMuEmulator12"):
                emulator = extras.setdefault(name, {})
                emulator["EmulatorPath"] = ""
                emulator["IsEnabled"] = False
            startup = gui.setdefault("StartUpSettings", {})
            startup["RunDirectly"] = False
            startup["StartEmulator"] = False
            startup["EmulatorPath"] = ""
            runtime = gui.setdefault("RuntimeSettings", {})
            runtime["ReportToPenguin"] = False
            runtime["ReportToYituliu"] = False
        update = value.setdefault("Update", {})
        update["CheckOnStartup"] = False
        update["CheckOnSchedule"] = False
        update["AutoDownloadUpdatePackage"] = False
        update["AutoInstallUpdatePackage"] = False
        update["DoNotShowUpdate"] = True
        notice = value.setdefault("AnnouncementInfo", {})
        notice["DoNotShow"] = True
        notice["DoNotShowAgain"] = True

    _rewrite_json(tools / "maa" / "config" / "gui.new.json", change_maa)
    _rewrite_json(tools / "baas" / "_internal" / "web" / "static" / "baas.json",
                  lambda value: replace_addresses(value, endpoints["baas"]))

    host_paths = host_paths or {}
    emulator_specs = emulator_specs or {}

    def change_mfa(value: dict[str, Any], target: str,
                   tool_name: str) -> None:
        device = value.setdefault("AdbDevice", {})
        device["AdbSerial"] = target
        kind, instance = emulator_specs.get(tool_name, ("", 0))
        # MFA's automatic device finder can select an unrelated adb.exe from
        # PATH (for example G:/platform-tools) and classify a LDPlayer as
        # Androws.  Keep the profile pinned to the configured emulator's adb
        # and extras driver so the launcher does not lose its screencap/input
        # channels between runs.
        if kind == "ldplayer":
            ldconsole = str(host_paths.get("ldconsole", "")).strip()
            ld_root = Path(ldconsole).parent if ldconsole else Path()
            ld_adb = ld_root / "adb.exe" if ld_root else Path()
            if ld_adb.is_file():
                device["AdbPath"] = str(ld_adb)
                device["ScreencapMethods"] = 64
                device["InputMethods"] = 8
                device["Config"] = json.dumps(
                    {"extras": {"ld": {
                        "enable": True,
                        "index": int(instance),
                        "path": str(ld_root),
                    }}}, separators=(",", ":"))
                return
        device["AdbPath"] = bundled_adb
        device["ScreencapMethods"] = 1
        device["InputMethods"] = 1
        device["Config"] = json.dumps({"extras": {}}, separators=(",", ":"))

    _rewrite_json(tools / "naruto" / "config" / "instances" / "default.json",
                  lambda value: change_mfa(value, endpoints["naruto"],
                                            "naruto"))
    _rewrite_json(tools / "gumballs" / "config" / "instances" / "default.json",
                  lambda value: change_mfa(value, endpoints["gumballs"],
                                            "gumballs"))

    def change_alas(value: dict[str, Any]) -> None:
        emulator = value.setdefault("Alas", {}).setdefault("Emulator", {})
        emulator["Serial"] = endpoints["alas"]
        emulator["ScreenshotMethod"] = "ADB"
        emulator["ControlMethod"] = "ADB"
        info = value["Alas"].setdefault("EmulatorInfo", {})
        info["path"] = ""

    _rewrite_json(tools / "alas" / "config" / "alas.json", change_alas)

    def change_qq(value: dict[str, Any]) -> None:
        adb = value.setdefault("adb", {})
        adb["address"] = endpoints["qq_reader"]
        adb["adb_path"] = bundled_adb
        adb["name"] = endpoints["qq_reader"]
        extras = adb.setdefault("config", {}).setdefault("extras", {})
        extras["mumu"] = {"enable": False, "index": 0, "path": ""}

    _rewrite_json(tools / "qq_reader" / "dev" / "config" / "maa_pi_config.json",
                  change_qq)
    return resolved


def sync_bundled_workflow_endpoint(root: Path, workflow: str,
                                   endpoint: str) -> None:
    """Update only the bundled tool owned by the workflow that discovered it."""
    endpoint = str(endpoint).strip()
    if not endpoint:
        return
    tools = root / "resources" / "tools"
    bundled_adb = str(root / "resources" / "runtime" / "adb.exe")
    if workflow == "azur_lane_daily":
        def change_alas(value: dict[str, Any]) -> None:
            emulator = value.setdefault("Alas", {}).setdefault("Emulator", {})
            emulator["Serial"] = endpoint
        _rewrite_json(tools / "alas" / "config" / "alas.json", change_alas)
    elif workflow == "qq_reader_trial":
        def change_qq(value: dict[str, Any]) -> None:
            adb = value.setdefault("adb", {})
            adb["address"] = endpoint
            adb["name"] = endpoint
            adb["adb_path"] = bundled_adb
        _rewrite_json(tools / "qq_reader" / "dev" / "config" /
                      "maa_pi_config.json", change_qq)
    elif workflow in {"daily_game", "blue_archive_daily", "naruto_daily",
                      "gumballs_daily"}:
        # These tools are synchronized by profile at startup; retaining the
        # discovered endpoint here ensures subsequent retries use the same VM.
        return


def sync_bundled_tool_profiles(root: Path, config: dict[str, Any]) -> None:
    """Keep the restored multi-emulator workflow endpoints tool-specific."""

    def endpoint(workflow_id: str) -> str:
        workflow = config.get("workflows", {}).get(workflow_id, {})
        for step in workflow.get("steps", []):
            value = step.get("device") or step.get("adb_device")
            if isinstance(value, str) and value.strip():
                return value
        return str(config.get("device", {}).get("address", "127.0.0.1:5555"))

    default = str(config.get("device", {}).get("address", "127.0.0.1:5555"))
    try:
        fallback_port = validate_port(default.rsplit(":", 1)[-1])
    except PortError:
        fallback_port = DEFAULT_DEVICE_PORT
    emulator_specs: dict[str, tuple[str, int]] = {}
    for tool_name, workflow_id in (("naruto", "naruto_daily"),
                                   ("gumballs", "gumballs_daily")):
        workflow = config.get("workflows", {}).get(workflow_id, {})
        for step in workflow.get("steps", []):
            kind = str(step.get("emulator_kind", "")).strip().casefold()
            if kind not in {"ldplayer", "mumu"}:
                continue
            try:
                instance = int(step.get("emulator_instance",
                                        step.get("instance", 0)))
            except (TypeError, ValueError):
                instance = 0
            emulator_specs[tool_name] = (kind, instance)
            break
    sync_bundled_tool_ports(root, fallback_port, {
        "maa": endpoint("daily_game"),
        "baas": endpoint("blue_archive_daily"),
        "alas": endpoint("azur_lane_daily"),
        "naruto": endpoint("naruto_daily"),
        "gumballs": endpoint("gumballs_daily"),
        "qq_reader": endpoint("qq_reader_trial"),
    }, host_paths=config.get("tools", {}), emulator_specs=emulator_specs)
