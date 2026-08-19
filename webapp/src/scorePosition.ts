import type {
  CoverageMeasure,
  ScorePositionResponse,
  ScoreTransportResponse,
} from './generated';

export function measureForScoreBeat(
  scoreBeat: number,
  measures: CoverageMeasure[],
): CoverageMeasure | null {
  if (!measures.length) return null;
  if (scoreBeat < measures[0].start_beat) return measures[0];
  return (
    measures.find((measure) => scoreBeat < measure.end_beat) ??
    measures[measures.length - 1]
  );
}

export function resolvePositionMeasure(
  position: ScorePositionResponse,
  measures: CoverageMeasure[],
): ScorePositionResponse {
  const measure = measureForScoreBeat(position.score_beat, measures);
  if (!measure) return position;
  return {
    ...position,
    measure_index: measure.measure - 1,
    measure_label: String(measure.measure),
    beat_in_measure: Math.max(0, position.score_beat - measure.start_beat),
  };
}

export function interpolatePosition(
  start: ScorePositionResponse,
  end: ScorePositionResponse,
  ratio: number,
  measures: CoverageMeasure[] = [],
): ScorePositionResponse {
  const clamped = Math.max(0, Math.min(1, ratio));
  const lerp = (a: number, b: number) => a + (b - a) * clamped;
  const fallbackMeasure = clamped < 0.5 ? start : end;
  const interpolated: ScorePositionResponse = {
    score_tick: Math.round(lerp(start.score_tick, end.score_tick)),
    score_beat: lerp(start.score_beat, end.score_beat),
    measure_index: fallbackMeasure.measure_index,
    measure_label: fallbackMeasure.measure_label,
    beat_in_measure:
      start.measure_index === end.measure_index
        ? lerp(start.beat_in_measure, end.beat_in_measure)
        : fallbackMeasure.beat_in_measure,
    source_seconds: lerp(start.source_seconds, end.source_seconds),
    confidence: Math.min(start.confidence, end.confidence),
  };
  return resolvePositionMeasure(interpolated, measures);
}

export function transportPositionAt(
  transport: ScoreTransportResponse | null | undefined,
  startedAt: string | null | undefined,
  nowMs: number,
  measures: CoverageMeasure[] = [],
): ScorePositionResponse | null {
  if (!transport?.anchors.length || !startedAt) return null;
  const startTime = new Date(startedAt).getTime();
  if (Number.isNaN(startTime)) return null;
  const elapsed = Math.max(0, (nowMs - startTime) / 1000);
  return transportPositionAtElapsed(transport, elapsed, measures);
}

export function transportPositionAtElapsed(
  transport: ScoreTransportResponse | null | undefined,
  elapsedSeconds: number,
  measures: CoverageMeasure[] = [],
): ScorePositionResponse | null {
  if (!transport?.anchors.length) return null;
  const elapsed = Math.max(0, elapsedSeconds);
  const anchors = transport.anchors;
  if (elapsed <= anchors[0].elapsed_seconds) {
    return resolvePositionMeasure(anchors[0].position, measures);
  }
  for (let index = 1; index < anchors.length; index += 1) {
    const previous = anchors[index - 1];
    const next = anchors[index];
    if (elapsed <= next.elapsed_seconds) {
      const duration = next.elapsed_seconds - previous.elapsed_seconds;
      return interpolatePosition(
        previous.position,
        next.position,
        duration > 0 ? (elapsed - previous.elapsed_seconds) / duration : 1,
        measures,
      );
    }
  }
  return resolvePositionMeasure(anchors[anchors.length - 1].position, measures);
}

export function transportElapsedAtScoreBeat(
  transport: ScoreTransportResponse | null | undefined,
  scoreBeat: number,
): number | null {
  if (!transport?.anchors.length) return null;
  const anchors = transport.anchors;
  if (scoreBeat <= anchors[0].position.score_beat) return anchors[0].elapsed_seconds;
  if (scoreBeat > anchors[anchors.length - 1].position.score_beat) return null;
  for (let index = 1; index < anchors.length; index += 1) {
    const previous = anchors[index - 1];
    const next = anchors[index];
    if (scoreBeat > next.position.score_beat) continue;
    const beatSpan = next.position.score_beat - previous.position.score_beat;
    if (beatSpan <= 0) continue;
    const ratio = (scoreBeat - previous.position.score_beat) / beatSpan;
    return previous.elapsed_seconds + ratio * (next.elapsed_seconds - previous.elapsed_seconds);
  }
  return anchors[anchors.length - 1].elapsed_seconds;
}

export function measureProgress(
  scoreBeat: number | null,
  measure: CoverageMeasure | null | undefined,
): number {
  if (scoreBeat === null || !measure) return 0.5;
  const duration = measure.end_beat - measure.start_beat;
  if (duration <= 0) return 0.5;
  return Math.max(0, Math.min(1, (scoreBeat - measure.start_beat) / duration));
}
