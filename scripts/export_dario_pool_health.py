from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

_LATEST_HEALTH: Final = """
SELECT i.client_slug, h.checked_at, h.status, h.oauth
FROM instances i
JOIN instance_health h ON h.id = (
    SELECT id FROM instance_health
    WHERE instance_id = i.id
    ORDER BY checked_at DESC, id DESC LIMIT 1
)
WHERE i.dario_health_monitoring = 1
"""


def _status(status: str | None, oauth: str | None) -> Literal["ok", "unavailable", "rate_limited"]:
    if status == "rate_limited":
        return "rate_limited"
    if status == "ok" and oauth not in ("broken", "expired", "none"):
        return "ok"
    return "unavailable"


def export_health(path: Path) -> str:
    connection: Final = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows: Final = connection.execute(_LATEST_HEALTH)
        return json.dumps(
            tuple(
                {
                    "upstream": upstream,
                    "observed_at": datetime.fromisoformat(checked_at).timestamp(),
                    "status": _status(status, oauth),
                }
                for upstream, checked_at, status, oauth in rows
            )
        )
    finally:
        connection.close()


if __name__ == "__main__":
    parser: Final = argparse.ArgumentParser(description="Export existing Dario Farm health observations")
    parser.add_argument("database", type=Path)
    database: Final = getattr(parser.parse_args(), "database")
    if isinstance(database, Path):
        sys.stdout.write(export_health(database) + "\n")
