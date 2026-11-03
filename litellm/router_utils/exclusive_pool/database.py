from __future__ import annotations

import logging
from typing import Final

from litellm.proxy.db.routing_prisma_wrapper import WriterPinnedClient, writer_wrapper
from litellm.repositories.model_repository import ModelRepository

from .discovery import CatalogRow
from .models import Failure
from .state import unavailable

_LOGGER: Final = logging.getLogger(__name__)


async def load_catalog() -> tuple[CatalogRow, ...] | Failure:
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        return unavailable()
    try:
        rows: Final = await ModelRepository(WriterPinnedClient(writer_wrapper(prisma_client.db))).table.find_many()
        return tuple(CatalogRow.model_validate(row, from_attributes=True) for row in rows)
    except Exception as exception:  # noqa: BLE001  # database drivers expose backend-specific failures
        _LOGGER.error("Endpoint pool catalog read failed: %s", type(exception).__name__)
        return unavailable()
