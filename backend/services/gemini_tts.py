"""Gemini TTS — clear neural speech fallback for Twilio Media Streams.

Uses gemini-2.5-flash-preview-tts → PCM 24 kHz → μ-law 8 kHz.
"""

from __future__ import annotations

import logging
from typing import Any

from google import genai
from google.genai import types

from config import get_settings
from services.voice_config import normalize_tts_text

logger = logging.getLogger(__name__)

try:
    import audioop
except ImportError:
    import audioop_lts as audioop  # type: ignore

DEFAULT_VOICE = "Fenrir"
TTS_MODELS = (
    "gemini-2.5-flash-preview-tts",
    "gemini-2.5-flash-tts",
    "gemini-2.0-flash-preview-tts",
)
_US_ACCENT_INSTRUCTION = (
    "You are a text-to-speech engine. Speak only the user's message. "
    "Accent: natural General American (United States) English. "
    "Pace: normal conversational (about 145–155 words per minute). "
    "Not rushed, not slow or sleepy. "
    "Tone: clear, professional, friendly. Do not add or omit words."
)


class GeminiTtsService:
    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = (settings.gemini_api_key or "").strip()
        self._client = genai.Client(api_key=self.api_key) if self.api_key else None
        self.model = TTS_MODELS[0]
        self.voice = DEFAULT_VOICE
        self._working_model: str | None = None

    def is_configured(self) -> bool:
        return bool(self._client)

    def _models(self) -> list[str]:
        out: list[str] = []
        for m in (self._working_model, self.model, *TTS_MODELS):
            if m and m not in out:
                out.append(m)
        return out

    async def text_to_mulaw(self, text: str, *, voice: str | None = None) -> bytes:
        if not self._client:
            raise RuntimeError("Gemini TTS not configured (GEMINI_API_KEY)")

        clean = normalize_tts_text(text) or ""
        clean = " ".join(clean.split())
        if not clean:
            raise ValueError("Gemini TTS text is empty")

        spoken = clean[:800]
        voice_name = voice or self.voice
        last_err: Exception | None = None

        for model in self._models():
            try:
                response = await self._client.aio.models.generate_content(
                    model=model,
                    contents=spoken,
                    config=types.GenerateContentConfig(
                        system_instruction=_US_ACCENT_INSTRUCTION,
                        response_modalities=["AUDIO"],
                        speech_config=types.SpeechConfig(
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                    voice_name=voice_name
                                )
                            )
                        ),
                    ),
                )
                pcm = _extract_pcm(response)
                if not pcm:
                    raise RuntimeError("Gemini TTS returned no audio")
                mulaw = _pcm24k_to_mulaw8k(pcm)
                if not mulaw:
                    raise RuntimeError("Gemini TTS μ-law conversion produced empty audio")
                self._working_model = model
                self.model = model
                logger.info(
                    "Gemini TTS ok model=%s voice=%s bytes=%d chars=%d",
                    model,
                    voice_name,
                    len(mulaw),
                    len(spoken),
                )
                return mulaw
            except Exception as e:
                last_err = e
                msg = str(e)
                # Quota → fail immediately so phone turn falls to SAPI without multi-model lag
                if any(t in msg for t in ("429", "RESOURCE_EXHAUSTED", "quota")):
                    logger.warning(
                        "Gemini TTS quota failed model=%s — failing fast (%s)",
                        model,
                        msg[:100],
                    )
                    raise
                if any(t in msg for t in ("404", "NOT_FOUND")):
                    logger.warning(
                        "Gemini TTS model=%s failed (%s) — trying next",
                        model,
                        msg[:100],
                    )
                    continue
                raise
        assert last_err is not None
        raise last_err


def _extract_pcm(response: Any) -> bytes:
    try:
        parts = response.candidates[0].content.parts
    except Exception:
        return b""
    for part in parts:
        inline = getattr(part, "inline_data", None)
        if inline is None:
            continue
        data = getattr(inline, "data", None)
        if not data:
            continue
        if isinstance(data, str):
            import base64

            return base64.b64decode(data)
        return bytes(data)
    return b""


def _pcm24k_to_mulaw8k(pcm_24k: bytes) -> bytes:
    """Gemini TTS is L16 PCM @ 24 kHz → Twilio μ-law @ 8 kHz."""
    if len(pcm_24k) < 4:
        return b""
    # Ensure even length for 16-bit samples
    if len(pcm_24k) % 2:
        pcm_24k = pcm_24k[:-1]
    pcm_8k, _ = audioop.ratecv(pcm_24k, 2, 1, 24000, 8000, None)
    # Mild gain so phone audio is clear without clipping
    pcm_8k = audioop.mul(pcm_8k, 2, 1.15)
    return audioop.lin2ulaw(pcm_8k, 2)
