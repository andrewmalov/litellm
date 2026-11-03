from __future__ import annotations

import math
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Final, Literal

import httpx
import openai
from pydantic import ConfigDict, TypeAdapter, ValidationError

import litellm

from .models import Charge, Denied, Event, FrozenModel, Granted, Health, Operation, Outcome, PoolConfig, Snapshot
from .request import recovery_request
from .state import complete, exhausted, has_capacity, quarantine, record_event, reject

_RESPONSE: Final = TypeAdapter(httpx.Response, config=ConfigDict(arbitrary_types_allowed=True))
_CODE: Final = TypeAdapter[str | None](str | None)


@dataclass(frozen=True, slots=True)
class ErrorPolicy:
    kind: Literal["client", "cooldown", "disabled"]
    confirmed_end: bool
    base_seconds: float = 15
    retry_after: str | None = None
    shared_quota: bool = False


def retry_after(value: str | None, now: float) -> float:
    if value is None:
        return 0
    try:
        number: Final = float(value)
        return max(0, number) if math.isfinite(number) else 0
    except ValueError:
        pass
    try:
        parsed: Final = parsedate_to_datetime(value)
        return max(0, parsed.timestamp() - now) if parsed.tzinfo is not None else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def classify(exception: Exception) -> ErrorPolicy:
    if isinstance(exception, (litellm.Timeout, litellm.APIConnectionError, openai.APIConnectionError)):
        return ErrorPolicy(kind="cooldown", confirmed_end=False)
    if isinstance(exception, (litellm.AuthenticationError, litellm.PermissionDeniedError, litellm.NotFoundError)):
        return ErrorPolicy(kind="disabled", confirmed_end=True)
    if isinstance(exception, litellm.BudgetExceededError):
        return ErrorPolicy(kind="client", confirmed_end=True)
    if isinstance(exception, litellm.RateLimitError):
        if exception.category in ("litellm_rate_limit", "litellm_batch_rate_limit"):
            return ErrorPolicy(kind="client", confirmed_end=True)
        if _provider_code(exception) in ("insufficient_quota", "billing_hard_limit_reached", "credit_balance_too_low"):
            return ErrorPolicy(kind="disabled", confirmed_end=True, shared_quota=True)
        return ErrorPolicy(
            kind="cooldown",
            confirmed_end=True,
            base_seconds=30,
            retry_after=_CODE.validate_python(_RESPONSE.validate_python(exception.response).headers.get("retry-after")),
            shared_quota=True,
        )
    if isinstance(
        exception, (litellm.BadRequestError, litellm.ContentPolicyViolationError, litellm.ContextWindowExceededError)
    ):
        return ErrorPolicy(kind="client", confirmed_end=True)
    if isinstance(exception, openai.APIStatusError):
        return ErrorPolicy(
            kind="cooldown",
            confirmed_end=True,
            retry_after=_CODE.validate_python(_RESPONSE.validate_python(exception.response).headers.get("retry-after")),
        )
    return ErrorPolicy(kind="cooldown", confirmed_end=False)


class _ProviderError(FrozenModel):
    code: str | None = None
    error: _ProviderError | None = None


def _body_code(value: object) -> str | None:
    try:
        body: Final = _ProviderError.model_validate(value)
    except ValidationError:
        return None
    return body.code or (body.error.code if body.error else None)


def _provider_code(exception: openai.APIError) -> str | None:
    try:
        supplied: Final = _body_code(exception.body)
        if supplied is not None:
            return supplied
        if isinstance(exception, openai.APIStatusError):
            response: Final = _RESPONSE.validate_python(exception.response)
            try:
                parsed_body: Final = _ProviderError.model_validate_json(response.content)
                parsed: Final = parsed_body.code or (parsed_body.error.code if parsed_body.error else None)
                if parsed is not None:
                    return parsed
            except ValueError:
                pass
        return _CODE.validate_python(exception.code)
    except ValidationError:
        return None


def fail(
    state: Snapshot, config: PoolConfig, operation: str, policy: ErrorPolicy, now: float, jitter: float
) -> Snapshot:
    active: Final = next((op for op in state.operations if op.id == operation), None)
    if active is None:
        return state
    if policy.kind == "client" and not active.probe:
        return reject(state, config, operation, now)
    ended: Final = complete(state, config, operation, now) if policy.confirmed_end else quarantine(state, operation)
    resource: Final = next(r for r in config.resources if r.id == active.resource)
    affected: Final = (
        frozenset(r.id for r in config.resources if r.scope == resource.scope)
        if policy.shared_quota
        else frozenset((resource.id,))
    )
    audited: Final = record_event(
        ended, config, Event(time=now, kind=f"upstream_{policy.kind}", client=active.client, resource=resource.id)
    )
    return audited.model_copy(
        update={
            "health": tuple(
                _failed_health(h, active, policy, now, jitter) if h.resource in affected else h for h in ended.health
            ),
            "sessions": tuple(
                s.model_copy(update={"resource": None}) if s.resource in affected else s for s in ended.sessions
            ),
        }
    )


def _failed_health(health: Health, operation: Operation, policy: ErrorPolicy, now: float, jitter: float) -> Health:
    if health.status in ("cooldown", "disabled") and health.generation != operation.health_generation:
        return health
    delay: Final = max(
        retry_after(policy.retry_after, now),
        min(300, policy.base_seconds * (1 << min(max(health.cycles, 0), 6)) * (1 + max(0, min(jitter, 0.1)))),
    )
    return health.model_copy(
        update={
            "status": "cooldown" if policy.kind == "client" else policy.kind,
            "retry_at": now + delay,
            "cycles": health.cycles + 1,
            "generation": operation.id,
            "success_since": None,
            "capacity": None,
        }
    )


def claim_probe(state: Snapshot, config: PoolConfig, resource: str, operation: str, now: float) -> Outcome:
    health: Final = next(h for h in state.health if h.resource == resource)
    target: Final = next(r for r in config.resources if r.id == resource)
    scope_ids: Final = frozenset(r.id for r in config.resources if r.scope == target.scope)
    if health.status != "cooldown" or health.retry_at > now or any(op.resource in scope_ids for op in state.operations):
        return Denied(state=state, failure=exhausted())
    tokens: Final = recovery_request(resource).reserved_tokens(1)
    if not has_capacity(state, f"resource:{target.scope}", target.limits, tokens, now, operation):
        return Denied(state=state, failure=exhausted())
    probe: Final = Operation(
        id=operation,
        logical=operation,
        client="",
        generation=operation,
        resource=resource,
        heartbeat=now,
        probe=True,
        health_generation=operation,
    )
    return Granted(
        resource=resource,
        operation=operation,
        state=state.model_copy(
            update={
                "health": tuple(
                    h.model_copy(update={"status": "probing", "generation": operation}) if h.resource == resource else h
                    for h in state.health
                ),
                "operations": (*state.operations, probe),
                "charges": (
                    *state.charges,
                    Charge(
                        scope=f"resource:{target.scope}",
                        operation=operation,
                        created_at=now,
                        tokens=tokens,
                    ),
                ),
            }
        ),
    )


def probe_success(state: Snapshot, config: PoolConfig, operation: str, now: float) -> Snapshot:
    active: Final = next((op for op in state.operations if op.id == operation and op.probe), None)
    if active is None:
        return state
    health: Final = next(h for h in state.health if h.resource == active.resource)
    if health.status != "probing" or health.generation != active.health_generation:
        return state
    ended: Final = complete(state, config, operation, now)
    return ended.model_copy(
        update={
            "health": tuple(
                h.model_copy(
                    update={
                        "status": "ready",
                        "success_since": now,
                        "capacity": 1 if h.resource == config.fallback.id else None,
                    }
                )
                if h.resource == active.resource
                else h
                for h in ended.health
            ),
        }
    )


def enable(state: Snapshot, resource: str, generation: str) -> Snapshot:
    return state.model_copy(
        update={
            "health": tuple(
                h.model_copy(update={"status": "cooldown", "retry_at": 0, "generation": generation})
                if h.resource == resource and h.status == "disabled"
                else h
                for h in state.health
            ),
        }
    )
