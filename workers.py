"""Background workers — never run heavy work on the UI thread."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, List, Set

import aiohttp
from PyQt6.QtCore import QThread, pyqtSignal

from details import AdaptiveThrottle, check_provider
from modeltest import summarise, test_models_concurrent
from providers import alternative_candidates, detect_keys_in_line
from ranking import score_record
from models_store import KeyRecord

log = logging.getLogger("workers")

# Per-provider minimum spacing between requests (seconds). Tight by default;
# stricter providers get a larger floor so we don't hammer them into a 429.
PROVIDER_MIN_INTERVAL = {
    "openai": 0.25,
    "anthropic": 0.25,
    "google": 0.10,
    "openrouter": 0.10,
    "together": 0.10,
    "groq": 0.05,
    "deepseek": 0.05,
    "mistral": 0.05,
    # providers probed with real inference — the sweep costs 2 requests, so give
    # them a little more room to avoid self-inflicted 429s
    "nvidia": 0.15,
    "sambanova": 0.15,
    "deepinfra": 0.15,
    "jina": 0.15,
    "voyage": 0.15,
    "cohere": 0.15,
    "voyage_ai": 0.15,
    "nebius": 0.15,
    "hyperbolic": 0.15,
    "ai21": 0.15,
    "minimax": 0.15,
}

_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", "venv", ".venv",
    "site-packages", ".idea", ".vscode", "dist", "build",
}

# Literal prefixes only — bytes.find on a 1GB dump is ~0.7s; unicode re.finditer was ~42s/pattern.
_KEY_MARKERS = (
    b"sk-or-v1-",
    b"sk-proj-",
    b"sk-ant-",
    b"gsk_",
    b"AIza",
    b"hf_",
    b"r8_",
    b"pplx-",
    b"xai-",
    b"fw_",
    b"csk-",
    b"ghp_",
    b"github_pat_",
    b"nvapi-",
    b"jina_",
    b"tvly-",
    b"lsv2_pt_",
    b"ls__",
    b"sk_",
    b"pa-",
    b"fc-", b"bu_", b"bless_", b"apify_api_", b"exa_", b"brd_", b"pcsk_", b"fal_", b"ak-",
    b"sk-",  # DeepSeek / legacy OpenAI — last so distinctive prefixes win line-dedupe first
)

_MAX_HITS_PER_MARKER = 200_000
_MAX_LINE = 8192


def _find_all(data: bytes, needle: bytes, cap: int = _MAX_HITS_PER_MARKER) -> list:
    out = []
    start = 0
    n = len(needle)
    while True:
        i = data.find(needle, start)
        if i < 0:
            return out
        out.append(i)
        start = i + n
        if len(out) >= cap:
            return out


def scan_file_bytes(data: bytes) -> list:
    """v2 idea (read whole file, search it) using fast byte prefix search.

    Returns [(key, provider, context_line), ...].
    """
    if not data:
        return []
    positions = []
    for marker in _KEY_MARKERS:
        positions.extend(_find_all(data, marker))
    if not positions:
        return []
    seen_line_off = set()
    seen_keys = set()
    hits = []
    for i in positions:
        ls = data.rfind(b"\n", 0, i) + 1
        if ls in seen_line_off:
            continue
        seen_line_off.add(ls)
        le = data.find(b"\n", i)
        if le < 0:
            le = len(data)
        if le - ls > _MAX_LINE:
            continue
        line = data[ls:le].decode("utf-8", errors="ignore").strip()
        if not line:
            continue
        for key, pid, ctx in detect_keys_in_line(line):
            if key in seen_keys:
                continue
            seen_keys.add(key)
            hits.append((key, pid, (ctx or line)[:500]))
    return hits


class ScanWorker(QThread):
    """v2 scanner: os.walk → read whole file → search prefixes → extract keys.

    Same structure as v2/key_scanner_gui.py ScanWorker.run. Prefix search is
    done on raw bytes (fast on GB dumps); Python unicode regex on 1GB is not.
    """

    found_batch = pyqtSignal(list)  # [(key, provider, source, context)]
    progress = pyqtSignal(str)
    done = pyqtSignal(int, dict)

    def __init__(
        self,
        root: str,
        extensions: Set[str],
        max_mb: int = 0,
        scan_all: bool = False,
        batch_size: int = 100,
        workers: int = 0,
        use_powershell: bool = False,
    ):
        super().__init__()
        self.root = root
        self.extensions = {e if e.startswith(".") else f".{e}" for e in extensions} or {".txt"}
        self.max_bytes = (max_mb * 1024 * 1024) if max_mb else 0  # 0 = no size skip
        self.max_mb = max_mb
        self.scan_all = scan_all
        self.batch_size = max(20, batch_size)
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        seen: Set[str] = set()
        batch: List[tuple] = []
        raw_matches = 0
        files_walked = 0
        files_checked = 0
        files_skipped_ext = 0
        files_skipped_size = 0
        files_unreadable = 0
        dirs_skipped = 0

        def flush():
            nonlocal batch
            if batch:
                self.found_batch.emit(list(batch))
                batch = []

        log.info(
            "Scan start (v2) root=%s max_mb=%s scan_all=%s exts=%s",
            self.root, self.max_mb or "unlimited", self.scan_all, sorted(self.extensions),
        )
        self.progress.emit("Scanning (v2: read file + search)…")

        for dirpath, dirnames, filenames in os.walk(self.root):
            if self._stop:
                break
            removed = [d for d in dirnames if d in _SKIP_DIRS]
            for d in removed:
                dirnames.remove(d)
            dirs_skipped += len(removed)
            for fname in filenames:
                if self._stop:
                    break
                files_walked += 1
                if fname.lower() == "_keyscout_hits.txt":
                    continue
                ext = os.path.splitext(fname)[1].lower()
                if not self.scan_all and ext not in self.extensions:
                    files_skipped_ext += 1
                    continue
                full = os.path.join(dirpath, fname)
                try:
                    sz = os.path.getsize(full)
                    if self.max_bytes and sz > self.max_bytes:
                        files_skipped_size += 1
                        continue
                    with open(full, "rb") as f:
                        data = f.read()
                except Exception:
                    files_unreadable += 1
                    continue
                files_checked += 1
                mb = sz / (1024 * 1024)
                self.progress.emit(
                    f"[{files_checked}] {os.path.basename(full)} ({mb:.1f} MB) · keys {len(seen)}"
                )
                try:
                    hits = scan_file_bytes(data)
                except Exception as e:
                    files_unreadable += 1
                    log.warning("Scan failed %s: %s", full, e)
                    continue
                del data
                for key, pid, ctx in hits:
                    raw_matches += 1
                    if key in seen:
                        continue
                    seen.add(key)
                    batch.append((key, pid, full, ctx or ""))
                    if len(batch) >= self.batch_size:
                        flush()

        flush()
        diag = {
            "method": "v2_scan",
            "files_walked": files_walked,
            "files_checked": files_checked,
            "files_skipped_ext": files_skipped_ext,
            "files_skipped_size": files_skipped_size,
            "files_unreadable": files_unreadable,
            "dirs_skipped": dirs_skipped,
            "scan_all": self.scan_all,
            "max_mb": self.max_mb,
            "raw_matches": raw_matches,
            "unique": len(seen),
            "stopped": self._stop,
        }
        log.info("Scan finished unique=%s diag=%s", len(seen), diag)
        self.done.emit(len(seen), diag)


class ModelTestWorker(QThread):
    """Say "hi" to each valid key's models and record which ones answer.

    Separate from CheckWorker on purpose: validation is cheap and covers thousands
    of keys, while this actually consumes (a tiny amount of) quota and therefore
    should be opt-in / re-runnable on its own.
    """

    result = pyqtSignal(str, dict)     # key -> model test payload
    progress = pyqtSignal(int, int)
    status = pyqtSignal(str)
    finished_ok = pyqtSignal()

    def __init__(
        self,
        items: List[tuple],            # [(key, provider, catalog_list), ...]
        concurrency: int = 8,
        timeout: float = 15.0,
        proxy: str = "",
        models_per_key: int = 3,
        test_paid: bool = False,
    ):
        super().__init__()
        self.items = items
        self.concurrency = max(1, concurrency)
        self.timeout = timeout
        self.proxy = (proxy or "").strip() or None
        self.models_per_key = max(1, models_per_key)
        self.test_paid = bool(test_paid)
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        asyncio.run(self._run())

    async def _run(self):
        total = len(self.items)
        done = 0
        worked = 0
        sem = asyncio.Semaphore(self.concurrency)
        connector = aiohttp.TCPConnector(limit=self.concurrency, limit_per_host=8, ttl_dns_cache=300)
        timeout = aiohttp.ClientTimeout(total=self.timeout + 5, connect=8)

        async def one(key: str, provider: str, catalog: list):
            nonlocal done, worked
            if self._stop:
                return
            async with sem:
                if self._stop:
                    return
                try:
                    res = await test_models_concurrent(
                        session, key, provider, catalog=catalog,
                        limit=self.models_per_key, timeout=self.timeout, proxy=self.proxy,
                        test_paid=self.test_paid,
                    )
                except Exception as e:
                    res = {"tested": [], "working": [], "note": f"error: {str(e)[:100]}"}
                if res.get("working"):
                    worked += 1
                res["summary"] = summarise(res)
                res["provider"] = provider
                self.result.emit(key, res)
                done += 1
                self.progress.emit(done, total)
                if done % 10 == 0 or done == total:
                    self.status.emit(f"Model test… {done}/{total} (working={worked})")

        log.info("ModelTestWorker start items=%d concurrency=%d models_per_key=%d test_paid=%s",
                 total, self.concurrency, self.models_per_key, self.test_paid)
        async with aiohttp.ClientSession(connector=connector, trust_env=True, timeout=timeout) as session:
            chunk = max(self.concurrency * 4, 16)
            for i in range(0, len(self.items), chunk):
                if self._stop:
                    break
                part = self.items[i:i + chunk]
                await asyncio.gather(*(one(k, p, c) for k, p, c in part))
        log.info("ModelTestWorker finished done=%s worked=%s", done, worked)
        self.finished_ok.emit()


class CheckWorker(QThread):
    """Validate keys, then enrich the ones that came back valid.

    Two phases:
      1. cheap sweep — one request per key (plus a probe where the provider's
         models list is public and cannot prove auth);
      2. enrichment pass — re-run ONLY the valid keys with light=False so the
         full DETAIL_FETCHERS run (balances, quotas, catalogs, org info).
    v3 could do one or the other: light mode returned the word "Valid" for most
    providers, and deep mode over thousands of keys was too slow to use.
    """

    result = pyqtSignal(str, dict)
    progress = pyqtSignal(int, int)
    status = pyqtSignal(str)
    phase = pyqtSignal(str)
    finished_ok = pyqtSignal()

    def __init__(
        self,
        items: List[tuple],
        concurrency: int = 30,
        timeout: float = 10.0,
        proxy: str = "",
        light: bool = True,
        enrich_valid: bool = True,
    ):
        super().__init__()
        self.items = items
        self.concurrency = max(1, concurrency)
        self.timeout = timeout
        self.proxy = (proxy or "").strip() or None
        self.light = light
        self.enrich_valid = enrich_valid
        self._stop = False

    def stop(self):
        self._stop = True

    async def _check_with_retry(self, session, key, provider, th):
        """check_provider with bounded retries on TRANSIENT failures only.

        Retries network errors, rate limits (429 / 403-with-rate-body, honouring
        Retry-After) and 5xx. Permanent failures (401/403 invalid key, 404) are
        final. A state of `rate_limited` or `unknown` is NOT folded into
        "invalid" — that was v3's bug: a throttled key was recorded dead forever.
        """
        max_retry = 2
        last = None
        for attempt in range(max_retry + 1):
            try:
                payload = await check_provider(
                    session, key, provider,
                    timeout=self.timeout, proxy=self.proxy, light=self.light,
                )
            except Exception as e:  # never let one key abort the whole batch
                payload = {
                    "valid": False,
                    "state": "unknown",
                    "error": f"exception: {str(e)[:120]}",
                    "remaining": None, "balance_summary": "",
                    "details": {}, "models": [], "info": "",
                }
            last = payload
            state = payload.get("state") or ("valid" if payload.get("valid") else "invalid")
            if state in ("valid", "valid_no_funds"):
                return payload
            err = str(payload.get("error") or "")
            retry_after = payload.get("retry_after")
            transient = (
                state == "rate_limited"
                or "network error" in err
                or "HTTP 5" in err
                or bool(payload.get("rate_limited"))
            )
            if transient and attempt < max_retry:
                if retry_after:
                    # Server told us how long to wait — honor it (capped at 30s).
                    delay = min(float(retry_after), 30.0)
                else:
                    delay = PROVIDER_MIN_INTERVAL.get(provider, 0.5) * (attempt + 1)
                if state == "rate_limited":
                    th.rate_limited()
                await asyncio.sleep(delay)
                continue
            return payload
        return last

    def _emit(self, key: str, provider: str, payload: dict):
        """Normalize a provider payload and publish one result to the GUI."""
        if not isinstance(payload, dict):
            payload = {"valid": False, "state": "unknown", "error": "invalid provider payload"}
        state = payload.get("state")
        if not state:
            state = "valid" if payload.get("valid") else "invalid"
            payload["state"] = state
        payload.setdefault("provider", provider)
        payload.setdefault("models", [])
        payload.setdefault("details", {})
        payload.setdefault("remaining", None)
        payload.setdefault("balance_summary", "")
        payload.setdefault("info", "")
        self.result.emit(key, payload)

    def run(self):
        asyncio.run(self._run())

    async def _run(self):
        total = len(self.items)
        done = 0
        ok_n = bad_n = 0
        sem = asyncio.Semaphore(self.concurrency)
        throttles: Dict[str, AdaptiveThrottle] = {}
        connector = aiohttp.TCPConnector(limit=self.concurrency, limit_per_host=12, ttl_dns_cache=300)
        valid_items: List[tuple] = []

        async def one(key: str, provider: str):
            nonlocal done, ok_n, bad_n
            if self._stop:
                return
            async with sem:
                if self._stop:
                    return
                th = throttles.setdefault(provider, AdaptiveThrottle(PROVIDER_MIN_INTERVAL.get(provider, 0.02)))
                await th.wait()
                try:
                    payload = await self._check_with_retry(session, key, provider, th)
                except Exception as e:
                    payload = {
                        "valid": False,
                        "state": "unknown",
                        "error": f"exception: {str(e)[:160]}",
                        "remaining": None,
                        "balance_summary": "",
                        "details": {},
                        "models": [],
                        "info": "",
                    }
                if payload.get("state") in ("valid", "valid_no_funds"):
                    th.ok()
                    ok_n += 1
                    if payload.get("state") == "valid":
                        valid_items.append((key, provider))
                else:
                    bad_n += 1
                self._emit(key, provider, payload)
                done += 1
                self.progress.emit(done, total)
                if done % 25 == 0 or done == total:
                    self.status.emit(f"Checking… {done}/{total} (ok={ok_n} bad={bad_n})")

        log.info("CheckWorker start items=%d concurrency=%d light=%s enrich=%s",
                 total, self.concurrency, self.light, self.enrich_valid)
        timeout = aiohttp.ClientTimeout(total=self.timeout + 5, connect=8)
        async with aiohttp.ClientSession(connector=connector, trust_env=True, timeout=timeout) as session:
            chunk = max(self.concurrency * 4, 40)
            self.phase.emit("sweep")
            for i in range(0, len(self.items), chunk):
                if self._stop:
                    break
                part = self.items[i:i + chunk]
                await asyncio.gather(*(one(k, p) for k, p in part))

            # --- phase 2: enrichment pass over the survivors only
            if self.light and self.enrich_valid and valid_items and not self._stop:
                n = len(valid_items)
                self.phase.emit("enrich")
                self.status.emit(f"Enriching {n} valid keys (balances / quotas / catalogs)…")
                log.info("CheckWorker enrich phase keys=%d", n)
                # Enrichment makes 1-3 extra requests per key, so keep it gentler
                # per host than the sweep to avoid tripping provider rate limits.
                deep_sem = asyncio.Semaphore(max(4, min(12, self.concurrency // 3)))
                enriched = 0

                async def enrich(key: str, provider: str):
                    nonlocal enriched
                    if self._stop:
                        return
                    async with deep_sem:
                        if self._stop:
                            return
                        th = throttles.setdefault(
                            provider,
                            AdaptiveThrottle(PROVIDER_MIN_INTERVAL.get(provider, 0.05)),
                        )
                        await th.wait()
                        try:
                            payload = await check_provider(
                                session,
                                key,
                                provider,
                                timeout=self.timeout,
                                proxy=self.proxy,
                                light=False,
                            )
                        except Exception as e:
                            payload = {
                                "valid": False,
                                "state": "unknown",
                                "error": f"enrichment exception: {str(e)[:160]}",
                                "remaining": None,
                                "balance_summary": "",
                                "details": {},
                                "models": [],
                                "info": "",
                            }

                        state = payload.get("state") or (
                            "valid" if payload.get("valid") else "invalid"
                        )
                        # Never downgrade a key that already passed the cheap sweep
                        # because a deep/balance endpoint is temporarily unavailable.
                        if state in ("valid", "valid_no_funds"):
                            self._emit(key, provider, payload)
                            th.ok()
                        elif state == "rate_limited":
                            th.rate_limited()
                        enriched += 1
                        if enriched % 10 == 0 or enriched == n:
                            self.status.emit(f"Enriching… {enriched}/{n}")

                enrich_chunk = max(8, min(48, self.concurrency * 2))
                for i in range(0, len(valid_items), enrich_chunk):
                    if self._stop:
                        break
                    part = valid_items[i:i + enrich_chunk]
                    await asyncio.gather(*(enrich(k, p) for k, p in part))

        if not self._stop:
            self.status.emit(f"Check complete · {done}/{total} checked · ok={ok_n} bad={bad_n}")
        else:
            self.status.emit(f"Check stopped · {done}/{total} checked")
        log.info(
            "CheckWorker finished done=%s ok=%s bad=%s enriched=%s stopped=%s",
            done, ok_n, bad_n, enriched if 'enriched' in locals() else 0, self._stop,
        )
        self.finished_ok.emit()
