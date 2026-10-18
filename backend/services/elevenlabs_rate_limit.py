"""One ElevenLabs request at a time, with a short capped retry for HTTP 429."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

ELEVENLABS_CONCURRENCY = 1
CACHE_QUEUE_MAX = 8
MAX_RATE_LIMIT_RETRIES = 2
MAX_BACKOFF_S = 8.0
BASE_BACKOFF_S = 0.5


def retry_after_seconds(retry_after: str | None, attempt: int) -> float:
    """Prefer Retry-After. Otherwise use capped exponential backoff."""
    header = (retry_after or "").strip()
    if header:
        try:
            return min(MAX_BACKOFF_S, max(0.0, float(header)))
        except ValueError:
            try:
                when = parsedate_to_datetime(header)
            except (TypeError, ValueError, IndexError):
                when = None
            if when is not None:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                delay = (when - datetime.now(timezone.utc)).total_seconds()
                return min(MAX_BACKOFF_S, max(0.0, delay))
    return min(MAX_BACKOFF_S, BASE_BACKOFF_S * (2**attempt))


async def rate_limit_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class ElevenLabsRequestSlot:
    """Serializes ElevenLabs HTTP calls. Live speech is counted while it waits."""

    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(ELEVENLABS_CONCURRENCY)
        self.live_waiting = 0

    def hold(self, *, live: bool) -> "_Hold":
        return _Hold(self, live)


class _Hold:
    def __init__(self, slot: ElevenLabsRequestSlot, live: bool) -> None:
        self._slot = slot
        self._live = live

    async def __aenter__(self) -> "_Hold":
        if self._live:
            self._slot.live_waiting += 1
        await self._slot._semaphore.acquire()
        if self._live:
            self._slot.live_waiting -= 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._slot._semaphore.release()
        return False


request_slot = ElevenLabsRequestSlot()
