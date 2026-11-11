# Organization monthly budget reset implementation plan

**Goal:** Let each organization choose a monthly reset day through the API, at midnight in Europe/Moscow

**Architecture:** Store a nullable `budget_reset_day` on the linked budget and keep the existing reset job, locks, counter resets and cache invalidation

**Contract:** An integer from 1 through 31 enables monthly resets and sets `budget_duration` to `1mo`. A missing day leaves the schedule unchanged. Sending a null day disables resets. Sending `budget_duration` without a day returns to the existing interval schedule. A short month uses its last day without changing the stored preferred day. Updating a schedule preserves spend

**Execution:** Implement inline using executing-plans and test-driven-development

## Task 1: Calendar calculation and API write behavior

- [x] Add failing tests to `tests/unit/proxy/common_utils/test_timezone_utils.py` for midnight boundaries, year rollover, leap February, and restoring day 31 after February
- [x] Add a pure monthly calculation and schedule-write helper in `litellm/proxy/common_utils/timezone_utils.py`, with an injected current time for tests
- [x] Run the mapped tests and confirm the new cases pass

## Task 2: Persistence and organization endpoints

- [x] Add failing API payload tests to `tests/unit/proxy/management_endpoints/test_organization_endpoints.py` covering set, clear, omitted fields, invalid days and preserving spend
- [x] Add a nullable schema column and a schema-only migration, and expose the validated field in budget and organization request models
- [x] Use the shared write helper in organization creation, both organization update APIs and budget update paths
- [x] Pass the stored day to the existing reset job so subsequent resets retain the schedule
- [x] Verify the mapped organization, budget and reset-job tests, run `make check`, review the diff

## Review focus

Ensure exact midnight selects the next month, short months do not move later resets permanently, unrelated patches preserve the schedule, clearing removes stale reset timestamps, and existing duration-only budgets keep their behavior
