"""Deepgram Nova streaming STT for the Media Streams path.

Twilio Gather does not call this. A missing key or a failed socket leaves the
existing STT path in place.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

from services.voice_runtime import VoiceConfigError

logger = logging.getLogger(__name__)

DEEPGRAM_LISTEN_URL = "wss://api.deepgram.com/v1/listen"
OnTranscript = Callable[["DeepgramTranscript"], Awaitable[None]]


@dataclass
class DeepgramTranscript:
    transcript: str
    is_final: bool
    speech_final: bool
    confidence: float | None = None
    start: float | None = None
    duration: float | None = None
    words: list[dict[str, Any]] = field(default_factory=list)

    @property
    def end(self) -> float | None:
        if self.start is None or self.duration is None:
            return None
        return self.start + self.duration


def build_listen_url(model: str) -> str:
    """Nova listens to 8 kHz μ-law. Deepgram endpointing stays off so Silero owns pauses."""
    query = urlencode(
        {
            "model": (model or "nova-3").strip() or "nova-3",
            "encoding": "mulaw",
            "sample_rate": "8000",
            "channels": "1",
            "interim_results": "true",
            "punctuate": "true",
            "smart_format": "true",
            "endpointing": "false",
            "vad_events": "true",
        }
    )
    return f"{DEEPGRAM_LISTEN_URL}?{query}"


def build_browser_listen_url(model: str) -> str:
    """16 kHz linear PCM from the browser microphone. Phone calls stay on μ-law."""
    query = urlencode(
        {
            "model": (model or "nova-3").strip() or "nova-3",
            "encoding": "linear16",
            "sample_rate": "16000",
            "channels": "1",
            "interim_results": "true",
            "punctuate": "true",
            "smart_format": "true",
            "numerals": "true",
            "endpointing": "false",
            "vad_events": "true",
        }
    )
    return f"{DEEPGRAM_LISTEN_URL}?{query}"


def parse_deepgram_message(payload: dict[str, Any]) -> DeepgramTranscript | None:
    if payload.get("type") not in {None, "Results"}:
        return None
    channel = payload.get("channel") or {}
    alternatives = channel.get("alternatives") or []
    if not alternatives:
        return None
    best = alternatives[0] or {}
    text = str(best.get("transcript") or "").strip()
    words = best.get("words") if isinstance(best.get("words"), list) else []
    confidence = best.get("confidence")
    start = payload.get("start")
    duration = payload.get("duration")
    return DeepgramTranscript(
        transcript=text,
        is_final=bool(payload.get("is_final")),
        speech_final=bool(payload.get("speech_final")),
        confidence=float(confidence) if confidence is not None else None,
        start=float(start) if start is not None else None,
        duration=float(duration) if duration is not None else None,
        words=words,
    )


class DeepgramLiveSession:
    """One streaming recognition socket. Audio is queued so the caller never blocks on the network."""

    def __init__(self, settings: Any, on_transcript: OnTranscript | None = None) -> None:
        self.settings = settings
        self.on_transcript = on_transcript
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._sender: asyncio.Task[None] | None = None
        self._out: asyncio.Queue[bytes | str | None] = asyncio.Queue(maxsize=250)
        self._latest_final = ""
        self._final_parts: list[str] = []
        self._latest: DeepgramTranscript | None = None
        self._flush_event = asyncio.Event()
        self._api_key = ""
        self.failed = False
        self.utterance_degraded = False
        self.model = ""
        self.server_message_types: list[str] = []
        self.safe_error = ""

    def require_config(self) -> tuple[str, str]:
        key = str(getattr(self.settings, "deepgram_api_key", "") or "").strip()
        model = str(getattr(self.settings, "deepgram_model", "") or "").strip()
        missing = []
        if not key:
            missing.append("DEEPGRAM_API_KEY")
        if not model:
            missing.append("DEEPGRAM_MODEL")
        if missing:
            raise VoiceConfigError(
                "Deepgram STT is not active. Set "
                + ", ".join(missing)
                + " in backend/.env. Twilio Gather remains the phone speech path."
            )
        self.model = model
        self._api_key = key
        return key, model

    def begin_utterance(self) -> None:
        """Drop any transcript from the previous payer turn."""
        self._final_parts.clear()
        self._latest_final = ""
        self._latest = None
        self._flush_event = asyncio.Event()
        self.utterance_degraded = False

    def _remember_final(self, text: str) -> None:
        """Keep one utterance. A longer revision replaces the previous piece."""
        text = " ".join((text or "").split())
        if not text:
            return
        if self._final_parts:
            prev = self._final_parts[-1]
            if text == prev or prev.endswith(text):
                return
            if text.startswith(prev):
                self._final_parts[-1] = text
                self._latest_final = " ".join(self._final_parts)
                return
        self._final_parts.append(text)
        self._latest_final = " ".join(self._final_parts)

    async def connect(self, timeout: float = 8, url: str | None = None) -> None:
        key, model = self.require_config()
        url = url or build_listen_url(model)
        try:
            self._ws = await asyncio.wait_for(
                _open_socket(url, {"Authorization": f"Token {key}"}),
                timeout=timeout,
            )
        except VoiceConfigError:
            self.failed = True
            raise
        except asyncio.TimeoutError as exc:
            self.failed = True
            self.safe_error = "timeout"
            raise VoiceConfigError(
                "Deepgram connection timed out. Twilio Gather remains the phone speech path."
            ) from exc
        except Exception as exc:
            self.failed = True
            self.safe_error = type(exc).__name__
            raise VoiceConfigError(
                "Deepgram connection failed. Twilio Gather remains the phone speech path. "
                f"Cause: {type(exc).__name__}."
            ) from exc
        self._reader = asyncio.create_task(self._read(), name="deepgram-read")
        self._sender = asyncio.create_task(self._send(), name="deepgram-send")

    def feed(self, mulaw: bytes) -> None:
        if not mulaw:
            return
        self._put(mulaw)

    async def flush_final(self, timeout: float = 1.5) -> str:
        """Ask Nova to finalize this utterance. Empty means the existing STT path should run."""
        text = await self._flush_once(timeout)
        if not text and self.safe_error == "finalize_timeout" and not self.failed:
            logger.warning("Deepgram finalize timed out. Retrying once.")
            self.safe_error = ""
            text = await self._flush_once(timeout)
            if not text:
                self.safe_error = self.safe_error or "finalize_timeout"
        if self.failed or self.utterance_degraded:
            self.begin_utterance()
            return ""
        self.begin_utterance()
        return text

    async def _flush_once(self, timeout: float) -> str:
        if self.failed or self._ws is None:
            return ""
        self._flush_event = asyncio.Event()
        self._put(json.dumps({"type": "Finalize"}))
        try:
            await asyncio.wait_for(self._flush_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.safe_error = self.safe_error or "finalize_timeout"
            logger.warning("Deepgram finalize timed out. Existing STT remains available.")
            return ""
        return self._latest_final.strip()

    async def current_final(self) -> str:
        return await self.flush_final()

    async def take_transcript(self, timeout: float = 0.7) -> str:
        """Finalize audio already streamed from the browser. Keep any text if Nova is slow."""
        if self.failed or self._ws is None:
            text = self._latest_final.strip()
            self.begin_utterance()
            return " ".join(text.split())
        text = await self._flush_once(timeout)
        if not text:
            text = self._latest_final.strip()
            if not text and self._latest is not None:
                text = (self._latest.transcript or "").strip()
        self.begin_utterance()
        return " ".join(text.split())

    async def close(self) -> None:
        self.failed = True
        try:
            self._out.put_nowait(None)
        except asyncio.QueueFull:
            pass
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
            except Exception:
                pass
            try:
                await self._ws.close()
            except Exception:
                pass
        for task in (self._sender, self._reader):
            if task and not task.done():
                task.cancel()
        self._ws = None

    def _put(self, item: bytes | str | None) -> None:
        if self.failed or self._ws is None:
            return
        try:
            self._out.put_nowait(item)
        except asyncio.QueueFull:
            # Keep the socket for later turns, but reject this incomplete utterance.
            # The full-buffer STT fallback still has all caller audio.
            self.utterance_degraded = True
            self.safe_error = self.safe_error or "audio_queue_overflow"
            logger.warning(
                "Deepgram audio queue overflow. Existing STT will finalize this utterance."
            )

    async def _send(self) -> None:
        while not self.failed:
            try:
                chunk = await asyncio.wait_for(self._out.get(), timeout=3)
            except asyncio.TimeoutError:
                if self._ws is None:
                    return
                try:
                    await self._ws.send(json.dumps({"type": "KeepAlive"}))
                except Exception:
                    self.failed = True
                    self.safe_error = self.safe_error or "keepalive_failed"
                    logger.warning("Deepgram keepalive failed. Existing STT remains available.")
                    return
                continue
            if chunk is None or self._ws is None:
                return
            try:
                await self._ws.send(chunk)
            except Exception:
                self.failed = True
                self.safe_error = self.safe_error or "send_failed"
                logger.warning("Deepgram audio send failed. Existing STT remains available.")
                return

    async def _read(self) -> None:
        if self._ws is None:
            return
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                message_type = str(payload.get("type") or "")
                if message_type and message_type not in self.server_message_types:
                    self.server_message_types.append(message_type)
                if message_type == "Error":
                    self.failed = True
                    self.safe_error = _public_error(payload, self._api_key)
                    self._flush_event.set()
                    logger.warning(
                        "Deepgram rejected the stream (%s). Existing STT remains available.",
                        self.safe_error or "error",
                    )
                    return
                parsed = parse_deepgram_message(payload)
                if parsed is None:
                    continue
                self._latest = parsed
                if parsed.is_final and parsed.transcript:
                    self._remember_final(parsed.transcript)
                if payload.get("from_finalize") or parsed.speech_final:
                    self._flush_event.set()
                if self.on_transcript is not None and parsed.transcript:
                    await self.on_transcript(parsed)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failed = True
            self.safe_error = self.safe_error or "reader_stopped"
            self._flush_event.set()
            logger.warning("Deepgram reader stopped. Existing STT remains available.")


def _public_error(payload: dict[str, Any], api_key: str) -> str:
    text = str(payload.get("err_code") or payload.get("description") or payload.get("message") or "error")
    text = " ".join(text.split())
    if api_key:
        text = text.replace(api_key, "[redacted]")
    return text[:180]


async def _open_socket(url: str, headers: dict[str, str]) -> Any:
    try:
        import websockets
    except ImportError as exc:
        raise VoiceConfigError(
            "Deepgram STT is not active. The websockets package is missing. "
            "Twilio Gather remains the phone speech path."
        ) from exc
    try:
        return await websockets.connect(
            url,
            additional_headers=headers,
            open_timeout=8,
        )
    except TypeError:
        return await websockets.connect(url, extra_headers=headers, open_timeout=8)
