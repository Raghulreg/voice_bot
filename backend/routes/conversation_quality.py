"""API for voice tuning, verbal policy, and latency monitoring (UI dashboard)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from config import get_settings, reload_settings
from models.schemas import VoiceSettingsPatch
from services.backchannel import BACKCHANNEL_PHRASES
from services.tts_service import TtsService
from services.verbal_composer import FORMAL_ACKS, INFORMAL_ACKS, LIGHT_ACKS, VARIANT_COUNT
from utils.env_file import set_env_value

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["conversation-quality"])


def _voice_payload() -> dict:
    s = get_settings()
    return {
        "stability": s.elevenlabs_stability,
        "similarity": s.elevenlabs_similarity,
        "style": s.elevenlabs_style,
        "speed": s.elevenlabs_speed,
        "volume_gain": s.elevenlabs_volume_gain,
        "speaker_boost": s.elevenlabs_speaker_boost,
        "voice_id_configured": bool(s.elevenlabs_voice_id),
        "model_id": s.elevenlabs_model_id,
        "emotion_note": (
            "Chris adjusts speed/stability/style slightly from detected payer mood "
            "(busy → faster; supportive → slower/warmer) on each TTS request."
        ),
        "backchannel_enabled": s.backchannel_enabled,
        "low_latency_calls": s.low_latency_calls,
        "twilio_gather_speech_timeout": s.twilio_gather_speech_timeout,
    }


@router.get("/conversation-quality")
async def get_conversation_quality():
    """Voice controls, verbal variety policy, and latency logging capabilities."""
    return {
        "voice": _voice_payload(),
        "verbal_habits": {
            "enabled": True,
            "reply_variants_per_step": VARIANT_COUNT,
            "description": (
                "Each workflow step precaches multiple phrasings; the composer picks a variant "
                "per turn using call id, payer text, and ack history so lines are not identical."
            ),
            "ack_tiers": [
                {"id": "skip", "label": "No standalone ack", "examples": ["(question only)"]},
                {"id": "light", "label": "Light fillers", "examples": list(LIGHT_ACKS[:5])},
                {"id": "informal", "label": "Informal bridges", "examples": list(INFORMAL_ACKS[:5])},
            ],
            "backchannel_phrases": list(BACKCHANNEL_PHRASES),
            "gemini_polish": "Strips repeated formal acks; skips leading ack after backchannel.",
        },
        "acknowledgement_policy": {
            "avoid_repeated_formal": True,
            "formal_ack_list": list(FORMAL_ACKS),
            "session_memory": "Tracks used_acks and backchannels_used for the whole call.",
            "after_backchannel_rule": "Main line must not start with another ack (Okay/Right already played).",
        },
        "turn_latency": {
            "enabled": True,
            "per_turn_fields": [
                "stt_ms (Gemini streaming STT final)",
                "llm_ms (Gemini reasoning)",
                "tts_ms (ElevenLabs synthesis)",
                "twilio_delay_ms (Media Streams WS send overhead)",
                "total_response_ms (STT + handler)",
                "first_audio_ms (time to first TTS audio)",
                "handler_ms (dialogue handler wall time)",
                "prompt_build_ms (context / exemplars in prompt)",
                "tts_cache_hit",
                "path (gemini_stream | echo | fast | …)",
                "gap_since_last_agent_ms",
                "twilio_stt_confidence",
            ],
            "observability": {
                "opentelemetry": "OTEL_ENABLED + optional OTEL_EXPORTER_OTLP_ENDPOINT",
                "cloudwatch": "CLOUDWATCH_ENABLED=true + AWS credentials",
                "sentry": "SENTRY_DSN",
            },
            "call_level_summary": "latency_summary on each saved call JSON",
            "view_in_ui": "Call Records → expand a call → Turn latency panel",
        },
    }


@router.patch("/conversation-quality/voice")
async def patch_voice_settings(body: VoiceSettingsPatch):
    """Update voice / latency toggles (writes backend/.env and reloads settings)."""
    mapping: list[tuple[str, object | None, str]] = [
        ("ELEVENLABS_STABILITY", body.stability, str),
        ("ELEVENLABS_SIMILARITY", body.similarity, str),
        ("ELEVENLABS_STYLE", body.style, str),
        ("ELEVENLABS_SPEED", body.speed, str),
        ("ELEVENLABS_VOLUME_GAIN", body.volume_gain, str),
        ("ELEVENLABS_SPEAKER_BOOST", body.speaker_boost, lambda v: "true" if v else "false"),
        ("BACKCHANNEL_ENABLED", body.backchannel_enabled, lambda v: "true" if v else "false"),
        ("LOW_LATENCY_CALLS", body.low_latency_calls, lambda v: "true" if v else "false"),
        ("TWILIO_GATHER_SPEECH_TIMEOUT", body.twilio_gather_speech_timeout, str),
    ]
    updated: list[str] = []
    for key, val, fmt in mapping:
        if val is None:
            continue
        set_env_value(key, fmt(val))
        updated.append(key)
    if not updated:
        raise HTTPException(status_code=400, detail="No settings provided to update.")
    reload_settings()
    logger.info("Updated conversation quality settings: %s", ", ".join(updated))
    return {"updated": updated, "voice": _voice_payload()}


@router.post("/conversation-quality/preview")
async def preview_voice():
    """Short TTS sample using current .env voice settings (same as live calls)."""
    tts = TtsService()
    if not tts.is_configured():
        raise HTTPException(
            status_code=503,
            detail="ElevenLabs not configured. Set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID.",
        )
    sample = (
        "Hello. This is Chris from hospital revenue cycle management, "
        "following up on a pending claim. This preview uses your current speed and stability settings."
    )
    try:
        audio = await tts.text_to_speech(sample, for_twilio=False, delivery_emotion="neutral")
    except Exception as e:
        logger.exception("Preview TTS failed")
        raise HTTPException(status_code=502, detail=str(e)) from e
    return Response(content=audio, media_type="audio/mpeg")

