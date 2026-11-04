# Multiple models per exclusive upstream implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task by task

**Goal:** Register several models on one physical upstream without allowing different clients to own it simultaneously

**Architecture:** Keep deployment IDs for Router selection and operation history, and group deployments by `exclusive_upstream_id` for ownership, capacity and quarantine. Discovery accepts several model registrations per upstream with consistent role, quota scope and limits. A client can switch models on its assigned upstream, including during concurrent work, but cannot move to another upstream while work remains

**Tech Stack:** Python, Pydantic, Redis WATCH/MULTI, existing LiteLLM model API

**Spec:** User-approved behavior in the conversation, extending `docs/superpowers/specs/2026-11-03-exclusive-endpoint-pool-design.md`

## Constraints

Preserve static configuration fingerprints, Redis history and existing deployment IDs. No new management API or dependencies. At most one active physical fallback per pool. Model names stay unchanged

## Review focus

Model switches during concurrent operations must retain physical ownership. Removed model registrations must retain locks and accounting. A failed operation must not release a sibling's active ownership. Limits and quarantine must cover every model on a physical upstream. Distinct physical resources sharing quota must remain independently ownable

## Task 1: Discovery and admission

- [x] Extend `tests/unit/router_utils/exclusive_pool/test_discovery.py` with two upstreams and three models each, model switching, cross-client exclusion, shared limits, quarantine, removal, failure and fallback cases
- [x] Run the tests and confirm failure at the current uniqueness check
- [x] Update `models.py`, `discovery.py`, `state.py` and `recovery.py` in `litellm/router_utils/exclusive_pool/` to group ownership by upstream while preserving deployment selection
- [x] Run the entire pool unit suite and the relevant Redis/proxy integration scenarios

## Task 2: Registration instructions and verification

- [x] Extend `tests/integration/routing/test_exclusive_pool_proxy.py` to register three models per upstream through the standard API and verify routing with two workers
- [x] Document six registrations with unique deployment IDs and two shared upstream IDs in `docker/exclusive-pool/README.md`
- [x] Run scoped lint and type checks, mutation checks and independent code review
- [x] Commit and push the changes on the existing feature branch

## Verification record

112 focused unit and client-harness tests passed, as did 18 PostgreSQL/Redis/proxy integration tests. The AMD64 image accepted six registrations for two physical upstreams, preserved model names and ownership across model switches, rejected a third owner and enforced device binding. All 12 production pool modules matched the tested checkout

A controlled mutation disabling physical ownership was detected by the model-switch regression. Independent review found three health/recovery issues; all were fixed with regression tests and the final review had no remaining material findings

Scoped Ruff, format, type-discipline and basedpyright checks passed. Incremental Ruff, type-discipline and test-quality gates passed against the previous commit. Full `make check` and the global type gate could not run because installed uv 0.10.2 does not meet the repository minimum 0.10.9. No gate limits or dependencies were changed
