"""Silero VAD — detect human speaking vs not speaking (~50ms).

Twilio audio is 8 kHz μ-law; Silero is most reliable at 16 kHz, so we upsample
8→16 before inference (window 512 @ 16 kHz ≈ 32 ms).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

TWILIO_RATE = 8000
SAMPLE_RATE = 16000  # Silero native
# 512 samples @ 16 kHz ≈ 32 ms
WINDOW_SAMPLES = 512

VadEvent = Literal["speech_start", "speech_end", ""]

_silero_available: bool | None = None


def silero_vad_available() -> bool:
    """Cached check — does not reload the model on every health poll."""
    global _silero_available
    if _silero_available is not None:
        return _silero_available
    try:
        from silero_vad import load_silero_vad  # noqa: F401

        _silero_available = True
    except Exception:
        _silero_available = False
    return _silero_available


@dataclass
class VadResult:
    speaking: bool
    event: VadEvent
    probability: float
    latency_ms: float


class SileroVadStream:
    """
    Streaming Silero VAD for Twilio μ-law 8 kHz packets.

    Feed every ~20ms packet; upsample to 16 kHz; evaluate ~32ms windows.
    """

    def __init__(
        self,
        *,
        threshold: float = 0.35,
        min_silence_duration_ms: int = 200,
        speech_pad_ms: int = 30,
    ) -> None:
        self.threshold = threshold
        self.min_silence_duration_ms = min_silence_duration_ms
        self.speech_pad_ms = speech_pad_ms
        self._model: Any = None
        self._iterator: Any = None
        self._pcm: list[float] = []
        self._speaking = False
        self._available = False
        self._last_prob = 0.0
        self._low_prob_windows = 0
        self._load()

    @property
    def available(self) -> bool:
        return self._available

    @property
    def speaking(self) -> bool:
        return self._speaking

    def _load(self) -> None:
        global _silero_available
        try:
            import torch
            from silero_vad import VADIterator, load_silero_vad

            torch.set_num_threads(1)
            try:
                self._model = load_silero_vad(onnx=True)
                backend = "onnx"
            except Exception:
                self._model = load_silero_vad(onnx=False)
                backend = "torch"
            self._iterator = VADIterator(
                self._model,
                threshold=self.threshold,
                sampling_rate=SAMPLE_RATE,
                min_silence_duration_ms=self.min_silence_duration_ms,
                speech_pad_ms=self.speech_pad_ms,
            )
            self._available = True
            _silero_available = True
            logger.info(
                "Silero VAD ready backend=%s rate=%d window=%dms threshold=%.2f",
                backend,
                SAMPLE_RATE,
                int(1000 * WINDOW_SAMPLES / SAMPLE_RATE),
                self.threshold,
            )
        except Exception as e:
            self._available = False
            _silero_available = False
            logger.error(
                "Silero VAD unavailable (%s). Install: pip install silero-vad onnxruntime torch",
                e,
            )

    def reset(self) -> None:
        self._pcm.clear()
        self._speaking = False
        self._last_prob = 0.0
        self._low_prob_windows = 0
        if self._iterator is not None:
            try:
                self._iterator.reset_states()
            except Exception:
                pass

    def push_mulaw(self, mulaw_packet: bytes) -> VadResult | None:
        if not mulaw_packet:
            return None
        if not self._available or self._iterator is None:
            return self._energy_fallback(mulaw_packet)

        self._pcm.extend(self._mulaw_8k_to_float_16k(mulaw_packet))
        result: VadResult | None = None
        while len(self._pcm) >= WINDOW_SAMPLES:
            chunk = self._pcm[:WINDOW_SAMPLES]
            del self._pcm[:WINDOW_SAMPLES]
            result = self._infer_window(chunk)
        return result

    def _infer_window(self, samples: list[float]) -> VadResult:
        import torch

        t0 = time.perf_counter()
        event: VadEvent = ""
        prob = self._last_prob

        try:
            tensor = torch.tensor(samples, dtype=torch.float32)
            with torch.no_grad():
                prob_t = self._model(tensor, SAMPLE_RATE)
            prob = float(prob_t.item() if hasattr(prob_t, "item") else prob_t)
            self._last_prob = prob

            speech_dict = self._iterator(tensor, return_seconds=False)
            if speech_dict and "start" in speech_dict:
                self._speaking = True
                self._low_prob_windows = 0
                event = "speech_start"
            elif speech_dict and "end" in speech_dict:
                self._speaking = False
                self._low_prob_windows = 0
                event = "speech_end"
            else:
                # Probability backup — telephony often under-triggers iterator start/end
                if not self._speaking and prob >= self.threshold:
                    self._speaking = True
                    self._low_prob_windows = 0
                    event = "speech_start"
                elif self._speaking and prob < max(0.15, self.threshold - 0.15):
                    self._low_prob_windows += 1
                    # ~3 windows @ 32ms ≈ 100ms soft hangover end
                    if self._low_prob_windows >= 3:
                        self._speaking = False
                        self._low_prob_windows = 0
                        event = "speech_end"
                else:
                    self._low_prob_windows = 0
        except Exception as e:
            logger.warning("Silero VAD infer failed: %s — using energy", e)
            return self._energy_from_floats(samples)

        latency_ms = (time.perf_counter() - t0) * 1000
        return VadResult(
            speaking=self._speaking,
            event=event,
            probability=prob,
            latency_ms=round(latency_ms, 2),
        )

    def _energy_from_floats(self, samples: list[float]) -> VadResult:
        energy = sum(abs(s) for s in samples) / max(1, len(samples))
        speaking = energy > 0.02
        event: VadEvent = ""
        if speaking and not self._speaking:
            event = "speech_start"
        elif not speaking and self._speaking:
            event = "speech_end"
        self._speaking = speaking
        return VadResult(
            speaking=speaking,
            event=event,
            probability=min(1.0, energy * 20),
            latency_ms=0.0,
        )

    def _energy_fallback(self, mulaw_packet: bytes) -> VadResult:
        """Fallback when Silero failed to load — use PCM RMS, not raw μ-law bytes."""
        try:
            import audioop
        except ImportError:
            import audioop_lts as audioop  # type: ignore
        try:
            pcm = audioop.ulaw2lin(mulaw_packet, 2)
            rms = float(audioop.rms(pcm, 2))
        except Exception:
            rms = 0.0
        speaking = rms > 350.0
        event: VadEvent = ""
        if speaking and not self._speaking:
            event = "speech_start"
        elif not speaking and self._speaking:
            event = "speech_end"
        self._speaking = speaking
        return VadResult(
            speaking=speaking,
            event=event,
            probability=min(1.0, rms / 2000.0),
            latency_ms=0.0,
        )

    @staticmethod
    def _mulaw_8k_to_float_16k(mulaw: bytes) -> list[float]:
        """Decode μ-law 8 kHz → float32, upsample 2× to 16 kHz for Silero."""
        try:
            import audioop
        except ImportError:
            import audioop_lts as audioop  # type: ignore

        pcm = audioop.ulaw2lin(mulaw, 2)
        samples_8k: list[float] = []
        for i in range(0, len(pcm), 2):
            sample = int.from_bytes(pcm[i : i + 2], "little", signed=True)
            samples_8k.append(sample / 32768.0)

        # Linear upsample 8 kHz → 16 kHz
        out: list[float] = []
        n = len(samples_8k)
        for i, s in enumerate(samples_8k):
            out.append(s)
            if i + 1 < n:
                out.append((s + samples_8k[i + 1]) * 0.5)
            else:
                out.append(s)
        return out
