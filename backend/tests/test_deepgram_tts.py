"""Unit tests for Deepgram Aura TTS; all provider HTTP requests are mocked."""
import unittest
from unittest.mock import patch

from services.deepgram_tts import DeepgramTtsService
from services.tts_service import TtsService
from unittest.mock import AsyncMock


class _Response:
    status_code = 200
    content = b"\x01" * 1000


class _Client:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, **kwargs):
        assert url.endswith("/v1/speak")
        assert kwargs["headers"]["Authorization"] == "Token test-key"
        assert kwargs["params"] == {
            "model": "aura-2-thalia-en",
            "encoding": "mulaw",
            "sample_rate": "8000",
        }
        assert kwargs["json"] == {"text": "Hello there."}
        return _Response()


class DeepgramTtsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = type("Settings", (), {
            "tts_primary": "deepgram",
            "deepgram_api_key": "test-key",
            "deepgram_tts_model": "aura-2",
            "deepgram_tts_voice": "aura-2-thalia-en",
        })()

    async def test_deepgram_alias_selects_deepgram_primary(self):
        with patch("config.get_settings", return_value=self.settings):
            service = TtsService()
            self.assertEqual(service.primary_provider(), "deepgram_tts")
            self.assertEqual(service.provider_order(allow_fallback=False), ["deepgram_tts"])

    async def test_phone_audio_request_is_mocked_and_ulaw_8khz(self):
        with patch("services.deepgram_tts.get_settings", return_value=self.settings), patch(
            "services.deepgram_tts.httpx.AsyncClient", _Client
        ):
            service = DeepgramTtsService()
            audio = await service.text_to_speech("Hello there.")
        self.assertEqual(len(audio), 1000)

    async def test_tts_service_uses_deepgram_and_records_provider(self):
        service = TtsService()
        service._deepgram_tts = type("Deepgram", (), {
            "is_configured": lambda self: True,
            "text_to_speech": AsyncMock(return_value=b"\x01" * 1000),
        })()
        with patch("config.get_settings", return_value=self.settings):
            audio = await service.text_to_speech("Hello there.", allow_fallback=False)
        self.assertEqual(len(audio), 1000)
        self.assertEqual(service.provider_name(), "deepgram_tts")

    async def test_missing_key_fails_without_network(self):
        settings = type("Settings", (), {
            "deepgram_api_key": "", "deepgram_tts_model": "aura-2", "deepgram_tts_voice": ""
        })()
        with patch("services.deepgram_tts.get_settings", return_value=settings):
            service = DeepgramTtsService()
            self.assertFalse(service.is_configured())
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                await service.text_to_speech("Hello.")


if __name__ == "__main__":
    unittest.main()
