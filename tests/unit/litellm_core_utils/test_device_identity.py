import pytest

from litellm.litellm_core_utils.device_identity import DeviceMissing, DeviceVerified, device_identity


def test_only_device_is_extracted_from_session_metadata() -> None:
    assert device_identity('{"device_id":"installation","session_id":"first"}') == DeviceVerified(
        device_id="installation"
    )
    assert device_identity('{"device_id":"installation","session_id":"second"}') == DeviceVerified(
        device_id="installation"
    )


@pytest.mark.parametrize(
    "value",
    [None, "", " ", "not-json", "{}", '{"device_id":42}', '{"device_id":true}', '{"device_id":" "}', "[]", "null"],
)
def test_missing_or_malformed_identity_never_falls_back_to_other_fields(value: object) -> None:
    assert isinstance(device_identity(value), DeviceMissing)
