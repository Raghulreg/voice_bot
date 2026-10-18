"""Streaming TTS — speak sentences ASAP as LLM tokens arrive.

Supports:
  - Full-text split (speak_streaming)
  - LLM delta stream → sentence → TTS with synth/play overlap (speak_from_llm_deltas)
  - Barge-in via should_stop()
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def split_speakable_units(text: str) -> list[str]:
    """Split reply into speakable units so TTS can start on the first sentence."""
    clean = " ".join((text or "").strip().split())
    if not clean:
        return []
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(clean) if p.strip()]
    if len(parts) <= 1:
        if len(clean) > 120 and "," in clean:
            rough = [p.strip() for p in clean.split(",") if p.strip()]
            if len(rough) > 1:
                rebuilt: list[str] = []
                buf = ""
                for i, p in enumerate(rough):
                    piece = p if i == len(rough) - 1 or p.endswith((".", "!", "?")) else f"{p},"
                    if not buf:
                        buf = piece
                    elif len(buf) + len(piece) < 90:
                        buf = f"{buf} {piece}"
                    else:
                        rebuilt.append(buf)
                        buf = piece
                if buf:
                    rebuilt.append(buf)
                return rebuilt or [clean]
        return [clean]
    return parts


def pop_early_clause(buffer: str, *, min_words: int = 8) -> tuple[str, str]:
    """Speak the first few words before the sentence ends, so TTS can start."""
    text = (buffer or "").lstrip()
    words = text.split()
    if len(words) < min_words:
        return "", buffer
    comma = text.find(",")
    if comma != -1 and len(text[:comma].split()) >= 4:
        return text[:comma].strip() + ",", text[comma + 1 :]
    cut = 0
    seen = 0
    for match in re.finditer(r"\S+", text):
        seen += 1
        if seen == min_words:
            cut = match.end()
            break
    return text[:cut].strip(), text[cut:]


def pop_complete_sentences(buffer: str) -> tuple[list[str], str]:
    """Pull finished sentences from a streaming text buffer; return (sentences, remainder)."""
    sentences: list[str] = []
    rest = buffer
    while True:
        s = rest.lstrip()
        m = re.match(r"(.+?[.!?])(\s+|$)", s)
        if not m:
            break
        sentence = m.group(1).strip()
        if not sentence:
            break
        sentences.append(sentence)
        rest = s[m.end() :]
    return sentences, rest


async def speak_streaming(
    *,
    text: str,
    synthesize: Callable[[str], Awaitable[bytes]],
    play: Callable[[bytes, bool], Awaitable[None]],
    call_id: str = "",
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """
    Stream TTS by sentence from a complete reply.

    `play(mulaw, finalize)` — finalize=True only on the last unit.
    `should_stop()` — barge-in: stop remaining units immediately.
    """
    units = split_speakable_units(text)
    if not units:
        return {"units": 0, "first_audio_ms": 0.0, "interrupted": False}

    t0 = time.perf_counter()
    first_audio_ms = 0.0
    interrupted = False

    next_task: asyncio.Task[bytes] = asyncio.create_task(synthesize(units[0]))

    for i, unit in enumerate(units):
        if should_stop and should_stop():
            interrupted = True
            if not next_task.done():
                next_task.cancel()
            logger.info("Streaming TTS barge-in before unit %d call=%s", i, call_id)
            break

        try:
            audio = await next_task
        except asyncio.CancelledError:
            interrupted = True
            break

        if should_stop and should_stop():
            interrupted = True
            break

        if i == 0:
            first_audio_ms = (time.perf_counter() - t0) * 1000
            logger.info(
                "Streaming TTS first sentence ready call=%s ms=%.0f chars=%d",
                call_id,
                first_audio_ms,
                len(unit),
            )

        if i + 1 < len(units):
            if should_stop and should_stop():
                interrupted = True
                await play(audio, True)
                break
            next_task = asyncio.create_task(synthesize(units[i + 1]))

        is_last = i == len(units) - 1 or (should_stop is not None and should_stop())
        await play(audio, is_last)
        if should_stop and should_stop():
            interrupted = True
            break

    return {
        "units": len(units),
        "first_audio_ms": round(first_audio_ms, 1),
        "model_from_env": True,
        "interrupted": interrupted,
        "llm_streaming": False,
    }


async def speak_from_llm_deltas(
    *,
    deltas: AsyncIterator[str],
    synthesize: Callable[[str], Awaitable[bytes]],
    play: Callable[[bytes, bool], Awaitable[None]],
    call_id: str = "",
    should_stop: Callable[[], bool] | None = None,
    flush_clause: bool = False,
    clause_words: int = 8,
) -> dict[str, Any]:
    """
    Consume LLM text deltas → flush each complete sentence to TTS ASAP.

    True overlap:
      LLM tokens → sentence 1 ready → TTS(1) while LLM continues
                 → sentence 2 ready → synth(2) while play(1)
    """
    t0 = time.perf_counter()
    first_audio_ms = 0.0
    interrupted = False
    units_played = 0

    sentence_q: asyncio.Queue[str | None] = asyncio.Queue()

    async def _producer() -> None:
        nonlocal interrupted
        buffer = ""
        try:
            async for delta in deltas:
                if should_stop and should_stop():
                    interrupted = True
                    break
                if not delta:
                    continue
                buffer += delta
                done, buffer = pop_complete_sentences(buffer)
                if flush_clause and not done:
                    clause, buffer = pop_early_clause(buffer, min_words=clause_words)
                    if clause:
                        done = [clause]
                for sent in done:
                    text = strip_robotic_lite(sent)
                    if text:
                        await sentence_q.put(text)
            if not interrupted and buffer.strip():
                text = strip_robotic_lite(buffer)
                if text:
                    await sentence_q.put(text)
        except asyncio.CancelledError:
            interrupted = True
        finally:
            await sentence_q.put(None)

    async def _consumer() -> None:
        nonlocal first_audio_ms, units_played, interrupted

        next_item = asyncio.create_task(sentence_q.get())
        audio_task: asyncio.Task[bytes] | None = None
        audio_text = ""

        while True:
            if should_stop and should_stop():
                interrupted = True
                break

            if audio_task is None:
                item = await next_item
                next_item = asyncio.create_task(sentence_q.get())
                if item is None:
                    break
                audio_text = item
                audio_task = asyncio.create_task(synthesize(item))

            done, _ = await asyncio.wait(
                {audio_task, next_item},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if should_stop and should_stop():
                interrupted = True
                break

            if audio_task in done:
                try:
                    audio = await audio_task
                except asyncio.CancelledError:
                    interrupted = True
                    break
                audio_task = None
                if units_played == 0:
                    first_audio_ms = (time.perf_counter() - t0) * 1000
                    logger.info(
                        "LLM→TTS first sentence call=%s ms=%.0f chars=%d",
                        call_id,
                        first_audio_ms,
                        len(audio_text),
                    )

                if next_item.done():
                    nxt = next_item.result()
                    is_last = nxt is None
                    await play(audio, is_last)
                    units_played += 1
                    if is_last:
                        break
                    next_item = asyncio.create_task(sentence_q.get())
                    audio_text = nxt
                    audio_task = asyncio.create_task(synthesize(nxt))
                else:
                    # LLM still going — speak now, keep awaiting next sentence
                    await play(audio, False)
                    units_played += 1

            elif next_item in done:
                nxt = next_item.result()
                next_item = asyncio.create_task(sentence_q.get())
                if nxt is None:
                    try:
                        audio = await audio_task
                    except asyncio.CancelledError:
                        interrupted = True
                        break
                    if units_played == 0:
                        first_audio_ms = (time.perf_counter() - t0) * 1000
                        logger.info(
                            "LLM→TTS first sentence call=%s ms=%.0f chars=%d",
                            call_id,
                            first_audio_ms,
                            len(audio_text),
                        )
                    await play(audio, True)
                    units_played += 1
                    audio_task = None
                    break
                # Next sentence arrived while still synthesizing current
                try:
                    audio = await audio_task
                except asyncio.CancelledError:
                    interrupted = True
                    break
                if units_played == 0:
                    first_audio_ms = (time.perf_counter() - t0) * 1000
                    logger.info(
                        "LLM→TTS first sentence call=%s ms=%.0f chars=%d",
                        call_id,
                        first_audio_ms,
                        len(audio_text),
                    )
                await play(audio, False)
                units_played += 1
                audio_text = nxt
                audio_task = asyncio.create_task(synthesize(nxt))

        if interrupted:
            if audio_task and not audio_task.done():
                audio_task.cancel()
            if not next_item.done():
                next_item.cancel()

    prod = asyncio.create_task(_producer(), name=f"llm-delta-prod-{call_id}")
    try:
        await _consumer()
    finally:
        if not prod.done():
            prod.cancel()
            try:
                await prod
            except asyncio.CancelledError:
                pass
        else:
            await prod

    return {
        "units": units_played,
        "first_audio_ms": round(first_audio_ms, 1),
        "model_from_env": True,
        "interrupted": interrupted,
        "llm_streaming": True,
    }


async def play_incoming_ulaw(
    chunks: AsyncIterator[bytes],
    send_frame: Callable[[bytes], Awaitable[None]],
    *,
    should_stop: Callable[[], bool] | None = None,
    frame_bytes: int = 640,
) -> dict[str, Any]:
    """Play provider audio as frames arrive. Prefill three frames, then pace near realtime."""
    t0 = time.perf_counter()
    first_audio_ms = 0.0
    frames = 0
    interrupted = False
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def _produce() -> None:
        buf = b""
        try:
            async for chunk in chunks:
                if should_stop and should_stop():
                    break
                if not chunk:
                    continue
                buf += chunk
                while len(buf) >= frame_bytes:
                    await queue.put(buf[:frame_bytes])
                    buf = buf[frame_bytes:]
            if buf and not (should_stop and should_stop()):
                if len(buf) < frame_bytes:
                    buf = buf + bytes([0xFF] * (frame_bytes - len(buf)))
                await queue.put(buf)
        finally:
            await queue.put(None)

    producer = asyncio.create_task(_produce())
    try:
        prefill: list[bytes] = []
        while len(prefill) < 3:
            item = await queue.get()
            if item is None:
                break
            prefill.append(item)
        for frame in prefill:
            if should_stop and should_stop():
                interrupted = True
                break
            if frames == 0:
                first_audio_ms = (time.perf_counter() - t0) * 1000
            await send_frame(frame)
            frames += 1
        while not interrupted:
            item = await queue.get()
            if item is None:
                break
            if should_stop and should_stop():
                interrupted = True
                break
            await send_frame(item)
            frames += 1
            await asyncio.sleep(0.07)
    finally:
        if not producer.done():
            producer.cancel()
            try:
                await producer
            except asyncio.CancelledError:
                pass
    return {
        "frames": frames,
        "bytes": frames * frame_bytes,
        "first_audio_ms": round(first_audio_ms, 1),
        "interrupted": interrupted,
    }


def strip_robotic_lite(text: str) -> str:
    """Light cleanup safe per-sentence."""
    return " ".join((text or "").strip().split())
