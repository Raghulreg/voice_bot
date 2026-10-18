import logging

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from config import get_settings
from utils.url import validate_public_url_for_twilio, resolve_public_url

logger = logging.getLogger(__name__)


class TwilioService:
    def __init__(self) -> None:
        settings = get_settings()
        self.client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        self.from_number = settings.twilio_phone_number
        # Import-time construction must not stop the API. Calls still validate
        # the URL in initiate_call before Twilio is contacted.
        resolved, _source = resolve_public_url(settings.public_url)
        try:
            self.public_url = validate_public_url_for_twilio(resolved)
        except ValueError:
            self.public_url = ""
            logger.warning(
                "PUBLIC_URL is missing or not an absolute http(s) URL. "
                "The API will start, but outbound calls stay blocked until it is set."
            )

    def initiate_call(
        self,
        to_number: str,
        call_id: str,
        public_url: str | None = None,
    ) -> str:
        """Place outbound call. Returns Twilio Call SID."""
        base = validate_public_url_for_twilio(public_url or self.public_url)
        webhook_url = f"{base}/api/webhooks/voice?call_id={call_id}"
        status_url = f"{base}/api/webhooks/status?call_id={call_id}"

        # Trial accounts allow to, from_, url, and status_callback.
        # status_callback_event and status_callback_method are rejected with HTTP 400.
        # Default without them: Twilio POSTs the completed status to status_callback.
        # Phone dial is commented out. The dashboard keeps the session on this page.
        # try:
        #     call = self.client.calls.create(
        #         to=to_number,
        #         from_=self.from_number,
        #         url=webhook_url,
        #         status_callback=status_url,
        #     )
        # except TwilioRestException as exc:
        #     logger.error(
        #         "Twilio calls.create rejected parameters=%s status=%s code=%s message=%s details=%s",
        #         ["to", "from_", "url", "status_callback"],
        #         exc.status,
        #         exc.code,
        #         exc.msg,
        #         exc.details,
        #     )
        #     raise
        # return call.sid
        logger.info(
            "Twilio dial commented out call_id=%s to=%s webhook=%s",
            call_id,
            to_number,
            webhook_url,
        )
        return f"local-{call_id}"

    def hangup(self, call_sid: str) -> None:
        # Twilio hangup is commented out while calls stay in the browser.
        # if call_sid:
        #     self.client.calls(call_sid).update(status="completed")
        return

    def download_recording(self, recording_url: str) -> bytes:
        """Download a Twilio call recording as WAV bytes (sync)."""
        import httpx

        settings = get_settings()
        url = recording_url if recording_url.endswith(".wav") else f"{recording_url}.wav"
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                url,
                auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                follow_redirects=True,
            )
            resp.raise_for_status()
            return resp.content

    async def download_recording_async(self, recording_url: str) -> bytes:
        """Download recording; retry until Twilio has finished writing the file."""
        import asyncio

        import httpx

        settings = get_settings()
        base = recording_url.rstrip("/")
        url = f"{base}.wav" if not base.endswith(".wav") else base

        last_err: Exception | None = None
        async with httpx.AsyncClient(timeout=8.0) as client:
            for attempt in range(2):
                try:
                    resp = await client.get(
                        url,
                        auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                        follow_redirects=True,
                    )
                    resp.raise_for_status()
                    if len(resp.content) > 400:
                        return resp.content
                except Exception as e:
                    last_err = e
                if attempt < 1:
                    await asyncio.sleep(0.1)

        if last_err:
            raise last_err
        raise RuntimeError("Twilio recording empty or not ready")
