# Index backend spike — codebase-memory-mcp

Evaluation only. Nothing here is imported by `sydes`; normal execution is untouched.

Evaluated: `DeusData/codebase-memory-mcp` v0.10.8 (MIT), darwin-arm64 release binary,
sha256 verified against the published `checksums.txt`.

The external binary, its 1.2 GB source clone, and its SQLite indexes are **not** vendored
or committed. The binary lives outside the repo; indexes live in
`~/.cache/codebase-memory-mcp/<project>.db`.

Run: `python3 harness.py --cbm /path/to/codebase-memory-mcp --repo /path/to/repo`

Findings are in the task report. The two that decide the recommendation:

1. Incremental updates are lossy. For identical source, a cold index of
   school-portal-api yields 1051 edges; an in-place incremental update of one file
   yields 1041. The 9 lost edges are exactly the cross-file `CALLS` edges from that
   file's handlers (`Depends(get_db)`, `Depends(get_current_user)`).
2. One-shot CLI costs ~4.8 s per invocation (daemon bootstrap), against 0.04 ms to
   read the same fact directly from its SQLite.
