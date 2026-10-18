"""TTS voice and repeat-detection settings — US English only."""

import re

LANGUAGE_SPEAKERS: dict[str, str] = {
    "en-US": "default",
}

FALLBACK_SPEAKERS: list[str] = []

TTS_PACE = 1.0
TTS_TEMPERATURE = 0.78

_REPEAT_PATTERNS = [
    r"didn'?t understand",
    r"don'?t understand",
    r"couldn'?t understand",
    r"not understand",
    r"didn'?t hear",
    r"couldn'?t hear",
    r"can'?t hear",
    r"repeat",
    r"say again",
    r"come again",
    r"pardon",
    r"what did you say",
    r"what\?",
    r"sorry\?",
]

_POLITE_FILLERS = [
    "ok", "okay", "sure", "yes", "yeah", "yep", "right", "got it",
    "understood", "thank you", "thanks", "mm hmm", "mm-hmm", "uh huh",
]

_VAGUE_PATTERNS = [
    r"^h+m+$",
    r"^um+$",
    r"^uh+$",
    r"^\.+$",
    r"^i don'?t know$",
    r"^not sure$",
]

_REPEAT_PREFIX = {
    "en-US": "No problem. Let me ask again.",
}

_FOLLOWUP_PREFIX = {
    "en-US": "Could you tell me a bit more?",
}

# Common phone-line mishearings — fixed using the last question as context
_CONTEXT_FIXES: list[tuple[re.Pattern[str], str, re.Pattern[str]]] = [
    (re.compile(r"^(plane|plain)$", re.I), "claim", re.compile(r"claim|authorization|preauth", re.I)),
    (re.compile(r"^(pre\s*auth|preauth)$", re.I), "pre-auth", re.compile(r"authorization|preauth|pre-auth", re.I)),
    (re.compile(r"^(reimburse|reimbursed)$", re.I), "reimbursement", re.compile(r"reimbursement|payment", re.I)),
    (re.compile(r"^(pending|pendings?)$", re.I), "pending", re.compile(r"status|pending|approved|denied", re.I)),
    (re.compile(r"^(spending|spendings?)$", re.I), "pending", re.compile(r"status|pending|claim", re.I)),
    (re.compile(r"^(approved?|approval)$", re.I), "approved", re.compile(r"status|approved|authorization", re.I)),
    (re.compile(r"^(rejected?|reject)$", re.I), "rejected", re.compile(r"status|rejected|appeal", re.I)),
    (re.compile(r"^(documents?|docs?)$", re.I), "documents", re.compile(r"document|upload|discharge|bill", re.I)),
    (re.compile(r"^(escalate|escalation)$", re.I), "escalation", re.compile(r"escalat|senior|manager", re.I)),
]

_GATHER_VOCAB = (
    r"claim|authorization|preauth|reimbursement|payer|insurer|EOB|denial|NPI|policy|"
    r"member|discharge|bill|approved|rejected|pending|documents|yes|no|hold|callback"
)


def speakers_to_try(language_code: str) -> list[str]:
    primary = LANGUAGE_SPEAKERS.get(language_code, "default")
    return [primary] if primary else []


def normalize_tts_text(text: str) -> str:
    t = (text or "").strip()
    # Refuse to vocalize model JSON blobs
    if t.startswith("{") or '"response_text"' in t or '"detected_language"' in t:
        m = re.search(r'"response_text"\s*:\s*"((?:\\.|[^"\\])*)"', t)
        if m:
            try:
                import json as _json

                t = _json.loads(f'"{m.group(1)}"')
            except Exception:
                t = m.group(1)
        else:
            return ""
    t = t.replace("—", ", ").replace("–", ", ")
    t = re.sub(r"\s+,", ",", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def user_wants_repeat(transcript: str) -> bool:
    t = transcript.strip().lower()
    return any(re.search(p, t) for p in _REPEAT_PATTERNS)


def is_polite_filler(transcript: str) -> bool:
    t = re.sub(r"[^\w\s']", " ", transcript.strip().lower().replace("-", " "))
    t = re.sub(r"\s+", " ", t).strip()
    normalized_fillers = {
        re.sub(r"\s+", " ", phrase.replace("-", " ")).strip()
        for phrase in _POLITE_FILLERS
    }
    if t in normalized_fillers:
        return True
    polite_words = {
        phrase
        for phrase in normalized_fillers
        if " " not in phrase
    }
    words = t.split()
    return bool(words) and all(word in polite_words for word in words)


def is_vague_answer(transcript: str) -> bool:
    t = transcript.strip().lower()
    if len(t) <= 2:
        return True
    return any(re.match(p, t) for p in _VAGUE_PATTERNS)


def is_valid_answer(transcript: str) -> bool:
    t = transcript.strip()
    if len(t) < 3:
        return False
    if is_vague_answer(t) or is_polite_filler(t):
        return False
    return True


def needs_clarification(
    transcript: str,
    last_question: str = "",
    confidence: float | None = None,
) -> bool:
    """Detect acknowledgements, clipped speech, and contextually ambiguous answers."""
    clean = re.sub(r"[^\w\s']", "", (transcript or "").strip().lower())
    words = clean.split()
    if not words or is_polite_filler(clean) or is_vague_answer(clean):
        return True
    if clean in {"completed", "complete", "done"}:
        return True
    if words[-1] in {
        "a", "an", "the", "and", "but", "for", "to", "with", "that",
        "is", "are", "was", "were", "have", "has",
    }:
        return True
    # Twilio sometimes omits Confidence (reported as 0). Only use it as a
    # quality signal when the transcript is also very short.
    if confidence is not None and confidence <= 0.15 and len(words) <= 3:
        return True
    return False


def clarification_response(transcript: str, last_question: str = "") -> str:
    """Return one concise clarification tied to the prior claim question."""
    clean = re.sub(r"[^\w\s']", "", (transcript or "").strip().lower())
    question = (last_question or "").lower()
    if clean in {"completed", "complete", "done"}:
        if "review" in question or "complet" in question:
            return "To confirm, was the claim review completed, and what was the outcome?"
        return "To confirm, what was completed—the review, document request, or payment?"
    if is_polite_filler(clean):
        return "Could you give me the full claim update?"
    return "I may have caught only part of that. Could you repeat the full update?"


def _norm_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if len(w) > 2}


def is_likely_stt_garbage(transcript: str) -> bool:
    """Reject hallucinated or non-speech STT output before it hits the LLM."""
    t = (transcript or "").strip()
    if not t:
        return True
    low = t.lower()
    if low in {"(silence)", "silence", "inaudible", "music", "hold music"}:
        return True
    # Very short non-words
    letters = re.sub(r"[^a-zA-Z0-9]", "", t)
    if len(letters) < 2:
        return True
    # Repeated same token noise
    words = re.findall(r"[a-z0-9']+", low)
    if words and len(set(words)) == 1 and len(words) >= 4:
        return True
    # Common Gemini phone hallucinations
    for bad in (
        "thank you for watching",
        "thanks for watching",
        "subscribe",
        "please like and",
        "as an ai",
        "i cannot hear",
        "i can't hear",
        "i am unable to transcribe",
        "no speech detected",
        "background noise",
    ):
        if bad in low:
            return True
    return False


def is_agent_echo(transcript: str, last_agent_text: str, threshold: float = 0.88) -> bool:
    """True if STT likely heard our own previous TTS (not soft word overlap)."""
    a = _norm_words(transcript)
    b = _norm_words(last_agent_text)
    # Short user answers ("yes", "paid", "member id is...") must not be treated as echo
    if not a or not b or len(a) < 4:
        return False
    overlap = len(a & b) / max(len(a), 1)
    t_norm = " ".join(sorted(a))
    b_norm = " ".join(sorted(b))
    if t_norm == b_norm and len(a) >= 4:
        return True
    # Require high overlap AND most agent words also present (near clone of agent line)
    return overlap >= threshold and len(a & b) >= 4


def correct_stt_transcript(transcript: str, last_question: str = "") -> str:
    words = transcript.strip().split()
    if not words:
        return transcript
    q_lower = (last_question or "").lower()
    fixed: list[str] = []
    for word in words:
        replacement = word
        for pattern, fix, context_re in _CONTEXT_FIXES:
            if pattern.match(word) and context_re.search(q_lower):
                replacement = fix
                break
        fixed.append(replacement)
    return " ".join(fixed)


def repeat_response(lang_code: str, question: str) -> str:
    prefix = _REPEAT_PREFIX.get(lang_code, _REPEAT_PREFIX["en-US"])
    spoken = (question or "").strip()
    looks_like_intro = (
        len(spoken) > 180
        or (
            re.search(r"\b(?:hi|hello),?\s+this is\b", spoken, re.I)
            and re.search(r"\bclaim (?:number|id)\b", spoken, re.I)
        )
    )
    if looks_like_intro:
        questions = re.findall(r"[^.!?]*\?", spoken)
        if questions:
            spoken = questions[-1].strip()
    return f"{prefix} {spoken}".strip()


def followup_response(lang_code: str, question: str) -> str:
    prefix = _FOLLOWUP_PREFIX.get(lang_code, _FOLLOWUP_PREFIX["en-US"])
    return f"{prefix} {question}"
