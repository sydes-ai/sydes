"""Oracle-free Sydes structured-context text per case, extracted directly
from fresh `verify-change` runs (results/*_sydes_result.json) -- counts and
names only, no verdict/gt language, no expected-path text."""

GO_CONTEXT = """## Sydes structural facts (from Sydes' own deterministic analysis, no LLM)

- Changed symbols attributed to this diff: ProcessTaskSendVerifyEmail, SendEmail, newTestRedisTaskProcessor, randomVerifyEmailUser, newSendVerifyEmailTask, TestProcessTaskSendVerifyEmail
- Sydes' deterministic route scan found 7 HTTP/gRPC-gateway routes in this repo (frameworks detected: express, express_mount_graph pattern matching -- Sydes is a generic tool, not Go-specific).
- known_entrypoints=7, http_entrypoints=0 route flows resolved to any of the 6 changed symbols above.
- graph_slice: seeds=6 (the changed symbols), routes=0 -- none of the 7 known routes were reachable from these seeds in Sydes' own call/usage graph.
- 0 accepted impacts (proven or inferred) were produced for this change.
- 0 mapped tests, 0 verification obligations.
- Sydes explicitly reports: "no route flow resolved to them" for all 6 changed symbols.
"""

JAVA_CONTEXT = """## Sydes structural facts (from Sydes' own deterministic analysis, no LLM)

- Changed symbols attributed to this diff: parseJWT, setUp, buildJwt, parseJWT_currentTokenMatchesRedis_succeeds, parseJWT_previousTokenMatchesWithinGraceWindow_succeeds, parseJWT_tokenMatchesNeitherCurrentNorPrevious_throwsTokenOutOfCtrl
- One accepted impact was produced: status=proven, label="doFilterInternal".
- Sydes' summary: "6 changed symbol(s); no route flow resolved to them" (beyond the one proven impact above).
- Full verification requires (external dependencies Sydes flagged): Redis, SQL database, Kafka, RabbitMQ, Elasticsearch, Mail/SMTP service.
"""

RUST_CONTEXT = """## Sydes structural facts (from Sydes' own deterministic analysis, no LLM)

- Sydes attributed 0 changed symbols to this diff (could not name any symbol the change belongs to).
- Sydes' deterministic route scan found 0 routes in this repository (deterministic_frameworks=none).
- 0 files were indexed with any symbols at all (files_indexed=0, symbols=0) by Sydes' own structural indexer for this repository.
- 0 accepted impacts were produced.
- Sydes' summary: "0 changed symbol(s); no route flow resolved to them."
"""
