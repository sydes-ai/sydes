# Sydes

Sydes is an AI-assisted system understanding tool for tracing API flows from code.
Phase 3 supports first real route -> flow -> sink tracing using bounded code context and local Ollama inference.

## Quick Ollama setup

1. Install Ollama and start the local server (`ollama serve`).
2. Pull a local model (example): `ollama pull llama3.1:8b`.
3. Optionally set env vars (defaults shown):
   - `SYDES_LLM_PROVIDER=ollama`
   - `SYDES_LLM_MODEL=llama3.1:8b`
   - `SYDES_LLM_BASE_URL=http://localhost:11434`

## Commands available now

```bash
sydes routes --repo api=./api
sydes trace "/users" --method POST --repo api=./api
```

## Current capability

- Discover API routes from bounded, selectively ranked files.
- Match a requested target route to discovered endpoint candidates.
- Infer one likely downstream flow from matched endpoint + nearby contextual files.
- Detect major sink types (database, external API, queue, file sink).
- Export graph-backed trace results (`nodes`, `edges`, `flows`) via terminal or JSON.

Flow tracing is inferred from code and bounded context. Results are partial but useful, not full architecture reconstruction.

## Current scope

- Bounded selective exploration rather than deep repo-wide parsing.
- Partial but useful inferred flows with explicit uncertainty.
- Local artifacts stored under `~/.sydes/`.

## Near-term roadmap

- Cross-repo linking for multi-service flows.
- Integration test suggestions from traced key flows.
- Richer graph analysis on top of exported trace structure.

## Artifacts

Sydes saves run artifacts under `~/.sydes/`.
