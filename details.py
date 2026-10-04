"""HTTP helpers + per-provider validate/detail extraction."""

from __future__ import annotations

import asyncio
import json
import re
import time
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from providers import PROVIDERS


def _parse_retry_after(headers) -> Optional[float]:
    """Parse an HTTP Retry-After header (seconds or HTTP-date) -> float seconds, or None."""
    if not headers:
        return None
    raw = headers.get("Retry-After")
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        if dt:
            return max(0.0, dt.timestamp() - time.time())
    except Exception:
        pass
    return None


def _looks_rate_limited(body) -> bool:
    """Heuristic: does an error body look like a RATE LIMIT (vs a dead/invalid key)?"""
    if body is None:
        return False
    if isinstance(body, (dict, list)):
        text = json.dumps(body).lower()
    else:
        text = str(body).lower()
    signals = (
        "rate limit", "ratelimit", "rate_limit", "too many requests",
        "quota exceeded", "request limit", "slow down", "temporarily blocked",
        "please retry", "try again later", "throttl",
    )
    if any(s in text for s in signals):
        return True
    # Numeric code only counts when it is the structured error code, not any
    # substring (a bare "429" match fired on unrelated IDs and messages).
    if isinstance(body, dict):
        err = body.get("error")
        for probe in (body.get("code"), body.get("status"),
                      err.get("code") if isinstance(err, dict) else None,
                      err.get("status") if isinstance(err, dict) else None):
            try:
                if int(probe) == 429:
                    return True
            except Exception:
                pass
    return False


def _looks_no_funds(body) -> bool:
    """402-style: the key authenticated but the account is out of credit.

    This is a POSITIVE result for a key-collection tool — it proves the key is
    real — yet v3 scored it the same as a revoked key because only 200 counted.
    """
    if body is None:
        return False
    text = json.dumps(body).lower() if isinstance(body, (dict, list)) else str(body).lower()
    signals = (
        "insufficient", "quota has been exhausted", "exceeded your current quota",
        "out of credit", "no credit", "balance is insufficient", "insufficient_quota",
        "payment required", "not enough balance", "please recharge", "arrears",
    )
    return any(s in text for s in signals)


# --------------------------------------------------------------------------
# States reported by check_provider. `valid` stays the only "good key" state so
# existing filters keep working; the others are refinements, not rejections.
STATE_VALID = "valid"
STATE_NO_FUNDS = "valid_no_funds"     # authenticated, no credit left
STATE_RATE_LIMITED = "rate_limited"    # transient; retry later, NOT dead
STATE_UNKNOWN = "unknown"              # 5xx / network — inconclusive
STATE_INVALID = "invalid"


_MODEL_ERR_RE = re.compile(
    r"model\b[^.\n]{0,80}?"
    r"(not found|does not exist|not exist|unknown|unsupported|invalid|end of life|"
    r"end-of-life|deprecated|no longer|retired|decommission|gone)"
    r"|"
    r"(not found|does not exist|unsupported|unknown|deprecated|retired|end of life)"
    r"[^.\n]{0,40}?\bmodel\b",
    re.IGNORECASE,
)


def _model_not_found(body) -> bool:
    """True when the error is specifically about the MODEL, not the key.

    Providers authenticate before resolving the model, so a model-level complaint
    means the key is live. Treating it as a dead key is what made every Anthropic
    and Voyage key in v3 report invalid. Also covers retired-model wording ("end
    of life", "no longer available") because providers retire ids constantly.

    The qualifier must sit NEXT TO the word "model" — a bare "invalid" match would
    turn "Invalid API key for this model" into a false positive.
    """
    if not isinstance(body, dict):
        return False
    err = body.get("error")
    if isinstance(err, dict):
        etype = str(err.get("type") or err.get("code") or "")
        if "model" in etype.lower() and _MODEL_ERR_RE.search(f"model {etype}"):
            return True
        if _MODEL_ERR_RE.search(str(err.get("message") or "")):
            return True
    for field in ("message", "detail", "msg", "title"):
        if _MODEL_ERR_RE.search(str(body.get(field) or "")):
            return True
    return False


# --------------------------------------------------------------------------
# Generic response miner. v3 hand-coded a 3-item field-name tuple per provider
# and gave up if the vendor renamed anything; this walks the whole payload and
# keeps anything that looks like money, quota or account metadata.
_MONEY_KEYS = (
    "balance", "credit", "remaining", "available", "quota", "limit",
    "usage", "used", "spent", "granted", "topped", "recharge", "funds",
)
_ACCOUNT_KEYS = (
    "plan", "tier", "organization", "org", "username", "user", "email",
    "name", "account", "is_free", "free_tier", "expires", "expiry", "created",
    "project", "role", "status", "trial",
)
_NUM_KEYS = ("total", "amount", "value", "count", "balance", "credit", "remaining")


def _walk_json(node, path: str = "", out: Optional[List[tuple]] = None, depth: int = 0) -> List[tuple]:
    """Flatten a JSON payload to [(dotted.path, scalar), ...] (bounded)."""
    if out is None:
        out = []
    if depth > 6 or len(out) > 200:
        return out
    if isinstance(node, dict):
        for k, v in node.items():
            _walk_json(v, f"{path}.{k}" if path else str(k), out, depth + 1)
    elif isinstance(node, list):
        for i, v in enumerate(node[:5]):
            _walk_json(v, f"{path}[{i}]", out, depth + 1)
    elif node is not None and not isinstance(node, bool):
        out.append((path, node))
    return out


def _pick_money(flat: List[tuple]) -> Optional[float]:
    """Best-guess remaining credit from a flattened payload."""
    best = None
    best_score = -1
    for path, val in flat:
        leaf = path.rsplit(".", 1)[-1].lower()
        if not any(m in leaf for m in _MONEY_KEYS):
            continue
        if any(bad in leaf for bad in ("used", "usage", "spent", "total_spent", "consumed")):
            continue  # that's spend, not remaining
        try:
            num = float(str(val).replace(",", "").strip())
        except Exception:
            continue
        score = 0
        if "remain" in leaf or "available" in leaf or "balance" in leaf:
            score = 3
        elif "credit" in leaf or "quota" in leaf:
            score = 2
        else:
            score = 1
        if score > best_score:
            best_score, best = score, num
    return best


def mine_generic_details(body: Any) -> Tuple[Dict[str, Any], Optional[float], str]:
    """Return (details, remaining, info_string) mined from any JSON payload."""
    if not isinstance(body, dict):
        return {}, None, ""
    flat = _walk_json(body)
    details: Dict[str, Any] = {}
    notes: List[str] = []
    for path, val in flat:
        leaf = path.rsplit(".", 1)[-1].lower()
        if any(a in leaf for a in _ACCOUNT_KEYS) and len(str(val)) < 80:
            if path not in details:
                details[path] = val
            if len(notes) < 8:
                notes.append(f"{leaf}={val}")
    remaining = _pick_money(flat)
    money_notes = []
    for path, val in flat:
        leaf = path.rsplit(".", 1)[-1].lower()
        if any(m in leaf for m in _MONEY_KEYS) and len(str(val)) < 40:
            if len(money_notes) < 6:
                money_notes.append(f"{leaf}={val}")
    info = "; ".join(notes[:6])
    if money_notes:
        info = (info + " | " if info else "") + " ".join(money_notes)
    return details, remaining, info[:400]


def compose_summary(provider_label: str, remaining: Optional[float], info: str,
                    model_count: int = 0) -> str:
    """Never return a bare 'Valid' — always carry whatever we actually know."""
    bits: List[str] = []
    if remaining is not None:
        bits.append(f"{provider_label} {_money(remaining, 2)}")
    if model_count:
        bits.append(f"{model_count} models")
    if info:
        bits.append(info)
    if not bits:
        bits.append(f"{provider_label} authenticated")
    s = " · ".join(bits)
    return s[:220]


async def make_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    headers: Dict[str, str],
    json_data: Any = None,
    timeout: float = 15,
    proxy: Optional[str] = None,
    headers_out: Optional[dict] = None,
) -> Tuple[Optional[int], Any]:
    """Return (status, parsed_body). Body read inside context (no Connection closed)."""
    try:
        kwargs = {
            "headers": headers,
            "timeout": aiohttp.ClientTimeout(total=timeout, sock_connect=6),
            "proxy": proxy,
        }
        if method.upper() == "GET":
            cm = session.get(url, **kwargs)
        else:
            cm = session.post(url, json=json_data, **kwargs)
        async with cm as resp:
            status = resp.status
            if headers_out is not None:
                headers_out.clear()
                for _hk, _hv in resp.headers.items():
                    headers_out[_hk] = _hv
            body: Any = None
            try:
                body = await resp.json(content_type=None)
            except Exception:
                try:
                    body = await resp.text()
                except Exception:
                    body = None
            return status, body
    except Exception:
        return None, None


def _money(v: Any, digits: int = 4) -> str:
    if v is None:
        return "n/a"
    try:
        x = float(v)
    except Exception:
        return str(v)
    if digits == 2:
        return f"${x:.2f}"
    s = f"${x:.{digits}f}".rstrip("0").rstrip(".")
    return s or "$0"


def parse_openrouter_details(key_payload: Any, credits_payload: Any = None) -> Dict[str, Any]:
    data = key_payload.get("data") if isinstance(key_payload, dict) and "data" in key_payload else (key_payload or {})
    if not isinstance(data, dict):
        data = {}
    usage = float(data.get("usage") or 0)
    usage_daily = float(data.get("usage_daily") or 0)
    usage_weekly = float(data.get("usage_weekly") or 0)
    usage_monthly = float(data.get("usage_monthly") or 0)
    is_free = bool(data.get("is_free_tier"))
    is_mgmt = bool(data.get("is_management_key"))
    is_prov = bool(data.get("is_provisioning_key"))
    if is_mgmt:
        key_type = "management"
    elif is_prov:
        key_type = "provisioning"
    elif is_free:
        key_type = "inference / free-tier"
    else:
        key_type = "inference / paid"

    total_credits = total_usage = remaining = None
    if isinstance(credits_payload, dict):
        cdata = credits_payload.get("data") if "data" in credits_payload else credits_payload
        if isinstance(cdata, dict) and ("total_credits" in cdata or "total_usage" in cdata):
            total_credits = float(cdata.get("total_credits") or 0)
            total_usage = float(cdata.get("total_usage") or 0)
            remaining = total_credits - total_usage
    if remaining is None and data.get("limit_remaining") is not None:
        try:
            remaining = float(data["limit_remaining"])
        except Exception:
            remaining = None

    return {
        "label": data.get("label"),
        "is_free_tier": is_free,
        "is_management_key": is_mgmt,
        "is_provisioning_key": is_prov,
        "key_type": key_type,
        "limit": data.get("limit"),
        "limit_reset": data.get("limit_reset"),
        "limit_remaining": data.get("limit_remaining"),
        "usage": usage,
        "usage_daily": usage_daily,
        "usage_weekly": usage_weekly,
        "usage_monthly": usage_monthly,
        "expires_at": data.get("expires_at"),
        "creator_user_id": data.get("creator_user_id"),
        "total_credits": total_credits,
        "total_usage": total_usage,
        "remaining": remaining,
        "has_balance": True,
    }


def format_openrouter_summary(d: Dict[str, Any]) -> str:
    tier = "Management" if d.get("is_management_key") else ("Free-tier" if d.get("is_free_tier") else "Pay-as-you-go")
    acct = _money(d.get("remaining"), 2)
    s = (
        f"{tier} - Used: {_money(d.get('usage'))} | {d.get('key_type')} | account {acct} | "
        f"d/w/m {_money(d.get('usage_daily'))}/{_money(d.get('usage_weekly'))}/{_money(d.get('usage_monthly'))}"
    )
    if d.get("total_credits") is not None:
        s += f" | purchased {_money(d.get('total_credits'), 2)} / used {_money(d.get('total_usage'), 2)}"
    return s


def format_openrouter_info(d: Dict[str, Any]) -> str:
    parts = []
    if d.get("creator_user_id"):
        parts.append(f"creator={d['creator_user_id']}")
    if d.get("limit") is None:
        parts.append("limit=unlimited")
    else:
        parts.append(f"limit={_money(d.get('limit'), 2)}")
    parts.append(f"expires={d.get('expires_at') or 'never'}")
    if d.get("label"):
        parts.append(f"label={d['label']}")
    return "; ".join(parts)


def _model_ids(body: Any, limit: int = 5) -> List[str]:
    """Return at most `limit` model ids (avoid huge lists freezing the UI/memory)."""
    if not isinstance(body, dict):
        return []
    data = body.get("data") or body.get("models") or []
    if not isinstance(data, list):
        return []
    out = []
    for m in data:
        if len(out) >= limit:
            break
        if isinstance(m, dict):
            mid = m.get("id") or m.get("name")
            if mid:
                out.append(str(mid))
        elif isinstance(m, str):
            out.append(m)
    return out


def _model_count(body: Any) -> int:
    if not isinstance(body, dict):
        return 0
    data = body.get("data") or body.get("models") or []
    return len(data) if isinstance(data, list) else 0


def _blank_result(error: str = "", state: str = STATE_UNKNOWN) -> Dict[str, Any]:
    return {
        "valid": state == STATE_VALID,
        "state": state,
        "error": error,
        "details": {},
        "models": [],
        "remaining": None,
        "balance_summary": "",
        "info": "",
    }


def _probe_payload(cfg: Dict[str, Any], key: str, model: Optional[str] = None) -> Dict[str, Any]:
    """Build a probe body, swapping in `__KEY__` and an optional model override."""
    base = dict(cfg["probe"].get("json") or {})
    out = {}
    for k, v in base.items():
        if v == "__KEY__":
            out[k] = key
        elif k == "model" and model:
            out[k] = model
        else:
            out[k] = v
    return out


async def _run_probe(
    session: aiohttp.ClientSession,
    cfg: Dict[str, Any],
    key: str,
    headers: Dict[str, str],
    timeout: float,
    proxy: Optional[str],
) -> Tuple[Optional[int], Any]:
    """Run the provider's real-inference probe, walking probe_models on model errors.

    Probes exist for providers whose models-list endpoint answers 200 without auth
    (NVIDIA, Jina, SambaNova, DeepInfra). A model-404 still proves the key, so we
    walk the candidate list and treat 'model not found' as authenticated.
    """
    probe = cfg.get("probe")
    if not probe:
        return None, None
    models = cfg.get("probe_models") or [None]
    last_status: Optional[int] = None
    last_body: Any = None
    for model in models:
        st, body = await make_request(
            session, probe.get("method", "POST"), probe["url"], headers,
            json_data=_probe_payload(cfg, key, model),
            timeout=timeout, proxy=proxy,
        )
        last_status, last_body = st, body
        if st in (probe.get("ok") or [200]):
            return st, body
        if st in (400, 404) and _model_not_found(body):
            return st, body
        if st in (401, 403) or st == 402:
            return st, body  # key-level verdict, no point trying another model
    return last_status, last_body


async def _rescue_validate(
    session: aiohttp.ClientSession,
    cfg: Dict[str, Any],
    key: str,
    headers: Dict[str, str],
    timeout: float,
    proxy: Optional[str],
) -> Tuple[bool, str]:
    """Last-chance auth proof after a model-list call failed.

    Used by Google (403 on /models usually means a restricted-but-live key) and by
    any provider that declares probe_models + model_404_is_valid.
    Returns (authenticated, note).
    """
    models = cfg.get("probe_models") or []
    url_tpl = cfg.get("probe_url")
    if not models or not url_tpl:
        return False, ""
    for model in models:
        url = url_tpl.format(model=model, key=key)
        st, body = await make_request(
            session, "POST", url,
            {"Content-Type": "application/json"},
            json_data={"contents": [{"parts": [{"text": "hi"}]}],
                       "generationConfig": {"maxOutputTokens": 1}},
            timeout=timeout, proxy=proxy,
        )
        if st == 200:
            return True, f"generateContent ok on {model}"
        if st in (400, 404, 410) and cfg.get("model_404_is_valid") and _model_not_found(body):
            # ONLY a complaint about the model proves the key. A blanket "anything
            # non-200 is fine" here reported bogus Google keys as valid, because a
            # rejected key answers 400 INVALID_ARGUMENT ("API key not valid").
            return True, f"key ok (model {model} unavailable)"
    return False, ""


async def discover_provider(
    session: aiohttp.ClientSession,
    key: str,
    timeout: float = 10,
    proxy: Optional[str] = None,
    light: bool = True,
) -> Dict[str, Any]:
    """Identify a key whose shape matched no pattern by trying opaque-key vendors.

    v3 could never reach Cohere / DeepInfra / SambaNova / Nebius / Hyperbolic /
    AI21 / MiniMax at all: they have no regex, so they were never detected and
    never checked. This pass tries each in priority order and keeps the first
    provider that authenticates.
    """
    from providers import discovery_order  # local import avoids a cycle

    for pid in discovery_order():
        res = await check_provider(
            session, key, pid, timeout=timeout, proxy=proxy, light=light, discover=False,
        )
        if res.get("state") in (STATE_VALID, STATE_NO_FUNDS):
            res["details"]["discovered_as"] = pid
            res["info"] = f"discovered: {pid}" + (f" | {res.get('info')}" if res.get("info") else "")
            return res
    return _blank_result("no provider accepted this key", state=STATE_UNKNOWN)


async def check_provider(
    session: aiohttp.ClientSession,
    key: str,
    provider: str,
    timeout: float = 10,
    proxy: Optional[str] = None,
    light: bool = True,
    discover: bool = True,
) -> Dict[str, Any]:
    """Validate a key and pull provider-specific details.

    light=True keeps the network cost to ONE request (plus a probe where the
    provider requires it) but still returns mined metadata — it no longer throws
    the payload away. Use light=False to run the full DETAIL_FETCHERS pass.
    """
    cfg = PROVIDERS.get(provider)
    if not cfg or not cfg.get("validate"):
        if discover:
            return await discover_provider(session, key, timeout=timeout, proxy=proxy, light=light)
        return _blank_result("unknown provider", state=STATE_UNKNOWN)

    headers = dict(cfg["headers"](key))
    v = cfg["validate"]
    url = v["url"]
    if v.get("key_in_url"):
        url = url.format(key=key)
    json_data = v.get("json")
    if isinstance(json_data, dict):
        # Allow "__KEY__" placeholder in provider validate payloads (e.g. Tavily)
        json_data = {
            kk: (key if vv == "__KEY__" else vv) for kk, vv in json_data.items()
        }

    detail_kind = cfg.get("detail") or "generic"
    validate_headers: Dict[str, str] = {}
    status, body = await make_request(
        session, v["method"], url, headers, json_data=json_data,
        timeout=timeout, proxy=proxy, headers_out=validate_headers,
    )
    ok_codes = v.get("ok") or [200]

    state = STATE_VALID
    error = ""
    retry_after = None
    auth_note = ""

    if status not in ok_codes:
        retry_after = _parse_retry_after(validate_headers)
        rescue_ok = False
        if status in (400, 403, 404) and cfg.get("probe_url"):
            rescue_ok, auth_note = await _rescue_validate(
                session, cfg, key, headers, timeout, proxy
            )
        if rescue_ok:
            state = STATE_VALID
            error = ""
        elif status == 429 or (status in (401, 403) and _looks_rate_limited(body)):
            state = STATE_RATE_LIMITED
            error = f"HTTP {status} (rate limited — retry later)"
        elif status == 402 or (status in (400, 403) and _looks_no_funds(body)):
            state = STATE_NO_FUNDS
            error = f"HTTP {status} (authenticated · no credit)"
        elif _model_not_found(body):
            # Providers authenticate BEFORE resolving the model, so a model-level
            # complaint proves the key is live. This is what mislabelled every
            # Anthropic and Voyage key as invalid in v3.
            state = STATE_VALID
            error = ""
            auth_note = "key ok (model unavailable)"
        elif status and status >= 500:
            state = STATE_UNKNOWN
            error = f"HTTP {status} (provider side failure — inconclusive)"
        else:
            state = STATE_INVALID
            hint = " (bad endpoint or not this provider)" if status == 404 else (
                " (invalid/revoked key, or wrong provider)" if status in (401, 403) else "")
            error = f"HTTP {status}{hint}" if status else "network error"

        if state != STATE_VALID:
            res = _blank_result(error, state=state)
            res["retry_after"] = retry_after
            res["rate_limited"] = state == STATE_RATE_LIMITED
            res["http_status"] = status
            return res

    # --- auth proven (or rescued). Providers whose models-list is public still
    # need a real inference call before we call the key good.
    if cfg.get("probe") and not auth_note:
        pst, pbody = await _run_probe(session, cfg, key, headers, timeout, proxy)
        if pst not in (cfg["probe"].get("ok") or [200]):
            if pst == 402 or _looks_no_funds(pbody):
                res = _blank_result("authenticated · no credit", state=STATE_NO_FUNDS)
                res["http_status"] = pst
                return res
            if pst == 429 or _looks_rate_limited(pbody):
                res = _blank_result("HTTP 429 (rate limited — retry later)", state=STATE_RATE_LIMITED)
                res["rate_limited"] = True
                return res
            if pst in (401, 403):
                res = _blank_result(f"HTTP {pst} (probe rejected the key)", state=STATE_INVALID)
                res["http_status"] = pst
                return res
            if _model_not_found(pbody):
                auth_note = "key ok (probe model unavailable)"
            else:
                res = _blank_result(
                    f"probe inconclusive (HTTP {pst})" if pst else "probe network error",
                    state=STATE_UNKNOWN,
                )
                res["http_status"] = pst
                return res
        else:
            auth_note = "probe ok"

    # Cohere check-api-key returns HTTP 200 with {"valid": false}
    if provider == "cohere" and isinstance(body, dict) and body.get("valid") is False:
        return _blank_result("Cohere says valid=false", state=STATE_INVALID)

    if light:
        return _light_result(cfg, provider, detail_kind, body, auth_note=auth_note)

    fetcher = DETAIL_FETCHERS.get(detail_kind, detail_generic)
    try:
        result = await fetcher(session, key, provider, cfg, headers, body, timeout, proxy)
    except Exception as e:
        result = {
            "remaining": None,
            "balance_summary": "",
            "details": {"raw_status": status},
            "models": [],
            "info": f"detail fetch failed: {str(e)[:120]}",
            "error": f"detail fetch failed: {str(e)[:120]}",
        }
    result["valid"] = True
    result["state"] = STATE_VALID
    result.setdefault("error", "")
    result.setdefault("details", {})
    result["details"]["has_balance"] = bool(cfg.get("has_balance"))
    if auth_note:
        result["details"]["auth_note"] = auth_note
        result["info"] = (result.get("info") + " | " if result.get("info") else "") + auth_note
    if not result.get("balance_summary"):
        result["balance_summary"] = compose_summary(
            cfg["name"], result.get("remaining"), result.get("info", ""),
            len(result.get("models") or []),
        )
    # Hard-cap stored models always
    if isinstance(result.get("models"), list) and len(result["models"]) > 5:
        result["details"]["model_count"] = result["details"].get("model_count") or len(result["models"])
        result["models"] = result["models"][:5]
    return result


def _light_result(
    cfg: Dict[str, Any],
    provider: str,
    detail_kind: str,
    body: Any,
    auth_note: str = "",
) -> Dict[str, Any]:
    """One-request result that still carries real information.

    v3's light path returned the literal string "Valid" for every provider whose
    detail kind wasn't a balance lookup, which is why 14+ providers looked empty.
    Everything here is derived from the payload we already paid for.
    """
    count = _model_count(body)
    models = _model_ids(body, 5 if detail_kind in ("openai_models", "google") else 3)
    details: Dict[str, Any] = {"has_balance": bool(cfg.get("has_balance")), "light": True}
    if count:
        details["model_count"] = count
    # Keep a wider preview than we display, so the "say hi" tester has real ids to
    # try without paying for a second catalog request.
    preview = _model_ids(body, 40)
    if preview:
        details["catalog_preview"] = preview
    mined, remaining, mined_info = mine_generic_details(body)
    for k, val in mined.items():
        details.setdefault(k, val)

    name = cfg["name"]
    if detail_kind == "huggingface" and isinstance(body, dict):
        who = body.get("name") or body.get("fullname") or "?"
        summary = f"HF user: {who}"
        details.setdefault("username", who)
    elif detail_kind == "github" and isinstance(body, dict):
        who = body.get("login") or "?"
        summary = f"GitHub @{who}"
        details.setdefault("login", who)
    else:
        summary = compose_summary(name, None, mined_info, count)
    if auth_note and auth_note not in summary:
        summary = f"{summary} · {auth_note}" if summary else auth_note

    info_parts = [p for p in (mined_info, f"models={count}" if count else "", auth_note) if p]
    return {
        "valid": True,
        "state": STATE_VALID,
        "error": "",
        "remaining": remaining,
        "balance_summary": summary[:220],
        "details": details,
        "models": models,
        "info": " | ".join(info_parts) or "ok",
    }


async def detail_basic(session, key, provider, cfg, headers, validate_body, timeout, proxy):
    """Generic enrichment: mine whatever the validate payload contained.

    'basic' used to return the literal word "Valid" for Perplexity, Voyage,
    ElevenLabs, Stability and LangSmith — the payload was already downloaded and
    then thrown away.
    """
    mined, remaining, mined_info = mine_generic_details(validate_body)
    models = _model_ids(validate_body)
    count = _model_count(validate_body)
    details: Dict[str, Any] = dict(mined)
    if count:
        details["model_count"] = count
    return {
        "remaining": remaining,
        "balance_summary": compose_summary(cfg.get("name") or provider, remaining, mined_info, count),
        "details": details,
        "models": models,
        "info": " | ".join(p for p in (mined_info, f"models={count}" if count else "") if p) or "ok",
    }


async def detail_openrouter(session, key, provider, cfg, headers, validate_body, timeout, proxy):
    # validate already hit /key — reuse body; also fetch credits
    st, credits = await make_request(
        session, "GET", "https://openrouter.ai/api/v1/credits", headers, timeout=timeout, proxy=proxy
    )
    credits_body = credits if st == 200 else None
    d = parse_openrouter_details(validate_body, credits_body)

    # Catalog preview: gives the "say hi" tester real, currently-served model ids
    # (OpenRouter retires free slugs often, so a curated list alone is fragile).
    catalog: List[str] = []
    murl = cfg.get("models_url")
    if murl:
        mst, mbody = await make_request(session, "GET", murl, headers,
                                        timeout=min(timeout, 10), proxy=proxy)
        if mst == 200:
            catalog = _model_ids(mbody, limit=200)
            if catalog:
                d["model_count"] = _model_count(mbody) or len(catalog)
                d["catalog_preview"] = catalog[:40]

    return {
        "remaining": d.get("remaining"),
        "balance_summary": format_openrouter_summary(d),
        "details": d,
        "models": catalog[:5],
        "info": format_openrouter_info(d),
    }


def _as_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        try:
            return float(str(v).strip().replace(",", ""))
        except Exception:
            return None


async def detail_deepseek(session, key, provider, cfg, headers, validate_body, timeout, proxy):
    """Parse GET /user/balance — balances are strings; accounts may have CNY and/or USD rows."""
    # If validate somehow didn't return JSON, refetch
    body = validate_body
    if not isinstance(body, dict) or "balance_infos" not in body:
        st, body = await make_request(
            session, "GET", "https://api.deepseek.com/user/balance",
            {**headers, "Accept": "application/json"},
            timeout=timeout, proxy=proxy,
        )
        if st != 200 or not isinstance(body, dict):
            return {
                "remaining": None,
                "balance_summary": f"Valid but balance parse failed (HTTP {st})",
                "details": {"raw_status": st},
                "models": [],
                "info": "balance endpoint odd",
            }

    is_available = body.get("is_available")
    infos = body.get("balance_infos") or []
    if not isinstance(infos, list):
        infos = []

    rows = []
    best_rem = None
    best_currency = "USD"
    parts = []
    for info in infos:
        if not isinstance(info, dict):
            continue
        cur = str(info.get("currency") or "?")
        total = _as_float(info.get("total_balance"))
        granted = _as_float(info.get("granted_balance"))
        topped = _as_float(info.get("topped_up_balance"))
        rows.append({
            "currency": cur,
            "total_balance": total,
            "granted_balance": granted,
            "topped_up_balance": topped,
        })
        # Prefer the currency row with the highest total (don't stuck on USD=0 when CNY has funds)
        if total is not None and (best_rem is None or total > best_rem):
            best_rem = total
            best_currency = cur
        bit = f"{cur} total={total if total is not None else '?'} granted={granted if granted is not None else '?'} topped={topped if topped is not None else '?'}"
        parts.append(bit)

    # remaining: use best currency total; if all zero that's real zero
    remaining = best_rem if best_rem is not None else 0.0
    avail = "available" if is_available else "NOT available"
    if parts:
        summary = f"DeepSeek {best_currency} {_money(remaining, 2)} ({avail}) | " + " · ".join(parts)
    else:
        summary = f"DeepSeek balance {_money(remaining, 2)} ({avail}) — empty balance_infos"

    # A key with a $0 balance still tells us something if we fetch its catalog —
    # v3 stored nothing at all in that case.
    models: List[str] = []
    details_extra: Dict[str, Any] = {}
    if not is_available or not remaining:
        murl = cfg.get("models_url")
        if murl:
            st, mbody = await make_request(session, "GET", murl, headers,
                                           timeout=min(timeout, 8), proxy=proxy)
            if st == 200:
                models = _model_ids(mbody, limit=20)
                details_extra["model_count"] = _model_count(mbody) or len(models)
                _, _, mi = mine_generic_details(mbody)
                if mi:
                    details_extra["models_info"] = mi

    return {
        "remaining": remaining,
        "balance_summary": summary[:220],
        "details": {
            "remaining": remaining,
            "currency": best_currency,
            "is_available": is_available,
            "balance_infos": rows,
            "has_balance": True,
            **details_extra,
        },
        "models": models,
        "info": f"is_available={is_available}; " + "; ".join(parts[:4]),
    }


async def detail_openai(session, key, provider, cfg, headers, validate_body, timeout, proxy):
    """Enrich OpenAI: model catalog + notable models; billing only if dashboard API allows."""
    body = validate_body
    if not isinstance(body, dict):
        st, body = await make_request(
            session, "GET", "https://api.openai.com/v1/models",
            headers, timeout=timeout, proxy=proxy,
        )
        if st != 200:
            return {
                "remaining": None,
                "balance_summary": f"Valid? models HTTP {st}",
                "details": {},
                "models": [],
                "info": f"HTTP {st}",
            }

    count = _model_count(body)
    models = _model_ids(body, limit=8)
    # Notable owned/available model flags (useful when balance API is locked)
    all_ids = []
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, list):
        for m in data:
            if isinstance(m, dict) and m.get("id"):
                all_ids.append(str(m["id"]))
    notable = []
    for needle in (
        "gpt-4o", "gpt-4.1", "gpt-4", "gpt-5", "o1", "o3", "o4",
        "chatgpt", "whisper", "dall-e", "tts",
    ):
        if any(needle in mid for mid in all_ids):
            notable.append(needle)

    key_kind = "project" if key.startswith("sk-proj-") else ("service" if key.startswith("sk-") else "unknown")
    remaining = None
    billing_note = "billing N/A (needs dashboard/admin access)"
    details: Dict[str, Any] = {
        "model_count": count,
        "notable": notable,
        "key_kind": key_kind,
        "has_balance": False,
    }

    # Legacy dashboard endpoints often 401 for normal API keys — try anyway, briefly
    for url, label in (
        ("https://api.openai.com/v1/dashboard/billing/credit_grants", "credit_grants"),
        ("https://api.openai.com/dashboard/billing/credit_grants", "credit_grants_alt"),
    ):
        st, grants = await make_request(session, "GET", url, headers, timeout=min(timeout, 8), proxy=proxy)
        if st == 200 and isinstance(grants, dict):
            remaining = _as_float(
                grants.get("total_available")
                or grants.get("total_available_credits")
            )
            used = _as_float(grants.get("total_used") or grants.get("total_used_credits"))
            granted = _as_float(grants.get("total_granted") or grants.get("total_granted_credits"))
            details.update({
                "remaining": remaining,
                "total_used": used,
                "total_granted": granted,
                "billing_source": label,
                "has_balance": True,
            })
            billing_note = f"credits avail={remaining} used={used} granted={granted}"
            break
        if st is not None:
            billing_note = f"billing HTTP {st}"

    if remaining is not None:
        summary = f"OpenAI {_money(remaining, 2)} avail · {count} models · {key_kind}"
    else:
        flags = ",".join(notable[:6]) if notable else "—"
        summary = f"OpenAI valid · {count} models · {key_kind} · [{flags}]"

    return {
        "remaining": remaining,
        "balance_summary": summary,
        "details": details,
        "models": models,
        "info": f"{billing_note}; notable={','.join(notable[:8])}",
    }


async def detail_google(session, key, provider, cfg, headers, validate_body, timeout, proxy):
    """Gemini model catalog + targeting metadata (the list is already in hand)."""
    models = _model_ids(validate_body, limit=200)
    if not models and isinstance(validate_body, dict):
        for m in validate_body.get("models") or []:
            if isinstance(m, dict) and m.get("name"):
                models.append(str(m["name"]).split("/")[-1])
    names = [m.split("/")[-1] for m in models]
    notable = [n for n in names if any(x in n for x in ("gemini-2", "gemini-1.5-pro", "flash", "pro"))][:8]
    mined, remaining, mined_info = mine_generic_details(validate_body)
    details: Dict[str, Any] = {"model_count": len(models), "notable": notable}
    for k, val in mined.items():
        details.setdefault(k, val)
    summary = compose_summary("Gemini", remaining, mined_info, len(models))
    return {
        "remaining": remaining,
        "balance_summary": summary,
        "details": details,
        "models": models[:40],
        "info": " | ".join(p for p in (f"models={len(models)}",
                                       "notable=" + ",".join(notable) if notable else "",
                                       mined_info) if p),
    }


async def detail_generic(session, key, provider, cfg, headers, validate_body, timeout, proxy):
    """Fallback enrichment for any provider without a bespoke parser.

    Mines the validate payload, then optionally pulls the model catalog, so a
    vendor we have never hand-coded still yields account/quota/model information
    instead of the word 'Valid'.
    """
    mined, remaining, mined_info = mine_generic_details(validate_body)
    details: Dict[str, Any] = {k: v for k, v in mined.items()}
    models = _model_ids(validate_body, limit=50)
    count = _model_count(validate_body)
    if not models and cfg.get("models_url"):
        st, body = await make_request(
            session, "GET", cfg["models_url"], headers, timeout=timeout, proxy=proxy
        )
        if st == 200:
            models = _model_ids(body, limit=50)
            count = _model_count(body) or len(models)
            m2, rem2, mi2 = mine_generic_details(body)
            for k, val in m2.items():
                details.setdefault(k, val)
            if rem2 is not None and remaining is None:
                remaining = rem2
            if mi2:
                mined_info = (mined_info + " | " if mined_info else "") + mi2
    if count:
        details["model_count"] = count
    label = cfg.get("name") or provider
    return {
        "remaining": remaining,
        "balance_summary": compose_summary(label, remaining, mined_info, count),
        "details": details,
        "models": models[:50],
        "info": " | ".join(p for p in (mined_info, f"models={count}" if count else "") if p) or "ok",
    }


async def detail_openai_models(session, key, provider, cfg, headers, validate_body, timeout, proxy):
    models = _model_ids(validate_body, limit=60)
    body = validate_body
    if not models and cfg.get("models_url"):
        st, body = await make_request(
            session, "GET", cfg["models_url"], headers, timeout=timeout, proxy=proxy
        )
        if st == 200:
            models = _model_ids(body, limit=60)
    details: Dict[str, Any] = {"model_count": _model_count(body) or len(models)}
    if models:
        details["catalog_preview"] = models[:40]
    return {
        "remaining": None,
        "balance_summary": f"Valid · {len(models)} models",
        "details": details,
        "models": models[:50],
        "info": f"models={len(models)}",
    }


async def detail_huggingface(session, key, provider, cfg, headers, validate_body, timeout, proxy):
    name = ""
    if isinstance(validate_body, dict):
        name = validate_body.get("name") or validate_body.get("fullname") or ""
        orgs = validate_body.get("orgs") or []
        info = f"user={name}"
        if orgs:
            info += f"; orgs={len(orgs)}"
    else:
        info = "whoami ok"
    return {
        "remaining": None,
        "balance_summary": f"HF user: {name or '?'}",
        "details": {"username": name},
        "models": [],
        "info": info,
    }


async def detail_github(session, key, provider, cfg, headers, validate_body, timeout, proxy):
    login = ""
    plan = ""
    if isinstance(validate_body, dict):
        login = validate_body.get("login") or ""
        public_repos = validate_body.get("public_repos")
        private_repos = validate_body.get("owned_private_repos") or validate_body.get("total_private_repos")
        p = validate_body.get("plan") or {}
        bits = []
        if isinstance(p, dict) and p.get("name"):
            bits.append(f"plan={p['name']}")
        if public_repos is not None:
            bits.append(f"pub={public_repos}")
        if private_repos is not None:
            bits.append(f"priv={private_repos}")
        plan = " · " + " ".join(bits) if bits else ""
    return {
        "remaining": None,
        "balance_summary": f"GitHub @{login}{plan}" if login else "GitHub valid",
        "details": {"login": login, "has_balance": False},
        "models": [],
        "info": f"login={login}{plan}",
    }


async def detail_siliconflow(session, key, provider, cfg, headers, validate_body, timeout, proxy):
    remaining = None
    summary = "Valid"
    info = ""
    details: Dict[str, Any] = {}
    if isinstance(validate_body, dict):
        data = validate_body.get("data") or validate_body
        if isinstance(data, dict):
            for k in ("balance", "totalBalance", "chargeBalance", "remainBalance"):
                if k in data:
                    try:
                        remaining = float(data[k])
                        break
                    except Exception:
                        pass
            details = {kk: data[kk] for kk in list(data)[:12] if not isinstance(data[kk], (dict, list))}
            if remaining is not None:
                summary = f"SiliconFlow balance {_money(remaining, 2)}"
            info = f"name={data.get('name') or data.get('email') or ''}".strip("=")
    # fallback balance endpoint
    if remaining is None:
        st, body = await make_request(
            session, "GET", "https://api.siliconflow.cn/v1/user/balance",
            headers, timeout=timeout, proxy=proxy,
        )
        if st == 200 and isinstance(body, dict):
            data = body.get("data") or body
            if isinstance(data, dict):
                for k in ("balance", "totalBalance"):
                    if k in data:
                        try:
                            remaining = float(data[k])
                            summary = f"SiliconFlow balance {_money(remaining, 2)}"
                        except Exception:
                            pass
    # generic mining as a safety net — vendors rename balance fields constantly
    mined, mined_rem, mined_info = mine_generic_details(validate_body)
    for k, val in mined.items():
        details.setdefault(k, val)
    if remaining is None and mined_rem is not None:
        remaining = mined_rem
        summary = f"SiliconFlow balance {_money(remaining, 2)}"
    if mined_info:
        info = (info + " | " if info else "") + mined_info
    return {
        "remaining": remaining,
        "balance_summary": summary,
        "details": details,
        "models": [],
        "info": info or "ok",
    }


async def detail_moonshot(session, key, provider, cfg, headers, validate_body, timeout, proxy):
    remaining = None
    details: Dict[str, Any] = {}
    mined, remaining, mined_info = mine_generic_details(validate_body)
    details.update(mined)
    if remaining is None and isinstance(validate_body, dict):
        data = validate_body.get("data") or validate_body
        if isinstance(data, dict):
            for k in ("available_balance", "balance", "cash_balance"):
                if k in data:
                    try:
                        remaining = float(data[k])
                        break
                    except Exception:
                        pass
    return {
        "remaining": remaining,
        "balance_summary": compose_summary("Moonshot", remaining, mined_info),
        "details": details,
        "models": [],
        "info": mined_info or ("balance ok" if remaining is not None else "ok"),
    }


async def detail_novita(session, key, provider, cfg, headers, validate_body, timeout, proxy):
    remaining = None
    details: Dict[str, Any] = {}
    info = ""
    # try dedicated balance
    st, body = await make_request(
        session, "GET", "https://api.novita.ai/v3/user/balance",
        headers, timeout=timeout, proxy=proxy,
    )
    if st == 200 and isinstance(body, dict):
        mined, remaining, mined_info = mine_generic_details(body)
        details.update(mined)
        info = mined_info
    # the /v3/user payload also carries plan/quota info — mine it too
    m2, rem2, mi2 = mine_generic_details(validate_body)
    for k, val in m2.items():
        details.setdefault(k, val)
    if remaining is None:
        remaining = rem2
    if mi2:
        info = (info + " | " if info else "") + mi2
    return {
        "remaining": remaining,
        "balance_summary": compose_summary("Novita", remaining, info),
        "details": details,
        "models": [],
        "info": info or "ok",
    }


async def detail_anthropic(session, key, provider, cfg, headers, validate_body, timeout, proxy):
    """count_tokens validates for free; admin keys can also read org usage."""
    details: Dict[str, Any] = {"has_balance": False}
    info_bits: List[str] = []
    if isinstance(validate_body, dict) and validate_body.get("input_tokens") is not None:
        details["input_tokens_probe"] = validate_body.get("input_tokens")
        info_bits.append(f"count_tokens ok (in={validate_body.get('input_tokens')})")

    # Non-billable org report — only succeeds for admin/console keys, which is
    # exactly the interesting case (it confirms org-level access, not just a key).
    usage_url = cfg.get("balance_url")
    if usage_url:
        st, usage = await make_request(
            session, "GET", usage_url + "?starting_at=" + time.strftime("%Y-%m-%dT00:00:00Z"),
            {**headers, "anthropic-version": "2023-06-01"},
            timeout=min(timeout, 8), proxy=proxy,
        )
        if st == 200 and isinstance(usage, dict):
            data = usage.get("data") or []
            details.update({
                "org_usage_visible": True,
                "org_usage_buckets": len(data) if isinstance(data, list) else 0,
                "has_balance": True,
            })
            info_bits.append(f"org usage report ok ({len(data) if isinstance(data, list) else 0} buckets)")
        elif st is not None:
            info_bits.append(f"org usage HTTP {st}")

    mined, remaining, mined_info = mine_generic_details(validate_body)
    for k, val in mined.items():
        details.setdefault(k, val)
    if mined_info:
        info_bits.append(mined_info)

    label = "Anthropic"
    summary = compose_summary(label, remaining, " · ".join(info_bits))
    return {
        "remaining": remaining,
        "balance_summary": summary,
        "details": details,
        "models": ["claude-3-5-haiku-latest"] if not remaining else [],
        "info": " | ".join(info_bits) or "count_tokens ok",
    }


async def detail_tavily(session, key, provider, cfg, headers, validate_body, timeout, proxy):
    return {
        "remaining": None,
        "balance_summary": "Valid (Tavily search)",
        "details": {"has_balance": False},
        "models": [],
        "info": "search ok",
    }


async def detail_cohere(session, key, provider, cfg, headers, validate_body, timeout, proxy):
    org = ""
    if isinstance(validate_body, dict):
        org = validate_body.get("organization_id") or ""
        owner = validate_body.get("owner_id") or ""
        info = f"org={org}; owner={owner}".strip("; ")
    else:
        info = "check-api-key ok"
    return {
        "remaining": None,
        "balance_summary": "Valid (Cohere)",
        "details": {"organization_id": org, "has_balance": False},
        "models": [],
        "info": info or "ok",
    }


async def detail_replicate(session, key, provider, cfg, headers, validate_body, timeout, proxy):
    email = username = ""
    if isinstance(validate_body, dict):
        email = validate_body.get("email") or ""
        username = validate_body.get("username") or ""
    who = f"@{username}" if username else (email or "?")
    return {
        "remaining": None,
        "balance_summary": f"Replicate {who}".strip(),
        "details": {"email": email, "username": username, "has_balance": False},
        "models": [],
        "info": f"user={username or email}",
    }


DETAIL_FETCHERS = {
    "basic": detail_basic,
    "generic": detail_generic,
    "openrouter": detail_openrouter,
    "deepseek": detail_deepseek,
    "openai": detail_openai,
    "google": detail_google,
    "openai_models": detail_openai_models,
    "huggingface": detail_huggingface,
    "github": detail_github,
    "siliconflow": detail_siliconflow,
    "moonshot": detail_moonshot,
    "novita": detail_novita,
    "anthropic": detail_anthropic,
    "tavily": detail_tavily,
    "cohere": detail_cohere,
    "replicate": detail_replicate,
}


class AdaptiveThrottle:
    def __init__(self, base_interval: float = 0.05):
        self.base_interval = base_interval
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._cooldown_until = 0.0
        self.interval = base_interval

    async def wait(self):
        async with self._lock:
            now = asyncio.get_event_loop().time()
            if now < self._cooldown_until:
                await asyncio.sleep(self._cooldown_until - now)
                now = asyncio.get_event_loop().time()
            wait = self.interval - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = asyncio.get_event_loop().time()

    def rate_limited(self):
        self.interval = min(self.interval * 2, 3.0)
        self._cooldown_until = asyncio.get_event_loop().time() + 2.0

    def ok(self):
        self.interval = max(self.base_interval, self.interval * 0.9)
