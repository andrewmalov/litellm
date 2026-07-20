# Sync Upstream LiteLLM to v1.93 (preserve local patches) Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the local LiteLLM fork in `/Users/amalov/litellm` in sync with upstream `v1.93.0` while preserving the 7 local commits on top of `v1.83.7-stable` and every tracked/untracked local change.

**Architecture:**
- Detached `HEAD` is currently at `dcb4a6868e` (7 commits on top of `v1.83.7-stable` from upstream).
- Upstream tag found via `git ls-remote --tags origin 'v1.93*'`: `v1.93.0` (`052b5a2169d8d3082e1d66e69f200a72b0c1e274`) — release commit, no patch tags beyond it (use `v1.93.0` if no higher stable exists; will verify before rebasing).
- Strategy: create a backup branch → push the 7 local commits onto `origin/main` (fast-forwardable from upstream `v1.93.0` only if upstream is reachable, otherwise rebase the 7 local commits onto `v1.93.0`) → restore stash of any uncommitted work.

**Tech Stack:** Git 2.x, LiteLLM upstream remote (`origin`).

---

## Pre-flight diagnostics

Run these first and capture the output verbatim — they justify the rest of the plan.

- [ ] **Step 1: Capture current state**

```bash
git status --short --branch
git rev-parse --short HEAD
git log --oneline v1.83.7-stable..HEAD
git ls-remote --tags origin 'v1.93*'
```

Expected: detached HEAD on `dcb4a6868e`, 7 commits ahead of `v1.83.7-stable`, upstream tag list shown.

- [ ] **Step 2: Verify `v1.93.0` exists locally and fetch**

```bash
git fetch origin --tags
git rev-parse v1.93.0^{commit}
```

Expected: command prints a 40-char SHA; abort plan if `v1.93.0` is missing.

---

## Task 1: Create reversible backups

**Files:**
- Modify: ref-only (`refs/heads/sync-backup-1.93`, `refs/stash`)

- [ ] **Step 1: Branch off current HEAD**

```bash
git branch sync-backup-pre-rebase HEAD
```

Expected: branch created; `git branch -v` shows `sync-backup-pre-rebase dcb4a6868e`.

- [ ] **Step 2: Stash uncommitted work**

```bash
git stash push -u -m "sync-1.93-pre-$(date +%s)" -- Dockerfile .specify PREMIUM.md docker/build-and-push.sh
```

Expected: `Saved working directory and index state on dcb4a6868e: stash@{0} …`; `git status` clean (other than untracked files already in the stash).

If stash refuses to capture a path, run `git stash push --include-untracked -m "sync-1.93-pre" -- .` to capture everything and proceed.

- [ ] **Step 3: Verify nothing uncommitted remains on disk**

```bash
git status --porcelain
git stash list
```

Expected: empty porcelain output; `stash@{0}` recorded.

---

## Task 2: Align with upstream v1.93.0

**Files:**
- Modify: `.git/HEAD`, `refs/heads/sync-1.93`

- [ ] **Step 1: Create update branch from upstream tag**

```bash
git switch -c sync-1.93 v1.93.0
```

Expected: `git status` on branch `sync-1.93`; `git rev-parse --short HEAD` = `052b5a21` (or whatever `v1.93.0` resolves to).

- [ ] **Step 2: Replay the 7 local commits on top**

```bash
git cherry-pick v1.83.7-stable..sync-backup-pre-rebase
```

Expected: either clean replay (no output, exit 0) or conflict markers — record any conflicts and resolve before continuing. If upstream `v1.93.0` history already contains the upstream parts of the 7 commits, `git rebase --onto v1.93.0 sync-backup-pre-rebase` is an acceptable equivalent (the cherry-pick form is preferred to keep authorship metadata).

- [ ] **Step 3: Sanity-check resulting history**

```bash
git log --oneline v1.93.0..sync-1.93
git log --oneline -3
```

Expected: exactly 7 commits listed on top of upstream; tip message matches `feat: add Yandex and TTK to UI provider dropdown`.

---

## Task 3: Restore local untracked/tracked edits

**Files:**
- Restore: `Dockerfile`, `.specify/`, `PREMIUM.md`, `docker/build-and-push.sh`

- [ ] **Step 1: Drop the reconstruction commit on top of the cherry-pick chain**

If the cherry-pick applied cleanly with no other diffs, no extra commit is needed; proceed to Step 2.

- [ ] **Step 2: Restore stash on top of new history**

```bash
git stash pop -- 3>&1 1>&2 2>&3 || true
```

Expected: working tree now contains `Dockerfile` modifications plus `.specify/`, `PREMIUM.md`, `docker/build-and-push.sh`. If `stash pop` conflicts, resolve them and commit as a follow-up; do not `--force-drop` the stash.

If `stash pop` reports conflicts, resolve manually, then:

```bash
git add Dockerfile .specify PREMIUM.md docker/build-and-push.sh
git commit -m "chore: restore uncommitted pre-sync edits on v1.93.0"
```

- [ ] **Step 3: Verify final tree**

```bash
git status --porcelain
git rev-parse --short HEAD
git log --oneline -1
git diff --stat sync-backup-pre-rebase..HEAD
```

Expected: clean working tree (after the restoration commit), `HEAD` on `sync-1.93` with restored changes committed; diffstat non-empty because of v1.93.0 jump.

---

## Task 4: Decide where to land the synchronized branch

The current branch was detached. Two safe outcomes:

- [ ] **Step 1 (recommended): Update working branch `litellm_internal_staging` to point at the new tip**

```bash
git branch -f litellm_internal_staging sync-1.93
git switch litellm_internal_staging
```

Expected: `git status` on `litellm_internal_staging`; `git rev-parse --short HEAD` matches the post-rebase tip.

- [ ] **Step 2: Leave `sync-backup-pre-rebase` for rollback**

```bash
git rev-parse --short sync-backup-pre-rebase
```

Expected: `dcb4a6868e`. To roll back: `git reset --hard sync-backup-pre-rebase` and `git stash pop`.

---

## Task 5: Verify LiteLLM still imports

- [ ] **Step 1: Confirm Python module loads**

```bash
python -c "import litellm; print(litellm.__version__)"
```

Expected: prints a version string beginning with `1.93.0`. If this fails, run `poetry install` first and retry.

- [ ] **Step 2: Quick lint sanity check**

```bash
poetry run ruff check litellm/litellm_core_utils/custom_logger_registry.py litellm/proxy/_types.py litellm/proxy/proxy_server.py 2>&1 | tail -20
```

Expected: no `SyntaxError`-grade errors. Other lint warnings are acceptable and recorded.

---

## Task 6: Report outcome

Append a summary under `docs/superpowers/plans/2026-07-20-sync-upstream-1.93-result.md` capturing:

- Starting `HEAD` (`dcb4a6868e`) and ending `HEAD` SHA
- Upstream ref used (`v1.93.0`)
- Conflicts encountered (or "none")
- Test/lint command outputs
- Manual follow-ups (e.g. apply patch tags beyond `v1.93.0` if they appear)
