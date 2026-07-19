from __future__ import annotations

import argparse
import logging
import sys
import time
import webbrowser
from pathlib import Path

from gameflow.config import DEFAULT_CONFIG, ConfigError, load_config
from gameflow.engine import Scheduler, WorkflowManager
from gameflow.store import Store
from gameflow.web import serve


ROOT = Path(__file__).resolve().parent


def build_engine(config_path: str) -> WorkflowManager:
    config = load_config(config_path)
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(log_dir / "gameflow.log", encoding="utf-8"),
                                  logging.StreamHandler()])
    return WorkflowManager(ROOT, config, Store(ROOT / "data" / "gameflow.db"))


def main() -> int:
    parser = argparse.ArgumentParser(description="GameFlow 游戏每日自动化编排器")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="action")
    web = sub.add_parser("web", help="启动本地控制面板")
    web.add_argument("--no-browser", action="store_true")
    run = sub.add_parser("run", help="运行一个工作流")
    run.add_argument("workflow")
    run.add_argument("--force", action="store_true")
    sub.add_parser("status", help="显示最近运行记录")
    args = parser.parse_args()
    try:
        manager = build_engine(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    if args.action == "run":
        ok, message = manager.start(args.workflow, "command", args.force)
        if not ok:
            print(message)
            return 1
        while manager.state()["running"]:
            manager.join(0.5)
        state = manager.engines[args.workflow].state()
        print(state["message"])
        return 0 if state.get("last_status") == "success" else 1
    if args.action == "status":
        for row in manager.store.recent():
            print(row)
        return 0
    host = manager.config.get("server", {}).get("host", "127.0.0.1")
    port = int(manager.config.get("server", {}).get("port", 8765))
    Scheduler(manager).start()
    url = f"http://{host}:{port}/"
    print(f"GameFlow 已启动：{url}  按 Ctrl+C 停止")
    if not getattr(args, "no_browser", False):
        webbrowser.open(url)
    try:
        serve(manager, host, port)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
