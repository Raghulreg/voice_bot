"""Caller tone for delivery only.

Frustration, confusion, and impatience change how the agent speaks.
They never create or edit claim facts, status, or amounts.
"""

from __future__ import annotations

import re
from typing import Any

_TONE_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "frustrated",
        (
            "frustrat",
            "ridiculous",
            "unacceptable",
            "this is taking forever",
            "waste of time",
            "sick of",
            "annoyed",
            "angry",
            "tired of",
        ),
    ),
    (
        "confused",
        (
            "confused",
            "don't understand",
            "do not understand",
            "what do you mean",
            "i'm lost",
            "im lost",
            "huh",
            "come again",
            "that doesn't make sense",
            "not following",
        ),
    ),
    (
        "impatient",
        (
            "i'm busy",
            "im busy",
            "in a hurry",
            "hurry up",
            "just tell me",
            "get to the point",
            "no time",
        ),
    ),
)

_GUIDANCE = {
    "frustrated": (
        "Tone guidance: the caller sounds frustrated. Acknowledge that in a few calm words, "
        "then give one clear next step. Do not change any claim fact."
    ),
    "confused": (
        "Tone guidance: the caller sounds unsure. Rephrase the same point in simpler words "
        "and ask one short question. Do not change any claim fact."
    ),
    "impatient": (
        "Tone guidance: the caller is in a hurry. Skip small talk and ask or answer one thing. "
        "Do not change any claim fact."
    ),
}


def detect_smart_tone(transcript: str, *, enabled: bool) -> dict[str, Any]:
    """Return a delivery tone. `facts` is always empty."""
    if not enabled:
        return {
            "enabled": False,
            "operational": False,
            "tone": None,
            "changes_claim_facts": False,
            "facts": {},
            "reason": "Smart Tone Detection is off.",
        }
    low = re.sub(r"\s+", " ", (transcript or "").lower()).strip()
    tone = None
    for name, cues in _TONE_CUES:
        if any(cue in low for cue in cues):
            tone = name
            break
    return {
        "enabled": True,
        "operational": True,
        "tone": tone,
        "changes_claim_facts": False,
        "facts": {},
        "reason": (
            f"Delivery tone is {tone}. Claim facts are unchanged."
            if tone
            else "No frustration, confusion, or impatience cue. Claim facts are unchanged."
        ),
    }


def delivery_prompt(tone: str) -> str:
    return _GUIDANCE.get((tone or "").strip().lower(), "")
