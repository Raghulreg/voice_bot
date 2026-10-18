"""Text-to-speech for Twilio Gather.

Provider is selected by TTS_PRIMARY. Deepgram Aura, Windows SAPI, Gemini TTS,
and ElevenLabs are supported. Fallbacks are used only when listed in
TTS_FALLBACK_PROVIDERS, and provider reporting records the engine that
actually generated the audio.
Never uses Edge TTS / browser speech engines.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from services.audio_utils import mulaw_to_pcm_wav as _mulaw_to_pcm_wav_impl
from services.audio_utils import mulaw_to_wav, wav_to_mulaw
from services.elevenlabs_service import ElevenLabsService

logger = logging.getLogger(__name__)

# After Gemini TTS 429, skip it so turns go straight to SAPI (no multi-model lag)
_gemini_cooldown_until = 0.0
_GEMINI_COOLDOWN_S = 300.0
# After ElevenLabs quota, skip so first reply is not multi-second model retries
_elevenlabs_skip_until = 0.0
_ELEVENLABS_SKIP_S = 120.0
_deepgram_skip_until = 0.0
_DEEPGRAM_SKIP_S = 60.0


class TtsService:
    def __init__(self) -> None:
        self._eleven = ElevenLabsService()
        self._gemini_tts = None
        self._deepgram_tts = None
        # Stay unverified until a provider actually returns audio.
        self._last_provider = "unverified"

    def _get_gemini_tts(self):
        if self._gemini_tts is None:
            from services.gemini_tts import GeminiTtsService

            self._gemini_tts = GeminiTtsService()
        return self._gemini_tts

    def _get_deepgram_tts(self):
        if self._deepgram_tts is None:
            from services.deepgram_tts import DeepgramTtsService

            self._deepgram_tts = DeepgramTtsService()
        return self._deepgram_tts

    def provider_name(self) -> str:
        return self._last_provider

    def primary_provider(self) -> str:
        from config import get_settings

        return _normalize_provider(getattr(get_settings(), "tts_primary", "")) or "windows_sapi"

    def fallback_providers(self) -> list[str]:
        """Providers explicitly approved in TTS_FALLBACK_PROVIDERS, in order."""
        from config import get_settings

        raw = str(getattr(get_settings(), "tts_fallback_providers", "") or "")
        names: list[str] = []
        for item in raw.split(","):
            name = _normalize_provider(item)
            if name and name not in names:
                names.append(name)
        return names

    def provider_order(self, *, allow_fallback: bool) -> list[str]:
        primary = self.primary_provider()
        if not allow_fallback:
            return [primary]
        return [primary, *[name for name in self.fallback_providers() if name != primary]]

    def voice_identity(self, provider: str | None = None) -> str:
        """Provider and voice that produced (or would produce) a clip."""
        from config import get_settings

        name = provider or self.primary_provider()
        settings = get_settings()
        if name == "deepgram_tts":
            voice = (
                str(getattr(settings, "deepgram_tts_voice", "") or "").strip()
                or str(getattr(settings, "deepgram_tts_model", "") or "").strip()
            )
        elif name == "elevenlabs":
            voice = str(getattr(settings, "elevenlabs_voice_id", "") or "").strip()
        elif name == "gemini_tts":
            from services.gemini_tts import DEFAULT_VOICE

            voice = DEFAULT_VOICE
        elif name == "windows_sapi":
            voice = "Microsoft David Desktop"
        else:
            voice = ""
        return f"{name}:{voice or 'default'}"

    def is_configured(self) -> bool:
        if self._eleven.is_configured():
            return True
        try:
            if self._get_deepgram_tts().is_configured():
                return True
        except Exception:
            pass
        try:
            if self._get_gemini_tts().is_configured():
                return True
        except Exception:
            pass
        try:
            from services.local_tts import is_available

            return is_available()
        except Exception:
            return False

    async def text_to_speech(
        self,
        text: str,
        language_code: str = "en-US",
        speaker: str | None = None,
        for_twilio: bool = True,
        fast: bool = False,
        delivery_emotion: str = "",
        allow_fallback: bool = True,
        live: bool = True,
    ) -> bytes:
        del language_code, speaker, fast
        last_err: Exception | None = None
        order = self.provider_order(allow_fallback=allow_fallback)
        # Cooldowns exist to skip to a fallback quickly; with no fallback,
        # skipping the only approved voice would just produce silence.
        honor_cooldown = len(order) > 1
        for name in order:
            try:
                audio = await self._synthesize(
                    name,
                    text,
                    for_twilio=for_twilio,
                    delivery_emotion=delivery_emotion,
                    live=live,
                    honor_cooldown=honor_cooldown,
                )
            except Exception as exc:
                last_err = exc
                continue
            if audio:
                self._last_provider = name
                return audio
        if len(order) == 1:
            if last_err is not None:
                raise RuntimeError(f"Primary TTS failed: {last_err}") from last_err
            raise RuntimeError("Primary TTS produced no audio")
        if last_err is not None:
            raise RuntimeError(f"TTS unavailable: {last_err}") from last_err
        raise RuntimeError(
            "TTS not configured. Set a verified TTS provider or GEMINI_API_KEY."
        )

    def supports_chunk_stream(self) -> bool:
        """True when the primary voice can play audio before the full clip exists."""
        name = self.primary_provider()
        if name == "elevenlabs":
            return self._eleven.is_configured()
        if name == "deepgram_tts":
            try:
                return self._get_deepgram_tts().is_configured()
            except Exception:
                return False
        return False

    async def iter_phone_chunks(
        self,
        text: str,
        *,
        delivery_emotion: str = "",
    ) -> AsyncIterator[bytes]:
        name = self.primary_provider()
        if name == "elevenlabs":
            async for chunk in self._eleven.iter_ulaw(text, delivery_emotion=delivery_emotion):
                yield chunk
            return
        if name == "deepgram_tts":
            async for chunk in self._get_deepgram_tts().iter_ulaw(text):
                yield chunk

    async def _synthesize(
        self,
        name: str,
        text: str,
        *,
        for_twilio: bool,
        delivery_emotion: str,
        live: bool,
        honor_cooldown: bool = True,
    ) -> bytes | None:
        global _gemini_cooldown_until, _elevenlabs_skip_until, _deepgram_skip_until
        now = time.monotonic()
        if name == "deepgram_tts":
            if honor_cooldown and now < _deepgram_skip_until:
                return None
            deepgram = self._get_deepgram_tts()
            if not deepgram.is_configured():
                return None
            try:
                audio = await deepgram.text_to_speech(text, for_twilio=for_twilio)
                if for_twilio and not _looks_like_phone_audio(audio):
                    raise RuntimeError("Deepgram TTS returned unusable phone audio")
                return audio
            except Exception as exc:
                _deepgram_skip_until = time.monotonic() + _DEEPGRAM_SKIP_S
                logger.warning("Deepgram TTS failed category=%s; cooling down %.0fs", type(exc).__name__, _DEEPGRAM_SKIP_S)
                raise
        if name == "elevenlabs":
            if not self._eleven.is_configured() or (honor_cooldown and now < _elevenlabs_skip_until):
                return None
            try:
                audio = await self._eleven.text_to_speech(
                    text,
                    for_twilio=for_twilio,
                    delivery_emotion=delivery_emotion,
                    live=live,
                )
            except Exception as exc:
                message = str(exc).lower()
                if "429" in message or "rate limited" in message:
                    logger.warning(
                        "ElevenLabs rate limited category=rate_limited — not an auth failure"
                    )
                elif (
                    "quota" in message
                    or "credits" in message
                    or "quota_exceeded" in message
                    or "quota exhausted" in message
                    or "401" in message
                    or "403" in message
                    or "missing_permissions" in message
                    or "invalid_api_key" in message
                    or "voice unavailable" in message
                    or "402" in message
                ):
                    _elevenlabs_skip_until = now + _ELEVENLABS_SKIP_S
                    logger.warning(
                        "ElevenLabs authentication or plan failure category=%s — skip %.0fs",
                        _auth_failure_category(message),
                        _ELEVENLABS_SKIP_S,
                    )
                else:
                    logger.warning("ElevenLabs TTS failed (%s)", exc)
                raise
            if for_twilio and not _looks_like_phone_audio(audio):
                raise RuntimeError("ElevenLabs returned unusable phone audio")
            return audio
        if name == "gemini_tts":
            if not for_twilio or (honor_cooldown and time.monotonic() < _gemini_cooldown_until):
                return None
            try:
                gemini = self._get_gemini_tts()
                if not gemini.is_configured():
                    return None
                audio = await gemini.text_to_mulaw(text)
            except Exception as exc:
                message = str(exc)
                if "429" in message or "RESOURCE_EXHAUSTED" in message or "quota" in message.lower():
                    _gemini_cooldown_until = time.monotonic() + _GEMINI_COOLDOWN_S
                    logger.warning(
                        "Gemini TTS quota hit — cooling down %.0fs (skip slow retries)",
                        _GEMINI_COOLDOWN_S,
                    )
                else:
                    logger.warning("Gemini TTS failed (%s)", exc)
                raise
            if not _looks_like_phone_audio(audio):
                raise RuntimeError("Gemini TTS returned unusable phone audio")
            return audio
        if name == "windows_sapi":
            if not for_twilio:
                return None
            from services import local_tts

            if not local_tts.is_available():
                return None
            audio = await local_tts.text_to_mulaw(text)
            if not _looks_like_phone_audio(audio):
                raise RuntimeError("Local SAPI returned empty/silent audio")
            if self.primary_provider() == "windows_sapi":
                logger.info("TTS provider=windows_sapi voice=Microsoft David Desktop")
            else:
                logger.warning(
                    "TTS fallback provider=windows_sapi voice=Microsoft David Desktop"
                )
            return audio
        return None

    @staticmethod
    def mulaw_to_pcm_wav(mulaw_bytes: bytes, sample_rate: int = 8000) -> bytes:
        from config import get_settings

        gain = float(get_settings().elevenlabs_volume_gain)
        return _mulaw_to_pcm_wav_impl(mulaw_bytes, sample_rate=sample_rate, gain=gain)

    mulaw_to_wav = staticmethod(mulaw_to_wav)
    wav_to_mulaw = staticmethod(wav_to_mulaw)


def _normalize_provider(value: str | None) -> str:
    name = (value or "").strip().lower()
    if not name:
        return ""
    if name in ("gemini", "gemini_tts"):
        return "gemini_tts"
    if name == "elevenlabs":
        return "elevenlabs"
    if name in ("deepgram", "deepgram_tts", "aura", "aura-2"):
        return "deepgram_tts"
    if name in ("windows_sapi", "sapi"):
        return "windows_sapi"
    return ""


def _auth_failure_category(message: str) -> str:
    if "missing_permissions" in message:
        return "missing_permissions"
    if "invalid_api_key" in message:
        return "invalid_api_key"
    if "402" in message or "voice unavailable" in message:
        return "voice_unavailable"
    if "quota" in message or "credits" in message:
        return "quota"
    if "401" in message or "403" in message:
        return "auth"
    return "unavailable"


def _looks_like_phone_audio(audio: bytes | None) -> bool:
    """Guard: never ship JSON errors or near-empty buffers to Twilio."""
    if not audio or len(audio) < 400:
        return False
    head = audio[:32].lstrip()
    if head[:1] in (b"{", b"[", b"<"):
        return False
    if b'"detail"' in audio[:160]:
        return False
    return True
