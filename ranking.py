"""Score keys and format ranked best-accounts export."""

from __future__ import annotations

import time
from typing import List, Optional

from .models_store import KeyRecord
from .providers import provider_name


def score_record(rec: KeyRecord) -> float:
    """Higher = better API to keep. Used for Best list ranking."""
    if rec.status != "valid":
        # rate_limited / unknown are inconclusive, not good — but they are also
        # not as bad as a revoked key, and they are retried rather than dropped.
        return -100.0 if rec.status == "invalid" else -60.0
    score = 20.0
    rem = rec.remaining
    details = rec.details or {}

    if details.get("state") == "valid_no_funds":
        # Authenticated but out of credit: proves the key is real, worth keeping
        # for the account it reveals, but not usable right now.
        score -= 45
    if details.get("discovered_as"):
        score += 4  # opaque key that we positively identified is more useful
    if details.get("org_usage_visible"):
        score += 12  # admin/console-level access
    if details.get("auth_note"):
        score -= 2
    # Having answered a real "hi" is the strongest proof a key works — worth more
    # than a balance figure, because it cannot be a stale or public endpoint.
    if rec.working_models:
        score += 25 + min(len(rec.working_models), 5) * 3
    if (details.get("model_tests") and not rec.working_models
            and details.get("model_test_note")):
        score -= 5  # tested and nothing answered

    if rem is not None:
        rem_f = float(rem)
        if rem_f < 0:
            score -= 80  # drained / negative — not useful
        elif rem_f == 0:
            score -= 40
        else:
            score += min(rem_f, 500.0)  # $1 ≈ 1 point, capped
            if rem_f >= 1:
                score += 15
            if rem_f >= 10:
                score += 20
    elif details.get("has_balance") is False and rec.models:
        score += 8  # valid with models but no $ API

    if details.get("is_free_tier") is True:
        score -= 25
    if details.get("is_free_tier") is False and (rem is None or float(rem) > 0):
        score += 15
    if details.get("limit") is None and details.get("is_free_tier") is False and (rem is None or float(rem) > 0):
        score += 10  # uncapped paid with credit left / unknown
    if details.get("is_management_key"):
        score += 12
    if rec.models:
        score += min(len(rec.models), 20) * 0.5
    return score


def is_best_candidate(rec: KeyRecord, min_remaining: float = 0.01, require_valid: bool = True) -> bool:
    if require_valid and rec.status != "valid":
        return False
    rem = rec.remaining
    details = rec.details or {}
    # Authenticated-but-broke keys are interesting but never "best"
    if details.get("state") == "valid_no_funds":
        return False
    # A key that answered a real prompt outranks everything else, even if the only
    # thing we know about it came from a rescue probe.
    if rec.working_models:
        return True
    if details.get("auth_note") and rem is None:
        return False  # only proved alive via a model-failure rescue
    # Negative / zero balance is never "best"
    if rem is not None and float(rem) < min_remaining:
        return False
    if rem is not None and float(rem) >= min_remaining:
        return True
    if details.get("is_management_key"):
        return True
    # A key that answered a real prompt is worth keeping even without a balance API.
    if rec.working_models:
        return True
    # paid uncapped with unknown remaining still interesting
    if rem is None and details.get("is_free_tier") is False and details.get("limit") is None:
        return True
    # valid keys with many models and no free flag
    if rem is None and rec.status == "valid" and not details.get("is_free_tier") and len(rec.models) >= 3:
        return True
    return False


def sort_best(records: List[KeyRecord]) -> List[KeyRecord]:
    return sorted(records, key=lambda r: (-r.score, -(r.remaining or -1.0), r.provider))


def _money(v: Optional[float], digits: int = 2) -> str:
    if v is None:
        return "n/a"
    try:
        x = float(v)
    except Exception:
        return str(v)
    if digits == 2:
        return f"${x:.2f}"
    s = f"${x:.{digits}f}".rstrip("0").rstrip(".")
    return s or "$0"


def format_best_report(records: List[KeyRecord], min_remaining: float = 0.01) -> str:
    ranked = sort_best([r for r in records if is_best_candidate(r, min_remaining)])
    lines = [
        "# AI Key Scout v4 — Best Accounts (ranked)",
        f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"# Min remaining filter: {_money(min_remaining)}",
        "#" + "=" * 60,
        "",
    ]
    if not ranked:
        lines.append("(no keys met the best-account criteria)")
        lines.append("")
    for i, r in enumerate(ranked, 1):
        rem_s = _money(r.remaining) if r.remaining is not None else "unknown"
        lines.append(f"#{i}  remaining: {rem_s}  score: {r.score:.1f}")
        lines.append(r.key)
        lines.append(f"    Provider:  {provider_name(r.provider)} ({r.provider})")
        lines.append(f"    Status:    {r.status}")
        if r.balance_summary:
            lines.append(f"    Summary:   {r.balance_summary}")
        if r.info:
            lines.append(f"    Info:      {r.info}")
        if r.models:
            preview = ", ".join(r.models[:8])
            extra = f" (+{len(r.models) - 8} more)" if len(r.models) > 8 else ""
            lines.append(f"    Models:    {preview}{extra}")
        d = r.details or {}
        if r.working_models:
            lines.append(f"    WORKING:   {'; '.join(r.working_models)}   ← answered 'hi'")
        for t in (d.get("model_tests") or []):
            if not isinstance(t, dict):
                continue
            mark = "ok " if t.get("ok") else "no "
            detail = t.get("reply") if t.get("ok") else (t.get("error") or t.get("status"))
            lines.append(f"      [{mark}] {t.get('model')}  ({t.get('latency_ms', '?')}ms)  {detail}")
        if d.get("model_test_note"):
            lines.append(f"    Note:      model test: {d['model_test_note']}")
        if d.get("total_credits") is not None:
            lines.append(
                f"    Account:   {_money(r.remaining)} remaining "
                f"({_money(d.get('total_credits'))} purchased / {_money(d.get('total_usage'))} used)"
            )
        if d.get("usage") is not None:
            lines.append(
                f"    Usage:     all {_money(d.get('usage'), 4)} | "
                f"d/w/m {_money(d.get('usage_daily'), 4)}/"
                f"{_money(d.get('usage_weekly'), 4)}/{_money(d.get('usage_monthly'), 4)}"
            )
        lines.append(f"    Sources:   {'; '.join(sorted(r.sources))}")
        lines.append("")
    lines.append("#" + "=" * 60)
    lines.append(f"# Best listed: {len(ranked)} / {len(records)} total keys")
    return "\n".join(lines)
