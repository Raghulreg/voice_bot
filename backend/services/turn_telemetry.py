"""Per-turn latency metrics for call logs (STT, LLM, TTS, Twilio, total).

Integrates with OpenTelemetry / CloudWatch / Sentry via services.observability.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TurnMetrics:
    turn_id: int = 0
    path: str = "unknown"
    call_id: str = ""
    webhook_received_at: float = 0.0
    gap_since_last_agent_ms: float | None = None
    previous_agent_audio_ms: float | None = None
    input_wait_ms: float | None = None
    twilio_stt_confidence: float | None = None
    transcript_chars: int = 0
    # Core latency breakdown (ms)
    stt_ms: float = 0.0
    endpointing_ms: float | None = None
    speech_end_to_stt_final_ms: float | None = None
    prompt_build_ms: float = 0.0
    first_llm_token_ms: float | None = None
    llm_ms: float = 0.0
    first_tts_chunk_ms: float | None = None
    tts_ms: float = 0.0
    twilio_delay_ms: float = 0.0
    first_audio_ms: float | None = None
    first_playback_ms: float | None = None
    eos_to_first_audio_ms: float | None = None
    orchestration_ms: float = 0.0
    total_response_ms: float = 0.0
    tts_cache_hit: bool = False
    gather_deferred: bool = False
    gather_initial_response_ms: float | None = None
    handler_ms: float = 0.0
    context_label: str = "exemplars_in_prompt"
    # Model path
    llm_model: str | None = None
    model_tier: str | None = None
    model_route_reason: str | None = None
    model_fallback_used: bool = False
    slm_used: bool = False
    llm_request_ms: float | None = None
    llm_prompt_tokens: int | None = None
    llm_output_tokens: int | None = None
    llm_thought_tokens: int | None = None
    llm_attempts: int = 0
    llm_error: str | None = None
    # Gather input-wait breakdown (ms), from partialResultCallback timestamps.
    audio_fetch_delay_ms: float | None = None
    caller_start_ms: float | None = None
    caller_speaking_ms: float | None = None
    speech_end_to_webhook_ms: float | None = None
    # Voice path
    tts_provider: str | None = None
    tts_voice: str | None = None
    tts_fallback_used: bool = False
    tts_failure_reason: str | None = None
    audio_turn: int | None = None
    # Offsets (ms) from webhook receipt: llm_start, llm_end, tts_start, tts_end
    stage_offsets_ms: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "path": self.path,
            "call_id": self.call_id,
            "gap_since_last_agent_ms": self.gap_since_last_agent_ms,
            "previous_agent_audio_ms": self.previous_agent_audio_ms,
            "input_wait_ms": self.input_wait_ms,
            "twilio_stt_confidence": self.twilio_stt_confidence,
            "transcript_chars": self.transcript_chars,
            "stt_ms": round(self.stt_ms, 1),
            "endpointing_ms": round(self.endpointing_ms, 1)
            if self.endpointing_ms is not None
            else None,
            "speech_end_to_stt_final_ms": round(self.speech_end_to_stt_final_ms, 1)
            if self.speech_end_to_stt_final_ms is not None
            else None,
            "prompt_build_ms": round(self.prompt_build_ms, 1),
            "first_llm_token_ms": round(self.first_llm_token_ms, 1)
            if self.first_llm_token_ms is not None
            else None,
            "llm_ms": round(self.llm_ms, 1),
            "first_tts_chunk_ms": round(self.first_tts_chunk_ms, 1)
            if self.first_tts_chunk_ms is not None
            else None,
            "tts_ms": round(self.tts_ms, 1),
            "twilio_delay_ms": round(self.twilio_delay_ms, 1),
            "first_audio_ms": round(self.first_audio_ms, 1)
            if self.first_audio_ms is not None
            else None,
            "first_playback_ms": round(self.first_playback_ms, 1)
            if self.first_playback_ms is not None
            else None,
            "eos_to_first_audio_ms": round(self.eos_to_first_audio_ms, 1)
            if self.eos_to_first_audio_ms is not None
            else None,
            "orchestration_ms": round(self.orchestration_ms, 1),
            "total_response_ms": round(self.total_response_ms or self.handler_ms, 1),
            "tts_cache_hit": self.tts_cache_hit,
            "gather_deferred": self.gather_deferred,
            "gather_initial_response_ms": (
                round(self.gather_initial_response_ms, 1)
                if self.gather_initial_response_ms is not None
                else None
            ),
            "handler_ms": round(self.handler_ms, 1),
            "context_ms": round(self.prompt_build_ms, 1),
            "context_label": self.context_label,
            "llm_model": self.llm_model,
            "model_tier": self.model_tier,
            "model_route_reason": self.model_route_reason,
            "model_fallback_used": self.model_fallback_used,
            "slm_used": self.slm_used,
            "llm_request_ms": _round(self.llm_request_ms),
            "llm_prompt_tokens": self.llm_prompt_tokens,
            "llm_output_tokens": self.llm_output_tokens,
            "llm_thought_tokens": self.llm_thought_tokens,
            "llm_attempts": self.llm_attempts,
            "llm_error": self.llm_error,
            "audio_fetch_delay_ms": _round(self.audio_fetch_delay_ms),
            "caller_start_ms": _round(self.caller_start_ms),
            "caller_speaking_ms": _round(self.caller_speaking_ms),
            "speech_end_to_webhook_ms": _round(self.speech_end_to_webhook_ms),
            "tts_provider": self.tts_provider,
            "tts_voice": self.tts_voice,
            "tts_fallback_used": self.tts_fallback_used,
            "tts_failure_reason": self.tts_failure_reason,
            "stage_offsets_ms": {k: round(v, 1) for k, v in self.stage_offsets_ms.items()},
            # Twilio Gather does not report when the caller stopped speaking.
            "speech_end_measured": self.speech_end_to_stt_final_ms is not None,
            # Filled when Twilio requests the reply audio (proxy for playback start).
            "webhook_to_audio_fetch_ms": None,
        }


def _round(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


@dataclass
class TurnTimer:
    """Accumulates phase timings for one dialogue turn."""

    metrics: TurnMetrics
    _started: float = field(default_factory=time.perf_counter)
    _llm_start: float | None = None
    _tts_start: float | None = None
    _prompt_start: float | None = None
    _stt_start: float | None = None
    _twilio_start: float | None = None

    def start_stt(self) -> None:
        self._stt_start = time.perf_counter()

    def end_stt(self) -> None:
        if self._stt_start is not None:
            self.metrics.stt_ms += (time.perf_counter() - self._stt_start) * 1000
            self._stt_start = None

    def add_stt_ms(self, ms: float) -> None:
        self.metrics.stt_ms += float(ms or 0)

    def start_prompt_build(self) -> None:
        self._prompt_start = time.perf_counter()

    def end_prompt_build(self) -> None:
        if self._prompt_start is not None:
            self.metrics.prompt_build_ms += (time.perf_counter() - self._prompt_start) * 1000
            self._prompt_start = None

    def start_llm(self) -> None:
        self._llm_start = time.perf_counter()

    def end_llm(self) -> None:
        if self._llm_start is not None:
            self.metrics.llm_ms += (time.perf_counter() - self._llm_start) * 1000
            self._llm_start = None

    def start_tts(self) -> None:
        self._tts_start = time.perf_counter()

    def end_tts(self) -> None:
        if self._tts_start is not None:
            self.metrics.tts_ms += (time.perf_counter() - self._tts_start) * 1000
            self._tts_start = None

    def add_tts_ms(self, ms: float) -> None:
        self.metrics.tts_ms += float(ms or 0)

    def start_twilio(self) -> None:
        self._twilio_start = time.perf_counter()

    def end_twilio(self) -> None:
        if self._twilio_start is not None:
            self.metrics.twilio_delay_ms += (time.perf_counter() - self._twilio_start) * 1000
            self._twilio_start = None

    def add_twilio_ms(self, ms: float) -> None:
        self.metrics.twilio_delay_ms += float(ms or 0)

    def mark_tts_cache_hit(self) -> None:
        self.metrics.tts_cache_hit = True

    def mark_stage(self, name: str, *, latest: bool = False) -> None:
        """Record a stage boundary; the first occurrence wins unless latest=True."""
        if latest or name not in self.metrics.stage_offsets_ms:
            self.metrics.stage_offsets_ms[name] = (time.perf_counter() - self._started) * 1000

    def set_first_audio_ms(self, ms: float | None) -> None:
        if ms is None:
            return
        if self.metrics.first_audio_ms is None:
            self.metrics.first_audio_ms = float(ms)

    def set_first_llm_token_ms(self, ms: float | None) -> None:
        if ms is not None and self.metrics.first_llm_token_ms is None:
            self.metrics.first_llm_token_ms = float(ms)

    def set_first_tts_chunk_ms(self, ms: float | None) -> None:
        if ms is not None and self.metrics.first_tts_chunk_ms is None:
            self.metrics.first_tts_chunk_ms = float(ms)

    def mark_playback_started(self) -> None:
        if self.metrics.first_playback_ms is not None:
            return
        elapsed_ms = (time.perf_counter() - self._started) * 1000
        self.metrics.first_playback_ms = elapsed_ms
        if self.metrics.first_audio_ms is None:
            self.metrics.first_audio_ms = elapsed_ms
        pre_handler = float(self.metrics.speech_end_to_stt_final_ms or 0)
        self.metrics.eos_to_first_audio_ms = pre_handler + elapsed_ms

    def finish(self) -> dict[str, Any]:
        self.end_prompt_build()
        self.end_llm()
        self.end_tts()
        self.end_stt()
        self.end_twilio()
        self.metrics.handler_ms = (time.perf_counter() - self._started) * 1000
        measured = (
            float(self.metrics.prompt_build_ms or 0)
            + float(self.metrics.llm_ms or 0)
            + float(self.metrics.tts_ms or 0)
            + float(self.metrics.twilio_delay_ms or 0)
        )
        self.metrics.orchestration_ms = max(0.0, self.metrics.handler_ms - measured)
        # Total = STT (pre-turn) + in-handler work, or wall handler if already inclusive
        if self.metrics.total_response_ms <= 0:
            self.metrics.total_response_ms = (
                float(self.metrics.stt_ms or 0) + float(self.metrics.handler_ms or 0)
            )
        finished = self.metrics.to_dict()
        try:
            from services.observability import record_turn_latency

            record_turn_latency(
                call_id=self.metrics.call_id or "",
                path=self.metrics.path,
                metrics=finished,
            )
        except Exception:
            pass
        return finished


def _latency_slo(
    *,
    p50_ms: float | None,
    p95_ms: float | None,
    sample_count: int,
) -> dict[str, Any]:
    """1200 ms applies to end-of-speech → first audio, never to payer speaking time."""
    try:
        from config import get_settings

        target = int(getattr(get_settings(), "voice_latency_target_ms", 1200) or 1200)
    except Exception:
        target = 1200
    return {
        "metric": "eos_to_first_audio_ms",
        "definition": (
            "Payer speech end to first audible AI response. "
            "Excludes payer speaking time and input wait (input_wait_ms)."
        ),
        "target_ms": target,
        "p50_ms": p50_ms,
        "p95_ms": p95_ms,
        "sample_count": sample_count,
        "excludes": ["input_wait_ms"],
        "components": [
            "speech_end_to_stt_final_ms",
            "first_llm_token_ms",
            "llm_ms",
            "first_tts_chunk_ms",
            "first_playback_ms",
            "twilio_delay_ms",
            "orchestration_ms",
        ],
        "met": None,
    }


def compute_latency_summary(transcript: list[dict[str, Any]]) -> dict[str, Any]:
    handlers: list[float] = []
    totals: list[float] = []
    stt_ms_vals: list[float] = []
    endpointing_vals: list[float] = []
    speech_to_final_vals: list[float] = []
    eos_first_audio_vals: list[float] = []
    first_llm_vals: list[float] = []
    first_tts_vals: list[float] = []
    first_playback_vals: list[float] = []
    llm_ms_vals: list[float] = []
    tts_ms_vals: list[float] = []
    twilio_ms_vals: list[float] = []
    orchestration_vals: list[float] = []
    prompt_ms_vals: list[float] = []
    llm_turns = 0
    tts_hits = 0
    tts_turns = 0
    gaps: list[float] = []
    input_waits: list[float] = []
    agent_audio: list[float] = []
    audio_fetch_vals: list[float] = []
    slm_turns = 0
    tts_fallback_turns = 0
    deferred_turns = 0
    breakdown: dict[str, list[float]] = {
        key: []
        for key in (
            "llm_request_ms", "audio_fetch_delay_ms", "caller_start_ms",
            "caller_speaking_ms", "speech_end_to_webhook_ms",
        )
    }

    for entry in transcript:
        m = entry.get("metrics")
        if not isinstance(m, dict):
            continue
        for key, values in breakdown.items():
            if m.get(key) is not None:
                values.append(float(m[key]))
        fetch = m.get("webhook_to_audio_fetch_ms")
        if fetch is not None:
            audio_fetch_vals.append(float(fetch))
        if m.get("slm_used"):
            slm_turns += 1
        if m.get("tts_fallback_used"):
            tts_fallback_turns += 1
        if m.get("gather_deferred"):
            deferred_turns += 1
        h = m.get("handler_ms")
        if h is not None:
            handlers.append(float(h))
        tot = m.get("total_response_ms")
        if tot is not None:
            totals.append(float(tot))
        stt_v = float(m.get("stt_ms") or 0)
        if stt_v > 0:
            stt_ms_vals.append(stt_v)
        endpointing_v = float(m.get("endpointing_ms") or 0)
        if endpointing_v > 0:
            endpointing_vals.append(endpointing_v)
        speech_final_v = float(m.get("speech_end_to_stt_final_ms") or 0)
        if speech_final_v > 0:
            speech_to_final_vals.append(speech_final_v)
        eos_audio = float(m.get("eos_to_first_audio_ms") or 0)
        if eos_audio > 0:
            eos_first_audio_vals.append(eos_audio)
        first_llm = float(m.get("first_llm_token_ms") or 0)
        if first_llm > 0:
            first_llm_vals.append(first_llm)
        first_tts = float(m.get("first_tts_chunk_ms") or 0)
        if first_tts > 0:
            first_tts_vals.append(first_tts)
        first_playback = float(m.get("first_playback_ms") or 0)
        if first_playback > 0:
            first_playback_vals.append(first_playback)
        llm_v = float(m.get("llm_ms") or 0)
        if llm_v > 0:
            llm_turns += 1
            llm_ms_vals.append(llm_v)
        tts_v = float(m.get("tts_ms") or 0)
        if tts_v > 0:
            tts_ms_vals.append(tts_v)
        tw = float(m.get("twilio_delay_ms") or 0)
        if tw > 0:
            twilio_ms_vals.append(tw)
        orchestration = float(m.get("orchestration_ms") or 0)
        if orchestration > 0:
            orchestration_vals.append(orchestration)
        pb = float(m.get("prompt_build_ms") or m.get("context_ms") or 0)
        if pb > 0:
            prompt_ms_vals.append(pb)
        if m.get("tts_cache_hit"):
            tts_hits += 1
        if m.get("tts_ms") is not None or m.get("tts_cache_hit"):
            tts_turns += 1
        g = m.get("gap_since_last_agent_ms")
        if g is not None:
            gaps.append(float(g))
        wait = m.get("input_wait_ms")
        if wait is not None:
            input_waits.append(float(wait))
        audio = m.get("previous_agent_audio_ms")
        if audio is not None:
            agent_audio.append(float(audio))

    def _p50(vals: list[float]) -> float | None:
        if not vals:
            return None
        s = sorted(vals)
        return s[len(s) // 2]

    def _p95(vals: list[float]) -> float | None:
        if not vals:
            return None
        return round(sorted(vals)[int(len(vals) * 0.95)], 1)

    return {
        "turns_with_metrics": len(handlers),
        "p50_handler_ms": round(_p50(handlers) or 0, 1),
        "p95_handler_ms": round(
            sorted(handlers)[int(len(handlers) * 0.95)] if handlers else 0, 1
        ),
        "p50_total_response_ms": round(_p50(totals) or 0, 1) if totals else None,
        "p95_total_response_ms": _p95(totals),
        "p50_stt_ms": round(_p50(stt_ms_vals) or 0, 1) if stt_ms_vals else None,
        "p50_endpointing_ms": round(_p50(endpointing_vals) or 0, 1)
        if endpointing_vals
        else None,
        "p95_endpointing_ms": _p95(endpointing_vals),
        "p50_speech_end_to_stt_final_ms": round(
            _p50(speech_to_final_vals) or 0, 1
        )
        if speech_to_final_vals
        else None,
        "p95_speech_end_to_stt_final_ms": _p95(speech_to_final_vals),
        "p50_eos_to_first_audio_ms": round(_p50(eos_first_audio_vals) or 0, 1)
        if eos_first_audio_vals
        else None,
        "p95_eos_to_first_audio_ms": round(
            sorted(eos_first_audio_vals)[int(len(eos_first_audio_vals) * 0.95)], 1
        )
        if eos_first_audio_vals
        else None,
        "p50_first_llm_token_ms": round(_p50(first_llm_vals) or 0, 1)
        if first_llm_vals
        else None,
        "p50_first_tts_chunk_ms": round(_p50(first_tts_vals) or 0, 1)
        if first_tts_vals
        else None,
        "p50_first_playback_ms": round(_p50(first_playback_vals) or 0, 1)
        if first_playback_vals
        else None,
        "p95_first_playback_ms": _p95(first_playback_vals),
        "p50_gap_since_agent_ms": round(_p50(gaps) or 0, 1) if gaps else None,
        "p50_input_wait_ms": round(_p50(input_waits) or 0, 1) if input_waits else None,
        "p50_agent_audio_ms": round(_p50(agent_audio) or 0, 1) if agent_audio else None,
        "p50_llm_ms": round(_p50(llm_ms_vals) or 0, 1) if llm_ms_vals else None,
        "p50_tts_ms": round(_p50(tts_ms_vals) or 0, 1) if tts_ms_vals else None,
        "p50_twilio_delay_ms": round(_p50(twilio_ms_vals) or 0, 1) if twilio_ms_vals else None,
        "p50_orchestration_ms": round(_p50(orchestration_vals) or 0, 1)
        if orchestration_vals
        else None,
        "p50_prompt_ms": round(_p50(prompt_ms_vals) or 0, 1) if prompt_ms_vals else None,
        "llm_turns": llm_turns,
        "tts_cache_hit_rate": round(tts_hits / tts_turns, 2) if tts_turns else None,
        "p95_stt_ms": _p95(stt_ms_vals),
        "p95_llm_ms": _p95(llm_ms_vals),
        "p95_tts_ms": _p95(tts_ms_vals),
        "p95_first_llm_token_ms": _p95(first_llm_vals),
        "p95_first_tts_chunk_ms": _p95(first_tts_vals),
        "p95_twilio_delay_ms": _p95(twilio_ms_vals),
        "p95_orchestration_ms": _p95(orchestration_vals),
        "p50_webhook_to_audio_fetch_ms": round(_p50(audio_fetch_vals) or 0, 1)
        if audio_fetch_vals
        else None,
        "p95_webhook_to_audio_fetch_ms": _p95(audio_fetch_vals),
        **{
            f"p50_{key}": round(_p50(values) or 0, 1) if values else None
            for key, values in breakdown.items()
        },
        "slm_turns": slm_turns,
        "tts_fallback_turns": tts_fallback_turns,
        "gather_deferred_turns": deferred_turns,
        "latency_slo": _latency_slo(
            p50_ms=round(_p50(eos_first_audio_vals) or 0, 1) if eos_first_audio_vals else None,
            p95_ms=round(
                sorted(eos_first_audio_vals)[int(len(eos_first_audio_vals) * 0.95)], 1
            )
            if eos_first_audio_vals
            else None,
            sample_count=len(eos_first_audio_vals),
        ),
    }


def profile_turn_stages(metrics: dict[str, Any]) -> dict[str, Any]:
    """Split one turn so reply-to-audio-fetch is not reported as model time."""
    handler = metrics.get("handler_ms")
    fetch = metrics.get("webhook_to_audio_fetch_ms")
    fetch_wait = None
    if fetch is not None and handler is not None:
        fetch_wait = round(max(0.0, float(fetch) - float(handler)), 1)
    return {
        "speech_end_to_stt_final_ms": metrics.get("speech_end_to_stt_final_ms"),
        "speech_end_measured": bool(metrics.get("speech_end_measured")),
        "stt_ms": metrics.get("stt_ms"),
        "input_wait_ms": metrics.get("input_wait_ms"),
        "llm_request_ms": metrics.get("llm_ms"),
        "tts_generation_ms": metrics.get("tts_ms"),
        "audio_ready_ms": round(float(handler), 1) if handler is not None else None,
        "twilio_fetch_wait_ms": fetch_wait,
        "reply_to_audio_fetch_ms": fetch,
    }


def attach_metrics(entry: dict[str, Any], metrics: dict[str, Any] | None) -> dict[str, Any]:
    if metrics:
        entry = dict(entry)
        entry["at"] = time.time()
        entry["metrics"] = metrics
    return entry
