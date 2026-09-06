from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .daily import operational_day
from .diagnostics import collect_failure_diagnostics
from .runners import (RUNNERS, Result, RunContext,
                      _restart_configured_emulator)
from .settings import workflow_host_path_errors
from .store import Store


class Engine:
    """Runs exactly one workflow. WorkflowManager owns one Engine per workflow."""

    def __init__(self, root: Path, config: dict[str, Any], store: Store):
        self.root, self.config, self.store = root, config, store
        self.state_root = Path(config.get("_state_root", root))
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
        host_errors = workflow_host_path_errors(self.config, workflow)
        if host_errors:
            return False, ("主机路径预检失败：" + "；".join(host_errors)
                           + "。请运行 configure-host 或编辑 data/host_settings.json")
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

    def _execute_step(self, run_id: int, workflow_id: str,
                      step: dict[str, Any], ctx: RunContext) -> Result:
        runner = RUNNERS.get(step["runner"])
        if not runner:
            return Result(False, f"未知执行器：{step['runner']}")
        retry_policy = self.config.get("failure_retry", {})
        retry_policy_enabled = bool(retry_policy.get("enabled", False))
        # With the global failure policy enabled, retries belong to the whole
        # workflow.  Retrying only this step would leave its script/emulator
        # alive and skip the run_always cleanup before the next attempt.
        retries_left = (0 if retry_policy_enabled else
                        max(0, int(step.get("retry", 0))))
        black_retries_left = (0 if retry_policy_enabled else
                              max(0, int(step.get("max_black_screen_restarts", 1))))
        last = Result(False, "未执行")
        attempt = 0
        while True:
            attempt += 1
            if self._stop.is_set() and not step.get("run_always"):
                return Result(False, "任务被用户停止")
            started = self.store.now()
            self.log(f"[{step['id']}] 第 {attempt} 次执行")
            try:
                last = runner(step, ctx)
            except Exception as exc:
                self.logger.exception("步骤发生异常")
                last = Result(False, f"异常：{exc}")
            diagnostics_enabled = bool(
                self.config.get("failure_diagnostics", {}).get("enabled", False))
            if not last.success and not self._stop.is_set() and diagnostics_enabled:
                try:
                    evidence = collect_failure_diagnostics(
                        self.state_root, self.config, workflow_id,
                        self.config["workflows"][workflow_id], step, attempt,
                        last.message, last.details,
                    )
                    last.details["failure_diagnostics"] = evidence
                    self.log(f"[{step['id']}] 错误现场已保存：{evidence['path']}")
                except Exception as exc:
                    self.logger.exception("保存错误现场失败")
                    last.details["failure_diagnostics_error"] = str(exc)
                    self.log(f"[{step['id']}] 保存错误现场失败：{exc}")
            emulator_restart = bool(last.details.get("black_screen_restart")
                                    or last.details.get("emulator_restart_requested"))
            if emulator_restart and not retry_policy_enabled:
                self.log(f"[{step['id']}] {last.message}")
                recovered = _restart_configured_emulator(step, ctx)
                last.details["emulator_restart_success"] = recovered.success
                last.details["emulator_restart_message"] = recovered.message
                if recovered.success:
                    last.message += f"；{recovered.message}，即将重新启动脚本"
                else:
                    last.message += f"；模拟器重启失败：{recovered.message}"
            step_status = last.status or ("success" if last.success else "failed")
            self.store.add_step(run_id, step, step_status,
                                attempt, started, last.message, last.details)
            if last.success:
                return last
            if (last.status in ("skipped", "needs_update", "cancelled")
                    or last.details.get("retryable") is False):
                return last
            should_retry = False
            if (emulator_restart and last.details.get("emulator_restart_success")
                    and black_retries_left > 0):
                black_retries_left -= 1
                should_retry = True
            elif retries_left > 0:
                retries_left -= 1
                should_retry = True
            if should_retry:
                default_retry_delay = retry_policy.get("retry_delay", 3)
                delay = float(last.details.get(
                    "retry_delay_override",
                    step.get("retry_delay", default_retry_delay)))
                self.log(f"[{step['id']}] 失败，{delay:g} 秒后重试：{last.message}")
                self._stop.wait(delay)
                continue
            return last

    def _execute_attempt(self, workflow: str, trigger: str) -> tuple[str, str, bool]:
        run_id = self.store.start_run(workflow, trigger)
        ctx = RunContext(self.root, self.config, self.log, self._stop, workflow)
        status, message, failed = "success", "正常结束", False
        retryable = True
        cleanup_errors: list[str] = []
        try:
            for configured_step in self.config["workflows"][workflow]["steps"]:
                step = dict(configured_step)
                if isinstance(configured_step.get("env"), dict):
                    step["env"] = dict(configured_step["env"])
                ctx.bind_device(step)
                if failed and not step.get("run_always"):
                    continue
                self._update(step=step["id"])
                # A user's stop request must not prevent run_always screenshots
                # and process cleanup from running.  Give those short, bounded
                # cleanup steps a fresh event while keeping the Engine stop flag
                # authoritative for the final workflow status.
                step_ctx = ctx
                if step.get("run_always") and self._stop.is_set():
                    step_ctx = RunContext(
                        self.root, self.config, self.log, threading.Event(), workflow)
                    step_ctx.runtime.update(ctx.runtime)
                result = self._execute_step(run_id, workflow, step, step_ctx)
                if not result.success:
                    special_status = result.status in {
                        "skipped", "needs_update", "cancelled",
                    }
                    if step.get("run_always"):
                        cleanup_errors.append(
                            f"{step['id']}：{result.message}")
                        if failed:
                            self.log(
                                f"清理步骤 {step['id']} 未成功，保留原始失败原因："
                                f"{result.message}")
                        else:
                            failed = True
                            retryable = False
                            status = "cleanup_failed"
                            message = (
                                f"任务完成但清理失败：{step['id']}："
                                f"{result.message}")
                            self.log(message)
                        continue
                    if step.get("continue_on_error") and not special_status:
                        self.log(f"步骤 {step['id']} 未成功但已按配置忽略：{result.message}")
                        continue
                    failed = True
                    if (special_status
                            or result.details.get("retryable") is False):
                        retryable = False
                    status = ("cancelled" if self._stop.is_set() else
                              (result.status or "failed"))
                    ending = {
                        "skipped": "已跳过",
                        "needs_update": "需要更新",
                    }.get(status, "异常结束")
                    message = f"步骤 {step['id']} {ending}：{result.message}"
                    self.log(message)
        finally:
            # A stop request is authoritative even when it arrives after the
            # final runner returned successfully.
            if self._stop.is_set():
                status = "cancelled"
                message = "任务已由用户取消"
            if cleanup_errors:
                message += "；清理警告：" + "；".join(cleanup_errors)
            self.store.finish_run(run_id, status, message)
        return status, message, retryable

    def _execute(self, workflow: str, trigger: str) -> None:
        retry_policy = self.config.get("failure_retry", {})
        retry_enabled = bool(retry_policy.get("enabled", False))
        # Daily batches already own their explicit second round so their first
        # and second attempt remain visible in the batch result. Standalone
        # runs get the same full-workflow retry behavior here.
        batch_attempt = trigger.startswith("daily_batch")
        max_attempts = (1 if batch_attempt or not retry_enabled else
                        max(1, int(retry_policy.get("max_attempts", 2))))
        status, message = "failed", "未执行"
        try:
            for attempt in range(1, max_attempts + 1):
                attempt_trigger = trigger if attempt == 1 else f"{trigger}_retry"
                if attempt > 1:
                    self.log(f"[{workflow}] 清理已完成，从流程开头开始第 {attempt} 次运行")
                status, message, retryable = self._execute_attempt(
                    workflow, attempt_trigger)
                if (status != "failed" or not retryable
                        or self._stop.is_set() or attempt >= max_attempts):
                    break
                delay = max(0.0, float(retry_policy.get("retry_delay", 3)))
                self.log(
                    f"[{workflow}] 第 {attempt} 次运行失败，脚本与模拟器清理已执行；"
                    f"{delay:g} 秒后从头重新启动流程")
                if self._stop.wait(delay):
                    status, message = "cancelled", "任务已由用户取消"
                    break
        finally:
            if self._stop.is_set():
                status, message = "cancelled", "任务已由用户取消"
            self._update(running=False, workflow=workflow, step=None, message=message,
                         last_status=status, last_finished=self.store.now())


class WorkflowManager:
    """Runs workflows independently and schedules an ordered daily batch."""

    def __init__(self, root: Path, config: dict[str, Any], store: Store):
        self.root, self.config, self.store = root, config, store
        self.state_root = Path(config.get("_state_root", root))
        interrupted = self.store.recover_interrupted_runs()
        if interrupted:
            logging.getLogger("gameflow").warning("已恢复 %d 条因旧进程退出而中断的运行记录", interrupted)
        self.engines = {name: Engine(root, config, store) for name in config["workflows"]}
        self._lock = threading.Lock()
        self._batch_thread: threading.Thread | None = None
        self._batch_stop = threading.Event()
        # A workflow can be cancelled without stopping the rest of a daily batch.
        # Access to this set is always protected by ``_lock``.
        self._batch_cancelled: set[str] = set()
        self._batch: dict[str, Any] = {
            "running": False, "queue": [], "active": [], "completed": [],
            "max_parallel": 1, "message": "就绪",
            "operational_day": operational_day(),
        }

    def workflow_states(self) -> dict[str, dict[str, Any]]:
        return {name: engine.state() for name, engine in self.engines.items()}

    def state(self) -> dict[str, Any]:
        states = self.workflow_states()
        daily_statuses = self.store.daily_run_statuses(list(states))
        with self._lock:
            # Finished batch results are status badges for one operational day.
            # Clear them on the first poll after 04:00. User participation
            # preferences live in web.UiPreferences and are deliberately not
            # touched here.
            current_day = operational_day()
            if (not self._batch["running"]
                    and self._batch.get("operational_day") != current_day):
                self._batch.update(queue=[], active=[], completed=[], message="就绪",
                                   operational_day=current_day)
            batch = dict(self._batch)
            batch["queue"] = list(self._batch["queue"])
            batch["active"] = list(self._batch["active"])
            batch["completed"] = list(self._batch["completed"])
        active = [name for name, state in states.items() if state["running"]]
        queued = set(batch["queue"])
        batch_results = {item["workflow"]: item for item in batch["completed"]}
        for name, workflow_state in states.items():
            daily = daily_statuses.get(name, {"status": "pending", "completed": False,
                                               "message": "今日尚未完成"})
            today_status = str(daily.get("status") or "pending")
            today_message = str(daily.get("message") or "")
            if workflow_state.get("running") or name in batch["active"]:
                today_status = "running"
                today_message = workflow_state.get("message") or "正在执行"
            elif name in queued:
                today_status = "queued"
                today_message = "已进入今日执行队列"
            elif (today_status == "pending" and name in batch_results):
                today_status = str(batch_results[name].get("status") or "pending")
                today_message = str(batch_results[name].get("message") or "")
            workflow_state.update(
                today_status=today_status,
                today_completed=bool(daily.get("completed")),
                today_message=today_message,
                today_started_at=daily.get("started_at"),
                today_finished_at=daily.get("finished_at"),
            )
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
            workflow_config = self.config["workflows"].get(name, {})
            if (name in self.engines and name not in selected and name != "self_test"
                    and not workflow_config.get("test_only", False)):
                selected.append(name)
        if not selected:
            return False, "请至少选择一个每日任务"
        max_parallel = max(1, min(int(max_parallel), len(selected), 2))
        with self._lock:
            if self._batch["running"] or any(e.state()["running"] for e in self.engines.values()):
                return False, "已有任务正在运行"
            self._batch_stop.clear()
            self._batch_cancelled.clear()
            self._batch = {"running": True, "queue": list(selected), "active": [],
                           "completed": [], "max_parallel": max_parallel,
                           "message": "准备每日流程",
                           "operational_day": operational_day()}
        self._batch_thread = threading.Thread(target=self._run_batch,
                                              args=(selected, max_parallel, force), daemon=True)
        self._batch_thread.start()
        return True, f"每日流程已开始，最多并行 {max_parallel} 个任务"

    def _run_batch(self, selected: list[str], max_parallel: int, force: bool) -> None:
        display_names = [self.config["workflows"][name].get("display_name", name)
                         for name in selected]
        omitted = [wf.get("display_name", name)
                   for name, wf in self.config["workflows"].items()
                   if (name not in selected and name != "self_test"
                       and not wf.get("test_only", False))]
        logging.getLogger("gameflow").info(
            "本轮每日流程已选择：%s；未选择：%s；最多并行：%d",
            "、".join(display_names), "、".join(omitted) or "无", max_parallel)
        retry_settings = self.config.get("daily_retry", {})
        retry_enabled = bool(retry_settings.get("enabled", True))
        retryable_statuses = {
            str(value) for value in retry_settings.get(
                "statuses", ["failed", "interrupted"])
        }
        all_round_results: list[dict[str, Any]] = []

        def run_round(names: list[str], round_number: int,
                      round_force: bool) -> list[dict[str, Any]]:
            pending = list(names)
            active: dict[str, Engine] = {}
            round_completed: list[dict[str, Any]] = []
            trigger = "daily_batch" if round_number == 1 else "daily_batch_retry"
            while pending or active:
                if self._batch_stop.is_set():
                    for engine in active.values():
                        engine.stop()
                    for engine in active.values():
                        engine.join()
                    for name, engine in list(active.items()):
                        state = engine.state()
                        round_completed.append({
                            "workflow": name,
                            "status": state.get("last_status", "cancelled"),
                            "message": state.get("message", "任务已停止"),
                            "round": round_number,
                        })
                    active.clear()
                    break

                # Apply individual cancellation requests before starting more work.
                with self._lock:
                    cancelled = set(self._batch_cancelled)
                for name in list(pending):
                    if name in cancelled:
                        pending.remove(name)
                        if not any(item["workflow"] == name for item in round_completed):
                            round_completed.append({
                                "workflow": name, "status": "cancelled",
                                "message": "任务已由用户取消", "round": round_number,
                            })
                for name, engine in list(active.items()):
                    if name in cancelled and engine.state()["running"]:
                        engine.stop()
                while pending and len(active) < max_parallel:
                    candidate_index = next((
                        index for index, candidate in enumerate(pending)
                        if not self._exclusive_conflict(candidate, list(active))
                    ), None)
                    if candidate_index is None:
                        break
                    name = pending.pop(candidate_index)
                    ok, start_message = self.engines[name].start(
                        name, trigger, round_force)
                    if ok:
                        active[name] = self.engines[name]
                        with self._lock:
                            cancel_after_start = name in self._batch_cancelled
                        if cancel_after_start:
                            self.engines[name].stop()
                    else:
                        round_completed.append({
                            "workflow": name, "status": "skipped",
                            "message": start_message, "round": round_number,
                        })
                finished = []
                for name, engine in active.items():
                    state = engine.state()
                    if not state["running"]:
                        round_completed.append({
                            "workflow": name,
                            "status": state.get("last_status", "failed"),
                            "message": state.get("message", ""),
                            "round": round_number,
                        })
                        finished.append(name)
                for name in finished:
                    active.pop(name)
                published = [*all_round_results, *round_completed]
                round_label = "第一轮" if round_number == 1 else "失败重试轮"
                with self._lock:
                    self._batch.update(
                        queue=list(pending), active=list(active),
                        completed=published, round=round_number,
                        message=(f"{round_label}：运行 {len(active)} 个，"
                                 f"等待 {len(pending)} 个"),
                    )
                time.sleep(0.1)
            return round_completed

        first_results = run_round(selected, 1, force)
        all_round_results.extend(first_results)
        retry_names = [
            item["workflow"] for item in first_results
            if item["status"] in retryable_statuses
            and item["workflow"] not in self._batch_cancelled
        ]
        retry_results: list[dict[str, Any]] = []
        if retry_enabled and retry_names and not self._batch_stop.is_set():
            retry_labels = [
                self.config["workflows"][name].get("display_name", name)
                for name in retry_names
            ]
            logging.getLogger("gameflow").info(
                "第一轮结束；以下失败任务进入第二轮自动重试：%s",
                "、".join(retry_labels))
            with self._lock:
                self._batch.update(
                    queue=list(retry_names), active=[], round=2,
                    message=f"第一轮结束，准备重试 {len(retry_names)} 个失败任务",
                    completed=list(first_results),
                )
            # The retry is deliberate and must not be blocked by today's prior run.
            retry_results = run_round(retry_names, 2, True)
            all_round_results.extend(retry_results)

        first_by_name = {item["workflow"]: item for item in first_results}
        retry_by_name = {item["workflow"]: item for item in retry_results}
        completed: list[dict[str, Any]] = []
        for name in selected:
            first = first_by_name.get(name)
            if first is None:
                continue
            retry = retry_by_name.get(name)
            if retry is None:
                completed.append(first)
                continue
            completed.append({
                **retry,
                "first_status": first["status"],
                "first_message": first.get("message", ""),
                "retried": True,
                "message": (f"第二轮：{retry.get('message', '')}；"
                            f"第一轮：{first.get('message', '')}"),
            })
        statuses = [item["status"] for item in completed]
        if self._batch_stop.is_set():
            message = "每日流程已停止"
        elif "cancelled" in statuses and all(
                s in ("success", "skipped", "cancelled") for s in statuses):
            message = "每日流程已完成，部分任务已取消"
        elif statuses and all(s in ("success", "skipped") for s in statuses):
            message = (f"每日流程全部完成；第二轮成功恢复 {len(retry_names)} 个失败任务"
                       if retry_names else "每日流程全部完成")
        else:
            still_failed = sum(status in retryable_statuses for status in statuses)
            message = f"每日流程完成；第二轮后仍有 {still_failed} 个失败任务"
        with self._lock:
            self._batch_cancelled.clear()
            self._batch.update(running=False, queue=[], active=[], completed=completed, message=message)

    def cancel(self, workflow: str) -> tuple[bool, str]:
        """Cancel one workflow without disturbing unrelated workflows.

        A queued daily workflow is removed and recorded as cancelled.  An active
        daily workflow, or an independently started workflow, receives only its
        own Engine stop event.
        """
        engine = self.engines.get(workflow)
        if engine is None:
            return False, f"不存在工作流：{workflow}"

        engine_to_stop: Engine | None = None
        with self._lock:
            if self._batch["running"]:
                queued = workflow in self._batch["queue"]
                active = workflow in self._batch["active"]
                engine_running = engine.state()["running"]
                if not queued and not active and not engine_running:
                    return False, f"任务未在本轮每日流程中等待或运行：{workflow}"
                if active and not engine_running and not queued:
                    return False, f"任务已经执行结束：{workflow}"

                self._batch_cancelled.add(workflow)
                if queued:
                    self._batch["queue"] = [name for name in self._batch["queue"]
                                            if name != workflow]
                    if not any(item["workflow"] == workflow
                               for item in self._batch["completed"]):
                        self._batch["completed"].append({
                            "workflow": workflow, "status": "cancelled",
                            "message": "任务已由用户取消",
                        })
                if engine_running:
                    engine_to_stop = engine
                self._batch["message"] = f"正在取消任务：{workflow}"
            else:
                if not engine.state()["running"]:
                    return False, f"任务当前未运行：{workflow}"
                engine_to_stop = engine

        if engine_to_stop is not None:
            engine_to_stop.stop()
            return True, f"已发送取消请求：{workflow}"
        return True, f"已取消等待中的任务：{workflow}"

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
            today = operational_day(now)
            for name, workflow in self.manager.config["workflows"].items():
                trigger = workflow.get("trigger", {})
                if not trigger.get("enabled") or trigger.get("type") != "daily":
                    continue
                key = (name, today)
                if now.strftime("%H:%M") >= trigger.get("time", "00:00") and key not in fired:
                    ok, _ = self.manager.start(name, "schedule")
                    if ok or self.manager.store.completed_today(name):
                        fired.add(key)
