# Hospital Payer Claims Follow-Up Voice Agent

Automate routine, high-volume **outbound follow-up calls** from hospital Revenue Cycle Management (RCM) to health insurance payers.

**Default phone path:** Twilio **Play + Gather** (`VOICE_TRANSPORT=gather`).  
The optional Media Streams path requires `VOICE_TRANSPORT=media_streams`, a Full
Twilio account, and configured Deepgram STT.
**Default dialogue:** claims AI from `prompt.py` (`DIALOGUE_MODE=ai`). Set `DIALOGUE_MODE=echo` only for latency testing without LLM.

## Architecture

Optional real-time path (not available on Twilio Trial):

```
Caller
    │
Twilio Media Streams
    │
WebSocket Server (FastAPI)
    │
Silero VAD
    │
Streaming STT
    │
Gemini Streaming LLM
    │
Streaming TTS (configured provider via `TTS_PRIMARY`)
    │
Twilio
    │
Caller
```

| Stage | What happens |
|-------|----------------|
| **Caller → Twilio Media Streams** | Continuous μ-law audio (~20ms packets) |
| **WebSocket Server (FastAPI)** | `/api/webhooks/media-stream` — immediate packet forward |
| **Silero VAD** | ~50ms speak / not-speak; silence timer; barge-in |
| **Streaming STT** | Deepgram `DEEPGRAM_MODEL`, with existing STT fallback |
| **Gemini Streaming LLM** | Token stream → sentence 1 ASAP (PII masked first) |
| **Streaming TTS** | Configured `ELEVENLABS_MODEL_ID` — first sentence speaks immediately |
| **Twilio → Caller** | Media Streams outbound audio |

Supporting infra: **PostgreSQL** (conversation state + call results), **Redis + Celery** (summaries / analytics / email), **OpenTelemetry / CloudWatch / Sentry** (latency).

## Stack

| Service | Role |
|---------|------|
| **Twilio Media Streams** | Continuous caller audio over WebSocket |
| **FastAPI WebSocket** | Packet pipeline + dialogue orchestration |
| **Silero VAD** | ~50ms speaking / not-speaking |
| **Gemini (`GEMINI_MODEL`)** | Streaming STT + streaming LLM (AI mode) |
| **TTS (`TTS_PRIMARY`)** | Select Deepgram Aura, ElevenLabs, Gemini TTS, or Windows SAPI explicitly |
| **Echo Bot** | Foundation: Hello → Hello Back (no LLM) |
| **PostgreSQL** | Conversation state + call results |
| **Redis + Celery** | Background jobs (summary, analytics, email) |

## Silero VAD

```
Audio packet (~20ms)
  → Silero VAD window (~32ms)
  → Human speaking  OR  Human not speaking  (~50ms)
  → on speech_end → finalize transcript
```

Install: `pip install silero-vad onnxruntime torch`

## Silence timer (80ms)

After Silero VAD says the human stopped speaking, **do not** call the LLM immediately.
Wait `SILENCE_TIMER_MS` (default **80**):

```
Hello....
(wait)
I need....
(wait)
Help
  → silence timer completes
  → LLM / Echo Bot
```

If speech resumes during the wait, the timer is cancelled (no bot interrupt). Tune later via `.env`.

## Barge-in (interruption)

When the bot is speaking and the human starts talking:

```
Bot speaking → VAD speech_start → Twilio clear (stop TTS)
             → cancel Gemini generation_task
             → listen to human
```

Twilio Media Streams `{"event":"clear"}` is the equivalent of `response.stream_stop()`.

## Streaming LLM

Not: wait for full Gemini paragraph → then TTS.

Instead (Media Streams AI / scenario mode):

```
Transcript → Gemini token stream
  → response_text sentence 1 → TTS (starts ASAP)
  → sentence 2 → TTS (overlaps with further tokens)
  → sentence 3 → TTS
```

`response_text` is the first JSON key so speech can begin before metadata finishes. Backchannel (if enabled) plays while Gemini already streams.

## Conversation state (PostgreSQL)

Durable call progress is stored in Postgres — not only prompt history.

| Column | Meaning |
|--------|---------|
| `current_claim` | Hospital + claim label |
| `current_caller` | Who we're talking to |
| `current_intent` | e.g. next_action |
| `current_stage` | greeting → … → ended |
| `authentication_done` | Provider ID&V / live-rep reached |
| `appeal_started` | Appeal path active |
| `claim_number` | Claim ID |

Table `conversation_state` is created **once** at backend startup (`CREATE TABLE IF NOT EXISTS`). Each turn only **UPSERTs** the row.

Configure via `.env`: `DB_HOST`, `DB_PORT`, `DB_USERNAME`, `DB_PASSWORD`, `DB_NAME` (or `DATABASE_URL`).

## Latency logging (OpenTelemetry + CloudWatch + Sentry)

Every dialogue turn records:

| Metric | Meaning |
|--------|---------|
| `stt_ms` | Streaming STT final transcription |
| `llm_ms` | Gemini generation |
| `tts_ms` | ElevenLabs synthesis |
| `twilio_delay_ms` | Media Streams WebSocket send overhead |
| `total_response_ms` | STT + handler wall time |
| `first_audio_ms` | Time to first playable TTS audio |

Emitted as:
- Structured logs: `LATENCY …` and `LATENCY_JSON {…}`
- **OpenTelemetry** spans/metrics (`OTEL_ENABLED`, optional `OTEL_EXPORTER_OTLP_ENDPOINT`)
- **CloudWatch** custom metrics when `CLOUDWATCH_ENABLED=true` (namespace `CallAgent/Latency`)
- **Sentry** breadcrumbs + slow-turn warnings when `SENTRY_DSN` is set (`LATENCY_SLOW_TOTAL_MS`, default 3000)

## PII guardrails

Before any transcript / history is sent to Gemini, sensitive values are masked:

| Field | Token |
|-------|--------|
| Patient Name | `[PATIENT_NAME]` |
| DOB | `[DOB]` |
| Insurance / Policy ID | `[INSURANCE_ID]` |
| SSN | `[SSN]` |

Example: `John Smith` → `[PATIENT_NAME]`. Toggle with `PII_MASKING_ENABLED`.

## Redis + Celery

Background jobs (after call end):

- Call summaries
- Analytics aggregation
- Summary email (optional SMTP)
- Batch re-processing

```bash
# Terminal A — Redis
redis-server

# Terminal B — Celery worker (from backend/)
celery -A worker.celery_app.celery_app worker --loglevel=info
```

## PostgreSQL storage

| Table | Stores |
|-------|--------|
| `conversation_state` | Current claim, caller, intent, stage, auth/appeal flags, claim number |
| `call_results` | Call results, claim status, reference numbers, redacted transcript, summary, metrics |

Created once at startup (`CREATE TABLE IF NOT EXISTS`); runtime only UPSERTs.

## Echo Bot (foundation)

```
Human: Hello → Transcript: Hello → Bot: Hello Back
```

If `echo_last.stt_to_first_audio_ms` is near **~500ms**, the Media Streams foundation is solid.  
Latency is saved on the call JSON (`echo_last`, `echo_latency`).

## Streaming TTS

```
Reply text → first sentence → TTS immediately → start speaking
           → remaining sentences stream while audio plays
```

Uses the provider selected by `TTS_PRIMARY`. For Deepgram Aura, configure `DEEPGRAM_API_KEY` and a voice-specific `DEEPGRAM_TTS_VOICE` (recommended) or `DEEPGRAM_TTS_MODEL`. For ElevenLabs, only the explicitly configured `ELEVENLABS_MODEL_ID` is used; Flash v2.5 must not be selected until account access and project approval are verified.

## Media Streams (optional)

```
Caller → Twilio Media Streams → WebSocket
       → Silero VAD (~50ms speaking / not-speaking)
       → Audio Chunk → Streaming STT → Partial / Updated Transcript
       → VAD speech_end → wait 80ms (silence timer; cancel if speech resumes)
       → Final Transcript → Echo/AI → Streaming TTS
```

Uses `DEEPGRAM_MODEL=nova-3` when Deepgram is configured, with the existing STT
fallback. Requires a Full Twilio account and public HTTPS/WSS (`ngrok http 8000`).

## ElevenLabs setup (agent voice + Voice & Quality)

1. API key: https://elevenlabs.io/app/settings/api-keys — enable **Text to Speech → Access**. Listing voices also needs **voices_read**; without it, `/v1/voices` returns HTTP 401 `missing_permissions` even when speech synthesis is allowed.
2. Set `ELEVENLABS_VOICE_ID` to a voice this account can synthesize. Keep `ELEVENLABS_MODEL_ID=eleven_turbo_v2_5` unless Flash v2.5 is approved and present on the account.
3. Some Voice Library voices return HTTP 402 `payment_required` on a free plan. That is not an invalid key. Gemini TTS, then Windows SAPI, speaks if ElevenLabs cannot.
4. Add to `backend/.env`:
   ```env
   ELEVENLABS_API_KEY=your_key
   ELEVENLABS_VOICE_ID=your_voice_design_id
   ELEVENLABS_MODEL_ID=eleven_turbo_v2_5
   DEFAULT_LANGUAGE=en-US
   ```

## Quick Start

```bash
cd backend
pip install -r requirements.txt
python main.py
```

```bash
ngrok http 8000   # set PUBLIC_URL in .env
```

```bash
cd frontend && npm install && npm run dev
```

Open http://localhost:5173 — ready when Twilio, Gemini, ElevenLabs, and PUBLIC_URL are configured.

## Architecture

```
Frontend → FastAPI WebSocket (/api/webhooks/media-stream)
                         ↑
              Twilio Media Streams (~20ms packets)
                         ↓
              forward immediately (async queue)
                         ↓
              Streaming STT (GEMINI_MODEL from .env)
                         ↓
              partial → updated → final transcript → dialogue → TTS
```

Packets are forwarded as they arrive. STT updates a **partial transcript** on each audio window, then a **final transcript** after silence — using `GEMINI_MODEL` from `.env` (not Deepgram/Assembly/ElevenLabs STT).

Call output saved to `backend/call_data/{call_id}.json`

## Where conversations are saved

**Not a database** — JSON files on disk.

| Layer | Location | When |
|-------|----------|------|
| Live call | In-memory (`call_records`) | Updates each turn |
| Persistent | `backend/call_data/{call_id}.json` | Saved during call + on hangup |
| UI | Call History → Download JSON | Reads memory + files |

Each JSON file includes: transcript, Q&A pairs, `resolution_code`, `denial_code`, `confidence_score`, claim fields, timestamps.

## Latency (fast conversation)

| Step | Speed | Optimization |
|------|-------|--------------|
| Agent reply | **Instant** (cached) | Four ack variants per workflow step pre-generated in ElevenLabs when call starts |
| Caller speech | **Continuous** | Twilio Media Streams → WebSocket → STT (no Record wait) |
| LLM | **Skipped on most turns** | Scripted ack + next question (no Gemini wait) |
| TTS cache miss | ~1–2s | Pre-warm on "Start Call" |

**Turn metrics** are stored on each transcript line in `call_data/{call_id}.json` (`handler_ms`, `llm_ms`, `tts_ms`, `gap_since_last_agent_ms`, `twilio_stt_confidence`) plus call-level `latency_summary`.

**Voice tuning** (optional in `backend/.env`): `ELEVENLABS_STABILITY`, `ELEVENLABS_SIMILARITY`, `ELEVENLABS_STYLE`, `ELEVENLABS_SPEED`, `ELEVENLABS_VOLUME_GAIN` — see `.env.example`.

Prompt rules in `prompt.py`: max **25 words** per reply, one question only, 2 exemplars when Gemini runs.
