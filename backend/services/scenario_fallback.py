"""Profile-aware fallbacks when Gemini returns no speakable line."""

from __future__ import annotations

from services.rcm_dialogue import RcmCollected, dynamic_fallback_question


def scenario_fallback_reply(
    tpa_executive_profile: str,
    payer_said: str,
    collected: RcmCollected,
    lang: str = "en-US",
    last_question: str = "",
) -> str:
    """One contextual question from open RCM gaps — not a fixed workflow index."""
    del tpa_executive_profile, lang
    return dynamic_fallback_question(collected, payer_said, last_question=last_question)
