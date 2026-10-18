"""Single registry for all 20 TPA executive profiles — UI, prompts, exemplars, fallbacks."""

from __future__ import annotations

from typing import Literal

# Must match claims_scenarios.TPA_EXECUTIVE_PROFILES and frontend TPA_PROFILES exactly.
ALL_TPA_EXECUTIVE_PROFILES: tuple[str, ...] = (
    "Helpful Executive",
    "Busy Executive",
    "Experienced Executive",
    "New Executive",
    "Cooperative Executive",
    "Uncooperative Executive",
    "Process Driven Executive",
    "Rule Focused Executive",
    "Empathetic Executive",
    "Impatient Executive",
    "Escalation Focused Executive",
    "Supervisor Type",
    "Technical Executive",
    "Incomplete Information Executive",
    "Slow Response Executive",
    "Fast Response Executive",
    "Knowledgeable Executive",
    "Confused Executive",
    "Defensive Executive",
    "Polite Executive",
)

CategoryId = Literal["collaborative", "challenging", "process", "experience"]
FallbackStyle = Literal["rushed", "firm", "warm", "process", "patient", "neutral"]

PROFILE_CATEGORY: dict[str, CategoryId] = {
    "Helpful Executive": "collaborative",
    "Cooperative Executive": "collaborative",
    "Empathetic Executive": "collaborative",
    "Polite Executive": "collaborative",
    "Knowledgeable Executive": "collaborative",
    "Busy Executive": "challenging",
    "Uncooperative Executive": "challenging",
    "Impatient Executive": "challenging",
    "Defensive Executive": "challenging",
    "Confused Executive": "challenging",
    "Process Driven Executive": "process",
    "Rule Focused Executive": "process",
    "Technical Executive": "process",
    "Escalation Focused Executive": "process",
    "Supervisor Type": "process",
    "Experienced Executive": "experience",
    "New Executive": "experience",
    "Slow Response Executive": "experience",
    "Fast Response Executive": "experience",
    "Incomplete Information Executive": "experience",
}

# How Chris (hospital caller) should adapt — one block per profile for Gemini system prompt.
CHRIS_DIRECTIVES: dict[str, str] = {
    "Helpful Executive": (
        "Payer rep is HELPFUL. Chris: warm brief thanks, confirm details, one clear question, ≤22 words."
    ),
    "Cooperative Executive": (
        "Payer rep is COOPERATIVE. Chris: friendly professional tone, confirmation loop, one question."
    ),
    "Empathetic Executive": (
        "Payer rep is EMPATHETIC. Chris: acknowledge hospital urgency briefly, one supportive question."
    ),
    "Polite Executive": (
        "Payer rep is POLITE. Chris: match courtesy, stay concise, one structured question."
    ),
    "Knowledgeable Executive": (
        "Payer rep is KNOWLEDGEABLE. Chris: skip basics, ask precise technical or policy detail, one question."
    ),
    "Busy Executive": (
        "Payer rep is BUSY/RUSHED. Chris: ≤18 words, no small talk, batch claim ID + patient, one sharp ask."
    ),
    "Uncooperative Executive": (
        "Payer rep is UNCOOPERATIVE. Chris: calm, fact-only, cite claim data, one firm question, no debate."
    ),
    "Impatient Executive": (
        "Payer rep is IMPATIENT. Chris: ≤15 words, one ask only, never repeat prior questions."
    ),
    "Defensive Executive": (
        "Payer rep is DEFENSIVE. Chris: compliance tone, no blame, document facts, one neutral question."
    ),
    "Confused Executive": (
        "Payer rep is CONFUSED. Chris: one simple question, offer to repeat claim number slowly, patient tone."
    ),
    "Process Driven Executive": (
        "Payer rep is PROCESS-DRIVEN. Chris: reference their workflow step, ask what document or stage is next."
    ),
    "Rule Focused Executive": (
        "Payer rep is RULE-FOCUSED. Chris: ask which policy clause applies, request rule or denial code."
    ),
    "Technical Executive": (
        "Payer rep is TECHNICAL. Chris: use codes (CPT/ICD/denial), itemized bill language, one technical ask."
    ),
    "Escalation Focused Executive": (
        "Payer rep escalates quickly. Chris: stay composed, offer supervisor email/ticket, one escalation path ask."
    ),
    "Supervisor Type": (
        "Payer rep is SUPERVISOR-level. Chris: lead with facts, respect authority, one decision-oriented question."
    ),
    "Experienced Executive": (
        "Payer rep is EXPERIENCED. Chris: professional peer tone, no over-explaining, one targeted ask."
    ),
    "New Executive": (
        "Payer rep is NEW. Chris: plain language, one step at a time, spell claim number if needed."
    ),
    "Slow Response Executive": (
        "Payer rep is SLOW. Chris: allow pauses in wording, one question, offer to hold or callback."
    ),
    "Fast Response Executive": (
        "Payer rep is FAST. Chris: match pace, ≤18 words, one question, no filler."
    ),
    "Incomplete Information Executive": (
        "Payer rep gives PARTIAL info. Chris: acknowledge gap, one clarifying follow-up on missing field."
    ),
}

CATEGORY_FALLBACK_PREFIX: dict[FallbackStyle, str] = {
    "rushed": "{reflect} quickly:",
    "firm": "{reflect} for our records,",
    "warm": "Thanks — {reflect}",
    "process": "{reflect} per your process,",
    "patient": "{reflect} when you can,",
    "neutral": "{reflect}",
}

PROFILE_FALLBACK_STYLE: dict[str, FallbackStyle] = {
    "Busy Executive": "rushed",
    "Impatient Executive": "rushed",
    "Fast Response Executive": "rushed",
    "Uncooperative Executive": "firm",
    "Defensive Executive": "firm",
    "Escalation Focused Executive": "firm",
    "Supervisor Type": "firm",
    "Helpful Executive": "warm",
    "Cooperative Executive": "warm",
    "Empathetic Executive": "warm",
    "Polite Executive": "warm",
    "Knowledgeable Executive": "warm",
    "Process Driven Executive": "process",
    "Rule Focused Executive": "process",
    "Technical Executive": "process",
    "New Executive": "patient",
    "Confused Executive": "patient",
    "Slow Response Executive": "patient",
    "Incomplete Information Executive": "patient",
    "Experienced Executive": "neutral",
}


def chris_directive_for_profile(profile: str) -> str:
    if profile in CHRIS_DIRECTIVES:
        return CHRIS_DIRECTIVES[profile]
    cat = PROFILE_CATEGORY.get(profile, "collaborative")
    return (
        f"Payer rep category: {cat}. Chris: reflect their last answer, one question, ≤22 words, "
        "no scripted checklist if already answered."
    )


def fallback_style_for_profile(profile: str) -> FallbackStyle:
    return PROFILE_FALLBACK_STYLE.get(profile, "neutral")


def validate_registry_against_claims(claims_profiles: list[str]) -> list[str]:
    """Return list of validation errors (empty if ok)."""
    errors: list[str] = []
    claims_set = set(claims_profiles)
    registry_set = set(ALL_TPA_EXECUTIVE_PROFILES)
    if claims_set != registry_set:
        errors.append(f"claims vs registry mismatch: only_in_claims={claims_set - registry_set}")
        errors.append(f"only_in_registry={registry_set - claims_set}")
    for p in ALL_TPA_EXECUTIVE_PROFILES:
        if p not in PROFILE_CATEGORY:
            errors.append(f"missing category: {p}")
        if p not in CHRIS_DIRECTIVES:
            errors.append(f"missing directive: {p}")
        if p not in PROFILE_FALLBACK_STYLE:
            errors.append(f"missing fallback style: {p}")
    return errors
