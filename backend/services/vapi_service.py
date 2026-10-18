"""Vapi + Twilio outbound phone calls (ElevenLabs is voice-preview only)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from config import get_settings, reload_settings
from utils.env_file import set_env_value

logger = logging.getLogger(__name__)

VAPI_BASE = "https://api.vapi.ai"


class VapiService:
    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = (settings.vapi_api_key or "").strip()
        self.assistant_id = (settings.vapi_assistant_id or "").strip()
        self.phone_number_id = (settings.vapi_phone_number_id or "").strip()
        self.voice_id = (settings.vapi_voice_id or "Elliot").strip()
        self.voice_version = (settings.vapi_voice_version or "2").strip()
        self.twilio_sid = (settings.twilio_account_sid or "").strip()
        self.twilio_token = (settings.twilio_auth_token or "").strip()
        self.twilio_number = (settings.twilio_phone_number or "").strip()

    def is_configured(self) -> bool:
        return bool(self.api_key and self.assistant_id and self.twilio_sid and self.twilio_number)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> Any:
        url = f"{VAPI_BASE}{path}"
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(method, url, headers=self._headers(), json=json_body)
            if resp.status_code >= 400:
                detail = resp.text[:800]
                raise RuntimeError(f"Vapi {method} {path} HTTP {resp.status_code}: {detail}")
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()

    def ensure_phone_number_id(self) -> str:
        """Return Vapi phoneNumberId for the Twilio number (import if needed)."""
        if self.phone_number_id:
            return self.phone_number_id

        if not self.twilio_number:
            raise RuntimeError("TWILIO_PHONE_NUMBER is required for Vapi outbound calls")

        numbers = self._request("GET", "/phone-number") or []
        if isinstance(numbers, dict):
            numbers = numbers.get("items") or numbers.get("results") or []

        target = self.twilio_number.replace(" ", "")
        for item in numbers:
            if not isinstance(item, dict):
                continue
            num = str(item.get("number") or "").replace(" ", "")
            if num == target and item.get("id"):
                phone_id = str(item["id"])
                self._persist_phone_number_id(phone_id)
                return phone_id

        created = self._request(
            "POST",
            "/phone-number",
            json_body={
                "provider": "twilio",
                "number": target,
                "twilioAccountSid": self.twilio_sid,
                "twilioAuthToken": self.twilio_token,
                "assistantId": self.assistant_id,
                "name": "ClaimsVoice Twilio",
            },
        )
        phone_id = str((created or {}).get("id") or "")
        if not phone_id:
            raise RuntimeError("Vapi imported Twilio number but returned no id")
        self._persist_phone_number_id(phone_id)
        return phone_id

    def _persist_phone_number_id(self, phone_id: str) -> None:
        self.phone_number_id = phone_id
        try:
            set_env_value("VAPI_PHONE_NUMBER_ID", phone_id)
            reload_settings()
            logger.info("Saved VAPI_PHONE_NUMBER_ID=%s", phone_id)
        except Exception as e:
            logger.warning("Could not persist VAPI_PHONE_NUMBER_ID: %s", e)

    def create_outbound_call(
        self,
        *,
        to_number: str,
        call_id: str,
        customer_name: str = "",
        variable_values: dict[str, str] | None = None,
        server_url: str | None = None,
    ) -> dict[str, Any]:
        """Place outbound call via Vapi (Twilio telephony). Returns Vapi call object."""
        if not self.is_configured():
            raise RuntimeError(
                "Vapi not configured. Set VAPI_API_KEY, VAPI_ASSISTANT_ID, and Twilio credentials."
            )

        phone_number_id = self.ensure_phone_number_id()
        overrides: dict[str, Any] = {
            "variableValues": variable_values or {},
            "voice": {
                "provider": "vapi",
                "voiceId": self.voice_id,
            },
            "metadata": {"call_id": call_id},
        }

        if server_url:
            overrides["serverUrl"] = server_url.rstrip("/")

        payload: dict[str, Any] = {
            "assistantId": self.assistant_id,
            "phoneNumberId": phone_number_id,
            "customer": {
                "number": to_number,
                "numberE164CheckEnabled": True,
            },
            "assistantOverrides": overrides,
            "metadata": {"call_id": call_id},
        }
        if customer_name:
            payload["customer"]["name"] = customer_name

        logger.info(
            "Vapi outbound call call_id=%s to=%s phoneNumberId=%s",
            call_id,
            to_number,
            phone_number_id,
        )
        result = self._request("POST", "/call", json_body=payload)
        if not isinstance(result, dict):
            raise RuntimeError("Vapi create call returned unexpected response")
        return result
