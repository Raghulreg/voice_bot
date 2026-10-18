"""Echo Bot — foundation latency test before full AI dialogue.

Human: Hello
  → Transcript: Hello
  → Bot: Hello Back

If this round-trips near ~500ms (STT final → first audio), Media Streams foundation is solid.
"""

from __future__ import annotations

import re
import time

_HELLO_RE = re.compile(r"\b(hello|hi|hey|good\s+(morning|afternoon|evening))\b", re.I)


def is_echo_mode(dialogue_mode: str) -> bool:
    return (dialogue_mode or "").strip().lower() in {"echo", "echo_bot", "echobot"}


def echo_greeting(agent_name: str = "Chris") -> str:
    return (
        f"Hi, this is {agent_name} on the echo bot. "
        "Say hello, and I will reply right away."
    )


def echo_reply(transcript: str) -> str:
    """Map final STT text → echo bot reply (no LLM)."""
    text = (transcript or "").strip()
    if not text:
        return "I did not catch that. Please say hello."
    if _HELLO_RE.search(text):
        return "Hello Back"
    # Classic echo — proves STT → TTS loop
    return text


def echo_latency_record(
    *,
    transcript: str,
    reply: str,
    stt_to_first_audio_ms: float | None,
    total_handler_ms: float | None,
) -> dict:
    """Structured echo-bot timing for call JSON / UI."""
    ok = (
        stt_to_first_audio_ms is not None
        and stt_to_first_audio_ms <= 800  # soft target around ~500ms
    )
    return {
        "mode": "echo",
        "transcript": transcript,
        "reply": reply,
        "stt_to_first_audio_ms": stt_to_first_audio_ms,
        "total_handler_ms": total_handler_ms,
        "foundation_ok": ok,
        "at": time.time(),
    }
