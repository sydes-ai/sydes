# Sydes

**Sydes tells you what a code change could break, what was verified, and what remains unverified.**

Sydes follows backend changes beyond the diff across services, APIs, libraries, and other system boundaries. It combines structural code intelligence with AI reasoning to show what is established, what is inferred, and what remains unverified.

> **Structural analysis provides evidence. AI interprets semantics.**

## Quick start

The fastest way to use Sydes is as a GitHub Action on your pull requests — no local install required. Prefer running it from your own terminal first? Skip ahead to [Using the CLI](#using-the-cli).

### 1. Add the Sydes workflow

Create `.github/workflows/sydes.yml`:

```yaml
name: Sydes

on:
  pull_request:
    branches: [ "main" ]   # or your default branch

permissions:
  contents: read
  pull-requests: write

jobs:
  sydes:
    uses: sydes-examples/sydes-action/.github/workflows/verify.yml@v2
    with:
      repo_alias: app
      # runtime_evidence: auto   # optional, Python: run the relevant existing tests (Beta)
    secrets:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

That's the whole workflow. Uncomment `runtime_evidence: auto` to add [Sydes Runtime
Evidence — Beta](#sydes-runtime-evidence--beta) for Python projects. It's a thin call into [sydes-examples/sydes-action](https://github.com/sydes-examples/sydes-action), a reusable, versioned workflow (`@v2`; `@v1` keeps working) maintained alongside Sydes itself — it installs the latest stable Sydes release, runs `verify-change`, and handles the PR comment, job summary, and result artifact for you. This is the same workflow file Sydes's own example repositories use in production.

### 2. Add your model key

**Repository → Settings → Secrets and variables → Actions → New repository secret**, named `OPENAI_API_KEY`.

Sydes also supports Anthropic and local Ollama models — pass `model: anthropic:claude-...` (or similar) under `with:` and add the matching secret. See [Model providers](#model-providers) for the full list and how they map when running the CLI directly.

### 3. Open a pull request

Sydes posts a single, persistent comment on the PR (updated in place on every rerun, never duplicated) summarizing what changed, what it may affect, what test evidence exists, what's still unknown, and an AI code-review pass. The same content is also written to the Actions job summary, and the full JSON result is uploaded as a build artifact.

### The check reports execution, not the verdict

The GitHub check (`Sydes verify-change`) passing means **Sydes ran successfully** — it says nothing about whether the change is fully verified. A `VERIFICATION INCOMPLETE` verdict alongside a green check is the expected, common case, not a contradiction: `verify-change` exits non-zero only on a real error (a git problem, a backend failure, a bad `--repo` value), never because of what the verdict says. Read the verdict from the PR comment, the job summary, or the uploaded JSON artifact (`summary.verdict`).

### Why tests aren't executed by default

Sydes should not try to recreate an arbitrary repository's CI environment. Your existing CI already knows how to provision dependencies, databases, queues, secrets, and services — so the reusable workflow leaves `run_tests` off by default and uses Sydes for **impact analysis** only. Pass `run_tests: true` under `with:` once your job's environment can actually install and run the repository's own tests.

The intended split is:

```text
Sydes
  → what changed?
  → what else could it affect?
  → what is structurally established?
  → what does AI infer?
  → what still lacks verification evidence?

Your CI
  → runs the repository's real tests in its real environment
```

### PR comments and job summaries

This comes built in — the reusable workflow renders `sydes-result.json` to Markdown, posts it as a PR comment upserted by a hidden marker (`<!-- sydes-verification-comment -->`) so reruns update the same comment instead of piling up duplicates, and writes the same content to the job summary. Nothing to build yourself.

The renderer and the frozen presentation contract it implements live in [sydes-examples/sydes-action](https://github.com/sydes-examples/sydes-action) (see its `CONTRACT.md` and `scripts/render_sydes_pr.py`) if you want to build your own renderer against the same `ChangeVerificationResult` JSON schema instead.

### A note on external-fork pull requests

The workflow above assumes same-repository PRs. A `pull_request` triggered by a PR from an external fork runs with a **read-only** token, so the PR-comment step will fail there — expected, not a bug.

Do not reach for `pull_request_target` to work around this without care: it runs with the base repository's permissions against the fork's (untrusted) code, and needs a deliberate secure design — typically, running the untrusted analysis in one permission-less job and only posting the comment from a separate, trusted job that never checks out or executes fork code. That split is not provided here.

---

## Using the CLI

Prefer running Sydes locally, or building your own integration instead of the GitHub Action above? Sydes is also a standalone CLI, published on PyPI. Its runtime dependencies — including `codebase-memory-mcp` — are installed with it.

### 1. Install Sydes

Sydes is still in beta — pin the exact version rather than relying on `pip`'s prerelease handling, which behaves differently depending on your other constraints and can silently skip a prerelease version entirely:

```bash
python -m pip install "sydes==0.2.0b2"
```

A plain `pip install sydes` also works, but during beta may resolve to an older non-prerelease version depending on your environment — pin the version above for a reproducible install.

For development against the latest unreleased `main`, install from GitHub instead:

```bash
python -m pip install "git+https://github.com/sydes-ai/sydes.git@main"
```

### 2. Add an LLM key

For OpenAI:

```bash
export OPENAI_API_KEY=...
```

Sydes also supports Anthropic and local Ollama models. See [Model providers](#model-providers).

This key is also what powers **AI recovery**: when a first analysis pass leaves a meaningful gap (no established path, or a changed test file with nothing mapped to it), Sydes automatically runs a second, evidence-based recovery pass over the repository before reporting its result — no flag required. Pass `--no-ai-recovery` to `verify-change` to disable it.

### 3. Run Sydes in your repository

From the repository you want to analyze:

```bash
sydes verify-change \
  --base origin/main \
  --repo app=. \
  --llm-policy auto \
  --impact-guide auto
```

On first use, Sydes prepares its code-intelligence backend automatically. The first run can take longer while the Codebase Memory runtime is bootstrapped and the repository is indexed.

### 4. Read the result

A result looks roughly like:

```text
SYDES CHANGE VERIFICATION

Risk:     MEDIUM
Verdict:  VERIFICATION INCOMPLETE
Analysis: PARTIAL

AFFECTED BEHAVIOR

PROVEN
  structurally established impact

INFERRED
  plausible downstream semantic impact

VERIFICATION
  some affected behavior still lacks verification evidence
```

The important labels are:

| Sydes says | Meaning |
| --- | --- |
| **PROVEN** | Sydes found structural evidence for the relationship or impact. |
| **INFERRED** | AI reasoning identified a plausible impact that structural evidence does not fully establish. |
| **VERIFICATION INCOMPLETE** | Some affected behavior still lacks sufficient verification evidence. |

`PROVEN` does **not** mean the code is correct. `VERIFICATION INCOMPLETE` is an intentional conservative result, not a crash.

---

## What Sydes does

Sydes starts from a code change and asks:

1. What behavior changed?
2. What other parts of the backend system can it affect?
3. Which impacts are structurally established?
4. Which additional impacts are plausible from semantics?
5. What verification evidence exists?
6. What remains unresolved or unverified?

```text
change
  ↓
structural evidence
  ↓
semantic impact
  ↓
system boundaries
  ↓
verification evidence
```

The goal is not to ask an LLM to read an entire repository. Sydes uses code intelligence to narrow the investigation first, then uses AI where semantic judgment is useful.

---

## Validated support

| Language | Support | Notes |
| --- | --- | --- |
| Python | Good | Best-supported path in current calibration. |
| Java | Good | Controller/service/interface paths work well; cross-cutting filters remain partial. |
| TypeScript | Moderate | Direct controller/service and some CQRS patterns work; deeper backward propagation limited. |
| Go | Moderate | HTTP/gRPC paths supported; async worker entrypoints limited. |
| Rust | Experimental | Partial route/flow coverage. |

This is not a universal guarantee — see [Validation](#validation) below for how it was measured.

### Validation

Sydes v0.2.0-beta.1 was exercised on 15 preregistered manual calibration cases across Python, Go, TypeScript, Java, and Rust, spanning simple, medium, and hard structural complexity.

| Complexity | Pass | Partial | Unsupported |
| --- | --- | --- | --- |
| Simple | 4 | 1 | 0 |
| Medium | 3 | 2 | 0 |
| Hard | 1 | 1 | 3 |

Full results, per-language breakdown, and methodology: [manual-v1-public-summary.md](https://github.com/sydes-examples/.github/blob/main/results/manual-v1-public-summary.md).

**This is a manual calibration suite, not an independent benchmark evaluation.**

A note on reading these results: `Pass` above is a *calibration* label meaning Sydes recovered the expected structural path and test evidence for that preregistered case. It is a different axis from the `VERIFICATION INCOMPLETE` verdict you'll see on your own real changes — that verdict commonly appears even on a `Pass` case, because it simply means tests were mapped but not executed (e.g. `--no-run-tests`, the recommended CI mode — see [Why tests aren't executed by default](#why-tests-arent-executed-by-default)). The two labels are not in tension: one describes whether Sydes found what it was calibrated to find, the other describes whether your specific run executed enough evidence to call the change fully verified.

### Showcase examples

Five real PRs from the calibration suite, chosen to show affected-path tracing, test mapping, and honest partial results — not chosen because they all pass. Full writeups: [showcase-v1.md](https://github.com/sydes-examples/.github/blob/main/results/showcase-v1.md).

- Python — [PY-M-01](https://github.com/sydes-examples/Kokoro-FastAPI/pull/3): a proven, multi-route affected-behavior trace.
- Go — [GO-M-01](https://github.com/sydes-examples/simplebank/pull/2): the honest boundary between proven and inferred evidence.
- TypeScript — [TS-M-01](https://github.com/sydes-examples/domain-driven-hexagon/pull/2): a dynamic CQRS command-bus dispatch resolved structurally.
- Java — [JAVA-M-01](https://github.com/sydes-examples/spring-boot-demo/pull/2): a clean medium-complexity result with test mapping onto previously-uncovered code.
- Rust — [RS-M-01](https://github.com/sydes-examples/Rocket/pull/2): **intentionally a partial result** — a real proven route alongside confirmed false positives, illustrating Rust's experimental support.

---

## Why

Passing tests or reviewing a diff does not establish everything a backend change may affect.

A local edit can propagate through:

- services
- APIs
- shared/internal libraries
- authentication and authorization logic
- queues and events
- persistence
- background processing

Sydes reconstructs enough system context to reason about the change instead of reviewing modified lines in isolation.

---

## Evidence model

### `PROVEN`

A relationship or impact is established by structural evidence Sydes traced.

This means the relationship is structurally grounded — **not** that the affected behavior is correct.

### `INFERRED`

AI reasoning identifies a plausible semantic impact that is not fully established by the structural path.

Inferred findings retain their uncertainty. They are evidence to investigate, not proof.

### `VERIFICATION INCOMPLETE`

Sydes does not have enough verification evidence to claim that all affected behavior has been verified.

This is deliberately conservative.

When Sydes maps verification obligations to affected behavior, individual obligations may be:

| State | Meaning |
| --- | --- |
| `passed` | A mapped test was executed and succeeded. |
| `failed` | A mapped test was executed and failed. |
| `unverified` | No existing test was found that verifies this behavior. |
| `unknown` | Relevant execution evidence could not be established, for example because of a missing dependency, unsupported runner, collection error, or timeout. |

A test merely existing is never reported as `passed`.

At a high level:

```text
affected behavior failed               → ACTION REQUIRED
anything remains unverified / unknown → VERIFICATION INCOMPLETE
all modeled affected behavior passed  → VERIFIED
```

Sydes does not guarantee complete coverage, full system testing, or universal framework support.

---

## Common usage

Every example below passes `--impact-guide auto`. That flag requires the `cbm` backend (see [Repository and code intelligence](#repository-and-code-intelligence)) — set `export SYDES_CODE_INTELLIGENCE=cbm` first, or it silently does nothing.

### Compare the current branch with `origin/main`

```bash
sydes verify-change \
  --base origin/main \
  --repo app=. \
  --llm-policy auto \
  --impact-guide auto
```

### Write a machine-readable result

```bash
sydes verify-change \
  --base origin/main \
  --repo app=. \
  --llm-policy auto \
  --impact-guide auto \
  --json sydes-result.json
```

### Analyze without executing local tests

```bash
sydes verify-change \
  --base origin/main \
  --repo app=. \
  --llm-policy auto \
  --impact-guide auto \
  --no-run-tests
```

### Analyze across multiple repositories

Repeat `--repo name=path`. The first repository is the changed repository.

```bash
sydes verify-change \
  --base main \
  --repo service2=~/repos/service2 \
  --repo service1=~/repos/service1 \
  --llm-policy auto \
  --impact-guide auto
```

### See all options

```bash
sydes verify-change --help
```

Uncommitted work is included by default. Use `--no-working-tree` when you want committed changes only.

---

## Tests and CI

Sydes determines what behavior matters; your existing CI provides the real execution evidence.

When local execution is enabled, Sydes only attempts tests it has mapped to affected behavior and only when the repository environment is already prepared.

Sydes does **not**:

- install the target repository's dependencies
- provision databases, queues, caches, or external services
- recreate arbitrary CI environments
- silently mock runtime dependencies

If the necessary environment is unavailable, Sydes reports the missing evidence conservatively rather than pretending the behavior was verified.

For most teams, the clean production model is:

```text
Sydes impact analysis
        +
existing CI execution
        ↓
verification evidence for the change
```

---

## System boundaries

A backend change does not necessarily terminate at an HTTP route.

Sydes is designed to investigate affected behavior across boundaries including:

- HTTP / APIs
- GraphQL / RPC
- shared and internal libraries
- authentication / authorization paths
- queues and events
- persistence
- background workers and scheduled jobs

Support depth varies by language, framework, and boundary. See [Current limitations](#current-limitations).

---

## Repository and code intelligence

Sydes uses repository/code intelligence so AI reasoning operates over a relevant slice of the codebase instead of blindly consuming the entire repository.

Sydes ships two code-intelligence backends: `native` (Sydes' own lightweight parser, the default) and `cbm` (the fuller `codebase-memory-mcp` code-graph backend, installed as a Sydes runtime dependency). **`--impact-guide` requires the `cbm` backend** — set `SYDES_CODE_INTELLIGENCE=cbm` to enable it; on the default `native` backend, `--impact-guide` has nothing to consult and is a no-op.

On first use with `cbm`, Sydes bootstraps the Codebase Memory native runtime into a local cache. This can take a noticeable moment once; subsequent runs reuse the local runtime/cache where possible.

The guiding principle is:

> **Make the model reason about less of the repository — but the right parts.**

Structural context can include:

- symbols and spans
- imports and exports
- call and usage relationships
- entrypoints
- nearby repository facts

AI then interprets what that evidence means for the change.

---

## Model providers

Sydes supports hosted and local LLM providers.

### OpenAI

```bash
export SYDES_LLM_PROVIDER=openai
export SYDES_LLM_MODEL=gpt-4.1-mini
export OPENAI_API_KEY=...
```

Or choose a model per command:

```bash
sydes verify-change \
  --base origin/main \
  --repo app=. \
  --model openai:gpt-4.1-mini \
  --impact-guide auto
```

### Anthropic

```bash
export SYDES_LLM_PROVIDER=anthropic
export SYDES_LLM_MODEL=claude-3-5-sonnet-latest
export ANTHROPIC_API_KEY=...
```

### Ollama

```bash
ollama serve
ollama pull llama3.1:8b

export SYDES_LLM_PROVIDER=ollama
export SYDES_LLM_MODEL=llama3.1:8b
export SYDES_LLM_BASE_URL=http://localhost:11434
```

Hosted providers consume paid API tokens. Local model quality varies by model and hardware.

---

## Advanced usage

The primary user workflow is `verify-change`. Sydes also exposes lower-level commands for inspecting route and flow structure directly.

### Trace a route

```bash
sydes trace "/users" \
  --method POST \
  --repo api=./api
```

### Discover routes

```bash
sydes routes --repo api=./api
```

### Cross-repository route tracing

```bash
sydes trace "/goodreads/books" \
  --method GET \
  --repo service1=~/sample_repos/microservices-level6/service1 \
  --repo service2=~/sample_repos/microservices-level6/service2
```

These commands are useful for deeper investigation, but they are not required for the normal PR/change-verification workflow.

### Additional `verify-change` controls

```bash
# Optional advisory AI code-review findings.
sydes verify-change --base main --repo app=. --code-review

# Disable model calls.
sydes verify-change --base main --repo app=. --llm-policy never

# More detailed evidence and diagnostics.
sydes verify-change --base main --repo app=. --verbose

# Set per-test execution timeout.
sydes verify-change --base main --repo app=. --test-timeout 30
```

### Sydes Runtime Evidence — Beta

Structural analysis says what *may* be connected. Runtime evidence adds what the repository's
own tests *actually executed*: which changed functions ran, under which tests, along which call
paths, and which changed branches were never taken. It is collected by
[DiffGenome](https://pypi.org/project/diffgenome/) (installed with Sydes on Python 3.12+;
0.1.9 or later) inside an OS sandbox. Off by default; it never changes the verdict; when the
evidence cannot be obtained the report says so instead of implying "no impact".

Supported path (Beta):

- Python projects tested with pytest (including unittest and pytest-django suites);
- automatic project detection: interpreter or environment, source and test roots, pytest
  config, import paths (`sydes runtime-detect` shows what was found and why;
  `--prepare DIR` builds an environment from the project's declared dependencies);
- automatic selection of the relevant tests: the PR's changed tests, then tests calling the
  changed functions, then one level of callers, within a fixed budget;
- tests run sandboxed: bubblewrap on Linux, Seatbelt on macOS; no network, writes only to a
  disposable copy of the repository;
- `.sydes.yml` for what cannot be inferred (service credentials, settings, a custom
  interpreter or roots).

```bash
sydes runtime-detect --base main                 # what would run, and why
sydes verify-change --base main --repo app=. --behavioral-map diffgenome --runtime-evidence auto
```

```yaml
# .sydes.yml (only when detection is not enough)
runtime:
  env: {DATABASE_HOST: 127.0.0.1}
  python: backend/.venv/bin/python
  source_roots: [backend/src]
  test_roots: [backend/tests]
  allow_loopback: true
```

In the GitHub Action (v2): `runtime_evidence: auto`. Known limitations:

- on Linux the sandbox's loopback cannot reach services running on the host (a database
  started by the job); tests that start their own localhost servers work;
- projects with a custom test-runner setup (for example a Django `TEST_RUNNER` needing
  settings) may need `.sydes.yml`;
- projects that cannot run on Python 3.12+ are not supported;
- AI recovery is separate and experimental, not part of Runtime Evidence.

Contract and design: [docs/integration/diffgenome-runtime-evidence.md](docs/integration/diffgenome-runtime-evidence.md),
[docs/integration/runtime-zero-config.md](docs/integration/runtime-zero-config.md).

### Output artifacts

`verify-change --json result.json` writes the same structured `ChangeVerificationResult` represented by the terminal renderer.

Sydes also stores local artifacts under:

```text
~/.sydes/
```

---

## Current limitations

Sydes is under active development.

- No guarantee of completeness — `VERIFICATION INCOMPLETE` means the evidence was insufficient, not that nothing was checked.
- Structural proof depends on the available code intelligence for a given language/framework; support depth varies (see [Validated support](#validated-support)).
- Some framework- or runtime-dispatched paths (async consumers, deep backward propagation, cross-cutting filters) remain partial or undetected.
- Test execution may be disabled depending on invocation, and always depends on an already-prepared repository environment — Sydes does not provision, mock, or contact runtime dependencies.

---

## Development

You only need this section if you want to work on Sydes itself.

### Clone

```bash
git clone https://github.com/sydes-ai/sydes.git
cd sydes
```

### Install development dependencies

```bash
uv sync
```

### Run Sydes from the source checkout

```bash
uv run sydes verify-change \
  --base origin/main \
  --repo app=. \
  --llm-policy auto \
  --impact-guide auto
```

### Run tests

```bash
uv run python -m pytest
```

### Build package artifacts

```bash
uv build
```

This produces the wheel and source distribution under `dist/`.

---

## Roadmap

Near-term work includes:

- broader system-boundary discovery
- deeper caller / service / library impact analysis
- richer GitHub PR and CI evidence integration
- deeper cross-service tracing
- stronger reuse of repository intelligence across changes

---

## License

MIT — see `LICENSE`.

