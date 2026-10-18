"""User-friendly Twilio API error messages."""


def format_twilio_error(exc: Exception) -> tuple[str, int]:
    """
    Return (message, http_status).
    Trial/unverified number errors cannot be fixed in code — user must verify or upgrade.
    """
    text = str(exc)

    if "21219" in text or "unverified" in text.lower():
        return (
            "Twilio trial: this exact number is not verified. "
            "Check the country code matches Twilio exactly — e.g. +919176242215 (India) "
            "is NOT the same as +19176242215 (USA). "
            "Verify the same E.164 number in Twilio Console → Verified Caller IDs, "
            "or upgrade to a paid account to call any number. "
            "https://www.twilio.com/docs/errors/21219",
            403,
        )

    if "21205" in text or "not a valid url" in text.lower():
        return (
            "Twilio webhook URL is invalid or unreachable. Run ngrok http 8000, click "
            "Auto-detect ngrok in the dashboard, and restart the backend. "
            "A dead ngrok URL causes Twilio to say 'application error' after the trial message.",
            503,
        )

    if "21211" in text or "invalid 'to' phone number" in text.lower():
        return (
            "Invalid phone number format. Use country code, e.g. +919876543210 or +19176242215.",
            400,
        )

    if "20003" in text or "authenticate" in text.lower():
        return (
            "Twilio authentication failed. Check TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in backend/.env.",
            503,
        )

    return text, 500
