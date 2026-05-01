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

- Flow and linking are inferred from bounded code context, not full program execution.
- Results are useful but can be partial and uncertain.
- Cross-repo linking is shallow (no recursive distributed trace expansion yet).
- Test matrix output is heuristic; runnable framework-specific test generation is future work.
- OSS export format is Sydes JSON only for now (no GraphML/richer interchange yet).

## Roadmap

- Runnable framework-specific test generation from matrix suggestions
- Deeper recursive multi-repo trace expansion
- Richer graph analysis over exported Sydes artifacts
