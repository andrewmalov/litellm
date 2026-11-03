import os
from typing import Final

import pytest
from e2e_config import unique_marker
from e2e_http import StreamingResponse
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
    def test_independent_clients_share_only_fallback_and_reject_device_conflicts(
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
        invalid: Final = (
            None,
            "",
            "not-json",
            "{}",
            '{"device_id":""}',
            '{"device_id":42}',
            '{"device_id":true}',
            '{"session_id":"session-only","account_uuid":"account-only"}',
        )
        for user_id in invalid:
            _assert_invalid_device(client, resources.key(models=[settings.model]), settings.model, user_id, prompt)

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
            "and invalid devices must not consume them"
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
        assert sorted(reply.status_code for reply in race) == [200, 409], "exactly one device must win first binding"
        for call, reply in zip(racers, race, strict=True):
            _assert_race_result(client, call, reply, settings.fallback_id)

        limited_key: Final = proxy.generate_key(KeyGenerateBody(models=[settings.model], rpm_limit=1))
        resources.defer(lambda: proxy.delete_key(limited_key))
        limited_request: Final = PoolRequest.for_device(
            settings.model, DeviceIdentity(device_id=f"limited-{marker}", session_id="limited"), prompt
        )
        assert deployment_id(client.send(limited_key, limited_request)) == settings.fallback_id
        limited: Final = client.send(limited_key, limited_request)
        assert_pool_error(limited, 429, "client_rate_limit_exceeded")
        assert float(limited.headers.get("retry-after", "0")) > 0, "client limits must report a retry delay"
        assert deployment_id(client.send(calls[2].key, calls[2].request)) == settings.fallback_id


def _assert_invalid_device(client: PoolClient, key: str, model: str, user_id: str | None, prompt: str) -> None:
    response: Final = client.send(key, PoolRequest.with_user_id(model, user_id, prompt))
    assert_pool_error(response, 400, "device_id_required")


def _assert_binding(
    client: PoolClient, call: PoolCall, device: DeviceIdentity, expected: str, model: str, prompt: str
) -> None:
    for session in range(8):
        _assert_session(client, call.key, device, expected, model, prompt, session)
    for address in ("192.0.2.1", "198.51.100.2"):
        assert deployment_id(client.send(call.key, call.request, ip=address)) == expected
    intruder: Final = PoolRequest.for_device(
        model, DeviceIdentity(device_id=f"other-{device.device_id}", session_id="other"), prompt
    )
    assert_pool_error(client.send(call.key, intruder), 409, "key_bound_to_another_device")
    assert deployment_id(client.send(call.key, call.request)) == expected


def _assert_session(
    client: PoolClient, key: str, device: DeviceIdentity, expected: str, model: str, prompt: str, session: int
) -> None:
    changed: Final = PoolRequest.for_device(
        model, DeviceIdentity(device_id=device.device_id, session_id=f"session-{session}"), prompt
    )
    responses: Final = simultaneous(client, (PoolCall(key=key, request=changed),) * 2)
    assert tuple(deployment_id(reply) for reply in responses) == (expected, expected)


def _assert_race_result(client: PoolClient, call: PoolCall, reply: StreamingResponse, fallback_id: str) -> None:
    repeated: Final = client.send(call.key, call.request)
    if reply.status_code == 200:
        assert deployment_id(reply) == fallback_id
        assert deployment_id(repeated) == fallback_id
        return
    assert_pool_error(reply, 409, "key_bound_to_another_device")
    assert_pool_error(repeated, 409, "key_bound_to_another_device")
