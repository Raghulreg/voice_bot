"""Lightweight extraction from payer STT when Gemini JSON fields are missing."""

from __future__ import annotations

import re
from typing import Any

_STATUS_WORDS = (
    ("denied", "denied"),
    ("rejected", "denied"),
    ("approved", "approved"),
    ("paid", "paid"),
    ("partial", "partial"),
    ("pending", "pending"),
    ("processing", "pending"),
    ("spending", "pending"),  # common STT mishear of "pending"
    ("under review", "under_review"),
    ("review", "under_review"),
)

_DOC_HINTS = (
    (re.compile(r"cath\s*lab|catlab", re.I), "Cath lab records"),
    (re.compile(r"discharge", re.I), "Discharge summary"),
    (re.compile(r"operative|op\s*report", re.I), "Operative report"),
    (re.compile(r"medical\s+record", re.I), "Medical records"),
    (re.compile(r"lab\s+result", re.I), "Lab results"),
    (re.compile(r"authorization|preauth", re.I), "Prior authorization"),
)

_REF_RE = re.compile(
    r"(?:reference|ref(?:erence)?(?:\s+number)?|control\s+(?:number|#)|claim\s+(?:ref|reference))"
    r"\s*(?:is|:)?\s*([0-9][0-9a-zA-Z\s\-]{1,24})",
    re.I,
)
_STATUS_PHRASE_RE = re.compile(
    r"(?:claim\s+)?status\s+(?:is\s+|was\s+)?([a-z][a-z\s\-]{2,24})",
    re.I,
)


def _normalize_status_fragment(raw: str) -> str:
    t = (raw or "").strip().lower()
    negative_payment = bool(
        re.search(r"\b(?:not|isn't|hasn't been|not yet)\s+paid\b", t)
    )
    for needle, code in _STATUS_WORDS:
        if needle == "paid" and negative_payment:
            continue
        if needle in t:
            return code
    return ""


def apply_payer_utterance(session: Any, payer_text: str) -> None:
    """Update session RCM fields from what the rep said (idempotent merges)."""
    t = (payer_text or "").strip()
    if len(t) < 2:
        return

    low = t.lower()
    data = getattr(session, "extracted_data", None) or {}
    if not isinstance(data, dict):
        data = {}

    # Keep approval, payment, and remittance as separate facts. A claim can be
    # approved without being paid, and payment does not prove EOB availability.
    if "approved" in low or "approval" in low:
        data["claim_approval_status"] = "approved"
    elif "denied" in low or "rejected" in low:
        data["claim_approval_status"] = "denied"
    if re.search(r"\b(?:not|isn't|hasn't been|not yet)\s+paid\b", low):
        data["payment_status"] = "not_paid"
    elif re.search(r"\b(?:has\s+been\s+|was\s+|is\s+)?paid\b", low):
        data["payment_status"] = "paid"
    if "remittance" in low or re.search(r"\beob\b", low):
        if any(word in low for word in ("available", "issued", "generated", "sent")):
            data["remittance_status"] = "available"
        elif any(phrase in low for phrase in ("not available", "not ready", "pending")):
            data["remittance_status"] = "not_available"
    session.extracted_data = data

    code = _normalize_status_fragment(low)
    m_status = _STATUS_PHRASE_RE.search(t)
    if m_status:
        code = _normalize_status_fragment(m_status.group(1)) or code
    if not code:
        code = _normalize_status_fragment(low)

    if code:
        if not getattr(session, "resolution_code", ""):
            session.resolution_code = code
        if not getattr(session, "resolution_status", ""):
            session.resolution_status = code.replace("_", " ")

    m_ref = _REF_RE.search(t)
    if m_ref:
        ref = re.sub(r"\s+", " ", m_ref.group(1)).strip()
        if ref:
            ref_norm = re.sub(r"[^a-z0-9]", "", ref.lower())
            claim_norm = re.sub(
                r"[^a-z0-9]", "", str(getattr(session, "claim_id", "") or "").lower()
            )
            if not (claim_norm and ref_norm == claim_norm):
                ref_key = (
                    "payment_reference"
                    if any(word in low for word in ("payment", "remittance", "trace"))
                    else "payer_reference"
                )
                data.setdefault(ref_key, ref)
                session.extracted_data = data

    if "document" in low and any(w in low for w in ("missing", "pending", "need", "required", "still")):
        docs: list[str] = list(getattr(session, "pending_documents", None) or [])
        for pattern, label in _DOC_HINTS:
            if pattern.search(t) and label not in docs:
                docs.append(label)
        if not docs:
            docs.append("Pending documents (unspecified)")
        session.pending_documents = docs

    if "denial" in low or "denied" in low:
        m_den = re.search(r"(?:denial|reason)\s*(?:code)?\s*(?:is|:)?\s*([A-Z0-9\-]{2,12})", t, re.I)
        if m_den and not getattr(session, "denial_code", ""):
            session.denial_code = m_den.group(1).upper()


def apply_validated_facts(session: Any, facts: dict[str, Any]) -> None:
    """Apply only facts produced by question-aware answer validation."""
    if not facts:
        return
    data = getattr(session, "extracted_data", None) or {}
    if not isinstance(data, dict):
        data = {}

    status = str(facts.get("claim_status") or "").strip()
    if status:
        session.resolution_code = status
        session.resolution_status = status.replace("_", " ")

    if facts.get("assigned_reviewer"):
        data["assigned_reviewer"] = str(facts["assigned_reviewer"]).strip()
    if facts.get("reviewer_status"):
        data["reviewer_status"] = str(facts["reviewer_status"]).strip()

    if facts.get("pending_documents_status") == "none":
        session.pending_documents = []
        data["pending_documents_status"] = "none"
    if facts.get("pending_documents_text"):
        text = str(facts["pending_documents_text"]).strip()
        session.pending_documents = [text]
        data["pending_documents_status"] = "pending"

    if facts.get("expected_date"):
        session.expected_payment_date = str(facts["expected_date"]).strip()
    for key in ("review_completion_date", "remittance_expected_date"):
        if facts.get(key):
            data[key] = str(facts[key]).strip()

    if facts.get("payer_reference"):
        ref = str(facts["payer_reference"]).strip()
        ref_norm = re.sub(r"[^a-z0-9]", "", ref.lower())
        claim_norm = re.sub(
            r"[^a-z0-9]", "", str(getattr(session, "claim_id", "") or "").lower()
        )
        if ref_norm and ref_norm != claim_norm:
            data["payer_reference"] = ref

    if facts.get("remittance_status"):
        data["remittance_status"] = str(facts["remittance_status"]).strip()
    session.extracted_data = data
