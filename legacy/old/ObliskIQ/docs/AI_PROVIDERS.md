# AI Providers — failover chain

ObliskIQ's two AI steps (Phase 2 proposals, Phase 4 decisions) run through an ordered
chain of providers. If one fails, the next one answers. **You control the order and the
model names** from `config/operator_panel.toml`; nothing else needs a code change.

| Provider | Free tier (checked 2026-10) | Key (GitHub secret / `.env`) | Default model |
|---|---|---|---|
| `gemini` | Flash models only — **Pro is paid-only since April 2026**. Limits are per *project*, not per key | `GEMINI_API_KEY` | `gemini-3.8-flash` |
| `openrouter` | `:free` models: 20 req/min, 50 req/day (1,000/day after ever buying ≥ $10 credit) | `OPENROUTER_API_KEY` | `nvidia/nemotron-3-super-120b-a12b:free` |
| `nvidia` | NIM developer tier, ~40 req/min | `NVIDIA_API_KEY` | `nvidia/llama-3.1-nemotron-70b-instruct` (v6.16.5; the previous default was retired 2026-10-03) |
| `groq` | ~8K tokens/min, 200K/day — **smaller than one empty prompt** (see below) | `GROQ_API_KEY` | `openai/gpt-oss-120b` |

You need **at least one** key. A provider with no key is skipped.

## Change the order or a model

Edit `config/operator_panel.toml` (everything is commented out by default):

```toml
[ai]
provider_order = ["gemini", "nvidia", "openrouter"]   # left to right; leave one out to disable it
nvidia_model   = "meta/llama-3.3-70b-instruct"        # swap a model ID (free IDs rotate)
```

A typo in a provider name fails loudly at startup. A non-blank env var
(`AI_PROVIDER_ORDER`, `GEMINI_MODEL`, …) wins over the panel; a *blank* one is ignored.

## What makes the chain move on to the next provider

- no API key set, or the key is rejected
- the configured model ID does not exist (the message names the setting to change)
- rate limit / quota (429) or server error after one quick retry
- an empty, blocked, or unusable reply
- a reply that breaks the Phase 2 / Phase 4 JSON contract even after one retry with the error appended
- the prompt cannot fit that provider's input window (skipped without calling it)

Phases 2 and 4 are routed independently. If every provider fails, the run **fails loudly**
and lists each provider's reason — it never invents decisions. Every scoreboard row is
stamped `provider:model` of whoever actually answered, so a fallback model's accuracy is
graded separately from the primary's.

## Groq and "degraded" providers

The fixed persona + schema of Phase 2/4 is ~9–10K tokens, more than Groq's free window, so on
the free tier Groq is always skipped with an explicit reason. Any provider whose input window
is below 100K tokens (`FULL_CONTEXT_MIN_TOKENS`) is **degraded**: it sees a heavily pruned
briefing, so a run it answers may hold or exit positions but **never opens new ones**.
Window sizes live in `modules/config/ai_providers.py` on purpose (code review, not the panel).

## Check it before you rely on it

```
python scripts/preflight.py           # confirms each configured model EXISTS (free)
python scripts/preflight.py --probe   # also sends one tiny real request per provider
```
