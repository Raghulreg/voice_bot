import unittest
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

from services.answer_validation import validate_answer
from services.conversation_engine import CallSession, _apply_ai_extraction
from services.gemini_service import GeminiService
from services.model_routing import route_model
from services.rcm_extraction import apply_validated_facts

PRIMARY = "gemini-primary"
SMALL = "gemini-small"


class ModelRoutingTests(unittest.TestCase):
    def test_validated_simple_answer_uses_small_model(self):
        route = route_model("It is under review.", "claim_status", True, PRIMARY, SMALL)
        self.assertEqual(route.tier, "simple")
        self.assertEqual(route.model, SMALL)

    def test_denial_reasoning_keeps_primary_model(self):
        route = route_model(
            "It was denied because of a policy exclusion.",
            "claim_status",
            True,
            PRIMARY,
            SMALL,
        )
        self.assertEqual(route.tier, "complex")
        self.assertEqual(route.model, PRIMARY)

    def test_unvalidated_answer_keeps_primary_model(self):
        route = route_model("And, and this", "reviewer", False, PRIMARY, SMALL)
        self.assertEqual(route.tier, "complex")
        self.assertEqual(route.model, PRIMARY)

    def test_unknown_topic_keeps_primary_model(self):
        route = route_model("Sure.", "unknown", True, PRIMARY, SMALL)
        self.assertEqual(route.model, PRIMARY)

    def test_long_answer_keeps_primary_model(self):
        long_answer = " ".join(["word"] * 30)
        route = route_model(long_answer, "claim_status", True, PRIMARY, SMALL)
        self.assertEqual(route.model, PRIMARY)

    def test_no_small_model_configured_keeps_existing_model(self):
        route = route_model("It is under review.", "claim_status", True, PRIMARY, "")
        self.assertEqual(route.tier, "simple")
        self.assertEqual(route.model, PRIMARY)


class ModelRoutingFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_simple_model_failure_retries_primary_model(self):
        service = GeminiService.__new__(GeminiService)
        service.model_name = PRIMARY
        service.simple_model_name = SMALL
        service._build_turn_prompt = Mock(return_value=("prompt", 1.0))
        service._generate = AsyncMock(
            side_effect=[
                RuntimeError("small unavailable"),
                {"response_text": "Under review.", "detected_emotion": "neutral"},
            ]
        )

        result = await service.generate_response(
            "It is under review.",
            [],
            "Chris",
            "en-US",
            answer_topic="claim_status",
            answer_valid=True,
        )

        models = [
            call.kwargs["model_name"] for call in service._generate.await_args_list
        ]
        self.assertEqual(models, [SMALL, PRIMARY])
        self.assertTrue(result["_timing"]["model_fallback_used"])
        self.assertEqual(result["_timing"]["model"], PRIMARY)
        self.assertEqual(
            result["_timing"]["model_route_reason"],
            "simple_model_failure_fallback",
        )

    async def test_primary_model_failure_is_not_retried(self):
        service = GeminiService.__new__(GeminiService)
        service.model_name = PRIMARY
        service.simple_model_name = SMALL
        service._build_turn_prompt = Mock(return_value=("prompt", 1.0))
        service._generate = AsyncMock(side_effect=RuntimeError("primary unavailable"))

        with self.assertRaises(RuntimeError):
            await service.generate_response(
                "Explain the denial reason.",
                [],
                "Chris",
                "en-US",
                answer_topic="claim_status",
                answer_valid=True,
            )
        self.assertEqual(service._generate.await_count, 1)

    async def test_stream_failure_before_speech_retries_primary_model(self):
        class Stream:
            def __init__(self, chunks):
                self._chunks = iter(chunks)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return SimpleNamespace(text=next(self._chunks))
                except StopIteration:
                    raise StopAsyncIteration

        models_called = []

        async def generate_content_stream(*, model, **_kwargs):
            models_called.append(model)
            if model == SMALL:
                raise RuntimeError("small unavailable")
            return Stream(
                [
                    '{"response_text":"Under review.",',
                    '"detected_emotion":"neutral"}',
                ]
            )

        service = GeminiService.__new__(GeminiService)
        service.model_name = PRIMARY
        service.simple_model_name = SMALL
        service._build_turn_prompt = Mock(return_value=("prompt", 1.0))
        service._client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(
                    generate_content_stream=generate_content_stream
                )
            )
        )

        events = [
            event
            async for event in service.generate_response_stream(
                "It is under review.",
                [],
                "Chris",
                "en-US",
                answer_topic="claim_status",
                answer_valid=True,
            )
        ]

        self.assertEqual(models_called, [SMALL, PRIMARY])
        done = next(event for event in events if event["event"] == "done")
        self.assertTrue(done["timing"]["model_fallback_used"])
        self.assertEqual(done["timing"]["model"], PRIMARY)


class ApprovedConversationFixTests(unittest.TestCase):
    def test_yes_correct_that_thing_is_not_approval(self):
        result = validate_answer(
            "Yes, correct that thing.",
            "Just to confirm, is the claim approved?",
        )
        self.assertTrue(result.ambiguous)
        self.assertNotIn("claim_status", result.facts)

    def test_plain_yes_to_single_status_question_is_accepted(self):
        result = validate_answer("Yes, that's correct.", "Is the claim approved?")
        self.assertTrue(result.accepted)
        self.assertEqual(result.facts["claim_status"], "approved")

    def test_plain_yes_to_multi_status_question_stays_unresolved(self):
        result = validate_answer(
            "Yes.",
            "Is the claim approved, denied, or still under review?",
        )
        self.assertTrue(result.ambiguous)
        self.assertNotIn("claim_status", result.facts)

    def test_negated_approval_is_not_approval(self):
        result = validate_answer(
            "It has not been approved.",
            "What is the current status of the claim?",
        )
        self.assertTrue(result.ambiguous)
        self.assertNotIn("claim_status", result.facts)

    def test_single_word_status_is_accepted(self):
        result = validate_answer("Approved.", "What is the current claim status?")
        self.assertTrue(result.accepted)
        self.assertEqual(result.facts["claim_status"], "approved")

    def test_reviewer_team_name_is_accepted(self):
        result = validate_answer(
            "The medical review team.",
            "Who is the assigned reviewer or review team?",
        )
        self.assertTrue(result.accepted)
        self.assertTrue(result.facts.get("assigned_reviewer"))

    def test_explicit_no_to_pending_documents_is_accepted(self):
        result = validate_answer(
            "No.",
            "Are there any pending documents needed for this claim?",
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.facts["pending_documents_status"], "none")

    def test_model_extraction_does_not_overwrite_validated_status(self):
        session = CallSession(call_id="c1", claim_id="CLM-1")
        apply_validated_facts(session, {"claim_status": "under_review"})
        _apply_ai_extraction(
            session,
            {},
            {"resolution_code": "approved", "resolution_status": "approved"},
            payer_text="Yes, correct that thing. It is approved.",
            question_topic="claim_status",
            answer_valid=False,
        )
        self.assertEqual(session.resolution_code, "under_review")


if __name__ == "__main__":
    unittest.main()
