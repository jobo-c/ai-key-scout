#!/usr/bin/env python3
"""Tests for the "say hi" model-testing feature.

Offline: request building, reply extraction, candidate filtering, summaries.
Live:    a bogus key must produce NO working models — the whole point of the
         feature is that it cannot be faked (unlike a public /models endpoint).

Run:  python3 tools/test_modeltest.py
      python3 tools/test_modeltest.py --live
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modeltest import (  # noqa: E402
    PROBE_TOKENS,
    _stage_candidates,
    build_request,
    candidate_models,
    chat_once,
    extract_reply,
    served_any_choice,
    summarise,
    test_models_concurrent,
)
from app.providers import CHAT_SPECS, PROVIDERS  # noqa: E402

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

    # 1. request building for every style we support
    url, body = build_request(PROVIDERS["groq"], "k", "llama-3.1-8b-instant")
    check("openai style posts to chat/completions", url.endswith("/chat/completions"))
    check("openai style sends 'hi'",
          body["messages"] == [{"role": "user", "content": "hi"}], str(body))
    check("openai style keeps the output budget tiny",
          1 <= body["max_tokens"] <= 32, str(body["max_tokens"]))

    url, body = build_request(PROVIDERS["anthropic"], "k", "claude-3-5-haiku-latest")
    check("anthropic style has a tiny max_tokens", 1 <= body["max_tokens"] <= 32)
    check("anthropic style has no openai 'stream'", "stream" not in body)

    url, body = build_request(PROVIDERS["google"], "KEY123", "gemini-2.5-flash")
    check("google style substitutes model AND key",
          "gemini-2.5-flash" in url and "KEY123" in url, url)
    check("google style keeps the output budget tiny",
          1 <= body["generationConfig"]["maxOutputTokens"] <= 32)

    url, body = build_request(PROVIDERS["voyage"], "k", "voyage-3.5-lite")
    check("embed style sends input list", body["input"] == ["hi"])

    check("unsupported provider builds no request", build_request(PROVIDERS["elevenlabs"], "k", "x") is None)

    # 2. reply extraction per style
    check("openai reply", extract_reply(
        {"choices": [{"message": {"content": "Hello!"}}]}, "openai") == "Hello!")
    check("anthropic reply", extract_reply(
        {"content": [{"type": "text", "text": "Hi"}]}, "anthropic") == "Hi")
    check("google reply", extract_reply(
        {"candidates": [{"content": {"parts": [{"text": "Hi"}]}}]}, "google") == "Hi")
    check("cohere reply", extract_reply(
        {"message": {"content": [{"text": "Hi"}]}}, "cohere") == "Hi")
    check("embed reply reports vector size", extract_reply(
        {"data": [{"embedding": [0.1] * 1024}]}, "embed") == "[vector 1024d]")
    # reasoning models: content is null, the trace lives in "reasoning"
    check("reasoning trace counts as a reply", extract_reply(
        {"choices": [{"message": {"content": None, "reasoning": "The"}}]}, "openai") == "The")
    check("empty openai reply is empty", extract_reply(
        {"choices": [{"message": {"content": ""}}]}, "openai") == "")

    # served_any_choice separates a real completion from a bare 200
    check("served_any_choice accepts a completion envelope", served_any_choice(
        {"choices": [{"finish_reason": "length", "message": {"content": None}}]}))
    check("served_any_choice rejects an empty 200", not served_any_choice({"data": []}))
    check("served_any_choice rejects a non-dict", not served_any_choice("ok"))

    # two-stage candidates: curated first, then a catalog-derived fallback, so a
    # retired curated id cannot dead-end the feature
    stages = _stage_candidates(PROVIDERS["openrouter"], ["some/new-model", "another/model"], limit=2)
    check("stage 1 is the curated list", stages[0] == PROVIDERS["openrouter"]["chat"]["models"][:2],
          str(stages))
    check("stage 2 falls back to the catalog",
          len(stages) == 2 and "some/new-model" in stages[1], str(stages))
    check("no duplicate work between stages", not set(stages[0]) & set(stages[1]))
    check("garbage body does not crash", extract_reply("not a dict", "openai") == "")

    # 3. candidate filtering drops non-chat models
    cands = candidate_models(
        PROVIDERS["groq"],
        ["llama-3.1-8b-instant", "whisper-large-v3", "text-embedding-3-small",
         "bge-reranker-v2", "tts-1", "dall-e-3", "playai-tts"],
        limit=4)
    check("chat candidates exclude non-chat models",
          cands == ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"], str(cands))
    check("candidate list respects the limit",
          len(candidate_models(PROVIDERS["openai"], [f"m{i}" for i in range(50)], limit=2)) == 2)
    check("curated ids always come first",
          candidate_models(PROVIDERS["deepseek"], ["zzz-other"], limit=2)[0] == "deepseek-chat")

    # 4. every provider either has a chat spec, is explicitly unsupported, or is a
    #    non-model service — nothing should be silently missing
    missing = []
    for pid, cfg in PROVIDERS.items():
        chat = cfg.get("chat")
        if chat is None:
            missing.append(pid)
        elif not chat.get("unsupported") and not chat.get("url"):
            missing.append(f"{pid}(no url)")
    check("no provider silently lacks a model-test plan", not missing, str(missing))

    # 5. summaries are never empty
    check("summary for a working key mentions the reply",
          summarise({"tested": [{"model": "m", "ok": True, "reply": "!", "latency_ms": 9}],
                     "working": ["m"], "note": ""}).startswith("WORKS"))
    check("summary for an unsupported provider explains why",
          "not tested" in summarise({"tested": [], "working": [], "note": "image only"}))
    check("summary for failures lists them",
          "no model answered" in summarise(
              {"tested": [{"model": "m", "ok": False, "error": "HTTP 404"}],
               "working": [], "note": ""}))

    # 6. the chat table itself is sane
    for pid, spec in CHAT_SPECS.items():
        if spec.get("unsupported"):
            continue
        check(f"chat spec [{pid}] is complete",
              bool(spec.get("style")) and bool(spec.get("url")) and bool(spec.get("models")),
              str(spec))
    check("every chat spec points at a plausible chat/embed path",
          all(any(t in s["url"] for t in ("chat", "generateContent", "embeddings", "messages", "completions"))
              for s in CHAT_SPECS.values() if s.get("url")), "")


async def live() -> None:
    import aiohttp

    print("== live ==")
    async with aiohttp.ClientSession() as session:
        bogus = "totally-invalid-key-1234567890abcdef"
        # A public /models endpoint cannot fake this: nothing should answer.
        for pid in ("groq", "nvidia", "deepinfra", "sambanova", "ai21", "google"):
            res = await test_models_concurrent(session, bogus, pid, limit=2, timeout=15)
            check(f"live[{pid}] no model answers a bogus key",
                  res["working"] == [], f"got {res['working']}")
            check(f"live[{pid}] reports the failures",
                  bool(res["tested"]) and all(not t.get("ok") for t in res["tested"]),
                  str(res["tested"])[:120])

        # A rejected key must NOT be reported as a working reply
        r = await chat_once(session, PROVIDERS["groq"], bogus, "llama-3.1-8b-instant", timeout=15)
        check("live[groq] chat_once marks bogus as not ok", not r.get("ok"), str(r))


def main() -> int:
    offline()
    if "--live" in sys.argv:
        asyncio.run(live())
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
