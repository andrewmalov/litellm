# Organization budget summary and daily key limits

An authenticated dashboard user can call `GET /organization/budget-summary` with their own session bearer token. The server resolves membership from that session's user ID on every call. Query parameters cannot select another user or expand membership. No organization membership returns `{"organizations": []}`

Each organization contains only `organization_id`, `organization_alias`, `max_budget`, `spend`, `remaining_budget`, `budget_duration`, `budget_reset_day` and `budget_reset_at`. Spend is the organization's persisted aggregate; limits and reset schedule come from its saved budget. Remaining is clamped to zero, while actual overspend remains visible. A null maximum and remaining mean unlimited. A zero maximum blocks inference. A missing reset timestamp is returned as null

API Keys displays this read-only summary for ordinary organization members using their dashboard session. The card refreshes on focus and every 60 seconds, formats the server timestamp in Europe/Moscow, and displays loading and API failures separately from balances

A proxy administrator can configure team metadata when creating or updating a team:

```json
{"key_budget_policy":{"budget_limits":[{"budget_duration":"1d","max_budget":30},{"budget_duration":"7d","max_budget":150}]}}
```

Put this object in the team's `metadata` field. Exactly one short window, either `5h` or `1d`, and one `7d` window are required. Budgets must be positive finite JSON numbers. `1d` lasts 24 hours, not until calendar midnight. Both limits apply per key alongside the unchanged organization budget

New keys inherit these windows through `/key/generate`. Request-time synchronization preserves existing matching reset timestamps and spend counters. Clients, including team administrators, cannot modify or remove the policy or override its budgets through key creation/update. Existing `5h`/`7d` clients require no migration

The separate monthly organization reset API is described in [organization-monthly-budget.md](organization-monthly-budget.md)
