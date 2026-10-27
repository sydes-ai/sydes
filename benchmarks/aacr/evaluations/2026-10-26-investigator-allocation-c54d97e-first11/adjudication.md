# Adjudication: unmatched CONFIRMED findings, allocation experiment (all arms)

This is a post-hoc, read-only check of each finding against the PR's base..head diff, using `git show`/`git diff` in `~/.cache/sydes-aacr/repos`. Nothing from it went back to Sydes. Only CONFIRMED findings that the AACR judge left unmatched are adjudicated. When a claim has the same file, mechanism and verdict basis as one adjudicated in an earlier run, that verdict is reused.

| Claim | Arms where it is unmatched | Verdict | Basis |
|---|---|---|---|
| browser-use-1482 (`0b21e50`) `tests/test_browser.py:179`: the new test mocks `requests.get`, but the probe uses `httpx`, so the launch assertion can be skipped | A2, C2 | **VALIDATED_DEFECT** | Reused from the b9391e5 run, finding #1. Same mechanism and quotes (`browser.py:200`, `:204`, `test_browser.py:180`). |
| gemini-cli-8154 `commentJson.ts:58`: `mcp remove` leaves the server in the persisted file, because `applyUpdates` only visits keys present in the updates | A1, A2, C1, C2 | **VALIDATED_DEFECT** | Reused from the b9391e5 run, finding #3. |
| react-33059 (`be58f26`) `ReactDOMForm-test.js:1673`: the PR commits `it.only`, so the file's other tests don't run | A1, C1 | **VALIDATED_DEFECT** | New, checked here. Line 9 of the base..head diff adds `+  it.only('multiple form submissions in rapid succession do not throw', ...)`. At head it is the only `.only` in the file (line 1673). Under Jest, `it.only` skips every sibling test in the file, which removes the form regression suite from CI for this file. The PR caused it. Severity is low to medium (test coverage). |
| dbeaver (`ed40132`) `DBSObjectFilter.java:153`: `matches(String)` → `matches(String...)` breaks binary compatibility for precompiled plugins | C2 | **PLAUSIBLE_UNVERIFIED** | Reused from the b9391e5 run, finding #2. The diff (lines 17–18) confirms the signature change and `MANIFEST.MF` exports the package. The impact on out-of-tree binaries isn't established. |

INVALID_FINDING: 0 in every arm.

## Validated useful per arm (AACR matches + VALIDATED_DEFECT)

| Arm | Matched | VALIDATED_DEFECT | Validated useful | PLAUSIBLE_UNVERIFIED | INVALID |
|---|---|---|---|---|---|
| A1 | 4 | 2 | **6** | 0 | 0 |
| A2 | 5 | 2 | **7** | 0 | 0 |
| C1 | 5 | 2 | **7** | 0 | 0 |
| C2 | 7 | 2 | **9** | 1 | 0 |
