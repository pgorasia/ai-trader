Perform the bounded SHADOW orders stage in this exact sequence:

1. Call `get_accounts` exactly once and wait for it to complete.
2. Identify the one account whose `agentic_allowed` value is the JSON boolean true. If there is not exactly one, do not guess and return a failed result.
3. Copy that account's `account_number` only into the required `account_number` argument of `get_equity_orders`, call `get_equity_orders` exactly once, and wait for it to complete before producing the final JSON. Passing the Agentic account number between these two read-only tools is required; omitting the orders call is not a valid fallback.

Return one `account_classifications` item per account, preserving response order, with only the six schema fields. Copy native values; do not calculate identity counts. Python makes and validates the identity decision. Return only counts and unexpected orders as symbol/state/side. Never include an account number or other identifier in the final JSON, and never return balances, URLs, raw responses, tokens, or credentials. No other tool, web, scanning, or writes.
