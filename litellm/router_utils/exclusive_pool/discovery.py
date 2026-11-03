from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal

from pydantic import Field, TypeAdapter, ValidationError, field_validator

from .models import CatalogEntry, Failure, FrozenModel, Health, Limits, PoolConfig, Resource, Snapshot
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
    exclusive_pool_limits: Limits | None = None


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
        if sum(entry.active and entry.role == "fallback" for entry in entries) > 1:
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
        entry.model_copy(update={"active": True}) != previous[entry.resource.id].model_copy(update={"active": True})
        for entry in catalog
        if entry.resource.id in previous
    ):
        return unavailable()
    retained: Final = tuple(
        entry.model_copy(update={"active": False}) for entry in snapshot.catalog if entry.resource.id not in incoming
    )
    combined: Final = tuple(sorted((*catalog, *retained), key=lambda entry: entry.resource.id))
    updated: Final = snapshot.model_copy(
        update={
            "catalog": combined,
            "discovered_at": now,
            "catalog_generation": snapshot.catalog_generation + 1,
            "health": tuple(
                next(
                    (h for h in snapshot.health if h.resource == entry.resource.id), Health(resource=entry.resource.id)
                )
                for entry in combined
            ),
        }
    )
    try:
        PoolConfig.model_validate(config.resolve(updated).model_dump())
    except ValidationError:
        return unavailable()
    return updated
