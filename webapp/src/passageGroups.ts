import type { CoverageMeasure, TakeResponse } from './generated';

export type PassageTakeGroup = {
  key: string;
  entryMeasure: number | null;
  startMeasure: number | null;
  endMeasure: number | null;
  takes: TakeResponse[];
};

function measureAtBeat(
  scoreBeat: number,
  measures: CoverageMeasure[],
): CoverageMeasure | null {
  return (
    measures.find(
      (measure) => scoreBeat >= measure.start_beat && scoreBeat < measure.end_beat,
    ) ?? null
  );
}

/**
 * Return the musical entry the performer selected for a recording.
 *
 * The selected cue/placement is stronger than the first matched onset: a
 * pickup, a rolled chord, or a few settling notes can legitimately align just
 * before the passage the performer meant to rehearse. Older/free recordings
 * have no intent anchor, so their aligned start is the honest fallback.
 */
export function takeEntryMeasure(
  take: TakeResponse,
  measures: CoverageMeasure[],
): number | null {
  const intendedBeat =
    take.cue?.kind === 'from_position'
      ? take.cue.target_beat
      : take.placement_hint?.target_beat;
  if (intendedBeat !== undefined) {
    const intendedMeasure = measureAtBeat(intendedBeat, measures);
    if (intendedMeasure) return intendedMeasure.measure;
  }
  return take.score_span ? take.score_span.start.measure_index + 1 : null;
}

/** Group the library by musical entry, never by global recording order. */
export function groupTakesByEntry(
  takes: TakeResponse[],
  measures: CoverageMeasure[],
): PassageTakeGroup[] {
  const grouped = new Map<string, PassageTakeGroup>();

  for (const take of takes) {
    const entryMeasure = takeEntryMeasure(take, measures);
    const key = entryMeasure === null ? 'unplaced' : `measure-${entryMeasure}`;
    const existing = grouped.get(key) ?? {
      key,
      entryMeasure,
      startMeasure: null,
      endMeasure: null,
      takes: [],
    };
    const startMeasure = take.score_span
      ? take.score_span.start.measure_index + 1
      : null;
    const endMeasure = take.score_span ? take.score_span.end.measure_index + 1 : null;
    existing.startMeasure =
      startMeasure === null
        ? existing.startMeasure
        : existing.startMeasure === null
          ? startMeasure
          : Math.min(existing.startMeasure, startMeasure);
    existing.endMeasure =
      endMeasure === null
        ? existing.endMeasure
        : existing.endMeasure === null
          ? endMeasure
          : Math.max(existing.endMeasure, endMeasure);
    existing.takes.push(take);
    grouped.set(key, existing);
  }

  for (const group of grouped.values()) {
    group.takes.sort(
      (left, right) =>
        Date.parse(right.recorded_at) - Date.parse(left.recorded_at),
    );
  }

  return [...grouped.values()].sort((left, right) => {
    if (left.entryMeasure === null) return 1;
    if (right.entryMeasure === null) return -1;
    return left.entryMeasure - right.entryMeasure;
  });
}

/**
 * Count kept, aligned recordings crossing each printed measure. This is the
 * score-side answer to "how many passes do I have here?" and intentionally
 * differs from a group's entry measure.
 */
export function takeCountsByMeasure(
  takes: TakeResponse[],
  measures: CoverageMeasure[],
): Record<number, number> {
  const counts: Record<number, number> = {};
  for (const take of takes) {
    if (
      take.status !== 'aligned' ||
      take.disposition === 'discarded' ||
      !take.score_span
    ) {
      continue;
    }
    const startBeat = take.score_span.start.score_beat;
    const endBeat = take.score_span.end.score_beat;
    for (const measure of measures) {
      if (measure.start_beat < endBeat && measure.end_beat > startBeat) {
        counts[measure.measure] = (counts[measure.measure] ?? 0) + 1;
      }
    }
  }
  return counts;
}
