# Inherited key budget policies implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task

**Goal:** Enforce administrator-owned individual 5h and 7d allowances through team and organization policies

**Architecture:** Typed policy parsing and permission helpers live in one shared module. Existing key creation and inference paths materialize inherited windows without changing counter identity or reset schedules

**Tech Stack:** Python, Pydantic, Prisma, existing Redis spend accounting

**Spec:** `docs/superpowers/specs/2026-11-04-inherited-key-budget-policy-design.md`

## Global constraints

Keep existing window semantics and aggregate budgets. Preserve unrelated working tree changes. No new dependencies, migrations or deployment. Commit and push on the existing branch

## Review focus

Metadata replacement must not remove protected policies. Null or empty budget updates must not bypass delegation. Team moves must retain counter identities and reset timestamps. Existing keys must receive policy updates before checking budgets. Spend accounting must read the same materialized windows as admission checks

## Task 1: Policy resolution and authority

- [x] Add failing behavior tests in `tests/unit/proxy/management_helpers/test_key_budget_policy.py` for typed parsing, policy selection, metadata authority and delegated budget edits
- [x] Run the tests and confirm the missing behavior fails
- [x] Implement typed policy resolution and permission helpers in `litellm/proxy/management_helpers/key_budget_policy.py`
- [x] Run the tests and scoped lint/type checks

## Task 2: Wire management and inference

- [x] Add failing management and authentication regressions in the existing mapped tests
- [x] Apply authority checks to team/organization creation and both organization update surfaces, key generation, update, regeneration and spend reset
- [x] Apply inheritance on generation and inference, persisting changed windows with existing reset timestamps and counters
- [x] Run the focused management, authentication and spend accounting suites

## Task 3: Verify and document

- [x] Document API configuration, individual budget behavior, migration and cache propagation
- [x] Run required repository checks and record any unavailable checks
- [x] Obtain an independent review, fix actionable findings with regressions, commit and push

## Execution record

User authorized implementation after selecting existing LiteLLM window mechanics. Execution continues inline without a further approval round


Verification uses the existing virtual environment with `TZ=UTC`, because an existing expiry test relies on naive local time. The final focused run passed 1669 tests with 2 baseline tests deselected. The suites cover key, team and organization management, authentication, multi-window accounting and the new policy helper

Two existing custom-auth source-inspection tests are excluded: `test_custom_auth_also_skips_budget_checks_for_zero_cost_models` and `test_custom_auth_attaches_the_user_budget_even_when_it_does_not_enforce`. Their assumptions already disagree with the implementation at the starting commit; the inspected function is unchanged by this work

Ruff strict, type discipline and test quality gates passed against starting commit `d3f9ff2cca9c0f48aa966d73a3766e4e211c6f71`. Scoped Basedpyright passed for the new helper; changed production lines introduced no diagnostics in the legacy files. Full `make check` and the repository type gate could not provision their environment: installed uv is 0.10.2, while the repository requires at least 0.10.9

Live checks used a disposable local PostgreSQL container and the source proxy. Curl requests created a policy team, issued a key, generated a synthetic response, changed policy allowances and read the updated key windows with unchanged reset timestamps. A real Prisma persistence check covered JSON-array reads and conditional updates. No real-provider generation or production deployment was performed. The owned proxy and database container were stopped and removed

Independent review found and fixed cache TTL extension, membership-role handling, retained-window reset and rotation bypasses, automatic rotation through the system account, and policy resolution for default-assigned teams. Regression tests reproduce the rotation and default-team findings before their fixes. Final review reported no blockers
