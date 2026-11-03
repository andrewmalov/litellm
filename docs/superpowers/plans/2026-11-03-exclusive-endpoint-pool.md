# Exclusive endpoint pool implementation plan

> For agentic workers: execute the test-first increment inline using superpowers:executing-plans

**Goal:** Start implementation with a reproducible multi-client acceptance contract and runner

**Architecture:** Use the shared e2e HTTP transport through the proxy front door, with independent virtual keys and Claude Code-shaped metadata. Keep deterministic upstream faults in the integration suite, separate from real-provider acceptance

**Tech Stack:** Python, pytest, Pydantic, the existing e2e harness, Bash

**Spec:** `docs/superpowers/specs/2026-11-03-exclusive-endpoint-pool-design.md`

## Global constraints

The inactivity interval is 900 seconds. Redis is the source of truth. Rejected requests must not allocate or refresh bindings. Live tests use real upstreams. Emulated metadata does not establish compatibility with installed Claude clients

## Review focus

A success without a deployment ID cannot prove routing. Simultaneous starts need a barrier. Client secrets must stay out of reports. Tests need an isolated empty pool and must not run concurrently against that pool. Shared fallback is not an exclusive lease and cannot be exhausted merely by creating idle clients

### Task 1: Client driver

Files: `tests/e2e/router/exclusive_pool_client.py`, `tests/e2e_harness/router/test_exclusive_pool_client.py`

- [x] Write and run failing harness tests for serialized identities, missing and malformed metadata, deployment readback, structured errors and secret-safe representations
- [x] Implement frozen request models, an injected transport client and strict response assertions
- [x] Run the harness tests and the e2e typing gate

### Task 2: Live acceptance and runner

Files: `tests/e2e/router/test_exclusive_endpoint_pool_e2e.py`, `scripts/run_exclusive_endpoint_pool_e2e.sh`, `tests/e2e/router/exclusive_endpoint_pool.md`

- [x] Add opt-in acceptance for two ordered primary deployments and a shared fallback, invalid devices before allocation, stickiness across sessions and forwarded addresses, concurrent traffic and first-device races
- [x] Add a runner that requires explicit isolated-pool settings, disables reruns and xdist, and preserves JUnit evidence without secrets
- [x] Verify collection, driver behavior and shell validation, and distinguish unavailable product acceptance from harness failures
- [x] Commit and push the first increment

### Subsequent increments

Add a controlled upstream integration fixture with request-start/completion barriers and an independent overlap journal before implementing Redis allocation. Cover active streams beyond 900 seconds, cancellation, process death with upstream still executing, Redis outage and data loss, stale generations and fail-closed quarantine

Then implement atomic identity validation and allocation, lifecycle hooks, error classification, cooldown/probing and shared quota accounting, each preceded by its regression tests. Exercise Retry-After seconds and dates, group quotas, retries charged once to client RPM, fallback ramp-up and rejected-request inactivity boundaries

Finish with real-provider acceptance and installed Claude Code/Desktop UI checks. Confirm identity formats and provider execution guarantees before declaring production support

## First increment verification

The initial harness run failed because `router.exclusive_pool_client` did not exist. After implementation, all 20 focused driver tests passed. The metadata contract suite passed 53 tests, with existing unregistered-marker warnings under its standalone pytest configuration. Whole-tree e2e basedpyright reported zero errors, warnings or notes

The e2e scenario collects with the explicit opt-in and is deselected without it. Runner validation rejects a missing master key before any HTTP call. Live product acceptance was not run: no local proxy answered, and the exclusive pool module/configuration does not exist yet. This is unavailable acceptance, not a demonstrated product RED result

`make check` bootstrap was blocked first by the system uv version, then, using the existing compatible uv in `.venv`, by npm 11.9.0 being below the dashboard requirement 11.10.0. Its underlying `scripts/pre_commit_lint.sh` ran with the existing compatible uv and passed all checks selected for these changes. The complete gate log is `.git/pre_commit_lint.log`

Independent review found no critical or important defects in the first increment. The sampling documentation was clarified to distinguish missing a particular replica from missing either replica

## Core implementation

Use frozen domain models and pure state transitions, with Redis WATCH/MULTI compare-and-set as the atomic boundary. Keep all state for a pool in one persistent Redis key so Redis Cluster places it in one slot. Missing coordination state fails closed and requires explicit operator initialization after confirming no old upstream work exists

Implement identity validation before API conversion, round-robin allocation and sticky fallback, per-client and per-resource reservation accounting, generation-safe completion and heartbeat. Unconfirmed upstream termination remains quarantined until an operator confirms completion. Redis snapshots contain digests and accounting only, never prompts or credentials

Connect through CustomLogger proxy and deployment hooks, retaining standard authentication and Router checks. Reserve before selection, mark upstream start separately, and roll back unsent failures. Streaming completion requires a terminal finish event followed by normal iterator exhaustion; cancellation and ambiguous transport failure do not establish upstream termination

Tests cover pure transition boundaries first, then real Redis contention and callback-to-provider wire behavior. Automatic recovery and production client-format qualification remain gated on demonstrated lifecycle correctness


## Core verification and remaining qualification

The focused suite passed 66 tests: pure identity/state/recovery/request/admin tests, real Redis contention and five real proxy scenarios with PostgreSQL, Redis, two workers and a scripted upstream. The proxy scenarios exercise virtual-key limits, device binding and conflicts, sticky primary/fallback selection, stream completion and cancellation, confirmed upstream failure followed by another primary and an actual recovery generation, concurrent first-device requests and missing coordination state

Independent review found abandoned probes stuck in probing and unbounded multiple-choice output reservation. Regression tests reproduced both defects before fixes. Matching-generation probe cleanup now restores cooldown, multiple choices are rejected, prompt schemas are included in input reservation and probes reserve their own input. Follow-up review found no further important issues in the reviewed core

Canonical-environment basedpyright reports zero diagnostics for the new production modules. The full type gate compared with the preceding commit c491d3b35c65 and the strict Ruff gate compared with HEAD show no rule growth for this implementation. Type discipline and test-quality gates passed. The whole branch still exceeds the default-branch baseline for pre-existing reportArgumentType, reportPrivateUsage, reportUnknownArgumentType and RUF012 counts. This is a remaining branch check failure, not a passing default-branch gate

The direct pre-commit runner also verified ordinary Ruff, formatting, e2e typing, circular imports, import safety and OpenAPI/dashboard type synchronization. make check stops earlier during dashboard bootstrap because installed npm 11.9.0 is below the required 11.10.0. No caps, dependency requirements or gate thresholds were changed

The current adapter supports single-choice text Chat Completions and one upstream attempt per request. Native Messages/Responses, installed Claude Code/Desktop versions, real providers and Redis Cluster/failover remain unqualified. No real-provider credentials were available for live acceptance. The operational setup and explicit initialization/recovery commands are in ../exclusive-endpoint-pool.md
