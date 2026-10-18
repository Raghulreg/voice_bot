"""ElevenLabs concurrency, 429 backoff, 401 cooldown, and cache queue."""

import asyncio
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services import call_storage
from services import elevenlabs_service as eleven_mod
from services import tts_service as tts_mod
from services.conversation_engine import CallSession, ConversationEngine
from services.elevenlabs_rate_limit import (
    CACHE_QUEUE_MAX,
    MAX_BACKOFF_S,
    request_slot,
    retry_after_seconds,
)
from services.elevenlabs_service import ElevenLabsService, elevenlabs_error_kind


def _audio() -> bytes:
    return bytes((index % 40) + 1 for index in range(900))


def _settings():
    return SimpleNamespace(
        elevenlabs_api_key="sk_test_not_a_real_key",
        elevenlabs_voice_id="cjVigY5qzO86Huf0OWal",
        elevenlabs_model_id="eleven_turbo_v2_5",
        elevenlabs_stability=0.42,
        elevenlabs_similarity=0.78,
        elevenlabs_style=0.18,
        elevenlabs_speed=1.0,
        elevenlabs_speaker_boost=True,
    )


class _Response:
    def __init__(self, status_code: int, body: str = "", content: bytes | None = None, headers=None):
        self.status_code = status_code
        self.text = body
        self.content = content if content is not None else body.encode()
        self.headers = headers or {}


class _Client:
    def __init__(self, posts: list[_Response]):
        self.posts = list(posts)
        self.calls = 0
        self.is_closed = False

    async def post(self, url, headers=None, params=None, json=None):
        self.calls += 1
        return self.posts.pop(0)

    async def get(self, url, headers=None):
        return _Response(401, '{"detail":{"status":"missing_permissions"}}')


class ElevenLabsRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        eleven_mod._resolved_voice_id = None
        eleven_mod._resolved_voice_label = ""
        eleven_mod._bad_voice_ids.clear()
        eleven_mod._voice_resolve_attempted = False
        eleven_mod._elevenlabs_quota_exhausted = False
        tts_mod._elevenlabs_skip_until = 0.0
        tts_mod._gemini_cooldown_until = 0.0
        request_slot._semaphore = asyncio.Semaphore(1)
        request_slot.live_waiting = 0

    def test_retry_after_is_capped_and_backoff_grows(self):
        self.assertEqual(retry_after_seconds("30", 0), MAX_BACKOFF_S)
        self.assertEqual(retry_after_seconds("1.5", 0), 1.5)
        self.assertEqual(retry_after_seconds(None, 0), 0.5)
        self.assertEqual(retry_after_seconds(None, 1), 1.0)
        self.assertLessEqual(retry_after_seconds(None, 8), MAX_BACKOFF_S)
        self.assertEqual(elevenlabs_error_kind(429, "quota_exceeded"), "rate_limited")
        self.assertEqual(
            elevenlabs_error_kind(401, '{"detail":{"status":"missing_permissions"}}'),
            "missing_permissions",
        )

    async def test_only_one_elevenlabs_request_runs_at_a_time(self):
        in_flight = 0
        max_seen = 0

        async def once():
            nonlocal in_flight, max_seen
            async with request_slot.hold(live=True):
                in_flight += 1
                max_seen = max(max_seen, in_flight)
                await asyncio.sleep(0.02)
                in_flight -= 1

        await asyncio.gather(once(), once(), once())
        self.assertEqual(max_seen, 1)

    async def test_429_retries_with_retry_after_then_succeeds(self):
        delays: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            delays.append(seconds)

        client = _Client(
            [
                _Response(429, "too many requests", headers={"Retry-After": "1.25"}),
                _Response(200, content=_audio()),
            ]
        )
        with patch("services.elevenlabs_service.get_settings", return_value=_settings()), patch(
            "services.elevenlabs_service.rate_limit_sleep",
            new=fake_sleep,
        ):
            service = ElevenLabsService()
            service._client = client
            audio = await service.text_to_speech("The claim is under review.")
        self.assertEqual(audio, _audio())
        self.assertEqual(delays, [1.25])
        self.assertEqual(client.calls, 2)
        self.assertFalse(eleven_mod._elevenlabs_quota_exhausted)
        self.assertEqual(tts_mod._elevenlabs_skip_until, 0.0)

    async def test_401_is_not_retried_and_sets_auth_cooldown(self):
        secret = "sk_test_not_a_real_key"
        client = _Client(
            [
                _Response(
                    401,
                    '{"detail":{"status":"missing_permissions","message":"' + secret + '"}}',
                ),
                _Response(200, content=_audio()),
            ]
        )
        service = tts_mod.TtsService()
        with patch("services.elevenlabs_service.get_settings", return_value=_settings()):
            eleven = ElevenLabsService()
        eleven._client = client
        service._eleven = eleven
        gemini = SimpleNamespace(
            is_configured=lambda: True,
            text_to_mulaw=AsyncMock(return_value=_audio()),
        )
        service._gemini_tts = gemini
        service.primary_provider = lambda: "elevenlabs"
        service.fallback_providers = lambda: ["gemini_tts"]
        with patch("services.local_tts.is_available", return_value=False):
            audio = await service.text_to_speech("The claim is under review.")
        self.assertEqual(audio, _audio())
        self.assertEqual(service.provider_name(), "gemini_tts")
        self.assertEqual(client.calls, 1)
        self.assertGreater(tts_mod._elevenlabs_skip_until, 0.0)
        self.assertNotIn(secret, "gemini_tts")

    async def test_cache_failure_does_not_store_fallback_audio(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        engine._cache_queue = None
        engine._cache_worker = None
        engine.tts = SimpleNamespace(
            provider_name=lambda: "windows_sapi",
            text_to_speech=AsyncMock(side_effect=RuntimeError("ElevenLabs rate limited HTTP 429")),
            mulaw_to_pcm_wav=lambda audio: audio,
        )
        session = CallSession(call_id="cache-test")
        turn = await engine._cache_phrase(session, "greeting", "Hello.", "en-US")
        self.assertIsNone(turn)
        self.assertEqual(session.phrase_cache, {})
        self.assertEqual(session.playback_audio, {})
        self.assertEqual(session.playback_provider, {})
        engine.tts.text_to_speech.assert_awaited()
        self.assertFalse(engine.tts.text_to_speech.await_args.kwargs["allow_fallback"])

    async def test_cache_queue_stays_serial_and_bounded(self):
        engine = ConversationEngine.__new__(ConversationEngine)
        engine._cache_queue = None
        engine._cache_worker = None
        in_flight = 0
        max_seen = 0
        started = asyncio.Event()
        release = asyncio.Event()
        first = True

        async def fake_cache(session, key, text, lang, verbal_meta=None):
            nonlocal in_flight, max_seen, first
            in_flight += 1
            max_seen = max(max_seen, in_flight)
            if first:
                first = False
                started.set()
                await release.wait()
            await asyncio.sleep(0)
            in_flight -= 1
            return 1

        engine._cache_phrase = fake_cache
        session = CallSession(call_id="queue-test")
        await engine._enqueue_cache_phrase((session, "k0", "Hello.", "en-US", None))
        await started.wait()
        for index in range(CACHE_QUEUE_MAX):
            await engine._enqueue_cache_phrase(
                (session, f"k{index+1}", "Hello.", "en-US", None)
            )
        self.assertEqual(engine._cache_queue.qsize(), CACHE_QUEUE_MAX)
        blocked = asyncio.create_task(
            engine._enqueue_cache_phrase((session, "overflow", "Hello.", "en-US", None))
        )
        await asyncio.sleep(0.05)
        self.assertFalse(blocked.done())
        release.set()
        await blocked
        await engine._cache_queue.join()
        self.assertEqual(max_seen, 1)
        engine._cache_worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await engine._cache_worker

    def test_load_call_json_is_the_supported_reader(self):
        self.assertFalse(hasattr(call_storage, "load_call_data"))
        parameters = list(inspect.signature(call_storage.load_call_json).parameters)
        self.assertEqual(parameters, ["call_id"])
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(call_storage, "CALL_DATA_DIR", Path(tmp)):
                call_storage.save_call_json(
                    "abc",
                    {"call_id": "abc", "transcript": [], "created_at": 1},
                )
                loaded = call_storage.load_call_json("abc")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["call_id"], "abc")
        self.assertIsNone(call_storage.load_call_json("missing-call"))


if __name__ == "__main__":
    unittest.main()
