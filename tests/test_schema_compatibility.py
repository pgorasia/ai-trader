from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path

from trader.models import SchemaValidationError
from trader.safety import lint_codex_output_schema, validate_json, validate_schema_file


ROOT = Path(__file__).resolve().parents[1]


class CodexSchemaCompatibilityTests(unittest.TestCase):
    def test_all_production_schemas_pass_compatibility_lint(self):
        for path in sorted((ROOT / "schemas").glob("*.schema.json")):
            with self.subTest(schema=path.name):
                validate_schema_file(path)

    def test_no_production_schema_contains_unique_items(self):
        for path in (ROOT / "schemas").glob("*.schema.json"):
            self.assertNotIn('"uniqueItems"', path.read_text(encoding="utf-8"))

    def test_linter_rejects_representative_unsupported_keywords(self):
        for keyword, value in (("uniqueItems", True), ("minItems", 1), ("format", "date-time"), ("minimum", 0)):
            schema = {"type": "object", "additionalProperties": False, "required": ["value"], "properties": {"value": {"type": "array" if keyword in {"uniqueItems", "minItems"} else "string" if keyword == "format" else "number", keyword: value}}}
            with self.subTest(keyword=keyword), self.assertRaises(SchemaValidationError):
                lint_codex_output_schema(schema)

    def test_supported_array_enum_and_anyof_null_are_accepted(self):
        schema = {"type": "object", "additionalProperties": False, "required": ["values", "choice", "maybe"], "properties": {"values": {"type": "array", "items": {"type": "string"}}, "choice": {"enum": ["A", "B"]}, "maybe": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
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
