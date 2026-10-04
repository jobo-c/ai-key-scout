"""Score keys and format ranked best-accounts export."""

from __future__ import annotations

import time
from typing import List, Optional

from models_store import KeyRecord
from providers import provider_name
from security import fingerprint, mask_secret


def score_record(rec: KeyRecord) -> float:
    """Bounded 0-100 capability score with a persisted component breakdown."""
    d = rec.details or {}
    if rec.status == "invalid":
        d["score_breakdown"] = {"authentication": 0}
        rec.details = d
        return 0.0
    if rec.status in ("pending", "error"):
        return 0.0
    if rec.status in ("unknown", "rate_limited"):
        return 15.0

    points = {}
    # Authentication is the foundation.
    points["authentication"] = 25.0
    if d.get("state") == "valid_no_funds":
        points["authentication"] = 20.0

    kind = d.get("provider_kind") or ("tool" if d.get("capabilities") and not rec.models else "llm")
    if kind == "tool":
        caps = len(d.get("capabilities") or [])
        points["capabilities"] = min(20.0, 5.0 + caps * 2.5)
        points["service_response"] = 20.0 if d.get("http_status") in (200, 201, 202) else 10.0
        points["account_info"] = 10.0 if any(k in d for k in ("plan", "tier", "organization", "account", "email", "username")) else 4.0
        rl = d.get("rate_limit") or {}
        points["rate_limits"] = 10.0 if rl.get("remaining") is not None else 5.0
        latency = d.get("latency_ms")
        points["latency"] = 10.0 if latency is None else (10.0 if float(latency) <= 500 else 7.0 if float(latency) <= 1500 else 4.0)
    else:
        working = len(rec.working_models or [])
        model_count = len(rec.models or [])
        points["working_models"] = min(25.0, working * 5.0)
        points["model_catalog"] = min(15.0, model_count * 0.75)
        if working:
            points["inference_proof"] = 20.0
        elif d.get("model_tests"):
            points["inference_proof"] = 5.0
        else:
            points["inference_proof"] = 0.0
        rem = rec.remaining
        if rem is not None:
            try:
                points["usable_credit"] = 10.0 if float(rem) > 0 else 0.0
            except Exception:
                points["usable_credit"] = 2.0
        else:
            points["usable_credit"] = 4.0
        rl = d.get("rate_limit") or {}
        points["rate_limits"] = 5.0 if rl.get("remaining") is not None else 2.0
        points["latency"] = 5.0 if d.get("latency_ms") is None or float(d.get("latency_ms", 9999)) <= 1000 else 2.0

    if d.get("is_free_tier") is True:
        # Free is useful, but not automatically better than a funded account.
        points["tier"] = 2.0
    elif d.get("is_free_tier") is False:
        points["tier"] = 5.0
    else:
        points["tier"] = 3.0

    total = min(100.0, max(0.0, sum(points.values())))
    d["score_breakdown"] = {k: round(v, 2) for k, v in points.items()}
    d["score_total"] = round(total, 2)
    d["score_version"] = "5.2"
    d["provider_kind"] = kind
    rec.details = d
    return round(total, 2)

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
        # AI Key Scout v5 — Best Accounts (ranked),
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
        lines.append(f"    Credential: {mask_secret(r.key)}  (fingerprint: {fingerprint(r.key)})")
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
