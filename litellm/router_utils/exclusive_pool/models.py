from __future__ import annotations

import hashlib
import re
from typing import Annotated, Final, Literal, TypeAlias

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
    models: tuple[str, ...] = ()
    primaries: tuple[Resource, ...] = ()
    fallback: Resource | None = None
    fallback_aliases: tuple[Resource, ...] = ()
    discovery: bool = False
    primary_limits: Limits = Field(default_factory=Limits)
    fallback_limits: Limits = Field(default_factory=Limits)
    retired: tuple[Resource, ...] = ()
    client_limits: Limits
    output_tokens: Annotated[int, Field(gt=0)] = 256
    heartbeat_seconds: Annotated[float, Field(gt=0)] = 10
    heartbeat_timeout: Annotated[float, Field(gt=0)] = 60
    device_header: str | None = None
    event_retention_seconds: Annotated[int, Field(gt=0)] = 86400

    @property
    def resources(self) -> tuple[Resource, ...]:
        return (*self.selectable, *self.retired)

    @property
    def selectable(self) -> tuple[Resource, ...]:
        return (*self.primaries, *self.fallbacks)

    @property
    def fallbacks(self) -> tuple[Resource, ...]:
        return (*((self.fallback,) if self.fallback is not None else ()), *self.fallback_aliases)

    def is_fallback(self, resource: str) -> bool:
        return any(resource == fallback.id for fallback in self.fallbacks)

    def upstream_deployments(self, upstream_id: str) -> frozenset[str]:
        return frozenset(resource.id for resource in self.resources if resource.upstream_id == upstream_id)

    def resolve(self, snapshot: Snapshot) -> PoolConfig:
        if not self.discovery:
            return self
        return self.model_copy(
            update={
                "models": tuple(sorted(frozenset((*self.models, *(entry.model for entry in snapshot.catalog))))),
                "primaries": tuple(e.resource for e in snapshot.catalog if e.active and e.role == "primary"),
                "fallback": next((e.resource for e in snapshot.catalog if e.active and e.role == "fallback"), None),
                "fallback_aliases": tuple(e.resource for e in snapshot.catalog if e.active and e.role == "fallback")[
                    1:
                ],
                "retired": tuple(e.resource for e in snapshot.catalog if not e.active),
            }
        )

    @property
    def fingerprint(self) -> str:
        excluded: Final = (
            {"models", "primaries", "fallback", "fallback_aliases", "retired"}
            if self.discovery
            else {"discovery", "primary_limits", "fallback_limits", "fallback_aliases", "retired"}
        )
        return hashlib.sha256(self.model_dump_json(exclude=excluded).encode()).hexdigest()

    @model_validator(mode="after")
    def consistent(self) -> Self:
        assert self.discovery or (self.models and self.primaries and self.fallback is not None), (
            "static pool is incomplete"
        )
        assert len(frozenset(r.id for r in self.resources)) == len(self.resources), "deployment IDs must be unique"
        assert self.discovery or (
            not self.fallback_aliases and len(frozenset(r.upstream_id for r in self.resources)) == len(self.resources)
        ), "static upstreams must be unique"
        assert all(_same_upstream_policy(resource, self.resources) for resource in self.resources), (
            "physical upstream policy differs"
        )
        assert len(frozenset(r.upstream_id for r in self.fallbacks)) <= 1, "only one physical fallback is supported"
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


def _same_upstream_policy(resource: Resource, resources: tuple[Resource, ...]) -> bool:
    return all(
        (other.scope, other.limits) == (resource.scope, resource.limits)
        for other in resources
        if other.upstream_id == resource.upstream_id
    )


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


class CatalogEntry(FrozenModel):
    resource: Resource
    model: str
    role: Literal["primary", "fallback"]
    active: bool = True


class Snapshot(FrozenModel):
    fingerprint: str
    catalog: tuple[CatalogEntry, ...] = ()
    discovered_at: float | None = None
    catalog_generation: int = 0
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
