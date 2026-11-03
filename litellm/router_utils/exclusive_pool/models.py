from __future__ import annotations

import hashlib
import re
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import Self


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class Limits(FrozenModel):
    parallel: Annotated[int, Field(gt=0)] = 4
    rpm: Annotated[int, Field(gt=0)] = 60
    tpm: Annotated[int, Field(gt=0)] = 60000


class Resource(FrozenModel):
    id: Annotated[str, Field(min_length=1)]
    upstream_id: Annotated[str, Field(min_length=1)]
    quota_scope: str | None = None
    limits: Limits = Field(default_factory=Limits)

    @property
    def scope(self) -> str:
        return self.quota_scope or self.upstream_id


class PoolConfig(FrozenModel):
    pool_id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]+$")]
    models: Annotated[tuple[str, ...], Field(min_length=1)]
    primaries: Annotated[tuple[Resource, ...], Field(min_length=1)]
    fallback: Resource
    client_limits: Limits
    output_tokens: Annotated[int, Field(gt=0)] = 256
    heartbeat_seconds: Annotated[float, Field(gt=0)] = 10
    heartbeat_timeout: Annotated[float, Field(gt=0)] = 60
    device_header: str | None = None
    event_retention_seconds: Annotated[int, Field(gt=0)] = 86400

    @property
    def resources(self) -> tuple[Resource, ...]:
        return (*self.primaries, self.fallback)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    @model_validator(mode="after")
    def consistent(self) -> Self:
        assert len(frozenset(r.id for r in self.resources)) == len(self.resources), "deployment IDs must be unique"
        assert len(frozenset(r.upstream_id for r in self.resources)) == len(self.resources), "upstreams must be unique"
        assert self.heartbeat_timeout > self.heartbeat_seconds, "heartbeat timeout must exceed its interval"
        assert all(_same_scope_limits(resource, self.resources) for resource in self.resources), (
            "shared quota limits differ"
        )
        assert self.device_header is None or (
            re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", self.device_header)
            and self.device_header.lower()
            not in (
                "user-agent",
                "authorization",
                "proxy-authorization",
                "x-forwarded-for",
                "x-real-ip",
                "forwarded",
                "host",
                "cookie",
                "referer",
            )
        ), "device header must be dedicated to device identity"
        return self


def _same_scope_limits(resource: Resource, resources: tuple[Resource, ...]) -> bool:
    return all(other.limits == resource.limits for other in resources if other.scope == resource.scope)


class Failure(FrozenModel):
    code: Literal[
        "endpoint_pool_exhausted",
        "key_bound_to_another_device",
        "client_rate_limit_exceeded",
        "routing_state_unavailable",
        "device_id_required",
        "unsupported_pool_request",
        "upstream_stream_incomplete",
    ]
    status: int
    message: str
    retry_after: int = 0


class Session(FrozenModel):
    client: str
    device: str
    generation: str
    resource: str | None
    last_activity: float
    established: bool = False


class Operation(FrozenModel):
    id: str
    logical: str
    client: str
    generation: str
    resource: str
    heartbeat: float
    started: bool = False
    quarantined: bool = False
    probe: bool = False
    health_generation: str = "initial"


class Charge(FrozenModel):
    scope: str
    operation: str
    created_at: float
    tokens: int
    settled_at: float | None = None


class Health(FrozenModel):
    resource: str
    status: Literal["ready", "cooldown", "probing", "disabled"] = "ready"
    retry_at: float = 0
    cycles: int = 0
    generation: str = "initial"
    success_since: float | None = None
    capacity: int | None = None


class Event(FrozenModel):
    time: float
    kind: str
    client: str = ""
    device: str = ""
    resource: str = ""


class Snapshot(FrozenModel):
    fingerprint: str
    cursor: str | None = None
    sessions: tuple[Session, ...] = ()
    operations: tuple[Operation, ...] = ()
    charges: tuple[Charge, ...] = ()
    health: tuple[Health, ...] = ()
    events: tuple[Event, ...] = ()


class Admission(FrozenModel):
    client: str
    device: str
    operation: str
    logical: str | None = None
    tokens: Annotated[int, Field(gt=0)]
    eligible: tuple[str, ...]
    limits: Limits | None = None


class Granted(FrozenModel):
    kind: Literal["granted"] = "granted"
    state: Snapshot
    resource: str
    operation: str


class Denied(FrozenModel):
    kind: Literal["denied"] = "denied"
    state: Snapshot
    failure: Failure


Outcome: TypeAlias = Granted | Denied
