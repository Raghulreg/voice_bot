import asyncio
import json
import logging
import time
import xml.sax.saxutils as xml_escape

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from config import get_settings
from services.conversation_engine import (
    TWILIO_GATHER_LANGS,
    _say_twiml,
    call_records,
    call_sessions,
    conversation_engine,
)
from services.ivr import DEFAULT_LANGUAGE, build_ivr_script, language_from_digit, next_discovery_question
from services.tts_service import TtsService
from services.voice_config import repeat_response
from utils.url import normalize_public_url, resolve_public_url

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def _call_id_from_vapi_payload(body: dict) -> str:
    meta = body.get("metadata") or {}
    if isinstance(meta, dict) and meta.get("call_id"):
        return str(meta["call_id"])
    message = body.get("message") or {}
    if isinstance(message, dict):
        call = message.get("call") or {}
        if isinstance(call, dict):
            cmeta = call.get("metadata") or {}
            if isinstance(cmeta, dict) and cmeta.get("call_id"):
                return str(cmeta["call_id"])
            overrides = call.get("assistantOverrides") or {}
            if isinstance(overrides, dict):
                ometa = overrides.get("metadata") or {}
                if isinstance(ometa, dict) and ometa.get("call_id"):
                    return str(ometa["call_id"])
                vals = overrides.get("variableValues") or {}
                if isinstance(vals, dict) and vals.get("call_id"):
                    return str(vals["call_id"])
    call = body.get("call") or {}
    if isinstance(call, dict):
        cmeta = call.get("metadata") or {}
        if isinstance(cmeta, dict) and cmeta.get("call_id"):
            return str(cmeta["call_id"])
    return ""


def _append_vapi_transcript(call_id: str, artifact: dict) -> None:
    record = call_records.get(call_id)
    if not record:
        return
    messages = artifact.get("messages") or []
    transcript = list(record.get("transcript") or [])
    existing = {(t.get("speaker"), t.get("text")) for t in transcript if isinstance(t, dict)}
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        text = str(msg.get("message") or msg.get("content") or "").strip()
        if not text or role in ("system", "tool", "function"):
            continue
        speaker = "agent" if role in ("bot", "assistant", "agent") else "customer"
        key = (speaker, text)
        if key in existing:
            continue
        existing.add(key)
        transcript.append({"speaker": speaker, "text": text, "source": "vapi"})
    record["transcript"] = transcript
    summary = artifact.get("summary")
    if summary:
        record["resolution_status"] = str(summary)[:500]


@router.post("/vapi")
async def vapi_server_events(request: Request):
    """Vapi server URL webhook — sync phone call status/transcript into call_records."""
    try:
        body = await request.json()
    except Exception:
        return {"ok": False}

    message = body.get("message") if isinstance(body.get("message"), dict) else body
    msg_type = str((message or {}).get("type") or body.get("type") or "")
    call_id = _call_id_from_vapi_payload(body)
    if not call_id:
        logger.debug("Vapi webhook without call_id type=%s", msg_type)
        return {"ok": True}

    record = call_records.get(call_id)
    session = call_sessions.get(call_id)
    if not record:
        logger.warning("Vapi webhook for unknown call_id=%s type=%s", call_id, msg_type)
        return {"ok": True}

    status_map = {
        "status-update": None,
        "hang": "completed",
        "end-of-call-report": "completed",
    }

    if msg_type == "status-update":
        status = str((message or {}).get("status") or "")
        mapped = {
            "queued": "calling",
            "ringing": "calling",
            "in-progress": "in_progress",
            "forwarding": "in_progress",
            "ended": "completed",
        }.get(status)
        if mapped:
            record["status"] = mapped
            if session:
                session.status = mapped
    elif msg_type in status_map and status_map[msg_type]:
        record["status"] = status_map[msg_type]
        if session:
            session.status = status_map[msg_type]

    if msg_type == "end-of-call-report":
        artifact = (message or {}).get("artifact") or {}
        if isinstance(artifact, dict):
            _append_vapi_transcript(call_id, artifact)
        ended_reason = (message or {}).get("endedReason")
        if ended_reason:
            record["extracted_data"] = {
                **(record.get("extracted_data") or {}),
                "vapi_ended_reason": ended_reason,
            }
        duration = (message or {}).get("durationSeconds") or (message or {}).get("durationMs")
        if duration is not None:
            try:
                seconds = float(duration)
                if seconds > 1000:
                    seconds = seconds / 1000.0
                record["duration_seconds"] = seconds
            except (TypeError, ValueError):
                pass
        try:
            from services.call_storage import save_call_json

            save_call_json(call_id, record)
        except Exception as e:
            logger.warning("Failed to persist Vapi call %s: %s", call_id, e)

    return {"ok": True}


def _public_base(session=None) -> str:
    """Return the call's fixed public base without doing an outbound HTTP check."""
    if session is not None and getattr(session, "public_base", ""):
        return normalize_public_url(session.public_base)
    settings = get_settings()
    url, _ = resolve_public_url(settings.public_url)
    resolved = normalize_public_url(url)
    if session is not None:
        session.public_base = resolved
    return resolved


def _find_session(call_id: str = "", call_sid: str = ""):
    if call_id and call_id in call_sessions:
        return call_sessions[call_id]
    if call_sid:
        for session in call_sessions.values():
            if session.call_sid == call_sid:
                return session
    return None


def _silence_wav_ms(ms: int = 200) -> bytes:
    """Minimal WAV silence so Twilio Play never 404s (avoids 'application error')."""
    import io
    import wave

    rate = 8000
    frames = int(rate * ms / 1000)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * frames)
    return buf.getvalue()


def _error_twiml(message: str) -> str:
    """Last-resort TwiML when the app cannot run — avoids Twilio 'application error'.

    Speaks only in the explicitly configured Say voice so an error never
    introduces a different voice; otherwise it hangs up silently.
    """
    say = _say_twiml(message).strip()
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response>{say}<Hangup/></Response>"
    )


def _xml_url(url: str) -> str:
    """Escape URL for use inside TwiML XML (& must be &amp; or Twilio says 'application error')."""
    return xml_escape.escape(url, {"'": "&apos;", '"': "&quot;"})


@router.post("/voice")
async def voice_webhook(request: Request, call_id: str = Query(default="")):
    """Answer call and connect Twilio Media Streams (continuous audio → STT)."""
    logger.info("Voice webhook hit for call_id=%s", call_id)
    try:
        session = call_sessions.get(call_id)
        if not call_id or not session:
            logger.error("Voice webhook: unknown call_id=%s", call_id)
            return Response(
                content=_error_twiml("Sorry, this call session was not found. Please try again."),
                media_type="application/xml",
            )

        session.preferred_language = DEFAULT_LANGUAGE
        session.ivr_completed = True
        session.authentication_done = True
        session.current_stage = session.current_stage or "authentication"
        from services.voice_runtime import (
            VoiceConfigError,
            cached_twilio_account_type,
            select_voice_transport,
        )

        try:
            transport = select_voice_transport(
                get_settings(),
                account_type=cached_twilio_account_type(),
            )
        except VoiceConfigError:
            logger.warning("Invalid VOICE_TRANSPORT. Using Twilio Gather.")
            transport = {"active": "gather", "reason": "invalid VOICE_TRANSPORT"}
        session.use_media_streams = transport["active"] == "media_streams"
        if call_id in call_records:
            call_records[call_id]["selected_language"] = DEFAULT_LANGUAGE
            call_records[call_id]["call_provider"] = (
                "twilio_media_streams" if session.use_media_streams else "twilio_gather"
            )
            call_records[call_id]["conversation_state"] = {
                **(call_records[call_id].get("conversation_state") or {}),
                "authentication_done": True,
                "current_stage": session.current_stage,
            }
        try:
            from services.conversation_state_db import persist_session_state

            persist_session_state(session)
        except Exception:
            logger.exception("conversation_state save failed after voice webhook")

        base = _public_base(session)
        if session.use_media_streams:
            logger.info(
                "Voice TwiML Media Streams selected for call_id=%s",
                call_id,
            )
            return Response(
                content=conversation_engine.build_media_stream_twiml(session, base),
                media_type="application/xml",
            )
        # Gather is the phone path unless VOICE_TRANSPORT=media_streams and the
        # account allows the Stream verb. Trial accounts strip Stream.
        started = time.perf_counter()
        twiml = await conversation_engine.build_bounded_intro_twiml(session, base)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if "<Play>" in twiml:
            response_kind = "play"
        elif "<Pause" in twiml:
            response_kind = "hold"
        else:
            response_kind = "other"
        logger.info(
            "Voice TwiML AI conversation sent for call_id=%s bytes=%d elapsed_ms=%.0f response=%s",
            call_id,
            len(twiml),
            elapsed_ms,
            response_kind,
        )
        if elapsed_ms >= 4500:
            logger.error(
                "Voice webhook stayed open past the tunnel response window call_id=%s elapsed_ms=%.0f",
                call_id,
                elapsed_ms,
            )
        return Response(content=twiml, media_type="application/xml")
    except Exception as e:
        logger.exception("Voice webhook failed: %s", e)
        return Response(
            content=_error_twiml("Sorry, we had a technical issue starting the call."),
            media_type="application/xml",
        )


@router.get("/ivr-audio")
async def ivr_audio(call_id: str = Query(default="")):
    """ElevenLabs TTS audio for Twilio Play."""
    session = call_sessions.get(call_id)
    if not session:
        logger.error("IVR audio: unknown call_id=%s", call_id)
        return Response(status_code=404, content="Unknown call")

    if session.ivr_audio_bytes:
        return Response(content=session.ivr_audio_bytes, media_type="audio/wav")

    try:
        text = build_ivr_script(
            session.agent_name,
            session.customer_name,
            hospital_name=session.hospital_name,
            claim_id=session.claim_id,
        )
        tts = TtsService()
        mulaw = await tts.text_to_speech(text, language_code="en-US", for_twilio=True, fast=True)
        session.ivr_audio_bytes = tts.mulaw_to_pcm_wav(mulaw)
        return Response(content=session.ivr_audio_bytes, media_type="audio/wav")
    except Exception as e:
        logger.exception("IVR ElevenLabs TTS failed for call %s: %s", call_id, e)
        return Response(status_code=503, content="TTS unavailable")


@router.post("/language")
async def language_webhook(request: Request, call_id: str = Query(default="")):
    """Step 2: Keypad language → start AI conversation (HTTP speech gather)."""
    try:
        form = await request.form()
        digit = str(form.get("Digits", "") or "").strip()
        session = call_sessions.get(call_id)
        if not call_id or not session:
            logger.error("Language webhook: unknown call_id=%s digit=%s", call_id, digit)
            return Response(
                content=_error_twiml("Call session expired. Please start a new call from the dashboard."),
                media_type="application/xml",
            )

        fallback_lang = session.preferred_language
        lang_code, lang_name = language_from_digit(digit, fallback_lang)
        logger.info("Language webhook: digit=%r → %s (%s) for call %s", digit, lang_name, lang_code, call_id)

        if lang_code != session.preferred_language:
            conversation_engine.reset_audio_cache(session)
        session.preferred_language = lang_code
        session.ivr_completed = True
        session.authentication_done = True
        if not session.current_stage or session.current_stage == "greeting":
            session.current_stage = "authentication"
        try:
            from services.conversation_state_db import persist_session_state

            persist_session_state(session)
        except Exception:
            logger.exception("conversation_state save failed after language webhook")

        if call_id in call_records:
            call_records[call_id]["selected_language"] = lang_code
            call_records[call_id]["ivr"] = {
                "digit_pressed": digit,
                "language_code": lang_code,
                "language_name": lang_name,
            }
            call_records[call_id].setdefault("transcript", []).append(
                {
                    "role": "system",
                    "text": f"Customer selected {lang_name} (pressed {digit})",
                    "language": lang_code,
                    "stage": "ivr",
                }
            )

        logger.info("Language %s selected for call %s — starting conversation", lang_name, call_id)
        base = _public_base(session)
        try:
            from services.echo_bot import is_echo_mode

            if not is_echo_mode(get_settings().dialogue_mode):
                if not session.phrase_cache.get("greeting"):
                    await conversation_engine.precache_intro(session)
                await conversation_engine.precache_backchannels(session)
                asyncio.create_task(conversation_engine.precache_replies(session))
            twiml = await conversation_engine.build_intro_twiml(session, base)
            return Response(content=twiml, media_type="application/xml")
        except Exception as e:
            logger.exception("Intro TwiML failed for call %s: %s", call_id, e)
            return Response(
                content=_error_twiml("Sorry, we could not start the conversation. Please try again."),
                media_type="application/xml",
            )
    except Exception as e:
        logger.exception("Language webhook failed: %s", e)
        return Response(
            content=_error_twiml("Sorry, we could not connect the AI agent."),
            media_type="application/xml",
        )


@router.post("/conversation")
async def conversation_webhook(
    request: Request,
    call_id: str = Query(default=""),
    hangup: int = Query(default=0),
):
    """Agent speaks → Twilio Gather (en-US STT) → Gemini → next question."""
    if hangup:
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
            media_type="application/xml",
        )

    session = call_sessions.get(call_id)
    if not call_id or not session:
        logger.error("Conversation webhook: unknown call_id=%s", call_id)
        return Response(
            content=_error_twiml("Call session expired. Please start a new call from the dashboard."),
            media_type="application/xml",
        )

    form = await request.form()
    recording_url = (form.get("RecordingUrl") or "").strip()
    recording_duration = float(form.get("RecordingDuration") or 0)
    speech = (form.get("SpeechResult") or "").strip()
    base = _public_base(session)

    try:
        raw_conf = form.get("Confidence")
        try:
            confidence = float(raw_conf) if raw_conf not in (None, "") else 0.0
        except (TypeError, ValueError):
            confidence = 0.0

        twilio_speech = speech
        speech = ""
        lang = session.preferred_language

        # Twilio Gather — US English speech recognition
        if lang in TWILIO_GATHER_LANGS and twilio_speech:
            speech = twilio_speech
            logger.info(
                "Call %s Twilio fast speech (conf=%.2f): %s",
                call_id,
                confidence,
                speech[:120],
            )
        elif recording_url and recording_duration >= 0.25:
            logger.info("Call %s recording %.1fs — no external STT (use Gather)", call_id, recording_duration)
            record = call_records.get(call_id)
            if record is not None:
                recordings = record.setdefault("recordings", [])
                if isinstance(recordings, list) and recording_url not in recordings:
                    recordings.append(recording_url)
                record["recording_url"] = recording_url
                record["recording_duration"] = recording_duration

        if not speech and twilio_speech:
            speech = twilio_speech
            logger.info(
                "Call %s Twilio fallback (conf=%.2f): %s",
                call_id,
                confidence,
                speech[:120],
            )

        if speech:
            conversation_engine.begin_customer_turn(session, confidence, len(speech))
            twiml = await conversation_engine.build_bounded_speech_response_twiml(
                session, speech, base
            )
        elif not session.greeting_done:
            twiml = await conversation_engine.build_intro_twiml(session, base)
        else:
            reprompt_lang = session.preferred_language
            q = session.last_question_asked or next_discovery_question(
                reprompt_lang, session.discovery_index
            )
            reprompt = repeat_response(reprompt_lang, q)
            session.last_question_asked = q
            twiml = await conversation_engine._build_agent_twiml(
                session, base, reprompt, reprompt_lang
            )

        logger.info("Conversation TwiML ready for call %s (%d bytes)", call_id, len(twiml))
        return Response(content=twiml, media_type="application/xml")
    except Exception as e:
        logger.exception("Conversation failed for call %s: %s", call_id, e)
        return Response(
            content=_error_twiml("Sorry, we had a technical issue. Please try again later."),
            media_type="application/xml",
        )


@router.post("/gather-partial")
async def gather_partial_webhook(call_id: str = Query(default="")):
    """Twilio partialResultCallback. Stores arrival time only; returns no TwiML."""
    session = call_sessions.get(call_id)
    if session is not None:
        conversation_engine.record_speech_partial(session)
    return Response(status_code=204)


@router.post("/conversation-continue")
async def conversation_continue_webhook(
    request: Request,
    call_id: str = Query(default=""),
):
    """Play Gemini reply after instant backchannel (no new STT on this hop)."""
    del request
    session = call_sessions.get(call_id)
    if not call_id or not session:
        return Response(
            content=_error_twiml("Call session expired."),
            media_type="application/xml",
        )
    base = _public_base(session)
    try:
        twiml = await conversation_engine.complete_pending_twiml(session, base)
        logger.info("Conversation-continue TwiML for call %s (%d bytes)", call_id, len(twiml))
        return Response(content=twiml, media_type="application/xml")
    except Exception as e:
        logger.exception("Conversation-continue failed for call %s: %s", call_id, e)
        return Response(
            content=_error_twiml("Sorry, we had a technical issue. Please try again."),
            media_type="application/xml",
        )


@router.get("/agent-audio")
async def agent_audio(call_id: str = Query(default=""), turn: int = Query(default=0)):
    """ElevenLabs TTS audio for Twilio Play during the conversation."""
    session = call_sessions.get(call_id)
    if not session:
        return Response(status_code=404, content="Unknown call")
    conversation_engine.mark_audio_fetched(session, turn)
    audio = await conversation_engine.ensure_playback_audio(session, turn)
    if not audio:
        logger.error("Agent audio missing call_id=%s turn=%s", call_id, turn)
        # Last resort: short silence WAV so Twilio does not play "application error"
        silence = _silence_wav_ms(200)
        return Response(content=silence, media_type="audio/wav")
    return Response(content=audio, media_type="audio/wav")


@router.post("/status")
async def status_webhook(request: Request, call_id: str = Query(default="")):
    form = await request.form()
    call_status = form.get("CallStatus", "")
    call_sid = form.get("CallSid", "")

    if call_id and call_id in call_records:
        record = call_records[call_id]
        record["status"] = call_status
        if call_status == "completed":
            record["duration_seconds"] = float(form.get("CallDuration", 0))
            conversation_engine._persist_call(call_id)

    logger.info("Call %s status: %s (sid: %s)", call_id, call_status, call_sid)
    return {"ok": True}


@router.websocket("/media-stream")
async def media_stream(websocket: WebSocket, call_id: str = Query(default="")):
    """
    FastAPI WebSocket server for Twilio Media Streams.

    Flow (realtime — do not wait for end of speech to forward):
      Twilio → WebSocket → receive ~20ms audio packets → forward immediately → STT worker
    """
    await websocket.accept()
    logger.info("Media stream WebSocket accepted call_id=%s", call_id or "(pending)")

    session = _find_session(call_id)
    resolved_call_id = call_id
    stream_started = False
    greeting_sent = False

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            event = data.get("event")

            if event == "connected":
                logger.info("Media stream connected call_id=%s", resolved_call_id or call_id)

            elif event == "start":
                start = data.get("start", {})
                custom = start.get("customParameters") or {}

                if not session:
                    resolved_call_id = custom.get("call_id") or call_id
                    session = _find_session(resolved_call_id, start.get("callSid", ""))

                if not session:
                    logger.error(
                        "Stream start with no session call_id=%s sid=%s",
                        resolved_call_id,
                        start.get("callSid"),
                    )
                    break

                session.websocket = websocket
                session.stream_sid = start["streamSid"]
                session.call_sid = start.get("callSid", session.call_sid)
                session.use_media_streams = True
                stream_started = True
                resolved_call_id = session.call_id
                if resolved_call_id in call_records:
                    call_records[resolved_call_id]["status"] = "in_progress"
                    call_records[resolved_call_id]["call_provider"] = "twilio_media_streams"
                logger.info("Stream started sid=%s call_id=%s", session.stream_sid, resolved_call_id)

                # Start immediate-forward + streaming STT before greeting
                from services.audio_stream_pipeline import attach_pipeline

                attach_pipeline(
                    session,
                    on_final=lambda text, s=session: conversation_engine.process_stream_utterance(
                        s, text
                    ),
                    on_partial=lambda text, is_final, s=session: conversation_engine.handle_partial_transcript(
                        s, text, is_final
                    ),
                    on_barge_in=lambda s=session: conversation_engine.handle_barge_in(s),
                    # Only TTS playback blocks listen — not LLM think / is_processing
                    is_bot_speaking=lambda s=session: bool(s.is_agent_speaking),
                    stt=conversation_engine.stt,
                )

                if not greeting_sent:
                    greeting_sent = True
                    # Do not block the WS read loop on Gemini+TTS (can stall audio)
                    asyncio.create_task(
                        conversation_engine.start_greeting(session),
                        name=f"greeting-{session.call_id}",
                    )

            elif event == "media" and session and stream_started:
                # ~20ms inbound μ-law packet — forward immediately (no end-of-speech wait)
                media = data.get("media") or {}
                if media.get("track") == "outbound":
                    continue
                payload = media.get("payload") or ""
                if payload:
                    conversation_engine.handle_media_chunk(session, payload)

            elif event == "mark" and session:
                mark_name = data.get("mark", {}).get("name", "")
                if mark_name == "agent_done":
                    session.is_agent_speaking = False
                    pipeline = getattr(session, "audio_pipeline", None)
                    if pipeline is not None and getattr(pipeline, "_vad", None) is not None:
                        try:
                            pipeline._vad.reset()
                        except Exception:
                            pass

            elif event == "stop":
                logger.info("Stream stopped for call %s", resolved_call_id)
                if resolved_call_id in call_records:
                    call_records[resolved_call_id]["status"] = "completed"
                break

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for call %s", resolved_call_id or call_id)
    except Exception as e:
        logger.exception("Media stream error for call %s: %s", resolved_call_id or call_id, e)
    finally:
        if session:
            pipeline = getattr(session, "audio_pipeline", None)
            if pipeline is not None:
                try:
                    await pipeline.stop()
                except Exception:
                    logger.exception("Failed stopping audio pipeline for %s", resolved_call_id)
        if session and stream_started:
            session.status = "completed"
            if resolved_call_id in call_records:
                call_records[resolved_call_id]["status"] = "completed"
            conversation_engine._persist_call(resolved_call_id)
        elif not session:
            logger.warning("Media stream closed without ever finding a session")
