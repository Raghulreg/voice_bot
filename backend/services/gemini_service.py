import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from typing import Any

from google import genai
from google.genai import types

from config import get_settings
from prompt_builder import (
    build_emotion_recovery_prompt,
    build_greeting_prompt,
    build_system_prompt,
    build_user_turn_prompt,
)
from services.model_routing import ModelRoute, route_model
from services.rcm_dialogue import RcmCollected

logger = logging.getLogger(__name__)

# Full metadata JSON needs more than ~220 tokens; truncation was spoken as raw JSON.
_MAX_OUTPUT_TOKENS = 768
_SIMPLE_MAX_OUTPUT_TOKENS = 320
_LLM_REQUEST_TIMEOUT_S = 8.0


def _error_label(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return f"{type(exc).__name__}:{code}" if code else type(exc).__name__


def _content_config(
    temperature: float,
    max_output_tokens: int = _MAX_OUTPUT_TOKENS,
    *,
    json_mode: bool = True,
) -> types.GenerateContentConfig:
    # No thinking_config: current models reject thinking_budget (400
    # INVALID_ARGUMENT) and Flash-Lite already defaults to minimal thinking.
    if json_mode:
        return types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            response_mime_type="application/json",
        )
    return types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )

# Hint so spoken text arrives before metadata → TTS can start mid-stream
_STREAM_ORDER_HINT = (
    "\n\nSTREAMING: Emit ONLY valid complete JSON. "
    "Put \"response_text\" as the FIRST key with natural spoken English only "
    "(no JSON braces inside that string). Finish the whole object."
)

_RESPONSE_TEXT_RE = re.compile(
    r'"response_text"\s*:\s*"((?:\\.|[^"\\])*)"',
    re.DOTALL,
)


def extract_spoken_text(raw: str) -> str:
    """Pull plain English out of a model reply — never return raw JSON for TTS."""
    text = (raw or "").strip()
    if not text:
        return ""

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()

    # Full JSON
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            spoken = str(data.get("response_text") or "").strip()
            if spoken and not looks_like_json_blob(spoken):
                return _unescape_json_string(spoken)
    except json.JSONDecodeError:
        pass

    # Truncated / partial JSON — capture response_text field only
    m = _RESPONSE_TEXT_RE.search(text)
    if m:
        spoken = _unescape_json_string(m.group(1)).strip()
        if spoken and not looks_like_json_blob(spoken):
            return spoken

    # Bare natural language (no JSON wrapper)
    if not looks_like_json_blob(text):
        # Strip accidental "response_text": prefix
        bare = re.sub(r'^["\']?response_text["\']?\s*:\s*["\']?', "", text, flags=re.I)
        bare = bare.strip().strip('"').strip("'")
        if bare and not looks_like_json_blob(bare):
            return bare[:400]

    return ""


def looks_like_json_blob(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if t.startswith("{") or t.startswith("["):
        return True
    if '"response_text"' in t or '"detected_language"' in t:
        return True
    if t.count('"') >= 4 and ":" in t and ("{" in t or "}" in t):
        return True
    return False


def _unescape_json_string(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except json.JSONDecodeError:
        return (
            s.replace("\\n", " ")
            .replace("\\t", " ")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )


def sanitize_spoken_response(text: str, *, fallback: str = "") -> str:
    """Guarantee TTS/UI only get plain English sentences."""
    spoken = extract_spoken_text(text) if looks_like_json_blob(text or "") else (text or "").strip()
    if looks_like_json_blob(spoken):
        spoken = extract_spoken_text(spoken)
    if not spoken or looks_like_json_blob(spoken):
        spoken = (fallback or "").strip()
    # Collapse whitespace; drop control chars
    spoken = re.sub(r"\s+", " ", spoken).strip()
    return spoken[:500]


class ResponseTextExtractor:
    """Incrementally extract the JSON string value of `response_text` from a stream."""

    def __init__(self) -> None:
        self._buf = ""
        self._phase = "seek_key"
        self._escape = False
        self.response_text = ""
        self.done = False

    def feed(self, chunk: str) -> str:
        if self.done or not chunk:
            return ""
        self._buf += chunk
        out: list[str] = []

        while self._buf and not self.done:
            if self._phase == "seek_key":
                idx = self._buf.find('"response_text"')
                if idx < 0:
                    # Keep a short tail in case key is split across chunks
                    self._buf = self._buf[-20:]
                    break
                self._buf = self._buf[idx + len('"response_text"') :]
                self._phase = "seek_colon"
            elif self._phase == "seek_colon":
                stripped = self._buf.lstrip()
                skipped = len(self._buf) - len(stripped)
                if not stripped:
                    self._buf = ""
                    break
                if stripped[0] != ":":
                    self._phase = "seek_key"
                    continue
                self._buf = stripped[1:]
                self._phase = "seek_quote"
                if skipped:
                    pass
            elif self._phase == "seek_quote":
                stripped = self._buf.lstrip()
                if not stripped:
                    self._buf = ""
                    break
                if stripped[0] != '"':
                    # Non-string value — cannot stream this field
                    self.done = True
                    self._phase = "done"
                    break
                self._buf = stripped[1:]
                self._phase = "in_string"
                self._escape = False
            elif self._phase == "in_string":
                i = 0
                while i < len(self._buf):
                    ch = self._buf[i]
                    if self._escape:
                        mapped = {
                            "n": "\n",
                            "t": "\t",
                            "r": "\r",
                            '"': '"',
                            "\\": "\\",
                            "/": "/",
                        }.get(ch, ch)
                        out.append(mapped)
                        self.response_text += mapped
                        self._escape = False
                        i += 1
                        continue
                    if ch == "\\":
                        self._escape = True
                        i += 1
                        continue
                    if ch == '"':
                        self._buf = self._buf[i + 1 :]
                        self.done = True
                        self._phase = "done"
                        break
                    out.append(ch)
                    self.response_text += ch
                    i += 1
                else:
                    self._buf = ""
                    break
                if self.done:
                    break

        return "".join(out)


def _claim_kwargs(
    hospital_name: str = "",
    claim_id: str = "",
    patient_name: str = "",
    claim_type: str = "",
    tpa_name: str = "",
    npi: str = "",
    policy_number: str = "",
    claim_age_days: int | None = None,
    tpa_executive_profile: str = "",
    scenario: dict[str, Any] | None = None,
    tpa_sector_id: str = "",
    focus_topics: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "hospital_name": hospital_name,
        "claim_id": claim_id,
        "patient_name": patient_name,
        "claim_type": claim_type,
        "tpa_name": tpa_name,
        "npi": npi,
        "policy_number": policy_number,
        "claim_age_days": claim_age_days,
        "tpa_executive_profile": tpa_executive_profile,
        "scenario": scenario,
        "tpa_sector_id": tpa_sector_id or "",
        "focus_topics": list(focus_topics or []),
    }


class GeminiService:
    def __init__(self) -> None:
        settings = get_settings()
        self.model_name = settings.gemini_model
        self.simple_model_name = settings.gemini_simple_model
        self._client = genai.Client(api_key=settings.gemini_api_key)

    def _route_turn(
        self,
        user_transcript: str,
        answer_topic: str,
        answer_valid: bool,
    ) -> ModelRoute:
        return route_model(
            user_transcript,
            answer_topic,
            answer_valid,
            self.model_name,
            self.simple_model_name,
        )

    def _parse_json_response(self, text: str) -> dict[str, Any]:
        cleaned = (text or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned).strip()

        spoken = extract_spoken_text(cleaned)
        result: dict[str, Any] | None = None

        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                result = parsed
        except json.JSONDecodeError:
            # Truncated JSON is common — recover fields we can
            result = None

        if result is None:
            result = {
                "detected_language": "en-US",
                "detected_emotion": "neutral",
                "conversation_stage": "information_gathering",
                "confidence_score": 0.5,
                "lead_score": 50,
                "next_action": "continue",
                "key_insight": "Partial JSON recovered" if spoken else "Parse fallback",
            }

        # Always force speakable English — never pass raw JSON to TTS
        result["response_text"] = sanitize_spoken_response(
            str(result.get("response_text") or spoken or ""),
            fallback=spoken,
        )
        if not result["response_text"]:
            logger.warning(
                "Gemini reply had no usable response_text (raw chars=%d)",
                len(cleaned),
            )
            result["response_text"] = ""
            result["key_insight"] = "Empty spoken text after sanitize"
        return result

    def _normalize_scores(self, result: dict[str, Any]) -> dict[str, Any]:
        conf = result.get("confidence_score")
        if conf is not None:
            try:
                conf_f = float(conf)
                result["confidence_score"] = conf_f
                result["lead_score"] = int(conf_f * 100)
            except (TypeError, ValueError):
                pass
        elif "lead_score" in result:
            try:
                result["confidence_score"] = float(result["lead_score"]) / 100.0
            except (TypeError, ValueError):
                result["confidence_score"] = 0.5
        # Final guard: only plain English leaves this service
        result["response_text"] = sanitize_spoken_response(
            str(result.get("response_text") or ""),
            fallback="",
        )
        return result

    async def _open_content_stream(self, **kwargs: Any) -> Any:
        """Open a Gemini stream. One retry on timeout, then the caller can fall back."""
        last_timeout: BaseException | None = None
        for attempt in range(2):
            try:
                return await asyncio.wait_for(
                    self._client.aio.models.generate_content_stream(**kwargs),
                    timeout=_LLM_REQUEST_TIMEOUT_S,
                )
            except asyncio.TimeoutError as exc:
                last_timeout = exc
                logger.warning("Gemini request timed out attempt=%s", attempt + 1)
        raise TimeoutError("Gemini request timed out") from last_timeout

    async def _generate(
        self,
        prompt: str,
        temperature: float,
        model_name: str | None = None,
        max_output_tokens: int = _MAX_OUTPUT_TOKENS,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        first_chunk_ms: float | None = None
        usage = None
        model_version = ""
        parts: list[str] = []
        stream = await self._open_content_stream(
            model=model_name or self.model_name,
            contents=prompt,
            config=_content_config(temperature, max_output_tokens),
        )
        async for chunk in stream:
            if first_chunk_ms is None:
                first_chunk_ms = (time.perf_counter() - started) * 1000
            parts.append(getattr(chunk, "text", None) or "")
            usage = getattr(chunk, "usage_metadata", None) or usage
            model_version = getattr(chunk, "model_version", None) or model_version
        result = self._normalize_scores(self._parse_json_response("".join(parts)))
        result["_request"] = {
            "request_ms": (time.perf_counter() - started) * 1000,
            "first_chunk_ms": first_chunk_ms,
            "prompt_tokens": getattr(usage, "prompt_token_count", None),
            "output_tokens": getattr(usage, "candidates_token_count", None),
            "thought_tokens": getattr(usage, "thoughts_token_count", None),
            "model_version": model_version,
        }
        return result

    async def generate_greeting(
        self,
        agent_name: str,
        preferred_language: str,
        customer_name: str = "",
        hospital_name: str = "",
        claim_id: str = "",
        patient_name: str = "",
        claim_type: str = "",
        tpa_name: str = "",
        npi: str = "",
        policy_number: str = "",
        claim_age_days: int | None = None,
        tpa_executive_profile: str = "",
        scenario: dict[str, Any] | None = None,
        tpa_sector_id: str = "",
        focus_topics: list[str] | None = None,
        **_extra: Any,
    ) -> dict[str, Any]:
        del _extra
        ctx = _claim_kwargs(
            hospital_name, claim_id, patient_name or customer_name, claim_type,
            tpa_name, npi, policy_number, claim_age_days,
            tpa_executive_profile, scenario, tpa_sector_id, focus_topics,
        )
        t_prompt = time.perf_counter()
        from services.pii_guardrails import redacted_claim_fields

        redacted = redacted_claim_fields(
            patient_name=patient_name or customer_name,
            policy_number=policy_number,
        )
        system = build_system_prompt(agent_name, preferred_language, **{
            **ctx,
            "patient_name": redacted["patient_name"] or ctx.get("patient_name", ""),
            "policy_number": redacted["policy_number"] or ctx.get("policy_number", ""),
        })
        user = build_greeting_prompt(
            agent_name,
            redacted["patient_name"] or customer_name,
            preferred_language,
            hospital_name=hospital_name,
            claim_id=claim_id,
            patient_name=redacted["patient_name"] or patient_name or customer_name,
            claim_type=claim_type,
            tpa_sector_id=tpa_sector_id,
        )
        prompt_build_ms = (time.perf_counter() - t_prompt) * 1000
        t_llm = time.perf_counter()
        result = await self._generate(system + "\n\n" + user, temperature=0.7)
        result.pop("_request", None)
        result["_timing"] = {
            "prompt_build_ms": prompt_build_ms,
            "llm_ms": (time.perf_counter() - t_llm) * 1000,
        }
        return result

    async def generate_response(
        self,
        user_transcript: str,
        conversation_history: list[dict],
        agent_name: str,
        preferred_language: str,
        last_question: str = "",
        hospital_name: str = "",
        claim_id: str = "",
        patient_name: str = "",
        claim_type: str = "",
        tpa_name: str = "",
        npi: str = "",
        policy_number: str = "",
        claim_age_days: int | None = None,
        tpa_executive_profile: str = "",
        scenario: dict[str, Any] | None = None,
        recent_acks: list[str] | None = None,
        after_backchannel: bool = False,
        rcm_collected: RcmCollected | None = None,
        conversation_state_block: str = "",
        tpa_sector_id: str = "",
        focus_topics: list[str] | None = None,
        answer_topic: str = "unknown",
        answer_valid: bool = False,
        **_extra: Any,
    ) -> dict[str, Any]:
        del _extra
        route = self._route_turn(user_transcript, answer_topic, answer_valid)
        prompt, prompt_build_ms = self._build_turn_prompt(
            user_transcript,
            conversation_history,
            agent_name,
            preferred_language,
            last_question=last_question,
            hospital_name=hospital_name,
            claim_id=claim_id,
            patient_name=patient_name,
            claim_type=claim_type,
            tpa_name=tpa_name,
            npi=npi,
            policy_number=policy_number,
            claim_age_days=claim_age_days,
            tpa_executive_profile=tpa_executive_profile,
            scenario=scenario,
            recent_acks=recent_acks,
            after_backchannel=after_backchannel,
            rcm_collected=rcm_collected,
            conversation_state_block=conversation_state_block,
            tpa_sector_id=tpa_sector_id,
            focus_topics=focus_topics,
            streaming=False,
            compact=route.tier == "simple",
        )
        t_llm = time.perf_counter()
        selected_model = route.model
        fallback_used = False
        output_tokens = (
            _SIMPLE_MAX_OUTPUT_TOKENS if route.tier == "simple" else _MAX_OUTPUT_TOKENS
        )
        attempts = 1
        first_error = ""
        try:
            result = await self._generate(
                prompt,
                temperature=0.65,
                model_name=selected_model,
                max_output_tokens=output_tokens,
            )
        except Exception as exc:
            if route.tier != "simple" or selected_model == self.model_name:
                raise
            first_error = _error_label(exc)
            logger.warning(
                "Approved simple model failed (%s); retrying the primary Gemini model",
                first_error,
            )
            selected_model = self.model_name
            fallback_used = True
            attempts = 2
            result = await self._generate(
                prompt,
                temperature=0.65,
                model_name=selected_model,
                max_output_tokens=_MAX_OUTPUT_TOKENS,
            )
        llm_ms = (time.perf_counter() - t_llm) * 1000
        request = result.pop("_request", None) or {}
        first_chunk = request.get("first_chunk_ms")
        if first_chunk is not None:
            # Relative to the first attempt, so a fallback includes the failed request.
            first_chunk = llm_ms - float(request.get("request_ms") or 0) + float(first_chunk)
        result["_timing"] = {
            "prompt_build_ms": prompt_build_ms,
            "llm_ms": llm_ms,
            "first_delta_ms": first_chunk,
            "request_ms": request.get("request_ms"),
            "prompt_tokens": request.get("prompt_tokens"),
            "output_tokens": request.get("output_tokens"),
            "thought_tokens": request.get("thought_tokens"),
            "model_version": request.get("model_version"),
            "attempts": attempts,
            "error": first_error,
            "model": selected_model,
            "model_tier": "complex" if fallback_used else route.tier,
            "model_route_reason": (
                "simple_model_failure_fallback" if fallback_used else route.reason
            ),
            "model_fallback_used": fallback_used,
        }

        emotion = result.get("detected_emotion", "neutral")
        if emotion in (
            "annoyed", "busy", "hesitant", "confused", "impatient",
            "frustrated", "defensive", "nervous", "rushed",
        ):
            result["emotion_strategy"] = build_emotion_recovery_prompt(emotion)

        return result

    def _build_turn_prompt(
        self,
        user_transcript: str,
        conversation_history: list[dict],
        agent_name: str,
        preferred_language: str,
        *,
        last_question: str = "",
        hospital_name: str = "",
        claim_id: str = "",
        patient_name: str = "",
        claim_type: str = "",
        tpa_name: str = "",
        npi: str = "",
        policy_number: str = "",
        claim_age_days: int | None = None,
        tpa_executive_profile: str = "",
        scenario: dict[str, Any] | None = None,
        recent_acks: list[str] | None = None,
        after_backchannel: bool = False,
        rcm_collected: RcmCollected | None = None,
        conversation_state_block: str = "",
        tpa_sector_id: str = "",
        focus_topics: list[str] | None = None,
        streaming: bool = False,
        compact: bool = False,
        reply_briefly: bool = False,
    ) -> tuple[str, float]:
        from services.pii_guardrails import mask_for_gemini, mask_text, redacted_claim_fields

        masked_transcript, masked_history, _ctx = mask_for_gemini(
            user_transcript=user_transcript,
            conversation_history=conversation_history,
            patient_name=patient_name,
            policy_number=policy_number,
        )
        redacted = redacted_claim_fields(
            patient_name=patient_name,
            policy_number=policy_number,
        )
        ctx = _claim_kwargs(
            hospital_name,
            claim_id,
            redacted["patient_name"] or patient_name,
            claim_type,
            tpa_name,
            npi,
            redacted["policy_number"] or policy_number,
            claim_age_days,
            tpa_executive_profile,
            scenario,
            tpa_sector_id,
            focus_topics,
        )
        t_prompt = time.perf_counter()
        if reply_briefly:
            history_lines = []
            for turn in masked_history[-4:]:
                role = "You" if turn.get("role") == "assistant" else "Payer"
                history_lines.append(f"{role}: {turn.get('content', '')}")
            history = "\n".join(history_lines) or "(call just started)"
            prompt = (
                f"You are {agent_name}, a hospital biller on a live phone call with the insurance rep.\n"
                "Answer what they just said in ONE short spoken sentence, using the claim facts. "
                f"This call is about {claim_type or 'the claim follow-up'}. Stay on that reason. "
                "If they ask for the claim, patient, or hospital, say that fact. "
                "Do not ask them to repeat themselves. Do not repeat your last sentence.\n"
                "Under 18 words. Speak the sentence itself. No JSON and no labels.\n\n"
                f"Hospital: {hospital_name or 'on file'}\n"
                f"Claim ID: {claim_id or 'on file'}\n"
                f"Patient: {redacted['patient_name'] or patient_name or 'on file'}\n"
                f"Claim type: {claim_type or 'on file'}\n"
                f"Payer: {tpa_name or 'on file'}\n"
                f"Conversation:\n{history}\n\n"
                f'Payer just said: "{masked_transcript}"'
            )
            return prompt, (time.perf_counter() - t_prompt) * 1000
        if compact:
            from prompt_builder import build_simple_turn_prompt

            prompt = build_simple_turn_prompt(
                agent_name,
                masked_transcript,
                masked_history,
                rcm_collected=rcm_collected,
                last_question=mask_text(last_question or "", _ctx),
            )
            for line in (conversation_state_block or "").splitlines():
                if line.startswith("Tone guidance:"):
                    prompt += "\n" + line
                    break
            if streaming:
                prompt += _STREAM_ORDER_HINT
            return prompt, (time.perf_counter() - t_prompt) * 1000
        system = build_system_prompt(agent_name, preferred_language, **ctx)
        state_block = mask_text(conversation_state_block or "", _ctx) if conversation_state_block else ""
        user = build_user_turn_prompt(
            masked_transcript,
            masked_history,
            preferred_language,
            last_question=mask_text(last_question or "", _ctx),
            recent_acks=recent_acks,
            after_backchannel=after_backchannel,
            rcm_collected=rcm_collected,
            conversation_state_block=state_block,
            tpa_sector_id=tpa_sector_id or "",
        )
        prompt = system + "\n\n" + user
        if streaming:
            prompt += _STREAM_ORDER_HINT
        return prompt, (time.perf_counter() - t_prompt) * 1000

    async def generate_response_stream(
        self,
        user_transcript: str,
        conversation_history: list[dict],
        agent_name: str,
        preferred_language: str,
        last_question: str = "",
        hospital_name: str = "",
        claim_id: str = "",
        patient_name: str = "",
        claim_type: str = "",
        tpa_name: str = "",
        npi: str = "",
        policy_number: str = "",
        claim_age_days: int | None = None,
        tpa_executive_profile: str = "",
        scenario: dict[str, Any] | None = None,
        recent_acks: list[str] | None = None,
        after_backchannel: bool = False,
        rcm_collected: RcmCollected | None = None,
        conversation_state_block: str = "",
        tpa_sector_id: str = "",
        focus_topics: list[str] | None = None,
        answer_topic: str = "unknown",
        answer_valid: bool = False,
        reply_briefly: bool = False,
        **_extra: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Stream Gemini tokens. Yields:
          {"event": "delta", "text": "..."}  — spoken response_text chars as they arrive
          {"event": "done", "result": {...}, "timing": {...}}
        """
        del _extra
        route = self._route_turn(user_transcript, answer_topic, answer_valid)
        prompt, prompt_build_ms = self._build_turn_prompt(
            user_transcript,
            conversation_history,
            agent_name,
            preferred_language,
            last_question=last_question,
            hospital_name=hospital_name,
            claim_id=claim_id,
            patient_name=patient_name,
            claim_type=claim_type,
            tpa_name=tpa_name,
            npi=npi,
            policy_number=policy_number,
            claim_age_days=claim_age_days,
            tpa_executive_profile=tpa_executive_profile,
            scenario=scenario,
            recent_acks=recent_acks,
            after_backchannel=after_backchannel,
            rcm_collected=rcm_collected,
            conversation_state_block=conversation_state_block,
            tpa_sector_id=tpa_sector_id,
            focus_topics=focus_topics,
            streaming=True,
            compact=route.tier == "simple",
            reply_briefly=reply_briefly,
        )

        t_llm = time.perf_counter()
        extractor = ResponseTextExtractor()
        full = ""
        first_delta_ms: float | None = None
        delta_chars = 0
        selected_model = route.model
        fallback_used = False
        emitted_spoken = False

        models = [route.model]
        if route.tier == "simple" and route.model != self.model_name:
            models.append(self.model_name)
        for attempt, model_name in enumerate(models):
            selected_model = model_name
            if attempt:
                fallback_used = True
                extractor = ResponseTextExtractor()
                full = ""
                logger.warning(
                    "Approved simple streaming model failed before speech; "
                    "retrying the primary Gemini model"
                )
            try:
                stream = await self._open_content_stream(
                    model=model_name,
                    contents=prompt,
                    config=_content_config(
                        0.4 if reply_briefly else 0.65,
                        80
                        if reply_briefly
                        else (
                            _SIMPLE_MAX_OUTPUT_TOKENS
                            if route.tier == "simple" and not attempt
                            else _MAX_OUTPUT_TOKENS
                        ),
                        json_mode=not reply_briefly,
                    ),
                )
                async for chunk in stream:
                    piece = getattr(chunk, "text", None) or ""
                    if not piece:
                        continue
                    full += piece
                    spoken = piece if reply_briefly else extractor.feed(piece)
                    # Never stream JSON key noise if extractor failed open
                    if spoken and not looks_like_json_blob(spoken):
                        emitted_spoken = True
                        delta_chars += len(spoken)
                        if first_delta_ms is None:
                            first_delta_ms = (time.perf_counter() - t_llm) * 1000
                            logger.info(
                                "Gemini stream first response_text delta ms=%.0f",
                                first_delta_ms,
                            )
                        yield {"event": "delta", "text": spoken}
                break
            except Exception:
                can_retry = (
                    not emitted_spoken
                    and attempt + 1 < len(models)
                )
                if can_retry:
                    logger.warning(
                        "Approved simple streaming model failed before speech"
                    )
                    continue
                logger.exception("Gemini generate_content_stream failed")
                if not extractor.response_text and not full:
                    raise
                break

        llm_ms = (time.perf_counter() - t_llm) * 1000
        if reply_briefly:
            spoken_line = sanitize_spoken_response(extract_spoken_text(full) or full)
            result = {
                "response_text": spoken_line,
                "detected_emotion": "neutral",
                "conversation_stage": "information_gathering",
                "advance_workflow": True,
                "confidence_score": 0.7,
                "lead_score": 70,
                "next_action": "continue",
            }
        else:
            result = self._normalize_scores(self._parse_json_response(full or "{}"))
        # Prefer incrementally extracted spoken text if JSON parse lost it
        extracted = sanitize_spoken_response(extractor.response_text or "")
        if extracted and not (result.get("response_text") or "").strip():
            result["response_text"] = extracted
        elif extracted and len(extracted) >= len(str(result.get("response_text") or "")):
            result["response_text"] = extracted
        result["response_text"] = sanitize_spoken_response(
            str(result.get("response_text") or ""),
            fallback=extracted,
        )

        emotion = result.get("detected_emotion", "neutral")
        if emotion in (
            "annoyed", "busy", "hesitant", "confused", "impatient",
            "frustrated", "defensive", "nervous", "rushed",
        ):
            result["emotion_strategy"] = build_emotion_recovery_prompt(emotion)

        result["_timing"] = {
            "prompt_build_ms": prompt_build_ms,
            "llm_ms": llm_ms,
            "first_delta_ms": first_delta_ms,
            "streamed_chars": delta_chars,
            "streaming": True,
            "model": selected_model,
            "model_tier": "complex" if fallback_used else route.tier,
            "model_route_reason": (
                "simple_model_failure_fallback" if fallback_used else route.reason
            ),
            "model_fallback_used": fallback_used,
        }
        yield {"event": "done", "result": result, "timing": result["_timing"]}
