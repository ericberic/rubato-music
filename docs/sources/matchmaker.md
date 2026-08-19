# Source: Matchmaker

## Source

- Repository: https://github.com/pymatchmaker/matchmaker
- Docs: https://pymatchmaker.readthedocs.io/en/latest/
- Peer-reviewed ISMIR 2025 paper:
  https://www.jku.at/fileadmin/gruppen/173/Research/Rach_3_Project/ParkEtAl-ISMIR-2025.pdf
- DOI record: https://doi.org/10.5281/zenodo.17811551

## Date Read

2026-07-19 (paper metadata and scope rechecked)

## Why It Matters

Matchmaker is a newer open-source Python package for real-time music alignment.
It is likely a cleaner integration target than the full ACCompanion application.

## Key Facts

- Package name is `pymatchmaker`.
- Supports Python 3.10-3.13 according to current README/docs.
- Provides a high-level `Matchmaker` class.
- Supports live and simulation modes.
- Accepts score files such as MusicXML and MIDI through Partitura.
- Supports audio and MIDI input.
- Current README lists MIDI methods: `arzt`, `dixon`, `outerhmm`, `hmm`, and
  `pthmm`.
- ISMIR 2025's large audio benchmark across (n)ASAP, Batik, and Vienna4x22
  found the Arzt OLTW variant strongest and onset-sensitive LSE stronger than
  chroma. The paper frames Matchmaker as an evaluation/integration library,
  not a new learned follower.

## Design Lessons For Rubato

- Use Matchmaker as the first integration target because it already exposes
  score positions in beats and accepts MusicXML/MIDI scores.
- Start with simulation mode before live Yamaha tests.
- Rubato's first real-package test uses offline MIDI simulation through
  `run_matchmaker_midi_file`, then converts Matchmaker's alignment path into
  `FollowerUpdate` rows.
- Prefer OLTW variants for accuracy, but test MIDI methods directly on Rubato's
  target excerpt.
- Use Matchmaker's `BytesMidiStream` path if routing Web MIDI through the
  existing FastAPI/WebSocket stack.
- Keep Rubato's internal interface follower-agnostic: a follower should yield
  `(score_beat, perf_time, confidence?)` so Matchmaker can be swapped later.

## Caveats

- Live device support requires optional dependencies such as `python-rtmidi`.
- For Rubato's Yamaha MIDI path, an integration spike must verify MIDI follower
  behavior directly.
- The peer-reviewed ISMIR 2025 benchmark is audio-based even though the package
  also exposes MIDI processors. Its ranking cannot be transferred to Yamaha
  MIDI without Rubato's own causal replay and hardware evaluation.
- The package requires FluidSynth for score synthesis in some flows and PortAudio
  for live audio-device support.
- Rubato should still standardize on Python 3.11/3.12 until the local live stack is verified.

## Links

- Detail page: [Matchmaker Mechanics](matchmaker-mechanics.md)
- Detail page: [Matchmaker Evaluation](matchmaker-evaluation.md)
- Related concept: [Matchmaker Real Follower Tests](../concepts/matchmaker-real-follower-tests.md)
- Related concept: [Score Following](../concepts/score-following.md)
- Related runbook: [First Chopin MVP Excerpt](../runbooks/chopin-mvp.md)
- Related runbook: [Yamaha MIDI Setup](../runbooks/yamaha-midi.md)
