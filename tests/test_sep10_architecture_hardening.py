from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator import ShadowOrchestrator
from trader.codex_runner import CodexRunner
from trader.models import CodexRunError, SchemaValidationError


ROOT = Path(__file__).resolve().parents[1]


class Sep10ArchitectureHardeningTests(unittest.TestCase):
    def setUp(self):
        self.core = object.__new__(ShadowOrchestrator)

    @staticmethod
    def candidate(symbol="TEST"):
        return {
            "symbol": symbol, "classification": "NEW", "scanner_percent_change": 5.0,
            "rvol": 2.0, "gap_return": 0.01, "intraday_return": 0.04,
            "distance_from_high": 0.001, "distance_from_vwap": 0.02,
            "vwap": 10.0, "rsi14": 60.0, "ema20": 9.9,
            "volume_persistence": 2.0, "technical_reason": "fixture",
            "material_requalification": None, "completed_5m_bars": [],
        }

    def test_exact_duplicate_finalist_collapses(self):
        item = self.candidate()
        cycle = {"symbols_processed": ["TEST"], "finalists": [item, deepcopy(item)]}
        self.core._normalize_luna_symbols(cycle)
        self.assertEqual(cycle["finalists"], [item])

    def test_differently_cased_duplicate_finalist_collapses(self):
        first = self.candidate("test")
        second = deepcopy(first); second["symbol"] = "TEST"
        cycle = {"symbols_processed": ["test", "TEST"], "finalists": [first, second]}
        self.core._normalize_luna_symbols(cycle)
        self.assertEqual(cycle["symbols_processed"], ["TEST"])
        self.assertEqual([item["symbol"] for item in cycle["finalists"]], ["TEST"])

    def test_conflicting_duplicate_finalist_fails_closed(self):
        first = self.candidate(); second = deepcopy(first); second["rvol"] = 3.0
        cycle = {"symbols_processed": ["TEST"], "finalists": [first, second]}
        with self.assertRaisesRegex(SchemaValidationError, "conflicting duplicate"):
            self.core._normalize_luna_symbols(cycle)

    def test_non_duplicate_finalists_unchanged(self):
        items = [self.candidate("AAA"), self.candidate("BBB")]
        cycle = {"symbols_processed": ["AAA", "BBB"], "finalists": deepcopy(items)}
        self.core._normalize_luna_symbols(cycle)
        self.assertEqual(cycle["finalists"], items)

    def test_revisit_telemetry_distinguishes_scan_and_finalist(self):
        cycle = {"symbols_processed": ["AAA", "BBB"], "finalists": [
            {"symbol": "AAA", "classification": "PREVIOUSLY_REJECTED_NO_MATERIAL_CHANGE"}
        ]}
        state = {"cooldowns": {
            "AAA": {"cooldown_until": "2026-09-10T10:00:00-04:00", "rejection_categories": ["EXTENSION"]},
            "BBB": {"cooldown_until": "2026-09-10T11:00:00-04:00", "rejection_categories": ["VOLUME"]},
            "CCC": {"cooldown_until": "2026-09-10T09:00:00-04:00", "rejection_categories": []},
        }}
        from datetime import datetime
        observed = self.core._revisit_observations(cycle, state, datetime.fromisoformat("2026-09-10T10:30:00-04:00"))
        self.assertEqual([item["disposition"] for item in observed], [
            "PREVIOUSLY_REJECTED_NO_MATERIAL_CHANGE", "SEEN_NOT_FINALIST", "NOT_SCANNED"
        ])
        self.assertEqual([item["cooldown_active"] for item in observed], [False, True, False])


class RunnerStructuralContractTests(unittest.TestCase):
    def runner(self):
        runner = object.__new__(CodexRunner)
        runner.project_root = ROOT
        runner.executable = "/fake/codex"
        runner.child_environment = {}
        runner.timeout_seconds = 10
        runner.transient_retries = 0
        runner.retry_backoff = 0
        runner._shadow_boundary = SimpleNamespace(
            server_name="robinhood", enabled_tools=frozenset({"get_accounts", "run_scan"})
        )
        return runner

    def test_required_research_is_enforced_before_output_acceptance(self):
        stream = "\n".join([
            json.dumps({"type": "thread.started"}), json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "id": "m"}}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ])
        completed = SimpleNamespace(returncode=0, stdout=stream, stderr=None)
        with tempfile.TemporaryDirectory() as temp, patch("trader.codex_runner.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(CodexRunError, "targeted web research") as raised:
                self.runner().run(
                    prompt_path=ROOT / "prompts/sol-senior.md",
                    schema_path=ROOT / "schemas/senior-decision.schema.json",
                    model="test", context={}, required_robinhood_tools=frozenset(),
                    allow_web=True, require_web_search=True, working_directory=Path(temp),
                )
        self.assertEqual(raised.exception.diagnostics["event_summary"]["agent_message_count"], 1)
        self.assertEqual(raised.exception.diagnostics["event_summary"]["event_type_counts"]["turn.completed"], 1)

    def test_invalid_fanout_configuration_fails_closed(self):
        with self.assertRaisesRegex(CodexRunError, "call limits"):
            self.runner().run(
                prompt_path=ROOT / "prompts/luna-stage-b.md",
                schema_path=ROOT / "schemas/luna-cycle.schema.json", model="test", context={},
                required_robinhood_tools=frozenset(), maximum_robinhood_tool_calls={"place_equity_order": 1},
            )


if __name__ == "__main__":
    unittest.main()
