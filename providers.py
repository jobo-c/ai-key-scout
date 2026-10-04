"""Provider registry: patterns, validation endpoints, detail capabilities."""

# AI tool/agent APIs live in a separate registry so they can evolve without
# turning the LLM provider table into an unmaintainable monolith.

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# priority: higher wins when multiple patterns match the same string
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "openrouter": {
        "name": "OpenRouter",
        "priority": 100,
        "patterns": [re.compile(r"\bsk-or-v1-[A-Za-z0-9_-]{20,}\b")],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://openrouter.ai/api/v1/key",
            "ok": [200],
        },
        "detail": "openrouter",
        "has_balance": True,
        # Used to seed the "say hi" model test with real, currently-served ids.
        "models_url": "https://openrouter.ai/api/v1/models",
    },
    "nous": {
        "name": "Nous Research / Nous Portal",
        "priority": 87,
        "patterns": [],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://inference-api.nousresearch.com/v1/models",
            "ok": [200],
        },
        "detail": "openai_models",
        "models_url": "https://inference-api.nousresearch.com/v1/models",
        "probe": {
            "method": "POST",
            "url": "https://inference-api.nousresearch.com/v1/chat/completions",
            "ok": [200],
            "json": {
                "model": "Hermes-4-70B",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
            },
        },
        "probe_models": ["Hermes-4-70B", "Hermes-4-405B"],
        "has_balance": True,
        "weak_pattern": True,
    },
    "anthropic": {
        "name": "Anthropic",
        "priority": 95,
        "patterns": [re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")],
        "headers": lambda k: {
            "x-api-key": k,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        # count_tokens is FREE (not billed) and is not tied to a chat model that
        # can be retired, so a live key is never misread as dead. Do NOT go back to
        # /v1/messages + claude-3-haiku-20240307: that model is retired upstream and
        # made every real key look invalid.
        "validate": {
            "method": "POST",
            "url": "https://api.anthropic.com/v1/messages/count_tokens",
            "ok": [200],
            "json": {
                "model": "claude-3-5-haiku-latest",
                "messages": [{"role": "user", "content": "hi"}],
            },
        },
        "detail": "anthropic",
        "has_balance": False,
        "balance_url": "https://api.anthropic.com/v1/organizations/usage_report/messages",
    },
    "openai": {
        "name": "OpenAI",
        "priority": 88,  # above deepseek; below openrouter/anthropic/google
        "patterns": [
            re.compile(r"\bsk-proj-[A-Za-z0-9_-]{20,}\b"),
            re.compile(r"\bsk-[A-Za-z0-9]{48}\b"),  # legacy OpenAI (no hyphens in body)
            # Do NOT use bare sk-… here — that steals sk-or-v1 OpenRouter keys
        ],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://api.openai.com/v1/models",
            "ok": [200],
        },
        "detail": "openai",
        "has_balance": True,
    },
    "google": {
        "name": "Google Gemini",
        "priority": 90,
        "patterns": [re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")],
        "headers": lambda k: {},
        "validate": {
            "method": "GET",
            "url": "https://generativelanguage.googleapis.com/v1beta/models?key={key}",
            "ok": [200],
            "key_in_url": True,
        },
        "detail": "google",
        "has_balance": False,
        # Rescue-probe models, tried in order. A 403 on the models list usually means
        # the key is live but restricted — probe generateContent to tell a working
        # Gemini key from a dead/other-Google-service key. First entry MUST be a
        # currently-served model: the old gemini-1.5-flash is retired and returned 404,
        # so every live key was still marked BAD.
        "probe_models": ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"],
        "probe_url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        "model_404_is_valid": True,
    },
    "groq": {
        "name": "Groq",
        "priority": 85,
        "patterns": [re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b")],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://api.groq.com/openai/v1/models",
            "ok": [200],
        },
        "detail": "openai_models",
        "models_url": "https://api.groq.com/openai/v1/models",
        "has_balance": False,
    },
    "deepseek": {
        "name": "DeepSeek",
        "priority": 82,
        "patterns": [re.compile(r"\bsk-[a-zA-Z0-9]{32}\b")],
        "headers": lambda k: {
            "Authorization": f"Bearer {k}",
            "Accept": "application/json",
        },
        "validate": {
            "method": "GET",
            "url": "https://api.deepseek.com/user/balance",
            "ok": [200],
        },
        "detail": "deepseek",
        "has_balance": True,
        # When the balance endpoint is empty (or the key is actually another sk-
        # vendor), the model catalog is still worth having.
        "models_url": "https://api.deepseek.com/models",
    },
    "xai": {
        "name": "xAI (Grok)",
        "priority": 85,
        "patterns": [re.compile(r"\bxai-[A-Za-z0-9_-]{20,}\b")],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://api.x.ai/v1/models",
            "ok": [200],
        },
        "detail": "openai_models",
        "models_url": "https://api.x.ai/v1/models",
        "has_balance": False,
    },
    "mistral": {
        "name": "Mistral",
        "priority": 60,
        "patterns": [re.compile(r"\b[A-Za-z0-9]{32}\b")],  # weak; validated by API
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://api.mistral.ai/v1/models",
            "ok": [200],
        },
        "detail": "openai_models",
        "models_url": "https://api.mistral.ai/v1/models",
        "has_balance": False,
        "weak_pattern": True,
    },
    "together": {
        "name": "Together AI",
        "priority": 55,
        "patterns": [
            re.compile(r"\b[a-f0-9]{64}\b"),  # common Together hex keys
        ],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "weak_pattern": True,  # 64-hex collides with other secrets — opt-in via filter trial only
        "validate": {
            "method": "GET",
            "url": "https://api.together.xyz/v1/models",
            "ok": [200],
        },
        "detail": "openai_models",
        "models_url": "https://api.together.xyz/v1/models",
        "has_balance": False,
    },
    "fireworks": {
        "name": "Fireworks",
        "priority": 75,
        "patterns": [re.compile(r"\bfw_[A-Za-z0-9]{20,}\b")],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://api.fireworks.ai/inference/v1/models",
            "ok": [200],
        },
        "detail": "openai_models",
        "models_url": "https://api.fireworks.ai/inference/v1/models",
        "has_balance": False,
    },
    "cerebras": {
        "name": "Cerebras",
        "priority": 75,
        # Real keys: csk- + long token (no year suffixes like e-2022 from cert junk)
        "patterns": [re.compile(r"\bcsk-[A-Za-z0-9]{32,}\b")],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://api.cerebras.ai/v1/models",
            "ok": [200],
        },
        "detail": "openai_models",
        "models_url": "https://api.cerebras.ai/v1/models",
        "has_balance": False,
    },
    "perplexity": {
        "name": "Perplexity",
        "priority": 85,
        "patterns": [re.compile(r"\bpplx-[A-Za-z0-9]{20,}\b")],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "POST",
            "url": "https://api.perplexity.ai/chat/completions",
            "ok": [200],
            "json": {
                "model": "sonar",
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 5,
            },
        },
        "detail": "basic",
        "has_balance": False,
    },
    "cohere": {
        "name": "Cohere",
        "priority": 70,
        # Opaque ~40-char keys — NO "co-" prefix (that matched English "co-ordinated" junk)
        "patterns": [],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "POST",
            "url": "https://api.cohere.com/v1/check-api-key",
            "ok": [200],
            # Body may be empty; validity is in JSON {"valid": true/false}
        },
        "detail": "cohere",
        "has_balance": False,
        "weak_pattern": True,
    },
    "huggingface": {
        "name": "Hugging Face",
        "priority": 80,
        "patterns": [re.compile(r"\bhf_[A-Za-z0-9]{20,}\b")],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://huggingface.co/api/whoami-v2",
            "ok": [200],
        },
        "detail": "huggingface",
        "has_balance": False,
    },
    "replicate": {
        "name": "Replicate",
        "priority": 80,
        "patterns": [re.compile(r"\br8_[A-Za-z0-9]{20,}\b")],
        "headers": lambda k: {"Authorization": f"Token {k}"},
        "validate": {
            "method": "GET",
            "url": "https://api.replicate.com/v1/account",
            "ok": [200],
        },
        "detail": "replicate",
        "has_balance": False,
    },
    "siliconflow": {
        "name": "SiliconFlow",
        "priority": 72,
        # Longer than DeepSeek's exact-32; won't match sk-or / sk-proj
        "patterns": [re.compile(r"\bsk-[a-z0-9]{40,}\b")],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://api.siliconflow.cn/v1/user/info",
            "ok": [200],
        },
        "detail": "siliconflow",
        "has_balance": True,
    },
    "moonshot": {
        "name": "Moonshot / Kimi",
        "priority": 71,
        # Same sk- shape as many CN providers — validated by balance URL
        "patterns": [re.compile(r"\bsk-[a-zA-Z0-9]{24,48}\b")],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://api.moonshot.ai/v1/users/me/balance",
            "ok": [200],
        },
        "detail": "moonshot",
        "has_balance": True,
        "weak_pattern": True,  # overlaps DeepSeek/OpenAI lengths — still tried after strong matches fail
    },
    "novita": {
        "name": "Novita",
        "priority": 76,
        # sk_… but NOT Stripe sk_live_ / sk_test_
        "patterns": [re.compile(r"\bsk_(?!live_|test_|prod_)[A-Za-z0-9]{24,}\b")],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            # /v3/openai/models returns 200 even for fake keys — do NOT use for auth
            "url": "https://api.novita.ai/v3/user",
            "ok": [200],
        },
        "detail": "novita",
        "has_balance": True,
    },
    "deepinfra": {
        "name": "DeepInfra",
        "priority": 70,
        # No stable public prefix — do not auto-detect (was matching junk di_…)
        "patterns": [],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://api.deepinfra.com/v1/openai/models",
            "ok": [200],
        },
        "detail": "openai_models",
        "models_url": "https://api.deepinfra.com/v1/openai/models",
        # /v1/openai/models is public (200 without auth) — probe real inference.
        "probe": {
            "method": "POST",
            "url": "https://api.deepinfra.com/v1/openai/chat/completions",
            "ok": [200],
            "json": {"model": "meta-llama/Meta-Llama-3.1-8B-Instruct", "max_tokens": 1,
                     "messages": [{"role": "user", "content": "hi"}]},
        },
        "has_balance": False,
        "weak_pattern": True,
    },
    "zhipu": {
        "name": "Zhipu / GLM",
        "priority": 84,
        # id.secret style: 32hex.alphanumeric
        "patterns": [re.compile(r"\b[a-f0-9]{32}\.[A-Za-z0-9]{16,}\b")],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://open.bigmodel.cn/api/paas/v4/models",
            "ok": [200],
        },
        "detail": "openai_models",
        "models_url": "https://open.bigmodel.cn/api/paas/v4/models",
        "has_balance": False,
    },
    "dashscope": {
        "name": "DashScope / Qwen",
        "priority": 74,
        "patterns": [re.compile(r"\bsk-[a-z0-9]{30,}\b")],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/models",
            "ok": [200],
        },
        "detail": "openai_models",
        "models_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/models",
        "has_balance": False,
        "weak_pattern": True,
    },
    "nvidia": {
        "name": "NVIDIA NIM",
        "priority": 86,
        "patterns": [re.compile(r"\bnvapi-[A-Za-z0-9_-]{20,}\b")],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://integrate.api.nvidia.com/v1/models",
            "ok": [200],
        },
        "detail": "openai_models",
        "models_url": "https://integrate.api.nvidia.com/v1/models",
        # SECURITY: /v1/models answers 200 with NO Authorization header at all, so
        # every string — including garbage — was reported "Valid". Auth must be
        # proven by an actual inference call.
        "probe": {
            "method": "POST",
            "url": "https://integrate.api.nvidia.com/v1/chat/completions",
            "ok": [200],
            "json": {"model": "ibm/granite-3.0-8b-instruct", "max_tokens": 1,
                     "messages": [{"role": "user", "content": "hi"}]},
        },
        # NVIDIA retires models constantly and answers 410 Gone for them (the old
        # meta/llama-3.1-8b-instruct is gone) — walk a list and let 410 fall through.
        "probe_models": ["ibm/granite-3.0-8b-instruct", "meta/llama-3.2-11b-vision-instruct",
                         "nvidia/nemotron-nano-3-30b-a3b", "google/gemma-3-4b-it"],
        "has_balance": False,
    },
    "sambanova": {
        "name": "SambaNova",
        "priority": 68,
        "patterns": [],  # often opaque; validate when pasted as generic later
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://api.sambanova.ai/v1/models",
            "ok": [200],
        },
        "detail": "openai_models",
        "models_url": "https://api.sambanova.ai/v1/models",
        # /v1/models is public (200 without auth) — probe a real completion instead.
        "probe": {
            "method": "POST",
            "url": "https://api.sambanova.ai/v1/chat/completions",
            "ok": [200],
            "json": {"model": "Meta-Llama-3.1-8B-Instruct", "max_tokens": 1,
                     "messages": [{"role": "user", "content": "hi"}]},
        },
        "has_balance": False,
        "weak_pattern": True,
    },
    "jina": {
        "name": "Jina AI",
        "priority": 80,
        "patterns": [re.compile(r"\bjina_[A-Za-z0-9_]{20,}\b")],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://api.jina.ai/v1/models",
            "ok": [200],
        },
        "detail": "openai_models",
        "models_url": "https://api.jina.ai/v1/models",
        # /v1/models is public — only a real embeddings call proves the key.
        "probe": {
            "method": "POST",
            "url": "https://api.jina.ai/v1/embeddings",
            "ok": [200],
            "json": {"input": ["ping"], "model": "jina-embeddings-v3"},
        },
        "probe_models": ["jina-embeddings-v3", "jina-embeddings-v2-base-en"],
        "model_404_is_valid": True,
        "has_balance": False,
    },
    "tavily": {
        "name": "Tavily",
        "priority": 80,
        "patterns": [re.compile(r"\btvly-[A-Za-z0-9_-]{20,}\b")],
        "headers": lambda k: {"Authorization": f"Bearer {k}", "Content-Type": "application/json"},
        "validate": {
            "method": "POST",
            "url": "https://api.tavily.com/search",
            "ok": [200],
            "json": {"api_key": "__KEY__", "query": "test", "max_results": 1},
        },
        "detail": "tavily",
        "has_balance": False,
    },
    "voyage": {
        "name": "Voyage AI",
        "priority": 79,
        # Real keys start with pa- (NOT vo- — that matched junk). /models is 404.
        "patterns": [re.compile(r"\bpa-[A-Za-z0-9_-]{32,}\b")],
        "headers": lambda k: {
            "Authorization": f"Bearer {k}",
            "Content-Type": "application/json",
        },
        "validate": {
            "method": "POST",
            "url": "https://api.voyageai.com/v1/embeddings",
            "ok": [200],
            "json": {"input": ["ping"], "model": "voyage-3.5-lite"},
        },
        # Newest-first; a retired/unknown model id must NOT mark a live key BAD,
        # so the checker walks this list and treats a model-404 as "key alive".
        "probe_models": ["voyage-3.5-lite", "voyage-3-lite", "voyage-3"],
        "model_404_is_valid": True,
        "detail": "basic",
        "has_balance": False,
    },
    "langsmith": {
        "name": "LangSmith",
        "priority": 77,
        "patterns": [
            re.compile(r"\blsv2_pt_[A-Za-z0-9_]{20,}\b"),
            re.compile(r"\bls__[A-Za-z0-9_]{20,}\b"),
        ],
        "headers": lambda k: {"x-api-key": k},
        "validate": {
            "method": "GET",
            # /info is PUBLIC (200 with no key at all) — it says nothing about the
            # key. /api/v1/sessions is auth-gated: 200 valid, 401 missing, 403 bad.
            "url": "https://api.smith.langchain.com/api/v1/sessions",
            "ok": [200],
        },
        "detail": "generic",
        "models_url": "https://api.smith.langchain.com/api/v1/workspaces",
        "has_balance": False,
    },
    "minimax": {
        "name": "MiniMax",
        "priority": 73,
        "patterns": [],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://api.minimax.io/v1/models",
            "ok": [200],
        },
        "detail": "openai_models",
        "models_url": "https://api.minimax.io/v1/models",
        "has_balance": False,
        "weak_pattern": True,
    },
    "nebius": {
        "name": "Nebius",
        "priority": 69,
        "patterns": [],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://api.studio.nebius.com/v1/models",
            "ok": [200],
        },
        "detail": "openai_models",
        "models_url": "https://api.studio.nebius.com/v1/models",
        "has_balance": False,
        "weak_pattern": True,
    },
    "hyperbolic": {
        "name": "Hyperbolic",
        "priority": 68,
        "patterns": [],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://api.hyperbolic.xyz/v1/models",
            "ok": [200],
        },
        "detail": "openai_models",
        "models_url": "https://api.hyperbolic.xyz/v1/models",
        "has_balance": False,
        "weak_pattern": True,
    },
    "ai21": {
        "name": "AI21",
        "priority": 67,
        "patterns": [],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://api.ai21.com/studio/v1/models",
            "ok": [200],
        },
        "detail": "openai_models",
        "models_url": "https://api.ai21.com/studio/v1/models",
        # /studio/v1/models is public (200 with an empty data list for ANY key) —
        # probe a real completion. jamba-1.5-mini is EOL; use the current mini.
        "probe": {
            "method": "POST",
            "url": "https://api.ai21.com/studio/v1/chat/completions",
            "ok": [200],
            "json": {"model": "jamba-mini-1.7", "max_tokens": 1,
                     "messages": [{"role": "user", "content": "hi"}]},
        },
        "probe_models": ["jamba-mini-1.7", "jamba-large-1.7"],
        "has_balance": False,
        "weak_pattern": True,
    },
    "github": {
        "name": "GitHub",
        "priority": 50,
        "patterns": [
            re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
            re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
        ],
        "headers": lambda k: {
            "Authorization": f"Bearer {k}",
            "Accept": "application/vnd.github+json",
        },
        "validate": {
            "method": "GET",
            "url": "https://api.github.com/user",
            "ok": [200],
        },
        "detail": "github",
        "has_balance": False,
    },
    "elevenlabs": {
        "name": "ElevenLabs",
        "priority": 70,
        "patterns": [re.compile(r"\bsk_[a-f0-9]{32,}\b")],
        "headers": lambda k: {"xi-api-key": k},
        "validate": {
            "method": "GET",
            "url": "https://api.elevenlabs.io/v1/user",
            "ok": [200],
        },
        "detail": "basic",
        "has_balance": False,
    },
    "stability": {
        "name": "Stability AI",
        "priority": 65,
        "patterns": [re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")],
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "validate": {
            "method": "GET",
            "url": "https://api.stability.ai/v1/user/account",
            "ok": [200],
        },
        "detail": "basic",
        "has_balance": False,
        "weak_pattern": True,
    },
}


# Merge non-LLM AI tools/agent services before building detection order.
from plugin_providers import PLUGIN_PROVIDERS
PROVIDERS.update(PLUGIN_PROVIDERS)

# ---------------------------------------------------------------------------
# Chat specs: how to ask a provider "hi" and prove the key really serves a model.
# Kept as a separate table so the endpoint/pattern definitions above stay readable.
#   style: openai | anthropic | google | cohere | embed
#   models: cheap, hand-picked ids tried first (newest-first, tolerant of EOL)
# An entry with `unsupported` is skipped by the model tester with that reason.
CHAT_SPECS: Dict[str, Dict[str, Any]] = {
    "openrouter": {
        "style": "openai",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        # Verified live 2026-09-26 against /api/v1/models (pricing prompt == 0).
        # `openrouter/free` is a router that lands on a currently-free model, so it
        # survives individual free slugs being retired — keep it first.
        "models": ["openrouter/free",
                   "liquid/lfm-2.5-2.6b:free",
                   "inclusionai/ling-3.0-flash-sante:free",
                   "openai/gpt-4o-mini"],
    },
    "openai": {
        "style": "openai",
        "url": "https://api.openai.com/v1/chat/completions",
        "models": ["gpt-4o-mini", "gpt-4.1-mini", "gpt-4o"],
    },
    "anthropic": {
        "style": "anthropic",
        "url": "https://api.anthropic.com/v1/messages",
        "models": ["claude-3-5-haiku-latest", "claude-3-7-sonnet-latest"],
    },
    "google": {
        "style": "google",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        "models": ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"],
    },
    "groq": {
        "style": "openai",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
    },
    "deepseek": {
        "style": "openai",
        "url": "https://api.deepseek.com/chat/completions",
        "models": ["deepseek-chat"],
    },
    "xai": {
        "style": "openai",
        "url": "https://api.x.ai/v1/chat/completions",
        "models": ["grok-3-mini", "grok-2-1212", "grok-beta"],
    },
    "mistral": {
        "style": "openai",
        "url": "https://api.mistral.ai/v1/chat/completions",
        "models": ["mistral-small-latest", "open-mistral-nemo", "mistral-tiny"],
    },
    "together": {
        "style": "openai",
        "url": "https://api.together.xyz/v1/chat/completions",
        "models": ["meta-llama/Llama-3.3-70B-Instruct-Turbo",
                   "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo"],
    },
    "fireworks": {
        "style": "openai",
        "url": "https://api.fireworks.ai/inference/v1/chat/completions",
        "models": ["accounts/fireworks/models/llama-v3p1-8b-instruct",
                   "accounts/fireworks/models/llama-v3p3-70b-instruct"],
    },
    "cerebras": {
        "style": "openai",
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "models": ["llama3.1-8b", "llama-3.3-70b"],
    },
    "nvidia": {
        "style": "openai",
        "url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "models": ["ibm/granite-3.0-8b-instruct", "meta/llama-3.2-11b-vision-instruct"],
    },
    "sambanova": {
        "style": "openai",
        "url": "https://api.sambanova.ai/v1/chat/completions",
        "models": ["Meta-Llama-3.1-8B-Instruct", "Meta-Llama-3.3-70B-Instruct"],
    },
    "deepinfra": {
        "style": "openai",
        "url": "https://api.deepinfra.com/v1/openai/chat/completions",
        "models": ["meta-llama/Meta-Llama-3.1-8B-Instruct",
                   "meta-llama/Meta-Llama-3.3-70B-Instruct-Turbo"],
    },
    "nebius": {
        "style": "openai",
        "url": "https://api.studio.nebius.com/v1/chat/completions",
        "models": ["meta-llama/Meta-Llama-3.1-8B-Instruct", "Qwen/Qwen3-30B-A3B"],
    },
    "hyperbolic": {
        "style": "openai",
        "url": "https://api.hyperbolic.xyz/v1/chat/completions",
        "models": ["meta-llama/Meta-Llama-3.1-8B-Instruct", "Qwen/Qwen2.5-72B-Instruct"],
    },
    "minimax": {
        "style": "openai",
        "url": "https://api.minimax.io/v1/chat/completions",
        "models": ["MiniMax-Text-01", "abab6.5s-chat"],
    },
    "novita": {
        "style": "openai",
        "url": "https://api.novita.ai/v3/openai/chat/completions",
        "models": ["meta-llama/llama-3.1-8b-instruct", "meta-llama/llama-3.3-70b-instruct"],
    },
    "siliconflow": {
        "style": "openai",
        "url": "https://api.siliconflow.cn/v1/chat/completions",
        "models": ["Qwen/Qwen2.5-7B-Instruct", "deepseek-ai/DeepSeek-V3"],
    },
    "moonshot": {
        "style": "openai",
        "url": "https://api.moonshot.ai/v1/chat/completions",
        "models": ["moonshot-v1-8k", "kimi-k2-0711-preview"],
    },
    "dashscope": {
        "style": "openai",
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "models": ["qwen-turbo", "qwen-plus"],
    },
    "zhipu": {
        "style": "openai",
        "url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "models": ["glm-4-flash", "glm-4-air"],
    },
    "ai21": {
        "style": "openai",
        "url": "https://api.ai21.com/studio/v1/chat/completions",
        "models": ["jamba-mini-1.7", "jamba-large-1.7"],
    },
    "perplexity": {
        "style": "openai",
        "url": "https://api.perplexity.ai/chat/completions",
        "models": ["sonar", "sonar-pro"],
    },
    "cohere": {
        "style": "cohere",
        "url": "https://api.cohere.com/v2/chat",
        "models": ["command-r7b-12-2024", "command-r-08-2024", "command-a-03-2025"],
    },
    "huggingface": {
        "style": "openai",
        "url": "https://router.huggingface.co/v1/chat/completions",
        "models": ["meta-llama/Llama-3.1-8B-Instruct",
                   "Qwen/Qwen2.5-7B-Instruct"],
    },
    # Not text generators — tested with a tiny embedding call instead.
    "voyage": {
        "style": "embed",
        "url": "https://api.voyageai.com/v1/embeddings",
        "models": ["voyage-3.5-lite", "voyage-3-lite"],
    },
    "jina": {
        "style": "embed",
        "url": "https://api.jina.ai/v1/embeddings",
        "models": ["jina-embeddings-v3"],
    },
    # Explicitly untestable, with the reason surfaced in the UI.
    "elevenlabs": {"unsupported": "voice synthesis only — no chat endpoint to say hi to"},
    "replicate": {"unsupported": "hosted predictions API — needs a model version per call"},
    "stability": {"unsupported": "image generation only"},
    "tavily": {"unsupported": "search API — already exercised by the validate call"},
    "langsmith": {"unsupported": "observability platform, not a model provider"},
    "github": {"unsupported": "GitHub API — not a model provider"},
}

for _pid, _spec in CHAT_SPECS.items():
    if _pid in PROVIDERS:
        PROVIDERS[_pid]["chat"] = _spec


# Strong patterns checked first when scanning free text
SCAN_ORDER = sorted(
    PROVIDERS.keys(),
    key=lambda pid: (-PROVIDERS[pid]["priority"], pid),
)


def provider_name(pid: str) -> str:
    return PROVIDERS.get(pid, {}).get("name", pid)


def _force_provider(key: str) -> Optional[str]:
    """Hard overrides from unmistakable prefixes."""
    if key.startswith("sk-or-v1-"):
        return "openrouter"
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith("sk-proj-"):
        return "openai"
    if key.startswith("AIza"):
        return "google"
    if key.startswith("gsk_"):
        return "groq"
    if key.startswith("hf_"):
        return "huggingface"
    if key.startswith("r8_"):
        return "replicate"
    if key.startswith("pplx-"):
        return "perplexity"
    if key.startswith("xai-"):
        return "xai"
    if key.startswith("fw_"):
        return "fireworks"
    if key.startswith("csk-"):
        return "cerebras"
    if key.startswith("co-"):
        return "cohere"
    if key.startswith("ghp_") or key.startswith("github_pat_"):
        return "github"
    if key.startswith("nvapi-"):
        return "nvidia"
    if key.startswith("jina_"):
        return "jina"
    if key.startswith("tvly-"):
        return "tavily"
    if key.startswith("pa-"):
        return "voyage"
    if key.startswith("fc-"):
        return "firecrawl"
    if key.startswith("bu_"):
        return "browser_use"
    if key.startswith("bless_"):
        return "browserless"
    if key.startswith("apify_api_"):
        return "apify"
    if key.startswith("exa_"):
        return "exa"
    if key.startswith("brd_"):
        return "brightdata"
    if key.startswith("pcsk_"):
        return "pinecone"
    if key.startswith("fal_"):
        return "fal"
    if key.startswith("ak-"):
        return "modal"
    if key.startswith("lsv2_pt_") or key.startswith("ls__"):
        return "langsmith"
    if key.startswith("sk_live_") or key.startswith("sk_test_"):
        return None  # Stripe — not an LLM key
    if key.startswith("sk_"):  # novita / elevenlabs-ish
        if re.fullmatch(r"sk_[a-f0-9]{32,}", key):
            return "elevenlabs"
        return "novita"
    if re.fullmatch(r"[a-f0-9]{32}\.[A-Za-z0-9]{16,}", key):
        return "zhipu"
    return None


def _looks_like_junk_key(key: str) -> bool:
    """Reject common false positives that caused mass 401/404 noise."""
    kl = key.lower()
    # English / version junk caught by loose prefixes
    if kl.startswith("co-") and any(x in kl for x in ("coord", "order", "content", "config", "cookie")):
        return True
    if re.search(r"-20[0-9]{2}\b", key):  # …e-2022 certificate-like
        return True
    if key.startswith("vo-"):  # not a Voyage prefix; was junk
        return True
    if key.startswith("di_"):  # invented DeepInfra prefix; junk
        return True
    if key.startswith(("sk_live_", "sk_test_", "pk_live_", "pk_test_")):
        return True
    return False


"""Provider-specific labelled-secret extraction for opaque API keys.

Opaque vendors often have no stable prefix. We only accept these when the
surrounding configuration explicitly names the provider, avoiding arbitrary
20+ character strings being treated as credentials.
"""
_LABELED_PROVIDER_PATTERNS = {
    "nous": re.compile(r"(?i)(?:NOUS|NOUSRESEARCH)(?:_API)?_KEY\s*[:=]\s*([A-Za-z0-9._~+/=-]{16,512})"),
    "cohere": re.compile(r"(?i)COHERE(?:_API)?_KEY\s*[:=]\s*([A-Za-z0-9._~+/=-]{16,512})"),
    "deepinfra": re.compile(r"(?i)DEEPINFRA(?:_API)?_(?:KEY|TOKEN)\s*[:=]\s*([A-Za-z0-9._~+/=-]{16,512})"),
    "sambanova": re.compile(r"(?i)SAMBANOVA(?:_API)?_KEY\s*[:=]\s*([A-Za-z0-9._~+/=-]{16,512})"),
    "nebius": re.compile(r"(?i)NEBIUS(?:_API)?_KEY\s*[:=]\s*([A-Za-z0-9._~+/=-]{16,512})"),
    "hyperbolic": re.compile(r"(?i)HYPERBOLIC(?:_API)?_KEY\s*[:=]\s*([A-Za-z0-9._~+/=-]{16,512})"),
    "ai21": re.compile(r"(?i)AI21(?:_API)?_KEY\s*[:=]\s*([A-Za-z0-9._~+/=-]{16,512})"),
    "minimax": re.compile(r"(?i)MINIMAX(?:_API)?_KEY\s*[:=]\s*([A-Za-z0-9._~+/=-]{16,512})"),
    "mistral": re.compile(r"(?i)MISTRAL(?:_API)?_KEY\s*[:=]\s*([A-Za-z0-9._~+/=-]{16,512})"),
    "together": re.compile(r"(?i)TOGETHER(?:AI)?(?:_API)?_KEY\s*[:=]\s*([A-Za-z0-9._~+/=-]{16,512})"),
    "siliconflow": re.compile(r"(?i)SILICONFLOW(?:_API)?_KEY\s*[:=]\s*([A-Za-z0-9._~+/=-]{16,512})"),
    "moonshot": re.compile(r"(?i)(?:MOONSHOT|KIMI)(?:_API)?_KEY\s*[:=]\s*([A-Za-z0-9._~+/=-]{16,512})"),
    "fireworks": re.compile(r"(?i)FIREWORKS(?:_API)?_KEY\s*[:=]\s*([A-Za-z0-9._~+/=-]{16,512})"),
    "groq": re.compile(r"(?i)GROQ(?:_API)?_KEY\s*[:=]\s*([A-Za-z0-9._~+/=-]{16,512})"),
    "cerebras": re.compile(r"(?i)CEREBRAS(?:_API)?_KEY\s*[:=]\s*([A-Za-z0-9._~+/=-]{16,512})"),
    "perplexity": re.compile(r"(?i)PERPLEXITY(?:_API)?_KEY\s*[:=]\s*([A-Za-z0-9._~+/=-]{16,512})"),
    "firecrawl": re.compile(r"\bfc-[A-Za-z0-9_-]{16,}\b"),
    "browser_use": re.compile(r"\bbu_[A-Za-z0-9_-]{20,}\b"),
    "browserless": re.compile(r"\bbless_[A-Za-z0-9_-]{12,}\b"),
    "apify": re.compile(r"\bapify_api_[A-Za-z0-9_-]{20,}\b"),
    "exa": re.compile(r"\bexa_[A-Za-z0-9_-]{16,}\b"),
    "brightdata": re.compile(r"\bbrd_[A-Za-z0-9_-]{16,}\b"),
    "pinecone": re.compile(r"\bpcsk_[A-Za-z0-9_-]{16,}\b"),
    "fal": re.compile(r"\bfal_[A-Za-z0-9_-]{16,}\b"),
    "modal": re.compile(r"\bak-[A-Za-z0-9_-]{12,}\b"),
    "openrouter": re.compile(r"(?i)OPENROUTER(?:_API)?_KEY\s*[:=]\s*([A-Za-z0-9._~+/=-]{16,512})"),
}
_LABELED_HINTS = tuple(x.pattern.split("\\s")[0].replace("(?i)", "") for x in _LABELED_PROVIDER_PATTERNS.values())

# Only capture candidates that already look like real key prefixes (avoid matching every quoted string in ULP dumps)
_ASSIGN_PATTERNS = [
    re.compile(
        r"(?i)(?:api[_\-]?key|api[_\-]?token|access[_\-]?token|secret[_\-]?key|"
        r"openai|anthropic|openrouter|authorization|bearer|x-api-key|"
        r"password|credential)[\s\"']*[:=][\s\"']*"
        r"(sk-or-v1-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,}|"
        r"sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{20,}|"
        r"AIza[A-Za-z0-9_-]{20,}|gsk_[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{20,}|"
        r"r8_[A-Za-z0-9]{20,}|pplx-[A-Za-z0-9_-]{20,}|xai-[A-Za-z0-9_-]{20,}|"
        r"fw_[A-Za-z0-9]{20,}|csk-[A-Za-z0-9]{20,}|nvapi-[A-Za-z0-9_-]{20,}|"
        r"jina_[A-Za-z0-9_]{20,}|tvly-[A-Za-z0-9_-]{20,}|pa-[A-Za-z0-9_-]{20,}|"
        r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"
    ),
    re.compile(
        r"(?i)(?:authorization\s*[:=]\s*)?bearer\s+"
        r"(sk-or-v1-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,}|"
        r"sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{20,}|gsk_[A-Za-z0-9]{20,})"
    ),
]


def _normalize_populated_text(text: str) -> str:
    """Make keys findable inside dumps: smart quotes, escaped JSON, URI encoding."""
    if not text:
        return ""
    t = text
    # smart quotes → ascii
    t = t.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    t = t.replace("\u00a0", " ")
    # common JSON escapes that break sk-or-v1
    t = t.replace("\\/", "/").replace("\\n", "\n").replace("\\t", "\t")
    # light URL-decoding for sk-%2D… style (only safe chars)
    if "%" in t:
        try:
            from urllib.parse import unquote
            t2 = unquote(t)
            # only keep if it didn't explode size wildly
            if len(t2) < len(t) * 3:
                t = t2
        except Exception:
            pass
    return t


def _line_containing(text: str, index: int) -> str:
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    if end < 0:
        end = len(text)
    line = text[start:end].strip()
    if len(line) > 300:
        # keep key vicinity
        rel = index - start
        a = max(0, rel - 80)
        b = min(len(line), rel + 120)
        line = ("…" if a else "") + line[a:b] + ("…" if b < len(line) else "")
    return line


def _register_hit(
    found: List[tuple],
    seen: set,
    key: str,
    provider: str,
    line: str,
) -> None:
    key = key.strip().strip("\"'`,;")
    if not key or key in seen or _looks_like_junk_key(key):
        return
    forced = _force_provider(key)
    if forced is None and key.startswith(("sk_live_", "sk_test_")):
        return
    # skip if contained in a longer already-found key
    if any(key != e and key in e for e in seen):
        return
    # drop shorter keys contained in this one
    for e in list(seen):
        if e != key and e in key:
            seen.discard(e)
            found[:] = [(k, p, ln) for k, p, ln in found if k != e]
    seen.add(key)
    found.append((key, forced or provider, line or ""))


# Precompiled strong patterns for hot path (ULP line scans)
_STRONG_PATS: List[tuple] = []


def _ensure_strong_pats():
    global _STRONG_PATS
    if _STRONG_PATS:
        return
    for pid in SCAN_ORDER:
        cfg = PROVIDERS[pid]
        if cfg.get("weak_pattern"):
            continue
        for pat in cfg.get("patterns") or []:
            _STRONG_PATS.append((pid, pat))


def detect_keys_in_line(line: str) -> List[tuple]:
    """Fast path for a single dump line (URL:login:pass:key etc)."""
    _ensure_strong_pats()
    line = line.strip()
    if not line:
        return []
    found: List[tuple] = []
    seen: set = set()
    for pid, pat in _STRONG_PATS:
        for m in pat.finditer(line):
            _register_hit(found, seen, m.group(0), pid, line[:400])
    # Opaque keys: only accept them when the variable name explicitly identifies
    # the provider. This adds coverage without scanning arbitrary long strings.
    for pid, pat in _LABELED_PROVIDER_PATTERNS.items():
        for m in pat.finditer(line):
            _register_hit(found, seen, m.group(1), pid, line[:400])

    # Assignment forms for strongly identifiable prefixes.
    if any(x in line.lower() for x in ("api", "key", "token", "bearer", "password", "openrouter", "openai")):
        for ap in _ASSIGN_PATTERNS:
            for m in ap.finditer(line):
                cand = m.group(1)
                pid = detect_provider_for_key(cand)
                if pid:
                    _register_hit(found, seen, cand, pid, line[:400])
    return found


def find_keys_in_content(content: str) -> List[tuple]:
    """v2 scanner: run compiled regexes over the whole file with finditer.

    Same hot path as v2 ScanWorker (read file → pattern.finditer(content)).
    Weak/generic patterns are skipped so ULP dumps are not flooded with junk.
    Returns [(key, provider_id, context_line), ...].
    """
    if not content:
        return []
    _ensure_strong_pats()
    found: List[tuple] = []
    seen: set = set()
    for pid, pat in _STRONG_PATS:
        for m in pat.finditer(content):
            _register_hit(found, seen, m.group(0), pid, _line_containing(content, m.start()))
    return found


def detect_keys_in_text(text: str) -> List[tuple]:
    """Return list of (key, provider_id, context_line) from free text / populated dumps.

    Finds keys standing alone OR embedded in JSON, .env, Bearer headers, CSV, logs.
    For huge files prefer line-loop + detect_keys_in_line (workers do this).
    """
    text = _normalize_populated_text(text)
    found: List[tuple] = []
    seen: set = set()
    _ensure_strong_pats()

    _STR_MARKERS = (
        "sk-or-v1-", "sk-proj-", "sk-ant-", "AIza", "gsk_", "hf_", "r8_",
        "pplx-", "xai-", "fw_", "csk-", "nvapi-", "jina_", "tvly-", "pa-",
        "ghp_", "github_pat_", "sk_", "sk-",
    )
    for line in text.splitlines():
        low = line.lower()
        labelled = any(h.lower() in low for h in _LABELED_HINTS)
        if not labelled and not any(m.lower() in low for m in _STR_MARKERS):
            continue
        for key, pid, ctx in detect_keys_in_line(line):
            _register_hit(found, seen, key, pid, ctx or line[:400])

    return found


def detect_provider_for_key(key: str) -> Optional[str]:
    forced = _force_provider(key)
    if forced:
        return forced
    best = None
    best_pri = -1
    for pid, cfg in PROVIDERS.items():
        if cfg.get("weak_pattern"):
            continue
        for pat in cfg.get("patterns") or []:
            m = pat.fullmatch(key) or pat.search(key)
            if m and m.group(0) == key:
                if cfg["priority"] > best_pri:
                    best_pri = cfg["priority"]
                    best = pid
    return best


def all_provider_ids() -> List[str]:
    return list(PROVIDERS.keys())


def detectable_provider_ids() -> List[str]:
    """Providers that auto-detect from text (strong key shape)."""
    return [pid for pid, cfg in PROVIDERS.items() if (cfg.get("patterns") and not cfg.get("weak_pattern"))]


def discovery_order(exclude: Optional[List[str]] = None) -> List[str]:
    """Candidate providers to try for a key whose shape matched nothing.

    These are the entries with no usable regex (opaque keys: Cohere, DeepInfra,
    SambaNova, Nebius, Hyperbolic, AI21, MiniMax). Without this pass those vendors
    can never be identified OR enriched — they were simply unreachable in v3.
    Highest priority first so the more specific vendors are tried before generic ones.
    """
    skip = set(exclude or [])
    out = []
    for pid in SCAN_ORDER:
        cfg = PROVIDERS[pid]
        if pid in skip:
            continue
        if cfg.get("patterns"):
            continue  # already covered by detection
        if not cfg.get("validate") or cfg.get("discoverable") is False:
            continue
        out.append(pid)
    return out


def alternative_candidates(key: str, exclude: Optional[List[str]] = None) -> List[str]:
    """Other providers whose key shape ALSO describes this key.

    `sk-[A-Za-z0-9]{32}` is claimed by DeepSeek, but Moonshot, SiliconFlow,
    DashScope, Stability and others use overlapping `sk-…` shapes. v3 assigned one
    provider and never reconsidered, which is why ~100 DeepSeek-labelled keys all
    reported "$0 / not available" — several are almost certainly a different vendor.
    Providers with an unmistakable prefix (sk-or-v1-, sk-ant-, AIza, gsk_…) are
    never re-homed.
    """
    skip = set(exclude or [])
    forced = _force_provider(key)
    unmistakable = {"openrouter", "anthropic", "google", "groq", "huggingface",
                    "replicate", "perplexity", "xai", "fireworks", "cerebras",
                    "github", "nvidia", "jina", "tavily", "langsmith", "voyage",
                    "firecrawl", "browser_use", "browserless", "apify", "exa",
                    "brightdata", "pinecone", "fal", "modal"}
    if forced in unmistakable:
        return []
    out: List[str] = []
    for pid in SCAN_ORDER:
        if pid in skip or pid in unmistakable:
            continue
        cfg = PROVIDERS[pid]
        if not cfg.get("validate"):
            continue
        for pat in cfg.get("patterns") or []:
            m = pat.fullmatch(key) or pat.search(key)
            if m and m.group(0) == key:
                out.append(pid)
                break
    return out


def count_by_provider(records) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in records:
        pid = getattr(r, "provider", None) or "?"
        out[pid] = out.get(pid, 0) + 1
    return out
