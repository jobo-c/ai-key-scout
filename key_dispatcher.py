#!/usr/bin/env python3
"""
key_dispatcher.py — multi-key, multi-provider chat dispatcher for AI Key Scout v4.

Turns a scout `working_keys.json` (or any list of keys) into a single client that:

  * pools every valid key per provider,
  * rotates keys (round-robin) so you multiply rate limits,
  * fails over to the next key / next provider on 401 / 429 / network errors,
  * speaks OpenAI-style chat messages (not just "hi"),
  * and (--serve) exposes an OpenAI-compatible HTTP proxy so anything that
    talks to OpenAI can transparently use all your keys at once.

It reuses the scout's own endpoint + request definitions (app/providers.py,
app/modeltest.py) so it never drifts from what the scout validated.

Usage:
    python key_dispatcher.py "hello there"                 # auto-route, print reply
    python key_dispatcher.py "hi" --provider groq          # force a provider
    python key_dispatcher.py "hi" --provider openrouter --model openai/gpt-4o-mini
    python key_dispatcher.py --list                         # show the key pool
    python key_dispatcher.py --serve --port 8080           # OpenAI-compatible proxy
    python key_dispatcher.py --keys sk-... gsk_... nvapi-...  # ad-hoc keys (auto-detect)

Library:
    from key_dispatcher import Dispatcher
    d = Dispatcher.from_scout("working_keys.json")
    print(d.chat([{"role":"user","content":"hi"}])["text"])
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# Run from the v4 folder so `app` resolves.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import aiohttp
from aiohttp import web

from app import providers as pv
from app import modeltest as mt

COOLDOWN_SEC = 30  # how long a 429/quota key is parked before retrying


# --------------------------------------------------------------------------- #
# Key pool
# --------------------------------------------------------------------------- #
class Dispatcher:
    def __init__(self) -> None:
        # provider -> {"keys": [...], "by_key": {key: [proven models]}, "idx": int}
        self.pools: Dict[str, Dict[str, Any]] = {}
        self.cooldown: Dict[Tuple[str, str], float] = {}

    # ---- construction -------------------------------------------------- #
    @classmethod
    def from_scout(cls, path: str) -> "Dispatcher":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        d = cls()
        d.ingest_records(data.get("records", []))
        return d

    @classmethod
    def from_keys(cls, keys: List[str]) -> "Dispatcher":
        d = cls()
        for k in keys:
            k = k.strip().strip("\"'`,;")
            if not k:
                continue
            pid = pv.detect_provider_for_key(k)
            if not pid:
                # try discovery order as a last resort
                for cand in pv.discovery_order():
                    pass  # providers with no pattern can't be auto-detected here
            if pid:
                d._add_key(pid, k)
        return d

    def ingest_records(self, records: List[Dict[str, Any]]) -> None:
        for r in records:
            pid = r.get("provider")
            key = r.get("key")
            model = r.get("model")
            if not pid or not key:
                continue
            self._add_key(pid, key)
            # a record with a real reply proves (key, model) works
            if r.get("reply"):
                self.pools[pid]["by_key"].setdefault(key, [])
                if model and model not in self.pools[pid]["by_key"][key]:
                    self.pools[pid]["by_key"][key].append(model)

    def _add_key(self, pid: str, key: str) -> None:
        if pid not in self.pools:
            self.pools[pid] = {"keys": [], "by_key": {}, "idx": 0}
        if key not in self.pools[pid]["keys"]:
            self.pools[pid]["keys"].append(key)

    # ---- introspection ------------------------------------------------ #
    def providers(self) -> List[str]:
        return list(self.pools.keys())

    def summary(self) -> str:
        lines = []
        for pid in sorted(self.pools, key=lambda p: -len(self.pools[p]["keys"])):
            n = len(self.pools[pid]["keys"])
            models = set()
            for ms in self.pools[pid]["by_key"].values():
                models.update(ms)
            lines.append(f"  {pid:<14} {n:>3} keys   {len(models)} proven models")
        return "\n".join(lines)

    # ---- model selection --------------------------------------------- #
    def _models_for(self, pid: str, key: str) -> List[str]:
        out: List[str] = []
        proven = self.pools[pid]["by_key"].get(key, [])
        out.extend(proven)
        chat = (pv.PROVIDERS.get(pid) or {}).get("chat") or {}
        for m in chat.get("models") or []:
            if m not in out:
                out.append(m)
        # de-dupe keeping order, cap to keep failover fast
        seen = set()
        uniq = [m for m in out if not (m in seen or seen.add(m))]
        return uniq[:6]

    def _eligible_keys(self, pid: str) -> List[str]:
        now = time.time()
        keys = self.pools[pid]["keys"]
        return [k for k in keys if self.cooldown.get((pid, k), 0) <= now]

    def _next_key(self, pid: str) -> Optional[str]:
        keys = self._eligible_keys(pid)
        if not keys:
            return None
        p = self.pools[pid]
        # rotate from stored idx but only over eligible keys
        start = p["idx"] % len(keys)
        k = keys[start]
        p["idx"] = (start + 1) % len(keys)
        return k

    def _route_order(self) -> List[str]:
        # biggest pools first — best chance of an immediate hit
        return sorted(self.pools, key=lambda p: -len(self.pools[p]["keys"]))

    # ---- request building (parameterised, mirrors modeltest) ---------- #
    @staticmethod
    def _payload(style: str, model: str, messages: List[Dict[str, str]],
                 max_tokens: int, temperature: float) -> Dict[str, Any]:
        if style in ("openai", "cohere"):
            return {"model": model, "messages": messages,
                    "max_tokens": max_tokens, "temperature": temperature, "stream": False}
        if style == "anthropic":
            return {"model": model, "max_tokens": max_tokens,
                    "messages": messages, "temperature": temperature}
        if style == "google":
            contents = []
            system = None
            for m in messages:
                role = m.get("role")
                text = m.get("content", "")
                if role == "system":
                    system = text
                    continue
                grole = "model" if role == "assistant" else "user"
                contents.append({"role": grole, "parts": [{"text": text}]})
            body: Dict[str, Any] = {
                "contents": contents,
                "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
            }
            if system:
                body["systemInstruction"] = {"parts": [{"text": system}]}
            return body
        return {}

    # ---- core async call --------------------------------------------- #
    async def _acall(self, session: aiohttp.ClientSession, pid: str, key: str,
                     model: str, messages: List[Dict[str, str]],
                     max_tokens: int, temperature: float, timeout: float) -> Dict[str, Any]:
        cfg = pv.PROVIDERS.get(pid)
        if not cfg:
            return {"ok": False, "error": f"unknown provider {pid}"}
        chat = cfg.get("chat") or {}
        style = chat.get("style")
        url = chat.get("url")
        if not style or not url:
            return {"ok": False, "error": f"{pid} has no chat endpoint"}
        if style == "google":
            url = url.format(model=model, key=key)
        payload = self._payload(style, model, messages, max_tokens, temperature)
        headers = dict(chat.get("headers_fn", cfg["headers"])(key))
        if style in ("openai", "cohere", "google") and "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"
        started = time.time()
        try:
            async with session.post(url, json=payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=timeout, sock_connect=6)) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = await resp.text()
        except Exception as e:
            return {"ok": False, "status": None,
                    "error": f"network error: {str(e)[:120]}",
                    "latency_ms": int((time.time() - started) * 1000)}
        latency = int((time.time() - started) * 1000)
        reply = mt.extract_reply(body, style) if status == 200 else ""
        if status == 200 and reply:
            return {"ok": True, "status": status, "provider": pid, "key": key,
                    "model": model, "text": reply, "latency_ms": latency}
        if status == 200 and mt.served_any_choice(body):
            return {"ok": True, "status": status, "provider": pid, "key": key,
                    "model": model, "text": "", "latency_ms": latency,
                    "note": "ran but returned no visible text"}
        # failure — record error, cooldown on rate limit
        err = ""
        if isinstance(body, dict):
            e = body.get("error")
            if isinstance(e, dict):
                err = str(e.get("message") or e.get("type") or "")
            err = err or str(body.get("detail") or body.get("message") or "")
        elif isinstance(body, str):
            err = body
        if status in (429,) or "rate" in err.lower() or "quota" in err.lower():
            self.cooldown[(pid, key)] = time.time() + COOLDOWN_SEC
        return {"ok": False, "status": status,
                "error": f"HTTP {status} {err[:120]}".strip(),
                "provider": pid, "key": key, "model": model, "latency_ms": latency}

    # ---- public chat -------------------------------------------------- #
    def chat(self, messages: List[Dict[str, str]], provider: Optional[str] = None,
             model: Optional[str] = None, max_tokens: int = 512,
             temperature: float = 0.7, timeout: float = 20,
             max_keys: int = 40) -> Dict[str, Any]:
        return asyncio.run(self._achat(messages, provider, model, max_tokens,
                                       temperature, timeout, max_keys))

    async def _achat(self, messages, provider, model, max_tokens, temperature,
                     timeout, max_keys) -> Dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            providers_order = [provider] if provider else self._route_order()
            last_err: Dict[str, Any] = {"ok": False, "error": "no providers available"}
            for pid in providers_order:
                if pid not in self.pools:
                    continue
                tried = 0
                while tried < max_keys:
                    key = self._next_key(pid)
                    if key is None:
                        break
                    models = [model] if model else self._models_for(pid, key)
                    if not models:
                        last_err = {"ok": False, "error": f"{pid}: no models known"}
                        break
                    for m in models:
                        res = await self._acall(session, pid, key, m, messages,
                                                max_tokens, temperature, timeout)
                        if res.get("ok"):
                            return res
                        last_err = res
                    tried += 1
                # if a specific model was forced and it failed everywhere, stop
                if model:
                    break
            last_err.setdefault("ok", False)
            return last_err

    # ---- OpenAI-compatible local proxy -------------------------------- #
    async def _handle(self, request: aiohttp.web.Request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        messages = data.get("messages") or []
        model = data.get("model")
        # optional provider hint: "openrouter/foo" or header x-provider
        provider = request.headers.get("x-provider")
        if model and "/" in str(model) and not provider:
            maybe_pid = str(model).split("/", 1)[0]
            if maybe_pid in pv.PROVIDERS:
                provider = maybe_pid
        max_tokens = int(data.get("max_tokens", 512))
        temperature = float(data.get("temperature", 0.7))
        res = await self._achat(messages, provider, model, max_tokens,
                                temperature, 25, 60)
        if res.get("ok"):
            return aiohttp.web.json_response({
                "object": "chat.completion",
                "model": res.get("model"),
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": res.get("text")},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "_meta": {"provider": res.get("provider"), "key": res.get("key")[:12] + "…",
                          "latency_ms": res.get("latency_ms")},
            })
        return web.json_response(
            {"error": res.get("error", "all keys failed")}, status=502)

    def serve(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._handle)
        app.router.add_get("/health", lambda r: aiohttp.web.json_response({
            "providers": self.providers(),
            "keys": {p: len(self.pools[p]["keys"]) for p in self.pools},
        }))
        print(f"[dispatcher] OpenAI-compatible proxy on http://{host}:{port}/v1/chat/completions")
        print(self.summary())
        web.run_app(app, host=host, port=port, print=lambda *a, **k: None)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _main() -> None:
    ap = argparse.ArgumentParser(description="Multi-key / multi-provider LLM dispatcher")
    ap.add_argument("prompt", nargs="*", help="prompt text (ignored with --list/--serve)")
    ap.add_argument("--keys-file", default=os.path.join(_HERE, "working_keys.json"),
                    help="path to working_keys.json (default: alongside this script)")
    ap.add_argument("--keys", nargs="*", help="ad-hoc keys (auto-detect provider)")
    ap.add_argument("--provider", help="force a provider")
    ap.add_argument("--model", help="force a model")
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--timeout", type=float, default=20)
    ap.add_argument("--list", action="store_true", help="show the key pool and exit")
    ap.add_argument("--serve", action="store_true", help="run OpenAI-compatible proxy")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    if args.keys:
        d = Dispatcher.from_keys(args.keys)
    else:
        if not os.path.exists(args.keys_file):
            print(f"keys file not found: {args.keys_file}", file=sys.stderr)
            sys.exit(1)
        d = Dispatcher.from_scout(args.keys_file)

    if args.list or not (args.prompt or args.serve):
        print(f"Loaded {sum(len(d.pools[p]['keys']) for p in d.pools)} keys across "
              f"{len(d.pools)} providers:")
        print(d.summary())
        if not (args.prompt or args.serve):
            return

    if args.serve:
        d.serve(args.host, args.port)
        return

    prompt = " ".join(args.prompt)
    messages = [{"role": "user", "content": prompt}]
    res = d.chat(messages, provider=args.provider, model=args.model,
                 max_tokens=args.max_tokens, temperature=args.temperature,
                 timeout=args.timeout)
    if res.get("ok"):
        print(res.get("text"))
        print(f"\n-- provider={res.get('provider')} model={res.get('model')} "
              f"key={res.get('key')[:12]}… latency={res.get('latency_ms')}ms",
              file=sys.stderr)
    else:
        print(f"FAILED: {res.get('error')}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _main()
