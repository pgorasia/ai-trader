from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

import orchestrator
from orchestrator import ROOT, ShadowOrchestrator, parser
from trader.market_calendar import EquityMarketCalendar
from trader.models import CodexRunResult, PreflightError, SchemaValidationError
from trader.safety import load_config, validate_json
from trader.state import initial_state


class SmokeHarnessTests(unittest.TestCase):
    @staticmethod
    def historical_state():
        """Self-contained sanitized replay evidence; never depend on production state."""
        state = initial_state("2026-08-19")
        cycle = json.loads((ROOT / "tests/fixtures/luna_candidate.json").read_text())
        cycle.update({
            "cycle_id": "2026-08-19-cycle-1",
            "session_date": "2026-08-19",
            "timestamp": "2026-08-19T14:50:05-04:00",
            "scheduled_for": "2026-08-19T14:50:00-04:00",
        })
        calls = {
            "get_accounts": 1, "get_equity_orders": 1, "get_equity_positions": 1,
            "run_scan": 1, "get_equity_quotes": 1, "get_equity_tradability": 1,
            "get_equity_historicals": 1, "get_equity_technical_indicators": 3,
        }
        cycle["tool_call_count"] = {"total": sum(calls.values()), "run_scan": 1}
        cycle["cli_tool_calls"] = calls
        cycle["cli_usage"] = {}
        cycle["cli_diagnostics"] = {}
        state["cycles"] = [cycle]
        state["eod_completed"] = True
        return state

    def bare(self, root: Path, runner=None) -> ShadowOrchestrator:
        core = ShadowOrchestrator.__new__(ShadowOrchestrator)
        core.root = root
        core.config = load_config(ROOT / "config/strategy.yaml")
        core.calendar = EquityMarketCalendar(core.config["exchange_calendar"], int(core.config["schedule"]["eod_offset_minutes"]))
        core.runner = runner
        return core

    def stage_fixture(self, directory: str):
        root = Path(directory)
        (root / "state").mkdir(); (root / "schemas").mkdir()
        (root / "state/2026-08-19.json").write_text(json.dumps(self.historical_state()), encoding="utf-8")
        shutil.copy2(ROOT / "schemas/luna-cycle.schema.json", root / "schemas/luna-cycle.schema.json")
        return root, self.bare(root)

    def replay_inputs(self):
        state = self.historical_state()
        source = next(item for item in state["cycles"] if item["scheduled_for"].startswith("2026-08-19T14:50:"))
        schema_path = ROOT / "schemas/luna-cycle.schema.json"
        schema = json.loads(schema_path.read_text())
        cycle = {key: deepcopy(source[key]) for key in schema["required"]}
        calls = {key: value for key, value in source["cli_tool_calls"].items() if key != "get_portfolio"}
        cycle["tool_call_count"] = {"total": sum(calls.values()), "run_scan": calls["run_scan"]}
        state["cooldowns"] = {}
        session = EquityMarketCalendar().session_for(__import__("datetime").date(2026, 8, 19))
        return state, cycle, calls, session

    @staticmethod
    def eod_review(state):
        decisions = [{"symbol": rejection["symbol"], "decision_timestamp": decision["decision_timestamp"],
                      "classification": "INCONCLUSIVE", "subsequent_mfe_percent": None,
                      "subsequent_mae_percent": None, "later_material_setup": False, "analysis": "Synthetic test"}
                     for decision in state["senior_decisions"] for rejection in decision["rejections"]]
        symbols = {plan["original_plan"]["symbol"] for plan in state["shadow_plans"]}
        return {"session_date": state["session_date"], "timestamp": "2026-08-19T20:01:00Z",
                "symbol_bars": {symbol: [] for symbol in symbols}, "decision_reviews": decisions,
                "benchmark_closes": {"SPY": 1.0, "QQQ": 1.0},
                "robinhood_tool_call_count": 1, "errors": []}

    def test_stage_replay_is_local_read_only_and_ignores_portfolio(self):
        with tempfile.TemporaryDirectory() as directory:
            root, core = self.stage_fixture(directory)
            path = root / "state/2026-08-19.json"
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch("orchestrator.subprocess.run") as external:
                result = core.smoke_stage_b_replay("2026-08-19")
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())
            external.assert_not_called()

    def test_stage_replay_requires_scan_and_core_reconciliation(self):
        state, cycle, calls, session = self.replay_inputs(); core = self.bare(ROOT)
        for missing in ("run_scan", "get_accounts", "get_equity_positions", "get_equity_orders"):
            damaged = dict(calls); damaged.pop(missing)
            damaged_cycle = deepcopy(cycle)
            damaged_cycle["tool_call_count"] = {"total": sum(damaged.values()), "run_scan": damaged.get("run_scan", 0)}
            with self.subTest(missing=missing), self.assertRaises(SchemaValidationError):
                core._validate_luna(damaged_cycle, state, session, 0, observed_tool_calls=damaged)

    def test_stage_replay_enforces_all_finalist_evidence(self):
        state, cycle, calls, session = self.replay_inputs(); core = self.bare(ROOT)
        for missing in ("get_equity_quotes", "get_equity_tradability", "get_equity_historicals", "get_equity_technical_indicators"):
            damaged = dict(calls); damaged.pop(missing)
            damaged_cycle = deepcopy(cycle)
            damaged_cycle["tool_call_count"]["total"] = sum(damaged.values())
            with self.subTest(missing=missing), self.assertRaises(SchemaValidationError):
                core._validate_luna(damaged_cycle, state, session, 0, observed_tool_calls=damaged)

    def test_luna_schema_uses_exact_schema_zero_mcp_and_preserves_production(self):
        schema = ROOT / "schemas/luna-cycle.schema.json"
        minimal = self.historical_state()["cycles"][0]
        required = json.loads(schema.read_text())["required"]
        minimal = {key: minimal[key] for key in required}
        fake = Mock()
        fake.run.return_value = CodexRunResult(data=minimal)
        core = self.bare(ROOT, fake)
        before = orchestrator._production_snapshot(ROOT)
        result = core.smoke_luna_schema()
        self.assertTrue(result["model_invoked"])
        call = fake.run.call_args.kwargs
        self.assertEqual(call["schema_path"], schema)
        self.assertTrue(call["disable_all_mcp"])
        self.assertEqual(call["required_robinhood_tools"], frozenset())
        self.assertNotIn("robinhood_enabled_tools", call)
        self.assertEqual(call["working_directory"], ROOT)
        self.assertNotEqual(call["prompt_path"].parent, call["working_directory"])
        prompt = call["prompt_path"].read_text(encoding="utf-8") if call["prompt_path"].exists() else ""
        self.assertFalse(prompt, "temporary smoke prompt unexpectedly survived cleanup")
        self.assertEqual(before, orchestrator._production_snapshot(ROOT))

    def test_runner_mcp_disabled_contract_has_no_servers_or_tools(self):
        runner = __import__("trader.codex_runner", fromlist=["CodexRunner"]).CodexRunner.__new__(__import__("trader.codex_runner", fromlist=["CodexRunner"]).CodexRunner)
        runner.executable = "codex"; runner.project_root = ROOT
        command = runner.build_command("gpt-5.6-luna", ROOT / "schemas/luna-cycle.schema.json", Path("/tmp/out"), allow_web=False, disable_all_mcp=True)
        self.assertIn("mcp_servers={}", command)
        self.assertNotIn("enabled_tools", " ".join(command))
        self.assertEqual(command[command.index("--cd") + 1], str(ROOT))
        self.assertNotEqual(command[command.index("--output-last-message") + 1], str(ROOT))
        self.assertNotIn("--skip-git-repo-check", command)

    def test_eod_cli_requires_session(self):
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            orchestrator.main(["--smoke-eod"])

    def test_eod_refuses_active_service_before_runner(self):
        core = self.bare(ROOT, Mock())
        with patch("orchestrator._service_active", return_value=True), self.assertRaises(PreflightError):
            core.smoke_eod("2026-08-19")
        core.runner.run.assert_not_called()

    def test_eod_uses_production_pipeline_and_only_historicals(self):
        state = self.historical_state()
        review = self.eod_review(state)
        fake = Mock(); fake.run.return_value = CodexRunResult(data=review, tool_calls={"get_equity_historicals": 1})
        with tempfile.TemporaryDirectory() as directory:
            smoke_root = Path(directory)
            (smoke_root / "state").mkdir(); (smoke_root / "reports").mkdir()
            (smoke_root / "prompts").symlink_to(ROOT / "prompts", target_is_directory=True)
            (smoke_root / "schemas").symlink_to(ROOT / "schemas", target_is_directory=True)
            (smoke_root / "methodology").symlink_to(ROOT / "methodology", target_is_directory=True)
            state_path = smoke_root / "state/2026-08-19.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            core = self.bare(smoke_root, fake)
            reports_before = orchestrator._directory_snapshot(smoke_root / "reports")
            state_before = hashlib.sha256(state_path.read_bytes()).hexdigest()
            with patch("orchestrator._service_active", return_value=False):
                result = core.smoke_eod("2026-08-19")
            self.assertEqual(state_before, hashlib.sha256(state_path.read_bytes()).hexdigest())
            self.assertEqual(reports_before, orchestrator._directory_snapshot(smoke_root / "reports"))
        call = fake.run.call_args.kwargs
        self.assertEqual(call["robinhood_enabled_tools"], frozenset({"get_equity_historicals"}))
        self.assertEqual(call["required_robinhood_tools"], frozenset({"get_equity_historicals"}))
        self.assertNotIn("exact_robinhood_tools", call)
        self.assertEqual(call["working_directory"], smoke_root)
        self.assertEqual(call["prompt_path"], smoke_root / "prompts/eod-review.md")
        self.assertEqual(call["schema_path"], smoke_root / "schemas/eod-review.schema.json")
        methodology = (ROOT / "methodology/eod-v1.md").read_bytes()
        self.assertEqual(call["context"]["eod_methodology"]["sha256"], hashlib.sha256(methodology).hexdigest())
        self.assertEqual(result["allowed_robinhood_tools"], ["get_equity_historicals"])

    def test_eod_semantic_validator_is_production_validator(self):
        state = self.historical_state()
        review = self.eod_review(state)
        core = self.bare(ROOT)
        core._validate_eod_review(review, state, 0)
        damaged = deepcopy(review); damaged["benchmark_closes"]["SPY"] = None
        with self.assertRaises(Exception): core._validate_eod_review(damaged, state, 0)

    def test_all_smokes_refuse_non_shadow(self):
        altered = load_config(ROOT / "config/strategy.yaml"); altered["mode"] = "APPROVAL"
        for command in (["--smoke-luna-schema"], ["--smoke-stage-b-replay", "--session", "2026-08-19"], ["--smoke-eod", "--session", "2026-08-19"]):
            with self.subTest(command=command), patch("orchestrator.load_config", return_value=altered), patch("orchestrator.ShadowOrchestrator") as constructor:
                self.assertEqual(orchestrator.main(command), 2)
                constructor.assert_not_called()

    def test_no_smoke_contract_contains_write_tool(self):
        allowed = frozenset().union(frozenset(), orchestrator.JOB_TOOL_CONTRACTS["STAGE_B"], orchestrator.JOB_TOOL_CONTRACTS["EOD"])
        write_prefixes = ("place_", "cancel_", "review_", "create_", "update_", "delete_", "submit_", "modify_")
        self.assertFalse([tool for tool in allowed if tool.startswith(write_prefixes)])


if __name__ == "__main__":
    unittest.main()
