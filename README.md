# Sydes

Sydes is an AI-assisted system understanding tool for tracing API flows from code.
Sydes now performs a first-pass API endpoint discovery flow before deeper tracing.

## Current status (Phase 2)

- Shallow repo sensing and file inventory are implemented.
- Candidate file ranking and bounded selective file reads are implemented.
- First-pass endpoint discovery pipeline is wired behind `routes` and used by `trace` target resolution.
- Run artifacts are persisted under `~/.sydes/` (workspace-scoped JSON files).

## Commands available now

```bash
sydes routes --repo api=./api
sydes trace "/checkout" --method POST --repo api=./api
```

## Near-term roadmap

Next tracing phases are focused on:

- Downstream flow tracing expansion
- Sink detection
