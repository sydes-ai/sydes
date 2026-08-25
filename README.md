# Sydes

## What is Sydes?

Sydes uses code-graph evidence and AI reasoning to show what a backend change could affect — and what still hasn't been verified.

It reconstructs routes, follows internal calls (even across services), and surfaces side effects like database writes — without manually reading hundreds of files. For a specific code change, `sydes verify-change` (below) reports what it could prove reaches an affected behavior, what it could only infer, and what remains unverified — it does not guarantee complete coverage, full system testing, or universal framework support.

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

## Change verification

Analyze what a backend change actually touches, against a base branch:

```bash
sydes verify-change --base main
```

By default the current directory is the changed repo; pass `--repo name=path` to point elsewhere, and repeat it to let Sydes resolve calls that cross a service boundary:

```bash
sydes verify-change --base main \
  --repo service2=~/repos/service2 \
  --repo service1=~/repos/service1
```

Sydes reports:

- the changed files and the **symbols** the diff hunks actually land in
- **affected system flows** — the routes and event consumers that reach those symbols, and the databases, outbound clients, and events they reach in turn, each edge carrying the source line it was inferred from
- **verification** — for each affected behavior, the existing tests that cover it are **executed**, and the behavior is reported as `passed`, `failed`, `unverified`, or `unknown`
- **verification gaps** — system behaviors the change may alter with no located evidence
- **runtime requirements** — what would have to be running to exercise the flow (Sydes does not provision, mock, or contact anything)
- **cross-repo impact** — outbound boundaries, resolved to a sibling repo when one is configured

### Verification states

| State | Meaning |
| --- | --- |
| `passed` | A mapped test was executed and succeeded. |
| `failed` | A mapped test was executed and failed. |
| `unverified` | No existing test was found that can verify this behavior. |
| `unknown` | A test exists but could not be run or interpreted — missing dependency, absent runner, unsupported framework, collection error, or timeout. |

A test being present is never reported as `passed`. Infrastructure problems are
never reported as `failed`; they become `unknown` with a named blocker, linked
to the runtime dependency behind them where one was discovered.

### PROVEN vs. INFERRED impact

Separately from test-verification state, each affected behavior Sydes reports
carries how it was found:

| Status | Meaning |
| --- | --- |
| `PROVEN` | Reached by a deterministic call/route path Sydes actually traced. |
| `INFERRED` | Proposed by an AI reasoning pass over code-graph evidence, with no traced deterministic path. Shown with a confidence score and a reason. |

`INFERRED` findings are evidence to investigate, not proof — they can never by
themselves produce a `VERIFIED` verdict. This AI pass (`--impact-guide`) is
optional and requires the `cbm` code-intelligence backend
(`SYDES_CODE_INTELLIGENCE=cbm`); on its first use, Sydes bootstraps the
`codebase-memory-mcp` native runtime into a local cache, which can take a
noticeable moment the very first time.

The verdict follows directly:

```text
any behavior failed                       -> ACTION REQUIRED
anything unverified or unknown            -> VERIFICATION INCOMPLETE
every affected behavior passed            -> VERIFIED
```

Only tests already mapped to affected behavior are executed. Sydes targets a
single case where it can (`pytest tests/test_app.py::test_add_item`), widens to
the file when it cannot, and records which granularity it achieved. It runs no
suite-wide command, installs nothing, and loads no `.env` file.

Supported runners: pytest, unittest, jest, mocha, and `node --test` — each only
when a repository file (`pyproject.toml`, `pytest.ini`, `setup.cfg`,
`package.json`, …) proves it is configured. An unidentifiable setup is `unknown`,
never a guessed command.

Useful flags:

```bash
sydes verify-change --base main --json result.json   # structured artifact for CI/PR tooling
sydes verify-change --base main --code-review        # also run the advisory LLM code-findings pass (off by default)
sydes verify-change --base main --llm-policy never   # deterministic analysis only, no model calls
sydes verify-change --base main --impact-guide auto  # AI semantic impact inference for unresolved impact (cbm backend only)
sydes verify-change --base main --verbose            # per-edge evidence, runner output, diagnostics
sydes verify-change --base main --no-run-tests       # map tests but do not execute them
sydes verify-change --base main --test-timeout 30    # per-test process timeout (default 120s)
```

The command runs non-interactively and reads no terminal state, so it works unchanged inside GitHub Actions. `--json` writes the same `ChangeVerificationResult` model the terminal renderer consumes; a run is also saved as a `change_verification` artifact under `~/.sydes/`.

Uncommitted work is included by default so a change can be analyzed before it is committed; pass `--no-working-tree` for committed changes only.

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

## Output path contract

`--output` supports either an explicit file path or an artifact directory.

Routes:
- `--output path/to/routes.json` writes that file.
- `--output path/to/run_dir` writes `path/to/run_dir/routes.json`.

Trace:
- `--output path/to/run_dir` writes:
  - `trace_result.json`
  - `trace_graph.json`
  - `test_matrix.json` (when generated)
  - `flow_expansion.json` (when generated)
- `--output path/to/trace.json` writes a single trace result JSON file.

## Build outputs

Build Python package artifacts:

```bash
uv build
```

This produces:
- `dist/sydes-*.whl`
- `dist/sydes-*.tar.gz`

Build a standalone executable for the current OS/arch:

```bash
uv run python scripts/build_binary.py
```

This produces:
- `dist/binaries/<platform-key>/sydes`
- on Windows: `dist/binaries/<platform-key>/sydes.exe`

Platform keys follow:
- `darwin-arm64`
- `darwin-x64`
- `linux-x64`
- `win32-x64`

Cross-platform binaries should be built on the target OS/architecture (or via CI runners for each target).

## Current limitations

- Local model quality can vary by model choice, prompt fit, and hardware/runtime conditions.
- Flow traces are inferred from code context, not runtime traces or full execution capture.
- Large repositories are explored selectively (bounded candidate ranking and file reads), not exhaustively.
- Framework-specific behavior is not guaranteed in V1.
- Cross-repo linking currently works for detectable internal API-call patterns and remains shallow.
- OSS export format is Sydes-native JSON for now; GraphML is not exported yet.
- `verify-change` attributes changes to symbols for Python and JavaScript/TypeScript only; other languages fall back to the enclosing route declaration region.
- `verify-change` executes only the tests it already mapped to affected behavior; it never runs the full suite, and a behavior with no mapped test stays `unverified`.
- Test execution uses a repository virtualenv or `node_modules/.bin` when present and otherwise `python3`; it never installs dependencies, so an unprepared environment yields `unknown`.
- `verify-change` reports runtime requirements but never provisions, mocks, or contacts them.

## Roadmap

- Generate runnable integration tests from inferred flows
- Deeper cross-service tracing (recursive API chains)
- Graph-based system analysis over exported artifacts
