"""PII guardrails — mask sensitive fields before sending text to Gemini.

Masks:
  Patient Name  → [PATIENT_NAME]
  DOB           → [DOB]
  Insurance ID  → [INSURANCE_ID]
  SSN           → [SSN]
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

TOKEN_PATIENT_NAME = "[PATIENT_NAME]"
TOKEN_DOB = "[DOB]"
TOKEN_INSURANCE_ID = "[INSURANCE_ID]"
TOKEN_SSN = "[SSN]"

# US SSN variants
_SSN_RE = re.compile(
    r"\b(?:\d{3}[-.\s]?\d{2}[-.\s]?\d{4}|\d{9})\b"
)
# DOB: 01/15/1980, 1980-01-15, January 15 1980, 15 Jan 1980
_DOB_RE = re.compile(
    r"\b(?:"
    r"\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"
    r"|\d{4}[/.\-]\d{1,2}[/.\-]\d{1,2}"
    r"|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{2,4}"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r",?\s+\d{2,4}"
    r")\b",
    re.I,
)
# Member / insurance style IDs (alphanumeric 6–20 with optional dashes)
_GENERIC_MEMBER_RE = re.compile(
    r"\b(?:member\s*(?:id|number|#)|insurance\s*(?:id|number|#)|"
    r"policy\s*(?:id|number|#)|subscriber\s*(?:id|number))\s*[:=]?\s*"
    r"([A-Z0-9][A-Z0-9\-]{5,19})\b",
    re.I,
)


@dataclass
class PiiContext:
    """Known PII for a call — used for exact-string masking before LLM."""

    patient_name: str = ""
    customer_name: str = ""
    date_of_birth: str = ""
    insurance_ids: list[str] = field(default_factory=list)
    policy_number: str = ""
    ssn: str = ""
    extra_names: list[str] = field(default_factory=list)

    @classmethod
    def from_session(cls, session: Any) -> "PiiContext":
        ids: list[str] = []
        for key in ("policy_number", "member_id", "insurance_id"):
            val = getattr(session, key, None) or ""
            if isinstance(val, str) and val.strip():
                ids.append(val.strip())
        extracted = getattr(session, "extracted_data", None) or {}
        if isinstance(extracted, dict):
            for key in (
                "policy_number",
                "member_id",
                "insurance_id",
                "subscriber_id",
                "ssn",
                "date_of_birth",
                "dob",
            ):
                val = extracted.get(key)
                if val:
                    if key in ("ssn",):
                        pass
                    elif key in ("date_of_birth", "dob"):
                        pass
                    else:
                        ids.append(str(val).strip())
        dob = ""
        if isinstance(extracted, dict):
            dob = str(extracted.get("date_of_birth") or extracted.get("dob") or "").strip()
        dob = dob or str(getattr(session, "date_of_birth", "") or "").strip()
        ssn = ""
        if isinstance(extracted, dict):
            ssn = str(extracted.get("ssn") or "").strip()
        ssn = ssn or str(getattr(session, "ssn", "") or "").strip()

        return cls(
            patient_name=str(getattr(session, "patient_name", "") or "").strip(),
            customer_name=str(getattr(session, "customer_name", "") or "").strip(),
            date_of_birth=dob,
            insurance_ids=ids,
            policy_number=str(getattr(session, "policy_number", "") or "").strip(),
            ssn=ssn,
        )


def _replace_literal(text: str, value: str, token: str) -> str:
    raw = (value or "").strip()
    if not raw or len(raw) < 2:
        return text
    # Longest-first for multi-word names
    pattern = re.compile(re.escape(raw), re.I)
    return pattern.sub(token, text)


def _mask_person_name(text: str, full_name: str) -> str:
    out = _replace_literal(text, full_name, TOKEN_PATIENT_NAME)
    parts = [p for p in re.split(r"\s+", full_name.strip()) if len(p) > 1]
    # Also mask "Last, First" and individual tokens when full name present
    if len(parts) >= 2:
        out = _replace_literal(out, f"{parts[-1]}, {parts[0]}", TOKEN_PATIENT_NAME)
        # First + Last only
        out = _replace_literal(out, f"{parts[0]} {parts[-1]}", TOKEN_PATIENT_NAME)
    return out


def mask_text(text: str, ctx: PiiContext | None = None) -> str:
    """Mask PII in a single string before sending to an LLM."""
    if not text:
        return text
    out = text
    ctx = ctx or PiiContext()

    # Exact known values first (highest confidence)
    for name in (
        ctx.patient_name,
        ctx.customer_name,
        *ctx.extra_names,
    ):
        if name:
            out = _mask_person_name(out, name)

    if ctx.date_of_birth:
        out = _replace_literal(out, ctx.date_of_birth, TOKEN_DOB)
    if ctx.ssn:
        out = _replace_literal(out, ctx.ssn, TOKEN_SSN)
    for iid in list(ctx.insurance_ids) + ([ctx.policy_number] if ctx.policy_number else []):
        out = _replace_literal(out, iid, TOKEN_INSURANCE_ID)

    # Pattern-based catch-all
    out = _SSN_RE.sub(TOKEN_SSN, out)
    out = _DOB_RE.sub(TOKEN_DOB, out)
    out = _GENERIC_MEMBER_RE.sub(
        lambda m: m.group(0).replace(m.group(1), TOKEN_INSURANCE_ID),
        out,
    )

    return out


def mask_history(
    history: list[dict],
    ctx: PiiContext | None = None,
) -> list[dict]:
    """Return a copy of conversation history with PII masked in content fields."""
    masked: list[dict] = []
    for turn in history or []:
        item = dict(turn)
        if "content" in item and isinstance(item["content"], str):
            item["content"] = mask_text(item["content"], ctx)
        if "text" in item and isinstance(item["text"], str):
            item["text"] = mask_text(item["text"], ctx)
        masked.append(item)
    return masked


def mask_for_gemini(
    *,
    user_transcript: str,
    conversation_history: list[dict],
    ctx: PiiContext | None = None,
    patient_name: str = "",
    policy_number: str = "",
    date_of_birth: str = "",
    ssn: str = "",
) -> tuple[str, list[dict], PiiContext]:
    """
    Prepare transcript + history for Gemini.

    Returns (masked_transcript, masked_history, ctx_used).
    """
    from config import get_settings

    if not getattr(get_settings(), "pii_masking_enabled", True):
        return user_transcript, list(conversation_history or []), ctx or PiiContext()

    base = ctx or PiiContext()
    if patient_name and not base.patient_name:
        base.patient_name = patient_name
    if policy_number:
        base.policy_number = policy_number
        if policy_number not in base.insurance_ids:
            base.insurance_ids.append(policy_number)
    if date_of_birth:
        base.date_of_birth = date_of_birth
    if ssn:
        base.ssn = ssn

    masked_transcript = mask_text(user_transcript or "", base)
    masked_hist = mask_history(list(conversation_history or []), base)

    if masked_transcript != (user_transcript or ""):
        logger.debug("PII masked in user transcript before Gemini")

    return masked_transcript, masked_hist, base


def redacted_claim_fields(
    *,
    patient_name: str = "",
    policy_number: str = "",
    date_of_birth: str = "",
) -> dict[str, str]:
    """Tokenized claim fields for system prompt injection."""
    from config import get_settings

    if not getattr(get_settings(), "pii_masking_enabled", True):
        return {
            "patient_name": patient_name or "",
            "policy_number": policy_number or "",
            "date_of_birth": date_of_birth or "",
        }
    return {
        "patient_name": TOKEN_PATIENT_NAME if (patient_name or "").strip() else "",
        "policy_number": TOKEN_INSURANCE_ID if (policy_number or "").strip() else "",
        "date_of_birth": TOKEN_DOB if (date_of_birth or "").strip() else "",
    }
