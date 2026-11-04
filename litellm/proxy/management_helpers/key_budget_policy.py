import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Final, Literal, Protocol

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator
from typing_extensions import Self

from litellm.proxy._types import (
    BudgetLimitEntry,
    GenerateKeyRequest,
    LitellmUserRoles,
    RegenerateKeyRequest,
    UpdateKeyRequest,
    UserAPIKeyAuth,
)


class KeyBudgetWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    budget_duration: Literal["5h", "7d"]
    max_budget: float = Field(gt=0, allow_inf_nan=False)


class KeyBudgetPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    budget_limits: tuple[KeyBudgetWindow, KeyBudgetWindow]

    @model_validator(mode="after")
    def unique_durations(self) -> Self:
        if frozenset(w.budget_duration for w in self.budget_limits) != frozenset({"5h", "7d"}):
            raise ValueError("key_budget_policy requires one 5h and one 7d window")
        return self


_METADATA: Final = TypeAdapter(dict[str, object])
_JSON_VALUE: Final = TypeAdapter(object)
_WINDOWS: Final = TypeAdapter(tuple[BudgetLimitEntry, ...])
_BUDGET_FIELDS: Final = frozenset(
    {
        "budget_limits",
        "auto_rotate",
        "rotation_interval",
        "max_budget",
        "budget_duration",
        "budget_id",
        "soft_budget",
        "spend",
        "model_max_budget",
        "budget_fallbacks",
        "temp_budget_increase",
        "temp_budget_expiry",
    }
)


class _MetadataOwner(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    metadata: Mapping[str, object] | None = None


def policy_metadata(entity: object) -> Mapping[str, object] | None:
    return _MetadataOwner.model_validate(entity).metadata if entity is not None else None


def key_window_entries(windows: object) -> tuple[BudgetLimitEntry, ...]:
    return _WINDOWS.validate_python(windows or ())


def stored_key_window_entries(entity: object) -> tuple[BudgetLimitEntry, ...]:
    raw: Final[object] = getattr(entity, "budget_limits", None)
    return key_window_entries(_JSON_VALUE.validate_json(raw) if isinstance(raw, str) else raw)


def _policy_value(metadata: object) -> object:
    if metadata is None:
        return None
    return _METADATA.validate_python(metadata).get("key_budget_policy")


def _parse_policy(value: object) -> KeyBudgetPolicy | None:
    if value is None:
        return None
    try:
        return KeyBudgetPolicy.model_validate(value)
    except ValidationError:
        raise HTTPException(400, "key_budget_policy requires positive finite budgets for exactly 5h and 7d") from None


def enforce_policy_authority(
    *,
    requested_metadata: object,
    existing_metadata: object,
    metadata_present: bool,
    role: str | None,
) -> None:
    if not metadata_present:
        return
    requested: Final = _policy_value(requested_metadata)
    stored: Final = _policy_value(existing_metadata)
    if role != LitellmUserRoles.PROXY_ADMIN and requested != stored:
        raise HTTPException(403, "Only proxy admins can change key_budget_policy")
    _parse_policy(requested)


def enforce_key_budget_authority(
    *,
    data: GenerateKeyRequest | UpdateKeyRequest,
    caller: UserAPIKeyAuth,
    existing_org_id: str | None = None,
    managed: bool = False,
    requested_fields: frozenset[str] | None = None,
) -> None:
    if caller.user_role == LitellmUserRoles.PROXY_ADMIN:
        return
    if not managed and caller.user_role != LitellmUserRoles.ORG_ADMIN:
        return
    fields: Final = requested_fields if requested_fields is not None else frozenset(data.model_fields_set)
    if fields & _BUDGET_FIELDS:
        raise HTTPException(403, "Organization admins must inherit key budgets from a team or organization policy")
    if isinstance(data, (UpdateKeyRequest, RegenerateKeyRequest)):
        if "organization_id" in data.model_fields_set and data.organization_id != existing_org_id:
            raise HTTPException(403, "Organization admins cannot change a key's organization")
        return
    if not data.team_id and not data.organization_id:
        raise HTTPException(403, "Organization admins must specify team_id or organization_id")


def inherited_budget_windows(
    *,
    team_id: str | None,
    team_metadata: object,
    organization_metadata: object,
    existing_windows: object,
    next_reset: Callable[[str], datetime],
) -> tuple[BudgetLimitEntry, ...] | None:
    policy: Final = _parse_policy(_policy_value(team_metadata if team_id else organization_metadata))
    if policy is None:
        return None
    stored: Final = _WINDOWS.validate_python(existing_windows or ())
    resets: Final = {w.budget_duration: w.reset_at for w in stored if w.reset_at is not None}
    return tuple(
        BudgetLimitEntry(
            budget_duration=window.budget_duration,
            max_budget=window.max_budget,
            reset_at=resets.get(window.budget_duration) or next_reset(window.budget_duration),
        )
        for window in policy.budget_limits
    )


class KeyWindowTable(Protocol):
    async def find_unique(self, where: Mapping[str, object]) -> object: ...

    async def update_many(self, data: Mapping[str, object], where: Mapping[str, object]) -> int: ...


class _StoredKey(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    token: str
    team_id: str | None = None
    organization_id: str | None = None
    budget_limits: str | tuple[BudgetLimitEntry, ...] | None = None
    updated_at: datetime | None = None


async def synchronize_inherited_key_windows(
    *,
    token: str,
    team_id: str | None,
    org_id: str | None,
    team_metadata: object,
    organization_metadata: object,
    existing_windows: object,
    table: KeyWindowTable,
    next_reset: Callable[[str], datetime],
) -> tuple[BudgetLimitEntry, ...] | None:
    desired: Final = inherited_budget_windows(
        team_id=team_id,
        team_metadata=team_metadata,
        organization_metadata=organization_metadata,
        existing_windows=existing_windows,
        next_reset=next_reset,
    )
    if desired is None or desired == _WINDOWS.validate_python(existing_windows or ()):
        return desired
    row: Final = _StoredKey.model_validate(await table.find_unique(where={"token": token}))
    if row.team_id != team_id or (not team_id and row.organization_id != org_id):
        raise HTTPException(409, "Key scope changed while applying budget policy; retry the request")
    stored_windows: Final = key_window_entries(
        _JSON_VALUE.validate_json(row.budget_limits) if isinstance(row.budget_limits, str) else row.budget_limits
    )
    effective: Final = inherited_budget_windows(
        team_id=team_id,
        team_metadata=team_metadata,
        organization_metadata=organization_metadata,
        existing_windows=stored_windows,
        next_reset=next_reset,
    )
    if effective is None:
        return None
    if effective == stored_windows:
        return effective
    changed: Final = await table.update_many(
        where={
            "token": token,
            "team_id": row.team_id,
            "organization_id": row.organization_id,
            "updated_at": row.updated_at,
        },
        data={"budget_limits": json.dumps([w.model_dump(mode="json") for w in effective])},
    )
    if changed != 1:
        raise HTTPException(409, "Key windows changed while applying budget policy; retry the request")
    return effective


def key_organization_id(entity: object) -> str | None:
    primary: Final[object] = getattr(entity, "organization_id", None)
    fallback: Final[object] = getattr(entity, "org_id", None)
    return primary if isinstance(primary, str) else fallback if isinstance(fallback, str) else None


def requests_key_budget_change(data: GenerateKeyRequest | UpdateKeyRequest) -> bool:
    return bool(data.model_fields_set & (_BUDGET_FIELDS | frozenset({"team_id", "organization_id"})))


class _OrgMembership(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    organization_id: str
    user_role: str


_MEMBERSHIPS: Final = TypeAdapter(tuple[_OrgMembership, ...])


def admin_organization_ids(user: object) -> frozenset[str]:
    raw: Final[object] = getattr(user, "organization_memberships", None)
    memberships: Final = _MEMBERSHIPS.validate_python(raw if isinstance(raw, (list, tuple)) else ())
    return frozenset(m.organization_id for m in memberships if m.user_role == LitellmUserRoles.ORG_ADMIN)
