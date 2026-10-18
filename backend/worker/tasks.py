"""Background jobs: call summaries, analytics, email, batch processing."""

from __future__ import annotations

import logging
from typing import Any

from worker.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="worker.tasks.process_call_finished", bind=True, max_retries=3)
def process_call_finished(self, call_id: str) -> dict[str, Any]:
    """Orchestrate post-call work after a Media Streams / voice call ends."""
    logger.info("Celery process_call_finished start call_id=%s", call_id)
    summary = generate_call_summary(call_id)
    analytics = record_call_analytics(call_id)
    email = send_call_summary_email(call_id)
    return {
        "call_id": call_id,
        "summary": summary,
        "analytics": analytics,
        "email": email,
    }


@celery_app.task(name="worker.tasks.generate_call_summary")
def generate_call_summary(call_id: str) -> dict[str, Any]:
    """Build / refresh a short call summary and store on call_results."""
    from services.call_storage import load_call_json
    from services.conversation_state_db import (
        call_result_from_record,
        persist_call_result_from_record,
        save_call_result,
    )

    record = load_call_json(call_id) or {}
    if not record:
        logger.warning("No call JSON for summary call_id=%s", call_id)
        return {"ok": False, "reason": "missing_record"}

    from services.call_summary import apply_call_summary

    built = apply_call_summary(record)
    summary = built["summary"]
    row = call_result_from_record(call_id, record, session=None)
    row.summary = summary
    save_call_result(row)
    # Keep JSON in sync
    try:
        from services.call_storage import save_call_json

        save_call_json(call_id, record)
    except Exception:
        logger.exception("Failed updating call JSON summary call_id=%s", call_id)

    logger.info("Call summary saved call_id=%s", call_id)
    return {"ok": True, "summary": summary}


@celery_app.task(name="worker.tasks.record_call_analytics")
def record_call_analytics(call_id: str) -> dict[str, Any]:
    """Aggregate simple analytics into call_results.metrics / logs."""
    from services.call_storage import load_call_json
    from services.conversation_state_db import persist_call_result_from_record
    from services.turn_telemetry import compute_latency_summary

    record = load_call_json(call_id) or {}
    if not record:
        return {"ok": False, "reason": "missing_record"}

    transcript = record.get("transcript") if isinstance(record.get("transcript"), list) else []
    latency = record.get("latency_summary") or compute_latency_summary(transcript)
    analytics = {
        "turn_count": len([t for t in transcript if t.get("role") in ("customer", "agent")]),
        "customer_turns": len([t for t in transcript if t.get("role") == "customer"]),
        "agent_turns": len([t for t in transcript if t.get("role") == "agent"]),
        "barge_in_count": record.get("barge_in_count") or 0,
        "latency_summary": latency,
        "claim_id": record.get("claim_id") or "",
        "resolution_code": record.get("resolution_code") or "",
    }
    record["analytics"] = analytics
    record["latency_summary"] = latency
    persist_call_result_from_record(call_id, record)
    try:
        from services.call_storage import save_call_json

        save_call_json(call_id, record)
    except Exception:
        logger.exception("Failed saving analytics JSON call_id=%s", call_id)

    logger.info("Analytics recorded call_id=%s turns=%s", call_id, analytics["turn_count"])
    return {"ok": True, "analytics": analytics}


@celery_app.task(name="worker.tasks.send_call_summary_email")
def send_call_summary_email(call_id: str) -> dict[str, Any]:
    """
    Email call summary to configured recipients.

    Uses SMTP if EMAIL_ENABLED + SMTP_* set; otherwise logs a dry-run.
    """
    from config import get_settings
    from services.call_storage import load_call_json

    settings = get_settings()
    record = load_call_json(call_id) or {}
    to_addr = (getattr(settings, "email_to", None) or "").strip()
    summary = record.get("summary") or f"Call {call_id} completed."
    subject = f"[Call Agent] Summary {record.get('claim_id') or call_id}"

    if not getattr(settings, "email_enabled", False) or not to_addr:
        logger.info(
            "Email dry-run call_id=%s to=%s subject=%s body=%r",
            call_id,
            to_addr or "(unset)",
            subject,
            summary[:200],
        )
        return {"ok": True, "sent": False, "dry_run": True}

    try:
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = (getattr(settings, "email_from", None) or "noreply@localhost").strip()
        msg["To"] = to_addr
        body = (
            f"Call ID: {call_id}\n"
            f"Claim: {record.get('claim_id')}\n"
            f"Status: {record.get('resolution_status') or record.get('resolution_code')}\n"
            f"Outcome: {record.get('status')}\n\n"
            f"Summary:\n{summary}\n"
        )
        msg.set_content(body)

        host = (getattr(settings, "smtp_host", None) or "localhost").strip()
        port = int(getattr(settings, "smtp_port", 587) or 587)
        user = (getattr(settings, "smtp_username", None) or "").strip()
        password = (getattr(settings, "smtp_password", None) or "").strip()
        use_tls = bool(getattr(settings, "smtp_use_tls", True))

        with smtplib.SMTP(host, port, timeout=30) as smtp:
            if use_tls:
                smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
        logger.info("Summary email sent call_id=%s to=%s", call_id, to_addr)
        return {"ok": True, "sent": True}
    except Exception as e:
        logger.exception("Email failed call_id=%s", call_id)
        return {"ok": False, "error": str(e)}


@celery_app.task(name="worker.tasks.batch_process_calls")
def batch_process_calls(call_ids: list[str] | None = None) -> dict[str, Any]:
    """Batch re-process call summaries/analytics for many call IDs."""
    from services.call_storage import list_saved_call_files

    ids = list(call_ids or [])
    if not ids:
        ids = [p.stem for p in list_saved_call_files()[:50]]

    results = []
    for cid in ids:
        try:
            generate_call_summary(cid)
            record_call_analytics(cid)
            results.append({"call_id": cid, "ok": True})
        except Exception as e:
            logger.exception("Batch item failed call_id=%s", cid)
            results.append({"call_id": cid, "ok": False, "error": str(e)})
    return {"ok": True, "processed": len(results), "results": results}
