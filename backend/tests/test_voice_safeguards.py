import asyncio
import unittest
from unittest.mock import patch

from services.conversation_engine import (
    CallSession,
    ConversationEngine,
    _apply_ai_extraction,
    call_records,
)
from services.rcm_extraction import apply_payer_utterance
from services.conversation_state_db import _collect_reference_numbers
from services.streaming_tts import speak_streaming
from services.turn_telemetry import (
    TurnMetrics,
    TurnTimer,
    compute_latency_summary,
)
from services.voice_config import (
    clarification_response,
    is_polite_filler,
    needs_clarification,
    repeat_response,
)


class ClarificationTests(unittest.TestCase):
    def test_completed_is_clarified(self):
        self.assertTrue(needs_clarification("Completed."))
        response = clarification_response("Completed.", "Is the review complete?")
        self.assertIn("outcome", response.lower())

    def test_clipped_and_low_confidence_short_text_are_clarified(self):
        self.assertTrue(needs_clarification("I have a"))
        self.assertTrue(needs_clarification("Maybe later", confidence=0.1))

    def test_multiword_ack_is_not_an_answer(self):
        self.assertTrue(is_polite_filler("Yes, sure."))
        self.assertTrue(needs_clarification("Yeah, okay."))
        self.assertFalse(is_polite_filler("You."))

    def test_repeat_does_not_replay_full_greeting(self):
        greeting = (
            "Hi, this is Chris calling from Apollo Hospital about claim number 21. "
            "I am calling for a status update. What is the current claim status?"
        )
        repeated = repeat_response("en-US", greeting)
        self.assertIn("current claim status", repeated.lower())
        self.assertNotIn("apollo hospital", repeated.lower())


class EvidenceTests(unittest.TestCase):
    def test_approval_does_not_mean_paid(self):
        session = CallSession(call_id="approval")
        apply_payer_utterance(session, "The claim is approved but not paid yet.")
        self.assertEqual(session.extracted_data["claim_approval_status"], "approved")
        self.assertEqual(session.extracted_data["payment_status"], "not_paid")
        self.assertNotEqual(session.resolution_code, "paid")

    def test_payment_does_not_imply_remittance(self):
        session = CallSession(call_id="payment")
        apply_payer_utterance(session, "The claim was paid yesterday.")
        self.assertEqual(session.extracted_data["payment_status"], "paid")
        self.assertNotIn("remittance_status", session.extracted_data)

    def test_claim_id_is_not_replaced_or_used_as_reference(self):
        session = CallSession(call_id="claim", claim_id="CLM-123")
        record = {}
        _apply_ai_extraction(
            session,
            record,
            {"extracted_data": {"claim_number": "CLM-123", "payer_reference": "CLM-123"}},
            "The payer reference is CLM-123.",
        )
        self.assertEqual(session.claim_id, "CLM-123")
        self.assertNotIn("payer_reference", session.extracted_data)

    def test_reference_alias_is_normalized(self):
        session = CallSession(call_id="alias", claim_id="CLM-123")
        _apply_ai_extraction(
            session,
            {},
            {"extracted_data": {"payer_reference_number": "PAYER-9988"}},
            "The payer reference number is PAYER-9988.",
        )
        self.assertEqual(session.extracted_data["payer_reference"], "PAYER-9988")

    def test_reference_aliases_are_persisted(self):
        refs = _collect_reference_numbers(
            {"extracted_data": {"payer_reference_number": "PAYER-9988"}},
            None,
        )
        self.assertEqual(refs, ["PAYER-9988"])

    def test_explicit_control_reference_is_stored(self):
        session = CallSession(call_id="reference")
        apply_payer_utterance(session, "The control number is 9988-AB.")
        self.assertEqual(session.extracted_data["payer_reference"], "9988-AB")

    def test_ambiguous_model_output_does_not_update_outcome(self):
        session = CallSession(call_id="ambiguous")
        _apply_ai_extraction(
            session,
            {},
            {
                "resolution_status": "paid",
                "resolution_code": "paid",
                "confidence_score": 0.99,
            },
            "Completed.",
        )
        self.assertEqual(session.resolution_status, "")
        self.assertEqual(session.resolution_code, "")
        self.assertEqual(session.confidence_score, 0.0)


class TelemetryTests(unittest.TestCase):
    def test_end_of_speech_to_first_audio_and_first_event_only(self):
        metrics = TurnMetrics(speech_end_to_stt_final_ms=200)
        timer = TurnTimer(metrics=metrics, _started=10.0)
        with patch("services.turn_telemetry.time.perf_counter", return_value=10.5):
            timer.mark_playback_started()
            timer.mark_playback_started()
        self.assertEqual(metrics.first_playback_ms, 500)
        self.assertEqual(metrics.eos_to_first_audio_ms, 700)

    def test_summary_percentiles_and_gather_nulls(self):
        transcript = [
            {"metrics": {"handler_ms": 100, "eos_to_first_audio_ms": 500}},
            {"metrics": {"handler_ms": 200, "eos_to_first_audio_ms": 700}},
            {"metrics": {"handler_ms": 300, "eos_to_first_audio_ms": 900}},
        ]
        summary = compute_latency_summary(transcript)
        self.assertEqual(summary["p50_eos_to_first_audio_ms"], 700)
        self.assertEqual(summary["p95_eos_to_first_audio_ms"], 900)

        gather = TurnMetrics(path="twilio_gather").to_dict()
        self.assertIsNone(gather["endpointing_ms"])
        self.assertIsNone(gather["eos_to_first_audio_ms"])


class StreamingInterruptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_interruption_stops_remaining_playback(self):
        stopped = False
        played = []

        async def synthesize(text):
            return text.encode()

        async def play(audio, _finalize):
            nonlocal stopped
            played.append(audio)
            stopped = True

        result = await speak_streaming(
            text="First sentence. Second sentence.",
            synthesize=synthesize,
            play=play,
            should_stop=lambda: stopped,
        )
        self.assertTrue(result["interrupted"])
        self.assertEqual(len(played), 1)


class PersistenceTests(unittest.TestCase):
    def test_live_snapshot_does_not_end_or_enqueue_call(self):
        call_id = "snapshot-test"
        call_records[call_id] = {"call_id": call_id, "transcript": []}
        engine = object.__new__(ConversationEngine)
        try:
            with (
                patch("services.conversation_engine.save_call_json"),
                patch(
                    "services.conversation_state_db.persist_call_result_from_record"
                ),
                patch("worker.celery_app.enqueue_call_finished") as enqueue,
            ):
                engine._persist_call(call_id, final=False)
            self.assertNotIn("ended_at", call_records[call_id])
            enqueue.assert_not_called()
        finally:
            call_records.pop(call_id, None)


if __name__ == "__main__":
    unittest.main()
