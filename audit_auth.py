#!/usr/bin/env python3
"""Audit every provider's validate endpoint against a bogus key.

An endpoint that answers 200 to a key we just made up cannot be used to prove
authentication. v3 shipped four of these (NVIDIA, Jina, SambaNova, DeepInfra) and
they silently reported garbage as "Valid" — this script finds them automatically
so the list can never drift again.

Run:  python3 tools/audit_auth.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from details import check_provider  # noqa: E402
from providers import PROVIDERS, SCAN_ORDER  # noqa: E402

BOGUS = "totally-invalid-key-1234567890abcdef"

# Providers whose validate is a real generation call by construction.
INHERENTLY_SAFE = {"anthropic", "perplexity", "cohere", "tavily", "voyage"}


async def main() -> int:
    import aiohttp

    unsafe = []
    unknown = []
    async with aiohttp.ClientSession() as session:
        for pid in SCAN_ORDER:
            cfg = PROVIDERS[pid]
            if not cfg.get("validate"):
                continue
            try:
                res = await check_provider(session, BOGUS, pid, timeout=12, light=True,
                                           discover=False)
            except Exception as e:
                unknown.append((pid, f"exception {str(e)[:60]}"))
                continue
            state = res.get("state")
            tag = "SAFE " if state == "invalid" else ("LEAK " if state == "valid" else "ODD  ")
            print(f"{tag} {pid:14} state={state:16} summary={res.get('balance_summary','')[:60]!r}")
            if state == "valid" and pid not in INHERENTLY_SAFE:
                unsafe.append(pid)
            elif state in ("unknown", "valid_no_funds"):
                unknown.append((pid, state))

    print()
    if unsafe:
        print("!! A bogus key was accepted by:", ", ".join(unsafe))
        print("   Each of these needs a `probe` (real inference) in providers.py.")
    else:
        print("No provider accepted a bogus key.")
    if unknown:
        print("Inconclusive (needs a human look):", unknown)
    return 1 if unsafe else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
