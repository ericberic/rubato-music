# Source Detail: Matchmaker Evaluation

## Source

- Parent source note: [Matchmaker](matchmaker.md)
- Paper: https://www.carloscancinochacon.com/documents/peer_reviewed/ParkEtAl-ISMIR-2025.pdf

## Evaluation Setup

The ISMIR 2025 paper evaluates real-time alignment on:

- `(n)ASAP`: expressive solo piano performances from MAESTRO with alignments.
- `Batik`: Mozart sonata recordings by one pianist.
- `Vienna4x22`: 22 renditions of four easier pieces.

The study uses 177 performances, 77 pieces, 58,846 beats, 247,035 notes, and
7.74 hours of performance data. Evaluations were simulation-based for
reproducibility.

## Metrics

- Alignment rate within thresholds from 50 ms to 2000 ms.
- Average and median absolute error.
- Beat-domain error.
- Skewness/kurtosis of errors.
- System latency excluding hardware I/O.

## Results

- OLTW methods outperform HMM across datasets in accuracy and coverage.
- OLTW Arzt has consistently stronger total alignment rate than OLTW Dixon.
- HMM can linger in local regions because of sticky state behavior.
- Log-spectral energy had the best reported feature tradeoff: 241.85 ms MAE and
  0.91 ms feature latency.
- OLTW Arzt alignment latency was reported as 0.07 ms; HMM alignment latency was
  3.59 ms.

Representative performance-domain median errors:

- `(n)ASAP`: OLTW Arzt 91.18 ms; HMM 346.01 ms.
- `Batik`: OLTW Arzt 107.15 ms; HMM 641.77 ms.
- `Vienna4x22`: OLTW Arzt 152.51 ms; HMM 319.13 ms.

Representative beat-domain median errors:

- `(n)ASAP`: OLTW Arzt 0.16 beats; HMM 0.66 beats.
- `Batik`: OLTW Arzt 0.18 beats; HMM 0.67 beats.
- `Vienna4x22`: OLTW Arzt 0.24 beats; HMM 0.51 beats.

## Rubato Caveat

The strongest published results are audio-based. Rubato still needs a
MIDI-input evaluation on the chosen Chopin excerpt.

## Related

- [Matchmaker](matchmaker.md)
- [Score Following](../concepts/score-following.md)
