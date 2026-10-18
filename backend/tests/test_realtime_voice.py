"""LiveKit orchestration, tone delivery, call summary, and chunked TTS."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.call_summary import apply_call_summary
from services.elevenlabs_service import voice_settings_for_call
from services.livekit_session import build_livekit_token, open_livekit_session, public_livekit_status
from services.smart_tone import detect_smart_tone
from services.streaming_tts import play_incoming_ulaw
from services.conversation_engine import CallSession, ConversationEngine


class ToneAndSummaryTests(unittest.TestCase):
    def test_frustration_sets_delivery_without_changing_claim_facts(self):
        result = detect_smart_tone(
            "This is ridiculous, I am frustrated and the claim should be approved.",
            enabled=True,
        )
        self.assertEqual(result["tone"], "frustrated")
        self.assertEqual(result["facts"], {})
        self.assertFalse(result["changes_claim_facts"])

        session = CallSession(call_id="tone-on")
        session.resolution_code = "under_review"
        with patch(
            "services.conversation_engine.get_settings",
            return_value=SimpleNamespace(smart_tone_detection_enabled=True),
        ):
            ConversationEngine._apply_tone_signal(session, "I am frustrated with this.", None)
        self.assertEqual(session.delivery_emotion, "frustrated")
        self.assertEqual(session.resolution_code, "under_review")

    def test_summary_keeps_recordings_and_names_follow_ups(self):
        record = {
            "claim_id": "CLM-9",
            "resolution_code": "pending",
            "follow_up_date": "2026-10-20",
            "pending_documents": ["discharge summary"],
            "recording_url": "https://api.twilio.com/recording-1",
            "recordings": ["https://api.twilio.com/recording-1"],
            "transcript": [
                {"role": "customer", "text": "It is still pending."},
                {"role": "agent", "text": "Thanks, who is reviewing it?"},
            ],
        }
        built = apply_call_summary(record)
        self.assertIn("Outcome: pending", built["summary"])
        self.assertIn("2026-10-20", " ".join(built["follow_up_actions"]))
        self.assertIn("discharge summary", " ".join(built["follow_up_actions"]))
        self.assertEqual(record["recording_url"], "https://api.twilio.com/recording-1")
        self.assertEqual(len(record["transcript"]), 2)


class LiveKitTests(unittest.TestCase):
    def test_verified_room_stays_on_twilio_and_hides_the_secret(self):
        secret = "super-secret-value"

        async def _ok(_settings, room):
            return {"name": room, "sid": "RM1"}

        async def _run():
            return await open_livekit_session(
                SimpleNamespace(
                    livekit_url="wss://example.livekit.cloud",
                    livekit_api_key="key",
                    livekit_api_secret=secret,
                ),
                room_name="call-1",
                connector=_ok,
            )

        result = asyncio.run(_run())
        public = public_livekit_status(result)
        self.assertTrue(public["connected"])
        self.assertEqual(public["room"], "call-1")
        self.assertEqual(public["phone_path"], "twilio")
        self.assertNotIn(secret, str(public))

    def test_access_token_does_not_contain_the_secret(self):
        secret = "super-secret-value"
        token = build_livekit_token(secret and "api-key", secret, identity="agent", room="call-1")
        self.assertNotIn(secret, token)
        self.assertEqual(len(token.split(".")), 3)


class ChunkPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_frames_start_before_the_provider_finishes(self):
        sent: list[bytes] = []

        async def chunks():
            yield bytes([1, 2, 3]) * 300
            yield bytes([4, 5, 6]) * 300

        async def send(frame: bytes) -> None:
            sent.append(frame)

        result = await play_incoming_ulaw(chunks(), send)
        self.assertGreaterEqual(result["frames"], 1)
        self.assertGreaterEqual(len(sent[0]), 640)

    def test_flash_voice_settings_stay_conversational(self):
        settings = SimpleNamespace(
            elevenlabs_stability=0.42,
            elevenlabs_similarity=0.78,
            elevenlabs_style=0.18,
            elevenlabs_speed=1.0,
            elevenlabs_speaker_boost=True,
            elevenlabs_model_id="eleven_flash_v2_5",
        )
        with patch("services.elevenlabs_service.get_settings", return_value=settings):
            voice = voice_settings_for_call("frustrated")
        self.assertLessEqual(voice["style"], 0.08)
        self.assertGreaterEqual(voice["stability"], 0.4)
        self.assertLessEqual(voice["speed"], 1.03)
