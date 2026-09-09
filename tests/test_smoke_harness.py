from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import orchestrator
from orchestrator import ROOT, ShadowOrchestrator, parser
from trader.market_calendar import EquityMarketCalendar
from trader.models import CodexRunError, CodexRunResult, PreflightError, SchemaValidationError
from trader.safety import load_config, validate_json
from trader.state import initial_state
from trader.shadow_boundary import APPROVED_SHADOW_ROBINHOOD_TOOLS


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

    def luna_probe_result(self, *, calls=None, malformed=False, offsets=(0, 5, 10), complete=True):
        session = EquityMarketCalendar().session_for(date(2026, 8, 19))
        start = session.market_open
        data = {"probe_symbol": "AAPL", "session_date": session.session_date, "errors": []}
        data["source_5m_bars"] = [
            {"timestamp": (start + timedelta(minutes=offset)).isoformat(), "open": 10,
             "high": 9 if malformed and offset == 0 else 11, "low": 9,
             "close": 10, "volume": 100, "complete": complete}
            for offset in offsets]
        return CodexRunResult(data=data,
            tool_calls={"get_equity_historicals": 1} if calls is None else calls)

    def test_luna_schema_live_probe_requires_history_and_preserves_production(self):
        schema = ROOT / "schemas/historical-probe.schema.json"
        fake = Mock()
        fake.run.return_value = self.luna_probe_result()
        core = self.bare(ROOT, fake)
        before = orchestrator._production_snapshot(ROOT)
        result = core.smoke_luna_schema("2026-08-19")
        self.assertTrue(result["model_invoked"])
        self.assertEqual(result["historical_reads"], 1)
        self.assertEqual(result["source_5m_bars"], 3)
        self.assertEqual(result["derived_15m_bars"], 1)
        call = fake.run.call_args.kwargs
        self.assertEqual(call["schema_path"], schema)
        self.assertEqual(call["required_robinhood_tools"], frozenset({"get_equity_historicals"}))
        self.assertEqual(call["robinhood_enabled_tools"], frozenset({"get_equity_historicals"}))
        self.assertTrue(call["exact_robinhood_tools"])
        self.assertEqual(call["working_directory"], ROOT)
        prompt = call["prompt_path"].read_text(encoding="utf-8")
        self.assertIn("Make exactly one `get_equity_historicals` call", prompt)
        self.assertIn("regular-session 5-minute OHLCV", prompt)
        self.assertIn("Python can exercise deterministic 15-minute aggregation", prompt)
        self.assertIn("Do not return VWAP", prompt)
        probe_schema = json.loads(schema.read_text())
        self.assertNotIn("vwap", json.dumps(probe_schema).lower())
        self.assertEqual(set(probe_schema["properties"]), {"probe_symbol", "session_date", "source_5m_bars", "errors"})
        self.assertEqual(before, orchestrator._production_snapshot(ROOT))

    def test_luna_schema_live_probe_fails_without_observed_historical_call(self):
        fake = Mock(); fake.run.return_value = self.luna_probe_result(calls={})
        with self.assertRaisesRegex(CodexRunError, "exactly one historical read"):
            self.bare(ROOT, fake).smoke_luna_schema("2026-08-19")

    def acceptance_runner(self, *, identity=None, failure_tool=None):
        runner = Mock()
        account = {"agentic_allowed": True, "brokerage_account_type": "individual",
                   "management_type": "self_directed", "state": "active",
                   "deactivated": False, "permanently_deactivated": False}
        identity_data = {"passed": True, "account_contexts": [
            {"account_number": "EPHEMERAL-ACCOUNT", **account}], "errors": []}
        if identity is not None:
            identity_data = identity

        def run(**kwargs):
            tool = next(iter(kwargs["required_robinhood_tools"]))
            if tool == failure_tool:
                raise CodexRunError(f"{tool} failed")
            if tool == "get_accounts":
                data = deepcopy(identity_data)
            elif tool == "get_portfolio":
                data = {"account_equity": 100.0,
                        "buying_power": 100.0, "portfolio_status": "active"}
            elif tool == "get_equity_positions":
                data = {"baseline_position_count": 0,
                        "baseline_positions_present": False, "baseline_positions": []}
            else:
                data = {"relevant_order_count": 0,
                        "open_pending_count": 0, "baseline_external_order_count": 0,
                        "baseline_external_orders_present": False, "baseline_external_orders": []}
            return CodexRunResult(data=data, tool_calls={tool: 1})
        runner.run.side_effect = run
        return runner

    def test_live_acceptance_preflight_is_deterministically_account_first(self):
        runner = self.acceptance_runner(); core = self.bare(ROOT, runner)
        core.boundary = Mock(policy_version="shadow-robinhood-readonly-v1")
        with patch("orchestrator._service_active", return_value=False):
            result = core.smoke_preflight_acceptance()
        self.assertEqual(result["status"], "PASS")
        calls = [call.kwargs for call in runner.run.call_args_list]
        self.assertEqual([call["required_robinhood_tools"] for call in calls], [
            frozenset({"get_accounts"}), frozenset({"get_portfolio"}),
            frozenset({"get_equity_positions"}), frozenset({"get_equity_orders"})])
        self.assertTrue(all(call["required_robinhood_tools"] == call["robinhood_enabled_tools"] for call in calls))
        self.assertNotIn("selected_account_number", calls[0]["context"])
        self.assertTrue(all(call["context"]["selected_account_number"] == "EPHEMERAL-ACCOUNT" for call in calls[1:]))
        self.assertEqual([call["schema_path"].name for call in calls[1:]], [
            "preflight-acceptance-portfolio.schema.json",
            "preflight-acceptance-positions.schema.json",
            "preflight-acceptance-orders.schema.json",
        ])
        for call in calls[1:]:
            properties = json.loads(call["schema_path"].read_text())["properties"]
            self.assertFalse({"passed", "errors", "account_reconciled", "account_classifications"} & set(properties))
        self.assertTrue(all(call["expected_robinhood_arguments"] == {
            next(iter(call["required_robinhood_tools"])): {"account_number": "EPHEMERAL-ACCOUNT"}
        } for call in calls[1:]))
        self.assertNotIn("EPHEMERAL-ACCOUNT", json.dumps(result))

    def test_live_acceptance_missing_or_failed_accounts_stops_scoped_calls(self):
        account = {"agentic_allowed": False, "brokerage_account_type": "individual",
                   "management_type": "self_directed", "state": "active",
                   "deactivated": False, "permanently_deactivated": False}
        missing = self.acceptance_runner(identity={"passed": False, "account_contexts": [
            {"account_number": "", **account}], "errors": ["missing"]})
        failed = self.acceptance_runner(failure_tool="get_accounts")
        for runner in (missing, failed):
            core = self.bare(ROOT, runner); core.boundary = Mock(policy_version="policy")
            with self.subTest(runner=runner), patch("orchestrator._service_active", return_value=False), self.assertRaises((PreflightError, CodexRunError)):
                core.smoke_preflight_acceptance()
            self.assertEqual(runner.run.call_count, 1)

    def test_live_acceptance_scoped_failure_fails_closed_without_later_calls(self):
        runner = self.acceptance_runner(failure_tool="get_portfolio")
        core = self.bare(ROOT, runner); core.boundary = Mock(policy_version="policy")
        with patch("orchestrator._service_active", return_value=False), self.assertRaises(CodexRunError):
            core.smoke_preflight_acceptance()
        self.assertEqual(runner.run.call_count, 2)

    def test_live_acceptance_exposes_only_four_approved_reads(self):
        runner = self.acceptance_runner(); core = self.bare(ROOT, runner)
        core.boundary = Mock(policy_version="policy")
        with patch("orchestrator._service_active", return_value=False): core.smoke_preflight_acceptance()
        exposed = set().union(*(call.kwargs["robinhood_enabled_tools"] for call in runner.run.call_args_list))
        self.assertEqual(exposed, {"get_accounts", "get_portfolio", "get_equity_positions", "get_equity_orders"})
        self.assertTrue(exposed <= APPROVED_SHADOW_ROBINHOOD_TOOLS)
        self.assertEqual(len(APPROVED_SHADOW_ROBINHOOD_TOOLS), 22)
        self.assertFalse([name for name in APPROVED_SHADOW_ROBINHOOD_TOOLS if name.startswith(
            ("place_", "cancel_", "review_", "create_", "update_", "delete_", "submit_", "modify_"))])

    def test_luna_schema_live_probe_fails_closed_on_malformed_source_bars(self):
        fake = Mock(); fake.run.return_value = self.luna_probe_result(malformed=True)
        with self.assertRaisesRegex(SchemaValidationError, "Malformed OHLC"):
            self.bare(ROOT, fake).smoke_luna_schema("2026-08-19")

    def test_luna_schema_live_probe_rejects_incomplete_misaligned_outside_and_duplicate_bars(self):
        cases = {
            "incomplete": self.luna_probe_result(complete=False),
            "misaligned": self.luna_probe_result(offsets=(1, 6, 11)),
            "outside": self.luna_probe_result(offsets=(-5, 0, 5)),
            "duplicate": self.luna_probe_result(offsets=(0, 0, 5)),
        }
        for name, result in cases.items():
            fake = Mock(); fake.run.return_value = result
            with self.subTest(name=name), self.assertRaises(SchemaValidationError):
                self.bare(ROOT, fake).smoke_luna_schema("2026-08-19")

    def test_production_luna_vwap_semantics_are_unchanged(self):
        cycle = json.loads((ROOT / "tests/fixtures/luna_candidate.json").read_text())
        cycle["finalists"][0]["vwap"] = 0
        with self.assertRaisesRegex(SchemaValidationError, "vwap is outside its permitted bounds"):
            validate_json(cycle, ROOT / "schemas/luna-cycle.schema.json")

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

    def test_eod_replays_persisted_review_through_schema_and_production_validator(self):
        state = self.historical_state()
        review = self.eod_review(state)
        state["eod_review"] = review
        fake = Mock()
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
        fake.run.assert_not_called()
        self.assertEqual(result["smoke"], "EOD_PERSISTED_REPLAY")
        self.assertEqual(result["allowed_robinhood_tools"], [])

    def test_eod_persisted_replay_rejects_schema_invalid_completed_review(self):
        state = self.historical_state()
        state["eod_review"] = self.eod_review(state)
        state["eod_review"]["unexpected"] = "field"
        with tempfile.TemporaryDirectory() as directory:
            smoke_root = Path(directory)
            (smoke_root / "state").mkdir(); (smoke_root / "reports").mkdir()
            (smoke_root / "schemas").symlink_to(ROOT / "schemas", target_is_directory=True)
            (smoke_root / "state/2026-08-19.json").write_text(json.dumps(state), encoding="utf-8")
            with patch("orchestrator._service_active", return_value=False), self.assertRaises(SchemaValidationError):
                self.bare(smoke_root, Mock()).smoke_eod("2026-08-19")

    def test_eod_semantic_validator_is_production_validator(self):
        state = self.historical_state()
        review = self.eod_review(state)
        core = self.bare(ROOT)
        core._validate_eod_review(review, state, 0)
        damaged = deepcopy(review); damaged["benchmark_closes"]["SPY"] = None
        with self.assertRaises(Exception): core._validate_eod_review(damaged, state, 0)

    def test_all_smokes_refuse_non_shadow(self):
        altered = load_config(ROOT / "config/strategy.yaml"); altered["mode"] = "APPROVAL"
        for command in (["--smoke-luna-schema", "--session", "2026-08-19"], ["--smoke-stage-b-replay", "--session", "2026-08-19"], ["--smoke-eod", "--session", "2026-08-19"]):
            with self.subTest(command=command), patch("orchestrator.load_config", return_value=altered), patch("orchestrator.ShadowOrchestrator") as constructor:
                self.assertEqual(orchestrator.main(command), 2)
                constructor.assert_not_called()

    def test_no_smoke_contract_contains_write_tool(self):
        allowed = frozenset().union(frozenset(), orchestrator.JOB_TOOL_CONTRACTS["STAGE_B"], orchestrator.JOB_TOOL_CONTRACTS["EOD"])
        write_prefixes = ("place_", "cancel_", "review_", "create_", "update_", "delete_", "submit_", "modify_")
        self.assertFalse([tool for tool in allowed if tool.startswith(write_prefixes)])


if __name__ == "__main__":
    unittest.main()
