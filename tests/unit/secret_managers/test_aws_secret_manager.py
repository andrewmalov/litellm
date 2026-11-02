import base64

import pytest
from botocore.stub import Stubber

from litellm.secret_managers.aws_secret_manager import AWSKeyManagementService_V2

_CIPHERTEXT = b"ciphertext-bytes"
_KEY_ID = "arn:aws:kms:us-west-2:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab"


@pytest.mark.parametrize(
    "license_env", ["LITELLM_LICENSE", "LITELLM_SECRET_AWS_KMS_LITELLM_LICENSE", "LITELLM_PREMIUM_USER"]
)
@pytest.mark.parametrize(
    ("plaintext", "expected"),
    [
        pytest.param(b"True", True, id="true literal"),
        pytest.param(b"False", False, id="false literal"),
        pytest.param(b" True\n", True, id="padded true literal"),
        pytest.param(b"1", "1", id="integer literal"),
        pytest.param(b"[True]", "[True]", id="list literal"),
        pytest.param(b"'True'", "'True'", id="quoted string literal"),
        pytest.param(b"sk-not-a-literal", "sk-not-a-literal", id="plain secret"),
        pytest.param(b"", "", id="empty secret"),
    ],
)
def test_decrypt_value_turns_only_boolean_literals_into_bools(
    monkeypatch: pytest.MonkeyPatch, plaintext: bytes, expected: bool | str, license_env: str
) -> None:
    monkeypatch.setenv("AWS_REGION_NAME", "us-west-2")
    monkeypatch.delenv("LITELLM_LICENSE", raising=False)
    monkeypatch.delenv("LITELLM_SECRET_AWS_KMS_LITELLM_LICENSE", raising=False)
    monkeypatch.delenv("LITELLM_PREMIUM_USER", raising=False)
    monkeypatch.setenv(license_env, "true" if license_env == "LITELLM_PREMIUM_USER" else "license-for-test")
    monkeypatch.setenv("ENCRYPTED_SETTING", "aws_kms/" + base64.b64encode(_CIPHERTEXT).decode())
    kms = AWSKeyManagementService_V2()

    with Stubber(kms.kms_client) as stubber:
        stubber.add_response("decrypt", {"KeyId": _KEY_ID, "Plaintext": plaintext}, {"CiphertextBlob": _CIPHERTEXT})
        decrypted = kms.decrypt_value(secret_name="ENCRYPTED_SETTING")

    assert decrypted == expected
    assert type(decrypted) is type(expected)
