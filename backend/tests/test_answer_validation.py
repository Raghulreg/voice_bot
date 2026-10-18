import unittest

from services.answer_validation import (
    classify_question,
    dialogue_action,
    extract_explicit_secondary_facts,
    move_on_after_ambiguity,
    resolve_pending_date,
    validate_answer,
)
from services.conversation_engine import (
    CallSession,
    ConversationEngine,
    _apply_ai_extraction,
)
from services.rcm_extraction import apply_validated_facts


class QuestionSpecificValidationTests(unittest.TestCase):
    def test_you_is_reviewer_self_reference(self):
        result = validate_answer(
            "You.",
            "Who is the assigned reviewer or review team?",
            confidence=0.0,
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.reason, "reviewer_self_reference")
        self.assertEqual(
            result.facts["assigned_reviewer"],
            "payer representative on this call",
        )
        self.assertNotEqual(result.facts["assigned_reviewer"], "You")

    def test_zero_confidence_does_not_reject_a_clear_answer(self):
        result = validate_answer(
            "The claim is under review.",
            "What is the current claim status?",
            confidence=0.0,
        )
        self.assertTrue(result.accepted)
        self.assertNotEqual(result.reason, "low_confidence")

    def test_clear_document_negatives_are_accepted_but_garble_is_not(self):
        question = "Are any documents pending?"
        for answer in ("No.", "None", "No no"):
            result = validate_answer(answer, question, confidence=0.8)
            self.assertTrue(result.accepted, answer)
            self.assertEqual(result.facts["pending_documents_status"], "none")
        garbled = validate_answer(
            "No, no, it's not a good set of conditions.",
            question,
            confidence=0.74,
        )
        self.assertTrue(garbled.ambiguous)

    def test_date_under_reviewer_question_is_preserved_but_not_assigned(self):
        result = validate_answer(
            "October 20.",
            "Who is the assigned reviewer or review team?",
            confidence=0.63,
        )
        self.assertTrue(result.ambiguous)
        self.assertEqual(result.facts["candidate_date"], "October 20")
        self.assertNotIn("expected_date", result.facts)

    def test_pending_date_requires_event_context(self):
        unresolved = resolve_pending_date("October 20", "No, that is not right.")
        self.assertTrue(unresolved.ambiguous)

        review = resolve_pending_date("October 20", "That is the review completion date.")
        self.assertEqual(review.facts["review_completion_date"], "October 20")
        self.assertNotIn("expected_date", review.facts)

        payment = resolve_pending_date("October 20", "That is the payment date.")
        self.assertEqual(payment.facts["expected_date"], "October 20")

    def test_active_date_question_selects_the_correct_field(self):
        payment = validate_answer(
            "October 20.",
            "What is the expected payment or disbursement date?",
            confidence=0.9,
        )
        review = validate_answer(
            "October 20.",
            "What is the expected review completion date?",
            confidence=0.9,
        )
        self.assertEqual(payment.facts, {"expected_date": "October 20"})
        self.assertEqual(
            review.facts,
            {"review_completion_date": "October 20"},
        )

    def test_unclear_document_answer_is_not_negative_document_evidence(self):
        result = validate_answer(
            "No, no, it's not a good set of conditions.",
            "Are any specific documents pending for the review?",
            confidence=0.74,
        )
        self.assertTrue(result.ambiguous)
        self.assertNotIn("pending_documents_status", result.facts)

    def test_explicit_no_documents_is_accepted(self):
        result = validate_answer(
            "No, there are no pending documents.",
            "Are any documents pending?",
            confidence=0.9,
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.facts["pending_documents_status"], "none")

    def test_partial_transcripts_are_ambiguous(self):
        question = "Who is the assigned reviewer?"
        self.assertTrue(validate_answer("Is.", question, 0.26).ambiguous)
        self.assertTrue(validate_answer("and, and this", question, 0.75).ambiguous)

    def test_repeated_incomplete_answer_never_advances_to_model(self):
        unclear = validate_answer(
            "and, and this",
            "Who is the assigned reviewer?",
            confidence=0.75,
        )
        self.assertEqual(dialogue_action(unclear, already_clarified=False), "clarify")
        self.assertEqual(dialogue_action(unclear, already_clarified=True), "move_on")
        unclassified = validate_answer(
            "It is not a good set of conditions.",
            "Please continue.",
            confidence=0.8,
        )
        self.assertEqual(unclassified.reason, "unclassified_answer")
        self.assertEqual(
            dialogue_action(unclassified, already_clarified=True),
            "move_on",
        )

    def test_second_ambiguity_moves_to_a_different_topic(self):
        response = move_on_after_ambiguity("reviewer")
        self.assertIn("leave that unresolved", response.lower())
        self.assertIn("documents", response.lower())
        self.assertNotIn("who is assigned", response.lower())

    def test_repeated_status_does_not_answer_reviewer_question(self):
        result = validate_answer(
            "It is under review.",
            "Who is the assigned reviewer or review team?",
            confidence=0.9,
        )
        self.assertTrue(result.ambiguous)
        self.assertNotIn("assigned_reviewer", result.facts)

    def test_remittance_availability_and_date_are_distinct(self):
        result = validate_answer(
            "The EOB is not available and is expected October 25.",
            "Is the EOB available, and if not, when is it expected?",
            confidence=0.9,
        )
        self.assertEqual(result.facts["remittance_status"], "not_available")
        self.assertEqual(result.facts["remittance_expected_date"], "October 25")

    def test_multi_part_answer_keeps_only_explicit_labelled_facts(self):
        facts = extract_explicit_secondary_facts(
            "There are no pending documents. Payment is expected October 20, "
            "and the payer reference is PAY-9988."
        )
        self.assertEqual(facts["pending_documents_status"], "none")
        self.assertEqual(facts["expected_date"], "October 20")
        self.assertEqual(facts["payer_reference"], "PAY-9988")

    def test_reference_requires_reference_question_and_claim_id_is_rejected(self):
        result = validate_answer(
            "The number is 21.",
            "What is the payer reference number?",
            confidence=0.9,
        )
        session = CallSession(call_id="ref", claim_id="21")
        apply_validated_facts(session, result.facts)
        self.assertNotIn("payer_reference", session.extracted_data)


class ConversationContextTests(unittest.TestCase):
    def test_clarification_preserves_original_question_context(self):
        engine = object.__new__(ConversationEngine)
        session = CallSession(call_id="context")
        engine._set_last_question(
            session,
            "Who is the assigned reviewer or review team?",
        )
        engine._set_last_question(
            session,
            "Who is assigned—the reviewer's name or the review team?",
            preserve_answer_context=True,
        )
        self.assertEqual(session.answer_topic, "reviewer")
        self.assertIn("assigned reviewer", session.answer_question.lower())

    def test_model_cannot_assign_date_to_reviewer_answer(self):
        session = CallSession(call_id="wrong-field")
        _apply_ai_extraction(
            session,
            {},
            {
                "expected_payment_date": "October 20",
                "extracted_data": {"review_date": "October 20"},
                "confidence_score": 0.95,
            },
            "October 20.",
            question_topic="reviewer",
            answer_valid=False,
        )
        self.assertEqual(session.expected_payment_date, "")
        self.assertNotIn("review_date", session.extracted_data)
        self.assertEqual(session.confidence_score, 0.0)

    def test_model_cannot_overwrite_validated_facts_or_dates(self):
        session = CallSession(call_id="protected")
        session.extracted_data["assigned_reviewer"] = "payer representative on this call"
        session.expected_payment_date = ""
        _apply_ai_extraction(
            session,
            {},
            {
                "expected_payment_date": "October 20",
                "extracted_data": {
                    "assigned_reviewer": "You",
                    "review_date": "October 20",
                    "payer_reference": "21",
                },
                "confidence_score": 0.95,
            },
            "You. October 20. The claim number is 21.",
            question_topic="reviewer",
            answer_valid=True,
        )
        self.assertEqual(
            session.extracted_data["assigned_reviewer"],
            "payer representative on this call",
        )
        self.assertEqual(session.expected_payment_date, "")
        self.assertNotIn("review_date", session.extracted_data)
        self.assertNotIn("payer_reference", session.extracted_data)

    def test_latest_call_fragment_sequence_does_not_create_facts(self):
        engine = object.__new__(ConversationEngine)
        session = CallSession(call_id="sequence")
        engine._set_last_question(
            session,
            "Who is the assigned reviewer or review team?",
        )

        reviewer = engine._interpret_customer_answer(session, "You.", 0.0)
        self.assertTrue(reviewer.accepted)
        self.assertEqual(
            session.extracted_data["assigned_reviewer"],
            "payer representative on this call",
        )
        engine._set_last_question(
            session,
            "Could you repeat the reviewer name?",
            preserve_answer_context=True,
        )

        date = engine._interpret_customer_answer(session, "October 20.", 0.63)
        self.assertTrue(date.ambiguous)
        self.assertIn("candidate_date", session.pending_answer_context)
        engine._set_last_question(
            session,
            date.clarification,
            preserve_answer_context=True,
        )

        unclear = engine._interpret_customer_answer(
            session,
            "No, no, it's not a good set of conditions.",
            0.74,
        )
        self.assertTrue(unclear.ambiguous)
        self.assertEqual(session.pending_answer_context, {})
        self.assertEqual(
            session.extracted_data["assigned_reviewer"],
            "payer representative on this call",
        )
        self.assertNotIn("review_completion_date", session.extracted_data)
        self.assertEqual(session.expected_payment_date, "")

    def test_question_classification(self):
        self.assertEqual(
            classify_question("What is the current claim status?"),
            "claim_status",
        )
        self.assertEqual(
            classify_question("Is the remittance advice available?"),
            "remittance",
        )


if __name__ == "__main__":
    unittest.main()
