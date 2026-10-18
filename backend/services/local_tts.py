"""Local Windows SAPI TTS fallback when cloud TTS is unavailable.

Single locked US male voice at an energetic natural pace (not sleepy).
Produces μ-law 8 kHz for Twilio Media Streams.

NOTE: SAPI is never as natural as ElevenLabs Josh — top up credits for real US energy.
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import xml.sax.saxutils
from pathlib import Path

from services.audio_utils import wav_to_mulaw
from services.voice_config import normalize_tts_text

logger = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^\w\s.,!?'\-]")

# Speech rate -10..+10. +1 = normal-to-bright US phone (not racing).
_SAPI_RATE = 1
_SAPI_VOLUME = 100
_PROSODY_RATE = "+0%"
_PROSODY_PITCH = "+4%"
_LOCKED_VOICE = "Microsoft David Desktop"


def _sanitize(text: str) -> str:
    clean = normalize_tts_text(text) or ""
    clean = _UNSAFE.sub(" ", clean)
    clean = " ".join(clean.split())
    clean = clean.strip()
    if clean and clean[-1] not in ".!?":
        clean = clean + "."
    return clean[:500]


def _sapi_to_wav_sync(text: str, wav_path: Path) -> None:
    """Synthesize with Windows System.Speech (SSML energy + rate) into a WAV file."""
    import subprocess

    spoken = _sanitize(text)
    if not spoken:
        raise ValueError("Local TTS text is empty")

    xml_body = xml.sax.saxutils.escape(spoken)
    ssml = (
        "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='en-US'>"
        f"<prosody rate='{_PROSODY_RATE}' pitch='{_PROSODY_PITCH}'>{xml_body}</prosody>"
        "</speak>"
    )
    # Write SSML to temp file so we avoid PowerShell quote hell
    ssml_path = wav_path.with_suffix(".ssml")
    ssml_path.write_text(ssml, encoding="utf-8")
    wav = str(wav_path).replace("'", "''")
    ssml_file = str(ssml_path).replace("'", "''")
    ps_plain = spoken.replace("'", "''")
    locked = _LOCKED_VOICE.replace("'", "''")

    script = f"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.Rate = {_SAPI_RATE}
$s.Volume = {_SAPI_VOLUME}
$locked = '{locked}'
$picked = $null
$v = $s.GetInstalledVoices() | ForEach-Object {{ $_.VoiceInfo }} |
  Where-Object {{ $_.Name -eq $locked -or $_.Name -like ($locked + '*') }} |
  Select-Object -First 1
if ($v) {{ $picked = $v.Name }}
if (-not $picked) {{
  $v = $s.GetInstalledVoices() | ForEach-Object {{ $_.VoiceInfo }} |
    Where-Object {{ $_.Gender -eq 'Male' -and $_.Culture.Name -like 'en-US*' }} |
    Select-Object -First 1
  if ($v) {{ $picked = $v.Name }}
}}
if ($picked) {{ $s.SelectVoice($picked) }}
$s.SetOutputToWaveFile('{wav}')
try {{
  $ssml = Get-Content -LiteralPath '{ssml_file}' -Raw -Encoding UTF8
  $s.SpeakSsml($ssml)
}} catch {{
  $s.Speak('{ps_plain}')
}}
$used = $s.Voice.Name
$s.Dispose()
Write-Output $used
"""
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0 or not wav_path.is_file() or wav_path.stat().st_size < 44:
        err = (result.stderr or result.stdout or "unknown").strip()[:300]
        raise RuntimeError(f"Windows SAPI TTS failed: {err}")
    voice_used = (result.stdout or "").strip().splitlines()
    voice_name = voice_used[-1] if voice_used else "?"
    logger.info(
        "Local SAPI energetic US voice=%s rate=%d prosody=%s/%s",
        voice_name,
        _SAPI_RATE,
        _PROSODY_RATE,
        _PROSODY_PITCH,
    )


async def text_to_mulaw(text: str) -> bytes:
    """Async wrapper — Windows SAPI → WAV → μ-law 8 kHz."""
    spoken = _sanitize(text)
    if not spoken:
        raise ValueError("Local TTS text is empty")

    def _run() -> bytes:
        with tempfile.TemporaryDirectory(prefix="local_tts_") as tmp:
            wav_path = Path(tmp) / "speech.wav"
            _sapi_to_wav_sync(spoken, wav_path)
            return wav_to_mulaw(wav_path.read_bytes())

    mulaw = await asyncio.to_thread(_run)
    if not mulaw:
        raise RuntimeError("Local TTS produced empty audio")
    logger.info("Local SAPI TTS ok bytes=%d chars=%d", len(mulaw), len(spoken))
    return mulaw


def is_available() -> bool:
    import sys

    return sys.platform == "win32"
