import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Final
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from litellm.proxy._types import GenerateKeyRequest, LitellmUserRoles, UpdateKeyRequest, UserAPIKeyAuth
from litellm.proxy.management_helpers.key_budget_policy import (
    enforce_key_budget_authority,
    enforce_policy_authority,
    inherited_budget_windows,
    synchronize_inherited_key_windows,
)


def _metadata(short: float = 10, week: float = 100) -> dict[str, object]:
    return {
        "key_budget_policy": {
            "budget_limits": [
                {"budget_duration": "5h", "max_budget": short},
                {"budget_duration": "7d", "max_budget": week},
            ]
        }
    }


def test_team_policy_overrides_org_without_resetting_existing_windows():
    reset: Final = datetime(2030, 1, 7, tzinfo=timezone.utc)
    windows: Final = inherited_budget_windows(
        team_id="team-a",
        team_metadata=_metadata(20, 200),
        organization_metadata=_metadata(),
        existing_windows=[{"budget_duration": "5h", "max_budget": 1, "reset_at": reset}],
        next_reset=lambda duration: reset,
    )
    assert windows is not None
    assert [(w.budget_duration, w.max_budget, w.reset_at) for w in windows] == [
        ("5h", 20, reset),
        ("7d", 200, reset),
    ]


def test_unteamed_key_uses_organization_policy():
    reset: Final = datetime(2030, 1, 7, tzinfo=timezone.utc)
    windows: Final = inherited_budget_windows(
        team_id=None,
        team_metadata=_metadata(20, 200),
        organization_metadata=_metadata(),
        existing_windows=None,
        next_reset=lambda duration: reset,
    )
    assert windows is not None
    assert tuple(w.max_budget for w in windows) == (10, 100)


def test_unconfigured_team_does_not_inherit_organization_default():
    assert (
        inherited_budget_windows(
            team_id="team-a",
            team_metadata={},
            organization_metadata=_metadata(),
            existing_windows=None,
            next_reset=lambda duration: datetime(2030, 1, 7, tzinfo=timezone.utc),
        )
        is None
    )


@pytest.mark.parametrize(
    "policy",
    [
        {},
        {"budget_limits": []},
        {"budget_limits": [{"budget_duration": "5h", "max_budget": 1}]},
        _metadata(-1)["key_budget_policy"],
        _metadata(float("nan"))["key_budget_policy"],
        _metadata(float("inf"))["key_budget_policy"],
    ],
)
def test_invalid_policy_is_rejected_even_for_proxy_admin(policy: object):
    with pytest.raises(HTTPException) as error:
        enforce_policy_authority(
            requested_metadata={"key_budget_policy": policy},
            existing_metadata=None,
            metadata_present=True,
            role=LitellmUserRoles.PROXY_ADMIN,
        )
    assert error.value.status_code == 400


@pytest.mark.parametrize("metadata", [{}, None, {"key_budget_policy": None}, _metadata(20)])
def test_delegated_admin_cannot_replace_or_remove_policy(metadata: object):
    with pytest.raises(HTTPException) as error:
        enforce_policy_authority(
            requested_metadata=metadata,
            existing_metadata=_metadata(),
            metadata_present=True,
            role=LitellmUserRoles.ORG_ADMIN,
        )
    assert error.value.status_code == 403


def test_unchanged_policy_can_be_resent_by_org_admin():
    enforce_policy_authority(
        requested_metadata=_metadata(),
        existing_metadata=_metadata(),
        metadata_present=True,
        role=LitellmUserRoles.ORG_ADMIN,
    )

    with pytest.raises(HTTPException) as error:
        enforce_policy_authority(
            requested_metadata=_metadata(20), existing_metadata=_metadata(),
            metadata_present=True, role=LitellmUserRoles.ORG_ADMIN,
        )
    assert error.value.status_code == 403


@pytest.mark.parametrize(
    "values",
    [
        {"budget_limits": []},
        {"budget_limits": None},
        {"spend": 0},
        {"max_budget": None},
        {"budget_duration": "7d"},
        {"organization_id": None},
        {"organization_id": "other-org"},
    ],
)
def test_org_admin_cannot_modify_budget_or_organization(values: dict[str, object]):
    with pytest.raises(HTTPException) as error:
        enforce_key_budget_authority(
            data=UpdateKeyRequest.model_validate({"key": "key-a", **values}),
            caller=UserAPIKeyAuth(user_role=LitellmUserRoles.ORG_ADMIN),
            existing_org_id="org-a",
        )
    assert error.value.status_code == 403


def test_org_admin_must_scope_new_key():
    with pytest.raises(HTTPException) as error:
        enforce_key_budget_authority(
            data=GenerateKeyRequest(),
            caller=UserAPIKeyAuth(user_role=LitellmUserRoles.ORG_ADMIN),
        )
    assert error.value.status_code == 403


def test_org_admin_may_create_team_key_without_budget_overrides():
    enforce_key_budget_authority(
        data=GenerateKeyRequest(team_id="team-a"),
        caller=UserAPIKeyAuth(user_role=LitellmUserRoles.ORG_ADMIN),
    )

    with pytest.raises(HTTPException) as error:
        enforce_key_budget_authority(
            data=GenerateKeyRequest(team_id="team-a", budget_limits=[]),
            caller=UserAPIKeyAuth(user_role=LitellmUserRoles.ORG_ADMIN),
        )
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_policy_update_preserves_fresh_database_reset_over_stale_cached_reset():
    cached_reset: Final = datetime(2030, 1, 7, tzinfo=timezone.utc)
    database_reset: Final = datetime(2030, 1, 14, tzinfo=timezone.utc)
    row: Final = {
        "token": "key-a",
        "team_id": "team-a",
        "organization_id": "org-a",
        "budget_limits": json.dumps(
            [
                {"budget_duration": "5h", "max_budget": 1, "reset_at": database_reset.isoformat()},
                {"budget_duration": "7d", "max_budget": 2, "reset_at": database_reset.isoformat()},
            ]
        ),
    }
    table: Final = SimpleNamespace(find_unique=AsyncMock(return_value=row), update_many=AsyncMock(return_value=1))
    windows: Final = await synchronize_inherited_key_windows(
        token="key-a",
        team_id="team-a",
        org_id="org-a",
        team_metadata=_metadata(),
        organization_metadata=None,
        existing_windows=[{"budget_duration": "5h", "max_budget": 1, "reset_at": cached_reset}],
        table=table,
        next_reset=lambda duration: cached_reset,
    )
    assert windows is not None
    assert tuple(w.reset_at for w in windows) == (database_reset, database_reset)
    assert tuple(w.max_budget for w in windows) == (10, 100)
    assert json.loads(table.update_many.call_args.kwargs["data"]["budget_limits"]) == [
        w.model_dump(mode="json") for w in windows
    ]
    assert table.update_many.call_args.kwargs["where"]["token"] == row["token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_changed,changed_count", [(True, 1), (False, 0)])
async def test_policy_update_rejects_concurrent_scope_or_window_changes(scope_changed: bool, changed_count: int):
    reset: Final = datetime(2030, 1, 7, tzinfo=timezone.utc)
    table: Final = SimpleNamespace(
        find_unique=AsyncMock(return_value={"token": "key-a", "team_id": "other" if scope_changed else "team-a"}),
        update_many=AsyncMock(return_value=changed_count),
    )
    with pytest.raises(HTTPException) as error:
        await synchronize_inherited_key_windows(
            token="key-a",
            team_id="team-a",
            org_id=None,
            team_metadata=_metadata(),
            organization_metadata=None,
            existing_windows=None,
            table=table,
            next_reset=lambda duration: reset,
        )
    assert error.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted", ["null", None, [{"budget_duration": "5h", "max_budget": 1}]])
async def test_policy_materializes_windows_previously_cleared_to_json_null(persisted: object):
    reset: Final = datetime(2030, 1, 7, tzinfo=timezone.utc)
    table: Final = SimpleNamespace(
        find_unique=AsyncMock(return_value={"token": "key-a", "team_id": "team-a", "budget_limits": persisted}),
        update_many=AsyncMock(return_value=1),
    )
    windows: Final = await synchronize_inherited_key_windows(
        token="key-a",
        team_id="team-a",
        org_id=None,
        team_metadata=_metadata(),
        organization_metadata=None,
        existing_windows=None,
        table=table,
        next_reset=lambda duration: reset,
    )
    assert windows is not None
    assert tuple(w.max_budget for w in windows) == (10, 100)


@pytest.mark.parametrize("role", [LitellmUserRoles.INTERNAL_USER, LitellmUserRoles.ORG_ADMIN])
def test_managed_policy_cannot_be_overridden_by_either_global_role(role: str):
    with pytest.raises(HTTPException) as error:
        enforce_key_budget_authority(
            data=UpdateKeyRequest(key="key-a", budget_limits=[]),
            caller=UserAPIKeyAuth(user_role=role),
            managed=True,
        )
    assert error.value.status_code == 403


@pytest.mark.parametrize("request_type", [GenerateKeyRequest, UpdateKeyRequest])
@pytest.mark.parametrize("settings", [{"auto_rotate": True}, {"rotation_interval": "1h"}])
def test_policy_keys_reject_delegated_automatic_rotation(
    request_type: type[GenerateKeyRequest] | type[UpdateKeyRequest], settings: dict[str, object]
):
    with pytest.raises(HTTPException) as error:
        enforce_key_budget_authority(
            data=request_type(key="key-a", team_id="team-a", **settings),
            caller=UserAPIKeyAuth(user_role=LitellmUserRoles.INTERNAL_USER),
            managed=True,
        )
    assert error.value.status_code == 403
