# Open-Source Readiness

Status of preparing Rubato for a public release, plus the safe-publish
procedure. Last reviewed: 2026-08-18.

## Summary

| Area | Verdict |
| --- | --- |
| Secrets / credentials / tokens / keys | ✅ None found in the release tree |
| Personal data (emails, home paths, IPs, device names) | ✅ Clean — machine-local bindings live outside git |
| Source-code license | ✅ MIT (`LICENSE`, © Eric Huang) |
| Git history exposure | ⚠️ Publish from the orphan snapshot only — see below |
| Musical asset licensing | ⚠️ Blocker — third-party MIDI is not redistributable |
| Front door (README, screenshots, docs) | ✅ In good shape after this pass |

## 1. Secret / credential scan

The release snapshot (`release-v1.0.0`, an orphan single commit) was scanned for:

- High-signal secrets: AWS keys, `sk-`/OpenAI, Slack `xox*`, GitHub `ghp_`/PAT,
  Google `AIza`, JWTs, PEM private keys — **none found**.
- Assignment-style secrets (`password=`, `api_key=`, `token=`) — **none found**.
- Personal emails, `/Users/<name>` or `/home/<name>` paths, private IPs
  (10./192.168./172.16-31.) — **none found** (container paths like
  `/home/rubato`, `/home/runner` are fine).
- Built JS bundles and sourcemaps under `src/aimusic/server/static/assets/` —
  **no leaked absolute build paths**.

The only env file is `.env.example`, a safe template. The DVC remote is a
relative local path (`./dvc_remote`), not a credentialed endpoint.

Machine-local details (CoreAudio device names, plug-in paths, room calibration)
are deliberately kept out of git — they live in the application state directory,
not the repo (see `src/aimusic/audio/live_config.py`).

## 2. Git history — publish the orphan snapshot, not the working repo

`release-v1.0.0` is an **orphan commit with no parents**: a single squashed
snapshot of the tree. This is the correct release artifact because it carries
**no history** to audit or leak.

The risk is the *existing* repository. `main` and `release/public-prep` carry
250+ commits that were never audited line-by-line for secrets. **Do not simply
flip `github.com/ericberic/Rubato` to public** — that would expose all of it.

Recommended publish procedure:

1. Create a **brand-new empty** public GitHub repository.
2. Push **only** the orphan snapshot as the initial commit / default branch:
   ```bash
   git push <new-public-remote> release-v1.0.0:main
   git push <new-public-remote> vX.Y.Z   # if tagging
   ```
3. Do **not** push `main`, `release/public-prep`, `codex/*`, `claude/*`, or any
   other branch, and do **not** push the DVC cache or `./dvc_remote`.
4. Confirm the new repo shows exactly one commit and no extra branches/tags.
5. Keep the private `ericberic/Rubato` repo private as the development origin.

If instead you ever want to open the *existing* repo with history, run a
dedicated history scanner first (e.g. `gitleaks detect`, `trufflehog git`) over
all refs, and rewrite/expunge anything it finds. The orphan-snapshot route
avoids this entirely and is strongly preferred.

### Ongoing secret scanning (both repos)

Three layers keep keys out of git, public and private:

1. **Pre-commit** — `.pre-commit-config.yaml` runs gitleaks on every `git commit`
   after a one-time `pre-commit install`. This is the private-repo safety net:
   secrets are blocked before they ever land in a commit.
2. **CI** — `.github/workflows/gitleaks.yml` scans every push/PR and weekly.
   gitleaks-action is free for personal accounts and individual-owned repos.
3. **Pre-publish** — `make secrets-scan` for a full-history scan before pushing a
   snapshot to a public remote.

## 3. Musical asset licensing (blocker)

The MIT license covers **code only**. Musical assets have separate rights,
documented per bundle in `source_manifest.yaml` / `oguri_source_manifest.yaml`.

- **Chopin Op. 11 composition** — public domain. ✅
- **Joseffy two-piano reduction** (G. Schirmer, 1918, IMSLP) — public-domain
  edition, DVC-tracked, not committed to git. ✅
- **Oguri performance MIDI** (kunstderfuge.com) — permits **private,
  non-commercial use only**, with redistribution limits. **Resolved on this
  branch:** the source and everything derived from it are now removed from git
  and `.gitignore`d, so none of it ships in the public snapshot:
  - `data/scores/chopin_op11_movement_2/source/oguri_concerto_11_2.mid` (source)
  - `data/scores/chopin_op11_movement_2/derived/{solo_reference,orchestra_accompaniment}.mid`

  Only the **derivation code** is committed (`src/aimusic/accompaniment/oguri*.py`,
  CLI `extract-oguri`). Contributors fetch the source for their own private use
  and regenerate every derived file byte-for-byte with a single command:

  ```bash
  uv run python -m aimusic.cli extract-oguri --movement 2
  ```

  Tests that read these files skip cleanly when they are absent (shared guard in
  `tests/oguri_guard.py`), so the scrubbed public snapshot's `pytest` run stays
  green while local runs keep full coverage.
- **Movement-1 MuseScore export** (`assets/scores/chopin_op11_i_allegro_maestoso/source/`)
  — the composition is PD, but the manifest flags the edition/export provenance
  as unverified. Confirm the MuseScore project source before redistributing the
  committed `score.pdf` / `.mxl` / `.mid`.

### Do not ship an asset downloader

The `release-v1.0.0` snapshot carried a `scripts/download_oguri_midi.py` helper
that (a) called the kunstderfuge file **"public domain"** (it is not) and (b)
spoofed a browser `User-Agent` to fetch a rights-restricted file. That script is
**not** on this branch and should stay out of the public snapshot. Acquisition
of the private source is documented, manual, and the user's responsibility.

## 4. Front door

Done in this pass (on the readiness branch; fold into the release snapshot):

- Fixed the broken Python badge URL (`https.img.shields.io` → `https://img...`).
- Replaced the fake "60-Second Demo" badge (it linked to the repo, not a video)
  with a real screenshot gallery.
- Fixed the Mermaid diagram (`\n` renders literally on GitHub → `<br/>`).
- Added a screenshot hero + gallery using `docs/screenshots/*.jpg`.
- Added an honest **Score & Data Licensing** section and a **Project Status**
  section (alpha; live FOLLOW experimental).

Still worth doing:

- Record a real short demo video/GIF and link it (the strongest single lift for
  a performance-oriented project).
- Add `CONTRIBUTING.md` and a `CODE_OF_CONDUCT.md`.
- Add an issue/PR template under `.github/`.
- Decide whether to ship the pre-built `webapp` bundle in
  `src/aimusic/server/static/assets/` or build it on install (currently committed;
  ~4 MB of JS + sourcemaps).

## Dev model (decided)

Publish **one scrubbed snapshot** now; **no ongoing sync**. Development continues
in the private repo. The public repo is a standalone, one-time published snapshot
— not a mirror and not upstream of private work. If contributions arrive, port
accepted patches into private manually; the public repo is not kept in lockstep.

## Pre-publish checklist

- [x] Resolve derived-MIDI licensing (§3) — source + derivatives removed from
      git and `.gitignore`d; only derivation code committed.
- [x] Verify `extract-oguri` regenerates all derived files byte-for-byte.
- [x] Skip-when-absent test guards so the scrubbed snapshot's `pytest` is green.
- [x] Keep the asset downloader out of the public snapshot (§3).
- [ ] Regenerate the scrubbed orphan snapshot from this branch's tree.
- [ ] Push the orphan snapshot to a fresh public repo (§2).
- [ ] Verify the public repo has one commit, no stray branches/tags, no DVC cache.
- [x] Run a secret scan over the snapshot as a final gate (high-signal grep;
      `make secrets-scan` once gitleaks is installed).
- [x] Secret scanning wired for both repos (pre-commit + CI + `make secrets-scan`).
- [ ] (Optional) Add CONTRIBUTING / CoC / issue templates / demo video.
