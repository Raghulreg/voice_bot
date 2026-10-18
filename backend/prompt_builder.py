"""Assembles final prompts from templates in prompt.py, claims_scenarios, and tpa_sectors."""

from typing import Any

import prompt as prompts
from claims_scenarios import claim_context_block, format_exemplars_for_prompt, scenario_context_block
from services.profile_registry import chris_directive_for_profile
from services.rcm_dialogue import RcmCollected, format_rcm_gaps_block
from services.ivr import DEFAULT_LANGUAGE
from tpa_sectors import get_tpa_sector, sector_context_block

SUPPORTED_LANGUAGES = {
    "en-US": "English (US)",
}

EMOTION_STRATEGIES = {
    "busy": prompts.EMOTION_STRATEGY_BUSY,
    "impatient": prompts.EMOTION_STRATEGY_IMPATIENT,
    "frustrated": prompts.EMOTION_STRATEGY_FRUSTRATED,
    "confused": prompts.EMOTION_STRATEGY_CONFUSED,
    "defensive": prompts.EMOTION_STRATEGY_DEFENSIVE,
    "supportive": prompts.EMOTION_STRATEGY_SUPPORTIVE,
    "helpful": prompts.EMOTION_STRATEGY_SUPPORTIVE,
    "cooperative": prompts.EMOTION_STRATEGY_SUPPORTIVE,
    "nervous": prompts.EMOTION_STRATEGY_CONFUSED,
    "rushed": prompts.EMOTION_STRATEGY_BUSY,
}


def build_system_prompt(
    agent_name: str,
    preferred_language: str = DEFAULT_LANGUAGE,
    hospital_name: str = "",
    claim_id: str = "",
    patient_name: str = "",
    claim_type: str = "",
    tpa_name: str = "",
    npi: str = "",
    policy_number: str = "",
    claim_age_days: int | None = None,
    tpa_executive_profile: str = "",
    scenario: dict[str, Any] | None = None,
    tpa_sector_id: str = "",
    focus_topics: list[str] | None = None,
    **_extra: Any,
) -> str:
    del _extra
    lang_name = SUPPORTED_LANGUAGES.get(preferred_language, "English (US)")
    claim_ctx = claim_context_block(
        hospital_name,
        claim_id,
        patient_name,
        claim_type,
        tpa_name,
        npi=npi,
        policy_number=policy_number,
        claim_age_days=claim_age_days,
    )
    claim_ctx += "\n\n" + sector_context_block(tpa_sector_id, focus_topics)
    if tpa_executive_profile:
        claim_ctx += f"\n\n## Counterparty profile\nSimulated style: **{tpa_executive_profile}**."
        claim_ctx += f"\n{chris_directive_for_profile(tpa_executive_profile)}"
        claim_ctx += (
            "\nRespond to what they just said — do not read a fixed script. "
            "Stay inside the active TPA sector topics."
        )
    if scenario:
        claim_ctx += f"\n\n{scenario_context_block(scenario)}"

    profile = tpa_executive_profile or (scenario.get("tpa_executive_profile") if scenario else None)
    exemplar_limit = 3 if profile else 2
    sector = get_tpa_sector(tpa_sector_id)
    sector_name = sector["name"] if sector else "General TPA services"
    return prompts.SYSTEM_PROMPT.format(
        agent_name=agent_name,
        hospital_name=hospital_name or "the hospital",
        lang_name=lang_name,
        preferred_language=preferred_language,
        claim_context=claim_ctx,
        exemplars=format_exemplars_for_prompt(limit=exemplar_limit, profile=profile),
        tpa_sector_name=sector_name,
    )


def build_greeting_prompt(
    agent_name: str,
    customer_name: str = "",
    lang_code: str = DEFAULT_LANGUAGE,
    hospital_name: str = "",
    claim_id: str = "",
    patient_name: str = "",
    claim_type: str = "",
    tpa_sector_id: str = "",
) -> str:
    del lang_code
    sector = get_tpa_sector(tpa_sector_id)
    sector_name = sector["name"] if sector else (claim_type or "claims and benefits")
    return prompts.GREETING_PROMPT.format(
        agent_name=agent_name,
        hospital_name=hospital_name or "the hospital",
        claim_id=claim_id or "pending",
        patient_name=patient_name or customer_name or "the patient",
        claim_type=claim_type or "outstanding claim",
        tpa_sector_name=sector_name,
    )


def build_user_turn_prompt(
    user_transcript: str,
    conversation_history: list[dict],
    lang_code: str = DEFAULT_LANGUAGE,
    emotion_hint: str = "",
    last_question: str = "",
    recent_acks: list[str] | None = None,
    after_backchannel: bool = False,
    rcm_collected: RcmCollected | None = None,
    conversation_state_block: str = "",
    tpa_sector_id: str = "",
) -> str:
    del lang_code
    history_lines = []
    for turn in conversation_history[-8:]:
        # You = hospital outbound agent; them = insurance/TPA rep on the line
        role = "You (provider)" if turn["role"] == "assistant" else "Payer rep"
        history_lines.append(f"{role}: {turn['content']}")
    history_text = "\n".join(history_lines) or "(call just started)"

    sector = get_tpa_sector(tpa_sector_id)
    sector_name = sector["name"] if sector else "General TPA services"

    rcm_gaps_block = format_rcm_gaps_block(rcm_collected or RcmCollected())
    if last_question:
        emotion_hint = (
            f"{emotion_hint}\nLast point you raised: \"{last_question}\""
            if emotion_hint
            else f"Last point you raised: \"{last_question}\""
        )

    ack_block = ""
    if recent_acks:
        ack_block = (
            "\n## Acknowledgement policy\n"
            f"Do NOT reuse these acks already spoken this call: {', '.join(recent_acks[:8])}.\n"
            "Prefer no ack or a fresh informal bridge, then one question.\n"
        )
    if after_backchannel:
        ack_block += (
            "\n## This turn\nYou already played a short backchannel. "
            "Do NOT start with another acknowledgement. Do NOT quote them back. "
            "Ask or answer one thing based on their last line — stay in the active TPA sector.\n"
        )

    return prompts.USER_TURN_PROMPT.format(
        history_text=history_text,
        user_transcript=user_transcript,
        rcm_gaps_block=rcm_gaps_block,
        conversation_state_block=conversation_state_block or "",
        emotion_hint=(emotion_hint or "Continue professionally within the TPA sector.") + ack_block,
        tpa_sector_name=sector_name,
    )


def build_simple_turn_prompt(
    agent_name: str,
    user_transcript: str,
    conversation_history: list[dict],
    rcm_collected: RcmCollected | None = None,
    last_question: str = "",
) -> str:
    """Short prompt for a validated single-topic turn. The route still selects the model."""
    history_lines = []
    for turn in conversation_history[-4:]:
        role = "You" if turn.get("role") == "assistant" else "Payer"
        history_lines.append(f"{role}: {turn.get('content', '')}")
    history = "\n".join(history_lines) or "(call just started)"
    gaps = format_rcm_gaps_block(rcm_collected or RcmCollected())
    asked = f'\nYou last asked: "{last_question}"' if last_question else ""
    return (
        f"You are {agent_name}, a hospital biller on a phone call with the payer.\n"
        "Acknowledge their confirmed answer in a few words, then ask ONE next unknown question.\n"
        "If they confirmed the claim is pending or under review, do not ask why or what is causing it.\n"
        "Do not ask for a denial reason unless they said denied, rejected, or partial.\n"
        "Under 25 words. JSON only: "
        '{"response_text":"", "detected_emotion":"neutral", '
        '"conversation_stage":"information_gathering", "advance_workflow":true}\n\n'
        f"{gaps}\n{asked}\n\nConversation:\n{history}\n\nPayer just said: \"{user_transcript}\""
    )


def build_emotion_recovery_prompt(emotion: str) -> str:
    return EMOTION_STRATEGIES.get(emotion, prompts.EMOTION_STRATEGY_DEFAULT)
