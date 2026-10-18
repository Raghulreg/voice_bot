import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.deepgram_stt import (
    DeepgramLiveSession,
    _public_error,
    build_listen_url,
    parse_deepgram_message,
)
from services.audio_stream_pipeline import LiveAudioPipeline
from services.elevenlabs_service import configured_model_candidates
from services.livekit_session import open_livekit_session
from services.model_routing import route_model
from services.smart_tone import detect_smart_tone
from services.silero_vad import SileroVadStream
from services.stt_service import StreamingSttSession
from services.turn_telemetry import compute_latency_summary
from services.conversation_engine import CallSession, ConversationEngine
from services.voice_runtime import (
    VoiceConfigError,
    account_supports_media_streams,
    media_stream_stt_choice,
    resolve_voice_runtime,
    select_voice_transport,
)


class _ScriptedSocket:
    def __init__(self) -> None:
        self.sent: list[bytes | str] = []
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def send(self, data):
        self.sent.append(data)
        if isinstance(data, str) and "Finalize" in data:
            await self.incoming.put(
                json.dumps(
                    {
                        "type": "Results",
                        "is_final": True,
                        "speech_final": False,
                        "from_finalize": True,
                        "start": 0.0,
                        "duration": 0.4,
                        "channel": {
                            "alternatives": [
                                {
                                    "transcript": "under review",
                                    "confidence": 0.88,
                                    "words": [{"word": "under", "start": 0.0, "end": 0.2}],
                                }
                            ]
                        },
                    }
                )
            )

    async def close(self):
        await self.incoming.put(None)


def _settings(**overrides):
    values = {
        "call_provider": "media_streams",
        "deepgram_api_key": "",
        "deepgram_model": "nova-3",
        "livekit_url": "",
        "livekit_api_key": "",
        "livekit_api_secret": "",
        "elevenlabs_api_key": "present",
        "elevenlabs_model_id": "eleven_turbo_v2_5",
        "gemini_model": "gemini-primary",
        "gemini_simple_model": "",
        "vad_enabled": True,
        "vad_provider": "silero",
        "smart_tone_detection_enabled": False,
        "voice_transport": "gather",
        "voice_streaming_enabled": True,
        "voice_latency_target_ms": 1200,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class VoiceRuntimeConfigTests(unittest.TestCase):
    def test_default_transport_is_gather(self):
        choice = select_voice_transport(_settings(), account_type="Trial")
        self.assertEqual(choice["active"], "gather")
        self.assertFalse(choice["fallback"])
        self.assertFalse(account_supports_media_streams("Trial"))

    def test_media_streams_on_trial_falls_back_to_gather(self):
        choice = select_voice_transport(
            _settings(voice_transport="media_streams", deepgram_api_key="test-key"),
            account_type="Trial",
        )
        self.assertEqual(choice["requested"], "media_streams")
        self.assertEqual(choice["active"], "gather")
        self.assertTrue(choice["fallback"])
        self.assertIn("trial", choice["reason"].lower())
        status = resolve_voice_runtime(
            _settings(voice_transport="media_streams", deepgram_api_key="test-key"),
            account_type="Trial",
        )
        self.assertEqual(status["active_phone_path"], "twilio_gather")
        self.assertFalse(status["deepgram"]["selected_on_active_path"])

    def test_media_streams_on_full_account_is_selected(self):
        choice = select_voice_transport(
            _settings(voice_transport="media_streams", deepgram_api_key="test-key"),
            account_type="Full",
        )
        self.assertEqual(choice["active"], "media_streams")
        self.assertFalse(choice["fallback"])
        status = resolve_voice_runtime(
            _settings(voice_transport="media_streams", deepgram_api_key="test-key"),
            account_type="Full",
        )
        self.assertEqual(status["active_stt"], "deepgram")
        self.assertTrue(status["deepgram"]["selected_on_active_path"])
        self.assertFalse(status["deepgram"]["operational"])

    def test_full_account_without_deepgram_stays_on_gather(self):
        choice = select_voice_transport(
            _settings(voice_transport="media_streams", deepgram_api_key=""),
            account_type="Full",
        )
        self.assertEqual(choice["active"], "gather")
        self.assertTrue(choice["fallback"])
        self.assertIn("DEEPGRAM_API_KEY", choice["reason"])

    def test_unknown_transport_is_rejected(self):
        with self.assertRaises(VoiceConfigError) as raised:
            select_voice_transport(_settings(voice_transport="livekit"), account_type="Full")
        self.assertIn("VOICE_TRANSPORT", str(raised.exception))

    def test_media_stream_twiml_points_at_the_websocket(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        session = CallSession(call_id="c1")
        xml = engine.build_media_stream_twiml(session, "https://example.test")
        self.assertIn("<Connect>", xml)
        self.assertIn("<Stream", xml)
        self.assertIn("wss://example.test/api/webhooks/media-stream?call_id=c1", xml)
        self.assertNotIn("<Gather", xml)

    def test_missing_deepgram_key_keeps_gather_and_existing_stt(self):
        status = resolve_voice_runtime(_settings())
        self.assertEqual(status["active_phone_path"], "twilio_gather")
        self.assertEqual(status["active_stt"], "twilio_gather")
        self.assertTrue(status["gather_fallback"])
        self.assertFalse(status["deepgram"]["operational"])
        self.assertEqual(status["deepgram"]["missing"], ["DEEPGRAM_API_KEY"])
        self.assertEqual(status["deepgram"]["media_stream_stt"], "existing")
        self.assertFalse(status["streaming"]["operational_on_active_path"])

    def test_deepgram_key_does_not_replace_gather(self):
        settings = _settings(deepgram_api_key="test-key")
        status = resolve_voice_runtime(settings)
        self.assertEqual(status["active_stt"], "twilio_gather")
        self.assertFalse(status["deepgram"]["selected_on_active_path"])
        self.assertEqual(media_stream_stt_choice(settings), "deepgram")
        self.assertNotIn("test-key", str(status))

    def test_empty_deepgram_model_is_rejected_when_key_is_set(self):
        with self.assertRaises(VoiceConfigError) as raised:
            media_stream_stt_choice(_settings(deepgram_api_key="test-key", deepgram_model=""))
        self.assertIn("DEEPGRAM_MODEL", str(raised.exception))
        self.assertNotIn("test-key", str(raised.exception))

    def test_deepgram_session_reports_missing_credentials(self):
        session = DeepgramLiveSession(_settings(deepgram_api_key="", deepgram_model="nova-3"))
        with self.assertRaises(VoiceConfigError) as raised:
            session.require_config()
        self.assertIn("DEEPGRAM_API_KEY", str(raised.exception))

    def test_listen_url_uses_configured_model_and_does_not_cut_pauses(self):
        url = build_listen_url("nova-3")
        self.assertIn("model=nova-3", url)
        self.assertIn("interim_results=true", url)
        self.assertIn("endpointing=false", url)
        self.assertNotIn("api_key", url)

    def test_parser_keeps_interim_final_confidence_and_boundaries(self):
        parsed = parse_deepgram_message(
            {
                "type": "Results",
                "is_final": True,
                "speech_final": True,
                "start": 1.0,
                "duration": 0.8,
                "channel": {
                    "alternatives": [
                        {
                            "transcript": "under review",
                            "confidence": 0.91,
                            "words": [{"word": "under", "start": 1.0, "end": 1.3, "confidence": 0.9}],
                        }
                    ]
                },
            }
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertTrue(parsed.is_final)
        self.assertTrue(parsed.speech_final)
        self.assertEqual(parsed.transcript, "under review")
        self.assertEqual(parsed.confidence, 0.91)
        self.assertEqual(parsed.end, 1.8)
        self.assertEqual(parsed.words[0]["word"], "under")

    def test_error_text_redacts_the_api_key(self):
        text = _public_error({"description": "bad secret-value"}, "secret-value")
        self.assertNotIn("secret-value", text)
        self.assertIn("[redacted]", text)

    def test_begin_utterance_drops_the_previous_transcript(self):
        session = DeepgramLiveSession(_settings(deepgram_api_key="secret-value"))
        session._latest_final = "claim approved"
        session._final_parts = ["claim approved"]
        session.begin_utterance()
        self.assertEqual(session._latest_final, "")
        self.assertEqual(session._final_parts, [])

    def test_longer_final_replaces_the_shorter_revision(self):
        session = DeepgramLiveSession(_settings(deepgram_api_key="secret-value"))
        session._remember_final("claim number")
        session._remember_final("claim number please")
        session._remember_final("Which payer is this?")
        self.assertEqual(
            session._latest_final,
            "claim number please Which payer is this?",
        )

    def test_connect_timeout_does_not_expose_the_key(self):
        async def _hang(_url, _headers):
            await asyncio.sleep(5)

        async def _run():
            session = DeepgramLiveSession(_settings(deepgram_api_key="secret-value"))
            with patch("services.deepgram_stt._open_socket", _hang):
                await session.connect(timeout=0.05)

        with self.assertRaises(VoiceConfigError) as raised:
            asyncio.run(_run())
        self.assertIn("timed out", str(raised.exception))
        self.assertNotIn("secret-value", str(raised.exception))

    def test_finalize_waits_for_nova_result_and_clears_it(self):
        async def _run():
            session = DeepgramLiveSession(_settings(deepgram_api_key="secret-value"))
            socket = _ScriptedSocket()
            session._ws = socket
            session.model = "nova-3"
            session._reader = asyncio.create_task(session._read())
            session._sender = asyncio.create_task(session._send())
            session.begin_utterance()
            text = await session.flush_final(timeout=1)
            await session.close()
            return text, socket.sent

        text, sent = asyncio.run(_run())
        self.assertEqual(text, "under review")
        self.assertTrue(any(isinstance(item, str) and "Finalize" in item for item in sent))
        self.assertNotIn("secret-value", text)

    def test_finalize_timeout_rejects_incomplete_deepgram_text(self):
        async def _run():
            session = DeepgramLiveSession(_settings(deepgram_api_key="secret-value"))
            session._ws = SimpleNamespace()
            session._latest_final = "claim approved"
            session._final_parts = ["claim approved"]
            return await session.flush_final(timeout=0.01)

        self.assertEqual(asyncio.run(_run()), "")

    def test_queue_overflow_degrades_only_the_current_utterance(self):
        session = DeepgramLiveSession(_settings(deepgram_api_key="secret-value"))
        session._ws = SimpleNamespace()
        session._out = asyncio.Queue(maxsize=1)
        session._out.put_nowait(b"first")

        session.feed(b"second")

        self.assertFalse(session.failed)
        self.assertTrue(session.utterance_degraded)
        self.assertEqual(session.safe_error, "audio_queue_overflow")

    def test_provider_failure_message_has_no_secret(self):
        message = str(
            VoiceConfigError(
                "Deepgram connection failed. Twilio Gather remains the phone speech path. "
                "Cause: ConnectionClosed."
            )
        )
        self.assertIn("Twilio Gather", message)
        self.assertNotIn("Token", message)

    def test_missing_livekit_credentials_block_the_session(self):
        async def _run():
            await open_livekit_session(_settings())

        with self.assertRaises(VoiceConfigError) as raised:
            import asyncio

            asyncio.run(_run())
        text = str(raised.exception)
        self.assertIn("LIVEKIT_URL", text)
        self.assertIn("LIVEKIT_API_KEY", text)
        self.assertIn("LIVEKIT_API_SECRET", text)
        self.assertIn("Twilio Gather", text)

    def test_livekit_credentials_stay_on_twilio_when_the_room_is_not_verified(self):
        async def _fail(_settings_obj, _room):
            raise TimeoutError("timed out")

        async def _run():
            return await open_livekit_session(
                _settings(
                    livekit_url="wss://example.livekit.cloud",
                    livekit_api_key="key",
                    livekit_api_secret="secret",
                ),
                connector=_fail,
            )

        result = asyncio.run(_run())
        self.assertFalse(result["connected"])
        self.assertEqual(result["phone_path"], "twilio")
        self.assertIn("Twilio", result["reason"])
        self.assertNotIn("secret", str(result))

    def test_flash_is_not_selected_while_turbo_is_configured(self):
        status = resolve_voice_runtime(_settings())
        self.assertEqual(status["tts"]["provider"], "windows_sapi")
        self.assertEqual(status["tts"]["voice"], "Microsoft David Desktop")
        self.assertEqual(status["tts"]["elevenlabs_model"], "eleven_turbo_v2_5")
        self.assertFalse(status["tts"]["flash_v2_5_selected"])
        self.assertEqual(status["tts"]["fallback"], "none")
        self.assertFalse(status["tts"]["twilio_say_fallback_configured"])
        self.assertFalse(status["tts"]["operational"])
        self.assertEqual(
            configured_model_candidates("eleven_turbo_v2_5"),
            ["eleven_turbo_v2_5"],
        )
        self.assertNotIn(
            "eleven_flash_v2_5",
            configured_model_candidates("eleven_turbo_v2_5"),
        )

    def test_empty_simple_model_stays_on_the_primary_model(self):
        status = resolve_voice_runtime(_settings())
        self.assertFalse(status["llm"]["simple_model_active"])
        route = route_model("It is under review.", "claim_status", True, "gemini-primary", "")
        self.assertEqual(route.model, "gemini-primary")

    def test_smart_tone_disabled_does_not_change_facts(self):
        result = detect_smart_tone("I am frustrated and the claim should be approved.", enabled=False)
        self.assertFalse(result["operational"])
        self.assertIsNone(result["tone"])
        self.assertEqual(result["facts"], {})
        self.assertFalse(result["changes_claim_facts"])

    def test_enabled_smart_tone_still_does_not_invent_a_decision(self):
        result = detect_smart_tone("Yes, correct that thing.", enabled=True)
        self.assertTrue(result["operational"])
        self.assertIsNone(result["tone"])
        self.assertEqual(result["facts"], {})
        self.assertFalse(result["changes_claim_facts"])

    def test_disabled_tone_does_not_change_delivery_or_claim_state(self):
        session = CallSession(call_id="tone-off")
        session.delivery_emotion = ""
        session.resolution_code = "under_review"

        with patch(
            "services.conversation_engine.get_settings",
            return_value=SimpleNamespace(smart_tone_detection_enabled=False),
        ):
            ConversationEngine._apply_tone_signal(
                session,
                "I am frustrated; approve it.",
                "frustrated",
            )

        self.assertEqual(session.delivery_emotion, "")
        self.assertEqual(session.resolution_code, "under_review")

    def test_latency_slo_excludes_payer_speaking_time(self):
        summary = compute_latency_summary(
            [
                {
                    "metrics": {
                        "handler_ms": 100,
                        "input_wait_ms": 8000,
                        "eos_to_first_audio_ms": 500,
                        "stt_ms": 40,
                        "llm_ms": 200,
                        "first_llm_token_ms": 80,
                        "tts_ms": 90,
                        "first_tts_chunk_ms": 30,
                        "twilio_delay_ms": 20,
                    }
                },
                {
                    "metrics": {
                        "input_wait_ms": 9000,
                        "eos_to_first_audio_ms": 900,
                    }
                },
            ]
        )
        slo = summary["latency_slo"]
        self.assertEqual(slo["metric"], "eos_to_first_audio_ms")
        self.assertEqual(slo["target_ms"], 1200)
        self.assertIn("input_wait_ms", slo["excludes"])
        self.assertEqual(slo["p50_ms"], 900)
        self.assertIsNone(slo["met"])
        self.assertNotEqual(slo["p50_ms"], summary["p50_input_wait_ms"])


class StreamingTranscriptSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_final_never_promotes_interim_caption(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        engine.handle_stream_speech = AsyncMock()
        engine.stt = SimpleNamespace(last_error="", last_provider="", model_name="")
        session = CallSession(call_id="stale-partial")
        session.live_partial_transcript = "claim approved"

        await engine.process_stream_utterance(session, "")

        engine.handle_stream_speech.assert_not_awaited()
        self.assertEqual(session.turn_count, 0)

    async def test_failed_final_does_not_promote_interim_caption(self):
        stt = SimpleNamespace(
            transcribe_mulaw=AsyncMock(return_value=""),
            last_provider="mock",
        )
        on_partial = AsyncMock()
        session = StreamingSttSession(
            stt=stt,
            call_id="stale-partial",
            on_partial=on_partial,
        )
        session.partial_text = "claim approved"
        session.buffer.extend(bytes([0xFF]) * 1600)

        final = await session.finalize()

        self.assertEqual(final, "")
        on_partial.assert_not_awaited()

    async def test_fallback_stt_buffers_without_duplicate_partials(self):
        stt = SimpleNamespace(transcribe_mulaw=AsyncMock(), last_provider="mock")
        on_partial = AsyncMock()
        session = StreamingSttSession(
            stt=stt,
            call_id="deepgram-primary",
            on_partial=on_partial,
        )

        session.feed(bytes([0xFF]) * 5000, emit_partials=False)
        await asyncio.sleep(0)

        self.assertEqual(len(session.buffer), 5000)
        stt.transcribe_mulaw.assert_not_awaited()
        on_partial.assert_not_awaited()

    async def test_multiple_final_utterances_are_queued_in_order(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        engine.handle_partial_transcript = AsyncMock()
        engine.handle_barge_in = AsyncMock()
        session = CallSession(call_id="queued-finals")
        session.is_processing = True

        await engine.process_stream_utterance(session, "first final")
        await engine.process_stream_utterance(session, "second final")

        self.assertEqual(
            session.pending_utterances,
            ["first final", "second final"],
        )

    async def test_deepgram_final_metadata_is_retained_for_validation(self):
        pipeline = LiveAudioPipeline.__new__(LiveAudioPipeline)
        pipeline.last_stt_confidence = None
        pipeline.last_stt_start = None
        pipeline.last_stt_duration = None
        pipeline._emit_partial = AsyncMock()
        result = SimpleNamespace(
            transcript="under review",
            is_final=True,
            confidence=0.91,
            start=1.2,
            duration=0.8,
        )

        await pipeline._on_deepgram_transcript(result)

        self.assertEqual(pipeline.last_stt_confidence, 0.91)
        self.assertEqual(pipeline.last_stt_start, 1.2)
        self.assertEqual(pipeline.last_stt_duration, 0.8)
        pipeline._emit_partial.assert_awaited_once_with("under review", True)


class VadSafetyTests(unittest.TestCase):
    @staticmethod
    def _fallback_vad() -> SileroVadStream:
        vad = SileroVadStream.__new__(SileroVadStream)
        vad._available = False
        vad._iterator = None
        vad._pcm = []
        vad._speaking = False
        vad._last_prob = 0.0
        vad._low_prob_windows = 0
        return vad

    def test_silence_does_not_start_speech(self):
        vad = self._fallback_vad()
        result = vad.push_mulaw(bytes([0xFF]) * 160)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.speaking)
        self.assertEqual(result.event, "")

    def test_energy_fallback_starts_and_ends_short_utterance(self):
        try:
            import audioop
        except ImportError:
            import audioop_lts as audioop
        loud_pcm = (12000).to_bytes(2, "little", signed=True) * 160
        loud_mulaw = audioop.lin2ulaw(loud_pcm, 2)
        vad = self._fallback_vad()

        start = vad.push_mulaw(loud_mulaw)
        end = vad.push_mulaw(bytes([0xFF]) * 160)

        assert start is not None and end is not None
        self.assertEqual(start.event, "speech_start")
        self.assertEqual(end.event, "speech_end")
        self.assertFalse(end.speaking)

    def test_reset_clears_session_state(self):
        vad = self._fallback_vad()
        vad._speaking = True
        vad._pcm = [0.1, 0.2]
        vad.reset()
        self.assertFalse(vad.speaking)
        self.assertEqual(vad._pcm, [])


if __name__ == "__main__":
    unittest.main()
