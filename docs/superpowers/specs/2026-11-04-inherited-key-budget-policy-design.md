# Inherited key budget policies

Keep LiteLLM's existing key budget window accounting and reset schedules

Proxy administrators configure `metadata.key_budget_policy` on teams and organizations, containing `budget_limits` with exactly one `5h` and one `7d` window, each with a positive finite `max_budget`. These are individual key allowances, separate from aggregate team and organization budgets

A key with a team uses that team's policy. A key without a team uses its organization's policy. An absent policy preserves existing key behavior. Explicit policy removal preserves the last materialized key windows until an administrator changes them

Resolve policies on key creation and before inference budget checks. Existing keys receive changed allowances lazily, keeping their stored reset timestamps and duration-based spend counters. Persist effective windows so the existing reset job, spend accounting, reservations and key information endpoints agree. Changes propagate through existing entity cache invalidation and TTLs

Only proxy administrators can add, replace or remove policies, including through metadata replacement. Invalid policies fail closed. For configured policy keys, non-proxy callers may create keys and assign teams using existing access checks, but cannot supply budget settings, reset spend or clear/change an existing key's organization. Stored key windows retain this protection after moving to an unconfigured scope or removing an entity policy. Keys outside any policy and without stored windows keep their existing authorization rules. Globally assigned organization administrators additionally cannot supply budgets or reset spend even without a configured policy. Organization administrators must supply an organization or team when creating keys. Existing cross-organization access checks remain in force

No aggregate budget inheritance, new UI, new management API, schema migration, image publication or production deployment is included

Regression tests cover policy selection, malformed policies, independent key counters, live allowance changes, reset preservation, explicit null/empty bypasses, metadata replacement, and management endpoint wiring
