"""LiveKit Cloud room for call orchestration.

Phone audio stays on Twilio (Gather or Media Streams). When LIVEKIT_URL,
LIVEKIT_API_KEY, and LIVEKIT_API_SECRET are set, each call opens a LiveKit
room so the session can be tracked beside the Twilio leg. The API secret and
access token are never stored on the call record.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Awaitable, Callable

import httpx

from services.voice_runtime import VoiceConfigError, missing_livekit

logger = logging.getLogger(__name__)

RoomConnector = Callable[[Any, str], Awaitable[dict[str, Any]]]


def livekit_http_host(url: str) -> str:
    raw = (url or "").strip().rstrip("/")
    if raw.startswith("wss://"):
        return "https://" + raw[len("wss://") :]
    if raw.startswith("ws://"):
        return "http://" + raw[len("ws://") :]
    return raw


def build_livekit_token(api_key: str, api_secret: str, *, identity: str, room: str) -> str:
    """HS256 access token. The secret is used only as the HMAC key."""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": api_key,
        "sub": identity,
        "nbf": now - 5,
        "exp": now + 600,
        "video": {
            "roomCreate": True,
            "roomJoin": True,
            "room": room,
            "canPublish": True,
            "canSubscribe": True,
        },
    }
    signing = f"{_b64(header)}.{_b64(payload)}"
    signature = hmac.new(api_secret.encode(), signing.encode(), hashlib.sha256).digest()
    return f"{signing}.{_b64_bytes(signature)}"


def _b64(payload: dict[str, Any]) -> str:
    return _b64_bytes(json.dumps(payload, separators=(",", ":")).encode())


def _b64_bytes(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


async def default_livekit_connector(settings: Any, room_name: str) -> dict[str, Any]:
    """Create the room on LiveKit Cloud. Raises on HTTP or network failure."""
    host = livekit_http_host(str(getattr(settings, "livekit_url", "") or ""))
    api_key = str(getattr(settings, "livekit_api_key", "") or "").strip()
    api_secret = str(getattr(settings, "livekit_api_secret", "") or "").strip()
    if not host or not api_key or not api_secret:
        raise VoiceConfigError("LiveKit credentials are incomplete.")
    token = build_livekit_token(
        api_key,
        api_secret,
        identity=f"agent-{room_name}",
        room=room_name,
    )
    url = f"{host}/twirp/livekit.RoomService/CreateRoom"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=2.0)) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"name": room_name, "empty_timeout": 300, "max_participants": 8},
            )
    except httpx.TimeoutException as exc:
        raise TimeoutError("LiveKit room request timed out") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"LiveKit room request failed ({type(exc).__name__})") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"LiveKit HTTP {response.status_code}")
    try:
        body = response.json()
    except Exception as exc:
        raise RuntimeError("LiveKit room response was not JSON") from exc
    if not isinstance(body, dict):
        raise RuntimeError("LiveKit room response was not an object")
    return {"name": str(body.get("name") or room_name), "sid": str(body.get("sid") or "")}


async def open_livekit_session(
    settings: Any,
    *,
    room_name: str = "",
    connector: RoomConnector | None = None,
) -> dict[str, Any]:
    """Open an orchestration room, or report that Twilio remains the phone path."""
    missing = missing_livekit(settings)
    if missing:
        raise VoiceConfigError(
            "LiveKit is not connected. Set "
            + ", ".join(missing)
            + " in backend/.env. The phone path stays on Twilio Gather."
        )
    room = (room_name or "voice-agent").strip() or "voice-agent"
    connect = connector or default_livekit_connector
    try:
        info = await connect(settings, room)
    except VoiceConfigError:
        raise
    except Exception as exc:
        logger.warning("LiveKit room was not verified (%s). Twilio remains the phone path.", type(exc).__name__)
        return {
            "connected": False,
            "operational": False,
            "room": room,
            "phone_path": "twilio",
            "reason": (
                "LiveKit credentials are set, but the room connection was not verified. "
                "Twilio remains the phone path."
            ),
        }
    name = str((info or {}).get("name") or room)
    return {
        "connected": True,
        "operational": True,
        "room": name,
        "phone_path": "twilio",
        "reason": "LiveKit room is open for orchestration. Phone audio stays on Twilio.",
    }


def public_livekit_status(info: dict[str, Any] | None) -> dict[str, Any]:
    """Drop tokens and secrets before a status object is saved or returned."""
    data = info or {}
    return {
        "connected": bool(data.get("connected")),
        "operational": bool(data.get("operational")),
        "room": str(data.get("room") or ""),
        "phone_path": "twilio",
        "reason": str(data.get("reason") or ""),
    }


async def attach_call_room(call_id: str) -> None:
    """Best-effort room for one call. Never blocks the Twilio dial for long."""
    from config import get_settings
    from services.conversation_engine import call_records, call_sessions

    settings = get_settings()
    record = call_records.get(call_id)
    session = call_sessions.get(call_id)
    if record is None:
        return
    if missing_livekit(settings):
        status = public_livekit_status(
            {
                "connected": False,
                "room": "",
                "reason": "Set LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET to open a room.",
            }
        )
        record["livekit"] = status
        if session is not None:
            session.livekit = status
        return
    try:
        info = await open_livekit_session(settings, room_name=f"call-{call_id}")
    except Exception:
        logger.warning("LiveKit attach failed call_id=%s", call_id)
        info = {
            "connected": False,
            "room": f"call-{call_id}",
            "reason": "LiveKit room was not opened. Twilio remains the phone path.",
        }
    status = public_livekit_status(info)
    record["livekit"] = status
    if session is not None:
        session.livekit = status
