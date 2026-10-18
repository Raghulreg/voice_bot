import asyncio
import base64
import json
import logging
import re
import time
import uuid
import xml.sax.saxutils as xml_escape
from dataclasses import dataclass, field
from typing import Any

from services.backchannel import BACKCHANNEL_PHRASES, select_backchannel
from services.answer_validation import (
    AnswerValidation,
    clarification_phrases,
    classify_question,
    dialogue_action,
    extract_explicit_secondary_facts,
    move_on_after_ambiguity,
    resolve_pending_date,
    validate_answer,
)
from services.call_storage import save_call_json
from config import get_settings
from claims_scenarios import get_scenario_by_id
from services.gemini_service import GeminiService, _error_label
from services.ivr import (
    confirmation_message,
    discovery_questions,
    fallback_agent_reply,
    fallback_greeting,
    fast_agent_reply,
    language_from_digit,
    next_discovery_question,
)
from services.scenario_fallback import scenario_fallback_reply
from services.rcm_dialogue import (
    RcmCollected,
    open_rcm_gaps,
    replace_unnecessary_status_probe,
    workflow_reply_for_confirmed_status,
)
from services.rcm_extraction import apply_validated_facts
from services.tts_service import TtsService
from services.stt_service import SttService
from services.call_summary import apply_call_summary
from services.smart_tone import delivery_prompt, detect_smart_tone
from services.echo_bot import echo_greeting, echo_latency_record, echo_reply, is_echo_mode
from services.streaming_tts import play_incoming_ulaw, speak_from_llm_deltas, speak_streaming
from services.turn_telemetry import TurnMetrics, TurnTimer, attach_metrics, compute_latency_summary
from services.twilio_service import TwilioService
from services.audio_utils import wav_to_mulaw
from services.verbal_composer import (
    build_precache_variants,
    polish_gemini_response,
    select_reply_variant,
    strip_robotic_payer_echo,
)
from services.voice_config import (
    correct_stt_transcript,
    followup_response,
    is_agent_echo,
    is_likely_stt_garbage,
    is_vague_answer,
    repeat_response,
    user_wants_repeat,
)
from utils.url import public_https_to_wss

logger = logging.getLogger(__name__)

# ElevenLabs HTTP itself is limited to one request. This queue only holds
# cache jobs so a phrase burst cannot run beside that request.
_CACHE_QUEUE_MAX = 8


def _spoken_text_key(text: str) -> str:
    return "say:" + " ".join((text or "").lower().split())


def _twilio_say_voice() -> str:
    """Explicitly configured Say voice, or empty when Say must not be used."""
    return str(getattr(get_settings(), "twilio_say_voice", "") or "").strip()


def _say_twiml(text: str, lang: str = "en-US") -> str:
    """Twilio Say in the configured voice; empty string when none is configured."""
    voice_name = _twilio_say_voice()
    if not voice_name or not (text or "").strip():
        return ""
    safe = xml_escape.escape(text)
    voice = xml_escape.escape(voice_name, {"'": "&apos;", '"': "&quot;"})
    return f'    <Say voice="{voice}" language="{lang}">{safe}</Say>'

# Twilio Gather (legacy HTTP fallback). Active speech path: Media Streams → STT.
GATHER_TIMEOUT = "5"  # seconds to wait for the caller to start speaking
# Upper bound for one synthesis. The webhook itself is bounded separately by
# GATHER_WEBHOOK_BUDGET_S, so this only stops a hung provider request.
GATHER_TTS_TIMEOUT_S = 8.0
# Greeting webhooks that stayed open for 6s and 6.7s never delivered an HTTP
# response through the public tunnel. Twilio then said it could not reach the
# server and hung up at 0:00. A 4.45s greeting response was accepted. Answer
# before that window; the silent pause collects the greeting or reply.
GATHER_WEBHOOK_BUDGET_S = 3.5
GATHER_CONTINUE_BUDGET_S = 3.5
# Silent continuation hops before the deferred reply is abandoned.
GATHER_MAX_PENDING_HOLDS = 6
# One silent pause per continuation. Trial accounts cap TwiML redirects per call,
# so this stays long enough that a slow turn needs only one extra hop.
GATHER_HOLD_PAUSE_S = "4"
# Generate the greeting before Twilio dials. The public tunnel dropped voice
# webhooks that stayed open while greeting audio was still being created.
GREETING_PREPARE_BUDGET_S = 8.0
RECORD_SILENCE_TIMEOUT = "1"
RECORD_MAX_LENGTH = "12"

TWILIO_GATHER_LANGS: frozenset[str] = frozenset({"en-US"})
_GATHER_HINTS = (
    "claim, authorization, preauth, reimbursement, payer, insurer, EOB, denial code, "
    "NPI, policy number, member ID, discharge summary, final bill, ICD, CPT, "
    "approved, rejected, pending, documents, yes, no, hold, callback, provider services"
)

# In-memory session store (replace with Redis in production)
call_sessions: dict[str, "CallSession"] = {}
call_records: dict[str, dict[str, Any]] = {}


@dataclass
class CallSession:
    call_id: str
    call_sid: str = ""
    stream_sid: str = ""
    public_base: str = ""
    customer_name: str = ""
    customer_phone: str = ""
    agent_name: str = "Chris"
    preferred_language: str = "en-US"
    hospital_name: str = ""
    claim_id: str = ""
    patient_name: str = ""
    claim_type: str = "Claim Pending"
    tpa_name: str = ""
    npi: str = ""
    policy_number: str = ""
    claim_age_days: int | None = None
    scenario_id: str = ""
    tpa_executive_profile: str = ""
    tpa_sector_id: str = ""
    focus_topics: list[str] = field(default_factory=list)
    scenario_snapshot: dict[str, Any] = field(default_factory=dict)
    ivr_completed: bool = False
    # Durable conversation state (also persisted to PostgreSQL)
    current_stage: str = "greeting"
    current_intent: str = ""
    authentication_done: bool = False
    appeal_started: bool = False
    conversation_history: list[dict] = field(default_factory=list)
    audio_buffer: bytearray = field(default_factory=bytearray)
    audio_pipeline: Any = None  # LiveAudioPipeline — immediate 20ms packet forwarder
    live_partial_transcript: str = ""
    is_agent_speaking: bool = False
    is_processing: bool = False
    # Barge-in: set to stop TTS immediately; clear before each speak
    tts_stop: asyncio.Event = field(default_factory=asyncio.Event)
    barge_in_count: int = 0
    generation_task: Any = None  # in-flight Gemini / reply task to cancel
    pending_utterances: list[str] = field(default_factory=list)
    silence_frames: int = 0
    last_speech_time: float = 0.0
    turn_count: int = 0
    lead_score: int = 0
    confidence_score: float = 0.0
    resolution_status: str = ""
    resolution_code: str = ""
    denial_code: str = ""
    pending_documents: list[str] = field(default_factory=list)
    expected_payment_date: str = ""
    follow_up_date: str = ""
    extracted_data: dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    websocket: Any = None
    browser_mode: bool = False
    browser_audio_parts: list[bytes] = field(default_factory=list)
    browser_live_queue: Any = None
    browser_marks: dict[str, float] = field(default_factory=dict)
    ivr_audio_bytes: bytes | None = None
    greeting_done: bool = False
    discovery_index: int = 0
    last_question_asked: str = ""
    last_question_index: int = 0
    answer_question: str = ""
    answer_topic: str = "unknown"
    current_answer_valid: bool = False
    pending_answer_context: dict[str, Any] = field(default_factory=dict)
    followup_used: set[str] = field(default_factory=set)
    recent_stt: list[str] = field(default_factory=list)
    phrase_cache: dict[str, int] = field(default_factory=dict)
    phrase_meta: dict[str, tuple[str, str]] = field(default_factory=dict)
    phrase_verbal_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    clarification_cached_topics: set[str] = field(default_factory=set)
    used_acks: list[str] = field(default_factory=list)
    last_agent_twiml_at: float = 0.0
    last_agent_audio_ms: float = 0.0
    last_audio_fetched_at: float = 0.0
    speech_first_partial_at: float = 0.0
    speech_last_partial_at: float = 0.0
    turn_metrics_serial: int = 0
    active_turn_timer: TurnTimer | None = None
    delivery_emotion: str = ""
    livekit: dict[str, Any] = field(default_factory=dict)
    pending_gemini: dict[str, Any] | None = None
    pending_twiml: dict[str, Any] | None = None
    greeting_prepare_task: Any = None
    backchannels_used: list[str] = field(default_factory=list)
    cache_token: int = 0
    playback_audio: dict[int, bytes] = field(default_factory=dict)
    playback_text: dict[int, str] = field(default_factory=dict)
    playback_lang: dict[int, str] = field(default_factory=dict)
    playback_provider: dict[int, str] = field(default_factory=dict)
    playback_voice: dict[int, str] = field(default_factory=dict)
    audio_turn_metrics: dict[int, tuple[dict[str, Any], float]] = field(default_factory=dict)
    audio_turn_id: int = 0
    last_stt_ms: float = 0.0
    _audio_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    SILENCE_THRESHOLD = 25  # ~500ms of quiet (~20ms media frames) → end utterance
    MIN_AUDIO_BYTES = 6000  # ~0.75s mulaw @ 8 kHz before cutting an utterance
    MAX_TURNS = 25
    use_media_streams: bool = True

    def add_turn(self, role: str, content: str) -> None:
        self.conversation_history.append({"role": role, "content": content})


def create_call_session(
    customer_phone: str,
    customer_name: str = "",
    agent_name: str = "Chris",
    preferred_language: str = "en-US",
    hospital_name: str = "",
    claim_id: str = "",
    patient_name: str = "",
    claim_type: str = "Claim Pending",
    tpa_name: str = "",
    npi: str = "",
    policy_number: str = "",
    claim_age_days: int | None = None,
    scenario_id: str = "",
    tpa_executive_profile: str = "",
    tpa_sector_id: str = "",
    focus_topics: list[str] | None = None,
) -> str:
    patient = patient_name or customer_name
    payer = tpa_name
    scenario = get_scenario_by_id(scenario_id) if scenario_id else None
    profile = tpa_executive_profile or (str(scenario.get("tpa_executive_profile", "")) if scenario else "")
    effective_claim_type = claim_type
    if scenario and scenario.get("claim_type") and claim_type == "Claim Pending":
        effective_claim_type = str(scenario["claim_type"])
    focus = [t for t in (focus_topics or []) if t]
    sector = (tpa_sector_id or "").strip().lower()

    call_id = str(uuid.uuid4())
    call_sessions[call_id] = CallSession(
        call_id=call_id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        agent_name=agent_name,
        preferred_language=preferred_language,
        hospital_name=hospital_name,
        claim_id=claim_id,
        patient_name=patient,
        claim_type=effective_claim_type,
        tpa_name=payer,
        npi=npi,
        policy_number=policy_number,
        claim_age_days=claim_age_days,
        scenario_id=scenario_id,
        tpa_executive_profile=profile,
        tpa_sector_id=sector,
        focus_topics=focus,
        scenario_snapshot=dict(scenario) if scenario else {},
        current_stage="greeting",
        current_intent="claim_follow_up",
        authentication_done=False,
        appeal_started="appeal" in (effective_claim_type or "").lower(),
    )
    call_records[call_id] = {
        "call_id": call_id,
        "customer_phone": customer_phone,
        "customer_name": customer_name,
        "patient_name": patient,
        "hospital_name": hospital_name,
        "claim_id": claim_id,
        "claim_type": effective_claim_type,
        "tpa_name": payer,
        "payer_name": payer,
        "npi": npi,
        "policy_number": policy_number,
        "claim_age_days": claim_age_days,
        "scenario_id": scenario_id,
        "tpa_executive_profile": profile,
        "tpa_sector_id": sector,
        "focus_topics": focus,
        "agent_name": agent_name,
        "preferred_language": preferred_language,
        "selected_language": preferred_language,
        "status": "initiated",
        "created_at": time.time(),
        "transcript": [],
        "lead_score": 0,
        "confidence_score": 0.0,
        "resolution_status": "",
        "resolution_code": "",
        "denial_code": "",
        "pending_documents": [],
        "expected_payment_date": "",
        "follow_up_date": "",
        "extracted_data": {},
        "duration_seconds": 0,
        "ivr": None,
        "conversation_state": {
            "current_stage": "greeting",
            "current_intent": "claim_follow_up",
            "authentication_done": False,
            "appeal_started": "appeal" in (effective_claim_type or "").lower(),
            "claim_number": claim_id,
            "tpa_sector_id": sector,
            "focus_topics": focus,
        },
    }
    # Persist structured state to Postgres (table created once at startup)
    try:
        from services.conversation_state_db import persist_session_state

        persist_session_state(call_sessions[call_id])
    except Exception:
        logger.exception("Initial conversation_state save failed call_id=%s", call_id)
    return call_id


def _claim_session_kwargs(session: CallSession) -> dict[str, Any]:
    return {
        "hospital_name": session.hospital_name,
        "claim_id": session.claim_id,
        "patient_name": session.patient_name,
        "claim_type": session.claim_type,
        "tpa_name": session.tpa_name,
        "npi": session.npi,
        "policy_number": session.policy_number,
        "claim_age_days": session.claim_age_days,
        "tpa_sector_id": getattr(session, "tpa_sector_id", "") or "",
        "focus_topics": list(getattr(session, "focus_topics", None) or []),
    }


def _with_tone_guidance(state_block: str, session: CallSession) -> str:
    hint = delivery_prompt(getattr(session, "delivery_emotion", "") or "")
    if not hint:
        return state_block
    return f"{state_block}\n{hint}".strip()


def _gemini_session_kwargs(session: CallSession) -> dict[str, Any]:
    scenario = session.scenario_snapshot or None
    try:
        from services.conversation_state_db import state_from_session

        state_block = state_from_session(session).to_prompt_block()
    except Exception:
        state_block = ""
    return {
        **_claim_session_kwargs(session),
        "tpa_executive_profile": session.tpa_executive_profile,
        "scenario": scenario if scenario else None,
        "rcm_collected": _rcm_collected_from_session(session),
        "conversation_state_block": _with_tone_guidance(state_block, session),
        "answer_topic": session.answer_topic,
        "answer_valid": session.current_answer_valid,
    }


def _rcm_collected_from_session(session: CallSession) -> RcmCollected:
    return RcmCollected(
        resolution_status=session.resolution_status,
        resolution_code=session.resolution_code,
        denial_code=session.denial_code,
        pending_documents=list(session.pending_documents),
        expected_payment_date=session.expected_payment_date,
        follow_up_date=session.follow_up_date,
        extracted_data=dict(session.extracted_data),
    )


MAX_SCENARIO_TURNS = 14


def _should_end_scenario_call(session: CallSession, ai: dict[str, Any]) -> bool:
    action = str(ai.get("next_action") or "").lower()
    stage = str(ai.get("conversation_stage") or "").lower()
    if action in ("end_call", "document_and_close"):
        return True
    if stage in ("ended", "call_summary"):
        return True
    if session.turn_count >= MAX_SCENARIO_TURNS:
        return True
    gaps = open_rcm_gaps(_rcm_collected_from_session(session))
    if not gaps and session.turn_count >= 4:
        return True
    return False


def session_uses_scenario_mode(session: CallSession) -> bool:
    """Use Gemini dynamic dialogue for TPA profile, exemplar, or selected sector."""
    settings = get_settings()
    if settings.low_latency_calls:
        return False
    return bool(
        session.tpa_executive_profile
        or session.scenario_id
        or getattr(session, "tpa_sector_id", "")
    )


def _sync_rcm_fields_to_record(session: CallSession, record: dict[str, Any]) -> None:
    for field in (
        "resolution_status",
        "resolution_code",
        "denial_code",
        "expected_payment_date",
        "follow_up_date",
        "pending_documents",
        "extracted_data",
    ):
        val = getattr(session, field, None)
        if val:
            record[field] = val


def _apply_ai_extraction(
    session: CallSession,
    record: dict[str, Any],
    ai_response: dict[str, Any],
    payer_text: str = "",
    question_topic: str = "",
    answer_valid: bool = True,
) -> None:
    """Merge model extraction only when the payer's words support the field."""
    evidence = (payer_text or "").lower()
    has_status = any(
        phrase in evidence
        for phrase in (
            "approved", "approval", "denied", "rejected", "paid", "pending",
            "processing", "under review",
        )
    )
    has_denial = "denial" in evidence or "denied" in evidence
    has_documents = any(
        word in evidence
        for word in ("document", "record", "report", "summary", "bill", "eob")
    )
    has_date = any(
        word in evidence
        for word in (
            "date", "today", "tomorrow", "day", "week", "month", "hour",
            "expected", "scheduled", " by ", " on ",
        )
    )

    status_allowed = not question_topic or question_topic == "claim_status"
    docs_allowed = not question_topic or question_topic == "pending_documents"
    date_allowed = not question_topic or question_topic in (
        "expected_date",
        "payment_date",
        "review_completion_date",
        "remittance",
    )
    payment_date_allowed = question_topic == "payment_date"
    review_date_allowed = question_topic == "review_completion_date"
    if (
        ai_response.get("resolution_status")
        and has_status
        and status_allowed
        and answer_valid
        and not session.resolution_status
    ):
        session.resolution_status = str(ai_response["resolution_status"])
    if (
        ai_response.get("resolution_code")
        and has_status
        and status_allowed
        and answer_valid
        and not session.resolution_code
    ):
        session.resolution_code = str(ai_response["resolution_code"])
    if (
        ai_response.get("denial_code")
        and has_denial
        and status_allowed
        and answer_valid
        and not session.denial_code
    ):
        session.denial_code = str(ai_response["denial_code"])
    if (
        ai_response.get("expected_payment_date")
        and has_date
        and payment_date_allowed
        and answer_valid
        and not session.expected_payment_date
    ):
        session.expected_payment_date = str(ai_response["expected_payment_date"])
    if (
        ai_response.get("follow_up_date")
        and has_date
        and date_allowed
        and answer_valid
        and not session.follow_up_date
    ):
        session.follow_up_date = str(ai_response["follow_up_date"])
    docs = ai_response.get("pending_documents")
    if (
        isinstance(docs, list)
        and docs
        and has_documents
        and docs_allowed
        and answer_valid
        and not session.pending_documents
    ):
        session.pending_documents = [str(d) for d in docs]
    allowed: dict[str, Any] = {}
    claim_norm = re.sub(r"[^a-z0-9]", "", (session.claim_id or "").lower())
    extracted = ai_response.get("extracted_data")
    if answer_valid and isinstance(extracted, dict) and extracted:
        for raw_key, value in extracted.items():
            key = str(raw_key)
            key_l = key.lower()
            if key_l in ("claim_number", "claim_id", "claim_no"):
                # Claim context is already authoritative. Never overwrite it from
                # model output or infer a payer reference from the claim number.
                continue
            if "payer_reference" in key_l or "control_number" in key_l:
                if (
                    question_topic in ("", "payer_reference")
                    and ("reference" in evidence or "control number" in evidence)
                ):
                    value_norm = re.sub(
                        r"[^a-z0-9]", "", str(value or "").lower()
                    )
                    if value_norm and value_norm != claim_norm:
                        allowed["payer_reference"] = value
                continue
            if "payment_reference" in key_l or "remittance_reference" in key_l:
                if (
                    question_topic in ("", "payer_reference", "remittance")
                    and
                    ("payment" in evidence or "remittance" in evidence)
                    and ("reference" in evidence or "trace" in evidence)
                ):
                    value_norm = re.sub(
                        r"[^a-z0-9]", "", str(value or "").lower()
                    )
                    if value_norm and value_norm != claim_norm:
                        allowed["payment_reference"] = value
                continue
            if "eob" in key_l or "remittance" in key_l:
                if question_topic in ("", "remittance") and (
                    "eob" in evidence or "remittance" in evidence
                ):
                    allowed[key] = value
                continue
            if any(word in key_l for word in ("reviewer", "adjuster", "queue_owner")):
                if question_topic == "reviewer" and any(
                    word in evidence for word in ("reviewer", "adjuster", "team", "queue")
                ):
                    allowed[key] = value
                continue
            if "date" in key_l:
                if review_date_allowed and has_date:
                    allowed[key] = value
                continue
            # Preserve other extracted facts only if their value was explicitly
            # spoken by the payer.
            value_text = str(value).strip().lower()
            if not question_topic and value_text and value_text in evidence:
                allowed[key] = value
        for key, value in allowed.items():
            session.extracted_data.setdefault(key, value)
    else:
        extracted = {}
    conf = ai_response.get("confidence_score")
    if answer_valid and conf is not None and (
        has_status or has_denial or has_documents or has_date or bool(allowed)
    ):
        try:
            session.confidence_score = float(conf)
            session.lead_score = int(float(conf) * 100)
        except (TypeError, ValueError):
            pass

    stage = str(ai_response.get("conversation_stage") or "").strip()
    if stage:
        session.current_stage = stage
    intent = str(
        ai_response.get("next_action")
        or ai_response.get("negotiation_technique")
        or session.current_intent
        or ""
    ).strip()
    if intent:
        session.current_intent = intent

    stage_l = (session.current_stage or "").lower()
    if session.ivr_completed or stage_l not in ("", "greeting", "ivr_navigation"):
        if stage_l != "ivr_navigation":
            session.authentication_done = session.authentication_done or stage_l not in (
                "greeting",
                "ivr_navigation",
            )
    if session.ivr_completed:
        session.authentication_done = True

    action_l = str(ai_response.get("next_action") or "").lower()
    claim_type_l = (session.claim_type or "").lower()
    if (
        "appeal" in claim_type_l
        or "appeal" in action_l
        or "appeal" in stage_l
        or any("appeal" in str(v).lower() for v in allowed.values())
    ):
        session.appeal_started = True

    for field in (
        "resolution_status",
        "resolution_code",
        "denial_code",
        "expected_payment_date",
        "follow_up_date",
        "pending_documents",
        "extracted_data",
        "confidence_score",
        "lead_score",
    ):
        val = getattr(session, field, None)
        if val:
            record[field] = val

    if session.hospital_name and session.claim_id:
        claim_label = f"{session.hospital_name} / {session.claim_id}"
    else:
        claim_label = session.claim_id or ""
    record["conversation_state"] = {
        "current_claim": claim_label,
        "current_caller": session.customer_name
        or session.patient_name
        or session.customer_phone,
        "current_intent": session.current_intent,
        "current_stage": session.current_stage,
        "authentication_done": session.authentication_done,
        "appeal_started": session.appeal_started,
        "claim_number": session.claim_id,
    }
    try:
        from services.conversation_state_db import persist_session_state

        persist_session_state(session)
    except Exception:
        logger.exception("conversation_state save failed call_id=%s", session.call_id)


class ConversationEngine:
    def __init__(self) -> None:
        self.tts = TtsService()
        self.stt = SttService()
        self.gemini = GeminiService()
        self.twilio = TwilioService()
        self._cache_queue: asyncio.Queue | None = None
        self._cache_worker: asyncio.Task | None = None

    def _xml_url(self, url: str) -> str:
        return xml_escape.escape(url, {"'": "&apos;", '"': "&quot;"})

    @staticmethod
    def _apply_tone_signal(
        session: CallSession, transcript: str, model_tone: object
    ) -> None:
        """Tone can affect delivery only; it can never create or modify claim facts."""
        enabled = bool(get_settings().smart_tone_detection_enabled)
        signal = detect_smart_tone(transcript, enabled=enabled)
        session.last_tone = signal
        tone = signal.get("tone") if signal.get("operational") else None
        if not tone and enabled and model_tone:
            candidate = str(model_tone).strip().lower()
            if candidate in {"frustrated", "confused", "impatient"}:
                tone = candidate
        if tone:
            session.delivery_emotion = str(tone)

    def build_media_stream_twiml(self, session: CallSession, base_url: str) -> str:
        """Bidirectional Media Streams — continuous audio to WebSocket → STT."""
        wss = public_https_to_wss(base_url)
        stream_url = self._xml_url(f"{wss}/api/webhooks/media-stream?call_id={session.call_id}")
        call_id_safe = xml_escape.escape(session.call_id, {"'": "&apos;", '"': "&quot;"})
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{stream_url}" track="inbound_track">
            <Parameter name="call_id" value="{call_id_safe}" />
        </Stream>
    </Connect>
</Response>"""

    def _ensure_question_in_response(
        self, text: str, lang: str, index: int, *, dynamic: bool = False
    ) -> str:
        """Ensure a question mark; scripted path uses checklist, dynamic trusts Gemini wording."""
        if "?" in text:
            return text
        if dynamic:
            return f"{text.rstrip('.')} Could you help me with an update on this claim?"
        q = next_discovery_question(lang, index)
        return f"{text.rstrip('.')} {q}"

    def _extract_question(self, text: str) -> str:
        """Pull the last question sentence from agent speech."""
        if "?" not in text:
            return text
        parts = text.replace("!", ".").replace("?", "?|").split("|")
        for part in reversed(parts):
            if "?" in part:
                return part.strip()
        return text

    def _set_last_question(
        self,
        session: CallSession,
        text: str,
        index: int | None = None,
        *,
        preserve_answer_context: bool = False,
    ) -> None:
        question = self._extract_question(text)
        session.last_question_asked = question
        if not preserve_answer_context:
            session.answer_question = question
            session.answer_topic = classify_question(question)
            session.pending_answer_context.clear()
            self._precache_clarifications_async(session, session.answer_topic)
        if index is not None:
            session.last_question_index = index

    def _precache_clarifications_async(self, session: CallSession, topic: str) -> None:
        """Generate this topic's fixed clarification audio while the caller answers."""
        if not session.call_sid or topic in session.clarification_cached_topics:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        session.clarification_cached_topics.add(topic)
        lang = session.preferred_language

        async def _run() -> None:
            for text in clarification_phrases(topic):
                await self._enqueue_cache_phrase(
                    (session, _spoken_text_key(text), text, lang, None)
                )

        asyncio.create_task(_run())

    def _interpret_customer_answer(
        self,
        session: CallSession,
        transcript: str,
        confidence: float | None,
    ) -> AnswerValidation:
        pending_date = str(
            session.pending_answer_context.get("candidate_date") or ""
        ).strip()
        was_pending_date = bool(pending_date)
        if pending_date:
            result = resolve_pending_date(pending_date, transcript)
            if result.ambiguous:
                # Keep a volunteered date for one clarification turn only.
                # If its meaning is still unclear, discard it rather than
                # repeatedly asking or assigning it to a guessed field.
                session.pending_answer_context.clear()
        else:
            result = validate_answer(
                transcript,
                session.answer_question or session.last_question_asked,
                confidence,
            )
        secondary_facts = extract_explicit_secondary_facts(transcript)
        if session.answer_topic != "payment_date":
            secondary_facts.pop("expected_date", None)
        if session.answer_topic != "review_completion_date":
            secondary_facts.pop("review_completion_date", None)
        for key, value in secondary_facts.items():
            result.facts.setdefault(key, value)
        if secondary_facts:
            apply_validated_facts(session, secondary_facts)
        session.current_answer_valid = result.accepted and not result.ambiguous
        if (
            not was_pending_date
            and result.reason == "date_wrong_topic"
            and result.facts.get("candidate_date")
        ):
            session.pending_answer_context = {
                "candidate_date": result.facts["candidate_date"],
                "original_question": session.answer_question
                or session.last_question_asked,
            }
        elif session.current_answer_valid:
            session.pending_answer_context.clear()
            apply_validated_facts(session, result.facts)
        return result

    def _persist_call_async(self, call_id: str) -> None:
        asyncio.create_task(asyncio.to_thread(self._persist_call, call_id, False))

    async def _cache_phrase(
        self,
        session: CallSession,
        key: str,
        text: str,
        lang: str,
        verbal_meta: dict[str, Any] | None = None,
    ) -> int | None:
        if key in session.phrase_cache:
            return session.phrase_cache[key]
        token = session.cache_token
        turn = await self._store_agent_audio(
            session,
            text,
            lang,
            fast=True,
            allow_fallback=False,
            live=False,
        )
        if turn is None or token != session.cache_token:
            return None
        session.phrase_cache[key] = turn
        session.phrase_meta[key] = (text, lang)
        if verbal_meta:
            session.phrase_verbal_meta[key] = verbal_meta
        return turn

    async def precache_intro(self, session: CallSession) -> None:
        """Pre-generate greeting audio so the first question plays instantly."""
        if "greeting" in session.phrase_cache:
            return
        lang = session.preferred_language
        if session_uses_scenario_mode(session):
            greeting_result = await self._gemini_greeting(session, lang)
            greeting = self._ensure_question_in_response(
                greeting_result.get("response_text") or fallback_greeting(
                    session.agent_name,
                    session.customer_name,
                    lang,
                    hospital_name=session.hospital_name,
                    claim_id=session.claim_id,
                    patient_name=session.patient_name,
                    claim_type=session.claim_type,
                    npi=session.npi,
                    policy_number=session.policy_number,
                ),
                lang,
                0,
                dynamic=True,
            )
        else:
            greeting = self._ensure_question_in_response(
                fallback_greeting(
                    session.agent_name,
                    session.customer_name,
                    lang,
                    hospital_name=session.hospital_name,
                    claim_id=session.claim_id,
                    patient_name=session.patient_name,
                    claim_type=session.claim_type,
                    npi=session.npi,
                    policy_number=session.policy_number,
                ),
                lang,
                0,
            )
        await self._cache_phrase(session, "greeting", greeting, lang)

    async def _precache_indices(
        self, session: CallSession, indices: list[int] | None = None
    ) -> None:
        """Cache TTS for discovery reply variants, repeats, and followups."""
        lang = session.preferred_language
        questions = discovery_questions(lang)
        if indices is None:
            indices = list(range(len(questions)))

        jobs = []
        for i in indices:
            if i >= len(questions):
                continue
            q = questions[i]
            for text, verbal in build_precache_variants(lang, i):
                key = verbal["cache_key"]
                jobs.append((session, key, text, lang, verbal))
            jobs.append((session, f"repeat:{i}", repeat_response(lang, q), lang, None))
            jobs.append((session, f"followup:{i}", followup_response(lang, q), lang, None))

        for job in jobs:
            await self._enqueue_cache_phrase(job)
        if self._cache_queue is not None and jobs:
            await self._cache_queue.join()

    async def precache_immediate_replies(self, session: CallSession) -> None:
        """Cache all discovery replies — every turn must play instantly."""
        await self.precache_replies(session)

    async def prepare_greeting_before_dial(self, session: CallSession) -> bool:
        """Create greeting audio before the outbound call is placed.

        Returns True when the greeting clip is already cached. A slow provider
        does not cancel the work; the voice webhook can still use it if it
        finishes, or return a silent continuation if it does not.
        """
        started = time.perf_counter()
        task = asyncio.create_task(
            self.precache_intro(session),
            name=f"greeting-prepare-{session.call_id}",
        )
        session.greeting_prepare_task = task
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=GREETING_PREPARE_BUDGET_S)
        except asyncio.TimeoutError:
            logger.warning(
                "Greeting audio still preparing at dial call_id=%s budget_s=%.1f",
                session.call_id,
                GREETING_PREPARE_BUDGET_S,
            )
            return False
        except Exception:
            logger.exception("Greeting prepare failed call_id=%s", session.call_id)
            return False
        ready = "greeting" in session.phrase_cache
        logger.info(
            "Greeting prepare finished call_id=%s elapsed_ms=%.0f cached=%s",
            session.call_id,
            (time.perf_counter() - started) * 1000,
            ready,
        )
        return ready

    async def warm_session_cache(self, session: CallSession) -> None:
        """Pre-generate intro and discovery reply variants (all modes)."""
        try:
            task = session.greeting_prepare_task
            if task is not None:
                await task
            else:
                await self.precache_intro(session)
            await self.precache_backchannels(session)
            # Dynamic scenario replies are generated by Gemini and never read
            # the scripted reply cache. Avoid dozens of unused paid TTS calls.
            if not session_uses_scenario_mode(session):
                asyncio.create_task(self.precache_replies(session))
            logger.info(
                "Intro cache ready for call %s (scenario_mode=%s)",
                session.call_id,
                session_uses_scenario_mode(session),
            )
        except Exception as e:
            logger.warning("Warm cache failed for call %s: %s", session.call_id, e)

    def reset_audio_cache(self, session: CallSession) -> None:
        """Clear cached TTS when caller picks a different language."""
        session.cache_token += 1
        session.phrase_cache.clear()
        session.phrase_meta.clear()
        session.phrase_verbal_meta.clear()
        session.clarification_cached_topics.clear()
        session.playback_audio.clear()
        session.playback_text.clear()
        session.playback_lang.clear()
        session.playback_provider.clear()
        session.playback_voice.clear()
        session.audio_turn_metrics.clear()
        session.audio_turn_id = 0

    async def precache_backchannels(self, session: CallSession) -> None:
        lang = session.preferred_language
        for i, text in enumerate(BACKCHANNEL_PHRASES):
            await self._cache_phrase(session, f"bc:{i}", text, lang)

    async def precache_replies(self, session: CallSession) -> None:
        """Background: cache discovery reply variants while the caller speaks."""
        lang = session.preferred_language
        questions = discovery_questions(lang)
        remaining = [
            i
            for i in range(len(questions))
            if f"reply:{i}:v0" not in session.phrase_cache
        ]
        await self._precache_indices(session, remaining)
        logger.info("Precached %s phrases for call %s", len(session.phrase_cache), session.call_id)

    def _precache_next_reply_async(self, session: CallSession) -> None:
        """Fire-and-forget: cache the next discovery reply while caller speaks."""
        idx = session.discovery_index
        if f"reply:{idx}:v0" in session.phrase_cache:
            return
        asyncio.create_task(self._precache_indices(session, [idx]))

    def _needs_gemini(self, session: CallSession, transcript: str) -> bool:
        """Use Gemini when a TPA profile/scenario is selected, or at workflow close."""
        if session_uses_scenario_mode(session):
            return True
        questions = discovery_questions(session.preferred_language)
        if session.discovery_index >= len(questions):
            return True
        if is_vague_answer(transcript):
            return False
        return False

    def _stt_looks_wrong(self, session: CallSession, transcript: str) -> bool:
        """Detect repeated identical STT (bad audio) — treat as not heard."""
        if not transcript:
            return True
        session.recent_stt.append(transcript.strip().lower())
        session.recent_stt = session.recent_stt[-3:]
        if len(session.recent_stt) >= 2 and len(set(session.recent_stt)) == 1:
            logger.warning("Repeated identical STT %r — likely bad recording", transcript)
            return True
        return False

    async def transcribe_recording(
        self, recording_url: str, lang_hint: str, min_duration: float = 0.0
    ) -> str:
        """Legacy Record fallback — Media Streams path uses continuous STT instead."""
        del recording_url, lang_hint, min_duration
        logger.info("Recording STT skipped — use Twilio Media Streams continuous STT")
        return ""

    async def clear_twilio_audio(self, session: CallSession) -> None:
        """Twilio Media Streams equivalent of response.stream_stop() — clear queued TTS."""
        if not session.websocket or not session.stream_sid:
            return
        try:
            await session.websocket.send_text(
                json.dumps({"event": "clear", "streamSid": session.stream_sid})
            )
            logger.info("Twilio stream cleared (barge-in) call=%s", session.call_id)
        except Exception as e:
            logger.warning("Twilio clear failed call=%s: %s", session.call_id, e)

    async def cancel_generation(self, session: CallSession) -> None:
        """Cancel in-flight Gemini / reply generation (LLM stream stop)."""
        pending = session.pending_gemini
        if pending and isinstance(pending, dict):
            task = pending.get("task")
            if task is not None and hasattr(task, "cancel"):
                task.cancel()
                logger.info("Cancelled pending Gemini task call=%s", session.call_id)
            session.pending_gemini = None

        gen = session.generation_task
        if gen is not None and hasattr(gen, "cancel") and not gen.done():
            gen.cancel()
            logger.info("Cancelled generation_task call=%s", session.call_id)
        session.generation_task = None

    async def handle_barge_in(self, session: CallSession) -> None:
        """
        Human interrupted while bot speaking / generating.

        Bot speaking → VAD speech → stop TTS (Twilio clear) → cancel Gemini → listen.
        """
        if not (
            session.is_agent_speaking
            or session.is_processing
            or session.generation_task is not None
        ):
            return

        logger.info(
            "Barge-in detected call=%s — stopping TTS and cancelling generation",
            session.call_id,
        )
        session.tts_stop.set()
        session.barge_in_count += 1
        await self.clear_twilio_audio(session)
        await self.cancel_generation(session)
        session.is_agent_speaking = False

        record = call_records.get(session.call_id)
        if record is not None:
            record.setdefault("barge_ins", []).append({"at": time.time()})
            record["barge_in_count"] = session.barge_in_count

    async def send_audio_to_twilio(
        self,
        session: CallSession,
        mulaw_audio: bytes,
        *,
        finalize: bool = True,
    ) -> None:
        if session.browser_mode:
            if mulaw_audio:
                session.browser_audio_parts.append(mulaw_audio)
                live_q = session.browser_live_queue
                if live_q is not None:
                    for offset in range(0, len(mulaw_audio), 640):
                        live_q.put_nowait(mulaw_audio[offset : offset + 640])
            return
        if not session.websocket or not session.stream_sid:
            return

        if session.tts_stop.is_set():
            await self.clear_twilio_audio(session)
            session.is_agent_speaking = False
            return

        session.is_agent_speaking = True
        timer = session.active_turn_timer
        # 80ms frames — fewer WS messages, smoother phone audio than 20ms drip
        chunk_size = 640
        twilio_overhead_ms = 0.0
        # Prefill Twilio jitter buffer so speech doesn't stutter at the start
        prefill_chunks = 4
        chunk_index = 0

        for i in range(0, len(mulaw_audio), chunk_size):
            if session.tts_stop.is_set():
                # Human barged in — stop mid-utterance
                await self.clear_twilio_audio(session)
                session.is_agent_speaking = False
                logger.info("TTS stopped mid-stream (barge-in) call=%s", session.call_id)
                return
            chunk = mulaw_audio[i : i + chunk_size]
            if timer is not None:
                timer.mark_playback_started()
            # Pad final short frame so Twilio doesn't click/cut the last phoneme
            if len(chunk) < chunk_size:
                chunk = chunk + bytes([0xFF] * (chunk_size - len(chunk)))
            payload = base64.b64encode(chunk).decode("utf-8")
            message = {
                "event": "media",
                "streamSid": session.stream_sid,
                "media": {"payload": payload},
            }
            try:
                t_send = time.perf_counter()
                await session.websocket.send_text(json.dumps(message))
                twilio_overhead_ms += (time.perf_counter() - t_send) * 1000
            except Exception as e:
                logger.warning("Media Streams send failed: %s", e)
                break
            # Pace slightly under realtime after prefill (80ms audio → ~70ms sleep)
            if chunk_index >= prefill_chunks:
                await asyncio.sleep(0.070)
            chunk_index += 1

        if timer is not None and twilio_overhead_ms > 0:
            timer.add_twilio_ms(twilio_overhead_ms)

        if finalize and not session.tts_stop.is_set():
            mark_message = {
                "event": "mark",
                "streamSid": session.stream_sid,
                "mark": {"name": "agent_done"},
            }
            try:
                t_mark = time.perf_counter()
                await session.websocket.send_text(json.dumps(mark_message))
                if timer is not None:
                    timer.add_twilio_ms((time.perf_counter() - t_mark) * 1000)
            except Exception:
                pass
            session.is_agent_speaking = False
        elif session.tts_stop.is_set():
            session.is_agent_speaking = False

    async def _send_mulaw_frame(self, session: CallSession, frame: bytes) -> None:
        if session.tts_stop.is_set() or not session.websocket or not session.stream_sid:
            return
        if len(frame) < 640:
            frame = frame + bytes([0xFF] * (640 - len(frame)))
        session.is_agent_speaking = True
        timer = session.active_turn_timer
        if timer is not None:
            timer.mark_playback_started()
        payload = base64.b64encode(frame).decode("utf-8")
        started = time.perf_counter()
        await session.websocket.send_text(
            json.dumps(
                {
                    "event": "media",
                    "streamSid": session.stream_sid,
                    "media": {"payload": payload},
                }
            )
        )
        if timer is not None:
            timer.add_twilio_ms((time.perf_counter() - started) * 1000)

    async def _play_provider_chunks(
        self,
        session: CallSession,
        text: str,
        emotion: str,
        timer: TurnTimer | None,
    ) -> dict[str, Any] | None:
        """Start phone audio while the TTS provider is still generating it."""
        supports = getattr(self.tts, "supports_chunk_stream", None)
        iterate = getattr(self.tts, "iter_phone_chunks", None)
        if not callable(supports) or not callable(iterate) or not supports():
            return None
        if not session.websocket or not session.stream_sid:
            return None
        try:
            played = await play_incoming_ulaw(
                iterate(text, delivery_emotion=emotion),
                lambda frame: self._send_mulaw_frame(session, frame),
                should_stop=session.tts_stop.is_set,
            )
        except Exception as exc:
            logger.warning(
                "Chunked TTS failed call=%s (%s). Using full synthesis.",
                session.call_id,
                type(exc).__name__,
            )
            return None
        if int(played.get("bytes") or 0) < 400:
            return None
        if timer is not None and played.get("first_audio_ms") is not None:
            timer.set_first_audio_ms(float(played["first_audio_ms"]))
            timer.set_first_tts_chunk_ms(float(played["first_audio_ms"]))
        if session.tts_stop.is_set():
            await self.clear_twilio_audio(session)
            session.is_agent_speaking = False
        elif session.websocket and session.stream_sid:
            try:
                await session.websocket.send_text(
                    json.dumps(
                        {
                            "event": "mark",
                            "streamSid": session.stream_sid,
                            "mark": {"name": "agent_done"},
                        }
                    )
                )
            except Exception:
                pass
            session.is_agent_speaking = False
        return {
            "units": 1,
            "first_audio_ms": played.get("first_audio_ms"),
            "streaming": True,
            "chunked": True,
            "interrupted": bool(played.get("interrupted")),
            "tts_provider": self.tts.provider_name(),
        }

    async def speak(
        self,
        session: CallSession,
        text: str,
        language_code: str | None = None,
        *,
        cached_turn: int | None = None,
        streaming: bool = True,
    ) -> dict[str, Any]:
        """
        Speak on Media Streams.

        Streaming TTS (default): first sentence → speech immediately, then remaining units.
        Barge-in: session.tts_stop stops playback immediately (Twilio clear).
        """
        lang = language_code or session.preferred_language
        # Never speak model JSON / metadata — plain English only
        from services.gemini_service import looks_like_json_blob, sanitize_spoken_response

        clean_text = sanitize_spoken_response(text or "")
        if not clean_text:
            if looks_like_json_blob(text or ""):
                logger.warning(
                    "Blocked JSON blob from TTS call=%s preview=%r",
                    session.call_id,
                    (text or "")[:100],
                )
            return {
                "units": 0,
                "first_audio_ms": None,
                "streaming": False,
                "interrupted": False,
                "skipped": True,
            }
        text = clean_text
        stats: dict[str, Any] = {
            "units": 1,
            "first_audio_ms": None,
            "streaming": False,
            "interrupted": False,
        }
        # Allow this speak to run until a new barge-in
        session.tts_stop.clear()
        # Browser playback cannot start until this function returns, so one
        # TTS request is faster than synthesizing each sentence in series.
        if session.browser_mode:
            streaming = False
        try:
            if cached_turn is not None and cached_turn in session.playback_audio:
                mulaw = wav_to_mulaw(session.playback_audio[cached_turn])
                t0 = time.perf_counter()
                await self.send_audio_to_twilio(session, mulaw)
                stats["first_audio_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                stats["interrupted"] = session.tts_stop.is_set()
                return stats

            emotion = getattr(session, "delivery_emotion", "") or ""
            timer = session.active_turn_timer

            if streaming:
                streamed = await self._play_provider_chunks(session, text, emotion, timer)
                if streamed is not None:
                    stats.update(streamed)
                    stats["streaming"] = True
                    stats["interrupted"] = session.tts_stop.is_set()
                    return stats

            if not streaming:
                t0 = time.perf_counter()
                audio = await self.tts.text_to_speech(
                    text,
                    language_code=lang,
                    for_twilio=True,
                    delivery_emotion=emotion,
                )
                synth_ms = (time.perf_counter() - t0) * 1000
                stats["first_audio_ms"] = round(synth_ms, 1)
                if timer is not None:
                    timer.add_tts_ms(synth_ms)
                    timer.set_first_tts_chunk_ms(synth_ms)
                    timer.set_first_audio_ms(synth_ms)
                await self.send_audio_to_twilio(session, audio)
                stats["interrupted"] = session.tts_stop.is_set()
                return stats

            async def _synthesize(unit: str) -> bytes:
                if session.tts_stop.is_set():
                    return b""
                t0 = time.perf_counter()
                audio = await self.tts.text_to_speech(
                    unit,
                    language_code=lang,
                    for_twilio=True,
                    delivery_emotion=emotion,
                )
                synth_ms = (time.perf_counter() - t0) * 1000
                if timer is not None:
                    timer.add_tts_ms(synth_ms)
                    timer.set_first_tts_chunk_ms(synth_ms)
                return audio

            async def _play(mulaw: bytes, finalize: bool) -> None:
                if session.tts_stop.is_set():
                    await self.clear_twilio_audio(session)
                    session.is_agent_speaking = False
                    return
                if not mulaw or len(mulaw) < 400:
                    logger.error(
                        "Skipping empty TTS chunk call=%s finalize=%s",
                        session.call_id,
                        finalize,
                    )
                    return
                # Keep is_agent_speaking True across sentence units so echo
                # does not barge-in and chop the agent mid-reply
                await self.send_audio_to_twilio(session, mulaw, finalize=finalize)

            def _should_stop() -> bool:
                return session.tts_stop.is_set()

            stream_stats = await speak_streaming(
                text=text,
                synthesize=_synthesize,
                play=_play,
                call_id=session.call_id,
                should_stop=_should_stop,
            )
            stats.update(stream_stats)
            stats["streaming"] = True
            stats["interrupted"] = session.tts_stop.is_set()
            stats["tts_provider"] = self.tts.provider_name()
            if timer is not None and stats.get("first_audio_ms") is not None:
                timer.set_first_audio_ms(float(stats["first_audio_ms"]))
            logger.info(
                "Spoke call=%s provider=%s units=%s first_audio_ms=%s interrupted=%s",
                session.call_id,
                stats.get("tts_provider"),
                stats.get("units"),
                stats.get("first_audio_ms"),
                stats.get("interrupted"),
            )
            return stats
        except Exception as e:
            logger.error("TTS failed on Media Streams: %s", e)
            return stats
        finally:
            # Never leave the pipeline stuck deaf after TTS
            session.is_agent_speaking = False
            pipeline = getattr(session, "audio_pipeline", None)
            if pipeline is not None and getattr(pipeline, "_vad", None) is not None:
                try:
                    pipeline._vad.reset()
                except Exception:
                    pass

    async def _stream_say(
        self,
        session: CallSession,
        text: str,
        lang: str,
        *,
        stage: str,
        path: str,
        cached_turn: int | None = None,
        end_call: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = call_records.get(session.call_id, {})
        tts_stats = await self.speak(
            session, text, lang, cached_turn=cached_turn, streaming=True
        )
        session.add_turn("assistant", text)
        entry: dict[str, Any] = {
            "role": "agent",
            "text": text,
            "language": lang,
            "stage": stage,
            "source": "media_streams",
            "tts": {
                "streaming": bool(tts_stats.get("streaming")),
                "units": tts_stats.get("units"),
                "first_audio_ms": tts_stats.get("first_audio_ms"),
            },
        }
        if extra:
            entry.update(extra)
        agent_entry = attach_metrics(entry, self._finish_turn_metrics(session, path))
        record.setdefault("transcript", []).append(agent_entry)
        session.last_agent_twiml_at = time.time()
        self._persist_call_async(session.call_id)
        if end_call:
            session.status = "completed"
            record["status"] = "completed"
            if session.call_sid:
                try:
                    self.twilio.hangup(session.call_sid)
                except Exception as e:
                    logger.warning("Hangup after Media Streams close failed: %s", e)
        return tts_stats

    async def process_user_audio(self, session: CallSession) -> None:
        """Legacy entry — prefer process_stream_utterance via LiveAudioPipeline."""
        if session.is_processing or len(session.audio_buffer) < session.MIN_AUDIO_BYTES:
            session.audio_buffer.clear()
            return
        audio_data = bytes(session.audio_buffer)
        session.audio_buffer.clear()
        transcript = await self.stt.transcribe_mulaw(
            audio_data, language_code=session.preferred_language
        )
        await self.process_stream_utterance(session, transcript or "")

    async def handle_partial_transcript(
        self, session: CallSession, text: str, is_final: bool
    ) -> None:
        """Streaming STT partial / final — drives live frontend captions."""
        session.live_partial_transcript = text
        record = call_records.get(session.call_id)
        if record is not None:
            provider = getattr(self.stt, "last_provider", "") or self.stt.model_name
            record["live_transcript"] = {
                "text": text,
                "is_final": is_final,
                "updated_at": time.time(),
                "model": provider,
                "source": "streaming_stt",
            }
            record["updated_at"] = time.time()
            if not is_final and text:
                history = record.setdefault("partial_transcripts", [])
                if isinstance(history, list):
                    if not history or history[-1].get("text") != text:
                        history.append({"text": text, "at": time.time()})
                        if len(history) > 40:
                            del history[:-40]

    async def process_stream_utterance(
        self, session: CallSession, transcript: str
    ) -> None:
        """
        Dialogue for one finalized streaming-STT utterance.

        Echo mode: Human → transcript → Hello Back / echo (no LLM).
        AI mode: full claims dialogue with streaming TTS replies.
        """
        # Never promote an interim caption to a final claim answer. The STT provider
        # must explicitly finalize the transcript before validation can consume it.
        text = (transcript or "").strip()
        # Drop only clear garbage / near-full agent echo (do not over-filter real replies)
        last_agent = ""
        for turn in reversed(session.conversation_history or []):
            if turn.get("role") == "assistant":
                last_agent = str(turn.get("content") or "")
                break
        if text and is_likely_stt_garbage(text):
            logger.warning(
                "Ignoring garbage STT call=%s chars=%d",
                session.call_id,
                len(text),
            )
            text = ""
        elif text and is_agent_echo(text, last_agent):
            logger.warning(
                "Ignoring agent-echo STT call=%s chars=%d",
                session.call_id,
                len(text),
            )
            text = ""

        record = call_records.get(session.call_id, {})
        if not text:
            # STT returned empty — usually Gemini quota. Mark UI so user sees the miss.
            pipeline = getattr(session, "audio_pipeline", None)
            stt_err = (
                getattr(self.stt, "last_error", "")
                or getattr(pipeline, "last_stt_error", "")
                or ""
            )
            logger.info(
                "Empty transcript call=%s provider=%s err=%s — keep listening",
                session.call_id,
                getattr(self.stt, "last_provider", "") or "?",
                (stt_err or "")[:120],
            )
            if record is not None:
                record["live_transcript"] = {
                    "text": "",
                    "is_final": True,
                    "updated_at": time.time(),
                    "missed": True,
                    "error": (stt_err or "")[:200],
                    "source": "streaming_stt",
                }
                record["updated_at"] = time.time()
            hard_failure = stt_err in {
                "finalize_timeout",
                "reader_stopped",
                "send_failed",
                "timeout",
                "keepalive_failed",
            }
            if (
                hard_failure
                and session.greeting_done
                and not session.is_agent_speaking
                and pipeline is not None
            ):
                pipeline.last_stt_error = ""
                try:
                    await self.speak(
                        session,
                        "Sorry, I didn't catch that. Could you say that once more?",
                        session.preferred_language,
                    )
                except Exception:
                    logger.warning("STT fallback prompt failed call=%s", session.call_id)
            return

        # Push heard text to UI immediately (before LLM) so live conversation updates
        await self.handle_partial_transcript(session, text, True)

        if session.is_processing:
            # Preserve finalized utterance order while the prior turn unwinds.
            # Bound the queue so network retries cannot grow memory indefinitely.
            if len(session.pending_utterances) < 3:
                session.pending_utterances.append(text)
            else:
                logger.warning(
                    "Pending utterance queue full call=%s; keeping earlier speech",
                    session.call_id,
                )
            if session.is_agent_speaking or session.generation_task is not None:
                await self.handle_barge_in(session)
            return

        session.is_processing = True
        session.last_speech_time = 0.0
        t0 = time.perf_counter()
        try:
            pipeline = getattr(session, "audio_pipeline", None)
            confidence: float | None = None
            if pipeline is not None:
                session.last_stt_ms = float(getattr(pipeline, "last_stt_ms", 0) or 0)
                raw_confidence = getattr(pipeline, "last_stt_confidence", None)
                confidence = (
                    float(raw_confidence) if raw_confidence is not None else None
                )
            self.begin_customer_turn(session, confidence, len(text))

            if is_echo_mode(get_settings().dialogue_mode):
                await self._handle_echo_turn(session, text, started_at=t0)
            else:
                await self.handle_stream_speech(session, text, confidence=confidence)
            session.live_partial_transcript = ""
            if record is not None:
                # Clear in-progress caption once turn is committed to transcript
                lt = record.get("live_transcript")
                if isinstance(lt, dict) and lt.get("is_final"):
                    record["live_transcript"] = {
                        **lt,
                        "committed": True,
                        "updated_at": time.time(),
                    }
        except asyncio.CancelledError:
            logger.info("process_stream_utterance cancelled (barge-in) call=%s", session.call_id)
            # Do not re-raise — would kill the audio pipeline worker
        except Exception as e:
            logger.exception("Media Streams process_stream_utterance failed: %s", e)
            try:
                from services.observability import capture_exception

                capture_exception(e, call_id=session.call_id, path="process_stream_utterance")
            except Exception:
                pass
            if not session.tts_stop.is_set():
                try:
                    await self.speak(
                        session,
                        "Sorry, I didn't catch that. Could you repeat?",
                        session.preferred_language,
                    )
                except Exception:
                    pass
        finally:
            session.is_processing = False
            pending = (
                session.pending_utterances.pop(0).strip()
                if session.pending_utterances
                else ""
            )
            if pending:
                asyncio.create_task(
                    self.process_stream_utterance(session, pending),
                    name=f"pending-utt-{session.call_id}",
                )

    async def _handle_echo_turn(
        self, session: CallSession, transcript: str, *, started_at: float
    ) -> None:
        """Echo Bot foundation: transcript → reply → streaming TTS; log latency."""
        record = call_records.get(session.call_id, {})
        lang = session.preferred_language
        raw = (transcript or "").strip()

        if not raw:
            reply = echo_reply("")
            tts_stats = await self._stream_say(
                session, reply, lang, stage="echo", path="echo_empty"
            )
            self._record_echo_latency(session, record, raw, reply, started_at, tts_stats)
            return

        session.add_turn("user", raw)
        session.turn_count += 1
        cust: dict[str, Any] = {
            "role": "customer",
            "text": raw,
            "language": lang,
            "source": "media_streams",
            "stage": "echo",
        }
        partial = self._customer_metrics_partial(session)
        if partial:
            cust["at"] = time.time()
            cust["metrics"] = partial
        record.setdefault("transcript", []).append(cust)

        reply = echo_reply(raw)
        tts_stats = await self._stream_say(
            session, reply, lang, stage="echo", path="echo"
        )
        self._record_echo_latency(session, record, raw, reply, started_at, tts_stats)
        logger.info(
            "Echo Bot call=%s in=%r out=%r first_audio_ms=%s total_ms=%.0f",
            session.call_id,
            raw[:80],
            reply[:80],
            tts_stats.get("first_audio_ms"),
            (time.perf_counter() - started_at) * 1000,
        )

    def _record_echo_latency(
        self,
        session: CallSession,
        record: dict,
        transcript: str,
        reply: str,
        started_at: float,
        tts_stats: dict[str, Any],
    ) -> None:
        first_ms = tts_stats.get("first_audio_ms")
        # Approximate STT-final → first audio: handler start to first TTS unit ready
        stt_to_first = float(first_ms) if first_ms is not None else None
        total_ms = round((time.perf_counter() - started_at) * 1000, 1)
        entry = echo_latency_record(
            transcript=transcript,
            reply=reply,
            stt_to_first_audio_ms=stt_to_first,
            total_handler_ms=total_ms,
        )
        record.setdefault("echo_latency", []).append(entry)
        record["echo_last"] = entry
        self._persist_call_async(session.call_id)

    async def handle_stream_speech(
        self,
        session: CallSession,
        speech_text: str,
        confidence: float | None = None,
    ) -> None:
        """Same dialogue rules as HTTP Gather path, delivered over Media Streams TTS."""
        record = call_records.get(session.call_id, {})
        raw = speech_text.strip()
        transcript = correct_stt_transcript(raw, session.last_question_asked)
        # Soft reject only — avoid re-asking immediately and blocking listen again
        if transcript and is_likely_stt_garbage(transcript):
            logger.warning(
                "STT garbage call=%s chars=%d — skip turn",
                session.call_id,
                len(transcript),
            )
            return
        if transcript and is_agent_echo(transcript, session.last_question_asked):
            logger.warning(
                "STT echo call=%s chars=%d — skip turn",
                session.call_id,
                len(transcript),
            )
            return

        lang = session.preferred_language
        questions = discovery_questions(lang)

        if not transcript:
            idx = session.last_question_index
            reprompt = repeat_response(
                lang,
                session.last_question_asked or next_discovery_question(lang, idx),
            )
            cached = session.phrase_cache.get(f"repeat:{idx}")
            self._set_last_question(session, reprompt, index=idx)
            await self._stream_say(
                session, reprompt, lang, stage="reprompt", path="reprompt_empty", cached_turn=cached
            )
            return

        session.add_turn("user", transcript)
        session.turn_count += 1
        validation = self._interpret_customer_answer(
            session, transcript, confidence
        )
        _sync_rcm_fields_to_record(session, record)
        cust: dict[str, Any] = {
            "role": "customer",
            "text": transcript,
            "raw_stt": raw,
            "raw_transcript": raw,
            "interpreted_topic": validation.topic,
            "validation_result": "accepted" if validation.accepted else "ambiguous",
            "clarification_reason": validation.reason,
            "interpretation": validation.to_dict(),
            "language": lang,
            "source": "media_streams",
        }
        if confidence is not None:
            cust["stt_confidence"] = confidence
        partial = self._customer_metrics_partial(session)
        if partial:
            cust["at"] = time.time()
            cust["metrics"] = partial
        record.setdefault("transcript", []).append(cust)
        # Persist immediately so frontend live poll shows user speech before LLM finishes
        self._persist_call_async(session.call_id)

        if session.browser_mode:
            await self._stream_gemini_reply(
                session,
                record,
                transcript,
                lang,
                session.last_question_index,
                after_backchannel=False,
            )
            return

        if self._stt_looks_wrong(session, transcript):
            idx = session.last_question_index
            reprompt = repeat_response(
                lang,
                session.last_question_asked or next_discovery_question(lang, idx),
            )
            cached = session.phrase_cache.get(f"repeat:{idx}")
            session.recent_stt.clear()
            await self._stream_say(
                session, reprompt, lang, stage="repeat", path="stt_retry", cached_turn=cached
            )
            return

        if user_wants_repeat(transcript):
            idx = session.last_question_index
            response_text = repeat_response(
                lang,
                session.last_question_asked or next_discovery_question(lang, idx),
            )
            cached = session.phrase_cache.get(f"repeat:{idx}")
            self._set_last_question(session, response_text, index=idx)
            await self._stream_say(
                session, response_text, lang, stage="repeat", path="repeat", cached_turn=cached
            )
            return

        idx = session.last_question_index
        followup_key = re.sub(
            r"\s+", " ", (session.answer_question or session.last_question_asked or f"question:{idx}").strip().lower()
        )
        action = dialogue_action(validation, followup_key in session.followup_used)
        if action == "clarify":
            new_question = validation.reason == "help_agreed"
            if not new_question:
                session.followup_used.add(followup_key)
            response_text = validation.clarification
            self._set_last_question(
                session,
                response_text,
                index=idx,
                preserve_answer_context=(
                    validation.reason != "low_confidence_confirm" and not new_question
                ),
            )
            await self._stream_say(
                session,
                response_text,
                lang,
                stage="clarification",
                path="clarification",
            )
            return

        if action == "move_on":
            response_text = move_on_after_ambiguity(validation.topic)
            self._set_last_question(session, response_text, index=idx)
            await self._stream_say(
                session,
                response_text,
                lang,
                stage="clarification",
                path="clarification_bounded",
            )
            return

        await self._advance_discovery_stream(session, record, transcript, lang, questions)

    async def _advance_discovery_stream(
        self,
        session: CallSession,
        record: dict,
        transcript: str,
        lang: str,
        questions: list[str],
    ) -> None:
        idx = session.discovery_index

        if idx >= len(questions) and not session_uses_scenario_mode(session):
            closing = (
                "Thank you. I will log the resolution code and update our RCM system. "
                "This interaction is documented on the claim file. Goodbye."
            )
            await self._stream_say(
                session, closing, lang, stage="call_summary", path="closing", end_call=True
            )
            return

        if session_uses_scenario_mode(session):
            confirmed = self._confirmed_status_line(session)
            if confirmed:
                await self._stream_say(
                    session,
                    confirmed,
                    lang,
                    stage="information_gathering",
                    path="workflow",
                )
                return
            await self._stream_gemini_reply(
                session,
                record,
                transcript,
                lang,
                idx,
                after_backchannel=bool(get_settings().backchannel_enabled),
            )
            return

        _v, response_text, verbal_meta, cache_key = select_reply_variant(
            session.call_id,
            idx,
            transcript,
            session.used_acks,
            session.phrase_meta,
            session.phrase_verbal_meta,
            lang,
        )
        self._note_ack_used(session, verbal_meta)
        cached = session.phrase_cache.get(cache_key)
        session.discovery_index = idx + 1
        self._set_last_question(session, response_text, index=idx)
        await self._stream_say(
            session,
            response_text,
            lang,
            stage="discovery",
            path="fast",
            cached_turn=cached,
            extra={"verbal": verbal_meta},
        )

    def _persist_call(self, call_id: str, final: bool = True) -> None:
        record = call_records.get(call_id)
        if not record:
            return
        if final:
            record["ended_at"] = time.time()
        transcript = record.get("transcript")
        if isinstance(transcript, list):
            record["latency_summary"] = compute_latency_summary(transcript)
        try:
            apply_call_summary(record)
        except Exception:
            logger.exception("Call summary failed call_id=%s", call_id)
        try:
            save_call_json(call_id, record)
        except Exception as e:
            logger.error("Failed to save call JSON: %s", e)

        session = call_sessions.get(call_id)
        try:
            from services.conversation_state_db import (
                persist_call_result_from_record,
                persist_session_state,
            )

            if session is not None:
                persist_session_state(session)
            persist_call_result_from_record(call_id, record, session)
        except Exception:
            logger.exception("Failed persisting call results to Postgres call_id=%s", call_id)

        if final:
            try:
                from worker.celery_app import enqueue_call_finished

                enqueue_call_finished(call_id)
            except Exception:
                logger.exception("Failed enqueueing post-call jobs call_id=%s", call_id)

    def begin_customer_turn(
        self,
        session: CallSession,
        stt_confidence: float | None,
        transcript_len: int,
    ) -> TurnTimer:
        session.turn_metrics_serial += 1
        gap: float | None = None
        if session.last_agent_twiml_at:
            gap = round((time.time() - session.last_agent_twiml_at) * 1000, 1)
        audio_ms = float(session.last_agent_audio_ms or 0)
        input_wait_ms = (
            round(max(0.0, gap - audio_ms), 1) if gap is not None else None
        )
        pipeline = getattr(session, "audio_pipeline", None)
        stt_ms = float(
            getattr(session, "last_stt_ms", 0)
            or (getattr(pipeline, "last_stt_ms", 0) if pipeline else 0)
            or 0
        )
        endpointing_ms = float(
            getattr(pipeline, "last_endpointing_ms", 0) if pipeline else 0
        )
        speech_end_to_stt_final_ms = float(
            getattr(pipeline, "last_speech_end_to_stt_final_ms", 0)
            if pipeline
            else 0
        )
        metrics = TurnMetrics(
            turn_id=session.turn_metrics_serial,
            call_id=session.call_id,
            webhook_received_at=time.time(),
            gap_since_last_agent_ms=gap,
            previous_agent_audio_ms=round(audio_ms, 1) if audio_ms > 0 else None,
            input_wait_ms=input_wait_ms,
            twilio_stt_confidence=stt_confidence,
            transcript_chars=transcript_len,
            stt_ms=stt_ms,
            endpointing_ms=endpointing_ms or None,
            speech_end_to_stt_final_ms=speech_end_to_stt_final_ms or None,
        )
        self._split_input_wait(session, metrics)
        timer = TurnTimer(metrics=metrics)
        session.active_turn_timer = timer
        return timer

    def _split_input_wait(self, session: CallSession, metrics: TurnMetrics) -> None:
        """Divide input wait into fetch, caller start, speaking, and speech-end-to-webhook."""
        now = metrics.webhook_received_at
        sent = session.last_agent_twiml_at
        fetched = session.last_audio_fetched_at
        if sent and fetched >= sent:
            metrics.audio_fetch_delay_ms = (fetched - sent) * 1000
        first = session.speech_first_partial_at
        last = session.speech_last_partial_at
        if first and last and sent and first >= sent:
            playback_end = (fetched or sent) + float(session.last_agent_audio_ms or 0) / 1000
            metrics.caller_start_ms = max(0.0, (first - playback_end) * 1000)
            metrics.caller_speaking_ms = (last - first) * 1000
            metrics.speech_end_to_webhook_ms = max(0.0, (now - last) * 1000)
        session.speech_first_partial_at = 0.0
        session.speech_last_partial_at = 0.0

    def _customer_metrics_partial(self, session: CallSession) -> dict[str, Any]:
        timer = session.active_turn_timer
        if not timer:
            return {}
        m = timer.metrics
        return {
            "turn_id": m.turn_id,
            "twilio_stt_confidence": m.twilio_stt_confidence,
            "gap_since_last_agent_ms": m.gap_since_last_agent_ms,
            "previous_agent_audio_ms": m.previous_agent_audio_ms,
            "input_wait_ms": m.input_wait_ms,
        }

    def _finish_turn_metrics(self, session: CallSession, path: str) -> dict[str, Any] | None:
        timer = session.active_turn_timer
        if not timer:
            return None
        timer.metrics.path = path
        finished = timer.finish()
        session.active_turn_timer = None
        if timer.metrics.audio_turn is not None:
            session.audio_turn_metrics[timer.metrics.audio_turn] = (
                finished,
                timer.metrics.webhook_received_at,
            )
        return finished

    def _apply_gemini_timing(self, session: CallSession, ai: dict[str, Any]) -> None:
        timing = ai.pop("_timing", None)
        if not timing or not session.active_turn_timer:
            return
        t = session.active_turn_timer
        t.metrics.prompt_build_ms += float(timing.get("prompt_build_ms") or 0)
        t.metrics.llm_ms += float(timing.get("llm_ms") or 0)
        t.set_first_llm_token_ms(timing.get("first_delta_ms"))
        model = timing.get("model")
        if model:
            t.metrics.llm_model = str(model)
            primary = getattr(getattr(self, "gemini", None), "model_name", "") or ""
            t.metrics.slm_used = bool(primary) and str(model) != primary
        if timing.get("model_tier"):
            t.metrics.model_tier = str(timing["model_tier"])
        if timing.get("model_route_reason"):
            t.metrics.model_route_reason = str(timing["model_route_reason"])
        t.metrics.model_fallback_used = bool(timing.get("model_fallback_used"))
        if timing.get("request_ms") is not None:
            t.metrics.llm_request_ms = float(timing["request_ms"])
        t.metrics.llm_prompt_tokens = timing.get("prompt_tokens")
        t.metrics.llm_output_tokens = timing.get("output_tokens")
        t.metrics.llm_thought_tokens = timing.get("thought_tokens")
        t.metrics.llm_attempts = int(timing.get("attempts") or 1)
        t.metrics.llm_error = timing.get("error") or None

    def _note_ack_used(self, session: CallSession, verbal: dict[str, Any] | None) -> None:
        if not verbal:
            return
        ack = (verbal.get("ack") or "").strip()
        if ack:
            session.used_acks.append(ack)

    async def start_greeting(self, session: CallSession) -> None:
        record = call_records.get(session.call_id, {})
        lang_code = session.preferred_language
        language_from_digit("", lang_code)

        try:
            if is_echo_mode(get_settings().dialogue_mode):
                text = echo_greeting(session.agent_name or "Chris")
                session.add_turn("assistant", text)
                record.setdefault("transcript", []).append(
                    {
                        "role": "agent",
                        "text": text,
                        "language": lang_code,
                        "stage": "echo_greeting",
                        "source": "echo_bot",
                    }
                )
                record["status"] = "in_progress"
                record["dialogue_mode"] = "echo"
                await self.speak(session, text, lang_code, streaming=False)
                logger.info("Echo Bot greeting sent for call %s", session.call_id)
                return

            # AI mode: one natural greeting from prompt.py (no robotic pre-message)
            greeting = await self.gemini.generate_greeting(
                session.agent_name,
                session.preferred_language,
                session.customer_name,
                **_claim_session_kwargs(session),
            )
            text = (greeting.get("response_text") or "").strip()
            from services.gemini_service import sanitize_spoken_response

            text = sanitize_spoken_response(
                text,
                fallback=fallback_greeting(
                    session.agent_name or "Chris",
                    session.customer_name or "",
                    lang_code,
                    hospital_name=session.hospital_name or "",
                    claim_type=session.claim_type
                    or (
                        (getattr(session, "tpa_sector_id", "") or "").replace("_", " ")
                        if getattr(session, "tpa_sector_id", "")
                        else "eligibility and claims"
                    ),
                ),
            )
            lang = greeting.get("detected_language", session.preferred_language) or lang_code

            if not text:
                logger.warning("Gemini returned empty greeting for call %s", session.call_id)
                text = fallback_greeting(
                    session.agent_name or "Chris",
                    session.customer_name or "",
                    lang_code,
                    hospital_name=session.hospital_name or "",
                    claim_id=session.claim_id or "",
                    patient_name=session.patient_name or "",
                    claim_type=session.claim_type or "",
                    npi=session.npi or "",
                    policy_number=session.policy_number or "",
                )

            session.add_turn("assistant", text)
            record.setdefault("transcript", []).append(
                {"role": "agent", "text": text, "language": lang, "stage": "greeting"}
            )
            record["status"] = "in_progress"
            record["dialogue_mode"] = "ai"

            await self.speak(session, text, lang, streaming=True)
            logger.info("Greeting sent for call %s", session.call_id)
        except Exception as e:
            logger.exception("start_greeting failed for call %s: %s", session.call_id, e)
            fallback = (
                echo_greeting(session.agent_name or "Chris")
                if is_echo_mode(get_settings().dialogue_mode)
                else fallback_greeting(
                    session.agent_name or "Chris",
                    session.customer_name or "",
                    lang_code,
                    hospital_name=session.hospital_name or "",
                    claim_id=session.claim_id or "",
                    patient_name=session.patient_name or "",
                )
            )
            try:
                await self.speak(session, fallback, lang_code, streaming=True)
            except Exception as tts_err:
                logger.exception("Fallback TTS also failed: %s", tts_err)

    def handle_media_chunk(self, session: CallSession, payload: str) -> None:
        """
        Immediate forward of one Twilio ~20ms media packet.

        Always forwarded (including while bot speaks) so Silero VAD can detect barge-in.
        """
        from services.audio_stream_pipeline import attach_pipeline

        pipeline = getattr(session, "audio_pipeline", None)
        if pipeline is None:
            pipeline = attach_pipeline(
                session,
                on_final=lambda text, s=session: self.process_stream_utterance(s, text),
                on_partial=lambda text, is_final, s=session: self.handle_partial_transcript(
                    s, text, is_final
                ),
                on_barge_in=lambda s=session: self.handle_barge_in(s),
                is_bot_speaking=lambda s=session: bool(s.is_agent_speaking),
                stt=self.stt,
            )

        pipeline.forward_packet_b64(payload)

    def twilio_speech_language(self, lang_code: str) -> str:
        """Map app locale to Twilio speech recognition language."""
        return lang_code if lang_code else "en-US"

    def _ensure_cache_worker(self) -> asyncio.Queue:
        if self._cache_queue is None:
            self._cache_queue = asyncio.Queue(maxsize=_CACHE_QUEUE_MAX)
        if self._cache_worker is None or self._cache_worker.done():
            self._cache_worker = asyncio.create_task(self._run_cache_queue())
        return self._cache_queue

    async def _enqueue_cache_phrase(self, job: tuple) -> None:
        queue = self._ensure_cache_worker()
        await queue.put(job)

    async def _run_cache_queue(self) -> None:
        from services.elevenlabs_rate_limit import request_slot

        queue = self._cache_queue
        if queue is None:
            return
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                while request_slot.live_waiting:
                    await asyncio.sleep(0.01)
                session, key, text, lang, verbal = item
                await self._cache_phrase(session, key, text, lang, verbal)
            except Exception as exc:
                logger.warning(
                    "Cache generation failed category=%s",
                    type(exc).__name__,
                )
            finally:
                queue.task_done()

    async def _store_agent_audio(
        self,
        session: CallSession,
        text: str,
        lang: str,
        fast: bool = False,
        allow_fallback: bool = True,
        live: bool = True,
    ) -> int | None:
        async with session._audio_lock:
            session.audio_turn_id += 1
            turn = session.audio_turn_id

        timer = session.active_turn_timer
        if timer:
            timer.start_tts()
            timer.mark_stage("tts_start")
        try:
            mulaw = await self.tts.text_to_speech(
                text,
                language_code=lang,
                for_twilio=True,
                fast=fast,
                delivery_emotion=session.delivery_emotion,
                allow_fallback=allow_fallback,
                live=live,
            )
            provider = self.tts.provider_name()
            expected = self.tts.primary_provider()
            if not allow_fallback and provider != expected:
                raise RuntimeError(f"cache provider {provider}")
            wav = self.tts.mulaw_to_pcm_wav(mulaw)
            voice = self._voice_identity(provider)
            async with session._audio_lock:
                session.playback_audio[turn] = wav
                session.playback_text[turn] = text
                session.playback_lang[turn] = lang
                session.playback_provider[turn] = provider
                session.playback_voice[turn] = voice
            if timer:
                timer.metrics.tts_provider = provider
                timer.metrics.tts_voice = voice
                timer.metrics.tts_fallback_used = provider != expected
            return turn
        except Exception as e:
            logger.error("TTS failed for call %s turn %s lang=%s: %s", session.call_id, turn, lang, e)
            async with session._audio_lock:
                session.playback_audio.pop(turn, None)
                session.playback_text.pop(turn, None)
                session.playback_lang.pop(turn, None)
                session.playback_provider.pop(turn, None)
                session.playback_voice.pop(turn, None)
                session.audio_turn_id -= 1
            return None
        finally:
            if timer:
                timer.end_tts()
                timer.mark_stage("tts_end", latest=True)

    def _voice_identity(self, provider: str | None = None) -> str:
        identify = getattr(self.tts, "voice_identity", None)
        if callable(identify):
            return identify(provider)
        return f"{provider or self.tts.primary_provider()}:default"

    def _mark_agent_twiml_sent(self, session: CallSession) -> None:
        session.last_agent_twiml_at = time.time()
        session.last_audio_fetched_at = 0.0
        session.speech_first_partial_at = 0.0
        session.speech_last_partial_at = 0.0

    def record_speech_partial(self, session: CallSession) -> None:
        """Twilio partialResultCallback: caller speech is being recognized now."""
        now = time.time()
        if not session.speech_first_partial_at:
            session.speech_first_partial_at = now
        session.speech_last_partial_at = now

    def mark_audio_fetched(self, session: CallSession, turn: int) -> None:
        """Record when Twilio first requested this turn's audio for playback."""
        if not session.last_audio_fetched_at:
            session.last_audio_fetched_at = time.time()
        entry = session.audio_turn_metrics.pop(turn, None)
        if entry is None:
            return
        metrics, received_at = entry
        if received_at:
            metrics["webhook_to_audio_fetch_ms"] = round(
                max(0.0, (time.time() - float(received_at)) * 1000), 1
            )
            from services.turn_telemetry import profile_turn_stages

            stages = profile_turn_stages(metrics)
            metrics["latency_stages_ms"] = stages
            metrics["twilio_fetch_wait_ms"] = stages["twilio_fetch_wait_ms"]

    async def ensure_playback_audio(self, session: CallSession, turn: int) -> bytes | None:
        """Return cached WAV or regenerate on demand (Twilio Play retry safety net)."""
        audio = session.playback_audio.get(turn)
        if audio:
            return audio

        text = session.playback_text.get(turn)
        lang = session.playback_lang.get(turn, session.preferred_language)
        if not text:
            for key, cached_turn in session.phrase_cache.items():
                if cached_turn == turn:
                    meta = session.phrase_meta.get(key)
                    if meta:
                        text, lang = meta
                    break
        if not text:
            return None

        logger.warning("Regenerating missing audio for call %s turn %s", session.call_id, turn)
        try:
            mulaw = await self.tts.text_to_speech(
                text,
                language_code=lang,
                for_twilio=True,
                fast=True,
                delivery_emotion=session.delivery_emotion,
                live=True,
            )
            wav = self.tts.mulaw_to_pcm_wav(mulaw)
            provider = self.tts.provider_name()
            session.playback_audio[turn] = wav
            session.playback_provider[turn] = provider
            session.playback_voice[turn] = self._voice_identity(provider)
            return wav
        except Exception as e:
            logger.error("On-demand TTS regen failed call %s turn %s: %s", session.call_id, turn, e)
            return None

    def _resolve_cached_turn(self, session: CallSession, cached_turn: int | None) -> int | None:
        """Use cached TTS only when audio exists in the currently configured voice."""
        if cached_turn is None:
            return None
        if cached_turn in session.playback_audio:
            recorded = session.playback_voice.get(cached_turn)
            expected = self._voice_identity() if recorded is not None else None
            if recorded is not None and recorded != expected:
                logger.warning(
                    "Cached turn %s voice %s does not match %s for call %s — regenerating",
                    cached_turn,
                    recorded,
                    expected,
                    session.call_id,
                )
                return None
            return cached_turn
        logger.warning(
            "Cached turn %s missing audio for call %s — regenerating",
            cached_turn,
            session.call_id,
        )
        return None

    def _customer_listen_xml(self, action: str, lang: str) -> str:
        """Fast Gather for US English (Twilio built-in speech recognition)."""
        if lang in TWILIO_GATHER_LANGS:
            hints = xml_escape.escape(_GATHER_HINTS)
            settings = get_settings()
            speech_timeout = xml_escape.escape(settings.twilio_gather_speech_timeout or "2")
            partial = ""
            if getattr(settings, "twilio_gather_partial_timing", False) and (
                "/api/webhooks/conversation?" in action
            ):
                partial_url = action.replace(
                    "/api/webhooks/conversation?", "/api/webhooks/gather-partial?"
                )
                partial = (
                    f'partialResultCallback="{partial_url}" '
                    'partialResultCallbackMethod="POST" '
                )
            return (
                f'    <Gather input="speech" speechTimeout="{speech_timeout}" '
                f'speechModel="phone_call" language="{lang}" hints="{hints}" '
                f'action="{action}" method="POST" timeout="{GATHER_TIMEOUT}" '
                f'{partial}actionOnEmptyResult="true"/>'
            )
        return (
            f'    <Record action="{action}" method="POST" maxLength="{RECORD_MAX_LENGTH}" '
            f'timeout="{RECORD_SILENCE_TIMEOUT}" playBeep="false" transcribe="false" '
            f'trim="trim-silence"/>'
        )

    def _wrap_play_and_record(
        self, base_url: str, call_id: str, play_url: str | None, say_text: str | None, lang: str
    ) -> str:
        action = self._xml_url(f"{base_url}/api/webhooks/conversation?call_id={call_id}")
        parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<Response>"]
        if play_url:
            parts.append(f"    <Play>{self._xml_url(play_url)}</Play>")
        elif say_text:
            say = _say_twiml(say_text, lang)
            if say:
                parts.append(say)
            else:
                logger.error(
                    "Agent audio unavailable call=%s and TWILIO_SAY_VOICE is not set; "
                    "listening without speaking to avoid a different voice",
                    call_id,
                )
        parts.append(self._customer_listen_xml(action, lang))
        parts.append(f'    <Redirect method="POST">{action}</Redirect>')
        parts.append("</Response>")
        return "\n".join(parts)

    async def _gemini_greeting(
        self, session: CallSession, lang_code: str
    ) -> dict[str, Any]:
        try:
            result = await self.gemini.generate_greeting(
                session.agent_name,
                lang_code,
                session.customer_name,
                **_gemini_session_kwargs(session),
            )
            self._apply_gemini_timing(session, result)
            return result
        except Exception as e:
            logger.warning("Gemini greeting failed for call %s, using fallback: %s", session.call_id, e)
            return {
                "response_text": fallback_greeting(
                    session.agent_name,
                    session.customer_name,
                    lang_code,
                    hospital_name=session.hospital_name,
                    claim_id=session.claim_id,
                    patient_name=session.patient_name,
                    claim_type=session.claim_type,
                    npi=session.npi,
                    policy_number=session.policy_number,
                ),
                "detected_language": lang_code,
                "detected_emotion": "professional",
                "conversation_stage": "greeting",
                "confidence_score": 0.65,
                "lead_score": 65,
                "next_action": "continue",
            }

    async def _stream_gemini_reply(
        self,
        session: CallSession,
        record: dict,
        transcript: str,
        lang: str,
        idx: int,
        *,
        after_backchannel: bool,
    ) -> None:
        """
        Streaming LLM path (Media Streams):

          transcript → Gemini token stream → sentence 1 → TTS
                                           → sentence 2 → TTS (overlap)
        Backchannel (if enabled) plays while Gemini already streams.
        """
        self._apply_tone_signal(session, transcript, None)
        recent = list(session.used_acks[-6:]) + list(session.backchannels_used[-4:])
        delta_q: asyncio.Queue[str | None] = asyncio.Queue()
        result_box: dict[str, Any] = {}

        async def _llm_producer() -> None:
            fallback = scenario_fallback_reply(
                session.tpa_executive_profile,
                transcript,
                _rcm_collected_from_session(session),
                lang,
                last_question=session.last_question_asked,
            )
            try:
                turn_kwargs = _gemini_session_kwargs(session)
                if session.browser_mode:
                    turn_kwargs["reply_briefly"] = True
                async for ev in self.gemini.generate_response_stream(
                    transcript,
                    session.conversation_history,
                    session.agent_name,
                    session.preferred_language,
                    last_question=session.last_question_asked,
                    recent_acks=recent[-10:],
                    after_backchannel=after_backchannel,
                    **turn_kwargs,
                ):
                    if session.tts_stop.is_set():
                        break
                    if ev.get("event") == "delta":
                        text = ev.get("text") or ""
                        if text:
                            if session.browser_mode and "first_token" not in session.browser_marks:
                                now = time.perf_counter()
                                session.browser_marks["first_token"] = now
                                base = session.browser_marks.get("speech_end") or now
                                logger.info(
                                    "VOICE first_ai_token call=%s ms=%.0f",
                                    session.call_id,
                                    (now - base) * 1000,
                                )
                            await delta_q.put(text)
                    elif ev.get("event") == "done":
                        ai = ev.get("result") or {}
                        result_box["ai"] = ai
                        self._apply_gemini_timing(session, ai)
                        self._apply_tone_signal(
                            session, transcript, ai.get("detected_emotion")
                        )
            except asyncio.CancelledError:
                logger.info("Gemini stream cancelled (barge-in) call=%s", session.call_id)
                raise
            except Exception as e:
                logger.warning(
                    "Gemini stream failed call=%s — fallback: %s", session.call_id, e
                )
                result_box["ai"] = {
                    "response_text": fallback,
                    "detected_language": lang,
                    "detected_emotion": "neutral",
                    "conversation_stage": "information_gathering",
                    "confidence_score": session.confidence_score or 0.7,
                    "lead_score": session.lead_score or 70,
                    "next_action": "continue",
                    "advance_workflow": True,
                }
                if not session.tts_stop.is_set():
                    await delta_q.put(fallback)
            finally:
                await delta_q.put(None)

        # Start Gemini immediately (overlaps backchannel + TTS)
        if session.browser_mode:
            logger.info("VOICE ai_request_started call=%s", session.call_id)
        llm_task = asyncio.create_task(
            _llm_producer(), name=f"gemini-stream-{session.call_id}"
        )
        session.generation_task = llm_task

        if get_settings().backchannel_enabled and not session.browser_mode:
            bc_text, bc_key = select_backchannel(session.call_id, session.backchannels_used)
            session.backchannels_used.append(bc_text)
            bc_turn = session.phrase_cache.get(bc_key)
            await self.speak(session, bc_text, lang, cached_turn=bc_turn)
            session.add_turn("assistant", bc_text)
            record.setdefault("transcript", []).append(
                {
                    "role": "agent",
                    "text": bc_text,
                    "language": lang,
                    "stage": "backchannel",
                    "source": "media_streams",
                }
            )

        if session.tts_stop.is_set():
            logger.info("Skipping Gemini TTS after barge-in call=%s", session.call_id)
            if not llm_task.done():
                llm_task.cancel()
                try:
                    await llm_task
                except asyncio.CancelledError:
                    pass
            session.generation_task = None
            return

        async def _deltas():
            while True:
                item = await delta_q.get()
                if item is None:
                    return
                yield item

        emotion = getattr(session, "delivery_emotion", "") or ""
        timer = session.active_turn_timer
        spoken_units: list[str] = []
        stop_after_first = {"on": False}

        live_playback = session.browser_live_queue is not None

        async def _synthesize(unit: str) -> bytes:
            if session.tts_stop.is_set() or stop_after_first["on"]:
                return b""
            spoken_units.append(unit)
            t0 = time.perf_counter()
            target_q = session.browser_live_queue
            if live_playback and self.tts.supports_chunk_stream():
                base = session.browser_marks.get("speech_end") or t0
                if "tts_request" not in session.browser_marks:
                    session.browser_marks["tts_request"] = t0
                    logger.info(
                        "VOICE first_tts_request call=%s ms=%.0f",
                        session.call_id,
                        (t0 - base) * 1000,
                    )
                parts: list[bytes] = []
                pending = b""
                emitted = False

                def _emit(blob: bytes) -> None:
                    nonlocal emitted
                    if (
                        not blob
                        or target_q is None
                        or session.browser_live_queue is not target_q
                    ):
                        return
                    if "first_chunk" not in session.browser_marks:
                        now = time.perf_counter()
                        session.browser_marks["first_chunk"] = now
                        logger.info(
                            "VOICE first_audio_chunk call=%s ms=%.0f",
                            session.call_id,
                            (now - base) * 1000,
                        )
                    target_q.put_nowait(blob)
                    emitted = True

                try:
                    async for frame in self.tts.iter_phone_chunks(
                        unit, delivery_emotion=emotion
                    ):
                        if (
                            session.tts_stop.is_set()
                            or stop_after_first["on"]
                            or session.browser_live_queue is not target_q
                        ):
                            break
                        if not frame:
                            continue
                        parts.append(frame)
                        pending += frame
                        if len(pending) >= 320:
                            _emit(pending)
                            pending = b""
                    if pending:
                        _emit(pending)
                except Exception:
                    logger.warning(
                        "Browser voice stream failed call=%s. Speaking the full line instead.",
                        session.call_id,
                    )
                    if emitted:
                        pending = b""
                    else:
                        parts = []
                audio = b"".join(parts)
                if audio:
                    session.browser_audio_parts.append(audio)
                    synth_ms = (time.perf_counter() - t0) * 1000
                    if timer is not None:
                        timer.add_tts_ms(synth_ms)
                        timer.set_first_tts_chunk_ms(synth_ms)
                    return audio
            try:
                audio = await self.tts.text_to_speech(
                    unit,
                    language_code=lang,
                    for_twilio=True,
                    delivery_emotion=emotion,
                )
            except Exception:
                if not live_playback:
                    raise
                logger.warning(
                    "Browser reply voice failed call=%s. Continuing the conversation.",
                    session.call_id,
                )
                return b""
            synth_ms = (time.perf_counter() - t0) * 1000
            if timer is not None:
                timer.add_tts_ms(synth_ms)
                timer.set_first_tts_chunk_ms(synth_ms)
            if live_playback and audio and target_q is not None and session.browser_live_queue is target_q:
                base = session.browser_marks.get("speech_end") or t0
                if "first_chunk" not in session.browser_marks:
                    now = time.perf_counter()
                    session.browser_marks["first_chunk"] = now
                    logger.info(
                        "VOICE first_audio_chunk call=%s ms=%.0f",
                        session.call_id,
                        (now - base) * 1000,
                    )
                session.browser_audio_parts.append(audio)
                target_q.put_nowait(audio)
            return audio

        async def _play(mulaw: bytes, finalize: bool) -> None:
            if session.tts_stop.is_set():
                await self.clear_twilio_audio(session)
                session.is_agent_speaking = False
                return
            if live_playback:
                return
            if not mulaw or len(mulaw) < 400:
                logger.error(
                    "Skipping empty LLM TTS chunk call=%s",
                    session.call_id,
                )
                return
            await self.send_audio_to_twilio(session, mulaw, finalize=finalize)
            if session.browser_mode:
                stop_after_first["on"] = True

        # Fresh speak window for LLM audio (backchannel already finished)
        session.tts_stop.clear()
        tts_stats = await speak_from_llm_deltas(
            deltas=_deltas(),
            synthesize=_synthesize,
            play=_play,
            call_id=session.call_id,
            should_stop=lambda: session.tts_stop.is_set() or stop_after_first["on"],
            flush_clause=live_playback,
            clause_words=4 if live_playback else 8,
        )
        if session.browser_mode and (tts_stats.get("units") or 0) > 0:
            spoken_now = " ".join(spoken_units).strip()
            if spoken_now:
                self._set_last_question(session, spoken_now, index=idx)
                session.add_turn("assistant", spoken_now)
                record.setdefault("transcript", []).append(
                    {
                        "role": "agent",
                        "text": spoken_now,
                        "language": lang,
                        "stage": "discovery",
                        "source": "browser",
                        "path": "gemini_stream",
                    }
                )
                self._persist_call_async(session.call_id)
            logger.info(
                "Browser first sentence ready call=%s chars=%d",
                session.call_id,
                len(spoken_now),
            )

            async def _finish_browser() -> None:
                try:
                    await llm_task
                except asyncio.CancelledError:
                    return
                except Exception:
                    logger.warning(
                        "Browser reply finish failed call=%s", session.call_id
                    )
                    return
                finally:
                    if session.generation_task is llm_task:
                        session.generation_task = None
                ai = result_box.get("ai") or {}
                if not ai:
                    return
                try:
                    _sync_rcm_fields_to_record(session, record)
                    _apply_ai_extraction(
                        session,
                        record,
                        ai,
                        transcript,
                        question_topic=session.answer_topic,
                        answer_valid=session.current_answer_valid,
                    )
                    self._persist_call_async(session.call_id)
                except Exception:
                    logger.warning(
                        "Browser reply extract failed call=%s", session.call_id
                    )

            asyncio.create_task(
                _finish_browser(), name=f"browser-finish-{session.call_id}"
            )
            return
        if timer is not None and tts_stats.get("first_audio_ms") is not None:
            timer.set_first_audio_ms(float(tts_stats["first_audio_ms"]))

        try:
            await llm_task
        except asyncio.CancelledError:
            logger.info("Gemini stream task cancelled call=%s", session.call_id)
        finally:
            if session.generation_task is llm_task:
                session.generation_task = None

        if session.tts_stop.is_set():
            return

        ai = result_box.get("ai") or {}
        _sync_rcm_fields_to_record(session, record)
        _apply_ai_extraction(
            session,
            record,
            ai,
            transcript,
            question_topic=session.answer_topic,
            answer_valid=session.current_answer_valid,
        )

        response_text = strip_robotic_payer_echo((ai.get("response_text") or "").strip())
        # Already spoken from the stream — don't re-polish into a different script
        verbal_meta: dict[str, Any] = {}

        # Nothing spoken (empty stream) — fallback full speak
        if (tts_stats.get("units") or 0) == 0:
            if not response_text:
                response_text = scenario_fallback_reply(
                    session.tpa_executive_profile,
                    transcript,
                    _rcm_collected_from_session(session),
                    lang,
                    last_question=session.last_question_asked,
                )
            end_call = bool(_should_end_scenario_call(session, ai))
            if end_call and "goodbye" not in response_text.lower():
                response_text = (
                    f"{response_text.rstrip('.')}. "
                    "Thank you — we will update our RCM system. Goodbye."
                ).strip()
            self._set_last_question(session, response_text, index=idx)
            await self._stream_say(
                session,
                response_text,
                lang,
                stage=str(ai.get("conversation_stage") or "discovery"),
                path="gemini_fallback",
                end_call=end_call,
                extra={"emotion": ai.get("detected_emotion"), "llm_streaming": False},
            )
            return

        end_call = bool(_should_end_scenario_call(session, ai))
        spoken = response_text or "(streamed)"
        self._set_last_question(session, spoken, index=idx)
        session.add_turn("assistant", spoken)
        entry: dict[str, Any] = {
            "role": "agent",
            "text": spoken,
            "language": lang,
            "stage": str(ai.get("conversation_stage") or "discovery"),
            "source": "media_streams",
            "path": "gemini_stream",
            "tts": {
                "streaming": True,
                "llm_streaming": True,
                "units": tts_stats.get("units"),
                "first_audio_ms": tts_stats.get("first_audio_ms"),
            },
            "emotion": ai.get("detected_emotion"),
        }
        if verbal_meta:
            entry["verbal"] = verbal_meta
        timing = (ai.get("_timing") or {}) if isinstance(ai, dict) else {}
        if timing:
            entry["llm_timing"] = {
                "first_delta_ms": timing.get("first_delta_ms"),
                "llm_ms": timing.get("llm_ms"),
                "streamed_chars": timing.get("streamed_chars"),
                "model": timing.get("model"),
                "model_tier": timing.get("model_tier"),
                "model_route_reason": timing.get("model_route_reason"),
            }
        record.setdefault("transcript", []).append(
            attach_metrics(entry, self._finish_turn_metrics(session, "gemini_stream"))
        )
        self._persist_call_async(session.call_id)

        if end_call:
            if "goodbye" not in spoken.lower() and not session.tts_stop.is_set():
                await self.speak(
                    session,
                    "Thank you — we will update our RCM system. Goodbye.",
                    lang,
                )
            session.status = "completed"
            record["status"] = "completed"
            if session.call_sid:
                try:
                    self.twilio.hangup(session.call_sid)
                except Exception as e:
                    logger.warning("Hangup after streamed close failed: %s", e)

    async def _gemini_turn(
        self, session: CallSession, transcript: str, *, after_backchannel: bool = False
    ) -> dict[str, Any]:
        self._apply_tone_signal(session, transcript, None)
        recent = list(session.used_acks[-6:]) + list(session.backchannels_used[-4:])

        async def _run() -> dict[str, Any]:
            timer = session.active_turn_timer
            if timer:
                timer.mark_stage("llm_start")
            try:
                result = await self.gemini.generate_response(
                    transcript,
                    session.conversation_history,
                    session.agent_name,
                    session.preferred_language,
                    last_question=session.last_question_asked,
                    recent_acks=recent[-10:],
                    after_backchannel=after_backchannel,
                    **_gemini_session_kwargs(session),
                )
                if timer:
                    timer.mark_stage("llm_end")
                self._apply_gemini_timing(session, result)
                self._apply_tone_signal(
                    session, transcript, result.get("detected_emotion")
                )
                if not (result.get("response_text") or "").strip():
                    logger.warning("Gemini empty response_text for call %s", session.call_id)
                return result
            except asyncio.CancelledError:
                logger.info("Gemini turn cancelled (barge-in) call=%s", session.call_id)
                raise
            except Exception as e:
                logger.warning(
                    "Gemini response failed for call %s, using fallback: %s",
                    session.call_id,
                    e,
                )
                if timer:
                    timer.mark_stage("llm_end")
                    offsets = timer.metrics.stage_offsets_ms
                    if "llm_start" in offsets and "llm_end" in offsets:
                        timer.metrics.llm_ms += max(
                            0.0, offsets["llm_end"] - offsets["llm_start"]
                        )
                    timer.metrics.model_route_reason = "llm_failure_scripted_fallback"
                    timer.metrics.llm_error = _error_label(e)
                    timer.metrics.model_fallback_used = True
                return {
                    "response_text": scenario_fallback_reply(
                        session.tpa_executive_profile,
                        transcript,
                        _rcm_collected_from_session(session),
                        session.preferred_language,
                        last_question=session.last_question_asked,
                    ),
                    "detected_language": session.preferred_language,
                    "detected_emotion": "neutral",
                    "conversation_stage": "information_gathering",
                    "confidence_score": session.confidence_score or 0.7,
                    "lead_score": session.lead_score or 70,
                    "next_action": "continue",
                    "advance_workflow": True,
                }

        task = asyncio.create_task(_run(), name=f"gemini-turn-{session.call_id}")
        session.generation_task = task
        try:
            return await task
        finally:
            if session.generation_task is task:
                session.generation_task = None

    async def _await_playback_audio(
        self, session: CallSession, text: str, lang: str
    ) -> tuple[int | None, str]:
        """Synthesize in the configured voice, retrying once on a fast failure."""
        started = time.perf_counter()
        for attempt in range(2):
            remaining = GATHER_TTS_TIMEOUT_S - (time.perf_counter() - started)
            if remaining <= 0:
                return None, "timeout"
            try:
                turn = await asyncio.wait_for(
                    self._store_agent_audio(session, text, lang, fast=True),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                return None, "timeout"
            if turn is not None:
                return turn, ""
            if attempt == 0:
                logger.warning(
                    "TTS failed for call %s; retrying the configured voice once",
                    session.call_id,
                )
        return None, "synthesis_failed"

    async def _build_agent_twiml(
        self,
        session: CallSession,
        base_url: str,
        text: str,
        lang: str,
        end_call: bool = False,
        cached_turn: int | None = None,
    ) -> str:
        """Play agent speech then record customer answer (or hang up)."""
        if end_call and "goodbye" not in text.lower():
            text = f"{text.rstrip()} Thank you for your time. Goodbye.".strip()
        if cached_turn is None:
            cached_turn = session.phrase_cache.get(_spoken_text_key(text))
        turn = self._resolve_cached_turn(session, cached_turn)
        if turn is not None and cached_turn is not None and session.active_turn_timer:
            session.active_turn_timer.mark_tts_cache_hit()
        say_reason = ""
        if turn is None:
            turn, say_reason = await self._await_playback_audio(session, text, lang)
        if turn is None:
            fallback = "twilio_say" if _twilio_say_voice() else "none"
            logger.info(
                "Playback selected call=%s provider=%s reason=%s",
                session.call_id,
                fallback,
                say_reason or "synthesis_failed",
            )
            if session.active_turn_timer:
                metrics = session.active_turn_timer.metrics
                metrics.tts_provider = fallback
                metrics.tts_fallback_used = True
                metrics.tts_failure_reason = say_reason or "synthesis_failed"
            if end_call:
                self._persist_call_async(session.call_id)
                say = _say_twiml(text, lang)
                return (
                    '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n'
                    + (f"{say}\n" if say else "")
                    + "    <Hangup/>\n</Response>"
                )
            return self._wrap_play_and_record(base_url, session.call_id, None, text, lang)
        play_url = (
            f"{base_url}/api/webhooks/agent-audio?call_id={session.call_id}&turn={turn}"
            if turn is not None
            else None
        )
        logger.info(
            "Playback selected call=%s turn=%s provider=%s",
            session.call_id,
            turn,
            session.playback_provider.get(turn, "unrecorded"),
        )

        if session.active_turn_timer:
            session.active_turn_timer.metrics.audio_turn = turn

        if end_call:
            self._persist_call_async(session.call_id)
            return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{self._xml_url(play_url)}</Play>
    <Hangup/>
</Response>"""

        if not end_call and not session_uses_scenario_mode(session):
            self._precache_next_reply_async(session)
        wav = session.playback_audio.get(turn)
        # PCM WAV is 8 kHz, 16-bit mono; subtract the standard 44-byte header.
        session.last_agent_audio_ms = (
            max(0.0, (len(wav) - 44) / 16.0) if wav else 0.0
        )
        self._mark_agent_twiml_sent(session)
        return self._wrap_play_and_record(base_url, session.call_id, play_url, text if not play_url else None, lang)

    async def build_intro_twiml(self, session: CallSession, base_url: str) -> str:
        record = call_records.get(session.call_id, {})
        lang_code = session.preferred_language
        _, lang_name = language_from_digit("", lang_code)

        if "greeting" in session.phrase_meta:
            text = session.phrase_meta["greeting"][0]
        elif session_uses_scenario_mode(session):
            greeting_result = await self._gemini_greeting(session, lang_code)
            _apply_ai_extraction(session, record, greeting_result)
            text = greeting_result.get("response_text") or fallback_greeting(
                session.agent_name,
                session.customer_name,
                lang_code,
                hospital_name=session.hospital_name,
                claim_id=session.claim_id,
                patient_name=session.patient_name,
                claim_type=session.claim_type,
                npi=session.npi,
                policy_number=session.policy_number,
            )
            text = self._ensure_question_in_response(text, lang_code, 0, dynamic=True)
        else:
            text = self._ensure_question_in_response(
                fallback_greeting(
                    session.agent_name,
                    session.customer_name,
                    lang_code,
                    hospital_name=session.hospital_name,
                    claim_id=session.claim_id,
                    patient_name=session.patient_name,
                    claim_type=session.claim_type,
                    npi=session.npi,
                    policy_number=session.policy_number,
                ),
                lang_code,
                0,
            )
        lang = lang_code

        session.add_turn("assistant", text)
        record.setdefault("transcript", []).append(
            {"role": "agent", "text": text, "language": lang, "stage": "greeting"}
        )
        record["status"] = "in_progress"
        session.greeting_done = True
        if not session_uses_scenario_mode(session):
            session.discovery_index = 1
        self._set_last_question(session, text, index=0)

        logger.info("HTTP conversation started for call %s (lang=%s)", session.call_id, lang_name)
        self._persist_call_async(session.call_id)

        greeting_turn = session.phrase_cache.get("greeting")
        if greeting_turn is None:
            greeting_turn = await self._store_agent_audio(session, text, lang, fast=True)
        if greeting_turn is None:
            greeting_turn = await self._store_agent_audio(session, text, lang, fast=False)

        if greeting_turn is not None:
            logger.info(
                "Playback selected call=%s turn=%s provider=%s",
                session.call_id,
                greeting_turn,
                session.playback_provider.get(greeting_turn, "unrecorded"),
            )
            greet_play = (
                f"{base_url}/api/webhooks/agent-audio?call_id={session.call_id}&turn={greeting_turn}"
            )
            action = self._xml_url(f"{base_url}/api/webhooks/conversation?call_id={session.call_id}")
            listen = self._customer_listen_xml(action, lang_code)
            wav = session.playback_audio.get(greeting_turn)
            session.last_agent_audio_ms = (
                max(0.0, (len(wav) - 44) / 16.0) if wav else 0.0
            )
            self._mark_agent_twiml_sent(session)
            return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{self._xml_url(greet_play)}</Play>
{listen}
    <Redirect method="POST">{action}</Redirect>
</Response>"""
        logger.warning(
            "Greeting TTS unavailable for call %s — Say fallback %s",
            session.call_id,
            "configured" if _twilio_say_voice() else "disabled",
        )
        return self._wrap_play_and_record(base_url, session.call_id, None, text, lang)

    def _build_backchannel_continue_twiml(
        self,
        session: CallSession,
        base_url: str,
        bc_turn: int | None,
        bc_text: str,
        lang: str,
    ) -> str:
        continue_url = self._xml_url(
            f"{base_url}/api/webhooks/conversation-continue?call_id={session.call_id}"
        )
        if bc_turn is not None:
            play = self._xml_url(
                f"{base_url}/api/webhooks/agent-audio?call_id={session.call_id}&turn={bc_turn}"
            )
            return (
                '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n'
                f"    <Play>{play}</Play>\n"
                f'    <Redirect method="POST">{continue_url}</Redirect>\n</Response>'
            )
        say = _say_twiml(bc_text, lang)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n'
            + (f"{say}\n" if say else "")
            + f'    <Redirect method="POST">{continue_url}</Redirect>\n</Response>'
        )

    def _build_pending_continue_twiml(
        self,
        session: CallSession,
        base_url: str,
    ) -> str:
        """Silent hold: no speech, just re-enter /conversation-continue."""
        continue_url = self._xml_url(
            f"{base_url}/api/webhooks/conversation-continue?call_id={session.call_id}"
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n'
            f'    <Pause length="{GATHER_HOLD_PAUSE_S}"/>\n'
            f'    <Redirect method="POST">{continue_url}</Redirect>\n</Response>'
        )

    async def build_bounded_speech_response_twiml(
        self,
        session: CallSession,
        speech_text: str,
        base_url: str,
    ) -> str:
        """Build a reply without holding Twilio's Gather webhook open too long."""
        if session.pending_twiml:
            logger.warning(
                "Replacing stale pending TwiML task for call %s",
                session.call_id,
            )
        task = asyncio.create_task(
            self.build_speech_response_twiml(session, speech_text, base_url),
            name=f"gather-reply-{session.call_id}-{session.turn_count + 1}",
        )
        return await self._bounded_twiml(session, task, base_url)

    async def build_bounded_intro_twiml(self, session: CallSession, base_url: str) -> str:
        """Greeting TwiML under the same webhook budget as conversation turns."""
        if session.pending_twiml:
            logger.warning("Replacing stale pending TwiML task for call %s", session.call_id)
        task = asyncio.create_task(
            self.build_intro_twiml(session, base_url),
            name=f"gather-intro-{session.call_id}",
        )
        return await self._bounded_twiml(session, task, base_url)

    async def _bounded_twiml(
        self,
        session: CallSession,
        task: asyncio.Task,
        base_url: str,
    ) -> str:
        started = time.perf_counter()
        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=GATHER_WEBHOOK_BUDGET_S,
            )
        except asyncio.TimeoutError:
            elapsed_ms = (time.perf_counter() - started) * 1000
            session.pending_twiml = {
                "task": task,
                "started_at": time.time(),
                "base_url": base_url,
                "holds": 0,
            }
            if session.active_turn_timer:
                session.active_turn_timer.metrics.gather_deferred = True
                session.active_turn_timer.metrics.gather_initial_response_ms = elapsed_ms
            logger.info(
                "Gather reply deferred call=%s after %.0fms (silent redirect)",
                session.call_id,
                elapsed_ms,
            )
            return self._build_pending_continue_twiml(session, base_url)

    async def complete_pending_twiml(
        self,
        session: CallSession,
        base_url: str,
    ) -> str:
        """Collect deferred Gather work, returning another silent hold if needed."""
        pending = session.pending_twiml
        if not pending:
            return await self.complete_pending_gemini_twiml(session, base_url)

        task = pending.get("task")
        if not isinstance(task, asyncio.Task):
            session.pending_twiml = None
            return await self.complete_pending_gemini_twiml(session, base_url)
        try:
            twiml = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=GATHER_CONTINUE_BUDGET_S,
            )
        except asyncio.TimeoutError:
            holds = int(pending.get("holds") or 0) + 1
            pending["holds"] = holds
            if holds >= GATHER_MAX_PENDING_HOLDS:
                logger.error(
                    "Deferred reply abandoned call=%s after %d silent holds",
                    session.call_id,
                    holds,
                )
                task.cancel()
                session.pending_twiml = None
                return self._wrap_play_and_record(
                    base_url, session.call_id, None, None, session.preferred_language
                )
            logger.info(
                "Gather continuation still pending call=%s hold=%d",
                session.call_id,
                holds,
            )
            return self._build_pending_continue_twiml(session, base_url)
        except asyncio.CancelledError:
            session.pending_twiml = None
            raise
        except Exception:
            logger.exception("Deferred reply failed call=%s", session.call_id)
            session.pending_twiml = None
            return self._wrap_play_and_record(
                base_url, session.call_id, None, None, session.preferred_language
            )
        finally:
            if task.done() and session.pending_twiml is pending:
                session.pending_twiml = None
        return twiml

    async def _begin_gemini_with_backchannel(
        self,
        session: CallSession,
        record: dict,
        transcript: str,
        base_url: str,
        lang: str,
        idx: int,
    ) -> str:
        if session.pending_gemini:
            logger.warning("Call %s clearing stale pending Gemini", session.call_id)
        task = asyncio.create_task(
            self._gemini_turn(session, transcript, after_backchannel=True)
        )
        session.pending_gemini = {
            "task": task,
            "transcript": transcript,
            "idx": idx,
            "started_at": time.time(),
        }
        bc_text, bc_key = select_backchannel(session.call_id, session.backchannels_used)
        session.backchannels_used.append(bc_text)
        bc_turn = session.phrase_cache.get(bc_key)
        if bc_turn is None:
            bc_turn = await self._store_agent_audio(session, bc_text, lang, fast=True)
        session.add_turn("assistant", bc_text)
        record.setdefault("transcript", []).append(
            {"role": "agent", "text": bc_text, "language": lang, "stage": "backchannel"}
        )
        self._persist_call_async(session.call_id)
        if session.active_turn_timer:
            session.active_turn_timer.metrics.path = "gemini_backchannel"
        logger.info("Backchannel %r → async Gemini for call %s", bc_text, session.call_id)
        return self._build_backchannel_continue_twiml(session, base_url, bc_turn, bc_text, lang)

    async def complete_pending_gemini_twiml(self, session: CallSession, base_url: str) -> str:
        record = call_records.get(session.call_id, {})
        lang = session.preferred_language
        questions = discovery_questions(lang)
        pending = session.pending_gemini
        if not pending:
            logger.warning("conversation-continue with no pending Gemini call %s", session.call_id)
            reprompt = session.last_question_asked or next_discovery_question(
                lang, session.discovery_index
            )
            return await self._build_agent_twiml(session, base_url, reprompt, lang)

        task = pending["task"]
        transcript = pending["transcript"]
        idx = pending["idx"]
        wait_ms = (time.time() - pending.get("started_at", time.time())) * 1000
        session.pending_gemini = None

        try:
            ai = await asyncio.wait_for(asyncio.shield(task), timeout=14.0)
        except Exception as e:
            logger.warning("Pending Gemini failed for call %s: %s", session.call_id, e)
            ai = {
                "response_text": scenario_fallback_reply(
                    session.tpa_executive_profile,
                    transcript,
                    _rcm_collected_from_session(session),
                    lang,
                    last_question=session.last_question_asked,
                ),
                "conversation_stage": "discovery",
                "advance_workflow": True,
            }

        return await self._finalize_gemini_turn_twiml(
            session,
            record,
            ai,
            transcript,
            idx,
            lang,
            questions,
            base_url,
            backchannel_wait_ms=wait_ms,
        )

    async def _finalize_gemini_turn_twiml(
        self,
        session: CallSession,
        record: dict,
        ai: dict[str, Any],
        transcript: str,
        idx: int,
        lang: str,
        questions: list[str],
        base_url: str,
        backchannel_wait_ms: float = 0,
    ) -> str:
        _sync_rcm_fields_to_record(session, record)
        _apply_ai_extraction(
            session,
            record,
            ai,
            transcript,
            question_topic=session.answer_topic,
            answer_valid=session.current_answer_valid,
        )
        response_text = (ai.get("response_text") or "").strip()
        verbal_meta: dict[str, Any] = {}
        cached = None
        after_bc = backchannel_wait_ms > 0
        if not response_text:
            logger.warning(
                "Gemini empty after backchannel for call %s idx %s — profile fallback",
                session.call_id,
                idx,
            )
            response_text = scenario_fallback_reply(
                session.tpa_executive_profile,
                transcript,
                _rcm_collected_from_session(session),
                lang,
                last_question=session.last_question_asked,
            )
            verbal_meta = {"source": "scenario_fallback", "ack_tier": "none"}
        else:
            response_text, verbal_meta = polish_gemini_response(
                session.call_id,
                response_text,
                transcript,
                session.used_acks + session.backchannels_used,
                after_backchannel=after_bc,
            )
        corrected = replace_unnecessary_status_probe(
            response_text, _rcm_collected_from_session(session)
        )
        if corrected and corrected != response_text:
            response_text = corrected
            verbal_meta = {**verbal_meta, "source": "workflow_next_question"}
        response_text = strip_robotic_payer_echo(response_text)
        self._note_ack_used(session, verbal_meta)
        stage = ai.get("conversation_stage") or "discovery"
        advance = ai.get("advance_workflow", True)
        if not session_uses_scenario_mode(session):
            if advance and idx < len(questions):
                session.discovery_index = idx + 1
        self._set_last_question(session, response_text, index=idx if not advance else idx)
        end_call = False
        if session_uses_scenario_mode(session) and _should_end_scenario_call(session, ai):
            end_call = True
            if "goodbye" not in response_text.lower():
                response_text = (
                    f"{response_text.rstrip('.')}. "
                    "Thank you — we will update our RCM system. Goodbye."
                ).strip()
        logger.info(
            "Gemini main line turn %s call %s (%.0fms after backchannel)",
            session.turn_count,
            session.call_id,
            backchannel_wait_ms,
        )
        twiml = await self._build_agent_twiml(
            session, base_url, response_text, lang, end_call=end_call, cached_turn=cached
        )
        path = "gemini"
        if session.active_turn_timer and session.active_turn_timer.metrics.path == "workflow":
            path = "workflow"
        metrics = self._finish_turn_metrics(session, path)
        if metrics is not None and backchannel_wait_ms:
            metrics["backchannel_to_main_ms"] = round(backchannel_wait_ms, 1)
        agent_entry = attach_metrics(
            {
                "role": "agent",
                "text": response_text,
                "language": lang,
                "stage": stage,
                "emotion": ai.get("detected_emotion"),
                "verbal": verbal_meta,
            },
            metrics,
        )
        session.add_turn("assistant", response_text)
        record.setdefault("transcript", []).append(agent_entry)
        self._persist_call_async(session.call_id)
        return twiml

    def _confirmed_status_line(self, session: CallSession) -> str | None:
        """Local next question after a validated status, so we do not re-ask it."""
        if not (session.current_answer_valid and session.answer_topic == "claim_status"):
            return None
        return workflow_reply_for_confirmed_status(_rcm_collected_from_session(session))

    async def _advance_discovery(
        self,
        session: CallSession,
        record: dict,
        transcript: str,
        base_url: str,
        lang: str,
        questions: list[str],
    ) -> str:
        """Acknowledge answer and ask the next discovery question."""
        idx = session.discovery_index

        # All workflow questions answered — close with RCM summary (do not hang up mid-call).
        # Scripted checklist path only — scenario mode uses Gemini until dynamic end.
        if idx >= len(questions) and not session_uses_scenario_mode(session):
            closing = (
                "Thank you. I will log the resolution code and update our RCM system. "
                "This interaction is documented on the claim file. Goodbye."
            )
            session.status = "completed"
            record["status"] = "completed"
            session.add_turn("assistant", closing)
            record.setdefault("transcript", []).append(
                {"role": "agent", "text": closing, "language": lang, "stage": "call_summary"}
            )
            self._persist_call_async(session.call_id)
            logger.info("Workflow complete for call %s — closing", session.call_id)
            return await self._build_agent_twiml(
                session, base_url, closing, lang, end_call=True
            )

        if session_uses_scenario_mode(session):
            confirmed = self._confirmed_status_line(session)
            if confirmed:
                if session.active_turn_timer:
                    timer = session.active_turn_timer
                    timer.metrics.path = "workflow"
                    timer.metrics.model_route_reason = "validated_fact_next_question"
                    timer.metrics.llm_ms = 0.0
                return await self._finalize_gemini_turn_twiml(
                    session,
                    record,
                    {
                        "response_text": confirmed,
                        "conversation_stage": "information_gathering",
                        "advance_workflow": True,
                        "detected_emotion": "neutral",
                    },
                    transcript,
                    idx,
                    lang,
                    questions,
                    base_url,
                )
            if session.active_turn_timer:
                session.active_turn_timer.metrics.path = "gemini"
            if get_settings().backchannel_enabled:
                return await self._begin_gemini_with_backchannel(
                    session, record, transcript, base_url, lang, idx
                )
            ai = await self._gemini_turn(session, transcript)
            return await self._finalize_gemini_turn_twiml(
                session, record, ai, transcript, idx, lang, questions, base_url
            )

        _v, response_text, verbal_meta, cache_key = select_reply_variant(
            session.call_id,
            idx,
            transcript,
            session.used_acks,
            session.phrase_meta,
            session.phrase_verbal_meta,
            lang,
        )
        self._note_ack_used(session, verbal_meta)
        cached = session.phrase_cache.get(cache_key)
        session.discovery_index = idx + 1
        self._set_last_question(session, response_text, index=idx)
        logger.info("Advance to Q%s for call %s (variant %s)", idx, session.call_id, _v)
        twiml = await self._build_agent_twiml(
            session, base_url, response_text, lang, end_call=False, cached_turn=cached
        )
        agent_entry = attach_metrics(
            {
                "role": "agent",
                "text": response_text,
                "language": lang,
                "stage": "discovery",
                "verbal": verbal_meta,
            },
            self._finish_turn_metrics(session, "fast"),
        )
        session.add_turn("assistant", response_text)
        record.setdefault("transcript", []).append(agent_entry)
        self._persist_call_async(session.call_id)
        return twiml

    async def build_speech_response_twiml(
        self, session: CallSession, speech_text: str, base_url: str
    ) -> str:
        record = call_records.get(session.call_id, {})
        raw = speech_text.strip()
        transcript = correct_stt_transcript(raw, session.last_question_asked)
        if transcript != raw:
            logger.info(
                "Call %s STT corrected %r → %r",
                session.call_id,
                raw[:80],
                transcript[:80],
            )
        lang = session.preferred_language
        questions = discovery_questions(lang)

        if not transcript:
            idx = session.last_question_index
            reprompt = repeat_response(
                lang,
                session.last_question_asked or next_discovery_question(lang, idx),
            )
            cached = session.phrase_cache.get(f"repeat:{idx}")
            self._set_last_question(session, reprompt, index=idx)
            twiml = await self._build_agent_twiml(
                session, base_url, reprompt, lang, cached_turn=cached
            )
            self._finish_turn_metrics(session, "reprompt_empty")
            return twiml

        session.add_turn("user", transcript)
        session.turn_count += 1
        confidence = (
            session.active_turn_timer.metrics.twilio_stt_confidence
            if session.active_turn_timer
            else None
        )
        validation = self._interpret_customer_answer(
            session, transcript, confidence
        )
        _sync_rcm_fields_to_record(session, record)
        cust: dict[str, Any] = {
            "role": "customer",
            "text": transcript,
            "raw_stt": raw,
            "raw_transcript": raw,
            "interpreted_topic": validation.topic,
            "validation_result": "accepted" if validation.accepted else "ambiguous",
            "clarification_reason": validation.reason,
            "interpretation": validation.to_dict(),
            "language": lang,
        }
        partial = self._customer_metrics_partial(session)
        if partial:
            cust["at"] = time.time()
            cust["metrics"] = partial
        record.setdefault("transcript", []).append(cust)

        if self._stt_looks_wrong(session, transcript):
            idx = session.last_question_index
            reprompt = repeat_response(
                lang,
                session.last_question_asked or next_discovery_question(lang, idx),
            )
            cached = session.phrase_cache.get(f"repeat:{idx}")
            session.recent_stt.clear()
            twiml = await self._build_agent_twiml(
                session, base_url, reprompt, lang, cached_turn=cached
            )
            agent_entry = attach_metrics(
                {"role": "agent", "text": reprompt, "language": lang, "stage": "repeat"},
                self._finish_turn_metrics(session, "stt_retry"),
            )
            session.add_turn("assistant", reprompt)
            record.setdefault("transcript", []).append(agent_entry)
            self._persist_call_async(session.call_id)
            return twiml

        # Fast path: repeat request — skip STT/Gemini/TTS generation
        if user_wants_repeat(transcript):
            idx = session.last_question_index
            response_text = repeat_response(
                lang,
                session.last_question_asked or next_discovery_question(lang, idx),
            )
            cached = session.phrase_cache.get(f"repeat:{idx}")
            self._set_last_question(session, response_text, index=idx)
            logger.info("Fast repeat for call %s", session.call_id)
            twiml = await self._build_agent_twiml(
                session, base_url, response_text, lang, cached_turn=cached
            )
            agent_entry = attach_metrics(
                {"role": "agent", "text": response_text, "language": lang, "stage": "repeat"},
                self._finish_turn_metrics(session, "repeat"),
            )
            session.add_turn("assistant", response_text)
            record.setdefault("transcript", []).append(agent_entry)
            self._persist_call_async(session.call_id)
            return twiml

        idx = session.last_question_index
        followup_key = re.sub(
            r"\s+", " ", (session.answer_question or session.last_question_asked or f"question:{idx}").strip().lower()
        )
        action = dialogue_action(validation, followup_key in session.followup_used)
        if action == "clarify":
            new_question = validation.reason == "help_agreed"
            if not new_question:
                session.followup_used.add(followup_key)
            response_text = validation.clarification
            self._set_last_question(
                session,
                response_text,
                index=idx,
                preserve_answer_context=(
                    validation.reason != "low_confidence_confirm" and not new_question
                ),
            )
            logger.info(
                "Clarifying incomplete payer answer call=%s confidence=%s chars=%d",
                session.call_id,
                confidence,
                len(transcript),
            )
            twiml = await self._build_agent_twiml(
                session, base_url, response_text, lang
            )
            agent_entry = attach_metrics(
                {
                    "role": "agent",
                    "text": response_text,
                    "language": lang,
                    "stage": "clarification",
                },
                self._finish_turn_metrics(session, "clarification"),
            )
            session.add_turn("assistant", response_text)
            record.setdefault("transcript", []).append(agent_entry)
            self._persist_call_async(session.call_id)
            return twiml

        if action == "move_on":
            response_text = move_on_after_ambiguity(validation.topic)
            self._set_last_question(session, response_text, index=idx)
            twiml = await self._build_agent_twiml(
                session, base_url, response_text, lang
            )
            agent_entry = attach_metrics(
                {
                    "role": "agent",
                    "text": response_text,
                    "language": lang,
                    "stage": "clarification",
                },
                self._finish_turn_metrics(session, "clarification_bounded"),
            )
            session.add_turn("assistant", response_text)
            record.setdefault("transcript", []).append(agent_entry)
            self._persist_call_async(session.call_id)
            return twiml

        return await self._advance_discovery(
            session, record, transcript, base_url, lang, questions
        )


conversation_engine = ConversationEngine()
