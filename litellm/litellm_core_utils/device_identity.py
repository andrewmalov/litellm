from dataclasses import dataclass
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError


@dataclass(frozen=True, slots=True)
class DeviceVerified:
    device_id: str


@dataclass(frozen=True, slots=True)
class DeviceMissing:
    pass


class _Identity(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)
    device_id: Annotated[str, Field(min_length=1, max_length=512)]


def device_identity(value: object, *, explicit_header: str | None = None) -> DeviceVerified | DeviceMissing:
    if explicit_header is not None:
        return _verified(explicit_header)
    if not isinstance(value, str):
        return DeviceMissing()
    try:
        parsed: Final = _Identity.model_validate_json(value)
    except ValidationError:
        return DeviceMissing()
    return _verified(parsed.device_id)


def _verified(value: str) -> DeviceVerified | DeviceMissing:
    if not value.strip() or len(value) > 512 or value != value.strip():
        return DeviceMissing()
    return DeviceVerified(device_id=value)
