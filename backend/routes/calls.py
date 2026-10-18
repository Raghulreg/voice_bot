import asyncio
import base64
import json
import logging
import time

from pydantic import BaseModel

from fastapi import APIRouter, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from config import get_settings, reload_settings
from models.schemas import (
    InitiateCallRequest,
    InitiateCallResponse,
    PublicUrlRequest,
    PublicUrlResponse,
)
from services.deepgram_stt import DeepgramLiveSession, build_browser_listen_url
from services.conversation_engine import (
    call_records,
    call_sessions,
    conversation_engine,
    create_call_session,
)
from services.call_storage import CALL_DATA_DIR, load_call_json, list_all_call_records, save_call_json
from services.duplicate_detection import check_call_duplicate, find_scenario_duplicates
from services.stt_service import SttService
from services.tts_service import TtsService
from services.twilio_service import TwilioService
from services.vapi_service import VapiService
from services.voice_runtime import (
    VoiceConfigError,
    cached_twilio_account_type,
    resolve_voice_runtime,
    select_voice_transport,
)
from utils.env_file import set_env_value
from utils.phone import normalize_call_phone
from utils.twilio_errors import format_twilio_error
from utils.url import (
    discover_ngrok_url,
    ensure_ngrok_tunnel,
    ensure_reachable_public_url,
    is_public_url_ready,
    normalize_public_url,
    resolve_public_url,
    validate_public_url_for_twilio,
    verify_public_url_reachable,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["calls"])

_health_cache: dict[str, object] = {"expires_at": 0.0}


async def _health_payload() -> dict:
    settings = get_settings()
    effective_url, source = resolve_public_url(settings.public_url)
    now = time.time()
    tts = TtsService()

    reachable = False
    raw_public = (settings.public_url or "").strip()
    if raw_public and not is_public_url_ready(effective_url):
        webhook_message = (
            "PUBLIC_URL is set but is not an absolute http(s) URL. "
            "Twilio needs a full public https URL."
        )
    else:
        webhook_message = "PUBLIC_URL not configured"

    if effective_url and is_public_url_ready(effective_url):
        cache_key = effective_url
        if (
            _health_cache.get("url") == cache_key
            and now < float(_health_cache.get("expires_at", 0))
        ):
            reachable = bool(_health_cache.get("reachable"))
            webhook_message = str(_health_cache.get("message", ""))
        else:
            reachable, webhook_message = await asyncio.to_thread(
                verify_public_url_reachable, effective_url
            )
            _health_cache.update(
                {
                    "url": cache_key,
                    "reachable": reachable,
                    "message": webhook_message if not reachable else "",
                    "expires_at": now + 30.0,
                }
            )

    try:
        account_type = await asyncio.wait_for(
            asyncio.to_thread(cached_twilio_account_type),
            timeout=5,
        )
    except Exception:
        account_type = ""

    vapi = VapiService()
    stt = SttService()
    provider = (settings.call_provider or "media_streams").strip().lower()
    silero_ok = False
    try:
        from services.silero_vad import silero_vad_available

        silero_ok = silero_vad_available()
    except Exception:
        silero_ok = False
    return {
        "status": "ok",
        "twilio_configured": bool(settings.twilio_account_sid and settings.twilio_phone_number),
        "vapi_configured": vapi.is_configured(),
        "call_provider": provider,
        "dialogue_mode": (settings.dialogue_mode or "ai").strip().lower(),
        "silero_vad": silero_ok,
        "stt_configured": stt.is_configured(),
        "stt_provider": getattr(stt, "stt_provider", "auto"),
        "stt_elevenlabs": bool(getattr(stt, "eleven_key", "")),
        "tts_configured": tts.is_configured(),
        "tts_provider": tts.provider_name(),
        "elevenlabs_configured": bool((settings.elevenlabs_api_key or "").strip()),
        "gemini_configured": bool(settings.gemini_api_key),
        "voice_pipeline": resolve_voice_runtime(
            settings,
            account_type=account_type,
        ),
        "public_url": effective_url,
        "public_url_ready": is_public_url_ready(effective_url),
        "public_url_source": source,
        "webhook_reachable": reachable,
        "webhook_message": webhook_message if not reachable else "",
    }


@router.post("/calls/initiate", response_model=InitiateCallResponse)
async def initiate_call(req: InitiateCallRequest):
    """
    Default: Twilio Media Streams (continuous audio → WebSocket → STT).
    Optional: CALL_PROVIDER=vapi for Vapi + Twilio orchestration.
    """
    settings = get_settings()
    provider = (settings.call_provider or "media_streams").strip().lower()

    if req.scenario_id:
        from claims_scenarios import get_scenario_by_id

        if not get_scenario_by_id(req.scenario_id):
            raise HTTPException(
                status_code=400,
                detail=f"Unknown scenario_id: {req.scenario_id}. Choose a valid ID from Guidelines.",
            )

    if req.tpa_sector_id:
        from tpa_sectors import valid_sector_id

        if not valid_sector_id(req.tpa_sector_id):
            raise HTTPException(
                status_code=400,
                detail=f"Unknown tpa_sector_id: {req.tpa_sector_id}. Use GET /api/calls/tpa-sectors.",
            )

    normalized_phone = normalize_call_phone(req.phone_number)
    if req.claim_id and normalized_phone:
        try:
            dup = check_call_duplicate(req.claim_id, normalized_phone, call_records)
        except Exception:
            dup = None
        # Only block when another call for same claim+phone is still active
        if dup and dup.get("block") and dup.get("reason") == "active_call":
            raise HTTPException(status_code=409, detail=dup["message"])
        if dup and not dup.get("block"):
            logger.info(
                "Duplicate soft-warn claim=%s: %s",
                req.claim_id,
                dup.get("message"),
            )

    if provider == "vapi":
        return await _initiate_vapi_call(req, normalized_phone)
    return await _initiate_media_streams_call(req, normalized_phone)


async def _initiate_media_streams_call(
    req: InitiateCallRequest, normalized_phone: str
) -> InitiateCallResponse:
    settings = get_settings()
    tts = TtsService()
    stt = SttService()

    if not settings.twilio_account_sid or not settings.twilio_phone_number:
        raise HTTPException(
            status_code=503,
            detail="Twilio not configured. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER.",
        )
    if not settings.gemini_api_key or not stt.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Media Streams STT requires GEMINI_API_KEY for continuous speech recognition.",
        )
    if not tts.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Agent voice is not configured for the active TTS provider.",
        )

    try:
        effective_url, url_source = ensure_reachable_public_url(settings.public_url)
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    if url_source == "ngrok" and effective_url != normalize_public_url(settings.public_url):
        set_env_value("PUBLIC_URL", effective_url)
        reload_settings()
        logger.info("Auto-updated PUBLIC_URL to live ngrok: %s", effective_url)

    call_id = create_call_session(
        customer_phone=normalized_phone,
        customer_name=req.customer_name,
        agent_name=req.agent_name,
        preferred_language="en-US",
        hospital_name=req.hospital_name,
        claim_id=req.claim_id,
        patient_name=req.patient_name or req.customer_name,
        claim_type=req.claim_type,
        tpa_name=req.resolved_payer_name(),
        npi=req.npi,
        policy_number=req.policy_number,
        claim_age_days=req.claim_age_days,
        scenario_id=req.scenario_id,
        tpa_executive_profile=req.tpa_executive_profile,
        tpa_sector_id=req.tpa_sector_id,
        focus_topics=req.focus_topics,
    )
    session = call_sessions.get(call_id)
    if session:
        # Keep every later Gather/Play callback on the exact validated host
        # selected for this call, even if ngrok or .env changes mid-call.
        session.public_base = normalize_public_url(effective_url)
    try:
        transport = select_voice_transport(
            settings,
            account_type=cached_twilio_account_type(),
        )
    except VoiceConfigError:
        logger.warning("Invalid VOICE_TRANSPORT. Using Twilio Gather.")
        transport = {"active": "gather"}
    if session:
        session.use_media_streams = transport["active"] == "media_streams"
        session.browser_mode = True
        logger.info("Browser session ready call_id=%s. Skipping phone greeting cache.", call_id)

    try:
        twilio = TwilioService()
        call_sid = twilio.initiate_call(
            normalized_phone,
            call_id,
            public_url=effective_url,
        )
        if session:
            session.call_sid = call_sid
            session.browser_mode = str(call_sid).startswith("local-")
        call_records[call_id]["call_sid"] = call_sid
        call_records[call_id]["call_provider"] = (
            "browser"
            if str(call_sid).startswith("local-")
            else "twilio_media_streams"
            if transport["active"] == "media_streams"
            else "twilio_gather"
        )
        call_records[call_id]["status"] = (
            "in-progress" if str(call_sid).startswith("local-") else "calling"
        )
        path_label = (
            "Media Streams" if transport["active"] == "media_streams" else "Twilio Gather"
        )
        return InitiateCallResponse(
            call_id=call_id,
            call_sid=call_sid,
            status="calling",
            message=(
                f"{path_label} call started to {req.phone_number} "
                f"— claim {req.claim_id or 'pending'}"
            ),
        )
    except Exception as e:
        logger.error("Media Streams call initiation failed: %s", e)
        call_records[call_id]["status"] = "failed"
        message, status_code = format_twilio_error(e)
        raise HTTPException(status_code=status_code, detail=message) from e


async def _initiate_vapi_call(
    req: InitiateCallRequest, normalized_phone: str
) -> InitiateCallResponse:
    settings = get_settings()
    vapi = VapiService()
    if not vapi.is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "CALL_PROVIDER=vapi requires VAPI_API_KEY, VAPI_ASSISTANT_ID, and Twilio credentials."
            ),
        )

    call_id = create_call_session(
        customer_phone=normalized_phone,
        customer_name=req.customer_name,
        agent_name=req.agent_name,
        preferred_language="en-US",
        hospital_name=req.hospital_name,
        claim_id=req.claim_id,
        patient_name=req.patient_name or req.customer_name,
        claim_type=req.claim_type,
        tpa_name=req.resolved_payer_name(),
        npi=req.npi,
        policy_number=req.policy_number,
        claim_age_days=req.claim_age_days,
        scenario_id=req.scenario_id,
        tpa_executive_profile=req.tpa_executive_profile,
        tpa_sector_id=req.tpa_sector_id,
        focus_topics=req.focus_topics,
    )

    session = call_sessions.get(call_id)
    patient = req.patient_name or req.customer_name
    payer = req.resolved_payer_name()
    variable_values = {
        "call_id": call_id,
        "agent_name": req.agent_name or settings.default_agent_name,
        "hospital_name": req.hospital_name or "",
        "claim_id": req.claim_id or "",
        "patient_name": patient or "",
        "claim_type": req.claim_type or "Claim Pending",
        "payer_name": payer or "",
        "tpa_name": payer or "",
        "npi": req.npi or "",
        "policy_number": req.policy_number or "",
        "claim_age_days": str(req.claim_age_days) if req.claim_age_days is not None else "",
        "tpa_executive_profile": req.tpa_executive_profile or "",
        "scenario_id": req.scenario_id or "",
        "tpa_sector_id": req.tpa_sector_id or "",
        "focus_topics": ", ".join(req.focus_topics or []),
    }

    effective_url, _ = resolve_public_url(settings.public_url)
    server_url = None
    if is_public_url_ready(effective_url):
        server_url = f"{normalize_public_url(effective_url)}/api/webhooks/vapi"

    try:
        # Vapi+Twilio phone dial is commented out. Talk on the dashboard instead.
        # result = await asyncio.to_thread(
        #     vapi.create_outbound_call,
        #     to_number=normalized_phone,
        #     call_id=call_id,
        #     customer_name=patient,
        #     variable_values=variable_values,
        #     server_url=server_url,
        # )
        logger.info("Vapi outbound call commented out call_id=%s", call_id)
        result = {"id": f"local-{call_id}"}
        vapi_call_id = str(result.get("id") or "")
        call_sid = f"local-{call_id}"
        if session:
            session.call_sid = call_sid
            session.browser_mode = True
        call_records[call_id]["call_sid"] = call_sid
        call_records[call_id]["vapi_call_id"] = vapi_call_id
        call_records[call_id]["call_provider"] = "browser"
        call_records[call_id]["status"] = "in-progress"

        return InitiateCallResponse(
            call_id=call_id,
            call_sid=call_sid or vapi_call_id,
            status="calling",
            message=(
                f"Payer claims follow-up initiated via Vapi+Twilio to {req.phone_number} "
                f"— claim {req.claim_id or 'pending'}"
            ),
        )
    except Exception as e:
        logger.error("Vapi call initiation failed: %s", e)
        call_records[call_id]["status"] = "failed"
        raise HTTPException(status_code=502, detail=str(e)) from e


class LiveTurnRequest(BaseModel):
    text: str


def _wav_b64(wav: bytes) -> str:
    return base64.b64encode(wav).decode("ascii") if wav else ""


@router.post("/calls/{call_id}/live-greeting")
async def live_greeting(call_id: str):
    """Speak the opening line in the browser. Does not dial Twilio."""
    session = call_sessions.get(call_id)
    record = call_records.get(call_id)
    if session is None or record is None:
        raise HTTPException(status_code=404, detail="Call not found")
    session.browser_mode = True
    from services.ivr import fallback_greeting

    text = fallback_greeting(
        session.agent_name,
        session.customer_name,
        session.preferred_language,
        hospital_name=session.hospital_name,
        claim_id=session.claim_id,
        patient_name=session.patient_name,
        claim_type=session.claim_type,
    )
    started = time.perf_counter()
    mulaw = await conversation_engine.tts.text_to_speech(
        text,
        language_code=session.preferred_language,
        for_twilio=True,
        live=True,
        allow_fallback=False,
    )
    logger.info(
        "Browser greeting ready call_id=%s synth_ms=%.0f",
        call_id,
        (time.perf_counter() - started) * 1000,
    )
    if text and not any(item.get("text") == text for item in record.get("transcript") or []):
        session.add_turn("assistant", text)
        session.last_question_asked = text
        record.setdefault("transcript", []).append(
            {"role": "agent", "text": text, "language": session.preferred_language, "source": "browser"}
        )
        conversation_engine._persist_call_async(call_id)
    return {"text": text, "audio_base64": _wav_b64(mulaw), "encoding": "mulaw"}


async def _transcribe_browser_clip(audio: bytes, content_type: str) -> str:
    """Transcribe the microphone clip with Deepgram Nova, not the browser guess."""
    settings = get_settings()
    key = str(getattr(settings, "deepgram_api_key", "") or "").strip()
    model = str(getattr(settings, "deepgram_model", "") or "").strip() or "nova-3"
    if not key:
        raise HTTPException(status_code=503, detail="DEEPGRAM_API_KEY is required to hear the microphone.")
    mime = (content_type or "audio/webm").split(";")[0].strip() or "audio/webm"
    import httpx

    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=3.0)) as client:
        response = await client.post(
            "https://api.deepgram.com/v1/listen",
            params={
                "model": model,
                "language": "en",
                "smart_format": "true",
                "punctuate": "true",
                "numerals": "true",
            },
            headers={"Authorization": f"Token {key}", "Content-Type": mime},
            content=audio,
        )
    if response.status_code >= 400:
        logger.warning("Browser STT HTTP %s", response.status_code)
        return ""
    payload = response.json()
    channels = ((payload.get("results") or {}).get("channels") or [])
    alternatives = (channels[0].get("alternatives") if channels else None) or []
    text = str((alternatives[0] or {}).get("transcript") or "").strip() if alternatives else ""
    logger.info(
        "Browser heard chars=%d stt_ms=%.0f",
        len(text),
        (time.perf_counter() - started) * 1000,
    )
    return " ".join(text.split())


def _pcm16_wav(pcm: bytes, sample_rate: int = 16000) -> bytes:
    import struct

    data_size = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        sample_rate,
        sample_rate * 2,
        2,
        16,
        b"data",
        data_size,
    )
    return header + pcm


async def _transcribe_pcm16(pcm: bytes) -> str:
    """Hear raw 16 kHz microphone audio when the live stream returns no words."""
    if len(pcm) < 3200:
        return ""
    return await _transcribe_browser_clip(_pcm16_wav(pcm), "audio/wav")


async def _reply_to_browser_speech(call_id: str, spoken: str) -> dict[str, str]:
    session = call_sessions.get(call_id)
    record = call_records.get(call_id)
    if session is None or record is None:
        raise HTTPException(status_code=404, detail="Call not found")
    spoken = " ".join(spoken.split())
    live_q = session.browser_live_queue

    async def _finish_live_queue() -> None:
        if live_q is None:
            return
        try:
            live_q.put_nowait(None)
        except Exception:
            pass

    if not spoken:
        await _finish_live_queue()
        return {"text": "", "audio_base64": "", "heard": ""}
    heard = spoken.lower()
    now = time.time()
    last_heard = str(getattr(session, "browser_last_heard", "") or "")
    last_at = float(getattr(session, "browser_last_heard_at", 0) or 0)
    if getattr(session, "browser_turn_busy", False) or (
        heard == last_heard and now - last_at < 5
    ):
        logger.info("Dropped duplicate browser turn call_id=%s", call_id)
        await _finish_live_queue()
        return {"text": "", "audio_base64": "", "heard": spoken}
    session.browser_turn_busy = True
    session.browser_last_heard = heard
    session.browser_last_heard_at = now
    session.browser_mode = True
    session.browser_audio_parts.clear()
    try:
        await conversation_engine.handle_stream_speech(session, spoken, confidence=0.99)
    finally:
        await _finish_live_queue()
        if session.browser_live_queue is live_q or session.browser_live_queue is None:
            session.browser_turn_busy = False
    if live_q is not None:
        reply = ""
        for item in reversed(record.get("transcript") or []):
            if item.get("role") == "agent":
                reply = item.get("text") or ""
                break
        base = session.browser_marks.get("speech_end") or 0
        chunk_at = session.browser_marks.get("first_chunk") or 0
        token_at = session.browser_marks.get("first_token") or 0
        tts_at = session.browser_marks.get("tts_request") or 0
        logger.info(
            "VOICE speech_to_first_chunk call=%s ms=%.0f token_ms=%.0f tts_ms=%.0f",
            call_id,
            ((chunk_at - base) * 1000) if chunk_at and base else -1,
            ((token_at - base) * 1000) if token_at and base else -1,
            ((tts_at - base) * 1000) if tts_at and base else -1,
        )
        return {"text": reply, "audio_base64": "", "heard": spoken}
    mulaw = b"".join(session.browser_audio_parts)
    wav = TtsService.mulaw_to_pcm_wav(mulaw) if mulaw else b""
    reply = ""
    for item in reversed(record.get("transcript") or []):
        if item.get("role") == "agent":
            reply = item.get("text") or ""
            break
    return {"text": reply, "audio_base64": _wav_b64(wav), "heard": spoken}


@router.post("/calls/{call_id}/live-turn")
async def live_turn(call_id: str, body: LiveTurnRequest):
    """Take one spoken line from the browser and return the spoken reply."""
    return await _reply_to_browser_speech(call_id, body.text)


@router.post("/calls/{call_id}/live-hear")
async def live_hear(call_id: str, file: UploadFile = File(...)):
    """Hear the microphone recording, then answer once."""
    audio = await file.read()
    if len(audio) < 1500:
        return {"text": "", "audio_base64": "", "heard": ""}
    spoken = await _transcribe_browser_clip(audio, file.content_type or "audio/webm")
    if not spoken:
        return {"text": "", "audio_base64": "", "heard": ""}
    return await _reply_to_browser_speech(call_id, spoken)


@router.websocket("/calls/{call_id}/live-talk")
async def live_talk(websocket: WebSocket, call_id: str):
    """Browser microphone only. Twilio call, Gather, and Media Streams are not used here."""
    await websocket.accept()
    session = call_sessions.get(call_id)
    record = call_records.get(call_id)
    if session is None or record is None:
        await websocket.send_json({"type": "fallback"})
        await websocket.close()
        return
    settings = get_settings()
    dg = DeepgramLiveSession(settings)
    try:
        key_model = str(getattr(settings, "deepgram_model", "") or "").strip() or "nova-3"
        await dg.connect(url=build_browser_listen_url(key_model))
    except Exception:
        logger.warning("Browser live STT unavailable call_id=%s", call_id)
        try:
            await websocket.send_json({"type": "fallback"})
        except Exception:
            pass
        await dg.close()
        await websocket.close()
        return
    await websocket.send_json({"type": "ready"})
    busy = False
    turn_id = 0
    playback: asyncio.Task | None = None
    heard_audio = bytearray()
    pending_feed = bytearray()
    feed_lock = asyncio.Lock()
    refresh_task: asyncio.Task | None = None

    def _this_turn_only(text: str) -> str:
        spoken = " ".join((text or "").split())
        if not spoken:
            return ""
        previous = ""
        for item in reversed(record.get("transcript") or []):
            if item.get("role") == "customer":
                previous = " ".join(str(item.get("text") or "").split())
                break
        if (
            previous
            and spoken.lower().startswith(previous.lower())
            and len(spoken) > len(previous) + 1
        ):
            spoken = spoken[len(previous) :].strip(" .,!?:;-")
        return " ".join(spoken.split())

    async def _open_fresh_listener() -> None:
        """Next utterance gets a new recognizer so the previous sentence cannot leak."""
        nonlocal dg
        previous = dg
        fresh = DeepgramLiveSession(settings)
        try:
            await fresh.connect(url=build_browser_listen_url(key_model))
        except Exception:
            logger.warning("Browser STT refresh failed call=%s", call_id)
            async with feed_lock:
                if pending_feed and not previous.failed:
                    previous.feed(bytes(pending_feed))
                    pending_feed.clear()
            return
        async with feed_lock:
            dg = fresh
            if pending_feed:
                fresh.feed(bytes(pending_feed))
                pending_feed.clear()
        try:
            await previous.close()
        except Exception:
            pass
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data:
                if not busy:
                    heard_audio.extend(data)
                    if len(heard_audio) > 320_000:
                        del heard_audio[: len(heard_audio) - 320_000]
                    async with feed_lock:
                        if refresh_task is not None and not refresh_task.done():
                            pending_feed.extend(data)
                        else:
                            dg.feed(data)
                continue
            raw = message.get("text")
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = payload.get("type")
            if kind == "interrupt":
                turn_id += 1
                session.tts_stop.set()
                session.browser_turn_busy = False
                await conversation_engine.cancel_generation(session)
                dg.begin_utterance()
                heard_audio.clear()
                pending_feed.clear()
                if playback is not None and not playback.done():
                    playback.cancel()
                session.browser_live_queue = None
                busy = False
                continue
            if kind != "end" or busy:
                continue
            speech_end = time.perf_counter()
            if refresh_task is not None and not refresh_task.done():
                await refresh_task
            pcm = bytes(heard_audio)
            heard_audio.clear()
            logger.info("VOICE speech_end call=%s mic_bytes=%d", call_id, len(pcm))
            spoken = ""
            already = " ".join((dg._latest_final or "").split())
            if not already and dg._latest is not None:
                already = " ".join((dg._latest.transcript or "").split())
            # The microphone already waited out the pause, so the words are in.
            # A long Finalize wait is a second pause before the reply starts.
            stt_wait = 0.2 if already else 0.4
            if not dg.failed and dg._reader is not None and not dg._reader.done():
                spoken = await dg.take_transcript(stt_wait)
            else:
                spoken = " ".join((dg._latest_final or "").split())
                logger.warning("Browser STT socket closed. Opening a new one call=%s", call_id)
                try:
                    await dg.close()
                except Exception:
                    pass
                dg = DeepgramLiveSession(settings)
                try:
                    await dg.connect(url=build_browser_listen_url(key_model))
                except Exception:
                    logger.warning("Browser STT reconnect failed call=%s", call_id)
            spoken = _this_turn_only(spoken)
            transcript_ms = (time.perf_counter() - speech_end) * 1000
            logger.info(
                "VOICE final_transcript call=%s ms=%.0f chars=%d",
                call_id,
                transcript_ms,
                len(spoken),
            )
            if not spoken:
                await websocket.send_json(
                    {"type": "audio_end", "text": "", "audio_base64": "", "heard": ""}
                )
                refresh_task = asyncio.create_task(
                    _open_fresh_listener(),
                    name=f"browser-stt-{call_id}",
                )
                continue
            turn_id += 1
            this_turn = turn_id
            busy = True
            live_q: asyncio.Queue = asyncio.Queue()
            session.browser_live_queue = live_q
            session.browser_marks = {"speech_end": speech_end}
            session.tts_stop.clear()

            async def _play_turn(turn: int, queue: asyncio.Queue, heard: str, started: float) -> None:
                nonlocal busy
                first = True
                try:
                    while True:
                        item = await queue.get()
                        if turn != turn_id or item is None:
                            break
                        if first:
                            first = False
                            logger.info(
                                "VOICE first_chunk_to_socket call=%s ms=%.0f",
                                call_id,
                                (time.perf_counter() - started) * 1000,
                            )
                            await websocket.send_json({"type": "audio_start"})
                        await websocket.send_bytes(item)
                except asyncio.CancelledError:
                    return
                except Exception:
                    logger.exception("Browser playback failed call=%s", call_id)
                if turn != turn_id:
                    busy = False
                    return
                busy = False
                session.browser_turn_busy = False
                reply = ""
                for item in reversed(record.get("transcript") or []):
                    if item.get("role") == "agent":
                        reply = item.get("text") or ""
                        break
                try:
                    await websocket.send_json(
                        {
                            "type": "audio_end",
                            "text": reply,
                            "heard": heard,
                            "audio_base64": "",
                        }
                    )
                except Exception:
                    logger.warning("Could not tell the browser the turn finished call=%s", call_id)

            async def _speak_turn(heard: str) -> None:
                try:
                    await _reply_to_browser_speech(call_id, heard)
                except Exception:
                    logger.exception("Browser turn failed call_id=%s", call_id)
                    if session.browser_live_queue is live_q:
                        live_q.put_nowait(None)

            playback = asyncio.create_task(
                _play_turn(this_turn, live_q, spoken, speech_end),
                name=f"browser-play-{call_id}",
            )
            asyncio.create_task(_speak_turn(spoken), name=f"browser-say-{call_id}")
            refresh_task = asyncio.create_task(
                _open_fresh_listener(),
                name=f"browser-stt-{call_id}",
            )
    except WebSocketDisconnect:
        pass
    finally:
        if refresh_task is not None and not refresh_task.done():
            refresh_task.cancel()
        if playback is not None and not playback.done():
            playback.cancel()
        session.browser_live_queue = None
        await dg.close()


@router.get("/calls")
async def list_calls():
    return {"calls": list_all_call_records(call_records)}


@router.get("/calls/tpa-sectors")
async def list_tpa_sectors_endpoint():
    """Return all 25 TPA service sectors with topics and sample question patterns."""
    from tpa_sectors import list_tpa_sectors

    sectors = list_tpa_sectors()
    return {
        "count": len(sectors),
        "sectors": sectors,
        "message": "Select a sector, optional focus topics, then initiate a call.",
    }


@router.get("/calls/scenarios")
async def list_scenarios(profile: str | None = None):
    """Return gold exemplar scenarios (all 40, or filtered by TPA executive profile)."""
    from claims_scenarios import GOLD_EXEMPLARS, TPA_EXECUTIVE_PROFILES, get_exemplars_by_profile

    if profile:
        scenarios = get_exemplars_by_profile(profile)
        if not scenarios:
            scenarios = []
    else:
        scenarios = GOLD_EXEMPLARS

    return {
        "count": len(scenarios),
        "profiles": TPA_EXECUTIVE_PROFILES,
        "scenarios": scenarios,
    }


@router.get("/calls/scenarios/duplicates")
async def audit_scenario_duplicates(threshold: float = 0.82):
    """Master Prompt step 5 — audit gold exemplars for exact and near-duplicates."""
    from claims_scenarios import GOLD_EXEMPLARS

    if not 0.5 <= threshold <= 1.0:
        raise HTTPException(status_code=400, detail="threshold must be between 0.5 and 1.0")
    return find_scenario_duplicates(GOLD_EXEMPLARS, similarity_threshold=threshold)


@router.get("/calls/scenarios/{scenario_id}")
async def get_scenario(scenario_id: str):
    """Return one gold exemplar scenario by ID."""
    from claims_scenarios import get_scenario_by_id

    scenario = get_scenario_by_id(scenario_id)
    if not scenario:
        raise HTTPException(status_code=404, detail=f"Unknown scenario_id: {scenario_id}")
    return {"scenario": scenario}


@router.get("/calls/{call_id}")
async def get_call(call_id: str):
    record = call_records.get(call_id)
    if not record:
        record = load_call_json(call_id)
    if not record:
        raise HTTPException(status_code=404, detail="Call not found")
    return record


@router.get("/calls/{call_id}/download")
async def download_call_json(call_id: str):
    """Download saved call transcript and responses as JSON."""
    path = CALL_DATA_DIR / f"{call_id}.json"
    if path.exists():
        return FileResponse(
            path,
            media_type="application/json",
            filename=f"call_{call_id}.json",
        )

    record = call_records.get(call_id)
    if not record:
        saved = load_call_json(call_id)
        if not saved:
            raise HTTPException(status_code=404, detail="Call not found")
        path = save_call_json(call_id, saved)
    else:
        path = save_call_json(call_id, record)

    return FileResponse(
        path,
        media_type="application/json",
        filename=f"call_{call_id}.json",
    )


@router.get("/health/ping")
async def health_ping():
    """Lightweight liveness probe — no outbound ngrok check (avoids recursion)."""
    return {"status": "ok"}


@router.get("/health")
async def health():
    return await _health_payload()


@router.post("/public-url", response_model=PublicUrlResponse)
async def save_public_url(req: PublicUrlRequest):
    try:
        url = validate_public_url_for_twilio(req.public_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    reachable, msg = verify_public_url_reachable(url)
    if not reachable:
        raise HTTPException(status_code=400, detail=msg)

    set_env_value("PUBLIC_URL", url)
    reload_settings()

    return PublicUrlResponse(
        public_url=url,
        public_url_ready=True,
        public_url_source="env",
        message="PUBLIC_URL saved to backend/.env",
    )

@router.post("/public-url/discover", response_model=PublicUrlResponse)
async def discover_public_url():
    discovered = discover_ngrok_url()
    if not discovered:
        raise HTTPException(
            status_code=404,
            detail=(
                "ngrok not detected. Open a terminal and run: ngrok http 8000 "
                "then click Auto-detect again or paste the https URL below."
            ),
        )

    try:
        url = validate_public_url_for_twilio(discovered)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    reachable, msg = verify_public_url_reachable(url)
    if not reachable:
        raise HTTPException(status_code=400, detail=msg)

    set_env_value("PUBLIC_URL", url)
    reload_settings()

    return PublicUrlResponse(
        public_url=url,
        public_url_ready=True,
        public_url_source="ngrok",
        message=f"Detected ngrok URL and saved to backend/.env",
    )


@router.post("/public-url/connect", response_model=PublicUrlResponse)
async def connect_public_url():
    """Use the live ngrok tunnel, or start one, and save it. No pasted URL."""
    settings = get_settings()
    source = "ngrok"
    try:
        url, source = await asyncio.to_thread(
            ensure_reachable_public_url, settings.public_url
        )
    except ValueError:
        try:
            url = await asyncio.to_thread(ensure_ngrok_tunnel)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        reachable, msg = await asyncio.to_thread(verify_public_url_reachable, url)
        if not reachable:
            raise HTTPException(status_code=400, detail=msg)

    try:
        url = validate_public_url_for_twilio(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if url != normalize_public_url(settings.public_url or ""):
        set_env_value("PUBLIC_URL", url)
        reload_settings()
        logger.info("Saved live public URL from %s", source)
    _health_cache["expires_at"] = 0.0

    return PublicUrlResponse(
        public_url=url,
        public_url_ready=True,
        public_url_source=source,
        message="Public link connected",
    )

