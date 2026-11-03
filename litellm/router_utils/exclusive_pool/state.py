from __future__ import annotations

import math
from functools import reduce
from typing import Final

from .models import (
    Admission,
    Charge,
    Denied,
    Event,
    Failure,
    Granted,
    Health,
    Limits,
    Operation,
    Outcome,
    PoolConfig,
    Resource,
    Session,
    Snapshot,
)

IDLE_SECONDS: Final = 900


def unavailable() -> Failure:
    return Failure(code="routing_state_unavailable", status=503, message="Endpoint pool coordination is unavailable")


def exhausted() -> Failure:
    return Failure(
        code="endpoint_pool_exhausted", status=503, message="Все основные и резервные эндпоинты заняты или недоступны"
    )


def empty_state(config: PoolConfig) -> Snapshot:
    return Snapshot(
        fingerprint=config.fingerprint,
        health=() if config.discovery else tuple(Health(resource=r.id) for r in config.resources),
    )


def _active(state: Snapshot, client: str) -> tuple[Operation, ...]:
    return tuple(op for op in state.operations if op.client == client)


def _live(state: Snapshot, session: Session, now: float) -> bool:
    return bool(_active(state, session.client)) or now < session.last_activity + IDLE_SECONDS


def _prune(state: Snapshot, now: float) -> Snapshot:
    return state.model_copy(
        update={
            "sessions": tuple(s for s in state.sessions if _live(state, s, now)),
            "charges": tuple(
                c for c in state.charges if c.settled_at is None or max(c.created_at, c.settled_at) > now - 60
            ),
        }
    )


def _charges(state: Snapshot, scope: str) -> tuple[Charge, ...]:
    return tuple(c for c in state.charges if c.scope == scope)


def has_capacity(state: Snapshot, scope: str, limits: Limits, tokens: int, now: float, logical: str) -> bool:
    charges: Final = tuple(
        c for c in _charges(state, scope) if c.settled_at is None or max(c.created_at, c.settled_at) > now - 60
    )
    existing: Final = next((c for c in charges if c.operation == logical), None)
    count: Final = sum(c.created_at > now - 60 for c in charges)
    reserved: Final = sum(c.tokens for c in charges)
    return count + (existing is None) <= limits.rpm and reserved + (0 if existing else tokens) <= limits.tpm


def _scope_operations(state: Snapshot, config: PoolConfig, resource: Resource) -> tuple[Operation, ...]:
    ids: Final = frozenset(r.id for r in config.resources if r.scope == resource.scope)
    return tuple(op for op in state.operations if op.resource in ids)


def _resource_available(
    state: Snapshot, config: PoolConfig, resource: Resource, request: Admission, now: float
) -> bool:
    health: Final = next(h for h in state.health if h.resource == resource.id)
    operations: Final = _scope_operations(state, config, resource)
    if resource.id not in request.eligible or health.status != "ready":
        return False
    if any(op.quarantined or now - op.heartbeat >= config.heartbeat_timeout for op in operations):
        return False
    if len(operations) >= min(resource.limits.parallel, health.capacity or resource.limits.parallel):
        return False
    if not config.is_fallback(resource.id) and any(
        s.client != request.client and s.resource == resource.id for s in state.sessions
    ):
        return False
    return has_capacity(state, f"resource:{resource.scope}", resource.limits, request.tokens, now, request.operation)


def _ordered_primaries(state: Snapshot, config: PoolConfig) -> tuple[Resource, ...]:
    ids: Final = tuple(r.id for r in config.primaries)
    offset: Final = ids.index(state.cursor) + 1 if state.cursor in ids else 0
    return (*config.primaries[offset:], *config.primaries[:offset])


def _choose(
    state: Snapshot, config: PoolConfig, request: Admission, session: Session | None, now: float
) -> Resource | None:
    if session is not None and session.resource is None and _active(state, request.client):
        return None
    if (
        session is not None
        and session.resource not in tuple(r.id for r in config.selectable)
        and _active(state, request.client)
    ):
        return None
    previous: Final = next((r for r in config.selectable if session is not None and r.id == session.resource), None)
    if previous is not None and _resource_available(state, config, previous, request, now):
        return previous
    if previous is not None:
        prior_health: Final = next(h for h in state.health if h.resource == previous.id)
        if prior_health.status == "ready" or any(op.resource == previous.id for op in state.operations):
            return None
    return next(
        (
            r
            for r in (*_ordered_primaries(state, config), *((config.fallback,) if config.fallback is not None else ()))
            if _resource_available(state, config, r, request, now)
        ),
        None,
    )


def acquire(state: Snapshot, config: PoolConfig, request: Admission, now: float) -> Outcome:
    if config.discovery and (state.discovered_at is None or now - state.discovered_at >= config.heartbeat_timeout):
        return Denied(state=state, failure=unavailable())
    if state.fingerprint != config.fingerprint:
        return Denied(state=state, failure=unavailable())
    current: Final = _prune(state, now)
    session: Final = next((s for s in current.sessions if s.client == request.client), None)
    if session is not None and session.device != request.device:
        return Denied(
            state=record_event(
                state, config, Event(time=now, kind="device_conflict", client=request.client, device=request.device)
            ),
            failure=Failure(
                code="key_bound_to_another_device",
                status=409,
                message="API key is bound to another device",
                retry_after=max(1, math.ceil(session.last_activity + IDLE_SECONDS - now)),
            ),
        )
    repeated: Final = next((op for op in current.operations if op.id == request.operation), None)
    if repeated is not None:
        if repeated.client != request.client or repeated.quarantined:
            return Denied(state=state, failure=unavailable())
        return Granted(state=current, resource=repeated.resource, operation=repeated.id)
    logical: Final = request.logical or request.operation
    client_limits: Final = request.limits or config.client_limits
    if len(_active(current, request.client)) >= client_limits.parallel or not has_capacity(
        current, f"client:{request.client}", client_limits, request.tokens, now, logical
    ):
        return Denied(
            state=record_event(
                state, config, Event(time=now, kind="client_rate_limit", client=request.client, device=request.device)
            ),
            failure=Failure(
                code="client_rate_limit_exceeded", status=429, message="Client rate limit exceeded", retry_after=60
            ),
        )
    resource: Final = _choose(current, config, request, session, now)
    if resource is None:
        return Denied(state=state, failure=exhausted())
    bound: Final = Session(
        client=request.client,
        device=request.device,
        generation=session.generation if session else request.operation,
        resource=resource.id,
        last_activity=session.last_activity if session else now,
        established=session.established if session else False,
    )
    operation: Final = Operation(
        id=request.operation,
        logical=logical,
        client=request.client,
        generation=bound.generation,
        resource=resource.id,
        heartbeat=now,
        health_generation=next(h.generation for h in current.health if h.resource == resource.id),
    )
    client_charge: Final = (
        ()
        if any(c.scope == f"client:{request.client}" and c.operation == logical for c in current.charges)
        else (Charge(scope=f"client:{request.client}", operation=logical, created_at=now, tokens=request.tokens),)
    )
    updated: Final = current.model_copy(
        update={
            "sessions": (*tuple(s for s in current.sessions if s.client != request.client), bound),
            "operations": (*current.operations, operation),
            "charges": (
                *current.charges,
                *client_charge,
                Charge(
                    scope=f"resource:{resource.scope}",
                    operation=request.operation,
                    created_at=now,
                    tokens=request.tokens,
                ),
            ),
            "cursor": resource.id
            if not config.is_fallback(resource.id) and (session is None or session.resource != resource.id)
            else current.cursor,
        }
    )
    return Granted(state=updated, resource=resource.id, operation=operation.id)


def start(state: Snapshot, operation: str, now: float) -> Snapshot:
    active: Final = next((op for op in state.operations if op.id == operation), None)
    if active is None or active.quarantined:
        return state
    return state.model_copy(
        update={
            "operations": tuple(
                op.model_copy(update={"started": True, "heartbeat": now}) if op.id == operation else op
                for op in state.operations
            ),
        }
    )


def heartbeat(state: Snapshot, operation: str, now: float) -> Snapshot:
    return state.model_copy(
        update={
            "operations": tuple(
                op.model_copy(update={"heartbeat": now}) if op.id == operation and not op.quarantined else op
                for op in state.operations
            ),
        }
    )


def quarantine(state: Snapshot, operation: str) -> Snapshot:
    return state.model_copy(
        update={
            "operations": tuple(
                op.model_copy(update={"quarantined": True}) if op.id == operation else op for op in state.operations
            ),
        }
    )


def complete(
    state: Snapshot, config: PoolConfig, operation: str, now: float, actual_tokens: int | None = None
) -> Snapshot:
    active: Final = next((op for op in state.operations if op.id == operation), None)
    if active is None:
        return state
    remaining: Final = tuple(op for op in state.operations if op.id != operation)
    client_still_active: Final = any(op.client == active.client for op in remaining)
    resource: Final = next(r for r in config.resources if r.id == active.resource)
    return state.model_copy(
        update={
            "operations": remaining,
            "sessions": tuple(_finish_session(s, active, client_still_active, now) for s in state.sessions),
            "charges": tuple(_settle(c, active, resource, now, actual_tokens) for c in state.charges),
            "health": tuple(
                _released_probe(_successful_health(h, active, resource, now), active) for h in state.health
            ),
        }
    )


def _finish_session(session: Session, active: Operation, still_active: bool, now: float) -> Session:
    if session.client != active.client or session.generation != active.generation or still_active:
        return session
    return session.model_copy(update={"last_activity": now, "established": True})


def _settle(charge: Charge, operation: Operation, resource: Resource, now: float, actual: int | None) -> Charge:
    if (charge.scope, charge.operation) not in (
        (f"client:{operation.client}", operation.logical),
        (f"resource:{resource.scope}", operation.id),
    ):
        return charge
    return charge.model_copy(update={"settled_at": now, "tokens": charge.tokens if actual is None else max(0, actual)})


def abort(state: Snapshot, config: PoolConfig, operation: str, now: float) -> Snapshot:
    active: Final = next((op for op in state.operations if op.id == operation), None)
    if active is None:
        return state
    if active.started:
        return quarantine(state, operation)
    finished: Final = state.model_copy(
        update={"operations": tuple(op for op in state.operations if op.id != operation)}
    )
    return finished.model_copy(
        update={
            "sessions": tuple(s for s in finished.sessions if s.established or bool(_active(finished, s.client))),
            "charges": tuple(c for c in finished.charges if c.operation not in (active.id, active.logical)),
            "health": tuple(_released_probe(h, active) for h in state.health),
        }
    )


def _released_probe(health: Health, operation: Operation) -> Health:
    if operation.probe and health.status == "probing" and health.generation == operation.health_generation:
        return health.model_copy(update={"status": "cooldown"})
    return health


def _successful_health(health: Health, operation: Operation, resource: Resource, now: float) -> Health:
    if health.resource != operation.resource or health.status != "ready" or operation.probe:
        return health
    successful_since: Final = health.success_since if health.success_since is not None else now
    return health.model_copy(
        update={
            "success_since": successful_since,
            "cycles": 0 if now - successful_since >= 300 else health.cycles,
            "capacity": min(resource.limits.parallel, health.capacity + 1) if health.capacity is not None else None,
        }
    )


def reject(state: Snapshot, config: PoolConfig, operation: str, now: float) -> Snapshot:
    active: Final = next((op for op in state.operations if op.id == operation), None)
    if active is None:
        return state
    remaining: Final = tuple(op for op in state.operations if op.id != operation)
    finished: Final = complete(state, config, operation, now, actual_tokens=0)
    return finished.model_copy(
        update={
            "sessions": tuple(
                s for s in state.sessions if s.established or any(op.client == s.client for op in remaining)
            ),
            "health": tuple(_released_probe(h, active) for h in state.health),
        }
    )


def record_event(state: Snapshot, config: PoolConfig, event: Event) -> Snapshot:
    retained: Final = tuple(item for item in state.events if item.time > event.time - config.event_retention_seconds)
    return state.model_copy(update={"events": (*retained, event)[-256:]})


def sweep(state: Snapshot, config: PoolConfig, now: float) -> Snapshot:
    expired: Final = tuple(
        op for op in state.operations if not op.started and now - op.heartbeat >= config.heartbeat_timeout
    )
    released: Final = reduce(lambda current, op: abort(current, config, op.id, now), expired, state)
    return _prune(released, now).model_copy(
        update={
            "operations": tuple(
                op.model_copy(update={"quarantined": True}) if now - op.heartbeat >= config.heartbeat_timeout else op
                for op in released.operations
            ),
            "events": tuple(event for event in released.events if event.time > now - config.event_retention_seconds),
        }
    )
