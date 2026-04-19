# Sydes

Sydes is an AI-assisted system understanding tool for tracing API flows from code.
Phase 2 supports local endpoint discovery with Ollama and trace target resolution from discovered routes.

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
sydes trace "/checkout" --method POST --repo api=./api
```

## Current capability

- Endpoint discovery works locally via Ollama on a bounded candidate file set.
- Route matching works for grounding `trace` targets to discovered endpoints.
- Downstream flow tracing and sink detection are next.

## Artifacts

Sydes saves run artifacts under `~/.sydes/`.
