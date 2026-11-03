# Exclusive endpoint pool acceptance

This is the first test-first increment for [the design](../../../../docs/superpowers/specs/2026-11-03-exclusive-endpoint-pool-design.md). The opt-in core is now implemented for text chat completions. Configure it using [the runbook](../../../../docs/superpowers/exclusive-endpoint-pool.md) before live acceptance

Use a dedicated, empty pool with exactly two ordered primary deployments and one fallback. All three must support the same model and chat completions, expose stable `x-litellm-model-id` response headers, and use distinct upstream resources. The fallback and client keys must allow at least two simultaneous requests. Allow enough RPM/TPM for approximately 100 short generations, without response caching, retries or independent router affinity overriding pool decisions. The script does not create model configuration because pool configuration is provisioned through the operator JSON file and proxy YAML

The operator provisions these deployments before running the suite. Management endpoints create fresh scoped virtual keys and delete them on teardown. Pool bindings can outlive key deletion, so stop traffic and wait at least 900 seconds after the last completed request before reusing the pool. Never reset Redis to make this faster while upstream work might still be running

For cross-process acceptance, point `LITELLM_PROXY_URL` at a balanced front door for at least two workers sharing Redis. Sixteen successful requests per binding give a 2^-16 probability of missing a particular replica out of two independently balanced replicas. This is sampling, not a guarantee of worker coverage. The two-thread barrier coordinates competing device requests on one key; it does not prove simultaneous execution inside Redis or at the provider

Export the master key privately, then run:

```bash
export LITELLM_PROXY_URL=http://localhost:4000
export E2E_EXCLUSIVE_POOL_MODEL=exclusive-pool
export E2E_EXCLUSIVE_POOL_PRIMARY_1=primary-a
export E2E_EXCLUSIVE_POOL_PRIMARY_2=primary-b
export E2E_EXCLUSIVE_POOL_FALLBACK=fallback
scripts/run_exclusive_endpoint_pool_e2e.sh
```

The script forces live provider calls, one test process and zero automatic test reruns. It saves the run log and JUnit XML under the path returned by `git rev-parse --git-path exclusive-pool-e2e`. Do not use `--showlocals`: request objects and the harness resource registry can contain virtual keys

The scenario sends invalid identifiers before allocating clients, then fills both primaries and puts two clients on fallback. It checks sticky assignments across parallel requests, changed sessions and forwarded addresses, device conflicts on every route, a race between two devices using a fresh key, and an RPM-limited key rejected on fallback without disabling it. A changed `X-Forwarded-For` tests forwarded-address handling only; actual network egress changes need a separate live-client check. JSON metadata emulates the format already seen in repository tests and does not confirm installed Claude Code or Desktop versions

Only a two-primary cycle is covered here. Longer round-robin sequences after release, 900-second inactivity, rejected-request lease extension, fallback exhaustion, TPM and in-flight limits, streaming cancellation and resource recovery need subsequent tests. Controlled fault cases belong under `tests/integration/routing`, with upstream request-start/completion barriers and an independent overlap journal. That fixture must prove an old upstream call has ended before a resource can be reassigned, including after worker death, Redis outage or data loss. A response header alone cannot prove this

Live acceptance requires the opt-in module and initialized coordination state. A missing proxy or bad credentials are setup failures, not proof of the intended product RED state. Final proof requires reproducible live requests and actual Claude Code/Desktop runs, including what their users see
