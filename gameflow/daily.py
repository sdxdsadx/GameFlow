from __future__ import annotations

from datetime import datetime, timedelta


DAILY_RESET_HOUR = 4


def local_now() -> datetime:
    """Return the current local, timezone-aware time."""
    return datetime.now().astimezone()


def operational_day(moment: datetime | None = None) -> str:
    """Return the local game-day key; a new day starts at 04:00."""
    value = moment if moment is not None else local_now()
    if value.tzinfo is None:
        value = value.astimezone()
    return (value - timedelta(hours=DAILY_RESET_HOUR)).date().isoformat()


def parse_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
