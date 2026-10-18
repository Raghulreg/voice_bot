"""Varied acknowledgements and discovery reply lines for natural phone dialogue."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from services.ivr import next_workflow_question
from services.voice_config import is_polite_filler, is_valid_answer

VARIANT_COUNT = 4

# Tier 0: no standalone ack — question only (or embedded lead-in)
SKIP_LEADS: list[str] = [
    "",
]

LIGHT_ACKS: list[str] = [
    "Right.",
    "Got it.",
    "Okay.",
    "Sure.",
    "Mm-hmm.",
]

INFORMAL_ACKS: list[str] = [
    "Makes sense.",
    "I hear you.",
    "Fair enough.",
    "Thanks for that.",
    "Alright.",
]

FORMAL_ACKS: list[str] = [
    "Noted.",
    "Understood.",
]

_FORMAL_RE = re.compile(
    r"^(understood|noted|thank you|thanks|okay|ok|right|got it|sure|confirmed)\.?\s*",
    re.I,
)

# Fallback / LLM sometimes echoes STT: "Thanks — On Yes, sure. — <question>"
_PAYER_ECHO_RE = re.compile(
    r"^(?:thanks\s*[—\-]\s*)?(?:on\s+.+?\s*[—\-]\s*)",
    re.I | re.DOTALL,
)


def strip_robotic_payer_echo(text: str) -> str:
    """Remove read-back of payer speech — unnatural on a phone call."""
    t = (text or "").strip()
    while True:
        n = _PAYER_ECHO_RE.sub("", t, count=1).strip()
        n = re.sub(r"^[—\-]\s*", "", n).strip()
        if n == t:
            break
        t = n
    return t


def normalize_ack(text: str) -> str:
    t = re.sub(r"[^\w\s']", "", (text or "").strip().lower())
    return re.sub(r"\s+", " ", t).strip()


def reply_cache_key(question_index: int, variant: int) -> str:
    return f"reply:{question_index}:v{variant}"


def _tier_for_variant(variant: int) -> str:
    if variant == 0:
        return "skip"
    if variant == 1:
        return "light"
    if variant == 2:
        return "informal"
    return "informal_alt"


def _ack_for_variant(variant: int, question_index: int) -> tuple[str, str]:
    tier = _tier_for_variant(variant)
    if tier == "skip":
        lead = SKIP_LEADS[question_index % len(SKIP_LEADS)]
        return lead.strip(), "skip"
    if tier == "light":
        ack = LIGHT_ACKS[(question_index + variant) % len(LIGHT_ACKS)]
        return ack, "light"
    pool = INFORMAL_ACKS if variant == 2 else INFORMAL_ACKS[::-1]
    ack = pool[(question_index + variant) % len(pool)]
    return ack, "informal"


def build_precache_line(lang: str, question_index: int, variant: int) -> tuple[str, dict[str, Any]]:
    """Fixed lines for TTS warm cache (payer-agnostic)."""
    question = next_workflow_question(lang, question_index)
    ack, tier = _ack_for_variant(variant, question_index)
    if tier == "skip":
        lead = SKIP_LEADS[question_index % len(SKIP_LEADS)]
        if lead:
            text = f"{lead}{question}"
        else:
            text = question
        return text, {
            "ack": "",
            "ack_tier": "skip",
            "variant": variant,
            "cache_key": reply_cache_key(question_index, variant),
        }
    text = f"{ack} {question}"
    return text, {
        "ack": ack,
        "ack_tier": tier,
        "variant": variant,
        "cache_key": reply_cache_key(question_index, variant),
    }


def build_precache_variants(lang: str, question_index: int) -> list[tuple[str, dict[str, Any]]]:
    return [build_precache_line(lang, question_index, v) for v in range(VARIANT_COUNT)]


def _payer_wants_brevity(payer_text: str) -> bool:
    t = (payer_text or "").lower()
    return any(w in t for w in ("busy", "hurry", "quick", "short on time", "one minute"))


def _score_variant(
    variant: int,
    verbal: dict[str, Any],
    payer_text: str,
    used_acks: list[str],
    call_id: str,
    question_index: int,
) -> float:
    ack = verbal.get("ack") or ""
    tier = verbal.get("ack_tier") or "skip"
    norm = normalize_ack(ack)
    used = {normalize_ack(u) for u in used_acks if u}

    if norm and norm in used:
        return -100.0

    score = 10.0
    formal_used = sum(1 for u in used_acks if normalize_ack(u) in {normalize_ack(a) for a in FORMAL_ACKS})
    if tier in ("light", "informal", "informal_alt", "skip"):
        score += 2.0
    if tier == "skip" and len((payer_text or "").split()) >= 12:
        score += 8.0
    if tier == "skip" and is_valid_answer(payer_text):
        score += 4.0
    if tier == "light" and is_polite_filler(payer_text):
        score += 5.0
    if tier == "informal" and len((payer_text or "").split()) >= 6:
        score += 3.0
    if formal_used >= 2 and tier not in ("light", "skip", "informal", "informal_alt"):
        score -= 20.0
    if _payer_wants_brevity(payer_text) and tier == "skip":
        score += 6.0

    # Stable tie-break per call
    seed = int(hashlib.sha256(f"{call_id}:{question_index}:{variant}".encode()).hexdigest()[:8], 16)
    score += (seed % 100) / 1000.0
    return score


def select_reply_variant(
    call_id: str,
    question_index: int,
    payer_text: str,
    used_acks: list[str],
    phrase_meta: dict[str, tuple[str, str]],
    phrase_verbal_meta: dict[str, dict[str, Any]],
    lang: str = "en-US",
) -> tuple[int, str, dict[str, Any], str]:
    """Pick best precached variant for this payer turn."""
    best_v = 0
    best_score = -999.0
    best_text = next_workflow_question(lang, question_index)
    best_verbal: dict[str, Any] = {"ack": "", "ack_tier": "skip", "variant": 0}
    best_key = reply_cache_key(question_index, 0)

    for v in range(VARIANT_COUNT):
        key = reply_cache_key(question_index, v)
        meta = phrase_meta.get(key)
        verbal = phrase_verbal_meta.get(key) or {}
        if not meta:
            text, verbal = build_precache_line(lang, question_index, v)
        else:
            text = meta[0]
        if not verbal:
            _, verbal = build_precache_line(lang, question_index, v)
        score = _score_variant(v, verbal, payer_text, used_acks, call_id, question_index)
        if score > best_score:
            best_score = score
            best_v = v
            best_text = text
            best_verbal = dict(verbal)
            best_key = key

    best_verbal["selected_score"] = round(best_score, 2)
    return best_v, best_text, best_verbal, best_key


def strip_leading_ack(text: str) -> str:
    return _FORMAL_RE.sub("", (text or "").strip()).strip()


def polish_gemini_response(
    call_id: str,
    response_text: str,
    payer_text: str,
    used_acks: list[str],
    *,
    after_backchannel: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Reduce repeated formal acks on LLM lines; optional light bridge."""
    text = strip_robotic_payer_echo((response_text or "").strip())
    used = {normalize_ack(u) for u in used_acks if u}
    lead = text
    if not after_backchannel:
        lead = strip_leading_ack(text)
        for ack in FORMAL_ACKS + LIGHT_ACKS + INFORMAL_ACKS:
            if lead.lower().startswith(ack.lower()):
                lead = strip_leading_ack(lead)
                break
    lead = strip_robotic_payer_echo(lead)

    if after_backchannel:
        return lead or text, {"ack": "", "ack_tier": "skip", "source": "gemini_after_backchannel"}

    if is_valid_answer(payer_text) and len(payer_text.split()) >= 10:
        return lead, {"ack": "", "ack_tier": "skip", "source": "gemini_polish"}

    if len(used) >= 2 and not lead.lower().startswith(tuple(a.lower() for a in LIGHT_ACKS + INFORMAL_ACKS)):
        bridge = LIGHT_ACKS[len(used) % len(LIGHT_ACKS)]
        if normalize_ack(bridge) not in used:
            return f"{bridge} {lead}", {
                "ack": bridge,
                "ack_tier": "light",
                "source": "gemini_polish",
            }

    return lead or text, {"ack": "", "ack_tier": "none", "source": "gemini_polish"}


def fast_agent_reply(lang: str, question_index: int, user_said: str = "") -> str:
    """Legacy helper — composes a single line without session context."""
    text, _ = build_precache_line(lang, question_index, question_index % VARIANT_COUNT)
    if user_said and _tier_for_variant(question_index % VARIANT_COUNT) != "skip":
        return text
    q = next_workflow_question(lang, question_index)
    if is_valid_answer(user_said) and len(user_said.split()) >= 10:
        return q
    return text
