# Sync Upstream LiteLLM to v1.93 — Result (2026-07-20)

## Goal recap
Bring local LiteLLM fork in `/Users/amalov/litellm` to upstream `v1.93.0` while preserving 7 local commits on top of `v1.83.7-stable` plus tracked/uncommitted local files.

## Starting state
- Detached `HEAD` at `dcb4a6868e`.
- `origin` remote only (`https://github.com/BerriAI/litellm.git`).
- 7 local commits (added on top of `v1.83.7-stable`): TTK deployment, caddy fix, DATABASE_URL placeholder fix, dup-enum fix, Yandex/TTK providers, UI provider dropdown, and the `LITELLM_DISABLE_EXTERNAL_INTEGRATIONS` flag.
- Uncommitted work: `Dockerfile` (private registry ENV/ARG block), `.specify/`, `PREMIUM.md`, `docker/build-and-push.sh`.

## Steps performed
1. Captured baseline (branch/HEAD/status) and confirmed upstream tag list:
   - `v1.93.0` SHA `052b5a2169d8d3082e1d66e69f200a72b0c1e274` is the latest stable.
2. Created backup branch `sync-backup-pre-rebase` pointing at pre-sync HEAD `dcb4a6868e`.
3. Stashed uncommitted edits as `stash@{0}` (`git stash push -u`, capture included untracked `.specify/`, `PREMIUM.md`, `docker/build-and-push.sh` and modified `Dockerfile`).
4. `git fetch origin tag v1.93.0` and `git checkout -b sync-1.93 v1.93.0`.
5. Replayed the 7 local commits via `git cherry-pick v1.83.7-stable..sync-backup-pre-rebase`. Resolutions:
   - `litellm/proxy/_types.py` (`2dcc9f680a`): kept upstream `external_integrations_disabled` text.
   - `litellm/litellm_core_utils/get_llm_provider_logic.py` (`84cc463508`): kept upstream `pinstripes`/`meta` branches and appended new `ttk` branch after them.
   - `litellm/types/utils.py` (`84cc463508`): kept upstream `GDC` and added `YANDEX`/`TTK`.
   - `litellm/llms/openai_like/providers.json` (`84cc463508`): union of upstream 10 providers and our `ttk`. JSON validates.
   - `ui/litellm-dashboard/src/components/provider_info_helpers.tsx` (`84cc463508`, three blocks): kept upstream `ZAI` enum entry and `zai/glm-4.5` default-model arm, added `Yandex`/`TTK`.
   - `litellm/litellm_core_utils/custom_logger_registry.py` (`a0ba566046`): merged by taking the **full** upstream v1.93.0 callback registry and wrapping it with the `_should_disable_external_integrations()` guard from our patch (added `azure_sentinel`, `mavvrik`, `newrelic` that exist only on the upstream side).
   - `litellm/proxy/common_utils/callback_utils.py` (`a0ba566046`): merged both sides — kept `copy` + upstream typing imports plus our `os` import; kept our `LITELLM_DISABLE_EXTERNAL_INTEGRATIONS` early-return inside `initialize_callbacks_on_proxy`.
   - `litellm/proxy/proxy_server.py` (`a0ba566046`): kept upstream single-line `raise HTTPException(...)` and prepended our external-integrations guard + `import base64`.
   - `Dockerfile` (stash pop): our `ARG REGISTRY=…` and `ENV DOCKER_REGISTRY=${REGISTRY}` blocks are present above `apk add supervisor` while upstream base/base-image ARG and builder/runtime structure from v1.93.0 are intact.
6. Final commit `chore: restore pre-sync edits on v1.93.0` (3d47de651a) materializes the stash contents on top.
7. Reset `litellm_internal_staging` to `sync-1.93` (new tip `3d47de651a`).

## Resulting history (v1.93.0..HEAD)

```
* 3d47de651a chore: restore pre-sync edits on v1.93.0
* 1babfbdb46 feat: add Yandex and TTK to UI provider dropdown
* f54052154e feat: add LITELLM_DISABLE_EXTERNAL_INTEGRATIONS flag to disable external integrations
* 065a1e4436 feat: add Yandex Cloud and TTK LLM providers
* 651fe4f5b2 fix: remove duplicate external integrations enum value
* 80ac75e2d3 fix: use POSTGRES_PASSWORD env var in DATABASE_URL instead of placeholder
* d956ccc4f9 fix: use caddy:3 instead of non-existent caddy:3-alpine tag
* b6ea2ca56f feat: add TTK deployment config with enterprise license support
```

## Validation results
- `python json.load(litellm/llms/openai_like/providers.json)` → OK, includes upstream providers (`aihubmix`, `crusoe`, `darkbloom`, `empiriolabs`, `libertai`, `meta`, `neosantara`, `parasail`, `pinstripes`, `tensormesh`) plus local `ttk`.
- `ast.parse` passes for `_types.py`, `types/utils.py`, `custom_logger_registry.py`, `get_llm_provider_logic.py`, `callback_utils.py`.
- `LlmProviders.YANDEX`, `LlmProviders.TTK`, `LlmProviders.GDC` all resolvable.
- `Dockerfile` retains v1.93.0 base/runtime ARGs and adds our private registry ENV/ARG block.
- `git status --short`: only untracked `docs/` (the plan + plan-result files added by this session).

## Rollback path
- `git switch sync-backup-pre-rebase` returns the repository to its pre-sync state (`dcb4a6868e`).
- `git branch -f litellm_internal_staging sync-backup-pre-rebase` restores the original `litellm_internal_staging` pointer.

## Follow-ups
- Push `litellm_internal_staging` to `origin` only after manual review of the conflict resolutions above.
- Re-run `make pre-commit` once the working tree aligns with the new history (Dockerfile + UI patches ready, no staged changes).
- If a `v1.93.0.patch.N` tag appears later, fetch and cherry-pick it on top of `3d47de651a` using the same recipe.
