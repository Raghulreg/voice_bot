"""Instant micro-acknowledgements while Gemini runs (perceived latency)."""

from __future__ import annotations

import hashlib

BACKCHANNEL_PHRASES: tuple[str, ...] = (
    "Right.",
    "Got it.",
    "Okay.",
    "Mm-hmm.",
    "Sure.",
)


def backchannel_cache_key(index: int) -> str:
    return f"bc:{index}"


def select_backchannel(call_id: str, used: list[str]) -> tuple[str, str]:
    """Return (phrase, cache key bc:N). Avoid repeating the same phrase when possible."""
    used_set = {u.strip().lower() for u in used}
    best_i = 0
    best_score = -1
    for i, phrase in enumerate(BACKCHANNEL_PHRASES):
        score = 10
        if phrase.lower() in used_set:
            score = -10
        seed = int(hashlib.sha256(f"{call_id}:bc:{i}".encode()).hexdigest()[:6], 16)
        score += seed % 3
        if score > best_score:
            best_score = score
            best_i = i
    phrase = BACKCHANNEL_PHRASES[best_i]
    return phrase, backchannel_cache_key(best_i)
