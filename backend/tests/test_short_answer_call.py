"""Regressions from call d0e48d1c. No network or paid API calls."""

import os
import time
import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from services.answer_validation import clarification_phrases, dialogue_action, validate_answer
from services.conversation_engine import CallSession, ConversationEngine
from services.gemini_service import GeminiService
from services.turn_telemetry import TurnMetrics, TurnTimer, compute_latency_summary

GREETING = (
    "Hi, this is Chris calling from Apollo Hospital regarding claims status. "
    "Could you please help me check on a pending claim?"
)
STATUS = "Could you please tell me the current status of the claim?"
REVIEWER = (
    "I couldn't confirm the status, so I'll leave it unresolved. "
    "Could you please tell me who is reviewing the claim?"
)


class ShortAnswerTests(unittest.TestCase):
    def test_yes_i_do_this_agrees_to_help_without_storing_a_status(self):
        result = validate_answer("Yes, I do this.", GREETING, 0.828)
        self.assertEqual(result.reason, "help_agreed")
        self.assertEqual(result.facts, {})
        self.assertIn("current status", result.clarification.lower())
        self.assertNotIn("cut off", result.clarification.lower())

    def test_plain_yes_to_the_greeting_is_not_pending(self):
        result = validate_answer("Yes.", GREETING, 0.9)
        self.assertFalse(result.accepted)
        self.assertEqual(result.facts, {})

    def test_greeting_answer_with_a_status_is_still_recorded(self):
        result = validate_answer("Yes, it is still pending.", GREETING, 0.9)
        self.assertTrue(result.accepted)
        self.assertEqual(result.facts["claim_status"], "pending")

    def test_yes_uh_to_status_question_is_not_cut_off(self):
        result = validate_answer("Yes. Uh", STATUS, 0.43)
        self.assertNotEqual(result.reason, "incomplete_fragment")
        self.assertEqual(result.facts, {})
        self.assertIn("current status", result.clarification.lower())

    def test_uh_yes_is_not_a_reviewer_name(self):
        result = validate_answer("Uh, yes.", REVIEWER, 0.94)
        self.assertFalse(result.accepted)
        self.assertNotIn("assigned_reviewer", result.facts)
        self.assertIn("reviewer", result.clarification.lower())

    def test_filler_before_a_real_name_is_still_a_reviewer(self):
        result = validate_answer("Uh, Sarah Lee.", REVIEWER, 0.9)
        self.assertTrue(result.accepted)
        self.assertIn("Sarah Lee", result.facts["assigned_reviewer"])

    def test_cut_off_answer_is_still_detected(self):
        result = validate_answer("The claim is, uh", STATUS, 0.9)
        self.assertEqual(result.reason, "incomplete_fragment")

    def test_long_answer_with_hesitation_is_kept_whole(self):
        text = "Uh, the claim is, um, still under review with the medical team."
        result = validate_answer(text, STATUS, 0.85)
        self.assertTrue(result.accepted)
        self.assertEqual(result.facts["claim_status"], "under_review")

    def test_only_filler_asks_once_politely(self):
        result = validate_answer("Uh.", STATUS, 0.6)
        self.assertEqual(result.reason, "short_fragment")
        self.assertEqual(dialogue_action(result, already_clarified=False), "clarify")

    def test_help_agreed_line_is_precached(self):
        line = validate_answer("Yes, I do this.", GREETING, 0.8).clarification
        self.assertIn(line, clarification_phrases("claim_status"))


class HelpAgreedFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_question_after_help_is_a_fresh_question(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        engine._build_agent_twiml = AsyncMock(return_value="<Response/>")
        engine._persist_call_async = MagicMock()
        engine._stt_looks_wrong = MagicMock(return_value=False)
        engine._precache_clarifications_async = MagicMock()
        session = CallSession(call_id="help")
        engine._set_last_question(session, GREETING, index=0)
        call_records = {"help": {"transcript": []}}
        with patch("services.conversation_engine.call_records", call_records):
            await engine.build_speech_response_twiml(session, "Yes, I do this.", "https://x.test")
        self.assertEqual(session.answer_topic, "claim_status")
        self.assertIn("current status of the claim", session.answer_question)
        self.assertEqual(session.followup_used, set())
        result = validate_answer("Yes. Uh", session.answer_question, 0.43)
        self.assertEqual(dialogue_action(result, already_clarified=False), "clarify")


class GeminiRequestInstrumentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_records_first_chunk_tokens_and_one_attempt(self):
        service = GeminiService.__new__(GeminiService)
        service.model_name = "gemini-flash-lite-latest"
        service.simple_model_name = "gemini-3.5-flash-lite"
        usage = SimpleNamespace(
            prompt_token_count=410, candidates_token_count=38, thoughts_token_count=120
        )

        async def chunks():
            yield SimpleNamespace(text='{"response_text": "Who is', usage_metadata=None, model_version="m")
            yield SimpleNamespace(text=' reviewing it?"}', usage_metadata=usage, model_version="m")

        service._client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(generate_content_stream=AsyncMock(return_value=chunks()))
            )
        )
        result = await service.generate_response(
            "It is still pending.", [], "Chris", "en-US",
            answer_topic="claim_status", answer_valid=True,
        )
        timing = result["_timing"]
        self.assertEqual(result["response_text"], "Who is reviewing it?")
        self.assertEqual(timing["attempts"], 1)
        self.assertEqual(timing["model"], "gemini-3.5-flash-lite")
        self.assertEqual(timing["output_tokens"], 38)
        self.assertEqual(timing["thought_tokens"], 120)
        self.assertIsNotNone(timing["first_delta_ms"])
        self.assertLessEqual(timing["first_delta_ms"], timing["llm_ms"])
        self.assertNotIn("_request", result)

    async def test_fallback_records_two_attempts_and_the_error(self):
        service = GeminiService.__new__(GeminiService)
        service.model_name = "gemini-flash-lite-latest"
        service.simple_model_name = "gemini-3.5-flash-lite"
        service._build_turn_prompt = lambda *a, **k: ("prompt", 1.0)
        error = RuntimeError("unavailable")
        error.code = 503
        service._generate = AsyncMock(side_effect=[error, {"response_text": "Thanks."}])
        result = await service.generate_response(
            "It is still pending.", [], "Chris", "en-US",
            answer_topic="claim_status", answer_valid=True,
        )
        self.assertEqual(result["_timing"]["attempts"], 2)
        self.assertEqual(result["_timing"]["error"], "RuntimeError:503")
        self.assertEqual(result["_timing"]["model"], "gemini-flash-lite-latest")


class InputWaitBreakdownTests(unittest.TestCase):
    def test_partial_callbacks_split_input_wait(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        session = CallSession(call_id="wait")
        now = time.time()
        session.last_agent_twiml_at = now - 9.0
        session.last_audio_fetched_at = now - 8.4
        session.last_agent_audio_ms = 4000.0
        session.speech_first_partial_at = now - 3.0
        session.speech_last_partial_at = now - 2.2
        metrics = TurnMetrics(turn_id=1, call_id="wait", webhook_received_at=now)
        engine._split_input_wait(session, metrics)
        self.assertAlmostEqual(metrics.audio_fetch_delay_ms, 600, delta=5)
        self.assertAlmostEqual(metrics.caller_start_ms, 1400, delta=5)
        self.assertAlmostEqual(metrics.caller_speaking_ms, 800, delta=5)
        self.assertAlmostEqual(metrics.speech_end_to_webhook_ms, 2200, delta=5)
        self.assertEqual(session.speech_first_partial_at, 0.0)

    def test_without_partials_the_split_stays_empty(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        session = CallSession(call_id="none")
        session.last_agent_twiml_at = time.time() - 5
        metrics = TurnMetrics(turn_id=1, call_id="none", webhook_received_at=time.time())
        engine._split_input_wait(session, metrics)
        self.assertIsNone(metrics.speech_end_to_webhook_ms)

    def test_gather_partial_callback_is_opt_in_and_xml_stays_valid(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        action = "https://x.test/api/webhooks/conversation?call_id=c1"
        off = SimpleNamespace(
            twilio_gather_speech_timeout="2", twilio_gather_partial_timing=False
        )
        on = SimpleNamespace(
            twilio_gather_speech_timeout="2", twilio_gather_partial_timing=True
        )
        with patch("services.conversation_engine.get_settings", return_value=off):
            plain = engine._customer_listen_xml(action, "en-US")
        with patch("services.conversation_engine.get_settings", return_value=on):
            timed = engine._customer_listen_xml(action, "en-US")
        self.assertNotIn("partialResultCallback", plain)
        self.assertIn('speechTimeout="2"', plain)
        self.assertIn(
            'partialResultCallback="https://x.test/api/webhooks/gather-partial?call_id=c1"',
            timed,
        )
        for xml in (plain, timed):
            ET.fromstring(f"<Response>{xml}</Response>")

    def test_partial_timing_defaults_off(self):
        from config import Settings

        env = {k: v for k, v in os.environ.items() if k != "TWILIO_GATHER_PARTIAL_TIMING"}
        with patch.dict(os.environ, env, clear=True):
            settings = Settings(_env_file=None)
        self.assertFalse(settings.twilio_gather_partial_timing)

    def test_summary_reports_the_split(self):
        summary = compute_latency_summary(
            [{"metrics": {"speech_end_to_webhook_ms": 2300.0, "llm_request_ms": 6000.0}}]
        )
        self.assertEqual(summary["p50_speech_end_to_webhook_ms"], 2300.0)
        self.assertEqual(summary["p50_llm_request_ms"], 6000.0)


class AudioFetchKeptTests(unittest.TestCase):
    def test_fetch_time_is_still_recorded_per_turn(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        session = CallSession(call_id="fetch")
        metrics = {"handler_ms": 2.0}
        session.audio_turn_metrics[3] = (metrics, time.time() - 0.6)
        engine.mark_audio_fetched(session, 3)
        self.assertAlmostEqual(metrics["webhook_to_audio_fetch_ms"], 600, delta=50)
        self.assertGreater(session.last_audio_fetched_at, 0)


if __name__ == "__main__":
    unittest.main()
