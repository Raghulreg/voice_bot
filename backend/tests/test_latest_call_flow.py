"""Regressions from call 1c727285. No network or paid API calls."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from prompt_builder import build_simple_turn_prompt
from services.answer_validation import validate_answer
from services.conversation_engine import (
    GATHER_CONTINUE_BUDGET_S,
    GATHER_HOLD_PAUSE_S,
    GATHER_WEBHOOK_BUDGET_S,
    CallSession,
    ConversationEngine,
)
from services.gemini_service import GeminiService, _SIMPLE_MAX_OUTPUT_TOKENS
from services.rcm_dialogue import (
    RcmCollected,
    open_rcm_gaps,
    replace_unnecessary_status_probe,
    workflow_reply_for_confirmed_status,
)
from services.turn_telemetry import TurnMetrics, TurnTimer, profile_turn_stages

STATUS_QUESTION = "Could you please tell me the current status of the claim?"
PAYER = "yes, we are still pending"
CAUSE = "Could you please let me know what is currently causing the pending status on this claim?"


class PendingStatusFlowTests(unittest.TestCase):
    def test_pending_answer_is_accepted(self):
        result = validate_answer(PAYER, STATUS_QUESTION, 0.65)
        self.assertTrue(result.accepted)
        self.assertEqual(result.facts["claim_status"], "pending")

    def test_confirmed_pending_asks_for_the_reviewer(self):
        collected = RcmCollected(resolution_status="pending", resolution_code="pending")
        spoken = workflow_reply_for_confirmed_status(collected)
        self.assertIn("still pending", spoken.lower())
        self.assertIn("who is reviewing", spoken.lower())
        self.assertNotIn("causing", spoken.lower())
        self.assertNotIn("why", spoken.lower())

    def test_pending_gaps_do_not_ask_for_a_denial_reason(self):
        gaps = open_rcm_gaps(
            RcmCollected(resolution_status="pending", resolution_code="pending")
        )
        self.assertTrue(any("reviewer" in gap for gap in gaps))
        self.assertFalse(any("denial" in gap for gap in gaps))

    def test_cause_question_is_replaced_with_the_next_workflow_question(self):
        collected = RcmCollected(resolution_status="pending", resolution_code="pending")
        spoken = replace_unnecessary_status_probe(CAUSE, collected)
        self.assertIn("who is reviewing", spoken.lower())
        self.assertNotIn("causing", spoken.lower())

    def test_denied_status_still_asks_for_the_denial_code(self):
        spoken = workflow_reply_for_confirmed_status(
            RcmCollected(resolution_status="denied", resolution_code="denied")
        )
        self.assertIn("denial code", spoken.lower())


class WorkflowSkipsModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_validated_pending_turn_does_not_call_the_model(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        engine.gemini = SimpleNamespace(
            generate_response=AsyncMock(side_effect=AssertionError("model call"))
        )
        engine._finalize_gemini_turn_twiml = AsyncMock(return_value="<Response/>")
        session = CallSession(call_id="pending", scenario_id="scenario")
        session.answer_topic = "claim_status"
        session.current_answer_valid = True
        session.resolution_code = "pending"
        session.resolution_status = "pending"
        session.active_turn_timer = TurnTimer(
            metrics=TurnMetrics(turn_id=2, call_id="pending")
        )
        settings = SimpleNamespace(low_latency_calls=False)
        with patch("services.conversation_engine.get_settings", return_value=settings):
            await engine._advance_discovery(
                session, {}, PAYER, "https://example.test", "en-US", []
            )
        engine.gemini.generate_response.assert_not_called()
        spoken = engine._finalize_gemini_turn_twiml.await_args.args[2]["response_text"]
        self.assertIn("who is reviewing", spoken.lower())
        self.assertNotIn("causing", spoken.lower())
        self.assertEqual(session.active_turn_timer.metrics.path, "workflow")
        self.assertEqual(session.active_turn_timer.metrics.llm_ms, 0.0)


class MeasuredLatencyTests(unittest.TestCase):
    def test_reply_to_audio_fetch_is_not_counted_as_llm_time(self):
        stages = profile_turn_stages(
            {
                "handler_ms": 6810.6,
                "llm_ms": 4173.2,
                "tts_ms": 2632.4,
                "stt_ms": 0.0,
                "speech_end_to_stt_final_ms": None,
                "speech_end_measured": False,
                "input_wait_ms": 7552.4,
                "webhook_to_audio_fetch_ms": 12526.0,
            }
        )
        self.assertEqual(stages["llm_request_ms"], 4173.2)
        self.assertEqual(stages["tts_generation_ms"], 2632.4)
        self.assertEqual(stages["audio_ready_ms"], 6810.6)
        self.assertEqual(stages["twilio_fetch_wait_ms"], 5715.4)
        self.assertFalse(stages["speech_end_measured"])
        self.assertLess(stages["llm_request_ms"], stages["reply_to_audio_fetch_ms"])

    def test_webhook_budget_answers_before_the_tunnel_drops_the_response(self):
        # 4.45s greeting responses were accepted. 6s and 6.7s responses were not.
        self.assertLessEqual(GATHER_WEBHOOK_BUDGET_S, 4.0)
        self.assertLessEqual(GATHER_CONTINUE_BUDGET_S, 4.0)
        self.assertGreater(GATHER_WEBHOOK_BUDGET_S, 0)
        self.assertEqual(GATHER_HOLD_PAUSE_S, "4")


class SimplePromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_simple_route_uses_the_short_prompt_and_output_cap(self):
        service = GeminiService.__new__(GeminiService)
        service.model_name = "gemini-flash-lite-latest"
        service.simple_model_name = "gemini-3.5-flash-lite"
        service._generate = AsyncMock(return_value={"response_text": "Who is reviewing the claim?"})
        await service.generate_response(
            "It is still pending.",
            [],
            "Chris",
            "en-US",
            answer_topic="claim_status",
            answer_valid=True,
        )
        prompt = service._generate.await_args.args[0]
        self.assertIn("do not ask why", prompt.lower())
        self.assertNotIn("Exemplars", prompt)
        self.assertEqual(
            service._generate.await_args.kwargs["max_output_tokens"],
            _SIMPLE_MAX_OUTPUT_TOKENS,
        )
        self.assertIn(
            "do not ask why",
            build_simple_turn_prompt("Chris", "It is still pending.", []).lower(),
        )


if __name__ == "__main__":
    unittest.main()
