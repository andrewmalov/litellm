from typing import Final

import pytest
from pydantic import ValidationError

from litellm.router_utils.exclusive_pool.models import (
    Admission,
    Granted,
    Health,
    Limits,
    PoolConfig,
    Resource,
    Snapshot,
)
from litellm.router_utils.exclusive_pool.state import acquire


def test_state_from_the_single_model_release_remains_admissible() -> None:
    config: Final = PoolConfig(
        pool_id="legacy",
        models=("m",),
        primaries=(Resource(id="a", upstream_id="a"),),
        fallback=Resource(id="f", upstream_id="f"),
        client_limits=Limits(),
    )
    previous: Final = Snapshot(
        fingerprint="8ec01cbd7ad85f41fb59c61dcc92761ddad77d7c89b9f5686d4eff900601f27b",
        health=(Health(resource="a"), Health(resource="f")),
    )
    result: Final = acquire(
        previous, config, Admission(client="one", device="d", operation="first", tokens=1, eligible=("a", "f")), 1
    )
    assert isinstance(result, Granted) and result.resource == "a"


@pytest.mark.parametrize(
    "header", ["User-Agent", "Authorization", "X-Forwarded-For", "Cookie", "Host", "Forwarded", "X-Real-IP"]
)
def test_device_header_must_be_dedicated_to_device_identity(header: str) -> None:
    values: Final = {
        "pool_id": "p",
        "models": ("m",),
        "primaries": (Resource(id="a", upstream_id="a"),),
        "fallback": Resource(id="f", upstream_id="f"),
        "client_limits": Limits(),
    }
    with pytest.raises(ValidationError):
        PoolConfig.model_validate({**values, "device_header": header})
    assert PoolConfig.model_validate({**values, "device_header": "X-Device-ID"}).device_header == "X-Device-ID"
