/**
 * What the performer can assert about a place in the score.
 *
 * Every score workflow is the same two steps: point at a span, then say what is
 * true about it. Anchors, free regions, mislabelled measures, missing measures
 * and starting live all fit that shape. The context menu used to grow a new
 * section per workflow, so each feature widened it and the shared structure
 * stayed invisible.
 *
 * Here the span and the assertion are separate. Adding a workflow means adding
 * one entry to `ASSERTIONS`, not another branch in the template. The catalogue
 * is pure data so it can be tested without mounting a component, and so the
 * menu cannot disagree with what is actually applicable.
 */

export type SpanKind = 'point' | 'range';

export interface ScoreSpan {
  kind: SpanKind;
  /** Inclusive. For a point, `from` and `to` are the same measure. */
  fromMeasure: number;
  toMeasure: number;
  /** Beat within `fromMeasure`, 1-based. Only meaningful for a point. */
  beat?: number;
}

export interface AssertionContext {
  span: ScoreSpan;
  /** An anchor the performer has already selected (clicked or dragged). */
  hasSelectedAnchor: boolean;
  /** An existing anchor near the point, movable to it. */
  hasNearbyAnchor: boolean;
  /** How many anchors fall inside the span. */
  anchorsInSpan: number;
  /** Whether the span is already declared free (cadenza, fermata, lead-in). */
  isFreeRegion: boolean;
  /** Hardware and score artifacts are ready for a live start. */
  canStartLive: boolean;
}

/** Ordered so the most common intent is nearest the cursor. */
export type AssertionGroup = 'position' | 'structure' | 'correction' | 'transport';

export interface Assertion {
  id: string;
  label: (ctx: AssertionContext) => string;
  hint: string;
  group: AssertionGroup;
  /** Needs confirmation or is hard to undo; rendered quieter and last. */
  destructive?: boolean;
  applies: (ctx: AssertionContext) => boolean;
}

const isPoint = (ctx: AssertionContext) => ctx.span.kind === 'point';
const isRange = (ctx: AssertionContext) => ctx.span.kind === 'range';
export const spanLength = (span: ScoreSpan) => span.toMeasure - span.fromMeasure + 1;

export const ASSERTIONS: Assertion[] = [
  // -- position: "I am here" ------------------------------------------------
  {
    id: 'anchor.add',
    label: () => 'Anchor is here',
    hint: 'Orchestra hits with your lowest nearby bass note.',
    group: 'position',
    applies: (ctx) => isPoint(ctx) && !ctx.hasSelectedAnchor,
  },
  {
    id: 'anchor.move',
    label: () => 'Move nearest anchor here',
    hint: 'Keeps the anchor, corrects where it sits.',
    group: 'position',
    applies: (ctx) => isPoint(ctx) && ctx.hasNearbyAnchor && !ctx.hasSelectedAnchor,
  },
  // -- structure: "this span behaves differently" --------------------------
  {
    id: 'region.free',
    label: (ctx) =>
      `Mark ${spanLength(ctx.span)} measures as a free region`,
    hint: 'Cadenza, fermata or lead-in: following stops, a handoff detector brings the orchestra back.',
    group: 'structure',
    applies: (ctx) => isRange(ctx) && !ctx.isFreeRegion,
  },
  {
    id: 'region.free.clear',
    label: () => 'No longer a free region',
    hint: 'Return this span to normal score following.',
    group: 'structure',
    applies: (ctx) => ctx.isFreeRegion,
  },
  // -- correction: "the score data is wrong here" --------------------------
  {
    id: 'measure.missing',
    label: () => 'A measure is missing here',
    hint: 'The score has a bar the layout did not detect.',
    group: 'correction',
    applies: (ctx) => isPoint(ctx),
  },
  {
    id: 'measure.wrong',
    label: (ctx) =>
      spanLength(ctx.span) === 1
        ? 'This measure is mislabelled'
        : `These ${spanLength(ctx.span)} measures are mislabelled`,
    hint: 'The box is here but its number is wrong.',
    group: 'correction',
    applies: () => true,
  },
  {
    id: 'measure.spurious',
    label: () => 'This is not a measure',
    hint: 'A detected box that is not really a bar.',
    group: 'correction',
    destructive: true,
    applies: (ctx) => isPoint(ctx),
  },
  // -- transport -----------------------------------------------------------
  {
    id: 'live.start',
    label: () => 'Start live with orchestra here',
    hint: 'Starts immediately. Join when you have the pulse.',
    group: 'transport',
    applies: (ctx) => isPoint(ctx) && ctx.canStartLive,
  },
  {
    id: 'anchor.remove',
    label: () => 'Remove this anchor',
    hint: '',
    group: 'transport',
    destructive: true,
    applies: (ctx) => ctx.hasSelectedAnchor,
  },
  {
    id: 'anchor.clearSpan',
    label: (ctx) => `Clear ${ctx.anchorsInSpan} anchors in this span`,
    hint: '',
    group: 'transport',
    destructive: true,
    applies: (ctx) => ctx.anchorsInSpan > 1,
  },
];

const GROUP_ORDER: AssertionGroup[] = ['position', 'structure', 'correction', 'transport'];

/**
 * The assertions valid for this span, grouped and ordered.
 *
 * Filtering here rather than in the template is what keeps the menu short: a
 * point never offers span-only actions, and a span never offers beat-precise
 * ones, so the performer only ever sees choices that can actually apply.
 */
export function assertionsFor(ctx: AssertionContext): Assertion[] {
  const available = ASSERTIONS.filter((a) => a.applies(ctx));
  return available.sort((a, b) => {
    const byGroup = GROUP_ORDER.indexOf(a.group) - GROUP_ORDER.indexOf(b.group);
    if (byGroup !== 0) return byGroup;
    // Destructive last within a group, so a mis-click near the top is cheap.
    return Number(a.destructive ?? false) - Number(b.destructive ?? false);
  });
}

export function describeSpan(span: ScoreSpan, beatLabel?: string): string {
  if (span.kind === 'point') {
    return beatLabel
      ? `m. ${span.fromMeasure} · beat ${beatLabel}`
      : `m. ${span.fromMeasure}`;
  }
  return `m. ${span.fromMeasure}–${span.toMeasure} · ${spanLength(span)} measures`;
}
