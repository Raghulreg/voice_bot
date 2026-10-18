"""Dynamic RCM dialogue hints — what to ask next, not a fixed script order."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

@dataclass
class RcmCollected:
    resolution_status: str = ""
    resolution_code: str = ""
    denial_code: str = ""
    pending_documents: list[str] | None = None
    expected_payment_date: str = ""
    follow_up_date: str = ""
    extracted_data: dict[str, Any] | None = None


def _has_docs(collected: RcmCollected) -> bool:
    data = collected.extracted_data or {}
    return bool(
        collected.pending_documents
        or data.get("pending_documents_status") == "none"
    )


def _has_status(collected: RcmCollected) -> bool:
    return bool(collected.resolution_status or collected.resolution_code)


def _has_payer_reference(collected: RcmCollected) -> bool:
    data = collected.extracted_data or {}
    return any(
        str(data.get(key) or "").strip()
        for key in (
            "payer_reference",
            "payer_reference_number",
            "payment_reference",
            "payment_reference_number",
        )
    )


def _has_review_owner(collected: RcmCollected) -> bool:
    data = collected.extracted_data or {}
    return any(
        str(data.get(key) or "").strip()
        for key in ("assigned_reviewer", "reviewer", "queue_owner", "adjuster")
    )


def open_rcm_gaps(collected: RcmCollected) -> list[str]:
    """Topics still useful for hospital RCM — order is not prescribed."""
    gaps: list[str] = []
    if not _has_status(collected):
        gaps.append("current claim status")
    if not _has_payer_reference(collected):
        gaps.append("payer reference or claim control number")
    status = f"{collected.resolution_status} {collected.resolution_code}".lower()
    denied = any(word in status for word in ("denied", "rejected", "partial"))
    if denied and not collected.denial_code:
        gaps.append("denial or partial-payment reason and code")
    if not _has_docs(collected):
        gaps.append("any pending documents or records the payer still needs")
    if ("review" in status or "pending" in status) and not _has_review_owner(collected):
        gaps.append("assigned reviewer, review team, or queue owner")
    if not collected.expected_payment_date:
        gaps.append("expected payment date or medical review completion timeline")
    if not collected.extracted_data or not (
        collected.extracted_data.get("eob_or_remittance")
        or collected.extracted_data.get("remittance_status")
    ):
        gaps.append("EOB / remittance advice availability and delivery to billing")
    if not collected.follow_up_date and collected.resolution_code in (
        "",
        "pending",
        "partial",
        "under_review",
        "unresolved",
        "unknown",
    ):
        gaps.append("follow-up callback date and adjuster or queue owner if unresolved")
    if not collected.resolution_code or collected.resolution_code == "unknown":
        gaps.append("resolution code to log in the hospital RCM system")
    return gaps


def format_rcm_gaps_block(collected: RcmCollected) -> str:
    gaps = open_rcm_gaps(collected)
    if not gaps:
        return (
            "## RCM data status\n"
            "Core fields appear captured. Confirm resolution, thank the rep, and close professionally "
            "unless they raise a new blocker."
        )
    bullets = "\n".join(f"- {g}" for g in gaps)
    return (
        "## RCM data still open (themes only — invent ONE natural question; "
        "do NOT read this list aloud or follow a fixed order)\n"
        f"{bullets}"
    )


_STATUS_ACK = {
    "pending": "Thank you, I'll note the claim is still pending.",
    "under_review": "Thank you, I'll note the claim is under review.",
    "approved": "Thank you, I'll note the claim is approved.",
    "denied": "Thank you, I'll note the claim was denied.",
    "rejected": "Thank you, I'll note the claim was rejected.",
    "paid": "Thank you, I'll note the claim has been paid.",
    "partial": "Thank you, I'll note the claim was partially paid.",
}


def replace_unnecessary_status_probe(spoken: str, collected: RcmCollected) -> str | None:
    """Replace a why/cause question when that status is already confirmed."""
    code = (collected.resolution_code or "").strip().lower()
    if code not in ("pending", "under_review", "approved", "paid"):
        return None
    low = (spoken or "").lower()
    if not any(word in low for word in ("causing", "cause", "why", "reason")):
        return None
    return workflow_reply_for_confirmed_status(collected)


def workflow_reply_for_confirmed_status(collected: RcmCollected) -> str | None:
    """Acknowledge a confirmed status and ask the one next fact the file still needs."""
    code = (collected.resolution_code or "").strip().lower()
    ack = _STATUS_ACK.get(code)
    if not ack:
        return None
    denied = code in ("denied", "rejected", "partial")
    if denied and not collected.denial_code:
        question = "What is the denial code?"
    elif code in ("pending", "under_review") and not _has_review_owner(collected):
        question = "Who is reviewing the claim?"
    elif not _has_docs(collected):
        question = "Are any documents still needed?"
    elif code in ("pending", "under_review") and not (collected.extracted_data or {}).get(
        "review_completion_date"
    ):
        question = "What is the expected review completion date?"
    elif not _has_payer_reference(collected):
        question = "What is the payer reference or claim control number?"
    elif not (
        (collected.extracted_data or {}).get("remittance_status")
        or (collected.extracted_data or {}).get("eob_or_remittance")
    ):
        question = "Is the EOB or remittance available?"
    elif not collected.follow_up_date and code in ("pending", "under_review", "unresolved"):
        question = "Is there a follow-up date for this claim?"
    else:
        return None
    return f"{ack} {question}"


def _fallback_question_for_gap(gap: str) -> str:
    templates = {
        "current claim status": "What is the current claim status on your side?",
        "payer reference or claim control number": "May I have the payer reference or claim control number for our file?",
    }
    return templates.get(gap, f"Could you help with {gap}?")


def _normalize_question(text: str) -> str:
    return " ".join((text or "").lower().split())


def dynamic_fallback_question(
    collected: RcmCollected,
    payer_said: str = "",
    last_question: str = "",
) -> str:
    """One natural question when Gemini is unavailable — not the scripted checklist."""
    gaps = open_rcm_gaps(collected)
    payer = (payer_said or "").lower()
    last_norm = _normalize_question(last_question)

    if "partial" in payer and "paid" in payer and gaps:
        return "Thanks — what denial or adjustment code applies, and what do you still need from the hospital?"
    if "denied" in payer:
        return "Understood — what is the denial code and the exact documents required to reopen or appeal?"
    if "pending" in payer and "document" in payer:
        return "Got it — which documents are still missing, and where should we send them?"

    for gap in gaps:
        candidate = _fallback_question_for_gap(gap)
        if _normalize_question(candidate) != last_norm:
            return candidate

    if gaps:
        gap = gaps[0]
        if gap == "current claim status":
            return "Is the claim still pending review, or has a decision been posted?"
        return _fallback_question_for_gap(gap)

    return (
        "Thank you — may I confirm the resolution code for our RCM update and that this call is noted on the claim?"
    )
