# AI Key Scout

AI Key Scout is a local-first tool for discovering, validating, testing, ranking, and exporting AI-provider API credentials from files and project directories.

> **Authorization and safety:** Only scan and validate credentials you own or are explicitly authorized to audit. API validation can contact provider services and model tests can consume quota. Paid-model testing is opt-in.

## What it does

- Scans project directories and supported text/config files for AI credentials.
- Recognizes a broad set of AI providers and common environment-variable naming patterns.
- Deduplicates discovered credentials and identifies providers.
- Validates credentials with provider-specific API calls where supported.
- Retrieves available model information when the provider exposes it.
- Separates verified free-model results from paid-model results when model metadata allows it.
- Tests real model responses instead of treating an HTTP authentication response as proof that inference works.
- Supports optional paid-model testing. Keep this disabled when you do not want to consume credits.
- Ranks working credentials by useful verification results.
- Produces reports without exposing full credentials in normal output.
- Exports the best verified credentials into environment-backed configuration for Hermes and OpenCode.

## Provider coverage

The provider registry includes, among others:

OpenRouter, OpenAI, Anthropic, Google/Gemini, Groq, DeepSeek, xAI, Mistral, Together, Fireworks, Cerebras, Perplexity, Cohere, Hugging Face, Replicate, SiliconFlow, Moonshot/Kimi, Novita, DeepInfra, Zhipu/z.ai, DashScope/Alibaba, NVIDIA, SambaNova, Jina, Voyage, Tavily, LangSmith, MiniMax, Nebius, Hyperbolic and AI21.

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
- **Both**: a directory containing both configurations

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

Before opening a pull request, test the scanner and model verification against safe test credentials or mocked provider responses. Never add real credentials to fixtures.

## License

See the repository license file for the project's licensing terms.