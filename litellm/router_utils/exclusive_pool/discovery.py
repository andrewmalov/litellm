from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal

from pydantic import Field, TypeAdapter, ValidationError, field_validator

from .models import CatalogEntry, Failure, FrozenModel, Health, PoolConfig, Resource, ResourceLimits, Snapshot
from .state import unavailable

_METADATA: Final = TypeAdapter(Mapping[str, object])


class CatalogRow(FrozenModel):
    model_id: str
    model_name: str
    model_info: Mapping[str, object]
    blocked: bool = False

    @field_validator("model_info", mode="before")
    @classmethod
    def parse_metadata(cls, value: object) -> Mapping[str, object]:
        if value is None:
            return {}
        return _METADATA.validate_json(value) if isinstance(value, str) else _METADATA.validate_python(value)


class _Labels(FrozenModel):
    exclusive_pool_role: Literal["primary", "fallback"]
    exclusive_upstream_id: str | None = Field(default=None, min_length=1)
    exclusive_quota_scope: str | None = Field(default=None, min_length=1)
    exclusive_pool_limits: ResourceLimits | None = None


def _entry(row: CatalogRow, config: PoolConfig) -> CatalogEntry:
    labels: Final = _Labels.model_validate(row.model_info)
    return CatalogEntry(
        resource=Resource(
            id=row.model_id,
            upstream_id=labels.exclusive_upstream_id or row.model_id,
            quota_scope=labels.exclusive_quota_scope,
            limits=labels.exclusive_pool_limits
            or (config.fallback_limits if labels.exclusive_pool_role == "fallback" else config.primary_limits),
        ),
        model=row.model_name,
        role=labels.exclusive_pool_role,
        active=not row.blocked,
    )


def discover(rows: tuple[CatalogRow, ...], config: PoolConfig) -> tuple[CatalogEntry, ...] | Failure:
    try:
        entries: Final = tuple(
            _entry(row, config) for row in rows if row.model_info.get("exclusive_pool") == config.pool_id
        )
        if len(frozenset(e.resource.id for e in entries)) != len(entries):
            return unavailable()
        if any(entry.role == "primary" and entry.resource.limits.parallel is None for entry in entries):
            return unavailable()
        if (
            len(frozenset(entry.resource.upstream_id for entry in entries if entry.active and entry.role == "fallback"))
            > 1
        ):
            return unavailable()
        return tuple(sorted(entries, key=lambda entry: entry.resource.id))
    except ValidationError:
        return unavailable()


def reconcile(
    snapshot: Snapshot, config: PoolConfig, catalog: tuple[CatalogEntry, ...], now: float
) -> Snapshot | Failure:
    if not config.discovery or snapshot.fingerprint != config.fingerprint:
        return unavailable()
    incoming: Final = {entry.resource.id: entry for entry in catalog}
    previous: Final = {entry.resource.id: entry for entry in snapshot.catalog}
    if any(
        not _compatible_update(entry, previous[entry.resource.id]) for entry in catalog if entry.resource.id in previous
    ):
        return unavailable()
    fallback_by_upstream: Final = {entry.resource.upstream_id: entry for entry in catalog if entry.role == "fallback"}
    retained: Final = tuple(
        _retire(entry, fallback_by_upstream.get(entry.resource.upstream_id))
        for entry in snapshot.catalog
        if entry.resource.id not in incoming
    )
    combined: Final = tuple(sorted((*catalog, *retained), key=lambda entry: entry.resource.id))
    if any(not _consistent_role(entry, combined) for entry in combined):
        return unavailable()
    updated: Final = snapshot.model_copy(
        update={
            "catalog": combined,
            "discovered_at": now,
            "catalog_generation": snapshot.catalog_generation + 1,
            "health": tuple(_inherited_health(entry, snapshot) for entry in combined),
        }
    )
    try:
        PoolConfig.model_validate(config.resolve(updated).model_dump())
    except ValidationError:
        return unavailable()
    return updated


def _compatible_update(entry: CatalogEntry, previous: CatalogEntry) -> bool:
    original: Final = previous.model_copy(update={"active": entry.active})
    if entry.role == "fallback" and previous.role == "fallback":
        return (
            entry.model_dump()
            == original.model_copy(
                update={"resource": _with_parallel(previous.resource, entry.resource.limits.parallel)}
            ).model_dump()
        )
    return entry.model_dump() == original.model_dump()


def _with_parallel(resource: Resource, parallel: int | None) -> Resource:
    return resource.model_copy(update={"limits": resource.limits.model_copy(update={"parallel": parallel})})


def _retire(entry: CatalogEntry, fallback: CatalogEntry | None) -> CatalogEntry:
    resource: Final = (
        _with_parallel(entry.resource, fallback.resource.limits.parallel)
        if entry.role == "fallback" and fallback is not None
        else entry.resource
    )
    return entry.model_copy(update={"active": False, "resource": resource})


def _consistent_role(entry: CatalogEntry, catalog: tuple[CatalogEntry, ...]) -> bool:
    return all(
        other.role == entry.role for other in catalog if other.resource.upstream_id == entry.resource.upstream_id
    )


def _inherited_health(entry: CatalogEntry, snapshot: Snapshot) -> Health:
    siblings: Final = frozenset(
        previous.resource.id
        for previous in snapshot.catalog
        if previous.resource.upstream_id == entry.resource.upstream_id
    )
    inherited: Final = next((health for health in snapshot.health if health.resource in siblings), None)
    return (
        inherited.model_copy(update={"resource": entry.resource.id})
        if inherited
        else Health(resource=entry.resource.id)
    )
