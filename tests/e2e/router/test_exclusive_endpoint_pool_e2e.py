import os
from typing import Final

import pytest
from e2e_config import unique_marker
from e2e_metadata import Domain, Subject, meta
from exclusive_pool_client import (
    DeviceIdentity,
    PoolCall,
    PoolClient,
    PoolRequest,
    PoolSettings,
    assert_pool_error,
    deployment_id,
    simultaneous,
)
from lifecycle import ResourceManager
from models import KeyGenerateBody
from proxy_client import ProxyClient


@pytest.mark.e2e
@pytest.mark.exclusive_pool
@pytest.mark.quiet_stack
class TestExclusiveEndpointPool:
    @meta(Subject(domain=Domain.ROUTING))
    def test_independent_keys_share_only_fallback_and_keep_assignments_across_device_changes(
        self, proxy: ProxyClient, resources: ResourceManager
    ) -> None:
        """At two balanced replicas, 16 independent calls miss a particular replica with probability 2^-16."""
        settings: Final = PoolSettings(
            model=os.environ["E2E_EXCLUSIVE_POOL_MODEL"],
            primary_ids=(os.environ["E2E_EXCLUSIVE_POOL_PRIMARY_1"], os.environ["E2E_EXCLUSIVE_POOL_PRIMARY_2"]),
            fallback_id=os.environ["E2E_EXCLUSIVE_POOL_FALLBACK"],
        )
        client: Final = PoolClient(proxy.transport)
        marker: Final = unique_marker()
        prompt: Final = f"Reply with OK. {marker}"
        keys: Final = tuple(resources.key(models=[settings.model]) for _ in range(4))
        devices: Final = tuple(
            DeviceIdentity(device_id=f"{marker}-{index}", session_id="initial") for index in range(4)
        )
        calls: Final = tuple(
            PoolCall(key=key, request=PoolRequest.for_device(settings.model, device, prompt))
            for key, device in zip(keys, devices, strict=True)
        )
        initial: Final = tuple(deployment_id(client.send(call.key, call.request)) for call in calls)
        assert initial[:2] in (settings.primary_ids, tuple(reversed(settings.primary_ids))), (
            "the first two valid keys must receive different primaries in cyclic order, "
            "independently of device identity"
        )
        assert initial[2:] == (settings.fallback_id, settings.fallback_id), "idle fallback clients must share capacity"

        assert tuple(deployment_id(reply) for reply in simultaneous(client, calls)) == initial

        for call, device, expected in zip(calls, devices, initial, strict=True):
            _assert_binding(client, call, device, expected, settings.model, prompt)

        race_key: Final = resources.key(models=[settings.model])
        racers: Final = tuple(
            PoolCall(
                key=race_key,
                request=PoolRequest.for_device(
                    settings.model, DeviceIdentity(device_id=f"racer-{marker}-{index}", session_id="race"), prompt
                ),
            )
            for index in range(2)
        )
        race: Final = simultaneous(client, racers)
        assert tuple(deployment_id(reply) for reply in race) == (settings.fallback_id,) * len(racers)
        for call in racers:
            assert deployment_id(client.send(call.key, call.request)) == settings.fallback_id

        limited_key: Final = proxy.generate_key(KeyGenerateBody(models=[settings.model], rpm_limit=1))
        resources.defer(lambda: proxy.delete_key(limited_key))
        limited_request: Final = PoolRequest.for_device(
            settings.model, DeviceIdentity(device_id=f"limited-{marker}", session_id="limited"), prompt
        )
        assert deployment_id(client.send(limited_key, limited_request)) == settings.fallback_id
        limited: Final = client.send(limited_key, PoolRequest.with_user_id(settings.model, None, prompt))
        assert_pool_error(limited, 429, "client_rate_limit_exceeded")
        assert float(limited.headers.get("retry-after", "0")) > 0, "client limits must report a retry delay"
        assert deployment_id(client.send(calls[2].key, calls[2].request)) == settings.fallback_id


def _assert_binding(
    client: PoolClient, call: PoolCall, device: DeviceIdentity, expected: str, model: str, prompt: str
) -> None:
    for session in range(8):
        _assert_session(client, call.key, device, expected, model, prompt, session)
    for address in ("192.0.2.1", "198.51.100.2"):
        assert deployment_id(client.send(call.key, call.request, ip=address)) == expected
    for user_id in (None, "", "not-json", "{}", '{"device_id":42}', '{"session_id":"session-only"}'):
        assert deployment_id(client.send(call.key, PoolRequest.with_user_id(model, user_id, prompt))) == expected


def _assert_session(
    client: PoolClient, key: str, device: DeviceIdentity, expected: str, model: str, prompt: str, session: int
) -> None:
    changed: Final = PoolRequest.for_device(
        model, DeviceIdentity(device_id=f"{device.device_id}-{session}", session_id=f"session-{session}"), prompt
    )
    responses: Final = simultaneous(client, (PoolCall(key=key, request=changed),) * 2)
    assert tuple(deployment_id(reply) for reply in responses) == (expected, expected)
