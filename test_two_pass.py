#!/usr/bin/env python3
"""Integration test: does the two-pass CheckWorker actually enrich?

Qt is exercised offscreen. check_provider is stubbed so the test is deterministic
and makes no network calls — we are testing the orchestration (sweep → enrich
only the survivors → emit richer records), not the providers.

Run:  QT_QPA_PLATFORM=offscreen python3 tools/test_two_pass.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import QCoreApplication  # noqa: E402

import app.workers as W  # noqa: E402

CALLS = []


async def fake_check_provider(session, key, provider, timeout=10, proxy=None,
                              light=True, discover=True):
    """Light pass: valid but information-poor. Deep pass: same key + real detail."""
    CALLS.append((key, provider, light))
    if key == "valid-key":
        if light:
            return {"valid": True, "state": "valid", "error": "", "remaining": None,
                    "balance_summary": "NVIDIA authenticated", "details": {"light": True},
                    "models": [], "info": "ok"}
        return {"valid": True, "state": "valid", "error": "", "remaining": 42.5,
                "balance_summary": "NVIDIA $42.50 · 12 models",
                "details": {"model_count": 12}, "models": ["a", "b"], "info": "models=12"}
    if key == "throttled-key":
        return {"valid": False, "state": "rate_limited", "error": "HTTP 429",
                "remaining": None, "balance_summary": "", "details": {},
                "models": [], "info": "", "rate_limited": True, "retry_after": None}
    if key == "broke-key":
        return {"valid": False, "state": "valid_no_funds", "error": "no credit",
                "remaining": None, "balance_summary": "", "details": {},
                "models": [], "info": ""}
    if key == "flaky-key":
        return {"valid": False, "state": "unknown", "error": "HTTP 502",
                "remaining": None, "balance_summary": "", "details": {},
                "models": [], "info": ""}
    return {"valid": False, "state": "invalid", "error": "HTTP 401",
            "remaining": None, "balance_summary": "", "details": {}, "models": [], "info": ""}


def test_model_worker() -> int:
    """ModelTestWorker: only valid keys are tested, results land on the record."""
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    calls = []

    async def fake_test(session, key, provider, catalog=None, limit=3, timeout=12, proxy=None):
        calls.append((key, provider, tuple(catalog or ())))
        if key == "good-key":
            return {"tested": [{"model": "m1", "ok": True, "text": True,
                                "reply": "Hello!", "latency_ms": 12}],
                    "working": ["m1"], "note": ""}
        return {"tested": [{"model": "m1", "ok": False, "error": "HTTP 401"}],
                "working": [], "note": ""}

    W.test_models_concurrent = fake_test
    results = {}
    done = []
    items = [("good-key", "groq", ["llama-3.1-8b-instant"]),
             ("dead-key", "groq", ["llama-3.1-8b-instant"])]
    worker = W.ModelTestWorker(items, concurrency=2, timeout=5, models_per_key=2)
    worker.result.connect(lambda k, p: results.__setitem__(k, p))
    worker.finished_ok.connect(lambda: done.append(True))
    worker.start()
    for _ in range(600):
        app.processEvents()
        if done:
            break
        worker.wait(20)
    worker.wait(2000)
    app.processEvents()

    fails = 0

    def check(name, cond, extra=""):
        nonlocal fails
        if cond:
            print(f"  ok   {name}")
        else:
            fails += 1
            print(f"  FAIL {name} {extra}")

    check("model worker finished", bool(done))
    check("both keys were tested", len(calls) == 2, str(calls))
    check("catalog is forwarded to the tester",
          calls and calls[0][2] == ("llama-3.1-8b-instant",), str(calls))
    check("working key reports its model", results["good-key"]["working"] == ["m1"])
    check("dead key reports nothing working", results["dead-key"]["working"] == [])
    check("summary is attached", results["good-key"].get("summary", "").startswith("WORKS"))
    return fails


def main() -> int:
    w = W
    fake = fake_check_provider
    fails = run_check_worker_test(w, fake)
    fails += test_model_worker()
    print(f"\n{fails} failures")
    return 1 if fails else 0


def run_check_worker_test(W, fake_check_provider) -> int:
    app = QCoreApplication(sys.argv)
    W.check_provider = fake_check_provider  # stub the network layer
    W.PROVIDER_MIN_INTERVAL.clear()        # no artificial pacing in the test

    items = [("valid-key", "nvidia"), ("throttled-key", "nvidia"),
             ("broke-key", "openai"), ("flaky-key", "groq"), ("dead-key", "openai")]
    results = {}
    phases = []

    worker = W.CheckWorker(items, concurrency=5, timeout=5, light=True, enrich_valid=True)
    worker.result.connect(lambda k, p: results.__setitem__(k, p))
    worker.phase.connect(phases.append)
    done = []
    worker.finished_ok.connect(lambda: done.append(True))

    worker.start()
    for _ in range(600):
        app.processEvents()
        if done:
            break
        worker.wait(20)
    worker.wait(2000)
    app.processEvents()

    fails = 0

    def check(name, cond, extra=""):
        nonlocal fails
        if cond:
            print(f"  ok   {name}")
        else:
            fails += 1
            print(f"  FAIL {name} {extra}")

    check("worker finished", bool(done))
    check("sweep phase emitted", "sweep" in phases, str(phases))
    check("enrich phase emitted", "enrich" in phases, str(phases))

    # the interesting key really was enriched, and the enriched payload replaced
    # the thin one on the record
    v = results.get("valid-key", {})
    check("valid key enriched with a balance", v.get("remaining") == 42.5, f"got {v.get('remaining')}")
    check("valid key enriched with models", v.get("models") == ["a", "b"], f"got {v.get('models')}")
    check("valid key summary is not bare", "42.50" in (v.get("balance_summary") or ""),
          f"got {v.get('balance_summary')!r}")

    # dead keys must NOT be re-requested in the deep pass
    deep_keys = {k for k, _p, light in CALLS if not light}
    check("only survivors enriched", deep_keys == {"valid-key"}, str(deep_keys))
    check("throttled key not enriched", "throttled-key" not in deep_keys)
    check("no-funds key not enriched", "broke-key" not in deep_keys)

    # states survive as statuses instead of collapsing into "invalid"
    check("rate_limited preserved", results["throttled-key"].get("state") == "rate_limited")
    check("valid_no_funds preserved", results["broke-key"].get("state") == "valid_no_funds")
    check("unknown preserved", results["flaky-key"].get("state") == "unknown")

    print(f"\nCALLS: {CALLS}")
    print(f"{fails} failures")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
