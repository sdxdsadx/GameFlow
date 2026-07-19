from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .runners import RUNNERS, Result, RunContext
from .mailer import send_daily_screenshots
from .store import Store


class Engine:
    """Runs exactly one workflow. WorkflowManager owns one Engine per workflow."""

    def __init__(self, root: Path, config: dict[str, Any], store: Store):
        self.root, self.config, self.store = root, config, store
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state: dict[str, Any] = {
            "running": False, "workflow": None, "step": None, "message": "就绪"
        }
        self.logger = logging.getLogger("gameflow")

    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _update(self, **values: Any) -> None:
        with self._lock:
            self._state.update(values)

    def log(self, message: str) -> None:
        self.logger.info(message)
        self._update(message=message[-500:])

    def start(self, workflow: str, trigger: str = "manual", force: bool = False) -> tuple[bool, str]:
        if workflow not in self.config["workflows"]:
            return False, f"不存在工作流：{workflow}"
        if self.state()["running"]:
            return False, f"工作流正在运行：{workflow}"
        wf = self.config["workflows"][workflow]
        if wf.get("once_per_day") and not force and self.store.completed_today(workflow):
            return False, "今日已经成功执行；如需重跑请使用强制执行"
        self._stop.clear()
        self._update(running=True, workflow=workflow, step=None, message="准备运行")
        self._thread = threading.Thread(target=self._execute, args=(workflow, trigger), daemon=True)
        self._thread.start()
        return True, "已开始"

    def stop(self) -> tuple[bool, str]:
        if not self.state()["running"]:
            return False, "当前没有运行中的工作流"
        self._stop.set()
        self._update(message="正在停止……")
        return True, "已发送停止请求"

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _execute_step(self, run_id: int, step: dict[str, Any], ctx: RunContext) -> Result:
        runner = RUNNERS.get(step["runner"])
        if not runner:
            return Result(False, f"未知执行器：{step['runner']}")
        retries = max(0, int(step.get("retry", 0)))
        last = Result(False, "未执行")
        for attempt in range(1, retries + 2):
            if self._stop.is_set() and not step.get("run_always"):
                return Result(False, "任务被用户停止")
            started = self.store.now()
            self.log(f"[{step['id']}] 第 {attempt} 次执行")
            try:
                last = runner(step, ctx)
            except Exception as exc:
                self.logger.exception("步骤发生异常")
                last = Result(False, f"异常：{exc}")
            step_status = last.status or ("success" if last.success else "failed")
            self.store.add_step(run_id, step, step_status,
                                attempt, started, last.message, last.details)
            if last.success or last.status == "needs_update":
                return last
            if attempt <= retries:
                delay = float(step.get("retry_delay", 3))
                self.log(f"[{step['id']}] 失败，{delay:g} 秒后重试：{last.message}")
                self._stop.wait(delay)
        return last

    def _execute(self, workflow: str, trigger: str) -> None:
        run_id = self.store.start_run(workflow, trigger)
        ctx = RunContext(self.root, self.config, self.log, self._stop)
        status, message, failed = "success", "正常结束", False
        try:
            for step in self.config["workflows"][workflow]["steps"]:
                if failed and not step.get("run_always"):
                    continue
                self._update(step=step["id"])
                result = self._execute_step(run_id, step, ctx)
                if result.status == "needs_update":
                    failed = True
                    status = "needs_update"
                    message = f"步骤 {step['id']} 需要更新：{result.message}"
                    self.log(message)
                elif not result.success:
                    if step.get("continue_on_error"):
                        self.log(f"步骤 {step['id']} 未成功但已按配置忽略：{result.message}")
                        continue
                    failed = True
                    if status == "needs_update":
                        self.log(f"更新状态后的清理步骤 {step['id']} 异常：{result.message}")
                    else:
                        status = "cancelled" if self._stop.is_set() else "failed"
                        message = f"步骤 {step['id']} 异常结束：{result.message}"
                        self.log(message)
        finally:
            self.store.finish_run(run_id, status, message)
            self._update(running=False, workflow=workflow, step=None, message=message,
                         last_status=status, last_finished=self.store.now())


class WorkflowManager:
    """Runs workflows independently and schedules an ordered daily batch."""

    def __init__(self, root: Path, config: dict[str, Any], store: Store):
        self.root, self.config, self.store = root, config, store
        interrupted = self.store.recover_interrupted_runs()
        if interrupted:
            logging.getLogger("gameflow").warning("已恢复 %d 条因旧进程退出而中断的运行记录", interrupted)
        self.engines = {name: Engine(root, config, store) for name in config["workflows"]}
        self._lock = threading.Lock()
        self._batch_thread: threading.Thread | None = None
        self._batch_stop = threading.Event()
        self._batch: dict[str, Any] = {
            "running": False, "queue": [], "active": [], "completed": [],
            "max_parallel": 1, "message": "就绪"
        }

    def workflow_states(self) -> dict[str, dict[str, Any]]:
        return {name: engine.state() for name, engine in self.engines.items()}

    def state(self) -> dict[str, Any]:
        states = self.workflow_states()
        with self._lock:
            batch = dict(self._batch)
            batch["queue"] = list(self._batch["queue"])
            batch["active"] = list(self._batch["active"])
            batch["completed"] = list(self._batch["completed"])
        active = [name for name, state in states.items() if state["running"]]
        return {
            "running": bool(active) or batch["running"], "workflow": ", ".join(active) or None,
            "step": None, "message": batch["message"] if batch["running"] else ("运行中" if active else "就绪"),
            "active": active, "workflows": states, "batch": batch
        }

    def _exclusive_group(self, workflow: str) -> str:
        return str(self.config["workflows"].get(workflow, {}).get("exclusive_group", "")).strip()

    def _exclusive_with(self, workflow: str) -> set[str]:
        values = self.config["workflows"].get(workflow, {}).get("exclusive_with", [])
        return {str(value) for value in values}

    def _exclusive_conflict(self, workflow: str,
                            active_names: list[str] | None = None) -> str | None:
        group = self._exclusive_group(workflow)
        names = (active_names if active_names is not None else
                 [name for name, engine in self.engines.items() if engine.state()["running"]])
        for name in names:
            if name == workflow:
                continue
            same_group = bool(group and self._exclusive_group(name) == group)
            explicitly_blocked = (name in self._exclusive_with(workflow)
                                  or workflow in self._exclusive_with(name))
            if same_group or explicitly_blocked:
                return name
        return None

    def start(self, workflow: str, trigger: str = "manual", force: bool = False) -> tuple[bool, str]:
        engine = self.engines.get(workflow)
        if not engine:
            return False, f"不存在工作流：{workflow}"
        if self._batch["running"]:
            return False, "每日流程正在调度中，请先停止或等待完成"
        conflict = self._exclusive_conflict(workflow)
        if conflict:
            return False, f"{workflow} 与正在运行的 {conflict} 不能同时执行"
        return engine.start(workflow, trigger, force)

    def start_daily(self, workflows: list[str], max_parallel: int = 1,
                    force: bool = False) -> tuple[bool, str]:
        selected = []
        for name in workflows:
            if name in self.engines and name not in selected and name != "self_test":
                selected.append(name)
        if not selected:
            return False, "请至少选择一个每日任务"
        max_parallel = max(1, min(int(max_parallel), len(selected), 2))
        with self._lock:
            if self._batch["running"] or any(e.state()["running"] for e in self.engines.values()):
                return False, "已有任务正在运行"
            self._batch_stop.clear()
            self._batch = {"running": True, "queue": list(selected), "active": [],
                           "completed": [], "max_parallel": max_parallel, "message": "准备每日流程"}
        self._batch_thread = threading.Thread(target=self._run_batch,
                                              args=(selected, max_parallel, force), daemon=True)
        self._batch_thread.start()
        return True, f"每日流程已开始，最多并行 {max_parallel} 个任务"

    def _run_batch(self, selected: list[str], max_parallel: int, force: bool) -> None:
        batch_started_epoch = time.time()
        display_names = [self.config["workflows"][name].get("display_name", name)
                         for name in selected]
        omitted = [wf.get("display_name", name)
                   for name, wf in self.config["workflows"].items()
                   if name not in selected and name != "self_test"]
        logging.getLogger("gameflow").info(
            "本轮每日流程已选择：%s；未选择：%s；最多并行：%d",
            "、".join(display_names), "、".join(omitted) or "无", max_parallel)
        pending = list(selected)
        active: dict[str, Engine] = {}
        completed: list[dict[str, str]] = []
        while pending or active:
            if self._batch_stop.is_set():
                for engine in active.values():
                    engine.stop()
                for engine in active.values():
                    engine.join()
                break
            while pending and len(active) < max_parallel:
                candidate_index = next((index for index, candidate in enumerate(pending)
                                        if not self._exclusive_conflict(candidate, list(active))), None)
                if candidate_index is None:
                    break
                name = pending.pop(candidate_index)
                ok, message = self.engines[name].start(name, "daily_batch", force)
                if ok:
                    active[name] = self.engines[name]
                else:
                    completed.append({"workflow": name, "status": "skipped", "message": message})
            finished = []
            for name, engine in active.items():
                state = engine.state()
                if not state["running"]:
                    completed.append({"workflow": name, "status": state.get("last_status", "failed"),
                                      "message": state.get("message", "")})
                    finished.append(name)
            for name in finished:
                active.pop(name)
            with self._lock:
                self._batch.update(queue=list(pending), active=list(active), completed=list(completed),
                                   message=f"运行 {len(active)} 个，等待 {len(pending)} 个")
            time.sleep(0.1)
        statuses = [item["status"] for item in completed]
        if self._batch_stop.is_set():
            message = "每日流程已停止"
        elif "needs_update" in statuses:
            message = "每日流程结束，但存在需要更新的任务"
        elif statuses and all(s in ("success", "skipped") for s in statuses):
            message = "每日流程全部完成"
        else:
            message = "每日流程完成，但存在失败任务"
        if not self._batch_stop.is_set():
            mail_ok, mail_message = send_daily_screenshots(
                self.root, self.config, selected, completed, batch_started_epoch)
            logging.getLogger("gameflow").info(mail_message)
            message += "；" + mail_message
        with self._lock:
            self._batch.update(running=False, queue=[], active=[], completed=completed, message=message)

    def stop(self) -> tuple[bool, str]:
        running = [engine for engine in self.engines.values() if engine.state()["running"]]
        if not running and not self._batch["running"]:
            return False, "当前没有运行中的任务"
        self._batch_stop.set()
        for engine in running:
            engine.stop()
        return True, "已发送停止请求"

    def join(self, timeout: float | None = None) -> None:
        if self._batch_thread and self._batch_thread.is_alive():
            self._batch_thread.join(timeout)
        else:
            for engine in self.engines.values():
                if engine.state()["running"]:
                    engine.join(timeout)


class Scheduler:
    def __init__(self, manager: WorkflowManager):
        self.manager = manager
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        fired: set[tuple[str, str]] = set()
        while not self._stop.wait(20):
            now = datetime.now().astimezone()
            today = now.date().isoformat()
            for name, workflow in self.manager.config["workflows"].items():
                trigger = workflow.get("trigger", {})
                if not trigger.get("enabled") or trigger.get("type") != "daily":
                    continue
                key = (name, today)
                if now.strftime("%H:%M") >= trigger.get("time", "00:00") and key not in fired:
                    ok, _ = self.manager.start(name, "schedule")
                    if ok or self.manager.store.completed_today(name):
                        fired.add(key)
