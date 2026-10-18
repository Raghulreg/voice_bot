"""Duplicate detection for gold exemplars and live follow-up calls."""

from __future__ import annotations

import re
import time
from itertools import combinations
from typing import Any

from services.call_storage import list_saved_call_files, load_call_json

# Nearc-duplicate threshold for scenario text (Jaccard on word tokens).
DEFAULT_SCENARIO_SIMILARITY = 0.82

ACTIVE_CALL_STATUSES = frozenset(
    {"initiated", "calling", "ringing", "answered", "in-progress"}
)

RECENT_CALL_HOURS = 24


def normalize_text(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def token_set(text: str) -> set[str]:
    return {w for w in normalize_text(text).split() if len(w) > 2}


def jaccard_similarity(a: str, b: str) -> float:
    sa, sb = token_set(a), token_set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def scenario_text_blob(scenario: dict[str, Any]) -> str:
    parts = [
        scenario.get("conversation_context", ""),
        scenario.get("ai_understanding", ""),
        scenario.get("ai_response", ""),
        scenario.get("executive_response", ""),
        scenario.get("call_outcome", ""),
    ]
    return " ".join(str(p) for p in parts if p)


def find_scenario_duplicates(
    scenarios: list[dict[str, Any]],
    *,
    similarity_threshold: float = DEFAULT_SCENARIO_SIMILARITY,
) -> dict[str, Any]:
    """Audit scenarios for exact ID clashes and near-duplicate dialogue text."""
    exact_id_clashes: list[str] = []
    seen_ids: dict[str, int] = {}
    for s in scenarios:
        sid = str(s.get("scenario_id") or "")
        if not sid:
            continue
        seen_ids[sid] = seen_ids.get(sid, 0) + 1
    exact_id_clashes = [sid for sid, n in seen_ids.items() if n > 1]

    near_pairs: list[dict[str, Any]] = []
    for i, j in combinations(range(len(scenarios)), 2):
        a, b = scenarios[i], scenarios[j]
        id_a = str(a.get("scenario_id") or f"row-{i}")
        id_b = str(b.get("scenario_id") or f"row-{j}")
        if id_a == id_b:
            continue

        sim = jaccard_similarity(scenario_text_blob(a), scenario_text_blob(b))
        if sim < similarity_threshold:
            continue

        same_profile = a.get("tpa_executive_profile") == b.get("tpa_executive_profile")
        same_claim = (a.get("claim_type") or "") == (b.get("claim_type") or "")

        near_pairs.append(
            {
                "scenario_a": id_a,
                "scenario_b": id_b,
                "similarity": round(sim, 3),
                "same_profile": same_profile,
                "same_claim_type": same_claim,
                "profile_a": a.get("tpa_executive_profile"),
                "profile_b": b.get("tpa_executive_profile"),
                "claim_type_a": a.get("claim_type"),
                "claim_type_b": b.get("claim_type"),
            }
        )

    near_pairs.sort(key=lambda p: p["similarity"], reverse=True)
    flagged = len(exact_id_clashes) + len(near_pairs)
    total = len(scenarios)
    pct = round((flagged / total * 100) if total else 0.0, 1)

    return {
        "total_scenarios": total,
        "exact_duplicate_ids": exact_id_clashes,
        "near_duplicate_pairs": near_pairs,
        "near_duplicate_count": len(near_pairs),
        "flagged_count": flagged,
        "duplicate_percentage": pct,
        "similarity_threshold": similarity_threshold,
        "status": "pass" if flagged == 0 else "review",
        "message": (
            "No duplicates detected."
            if flagged == 0
            else f"{flagged} duplicate issue(s) found — review before adding to corpus."
        ),
    }


def _claim_phone_key(claim_id: str, phone: str) -> tuple[str, str]:
    claim = re.sub(r"\s+", "", (claim_id or "").strip().lower())
    phone_digits = re.sub(r"\D", "", phone or "")
    return claim, phone_digits


def _collect_call_records(in_memory: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for r in in_memory.values():
        cid = r.get("call_id")
        if cid:
            by_id[str(cid)] = r

    for path in list_saved_call_files():
        try:
            data = load_call_json(path.stem)
        except Exception:
            # load_call_json should not raise; belt-and-suspenders for initiate path
            continue
        if not data or not data.get("call_id"):
            continue
        cid = str(data["call_id"])
        if cid not in by_id:
            by_id[cid] = data

    return list(by_id.values())


def check_call_duplicate(
    claim_id: str,
    phone: str,
    in_memory_records: dict[str, dict[str, Any]],
    *,
    recent_hours: int = RECENT_CALL_HOURS,
) -> dict[str, Any] | None:
    """
    Return duplicate call info if the same claim+phone should not be dialed again.
    Blocks active duplicates; warns on recent completed calls.
    Never raises — corrupt disk records are skipped.
    """
    claim_key, phone_key = _claim_phone_key(claim_id, phone)
    if not claim_key or not phone_key:
        return None

    now = time.time()
    cutoff = now - recent_hours * 3600

    try:
        records = _collect_call_records(in_memory_records)
    except Exception:
        # Do not block call start if history scan fails
        return None

    for record in records:
        try:
            r_claim, r_phone = _claim_phone_key(
                str(record.get("claim_id") or ""),
                str(record.get("customer_phone") or ""),
            )
            if r_claim != claim_key or r_phone != phone_key:
                continue

            status = record.get("status", "")
            if status in ACTIVE_CALL_STATUSES:
                return {
                    "block": True,
                    "reason": "active_call",
                    "message": (
                        f"A follow-up call for claim {claim_id or claim_key} is already in progress. "
                        "Wait for it to finish before dialing again."
                    ),
                    "existing_call_id": record.get("call_id"),
                    "existing_status": status,
                }

            created = float(record.get("created_at") or 0)
            # Completed / failed prior calls: never hard-block — allow re-dial for
            # testing and legitimate follow-ups. Return soft warn only.
            if status in ("completed", "failed", "no-answer", "busy", "canceled", "cancelled") and created >= cutoff:
                return {
                    "block": False,
                    "reason": "recent_prior_call",
                    "message": (
                        f"Note: this claim+phone had a {status} call within the last "
                        f"{recent_hours} hours. Starting another call anyway."
                    ),
                    "existing_call_id": record.get("call_id"),
                    "existing_status": status,
                    "completed_at": created,
                }
        except Exception:
            continue

    return None
