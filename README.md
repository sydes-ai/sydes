# Sydes

Sydes is an AI-assisted system understanding tool for tracing API flows from code.
Phase 5 supports route -> flow -> sink tracing, integration test suggestions, and shallow cross-repo API linking.

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
sydes trace "/checkout" --method POST --repo gateway=./gateway --repo api=./api --repo worker=./worker
```

## Current capability

- Discover API routes from bounded, selectively ranked files.
- Match a requested target route to discovered endpoint candidates.
- Infer one likely downstream flow from matched endpoint + nearby contextual files.
- Detect major sink types (database, external API, queue, file sink).
- Connect likely internal API calls across multiple repos when detectable.
- Suggest integration tests from traced flow and sink evidence.
- Export graph-backed trace results (`nodes`, `edges`, `flows`) via terminal or JSON.

Flow tracing is inferred from code and bounded context. Results are partial but useful, not full architecture reconstruction.

Example trace command:

```bash
sydes trace "/users" --method POST --repo api=./api
```

Example output (abridged):

```text
Flow:
  1. endpoint: /users
  2. step: create User object
  3. step: db.add
  4. step: db.commit

Sinks:
  - database: write

Suggested Tests:
  - post_users_creates_record
    validate primary route behavior from inferred flow and sink evidence
    expects: request succeeds with expected response
    expects: created data is persisted
```

## Current scope

- Bounded selective exploration rather than deep repo-wide parsing.
- Partial but useful inferred flows with explicit uncertainty.
- Shallow cross-repo linking from detectable internal API call patterns.
- No full recursive distributed tracing yet.
- Suggested tests are structured and heuristic, not runnable framework-specific test files.
- Local artifacts stored under `~/.sydes/`.

## Near-term roadmap

- Runnable framework-specific test generation from structured suggestions.
- Deeper recursive multi-repo trace expansion.
- Richer graph analysis on top of exported trace structure.

## Artifacts

Sydes saves run artifacts under `~/.sydes/`.

## Artifact Export

You can export saved run artifacts as Sydes-native JSON:

```bash
sydes export ~/.sydes/workspaces/<workspace-id>/artifacts/<run-id>/trace_result.json
```

- Export output is Sydes JSON for OSS users at this stage.
- GraphML and richer interchange formats are not exposed yet.
