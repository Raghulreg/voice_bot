"""IVR and scripted payer claims follow-up workflow — US English only."""

DEFAULT_LANGUAGE = "en-US"
DEFAULT_LANGUAGE_NAME = "English (US)"


def build_ivr_script(
    agent_name: str,
    customer_name: str = "",
    hospital_name: str = "",
    claim_id: str = "",
    payer_name: str = "",
) -> str:
    """Short English connect message (language selection removed — US English only)."""
    hospital = hospital_name or "the hospital"
    payer = payer_name or "the insurance provider"
    claim_part = f" for claim {claim_id}" if claim_id else ""
    return (
        f"Connecting you to {payer} provider services{claim_part} "
        f"on behalf of {hospital}. One moment please."
    )


def confirmation_message(lang_code: str = DEFAULT_LANGUAGE, lang_name: str = DEFAULT_LANGUAGE_NAME) -> str:
    """Legacy helper — AI greets from prompt.py; keep a short English hold message."""
    return "One moment please."


def language_from_digit(digit: str, fallback: str = DEFAULT_LANGUAGE) -> tuple[str, str]:
    """US English only — ignore keypad digit."""
    return DEFAULT_LANGUAGE, DEFAULT_LANGUAGE_NAME


def fallback_greeting(
    agent_name: str,
    customer_name: str,
    lang_code: str = DEFAULT_LANGUAGE,
    hospital_name: str = "",
    claim_id: str = "",
    patient_name: str = "",
    claim_type: str = "",
    npi: str = "",
    policy_number: str = "",
) -> str:
    """Outbound provider-office open (Gemini fallback). Keep short."""
    del customer_name, lang_code, claim_id, patient_name, npi, policy_number
    hospital = hospital_name or "the hospital"
    purpose = claim_type or "a benefits and claims matter"
    return (
        f"Hi, this is {agent_name} calling from {hospital} regarding {purpose}. "
        f"Could you help me with that?"
    )


def workflow_questions(lang_code: str = DEFAULT_LANGUAGE) -> list[str]:
    """Ordered payer claims follow-up questions (status → extraction → RCM closure)."""
    return [
        "Could you confirm the current status of this claim — pending, denied, or approved — and the payer reference number?",
        "If denied or partially paid, what is the denial code or reason, and what is required to complete review?",
        "Are there any pending documents we must submit — discharge summary, itemized bill, operative notes, or EOB?",
        "What is the expected payment date or medical review completion timeline for this claim?",
        "Is an Explanation of Benefits available, and can you send the remittance advice to our billing team?",
        "If the claim remains unresolved, what follow-up callback date should we schedule, and who is the assigned adjuster?",
        "May I confirm the resolution code for our RCM system update and that this interaction is documented on the claim file?",
    ]


def discovery_questions(lang_code: str = DEFAULT_LANGUAGE) -> list[str]:
    return workflow_questions(lang_code)


def fallback_discovery_questions(lang_code: str = DEFAULT_LANGUAGE) -> list[str]:
    return workflow_questions(lang_code)


def next_workflow_question(lang_code: str = DEFAULT_LANGUAGE, index: int = 0) -> str:
    questions = workflow_questions(lang_code)
    return questions[min(index, len(questions) - 1)]


def next_discovery_question(lang_code: str = DEFAULT_LANGUAGE, index: int = 0) -> str:
    return next_workflow_question(lang_code, index)


def fast_agent_reply(lang_code: str = DEFAULT_LANGUAGE, question_index: int = 0, user_said: str = "") -> str:
    from services.verbal_composer import fast_agent_reply as _compose

    return _compose(lang_code, question_index, user_said)


def acknowledgement(lang_code: str = DEFAULT_LANGUAGE, question_index: int = 0) -> str:
    from services.verbal_composer import build_precache_line

    text, meta = build_precache_line(lang_code, question_index, question_index % 4)
    return meta.get("ack") or ""


def fast_agent_reply_legacy(lang_code: str = DEFAULT_LANGUAGE, question_index: int = 0, user_said: str = "") -> str:
    q = next_workflow_question(lang_code, question_index)
    if user_said:
        return f"{acknowledgement(lang_code, question_index)} {q}"
    return q


def fallback_agent_reply(
    agent_name: str,
    lang_code: str = DEFAULT_LANGUAGE,
    turn_count: int = 0,
    user_said: str = "",
) -> str:
    return fast_agent_reply(lang_code, turn_count, user_said)
