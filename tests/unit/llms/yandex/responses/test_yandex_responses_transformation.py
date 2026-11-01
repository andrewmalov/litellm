from typing import Final

import litellm
from litellm.llms.yandex.responses.transformation import YandexResponsesConfig
from litellm.types.utils import LlmProviders
from litellm.utils import ProviderConfigManager


def test_yandex_responses_config_is_resolved_through_public_registry() -> None:
    config: Final = ProviderConfigManager.get_provider_responses_api_config(
        provider=LlmProviders.YANDEX, model="gpt://test-folder/aliceai-llm/latest"
    )
    assert isinstance(config, YandexResponsesConfig)
    assert litellm.YandexResponsesConfig is YandexResponsesConfig
    assert config.custom_llm_provider == LlmProviders.YANDEX
