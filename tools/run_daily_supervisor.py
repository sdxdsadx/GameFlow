"""Run the daily batch in a separate supervised process using the latest config.

This is used when the long-running GameFlow web server cannot be restarted (it
holds an older in-memory config).  It starts the same daily batch from the
current config/ui_preferences and prints periodic status until the batch ends.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(r"G:\project_I")
sys.path.insert(0, str(ROOT))

from gameflow.config import load_config  # noqa: E402
from gameflow.engine import WorkflowManager  # noqa: E402
from gameflow.store import Store  # noqa: E402


def main() -> int:
    config = load_config(str(ROOT / "config" / "workflow.json"))
    prefs = json.loads((ROOT / "data" / "ui_preferences.json").read_text(encoding="utf-8"))
    exclude = set(sys.argv[1:]) if len(sys.argv) > 1 else set()
    selected = [name for name in prefs.get("order", [])
                if prefs.get("workflows", {}).get(name, {}).get("enabled")
                and name not in exclude]
    max_parallel = 1  # 用户要求一个个顺序运行，不要一次跑一大堆
    if not selected:
        print("no workflows selected")
        return 1
    # Use a separate DB to avoid lock conflicts with the long-running web server.
    manager = WorkflowManager(ROOT, config, Store(ROOT / "data" / "gameflow_supervised_seq.db"))
    ok, message = manager.start_daily(selected, max_parallel, force=False)
    print(f"start_daily -> {ok}: {message}", flush=True)
    if not ok:
        return 1
    deadline = time.monotonic() + 6 * 60 * 60
    last_print = 0.0
    while True:
        try:
            state = manager.state()
            batch = state.get("batch", {})
            running = state.get("running") or batch.get("running")
        except Exception as exc:
            print(f"[{time.strftime('%H:%M:%S')}] state error (will retry): {exc}", flush=True)
            time.sleep(5)
            continue
        if not running:
            break
        if time.monotonic() > deadline:
            print("TIMEOUT waiting daily batch", flush=True)
            manager.stop_all()
            return 3
        if time.monotonic() - last_print >= 30:
            last_print = time.monotonic()
            active = batch.get("active", [])
            queue = batch.get("queue", [])
            completed = batch.get("completed", [])
            print(f"[{time.strftime('%H:%M:%S')}] active={active} queue={queue} "
                  f"completed={[(x.get('workflow'), x.get('status')) for x in completed]}",
                  flush=True)
        manager.join(1.0)
    state = manager.state()
    batch = state.get("batch", {})
    print("BATCH DONE", json.dumps({
        "message": batch.get("message"),
        "completed": batch.get("completed"),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
