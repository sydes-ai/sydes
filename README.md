# Sydes

Sydes is an AI-assisted system understanding tool for tracing API flows from code.
Phase 1 is focused on locking a practical CLI surface and internal data contracts.

## Current status (Phase 1)

- Repository scaffold is in place under `src/sydes/`.
- CLI contract for `trace` and `routes` is implemented.
- Graph-backed V1 result models are defined.
- Commands are currently stubbed (no real tracing yet).

## Commands available now

```bash
sydes trace "/checkout" --method POST --repo api=./api
sydes trace "/checkout" --method POST --repo api=./api --format json
sydes routes --repo api=./api
```

## Near-term roadmap

Real tracing behavior is upcoming and will be added incrementally:

- Endpoint discovery
- Selective flow expansion
- Sink detection
- Integration test suggestions
