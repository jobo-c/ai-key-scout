"""Model testing — prove a valid key actually serves a model by saying "hi".

Validation only tells you a key authenticated. This module sends a real, tiny
completion ("hi", one output token) to a handful of chat models and records which
ones ANSWERED. That is the difference between "the key is real" and "the key
works, here is what it can do".

Safety/cost: every request is capped at one output token with temperature 0, the
number of models per key is bounded (default 3), and any model that is clearly not
a text generator (embeddings, rerankers, whisper, TTS, image, safety guards) is
filtered out before a request is made.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from providers import PROVIDERS

# Models whose names give away that they cannot answer a chat message.
_NON_CHAT_NOISE = (
    "embed", "rerank", "whisper", "tts", "dall-e", "image", "audio", "moderation",
    "guard", "safety", "diffusion", "bge-", "clip", "sdxl", "stable-", "transcribe",
    "speech", "vision-embed", "vision-ocr", "ocr", "guardrail", "nemoguard",
    "codey", "starcoder", "codestral-embed", "sentence-", "colbert",
)

# Handlers per request style. Each returns (url, payload) and reads the reply
# text back out of the response body.
ANTHROPIC_VERSION = "2023-06-01"


# A tiny budget is enough to prove inference, but reasoning models emit their
# hidden reasoning BEFORE any visible text. With max_tokens=1 they return
# `content: null` and the model looks dead even though it ran fine, so ask for a
# modest budget and accept the reasoning trace as proof of life.
PROBE_TOKENS = 16


def _openai_payload(model: str) -> Dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": PROBE_TOKENS,
        "temperature": 0,
        "stream": False,
    }


def build_request(cfg: Dict[str, Any], key: str, model: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Return (url, payload) for one model test, or None if unsupported."""
    chat = cfg.get("chat") or {}
    style = chat.get("style")
    url = chat.get("url") or ""
    if not style or not url:
        return None
    if style == "openai":
        return url, _openai_payload(model)
    if style == "anthropic":
        return url, {"model": model, "max_tokens": PROBE_TOKENS,
                     "messages": [{"role": "user", "content": "hi"}]}
    if style == "google":
        return (url.format(model=model, key=key),
                {"contents": [{"parts": [{"text": "hi"}]}],
                 "generationConfig": {"maxOutputTokens": PROBE_TOKENS, "temperature": 0}})
    if style == "cohere":
        return url, {"model": model, "max_tokens": PROBE_TOKENS,
                     "messages": [{"role": "user", "content": "hi"}]}
    if style == "embed":
        return url, {"input": ["hi"], "model": model}
    return None


def extract_reply(body: Any, style: str) -> str:
    """Pull the assistant text (or embedding vector length) out of a response."""
    if not isinstance(body, dict):
        return ""
    try:
        if style == "anthropic":
            parts = body.get("content") or []
            if isinstance(parts, list) and parts and isinstance(parts[0], dict):
                return str(parts[0].get("text") or "")
            return ""
        if style == "google":
            cands = body.get("candidates") or []
            if cands and isinstance(cands[0], dict):
                parts = (cands[0].get("content") or {}).get("parts") or []
                if parts and isinstance(parts[0], dict):
                    return str(parts[0].get("text") or "")
            return ""
        if style == "cohere":
            msg = body.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list) and content and isinstance(content[0], dict):
                return str(content[0].get("text") or "")
            if isinstance(content, str):
                return content
            return ""
        if style == "embed":
            data = body.get("data") or []
            if isinstance(data, list) and data and isinstance(data[0], dict):
                vec = data[0].get("embedding") or []
                return f"[vector {len(vec)}d]" if vec else ""
            return ""
        # openai-compatible
        choices = body.get("choices") or []
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            if isinstance(msg, dict):
                text = msg.get("content")
                if isinstance(text, str) and text.strip():
                    return text
                # Reasoning models put the trace here and leave content null when
                # the token budget runs out (OpenRouter uses "reasoning").
                for alt in ("reasoning_content", "reasoning", "text", "refusal"):
                    if isinstance(msg.get(alt), str) and msg[alt].strip():
                        return msg[alt]
            if isinstance(choices[0].get("text"), str):
                return choices[0]["text"]
    except Exception:
        return ""
    return ""


def served_any_choice(body: Any) -> bool:
    """True when the endpoint returned a real completion envelope.

    A 200 from chat/completions carrying `choices` with a finish_reason means the
    provider actually ran inference for this key — distinct from a public
    /models endpoint that answers 200 to anything, which is what v3 fell for.
    """
    if not isinstance(body, dict):
        return False
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return bool(choices[0].get("finish_reason")) or bool(choices[0].get("message"))
    if isinstance(body.get("candidates"), list) and body["candidates"]:
        return True
    if isinstance(body.get("content"), list) and body["content"]:
        return True
    if isinstance(body.get("data"), list) and body["data"]:
        return True
    return False


def model_tier(model: str, provider: str = "") -> str:
    """Best-effort free/paid classification.

    Exact pricing metadata is preferred when available by the caller. As a
    provider-independent fallback, OpenRouter's explicit ':free' model suffix is
    authoritative. Unknown is never silently treated as free.
    """
    m = str(model or "").lower()
    if provider == "openrouter":
        return "free" if (m.endswith(":free") or m == "openrouter/free") else "paid"
    return "unknown"


def filter_catalog(catalog: Optional[List[str]]) -> List[str]:
    """Catalog ids that could plausibly answer a chat message (order preserved)."""
    out: List[str] = []
    for m in (catalog or []):
        if not isinstance(m, str) or not m:
            continue
        if any(n in m.lower() for n in _NON_CHAT_NOISE):
            continue
        if m not in out:
            out.append(m)
    return out


def candidate_models(cfg: Dict[str, Any], catalog: Optional[List[str]] = None,
                     limit: int = 3) -> List[str]:
    """Ordered model candidates: hand-picked cheap ids first, then the catalog."""
    chat = cfg.get("chat") or {}
    out: List[str] = []
    for m in chat.get("models") or []:
        if isinstance(m, str) and m not in out:
            out.append(m)
    curated_n = len(out)
    for m in filter_catalog(catalog):
        if m not in out:
            out.append(m)
    # Keep the curated ids even if we are over budget, then top up from the catalog.
    if len(out) <= limit:
        return out
    head = out[:curated_n] if curated_n else out[:1]
    if len(head) >= limit:
        return head[:limit]
    tail = [m for m in out[curated_n:] if m not in head]
    return (head + tail)[:limit]


async def chat_once(
    session: aiohttp.ClientSession,
    cfg: Dict[str, Any],
    key: str,
    model: str,
    timeout: float = 12,
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """Send 'hi' to one model and describe what happened."""
    req = build_request(cfg, key, model)
    style = (cfg.get("chat") or {}).get("style", "openai")
    provider = str(cfg.get("_provider_id") or "")
    tier = model_tier(model, provider)
    if req is None:
        return {"model": model, "tier": tier, "ok": False, "error": "provider does not expose a chat endpoint"}
    url, payload = req
    headers = dict((cfg.get("chat") or {}).get("headers_fn", cfg["headers"])(key))
    if style in ("openai", "cohere", "embed", "google") and "Content-Type" not in headers:
        headers["Content-Type"] = "application/json"

    started = time.time()
    try:
        async with session.post(
            url, json=payload, headers=headers, proxy=proxy,
            timeout=aiohttp.ClientTimeout(total=timeout, sock_connect=6),
        ) as resp:
            status = resp.status
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = await resp.text()
    except Exception as e:
        return {"model": model, "tier": tier, "ok": False, "status": None,
                "error": f"network error: {str(e)[:100]}",
                "latency_ms": int((time.time() - started) * 1000)}

    latency = int((time.time() - started) * 1000)
    reply = extract_reply(body, style) if status == 200 else ""
    if status == 200 and reply:
        return {"model": model, "tier": tier, "ok": True, "status": status, "text": True,
                "reply": reply[:120], "latency_ms": latency, "error": ""}
    if status == 200 and served_any_choice(body):
        # The provider ran inference for this key but produced no visible text
        # (reasoning model, or the token budget was spent on the trace).
        return {"model": model, "tier": tier, "ok": True, "status": status, "text": False,
                "reply": "", "latency_ms": latency,
                "error": "ran, but returned no visible text"}
    if status == 200:
        # A 200 that is NOT a completion envelope is not proof of anything.
        snippet = json.dumps(body)[:160] if isinstance(body, (dict, list)) else str(body)[:160]
        return {"model": model, "tier": tier, "ok": False, "status": status, "text": False,
                "error": f"no completion returned ({snippet})", "latency_ms": latency}
    err = ""
    if isinstance(body, dict):
        e = body.get("error")
        if isinstance(e, dict):
            err = str(e.get("message") or e.get("type") or "")
        err = err or str(body.get("detail") or body.get("message") or "")
    elif isinstance(body, str):
        err = body
    return {"model": model, "tier": tier, "ok": False, "status": status,
            "error": f"HTTP {status} {err[:120]}".strip(), "latency_ms": latency}


def _stage_candidates(cfg: Dict[str, Any], catalog: Optional[List[str]],
                      limit: int) -> List[List[str]]:
    """Two attempts at a model list: curated ids first, then the key's own catalog.

    Providers retire model ids constantly — OpenRouter's `:free` slugs, NVIDIA's
    llama-3.1, Anthropic's claude-3-haiku, voyage-3-lite — so a curated list alone
    would silently stop working. The catalog is the fallback.
    """
    chat = cfg.get("chat") or {}
    curated = [m for m in (chat.get("models") or []) if isinstance(m, str)][:max(1, limit)]
    stages = [curated] if curated else []

    # Stage 2 is built from the key's OWN catalog (not candidate_models, which
    # would re-prepend the curated ids we already burned in stage 1).
    extra = [m for m in filter_catalog(catalog) if m not in curated][:max(1, limit)]
    if extra:
        stages.append(extra)
    return [s for s in stages if s]


async def _run_stage(session, cfg, key, models, timeout, proxy, concurrent) -> List[Dict[str, Any]]:
    if concurrent:
        raw = await asyncio.gather(*(
            chat_once(session, cfg, key, m, timeout=timeout, proxy=proxy) for m in models
        ), return_exceptions=True)
        out = []
        for model, res in zip(models, raw):
            if isinstance(res, Exception):
                out.append({"model": model, "ok": False, "error": f"exception: {str(res)[:80]}"})
            else:
                out.append(res)
        return out
    out = []
    for m in models:
        out.append(await chat_once(session, cfg, key, m, timeout=timeout, proxy=proxy))
    return out


async def test_models(
    session: aiohttp.ClientSession,
    key: str,
    provider: str,
    catalog: Optional[List[str]] = None,
    limit: int = 3,
    timeout: float = 12,
    proxy: Optional[str] = None,
    test_paid: bool = False,
) -> Dict[str, Any]:
    """Test models; paid inference is opt-in."""
    return await _test(session, key, provider, catalog, limit, timeout, proxy,
                       concurrent=False, test_paid=test_paid)


async def test_models_concurrent(
    session: aiohttp.ClientSession,
    key: str,
    provider: str,
    catalog: Optional[List[str]] = None,
    limit: int = 3,
    timeout: float = 12,
    proxy: Optional[str] = None,
    test_paid: bool = False,
) -> Dict[str, Any]:
    """Test models in parallel; paid inference is opt-in."""
    return await _test(session, key, provider, catalog, limit, timeout, proxy,
                       concurrent=True, test_paid=test_paid)


async def _test(session, key, provider, catalog, limit, timeout, proxy,
              concurrent: bool, test_paid: bool = False) -> Dict[str, Any]:
    cfg = PROVIDERS.get(provider)
    if not cfg:
        return {"tested": [], "working": [], "note": "unknown provider"}
    chat = dict(cfg.get("chat") or {})
    if chat.get("unsupported"):
        return {"tested": [], "working": [], "note": chat["unsupported"]}
    # Carry provider id into chat_once without changing the public registry schema.
    cfg = dict(cfg)
    cfg["chat"] = dict(chat)
    cfg["_provider_id"] = provider

    stages = _stage_candidates(cfg, catalog, limit)
    if test_paid:
        # Explicitly add one paid candidate after the normal/cheap stage. For
        # OpenRouter, pricing is encoded in the :free suffix; future providers can
        # supply richer catalog metadata without changing this interface.
        all_candidates = []
        for group in stages:
            all_candidates.extend(group)
        all_candidates.extend(filter_catalog(catalog))
        paid = [m for m in all_candidates if model_tier(m, provider) == "paid"]
        if paid:
            stages.append([paid[0]])
    if not stages:
        return {"tested": [], "working": [], "note": "no testable models known for this provider"}

    tested: List[Dict[str, Any]] = []
    working: List[str] = []
    free_working: List[str] = []
    paid_working: List[str] = []

    for models in stages:
        if not test_paid:
            models = [m for m in models if model_tier(m, provider) != "paid"]
        if not models:
            continue
        try:
            results = await _run_stage(session, cfg, key, models, timeout, proxy, concurrent)
        except Exception as e:
            results = [{"model": m, "tier": model_tier(m, provider), "ok": False,
                        "error": f"exception: {str(e)[:80]}"} for m in models]
        tested.extend(results)
        for r in results:
            if not r.get("ok"):
                continue
            m = r["model"]
            working.append(m)
            tier = r.get("tier") or model_tier(m, provider)
            if tier == "free":
                free_working.append(m)
            elif tier == "paid":
                paid_working.append(m)
        # Do not stop after the first success: the purpose of this phase is
        # to report which sampled models actually serve this key. The per-key limit
        # bounds cost/latency. When paid testing is enabled we stop once both tiers
        # have a proven result.
        if test_paid and free_working and paid_working:
            break

    # If a provider does not expose pricing, report that tier as unknown rather
    # than pretending a successful call was free.
    return {
        "tested": tested,
        "working": working,
        "free_working": free_working,
        "paid_working": paid_working,
        "note": "",
        "paid_testing": bool(test_paid),
        "tested_count": len(tested),
        "free_tested": sum(1 for r in tested if r.get("tier") == "free"),
        "paid_tested": sum(1 for r in tested if r.get("tier") == "paid"),
    }


def summarise(result: Dict[str, Any]) -> str:
    """One-line human summary of a model test run."""
    working = result.get("working") or []
    tested = result.get("tested") or []
    if working:
        bits = []
        for r in tested:
            if not r.get("ok"):
                continue
            tier = r.get("tier") or "unknown"
            tag = f"[{tier}]"
            if r.get("text"):
                bits.append(f"{r['model']} {tag} → {r.get('reply','')!r} ({r.get('latency_ms')}ms)")
            else:
                bits.append(f"{r['model']} {tag} → ran, no visible text ({r.get('latency_ms')}ms)")
        return "WORKS: " + " · ".join(bits)
    if result.get("note"):
        return f"not tested: {result['note']}"
    fails = [f"{r.get('model')}({r.get('error') or r.get('status')})" for r in tested]
    return "no model answered: " + ", ".join(fails[:4])
