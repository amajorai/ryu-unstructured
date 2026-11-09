"""Production job-table regressions for cancellation, retry, and bounded state."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ryu_unstructured.jobs import JobStore


def await_status(store: JobStore, job_id: str, expected: str) -> dict[str, object]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        job = store.get(job_id)
        if job is not None:
            snapshot = job.snapshot()
            if snapshot["status"] == expected:
                return snapshot
        time.sleep(0.005)
    raise AssertionError(f"job {job_id} did not reach {expected}")


class JobStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = JobStore()
        # Deterministic queueing without changing the production global limit.
        self.store._slots = threading.Semaphore(1)
        self.tempdir = tempfile.TemporaryDirectory()
        self.document = Path(self.tempdir.name) / "document.txt"
        self.document.write_text("hello", encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_running_cancel_wins_completion_race_and_retry_succeeds(self) -> None:
        parser_started = threading.Event()
        release_parser = threading.Event()

        def blocked_parse(*_args: object) -> dict[str, str]:
            parser_started.set()
            self.assertTrue(release_parser.wait(2))
            return {"markdown": "parsed"}

        with patch("ryu_unstructured.jobs.parse_file", side_effect=blocked_parse):
            first = self.store.submit(self.document, {}, "document.txt")
            self.assertTrue(parser_started.wait(2))
            cancelled = self.store.cancel(first.id)
            self.assertIsNotNone(cancelled)
            release_parser.set()
            snapshot = await_status(self.store, first.id, "cancelled")
            time.sleep(0.03)
            self.assertEqual(first.snapshot()["status"], "cancelled")
            self.assertIsNone(snapshot["result"])

        with patch(
            "ryu_unstructured.jobs.parse_file", return_value={"markdown": "retry result"}
        ):
            retry = self.store.submit(self.document, {}, "document.txt")
            succeeded = await_status(self.store, retry.id, "succeeded")
            self.assertEqual(succeeded["result"], {"markdown": "retry result"})

    def test_queued_cancel_never_calls_parser_and_cleans_inline_file(self) -> None:
        parser_started = threading.Event()
        release_parser = threading.Event()
        parsed_names: list[str] = []

        def blocked_then_record(path: Path, *_args: object) -> dict[str, str]:
            parsed_names.append(path.name)
            parser_started.set()
            self.assertTrue(release_parser.wait(2))
            return {"markdown": path.name}

        queued_upload = Path(self.tempdir.name) / "queued-upload.txt"
        queued_upload.write_text("temporary", encoding="utf-8")
        with patch("ryu_unstructured.jobs.parse_file", side_effect=blocked_then_record):
            running = self.store.submit(self.document, {})
            self.assertTrue(parser_started.wait(2))
            queued = self.store.submit(
                queued_upload, {}, cleanup_path=queued_upload
            )
            self.assertEqual(queued.snapshot()["status"], "queued")
            self.store.cancel(queued.id)
            release_parser.set()
            await_status(self.store, running.id, "succeeded")
            await_status(self.store, queued.id, "cancelled")
            deadline = time.monotonic() + 2
            while queued_upload.exists() and time.monotonic() < deadline:
                time.sleep(0.005)

        self.assertEqual(parsed_names, ["document.txt"])
        self.assertFalse(queued_upload.exists())


if __name__ == "__main__":
    unittest.main()
