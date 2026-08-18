from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from orchestrator import ShadowOrchestrator
from trader.models import CodexRunError, CodexRunResult, StateCorruptionError
from trader.reporting import preflight_report_artifact, write_json_companion
from trader.state import StateStore, initial_state


class Clock:
    def __init__(self, value): self.value = value
    def now(self): return self.value


def staged_payload(name):
    account = {"agentic_allowed": True, "brokerage_account_type": "individual", "management_type": "self_directed", "state": "active", "deactivated": False, "permanently_deactivated": False}
    common = {"passed": True, "account_classifications": [account], "errors": []}
    if name == "preflight.md": return common
    if name == "preflight-portfolio.md": return {**common, "account_reconciled": True, "account_equity": 100.0, "buying_power": 100.0, "portfolio_status": "active"}
    if name == "preflight-positions.md": return {**common, "account_reconciled": True, "baseline_position_count": 0, "baseline_positions_present": False, "baseline_positions": []}
    return {**common, "account_reconciled": True, "relevant_order_count": 0, "open_pending_count": 0, "baseline_external_order_count": 0, "baseline_external_orders_present": False, "baseline_external_orders": []}


class Runner:
    def __init__(self): self.calls = 0
    def run(self, **kwargs):
        self.calls += 1
        tools = kwargs["required_robinhood_tools"]
        return CodexRunResult(data=deepcopy(staged_payload(kwargs["prompt_path"].name)), tool_calls={tool: 1 for tool in tools})
    def safe_diagnostics(self): return {"mcp_teardown_warning": False, "diagnostic_codes": []}


class Boundary:
    policy_version = "test"


class FailedRunner(Runner):
    def run(self, **kwargs):
        self.calls += 1
        raise CodexRunError("Codex emitted terminal failure event: error")
    def safe_diagnostics(self):
        return {"mcp_teardown_warning": False, "diagnostic_codes": [], "codex_failure_diagnostics": {"process_return_code": 9, "structured_error_count": 1}}


class PreflightReportTests(unittest.TestCase):
    def core(self, directory):
        core = ShadowOrchestrator.__new__(ShadowOrchestrator)
        core.root = Path(directory); core.store = StateStore(core.root / "state")
        core.runner = Runner(); core.boundary = Boundary()
        core.config = {"models": {"luna": "test"}}
        core.clock = Clock(datetime.fromisoformat("2026-08-14T09:35:01-04:00"))
        return core

    def test_first_and_second_independent_preflights_have_distinct_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            core = self.core(directory); state = initial_state("2026-08-14"); core.store.save(state)
            core.preflight(state, datetime.fromisoformat("2026-08-14T09:35:00-04:00"))
            first = state["preflight_operations"][0]["report_artifact"]
            core.preflight(state, datetime.fromisoformat("2026-08-14T09:35:00-04:00"))
            second = state["preflight_operations"][1]["report_artifact"]
            self.assertNotEqual(first, second); self.assertTrue((core.root / first).exists()); self.assertTrue((core.root / second).exists())

    def test_retry_same_operation_resolves_same_artifact_without_child_run(self):
        with tempfile.TemporaryDirectory() as directory:
            core = self.core(directory); state = initial_state("2026-08-14"); core.store.save(state)
            first = core.preflight(state, datetime.fromisoformat("2026-08-14T09:35:00-04:00")); operation = state["preflight_operations"][0]
            recovered = core.preflight(state, datetime.fromisoformat("2026-08-14T09:36:00-04:00"), operation_id=operation["operation_id"])
            self.assertEqual(recovered, first); self.assertEqual(core.runner.calls, 4); self.assertEqual(len(state["preflight_operations"]), 1)

    def test_identical_evidence_is_idempotent_and_conflicting_evidence_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            core = self.core(directory); state = initial_state("2026-08-14"); core.store.save(state)
            core.preflight(state, datetime.fromisoformat("2026-08-14T09:35:00-04:00")); operation = state["preflight_operations"][0]
            path = core.root / operation["report_artifact"]
            original = path.read_bytes(); write_json_companion(path, json.loads(original))
            core.preflight(state, datetime.fromisoformat("2026-08-14T09:36:00-04:00"), operation_id=operation["operation_id"])
            changed = json.loads(original); changed["passed"] = False; path.write_text(json.dumps(changed), encoding="utf-8")
            before = deepcopy(state)
            with self.assertRaises(StateCorruptionError): core.preflight(state, datetime.fromisoformat("2026-08-14T09:36:00-04:00"), operation_id=operation["operation_id"])
            self.assertEqual(state, before)

    def test_unrelated_same_day_artifact_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            core = self.core(directory); legacy = core.root / "reports/2026-08-14-preflight.json"
            write_json_companion(legacy, {"historical": True}); before = legacy.read_bytes()
            state = initial_state("2026-08-14"); core.store.save(state)
            core.preflight(state, datetime.fromisoformat("2026-08-14T09:35:00-04:00"))
            self.assertEqual(legacy.read_bytes(), before)

    def test_failed_preflight_persists_sanitized_codex_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            core = self.core(directory); core.runner = FailedRunner()
            state = initial_state("2026-08-14"); core.store.save(state)
            with self.assertRaises(CodexRunError):
                core.preflight(state, datetime.fromisoformat("2026-08-14T09:35:00-04:00"))
            report_path = core.root / state["preflight_operations"][0]["report_artifact"]
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["codex_failure_diagnostics"]["process_return_code"], 9)
            self.assertEqual(report["codex_failure_diagnostics"]["structured_error_count"], 1)

    def test_success_resolves_prior_preflight_failure_without_false_security_event(self):
        with tempfile.TemporaryDirectory() as directory:
            core = self.core(directory); core.runner = FailedRunner()
            state = initial_state("2026-08-14"); core.store.save(state)
            with self.assertRaises(CodexRunError):
                core.preflight(state, datetime.fromisoformat("2026-08-14T09:35:00-04:00"))
            self.assertFalse(state["errors"][0]["resolved"])
            self.assertEqual(state["security_events"], [])
            core.runner = Runner()
            core.preflight(state, datetime.fromisoformat("2026-08-14T09:36:00-04:00"))
            self.assertTrue(state["errors"][0]["resolved"])
            self.assertIn("resolved_at", state["errors"][0])

    def test_unique_operations_do_not_overwrite_and_historical_names_remain_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            core = self.core(directory); state = initial_state("2026-08-14"); core.store.save(state)
            for _ in range(4): core.preflight(state, datetime.fromisoformat("2026-08-14T09:35:00-04:00"))
            paths = [item["report_artifact"] for item in state["preflight_operations"]]
            self.assertEqual(len(paths), len(set(paths)))
            senior = core.root / "reports/2026-08-14-senior-1.json"; eod = core.root / "reports/2026-08-14-eod.json"
            for path in (senior, eod):
                write_json_companion(path, {"version": 1})
                with self.assertRaises(StateCorruptionError): write_json_companion(path, {"version": 2})

    def test_concurrent_unique_operations_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); started = datetime.fromisoformat("2026-08-14T09:35:00-04:00")
            operation_ids = [f"preflight:{started.isoformat()}:{index:032x}" for index in range(8)]
            paths = [root / preflight_report_artifact("2026-08-14", started, value) for value in operation_ids]
            with ThreadPoolExecutor(max_workers=8) as pool:
                written = list(pool.map(lambda pair: write_json_companion(pair[0], {"operation_id": pair[1]}), zip(paths, operation_ids)))
            self.assertEqual(set(written), set(paths))
            self.assertEqual({json.loads(path.read_text())["operation_id"] for path in paths}, set(operation_ids))


if __name__ == "__main__": unittest.main()
