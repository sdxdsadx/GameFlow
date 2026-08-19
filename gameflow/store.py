from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .daily import operational_day, parse_timestamp


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
            self._ensure_column(db, "runs", "owner_pid", "INTEGER")
            db.execute("""CREATE TABLE IF NOT EXISTS step_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
                step_id TEXT NOT NULL, runner TEXT NOT NULL, status TEXT NOT NULL,
                attempt INTEGER NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
                message TEXT DEFAULT '', details TEXT DEFAULT '{}')""")
            db.execute("""CREATE TABLE IF NOT EXISTS workflow_flags (
                workflow TEXT NOT NULL, flag_key TEXT NOT NULL,
                flag_value TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (workflow, flag_key))""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _ensure_column(db: sqlite3.Connection, table: str,
                       column: str, definition: str) -> None:
        columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def start_run(self, workflow: str, trigger: str) -> int:
        with self._lock, self._connect() as db:
            cur = db.execute(
                """INSERT INTO runs(workflow,status,trigger_name,started_at,owner_pid)
                   VALUES(?,?,?,?,?)""",
                (workflow, "running", trigger, self.now(), os.getpid()))
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

    def set_workflow_flag(self, workflow: str, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        with self._lock, self._connect() as db:
            db.execute("""INSERT INTO workflow_flags(workflow,flag_key,flag_value,updated_at)
                       VALUES(?,?,?,?) ON CONFLICT(workflow,flag_key) DO UPDATE SET
                       flag_value=excluded.flag_value,updated_at=excluded.updated_at""",
                       (workflow, key, payload, self.now()))

    def get_workflow_flag(self, workflow: str, key: str, default: Any = None) -> Any:
        with self._connect() as db:
            row = db.execute(
                "SELECT flag_value FROM workflow_flags WHERE workflow=? AND flag_key=?",
                (workflow, key)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["flag_value"])
        except (TypeError, json.JSONDecodeError):
            return default

    def delete_workflow_flag(self, workflow: str, key: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM workflow_flags WHERE workflow=? AND flag_key=?",
                       (workflow, key))

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def completed_today(self, workflow: str, now: datetime | None = None) -> bool:
        """Whether the workflow succeeded in the current 04:00-based game day."""
        today = operational_day(now)
        with self._connect() as db:
            rows = db.execute(
                "SELECT started_at FROM runs WHERE workflow=? AND status='success' ORDER BY id DESC",
                (workflow,)).fetchall()
        for row in rows:
            started = parse_timestamp(row["started_at"])
            if started is not None and operational_day(started) == today:
                return True
        return False

    def daily_run_statuses(self, workflows: list[str] | set[str] | tuple[str, ...],
                           now: datetime | None = None) -> dict[str, dict[str, Any]]:
        """Return each workflow's status for the current 04:00-based game day."""
        names = list(dict.fromkeys(str(name) for name in workflows))
        result = {
            name: {"status": "pending", "completed": False, "message": "今日尚未完成",
                   "started_at": None, "finished_at": None}
            for name in names
        }
        if not names:
            return result
        placeholders = ",".join("?" for _ in names)
        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM runs WHERE workflow IN ({placeholders}) ORDER BY id DESC",
                names).fetchall()
        today = operational_day(now)
        latest: dict[str, dict[str, Any]] = {}
        successful: dict[str, dict[str, Any]] = {}
        for raw in rows:
            row = dict(raw)
            name = str(row["workflow"])
            started = parse_timestamp(row.get("started_at"))
            if started is None or operational_day(started) != today:
                continue
            latest.setdefault(name, row)
            if row.get("status") == "success":
                successful.setdefault(name, row)
        for name in names:
            row = successful.get(name) or latest.get(name)
            if row is None:
                continue
            status = str(row.get("status") or "pending")
            result[name] = {
                "status": status,
                "completed": bool(successful.get(name)),
                "message": str(row.get("message") or ""),
                "started_at": row.get("started_at"),
                "finished_at": row.get("finished_at"),
            }
        return result

    def recover_interrupted_runs(self) -> int:
        """Mark runs abandoned by a previous GameFlow process as interrupted."""
        now = datetime.now().astimezone()
        stale_ownerless_hours = 6.0
        recovered = 0
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT id,started_at,owner_pid FROM runs WHERE status='running'"
            ).fetchall()
            for row in rows:
                owner_pid = row["owner_pid"]
                should_recover = False
                if owner_pid is not None:
                    should_recover = not self._process_alive(int(owner_pid))
                else:
                    started = parse_timestamp(row["started_at"])
                    if started is not None:
                        age_hours = (now - started).total_seconds() / 3600
                        should_recover = age_hours >= stale_ownerless_hours
                if not should_recover:
                    continue
                db.execute(
                    "UPDATE runs SET status='interrupted',finished_at=?,message=? WHERE id=?",
                    (self.now(), "GameFlow 进程在任务完成前退出，运行状态已失去监管",
                     row["id"]))
                recovered += 1
        return recovered

    @staticmethod
    def _process_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name != "nt":
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False
