"""Run one isolated MaaFramework template-recognition click for Naruto."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
from pathlib import Path


STATUS_SUCCEEDED = 3000


def _bytes(value: str | Path) -> bytes:
    return os.fsencode(os.fspath(value))


class MaaNarutoClicker:
    def __init__(self, runtime: Path, resource: Path, adb: Path,
                 device: str, debug_image: Path | None = None) -> None:
        self.runtime = runtime
        self.resource_path = resource
        self.adb = adb
        self.device = device
        self.debug_image = debug_image
        self._dll_dir = os.add_dll_directory(str(runtime))
        self.lib = ctypes.CDLL(str(runtime / "MaaFramework.dll"))
        self.resource = ctypes.c_void_p()
        self.controller = ctypes.c_void_p()
        self.tasker = ctypes.c_void_p()
        self._configure_api()

    def _configure_api(self) -> None:
        lib = self.lib
        lib.MaaVersion.restype = ctypes.c_char_p
        lib.MaaGlobalSetOption.argtypes = [ctypes.c_int32, ctypes.c_void_p,
                                           ctypes.c_uint64]
        lib.MaaGlobalSetOption.restype = ctypes.c_uint8
        lib.MaaResourceCreate.restype = ctypes.c_void_p
        lib.MaaResourcePostBundle.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.MaaResourcePostBundle.restype = ctypes.c_int64
        lib.MaaResourceWait.argtypes = [ctypes.c_void_p, ctypes.c_int64]
        lib.MaaResourceWait.restype = ctypes.c_int32
        lib.MaaResourceDestroy.argtypes = [ctypes.c_void_p]
        lib.MaaDbgControllerCreate.argtypes = [ctypes.c_char_p]
        lib.MaaDbgControllerCreate.restype = ctypes.c_void_p
        lib.MaaAdbControllerCreate.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint64, ctypes.c_uint64,
            ctypes.c_char_p, ctypes.c_char_p]
        lib.MaaAdbControllerCreate.restype = ctypes.c_void_p
        lib.MaaControllerPostConnection.argtypes = [ctypes.c_void_p]
        lib.MaaControllerPostConnection.restype = ctypes.c_int64
        lib.MaaControllerWait.argtypes = [ctypes.c_void_p, ctypes.c_int64]
        lib.MaaControllerWait.restype = ctypes.c_int32
        lib.MaaControllerSetOption.argtypes = [ctypes.c_void_p, ctypes.c_int32,
                                               ctypes.c_void_p, ctypes.c_uint64]
        lib.MaaControllerSetOption.restype = ctypes.c_uint8
        lib.MaaControllerDestroy.argtypes = [ctypes.c_void_p]
        lib.MaaTaskerCreate.restype = ctypes.c_void_p
        lib.MaaTaskerBindResource.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.MaaTaskerBindResource.restype = ctypes.c_uint8
        lib.MaaTaskerBindController.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.MaaTaskerBindController.restype = ctypes.c_uint8
        lib.MaaTaskerPostTask.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                         ctypes.c_char_p]
        lib.MaaTaskerPostTask.restype = ctypes.c_int64
        lib.MaaTaskerWait.argtypes = [ctypes.c_void_p, ctypes.c_int64]
        lib.MaaTaskerWait.restype = ctypes.c_int32
        lib.MaaTaskerDestroy.argtypes = [ctypes.c_void_p]

    def run(self, entry: str, log_dir: Path) -> dict[str, object]:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_value = _bytes(log_dir)
        log_buffer = ctypes.create_string_buffer(log_value)
        self.lib.MaaGlobalSetOption(
            1, ctypes.cast(log_buffer, ctypes.c_void_p), len(log_value))

        self.resource = ctypes.c_void_p(self.lib.MaaResourceCreate())
        if not self.resource.value:
            raise RuntimeError("MaaResourceCreate failed")
        resource_id = self.lib.MaaResourcePostBundle(
            self.resource, _bytes(self.resource_path))
        resource_status = self.lib.MaaResourceWait(self.resource, resource_id)
        if resource_status != STATUS_SUCCEEDED:
            raise RuntimeError(f"MAA resource load failed: {resource_status}")

        if self.debug_image:
            controller_value = self.lib.MaaDbgControllerCreate(
                _bytes(self.debug_image))
        else:
            controller_value = self.lib.MaaAdbControllerCreate(
                _bytes(self.adb), self.device.encode("utf-8"),
                1 << 1, 1, b"{}", _bytes(self.runtime / "MaaAgentBinary"))
        self.controller = ctypes.c_void_p(controller_value)
        if not self.controller.value:
            raise RuntimeError("Maa controller create failed")

        # Preserve the 720p coordinate system used by the captured templates.
        short_side = ctypes.c_int32(720)
        self.lib.MaaControllerSetOption(
            self.controller, 2, ctypes.byref(short_side),
            ctypes.sizeof(short_side))
        connection_id = self.lib.MaaControllerPostConnection(self.controller)
        connection_status = self.lib.MaaControllerWait(
            self.controller, connection_id)
        if connection_status != STATUS_SUCCEEDED:
            raise RuntimeError(f"MAA controller connection failed: {connection_status}")

        self.tasker = ctypes.c_void_p(self.lib.MaaTaskerCreate())
        if not self.tasker.value:
            raise RuntimeError("MaaTaskerCreate failed")
        if not self.lib.MaaTaskerBindResource(self.tasker, self.resource):
            raise RuntimeError("MaaTaskerBindResource failed")
        if not self.lib.MaaTaskerBindController(self.tasker, self.controller):
            raise RuntimeError("MaaTaskerBindController failed")
        task_id = self.lib.MaaTaskerPostTask(
            self.tasker, entry.encode("utf-8"), b"{}")
        task_status = self.lib.MaaTaskerWait(self.tasker, task_id)
        return {
            "success": task_status == STATUS_SUCCEEDED,
            "status": task_status,
            "entry": entry,
            "framework_version": self.lib.MaaVersion().decode("utf-8", "replace"),
            "controller": "debug" if self.debug_image else self.device,
        }

    def close(self) -> None:
        if self.tasker.value:
            self.lib.MaaTaskerDestroy(self.tasker)
            self.tasker = ctypes.c_void_p()
        if self.controller.value:
            self.lib.MaaControllerDestroy(self.controller)
            self.controller = ctypes.c_void_p()
        if self.resource.value:
            self.lib.MaaResourceDestroy(self.resource)
            self.resource = ctypes.c_void_p()
        self._dll_dir.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("entry")
    parser.add_argument("--runtime", type=Path,
                        default=Path(r"G:\project_X\dev"))
    parser.add_argument("--resource", type=Path,
                        default=Path(__file__).parent / "resource")
    parser.add_argument("--adb", type=Path,
                        default=Path(r"E:\leidian\LDPlayer9\adb.exe"))
    parser.add_argument("--device", default="emulator-5554")
    parser.add_argument("--image", type=Path)
    parser.add_argument("--log-dir", type=Path,
                        default=Path(__file__).parent / "logs")
    args = parser.parse_args()
    clicker = MaaNarutoClicker(
        args.runtime, args.resource, args.adb, args.device, args.image)
    try:
        result = clicker.run(args.entry, args.log_dir)
    except Exception as exc:
        result = {"success": False, "entry": args.entry,
                  "error": str(exc)}
    finally:
        clicker.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") else 2


if __name__ == "__main__":
    raise SystemExit(main())
