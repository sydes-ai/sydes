# SimpleFastPyAPI Example

## Setup

```bash
git clone https://github.com/ksnaik/SimpleFastPyAPI.git ~/sample_repos/SimpleFastPyAPI
```

## Discover routes

```bash
uv run sydes routes --repo api=~/sample_repos/SimpleFastPyAPI
```

Expected snippet (abridged):

```text
Sydes Routes Discovery
Routes discovered: 1
  - POST /users
```

## Trace flow

```bash
uv run sydes trace "/users" --method POST --repo api=~/sample_repos/SimpleFastPyAPI
```

Expected snippet (abridged):

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
```

Note: Output can vary slightly across local models and prompt context.

