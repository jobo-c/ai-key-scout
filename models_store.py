"""In-memory key records for the UI."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Set


@dataclass
class KeyRecord:
    key: str
    provider: str
    sources: Set[str] = field(default_factory=set)
    status: str = "pending"  # pending | valid | invalid | error
    remaining: Optional[float] = None
    balance_summary: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    models: List[str] = field(default_factory=list)
    # Models that actually ANSWERED a "hi" prompt with this key (see modeltest.py).
    working_models: List[str] = field(default_factory=list)
    score: float = 0.0
    info: str = ""
    error: str = ""
    # Sample source lines from populated .txt / JSON / .env dumps (where the key was found)
    context_lines: List[str] = field(default_factory=list)

    def mask(self, keep: int = 6) -> str:
        k = self.key
        if len(k) <= keep + 6:
            return k
        return f"{k[:keep]}...{k[-6:]}"

    def add_context(self, line: str, limit: int = 5) -> None:
        line = (line or "").strip()
        if not line or line in self.context_lines:
            return
        self.context_lines.append(line[:400])
        if len(self.context_lines) > limit:
            self.context_lines = self.context_lines[:limit]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["sources"] = sorted(self.sources)
        return d


class KeyStore:
    def __init__(self) -> None:
        self.order: List[str] = []
        self.records: Dict[str, KeyRecord] = {}

    def clear(self) -> None:
        self.order.clear()
        self.records.clear()

    def __len__(self) -> int:
        return len(self.order)

    def add(
        self,
        key: str,
        provider: str,
        source: str = "pasted",
        context: str = "",
    ) -> KeyRecord:
        if key in self.records:
            rec = self.records[key]
            rec.sources.add(source)
            if context:
                rec.add_context(context)
            # upgrade provider if higher priority later handled by caller
            return rec
        # containment: drop shorter keys inside this one
        for existing in list(self.order):
            if existing != key and existing in key:
                self.remove(existing)
        # skip if this key is contained in a longer one
        for existing in self.order:
            if existing != key and key in existing:
                return self.records[existing]
        rec = KeyRecord(key=key, provider=provider, sources={source})
        if context:
            rec.add_context(context)
        self.records[key] = rec
        self.order.append(key)
        return rec

    def remove(self, key: str) -> None:
        self.records.pop(key, None)
        if key in self.order:
            self.order.remove(key)

    def get(self, key: str) -> Optional[KeyRecord]:
        return self.records.get(key)

    def all(self) -> List[KeyRecord]:
        return [self.records[k] for k in self.order if k in self.records]

    def update_from_check(self, key: str, **kwargs: Any) -> Optional[KeyRecord]:
        rec = self.records.get(key)
        if not rec:
            return None
        for k, v in kwargs.items():
            if hasattr(rec, k):
                setattr(rec, k, v)
        return rec
