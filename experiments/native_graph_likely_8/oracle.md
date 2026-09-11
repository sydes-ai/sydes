# Frozen oracle — 8 known production-Likely cases

Frozen BEFORE any experimental graph code ran. Source: exact `accepted_impacts`
entries from the actual `sydes-result.json` artifacts already produced by the
last real runs of PR6 and PR7 (not re-run for this experiment), plus direct
manual reading of the real repository source at each PR's own branch. Do not
edit this file after Phase 1 begins.

Repo checkouts used (read-only, local): `/Users/ksnaik/sample_repos/Kokoro-FastAPI`,
branch `py-real-01-max-output-duration` for cases 1–6, branch
`py-real-02-normalizer-voice-maintenance` for cases 7–8.

---

### 1. `GET /dev/model`
- Canonical impact id: `flow:GET:/dev/model`, `status: inferred`
- `changed_symbols`: `["generate_audio", "generate_audio_stream"]`
- Handler: `model_status` (`api/src/routers/development.py`)
- **Oracle: FALSE.** `model_status` calls only
  `tts_service.model_manager.status()`. It never calls, references, or
  imports `TTSService.generate_audio`/`generate_audio_stream`.

### 2. `POST /dev/generate_from_phonemes`
- Canonical impact id: `flow:POST:/dev/generate_from_phonemes`, `status: inferred`
- `changed_symbols`: `["generate_audio", "generate_audio_stream"]`
- Handler: `generate_from_phonemes` (`api/src/routers/development.py`)
- **Oracle: FALSE.** Calls `tts_service.generate_from_phonemes(...)` — a
  distinct `TTSService` method that internally calls
  `backend._get_pipeline(...).generate_from_tokens(...)`, never
  `generate_audio`/`generate_audio_stream`.

### 3. `POST /dev/reload`
- Canonical impact id: `flow:POST:/dev/reload`, `status: inferred`
- `changed_symbols`: `["generate_audio", "generate_audio_stream"]`
- Handler: `reload_model` (`api/src/routers/development.py`)
- **Oracle: FALSE.** Calls only `tts_service.model_manager.reload()`.

### 4. `POST /dev/unload`
- Canonical impact id: `flow:POST:/dev/unload`, `status: inferred`
- `changed_symbols`: `["generate_audio", "generate_audio_stream"]`
- Handler: `unload_model` (`api/src/routers/development.py`)
- **Oracle: FALSE.** Calls only `tts_service.model_manager.unload()`.

### 5. `Settings` (`impact:app:Settings`)
- Canonical impact id: `impact:app:Settings`, `status: inferred`,
  `route_method: null`, `route_path: null` (whole-change level, not
  route-anchored by the artifact itself)
- `changed_symbols`: `["(whole change)", "Settings"]`
- **Manually verified real relation (exact, read from source at this PR's
  branch):**
  `Settings.max_output_duration_s` (`api/src/core/config.py:59`, new field)
  → read as `settings.max_output_duration_s` inside `_within_duration_ceiling`
  (`api/src/structures/schemas.py:73,76`)
  → `_within_duration_ceiling` is the `AfterValidator` of the `Duration` type
  alias (`schemas.py:81-82`)
  → `Duration` is the type of `max_duration_seconds` on `OpenAISpeechRequest`
  (`schemas.py:252`)
  → `OpenAISpeechRequest` is the `request:` parameter type of `create_speech`
  (`api/src/routers/openai_compatible.py`)
  → `create_speech` is the handler for `POST /audio/speech`.
- **Oracle: TRUE**, reachable from `POST /audio/speech`. The relation is
  attribute-read → validator-body → type-alias-argument → field-type →
  parameter-type → route-handler. No step is a function call.

### 6. `_within_duration_ceiling` (`impact:app:_within_duration_ceiling`)
- Canonical impact id: `impact:app:_within_duration_ceiling`, `status: inferred`,
  `route_method: null`, `route_path: null` (whole-change level)
- `changed_symbols`: `["(whole change)"]`
- **Manually verified real relation:** the same chain as case 5, minus its
  first hop — `_within_duration_ceiling` → `Duration` (type-alias argument)
  → `OpenAISpeechRequest.max_duration_seconds` (field type) →
  `create_speech` (parameter type) → `POST /audio/speech` (route handler).
- **Oracle: TRUE**, reachable from `POST /audio/speech`.
- **Known mechanism, recorded for scoring only — not to be encoded into the
  algorithm:** this is Pydantic's `Annotated[..., AfterValidator(fn)]`
  wiring. The experiment must discover the *generic* shape (a symbol named
  as an argument inside a type-alias/annotation assignment, which is itself
  used as a field's type, which is itself used as a parameter's type) —
  never a rule keyed on the literal names `Annotated`, `AfterValidator`, or
  `_within_duration_ceiling`.

### 7. `convert_audio` (`AudioService.convert_audio`)
- Canonical impact id:
  `impact:app:home-runner-work-Kokoro-FastAPI-Kokoro-FastAPI.api.src.services.audio.AudioService.convert_audio`,
  `status: inferred`, `kind: decorated`, `route_method: null`, `route_path: null`
  (not route-anchored by the artifact; `changed_symbols`:
  `["_speak_url_symbols", "handle_email", "handle_url"]` — i.e. this impact
  was proposed BECAUSE of proximity to the changed normalizer functions, not
  because of any discovered call relation to them)
- File: `api/src/services/audio.py`, class `AudioService`
- **Manually verified real relation:** `convert_audio` is called from
  `TTSService._process_chunk` (`api/src/services/tts_service.py`) as a
  downstream **audio-encoding** step, operating on already-generated audio
  samples. The changed normalizer functions (`handle_url`/`handle_email`/
  `_speak_url_symbols`) are called from `normalize_text` →
  `text_processor.smart_split`, an **upstream text-preprocessing** step in
  the same overall `generate_audio_stream` pipeline. Both are reachable from
  `POST /audio/speech`, but neither calls, is called by, or otherwise
  references the other — they are sibling stages of one pipeline, not
  caller/callee.
- **Oracle: FALSE** — no relation between the changed symbols and
  `convert_audio` exists; only entrypoint-proximity, which is not evidence.

### 8. `trim_audio` (`AudioService.trim_audio`)
- Canonical impact id:
  `impact:app:home-runner-work-Kokoro-FastAPI-Kokoro-FastAPI.api.src.services.audio.AudioService.trim_audio`,
  same shape as case 7.
- **Manually verified real relation:** identical situation to case 7 —
  `trim_audio` is a sibling downstream audio-encoding step called from the
  same `TTSService._process_chunk`, unrelated to the changed normalizer
  functions.
- **Oracle: FALSE**, same reasoning as case 7.

---

## Summary table (frozen)

| # | Candidate | Oracle | Entrypoint used for testing |
|---|---|---|---|
| 1 | `GET /dev/model` → `generate_audio_stream` | FALSE | `GET /dev/model` |
| 2 | `POST /dev/generate_from_phonemes` → `generate_audio_stream` | FALSE | `POST /dev/generate_from_phonemes` |
| 3 | `POST /dev/reload` → `generate_audio_stream` | FALSE | `POST /dev/reload` |
| 4 | `POST /dev/unload` → `generate_audio_stream` | FALSE | `POST /dev/unload` |
| 5 | `Settings.max_output_duration_s` | TRUE | `POST /audio/speech` |
| 6 | `_within_duration_ceiling` | TRUE | `POST /audio/speech` |
| 7 | `AudioService.convert_audio` | FALSE | `POST /audio/speech` |
| 8 | `AudioService.trim_audio` | FALSE | `POST /audio/speech` |
