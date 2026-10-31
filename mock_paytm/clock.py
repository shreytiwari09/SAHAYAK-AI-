"""A controllable clock so demos and tests can fast-forward time (e.g. past a settlement's T+1).

All timestamps are timezone-aware UTC.
"""

from datetime import UTC, datetime, timedelta

_offset = timedelta(0)


def now() -> datetime:
    return datetime.now(UTC) + _offset


def advance(seconds: float) -> datetime:
    global _offset
    _offset += timedelta(seconds=seconds)
    return now()


def reset() -> None:
    global _offset
    _offset = timedelta(0)
