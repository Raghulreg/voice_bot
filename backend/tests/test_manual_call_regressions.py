"""Regressions from the latest manual call. No network or paid API calls."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from prompt import SYSTEM_PROMPT, USER_TURN_PROMPT
from services.answer_validation import (
    classify_question,
    dialogue_action,
    move_on_after_ambiguity,
    validate_answer,
)
from services.conversation_engine import ConversationEngine, CallSession
from services.gemini_service import _content_config
from services.model_routing import route_model


class SpeechUnderstandingTests(unittest.TestCase):
    STATUS = "Could you help me check the current status?"

    def test_short_review_team_is_accepted(self):
        result = validate_answer("Review team.", "Who is reviewing the claim?", 0.95)
        self.assertTrue(result.accepted)
        self.assertEqual(result.facts["assigned_reviewer"], "Review team.")

    def test_medium_pending_status_is_accepted(self):
        result = validate_answer(
            "The claim is currently pending.", self.STATUS, 0.8
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.facts["claim_status"], "pending")

    def test_longer_answer_keeps_the_review_team_and_status(self):
        text = "The assigned review team is SSL, and the claim is still under review."
        reviewer = validate_answer(text, "Who is reviewing the claim?", 0.7)
        status = validate_answer(text, self.STATUS, 0.7)
        self.assertTrue(reviewer.accepted)
        self.assertIn("SSL", reviewer.facts["assigned_reviewer"])
        self.assertTrue(status.accepted)
        self.assertEqual(status.facts["claim_status"], "under_review")

    def test_ssl_review_is_a_team_not_a_fragment(self):
        result = validate_answer("SSL review.", "Who is reviewing the claim?", 0.0)
        self.assertTrue(result.accepted)

    def test_low_confidence_status_is_confirmed_not_stored(self):
        result = validate_answer("Claim approved.", self.STATUS, 0.096)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "low_confidence_confirm")
        self.assertEqual(result.facts, {})
        self.assertIn("approved", result.clarification.lower())
        self.assertNotIn("denied", result.clarification.lower())

    def test_empty_transcript_is_not_a_confirmed_answer(self):
        result = validate_answer("   ", self.STATUS, None)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "empty")

    def test_acknowledgement_asks_one_open_status_question(self):
        for text in ("Okay.", "Yes, sure."):
            result = validate_answer(text, self.STATUS, 0.8)
            self.assertFalse(result.accepted, text)
            self.assertNotIn("approved, denied", result.clarification.lower())
            self.assertIn("current status", result.clarification.lower())

    def test_second_unclear_answer_does_not_ask_the_same_question(self):
        unclear = validate_answer("and, and this", "Who is reviewing the claim?", 0.4)
        self.assertEqual(dialogue_action(unclear, already_clarified=True), "move_on")

    def test_move_on_sentence_is_classified_as_its_new_question(self):
        spoken = move_on_after_ambiguity("reviewer")
        self.assertEqual(classify_question(spoken), "pending_documents")
        documents = validate_answer("Documents.", spoken, 0.4)
        self.assertTrue(documents.accepted)
        self.assertEqual(documents.topic, "pending_documents")

    def test_status_question_is_not_a_forced_choice_menu(self):
        result = validate_answer("Hmm.", self.STATUS, 0.2)
        self.assertNotIn("approved, denied, pending, or under review", result.clarification.lower())


class PromptAndRoutingTests(unittest.TestCase):
    def test_prompts_ask_for_one_natural_question(self):
        self.assertIn("one natural question", SYSTEM_PROMPT)
        self.assertIn("current status of the claim", SYSTEM_PROMPT)
        self.assertIn("acknowledge that answer", USER_TURN_PROMPT)

    def test_request_omits_rejected_thinking_budget(self):
        self.assertIsNone(_content_config(0.65).thinking_config)

    def test_simple_and_complex_routing_stay_separate(self):
        simple = route_model("It is under review.", "claim_status", True, "primary", "small")
        complex_turn = route_model("Why was it denied?", "claim_status", True, "primary", "small")
        unconfigured = route_model("It is under review.", "claim_status", True, "primary", "")
        self.assertEqual(simple.model, "small")
        self.assertEqual(complex_turn.model, "primary")
        self.assertEqual(unconfigured.model, "primary")
        self.assertEqual(unconfigured.reason, "simple_model_not_configured")


class WebhookReliabilityTests(unittest.TestCase):
    def test_gather_waits_through_a_short_pause_in_phone_audio(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        settings = SimpleNamespace(twilio_gather_speech_timeout="2")
        with patch("services.conversation_engine.get_settings", return_value=settings):
            xml = engine._customer_listen_xml("https://example.test/next", "en-US")
        self.assertIn('speechTimeout="2"', xml)
        self.assertIn('timeout="5"', xml)
        self.assertIn('speechModel="phone_call"', xml)

    def test_slow_hold_uses_one_pause_and_no_filler(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        session = CallSession(call_id="hold")
        xml = engine._build_pending_continue_twiml(session, "https://example.test")
        self.assertIn('<Pause length="4"/>', xml)
        self.assertEqual(xml.count("<Redirect"), 1)
        self.assertNotIn("<Say", xml)
        self.assertNotIn("one moment", xml.lower())


if __name__ == "__main__":
    unittest.main()
