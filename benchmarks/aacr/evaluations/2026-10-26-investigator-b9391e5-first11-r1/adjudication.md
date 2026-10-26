# Manual adjudication: unmatched CONFIRMED findings

Run: `2026-10-26-investigator-b9391e5-first11-r1` (Sydes `b9391e5`, `--investigator`).

Scope: only the Investigator findings that were CONFIRMED and that AACR's judge
did not match to any reference comment. UNKNOWN and REFUTED hypotheses are not
adjudicated. Each finding was checked against the repository at the case's head
commit and against the review base (the merge base the harness used). This was
done after the Sydes findings and the AACR scores existed, and nothing from it
went back to Sydes.

Classes:
- `VALIDATED_DEFECT`: the source shows a real defect that the PR caused or exposed.
- `PLAUSIBLE_UNVERIFIED`: the claim may be right, but the evidence is not enough to confirm it by hand.
- `INVALID_FINDING`: the claim is false, unsupported, or pre-existing with no causal link.

| # | Case | Location | Class |
|---|---|---|---|
| 1 | browser-use__browser-use-1482 | `tests/test_browser.py:180` | VALIDATED_DEFECT (low severity, test reliability) |
| 2 | dbeaver__dbeaver-38239 | `plugins/org.jkiss.dbeaver.model/src/org/jkiss/dbeaver/model/struct/DBSObjectFilter.java:153` | PLAUSIBLE_UNVERIFIED |
| 3 | google-gemini__gemini-cli-8154 | `packages/cli/src/utils/commentJson.ts:58` | VALIDATED_DEFECT |
| 4 | google-gemini__gemini-cli-8154 | `packages/cli/src/utils/commentJson.ts:113` | VALIDATED_DEFECT |

## 1. browser-use-1482: the new custom-port test mocks the wrong HTTP client

**Claim.** The new test does not reliably exercise the launch branch. It
monkeypatches `requests.get`, but the probe it is meant to intercept uses httpx.

**Evidence.**
- The PR adds `test_user_provided_browser_launch_on_custom_chrome_remote_debugging_port` (+77 lines in `tests/test_browser.py`).
- That test calls `monkeypatch.setattr(requests, 'get', dummy_get)`.
- `browser_use/browser/browser.py` at `0b21e50` probes `/json/version` with `httpx.AsyncClient().get` (lines 200–202 and 241–243). It never calls `requests.get`.

**Consequence.**
- The mock is never used, and the outcome depends on the machine running the test.
- If any service answers on `localhost:9223`, the code takes the reuse branch. The `create_subprocess_exec` assertion then never runs, and the test still passes.
- If nothing answers, the post-launch loop sleeps through about 10 failed probes before connecting.

**Verdict.** This is a defect in code the PR added, so it is caused by the PR. Severity is low.

## 2. dbeaver-38239: `matches(String)` → `matches(String...)` breaks binary compatibility

**Claim.** Plugins compiled against the old `DBSObjectFilter.matches(String)` will
fail at run time with `NoSuchMethodError`.

**Evidence.**
- The diff replaces `public synchronized boolean matches(String name)` with `matches(String... names)`.
- No single-`String` overload remains at head.
- The JVM descriptor changes from `(Ljava/lang/String;)Z` to `([Ljava/lang/String;)Z`.
- `org.jkiss.dbeaver.model.struct` is exported in `plugins/org.jkiss.dbeaver.model/META-INF/MANIFEST.MF`.

**Why not validated.** The mechanism is verified, but the impact is not.
- Every in-repo caller is recompiled together.
- Nothing in the repository says precompiled out-of-tree plugins are a supported compatibility target.

The claim's impact may therefore be stronger than its evidence. This is close to a
CLAIM issue.

## 3. gemini-cli-8154: removing an MCP server leaves it in the settings file

**Claim.** `gemini mcp remove` reports success, but the server is still in an
existing settings file.

**Evidence.**
1. `commands/mcp/remove.ts` runs `delete mcpServers[name]`, then `settings.setValue(scope, 'mcpServers', mcpServers)`.
2. `setValue` → `saveSettings(settingsFile, envMappings)` → `updateSettingsFilePreservingFormat(path, settingsToSave, envVarMappings)`.
3. `updateSettingsFilePreservingFormat` (new in this PR; `commentJson.ts` doesn't exist at the review base) parses the existing file and calls `applyUpdates(parsed, updates)`.
4. `applyUpdates` iterates only `Object.entries(updates)` and merges object values recursively into the parsed object (lines 58–77). A key that was deleted from `updates` is never visited, so it survives in the parsed object and is written back.
5. At the review base, `saveSettings` wrote `JSON.stringify(settingsToSave, null, 2)` whole, so the removal took effect.

**Verdict.** This is a regression caused by the PR.

## 4. gemini-cli-8154: a setting backed by an env-var reference can't be replaced with a new literal

**Claim.** After load, a `$VAR`-backed path is restored to `$VAR` on every save,
even when `setValue` has written a new literal string there.

**Evidence.**
- Mappings are recorded only at load (`settings.ts:672–682`, via `trackEnvVarMappings`).
- `setValue` reuses `this.envVarMappings.get(scope)` unchanged.
- `restoreEnvVarsRecursive` replaces any string at a mapped path with `mapping.originalValue`. It never compares that string against `resolvedValue` (`commentJson.ts:114–116`).
- So setting `"new-literal"` at a path that was loaded from `"$API_KEY"` writes `"$API_KEY"` back.
- Both the mapping and the restoration are new in this PR.

**Verdict.** This is a defect caused by the PR.

## Result

- Validated useful findings = AACR matched (5) + VALIDATED_DEFECT unmatched (3) = **8**.
- Plausible but unverified: 1. Invalid: 0.
- This count is separate from AACR precision and recall.
- The baseline's 4 unmatched findings were not adjudicated, so there is no matching product-view number for Sydes 0.4.0.
