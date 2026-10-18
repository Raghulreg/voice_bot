"""Question-aware validation between raw STT and claim-state updates."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any


_MONTH = (
    r"january|february|march|april|may|june|july|august|"
    r"september|october|november|december"
)
_DATE_RE = re.compile(
    rf"\b(?:(?:{_MONTH})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?|"
    r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|"
    r"today|tomorrow|next\s+(?:monday|tuesday|wednesday|thursday|friday|week|month))\b",
    re.I,
)
_REFERENCE_RE = re.compile(r"\b[A-Z0-9][A-Z0-9-]{2,}\b", re.I)
_FRAGMENT_WORDS = {
    "a", "an", "and", "but", "is", "it", "or", "that", "the", "this",
    "to", "uh", "um", "you",
}
# Hesitation sounds carry no answer content and do not make an answer cut off.
_FILLER_WORDS = {"uh", "um", "uhm", "er", "erm", "hmm", "mm", "ah"}
_AGREEMENT_WORDS = {"yes", "yeah", "yep", "sure", "okay", "ok", "certainly", "absolutely"}
_HELP_REQUEST_RE = re.compile(
    r"\b(?:could|can|would|will)\s+you\s+(?:please\s+)?(?:help|assist)\b"
)
_STATUS_WORDS_RE = re.compile(
    r"\b(?:approved|denied|rejected|paid|pending|processing|under review|partial)\b"
)


@dataclass
class AnswerValidation:
    topic: str = "unknown"
    accepted: bool = False
    ambiguous: bool = True
    reason: str = ""
    clarification: str = ""
    facts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_question(question: str) -> str:
    """Classify the question actually being asked, which is the last sentence."""
    text = " ".join((question or "").split())
    sentences = [part.strip() for part in re.split(r"(?<=[.?!])\s+", text) if part.strip()]
    if len(sentences) > 1:
        last = _classify_question_text(sentences[-1])
        if last != "unknown":
            return last
    return _classify_question_text(text)


def _classify_question_text(question: str) -> str:
    q = (question or "").lower()
    if "remittance" in q or re.search(r"\beob\b", q):
        return "remittance"
    if any(
        word in q for word in ("reviewer", "review team", "adjuster", "queue owner")
    ) or ("who" in q and "review" in q):
        return "reviewer"
    if any(word in q for word in ("document", "medical record", "report", "missing record")):
        return "pending_documents"
    if any(
        word in q
        for word in (
            "payer reference", "control number", "trace number",
            "reference number", "call reference",
        )
    ):
        return "payer_reference"
    if any(word in q for word in ("payment date", "disbursement date", "when will payment")):
        return "payment_date"
    if (
        "review" in q
        and any(word in q for word in ("completion", "complete", "date", "timeline", "when"))
    ):
        return "review_completion_date"
    if any(
        word in q
        for word in (
            "completion date", "expected date", "timeline", "when will", "when is",
        )
    ):
        return "expected_date"
    if any(word in q for word in ("claim status", "current status", "under review", "pending or")):
        return "claim_status"
    if re.search(r"\b(?:approved|denied|rejected|paid|pending)\b", q):
        return "claim_status"
    return "unknown"


_STATUS_QUESTION_TERMS = (
    ("under review", "under_review"),
    ("approved", "approved"),
    ("denied", "denied"),
    ("rejected", "denied"),
    ("pending", "pending"),
    ("paid", "paid"),
)
_VAGUE_REFERENTS = re.compile(
    r"\b(?:that thing|this thing|something|whatever|kind of|sort of|maybe|i think)\b"
)
_AFFIRMATIVE_RE = re.compile(
    r"^(?:yes|yeah|yep|correct|right|that's right|that is right|"
    r"yes it is|yes correct|yes that's correct|yes that is correct)\.?$"
)


def _confirmed_status(question: str, low: str) -> str:
    """Accept a plain yes only when the question named exactly one status."""
    q = (question or "").lower()
    named = {code for phrase, code in _STATUS_QUESTION_TERMS if phrase in q}
    if len(named) != 1 or _VAGUE_REFERENTS.search(low):
        return ""
    normalized = re.sub(r"[^\w\s']", "", low).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return next(iter(named)) if _AFFIRMATIVE_RE.match(normalized) else ""


_ACK_WORDS = {"okay", "ok", "sure", "alright", "hello", "hi", "thanks", "yeah", "yep", "yes"}
_ACK_PHRASES = {"thank you", "yes sure", "yeah sure", "yeah okay", "yes okay", "ok thanks"}


def _last_sentence(question: str) -> str:
    text = " ".join((question or "").split())
    parts = [p.strip() for p in re.split(r"(?<=[.?!])\s+", text) if p.strip()]
    return parts[-1] if parts else text


def _is_help_agreement(question: str, low: str, words: list[str]) -> bool:
    """'Yes' to 'Could you help me ...?' agrees to help; it states no claim fact."""
    if not words or words[0] not in _AGREEMENT_WORDS or len(words) > 6:
        return False
    if not _HELP_REQUEST_RE.search(_last_sentence(question).lower()):
        return False
    return not _STATUS_WORDS_RE.search(low)


def _clarification(topic: str, reason: str) -> str:
    if reason == "help_agreed":
        return "Thank you. Could you please tell me the current status of the claim?"
    if reason == "date_wrong_topic":
        return "Could you tell me whether that date is for the review, the payment, or the remittance?"
    if reason == "incomplete_fragment":
        return "It sounded like that was cut off. Could you please finish the answer?"
    prompt = {
        "claim_status": "Could you please tell me the current status of the claim?",
        "reviewer": "Could you please tell me the reviewer's name or the team handling the claim?",
        "pending_documents": "Could you please tell me whether any documents are still needed?",
        "expected_date": "Could you please tell me the expected date, and what it is for?",
        "payment_date": "Could you please tell me the expected payment date?",
        "review_completion_date": "Could you please tell me when the review is expected to be completed?",
        "payer_reference": "Could you please tell me the reference or claim control number?",
        "remittance": "Could you please tell me whether the remittance is available?",
    }.get(topic, "Could you please say that once more?")
    if reason == "low_confidence":
        return f"I didn't quite catch that. {prompt}"
    if reason == "short_fragment":
        return f"Sorry, I only caught part of that. {prompt}"
    return prompt


def clarification_phrases(topic: str) -> list[str]:
    """Fixed lines this topic may need next, so TTS can be generated ahead of time."""
    if topic == "unknown":
        return []
    phrases = [
        _clarification(topic, ""),
        _clarification(topic, "short_fragment"),
        _clarification(topic, "low_confidence"),
        _clarification(topic, "incomplete_fragment"),
        move_on_after_ambiguity(topic),
    ]
    if topic == "claim_status":
        phrases.append(_clarification(topic, "help_agreed"))
    return phrases


def _is_reviewer_self_reference(words: list[str]) -> bool:
    joined = " ".join(words)
    return joined in {
        "you", "me", "myself", "i am", "its me", "it's me", "this is me",
    }


def _is_clear_document_negative(words: list[str]) -> bool:
    return bool(words) and all(word in {"no", "none", "nothing", "nope"} for word in words)


def dialogue_action(validation: AnswerValidation, already_clarified: bool) -> str:
    """Return clarify, move_on, or advance without sending weak answers to the LLM."""
    if (
        validation.accepted
        and not validation.ambiguous
        and validation.reason != "unclassified_answer"
    ):
        return "advance"
    if already_clarified:
        return "move_on"
    return "clarify"


def _is_acknowledgement(words: list[str]) -> bool:
    if not words or len(words) > 3:
        return False
    return " ".join(words) in _ACK_PHRASES or all(word in _ACK_WORDS for word in words)


def _confirm_low_confidence(explicit: AnswerValidation) -> AnswerValidation:
    """Ask about a recognizable answer without storing a low-confidence fact."""
    facts = explicit.facts
    if facts.get("claim_status"):
        label = str(facts["claim_status"]).replace("_", " ")
        heard = f"Did you say the claim is {label}?"
    elif facts.get("assigned_reviewer"):
        heard = f"Did you say the reviewer or team is {facts['assigned_reviewer']}?"
    elif facts.get("payer_reference"):
        heard = f"Did you say the reference number is {facts['payer_reference']}?"
    elif facts.get("pending_documents_status") == "none":
        heard = "Did you say there are no documents pending?"
    elif facts.get("pending_documents_text"):
        heard = "Did you say there are documents still needed?"
    else:
        heard = "I didn't quite catch that. Could you please repeat it?"
    return AnswerValidation(
        topic=explicit.topic,
        accepted=False,
        ambiguous=True,
        reason="low_confidence_confirm",
        clarification=heard,
    )


def _ambiguous(topic: str, reason: str, facts: dict[str, Any] | None = None) -> AnswerValidation:
    return AnswerValidation(
        topic=topic,
        reason=reason,
        clarification=_clarification(topic, reason),
        facts=facts or {},
    )


def _explicit_answer(
    text: str,
    low: str,
    words: list[str],
    topic: str,
    question: str,
) -> AnswerValidation | None:
    """Return a complete answer when the transcript states the asked fact."""
    if topic == "reviewer" and _is_reviewer_self_reference(words):
        return AnswerValidation(
            topic=topic,
            accepted=True,
            ambiguous=False,
            reason="reviewer_self_reference",
            facts={"assigned_reviewer": "payer representative on this call"},
        )
    if topic == "pending_documents" and (
        _is_clear_document_negative(words)
        or (
            bool(re.search(r"\b(?:no|none|nothing|not any)\b", low))
            and any(word in low for word in ("document", "record", "report", "summary", "bill"))
        )
    ):
        return AnswerValidation(
            topic=topic,
            accepted=True,
            ambiguous=False,
            reason="no_pending_documents",
            facts={"pending_documents_status": "none"},
        )
    if topic == "claim_status":
        if _VAGUE_REFERENTS.search(low):
            return None
        if re.search(
            r"\b(?:not|isn't|wasn't|hasn't|never)\s+(?:been\s+|yet\s+)?(?:approved|denied|rejected|paid)\b",
            low,
        ):
            return None
        confirmed = _confirmed_status(question, low)
        statuses = (
            ("under review", "under_review"),
            ("approved", "approved"),
            ("denied", "denied"),
            ("rejected", "denied"),
            ("pending", "pending"),
            ("processing", "pending"),
            ("paid", "paid"),
        )
        code = confirmed
        if not code:
            for phrase, mapped in statuses:
                if phrase in low:
                    code = mapped
                    break
        if code:
            return AnswerValidation(
                topic=topic,
                accepted=True,
                ambiguous=False,
                reason="confirmed_status" if confirmed else "explicit_status",
                facts={"claim_status": code},
            )
        return None
    if topic == "reviewer":
        if re.search(r"\b(?:under review|approved|denied|pending|paid)\b", low) and not any(
            word in low for word in ("team", "reviewer", "adjuster", "queue")
        ):
            return None
        if re.search(r"\b(?:not assigned|no reviewer|unassigned)\b", low):
            return AnswerValidation(
                topic=topic, accepted=True, ambiguous=False,
                reason="explicit_unassigned",
                facts={"reviewer_status": "unassigned"},
            )
        reviewer_stopwords = {
            "condition", "conditions", "document", "documents", "pending",
            "review", "status", "unknown", "unsure", "maybe", "someone",
            "not", "sure", "no", "idea", "the", "is", "and", "still",
        } | _AGREEMENT_WORDS | _FILLER_WORDS
        if any(word in low for word in ("team", "reviewer", "adjuster", "queue")) or (
            any(word not in reviewer_stopwords and word.isalpha() and len(word) > 2 for word in words)
            and "review" in words
        ):
            return AnswerValidation(
                topic=topic, accepted=True, ambiguous=False,
                reason="explicit_reviewer",
                facts={"assigned_reviewer": text},
            )
        return None
    if topic == "pending_documents" and any(
        word in words for word in ("document", "documents", "record", "records")
    ):
        return AnswerValidation(
            topic=topic, accepted=True, ambiguous=False,
            reason="explicit_documents",
            facts={"pending_documents_text": text},
        )
    if topic == "payer_reference":
        candidates = [
            value for value in _REFERENCE_RE.findall(text)
            if any(char.isdigit() for char in value)
        ]
        if candidates and len(words) >= 1:
            return AnswerValidation(
                topic=topic, accepted=True, ambiguous=False,
                reason="explicit_reference",
                facts={"payer_reference": candidates[-1]},
            )
    return None


def validate_answer(
    transcript: str,
    question: str,
    confidence: float | None = None,
) -> AnswerValidation:
    """Validate STT against the question it is supposed to answer."""
    text = " ".join((transcript or "").strip().split())
    low = text.lower()
    words = re.findall(r"[a-z0-9']+", low)
    topic = classify_question(question)

    if not words:
        return _ambiguous(topic, "empty")
    content = [word for word in words if word not in _FILLER_WORDS]
    if not content:
        return _ambiguous(topic, "short_fragment")
    words = content
    low = re.sub(r"\b(?:uh|um|uhm|er|erm|hmm|mm|ah)\b[,.]?\s*", "", low).strip()
    # Twilio reports 0 when it omits Confidence. That is unavailable evidence,
    # not proof that the transcript is low quality.
    if confidence is not None and confidence <= 0:
        confidence = None
    if _is_help_agreement(question, low, words):
        return _ambiguous(topic, "help_agreed")
    if _is_acknowledgement(words) and not _confirmed_status(question, low):
        return _ambiguous(topic, "acknowledgement")
    low_confidence = confidence is not None and 0 < confidence <= 0.25
    explicit = _explicit_answer(text, low, words, topic, question)
    if explicit is not None:
        if low_confidence:
            return _confirm_low_confidence(explicit)
        return explicit
    if low_confidence and len(words) <= 8:
        return _ambiguous(topic, "low_confidence")
    if topic == "reviewer" and _is_reviewer_self_reference(words):
        return AnswerValidation(
            topic=topic,
            accepted=True,
            ambiguous=False,
            reason="reviewer_self_reference",
            facts={"assigned_reviewer": "payer representative on this call"},
        )
    if topic == "pending_documents" and _is_clear_document_negative(words):
        return AnswerValidation(
            topic=topic,
            accepted=True,
            ambiguous=False,
            reason="no_pending_documents",
            facts={"pending_documents_status": "none"},
        )
    if confidence is not None and 0 < confidence <= 0.25 and len(words) <= 4:
        return _ambiguous(topic, "low_confidence")
    single_word_answers = {"yes", "no", "none"}
    if topic == "claim_status":
        single_word_answers |= {
            "approved", "denied", "rejected", "pending", "paid",
            "processing", "correct", "right", "yeah", "yep",
        }
    if (
        len(words) == 1
        and words[0] not in single_word_answers
    ) or all(word in _FRAGMENT_WORDS for word in words):
        return _ambiguous(topic, "short_fragment")
    if words[-1] in _FRAGMENT_WORDS or re.match(r"^(?:and|but|is)\b", low):
        return _ambiguous(topic, "incomplete_fragment")

    date_match = _DATE_RE.search(text)
    if date_match and topic not in {
        "expected_date",
        "payment_date",
        "review_completion_date",
        "remittance",
    }:
        return _ambiguous(
            topic,
            "date_wrong_topic",
            {"candidate_date": date_match.group(0)},
        )

    if any(phrase in low for phrase in ("i'll check", "let me check", "one moment", "please hold")):
        return AnswerValidation(topic=topic, accepted=True, ambiguous=False, reason="waiting")

    if topic == "claim_status":
        if _VAGUE_REFERENTS.search(low):
            return _ambiguous(topic, "vague_status_confirmation")
        if re.search(r"\b(?:not|isn't|wasn't|hasn't|never)\s+(?:been\s+|yet\s+)?(?:approved|denied|rejected|paid)\b", low):
            return _ambiguous(topic, "negated_status")
        confirmed = _confirmed_status(question, low)
        if confirmed:
            return AnswerValidation(
                topic=topic,
                accepted=True,
                ambiguous=False,
                reason="confirmed_status",
                facts={"claim_status": confirmed},
            )
        statuses = (
            ("under review", "under_review"),
            ("approved", "approved"),
            ("denied", "denied"),
            ("rejected", "denied"),
            ("pending", "pending"),
            ("processing", "pending"),
            ("paid", "paid"),
            ("partial", "partial"),
        )
        for phrase, code in statuses:
            if phrase in low:
                return AnswerValidation(
                    topic=topic,
                    accepted=True,
                    ambiguous=False,
                    reason="explicit_status",
                    facts={"claim_status": code},
                )
        return _ambiguous(topic, "missing_status")

    if topic == "reviewer":
        if re.search(r"\b(?:not assigned|no reviewer|unassigned)\b", low):
            return AnswerValidation(
                topic=topic, accepted=True, ambiguous=False,
                reason="explicit_unassigned",
                facts={"reviewer_status": "unassigned"},
            )
        if any(word in low for word in ("team", "reviewer", "adjuster", "queue")):
            return AnswerValidation(
                topic=topic, accepted=True, ambiguous=False,
                reason="explicit_reviewer",
                facts={"assigned_reviewer": text},
            )
        # A concise proper-name answer is natural for this question, but a
        # generic phrase must not be turned into a person or team.
        reviewer_stopwords = {
            "condition", "conditions", "document", "documents", "pending",
            "review", "status", "unknown", "unsure", "maybe", "someone",
            "not", "sure", "no", "idea",
        } | _AGREEMENT_WORDS | _FILLER_WORDS
        if (
            1 <= len(words) <= 3
            and all(word.isalpha() and len(word) > 1 for word in words)
            and not reviewer_stopwords.intersection(words)
        ):
            return AnswerValidation(
                topic=topic, accepted=True, ambiguous=False,
                reason="reviewer_name",
                facts={"assigned_reviewer": text},
            )
        return _ambiguous(topic, "missing_reviewer")

    if topic == "pending_documents":
        negative = bool(re.search(r"\b(?:no|none|nothing|not any)\b", low))
        document_evidence = any(
            word in low
            for word in ("document", "record", "report", "summary", "bill", "authorization")
        )
        if negative and (document_evidence or low in {"no", "none", "nothing"}):
            return AnswerValidation(
                topic=topic, accepted=True, ambiguous=False,
                reason="no_pending_documents",
                facts={"pending_documents_status": "none"},
            )
        if document_evidence and any(
            word in low for word in ("need", "missing", "pending", "required", "send")
        ):
            return AnswerValidation(
                topic=topic, accepted=True, ambiguous=False,
                reason="explicit_documents",
                facts={"pending_documents_text": text},
            )
        return _ambiguous(topic, "unclear_documents")

    if topic in ("payment_date", "review_completion_date"):
        if date_match:
            fact_key = (
                "expected_date"
                if topic == "payment_date"
                else "review_completion_date"
            )
            return AnswerValidation(
                topic=topic, accepted=True, ambiguous=False,
                reason="explicit_date",
                facts={fact_key: date_match.group(0)},
            )
        return _ambiguous(topic, "missing_date")

    if topic == "expected_date":
        if date_match:
            return _ambiguous(
                topic,
                "date_wrong_topic",
                {"candidate_date": date_match.group(0)},
            )
        return _ambiguous(topic, "missing_date")

    if topic == "payer_reference":
        candidates = _REFERENCE_RE.findall(text)
        candidates = [
            value for value in candidates
            if any(char.isdigit() for char in value)
        ]
        if candidates:
            return AnswerValidation(
                topic=topic, accepted=True, ambiguous=False,
                reason="explicit_reference",
                facts={"payer_reference": candidates[-1]},
            )
        return _ambiguous(topic, "missing_reference")

    if topic == "remittance":
        facts: dict[str, Any] = {}
        if date_match:
            facts["remittance_expected_date"] = date_match.group(0)
        if any(phrase in low for phrase in ("not available", "not ready", "pending", "no")):
            facts["remittance_status"] = "not_available"
        elif any(word in low for word in ("available", "issued", "generated", "sent", "yes")):
            facts["remittance_status"] = "available"
        if facts:
            return AnswerValidation(
                topic=topic, accepted=True, ambiguous=False,
                reason="explicit_remittance",
                facts=facts,
            )
        return _ambiguous(topic, "unclear_remittance")

    return AnswerValidation(
        topic=topic,
        accepted=True,
        ambiguous=False,
        reason="unclassified_answer",
    )


def resolve_pending_date(candidate_date: str, transcript: str) -> AnswerValidation:
    """Associate a preserved date only after the payer identifies what it means."""
    low = (transcript or "").lower()
    if "remittance" in low or re.search(r"\beob\b", low):
        facts = {"remittance_expected_date": candidate_date}
        topic = "remittance"
    elif "payment" in low or "disbursement" in low:
        facts = {"expected_date": candidate_date}
        topic = "expected_date"
    elif "review" in low or "completion" in low:
        facts = {"review_completion_date": candidate_date}
        topic = "expected_date"
    else:
        return _ambiguous("expected_date", "date_wrong_topic", {"candidate_date": candidate_date})
    return AnswerValidation(
        topic=topic,
        accepted=True,
        ambiguous=False,
        reason="resolved_date_context",
        facts=facts,
    )


def extract_explicit_secondary_facts(transcript: str) -> dict[str, Any]:
    """Keep clearly labelled facts from a natural multi-part answer."""
    text = " ".join((transcript or "").strip().split())
    low = text.lower()
    facts: dict[str, Any] = {}
    date_match = _DATE_RE.search(text)
    if date_match:
        date_value = date_match.group(0)
        if "remittance" in low or re.search(r"\beob\b", low):
            facts["remittance_expected_date"] = date_value
        elif "payment" in low or "disbursement" in low:
            facts["expected_date"] = date_value
        elif "review" in low and any(
            word in low for word in ("complete", "completion", "expected")
        ):
            facts["review_completion_date"] = date_value

    ref_match = re.search(
        r"(?:payer|payment|control|trace)?\s*reference\s*(?:number|#)?\s*(?:is|:)?\s*"
        r"([A-Z0-9][A-Z0-9-]{2,})",
        text,
        re.I,
    )
    if ref_match and any(char.isdigit() for char in ref_match.group(1)):
        facts["payer_reference"] = ref_match.group(1)

    if re.search(r"\bno\s+(?:pending|missing|required)\s+documents?\b", low):
        facts["pending_documents_status"] = "none"
    if "remittance" in low or re.search(r"\beob\b", low):
        if any(phrase in low for phrase in ("not available", "not ready", "pending")):
            facts["remittance_status"] = "not_available"
        elif any(word in low for word in ("available", "issued", "generated", "sent")):
            facts["remittance_status"] = "available"
    return facts


def move_on_after_ambiguity(topic: str) -> str:
    """Bound clarification loops without treating the unclear answer as a fact."""
    return {
        "claim_status": (
            "I couldn't confirm the status, so I'll leave it unresolved. "
            "Could you please tell me who is reviewing the claim?"
        ),
        "reviewer": (
            "I couldn't confirm the reviewer, so I'll leave that unresolved. "
            "Could you please tell me whether any documents are still needed?"
        ),
        "pending_documents": (
            "I couldn't confirm the documents, so I'll leave that unresolved. "
            "What is the expected review completion date?"
        ),
        "expected_date": (
            "I couldn't confirm what that date means, so I won't record it. "
            "What is the payer reference or control number?"
        ),
        "payment_date": (
            "I couldn't confirm the payment date, so I'll leave it unresolved. "
            "What is the payer reference or control number?"
        ),
        "review_completion_date": (
            "I couldn't confirm the review date, so I'll leave it unresolved. "
            "What is the payer reference or control number?"
        ),
        "payer_reference": (
            "I couldn't confirm the reference number, so I'll leave it unresolved. "
            "Is the EOB or remittance available?"
        ),
        "remittance": (
            "I couldn't confirm the remittance details, so I'll leave them unresolved. "
            "Is there a follow-up date for this claim?"
        ),
    }.get(
        topic,
        "I couldn't confirm that answer, so I'll leave it unresolved for this call.",
    )
