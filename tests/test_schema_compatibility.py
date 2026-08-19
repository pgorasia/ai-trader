from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path

import jsonschema

from trader.models import SchemaValidationError
from trader.safety import lint_codex_output_schema, validate_json, validate_schema_file


ROOT = Path(__file__).resolve().parents[1]


class CodexSchemaCompatibilityTests(unittest.TestCase):
    def test_all_production_schemas_pass_compatibility_lint(self):
        for path in sorted((ROOT / "schemas").rglob("*.json")):
            with self.subTest(schema=path.name):
                validate_schema_file(path)

    def test_named_model_response_schemas_pass_compatibility_lint(self):
        names = (
            "preflight.schema.json",
            "preflight-portfolio.schema.json",
            "preflight-positions.schema.json",
            "preflight-orders.schema.json",
            "senior-decision.schema.json",
            "shadow-monitor.schema.json",
            "eod-review.schema.json",
        )
        for name in names:
            with self.subTest(schema=name):
                validate_schema_file(ROOT / "schemas" / name)

    def test_luna_single_value_booleans_are_explicitly_typed(self):
        schema = json.loads((ROOT / "schemas" / "luna-cycle.schema.json").read_text(encoding="utf-8"))
        complete = schema["$defs"]["bar"]["properties"]["complete"]
        material = schema["properties"]["finalists"]["items"]["properties"]["material_requalification"]
        new_high_only = material["anyOf"][1]["properties"]["new_high_only"]
        self.assertEqual(complete, {"type": "boolean", "enum": [True]})
        self.assertEqual(new_high_only, {"type": "boolean", "enum": [False]})
        jsonschema.validate(True, complete)
        jsonschema.validate(False, new_high_only)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(False, complete)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(True, new_high_only)

    def test_linter_rejects_const_without_explicit_type(self):
        schema = {"type": "object", "additionalProperties": False, "required": ["value"], "properties": {"value": {"const": True}}}
        with self.assertRaisesRegex(SchemaValidationError, "must have an explicit type"):
            lint_codex_output_schema(schema)

    def test_linter_rejects_enum_without_explicit_type(self):
        schema = {"type": "object", "additionalProperties": False, "required": ["value"], "properties": {"value": {"enum": ["A", "B"]}}}
        with self.assertRaisesRegex(SchemaValidationError, "must have an explicit type"):
            lint_codex_output_schema(schema)

    def test_linter_rejects_object_missing_required_property(self):
        schema = {"type": "object", "additionalProperties": False, "required": [], "properties": {"value": {"type": "string"}}}
        with self.assertRaisesRegex(SchemaValidationError, "require every property"):
            lint_codex_output_schema(schema)

    def test_linter_rejects_object_missing_additional_properties_false(self):
        schema = {"type": "object", "required": ["value"], "properties": {"value": {"type": "string"}}}
        with self.assertRaisesRegex(SchemaValidationError, "additionalProperties false"):
            lint_codex_output_schema(schema)

    def test_no_production_schema_contains_unique_items(self):
        for path in (ROOT / "schemas").glob("*.schema.json"):
            self.assertNotIn('"uniqueItems"', path.read_text(encoding="utf-8"))

    def test_linter_rejects_representative_unsupported_keywords(self):
        for keyword, value in (("uniqueItems", True), ("minItems", 1), ("format", "date-time"), ("minimum", 0)):
            schema = {"type": "object", "additionalProperties": False, "required": ["value"], "properties": {"value": {"type": "array" if keyword in {"uniqueItems", "minItems"} else "string" if keyword == "format" else "number", keyword: value}}}
            with self.subTest(keyword=keyword), self.assertRaises(SchemaValidationError):
                lint_codex_output_schema(schema)

    def test_supported_array_enum_and_anyof_null_are_accepted(self):
        schema = {"type": "object", "additionalProperties": False, "required": ["values", "choice", "maybe"], "properties": {"values": {"type": "array", "items": {"type": "string"}}, "choice": {"type": "string", "enum": ["A", "B"]}, "maybe": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
        lint_codex_output_schema(schema)

    def test_non_boolean_staged_agentic_discriminator_rejected(self):
        data = {
            "passed": False,
            "account_classifications": [{"agentic_allowed": "true", "brokerage_account_type": "individual", "management_type": "self_directed", "state": "active", "deactivated": False, "permanently_deactivated": False}],
            "errors": ["invalid discriminator"],
        }
        with self.assertRaisesRegex(SchemaValidationError, "not of type 'boolean'"):
            validate_json(data, ROOT / "schemas" / "preflight.schema.json")


if __name__ == "__main__":
    unittest.main()
