from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, workflow TEXT NOT NULL,
                status TEXT NOT NULL, trigger_name TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT, message TEXT DEFAULT '')""")
            db.execute("""CREATE TABLE IF NOT EXISTS step_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
                step_id TEXT NOT NULL, runner TEXT NOT NULL, status TEXT NOT NULL,
                attempt INTEGER NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
                message TEXT DEFAULT '', details TEXT DEFAULT '{}')""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def start_run(self, workflow: str, trigger: str) -> int:
        with self._lock, self._connect() as db:
            cur = db.execute("INSERT INTO runs(workflow,status,trigger_name,started_at) VALUES(?,?,?,?)",
                             (workflow, "running", trigger, self.now()))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, message: str = "") -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE runs SET status=?,finished_at=?,message=? WHERE id=?",
                       (status, self.now(), message, run_id))

    def add_step(self, run_id: int, step: dict[str, Any], status: str, attempt: int,
                 started: str, message: str = "", details: dict[str, Any] | None = None) -> None:
        with self._lock, self._connect() as db:
            db.execute("""INSERT INTO step_runs(run_id,step_id,runner,status,attempt,started_at,
                       finished_at,message,details) VALUES(?,?,?,?,?,?,?,?,?)""",
                       (run_id, step["id"], step["runner"], status, attempt, started,
                        self.now(), message, json.dumps(details or {}, ensure_ascii=False)))

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def completed_today(self, workflow: str) -> bool:
        today = datetime.now().astimezone().date().isoformat()
        with self._connect() as db:
            row = db.execute("SELECT 1 FROM runs WHERE workflow=? AND status='success' AND substr(started_at,1,10)=? LIMIT 1",
                             (workflow, today)).fetchone()
        return row is not None

    def recover_interrupted_runs(self) -> int:
        """Mark runs abandoned by a previous GameFlow process as interrupted."""
        with self._lock, self._connect() as db:
            cur = db.execute(
                "UPDATE runs SET status='interrupted',finished_at=?,message=? WHERE status='running'",
                (self.now(), "GameFlow 进程在任务完成前退出，运行状态已失去监管"))
            return cur.rowcount
