"""Telephony audio helpers (mulaw ↔ WAV) for Twilio playback."""

import io
import wave

try:
    import audioop
except ImportError:
    import audioop_lts as audioop  # Python 3.13+


def apply_gain_to_pcm(pcm: bytes, gain: float) -> bytes:
    if gain == 1.0 or not pcm:
        return pcm
    gain = max(0.5, min(1.5, gain))
    return audioop.mul(pcm, 2, gain)


def mulaw_to_pcm_wav(mulaw_bytes: bytes, sample_rate: int = 8000, gain: float = 1.0) -> bytes:
    pcm = audioop.ulaw2lin(mulaw_bytes, 2)
    pcm = apply_gain_to_pcm(pcm, gain)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def mulaw_to_wav(mulaw_bytes: bytes, sample_rate: int = 8000) -> bytes:
    return mulaw_to_pcm_wav(mulaw_bytes, sample_rate)


def wav_to_mulaw(wav_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        pcm = wf.readframes(wf.getnframes())
        src_rate = wf.getframerate()
        if src_rate != 8000:
            pcm, _ = audioop.ratecv(pcm, 2, 1, src_rate, 8000, None)
        return audioop.lin2ulaw(pcm, 2)
