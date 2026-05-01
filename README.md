# Sydes

## What is Sydes?

Sydes is an AI-assisted system understanding tool for tracing API flows from code.
V1 focuses on practical API route-to-flow understanding, sink detection, shallow cross-repo linking, and structured test-matrix suggestions.

## Quickstart

1. Install Ollama and start the local server:
   - `ollama serve`
2. Pull a local model (example):
   - `ollama pull llama3.1:8b`
3. Optionally set env vars (defaults shown):
   - `SYDES_LLM_PROVIDER=ollama`
   - `SYDES_LLM_MODEL=llama3.1:8b`
   - `SYDES_LLM_BASE_URL=http://localhost:11434`
4. Run Sydes:

```bash
sydes routes --repo api=./api
```

## Example 1: single-repo API flow

Trace `POST /users` in a FastAPI-style repo:

```bash
sydes trace "/users" --method POST --repo api=./api
```

Example output (abridged):

```text
Flow:
  1. endpoint: /users
  2. step: db.add
  3. step: db.commit
  4. step: db.refresh

Sinks:
  - database: write

Test Matrix:
  Happy Path:
    - post_users_creates_resource
  Validation:
    - post_users_rejects_missing_required_field
  Side Effects:
    - post_users_writes_to_database
```

## Example 2: cross-repo API link

Trace a route in one service and link an internal call to another repo endpoint:

```bash
sydes trace "/goodreads/books" --method GET \
  --repo service1=~/sample_repos/microservices-level6/service1 \
  --repo service2=~/sample_repos/microservices-level6/service2
```

Expected cross-repo section (abridged):

```text
Cross-Repo Links:
  - service2 -> service1::GET /db/books
```

## What Sydes outputs

- Route discovery and target matching output
- Inferred flow steps and sink signals
- Cross-repo API link hints when detectable
- API test matrix suggestions by category
- Sydes-native JSON export:

```bash
sydes export ~/.sydes/workspaces/<workspace-id>/artifacts/<run-id>/trace_result.json
```

Artifacts are stored locally under `~/.sydes/`.

## Current limitations

- Local model quality can vary by model choice, prompt fit, and hardware/runtime conditions.
- Flow traces are inferred from code context, not runtime traces or full execution capture.
- Large repositories are explored selectively (bounded candidate ranking and file reads), not exhaustively.
- Framework-specific behavior is not guaranteed in V1.
- Cross-repo linking currently works for detectable internal API-call patterns and remains shallow.
- OSS export format is Sydes-native JSON for now; GraphML is not exported yet.

## Roadmap

- Runnable framework-specific test generation from matrix suggestions
- Deeper recursive multi-repo trace expansion
- Richer graph analysis over exported Sydes artifacts
