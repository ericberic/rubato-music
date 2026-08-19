import { describe, expect, it } from 'vitest';
import type { CoverageMeasure, ScorePositionResponse } from './generated';
import {
  interpolatePosition,
  measureForScoreBeat,
  measureProgress,
  transportPositionAt,
  transportPositionAtElapsed,
  transportElapsedAtScoreBeat,
} from './scorePosition';

const measures: CoverageMeasure[] = [
  {
    measure: 12,
    start_beat: 44,
    end_beat: 48,
    solo: true,
    state: 'uncovered',
    scope: 'solo',
    observed: false,
  },
  {
    measure: 13,
    start_beat: 48,
    end_beat: 52,
    solo: true,
    state: 'uncovered',
    scope: 'solo',
    observed: false,
  },
];

const start: ScorePositionResponse = {
  score_tick: 42240,
  score_beat: 44,
  measure_index: 11,
  measure_label: '12',
  beat_in_measure: 0,
  source_seconds: 72,
  confidence: 0.8,
};
const end: ScorePositionResponse = {
  score_tick: 49920,
  score_beat: 52,
  measure_index: 13,
  measure_label: '14',
  beat_in_measure: 0,
  source_seconds: 80,
  confidence: 0.8,
};

describe('continuous score position', () => {
  it('resolves the interpolated beat instead of snapping at an anchor midpoint', () => {
    const beforeBoundary = interpolatePosition(start, end, 0.49, measures);
    const afterBoundary = interpolatePosition(start, end, 0.51, measures);

    expect(beforeBoundary.measure_label).toBe('12');
    expect(beforeBoundary.beat_in_measure).toBeCloseTo(3.92);
    expect(afterBoundary.measure_label).toBe('13');
    expect(afterBoundary.beat_in_measure).toBeCloseTo(0.08);
  });

  it('exposes sub-measure progress for a continuously scanning cursor', () => {
    expect(measureProgress(44.4, measures[0])).toBeCloseTo(0.1);
    expect(measureProgress(46, measures[0])).toBeCloseTo(0.5);
    expect(measureProgress(47.6, measures[0])).toBeCloseTo(0.9);
  });

  it('snaps a beat in an uncovered metadata gap to the next printed measure', () => {
    const measuresWithGap = [
      measures[0],
      { ...measures[1], start_beat: 50, end_beat: 54 },
    ];

    expect(measureForScoreBeat(49, measuresWithGap)?.measure).toBe(13);
  });

  it('transitions anchor metadata even when measure geometry is unavailable', () => {
    const beforeMidpoint = interpolatePosition(start, end, 0.49);
    const afterMidpoint = interpolatePosition(start, end, 0.51);

    expect(beforeMidpoint.measure_label).toBe('12');
    expect(beforeMidpoint.beat_in_measure).toBe(start.beat_in_measure);
    expect(afterMidpoint.measure_label).toBe('14');
    expect(afterMidpoint.beat_in_measure).toBe(end.beat_in_measure);
  });

  it('supports a lookahead clock so the next page is ready before the boundary', () => {
    const startedAt = '2026-07-17T00:00:00.000Z';
    const transport = {
      piece_id: 'chopin_op11',
      movement: 2,
      mapping_id: 'fixture',
      mapping_review_state: 'machine' as const,
      canonical_position: false,
      anchors: [
        { elapsed_seconds: 0, position: start },
        { elapsed_seconds: 8, position: end },
      ],
    };
    const current = transportPositionAt(
      transport,
      startedAt,
      Date.parse(startedAt) + 3_900,
      measures,
    );
    const lookahead = transportPositionAt(
      transport,
      startedAt,
      Date.parse(startedAt) + 4_650,
      measures,
    );

    expect(current?.measure_label).toBe('12');
    expect(lookahead?.measure_label).toBe('13');
  });

  it('uses dense take landmarks for browser preview instead of stretching the span', () => {
    const measure14: ScorePositionResponse = {
      ...end,
      score_tick: 49920,
      score_beat: 52,
      measure_index: 13,
      measure_label: '14',
      beat_in_measure: 0,
      source_seconds: 78.094,
    };
    const reviewTransport = {
      piece_id: 'chopin_op11',
      movement: 2,
      mapping_id: 'fixture-v3',
      mapping_review_state: 'machine' as const,
      canonical_position: false,
      anchors: [
        { elapsed_seconds: 10.014, position: start },
        { elapsed_seconds: 13.992, position: measure14 },
      ],
    };

    const atFSharp = transportPositionAtElapsed(
      reviewTransport,
      13.992,
      [...measures, { ...measures[1], measure: 14, start_beat: 52, end_beat: 56 }],
    );

    expect(atFSharp?.measure_label).toBe('14');
    expect(atFSharp?.beat_in_measure).toBeCloseTo(0);
  });

  it('seeks an aligned review from a selected score beat on the same transport', () => {
    const reviewTransport = {
      piece_id: 'chopin_op11',
      movement: 2,
      mapping_id: 'fixture-v5',
      mapping_review_state: 'machine' as const,
      canonical_position: false,
      anchors: [
        { elapsed_seconds: 8, position: start },
        { elapsed_seconds: 16, position: end },
      ],
    };

    expect(transportElapsedAtScoreBeat(reviewTransport, 48)).toBeCloseTo(12);
    expect(transportElapsedAtScoreBeat(reviewTransport, 44)).toBe(8);
    expect(transportElapsedAtScoreBeat(reviewTransport, 60)).toBeNull();
  });
});
