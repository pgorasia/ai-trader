Perform the bounded SHADOW portfolio stage in this exact sequence:

1. Call `get_accounts` exactly once and wait for it to complete.
2. Identify the one account whose `agentic_allowed` value is the JSON boolean true. If there is not exactly one, do not guess and return a failed result.
3. Copy that account's `account_number` only into the required `account_number` argument of `get_portfolio`, call `get_portfolio` exactly once, and wait for it to complete before producing the final JSON. Passing the Agentic account number between these two read-only tools is required; omitting the portfolio call is not a valid fallback.

Return one `account_classifications` item per account, preserving response order, with only the six schema fields. Copy native values; do not calculate identity counts. Python makes and validates the identity decision. Derive `account_equity`, `buying_power`, and `portfolio_status` only from the completed `get_portfolio` result. Never include an account number or other identifier in the final JSON, and never return balances from `get_accounts`, URLs, raw responses, tokens, or credentials. No other tool, web, scanning, or writes.
