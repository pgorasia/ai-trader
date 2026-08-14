You are performing one bounded, read-only unattended SHADOW security preflight for the AI trader.

Read and obey AGENTS.md. The active Robinhood MCP allow-list is the security authority. Inspect the Robinhood tools actually exposed to this run and return their exact tool names in `available_robinhood_tools`. Do not call any capability whose name indicates order review, order placement, cancellation, option exercise, scanner modification, or watchlist modification. Merely report forbidden capabilities if exposed.

Use read-only Robinhood calls to:

1. Establish whether the Robinhood MCP is available.
2. Count accounts marked as Agentic/agentic-allowed. Do not output any full account identifier or any non-Agentic account balance, position, order, or transaction.
3. Only if exactly one dedicated Agentic account is identified, reconcile its real equity positions, equity orders (including pending/open), account equity, and buying power.
4. Report unexpected real positions and orders using symbol/quantity or symbol/state/side only.

Do not scan markets. Do not use web search. Do not run an order review. Do not place, prepare, simulate, modify, or cancel any order. Do not modify a scanner, watchlist, Codex configuration, project file, report, or state file. Return only JSON conforming to the supplied schema. If any read fails or identity is ambiguous, record the error and fail closed in the JSON.
