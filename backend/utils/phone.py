"""Normalize and validate outbound call phone numbers."""


def normalize_phone(phone: str) -> str:
    return phone.strip().replace(" ", "").replace("-", "")


def suggest_indian_number_fix(phone: str) -> str | None:
    """+19176242215 → +919176242215 when digits look like Indian mobile."""
    cleaned = normalize_phone(phone)
    if cleaned.startswith("+1") and len(cleaned) == 12:
        rest = cleaned[2:]
        if len(rest) == 10 and rest[0] in "6789":
            return f"+91{rest}"
    return None


def normalize_call_phone(phone: str) -> str:
    """Return E.164 phone, auto-fixing common +1 vs +91 mistake."""
    cleaned = normalize_phone(phone)
    fix = suggest_indian_number_fix(cleaned)
    return fix or cleaned
