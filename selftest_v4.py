#!/usr/bin/env python3
"""Headless self-test for AI Key Scout v4.

Two halves:
  * offline  — the parsing/mining/classification logic (no network)
  * live     — a handful of real calls with deliberately bogus keys, asserting
               the v3 false positives are gone and the new states work.

Run:  python3 selftest_v4.py          (offline only)
      python3 selftest_v4.py --live   (offline + live)
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from app.details import (  # noqa: E402
    STATE_INVALID,
    STATE_NO_FUNDS,
    STATE_RATE_LIMITED,
    STATE_UNKNOWN,
    STATE_VALID,
    _light_result,
    _looks_no_funds,
    _looks_rate_limited,
    _model_not_found,
    _pick_money,
    _walk_json,
    check_provider,
    mine_generic_details,
)
from app.providers import PROVIDERS, alternative_candidates, discovery_order  # noqa: E402
from app.ranking import is_best_candidate, score_record  # noqa: E402
from app.models_store import KeyRecord  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def offline() -> None:
    print("== offline ==")

    # 1. money mining finds balance fields the v3 hardcoded tuples missed
    body = {"data": {"account": {"credit_balance": "12.75", "plan": "pro"},
                     "usage": 3.25}}
    details, rem, info = mine_generic_details(body)
    check("mine: nested credit_balance found", rem == 12.75, f"got {rem}")
    check("mine: plan captured", details.get("data.account.plan") == "pro")

    # 2. 'used'/'usage' must NOT be mistaken for remaining credit
    d2, rem2, _ = mine_generic_details({"total_usage": 500, "data": {"usage": 90}})
    check("mine: usage is not treated as remaining", rem2 is None, f"got {rem2}")

    # 3. coin-style flat payload
    _, rem3, _ = mine_generic_details({"balance": "0.5", "currency": "USD"})
    check("mine: flat balance read", rem3 == 0.5, f"got {rem3}")

    # 4. pick_money prefers 'remaining' over generic 'credit'
    flat = _walk_json({"credit": 1, "remaining": 9})
    check("pick_money prefers 'remaining'", _pick_money(flat) == 9)

    # 5. model-level failures classify as "key is alive", not invalid
    check("model_not_found (anthropic style)",
          _model_not_found({"error": {"type": "not_found_error",
                                      "message": "model: claude-x not found"}}))
    check("model_not_found (openai style)",
          _model_not_found({"error": {"message": "The model `foo` does not exist"}}))
    check("model_not_found false for bad key",
          not _model_not_found({"error": {"type": "authentication_error",
                                          "message": "API key is invalid."}}))

    # 6. rate-limit detection must not fire on an unrelated "429" substring
    check("rate limit: message text", _looks_rate_limited({"error": {"message": "Too many requests"}}))
    check("rate limit: structured 429", _looks_rate_limited({"error": {"code": 429}}))
    check("rate limit: no false 429 substring",
          not _looks_rate_limited({"id": "req_429abc", "ok": True}))

    # 7. no-funds detection
    check("no_funds: insufficient_quota",
          _looks_no_funds({"error": {"code": "insufficient_quota",
                                     "message": "You exceeded your current quota"}}))
    check("no_funds false for auth error",
          not _looks_no_funds({"error": {"message": "API key is invalid"}}))

    # 8. light mode must never return a bare "Valid"
    for pid in ("groq", "nvidia", "jina", "elevenlabs", "replicate", "langsmith"):
        cfg = PROVIDERS[pid]
        out = _light_result(cfg, pid, cfg.get("detail") or "generic",
                            {"data": [{"id": f"m{i}"} for i in range(7)]})
        check(f"light[{pid}] not bare 'Valid'", out["balance_summary"].strip().lower() not in ("valid", ""),
              f"got {out['balance_summary']!r}")
        check(f"light[{pid}] carries model count", out["details"].get("model_count") == 7,
              f"got {out['details'].get('model_count')}")

    # 9. discovery list covers the previously unreachable vendors
    disc = discovery_order()
    for must in ("cohere", "deepinfra", "sambanova"):
        check(f"discovery includes {must}", must in disc)

    # 10. sk-32 keys get re-home candidates; unmistakable prefixes never do
    check("sk-32 has alternatives", len(alternative_candidates("sk-" + "a" * 32, ["deepseek"])) > 0)
    check("sk-or-v1 has no alternatives", alternative_candidates("sk-or-v1-" + "a" * 40) == [])
    check("AIza has no alternatives", alternative_candidates("AIza" + "a" * 35) == [])

    # 11. providers that lied about auth in v3 now declare a probe
    for pid in ("nvidia", "jina", "sambanova", "deepinfra"):
        check(f"{pid} declares an auth probe", bool(PROVIDERS[pid].get("probe")))
    check("anthropic validates with count_tokens",
          PROVIDERS["anthropic"]["validate"]["url"].endswith("/count_tokens"))
    check("google probe model is not the retired 1.5-flash",
          "1.5" not in PROVIDERS["google"]["probe_models"][0])

    # 12. ranking: no-funds and retryable states sort below a live key
    live = KeyRecord(key="k", provider="openai", status="valid", remaining=5.0,
                     details={"state": "valid"})
    broke = KeyRecord(key="k2", provider="openai", status="valid", remaining=None,
                      details={"state": "valid_no_funds"})
    throttled = KeyRecord(key="k3", provider="openai", status="rate_limited")
    check("score: live > broke", score_record(live) > score_record(broke))
    check("score: broke > throttled", score_record(broke) > score_record(throttled))
    check("broke key excluded from best", not is_best_candidate(broke))


async def live() -> None:
    import aiohttp

    print("== live (bogus keys against real endpoints) ==")
    bogus = "totally-invalid-key-1234567890abcdef"
    async with aiohttp.ClientSession() as session:
        # v3 reported these as "Valid" because their /models endpoint is public.
        for pid in ("nvidia", "jina", "deepinfra", "sambanova", "ai21"):
            res = await check_provider(session, bogus, pid, timeout=15, light=True)
            check(f"live[{pid}] garbage key rejected", res["state"] == STATE_INVALID,
                  f"got state={res['state']} err={res.get('error')!r}")

        # langsmith /info is public; the key-gated /sessions must reject garbage
        res = await check_provider(session, bogus, "langsmith", timeout=15, light=True)
        check("live[langsmith] garbage key rejected", res["state"] == STATE_INVALID,
              f"got state={res['state']} err={res.get('error')!r}")

        # google: a rejected key returns 400 INVALID_ARGUMENT — the rescue probe
        # must NOT read that as "model unavailable, so the key is fine"
        res = await check_provider(session, "AIza" + "x" * 35, "google", timeout=15, light=True)
        check("live[google] garbage key rejected", res["state"] == STATE_INVALID,
              f"got state={res['state']} summary={res.get('balance_summary')!r}")

        # real vendor, unmistakable prefix -> still rejected
        res = await check_provider(session, "sk-or-v1-" + "b" * 40, "openrouter", timeout=12)
        check("live[openrouter] garbage key rejected", res["state"] == STATE_INVALID,
              f"got {res['state']}")

        # unknown shape -> discovery runs and reports inconclusive rather than crash
        res = await check_provider(session, "zzz_" + "q" * 40, "unknown", timeout=8)
        check("live[unknown] discovery returns a state",
              res["state"] in (STATE_INVALID, STATE_UNKNOWN, STATE_NO_FUNDS),
              f"got {res['state']}")

        # no-funds classification path (OpenAI legacy key shape, bogus -> 401)
        res = await check_provider(session, "sk-proj-" + "c" * 40, "openai", timeout=12)
        check("live[openai] garbage key rejected", res["state"] == STATE_INVALID,
              f"got {res['state']}")


def main() -> int:
    offline()
    if "--live" in sys.argv:
        asyncio.run(live())
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
