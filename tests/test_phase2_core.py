from __future__ import annotations

import json
import multiprocessing
import os
import platform
import socket
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from trader.instance_lock import SingleInstanceLock
from trader.models import PreflightError, SchemaValidationError, StateCorruptionError
from trader.reporting import write_non_destructive_text
from trader.scheduler import SessionScheduler
from trader.shadow_monitor import ShadowPlanMonitor
from trader.state import StateStore, initial_state


ROOT = Path(__file__).resolve().parents[1]


def _lock_contender(path: str, queue) -> None:
    try:
        with SingleInstanceLock(Path(path), "2026-08-14", datetime.fromisoformat("2026-08-14T09:00:00-04:00")):
            queue.put("entered")
    except PreflightError:
        queue.put("blocked")


def _report_writer(path: str, content: str, queue) -> None:
    try:
        write_non_destructive_text(Path(path), content)
        queue.put("written")
    except StateCorruptionError:
        queue.put("collision")


def bar(timestamp: str, open_price=10.0, high=10.2, low=9.8, close=10.1, complete=True):
    return {"timestamp": timestamp, "open": open_price, "high": high, "low": low, "close": close, "volume": 1000, "complete": complete}


class ProcessLockTests(unittest.TestCase):
    def test_second_process_cannot_enter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orchestrator.lock"
            context = multiprocessing.get_context("fork")
            queue = context.Queue()
            with SingleInstanceLock(path, "2026-08-14", datetime.fromisoformat("2026-08-14T09:00:00-04:00")):
                process = context.Process(target=_lock_contender, args=(str(path), queue))
                process.start(); process.join(5)
                self.assertEqual(queue.get(timeout=1), "blocked")
                self.assertEqual(process.exitcode, 0)

    def test_dead_stale_owner_is_replaced_conservatively(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orchestrator.lock"
            path.write_text(json.dumps({"pid": 99999999, "hostname": socket.gethostname(), "process_start_timestamp": "2026-08-13T09:00:00-04:00", "session_date": "2026-08-13", "platform": platform.system()}), encoding="utf-8")
            with SingleInstanceLock(path, "2026-08-14", datetime.fromisoformat("2026-08-14T09:00:00-04:00")):
                owner = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(owner["pid"], os.getpid())

    def test_ambiguous_stale_owner_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orchestrator.lock"
            path.write_text('{"pid":"?"}', encoding="utf-8")
            with self.assertRaises(PreflightError):
                SingleInstanceLock(path, "2026-08-14", datetime.fromisoformat("2026-08-14T09:00:00-04:00")).acquire()


class StateRevisionTests(unittest.TestCase):
    def test_lost_update_revision_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            state = initial_state("2026-08-14")
            store.save(state)
            stale = store.load("2026-08-14")
            current = store.load("2026-08-14")
            current["operation_ids"].append("one"); store.save(current)
            stale["operation_ids"].append("two")
            with self.assertRaises(StateCorruptionError): store.save(stale)

    def test_unsupported_future_state_version_fails(self):
        state = initial_state("2026-08-14"); state["version"] = 999
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "2026-08-14.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaises(StateCorruptionError): StateStore(Path(directory)).load("2026-08-14")

    def test_nan_and_infinity_fail(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                state = initial_state("2026-08-14"); state["usage_counts"]["bad"] = value
                with self.assertRaises(StateCorruptionError): StateStore(Path(directory)).save(state)

    def test_impossible_outcome_transition_fails(self):
        plan = json.loads((ROOT / "tests/fixtures/senior_plan.json").read_text(encoding="utf-8"))
        record = {"plan_id": "p1", "frozen_at": plan["decision_timestamp"], "original_plan": plan, "outcome": ShadowPlanMonitor.initial_outcome()}
        record["outcome"].update({"status": "OPEN", "entry_triggered": True, "entry_timestamp": "2026-08-14T10:05:00-04:00", "entry_bar_timestamp": "2026-08-14T10:05:00-04:00", "entry_price": 10.0})
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory)); state = initial_state("2026-08-14")
            state["shadow_plans"].append(record); store.save(state)
            state["shadow_plans"][0]["outcome"]["status"] = "EXPIRED"
            with self.assertRaises(StateCorruptionError): store.save(state)


class TemporalBarTests(unittest.TestCase):
    def setUp(self):
        plan = json.loads((ROOT / "tests/fixtures/senior_plan.json").read_text(encoding="utf-8"))
        self.record = {"plan_id": "p1", "frozen_at": plan["decision_timestamp"], "original_plan": plan, "outcome": ShadowPlanMonitor.initial_outcome()}
        self.monitor = ShadowPlanMonitor(); self.as_of = datetime.fromisoformat("2026-08-14T10:15:00-04:00")

    def rejected(self, bars):
        with self.assertRaises(SchemaValidationError): self.monitor.evaluate(self.record, bars, self.as_of)

    def test_future_bar_rejected(self): self.rejected([bar("2026-08-14T10:15:00-04:00")])
    def test_prior_session_bar_rejected(self): self.rejected([bar("2026-08-13T10:00:00-04:00")])
    def test_after_hours_bar_rejected(self): self.rejected([bar("2026-08-14T16:00:00-04:00")])
    def test_malformed_alignment_rejected(self): self.rejected([bar("2026-08-14T10:01:00-04:00")])
    def test_duplicate_bar_rejected(self): self.rejected([bar("2026-08-14T10:00:00-04:00"), bar("2026-08-14T10:00:00-04:00")])
    def test_out_of_order_bar_rejected(self): self.rejected([bar("2026-08-14T10:05:00-04:00"), bar("2026-08-14T10:00:00-04:00")])

    def test_gap_through_stop_uses_worse_open(self):
        opened = deepcopy(self.record)
        opened["outcome"].update({"status": "OPEN", "entry_triggered": True, "entry_timestamp": "2026-08-14T10:05:00-04:00", "entry_bar_timestamp": "2026-08-14T10:05:00-04:00", "entry_price": 10.0, "entry_via_open": True, "entry_at_close": False, "entry_before_cutoff": True, "mfe": 0.1, "mae": -0.1})
        result = self.monitor.evaluate(opened, [bar("2026-08-14T10:10:00-04:00", 9.0, 9.2, 8.8, 9.1)], self.as_of)
        self.assertEqual(result["outcome"]["exit_price"], 9.0)
        self.assertAlmostEqual(result["outcome"]["pnl"], -2.0)

    def test_crossing_entry_has_unknown_mfe_and_mae(self):
        result = self.monitor.evaluate(self.record, [bar("2026-08-14T10:05:00-04:00", 9.8, 10.2, 9.7, 10.1)], datetime.fromisoformat("2026-08-14T10:10:00-04:00"))
        self.assertIsNone(result["outcome"]["mfe"]); self.assertIsNone(result["outcome"]["mae"])
        self.assertTrue(result["outcome"]["excursions_unknown"])


class ReportRaceTests(unittest.TestCase):
    def test_concurrent_report_writer_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.md"; context = multiprocessing.get_context("fork"); queue = context.Queue()
            processes = [context.Process(target=_report_writer, args=(str(path), value, queue)) for value in ("alpha", "beta")]
            for process in processes: process.start()
            for process in processes: process.join(5)
            outcomes = sorted(queue.get(timeout=1) for _ in processes)
            self.assertEqual(outcomes, ["collision", "written"])
            self.assertIn(path.read_text(encoding="utf-8"), {"alpha\n", "beta\n"})


class SchedulerPolicyTests(unittest.TestCase):
    def test_stale_slot_grace(self):
        scheduler = SessionScheduler({"stale_slot_grace_seconds": 30})
        slot = datetime.fromisoformat("2026-08-14T10:00:00-04:00")
        self.assertFalse(scheduler.is_stale(slot, datetime.fromisoformat("2026-08-14T10:00:30-04:00")))
        self.assertTrue(scheduler.is_stale(slot, datetime.fromisoformat("2026-08-14T10:00:31-04:00")))

    def test_scheduler_wake_is_dst_independent_and_nonoverlapping(self):
        text = (ROOT / "scripts/install-scheduler.ps1").read_text(encoding="utf-8")
        self.assertNotIn("ConvertTime", text)
        self.assertIn("MultipleInstances IgnoreNew", text)
        self.assertIn("AddHours(5)", text)


if __name__ == "__main__": unittest.main()
