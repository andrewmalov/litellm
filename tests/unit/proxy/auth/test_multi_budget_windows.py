"""
Unit tests for multi-budget-window enforcement on API keys.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Final
from unittest.mock import AsyncMock, patch

import pytest

import litellm
from litellm.caching.caching import DualCache
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.auth.auth_checks import _virtual_key_multi_budget_check
from litellm.proxy.management_helpers.key_budget_policy import inherited_budget_windows


def _make_valid_token(**kwargs) -> UserAPIKeyAuth:
    defaults = dict(
        token="sk-test-token",
        key_name="test",
        spend=0.0,
        max_budget=None,
        budget_limits=[],
    )
    defaults.update(kwargs)
    return UserAPIKeyAuth(**defaults)


@pytest.mark.asyncio
async def test_no_budget_limits_passes():
    """Keys with empty budget_limits should pass without raising."""
    token = _make_valid_token(budget_limits=[])
    # Should not raise
    await _virtual_key_multi_budget_check(valid_token=token)


@pytest.mark.asyncio
async def test_under_budget_passes():
    """Key with spend under all windows should pass."""
    token = _make_valid_token(
        budget_limits=[
            {"budget_duration": "24h", "max_budget": 10.0, "reset_at": None},
            {"budget_duration": "30d", "max_budget": 100.0, "reset_at": None},
        ]
    )
    with patch(
        "litellm.proxy.proxy_server.get_current_spend",
        new_callable=AsyncMock,
        return_value=1.0,  # well under both windows
    ):
        await _virtual_key_multi_budget_check(valid_token=token)


@pytest.mark.asyncio
async def test_over_first_window_raises():
    """Key exceeding the first (daily) window should raise BudgetExceededError."""
    token = _make_valid_token(
        budget_limits=[
            {"budget_duration": "24h", "max_budget": 5.0, "reset_at": None},
            {"budget_duration": "30d", "max_budget": 100.0, "reset_at": None},
        ]
    )

    spend_by_window = [6.0, 6.0]  # over daily, under monthly

    call_count = 0

    async def fake_get_spend(counter_key, fallback_spend, max_budget=None, **kwargs):
        nonlocal call_count
        val = spend_by_window[call_count]
        call_count += 1
        return val

    with patch("litellm.proxy.proxy_server.get_current_spend", side_effect=fake_get_spend):
        with pytest.raises(litellm.BudgetExceededError) as exc_info:
            await _virtual_key_multi_budget_check(valid_token=token)

    err = exc_info.value
    assert err.status_code == 422
    assert "24h" in str(err)
    assert "Key over" in str(err)


@pytest.mark.asyncio
async def test_over_second_window_raises():
    """Key exceeding only the monthly window should raise BudgetExceededError referencing 30d."""
    token = _make_valid_token(
        budget_limits=[
            {"budget_duration": "24h", "max_budget": 50.0, "reset_at": None},
            {"budget_duration": "30d", "max_budget": 5.0, "reset_at": None},
        ]
    )

    spend_by_window = [1.0, 10.0]  # under daily, over monthly

    call_count = 0

    async def fake_get_spend(counter_key, fallback_spend, max_budget=None, **kwargs):
        nonlocal call_count
        val = spend_by_window[call_count]
        call_count += 1
        return val

    with patch("litellm.proxy.proxy_server.get_current_spend", side_effect=fake_get_spend):
        with pytest.raises(litellm.BudgetExceededError) as exc_info:
            await _virtual_key_multi_budget_check(valid_token=token)

    err = exc_info.value
    assert err.status_code == 422
    assert "30d" in str(err)


@pytest.mark.asyncio
async def test_budget_limit_entry_objects_coerced():
    """BudgetLimitEntry Pydantic objects (not dicts) must be handled without KeyError.

    While budget_limits is normally serialized as List[dict], the auth check must
    tolerate BudgetLimitEntry objects in case they arrive without prior serialization.
    """
    from litellm.proxy._types import BudgetLimitEntry

    token = _make_valid_token(budget_limits=[])
    # Bypass Pydantic validation to simulate BudgetLimitEntry objects reaching the check
    object.__setattr__(
        token,
        "budget_limits",
        [BudgetLimitEntry(budget_duration="24h", max_budget=10.0)],
    )

    with patch(
        "litellm.proxy.proxy_server.get_current_spend",
        new_callable=AsyncMock,
        return_value=1.0,
    ):
        # Should not raise TypeError / KeyError — model_dump() coerces the object
        await _virtual_key_multi_budget_check(valid_token=token)


@pytest.mark.asyncio
@pytest.mark.parametrize("short_duration", ["1d", "5h"])
@pytest.mark.parametrize("exhausted", ["short", "week", None])
async def test_inherited_policy_enforces_shared_short_and_weekly_spend_on_both_workers(
    short_duration: str, exhausted: str | None
) -> None:
    reset: Final = datetime(2030, 1, 7, tzinfo=timezone.utc)
    windows: Final = inherited_budget_windows(
        team_id="team-daily",
        team_metadata={
            "key_budget_policy": {
                "budget_limits": [
                    {"budget_duration": short_duration, "max_budget": 30},
                    {"budget_duration": "7d", "max_budget": 150},
                ]
            }
        },
        organization_metadata=None,
        existing_windows=None,
        next_reset=lambda duration: reset,
    )
    assert windows is not None
    token: Final = UserAPIKeyAuth(
        token="shared-key", budget_limits=[window.model_dump(mode="json") for window in windows]
    )
    counter_values: Final = {
        f"spend:key:shared-key:window:{short_duration}": 30 if exhausted == "short" else 1,
        "spend:key:shared-key:window:7d": 150 if exhausted == "week" else 1,
    }

    async def read_redis(key: str) -> float:
        return counter_values[key]

    redis: Final = SimpleNamespace(async_get_cache=AsyncMock(side_effect=read_redis))
    workers: Final = (DualCache(redis_cache=redis), DualCache(redis_cache=redis))
    for worker in workers:
        worker.in_memory_cache.set_cache(key=f"spend:key:shared-key:window:{short_duration}", value=0)
        worker.in_memory_cache.set_cache(key="spend:key:shared-key:window:7d", value=0)
        with (
            patch("litellm.proxy.proxy_server.spend_counter_cache", worker),
            patch("litellm.proxy.proxy_server.prisma_client", None),
        ):
            if exhausted is None:
                await _virtual_key_multi_budget_check(valid_token=token)
            else:
                with pytest.raises(litellm.BudgetExceededError) as error:
                    await _virtual_key_multi_budget_check(valid_token=token)
                assert error.value.status_code == 422
                assert (short_duration if exhausted == "short" else "7d") in str(error.value)
                assert (
                    error.value.current_cost
                    == counter_values[f"spend:key:shared-key:window:{short_duration if exhausted == 'short' else '7d'}"]
                )
