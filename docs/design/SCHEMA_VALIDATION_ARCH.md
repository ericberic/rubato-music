# Schema & Validation Architecture

**Status:** Implemented. #85 (backend artifact/event schemas) landed in PR
[#86](https://github.com/ericberic/Rubato/pull/86). #83 and #84 (the
frontend contract pipeline + runtime validation) landed together as one PR
per this doc's own §4 sequencing note.

**Provenance:** Root-cause architecture review (2026-07-11) of the
whack-a-mole null/NaN-guard pattern that surfaced during PR
[#82](https://github.com/ericberic/Rubato/pull/82)'s five-round review.
Code references are against `origin/main` at `220fe48` (post-#82).

---

## 1. Diagnosis

### 1.1 One-sentence root cause

The system has exactly **one** validated data boundary — FastAPI request
ingress — while its other four boundaries (persisted JSON artifacts read
back from disk, WebSocket events, frontend API-response parsing, and the
hand-written TypeScript mirror types) all pass raw dicts / `any`, so every
consumer compensates locally with its own null/NaN/`isinstance` guard.

The fix is the classic *parse, don't validate* discipline: convert data to
a typed, validated object **once**, at the boundary where it enters the
process, and let all interior code trust the type. Every scattered guard is
a symptom of a boundary that never got a parser.

### 1.2 Boundary-by-boundary audit

| Boundary | Today | Verdict |
|---|---|---|
| HTTP request ingress (client → FastAPI) | Pydantic v2 models with `Field` constraints in `src/aimusic/server/schemas.py`; bad input → 422 | ✅ Healthy — this is the model for everything else |
| HTTP response egress (FastAPI → client) | `response_model=` on every route; `CoverageResponse(**data)` / `MeasureBoxesResponse(**data)` validate at the last moment | ⚠️ Validated, but only at the very end of a long untyped pipeline |
| **Persisted JSON artifacts** (`take.json`, `aligned.json`, `profile.json`, `coverage.json`) | Written from typed objects, **read back as raw `dict[str, object]`** | ❌ The main backend gap |
| **WebSocket events** (`WS /api/events`) | Hand-built dict literals at 4 publish sites; contract exists only as a hand-written TS union | ❌ No schema on either side |
| **Frontend response parsing** | `await response.json()` into `any` or `as Type` assertions; TS erased at runtime | ❌ The main frontend gap (issues #83/#84) |
| **TS type definitions** | Hand-written mirrors of the Pydantic models in `App.svelte`, `PdfCoverageOverlay.svelte`, `takeEvents.ts` | ❌ Silent drift by construction |
| True external inputs (user keystrokes, MusicXML, MIDI files) | Local guards (`cueSeconds` derivation, `attrib.get(...)`, mido error handling) | ✅ Guards are *correct* here — see §1.4 |

### 1.3 Concrete symptom inventory

**Backend — the artifact round-trip loses the type.**

`aligned.json` is written from the frozen dataclass `AlignedTake`
(`aligner.py:262`) via `asdict()`, but `store.get_aligned_result()` returns
`dict[str, object] | None`. Every consumer then re-derives structure:

- `coverage.py:347` — `result["score_start_beat"]` raw indexing (KeyError
  on a malformed file) next to `result.get("match_rate", 0.0)` (silently
  masks a missing field as "0% quality").
- `profile.py:255` — `result.get("cell_samples") or []`, then raw
  `sample["beat"]` / `sample["velocity"]` indexing inside the fold loop.
- `aligner.py:505–521` (`_validate_resolve_candidate`) —
  `existing.get("candidates") or []`, `isinstance(candidates, list)`, and a
  stringly `"start_position" not in chosen_candidate` version-skew probe.
- `routes.py:571–575` (`_take_response`) —
  `isinstance(aligned.get("candidates"), list)` plus per-item
  `isinstance(c, dict) and "start_beat" in c and "score" in c` filtering.
- `aligner.py:438–439` — `aligned.get("score_start_beat") if aligned else
  None` when publishing the alignment-done event.

`take.json` is read via `Take(**data)  # type: ignore[arg-type]`
(`store.py:167`): no field-type validation, wrong types pass silently, an
extra key is a `TypeError` crash. The dataclass and the `type: ignore` are
an admission that the read path has no parser.

Even inside `AlignedTake` the nested collections are stringly typed —
`timing_map: tuple[dict[str, float], ...]`, `candidates:
tuple[dict[str, float], ...]`, `cell_samples: tuple[dict[str, float], ...]`
— which is *why* key-presence probes exist downstream. Same for `Take.cue:
dict[str, object] | None`: the cue contract (`kind`, `target_beat`,
`cue_seconds`, `output_name` — design doc §4.1) lives only in a comment,
enforced by a hand-written dict comprehension that strips `created_at`
(`routes.py`, `stop_take_record`).

`profile.json` has **no schema at all** — `fold_takes()` returns a dict
literal and no route serves it, so nothing ever validates it. Its future
consumer is the live prior; a shape bug there will surface mid-rehearsal.

Take `status` is a set of module-level string constants (`store.py:24–29`)
compared with `==` everywhere including the frontend (`t.status ===
'ambiguous'`); a typo is invisible to every type checker in the stack.

**WebSocket events — a contract that exists only in comments.**

`events.publish()` takes `dict[str, Any]`; the three event shapes
(`take:recording_started/stopped`, `take:alignment_done`) are hand-built at
`routes.py:287`, `routes.py:544`, `routes.py:589`, `aligner.py:431`. The
only written contract is the hand-written `TakeEvent` union in
`takeEvents.ts`, applied by assertion: `JSON.parse(event.data) as
TakeEvent`. WS payloads are also invisible to FastAPI's OpenAPI spec, so
issue #83's codegen would not cover them unless we make it.

**Frontend — strict TS defeated at every boundary.**

`tsconfig.json` already has `"strict": true`; the problem is that the data
enters as `any`:

- `App.svelte:63–79` — `takes: any[]`, `coverageData: any`,
  `movement1CoverageData: any`; `playTake(take: any)`,
  `toggleDiscardTake(take: any)`, `resolveTake(take: any, ...)`.
- ~20 bare `await response.json()` sites (implicit `any`) plus `as
  MidiDevicesResponse` / `as OfflineRenderResponse` assertions.
- Hand-written mirror types: `App.svelte:8–28`,
  `PdfCoverageOverlay.svelte:23–33`, `takeEvents.ts` `TakeEvent`.
- The PR #82 whack-a-mole residue, i.e. consumers each guessing how
  defensive to be: `parseTimeOrNow()` (post-#82 `App.svelte:223–225`) and
  its two call sites' "unparseable timestamp = started now" fallback;
  `formatRecordedTime`'s `Number.isNaN(d.getTime())` fallback;
  `data.takes ?? []`; `event.duration_seconds ?? 0`;
  `summary?.percent_covered` optional chaining in the overlay.

**Type-level root of the `started_at` saga specifically:** the Pydantic
models type timestamps as `str` (`LiveStatusResponse.started_at: str |
None`, `Take.recorded_at: str`, produced by three copies of a
`strftime`-based `_utc_now()`). `str` cannot express "ISO-8601 datetime",
so the OpenAPI spec says `type: string`, so no generated type or validator
can ever check it, so every consumer re-asks the question. Typing them as
`datetime` makes Pydantic serialize ISO-8601, stamps `format: date-time`
into the spec, and lets the frontend validator enforce it — the whole class
of bug becomes unrepresentable.

### 1.4 What is *not* a symptom (guards that stay)

Guards at genuinely external, untrusted boundaries are the correct
architecture, not bandaids:

- **User keyboard input:** the `cueSeconds` derivation from
  `cueSecondsInput` (post-#82 `App.svelte:571`) and the
  `Number.parseFloat(hardwareRecordSeconds)` check — a number `<input>` can
  legitimately hold NaN mid-edit. These stay (though the derivation shrinks
  once the request goes through a generated, typed client).
- **MusicXML / MIDI parsing:** `attrib.get(...)` in `coverage.py` /
  `measure_boxes.py`, mido error handling in `store.py`. Third-party files
  are untrusted input; parse-and-validate here *is* the boundary parser.

The dividing line: **a guard on data we ourselves wrote (our artifacts, our
API's responses, our WS events) indicates a missing schema; a guard on data
the outside world wrote is the job.**

---

## 2. Decision: the stack

**Principle: Pydantic v2 is the single source of truth for every data
shape in the system — API requests/responses, persisted artifacts, and WS
events. Everything else (OpenAPI spec, TS types, Zod runtime schemas) is
generated from it. Nothing is hand-duplicated.**

### 2.1 Python: Pydantic v2 everywhere (no new backend deps)

Pydantic v2 is already a direct dependency (`pyproject.toml`) and already
guards one boundary well. Extend it to artifacts and events:

- New module `src/aimusic/takes/models.py` with frozen Pydantic models for
  the persisted contracts:
  - `TakeRecord` (take.json) — including `status:
    Literal["captured","aligning","aligned","ambiguous","unalignable","discarded"]`
    (replaces the loose string constants at the type level; constants can
    remain as the canonical spellings) and `cue: TakeCue | None` with an
    explicit `TakeCue` model (design doc §4.1 fields).
  - `AlignedResult` (aligned.json) with nested `TimingMapPoint`,
    `LocalizationCandidate` (`start_position: int | None = None` — the
    pre-existing version skew becomes an explicit optional field with one
    typed check, replacing the stringly `in` probe), `CellSample`.
  - `ProfileDoc` (+ curve-point models) and `CoverageDoc` — the builders in
    `profile.py` / `coverage.py` return models, not dict literals.
- Read path: `Model.model_validate_json(path.read_text())`; write path:
  `model_dump_json(indent=2)`. Artifact models use `extra="ignore"` so old
  readers tolerate newer writers; field types are strict.
- WS events: Pydantic models `TakeRecordingStarted`, `TakeRecordingStopped`,
  `TakeAlignmentDone` and a discriminated union `TakeEvent` (on `type`) in
  `schemas.py`; `events.publish()` accepts the model. The union is injected
  into the OpenAPI document (FastAPI's `webhooks` support, or a
  schema-components injection) so the frontend codegen covers it.
- Timestamps become `datetime` (timezone-aware) fields across
  `LiveStatusResponse`, `TakeRecord`, `CoverageDoc`, `ProfileDoc`; the three
  `_utc_now()` copies collapse to one helper returning `datetime`.
- API response models reuse the artifact sub-models (`TakeResponse` embeds
  `TakeCue`, `TakeCandidateResponse` ← `LocalizationCandidate`, coverage
  response ← `CoverageDoc` shapes) instead of re-declaring parallel shapes.

**Rejected alternatives:**
- `msgspec` — faster, but a *second* schema system alongside the Pydantic
  the API already requires; perf is irrelevant at rehearsal-app scale.
- `beartype` / `typeguard` — decorate function-call boundaries, which is
  not where the failures are; the failures are at (de)serialization
  boundaries, exactly what Pydantic owns.
- Raw JSON Schema + `jsonschema` — hand-maintained schemas are the same
  drift problem in a different file format; Pydantic emits JSON Schema for
  free when we want it.

### 2.2 The contract pipeline: OpenAPI → generated TS + Zod

- `scripts/export_openapi.py` dumps `app.openapi()` to a committed
  `webapp/openapi.json` (static export — codegen never needs a running
  server; diffs are reviewable).
- **`@hey-api/openapi-ts` with the Zod plugin** generates, in one pass:
  TS types, Zod runtime schemas, and a typed fetch client. Generated
  response validation is wired in so a response that doesn't match the
  schema **throws at the boundary** instead of leaking a malformed value
  into component code.
- CI check: regenerate and `git diff --exit-code` — a backend schema change
  that isn't regenerated fails CI; a regenerated breaking change fails
  `svelte-check`. Drift becomes structurally impossible.

**Why hey-api over the alternatives:** `openapi-typescript` alone solves
only #83 (types, erased at runtime); adding hand-written Zod for #84 would
reintroduce hand-duplication. `openapi-zod-client` couples to the Zodios
client, which is much less actively maintained. hey-api is one actively
maintained generator that produces both halves plus the client, so #83 and
#84 collapse into one pipeline. Zod's ~13 KB gzip cost is irrelevant for a
local-first app.

**Failure policy:** fail loudly at the boundary. A schema-invalid response
surfaces one error (`setMessage` + console), replacing today's per-field
silent fallbacks (`parseTimeOrNow`'s "pretend it started now",
`match_rate` defaulting to 0.0). Silent fallbacks are how five review
rounds happen: each consumer invents different corruption semantics.

### 2.3 Frontend

- TS `strict` is already on — no change needed there.
- Delete every hand-written API mirror type and every `as Type` /
  `any`-typed boundary; component code consumes generated types via the
  generated client.
- `takeEvents.ts` parses WS messages with the generated `TakeEvent` Zod
  schema instead of `as TakeEvent`.

---

## 3. Deletion scope

The point of the exercise: bandaids that come **out** when the boundaries
go in.

**Backend:**
- `store.py:167` `_take_from_dict` + `type: ignore[arg-type]` → 
  `TakeRecord.model_validate_json`.
- `store.py` / `live_control.py` duplicate `_utc_now()` strftime helpers →
  one `datetime` helper.
- `aligner.py:505–521` — `.get("candidates") or []`, `isinstance` check,
  `"start_position" not in` probe (the index-range check and the
  old-aligner error message survive as typed checks).
- `aligner.py:438–439` — `.get()` probing on the alignment-done event.
- `routes.py:571–575` — the `isinstance`/key-presence candidate filtering
  in `_take_response`.
- `routes.py` `stop_take_record` — the dict comprehension stripping
  `created_at` (bookkeeping lives outside the typed `TakeCue`).
- `coverage.py:347–349` — raw indexing + `.get("match_rate", 0.0)`.
- `coverage.py:262–264` — `int(m_data["measure"])` / `float(...)`
  re-coercions in `_rollup_measure` (typed measure-span input).
- `profile.py:255+` — `.get("cell_samples") or []` + raw sample indexing.
- `routes.py:628/699` — `CoverageResponse(**data)` /
  `MeasureBoxesResponse(**data)` last-moment re-validation becomes typed
  pass-through.

**Frontend (against post-#82 `App.svelte`):**
- Hand-written types: `App.svelte:8–28`, `PdfCoverageOverlay.svelte:23–33`,
  `takeEvents.ts` `TakeEvent` union (~60 lines).
- `takes: any[]`, `coverageData: any`, `movement1CoverageData: any`, and
  the `take: any` parameters on `playTake` / `toggleDiscardTake` /
  `resolveTake` / `discardAmbiguousTake`.
- `as MidiDevicesResponse`, `as OfflineRenderResponse`, `as TakeEvent`
  assertions; all bare `await response.json()` implicit-`any` sites.
- `parseTimeOrNow` and both call-site fallbacks; `formatRecordedTime`'s
  NaN fallback; `data.takes ?? []`; `event.duration_seconds ?? 0`;
  `summary?.percent_covered` defensive chaining.
- The bare `api()` fetch helper, replaced by the generated typed client.

**Explicitly kept:** user-input guards (`cueSeconds` derivation,
`hardwareRecordSeconds` parse) and MusicXML/MIDI parsing guards — see §1.4.

---

## 4. Issue mapping & sequencing

- **#85 (done, PR #86) — backend artifact + WS event schemas (§2.1).**
  Standalone value, no frontend dependency, and it landed first: the
  codegen in #83/#84 inherits whatever the Pydantic models say, so
  `datetime` fields, the `TakeEvent` union, and status `Literal`s needed to
  be in the spec before generation was worth wiring.
- **#83 (done) — the contract pipeline (§2.2 export + generation + CI drift
  check), deleting the hand-written TS types.**
- **#84 (done) — runtime validation wiring (Zod schemas in the client + WS
  parser), deleting the runtime guards.** With hey-api, #83 and #84 landed
  as one PR (this doc's own recommendation); kept as separate issues for
  review granularity.

  One generator-surfaced bug worth recording here: the WS event models'
  `type: Literal[...] = "..."` default made `type` *optional* in the
  emitted JSON Schema (a field with a default isn't `required`), so
  hey-api's Zod plugin generated `.optional().default(...)` for the
  discriminator. Zod v4's `discriminatedUnion` can't extract a literal
  through that wrapper, so every event's discriminator read as `undefined`
  and the very first real WS message crashed the parser with "Duplicate
  discriminator value". Fixed by making `type` a required field (no
  default) on all three event models and passing it explicitly at every
  construction site — the discriminator must always be present on the wire
  regardless, so this is the correct shape, not a workaround. See
  `src/aimusic/server/schemas.py` and `tests/test_server_schemas.py::test_type_is_required_not_defaulted`.

## 5. Testing

- Backend: round-trip tests per artifact model (write → read → equal), plus
  one malformed-file test each asserting a loud `ValidationError` (not a
  default). Existing on-disk fixtures from real takes validate unchanged —
  the models mirror the current shape exactly, so **no data migration**;
  the only intentional widening is `start_position: int | None`.
- Frontend: `svelte-check` already runs; add one test that a
  schema-invalid response rejects (guards the fail-loudly policy).
- CI: the regenerate-and-diff check from §2.2.
