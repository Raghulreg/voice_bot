"""SLM configuration, routing, redundant-call and telemetry checks. No network calls."""

import asyncio
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import Settings
from services.answer_validation import clarification_phrases, validate_answer
from services.conversation_engine import CallSession, ConversationEngine, _spoken_text_key
from services.gemini_service import GeminiService, _content_config
from services.turn_telemetry import TurnMetrics, TurnTimer
from services.verbal_composer import polish_gemini_response

SMALL = "gemini-3.5-flash-lite"
PRIMARY = "gemini-flash-lite-latest"
EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def _service(primary: str = PRIMARY, small: str = SMALL) -> GeminiService:
    service = GeminiService.__new__(GeminiService)
    service.model_name = primary
    service.simple_model_name = small
    service._build_turn_prompt = lambda *a, **k: ("prompt", 1.0)
    return service


class SimpleModelConfigTests(unittest.TestCase):
    def test_environment_variable_sets_simple_model(self):
        with patch.dict(os.environ, {"GEMINI_SIMPLE_MODEL": SMALL}):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.gemini_simple_model, SMALL)

    def test_simple_model_defaults_to_empty(self):
        env = {k: v for k, v in os.environ.items() if k != "GEMINI_SIMPLE_MODEL"}
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.gemini_simple_model, "")

    def test_env_example_documents_the_verified_model(self):
        lines = EXAMPLE.read_text(encoding="utf-8").splitlines()
        self.assertIn(f"GEMINI_SIMPLE_MODEL={SMALL}", lines)

    def test_service_reads_simple_model_from_settings(self):
        settings = SimpleNamespace(
            gemini_model=PRIMARY, gemini_simple_model=SMALL, gemini_api_key=""
        )
        with patch("services.gemini_service.get_settings", return_value=settings), patch(
            "services.gemini_service.genai.Client"
        ):
            service = GeminiService()
        self.assertEqual(service.simple_model_name, SMALL)
        self.assertEqual(service._route_turn("It is pending.", "claim_status", True).model, SMALL)

    def test_request_config_sends_no_thinking_budget(self):
        config = _content_config(0.65)
        self.assertIsNone(config.thinking_config)
        self.assertEqual(config.response_mime_type, "application/json")


class SimpleModelRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, transcript: str, topic: str, valid: bool, service=None):
        service = service or _service()
        service._generate = AsyncMock(return_value={"response_text": "Thanks."})
        result = await service.generate_response(
            transcript, [], "Chris", "en-US", answer_topic=topic, answer_valid=valid
        )
        return service, result

    async def test_simple_turn_makes_one_call_on_small_model(self):
        service, result = await self._run("It is still pending.", "claim_status", True)
        self.assertEqual(service._generate.await_count, 1)
        self.assertEqual(service._generate.await_args.kwargs["model_name"], SMALL)
        self.assertEqual(result["_timing"]["model_tier"], "simple")
        self.assertFalse(result["_timing"]["model_fallback_used"])

    async def test_denial_turn_makes_one_call_on_primary(self):
        service, result = await self._run(
            "It was denied, why should we appeal?", "claim_status", True
        )
        self.assertEqual(service._generate.await_count, 1)
        self.assertEqual(service._generate.await_args.kwargs["model_name"], PRIMARY)
        self.assertEqual(result["_timing"]["model_tier"], "complex")

    async def test_ambiguous_turn_stays_on_primary(self):
        service, _ = await self._run("and, and this", "reviewer", False)
        self.assertEqual(service._generate.await_args.kwargs["model_name"], PRIMARY)

    async def test_small_model_error_falls_back_to_primary_once(self):
        service = _service()
        service._generate = AsyncMock(
            side_effect=[RuntimeError("400 INVALID_ARGUMENT"), {"response_text": "Thanks."}]
        )
        result = await service.generate_response(
            "It is still pending.", [], "Chris", "en-US",
            answer_topic="claim_status", answer_valid=True,
        )
        models = [c.kwargs["model_name"] for c in service._generate.await_args_list]
        self.assertEqual(models, [SMALL, PRIMARY])
        self.assertEqual(result["_timing"]["model_route_reason"], "simple_model_failure_fallback")


class RedundantCallTests(unittest.TestCase):
    def test_gemini_polish_is_local_and_makes_no_model_call(self):
        with patch.object(GeminiService, "_generate", side_effect=AssertionError("model call")):
            text, meta = polish_gemini_response(
                "polish", "Thanks. Who is reviewing the claim?", "It's pending.", []
            )
        self.assertIn("reviewing", text)
        self.assertEqual(meta["source"], "gemini_polish")


class TelemetryTests(unittest.IsolatedAsyncioTestCase):
    def _engine_session(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        engine.gemini = SimpleNamespace(model_name=PRIMARY)
        session = CallSession(call_id="telemetry")
        session.active_turn_timer = TurnTimer(metrics=TurnMetrics(turn_id=1, call_id="telemetry"))
        return engine, session

    def test_small_model_turn_is_marked_as_slm(self):
        engine, session = self._engine_session()
        engine._apply_gemini_timing(
            session,
            {"_timing": {"llm_ms": 410.0, "model": SMALL, "model_tier": "simple",
                         "model_route_reason": "validated_single_topic_answer"}},
        )
        metrics = session.active_turn_timer.metrics
        self.assertTrue(metrics.slm_used)
        self.assertEqual(metrics.llm_model, SMALL)
        self.assertEqual(metrics.llm_ms, 410.0)

    async def test_failed_model_call_is_counted_as_llm_time(self):
        engine, session = self._engine_session()

        async def failing(*_a, **_k):
            await asyncio.sleep(0.05)
            raise RuntimeError("400 INVALID_ARGUMENT")

        engine.gemini.generate_response = failing
        engine._apply_tone_signal = lambda *a, **k: None
        with patch(
            "services.conversation_engine.scenario_fallback_reply", return_value="Thank you."
        ):
            result = await engine._gemini_turn(session, "It is pending.")
        metrics = session.active_turn_timer.metrics
        self.assertEqual(result["response_text"], "Thank you.")
        self.assertEqual(metrics.model_route_reason, "llm_failure_scripted_fallback")
        self.assertGreaterEqual(metrics.llm_ms, 40.0)


class ClarificationCacheTests(unittest.TestCase):
    def test_clarification_text_matches_a_precached_phrase(self):
        question = "Could you please tell me who is reviewing the claim?"
        result = validate_answer("and, and this", question, 0.4)
        keys = {_spoken_text_key(text) for text in clarification_phrases(result.topic)}
        self.assertIn(_spoken_text_key(result.clarification), keys)

    def test_unknown_topic_precaches_nothing(self):
        self.assertEqual(clarification_phrases("unknown"), [])

    def test_no_precache_without_a_live_call(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        session = CallSession(call_id="offline")
        engine._precache_clarifications_async(session, "reviewer")
        self.assertEqual(session.clarification_cached_topics, set())


if __name__ == "__main__":
    unittest.main()
