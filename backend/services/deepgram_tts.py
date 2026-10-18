"""Deepgram Aura text-to-speech for Twilio-compatible phone audio.

No network request is made until text_to_speech() is explicitly called.
For phone playback the service requests 8 kHz mu-law directly to avoid an
extra local resampling pass.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import httpx

from config import get_settings
from services.voice_config import normalize_tts_text

logger = logging.getLogger(__name__)
DEEPGRAM_SPEAK_URL = "https://api.deepgram.com/v1/speak"


class DeepgramTtsService:
    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = str(getattr(settings, "deepgram_api_key", "") or "").strip()
        self.model = str(getattr(settings, "deepgram_tts_model", "aura-2") or "aura-2").strip()
        self.voice = str(getattr(settings, "deepgram_tts_voice", "") or "").strip()
        self.timeout_s = 8.0
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None

    def _http_client(self) -> httpx.AsyncClient:
        """Keep-alive client so each turn skips a new TLS handshake."""
        loop = asyncio.get_running_loop()
        if self._client is None or self._client_loop is not loop or getattr(self._client, "is_closed", False):
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s, connect=2.5))
            self._client_loop = loop
        return self._client

    def is_configured(self) -> bool:
        return bool(self.api_key and (self.voice or self.model))

    async def text_to_speech(self, text: str, *, for_twilio: bool = True) -> bytes:
        if not self.is_configured():
            raise RuntimeError("Deepgram TTS is not configured; set DEEPGRAM_API_KEY and DEEPGRAM_TTS_MODEL or DEEPGRAM_TTS_VOICE")
        clean = normalize_tts_text(text)
        if not clean:
            raise ValueError("TTS text is empty")

        # Deepgram Aura model IDs are voice-specific (e.g. aura-2-thalia-en).
        # Prefer the explicit voice setting; otherwise allow model to be a full ID.
        model_id = self.voice or self.model
        params = {"model": model_id}
        if for_twilio:
            params.update({"encoding": "mulaw", "sample_rate": "8000"})
        else:
            params.update({"encoding": "linear16", "sample_rate": "24000"})

        client = self._http_client()
        try:
            response = await client.post(
                DEEPGRAM_SPEAK_URL,
                params=params,
                headers={"Authorization": f"Token {self.api_key}", "Content-Type": "application/json"},
                json={"text": clean},
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError("Deepgram TTS timed out") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Deepgram TTS request failed ({type(exc).__name__})") from exc

        if response.status_code >= 400:
            # Never include response body or credentials; provider bodies may echo request details.
            raise RuntimeError(f"Deepgram TTS HTTP {response.status_code}")
        audio = response.content
        kind = (response.headers.get("content-type") or "").lower()
        if len(audio) < 400 or "json" in kind or "html" in kind or "xml" in kind:
            raise RuntimeError("Deepgram TTS returned an invalid or empty audio payload")
        logger.info("TTS provider=deepgram_tts model=%s bytes=%d", model_id, len(audio))
        return audio

    async def iter_ulaw(self, text: str) -> AsyncIterator[bytes]:
        """Yield μ-law frames as Deepgram writes the speak response."""
        if not self.is_configured():
            raise RuntimeError("Deepgram TTS is not configured")
        clean = normalize_tts_text(text)
        if not clean:
            return
        model_id = self.voice or self.model
        client = self._http_client()
        try:
            async with client.stream(
                "POST",
                DEEPGRAM_SPEAK_URL,
                params={"model": model_id, "encoding": "mulaw", "sample_rate": "8000"},
                headers={
                    "Authorization": f"Token {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"text": clean},
            ) as response:
                if response.status_code >= 400:
                    raise RuntimeError(f"Deepgram TTS HTTP {response.status_code}")
                kind = (response.headers.get("content-type") or "").lower()
                if "json" in kind or "html" in kind or "xml" in kind:
                    raise RuntimeError("Deepgram TTS returned an invalid audio payload")
                buf = b""
                async for chunk in response.aiter_bytes():
                    buf += chunk
                    while len(buf) >= 640:
                        yield buf[:640]
                        buf = buf[640:]
                if len(buf) >= 200:
                    yield buf
        except httpx.TimeoutException as exc:
            raise RuntimeError("Deepgram TTS timed out") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Deepgram TTS request failed ({type(exc).__name__})") from exc
