#!/usr/bin/env bash
set -euo pipefail

: "${LITELLM_MASTER_KEY:?Set the isolated proxy master key}"
: "${E2E_EXCLUSIVE_POOL_MODEL:?Set the isolated pool model alias}"
: "${E2E_EXCLUSIVE_POOL_PRIMARY_1:?Set the first primary deployment ID}"
: "${E2E_EXCLUSIVE_POOL_PRIMARY_2:?Set the second primary deployment ID}"
: "${E2E_EXCLUSIVE_POOL_FALLBACK:?Set the fallback deployment ID}"

pool_repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$pool_repo_root"
export E2E_EXCLUSIVE_POOL=1
export E2E_FIXTURE_MODE=live
export PYTHONUNBUFFERED=1
pool_report_dir="$(git rev-parse --git-path exclusive-pool-e2e)"
mkdir -p "$pool_report_dir"
uv run pytest tests/e2e/router/test_exclusive_endpoint_pool_e2e.py \
    -n 0 --reruns 0 --tb=short -v \
    --junitxml="$pool_report_dir/results.xml" \
    2>&1 | tee "$pool_report_dir/run.log"
