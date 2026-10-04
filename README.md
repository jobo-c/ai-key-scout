# AI Key Scout

AI Key Scout v5.1 is a fast, local-first AI provider capability benchmark for authorized credential auditing. It discovers provider credentials, verifies authentication, tests usable models, ranks results, and builds one portable export folder for Hermes and OpenCode.

> **Authorization and safety:** Only scan and validate credentials you own or are explicitly authorized to audit. API validation can contact provider services and model tests can consume quota. Paid-model testing is opt-in.

## What it does

- Scans project directories and supported text/config files for AI credentials.
- Recognizes a broad set of AI providers and common environment-variable naming patterns.
- Deduplicates discovered credentials and identifies providers.
- Validates credentials with provider-specific API calls where supported.
- Loads persistent validation history at startup and skips credentials that were already validated; explicit recheck actions remain available.
- Shows provider/model information directly in the main results table, with an explicit full-key reveal toggle for authorized local auditing.
- Retrieves available model information when the provider exposes it.
- Separates verified free-model results from paid-model results when model metadata allows it.
- Tests real model responses instead of treating an HTTP authentication response as proof that inference works.
- Supports optional paid-model testing. Keep this disabled when you do not want to consume credits.
- Ranks working credentials by useful verification results.
- Produces reports without exposing full credentials in normal output.
- Exports the best verified credentials into environment-backed configuration for Hermes and OpenCode.

## Provider coverage

The provider registry includes, among others:

OpenRouter, OpenAI, Anthropic, Google/Gemini, Groq, DeepSeek, xAI, Mistral, Together, Fireworks, Cerebras, Perplexity, Cohere, Hugging Face, Replicate, SiliconFlow, Moonshot/Kimi, Novita, DeepInfra, Zhipu/z.ai, DashScope/Alibaba, NVIDIA, SambaNova, Jina, Voyage, Tavily, LangSmith, MiniMax, Nebius, Hyperbolic, AI21, and Nous Research / Nous Portal.

Provider coverage is intentionally extensible. Detection and validation are separate: a credential may be detected for a provider even when that provider's live validation endpoint is unavailable.

## Verification model

AI Key Scout distinguishes several useful states:

- **Detected** — a credential-like value was found.
- **Valid** — authentication was accepted by the provider.
- **Working model** — an actual model request succeeded.
- **Free working** — a verified working model is identified as free.
- **Paid working** — a verified working model is identified as paid.
- **Rate limited / quota / unavailable / invalid** — the provider response is classified where possible.

A successful authentication check does **not** automatically mean that every model on the account is usable.

### Paid testing

Paid inference testing is deliberately disabled by default. Enable it only when you own/control the account and explicitly accept possible usage charges.

## Export

The GUI can export the best verified credentials as:

- **Hermes**: `config.yaml` plus a protected `.env`
- **OpenCode**: `opencode.json` plus a protected `.env`
- **Both**: one directory containing `config.yaml`, `opencode.json`, and one shared protected `.env`
- **Full bundle**: also writes `providers.json`, `opencode.jsonc`, and `litellm.yaml` with environment-backed credentials

Secrets are referenced through environment variables rather than embedded directly into generated configuration files.

Example workflow:

1. Scan your authorized project/files.
2. Validate discovered credentials.
3. Test models.
4. Review free/paid working results.
5. Choose **Export Best**.
6. Select Hermes, OpenCode, or both.
7. Review the generated `.env` before starting the target application.

Do not commit generated `.env` files or credential/result artifacts.

## Installation

The repository intentionally does **not** require a `requirements.txt`. The platform installers install the runtime dependencies directly and can create an isolated Python virtual environment.

### Linux / Ubuntu

From the repository directory:

```bash
chmod +x install_linux.sh
./install_linux.sh
```

The installer checks Python/Git, installs CMake and build tools when needed, asks whether to create `.venv`, installs `PyQt6`, `requests`, and `PyYAML`, then runs a compilation check.

### Windows PowerShell

From the repository directory:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\\install_windows.ps1
```

The Windows installer offers `.venv` and can install CMake through `winget` when CMake is missing.

For the normal isolated setup, answer **Y** to the virtual-environment prompt.

## Running

The repository is a lightweight Python application. Use a virtual environment and install the project's dependencies before running the application.

The main entry point is:

```bash
python main.py
```

## Security notes

- Treat discovered API credentials as secrets.
- Never paste raw keys into public issues, logs, screenshots, or commits.
- Rotate/revoke credentials that have accidentally been committed or shared.
- Keep generated `.env` files local and restrict their permissions where supported.
- Review the selected export before using it with Hermes or OpenCode.
- Network validation should only be performed against accounts and systems you are authorized to test.

## Fast path

The scanner favors cheap provider-specific validation before deeper model work. Model catalogs are queried only when the provider exposes a useful catalog endpoint, model tests are concurrent, and paid inference remains opt-in. Keep `models_per_key` small for fast runs and increase it only when you need broader capability coverage.

### Export layout

A combined export is intentionally flat:

```text
ai-key-scout-export/
├── config.yaml
├── opencode.json
└── .env
```

The same `.env` is shared by Hermes and OpenCode, while both configuration files reference environment variables. The `schemas/` directory contains machine-readable JSON Schemas for provider definitions, scan results, queries, and the combined export.

## Development

Useful areas of the codebase:

- `providers.py` — provider registry and credential detection
- `modeltest.py` — model discovery and inference verification
- `ranking.py` — result ranking
- `report.py` — report generation
- `history.py` — validation history
- `gui.py` — graphical workflow
- `export_config.py` — Hermes/OpenCode export
- `workers.py` — background model-testing workers
- `schemas/` — JSON Schemas for providers, queries, results, and exports

Before opening a pull request, test the scanner and model verification against safe test credentials or mocked provider responses. Never add real credentials to fixtures.

## License

See the repository license file for the project's licensing terms.

## v5 security model

v5 is designed around **authorized credential auditing** and keeps credentials out
of normal reports and logs.

- Stable SHA-256 fingerprints identify credentials without storing the raw value in reports.
- UI/report output uses masked credentials such as `sk-a…1234`.
- Generated credential/result files are ignored by Git.
- `working_keys.json` and similar generated credential artifacts are intentionally local-only.
- Paid model inference remains opt-in because it can consume account credits.
- Provider authentication and model capability testing are reported separately.
- CI compiles the project and runs the security regression tests.

### v5 result model

A verified result can now distinguish:

`discovered → provider identified → authenticated → model tested → free/paid capability → ranked`

This makes the project useful as a provider capability benchmark for authorized
Hermes/OpenCode configuration rather than only a key detector.
