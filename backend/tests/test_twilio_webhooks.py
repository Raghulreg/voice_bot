import asyncio
import time
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from main import app
from routes import webhooks
from services.conversation_engine import (
    GATHER_WEBHOOK_BUDGET_S,
    CallSession,
    ConversationEngine,
    call_records,
    call_sessions,
)


BASE = "https://example.ngrok-free.dev"


def _twiml(action: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Gather action="{action}" method="POST"/></Response>'
    )


class BoundedGatherTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_reply_returns_hold_then_completed_twiml(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        session = CallSession(call_id="slow", public_base=BASE)
        final = _twiml(f"{BASE}/api/webhooks/conversation?call_id=slow")

        async def slow_reply(*_args):
            await asyncio.sleep(0.04)
            return final

        engine.build_speech_response_twiml = slow_reply
        with patch("services.conversation_engine.GATHER_WEBHOOK_BUDGET_S", 0.01):
            first = await engine.build_bounded_speech_response_twiml(
                session, "Pending.", BASE
            )

        self.assertIn("/api/webhooks/conversation-continue?call_id=slow", first)
        self.assertIn('method="POST"', first)
        root = ET.fromstring(first)
        self.assertIsNone(root.find("Say"))
        self.assertIsNone(root.find("Play"))
        self.assertNotIn("One moment", first)
        self.assertIsNotNone(session.pending_twiml)

        await asyncio.sleep(0.05)
        completed = await engine.complete_pending_twiml(session, BASE)
        self.assertEqual(completed, final)
        self.assertIsNone(session.pending_twiml)

    async def test_slow_greeting_returns_a_silent_hold_before_the_drop_window(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        session = CallSession(call_id="slow-greet", public_base=BASE)

        async def slow_intro(*_args):
            await asyncio.sleep(30)
            return "<Response><Play>https://x.test/audio</Play></Response>"

        engine.build_intro_twiml = slow_intro
        started = time.perf_counter()
        xml = await engine.build_bounded_intro_twiml(session, BASE)
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, GATHER_WEBHOOK_BUDGET_S + 0.75)
        self.assertLess(elapsed, 4.5)
        root = ET.fromstring(xml)
        self.assertIsNotNone(root.find("Pause"))
        self.assertIsNone(root.find("Play"))
        self.assertIsNone(root.find("Say"))
        self.assertIn("/api/webhooks/conversation-continue?call_id=slow-greet", xml)
        task = session.pending_twiml["task"]
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_slow_continuation_remains_bounded(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        session = CallSession(call_id="still-slow", public_base=BASE)

        async def very_slow():
            await asyncio.sleep(0.1)
            return "<Response/>"

        task = asyncio.create_task(very_slow())
        session.pending_twiml = {"task": task, "base_url": BASE}
        with patch("services.conversation_engine.GATHER_CONTINUE_BUDGET_S", 0.01):
            twiml = await engine.complete_pending_twiml(session, BASE)

        self.assertIn("/api/webhooks/conversation-continue?call_id=still-slow", twiml)
        self.assertFalse(task.cancelled())
        root = ET.fromstring(twiml)
        self.assertIsNone(root.find("Say"))
        self.assertNotIn("still checking", twiml.lower())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_hold_cap_abandons_reply_and_listens_silently(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        session = CallSession(call_id="capped", public_base=BASE, preferred_language="en-US")

        async def never():
            await asyncio.sleep(10)

        task = asyncio.create_task(never())
        session.pending_twiml = {"task": task, "base_url": BASE, "holds": 0}
        with patch("services.conversation_engine.GATHER_CONTINUE_BUDGET_S", 0.001), patch(
            "services.conversation_engine.GATHER_MAX_PENDING_HOLDS", 2
        ):
            first = await engine.complete_pending_twiml(session, BASE)
            second = await engine.complete_pending_twiml(session, BASE)
        await asyncio.sleep(0)

        self.assertIn("conversation-continue", first)
        self.assertIsNotNone(ET.fromstring(second).find("Gather"))
        self.assertNotIn("<Say", second)
        self.assertIsNone(session.pending_twiml)
        self.assertTrue(task.cancelled())

    async def test_failed_deferred_reply_listens_instead_of_hanging_up(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        session = CallSession(call_id="failed", public_base=BASE, preferred_language="en-US")

        async def boom():
            raise RuntimeError("provider down")

        task = asyncio.create_task(boom())
        await asyncio.sleep(0)
        session.pending_twiml = {"task": task, "base_url": BASE, "holds": 0}
        twiml = await engine.complete_pending_twiml(session, BASE)
        root = ET.fromstring(twiml)
        self.assertIsNotNone(root.find("Gather"))
        self.assertIsNone(root.find("Hangup"))
        self.assertIsNone(session.pending_twiml)

    async def test_slow_intro_returns_silent_redirect_then_greeting(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        session = CallSession(call_id="intro", public_base=BASE)
        greeting = _twiml(f"{BASE}/api/webhooks/conversation?call_id=intro")

        async def slow_intro(*_args):
            await asyncio.sleep(0.04)
            return greeting

        engine.build_intro_twiml = slow_intro
        with patch("services.conversation_engine.GATHER_WEBHOOK_BUDGET_S", 0.01):
            first = await engine.build_bounded_intro_twiml(session, BASE)
        self.assertNotIn("<Say", first)
        self.assertIn("conversation-continue", first)
        await asyncio.sleep(0.05)
        self.assertEqual(await engine.complete_pending_twiml(session, BASE), greeting)

    async def test_scenario_warmup_skips_unused_scripted_reply_cache(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        engine.precache_intro = AsyncMock()
        engine.precache_backchannels = AsyncMock()
        engine.precache_replies = AsyncMock()
        session = CallSession(call_id="scenario", tpa_executive_profile="Helpful Executive")

        with patch(
            "services.conversation_engine.session_uses_scenario_mode",
            return_value=True,
        ):
            await engine.warm_session_cache(session)

        engine.precache_intro.assert_awaited_once()
        engine.precache_backchannels.assert_awaited_once()
        engine.precache_replies.assert_not_awaited()

    async def test_scripted_warmup_still_precaches_replies(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        engine.precache_intro = AsyncMock()
        engine.precache_backchannels = AsyncMock()
        engine.precache_replies = AsyncMock()
        session = CallSession(call_id="scripted")

        with patch(
            "services.conversation_engine.session_uses_scenario_mode",
            return_value=False,
        ):
            await engine.warm_session_cache(session)
            await asyncio.sleep(0)

        engine.precache_replies.assert_awaited_once_with(session)

    async def test_cached_greeting_is_not_generated_again(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        session = CallSession(call_id="cached-greet")
        session.phrase_cache["greeting"] = 1
        engine._gemini_greeting = AsyncMock()
        await engine.precache_intro(session)
        engine._gemini_greeting.assert_not_called()

    async def test_greeting_prepare_finishes_before_a_second_warmup(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        session = CallSession(call_id="prep")
        calls = {"n": 0}

        async def intro(active):
            calls["n"] += 1
            active.phrase_cache["greeting"] = 3

        engine.precache_intro = intro
        engine.precache_backchannels = AsyncMock()
        engine.precache_replies = AsyncMock()
        ready = await engine.prepare_greeting_before_dial(session)
        self.assertTrue(ready)
        with patch(
            "services.conversation_engine.session_uses_scenario_mode",
            return_value=True,
        ):
            await engine.warm_session_cache(session)
        self.assertEqual(calls["n"], 1)
        engine.precache_backchannels.assert_awaited_once()

    async def test_slow_greeting_prepare_returns_without_cancelling_the_work(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        session = CallSession(call_id="slow-prep")

        async def intro(_session):
            await asyncio.sleep(30)
            _session.phrase_cache["greeting"] = 1

        engine.precache_intro = intro
        with patch("services.conversation_engine.GREETING_PREPARE_BUDGET_S", 0.01):
            ready = await engine.prepare_greeting_before_dial(session)
        self.assertFalse(ready)
        task = session.greeting_prepare_task
        self.assertFalse(task.done())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_missing_tts_configuration_does_not_name_elevenlabs(self):
        from routes.calls import _initiate_media_streams_call

        settings = type(
            "Settings",
            (),
            {
                "twilio_account_sid": "AC-test",
                "twilio_phone_number": "+10000000000",
                "gemini_api_key": "present",
            },
        )()
        with patch("routes.calls.get_settings", return_value=settings), patch(
            "routes.calls.SttService"
        ) as stt_cls, patch("routes.calls.TtsService") as tts_cls:
            stt_cls.return_value.is_configured.return_value = True
            tts_cls.return_value.is_configured.return_value = False
            with self.assertRaises(HTTPException) as caught:
                await _initiate_media_streams_call(object(), "+10000000000")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn("ELEVENLABS", caught.exception.detail.upper())
        self.assertIn("TTS", caught.exception.detail)


class TwilioWebhookRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        call_sessions.clear()
        call_records.clear()

    def tearDown(self):
        call_sessions.clear()
        call_records.clear()

    def test_voice_returns_parseable_xml_with_post_gather(self):
        session = CallSession(call_id="voice", public_base=BASE)
        call_sessions[session.call_id] = session
        call_records[session.call_id] = {"call_id": session.call_id, "transcript": []}
        xml = _twiml(f"{BASE}/api/webhooks/conversation?call_id=voice")

        with (
            patch("services.voice_runtime.cached_twilio_account_type", return_value="Trial"),
            patch(
                "services.voice_runtime.select_voice_transport",
                return_value={"active": "gather"},
            ),
            patch.object(
                webhooks.conversation_engine,
                "build_intro_twiml",
                new=AsyncMock(return_value=xml),
            ),
        ):
            response = self.client.post("/api/webhooks/voice?call_id=voice")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("application/xml"))
        root = ET.fromstring(response.text)
        gather = root.find("Gather")
        self.assertIsNotNone(gather)
        self.assertEqual(gather.attrib["method"], "POST")
        self.assertTrue(gather.attrib["action"].startswith(BASE))

    def test_conversation_posts_to_bounded_handler_and_returns_xml(self):
        session = CallSession(call_id="turn", public_base=BASE, greeting_done=True)
        call_sessions[session.call_id] = session
        xml = _twiml(f"{BASE}/api/webhooks/conversation?call_id=turn")

        with (
            patch.object(webhooks.conversation_engine, "begin_customer_turn"),
            patch.object(
                webhooks.conversation_engine,
                "build_bounded_speech_response_twiml",
                new=AsyncMock(return_value=xml),
            ) as bounded,
        ):
            response = self.client.post(
                "/api/webhooks/conversation?call_id=turn",
                data={"SpeechResult": "Pending.", "Confidence": "0.9"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("application/xml"))
        ET.fromstring(response.text)
        bounded.assert_awaited_once_with(session, "Pending.", BASE)

    def test_continuation_returns_parseable_xml(self):
        session = CallSession(call_id="continue", public_base=BASE)
        call_sessions[session.call_id] = session
        xml = _twiml(f"{BASE}/api/webhooks/conversation?call_id=continue")

        with patch.object(
            webhooks.conversation_engine,
            "complete_pending_twiml",
            new=AsyncMock(return_value=xml),
        ):
            response = self.client.post(
                "/api/webhooks/conversation-continue?call_id=continue"
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("application/xml"))
        ET.fromstring(response.text)

    def test_unknown_session_returns_valid_error_twiml(self):
        with patch("services.conversation_engine._twilio_say_voice", return_value=""):
            response = self.client.post(
                "/api/webhooks/conversation?call_id=does-not-exist",
                data={"SpeechResult": "Hello"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("application/xml"))
        root = ET.fromstring(response.text)
        self.assertIsNotNone(root.find("Hangup"))
        self.assertIsNone(root.find("Say"))

    def test_error_twiml_uses_only_the_configured_say_voice(self):
        with patch("services.conversation_engine._twilio_say_voice", return_value="Polly.Joanna"):
            xml = webhooks._error_twiml("Sorry.")
        say = ET.fromstring(xml).find("Say")
        self.assertIsNotNone(say)
        self.assertEqual(say.attrib["voice"], "Polly.Joanna")

    def test_agent_audio_fetch_records_playback_telemetry(self):
        session = CallSession(call_id="fetch", public_base=BASE)
        call_sessions[session.call_id] = session
        metrics = {"webhook_to_audio_fetch_ms": None}
        session.audio_turn_metrics[2] = (metrics, __import__("time").time() - 1.5)
        with patch.object(
            webhooks.conversation_engine,
            "ensure_playback_audio",
            new=AsyncMock(return_value=b"RIFF-test"),
        ):
            self.client.get("/api/webhooks/agent-audio?call_id=fetch&turn=2")
        self.assertGreaterEqual(metrics["webhook_to_audio_fetch_ms"], 1400)
        self.assertNotIn(2, session.audio_turn_metrics)

    def test_conversation_get_is_not_accepted(self):
        response = self.client.get("/api/webhooks/conversation?call_id=unused")
        self.assertEqual(response.status_code, 405)

    def test_agent_audio_returns_wav(self):
        session = CallSession(call_id="audio", public_base=BASE)
        call_sessions[session.call_id] = session
        with patch.object(
            webhooks.conversation_engine,
            "ensure_playback_audio",
            new=AsyncMock(return_value=b"RIFF-test"),
        ):
            response = self.client.get(
                "/api/webhooks/agent-audio?call_id=audio&turn=1"
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("audio/wav"))

    def test_status_callback_returns_json_ack(self):
        call_records["status"] = {"call_id": "status", "transcript": []}
        response = self.client.post(
            "/api/webhooks/status?call_id=status",
            data={"CallStatus": "ringing", "CallSid": "test-sid"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})

    def test_session_public_base_wins_over_changed_environment(self):
        session = CallSession(call_id="fixed", public_base=BASE)
        with patch.object(
            webhooks,
            "resolve_public_url",
            return_value=("https://stale.example", "env"),
        ):
            self.assertEqual(webhooks._public_base(session), BASE)


if __name__ == "__main__":
    unittest.main()
