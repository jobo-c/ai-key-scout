"""Persistent history of found API keys across runs (history.json)."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional


def history_path(base_dir: Optional[str] = None) -> str:
    root = base_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "history.json")


def load_history(path: Optional[str] = None) -> Dict[str, Any]:
    p = path or history_path()
    if not os.path.isfile(p):
        return {"version": 1, "updated_at": None, "keys": {}}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"version": 1, "updated_at": None, "keys": {}}
        data.setdefault("version", 1)
        data.setdefault("keys", {})
        if isinstance(data["keys"], list):
            # legacy list → map
            m = {}
            for row in data["keys"]:
                if isinstance(row, dict) and row.get("key"):
                    m[row["key"]] = row
            data["keys"] = m
        return data
    except Exception:
        return {"version": 1, "updated_at": None, "keys": {}}


def save_history(data: Dict[str, Any], path: Optional[str] = None) -> str:
    p = path or history_path()
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data["version"] = 1
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)
    return p


def merge_records(existing: Dict[str, Any], records: List[Any]) -> int:
    """Merge KeyRecord-like objects into history map. Returns number of new keys."""
    keys: Dict[str, Any] = existing.setdefault("keys", {})
    added = 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for r in records:
        key = getattr(r, "key", None) or (r.get("key") if isinstance(r, dict) else None)
        if not key:
            continue
        if key not in keys:
            added += 1
            keys[key] = {
                "key": key,
                "provider": getattr(r, "provider", None) or (r.get("provider") if isinstance(r, dict) else "unknown"),
                "first_seen": now,
                "last_seen": now,
                "sources": sorted(getattr(r, "sources", set()) or (r.get("sources") if isinstance(r, dict) else []) or []),
                "status": getattr(r, "status", "") or "",
                "remaining": getattr(r, "remaining", None),
                "balance_summary": getattr(r, "balance_summary", "") or "",
                "score": getattr(r, "score", 0) or 0,
                "info": getattr(r, "info", "") or "",
                "models": list(getattr(r, "models", None) or (r.get("models") if isinstance(r, dict) else []) or []),
                "working_models": list(getattr(r, "working_models", None) or (r.get("working_models") if isinstance(r, dict) else []) or []),
                "details": dict(getattr(r, "details", None) or (r.get("details") if isinstance(r, dict) else {}) or {}),
            }
        else:
            row = keys[key]
            row["last_seen"] = now
            src = set(row.get("sources") or [])
            extra = getattr(r, "sources", None)
            if extra:
                src |= set(extra)
            row["sources"] = sorted(src)
            # refresh check fields if present
            st = getattr(r, "status", None)
            if st and st not in ("pending", ""):
                row["status"] = st
            if getattr(r, "remaining", None) is not None:
                row["remaining"] = r.remaining
            if getattr(r, "balance_summary", None):
                row["balance_summary"] = r.balance_summary
            if getattr(r, "score", None):
                row["score"] = r.score
            if getattr(r, "info", None):
                row["info"] = r.info
            if getattr(r, "provider", None):
                row["provider"] = r.provider
            models = getattr(r, "models", None)
            if models:
                row["models"] = list(models)
            working = getattr(r, "working_models", None)
            if working:
                # union — a re-test should never lose a model that worked before
                prev = list(row.get("working_models") or [])
                row["working_models"] = sorted(set(prev) | set(working))
            details = getattr(r, "details", None)
            if details:
                row["details"] = dict(details)
    return added
