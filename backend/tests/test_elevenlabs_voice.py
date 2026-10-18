"""ElevenLabs voice selection, auth failures, and TTS fallback. No live calls."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.elevenlabs_service import (
    ElevenLabsService,
    _is_valid_ulaw_or_audio,
    configured_model_candidates,
    elevenlabs_error_kind,
    public_elevenlabs_failure,
)
from services import elevenlabs_service as eleven_mod
from services import tts_service as tts_mod
from services.elevenlabs_rate_limit import request_slot


def _settings(**overrides):
    values = {
        "elevenlabs_api_key": "sk_test_not_a_real_key",
        "elevenlabs_voice_id": "TxGEqnHWrfWFTfGW9XjX",
        "elevenlabs_model_id": "eleven_turbo_v2_5",
        "elevenlabs_stability": 0.42,
        "elevenlabs_similarity": 0.78,
        "elevenlabs_style": 0.18,
        "elevenlabs_speed": 1.0,
        "elevenlabs_speaker_boost": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _audio() -> bytes:
    return bytes((index % 40) + 1 for index in range(900))


class _Response:
    def __init__(
        self,
        status_code: int,
        body: str = "",
        content: bytes | None = None,
        headers: dict | None = None,
    ):
        self.status_code = status_code
        self.text = body
        self.content = content if content is not None else body.encode()
        self.headers = headers or {}


class _Client:
    def __init__(self, posts: list[_Response], gets: list[_Response] | None = None):
        self.posts = list(posts)
        self.gets = list(gets or [])
        self.post_urls: list[str] = []
        self.is_closed = False

    async def post(self, url, headers=None, params=None, json=None):
        self.post_urls.append(url)
        return self.posts.pop(0)

    async def get(self, url, headers=None):
        if self.gets:
            return self.gets.pop(0)
        return _Response(401, '{"detail":{"status":"missing_permissions"}}')


class ElevenLabsVoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        eleven_mod._resolved_voice_id = None
        eleven_mod._resolved_voice_label = ""
        eleven_mod._bad_voice_ids.clear()
        eleven_mod._voice_resolve_attempted = False
        eleven_mod._elevenlabs_quota_exhausted = False
        tts_mod._elevenlabs_skip_until = 0.0
        tts_mod._gemini_cooldown_until = 0.0
        request_slot._semaphore = __import__("asyncio").Semaphore(1)
        request_slot.live_waiting = 0

    def test_error_kind_separates_library_voice_from_quota_and_permissions(self):
        library = '{"detail":{"status":"payment_required","message":"Free users cannot use library voices via the API."}}'
        self.assertEqual(elevenlabs_error_kind(402, library), "voice_unavailable")
        self.assertEqual(
            elevenlabs_error_kind(401, '{"detail":{"status":"missing_permissions"}}'),
            "missing_permissions",
        )
        self.assertEqual(
            elevenlabs_error_kind(401, '{"detail":{"status":"invalid_api_key"}}'),
            "invalid_api_key",
        )
        self.assertEqual(
            elevenlabs_error_kind(402, '{"detail":{"status":"quota_exceeded"}}'),
            "quota",
        )
        secret = "sk_test_not_a_real_key"
        public = public_elevenlabs_failure(401, f"missing_permissions {secret}", secret)
        self.assertNotIn(secret, public)
        self.assertIn("missing_permissions", public)

    def test_quiet_ulaw_lead_in_is_still_phone_audio(self):
        speech = bytes([0xFF]) * 120 + bytes((index % 40) + 1 for index in range(800))
        self.assertTrue(_is_valid_ulaw_or_audio(speech, for_twilio=True))
        self.assertFalse(_is_valid_ulaw_or_audio(bytes([0xFF]) * 900, for_twilio=True))

    def test_configured_model_stays_turbo(self):
        self.assertEqual(
            configured_model_candidates("eleven_turbo_v2_5"),
            ["eleven_turbo_v2_5"],
        )

    async def test_blocked_library_voice_uses_the_next_verified_voice(self):
        client = _Client(
            [
                _Response(
                    402,
                    '{"detail":{"status":"payment_required","message":"cannot use library voices"}}',
                ),
                _Response(200, content=_audio()),
            ]
        )
        with patch("services.elevenlabs_service.get_settings", return_value=_settings()):
            service = ElevenLabsService()
            service._client = client
            audio = await service.text_to_speech("The claim is under review.")
        self.assertEqual(audio, _audio())
        self.assertFalse(eleven_mod._elevenlabs_quota_exhausted)
        self.assertEqual(eleven_mod._resolved_voice_id, "cjVigY5qzO86Huf0OWal")
        self.assertTrue(any(url.endswith("cjVigY5qzO86Huf0OWal") for url in client.post_urls))

    async def test_missing_permission_does_not_claim_success_or_leak_the_key(self):
        secret = "sk_test_not_a_real_key"
        client = _Client(
            [
                _Response(
                    401,
                    '{"detail":{"status":"missing_permissions","message":"missing text_to_speech '
                    + secret
                    + '"}}',
                )
            ]
        )
        with patch("services.elevenlabs_service.get_settings", return_value=_settings()):
            service = ElevenLabsService()
            service._client = client
            with self.assertRaises(RuntimeError) as raised:
                await service.text_to_speech("The claim is under review.")
        self.assertIn("missing_permissions", str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))
        self.assertFalse(eleven_mod._elevenlabs_quota_exhausted)

    async def test_401_uses_gemini_and_records_that_provider(self):
        service = tts_mod.TtsService()
        service.primary_provider = lambda: "elevenlabs"
        service.fallback_providers = lambda: ["gemini_tts", "windows_sapi"]
        service._eleven.is_configured = lambda: True
        service._eleven.text_to_speech = AsyncMock(
            side_effect=RuntimeError("ElevenLabs TTS HTTP 401 missing_permissions")
        )
        gemini = SimpleNamespace(
            is_configured=lambda: True,
            text_to_mulaw=AsyncMock(return_value=_audio()),
        )
        service._gemini_tts = gemini
        with patch("services.local_tts.is_available", return_value=False):
            audio = await service.text_to_speech("The claim is under review.")
        self.assertEqual(audio, _audio())
        self.assertEqual(service.provider_name(), "gemini_tts")

    async def test_gemini_failure_uses_windows_sapi(self):
        service = tts_mod.TtsService()
        service.primary_provider = lambda: "elevenlabs"
        service.fallback_providers = lambda: ["gemini_tts", "windows_sapi"]
        service._eleven.is_configured = lambda: True
        service._eleven.text_to_speech = AsyncMock(
            side_effect=RuntimeError("ElevenLabs voice unavailable HTTP 402 voice_unavailable")
        )
        gemini = SimpleNamespace(
            is_configured=lambda: True,
            text_to_mulaw=AsyncMock(side_effect=RuntimeError("Gemini TTS down")),
        )
        service._gemini_tts = gemini
        with patch("services.local_tts.is_available", return_value=True), patch(
            "services.local_tts.text_to_mulaw",
            new=AsyncMock(return_value=_audio()),
        ):
            audio = await service.text_to_speech("The claim is under review.")
        self.assertEqual(audio, _audio())
        self.assertEqual(service.provider_name(), "windows_sapi")


if __name__ == "__main__":
    unittest.main()
