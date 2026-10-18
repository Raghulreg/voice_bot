"""Primary TTS selection, fallback labels, and playback provider."""

import asyncio
import logging
import unittest
from unittest.mock import AsyncMock, patch

from services.conversation_engine import CallSession, ConversationEngine
from services.tts_service import TtsService


def _audio() -> bytes:
    return bytes((index % 40) + 1 for index in range(900))


class TtsProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_deepgram_setting_selects_aura_instead_of_sapi(self):
        service = TtsService()
        with patch("config.get_settings", return_value=type("Settings", (), {"tts_primary": "deepgram"})()):
            self.assertEqual(service.primary_provider(), "deepgram_tts")
        self.assertEqual(service.provider_order(allow_fallback=False), ["deepgram_tts"])

    def test_verified_primary_is_windows_sapi(self):
        service = TtsService()
        service.primary_provider = lambda: "windows_sapi"
        service.fallback_providers = lambda: ["gemini_tts", "elevenlabs", "deepgram_tts"]
        self.assertEqual(service.provider_order(allow_fallback=False), ["windows_sapi"])
        self.assertEqual(
            service.provider_order(allow_fallback=True),
            ["windows_sapi", "gemini_tts", "elevenlabs", "deepgram_tts"],
        )

    def test_no_fallback_unless_explicitly_configured(self):
        service = TtsService()
        settings = type("Settings", (), {"tts_primary": "deepgram", "tts_fallback_providers": ""})()
        with patch("config.get_settings", return_value=settings):
            self.assertEqual(service.provider_order(allow_fallback=True), ["deepgram_tts"])

    def test_configured_fallbacks_are_parsed_and_unknown_names_ignored(self):
        service = TtsService()
        settings = type(
            "Settings",
            (),
            {"tts_primary": "deepgram", "tts_fallback_providers": "elevenlabs, bogus, deepgram"},
        )()
        with patch("config.get_settings", return_value=settings):
            self.assertEqual(
                service.provider_order(allow_fallback=True),
                ["deepgram_tts", "elevenlabs"],
            )

    async def test_deepgram_failure_never_switches_to_male_sapi_voice(self):
        service = TtsService()
        service.primary_provider = lambda: "deepgram_tts"
        service.fallback_providers = lambda: []
        service._deepgram_tts = type("Deepgram", (), {})()
        service._deepgram_tts.is_configured = lambda: True
        service._deepgram_tts.text_to_speech = AsyncMock(side_effect=RuntimeError("Deepgram down"))
        sapi = AsyncMock(return_value=_audio())
        with patch("services.local_tts.is_available", return_value=True), patch(
            "services.local_tts.text_to_mulaw", new=sapi
        ):
            with self.assertRaises(RuntimeError):
                await service.text_to_speech("Hello.")
        sapi.assert_not_awaited()

    async def test_sole_primary_ignores_cooldown_so_retry_reaches_the_voice(self):
        import services.tts_service as tts_mod

        service = TtsService()
        service.primary_provider = lambda: "deepgram_tts"
        service.fallback_providers = lambda: []
        service._deepgram_tts = type("Deepgram", (), {})()
        service._deepgram_tts.is_configured = lambda: True
        service._deepgram_tts.text_to_speech = AsyncMock(return_value=_audio())
        with patch.object(tts_mod, "_deepgram_skip_until", float("inf")):
            audio = await service.text_to_speech("Hello.")
        self.assertEqual(audio, _audio())
        self.assertEqual(service.provider_name(), "deepgram_tts")

    def test_voice_identity_names_provider_and_voice(self):
        service = TtsService()
        settings = type(
            "Settings",
            (),
            {"tts_primary": "deepgram", "deepgram_tts_voice": "aura-2-thalia-en", "deepgram_tts_model": "aura-2"},
        )()
        with patch("config.get_settings", return_value=settings):
            self.assertEqual(service.voice_identity(), "deepgram_tts:aura-2-thalia-en")
            self.assertEqual(
                service.voice_identity("windows_sapi"), "windows_sapi:Microsoft David Desktop"
            )

    async def test_sapi_success_is_not_labeled_elevenlabs(self):
        service = TtsService()
        service.primary_provider = lambda: "windows_sapi"
        service._eleven.text_to_speech = AsyncMock(return_value=_audio())
        with patch("services.local_tts.is_available", return_value=True), patch(
            "services.local_tts.text_to_mulaw",
            new=AsyncMock(return_value=_audio()),
        ):
            audio = await service.text_to_speech("Hello.")
        self.assertEqual(audio, _audio())
        self.assertEqual(service.provider_name(), "windows_sapi")
        service._eleven.text_to_speech.assert_not_awaited()

    async def test_sapi_failure_uses_gemini_and_records_it(self):
        service = TtsService()
        service.primary_provider = lambda: "windows_sapi"
        service.fallback_providers = lambda: ["gemini_tts", "elevenlabs"]
        service._gemini_tts = type("Gemini", (), {})()
        service._gemini_tts.is_configured = lambda: True
        service._gemini_tts.text_to_mulaw = AsyncMock(return_value=_audio())
        service._eleven.text_to_speech = AsyncMock(return_value=_audio())
        with patch("services.local_tts.is_available", return_value=True), patch(
            "services.local_tts.text_to_mulaw",
            new=AsyncMock(side_effect=RuntimeError("SAPI down")),
        ):
            audio = await service.text_to_speech("Hello.")
        self.assertEqual(audio, _audio())
        self.assertEqual(service.provider_name(), "gemini_tts")
        service._eleven.text_to_speech.assert_not_awaited()

    async def test_playback_records_the_provider_that_spoke(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        produced = _audio()
        engine.tts = type("Tts", (), {})()
        engine.tts.provider_name = lambda: "windows_sapi"
        engine.tts.primary_provider = lambda: "windows_sapi"
        engine.tts.text_to_speech = AsyncMock(return_value=produced)
        engine.tts.mulaw_to_pcm_wav = lambda data: b"RIFF" + data
        session = CallSession(call_id="playback")
        turn = await engine._store_agent_audio(session, "Hello.", "en-US")
        self.assertIsNotNone(turn)
        self.assertEqual(session.playback_provider[turn], "windows_sapi")
        self.assertTrue(session.playback_audio[turn].startswith(b"RIFF"))

    async def test_cache_rejects_a_mislabeled_provider(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        engine.tts = type("Tts", (), {})()
        engine.tts.provider_name = lambda: "elevenlabs"
        engine.tts.primary_provider = lambda: "windows_sapi"
        engine.tts.text_to_speech = AsyncMock(return_value=_audio())
        engine.tts.mulaw_to_pcm_wav = lambda data: data
        session = CallSession(call_id="cache")
        turn = await engine._cache_phrase(session, "greeting", "Hello.", "en-US")
        self.assertIsNone(turn)
        self.assertEqual(session.playback_provider, {})
        self.assertFalse(engine.tts.text_to_speech.await_args.kwargs["allow_fallback"])


def _gather_session() -> CallSession:
    session = CallSession(call_id="gather-reply", preferred_language="en-US")
    session.phrase_cache["reply:0:v0"] = 1
    return session


class GatherReplyPlaybackTests(unittest.IsolatedAsyncioTestCase):
    def _engine(self) -> ConversationEngine:
        return ConversationEngine.__new__(ConversationEngine)

    async def test_cache_hit_plays_the_recorded_audio(self):
        engine = self._engine()
        session = _gather_session()
        session.playback_audio[3] = b"RIFF-cached"
        session.playback_provider[3] = "windows_sapi"
        engine._store_agent_audio = AsyncMock(return_value=9)
        with self.assertLogs("services.conversation_engine", level=logging.INFO) as logs:
            twiml = await engine._build_agent_twiml(
                session, "https://example.test", "Cached reply.", "en-US", cached_turn=3
            )
        self.assertIn("turn=3", twiml)
        self.assertIn("<Play>", twiml)
        self.assertNotIn("<Say", twiml)
        engine._store_agent_audio.assert_not_awaited()
        self.assertTrue(any("provider=windows_sapi" in line for line in logs.output))

    async def test_cache_miss_waits_for_windows_sapi(self):
        engine = self._engine()
        session = _gather_session()
        calls = 0

        async def store(session, text, lang, fast=True, allow_fallback=True, live=True):
            nonlocal calls
            calls += 1
            session.playback_audio[4] = b"RIFF-live"
            session.playback_provider[4] = "windows_sapi"
            return 4

        engine._store_agent_audio = store
        with self.assertLogs("services.conversation_engine", level=logging.INFO) as logs:
            twiml = await engine._build_agent_twiml(
                session, "https://example.test", "Live reply.", "en-US"
            )
        self.assertEqual(calls, 1)
        self.assertIn("turn=4", twiml)
        self.assertIn("<Play>", twiml)
        self.assertNotIn("<Say", twiml)
        self.assertTrue(any("provider=windows_sapi" in line for line in logs.output))

    async def test_synthesis_failure_without_say_voice_never_uses_say(self):
        engine = self._engine()
        session = _gather_session()
        calls = 0

        async def store(session, text, lang, fast=True, allow_fallback=True, live=True):
            nonlocal calls
            calls += 1
            return None

        engine._store_agent_audio = store
        with patch("services.conversation_engine._twilio_say_voice", return_value=""):
            with self.assertLogs("services.conversation_engine", level=logging.INFO) as logs:
                twiml = await engine._build_agent_twiml(
                    session, "https://example.test", "Need a fallback.", "en-US"
                )
        self.assertEqual(calls, 2)
        self.assertNotIn("<Say", twiml)
        self.assertNotIn("Polly", twiml)
        self.assertNotIn("agent-audio", twiml)
        self.assertIn("<Gather", twiml)
        self.assertTrue(any("provider=none reason=synthesis_failed" in line for line in logs.output))

    async def test_say_uses_only_the_explicitly_configured_voice(self):
        engine = self._engine()
        session = _gather_session()

        async def store(session, text, lang, fast=True, allow_fallback=True, live=True):
            return None

        engine._store_agent_audio = store
        with patch("services.conversation_engine._twilio_say_voice", return_value="Polly.Joanna"):
            with self.assertLogs("services.conversation_engine", level=logging.INFO) as logs:
                twiml = await engine._build_agent_twiml(
                    session, "https://example.test", "Need a fallback.", "en-US"
                )
        self.assertIn('<Say voice="Polly.Joanna"', twiml)
        self.assertNotIn("Polly.Matthew", twiml)
        self.assertTrue(any("provider=twilio_say reason=synthesis_failed" in line for line in logs.output))

    async def test_retry_succeeds_with_the_configured_voice(self):
        engine = self._engine()
        session = _gather_session()
        results = [None, 6]

        async def store(session, text, lang, fast=True, allow_fallback=True, live=True):
            turn = results.pop(0)
            if turn is not None:
                session.playback_audio[turn] = b"RIFF-retry"
                session.playback_provider[turn] = "deepgram_tts"
            return turn

        engine._store_agent_audio = store
        twiml = await engine._build_agent_twiml(
            session, "https://example.test", "Retry me.", "en-US"
        )
        self.assertIn("turn=6", twiml)
        self.assertNotIn("<Say", twiml)

    async def test_synthesis_timeout_without_say_voice_stays_silent(self):
        engine = self._engine()
        session = _gather_session()
        calls = 0

        async def store(session, text, lang, fast=True, allow_fallback=True, live=True):
            nonlocal calls
            calls += 1
            await asyncio.sleep(1)
            return 8

        engine._store_agent_audio = store
        with patch("services.conversation_engine.GATHER_TTS_TIMEOUT_S", 0.05), patch(
            "services.conversation_engine._twilio_say_voice", return_value=""
        ):
            with self.assertLogs("services.conversation_engine", level=logging.INFO) as logs:
                twiml = await engine._build_agent_twiml(
                    session, "https://example.test", "Too slow.", "en-US"
                )
        self.assertEqual(calls, 1)
        self.assertNotIn("<Say", twiml)
        self.assertNotIn("agent-audio", twiml)
        self.assertTrue(any("provider=none reason=timeout" in line for line in logs.output))

    async def test_end_call_plays_goodbye_in_agent_voice_without_say(self):
        engine = self._engine()
        engine._persist_call_async = lambda call_id: None
        session = _gather_session()
        spoken: list[str] = []

        async def store(session, text, lang, fast=True, allow_fallback=True, live=True):
            spoken.append(text)
            session.playback_audio[7] = b"RIFF-bye"
            session.playback_provider[7] = "deepgram_tts"
            return 7

        engine._store_agent_audio = store
        twiml = await engine._build_agent_twiml(
            session, "https://example.test", "We will update the claim.", "en-US", end_call=True
        )
        self.assertIn("<Play>", twiml)
        self.assertIn("<Hangup/>", twiml)
        self.assertNotIn("<Say", twiml)
        self.assertIn("Goodbye", spoken[0])

    async def test_cached_clip_in_a_different_voice_is_regenerated(self):
        engine = self._engine()
        engine.tts = type("Tts", (), {})()
        engine.tts.voice_identity = lambda provider=None: "deepgram_tts:aura-2-thalia-en"
        session = _gather_session()
        session.playback_audio[3] = b"RIFF-old-male"
        session.playback_voice[3] = "windows_sapi:Microsoft David Desktop"

        async def store(session, text, lang, fast=True, allow_fallback=True, live=True):
            session.playback_audio[9] = b"RIFF-new"
            session.playback_voice[9] = "deepgram_tts:aura-2-thalia-en"
            return 9

        engine._store_agent_audio = store
        twiml = await engine._build_agent_twiml(
            session, "https://example.test", "Cached reply.", "en-US", cached_turn=3
        )
        self.assertIn("turn=9", twiml)
        self.assertNotIn("turn=3", twiml)


if __name__ == "__main__":
    unittest.main()
