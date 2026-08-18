from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from orchestrator import ShadowOrchestrator
from trader.codex_runner import CodexRunner
from trader.models import CodexRunError, CodexRunResult, PreflightError, SchemaValidationError
from trader.safety import derive_preflight_identity, validate_json
from trader.shadow_boundary import APPROVED_SHADOW_ROBINHOOD_TOOLS, ShadowBoundaryResult, locate_codex_config
from trader.state import StateStore, initial_state


ROOT = Path(__file__).resolve().parents[1]
STAGE_TOOLS = {
    "identity": frozenset({"get_accounts"}),
    "portfolio": frozenset({"get_accounts", "get_portfolio"}),
    "positions": frozenset({"get_accounts", "get_equity_positions"}),
    "orders": frozenset({"get_accounts", "get_equity_orders"}),
}


def payload(stage: str) -> dict:
    common = {"passed": True, "account_classifications": [valid_account()], "errors": []}
    if stage == "identity": return common
    if stage == "portfolio": return {**common, "account_reconciled": True, "account_equity": 100.0, "buying_power": 100.0, "portfolio_status": "active"}
    if stage == "positions": return {**common, "account_reconciled": True, "baseline_position_count": 0, "baseline_positions_present": False, "baseline_positions": []}
    return {**common, "account_reconciled": True, "relevant_order_count": 0, "open_pending_count": 0, "baseline_external_order_count": 0, "baseline_external_orders_present": False, "baseline_external_orders": []}


def valid_account(**changes) -> dict:
    account = {"agentic_allowed": True, "brokerage_account_type": "individual", "management_type": "self_directed", "state": "active", "deactivated": False, "permanently_deactivated": False}
    account.update(changes)
    return account


class Clock:
    def now(self): return datetime.fromisoformat("2026-08-14T09:35:01-04:00")


class Boundary:
    policy_version = "shadow-robinhood-readonly-v1"


class StagedRunner:
    def __init__(self, mutations=None, failures=None, diagnostics=None):
        self.mutations = mutations or {}
        self.failures = failures or {}
        self.diagnostics = diagnostics or {}
        self.calls = []

    @staticmethod
    def stage(kwargs):
        name = kwargs["prompt_path"].name
        return "identity" if name == "preflight.md" else name.removeprefix("preflight-").removesuffix(".md")

    def run(self, **kwargs):
        stage = self.stage(kwargs)
        self.calls.append(kwargs)
        if stage in self.failures: raise CodexRunError(self.failures[stage])
        self.assert_contract(stage, kwargs)
        data = deepcopy(payload(stage))
        data.update(deepcopy(self.mutations.get(stage, {})))
        diagnostic = deepcopy(self.diagnostics.get(stage, {"mcp_teardown_warning": False, "diagnostic_codes": []}))
        return CodexRunResult(data=data, tool_calls={tool: 1 for tool in STAGE_TOOLS[stage]}, diagnostics=diagnostic)

    @staticmethod
    def assert_contract(stage, kwargs):
        if kwargs["required_robinhood_tools"] != STAGE_TOOLS[stage]: raise CodexRunError("wrong required tool contract")
        if kwargs["robinhood_enabled_tools"] != STAGE_TOOLS[stage]: raise CodexRunError("wrong per-run tool restriction")
        if kwargs["allow_web"] or kwargs["model"] != "gpt-5.6-luna": raise CodexRunError("wrong model or web policy")

    def safe_diagnostics(self): return {"mcp_teardown_warning": False, "diagnostic_codes": []}


class StagedPreflightTests(unittest.TestCase):
    def core(self, directory, runner):
        core = ShadowOrchestrator.__new__(ShadowOrchestrator)
        core.root = Path(directory)
        core.store = StateStore(core.root / "state")
        core.runner = runner
        core.boundary = Boundary()
        core.config = {"models": {"luna": "gpt-5.6-luna"}}
        core.clock = Clock()
        return core

    def execute(self, runner):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        core = self.core(directory.name, runner)
        state = initial_state("2026-08-14")
        core.store.save(state)
        result = core.preflight(state, datetime.fromisoformat("2026-08-14T09:35:00-04:00"))
        return core, state, result

    def test_all_four_stages_pass_with_exact_tools_luna_and_no_web(self):
        runner = StagedRunner()
        _core, _state, result = self.execute(runner)
        self.assertEqual([StagedRunner.stage(call) for call in runner.calls], ["identity", "portfolio", "positions", "orders"])
        self.assertEqual(result["timestamp"], "2026-08-14T09:35:00-04:00")
        self.assertTrue(all(result[key]["status"] == "PASS" for key in ("identity_job", "portfolio_job", "positions_job", "orders_job")))
        self.assertTrue(all(call["model"] == "gpt-5.6-luna" and not call["allow_web"] for call in runner.calls))

    def test_identity_failure_stops_later_stages(self):
        runner = StagedRunner(mutations={"identity": {"passed": False, "account_classifications": [valid_account(agentic_allowed=False)], "errors": ["ambiguous"]}})
        with self.assertRaises(PreflightError): self.execute(runner)
        self.assertEqual(len(runner.calls), 1)

    def test_missing_stage_calls_fail_closed_at_that_stage(self):
        for stage, expected_calls in (("portfolio", 2), ("positions", 3), ("orders", 4)):
            with self.subTest(stage=stage):
                runner = StagedRunner(failures={stage: "Required Robinhood tool calls were not observed"})
                with self.assertRaises(CodexRunError): self.execute(runner)
                self.assertEqual(len(runner.calls), expected_calls)

    def test_extra_duplicate_and_incomplete_tool_activity_fail_closed(self):
        for message in ("Unexpected Robinhood tool calls were observed", "must complete exactly once", "unterminated tool calls"):
            with self.subTest(message=message):
                runner = StagedRunner(failures={"portfolio": message})
                with self.assertRaises(CodexRunError): self.execute(runner)
                self.assertEqual(len(runner.calls), 2)

    def test_per_run_tools_outside_global_policy_rejected_before_launch(self):
        runner = CodexRunner.__new__(CodexRunner)
        runner._shadow_boundary = ShadowBoundaryResult(Path("/tmp/config.toml"), "robinhood-trading", APPROVED_SHADOW_ROBINHOOD_TOOLS)
        with patch("trader.codex_runner.subprocess.run") as child, self.assertRaises(CodexRunError):
            runner.run(prompt_path=ROOT / "prompts/preflight.md", schema_path=ROOT / "schemas/preflight.schema.json", model="test", context={}, required_robinhood_tools=frozenset({"review_equity_order"}), robinhood_enabled_tools=frozenset({"review_equity_order"}))
        child.assert_not_called()

    def test_config_override_serialization_and_global_config_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            configured = Path(directory) / "config.toml"
            configured.write_text('[mcp_servers.robinhood-trading]\nurl = "https://example.invalid/mcp"\n', encoding="utf-8")
            config_path = locate_codex_config({"config_path": str(configured.resolve())})
            before = config_path.read_bytes()
            runner = CodexRunner.__new__(CodexRunner)
            runner.executable = "codex"
            runner.project_root = ROOT
            runner._shadow_boundary = ShadowBoundaryResult(config_path, "robinhood-trading", APPROVED_SHADOW_ROBINHOOD_TOOLS)
            command = runner.build_command("gpt-5.6-luna", ROOT / "schemas/preflight-portfolio.schema.json", Path("/tmp/final.json"), allow_web=False, robinhood_enabled_tools=STAGE_TOOLS["portfolio"])
            override = command[command.index("--config") + 1]
            self.assertEqual(override, 'mcp_servers.robinhood-trading.enabled_tools=["get_accounts","get_portfolio"]')
            self.assertIn("shell_tool", command)
            self.assertEqual(config_path.read_bytes(), before)

    def test_raw_account_identifier_rejected_and_never_reported(self):
        unsafe = {**payload("identity"), "account_id": "RAW-ACCOUNT-123"}
        with self.assertRaises(SchemaValidationError): validate_json(unsafe, ROOT / "schemas/preflight.schema.json")
        core, state, _result = self.execute(StagedRunner())
        report = (core.root / state["preflight_operations"][0]["report_artifact"]).read_text(encoding="utf-8")
        self.assertNotIn("account_id", report)
        self.assertNotIn("account_number", report)

    def test_teardown_warning_is_recorded_per_stage(self):
        code = "ROBINHOOD_SESSION_DELETE_HTTP_400_AFTER_COMPLETION"
        _core, _state, result = self.execute(StagedRunner(diagnostics={"portfolio": {"mcp_teardown_warning": True, "diagnostic_codes": [code]}}))
        self.assertEqual(result["portfolio_job"]["mcp_teardown_warning_codes"], [code])

    def test_ordinary_mcp_failure_remains_fail_closed(self):
        runner = StagedRunner(failures={"positions": "MCP transport failure"})
        with self.assertRaises(CodexRunError): self.execute(runner)
        self.assertEqual(len(runner.calls), 3)

    def test_safe_identity_discrepancy_between_jobs_fails(self):
        runner = StagedRunner(mutations={"portfolio": {"account_classifications": [valid_account(), valid_account()]}})
        with self.assertRaises(PreflightError): self.execute(runner)
        self.assertEqual(len(runner.calls), 2)

    def test_existing_positions_pass_and_persist_as_external_baseline(self):
        for positions in ([{"symbol": "MU", "quantity": 2.0}], [{"symbol": "MU", "quantity": 2.0}, {"symbol": "SPY", "quantity": 0.5}]):
            with self.subTest(count=len(positions)):
                mutation = {"baseline_position_count": len(positions), "baseline_positions_present": True, "baseline_positions": positions}
                runner = StagedRunner(mutations={"positions": mutation})
                _core, state, result = self.execute(runner)
                self.assertEqual(len(runner.calls), 4)
                self.assertEqual(result["positions_job"]["baseline_position_count"], len(positions))
                self.assertTrue(result["positions_job"]["baseline_positions_present"])
                self.assertEqual(state["baseline_positions"], [{"attribution": "BASELINE_EXTERNAL", **item} for item in positions])
                self.assertEqual(state["shadow_positions"], [])
                self.assertEqual(state["completed_shadow_trades"], [])

    def test_external_open_orders_pass_and_persist_separately(self):
        for orders in ([{"symbol": "TEST", "state": "open", "side": "buy"}], [{"symbol": "MU", "state": "queued", "side": "buy"}, {"symbol": "SPY", "state": "confirmed", "side": "sell"}]):
            with self.subTest(count=len(orders)):
                mutation = {"relevant_order_count": len(orders), "open_pending_count": len(orders), "baseline_external_order_count": len(orders), "baseline_external_orders_present": True, "baseline_external_orders": orders}
                _core, state, result = self.execute(StagedRunner(mutations={"orders": mutation}))
                expected = [{"attribution": "BASELINE_EXTERNAL_ORDER", **item} for item in orders]
                self.assertEqual(result["orders_job"]["baseline_external_order_count"], len(orders))
                self.assertTrue(result["orders_job"]["baseline_external_orders_present"])
                self.assertEqual(state["baseline_external_orders"], expected)
                self.assertEqual(state["shadow_positions"], [])
                self.assertEqual(state["completed_shadow_trades"], [])

    def test_position_and_external_order_pass_together(self):
        positions = {"baseline_position_count": 1, "baseline_positions_present": True, "baseline_positions": [{"symbol": "MU", "quantity": 50.0}]}
        orders = {"relevant_order_count": 1, "open_pending_count": 1, "baseline_external_order_count": 1, "baseline_external_orders_present": True, "baseline_external_orders": [{"symbol": "MU", "state": "open", "side": "buy"}]}
        _core, state, result = self.execute(StagedRunner(mutations={"positions": positions, "orders": orders}))
        self.assertEqual(result["positions_job"]["status"], "PASS")
        self.assertEqual(result["orders_job"]["status"], "PASS")
        self.assertEqual(state["baseline_positions"][0]["attribution"], "BASELINE_EXTERNAL")
        self.assertEqual(state["baseline_external_orders"][0]["attribution"], "BASELINE_EXTERNAL_ORDER")

    def test_external_fill_reconciles_only_as_external_account_activity(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        open_order = {"relevant_order_count": 1, "open_pending_count": 1, "baseline_external_order_count": 1, "baseline_external_orders_present": True, "baseline_external_orders": [{"symbol": "MU", "state": "open", "side": "buy"}]}
        runner = StagedRunner(mutations={"orders": open_order})
        core = self.core(directory.name, runner); state = initial_state("2026-08-14"); core.store.save(state)
        core.preflight(state, datetime.fromisoformat("2026-08-14T09:35:00-04:00"))
        state["shadow_plans"] = []; state["shadow_positions"] = []; state["completed_shadow_trades"] = []
        runner.mutations = {"positions": {"baseline_position_count": 1, "baseline_positions_present": True, "baseline_positions": [{"symbol": "MU", "quantity": 5.0}]}}
        core.preflight(state, datetime.fromisoformat("2026-08-14T09:36:00-04:00"))
        self.assertEqual(state["baseline_external_orders"], [])
        self.assertEqual(state["baseline_positions"], [{"attribution": "BASELINE_EXTERNAL", "symbol": "MU", "quantity": 5.0}])
        self.assertEqual(state["shadow_positions"], [])
        self.assertEqual(state["completed_shadow_trades"], [])


class AgenticIdentitySemanticsTests(unittest.TestCase):
    def derive(self, accounts, **extra):
        data = {"account_classifications": deepcopy(accounts), **extra}
        derive_preflight_identity(data)
        return data

    def test_false_then_true_selects_exactly_one(self):
        data = self.derive([valid_account(agentic_allowed=False), valid_account()])
        self.assertEqual(data["agentic_account_count"], 1)
        self.assertTrue(data["unique_agentic_account"])

    def test_zero_true_fails_with_exact_reason(self):
        with self.assertRaisesRegex(PreflightError, "No account with agentic_allowed=true"):
            self.derive([valid_account(agentic_allowed=False)])

    def test_multiple_true_fails_with_exact_reason(self):
        with self.assertRaisesRegex(PreflightError, "Multiple accounts with agentic_allowed=true"):
            self.derive([valid_account(), valid_account()])

    def test_string_true_and_missing_field_are_not_candidates(self):
        for accounts in ([valid_account(agentic_allowed="true")], [{key: value for key, value in valid_account().items() if key != "agentic_allowed"}]):
            with self.subTest(accounts=accounts), self.assertRaisesRegex(PreflightError, "No account"):
                self.derive(accounts)

    def test_selected_account_sanity_checks_fail_closed(self):
        cases = {
            "state": "disabled", "deactivated": True, "permanently_deactivated": True,
            "brokerage_account_type": "joint", "management_type": "managed",
        }
        for field, value in cases.items():
            with self.subTest(field=field), self.assertRaisesRegex(PreflightError, field):
                self.derive([valid_account(**{field: value})])

    def test_default_cash_margin_and_order_do_not_select(self):
        false_account = valid_account(agentic_allowed=False)
        false_account.update({"is_default": True, "type": "cash"})
        true_account = valid_account()
        true_account.update({"is_default": False, "type": "margin"})
        for accounts in ([false_account, true_account], [true_account, false_account]):
            data = self.derive(accounts)
            self.assertTrue(data["selected_account_classification"]["agentic_allowed"])
            self.assertNotIn("is_default", data["selected_account_classification"])
            self.assertNotIn("type", data["selected_account_classification"])

    def test_model_reported_count_cannot_override_python(self):
        data = self.derive([valid_account()], agentic_account_count=99, unique_agentic_account=False)
        self.assertEqual(data["agentic_account_count"], 1)
        self.assertTrue(data["unique_agentic_account"])

    def test_all_stage_schemas_share_safe_identity_contract(self):
        classifications = payload("identity")["account_classifications"]
        for schema in ("preflight.schema.json", "preflight-portfolio.schema.json", "preflight-positions.schema.json", "preflight-orders.schema.json"):
            properties = json.loads((ROOT / "schemas" / schema).read_text())["$defs"]["accountClassification"]["properties"]
            self.assertEqual(set(properties), set(classifications[0]))
            self.assertNotIn("agentic_account_count", json.loads((ROOT / "schemas" / schema).read_text())["properties"])


if __name__ == "__main__":
    unittest.main()
