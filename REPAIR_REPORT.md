# Voice Bot Repair Report

## Changes made

1. Added `DeepgramTtsService` for Deepgram Aura speech generation. It requests Twilio-compatible 8 kHz mu-law audio for phone playback, uses a bounded timeout, and does not include provider response bodies or credentials in raised errors.
2. Added `DEEPGRAM_TTS_MODEL` and `DEEPGRAM_TTS_VOICE` to backend settings so those values are recognized instead of silently ignored.
3. Fixed `TTS_PRIMARY=deepgram` / `deepgram_tts` / `aura` / `aura-2` selection. Previously, an unsupported provider string silently selected Windows SAPI, which could produce a robotic voice while the configuration implied Deepgram.
4. Added a 60-second process-local cooldown after Deepgram TTS failures to avoid repeatedly waiting on a failing provider on every turn; normal provider fallback remains available when enabled.
5. Updated runtime TTS diagnostics, README, `.env.example`, and `.gitignore`.
6. Added mocked Deepgram TTS unit tests. No real Deepgram/Gemini/Twilio requests or phone calls were made.
7. Removed `.env`, `.venv`, `node_modules`, `.git`, `dist`, and local `call_data` from the repaired ZIP.

## Test results

- Python `compileall`: **PASS**
- `tests/test_deepgram_tts.py`: **4 passed** (all HTTP interactions mocked)
- Frontend TypeScript `tsc -b`: **PASS**
- Full backend pytest suite: **BLOCKED at collection** because this environment lacks `google-genai` and `twilio`; package installation was unavailable due network/DNS restrictions. This is not a confirmed application test failure.
- Vite production build: **BLOCKED** because the uploaded `node_modules` does not contain the Linux Rollup optional native package (`@rollup/rollup-linux-x64-gnu`). TypeScript compilation passed. Run `npm ci` on a network-enabled target machine and then `npm run build` to verify the production bundle.
- External credential/API tests, public HTTPS/WSS webhook checks, real-time audio tests, and live calls: **NOT RUN**. They require valid rotated credentials, a real public URL, provider availability, and explicit approval for any billable/outbound activity.

## Important configuration

Set `TTS_PRIMARY=deepgram` and use a valid voice-specific Aura ID such as `DEEPGRAM_TTS_VOICE=aura-2-thalia-en` if that voice is enabled on the account. The configured model/voice has not been verified against the provider account because no external API request was made.

## Security action required

The uploaded source ZIP contained a populated `backend/.env` with provider credentials. Rotate/revoke the credentials present in that file (including Twilio, Deepgram, Gemini, and LiveKit as applicable) and create a fresh local `backend/.env` from `backend/.env.example`. Do not share or commit the real `.env`.
