"""PostgreSQL persistence — conversation state, call results, claim status.

Tables are created once (CREATE TABLE IF NOT EXISTS). Runtime only UPSERTs.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from config import get_settings

logger = logging.getLogger(__name__)

_schema_lock = threading.Lock()
_schema_ready = False


@dataclass
class ConversationState:
    call_id: str
    current_claim: str = ""
    current_caller: str = ""
    current_intent: str = ""
    current_stage: str = "greeting"
    authentication_done: bool = False
    appeal_started: bool = False
    claim_number: str = ""
    customer_phone: str = ""
    hospital_name: str = ""
    claim_type: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_prompt_block(self) -> str:
        """Structured state for the LLM — not only chat history."""
        return (
            "## Conversation State (source of truth — prefer over guessing from history)\n"
            f"- Current Claim: {self.current_claim or self.claim_number or '(none)'}\n"
            f"- Claim Number: {self.claim_number or '(none)'}\n"
            f"- Current Caller: {self.current_caller or '(unknown)'}\n"
            f"- Current Intent: {self.current_intent or '(unset)'}\n"
            f"- Current Stage: {self.current_stage or '(unset)'}\n"
            f"- Authentication Done: {'yes' if self.authentication_done else 'no'}\n"
            f"- Appeal Started: {'yes' if self.appeal_started else 'no'}\n"
            f"- Hospital: {self.hospital_name or '(none)'}\n"
            f"- Claim Type: {self.claim_type or '(none)'}\n"
        )


@dataclass
class CallResultRow:
    call_id: str
    claim_number: str = ""
    claim_status: str = ""
    reference_numbers: list[str] = field(default_factory=list)
    resolution_code: str = ""
    resolution_status: str = ""
    summary: str = ""
    outcome: str = ""
    customer_phone: str = ""
    hospital_name: str = ""
    transcript_redacted: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


def database_url() -> str:
    s = get_settings()
    if (s.database_url or "").strip():
        return s.database_url.strip()
    host = (s.db_host or "").strip()
    name = (s.db_name or "").strip()
    user = (s.db_username or "").strip()
    password = (s.db_password or "").strip()
    port = int(s.db_port or 5432)
    if not (host and name and user):
        return ""
    return (
        f"postgresql://{user}:{password}@{host}:{port}/{name}"
        f"?sslmode=require"
    )


def is_db_configured() -> bool:
    return bool(database_url())


def _connect():
    import psycopg

    url = database_url()
    if not url:
        raise RuntimeError("PostgreSQL not configured (DB_* / DATABASE_URL)")
    return psycopg.connect(url)


def ensure_schema() -> bool:
    """Create conversation_state + call_results once per process."""
    global _schema_ready
    if _schema_ready:
        return True
    if not is_db_configured():
        logger.warning("Postgres skipped — set DB_HOST/DB_NAME/DB_USERNAME/DB_PASSWORD")
        return False

    with _schema_lock:
        if _schema_ready:
            return True
        try:
            with _connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS conversation_state (
                            call_id              TEXT PRIMARY KEY,
                            current_claim        TEXT NOT NULL DEFAULT '',
                            current_caller       TEXT NOT NULL DEFAULT '',
                            current_intent       TEXT NOT NULL DEFAULT '',
                            current_stage        TEXT NOT NULL DEFAULT 'greeting',
                            authentication_done  BOOLEAN NOT NULL DEFAULT FALSE,
                            appeal_started       BOOLEAN NOT NULL DEFAULT FALSE,
                            claim_number         TEXT NOT NULL DEFAULT '',
                            customer_phone       TEXT NOT NULL DEFAULT '',
                            hospital_name        TEXT NOT NULL DEFAULT '',
                            claim_type           TEXT NOT NULL DEFAULT '',
                            extra                JSONB NOT NULL DEFAULT '{}'::jsonb,
                            created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        );
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS call_results (
                            call_id              TEXT PRIMARY KEY,
                            claim_number         TEXT NOT NULL DEFAULT '',
                            claim_status         TEXT NOT NULL DEFAULT '',
                            reference_numbers    JSONB NOT NULL DEFAULT '[]'::jsonb,
                            resolution_code      TEXT NOT NULL DEFAULT '',
                            resolution_status    TEXT NOT NULL DEFAULT '',
                            summary              TEXT NOT NULL DEFAULT '',
                            outcome              TEXT NOT NULL DEFAULT '',
                            customer_phone       TEXT NOT NULL DEFAULT '',
                            hospital_name        TEXT NOT NULL DEFAULT '',
                            transcript_redacted  JSONB NOT NULL DEFAULT '[]'::jsonb,
                            metrics              JSONB NOT NULL DEFAULT '{}'::jsonb,
                            extra                JSONB NOT NULL DEFAULT '{}'::jsonb,
                            created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        );
                        """
                    )
                    cur.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_call_results_claim
                        ON call_results (claim_number);
                        """
                    )
                    cur.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_call_results_status
                        ON call_results (claim_status);
                        """
                    )
                conn.commit()
            _schema_ready = True
            logger.info(
                "PostgreSQL schema ready (conversation_state, call_results — created if missing)"
            )
            return True
        except Exception:
            logger.exception("Failed to ensure Postgres schema")
            return False


def save_conversation_state(state: ConversationState) -> bool:
    if not ensure_schema():
        return False
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO conversation_state (
                        call_id, current_claim, current_caller, current_intent,
                        current_stage, authentication_done, appeal_started,
                        claim_number, customer_phone, hospital_name, claim_type,
                        extra, updated_at
                    ) VALUES (
                        %(call_id)s, %(current_claim)s, %(current_caller)s, %(current_intent)s,
                        %(current_stage)s, %(authentication_done)s, %(appeal_started)s,
                        %(claim_number)s, %(customer_phone)s, %(hospital_name)s, %(claim_type)s,
                        %(extra)s::jsonb, NOW()
                    )
                    ON CONFLICT (call_id) DO UPDATE SET
                        current_claim = EXCLUDED.current_claim,
                        current_caller = EXCLUDED.current_caller,
                        current_intent = EXCLUDED.current_intent,
                        current_stage = EXCLUDED.current_stage,
                        authentication_done = EXCLUDED.authentication_done,
                        appeal_started = EXCLUDED.appeal_started,
                        claim_number = EXCLUDED.claim_number,
                        customer_phone = EXCLUDED.customer_phone,
                        hospital_name = EXCLUDED.hospital_name,
                        claim_type = EXCLUDED.claim_type,
                        extra = EXCLUDED.extra,
                        updated_at = NOW()
                    """,
                    {
                        "call_id": state.call_id,
                        "current_claim": state.current_claim or "",
                        "current_caller": state.current_caller or "",
                        "current_intent": state.current_intent or "",
                        "current_stage": state.current_stage or "greeting",
                        "authentication_done": bool(state.authentication_done),
                        "appeal_started": bool(state.appeal_started),
                        "claim_number": state.claim_number or "",
                        "customer_phone": state.customer_phone or "",
                        "hospital_name": state.hospital_name or "",
                        "claim_type": state.claim_type or "",
                        "extra": json.dumps(state.extra or {}),
                    },
                )
            conn.commit()
        return True
    except Exception:
        logger.exception("Failed saving conversation_state call_id=%s", state.call_id)
        return False


def load_conversation_state(call_id: str) -> ConversationState | None:
    if not call_id or not ensure_schema():
        return None
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT call_id, current_claim, current_caller, current_intent,
                           current_stage, authentication_done, appeal_started,
                           claim_number, customer_phone, hospital_name, claim_type,
                           COALESCE(extra, '{}'::jsonb)
                    FROM conversation_state
                    WHERE call_id = %s
                    """,
                    (call_id,),
                )
                row = cur.fetchone()
        if not row:
            return None
        extra = row[11]
        if isinstance(extra, str):
            extra = json.loads(extra)
        return ConversationState(
            call_id=row[0],
            current_claim=row[1] or "",
            current_caller=row[2] or "",
            current_intent=row[3] or "",
            current_stage=row[4] or "greeting",
            authentication_done=bool(row[5]),
            appeal_started=bool(row[6]),
            claim_number=row[7] or "",
            customer_phone=row[8] or "",
            hospital_name=row[9] or "",
            claim_type=row[10] or "",
            extra=dict(extra or {}),
        )
    except Exception:
        logger.exception("Failed loading conversation_state call_id=%s", call_id)
        return None


def state_from_session(session: Any) -> ConversationState:
    claim_number = (getattr(session, "claim_id", None) or "").strip()
    claim_type = (getattr(session, "claim_type", None) or "").strip()
    caller = (
        (getattr(session, "customer_name", None) or "").strip()
        or (getattr(session, "patient_name", None) or "").strip()
        or (getattr(session, "customer_phone", None) or "").strip()
    )
    stage = (getattr(session, "current_stage", None) or "greeting").strip() or "greeting"
    intent = (getattr(session, "current_intent", None) or "").strip()
    auth = bool(
        getattr(session, "authentication_done", False)
        or getattr(session, "ivr_completed", False)
    )
    appeal = bool(getattr(session, "appeal_started", False))
    if not appeal and "appeal" in claim_type.lower():
        appeal = True

    hospital = (getattr(session, "hospital_name", None) or "").strip()
    current_claim = claim_number
    if hospital and claim_number:
        current_claim = f"{hospital} / {claim_number}"
    elif hospital:
        current_claim = hospital

    extra = {
        "tpa_name": getattr(session, "tpa_name", "") or "",
        "resolution_status": getattr(session, "resolution_status", "") or "",
        "resolution_code": getattr(session, "resolution_code", "") or "",
        "scenario_id": getattr(session, "scenario_id", "") or "",
        "turn_count": int(getattr(session, "turn_count", 0) or 0),
    }
    return ConversationState(
        call_id=session.call_id,
        current_claim=current_claim,
        current_caller=caller,
        current_intent=intent,
        current_stage=stage,
        authentication_done=auth,
        appeal_started=appeal,
        claim_number=claim_number,
        customer_phone=(getattr(session, "customer_phone", None) or "").strip(),
        hospital_name=hospital,
        claim_type=claim_type,
        extra=extra,
    )


def persist_session_state(session: Any) -> bool:
    return save_conversation_state(state_from_session(session))


def apply_loaded_state(session: Any, state: ConversationState) -> None:
    if state.claim_number and not getattr(session, "claim_id", ""):
        session.claim_id = state.claim_number
    if state.current_stage:
        session.current_stage = state.current_stage
    if state.current_intent:
        session.current_intent = state.current_intent
    session.authentication_done = bool(state.authentication_done)
    if state.authentication_done:
        session.ivr_completed = True
    session.appeal_started = bool(state.appeal_started)


def _collect_reference_numbers(record: dict[str, Any], session: Any | None) -> list[str]:
    refs: list[str] = []
    for key in ("reference_number", "reference_numbers", "auth_number", "ticket_number"):
        val = record.get(key)
        if isinstance(val, list):
            refs.extend(str(x) for x in val if x)
        elif val:
            refs.append(str(val))
    extracted = record.get("extracted_data") or {}
    if isinstance(extracted, dict):
        for key in (
            "reference_number",
            "reference_numbers",
            "auth_number",
            "ticket_number",
            "confirmation_number",
            "payer_reference",
            "payer_reference_number",
            "payment_reference",
            "payment_reference_number",
        ):
            val = extracted.get(key)
            if isinstance(val, list):
                refs.extend(str(x) for x in val if x)
            elif val:
                refs.append(str(val))
    if session is not None:
        ed = getattr(session, "extracted_data", None) or {}
        if isinstance(ed, dict):
            for key in (
                "reference_number",
                "auth_number",
                "confirmation_number",
                "payer_reference",
                "payer_reference_number",
                "payment_reference",
                "payment_reference_number",
            ):
                if ed.get(key):
                    refs.append(str(ed[key]))
    # Dedupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for r in refs:
        r = r.strip()
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def call_result_from_record(
    call_id: str,
    record: dict[str, Any],
    session: Any | None = None,
) -> CallResultRow:
    from services.pii_guardrails import PiiContext, mask_history

    claim_number = str(
        record.get("claim_id")
        or (getattr(session, "claim_id", "") if session else "")
        or ""
    )
    claim_status = str(
        record.get("resolution_code")
        or record.get("claim_type")
        or (getattr(session, "resolution_code", "") if session else "")
        or record.get("status")
        or ""
    )
    transcript = record.get("transcript") if isinstance(record.get("transcript"), list) else []
    ctx = PiiContext.from_session(session) if session is not None else PiiContext(
        patient_name=str(record.get("patient_name") or ""),
        policy_number=str(record.get("policy_number") or ""),
    )
    redacted = mask_history(
        [{"role": t.get("role"), "text": t.get("text", ""), "content": t.get("text", "")} for t in transcript],
        ctx,
    )
    # Slim for storage
    redacted_slim = [
        {"role": t.get("role"), "text": t.get("text") or t.get("content") or ""}
        for t in redacted
    ]

    summary = str(record.get("summary") or "")
    if not summary and transcript:
        # Short placeholder — Celery job may replace with full summary
        agent_lines = [
            str(t.get("text") or "")
            for t in transcript
            if t.get("role") == "agent" and t.get("text")
        ]
        if agent_lines:
            summary = agent_lines[-1][:400]

    return CallResultRow(
        call_id=call_id,
        claim_number=claim_number,
        claim_status=claim_status,
        reference_numbers=_collect_reference_numbers(record, session),
        resolution_code=str(
            record.get("resolution_code")
            or (getattr(session, "resolution_code", "") if session else "")
            or ""
        ),
        resolution_status=str(
            record.get("resolution_status")
            or (getattr(session, "resolution_status", "") if session else "")
            or ""
        ),
        summary=summary,
        outcome=str(record.get("outcome") or record.get("status") or ""),
        customer_phone=str(record.get("customer_phone") or ""),
        hospital_name=str(record.get("hospital_name") or ""),
        transcript_redacted=redacted_slim,
        metrics=dict(record.get("latency_summary") or {}),
        extra={
            "denial_code": record.get("denial_code") or "",
            "scenario_id": record.get("scenario_id") or "",
            "dialogue_mode": record.get("dialogue_mode") or "",
        },
    )


def save_call_result(row: CallResultRow) -> bool:
    if not ensure_schema():
        return False
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO call_results (
                        call_id, claim_number, claim_status, reference_numbers,
                        resolution_code, resolution_status, summary, outcome,
                        customer_phone, hospital_name, transcript_redacted,
                        metrics, extra, updated_at
                    ) VALUES (
                        %(call_id)s, %(claim_number)s, %(claim_status)s, %(reference_numbers)s::jsonb,
                        %(resolution_code)s, %(resolution_status)s, %(summary)s, %(outcome)s,
                        %(customer_phone)s, %(hospital_name)s, %(transcript_redacted)s::jsonb,
                        %(metrics)s::jsonb, %(extra)s::jsonb, NOW()
                    )
                    ON CONFLICT (call_id) DO UPDATE SET
                        claim_number = EXCLUDED.claim_number,
                        claim_status = EXCLUDED.claim_status,
                        reference_numbers = EXCLUDED.reference_numbers,
                        resolution_code = EXCLUDED.resolution_code,
                        resolution_status = EXCLUDED.resolution_status,
                        summary = EXCLUDED.summary,
                        outcome = EXCLUDED.outcome,
                        customer_phone = EXCLUDED.customer_phone,
                        hospital_name = EXCLUDED.hospital_name,
                        transcript_redacted = EXCLUDED.transcript_redacted,
                        metrics = EXCLUDED.metrics,
                        extra = EXCLUDED.extra,
                        updated_at = NOW()
                    """,
                    {
                        "call_id": row.call_id,
                        "claim_number": row.claim_number or "",
                        "claim_status": row.claim_status or "",
                        "reference_numbers": json.dumps(row.reference_numbers or []),
                        "resolution_code": row.resolution_code or "",
                        "resolution_status": row.resolution_status or "",
                        "summary": row.summary or "",
                        "outcome": row.outcome or "",
                        "customer_phone": row.customer_phone or "",
                        "hospital_name": row.hospital_name or "",
                        "transcript_redacted": json.dumps(row.transcript_redacted or []),
                        "metrics": json.dumps(row.metrics or {}),
                        "extra": json.dumps(row.extra or {}),
                    },
                )
            conn.commit()
        logger.info(
            "Saved call_results call_id=%s claim=%s status=%s",
            row.call_id,
            row.claim_number,
            row.claim_status,
        )
        return True
    except Exception:
        logger.exception("Failed saving call_results call_id=%s", row.call_id)
        return False


def persist_call_result_from_record(
    call_id: str,
    record: dict[str, Any],
    session: Any | None = None,
) -> bool:
    return save_call_result(call_result_from_record(call_id, record, session))
