# Exclusive endpoint pool deployment

This stack targets linux/amd64 and runs two proxy workers, durable standalone Redis and PostgreSQL. It supports single-choice text Chat Completions and Anthropic Messages, including streaming, client tools, images, documents and account-bound server context. See [Anthropic Messages](anthropic-messages.md) for Claude Desktop configuration and content limits. Read the [pool runbook](../../docs/superpowers/exclusive-endpoint-pool.md) for routing guarantees and limitations

Run commands from this directory. Docker Compose v2 is required. Source builds need access to cgr.dev, packages.wolfi.dev, Docker Hub, GitHub and Python/npm registries. If Wolfi returns 403, check the US VPN before changing base images or dependencies

## Configuration

Create the environment without overwriting an existing file:

```bash
test ! -e .env && cp .env.example .env
chmod 600 .env
```

Fill every empty entry in `.env`. Generate independent master and salt keys with `openssl rand -hex 32`, prefixing each with `sk-`. Generate Redis and PostgreSQL passwords with `openssl rand -hex 32` too. Keep database passwords hexadecimal because the template embeds them in connection URLs without URL encoding. Preserve the salt key across restarts and backups

Upstreams are registered through the existing LiteLLM model API or Admin UI after startup. No upstream addresses or API keys belong in the Compose environment. `general_settings.store_model_in_db` is enabled in `proxy.yaml`; `pool.json` enables discovery for pool `exclusive`

The database supplies membership. `model_info.exclusive_pool` must exactly match `pool_id`; `exclusive_pool_role` must be `primary` or `fallback`. Public and provider model names remain independent of membership. Give interchangeable deployments the same public model name

Default resource limits come from `primary_limits` and `fallback_limits` in `pool.json`. The proxy binds to `127.0.0.1:4000` by default. Use an authenticated TLS reverse proxy for remote access. Neither Redis nor PostgreSQL publishes a host port. Redis uses AOF fsync on every write, no eviction and a 256 MB key-memory limit. Monitor disk space and write failures

## First start

Validate privately without rendering interpolated secrets. The configured image must contain discovery support:

```bash
docker compose config --quiet
docker compose pull proxy
docker compose up -d --wait redis postgres
```

Before initialization, independently confirm every resource that will join the pool has no outstanding requests. A new installation can initialize the empty pool before registering upstreams. Only after confirming upstreams are idle:

```bash
docker compose run --rm pool-admin init --confirm-upstream-idle
docker compose up -d --no-build --wait proxy
docker compose run --rm pool-admin status
curl --fail-with-body http://127.0.0.1:4000/health/liveliness
```

Normal startup never initializes pool state. `pool-admin` is an operator-only service behind the `ops` profile. An explicit `run pool-admin` enables it for that command. HTTP liveness does not prove pool readiness; inspect status and its `catalog` and `discovered_at` fields

## Register upstreams

In the Admin UI, add a model normally, expand advanced settings and put this JSON in **Model Info**:

```json
{
  "exclusive_pool": "exclusive",
  "exclusive_pool_role": "primary"
}
```

For the shared reserve, use role `fallback`. At most one active physical fallback is supported per pool, with several model registrations allowed on it. Primaries can serve without a fallback. An empty pool or a pool without a matching available resource fails closed

Automation uses the standard `/model/new` endpoint. This example needs `jq`. Set `MODEL_NAME` to the public name clients request, `PROVIDER_MODEL` to the provider-prefixed upstream model, `UPSTREAM_BASE_URL` to the complete API base path, `UPSTREAM_API_KEY` to its credential and `DEPLOYMENT_ID` to a unique stable ID. For an OpenAI-compatible upstream, the provider prefix is `openai/` and the base path usually includes `/v1`. Use the model your upstream supports, checking its current documentation

```bash
jq -n \
  --arg name "$MODEL_NAME" \
  --arg model "$PROVIDER_MODEL" \
  --arg url "$UPSTREAM_BASE_URL" \
  --arg key "$UPSTREAM_API_KEY" \
  --arg id "$DEPLOYMENT_ID" \
  '{model_name: $name,
    litellm_params: {model: $model, api_base: $url, api_key: $key, max_retries: 0},
    model_info: {id: $id, exclusive_pool: "exclusive", exclusive_pool_role: "primary"}}' \
  | curl --fail-with-body http://127.0.0.1:4000/model/new \
      -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
      -H 'Content-Type: application/json' --data-binary @-
```

Repeat with a new deployment ID for each model on each resource, using `exclusive_pool_role: "fallback"` for the reserve. These are administrative writes; clients must never receive the master key

### Several models on one upstream

Use `cr.selcloud.ru/ttkdc/litellm:exclusive-pool-multimodel-20261104` or a newer release containing this feature. Register each supported model separately with a unique `model_info.id`, and use the same `exclusive_upstream_id` for every model on that physical upstream. Keep the public and provider model names unchanged. For two upstreams serving `claude-opus-5-5`, `claude-sonnet-5-5` and `claude-haiku-5-5`, create six records with two physical upstream IDs

For an OpenAI-compatible upstream, this shell function registers the three models through the existing API:

```bash
register_upstream() {
  local upstream_id="$1" base_url="$2" api_key="$3" role="${4:-primary}"
  local model
  for model in claude-opus-5-5 claude-sonnet-5-5 claude-haiku-5-5; do
    jq -n \
      --arg upstream "$upstream_id" --arg url "$base_url" \
      --arg key "$api_key" --arg role "$role" --arg model "$model" \
      '{model_name: $model,
        litellm_params: {model: ("anthropic/" + $model), api_base: ($url | sub("/v1/?$"; "")), api_key: $key, max_retries: 0},
        model_info: {id: ($upstream + "-" + $model), exclusive_pool: "exclusive",
          exclusive_pool_role: $role, exclusive_upstream_id: $upstream}}' \
      | curl --fail-with-body http://127.0.0.1:4000/model/new \
          -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
          -H 'Content-Type: application/json' --data-binary @- || return
  done
}

register_upstream upstream-a "$UPSTREAM_A_BASE_URL" "$UPSTREAM_A_API_KEY"
register_upstream upstream-b "$UPSTREAM_B_BASE_URL" "$UPSTREAM_B_API_KEY"
```

Supply the two API base URLs and credentials in the shell running registration, without adding them to Compose. The function assumes the pool ID is `exclusive`. Registrations use the native `anthropic/` provider. Supply the API root; the function also accepts and removes a trailing `/v1`. Public model names stay unchanged

A client assigned upstream-a can request any of its registered models and stays on upstream-a, including with concurrent requests. Another client receives upstream-b. With two primaries and no fallback, a third client receives `endpoint_pool_exhausted`. All model registrations on one upstream share parallel, RPM and TPM limits and quarantine. If the assigned healthy upstream lacks the requested model, the request fails with `endpoint_pool_exhausted` until the session expires or normal failover becomes possible

Model registrations on the same physical upstream must have the same role, effective quota scope and limits. Set a common `exclusive_quota_scope` only when distinct physical upstreams also share an account quota. That shared quota does not merge their exclusive ownership

To upgrade an existing discovery deployment, stop incoming traffic, drain operations and replace all proxy workers before adding model aliases. Preserve Redis and PostgreSQL volumes and the existing `pool.json`; initialization and reconfiguration are unnecessary for this code upgrade. Do not mix old and new workers with multi-model registrations. For an existing registration, reuse its current `exclusive_upstream_id`, which defaults to its deployment ID, for the new sibling models

Create client virtual keys through `/key/generate`. Claude Desktop requests the registered public model name at `/v1/messages?beta=true`, authenticating with their virtual key. Device metadata is optional; changed or missing `device_id` values preserve the key assignment and shared limits. See the [multi-client runner](../../tests/e2e/router/exclusive_endpoint_pool.md) for provider acceptance. Scripted upstream tests do not qualify real providers or installed Claude clients

The catalog refreshes every `heartbeat_seconds`, 10 seconds by default. Worker-local Router refreshes can add further delay before every worker can use a new deployment. Admission requires a deployment to exist in both the shared catalog and the worker's eligible list. Adding capacity preserves existing client assignments. Unmarked deployments sharing the model name cannot be selected by the pool

Optional `model_info.exclusive_pool_limits` overrides resource `parallel`, `rpm` and `tpm`. `exclusive_upstream_id` identifies the physical resource and defaults to the deployment ID. Every model on the same physical resource must use the same upstream ID. Distinct upstream IDs must represent independently ownable physical resources. `exclusive_quota_scope` can group distinct resources sharing an account quota, with identical limits in that scope

Only DB-backed deployments participate in discovery. Static YAML configurations remain supported with `discovery: false`

## Operations

```bash
docker compose logs --tail 100 proxy
docker compose run --rm pool-admin status
docker compose run --rm pool-admin confirm --operation OPERATION_ID --confirm-upstream-idle
docker compose run --rm pool-admin enable --resource DEPLOYMENT_ID
```

Normal model additions, removals and blocking use the standard API/UI without restarting. Deleted or blocked resources stop receiving new admissions after discovery. Their operations, uncertain completions, health and quota charges remain in Redis. Re-adding the same ID preserves that history. Retired deployments remain protected against direct-ID calls

Retired catalog records are retained for safety. For a registered ID, the public model name, role, upstream identity, quota scope and limits are immutable in discovery. Restore the original metadata if an edit prevents synchronization. Keys can rotate without changing those fields. Drain all work and verify the upstream is idle before repointing a deployment at another physical resource

Discovery reads the writer database. A failed DB read or malformed pool metadata does not become an empty catalog. Protected requests fail closed when required synchronization fails. No new admissions or probes occur once the catalog is older than `heartbeat_timeout`. Unrelated models continue serving independently

Use `confirm` only after verifying the named quarantined request ended upstream. Timeout, disconnect, proxy shutdown and lost heartbeat do not prove completion. A healthy operation cannot be confirmed away

Changing pool policy in `pool.json` requires draining operations, stopping the proxy, running `pool-admin reconfigure` and recreating workers. Reconfiguration rejects active operations. Stop the stack with `docker compose down`, preserving both volumes. Never use `down -v` on a deployment

Back up both volumes and preserve `.env` securely. After Redis data loss or restoration of a stale backup, stop traffic and independently verify the end of all old upstream work before restoring trusted state or initializing again. PostgreSQL cannot reconstruct Redis ownership

## Upgrade the static template

This migration supports the original template with one public model name. Preserve the old `pool.json` as `pool.previous.json` and keep a backup of both volumes. Upgrade the image while retaining the old configuration, stop incoming traffic and wait for operations to finish. Resolve quarantine only after independently confirming upstream completion

Register the existing resources through `/model/new`, preserving their deployment IDs and public model name. Include pool labels, `exclusive_upstream_id` and `exclusive_pool_limits` matching each old resource. Set `exclusive_quota_scope` too if it was configured

Stop proxy workers. Replace `proxy.yaml`, `pool.json` and Compose with the discovery templates. Preserve `.env` infrastructure secrets and remove its old upstream entries. With the new image configured, migrate state by mounting the preserved configuration:

```bash
docker compose run --rm \
  -v "$PWD/pool.previous.json:/etc/litellm/pool.previous.json:ro" \
  pool-admin reconfigure --previous-config /etc/litellm/pool.previous.json
docker compose up -d --no-build --wait proxy
docker compose run --rm pool-admin status
```

Keep `pool_id` unchanged. Migration checks the old configuration fingerprint and imports resources as retired catalog records. Discovery activates matching DB registrations while preserving sessions, health and accounting. Verify all expected registrations and health before resuming traffic. Do not run `init` or delete Redis state during migration

## Build from source

The canonical build uses the repository's root Dockerfile:

```bash
docker compose build proxy
```

Release Rust compilation needs substantially more memory than proxy runtime. An 8 GB Docker VM failed locally during compilation; prefer a native x86 builder with sufficient memory

For the monthly budget release, `Dockerfile.runtime` reuses the trusted linux/amd64 exclusive-pool runtime pinned in its `BASE_IMAGE` argument. It preserves dependencies, Rust, enterprise code and the UI, copies the updated Python modules and budget migration, and regenerates Prisma from the updated schema. For future changes, recheck the full production diff before reusing this method

Supply that compatible base by digest and record the source revision:

```bash
docker build --platform linux/amd64 -f Dockerfile.runtime \
  --build-arg BASE_IMAGE="$TRUSTED_BASE_IMAGE_DIGEST" \
  --build-arg SOURCE_REVISION="$(git -C ../.. rev-parse HEAD)" \
  -t cr.selcloud.ru/ttkdc/litellm:exclusive-pool-monthly-budget ../..
```

The base must use Python 3.13 and `/app/.venv`. Do not substitute an unrelated LiteLLM release. Publish the image and transfer this directory without `.env` to another host, transferring secrets separately. Set `LITELLM_IMAGE` to the published digest, then follow first-start or migration instructions. `--no-build` leaves build options unused

## Individual key budget policies

See [key-budget-policies.md](key-budget-policies.md) for administrator-owned `5h` and `7d` allowances inherited from teams and organizations

## Monthly organization budgets

Use `cr.selcloud.ru/ttkdc/litellm:exclusive-pool-monthly-budget-20261113` for the combined pool and monthly budget release. It includes the `exclusive_pool.configured.callback` module, the new budget schema and the Prisma client. The standalone `org-monthly-budget-d766904765` image does not contain the pool module and cannot load this deployment's callback configuration

Set an organization's `budget_reset_day` through the [organization API](../../docs/organization-monthly-budget.md). Existing schedules and pool configuration are preserved. Apply the included schema migration through the normal migration service before starting proxy workers

Local verification used this directory's proxy and pool configuration with isolated PostgreSQL and Redis. The proxy started and `/health/liveliness` returned HTTP 200. Budget creation, schedule changes and disabling resets succeeded through `/budget/new` and `/budget/update`; invalid days returned 422 and unauthenticated requests returned 401. Organization HTTP writes require an Enterprise license, so their behavior was verified by the organization endpoint tests
