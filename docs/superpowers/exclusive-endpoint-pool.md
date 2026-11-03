# Exclusive endpoint pool

The opt-in core implements exclusive primary assignments, shared fallback, device binding, distributed RPM/TPM and in-flight limits, and coordinated recovery. The supported inference surface is currently text `/v1/chat/completions`, including streaming and text tool conversations with a single output choice. Other surfaces, images, provider-held conversation state and request-level upstream overrides are rejected before allocation

Claude Code-shaped JSON in `metadata.user_id` is accepted as an emulated format. This does not establish support for installed Claude Code or Desktop versions or their native Messages/Responses routes. An explicitly configured device header is also available. IP addresses are never used for ownership

## Configuration

For a source-built Docker image and a Compose stack with persistent Redis, PostgreSQL and an explicit operator initialization command, use the [deployment template](../../docker/exclusive-pool/README.md)

Provision separate upstream resources and configure every deployment in the same public model group. Deployment IDs must match the pool configuration. A physical upstream must appear only once, including outside the pool. Declare the same `quota_scope` and identical limits for deployments sharing an account quota

Save the pool configuration as `exclusive-pool.json`:

```json
{
  "pool_id": "exclusive",
  "models": ["exclusive-pool"],
  "primaries": [
    {"id": "primary-a", "upstream_id": "resource-a"},
    {"id": "primary-b", "upstream_id": "resource-b"}
  ],
  "fallback": {
    "id": "fallback",
    "upstream_id": "resource-fallback",
    "limits": {"parallel": 2, "rpm": 60, "tpm": 60000}
  },
  "client_limits": {"parallel": 2, "rpm": 60, "tpm": 60000},
  "output_tokens": 256,
  "heartbeat_seconds": 10,
  "heartbeat_timeout": 60,
  "event_retention_seconds": 86400
}
```

Primary limits default to parallel 4, RPM 60 and TPM 60000. Override these operator policy defaults to match the actual upstream capacity. Virtual-key limits can further lower the pool's client limits. The inactivity interval is fixed at 900 seconds after the final completed request, and cannot be shortened through configuration

The proxy configuration uses the latest upstream model selected by the operator through `UPSTREAM_MODEL`, separate endpoint credentials, and an explicitly loaded callback instance:

```yaml
model_list:
  - model_name: exclusive-pool
    litellm_params:
      model: os.environ/UPSTREAM_MODEL
      api_base: os.environ/PRIMARY_A_BASE_URL
      api_key: os.environ/PRIMARY_A_API_KEY
      max_retries: 0
    model_info:
      id: primary-a
  - model_name: exclusive-pool
    litellm_params:
      model: os.environ/UPSTREAM_MODEL
      api_base: os.environ/PRIMARY_B_BASE_URL
      api_key: os.environ/PRIMARY_B_API_KEY
      max_retries: 0
    model_info:
      id: primary-b
  - model_name: exclusive-pool
    litellm_params:
      model: os.environ/UPSTREAM_MODEL
      api_base: os.environ/FALLBACK_BASE_URL
      api_key: os.environ/FALLBACK_API_KEY
      max_retries: 0
    model_info:
      id: fallback
litellm_settings:
  callbacks:
    - litellm.router_utils.exclusive_pool.configured.callback
router_settings:
  num_retries: 0
  disable_cooldowns: true
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
```

This first implementation permits one upstream attempt per logical operation. It suppresses built-in retries, fallback and response caching for pool requests. Do not enable model-group retry policies, silent traffic mirroring, independent affinity or other hooks that rewrite pool routing. A confirmed upstream failure changes the resource state; the next client request can move to another primary or fallback while retaining the device binding. An incomplete stream is never retried after any output reaches the client

## Coordination and operation

Set `EXCLUSIVE_POOL_CONFIG` to the absolute JSON path and `EXCLUSIVE_POOL_REDIS_URL` privately to the Redis connection URL. All workers must use the same configuration and Redis. This implementation uses standalone Redis WATCH/MULTI transactions, with one persistent key per pool. Redis Cluster and automatic failover to asynchronously replicated state are not qualified. Use durable persistence and prevent eviction of the coordination key

Creating state is an operator action, not a proxy startup side effect. Before the first initialization, confirm the upstream resources have no outstanding requests:

```bash
uv run python -m litellm.router_utils.exclusive_pool.admin init --confirm-upstream-idle
uv run litellm --config exclusive-proxy.yaml --port 4000 --num_workers 2
```

Missing Redis state, a configuration mismatch or unavailable coordination yields `503 routing_state_unavailable`. Never initialize from an empty snapshot after data loss while old upstream work could still exist. Restore trustworthy state or wait for and confirm the end of every old request before explicitly initializing again

Inspect the current state, including safe operation IDs and bounded diagnostic events:

```bash
uv run python -m litellm.router_utils.exclusive_pool.admin status
```

The snapshot stores digests, resource IDs and accounting, without API keys, device identifiers, prompts or credentials. Event history is limited to 256 entries and the configured retention interval through background cleanup and on new events. Redis access remains an administrative privilege

Heartbeat loss reclaims unsent reservations. Started requests remain quarantined until their end is known. Client timeout and disconnect do not prove provider completion. This implementation deliberately does not infer a provider execution bound from a local timeout. After independently confirming a quarantined operation ended upstream, acknowledge only that operation ID:

```bash
uv run python -m litellm.router_utils.exclusive_pool.admin confirm \
  --operation OPERATION_ID --confirm-upstream-idle
```

A healthy heartbeat prevents this override. Confirmation is idempotent and cannot remove a different operation. A disabled deployment requires repaired credentials/configuration and an explicit enable, followed by an actual generation probe:

```bash
uv run python -m litellm.router_utils.exclusive_pool.admin enable --resource primary-a
```

Recovery workers compete atomically for probe ownership. Probes use the deployment's own model, credentials and settings through Router and consume upstream quota rather than a client-key quota. Unknown completion of a probe also requires confirmation. A recovered fallback starts at one in-flight request and grows by one after each successful completion up to its configured capacity

To change the pool, drain old operations, update the configuration on every worker and run `admin reconfigure`. The operation refuses to change state while old operations remain active. Workers with an obsolete configuration fail closed until reloaded

## Verification

The [multi-client runner](../../tests/e2e/router/exclusive_endpoint_pool.md) targets a live isolated pool with real providers. It is separate from controlled integration tests with a scripted upstream

The integration suite owns a real proxy, two workers, PostgreSQL, Redis and a local upstream. Its tests exercise device rejection before forwarding, primary/fallback assignments, conflicts, quota errors, stream completion and client cancellation. Pure transition tests cover the exact 900-second boundary, stale completion, heartbeat loss, quota reservations, cooldown and probing

Real-provider acceptance and installed Claude client qualification still require operator credentials and compatible clients. The controlled integration suite does not replace those checks
