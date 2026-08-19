import { describe, expect, it } from 'vitest';
import type { CoverageMeasure, TakeResponse } from './generated';
import {
  groupTakesByEntry,
  takeCountsByMeasure,
  takeEntryMeasure,
} from './passageGroups';

const measures: CoverageMeasure[] = [12, 13, 14, 23, 24, 25].map((measure) => ({
  measure,
  start_beat: (measure - 1) * 4,
  end_beat: measure * 4,
  solo: true,
  scope: 'solo',
  observed: false,
  state: 'uncovered',
}));

function position(measure: number, beatInMeasure = 0) {
  const scoreBeat = (measure - 1) * 4 + beatInMeasure;
  return {
    score_tick: scoreBeat * 960,
    score_beat: scoreBeat,
    measure_index: measure - 1,
    measure_label: String(measure),
    beat_in_measure: beatInMeasure,
    source_seconds: scoreBeat,
    confidence: 0.9,
  };
}

function take(
  id: string,
  recordedAt: string,
  startMeasure: number,
  endMeasure: number,
  targetMeasure?: number,
): TakeResponse {
  return {
    take_id: id,
    piece_id: 'chopin_op11',
    movement: 2,
    recorded_at: recordedAt,
    duration_seconds: 20,
    note_on_count: 100,
    lifecycle_revision: 1,
    midi_url: `/api/takes/${id}/midi`,
    status: 'aligned',
    analysis_state: 'aligned',
    disposition: 'kept',
    profile_membership: 'included',
    placement_hint:
      targetMeasure === undefined
        ? null
        : { source: 'selected_passage', target_beat: (targetMeasure - 1) * 4 },
    score_span: {
      canonical_position: true,
      mapping_id: 'fixture',
      mapping_review_state: 'machine',
      start: position(startMeasure),
      end: position(endMeasure, 3.5),
    },
  };
}

describe('score-first passage grouping', () => {
  it('groups repeated passes by selected entry rather than global chronology', () => {
    const first = take('first', '2026-07-19T20:00:00Z', 12, 14, 12);
    const second = take('second', '2026-07-19T21:00:00Z', 12, 14, 12);
    const laterPassage = take('later', '2026-07-19T20:30:00Z', 23, 25, 23);

    const groups = groupTakesByEntry([first, laterPassage, second], measures);

    expect(groups.map((group) => group.entryMeasure)).toEqual([12, 23]);
    expect(groups[0].takes.map((item) => item.take_id)).toEqual(['second', 'first']);
    expect(groups[0].startMeasure).toBe(12);
    expect(groups[0].endMeasure).toBe(14);
  });

  it('uses performer intent before an anticipatory matched onset', () => {
    const anticipatory = take('pickup', '2026-07-19T20:00:00Z', 12, 14, 13);
    expect(takeEntryMeasure(anticipatory, measures)).toBe(13);
  });

  it('does not let a legacy from-top cue file an aligned passage at measure one', () => {
    const fromTop = {
      ...take('from-top', '2026-07-19T20:00:00Z', 12, 14),
      cue: {
        kind: 'from_top' as const,
        target_beat: 0,
        cue_seconds: 8,
        output_name: 'Clavinova',
      },
    };

    expect(takeEntryMeasure(fromTop, measures)).toBe(12);
  });

  it('counts every kept aligned pass crossing a selected score measure', () => {
    const first = take('first', '2026-07-19T20:00:00Z', 12, 14, 12);
    const second = take('second', '2026-07-19T21:00:00Z', 13, 14, 13);
    const discarded = {
      ...take('discarded', '2026-07-19T22:00:00Z', 13, 14, 13),
      status: 'discarded' as const,
      disposition: 'discarded' as const,
    };

    expect(takeCountsByMeasure([first, second, discarded], measures)).toMatchObject({
      12: 1,
      13: 2,
      14: 2,
    });
  });

  it('does not count the next measure when a pass ends exactly on its barline', () => {
    const barlineEnding = take(
      'barline-ending',
      '2026-07-19T20:00:00Z',
      12,
      12,
      12,
    );
    if (!barlineEnding.score_span) throw new Error('fixture requires a score span');
    barlineEnding.score_span.end = position(13);

    const counts = takeCountsByMeasure([barlineEnding], measures);

    expect(counts[12]).toBe(1);
    expect(counts[13]).toBeUndefined();
  });
});
