"""Export verified AI keys into ready-to-use Hermes/OpenCode configurations.

Secrets are deliberately written to a sibling .env file. Generated YAML/JSON
contains environment-variable references instead of raw API keys.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterable, List, Tuple

from providers import PROVIDERS

# Built-in environment names used by Hermes and/or OpenCode providers.
ENV_NAMES = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "xai": "XAI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "together": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "cohere": "COHERE_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "huggingface": "HF_TOKEN",
    "zhipu": "ZAI_API_KEY",
    "zai": "ZAI_API_KEY",
    "moonshot": "KIMI_API_KEY",
    "kimi": "KIMI_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "deepinfra": "DEEPINFRA_API_KEY",
    "siliconflow": "SILICONFLOW_API_KEY",
    "nebius": "NEBIUS_API_KEY",
    "hyperbolic": "HYPERBOLIC_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "sambanova": "SAMBANOVA_API_KEY",
    "ai21": "AI21_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "replicate": "REPLICATE_API_TOKEN",
    "nous": "NOUS_API_KEY",
    "novita": "NOVITA_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
}

OPENAI_COMPATIBLE_NPM = {
    "openrouter": "@ai-sdk/openai-compatible",
    "deepseek": "@ai-sdk/openai-compatible",
    "groq": "@ai-sdk/groq",
    "openai": "@ai-sdk/openai",
    "anthropic": "@ai-sdk/anthropic",
    "google": "@ai-sdk/google",
    "mistral": "@ai-sdk/mistral",
    "xai": "@ai-sdk/openai-compatible",
    "together": "@ai-sdk/openai-compatible",
    "fireworks": "@ai-sdk/openai-compatible",
    "cohere": "@ai-sdk/openai-compatible",
    "perplexity": "@ai-sdk/openai-compatible",
    "deepinfra": "@ai-sdk/openai-compatible",
    "siliconflow": "@ai-sdk/openai-compatible",
    "nebius": "@ai-sdk/openai-compatible",
    "hyperbolic": "@ai-sdk/openai-compatible",
    "cerebras": "@ai-sdk/openai-compatible",
    "sambanova": "@ai-sdk/openai-compatible",
    "nvidia": "@ai-sdk/openai-compatible",
}

# Hermes first-class provider IDs. Providers not listed here can still be exported
# as OpenCode custom providers when they expose an OpenAI-compatible chat endpoint.
HERMES_IDS = {
    "openrouter": "openrouter",
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "gemini",
    "zai": "zai",
    "zhipu": "zai",
    "moonshot": "kimi-coding",
    "kimi": "kimi-coding",
    "minimax": "minimax",
    "deepseek": "deepseek",
    "huggingface": "huggingface",
    "nvidia": "nvidia",
    "deepinfra": "deepinfra",
    "dashscope": "alibaba",
    "siliconflow": "custom",
    "groq": "custom",
    "mistral": "custom",
    "together": "custom",
    "fireworks": "custom",
    "cohere": "custom",
    "perplexity": "custom",
    "xai": "custom",
    "cerebras": "custom",
    "sambanova": "custom",
    "nebius": "custom",
    "hyperbolic": "custom",
    "nous": "nous",
}

def _env_name(provider: str) -> str:
    return ENV_NAMES.get(provider.lower(), "AI_KEY_SCOUT_" + re.sub(r"[^A-Z0-9]+", "_", provider.upper()) + "_API_KEY")

def _base_url(cfg: Dict[str, Any]) -> str:
    chat = cfg.get("chat") or {}
    url = str(chat.get("url") or "").strip()
    if not url:
        return ""
    for suffix in ("/chat/completions", "/messages", "/generateContent"):
        if url.endswith(suffix):
            return url[:-len(suffix)]
    return url

def _best_model(rec: Any) -> str:
    d = rec.details or {}
    # Prefer a model actually proven to answer. Paid is not preferred over free.
    free = list(d.get("free_working") or [])
    paid = list(d.get("paid_working") or [])
    working = list(rec.working_models or [])
    for seq in (free, working, paid, list(rec.models or []), list(d.get("catalog_preview") or [])):
        for model in seq:
            if isinstance(model, str) and model.strip():
                return model.strip()
    return ""

def select_best(records: Iterable[Any], limit: int = 1) -> List[Any]:
    candidates = [
        r for r in records
        if getattr(r, "status", "") == "valid" and _best_model(r)
    ]
    candidates.sort(key=lambda r: (
        len(getattr(r, "working_models", []) or []),
        1 if (r.details or {}).get("free_working") else 0,
        float(getattr(r, "score", 0) or 0),
        float(getattr(r, "remaining", -1) if getattr(r, "remaining", None) is not None else -1),
    ), reverse=True)
    # One credential per provider prevents two OpenRouter (or other same-provider)
    # records from overwriting the same environment variable/config entry.
    out: List[Any] = []
    seen: set[str] = set()
    for r in candidates:
        pid = str(getattr(r, "provider", "")).lower()
        if pid in seen:
            continue
        seen.add(pid)
        out.append(r)
        if len(out) >= max(1, int(limit)):
            break
    return out

def _yaml_quote(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)

def write_hermes(records: Iterable[Any], directory: str, limit: int = 1) -> Tuple[str, str, int]:
    selected = select_best(records, limit)
    if not selected:
        raise ValueError("No valid key with a verified working model is available.")

    env_lines = [
        "# Generated by AI Key Scout. Keep this file private; do not commit it.",
    ]
    blocks: List[str] = ["# Generated by AI Key Scout", "# Secrets live in .env; this config only references them.", "model:"]
    first = selected[0]
    for r in selected:
        pid = str(r.provider).lower()
        env = _env_name(pid)
        env_lines.append(f"{env}={r.key}")
    # The first/best verified credential becomes Hermes' active model.
    pid = str(first.provider).lower()
    hermes_pid = HERMES_IDS.get(pid, "custom")
    model = _best_model(first)
    blocks.append(f"  provider: {_yaml_quote(hermes_pid)}")
    blocks.append(f"  default: {_yaml_quote(model)}")
    if hermes_pid == "custom":
        base = _base_url(PROVIDERS.get(pid, {}))
        if base:
            blocks.append(f"  base_url: {_yaml_quote(base)}")
            blocks.append(f"  api_key: {_yaml_quote('${_env_name(pid)}')}")
    blocks.extend(["", "# Verified alternatives:"])
    for r in selected:
        blocks.append(f"# - {r.provider}: {_best_model(r)} ({r.mask(6)})")
    os.makedirs(directory, exist_ok=True)
    cfg_path = os.path.join(directory, "config.yaml")
    env_path = os.path.join(directory, ".env")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("\n".join(blocks) + "\n")
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(env_lines) + "\n")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass
    return cfg_path, env_path, len(selected)



def write_extra_configs(records: Iterable[Any], directory: str, limit: int = 10) -> Dict[str, str]:
    """Write additional environment-backed interoperability configs."""
    selected = select_best(records, limit)
    if not selected:
        raise ValueError("No valid key with a verified working model is available.")
    os.makedirs(directory, exist_ok=True)
    providers = {}
    models = []
    env_lines = []
    for r in selected:
        pid = str(r.provider).lower()
        env = _env_name(pid)
        model = _best_model(r)
        base = _base_url(PROVIDERS.get(pid, {}))
        providers[pid] = {
            "name": str(getattr(r, "provider", pid)),
            "api_key_env": env,
            "base_url": base,
            "model": model,
            "status": r.status,
            "working_models": list(r.working_models or []),
        }
        if model:
            models.append({"provider": pid, "model": model, "env": env, "base_url": base})
        env_lines.append(f"{env}={r.key}")
    manifest = {
        "generated_by": "AI Key Scout v5",
        "providers": providers,
        "models": models,
        "env_file": ".env",
    }
    paths = {}
    p = os.path.join(directory, "providers.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")
    paths["providers"] = p
    p = os.path.join(directory, "opencode.jsonc")
    with open(p, "w", encoding="utf-8") as f:
        f.write("// Generated by AI Key Scout. Secrets are environment-backed.\n")
        json.dump(_opencode_payload(selected), f, indent=2, ensure_ascii=False)
        f.write("\n")
    paths["opencode_jsonc"] = p
    p = os.path.join(directory, "litellm.yaml")
    with open(p, "w", encoding="utf-8") as f:
        f.write("# Generated by AI Key Scout; API keys remain in .env.\nmodel_list:\n")
        for row in models:
            name = row["provider"] + "/" + row["model"]
            f.write(f"  - model_name: {json.dumps(name)}\n")
            f.write(f"    litellm_params:\n      model: {json.dumps(row['model'])}\n      api_key: os.environ/{row['env']}\n")
            if row["base_url"]:
                f.write(f"      api_base: {json.dumps(row['base_url'])}\n")
    paths["litellm"] = p
    return paths


def _opencode_payload(selected: Iterable[Any]) -> Dict[str, Any]:
    providers: Dict[str, Any] = {}
    for r in selected:
        pid = str(r.provider).lower()
        env = _env_name(pid)
        model = _best_model(r)
        npm = OPENAI_COMPATIBLE_NPM.get(pid, "@ai-sdk/openai-compatible")
        cfg = {
            "npm": npm,
            "name": str(getattr(r, "provider", pid)),
            "options": {"apiKey": "{env:" + env + "}"},
            "models": {model: {"name": model}} if model else {},
        }
        if npm == "@ai-sdk/openai-compatible":
            base = _base_url(PROVIDERS.get(pid, {}))
            if base:
                cfg["options"]["baseURL"] = base
        providers[pid] = cfg
    return {"$schema": "https://opencode.ai/config.json", "provider": providers}
def write_opencode(records: Iterable[Any], directory: str, limit: int = 1) -> Tuple[str, str, int]:
    selected = select_best(records, limit)
    if not selected:
        raise ValueError("No valid key with a verified working model is available.")
    providers: Dict[str, Any] = {}
    env_lines = ["# Generated by AI Key Scout. Keep this file private; do not commit it."]
    for r in selected:
        pid = str(r.provider).lower()
        env = _env_name(pid)
        env_lines.append(f"{env}={r.key}")
        model = _best_model(r)
        npm = OPENAI_COMPATIBLE_NPM.get(pid, "@ai-sdk/openai-compatible")
        cfg = {
            "npm": npm,
            "name": str(getattr(r, "provider", pid)),
            "options": {"apiKey": "{env:" + env + "}"},
            "models": {model: {"name": model}},
        }
        # Native AI SDK providers know their endpoint; custom/OpenAI-compatible
        # providers need the discovered base URL.
        if npm == "@ai-sdk/openai-compatible":
            base = _base_url(PROVIDERS.get(pid, {}))
            if base:
                cfg["options"]["baseURL"] = base
        providers[pid] = cfg
    payload = {
        "$schema": "https://opencode.ai/config.json",
        "provider": providers,
    }
    os.makedirs(directory, exist_ok=True)
    cfg_path = os.path.join(directory, "opencode.json")
    env_path = os.path.join(directory, ".env")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(env_lines) + "\n")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass
    return cfg_path, env_path, len(selected)

def write_bundle(records: Iterable[Any], directory: str, limit: int = 1) -> Dict[str, Any]:
    """Write Hermes and OpenCode side-by-side in one directory.

    A single .env is shared by both generated configs so the export is portable
    as one folder. No raw credential is written to config.yaml/opencode.json.
    """
    selected = select_best(records, limit)
    if not selected:
        raise ValueError("No valid key with a verified working model is available.")

    os.makedirs(directory, exist_ok=True)
    hermes_path, env_path, count = write_hermes(selected, directory, limit=len(selected))

    # Generate OpenCode into the same directory, then merge its env requirements
    # with the Hermes env file instead of creating a second secret file.
    _, _, _ = write_opencode(selected, directory, limit=len(selected))
    opencode_path = os.path.join(directory, "opencode.json")

    env_values = ["# Generated by AI Key Scout. Keep this file private; do not commit it."]
    seen = set()
    for r in selected:
        pid = str(r.provider).lower()
        env = _env_name(pid)
        if env not in seen:
            env_values.append(f"{env}={r.key}")
            seen.add(env)
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(env_values) + "\n")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass

    extras = write_extra_configs(selected, directory, limit=len(selected))

    return {
        "directory": directory,
        "hermes": (hermes_path, env_path, count),
        "opencode": (opencode_path, env_path, count),
        "count": count,
    }
