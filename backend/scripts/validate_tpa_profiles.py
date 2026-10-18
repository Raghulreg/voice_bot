"""Validate all 20 TPA profiles are wired consistently (run: python -m scripts.validate_tpa_profiles)."""

from __future__ import annotations

import sys

from claims_scenarios import GOLD_EXEMPLARS, TPA_EXECUTIVE_PROFILES, get_exemplars_by_profile
from services.profile_registry import ALL_TPA_EXECUTIVE_PROFILES, validate_registry_against_claims

# Frontend mirror (keep in sync with frontend/src/api.ts TPA_PROFILES)
FRONTEND_TPA_PROFILES = [
    "Helpful Executive",
    "Busy Executive",
    "Experienced Executive",
    "New Executive",
    "Cooperative Executive",
    "Uncooperative Executive",
    "Process Driven Executive",
    "Rule Focused Executive",
    "Empathetic Executive",
    "Impatient Executive",
    "Escalation Focused Executive",
    "Supervisor Type",
    "Technical Executive",
    "Incomplete Information Executive",
    "Slow Response Executive",
    "Fast Response Executive",
    "Knowledgeable Executive",
    "Confused Executive",
    "Defensive Executive",
    "Polite Executive",
]


def main() -> int:
    errors: list[str] = []
    errors.extend(validate_registry_against_claims(TPA_EXECUTIVE_PROFILES))

    if set(FRONTEND_TPA_PROFILES) != set(ALL_TPA_EXECUTIVE_PROFILES):
        errors.append("frontend TPA_PROFILES != profile_registry.ALL_TPA_EXECUTIVE_PROFILES")
    if list(FRONTEND_TPA_PROFILES) != list(ALL_TPA_EXECUTIVE_PROFILES):
        errors.append("frontend profile order differs from registry (cosmetic)")

    for profile in ALL_TPA_EXECUTIVE_PROFILES:
        ex = get_exemplars_by_profile(profile)
        if len(ex) < 1:
            errors.append(f"no gold exemplars for profile: {profile}")

    for ex in GOLD_EXEMPLARS:
        prof = ex.get("tpa_executive_profile")
        if prof and prof not in ALL_TPA_EXECUTIVE_PROFILES:
            errors.append(f"exemplar unknown profile: {prof}")

    if errors:
        print("TPA profile validation FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1

    print(f"OK: all {len(ALL_TPA_EXECUTIVE_PROFILES)} TPA profiles aligned (claims, registry, frontend, exemplars).")
    for profile in ALL_TPA_EXECUTIVE_PROFILES:
        n = len(get_exemplars_by_profile(profile))
        print(f"  {profile}: {n} exemplar(s), directive + fallback wired")
    return 0


if __name__ == "__main__":
    sys.exit(main())
