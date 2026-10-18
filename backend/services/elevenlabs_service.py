"""ElevenLabs TTS — natural U.S. English voices for Twilio (ulaw 8 kHz).

No Edge TTS. Uses API key voices only, with prebuilt US-accent fallbacks
when ELEVENLABS_VOICE_ID is invalid/missing from the account.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from config import get_settings
from services.elevenlabs_rate_limit import (
    MAX_RATE_LIMIT_RETRIES,
    rate_limit_sleep,
    request_slot,
    retry_after_seconds,
)
from services.voice_config import normalize_tts_text

logger = logging.getLogger(__name__)

ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
ELEVENLABS_VOICES_URL = "https://api.elevenlabs.io/v1/voices"

# Remember a working voice for this process (avoids 404 retry every sentence)
_resolved_voice_id: str | None = None
_resolved_voice_label: str = ""
_bad_voice_ids: set[str] = set()
_voice_resolve_attempted: bool = False
# After quota_exceeded, skip ElevenLabs until process restart / recharge
_elevenlabs_quota_exhausted: bool = False


def voice_settings_for_call(delivery_emotion: str = "") -> dict[str, Any]:
    """Conversational US phone speech. Flash keeps style low so it stays human."""
    s = get_settings()
    stability = float(s.elevenlabs_stability)
    style = float(s.elevenlabs_style)
    speed = float(s.elevenlabs_speed)
    similarity = float(s.elevenlabs_similarity)
    flash = "flash" in str(getattr(s, "elevenlabs_model_id", "") or "").lower()

    emo = (delivery_emotion or "").lower()
    if emo in ("impatient", "busy", "rushed"):
        speed = min(1.03, speed + 0.01)
    elif emo in ("confused", "supportive", "empathetic", "helpful"):
        speed = min(1.0, max(0.97, speed - 0.01))
        stability = min(0.55, stability + 0.04)
    elif emo in ("defensive", "frustrated"):
        speed = min(1.0, max(0.97, speed - 0.01))
        stability = min(0.55, stability + 0.06)

    speed = max(0.97, min(1.03, speed))
    style_floor = 0.0 if flash else 0.10
    style_cap = 0.08 if flash else 0.22
    style_value = style if style > 0 else (0.0 if flash else 0.15)
    settings: dict[str, Any] = {
        "stability": max(0.40 if flash else 0.35, min(0.55, stability)),
        "similarity_boost": max(0.72, min(0.85, similarity)),
        "style": max(style_floor, min(style_cap, style_value)),
        "use_speaker_boost": bool(s.elevenlabs_speaker_boost),
        "speed": speed,
    }
    return settings


# One locked US male. Fallbacks only if primary is blocked.
US_VOICE_FALLBACKS: list[tuple[str, str]] = [
    ("TxGEqnHWrfWFTfGW9XjX", "Josh"),
    ("cjVigY5qzO86Huf0OWal", "Eric"),
]


def configured_model_candidates(model_id: str) -> list[str]:
    """Only the explicitly configured/approved ElevenLabs model may be called."""
    configured = (model_id or "").strip()
    return [configured] if configured else []


def elevenlabs_error_kind(status_code: int, body: str) -> str:
    """Classify a provider failure without treating a blocked voice as empty credits."""
    low = (body or "").lower()
    if status_code == 429 or "too many requests" in low or "rate limit" in low:
        return "rate_limited"
    if "missing_permissions" in low or "missing the permission" in low:
        return "missing_permissions"
    if "invalid_api_key" in low or "invalid api key" in low:
        return "invalid_api_key"
    if (
        "library voice" in low
        or "cannot use library" in low
        or "cannot use this voice" in low
        or "voice_not_found" in low
        or "voice not found" in low
    ):
        return "voice_unavailable"
    if "quota_exceeded" in low or ("quota" in low and "library" not in low):
        return "quota"
    if status_code == 402 and "voice" in low:
        return "voice_unavailable"
    if status_code == 402:
        return "quota"
    if status_code in (401, 403):
        return "auth"
    if status_code == 404:
        return "voice_unavailable"
    return "other"


def public_elevenlabs_failure(status_code: int, body: str, secret: str = "") -> str:
    """Status and error class only. Never returns the key or the raw provider body."""
    text = body or ""
    if secret:
        text = text.replace(secret, "")
    return f"HTTP {status_code} {elevenlabs_error_kind(status_code, text)}"


class ElevenLabsService:
    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = (settings.elevenlabs_api_key or "").strip()
        self.voice_id = (settings.elevenlabs_voice_id or "").strip()
        # Flash v2.5 starts speech sooner. Voice settings above keep the delivery conversational.
        self.model_id = (settings.elevenlabs_model_id or "eleven_flash_v2_5").strip()
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=4.0))
        return self._client

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _candidate_voice_ids(self) -> list[str]:
        """Prefer one locked voice for the whole process (same speaker every turn)."""
        global _resolved_voice_id
        out: list[str] = []
        if _resolved_voice_id and _resolved_voice_id not in _bad_voice_ids:
            return [_resolved_voice_id]
        if self.voice_id and self.voice_id not in _bad_voice_ids:
            out.append(self.voice_id)
        for vid, _name in US_VOICE_FALLBACKS:
            if vid not in _bad_voice_ids and vid not in out:
                out.append(vid)
        return out

    async def _discover_account_voice(self) -> str | None:
        """One-shot: pick a prebuilt English voice from the account if env ID fails."""
        global _voice_resolve_attempted
        if _voice_resolve_attempted:
            return None
        _voice_resolve_attempted = True
        if not self.api_key:
            return None
        try:
            client = await self._get_client()
            resp = await client.get(
                ELEVENLABS_VOICES_URL,
                headers={"xi-api-key": self.api_key},
            )
            if resp.status_code >= 400:
                logger.warning(
                    "ElevenLabs list voices failed %s",
                    public_elevenlabs_failure(resp.status_code, resp.text or "", self.api_key),
                )
                return None
            voices = (resp.json() or {}).get("voices") or []
            # Prefer prebuilt / not cloned, English-ish labels
            preferred_names = {
                "adam",
                "josh",
                "eric",
                "brian",
                "chris",
                "daniel",
                "matthew",
                "rachel",
                "bella",
                "antoni",
            }
            scored: list[tuple[int, str, str]] = []
            for v in voices:
                vid = str(v.get("voice_id") or "")
                name = str(v.get("name") or "")
                if not vid or vid in _bad_voice_ids:
                    continue
                low = name.lower()
                score = 0
                if any(p in low for p in preferred_names):
                    score += 3
                cat = str(v.get("category") or "").lower()
                if cat in ("premade", "professional", "generated"):
                    score += 1
                scored.append((score, vid, name))
            scored.sort(key=lambda x: (-x[0], x[2]))
            if scored:
                best = scored[0]
                logger.info(
                    "ElevenLabs discovered account voice=%s name=%r score=%d",
                    best[1],
                    best[2],
                    best[0],
                )
                return best[1]
        except Exception as e:
            logger.warning("ElevenLabs voice discovery failed: %s", e)
        return None

    async def text_to_speech(
        self,
        text: str,
        for_twilio: bool = True,
        voice_id: str | None = None,
        delivery_emotion: str = "",
        live: bool = True,
    ) -> bytes:
        """Return mulaw 8 kHz bytes when for_twilio=True (Twilio-compatible)."""
        global _resolved_voice_id, _resolved_voice_label, _elevenlabs_quota_exhausted

        if not self.api_key:
            raise RuntimeError(
                "ElevenLabs not configured. Set ELEVENLABS_API_KEY in .env"
            )
        if _elevenlabs_quota_exhausted:
            raise RuntimeError(
                "ElevenLabs quota exhausted (0 credits). Top up at elevenlabs.io"
            )

        clean = normalize_tts_text(text)
        if not clean:
            raise ValueError("TTS text is empty")

        output_format = "ulaw_8000" if for_twilio else "mp3_44100_128"
        voice_settings = voice_settings_for_call(delivery_emotion)
        client = await self._get_client()

        candidates: list[str] = []
        if voice_id:
            candidates.append(voice_id)
        candidates.extend(self._candidate_voice_ids())
        seen: set[str] = set()
        ordered: list[str] = []
        for vid in candidates:
            if vid and vid not in seen:
                seen.add(vid)
                ordered.append(vid)

        last_err: Exception | None = None
        tried_discovery = False
        i = 0
        while i < len(ordered):
            vid = ordered[i]
            i += 1
            try:
                audio = await self._synthesize_once(
                    client,
                    vid=vid,
                    clean=clean,
                    output_format=output_format,
                    for_twilio=for_twilio,
                    voice_settings=voice_settings,
                    live=live,
                )
                if _resolved_voice_id != vid:
                    name = next((n for v, n in US_VOICE_FALLBACKS if v == vid), vid[:8])
                    logger.info(
                        "ElevenLabs TTS locked voice_id=%s (%s) — energetic natural US speed=%.2f",
                        vid,
                        name,
                        float(voice_settings.get("speed", 1.08)),
                    )
                    _resolved_voice_id = vid
                    _resolved_voice_label = name
                return audio
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                if "quota exhausted" in msg or "quota_exceeded" in msg:
                    _elevenlabs_quota_exhausted = True
                    raise RuntimeError(
                        "ElevenLabs quota exhausted (0 credits). Top up at elevenlabs.io"
                    ) from e
                # A library voice rejected on a free plan is not an empty-credit failure.
                if "voice unavailable" in msg or "not allowed" in msg:
                    _bad_voice_ids.add(vid)
                    logger.warning(
                        "ElevenLabs voice_id=%s blocked by plan — trying next US voice",
                        vid,
                    )
                    if not tried_discovery:
                        discovered = await self._discover_account_voice()
                        tried_discovery = True
                        if discovered and discovered not in seen:
                            ordered.append(discovered)
                            seen.add(discovered)
                    continue
                if "404" in msg or "voice_not_found" in msg or "not_found" in msg:
                    _bad_voice_ids.add(vid)
                    logger.warning(
                        "ElevenLabs voice_id=%s invalid — trying next US voice",
                        vid,
                    )
                    if not tried_discovery and i >= len(ordered):
                        discovered = await self._discover_account_voice()
                        tried_discovery = True
                        if discovered and discovered not in seen:
                            ordered.append(discovered)
                            seen.add(discovered)
                    continue
                raise

        if last_err is not None:
            raise RuntimeError(f"ElevenLabs TTS unavailable: {last_err}") from last_err
        raise RuntimeError("ElevenLabs TTS failed with no candidates")

    async def _synthesize_once(
        self,
        client: httpx.AsyncClient,
        *,
        vid: str,
        clean: str,
        output_format: str,
        for_twilio: bool,
        voice_settings: dict[str, Any],
        live: bool = True,
    ) -> bytes:
        url = ELEVENLABS_TTS_URL.format(voice_id=vid)
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "audio/basic" if for_twilio else "audio/mpeg",
        }
        # Never activate a model merely because its identifier exists. The configured
        # model is the approved model; provider-level failure falls back in TtsService.
        models = configured_model_candidates(self.model_id)

        last_body = ""
        for model_id in models:
            payload: dict[str, Any] = {
                "text": clean,
                "model_id": model_id,
                "language_code": "en",
                "voice_settings": dict(voice_settings),
            }
            resp = await self._post_tts(
                client,
                url=url,
                headers=headers,
                params={"output_format": output_format, "optimize_streaming_latency": "2"},
                payload=payload,
                live=live,
            )

            if resp.status_code >= 400:
                last_body = (resp.text or "")[:400]
                failure = public_elevenlabs_failure(
                    resp.status_code, last_body, self.api_key
                )
                kind = elevenlabs_error_kind(resp.status_code, last_body)
                low = last_body.lower()
                if kind == "quota":
                    raise RuntimeError("ElevenLabs quota exhausted")
                if kind == "voice_unavailable":
                    raise RuntimeError(f"ElevenLabs voice unavailable {failure}")
                if kind == "rate_limited":
                    raise RuntimeError("ElevenLabs rate limited HTTP 429")
                if kind in ("missing_permissions", "invalid_api_key", "auth"):
                    logger.warning(
                        "ElevenLabs authentication failure category=%s",
                        kind,
                    )
                    raise RuntimeError(f"ElevenLabs TTS {failure}")
                # Soft model/settings errors — simplify payload then maybe next model
                simplified = dict(payload)
                if "speed" in low and "speed" in simplified.get("voice_settings", {}):
                    simplified["voice_settings"] = {
                        k: v
                        for k, v in simplified["voice_settings"].items()
                        if k != "speed"
                    }
                    resp2 = await self._post_tts(
                        client,
                        url=url,
                        headers=headers,
                        params={"output_format": output_format, "optimize_streaming_latency": "2"},
                        payload=simplified,
                        live=live,
                    )
                    if resp2.status_code < 400:
                        resp = resp2
                    else:
                        last_body = (resp2.text or "")[:400]
                        low = last_body.lower()
                        if elevenlabs_error_kind(resp2.status_code, last_body) == "quota":
                            raise RuntimeError("ElevenLabs quota exhausted")
                if resp.status_code >= 400 and "language" in low:
                    simplified.pop("language_code", None)
                    resp2 = await self._post_tts(
                        client,
                        url=url,
                        headers=headers,
                        params={"output_format": output_format, "optimize_streaming_latency": "2"},
                        payload=simplified,
                        live=live,
                    )
                    if resp2.status_code < 400:
                        resp = resp2
                    else:
                        last_body = (resp2.text or "")[:400]
                        if elevenlabs_error_kind(resp2.status_code, last_body) == "quota":
                            raise RuntimeError("ElevenLabs quota exhausted")
                if resp.status_code >= 400:
                    logger.warning(
                        "ElevenLabs model=%s rejected %s — trying next model",
                        model_id,
                        public_elevenlabs_failure(resp.status_code, last_body, self.api_key),
                    )
                    continue

            audio = resp.content or b""
            if not _is_valid_ulaw_or_audio(audio, for_twilio=for_twilio):
                last_body = (resp.text or "")[:400] or repr(audio[:60])
                logger.warning(
                    "ElevenLabs non-audio body voice=%s model=%s bytes=%d — trying next",
                    vid,
                    model_id,
                    len(audio),
                )
                continue

            logger.debug(
                "ElevenLabs TTS ok voice=%s model=%s bytes=%d",
                vid,
                model_id,
                len(audio),
            )
            return audio

        raise RuntimeError(
            "ElevenLabs TTS "
            + public_elevenlabs_failure(0, last_body, self.api_key)
        )

    async def iter_ulaw(
        self,
        text: str,
        *,
        delivery_emotion: str = "",
        live: bool = True,
    ) -> AsyncIterator[bytes]:
        """Yield 8 kHz μ-law as ElevenLabs produces it. Caller plays each chunk."""
        if _elevenlabs_quota_exhausted or not self.api_key:
            raise RuntimeError("ElevenLabs TTS unavailable")
        clean = normalize_tts_text(text)
        if not clean:
            return
        vid = _resolved_voice_id or self.voice_id
        if not vid:
            raise RuntimeError("ElevenLabs voice is not configured")
        client = await self._get_client()
        url = ELEVENLABS_TTS_URL.format(voice_id=vid) + "/stream"
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "audio/basic",
        }
        payload = {
            "text": clean,
            "model_id": self.model_id or "eleven_flash_v2_5",
            "language_code": "en",
            "voice_settings": voice_settings_for_call(delivery_emotion),
        }
        async with request_slot.hold(live=live):
            async with client.stream(
                "POST",
                url,
                headers=headers,
                params={"output_format": "ulaw_8000", "optimize_streaming_latency": "2"},
                json=payload,
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", errors="ignore")
                    raise RuntimeError(
                        "ElevenLabs TTS " + public_elevenlabs_failure(resp.status_code, body, self.api_key)
                    )
                buf = b""
                yielded = 0
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    buf += chunk
                    head = buf[:48].lstrip()
                    if yielded == 0 and head[:1] in (b"{", b"[", b"<"):
                        if len(buf) < 180:
                            continue
                        raise RuntimeError("ElevenLabs stream returned a non-audio body")
                    while len(buf) >= 640:
                        yielded += 640
                        yield buf[:640]
                        buf = buf[640:]
                if buf and yielded + len(buf) >= 200 and buf[:1] not in (b"{", b"["):
                    yield buf

    async def _post_tts(
        self,
        client: httpx.AsyncClient,
        *,
        url: str,
        headers: dict[str, str],
        params: dict[str, str],
        payload: dict[str, Any],
        live: bool,
    ) -> httpx.Response:
        """One in-flight ElevenLabs call. HTTP 429 retries; HTTP 401 does not."""
        attempts = 0
        while True:
            async with request_slot.hold(live=live):
                resp = await client.post(
                    url,
                    headers=headers,
                    params=params,
                    json=payload,
                )
            if elevenlabs_error_kind(resp.status_code, resp.text or "") != "rate_limited":
                return resp
            if attempts >= MAX_RATE_LIMIT_RETRIES:
                logger.warning(
                    "ElevenLabs rate limited category=rate_limited retries_exhausted=%s",
                    attempts,
                )
                return resp
            delay = retry_after_seconds(resp.headers.get("Retry-After"), attempts)
            logger.warning(
                "ElevenLabs rate limited category=rate_limited retry_in_s=%.2f attempt=%s",
                delay,
                attempts + 1,
            )
            attempts += 1
            await rate_limit_sleep(delay)


def _is_valid_ulaw_or_audio(data: bytes, *, for_twilio: bool) -> bool:
    """Reject JSON/error bodies that would play as silence on the phone."""
    if not data or len(data) < 200:
        return False
    head = data[:48].lstrip()
    if head[:1] in (b"{", b"[", b"<"):
        return False
    if b'"detail"' in data[:160] or b'"status"' in data[:160]:
        return False
    if b"quota" in data[:120].lower() or b"invalid_request" in data[:120]:
        return False
    # A real clip often starts with mu-law silence (0xFF). Judge the whole buffer,
    # not the first few milliseconds, or valid speech is discarded.
    if for_twilio and (len(data) < 400 or len(set(data)) < 3):
        return False
    return True
