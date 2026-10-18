from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import field_validator
from pydantic_settings import BaseSettings

ENV_FILE = Path(__file__).parent / ".env"


class Settings(BaseSettings):
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

    # Vapi + Twilio — outbound phone calls (not used for Voice & Quality preview)
    vapi_api_key: str = ""
    vapi_public_key: str = ""
    vapi_assistant_id: str = ""
    vapi_phone_number_id: str = ""
    vapi_voice_id: str = "Elliot"
    vapi_voice_version: str = "2"

    # Verified phone voice. Gemini TTS is not selected until a synthesis succeeds.
    tts_primary: str = "windows_sapi"
    # Comma-separated fallback providers used when TTS_PRIMARY fails. Empty means
    # no cross-provider fallback, so a call never switches to a different voice.
    # Only list providers whose configured voice matches the primary voice.
    tts_fallback_providers: str = ""

    # ElevenLabs — optional fallback. Eric and Turbo stay configured.
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "cjVigY5qzO86Huf0OWal"  # Eric — US male verified on this account
    # Flash v2.5 is the low-latency conversational model. Style stays low so it
    # does not sound flat. Set ELEVENLABS_MODEL_ID=eleven_turbo_v2_5 to opt out.
    elevenlabs_model_id: str = "eleven_flash_v2_5"
    elevenlabs_stability: float = 0.42
    elevenlabs_similarity: float = 0.78
    elevenlabs_style: float = 0.18
    # 1.0 = normal speaking rate (not slow, not rushed)
    elevenlabs_speed: float = 1.0
    elevenlabs_speaker_boost: bool = True
    elevenlabs_volume_gain: float = 1.08

    # Twilio <Say> voice used only when the configured TTS provider cannot
    # produce audio. Empty disables <Say>; set it only to a voice that matches
    # the configured TTS voice.
    twilio_say_voice: str = ""

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    # Empty keeps simple turns on GEMINI_MODEL until a smaller model is approved.
    gemini_simple_model: str = ""

    # Deepgram Nova — Media Streams only. Empty key keeps the existing STT path.
    deepgram_api_key: str = ""
    deepgram_model: str = "nova-3"
    # Deepgram Aura TTS. The voice/model is sent only when TTS_PRIMARY=deepgram.
    deepgram_tts_model: str = "aura-2"
    deepgram_tts_voice: str = ""

    # LiveKit Cloud — not the phone path until a verified room connection exists.
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    backend_host: str = "0.0.0.0"
    backend_port: int = 8000
    public_url: str = ""

    default_agent_name: str = "Chris"
    default_language: str = "en-US"

    # Phone path: media_streams (Twilio Media Streams → WS → STT) or vapi
    call_provider: str = "media_streams"
    # gather is the working phone path. media_streams is used only when selected
    # and the Twilio account allows the Stream verb.
    voice_transport: str = "gather"
    # echo = latency test bot only; ai = full claims dialogue from prompt.py
    dialogue_mode: str = "ai"

    # Silero VAD — end a turn after a short pause, without cutting a hesitation.
    # VAD_ENABLED / VAD_PROVIDER gate this path. Thresholds stay the SILERO_VAD_* settings.
    vad_enabled: bool = True
    vad_provider: str = "silero"
    silero_vad_threshold: float = 0.35
    # ~280ms of quiet before a turn ends, so a short pause is not cut off.
    silero_vad_min_silence_ms: int = 280
    silero_vad_speech_pad_ms: int = 20
    # Adjusts delivery for frustration, confusion, or impatience. Never edits claim facts.
    smart_tone_detection_enabled: bool = True
    # Allows streaming on the Media Streams websocket. Does not make Twilio Gather stream.
    voice_streaming_enabled: bool = True
    # SLO target for eos_to_first_audio_ms. Not a claim that calls meet it.
    voice_latency_target_ms: int = 1200
    # After VAD speech_end → STT/LLM (lower = faster replies)
    silence_timer_ms: int = 240
    # auto = ElevenLabs Scribe first (when key set), then Gemini
    stt_provider: str = "auto"
    # Optional live captions (extra STT calls per ~0.5s) — usually leave false
    stt_enable_partials: bool = False

    # PostgreSQL — durable conversation state (Neon / any Postgres)
    database_url: str = ""
    db_host: str = ""
    db_port: int = 5432
    db_username: str = ""
    db_password: str = ""
    db_name: str = ""

    # Phone latency: use precached replies + skip per-turn Gemini (profile still saved on call record)
    low_latency_calls: bool = False
    # Twilio Gather end-of-speech: "auto" or seconds e.g. "1" (lower = Chris replies sooner; may cut you off)
    twilio_gather_speech_timeout: str = "2"
    # Off: adding partialResultCallback to the greeting TwiML made Twilio hang up
    # before fetching the greeting audio. Timing splits stay available if set true.
    twilio_gather_partial_timing: bool = False
    # Play instant "Right..." then finish Gemini on a follow-up webhook (intelligent, not scripted main reply)
    backchannel_enabled: bool = True

    # Observability — OpenTelemetry + CloudWatch + Sentry
    otel_enabled: bool = True
    otel_service_name: str = "call-agent"
    otel_exporter_otlp_endpoint: str = ""
    otel_console_export: bool = False
    sentry_dsn: str = ""
    sentry_environment: str = "development"
    sentry_traces_sample_rate: float = 0.2
    sentry_profiles_sample_rate: float = 0.0
    cloudwatch_enabled: bool = False
    cloudwatch_namespace: str = "CallAgent/Latency"
    aws_region: str = "us-east-1"
    latency_slow_total_ms: float = 3000.0

    # PII guardrails — mask before Gemini
    pii_masking_enabled: bool = True

    # Redis + Celery — background jobs
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = ""
    celery_result_backend: str = ""
    celery_enabled: bool = False

    # Optional summary email
    email_enabled: bool = False
    email_to: str = ""
    email_from: str = "noreply@localhost"
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True

    @field_validator("public_url", mode="before")
    @classmethod
    def strip_public_url(cls, v: object) -> str:
        if v is None:
            return ""
        return str(v).strip().rstrip("/")

    @field_validator("twilio_phone_number", mode="before")
    @classmethod
    def normalize_twilio_phone(cls, v: object) -> str:
        if v is None:
            return ""
        return str(v).strip().replace(" ", "").replace("-", "")

    class Config:
        env_file = str(ENV_FILE)
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    load_dotenv(ENV_FILE, override=True)
    get_settings.cache_clear()
    return get_settings()
