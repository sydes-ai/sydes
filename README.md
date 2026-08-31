# Sydes

**Know what a code change can break — and what still hasn't been verified.**

Sydes follows backend changes beyond the diff across services, APIs, libraries, and other system boundaries. It combines structural code intelligence with AI reasoning to show what is established, what is inferred, and what remains unverified.

> **Structural analysis provides evidence. AI interprets semantics.**

## Quick start

> **PyPI package coming next.** Until the package is published, install Sydes directly from GitHub. Its runtime dependencies — including `codebase-memory-mcp` — are installed with it.

### 1. Install Sydes

Using `pip`:

```bash
python -m pip install "git+https://github.com/sydes-ai/sydes.git"
```

Or, if you use `uv`:

```bash
uv tool install "git+https://github.com/sydes-ai/sydes.git"
```

### 2. Add an LLM key

For OpenAI:

```bash
export OPENAI_API_KEY=...
```

Sydes also supports Anthropic and local Ollama models. See [Model providers](#model-providers).

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

Create:

```text
.github/workflows/sydes.yml
```

with:

```yaml
name: Sydes

on:
  pull_request:

permissions:
  contents: read

jobs:
  analyze:
    runs-on: ubuntu-latest

    steps:
      - name: Check out repository
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install Sydes
        run: |
          python -m pip install "git+https://github.com/sydes-ai/sydes.git"

      - name: Analyze PR impact
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
        run: |
          sydes verify-change \
            --base "origin/${{ github.base_ref }}" \
            --repo app=. \
            --llm-policy auto \
            --impact-guide auto \
            --no-run-tests \
            --json sydes-result.json

      - name: Upload Sydes result
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: sydes-result
          path: sydes-result.json
```

Then add your model key once:

**Repository → Settings → Secrets and variables → Actions → New repository secret**

Name it:

```text
OPENAI_API_KEY
```

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

A richer PR-comment/check integration can consume CI evidence later. The workflow above is the minimal GitHub Actions integration available today.

> After Sydes is published on PyPI, the install step becomes simply `pip install sydes`.

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

The default code-intelligence path uses `codebase-memory-mcp`, which is installed as a Sydes runtime dependency.

On first use, Sydes bootstraps the Codebase Memory native runtime into a local cache. This can take a noticeable moment once; subsequent runs reuse the local runtime/cache where possible.

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

Current limitations include:

- System-boundary discovery is still expanding beyond route-centric flows.
- Support depth varies across languages and frameworks.
- Large repositories are explored selectively rather than exhaustively.
- Cross-repository linking is currently shallow and depends on detectable structural/API relationships.
- AI-inferred impacts are hypotheses with explicit uncertainty; they are not proof.
- Test execution depends on an already prepared repository environment.
- Sydes reports runtime requirements but does not provision, mock, or contact them.
- Generalized end-to-end system verification is not solved; Sydes returns `VERIFICATION INCOMPLETE` when the evidence is insufficient.

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

