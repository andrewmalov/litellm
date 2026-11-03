from typing import Final

import pytest
from pydantic import ValidationError

from litellm.router_utils.exclusive_pool.models import Limits, PoolConfig, Resource


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
