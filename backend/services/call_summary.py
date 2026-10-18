"""End-of-call summary, outcome, and follow-up actions.

Existing transcript, recording, and history fields are left in place.
"""

from __future__ import annotations

from typing import Any


def apply_call_summary(record: dict[str, Any]) -> dict[str, Any]:
    """Fill summary fields on the call record without removing other keys."""
    transcript = record.get("transcript") if isinstance(record.get("transcript"), list) else []
    outcome = str(
        record.get("outcome")
        or record.get("resolution_status")
        or record.get("resolution_code")
        or record.get("status")
        or "completed"
    ).strip()
    actions = _follow_up_actions(record)
    customer = [
        str(turn.get("text") or "").strip()
        for turn in transcript
        if turn.get("role") == "customer" and str(turn.get("text") or "").strip()
    ]
    parts: list[str] = []
    if record.get("claim_id"):
        parts.append(f"Claim {record['claim_id']}")
    if record.get("payer_name") or record.get("tpa_name"):
        parts.append(f"Payer {record.get('payer_name') or record.get('tpa_name')}")
    parts.append(f"Outcome: {outcome or 'completed'}")
    if customer:
        parts.append(f"Last payer line: {customer[-1][:180]}")
    parts.append("Follow-up: " + "; ".join(actions))
    summary = " | ".join(parts)

    record["outcome"] = outcome or "completed"
    record["follow_up_actions"] = actions
    record["summary"] = summary
    return {
        "summary": summary,
        "outcome": record["outcome"],
        "follow_up_actions": actions,
    }


def _follow_up_actions(record: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    follow_up = str(record.get("follow_up_date") or "").strip()
    if follow_up:
        actions.append(f"Call back on {follow_up}")
    documents = record.get("pending_documents") or []
    if isinstance(documents, list) and documents:
        names = ", ".join(str(item).strip() for item in documents[:6] if str(item).strip())
        if names:
            actions.append(f"Collect pending documents: {names}")
    denial = str(record.get("denial_code") or "").strip()
    if denial:
        actions.append(f"Review denial code {denial} for appeal or resubmission")
    payment = str(record.get("expected_payment_date") or "").strip()
    if payment:
        actions.append(f"Confirm payment on {payment}")
    code = str(record.get("resolution_code") or record.get("resolution_status") or "").lower()
    if code in {"pending", "under_review", "unresolved"} and not follow_up:
        actions.append("Schedule a follow-up and confirm the reviewer or queue")
    if not actions:
        actions.append("No further payer action was identified from this call")
    return actions
