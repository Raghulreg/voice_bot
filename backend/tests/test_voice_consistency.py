"""Filler removal, single-voice playback, model routing, and turn telemetry.

All providers are mocked; no network, telephony, or paid API request is made.
"""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from services.conversation_engine import (
    CallSession,
    ConversationEngine,
    _say_twiml,
)
from services.gemini_service import GeminiService
from services.model_routing import route_model
from services.turn_telemetry import TurnMetrics, TurnTimer, compute_latency_summary

BACKEND = Path(__file__).resolve().parents[1]
BASE = "https://example.ngrok-free.dev"
BANNED_FILLERS = (
    "one moment while i check that",
    "thank you for waiting. i am still checking that",
)


class FillerRemovalTests(unittest.TestCase):
    def test_banned_filler_is_absent_from_backend_source(self):
        offenders = []
        for path in BACKEND.rglob("*.py"):
            if ".venv" in path.parts or "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            if any(phrase in text for phrase in BANNED_FILLERS):
                offenders.append(str(path.relative_to(BACKEND)))
        self.assertEqual(offenders, [])

    def test_pending_hold_twiml_has_no_speech(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        session = CallSession(call_id="hold")
        twiml = engine._build_pending_continue_twiml(session, BASE)
        self.assertNotIn("<Say", twiml)
        self.assertNotIn("<Play", twiml)
        for phrase in BANNED_FILLERS:
            self.assertNotIn(phrase, twiml.lower())
        self.assertIn('<Redirect method="POST">', twiml)


class SayVoiceTests(unittest.TestCase):
    def test_default_settings_never_select_polly_matthew(self):
        from config import Settings

        self.assertEqual(Settings.model_fields["twilio_say_voice"].default, "")
        self.assertEqual(Settings.model_fields["tts_fallback_providers"].default, "")

    def test_say_is_empty_without_configured_voice(self):
        with patch(
            "services.conversation_engine.get_settings",
            return_value=SimpleNamespace(twilio_say_voice=""),
        ):
            self.assertEqual(_say_twiml("Hello."), "")

    def test_say_uses_configured_voice(self):
        with patch(
            "services.conversation_engine.get_settings",
            return_value=SimpleNamespace(twilio_say_voice="Polly.Joanna"),
        ):
            self.assertIn('voice="Polly.Joanna"', _say_twiml("Hello."))


class StoreAgentAudioVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_records_voice_and_telemetry(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        engine.tts = SimpleNamespace(
            provider_name=lambda: "deepgram_tts",
            primary_provider=lambda: "deepgram_tts",
            voice_identity=lambda provider=None: f"{provider or 'deepgram_tts'}:aura-2-thalia-en",
            text_to_speech=AsyncMock(return_value=b"\x01" * 900),
            mulaw_to_pcm_wav=lambda data: b"RIFF" + data,
        )
        session = CallSession(call_id="voice-store")
        engine.begin_customer_turn(session, 0.9, 10)
        turn = await engine._store_agent_audio(session, "Hello.", "en-US")
        metrics = session.active_turn_timer.metrics
        self.assertEqual(session.playback_voice[turn], "deepgram_tts:aura-2-thalia-en")
        self.assertEqual(metrics.tts_provider, "deepgram_tts")
        self.assertFalse(metrics.tts_fallback_used)
        self.assertIn("tts_start", metrics.stage_offsets_ms)
        self.assertIn("tts_end", metrics.stage_offsets_ms)


class ModelRoutingTests(unittest.IsolatedAsyncioTestCase):
    def test_unconfigured_small_model_is_reported_honestly(self):
        route = route_model("It is under review.", "claim_status", True, "primary", "")
        self.assertEqual(route.model, "primary")
        self.assertEqual(route.reason, "simple_model_not_configured")

    def test_sensitive_denial_stays_on_primary(self):
        route = route_model("Why was it denied?", "claim_status", True, "primary", "small")
        self.assertEqual(route.model, "primary")
        self.assertEqual(route.tier, "complex")

    async def _run(self, transcript, topic, valid, small):
        service = GeminiService.__new__(GeminiService)
        service.model_name = "primary"
        service.simple_model_name = small
        service._build_turn_prompt = Mock(return_value=("prompt", 1.0))
        service._generate = AsyncMock(
            return_value={"response_text": "Thanks.", "detected_emotion": "neutral"}
        )
        result = await service.generate_response(
            transcript, [], "Chris", "en-US", answer_topic=topic, answer_valid=valid
        )
        return service._generate.await_args.kwargs["model_name"], result["_timing"]

    async def test_simple_turn_executes_on_small_model_when_configured(self):
        model, timing = await self._run("It is under review.", "claim_status", True, "small")
        self.assertEqual(model, "small")
        self.assertEqual(timing["model_tier"], "simple")

    async def test_ambiguous_turn_executes_on_primary(self):
        model, timing = await self._run("Hmm, not sure, maybe.", "unknown", False, "small")
        self.assertEqual(model, "primary")
        self.assertEqual(timing["model_tier"], "complex")

    async def test_without_small_model_every_turn_executes_on_primary(self):
        model, timing = await self._run("It is under review.", "claim_status", True, "")
        self.assertEqual(model, "primary")
        self.assertEqual(timing["model_route_reason"], "simple_model_not_configured")

    def test_gemini_timing_marks_slm_use_in_turn_metrics(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        engine.gemini = SimpleNamespace(model_name="primary")
        session = CallSession(call_id="slm")
        session.active_turn_timer = TurnTimer(metrics=TurnMetrics())
        engine._apply_gemini_timing(
            session,
            {"_timing": {"llm_ms": 300, "model": "small", "model_tier": "simple",
                         "model_route_reason": "validated_single_topic_answer"}},
        )
        m = session.active_turn_timer.metrics
        self.assertTrue(m.slm_used)
        self.assertEqual(m.llm_model, "small")
        self.assertEqual(m.llm_ms, 300)


class TelemetryTests(unittest.TestCase):
    def test_metrics_expose_stage_provider_and_unmeasured_speech_end(self):
        timer = TurnTimer(metrics=TurnMetrics(turn_id=1))
        timer.mark_stage("llm_start")
        timer.mark_stage("llm_end")
        timer.metrics.tts_provider = "deepgram_tts"
        data = timer.finish()
        self.assertIn("llm_start", data["stage_offsets_ms"])
        self.assertEqual(data["tts_provider"], "deepgram_tts")
        self.assertFalse(data["speech_end_measured"])
        self.assertIsNone(data["eos_to_first_audio_ms"])
        self.assertIsNone(data["webhook_to_audio_fetch_ms"])

    def test_summary_counts_fetch_slm_and_fallbacks(self):
        transcript = [
            {"metrics": {"handler_ms": 900, "webhook_to_audio_fetch_ms": 1500,
                         "slm_used": True, "tts_fallback_used": False}},
            {"metrics": {"handler_ms": 1200, "webhook_to_audio_fetch_ms": 2500,
                         "tts_fallback_used": True, "gather_deferred": True}},
        ]
        summary = compute_latency_summary(transcript)
        self.assertEqual(summary["p50_webhook_to_audio_fetch_ms"], 2500)
        self.assertEqual(summary["slm_turns"], 1)
        self.assertEqual(summary["tts_fallback_turns"], 1)
        self.assertEqual(summary["gather_deferred_turns"], 1)
        self.assertIsNone(summary["latency_slo"]["met"])


if __name__ == "__main__":
    unittest.main()
