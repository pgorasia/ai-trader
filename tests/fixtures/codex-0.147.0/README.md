# Sanitized Codex exec protocol fixture

Validated parser target: native Codex CLI `0.147.0`, `codex exec --json` item lifecycle envelopes.

The JSONL fixture is intentionally metadata-only. It records event type, item ID/type, MCP server/tool identity where the real envelope supplies it, and turn lifecycle. It contains no arguments, results, account data, tokens, or session identifiers. In this observed shape, MCP identity is present on `item.started` and may be absent on the matching `item.completed`.

The final structured assistant response is not represented in this event fixture. Production obtains it exclusively from `--output-last-message` while `--output-schema` constrains it.
