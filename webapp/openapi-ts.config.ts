import { defineConfig } from '@hey-api/openapi-ts';

// Design doc docs/design/SCHEMA_VALIDATION_ARCH.md §2.2/§2.3 (issues #83/#84):
// one generation pass off the committed `webapp/openapi.json` produces TS
// types, Zod runtime schemas, and a typed fetch client whose response
// validation throws at the boundary on a schema mismatch ("fail loudly",
// §2.2) instead of leaking a malformed value into component code.
//
// Regenerate with `npm run generate` (chained after `scripts/export_openapi.py`
// via `predev`/`prebuild`, and checked for drift by `make check-types`).
export default defineConfig({
  input: './openapi.json',
  output: {
    path: './src/generated',
    // Committed, not gitignored: the CI drift check (`make check-types`) is
    // "regenerate + `git diff --exit-code`", which only works if the
    // generated output is checked in -- same pattern as `webapp/openapi.json`
    // itself already being committed.
    clean: true,
  },
  plugins: [
    {
      name: '@hey-api/typescript',
    },
    {
      name: 'zod',
      // Backend routes explicitly set `operation_id=` (src/aimusic/server/routes.py)
      // so generated Zod schema/SDK function names read as
      // `zCreateSession`/`createSession`, not FastAPI's default
      // `create_session_api_sessions_post`.
      metadata: true,
    },
    {
      name: '@hey-api/client-fetch',
      // Fail loudly at the boundary (design doc §2.2): with this off (the
      // default), a schema-invalid response's ZodError is swallowed into an
      // `{ error }` field unless each call site opts into `throwOnError`.
      // Baking it in here means every generated SDK call throws uniformly --
      // on a validation failure and on a non-2xx response alike -- so
      // component code needs exactly one catch/try path, not two.
      throwOnError: true,
    },
    {
      name: '@hey-api/sdk',
      // Call sites get the parsed response body directly (`const takes =
      // await listTakes(...)`), not a `{ data, error, request, response }`
      // envelope -- errors/validation failures are already communicated by
      // throwing (`throwOnError` above), so the envelope's `error` field
      // would always be unreachable dead code at every call site.
      responseStyle: 'data',
      validator: {
        // Requests are ours to construct -- correctness is enforced by the
        // TS type at the call site. Responses cross a real process boundary
        // (design doc §1: "parse, don't validate" -- convert at the
        // boundary, once), so only they get the runtime Zod check.
        request: false,
        response: 'zod',
      },
    },
  ],
});
