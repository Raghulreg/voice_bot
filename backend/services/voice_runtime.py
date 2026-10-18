"""Select the voice providers the running call path can actually use.

Empty credentials and unapproved models stay inactive. Twilio Gather remains
the phone path until a streaming provider is configured and connected.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

FLASH_V2_5_MODEL = "eleven_flash_v2_5"
LATENCY_SLO_METRIC = "eos_to_first_audio_ms"
LATENCY_SLO_DEFINITION = (
    "Payer speech end to first audible AI response. "
    "Excludes payer speaking time and input wait (input_wait_ms)."
)


class VoiceConfigError(Exception):
    """A provider was requested without the configuration needed to run it."""


def _text(settings: Any, name: str, default: str = "") -> str:
    return str(getattr(settings, name, default) or default).strip()


def _flag(settings: Any, name: str, default: bool = False) -> bool:
    value = getattr(settings, name, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


_account_type_cache: dict[str, Any] = {"value": "", "expires": 0.0}


def cached_twilio_account_type() -> str:
    """Return Trial or Full. Empty means the lookup failed. No credentials are logged."""
    now = time.monotonic()
    if now < float(_account_type_cache.get("expires") or 0):
        return str(_account_type_cache.get("value") or "")
    account_type = ""
    try:
        from config import get_settings
        from twilio.http.http_client import TwilioHttpClient
        from twilio.rest import Client

        settings = get_settings()
        sid = _text(settings, "twilio_account_sid")
        token = _text(settings, "twilio_auth_token")
        if sid and token:
            client = Client(sid, token, http_client=TwilioHttpClient(timeout=4))
            account = client.api.accounts(sid).fetch()
            account_type = str(getattr(account, "type", "") or "")
    except Exception:
        logger.warning("Twilio account type lookup failed. Gather remains available.")
        account_type = ""
    _account_type_cache["value"] = account_type
    _account_type_cache["expires"] = now + 300
    return account_type


def missing_livekit(settings: Any) -> list[str]:
    required = {
        "LIVEKIT_URL": _text(settings, "livekit_url"),
        "LIVEKIT_API_KEY": _text(settings, "livekit_api_key"),
        "LIVEKIT_API_SECRET": _text(settings, "livekit_api_secret"),
    }
    return [name for name, value in required.items() if not value]


def account_supports_media_streams(account_type: str) -> bool:
    """Twilio trial accounts strip <Stream>. Only a Full account keeps it."""
    return (account_type or "").strip().lower() == "full"


def select_voice_transport(settings: Any, *, account_type: str = "") -> dict[str, Any]:
    """Choose Gather or Media Streams. Gather wins unless streams are enabled and allowed."""
    requested = _text(settings, "voice_transport", "gather").lower() or "gather"
    if requested not in {"gather", "media_streams"}:
        raise VoiceConfigError(
            "VOICE_TRANSPORT must be gather or media_streams. "
            "Twilio Gather remains the phone path."
        )
    if requested == "gather":
        return {
            "requested": "gather",
            "active": "gather",
            "fallback": False,
            "account_type": account_type or "unknown",
            "stream_supported": account_supports_media_streams(account_type),
            "reason": "VOICE_TRANSPORT=gather.",
        }
    if not account_supports_media_streams(account_type):
        detail = (
            "Twilio trial accounts strip the Stream verb and replace it with a Say message. "
            if (account_type or "").strip().lower() == "trial"
            else "Twilio account support for Stream could not be verified. "
        )
        return {
            "requested": "media_streams",
            "active": "gather",
            "fallback": True,
            "account_type": account_type or "unknown",
            "stream_supported": False,
            "reason": detail + "Twilio Gather remains the phone path.",
        }
    if not _text(settings, "deepgram_api_key") or not _text(settings, "deepgram_model"):
        return {
            "requested": "media_streams",
            "active": "gather",
            "fallback": True,
            "account_type": account_type or "Full",
            "stream_supported": True,
            "reason": (
                "Media Streams needs DEEPGRAM_API_KEY and DEEPGRAM_MODEL. "
                "Twilio Gather remains the phone path."
            ),
        }
    return {
        "requested": "media_streams",
        "active": "media_streams",
        "fallback": False,
        "account_type": "Full",
        "stream_supported": True,
        "reason": "VOICE_TRANSPORT=media_streams on a Twilio account that allows Stream.",
    }


def media_stream_stt_choice(settings: Any) -> str:
    """STT for the Media Streams websocket path. Gather never uses this."""
    if not _flag(settings, "voice_streaming_enabled", True):
        return "existing"
    if not _text(settings, "deepgram_api_key"):
        return "existing"
    if not _text(settings, "deepgram_model"):
        raise VoiceConfigError(
            "Deepgram STT is not active. DEEPGRAM_API_KEY is set but DEEPGRAM_MODEL is empty. "
            "Twilio Gather remains the phone speech path."
        )
    return "deepgram"


def resolve_voice_runtime(settings: Any, *, account_type: str = "") -> dict[str, Any]:
    """Public provider status. Contains no credentials."""
    deepgram_key = bool(_text(settings, "deepgram_api_key"))
    deepgram_model = _text(settings, "deepgram_model", "nova-3") or "nova-3"
    tts_provider = _text(settings, "tts_primary", "windows_sapi") or "windows_sapi"
    simple_model = _text(settings, "gemini_simple_model")
    tts_model = _text(settings, "elevenlabs_model_id", "eleven_flash_v2_5")
    livekit_missing = missing_livekit(settings)
    vad_provider = _text(settings, "vad_provider", "silero").lower() or "silero"
    vad_enabled = _flag(settings, "vad_enabled", True)
    tone_enabled = _flag(settings, "smart_tone_detection_enabled", False)
    try:
        stream_stt = media_stream_stt_choice(settings)
    except VoiceConfigError:
        stream_stt = "existing"
    try:
        transport = select_voice_transport(settings, account_type=account_type)
    except VoiceConfigError as exc:
        transport = {
            "requested": _text(settings, "voice_transport", "gather"),
            "active": "gather",
            "fallback": True,
            "account_type": account_type or "unknown",
            "stream_supported": False,
            "reason": str(exc),
        }
    active_gather = transport["active"] != "media_streams"

    return {
        "active_phone_path": "twilio_gather" if active_gather else "twilio_media_streams",
        "active_stt": "twilio_gather" if active_gather else stream_stt,
        "gather_fallback": bool(transport["fallback"]) or active_gather,
        "voice_transport": transport,
        "call_provider_setting": _text(settings, "call_provider", "media_streams"),
        "streaming": {
            "requested": _flag(settings, "voice_streaming_enabled", True),
            "operational_on_active_path": (
                transport["active"] == "media_streams" and stream_stt == "deepgram"
            ),
            "media_streams_path_implemented": True,
            "reason": (
                "Twilio Media Streams is forwarding caller audio to Deepgram, "
                "then streaming the reply back on the same call."
                if transport["active"] == "media_streams" and stream_stt == "deepgram"
                else transport["reason"]
            ),
        },
        "deepgram": {
            "configured": deepgram_key,
            "model": deepgram_model,
            "model_verified_on_account": False,
            "operational": False,
            "selected_on_active_path": (
                transport["active"] == "media_streams" and stream_stt == "deepgram"
            ),
            "media_stream_stt": stream_stt,
            "missing": [] if deepgram_key else ["DEEPGRAM_API_KEY"],
            "reason": (
                "Deepgram is used only on the Media Streams path when DEEPGRAM_API_KEY is set. "
                "The live phone path is Twilio Gather."
            ),
        },
        "livekit": {
            "operational": False,
            "connected": False,
            "missing": livekit_missing,
            "phone_path": "twilio",
            "reason": (
                "Set LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET to open an "
                "orchestration room. Phone audio stays on Twilio. A room is marked "
                "connected only after CreateRoom succeeds."
                if livekit_missing
                else (
                    "LiveKit credentials are set. Each call opens an orchestration room. "
                    "Phone audio stays on Twilio."
                )
            ),
        },
        "tts": {
            "provider": tts_provider,
            "model": (
                _text(settings, "deepgram_tts_voice") or _text(settings, "deepgram_tts_model", "aura-2")
                if tts_provider.lower() in ("deepgram", "deepgram_tts", "aura", "aura-2")
                else tts_model if tts_provider.lower() == "elevenlabs"
                else "gemini-tts" if tts_provider.lower() in ("gemini", "gemini_tts")
                else "windows-sapi"
            ),
            "voice": (
                _text(settings, "deepgram_tts_voice") or _text(settings, "deepgram_tts_model", "aura-2")
                if tts_provider.lower() in ("deepgram", "deepgram_tts", "aura", "aura-2")
                else "Microsoft David Desktop" if tts_provider.lower() == "windows_sapi"
                else "configured provider voice"
            ),
            "elevenlabs_model": tts_model,
            "flash_v2_5_selected": tts_model == FLASH_V2_5_MODEL,
            "operational": False,
            "fallback": _text(settings, "tts_fallback_providers") or "none",
            "twilio_say_fallback_configured": bool(_text(settings, "twilio_say_voice")),
        },
        "llm": {
            "primary_model": _text(settings, "gemini_model"),
            "simple_model_configured": bool(simple_model),
            "simple_model_active": bool(simple_model),
            "missing": [] if simple_model else ["GEMINI_SIMPLE_MODEL"],
        },
        "vad": {
            "enabled": vad_enabled,
            "provider": vad_provider,
            "operational_on_active_path": False,
            "reuses": "SILERO_VAD_THRESHOLD, SILERO_VAD_MIN_SILENCE_MS, SILERO_VAD_SPEECH_PAD_MS",
            "reason": (
                "Silero runs on the Media Streams path. Gather uses Twilio end-of-speech. "
                "A non-silero VAD_PROVIDER is not treated as operational."
            ),
        },
        "smart_tone": {
            "enabled": tone_enabled,
            "operational": tone_enabled,
            "changes_claim_facts": False,
            "reason": (
                "Tone changes delivery only. Claim facts stay on the validated answer path."
                if tone_enabled
                else "Smart Tone Detection is off. Set SMART_TONE_DETECTION_ENABLED=true."
            ),
        },
        "latency": {
            "metric": LATENCY_SLO_METRIC,
            "definition": LATENCY_SLO_DEFINITION,
            "target_ms": int(getattr(settings, "voice_latency_target_ms", 1200) or 1200),
            "excludes": ["input_wait_ms"],
            "met": None,
        },
    }
