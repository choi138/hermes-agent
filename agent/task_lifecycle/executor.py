"""Synchronous Mac adapter for the existing, caller-approved Codex runner.

No worker or scheduler is created. Reconnecting callers inspect the ledger
before submitting; only the reservation's creator can invoke the runner.
"""

from collections.abc import Mapping
import os
import signal
import socket
import subprocess
import time

import psutil

from agent import codex_task_runner
from agent.codex_task_runner import TaskRequest

from .contract import TaskContract
from .directory_handoff import open_directory, spawn_pinned
from .types import LifecycleError, Phase, TERMINAL_PHASES, UNRESOLVED_PHASES


def _process_lookup(pid):
    try:
        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        return {"pid": pid, "started_at": process.create_time()}
    except psutil.NoSuchProcess:
        return None
    except psutil.Error as exc:
        raise LifecycleError("Cannot inspect process identity") from exc


def _terminate(pid, started_at):
    try:
        process = psutil.Process(pid)
        if process.create_time() != started_at or not process.is_running():
            raise LifecycleError("Process identity changed; refusing cancellation")
        # The runner/bootstrap owns a new session with PGID == workload PID.
        # Signal its descendants too: otherwise their inherited output pipes
        # can keep the runner waiting until timeout after the leader exits.
        if os.getpgid(pid) != pid or pid == os.getpgrp():
            raise LifecycleError("Process does not own its cancellation group")
        os.killpg(pid, signal.SIGTERM)
    except (psutil.Error, OSError) as exc:
        raise LifecycleError("Cannot terminate the recorded process") from exc


def _matches(current, saved):
    return isinstance(current, Mapping) and all(
        current.get(key) == saved[key] for key in ("pid", "started_at")
    )


class MacExecutor:
    def __init__(self, registry, *, authority=None, process_lookup=None, terminate=None, popen=None,
                 prompt_bytes=None, cancel=None, on_result=None):
        self.registry = registry
        self.authority = authority
        self._process_lookup = process_lookup or _process_lookup
        self._terminate = terminate or _terminate
        self._popen = popen or subprocess.Popen
        self.prompt_bytes = prompt_bytes
        self.cancel_signal = cancel
        self.on_result = on_result

    def submit(self, contract: TaskContract, request: TaskRequest) -> str:
        if not isinstance(contract, TaskContract) or not isinstance(request, TaskRequest):
            raise LifecycleError("Validated contract and request are required")
        contract.bind(self.authority, request)
        with open_directory(contract.workdir, self.authority._directory_identity) as (directory_fd, _):
            return self._submit_pinned(contract, request, directory_fd)

    def _submit_pinned(self, contract, request, directory_fd):
        submission = self.registry.submit(contract)
        run_id = submission.run_id
        if submission.outcome == "conflict":
            raise LifecycleError("Idempotency key payload conflict")
        if submission.outcome != "created":
            return run_id
        return self._execute_pinned(run_id, contract, request, directory_fd)

    def _execute_pinned(self, run_id, contract, request, directory_fd):
        # The detached workflow calls this only after its durable CAS claim.
        deadline = time.monotonic() + request.timeout

        def before_spawn(checked_request, run_dir):
            contract.bind(self.authority, checked_request)
            self.registry.mark_ready(run_id)

        def observed_popen(*args, **kwargs):
            contract.bind(self.authority, request)
            process = spawn_pinned(self._popen, *args, directory_fd=directory_fd,
                                   request=request, deadline=deadline, **kwargs)
            try:
                current = self._process_lookup(process.pid)
                if not isinstance(current, Mapping) or current.get("pid") != process.pid:
                    raise LifecycleError("Spawned process identity could not be observed")
                self.registry.mark_running(run_id, start_evidence={
                    "pid": process.pid, "started_at": current.get("started_at"), "executor": "mac",
                    "host": socket.gethostname(), "boot": psutil.boot_time(), "uid": os.getuid(),
                })
            except Exception:
                # The runner has not received the handle yet. Reuse its cleanup
                # here so an evidence failure cannot leak the spawned child.
                try:
                    codex_task_runner._stop_group(process)
                finally:
                    for name in ("stdin", "stdout", "stderr"):
                        stream = getattr(process, name, None)
                        if stream is not None:
                            stream.close()
                    self.registry.record_phase(run_id, Phase.UNKNOWN)
                raise
            return process

        try:
            options = {}
            if self.prompt_bytes is not None:
                options["prompt_bytes"] = self.prompt_bytes
            if self.cancel_signal is not None:
                options["cancel"] = self.cancel_signal
            result = codex_task_runner.run_task(request, before_spawn=before_spawn, popen=observed_popen,
                                                **options)
            if self.on_result is not None:
                self.on_result(result)
        except Exception:
            phase = self._run(run_id).phase
            if phase not in TERMINAL_PHASES:
                self.registry.record_phase(
                    run_id, Phase.BLOCKED if phase in UNRESOLVED_PHASES else Phase.UNKNOWN,
                )
            raise
        run = self._run(run_id)
        if result.get("status") == "cancelled" and run.phase not in TERMINAL_PHASES:
            self.registry.record_phase(run_id, Phase.CANCELLED)
            return run_id
        if run.phase not in TERMINAL_PHASES:
            if run.start_evidence is not None and type(result.get("process_returncode")) is int:
                self.registry.mark_execution_finished(run_id, exit_code=result["process_returncode"])
            else:
                self.registry.record_phase(
                    run_id, Phase.BLOCKED if run.phase in UNRESOLVED_PHASES else Phase.UNKNOWN,
                )
        return run_id

    def _run(self, run_id):
        run = self.registry.lookup(run_id)
        if run is None:
            raise LifecycleError(f"Unknown run: {run_id}")
        return run

    def status(self, run_id):
        run = self._run(run_id)
        if run.phase is Phase.RUNNING:
            current = self._process_lookup(run.start_evidence["pid"])
            if not _matches(current, run.start_evidence):
                return self.registry.record_phase(run_id, Phase.UNKNOWN)
        return run.phase

    def cancel(self, run_id):
        run = self._run(run_id)
        if run.start_evidence is None or run.phase is not Phase.RUNNING:
            raise LifecycleError("No active recorded process to cancel")
        saved = run.start_evidence
        if not _matches(self._process_lookup(saved["pid"]), saved):
            raise LifecycleError("Process identity mismatch; refusing cancellation")
        self._terminate(saved["pid"], saved["started_at"])
        return self.registry.record_phase(run_id, Phase.CANCELLED)
