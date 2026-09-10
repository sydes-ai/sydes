# Sydes

**Sydes tells you what a code change could break, what was verified, and what remains unverified.**

Sydes follows backend changes beyond the diff across services, APIs, libraries, and other system boundaries. It combines structural code intelligence with AI reasoning to show what is established, what is inferred, and what remains unverified.

> **Structural analysis provides evidence. AI interprets semantics.**

## Quick start

Sydes is published on PyPI. Its runtime dependencies — including `codebase-memory-mcp` — are installed with it.

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

## Run Sydes on every pull request

Sydes is non-interactive, so it can run directly in GitHub Actions.

Create `.github/workflows/sydes.yml`:

```yaml
name: Sydes

on:
  pull_request:
    branches: [ "main" ]   # match your default branch

permissions:
  contents: read
  pull-requests: write   # only needed to post/update the PR comment below

jobs:
  verify-change:
    runs-on: ubuntu-24.04

    steps:
      # Sydes resolves the change with `git merge-base <base> HEAD`, so both
      # the PR head and the PR base commit must be present locally.
      - name: Check out PR head
        uses: actions/checkout@v5
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v6
        with:
          python-version: "3.12"

      - name: Install Sydes
        run: |
          python -m pip install "sydes==0.2.0b2"

      - name: Run Sydes verify-change
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          # The impact guide needs the Codebase Memory backend. The default
          # backend is `native`, which would silently skip semantic
          # inference if this were left unset.
          SYDES_CODE_INTELLIGENCE: cbm
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
        run: |
          sydes verify-change \
            --base "$BASE_SHA" \
            --repo app=. \
            --llm-policy auto \
            --impact-guide auto \
            --code-review \
            --model openai:gpt-4.1-mini \
            --no-run-tests \
            --json sydes-result.json

      - name: Upload Sydes result
        if: always()
        uses: actions/upload-artifact@v5
        with:
          name: sydes-result
          path: sydes-result.json
```

Then add your model key once:

**Repository → Settings → Secrets and variables → Actions → New repository secret**, named `OPENAI_API_KEY`.

This is the minimal integration: it runs the analysis and uploads the JSON result as a build artifact. It does not post a PR comment or write a job summary — see [PR comments and job summaries](#pr-comments-and-job-summaries) to add those.

### The check reports execution, not the verdict

The GitHub check above (`verify-change`) passing means **Sydes ran successfully** — it says nothing about whether the change is fully verified. A `VERIFICATION INCOMPLETE` verdict alongside a green check is the expected, common case, not a contradiction: `verify-change` exits non-zero only on a real error (a git problem, a backend failure, a bad `--repo` value), never because of what the verdict says. Read the verdict from the uploaded JSON (`summary.verdict`) or, if you add the presentation layer below, from the PR comment.

### Why `--no-run-tests` in the example?

Sydes should not try to recreate an arbitrary repository's CI environment. Your existing CI already knows how to provision dependencies, databases, queues, secrets, and services.

The workflow above uses Sydes for **impact analysis** and leaves real test execution to your existing CI.

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

The example above only uploads a JSON artifact. To also get a persistent, human-readable PR comment and a populated Actions job summary, render `sydes-result.json` to Markdown and post it yourself — add a step after `verify-change` that:

1. runs a renderer script against `sydes-result.json` to produce a Markdown file,
2. appends that Markdown to `$GITHUB_STEP_SUMMARY`,
3. lists existing PR comments via the GitHub API, finds one containing a hidden marker comment (e.g. `<!-- sydes-verification-comment -->`), and updates it if found or creates it if not — so reruns update the same comment instead of piling up duplicates.

Sydes does not ship this renderer today; the JSON schema (`ChangeVerificationResult`) is stable and documented enough to build one against. For a complete, working reference — a renderer script, the full workflow wiring, and a reusable `workflow_call` version of it — see [sydes-examples/.github](https://github.com/sydes-examples/.github), used by Sydes's own public example repositories (e.g. [sydes-examples/Kokoro-FastAPI](https://github.com/sydes-examples/Kokoro-FastAPI)). It is a demo scaffold, not a published/versioned dependency, so treat it as a reference to copy from rather than something to depend on directly in your own repository today.

### A note on external-fork pull requests

The workflow above assumes same-repository PRs. A `pull_request` triggered by a PR from an external fork runs with a **read-only** token, so a step that tries to post a PR comment will fail there — expected, not a bug.

Do not reach for `pull_request_target` to work around this without care: it runs with the base repository's permissions against the fork's (untrusted) code, and needs a deliberate secure design — typically, running the untrusted analysis in one permission-less job and only posting the comment from a separate, trusted job that never checks out or executes fork code. That split is not provided here.

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

A note on reading these results: `Pass` above is a *calibration* label meaning Sydes recovered the expected structural path and test evidence for that preregistered case. It is a different axis from the `VERIFICATION INCOMPLETE` verdict you'll see on your own real changes — that verdict commonly appears even on a `Pass` case, because it simply means tests were mapped but not executed (e.g. `--no-run-tests`, the recommended CI mode — see [Why `--no-run-tests`](#why---no-run-tests-in-the-example)). The two labels are not in tension: one describes whether Sydes found what it was calibrated to find, the other describes whether your specific run executed enough evidence to call the change fully verified.

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

