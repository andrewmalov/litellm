# Exclusive endpoint pool deployment

This stack targets linux/amd64, builds the current checkout with the repository's root Dockerfile and runs two proxy workers, standalone Redis and PostgreSQL. It supports single-choice text Chat Completions, including streaming. Read the [pool runbook](../../docs/superpowers/exclusive-endpoint-pool.md) for routing guarantees and current limitations

Run the commands below from this directory. Docker Compose v2 or newer is required. Image builds need access to cgr.dev, packages.wolfi.dev, Docker Hub, GitHub and Python/npm registries. If Wolfi returns 403, check the US VPN before changing base images or dependencies

## Configuration

Create the deployment environment without overwriting an existing file:

```bash
test ! -e .env && cp .env.example .env
chmod 600 .env
```

Fill every empty entry in `.env`. Generate independent master and salt keys with `openssl rand -hex 32`, prefixing each with `sk-`. Generate Redis and PostgreSQL passwords with `openssl rand -hex 32` too. Keep database passwords hexadecimal because the template embeds them in connection URLs without URL encoding. Keep the salt key stable across restarts and backups

Set `UPSTREAM_MODEL` to the current text chat model supported by your endpoints, including its LiteLLM provider prefix. Check the repository's `model_prices_and_context_window.json` or the provider's current documentation before selecting it. Each base URL must include the endpoint's API path, such as `/v1` for an OpenAI-compatible service. Supply three separately provisioned resources, each with its own URL and API key

Edit `pool.json` to match actual endpoint capacity. Deployment IDs must match `proxy.yaml`. If different resources share account quota, declare their common `quota_scope` and identical limits. Do not represent one physical endpoint as multiple resources. Redis is dedicated to pool coordination, with AOF fsync on every write, no eviction and a 256 MB key-memory limit. Monitor disk space and Redis write failures

The proxy binds to `127.0.0.1:4000` by default. Put it behind an authenticated TLS reverse proxy for remote access. Set `LITELLM_BIND` and `LITELLM_PORT` explicitly if the reverse proxy runs elsewhere. Neither Redis nor PostgreSQL publishes a host port. The stack uses the bundled model cost map instead of fetching it from GitHub at startup

## Build and first start

Validate privately without rendering interpolated secrets. Skip `build proxy` when the configured image has already been built or published. Otherwise build from source, or use the runtime-layer method below with a verified compatible base:

```bash
docker compose config --quiet
docker compose build proxy
docker compose up -d --wait redis postgres
```

Before initialization, independently confirm all three upstream resources have no outstanding requests. Only after that confirmation, create state explicitly:

```bash
docker compose run --rm pool-admin init --confirm-upstream-idle
docker compose up -d --no-build --wait proxy
docker compose ps
docker compose run --rm pool-admin status
curl --fail-with-body http://127.0.0.1:4000/health/liveliness
```

`pool-admin` is an operator-only service behind the `ops` profile. An explicit `run pool-admin` enables it for that command. Normal `up` never initializes or replaces pool state. A green container healthcheck proves HTTP liveness only, so also inspect pool status before sending traffic

Use an admin bearer key to create a client virtual key through `/key/generate`. Clients call `/v1/chat/completions` with model `exclusive-pool`, that virtual key and `metadata.user_id` containing a JSON string with `device_id`. See the [multi-client runner](../../tests/e2e/router/exclusive_endpoint_pool.md) for live-provider acceptance. Container startup alone does not qualify real providers or installed Claude clients

The default image tag identifies the core source revision. Set a new `LITELLM_IMAGE` tag when rebuilding modified source. The build argument `LITELLM_RELEASE_TAG` in `compose.yaml` should identify that revision too. To build the x86 deployment image directly, use Buildx:

```bash
docker buildx build --platform linux/amd64 --target runtime \
  --build-arg LITELLM_RELEASE_TAG=exclusive-pool-5934b5681f \
  -t litellm-exclusive-pool:5934b5681f --load ../..
```

Full source builds compile the Rust extension with release LTO and need substantially more memory than running the proxy. Docker with 8 GB RAM failed locally during that compilation. A native x86 builder with enough memory is preferable to emulation

For this exact core revision, `Dockerfile.runtime` can reuse a trusted linux/amd64 runtime image built at `ea57c0345c`. Between that base and `5934b5681f`, `pyproject.toml`, `uv.lock`, Rust, enterprise code and proxy extras are unchanged. It copies every changed production Python module and preserves the already-built Rust extension and UI. It is limited to this revision pair. For later changes, recheck the full production diff or use the root Dockerfile

Supply the trusted base from your registry by digest:

```bash
docker build --platform linux/amd64 -f Dockerfile.runtime \
  --build-arg BASE_IMAGE="$TRUSTED_BASE_IMAGE_DIGEST" \
  -t litellm-exclusive-pool:5934b5681f ../..
```

The base must use Python 3.13 and `/app/.venv`, as the original image does. Do not substitute an unrelated LiteLLM release. Keep the base digest with the release record so the runtime-layer build remains reproducible

To deploy without the source checkout on another host, publish your built image to your registry, copy this directory without `.env`, transfer credentials separately, set `LITELLM_IMAGE` to the published digest and use `docker compose up -d --no-build --wait proxy` after starting dependencies and explicitly initializing the pool. Build options remain unused with `--no-build`

## Operations

```bash
docker compose logs --tail 100 proxy
docker compose run --rm pool-admin status
docker compose run --rm pool-admin confirm --operation OPERATION_ID --confirm-upstream-idle
docker compose run --rm pool-admin enable --resource primary-a
```

Use `confirm` only after verifying that the named quarantined request ended upstream. Timeout, disconnect, proxy shutdown and lost heartbeat do not prove completion. A healthy operation cannot be confirmed away

Drain upstream operations before changing configuration. Update both files consistently, run `docker compose run --rm pool-admin reconfigure` while the proxy is stopped, then recreate proxy workers. The admin command rejects changes while operations remain active

Stop the stack while retaining both volumes:

```bash
docker compose down
```

Do not use `down -v` on a deployment. Back up both volumes and preserve `.env` securely. After Redis data loss or restoration of a stale backup, stop traffic and independently verify the end of all old upstream operations before restoring trusted state or explicitly initializing again. Do not restart against stale restored state while previous operations might still exist. PostgreSQL retains virtual keys and spend data, but cannot reconstruct Redis pool ownership
