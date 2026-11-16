# Daily key limits and organization budget summary

Goal: ship the supplied specification to the existing two-worker pool deployment before bot activation

Architecture: retain existing window accounting and monthly organization budget enforcement, extend policy validation to either 5h/7d or 1d/7d, and expose a membership-scoped read-only projection. Display that projection on API Keys using the dashboard session

Constraints: preserve pool configuration, credentials, state, client roles, counters and unchanged reset timestamps. No bot changes or schema migrations. No administrative credentials in browser

## Implementation plan

1. Add a dashboard card and behavioral UI tests for ordinary members, limits, MSK reset, errors, refresh and session isolation
2. Extend policy validation with regression tests for valid pairs, invalid numbers/duplicates and preserved synchronization; exercise existing actual window enforcement and organization enforcement
3. Add GET /organization/budget-summary and route permissions with explicit response fields and fresh membership query; test cross-user access, revoked membership, null/zero/overspend
4. Review write authorization for legacy/v2 organization, budgets, team metadata, key create/update and null fields; add regression tests for any defects
5. Build updated dashboard and compatible linux/amd64 runtime image, verify pinned dependencies and existing pool/native Messages/monthly features, publish immutable digest
6. Save configuration, PostgreSQL and Redis backups and validate restore independently; capture prior digest and client state fingerprint
7. Drain incoming traffic and active operations, replace only proxy, run client-session positive/negative acceptance, remove temporary data via supported APIs, preserve rollback and report evidence

Review focus: stale membership authorization, explicit null overrides, zero budget bypass, independent worker cache, session data leakage, policy resynchronization resetting unchanged windows

Acceptance ruling: the generic budget reset helper rounds 1d to UTC midnight, which conflicts with the requested 24-hour key interval. Use a dedicated key-window reset calculation for 1d, starting at creation plus 24 hours and advancing from the prior reset anchor. Keep other durations, including legacy 5h/7d and monthly organization schedules, unchanged
