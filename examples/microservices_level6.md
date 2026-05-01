# microservices-level6 Example

## Setup

```bash
git clone https://github.com/ksnaik/microservices-level6.git ~/sample_repos/microservices-level6
```

## Discover routes across repos

```bash
uv run sydes routes --repo service1=~/sample_repos/microservices-level6/service1 --repo service2=~/sample_repos/microservices-level6/service2
```

Expected snippet (abridged):

```text
Sydes Routes Discovery
Routes discovered: 2
```

## Trace cross-repo link

```bash
uv run sydes trace "/goodreads/books" --method GET --repo service1=~/sample_repos/microservices-level6/service1 --repo service2=~/sample_repos/microservices-level6/service2
```

Expected snippet (abridged):

```text
Matched endpoint:
  - GET /goodreads/books

Cross-Repo Links:
  - service2 -> service1::GET /db/books
```

Note: Output can vary slightly across local models and prompt context.

