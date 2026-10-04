"""Output files — a "working keys" report that other tools (and Hermes) can read.

Everything here is plain data + plain text on purpose: no Qt, no network, no
state. The GUI calls write_working_file() after a model test or an export, but the
formatting can be unit-tested and the files can be consumed by anything.

File shape (working_keys.txt):

    # comments...
    # ---- machine-readable section: one pipe-delimited row per working model
    key | provider | provider_name | model | latency_ms | account | reply
    # ---- human/detail section
    key: ...
    provider: ...
    working_models: ...
    tested:
      [ok] model (12ms) reply="Hello"
      [--] model (HTTP 404) ...

Pipe-delimited rows are the contract: parse by splitting on " | " and ignoring
lines starting with '#'. Replies have "|" and newlines replaced so a row can never
break the format.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple\n\nfrom .security import fingerprint, mask_secret\n

WORKING_TXT = "working_keys.txt"
WORKING_JSON = "working_keys.json"
SEP = " | "


def _clean(text: Any, limit: int = 300) -> str:
    """Make a value safe for a pipe-delimited line."""
    s = str(text or "")
    s = s.replace("|", "/").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = " ".join(s.split())
    return s[:limit]


def _latency(test: Dict[str, Any]) -> str:
    ms = test.get("latency_ms")
    return str(ms) if ms is not None else ""


def working_rows(records) -> List[Dict[str, Any]]:
    """One entry per (key, working model) pair, richest evidence first."""
    rows: List[Dict[str, Any]] = []
    for r in records:
        working = list(getattr(r, "working_models", None) or [])
        if not working:
            continue
        details = getattr(r, "details", None) or {}
        tests = {t.get("model"): t for t in (details.get("model_tests") or [])
                 if isinstance(t, dict)}
        remaining = getattr(r, "remaining", None)
        for model in working:
            t = tests.get(model) or {}
            rows.append({
                "fingerprint": fingerprint(getattr(r, "key", "")),
                "masked_key": mask_secret(getattr(r, "key", "")),
                "provider": getattr(r, "provider", ""),
                "model": model,
                "tier": t.get("tier") or ("free" if str(model).endswith(":free") or str(model) == "openrouter/free" else "unknown"),
                "latency_ms": _latency(t),
                "account": "" if remaining is None else f"{float(remaining):.2f}",
                "reply": _clean(t.get("reply") or t.get("error") or "", 300),
                "text": bool(t.get("text", True)),
                "score": round(float(getattr(r, "score", 0) or 0), 2),
                "currency": (details.get("currency") or ""),
            })
    rows.sort(key=lambda x: (-x["score"], x["provider"], x["model"]))
    return rows


def format_working_txt(records, source_dir: str = "") -> Tuple[str, int]:
    """Render working_keys.txt. Returns (text, number_of_rows)."""
    rows = working_rows(records)
    keys = {r["fingerprint"] for r in rows}
    lines: List[str] = [
        "#" + "=" * 74,
        "# AI Key Scout v5 — WORKING KEYS",
        "# Every key that answered a real 'hi' prompt, per model.",
        f"# generated : {time.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    if source_dir:
        lines.append(f"# source    : {source_dir}")
    lines += [
        f"# counts    : {len(keys)} key(s), {len(rows)} working model(s)",
        "#" + "-" * 74,
        "# MACHINE-READABLE SECTION",
        "# one record per line, fields separated by ' | ' (space pipe space).",
        "# lines beginning with '#' are comments and should be skipped.",
        "# fields: key | provider | provider_name | model | tier | latency_ms | account | reply",
        "#   account = remaining balance on the account ('' when the provider",
        "#             exposes no balance API); reply = the model's answer to 'hi'",
        "#             ('' when the model ran but returned no visible text).",
        "#" + "-" * 74,
    ]
    for row in rows:
        lines.append(SEP.join([
            row["fingerprint"], row["provider"], _clean(provider_label(row["provider"])),
            row["model"], row["tier"], row["latency_ms"], row["account"], row["reply"] or "",
        ]))
    lines += [
        "#" + "-" * 74,
        "# DETAIL SECTION (same data, grouped per key)",
        "# every line below is a comment too, so a parser can treat any line that",
        "# does not start with '#' as a data row.",
        "#" + "=" * 74,
        "",
    ]

    by_key: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_key.setdefault(row["fingerprint"], []).append(row)

    for key, krows in by_key.items():
        r0 = krows[0]
        rec = _find_by_fingerprint(records, key)
        details = (getattr(rec, "details", None) or {}) if rec else {}
        lines.append(f"# credential: {key} ({r0.get("masked_key", "")})")
        lines.append(f"# provider: {r0['provider']} ({provider_label(r0['provider'])})")
        if r0["account"]:
            cur = f" {r0['currency']}" if r0["currency"] else ""
            lines.append(f"# account: {r0['account']}{cur}")
        lines.append(f"# working_models: {'; '.join(r['model'] for r in krows)}")
        lines.append("# tested:")
        for t in (details.get("model_tests") or []):
            if not isinstance(t, dict):
                continue
            if t.get("ok"):
                mark = "ok" if t.get("text", True) else "ok*"
                tier = t.get("tier") or ("free" if str(t.get("model") or "").endswith(":free") or str(t.get("model") or "") == "openrouter/free" else "unknown")
                body = f'reply="{_clean(t.get("reply") or "", 200)}"' if t.get("text", True) \
                    else "ran but returned no visible text"
                lines.append(f"#   [{mark}] {t.get('model')} [{tier}] ({t.get('latency_ms', '?')}ms) {body}")
            else:
                lines.append(f"#   [--] {t.get('model')} ({_clean(t.get('error') or t.get('status'), 160)})")
        if rec is not None:
            lines.append(f"# score: {float(getattr(rec, 'score', 0) or 0):.2f}")
            sources = sorted(getattr(rec, "sources", None) or [])
            if sources:
                lines.append(f"# sources: {'; '.join(sources[:4])}")
        lines.append("")
    if not rows:
        lines.append("# (no key has answered a 'hi' prompt yet — run 'Test models (say hi)')")
        lines.append("")
    lines.append("#" + "=" * 74)
    lines.append(f"# {len(keys)} key(s) / {len(rows)} working model(s)")
    return "\n".join(lines) + "\n", len(rows)


def _find_by_fingerprint(records, fp: str):
    for r in records:
        if fingerprint(getattr(r, "key", "") or "") == fp:
            return r
    return None


def provider_label(pid: str) -> str:
    """Human name for a provider id, without importing the registry graph."""
    try:
        from .providers import provider_name
        return provider_name(pid)
    except Exception:
        return pid


def _find(records, key: str):
    for r in records:
        if getattr(r, "key", None) == key:
            return r
    return None


def records_from_history(path: str) -> List[Any]:
    """Rebuild lightweight records from a history.json file.

    Lets the report be regenerated from disk (useful for a cron job / a Hermes
    run) without launching the GUI.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    keys = (data or {}).get("keys") or {}
    out = []
    for key, row in keys.items():
        if not isinstance(row, dict):
            continue
        if not row.get("working_models"):
            continue
        out.append(_Rec(key=key, **{
            "provider": row.get("provider") or "unknown",
            "remaining": row.get("remaining"),
            "score": row.get("score") or 0,
            "details": row.get("details") or {},
            "working_models": row.get("working_models") or [],
            "sources": set(row.get("sources") or []),
        }))
    return out


class _Rec:
    """Minimal duck-typed record for records_from_history()."""

    def __init__(self, key: str, provider: str = "", remaining: Optional[float] = None,
                 score: float = 0, details: Optional[Dict[str, Any]] = None,
                 working_models: Optional[List[str]] = None, sources=None, **_: Any):
        self.key = key
        self.provider = provider
        self.remaining = remaining
        self.score = score
        self.details = details or {}
        self.working_models = working_models or []
        self.sources = set(sources or [])


def format_working_json(records) -> Dict[str, Any]:
    rows = working_rows(records)
    return {
        "tool": "AI Key Scout v4",
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "keys": len({r["key"] for r in rows}),
        "working_models": len(rows),
        "records": rows,
    }


def write_working_file(records, out_dir: str,
                       filename: str = WORKING_TXT) -> Tuple[str, str, int]:
    """Write working_keys.txt (+ a .json mirror). Returns (txt, json, row_count)."""
    txt_path = os.path.join(out_dir, filename)
    json_path = os.path.join(out_dir, os.path.splitext(filename)[0] + ".json")
    text, n = format_working_txt(records, source_dir=out_dir)
    tmp = txt_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, txt_path)
    tmpj = json_path + ".tmp"
    with open(tmpj, "w", encoding="utf-8") as f:
        json.dump(format_working_json(records), f, indent=2, ensure_ascii=False)
    os.replace(tmpj, json_path)
    return txt_path, json_path, n
