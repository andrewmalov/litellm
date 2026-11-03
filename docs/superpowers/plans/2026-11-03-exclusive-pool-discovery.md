# Exclusive pool discovery implementation plan

**Goal:** Discover DB-backed upstreams through existing model management API/UI using `model_info.exclusive_pool` and `exclusive_pool_role`, independently of model names

**Architecture:** Keep static pools compatible. Dynamic pools read metadata from the writer database within a Redis WATCH transaction, publish a shared catalog and resolve routing policy from each transaction's snapshot. Retain removed resources as draining records so active operations, quarantine, health and quota history survive catalog changes

**Tech stack:** Pydantic, existing Prisma writer connection, Redis WATCH/MULTI, existing CustomLogger hooks

**Spec:** Existing exclusive endpoint pool design plus the approved metadata discovery approach in this conversation

## Constraints

Use existing model API/UI, no separate API or schema migration. Never store credentials in Redis. Discovery failures must not become an empty catalog. Missing Redis state still requires explicit initialization. Standard model names remain unchanged. Static pools remain supported

## Review focus

Stale workers cannot overwrite newer discovery results: watch Redis before reading the writer, reload after a catalog-generation conflict and reuse the DB result after state-only conflicts
Removed resources cannot receive new admissions or recovery probes, but completion and quarantine remain tracked
Re-adding an ID cannot reset health or quota history; changes of resource identity or role are rejected
Multiple model names share membership without sending a request to an incompatible model deployment
Unmarked deployments sharing a model name cannot bypass exclusivity

## Tasks

- [x] Write failing discovery transition tests for filtering, roles, malformed metadata, addition during active work, removal, quarantine and re-addition
- [x] Implement frozen catalog records, optional dynamic configuration and snapshot-derived routing configuration
- [x] Add failing Redis tests for cross-worker discovery, stale reads, source failure and state loss; implement async watched reconciliation
- [x] Integrate DB metadata discovery with callback and recovery lifecycle, enforce marked deployment protection and matching request eligibility
- [x] Verify live standard model API registration and routing with PostgreSQL, Redis and scripted upstreams
- [x] Update Compose and instructions to start with an empty DB-backed pool and register deployments through existing API/UI
- [x] Run focused tests, lint/type checks and independent review, fix findings, commit and push

## Verification

Pure state and request tests plus real Redis integration pass 67 tests. The real proxy suite passes nine scenarios with PostgreSQL, Redis, two workers and a scripted upstream, including standard API addition/removal, sticky assignment, direct-ID protection and unrelated-model availability after pool state loss. Existing client harness and identity checks pass 31 tests

Regression tests reproduced nullable metadata, forged deployment metadata, static migration loss and discovery starvation before their fixes. A deliberate mutation removing the catalog-generation guard fails the stale-worker Redis test; restoring the guard passes

Focused basedpyright reports zero diagnostics. Ruff, formatting and type discipline checks pass. Incremental strict Ruff, type-discipline and test-quality gates pass against the preceding commit. `make check` and the full type gate cannot provision their environment because installed uv 0.10.2 is below the repository requirement 0.10.9. No dependency requirements or gates were weakened

Independent code review found no remaining critical or important issues after two rounds of fixes. Real provider qualification remains outside this scripted-upstream validation

The linux/amd64 runtime-layer image also passed a live container check with the deployment configuration: standard model API registration, addition under traffic, removal to fallback, device conflict and preservation of all three assignments after proxy restart. This build reuses the trusted compatible native runtime; no full Rust source build is claimed
