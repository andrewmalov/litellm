from __future__ import annotations

import hmac
import os
import uuid
from pathlib import Path
from time import time
from typing import Final

from fastapi import FastAPI, Header, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, generate_latest
from redis.asyncio import Redis

from .models import Admission, CatalogEntry, Failure, PoolConfig, RoutingCounters, Snapshot
from .state import _prune, _resource_available, catalog_is_current
from .store import RedisPoolStore


def _catalog(snapshot: Snapshot, config: PoolConfig, model: str) -> tuple[CatalogEntry, ...]:
    if config.discovery:
        return tuple(entry for entry in snapshot.catalog if entry.active and entry.model == model)
    return tuple(
        CatalogEntry(resource=resource, model=model, role="fallback" if config.is_fallback(resource.id) else "primary")
        for resource in config.selectable
    )


def render_metrics(snapshot: Snapshot, config: PoolConfig, now: float, tokens: int) -> str:
    registry: Final = CollectorRegistry()
    Gauge("exclusive_pool_snapshot_timestamp_seconds", "Time of the validated snapshot", registry=registry).set(now)
    Gauge("exclusive_pool_reference_request_tokens", "Reservation for capacity estimates", registry=registry).set(
        tokens
    )
    primary: Final = Gauge(
        "exclusive_pool_primary_available", "Primary accounts available to a new session", ("model",), registry=registry
    )
    fallback: Final = Gauge(
        "exclusive_pool_fallback_available", "Fallback available to a new session", ("model",), registry=registry
    )
    transitions: Final = Counter(
        "exclusive_pool_fallback_transitions",
        "Requests beginning a new fallback assignment",
        ("model",),
        registry=registry,
    )
    exhausted: Final = Counter(
        "exclusive_pool_endpoint_pool_exhausted", "Requests refused by pool admission", ("model",), registry=registry
    )
    labels: Final = ("model", "upstream_id", "role", "deployment")
    health: Final = Gauge(
        "exclusive_pool_upstream_status", "Health of configured upstreams", (*labels, "status"), registry=registry
    )
    assigned: Final = Gauge(
        "exclusive_pool_upstream_assigned", "Upstream has a live session assignment", labels, registry=registry
    )
    retry: Final = Gauge(
        "exclusive_pool_upstream_retry_timestamp_seconds",
        "Earliest client request eligible for recovery",
        labels,
        registry=registry,
    )
    current: Final = _prune(snapshot, now)
    resolved: Final = config.resolve(snapshot)
    key: Final = uuid.uuid4().hex
    bound: Final = frozenset(session.resource for session in current.sessions)
    for model in resolved.models:
        entries: Final = _catalog(current, resolved, model)
        request: Final = Admission(
            client=key, device="", operation=key, tokens=tokens, eligible=tuple(e.resource.id for e in entries)
        )
        available: Final = tuple(
            entry for entry in entries if _resource_available(current, resolved, entry.resource, request, now)
        )
        primary.labels(model).set(len(frozenset(e.resource.upstream_id for e in available if e.role == "primary")))
        fallback.labels(model).set(int(any(e.role == "fallback" for e in available)))
        counts: Final = next((c for c in current.routing_counters if c.model == model), RoutingCounters(model=model))
        transitions.labels(model).inc(counts.fallback_transitions)
        exhausted.labels(model).inc(counts.exhausted)
        for entry in entries:
            status: Final = next(h for h in current.health if h.resource == entry.resource.id)
            values: Final = (model, entry.resource.upstream_id, entry.role, entry.resource.id)
            health.labels(*values, status.status).set(1)
            assigned.labels(*values).set(int(bool(bound & resolved.upstream_deployments(entry.resource.upstream_id))))
            if status.status in ("cooldown", "disabled") and status.retry_at > 0:
                retry.labels(*values).set(status.retry_at)
    return generate_latest(registry).decode()


def create_app() -> FastAPI:
    config: Final = PoolConfig.model_validate_json(Path(os.environ["EXCLUSIVE_POOL_CONFIG"]).read_bytes())
    token: Final = Path(os.environ["EXCLUSIVE_POOL_METRICS_TOKEN_FILE"]).read_bytes().strip()
    tokens: Final = int(os.environ["EXCLUSIVE_POOL_METRICS_REQUEST_TOKENS"])
    if len(token) < 32 or tokens <= 0:
        raise RuntimeError("Invalid pool metrics configuration")
    app: Final = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/metrics/exclusive-pool")
    async def metrics(authorization: str = Header(default="")) -> Response:
        if not hmac.compare_digest(authorization.encode(), b"Bearer " + token):
            return Response(status_code=401)
        async with Redis.from_url(
            os.environ["EXCLUSIVE_POOL_REDIS_URL"], socket_timeout=2, socket_connect_timeout=2
        ) as redis:
            result: Final = await RedisPoolStore(redis, config).read()
        now: Final = time()
        if isinstance(result, Failure) or not catalog_is_current(result, config, now):
            return Response(status_code=503)
        return Response(render_metrics(result, config, now, tokens), media_type=CONTENT_TYPE_LATEST)

    return app
