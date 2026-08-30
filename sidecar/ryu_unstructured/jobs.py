"""In-process parse-job table.

Jobs live only for the life of the process — Core owns the durable record. The
table is bounded (`MAX_JOBS`) because a finished job holds a whole document's
markdown and this sidecar is meant to idle-stop, not to grow.

Timeout honesty: CPython cannot kill a running thread, so the watchdog marks the
job `failed` at the deadline and stops waiting on it. The worker thread may run
to completion in the background; its result is discarded. That is real
enforcement from the caller's side (the job never hangs) and the ceiling on
wasted work is `MAX_WORKERS` stuck parses, after which submissions queue.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from .limits import MAX_JOBS, MAX_WORKERS, TIMEOUT_SECS
from .parser import ParseError, parse_file

JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
TERMINAL: set[str] = {"succeeded", "failed", "cancelled"}


@dataclass
class Job:
    id: str
    filename: str
    status: JobStatus = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    missing_dependencies: list[str] = field(default_factory=list)
    result: Optional[dict[str, Any]] = None
    _cancel: threading.Event = field(default_factory=threading.Event)

    def snapshot(self, *, include_result: bool = True) -> dict[str, Any]:
        """The poll payload. `result` is null until the job succeeds."""
        return {
            "job_id": self.id,
            "status": self.status,
            "filename": self.filename,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "error_code": self.error_code,
            "missing_dependencies": self.missing_dependencies,
            "result": self.result if include_result else None,
        }


class JobStore:
    """Thread-safe job registry with a bounded worker pool."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._slots = threading.Semaphore(MAX_WORKERS)

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return [self._jobs[job_id] for job_id in self._order if job_id in self._jobs]

    def cancel(self, job_id: str) -> Optional[Job]:
        """Cooperative cancel: a queued job never starts, a running one is dropped."""
        job = self.get(job_id)
        if job is None or job.status in TERMINAL:
            return job
        job._cancel.set()
        job.status = "cancelled"
        job.finished_at = time.time()
        return job

    def submit(
        self, path: Path, options: dict[str, Any], display_name: str | None = None
    ) -> Job:
        # `filename` is the *document's* name, not the file we happen to read: the
        # primary submit form points at an extensionless content-addressed blob,
        # and a poll that answered `9f2c…` would render as 64 hex characters on
        # the attachment chip.
        job = Job(
            id=f"parse_{uuid.uuid4().hex[:16]}", filename=display_name or path.name
        )
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._evict_locked()
        threading.Thread(
            target=self._run, args=(job, path, options), name=f"parse-{job.id}", daemon=True
        ).start()
        return job

    def _evict_locked(self) -> None:
        """Drop the oldest terminal jobs once the table is over budget."""
        while len(self._order) > MAX_JOBS:
            for index, job_id in enumerate(self._order):
                if self._jobs.get(job_id) and self._jobs[job_id].status in TERMINAL:
                    self._order.pop(index)
                    self._jobs.pop(job_id, None)
                    break
            else:
                # Nothing terminal to reclaim — every tracked job is still live.
                return

    def _run(self, job: Job, path: Path, options: dict[str, Any]) -> None:
        # Bound concurrency here rather than at submit so the HTTP handler always
        # answers 202 immediately; queued work waits on this semaphore, not the
        # caller's socket.
        self._slots.acquire()
        try:
            if job._cancel.is_set():
                return
            job.status = "running"
            job.started_at = time.time()
            deadline = job.started_at + TIMEOUT_SECS
            done = threading.Event()
            outcome: dict[str, Any] = {}

            def work() -> None:
                try:
                    outcome["result"] = parse_file(path, options, job.filename)
                except ParseError as exc:
                    outcome["error"] = exc
                except Exception as exc:  # noqa: BLE001 — a crash must not lose the job
                    outcome["error"] = ParseError(
                        "parse_failed", f"{type(exc).__name__}: {exc}"
                    )
                finally:
                    done.set()

            worker = threading.Thread(target=work, name=f"partition-{job.id}", daemon=True)
            worker.start()
            while not done.wait(timeout=0.25):
                if job._cancel.is_set():
                    return
                if time.time() >= deadline:
                    self._fail(
                        job,
                        "timeout",
                        f"parse exceeded the {TIMEOUT_SECS}s limit "
                        "(raise RYU_UNSTRUCTURED_TIMEOUT_SECS for very large scans)",
                    )
                    return

            if job._cancel.is_set():
                return
            error = outcome.get("error")
            if isinstance(error, ParseError):
                self._fail(job, error.code, str(error), missing=error.missing)
                return
            job.result = outcome.get("result")
            job.status = "succeeded"
            job.finished_at = time.time()
        finally:
            self._slots.release()

    @staticmethod
    def _fail(job: Job, code: str, message: str, *, missing: list[str] | None = None) -> None:
        job.status = "failed"
        job.error_code = code
        job.error = message
        job.missing_dependencies = missing or []
        job.finished_at = time.time()


STORE = JobStore()
