"""Phone STT for Twilio Media Streams.

Primary: ElevenLabs Scribe (when ELEVENLABS_API_KEY is set) — works when Gemini free-tier is exhausted.
Fallback: Gemini audio STT.

Root cause of "bot not hearing": Gemini 429 RESOURCE_EXHAUSTED empties the transcript.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable

import httpx
from google import genai
from google.genai import types

from config import get_settings
from services.audio_utils import mulaw_to_pcm_wav

logger = logging.getLogger(__name__)

ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"

_FINAL_PROMPT = (
    "Accurate speech-to-text for a U.S. phone call. "
    "Transcribe every clear English word in order. "
    "Keep status words and numbers exact. Do not invent jargon. "
    "If pure silence/hold music: (silence). Plain text only."
)

_PARTIAL_PROMPT = (
    "Partial phone transcription. Spoken English words only. "
    "If no speech: (silence)"
)

PARTIAL_EVERY_BYTES = 4000
PARTIAL_WINDOW_BYTES = 24000
MAX_IN_FLIGHT_PARTIALS = 1
FINAL_MAX_BYTES = 36000  # ~4.5s
ELEVENLABS_TIMEOUT_S = 4.5
GEMINI_TIMEOUT_S = 3.0
MAX_GEMINI_ATTEMPTS = 2

# After Gemini 429, skip Gemini STT for a few minutes
_gemini_stt_cooldown_until = 0.0
_GEMINI_STT_COOLDOWN_S = 90.0


def _partials_enabled() -> bool:
    try:
        return bool(getattr(get_settings(), "stt_enable_partials", False))
    except Exception:
        return False


OnPartial = Callable[[str, bool], Awaitable[None]]


def _trim_mulaw_silence(mulaw: bytes, *, frame: int = 160) -> bytes:
    if len(mulaw) < frame * 4:
        return mulaw
    try:
        import audioop
    except ImportError:
        import audioop_lts as audioop  # type: ignore

    def is_loud(chunk: bytes) -> bool:
        try:
            pcm = audioop.ulaw2lin(chunk, 2)
            return float(audioop.rms(pcm, 2)) >= 200
        except Exception:
            return True

    n = len(mulaw)
    start = 0
    while start + frame <= n and not is_loud(mulaw[start : start + frame]):
        start += frame
    end = n
    while end - frame >= start and not is_loud(mulaw[end - frame : end]):
        end -= frame
    start = max(0, start - frame * 2)
    end = min(n, end + frame * 2)
    trimmed = mulaw[start:end]
    return trimmed if len(trimmed) >= frame * 3 else mulaw


def _prepare_audio(mulaw_audio: bytes) -> bytes:
    audio = _trim_mulaw_silence(mulaw_audio)
    if len(audio) > FINAL_MAX_BYTES:
        audio = audio[-FINAL_MAX_BYTES:]
    return audio


class SttService:
    """ElevenLabs Scribe first → Gemini fallback."""

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = (settings.gemini_api_key or "").strip()
        self.eleven_key = (settings.elevenlabs_api_key or "").strip()
        self.model_name = (settings.gemini_model or "gemini-2.5-flash").strip()
        self.stt_provider = (
            str(getattr(settings, "stt_provider", "auto") or "auto").strip().lower()
        )
        self._client = genai.Client(api_key=self.api_key) if self.api_key else None
        self._http: httpx.AsyncClient | None = None
        self._preferred_gemini: str | None = None
        self.last_provider: str = ""
        self.last_error: str = ""

    def is_configured(self) -> bool:
        return bool(self.eleven_key or self._client)

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(ELEVENLABS_TIMEOUT_S + 1, connect=3.0))
        return self._http

    def open_stream(
        self,
        *,
        call_id: str,
        language_code: str = "en-US",
        on_partial: OnPartial | None = None,
    ) -> "StreamingSttSession":
        return StreamingSttSession(
            stt=self,
            call_id=call_id,
            language_code=language_code,
            on_partial=on_partial,
        )

    def _use_elevenlabs(self) -> bool:
        if not self.eleven_key:
            return False
        if self.stt_provider in ("elevenlabs", "scribe", "auto"):
            return True
        return False

    def _use_gemini(self) -> bool:
        if not self._client:
            return False
        if self.stt_provider in ("gemini", "auto"):
            return time.monotonic() >= _gemini_stt_cooldown_until
        return False

    async def transcribe_mulaw(
        self,
        mulaw_audio: bytes,
        language_code: str = "en-US",
        *,
        partial: bool = False,
    ) -> str:
        if not mulaw_audio:
            return ""
        if not self.is_configured():
            raise RuntimeError(
                "STT not configured. Set ELEVENLABS_API_KEY and/or GEMINI_API_KEY."
            )

        audio = _prepare_audio(mulaw_audio)
        self.last_error = ""
        wav = mulaw_to_pcm_wav(audio, sample_rate=8000)

        errors: list[str] = []

        # 1) ElevenLabs Scribe — primary while Gemini free tier is dead
        if self._use_elevenlabs():
            try:
                text = await asyncio.wait_for(
                    self._elevenlabs_stt(wav, language_code),
                    timeout=ELEVENLABS_TIMEOUT_S,
                )
                cleaned = self._clean_transcript(text)
                self.last_provider = "elevenlabs_scribe"
                logger.info(
                    "STT elevenlabs_scribe bytes=%d chars=%d",
                    len(audio),
                    len(cleaned or ""),
                )
                return cleaned
            except Exception as e:
                errors.append(f"elevenlabs: {e}")
                self.last_error = str(e)
                logger.warning("ElevenLabs STT failed (%s) — trying Gemini if available", e)

        # 2) Gemini
        if self._use_gemini():
            try:
                text = await self._gemini_stt(wav, language_code, partial=partial)
                cleaned = self._clean_transcript(text)
                self.last_provider = "gemini"
                return cleaned
            except Exception as e:
                errors.append(f"gemini: {e}")
                self.last_error = str(e)
                raise

        detail = "; ".join(errors) if errors else "no STT provider available"
        raise RuntimeError(f"STT failed: {detail}")

    async def _elevenlabs_stt(self, wav: bytes, language_code: str) -> str:
        client = await self._get_http()
        # map en-US → eng for Scribe
        lang = "eng"
        if language_code and len(language_code) >= 2:
            # scribe accepts ISO-639-3 or auto
            code = language_code.split("-")[0].lower()
            if code == "en":
                lang = "eng"
            else:
                lang = code

        # Prefer file field per ElevenLabs multipart API
        files = {"file": ("speech.wav", wav, "audio/wav")}
        data = {
            "model_id": "scribe_v1",
            "language_code": lang,
            "tag_audio_events": "false",
            "diarize": "false",
        }
        resp = await client.post(
            ELEVENLABS_STT_URL,
            headers={"xi-api-key": self.eleven_key},
            files=files,
            data=data,
        )
        if resp.status_code >= 400:
            body = resp.text[:300]
            raise RuntimeError(f"ElevenLabs STT HTTP {resp.status_code}: {body}")
        payload = resp.json() or {}
        return str(payload.get("text") or "").strip()

    def _gemini_models(self) -> list[str]:
        primary = (self._preferred_gemini or self.model_name or "gemini-2.5-flash").strip()
        ordered = [primary, "gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]
        out: list[str] = []
        for m in ordered:
            if m and m not in out:
                out.append(m)
        return out[:MAX_GEMINI_ATTEMPTS]

    async def _gemini_stt(
        self,
        wav: bytes,
        language_code: str,
        *,
        partial: bool,
    ) -> str:
        global _gemini_stt_cooldown_until
        assert self._client is not None
        base = _PARTIAL_PROMPT if partial else _FINAL_PROMPT
        prompt = f"{base} Language: {language_code or 'en-US'}."

        last_err: Exception | None = None
        for model in self._gemini_models():
            try:
                text = await asyncio.wait_for(
                    self._call_gemini_model(model, wav, prompt),
                    timeout=GEMINI_TIMEOUT_S,
                )
                self._preferred_gemini = model
                if model != self.model_name:
                    logger.info("STT Gemini model=%s", model)
                return text
            except asyncio.TimeoutError:
                last_err = TimeoutError(f"timeout model={model}")
                logger.warning("STT Gemini timeout model=%s", model)
                continue
            except Exception as e:
                last_err = e
                msg = str(e)
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower():
                    _gemini_stt_cooldown_until = time.monotonic() + _GEMINI_STT_COOLDOWN_S
                    logger.warning(
                        "Gemini STT quota exhausted — cooldown %.0fs; use ElevenLabs",
                        _GEMINI_STT_COOLDOWN_S,
                    )
                    break
                if any(t in msg for t in ("404", "NOT_FOUND", "no longer available")):
                    logger.warning("STT Gemini model=%s unavailable — next", model)
                    continue
                raise
        assert last_err is not None
        raise last_err

    async def _call_gemini_model(self, model: str, wav: bytes, prompt: str) -> str:
        assert self._client is not None
        response = await self._client.aio.models.generate_content(
            model=model,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_bytes(data=wav, mime_type="audio/wav"),
                        types.Part.from_text(text=prompt),
                    ],
                )
            ],
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=160,
            ),
        )
        return (response.text or "").strip()

    @staticmethod
    def _clean_transcript(text: str) -> str:
        cleaned = (text or "").strip().strip('"').strip("'")
        cleaned = re.sub(r"^(transcript|transcription)\s*:\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(
            r"^(i (can'?t|cannot) (hear|understand|transcribe).*$)",
            "",
            cleaned,
            flags=re.I,
        )
        low = cleaned.lower().strip()
        if low in {
            "(silence)",
            "silence",
            "[silence]",
            "n/a",
            "none",
            "...",
            "inaudible",
            "[inaudible]",
            "music",
            "holding",
            "hold music",
            "background noise",
            "",
        }:
            return ""
        if len(cleaned) < 1:
            return ""
        if re.fullmatch(r"[\W_]+", cleaned or ""):
            return ""
        return cleaned.strip()


class StreamingSttSession:
    def __init__(
        self,
        *,
        stt: SttService,
        call_id: str,
        language_code: str = "en-US",
        on_partial: OnPartial | None = None,
    ) -> None:
        self.stt = stt
        self.call_id = call_id
        self.language_code = language_code
        self.on_partial = on_partial
        self.buffer = bytearray()
        self.partial_text = ""
        self.final_text = ""
        self._bytes_since_partial = 0
        self._in_flight = 0
        self._closed = False
        self.last_finalize_ms: float = 0.0
        self.last_partial_ms: float = 0.0

    def feed(self, packet: bytes, *, emit_partials: bool = True) -> None:
        if self._closed or not packet:
            return
        self.buffer.extend(packet)
        self._bytes_since_partial += len(packet)
        if not emit_partials or not _partials_enabled():
            return
        if (
            self._bytes_since_partial >= PARTIAL_EVERY_BYTES
            and self._in_flight < MAX_IN_FLIGHT_PARTIALS
            and len(self.buffer) >= PARTIAL_EVERY_BYTES
        ):
            self._bytes_since_partial = 0
            self._in_flight += 1
            asyncio.create_task(self._run_partial(), name=f"stt-partial-{self.call_id}")

    def snapshot_audio(self) -> bytes:
        """Copy current utterance buffer for speculative STT (stream stays open)."""
        return bytes(self.buffer)

    def mark_finalized_externally(self, text: str, *, latency_ms: float = 0.0) -> None:
        """Apply text from speculative STT and close without a second API call."""
        self._closed = True
        self.final_text = (text or "").strip()
        self.partial_text = self.final_text
        self.last_finalize_ms = float(latency_ms or 0)
        self.buffer.clear()
        self._bytes_since_partial = 0

    async def _run_partial(self) -> None:
        try:
            window = bytes(self.buffer[-PARTIAL_WINDOW_BYTES:])
            if len(window) < PARTIAL_EVERY_BYTES // 2:
                return
            t0 = time.perf_counter()
            text = await self.stt.transcribe_mulaw(
                window, language_code=self.language_code, partial=True
            )
            self.last_partial_ms = (time.perf_counter() - t0) * 1000
            if text and text != self.partial_text:
                self.partial_text = text
                logger.info(
                    "STT partial call=%s provider=%s ms=%.0f chars=%d",
                    self.call_id,
                    self.stt.last_provider,
                    self.last_partial_ms,
                    len(text),
                )
                if self.on_partial:
                    await self.on_partial(text, False)
        except Exception:
            logger.exception("STT partial failed call=%s", self.call_id)
        finally:
            self._in_flight = max(0, self._in_flight - 1)

    async def finalize(self) -> str:
        self._closed = True
        for _ in range(8):
            if self._in_flight <= 0:
                break
            await asyncio.sleep(0.03)

        audio = bytes(self.buffer)
        self.buffer.clear()
        self._bytes_since_partial = 0

        if not audio:
            # Interim text is a caption, not validated evidence.
            self.final_text = ""
            self.last_finalize_ms = float(self.last_partial_ms or 0)
            return self.final_text

        try:
            t0 = time.perf_counter()
            text = await self.stt.transcribe_mulaw(
                audio, language_code=self.language_code, partial=False
            )
            self.last_finalize_ms = (time.perf_counter() - t0) * 1000
            # Never promote an incomplete interim transcript to a claim answer.
            self.final_text = text or ""
            logger.info(
                "STT final call=%s provider=%s ms=%.0f bytes=%d chars=%d",
                self.call_id,
                self.stt.last_provider or "?",
                self.last_finalize_ms,
                len(audio),
                len(self.final_text),
            )
        except Exception as e:
            logger.exception(
                "STT final failed call=%s err=%s — interim caption not committed",
                self.call_id,
                e,
            )
            self.final_text = ""
            self.last_finalize_ms = float(self.last_partial_ms or 0)

        if self.on_partial and self.final_text:
            await self.on_partial(self.final_text, True)
        return self.final_text

    def reset_for_next_utterance(self) -> None:
        self.buffer.clear()
        self.partial_text = ""
        self.final_text = ""
        self._bytes_since_partial = 0
        self._closed = False
