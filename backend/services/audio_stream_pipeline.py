"""Live Twilio Media Streams audio pipeline + Silero VAD + silence timer + streaming STT.

Twilio sends ~20ms mulaw packets continuously. This pipeline:
  1. Accepts each packet immediately (never waits for end-of-speech to forward)
  2. Silero VAD (~32–50ms) detects human speaking vs not speaking
  3. Feeds chunks into Streaming STT → partial / updated transcripts
  4. On VAD speech_end → wait SILENCE_TIMER_MS (default 700ms)
     - If speech resumes → cancel timer (no interrupt)
     - If timer completes → final transcript → LLM / Echo Bot
  5. Barge-in: while bot speaks, VAD speech_start → stop TTS / cancel LLM → listen
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from config import get_settings
from services.silero_vad import SileroVadStream
from services.stt_service import SttService, StreamingSttSession

logger = logging.getLogger(__name__)

PACKET_BYTES = 160
MAX_QUEUE_PACKETS = 250
# Minimum speech audio before utterance finalize (~0.2s @ 8kHz μ-law)
MIN_UTTERANCE_BYTES = 1600
# Default hangover after VAD says human stopped
DEFAULT_SILENCE_TIMER_MS = 80
# Force finalize if speech never gets a clean VAD end (~8s of audio)
MAX_UTTERANCE_BYTES = 64000
# PCM RMS thresholds after μ-law decode (telephony is often soft)
PCM_SPEECH_RMS = 280
PCM_QUIET_RMS = 140
# ~20ms/packet → 8 packets ≈ 160ms quiet → answer quickly
QUIET_PACKETS_FOR_END = 8
# Prefer speculative STT result if buffer only grew this much during silence wait
_SPEC_MAX_EXTRA_BYTES = 1600  # ~200ms of tail audio


OnFinalTranscript = Callable[[str], Awaitable[None]]
OnPartialTranscript = Callable[[str, bool], Awaitable[None]]
OnBargeIn = Callable[[], Awaitable[None]]
IsBotSpeaking = Callable[[], bool]


class LiveAudioPipeline:
    """Immediate packet forwarder + Silero VAD + silence timer + streaming STT + barge-in."""

    def __init__(
        self,
        call_id: str,
        *,
        language_code: str = "en-US",
        on_final_transcript: OnFinalTranscript,
        on_partial_transcript: OnPartialTranscript | None = None,
        on_barge_in: OnBargeIn | None = None,
        is_bot_speaking: IsBotSpeaking | None = None,
        stt: SttService | None = None,
    ) -> None:
        self.call_id = call_id
        self.language_code = language_code
        self.on_final_transcript = on_final_transcript
        self.on_partial_transcript = on_partial_transcript
        self.on_barge_in = on_barge_in
        self.is_bot_speaking = is_bot_speaking
        self.stt = stt or SttService()
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=MAX_QUEUE_PACKETS)
        self._worker: asyncio.Task[None] | None = None
        self._closed = False
        self.packets_received = 0
        self.packets_forwarded = 0
        self.packets_dropped = 0

        settings = get_settings()
        from services.voice_runtime import media_stream_stt_choice

        try:
            self._stream_stt = media_stream_stt_choice(settings)
        except Exception:
            logger.warning("Media Streams STT choice failed. Using the existing STT path.")
            self._stream_stt = "existing"
        vad_provider = str(getattr(settings, "vad_provider", "silero") or "silero").strip().lower()
        self._vad_enabled = bool(getattr(settings, "vad_enabled", True)) and vad_provider == "silero"
        self._deepgram = None
        self._deepgram_started = False
        self._deepgram_utterance = False
        self._vad = SileroVadStream(
            threshold=float(getattr(settings, "silero_vad_threshold", 0.5) or 0.5),
            min_silence_duration_ms=int(
                getattr(settings, "silero_vad_min_silence_ms", 200) or 200
            ),
            speech_pad_ms=int(getattr(settings, "silero_vad_speech_pad_ms", 30) or 30),
        )
        self.silence_timer_ms = int(
            getattr(settings, "silence_timer_ms", DEFAULT_SILENCE_TIMER_MS)
            or DEFAULT_SILENCE_TIMER_MS
        )
        self._utterance_bytes = 0
        self._heard_speech = False
        self._busy = False
        self._pending_finalize = False
        self._barge_listening = False
        self._stream: StreamingSttSession | None = None
        self._vad_latencies: list[float] = []
        self._silence_task: asyncio.Task[None] | None = None
        self.barge_in_events = 0
        self.last_stt_ms: float = 0.0
        self.last_stt_confidence: float | None = None
        self.last_stt_start: float | None = None
        self.last_stt_duration: float | None = None
        self.last_endpointing_ms: float = 0.0
        self.last_speech_end_to_stt_final_ms: float = 0.0
        self.last_stt_error: str = ""
        self._speech_end_at: float = 0.0
        # Require sustained loud speech before cutting agent TTS (speaker echo)
        self._barge_confirm_packets = 0
        # Brief hangover after TTS so speaker echo does not start a false utterance
        self._echo_guard_until = 0.0
        self._was_bot_speaking = False
        # True only while dialogue callback runs (LLM/TTS) — does NOT block STT feed
        self._handling = False
        # Speculative STT started when silence timer arms (overlaps wait + API)
        self._spec_task: asyncio.Task[str] | None = None
        self._spec_audio_len: int = 0

    def _bot_active(self) -> bool:
        if self.is_bot_speaking is None:
            return False
        try:
            return bool(self.is_bot_speaking())
        except Exception:
            return False

    def _update_echo_guard(self, bot: bool) -> bool:
        """Mute STT while agent is speaking + short post-TTS hangover (~120ms)."""
        now = time.monotonic()
        if bot:
            self._was_bot_speaking = True
            return True
        if self._was_bot_speaking:
            self._was_bot_speaking = False
            self._echo_guard_until = now + 0.08
        return now < self._echo_guard_until

    @staticmethod
    def _pcm_rms(mulaw_packet: bytes) -> float:
        """True signal level from μ-law → PCM. Do not use abs(byte-128)."""
        if not mulaw_packet:
            return 0.0
        try:
            import audioop
        except ImportError:
            import audioop_lts as audioop  # type: ignore
        try:
            pcm = audioop.ulaw2lin(mulaw_packet, 2)
            return float(audioop.rms(pcm, 2))
        except Exception:
            return 0.0

    def start(self) -> None:
        if self._worker and not self._worker.done():
            return
        self._closed = False
        self._open_stream_session()
        self._worker = asyncio.create_task(self._run(), name=f"audio-pipeline-{self.call_id}")
        logger.info(
            "LiveAudioPipeline started call_id=%s silero_vad=%s silence_timer_ms=%d stt_model=%s stream_stt=%s barge_in=%s",
            self.call_id,
            self._vad_enabled and self._vad.available,
            self.silence_timer_ms,
            self.stt.model_name,
            self._stream_stt,
            self.on_barge_in is not None,
        )

    def _open_stream_session(self) -> None:
        self._cancel_silence_timer()
        self._cancel_speculative_stt()
        self._stream = self.stt.open_stream(
            call_id=self.call_id,
            language_code=self.language_code,
            on_partial=self._emit_partial,
        )
        self._utterance_bytes = 0
        self._heard_speech = False
        self._deepgram_utterance = False
        self._barge_listening = False
        self._vad.reset()

    def _cancel_silence_timer(self) -> None:
        """Cancel pending LLM/echo fire if human starts speaking again."""
        task = self._silence_task
        self._silence_task = None
        if task and not task.done():
            task.cancel()
            logger.debug(
                "Silence timer cancelled call=%s (speech resumed)",
                self.call_id,
            )
        self._cancel_speculative_stt()

    def _cancel_speculative_stt(self) -> None:
        task = self._spec_task
        self._spec_task = None
        self._spec_audio_len = 0
        if task and not task.done():
            task.cancel()

    def _start_speculative_stt(self) -> None:
        """Kick STT as soon as VAD ends so silence wait overlaps transcription."""
        if self._deepgram is not None and not self._deepgram.failed:
            return
        self._cancel_speculative_stt()
        if self._stream is None:
            return
        audio = self._stream.snapshot_audio()
        if len(audio) < MIN_UTTERANCE_BYTES:
            return
        self._spec_audio_len = len(audio)
        lang = self.language_code
        stt = self.stt

        async def _run() -> str:
            try:
                return await stt.transcribe_mulaw(audio, language_code=lang, partial=False)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "Speculative STT failed call=%s: %s",
                    self.call_id,
                    e,
                )
                return ""

        self._spec_task = asyncio.create_task(
            _run(),
            name=f"stt-spec-{self.call_id}",
        )
        logger.debug(
            "Speculative STT started call=%s bytes=%d",
            self.call_id,
            len(audio),
        )

    def _arm_silence_timer(self) -> None:
        """
        VAD said human stopped — wait silence_timer_ms before calling LLM.

        Example: "Hello...." (wait) "I need...." (wait) "Help" → then LLM.
        """
        self._cancel_silence_timer()
        if self._speech_end_at <= 0:
            self._speech_end_at = time.perf_counter()
        if self._utterance_bytes < MIN_UTTERANCE_BYTES:
            logger.debug(
                "Silence timer skipped call=%s (utterance too short bytes=%d)",
                self.call_id,
                self._utterance_bytes,
            )
            return
        # Overlap STT API with the short silence hangover
        self._start_speculative_stt()
        delay = max(0, self.silence_timer_ms) / 1000.0
        self._silence_task = asyncio.create_task(
            self._silence_timer_fire(delay),
            name=f"silence-timer-{self.call_id}",
        )
        logger.info(
            "Silence timer armed call=%s wait_ms=%d (VAD speech_end — not calling LLM yet)",
            self.call_id,
            self.silence_timer_ms,
        )

    async def _silence_timer_fire(self, delay_s: float) -> None:
        try:
            await asyncio.sleep(delay_s)
        except asyncio.CancelledError:
            return
        if self._closed or not self._heard_speech:
            return
        # Only block concurrent finalize, not "agent is still talking" —
        # user may have spoken during TTS (barge) and we need that audio.
        if self._busy:
            self._pending_finalize = True
            logger.info(
                "Silence timer deferred call=%s (finalize in progress)",
                self.call_id,
            )
            return
        if self._handling and not self._barge_listening:
            # Prior turn still in LLM/TTS — keep audio in buffer, wait for room
            self._pending_finalize = True
            logger.info(
                "Silence timer deferred call=%s (prior turn still handling)",
                self.call_id,
            )
            return
        logger.info(
            "Silence timer fired call=%s after %dms — finalizing utterance for LLM/Echo",
            self.call_id,
            self.silence_timer_ms,
        )
        self._silence_task = None
        await self._finalize_utterance()

    async def _emit_partial(self, text: str, is_final: bool) -> None:
        if self.on_partial_transcript:
            await self.on_partial_transcript(text, is_final)

    async def _trigger_barge_in(self) -> None:
        if not self.on_barge_in:
            return
        self.barge_in_events += 1
        self._barge_listening = True
        self._pending_finalize = False
        # Fresh STT buffer for the interrupting utterance
        self._stream = self.stt.open_stream(
            call_id=self.call_id,
            language_code=self.language_code,
            on_partial=self._emit_partial,
        )
        self._utterance_bytes = 0
        self._heard_speech = True
        self._cancel_silence_timer()
        try:
            await self.on_barge_in()
        except Exception:
            logger.exception("Barge-in handler failed call=%s", self.call_id)

    async def stop(self) -> None:
        self._closed = True
        self._cancel_silence_timer()
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            try:
                _ = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        if self._worker:
            try:
                await asyncio.wait_for(self._worker, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._worker.cancel()
            self._worker = None
        if self._deepgram is not None:
            try:
                await self._deepgram.close()
            except Exception:
                logger.warning("Deepgram close failed call_id=%s", self.call_id)
            self._deepgram = None
        avg_vad = (
            round(sum(self._vad_latencies) / len(self._vad_latencies), 2)
            if self._vad_latencies
            else None
        )
        logger.info(
            "LiveAudioPipeline stopped call_id=%s recv=%d fwd=%d drop=%d barge_ins=%d avg_vad_ms=%s",
            self.call_id,
            self.packets_received,
            self.packets_forwarded,
            self.packets_dropped,
            self.barge_in_events,
            avg_vad,
        )

    def forward_packet_b64(self, payload_b64: str) -> None:
        if self._closed or not payload_b64:
            return
        try:
            packet = base64.b64decode(payload_b64)
        except Exception:
            return
        self.forward_packet(packet)

    def forward_packet(self, packet: bytes) -> None:
        """Forward one ~20ms μ-law packet immediately (no end-of-speech wait)."""
        if self._closed or not packet:
            return
        self.packets_received += 1
        try:
            self.queue.put_nowait(packet)
            self.packets_forwarded += 1
        except asyncio.QueueFull:
            try:
                _ = self.queue.get_nowait()
                self.packets_dropped += 1
            except asyncio.QueueEmpty:
                pass
            try:
                self.queue.put_nowait(packet)
                self.packets_forwarded += 1
            except asyncio.QueueFull:
                self.packets_dropped += 1

    async def _start_deepgram(self) -> None:
        if self._stream_stt != "deepgram" or self._deepgram_started:
            return
        self._deepgram_started = True
        try:
            from services.deepgram_stt import DeepgramLiveSession

            session = DeepgramLiveSession(
                get_settings(),
                on_transcript=self._on_deepgram_transcript,
            )
            await session.connect()
            self._deepgram = session
            logger.info("Deepgram STT connected call_id=%s model=%s", self.call_id, session.model)
        except Exception as exc:
            self._deepgram = None
            logger.warning(
                "Deepgram STT unavailable call_id=%s cause=%s. Existing STT remains in use.",
                self.call_id,
                type(exc).__name__,
            )

    async def _on_deepgram_transcript(self, result: Any) -> None:
        if not getattr(result, "transcript", ""):
            return
        if bool(getattr(result, "is_final", False)):
            confidence = getattr(result, "confidence", None)
            self.last_stt_confidence = (
                float(confidence) if confidence is not None else None
            )
            start = getattr(result, "start", None)
            duration = getattr(result, "duration", None)
            self.last_stt_start = float(start) if start is not None else None
            self.last_stt_duration = (
                float(duration) if duration is not None else None
            )
        await self._emit_partial(result.transcript, bool(result.is_final))

    async def _run(self) -> None:
        await self._start_deepgram()
        # Low-RMS hangover for speech_end when Silero is slow on telephony
        quiet_packets = 0
        # Barge-in: need sustained strong speech (not soft speaker echo)
        BARGE_MIN_PROB = 0.55
        BARGE_MIN_RMS = 900
        BARGE_CONFIRM = 6  # ~120ms of strong speech before the caller cuts the agent off
        # Ignore the first moments of TTS so speaker echo does not count as an interruption
        BARGE_GRACE_S = 0.45
        agent_speech_started_at = 0.0
        while True:
            packet = await self.queue.get()
            if packet is None:
                self._cancel_silence_timer()
                if self._heard_speech and not self._busy:
                    await self._finalize_utterance()
                break

            bot = self._bot_active()
            if bot and agent_speech_started_at <= 0:
                agent_speech_started_at = time.monotonic()
            if not bot:
                agent_speech_started_at = 0.0
            echo_guard = self._update_echo_guard(bot)
            rms = self._pcm_rms(packet)
            # During STT finalize only — never for whole LLM/TTS (that blocked listening)
            finalize_busy = self._busy

            # Silero on the Media Streams path when VAD_ENABLED and VAD_PROVIDER=silero.
            vad = self._vad.push_mulaw(packet) if self._vad_enabled else None
            if vad is not None:
                self._vad_latencies.append(vad.latency_ms)
                if len(self._vad_latencies) > 200:
                    self._vad_latencies = self._vad_latencies[-100:]

                if vad.event == "speech_start" or (
                    bot and vad.speaking and not self._barge_listening
                ):
                    # Barge only when agent is actually playing TTS (not LLM wait)
                    in_grace = (
                        bot
                        and agent_speech_started_at > 0
                        and (time.monotonic() - agent_speech_started_at) < BARGE_GRACE_S
                    )
                    if bot and not self._barge_listening and not in_grace:
                        strong = (
                            float(vad.probability) >= BARGE_MIN_PROB and rms >= BARGE_MIN_RMS
                        )
                        if strong:
                            self._barge_confirm_packets += 1
                        else:
                            self._barge_confirm_packets = max(
                                0, self._barge_confirm_packets - 2
                            )
                        if self._barge_confirm_packets >= BARGE_CONFIRM:
                            self._cancel_silence_timer()
                            quiet_packets = 0
                            self._barge_confirm_packets = 0
                            self._echo_guard_until = 0.0
                            self._was_bot_speaking = False
                            logger.info(
                                "Silero VAD barge-in confirmed call=%s prob=%.2f rms=%.0f",
                                self.call_id,
                                vad.probability,
                                rms,
                            )
                            await self._trigger_barge_in()
                    elif bot and in_grace:
                        self._barge_confirm_packets = 0
                    elif vad.event == "speech_start" and not echo_guard and not finalize_busy:
                        self._cancel_silence_timer()
                        self._speech_end_at = 0.0
                        quiet_packets = 0
                        self._barge_confirm_packets = 0
                        self._heard_speech = True
                        logger.info(
                            "Silero VAD speech_start call=%s prob=%.2f rms=%.0f",
                            self.call_id,
                            vad.probability,
                            rms,
                        )
                elif vad.event == "speech_end" and self._heard_speech:
                    self._barge_confirm_packets = 0
                    if not (bot and not self._barge_listening):
                        logger.info(
                            "Silero VAD speech_end call=%s prob=%.2f bytes=%d rms=%.0f",
                            self.call_id,
                            vad.probability,
                            self._utterance_bytes,
                            rms,
                        )
                        self._arm_silence_timer()
                elif not bot:
                    self._barge_confirm_packets = 0

            # Listen whenever agent is not speaking (including during LLM think / handling)
            listening = (
                (not bot and not finalize_busy) or self._barge_listening
            ) and (not echo_guard or self._barge_listening)

            # PCM-RMS backup: start even if Silero start event is late
            if listening and rms >= PCM_SPEECH_RMS and not self._heard_speech:
                self._heard_speech = True
                self._cancel_silence_timer()
                self._speech_end_at = 0.0
                quiet_packets = 0
                logger.info(
                    "Energy speech_start call=%s rms=%.0f (Silero backup)",
                    self.call_id,
                    rms,
                )

            # PCM-RMS backup: end after sustained quiet
            if self._heard_speech and listening:
                if rms < PCM_QUIET_RMS:
                    quiet_packets += 1
                else:
                    quiet_packets = 0
                if quiet_packets >= QUIET_PACKETS_FOR_END and (
                    self._silence_task is None or self._silence_task.done()
                ):
                    logger.info(
                        "Energy speech_end call=%s after quiet packets=%d rms=%.0f",
                        self.call_id,
                        quiet_packets,
                        rms,
                    )
                    quiet_packets = 0
                    self._arm_silence_timer()

            # Mute STT only while agent is speaking (or brief hangover) — not during LLM
            if (bot or echo_guard) and not self._barge_listening:
                continue

            # Brief lock only during audio finalize itself
            if finalize_busy and not self._barge_listening:
                continue

            self._utterance_bytes += len(packet)

            # Feed STT once we hear speech (or finishing silence after speech)
            pending_silence = self._silence_task is not None and not self._silence_task.done()
            speaking_now = bool(vad and vad.speaking) or rms >= PCM_SPEECH_RMS
            if self._stream is not None and (
                self._heard_speech or speaking_now or pending_silence
            ):
                # Preserve the full buffer as a fallback, but do not run a second
                # partial recognizer while Deepgram is supplying live captions.
                self._stream.feed(
                    packet,
                    emit_partials=self._deepgram is None,
                )
            if self._deepgram is not None and (
                self._heard_speech or speaking_now or pending_silence
            ):
                if not self._deepgram_utterance:
                    self._deepgram.begin_utterance()
                    self._deepgram_utterance = True
                    self.last_stt_confidence = None
                    self.last_stt_start = None
                    self.last_stt_duration = None
                self._deepgram.feed(packet)

            # Safety: never sit forever if VAD end never fires
            if (
                self._heard_speech
                and self._utterance_bytes >= MAX_UTTERANCE_BYTES
                and not self._busy
                and (self._silence_task is None or self._silence_task.done())
            ):
                logger.info(
                    "Max utterance length call=%s bytes=%d — forcing finalize",
                    self.call_id,
                    self._utterance_bytes,
                )
                await self._finalize_utterance()

    async def _finalize_utterance(self) -> None:
        if self._busy:
            self._pending_finalize = True
            return
        if self._handling and not self._barge_listening:
            # Do not pile concurrent dialogue turns; keep collected audio for later
            self._pending_finalize = True
            return
        # Cancel silence timer only — keep speculative STT until _resolve_final_text
        sil = self._silence_task
        self._silence_task = None
        if sil and not sil.done():
            sil.cancel()
        self._busy = True
        self._barge_listening = False
        self._pending_finalize = False
        t0 = time.perf_counter()
        final_text = ""
        utterance_bytes = self._utterance_bytes
        try:
            final_text = await self._resolve_final_text()
            finished_at = time.perf_counter()
            if self._speech_end_at > 0:
                self.last_endpointing_ms = max(
                    0.0, (t0 - self._speech_end_at) * 1000
                )
                self.last_speech_end_to_stt_final_ms = max(
                    0.0, (finished_at - self._speech_end_at) * 1000
                )
            else:
                self.last_endpointing_ms = 0.0
                self.last_speech_end_to_stt_final_ms = self.last_stt_ms
            self._speech_end_at = 0.0
            # CRITICAL: reopen STT + clear finalize lock BEFORE dialogue (LLM/TTS).
            self._open_stream_session()
            self._busy = False
            self._handling = True
            try:
                await self.on_final_transcript(final_text or "")
            finally:
                self._handling = False
        except Exception:
            logger.exception("LiveAudioPipeline final transcript failed call_id=%s", self.call_id)
            self._busy = False
            self._handling = False
            try:
                self._open_stream_session()
            except Exception:
                pass
        finally:
            logger.info(
                "LiveAudioPipeline utterance done call_id=%s ms=%.0f bytes=%d chars=%d",
                self.call_id,
                (time.perf_counter() - t0) * 1000,
                utterance_bytes,
                len(final_text or ""),
            )
            pending = self._pending_finalize
            enough = self._utterance_bytes >= MIN_UTTERANCE_BYTES and self._heard_speech
            if pending and enough and not self._busy and not self._handling:
                await self._finalize_utterance()

    async def _deepgram_final_text(self) -> str:
        session = self._deepgram
        if session is None or session.failed:
            return ""
        try:
            return (await session.flush_final()).strip()
        except Exception:
            logger.warning("Deepgram final transcript failed call_id=%s", self.call_id)
            return ""

    async def _resolve_final_text(self) -> str:
        """Use speculative STT when ready; otherwise full finalize."""
        stream = self._stream
        spec = self._spec_task
        spec_len = self._spec_audio_len
        self._spec_task = None
        self._spec_audio_len = 0
        t0 = time.perf_counter()

        deepgram_text = await self._deepgram_final_text()
        deepgram = self._deepgram
        self.last_stt_error = str(getattr(deepgram, "safe_error", "") or "") if deepgram is not None else ""
        if deepgram_text:
            self.last_stt_error = ""
            if spec and not spec.done():
                spec.cancel()
            self.last_stt_ms = (time.perf_counter() - t0) * 1000
            if stream is not None:
                stream.mark_finalized_externally(deepgram_text, latency_ms=self.last_stt_ms)
            return deepgram_text

        current_len = len(stream.buffer) if stream is not None else 0
        extra = max(0, current_len - spec_len) if spec_len else current_len

        if (
            spec is not None
            and not spec.cancelled()
            and spec_len > 0
            and extra <= _SPEC_MAX_EXTRA_BYTES
        ):
            try:
                if not spec.done():
                    text = await asyncio.wait_for(asyncio.shield(spec), timeout=8.0)
                else:
                    text = spec.result()
                if text and str(text).strip():
                    ms = (time.perf_counter() - t0) * 1000
                    self.last_stt_ms = ms
                    if stream is not None:
                        stream.mark_finalized_externally(str(text), latency_ms=ms)
                    logger.info(
                        "STT speculative hit call=%s ms=%.0f chars=%d",
                        self.call_id,
                        ms,
                        len(str(text)),
                    )
                    return str(text)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception) as e:
                logger.warning(
                    "Speculative STT unused call=%s (%s) — full finalize",
                    self.call_id,
                    e,
                )
                if spec and not spec.done():
                    spec.cancel()
        else:
            if spec and not spec.done():
                spec.cancel()

        if stream is not None:
            text = await stream.finalize()
            self.last_stt_ms = float(getattr(stream, "last_finalize_ms", 0) or 0)
            return text or ""
        return ""
def attach_pipeline(
    session: Any,
    *,
    on_final: OnFinalTranscript,
    on_partial: OnPartialTranscript | None = None,
    on_barge_in: OnBargeIn | None = None,
    is_bot_speaking: IsBotSpeaking | None = None,
    stt: SttService | None = None,
) -> LiveAudioPipeline:
    """Create + start a LiveAudioPipeline with Silero VAD + silence timer + streaming STT."""
    existing = getattr(session, "audio_pipeline", None)
    if isinstance(existing, LiveAudioPipeline):
        # Refresh callbacks if re-attached from handle_media_chunk after webhook start
        if on_barge_in is not None:
            existing.on_barge_in = on_barge_in
        if is_bot_speaking is not None:
            existing.is_bot_speaking = is_bot_speaking
        return existing
    lang = getattr(session, "preferred_language", None) or "en-US"
    pipeline = LiveAudioPipeline(
        call_id=session.call_id,
        language_code=lang,
        on_final_transcript=on_final,
        on_partial_transcript=on_partial,
        on_barge_in=on_barge_in,
        is_bot_speaking=is_bot_speaking,
        stt=stt,
    )
    session.audio_pipeline = pipeline
    pipeline.start()
    return pipeline
