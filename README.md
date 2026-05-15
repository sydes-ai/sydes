# Sydes

## What is Sydes?

Sydes helps you understand how API requests flow through your backend — directly from code.

It reconstructs routes, follows internal calls (even across services), and surfaces side effects like database writes — without manually reading hundreds of files.

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
Works best on multi-service backends (API + worker + gateway).

## Model providers

Sydes supports local and hosted LLM providers. You can select a model per command with `--model`, or set environment defaults.

CLI override examples:

```bash
sydes routes --repo api=./api --model ollama:llama3.1:8b
sydes trace "/checkout" --method POST --repo api=./api --model openai:gpt-4.1-mini
sydes trace "/checkout" --method POST --repo api=./api --model anthropic:claude-3-5-sonnet-latest
```

### Ollama (local)

```bash
export SYDES_LLM_PROVIDER=ollama
export SYDES_LLM_MODEL=llama3.1:8b
export SYDES_LLM_BASE_URL=http://localhost:11434
```

### OpenAI (hosted)

```bash
export SYDES_LLM_PROVIDER=openai
export SYDES_LLM_MODEL=gpt-4.1-mini
export OPENAI_API_KEY=...
```

### Anthropic (hosted)

```bash
export SYDES_LLM_PROVIDER=anthropic
export SYDES_LLM_MODEL=claude-3-5-sonnet-latest
export ANTHROPIC_API_KEY=...
```

If a hosted provider key is missing, Sydes returns a friendly setup error before making API calls.

Hosted providers (OpenAI/Anthropic) are paid APIs and consume tokens; usage and cost depend on your selected model and prompt size.

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
This shows that a request to `/goodreads/books` in service2 calls `/db/books` in service1.

## What Sydes outputs

- API route → flow reconstruction (what actually happens inside a request)
- Internal steps and side-effect signals (e.g. database writes)
- Cross-repo API links (when one service calls another)
- Structured API test matrix suggestions
- Sydes-native JSON export for further analysis

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

- Generate runnable integration tests from inferred flows
- Deeper cross-service tracing (recursive API chains)
- Graph-based system analysis over exported artifacts
