"""Route simple payer turns to the approved small model; keep complex turns on the primary model."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SIMPLE_TOPICS = {
    "claim_status",
    "reviewer",
    "pending_documents",
    "payment_date",
    "review_completion_date",
    "payer_reference",
    "remittance",
}

_COMPLEX_MARKERS = (
    "appeal", "clause", "coding", "coverage", "denial", "denied", "dispute",
    "escalat", "exclusion", "explain", "medical necessity", "partial",
    "policy", "reason", "reject", "why",
)


@dataclass(frozen=True)
class ModelRoute:
    tier: str
    model: str
    reason: str


def route_model(
    transcript: str,
    answer_topic: str,
    answer_valid: bool,
    primary_model: str,
    simple_model: str = "",
) -> ModelRoute:
    """Choose a model without changing the provider or bypassing answer validation."""
    primary = (primary_model or "").strip()
    small = (simple_model or "").strip() or primary
    low = (transcript or "").lower()
    words = re.findall(r"[a-z0-9']+", low)

    if any(marker in low for marker in _COMPLEX_MARKERS):
        return ModelRoute("complex", primary, "complex_reasoning_terms")
    if not answer_valid:
        return ModelRoute("complex", primary, "unvalidated_answer")
    if answer_topic not in _SIMPLE_TOPICS:
        return ModelRoute("complex", primary, "unclassified_topic")
    if len(words) > 24:
        return ModelRoute("complex", primary, "long_answer")
    if small == primary:
        return ModelRoute("simple", primary, "simple_model_not_configured")
    return ModelRoute("simple", small, "validated_single_topic_answer")
