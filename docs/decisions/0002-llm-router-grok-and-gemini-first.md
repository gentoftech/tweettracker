# 0002 — 5-Provider LLM Router; Ship Grok + Gemini Adapters (Algo Diff Summaries in Build 1, Epic 5 in Build 2)

Status: accepted 2026-11-01 (updated per 0004)

## Decision

Define a provider-agnostic `LLMProvider` interface and config schema supporting 5 providers (`xai_grok`, `google_gemini`, `openai`, `anthropic`, `deepinfra`), shipping **xAI Grok (`XAI_API_KEY`)** and **Google Gemini (`GOOGLE_AI_API_KEY`)** in Build 1. Per `decisions/0004`, Build 1 uses the LLM router strictly for **Upstream `Algo Sync Watcher` Rust diff impact summaries** (with deterministic fallback if no LLM key is set), while Epic 5's LLM-heavy Pattern Attribution & Pre-Post Draft Lab are deferred to Build 2.

## What forced it

- Grok has native domain alignment with X vernacular and `xai-org/x-algorithm` semantics, while `GOOGLE_AI_API_KEY` offers near-zero cost for daily git-diff impact analysis.
- Deferring Epic 5 keeps Build 1's core graph, breakout radar, and backtester 100% deterministic (`$0` LLM runtime cost during normal polling), while keeping the 5-provider router ready for Build 2.

## Consequences

- Build 1 runs cleanly even with only `GOOGLE_AI_API_KEY` (or zero LLM keys in pure deterministic mode).
- OpenAI, Anthropic, and DeepInfra adapters remain stubbed config slots until needed.
