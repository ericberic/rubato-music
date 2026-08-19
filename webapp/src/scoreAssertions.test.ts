import { describe, expect, it } from 'vitest';
import {
  ASSERTIONS,
  assertionsFor,
  describeSpan,
  spanLength,
  type AssertionContext,
  type ScoreSpan,
} from './scoreAssertions';

const point = (measure = 42, beat = 1): ScoreSpan => ({
  kind: 'point',
  fromMeasure: measure,
  toMeasure: measure,
  beat,
});
const range = (from = 101, to = 103): ScoreSpan => ({
  kind: 'range',
  fromMeasure: from,
  toMeasure: to,
});

const ctx = (over: Partial<AssertionContext> = {}): AssertionContext => ({
  span: point(),
  hasSelectedAnchor: false,
  hasNearbyAnchor: false,
  anchorsInSpan: 0,
  isFreeRegion: false,
  canStartLive: true,
  ...over,
});

const ids = (c: AssertionContext) => assertionsFor(c).map((a) => a.id);

describe('span shape decides what can be asserted', () => {
  it('offers beat-precise actions only on a point', () => {
    expect(ids(ctx({ span: point() }))).toContain('anchor.add');
    expect(ids(ctx({ span: range() }))).not.toContain('anchor.add');
  });

  it('offers a free region only on a range', () => {
    // A cadenza is a span, not an instant. Offering it on a point would invite
    // a one-measure region that cannot express mm.101-103.
    expect(ids(ctx({ span: range() }))).toContain('region.free');
    expect(ids(ctx({ span: point() }))).not.toContain('region.free');
  });

  it('never offers to start live from a span', () => {
    expect(ids(ctx({ span: range() }))).not.toContain('live.start');
  });
});

describe('the menu stays short', () => {
  it('shows only applicable choices, not the whole catalogue', () => {
    const shown = assertionsFor(ctx());
    expect(shown.length).toBeLessThan(ASSERTIONS.length);
  });

  it('a plain point with no anchors offers a handful of actions', () => {
    // The point of the redesign: fewer clicks and no scanning past choices
    // that cannot apply.
    expect(assertionsFor(ctx()).length).toBeLessThanOrEqual(5);
  });
});

describe('anchor state', () => {
  it('offers move only when an anchor is nearby and none is selected', () => {
    expect(ids(ctx({ hasNearbyAnchor: true }))).toContain('anchor.move');
    expect(ids(ctx({ hasNearbyAnchor: true, hasSelectedAnchor: true }))).not.toContain(
      'anchor.move',
    );
  });

  it('offers removal only for a selected anchor', () => {
    expect(ids(ctx())).not.toContain('anchor.remove');
    expect(ids(ctx({ hasSelectedAnchor: true }))).toContain('anchor.remove');
  });

  it('offers a bulk clear only when it beats removing them one by one', () => {
    expect(ids(ctx({ anchorsInSpan: 1 }))).not.toContain('anchor.clearSpan');
    expect(ids(ctx({ anchorsInSpan: 4 }))).toContain('anchor.clearSpan');
  });
});

describe('free regions toggle rather than duplicate', () => {
  it('a declared region offers removal, not another declaration', () => {
    const declared = ids(ctx({ span: range(), isFreeRegion: true }));
    expect(declared).toContain('region.free.clear');
    expect(declared).not.toContain('region.free');
  });
});

describe('ordering', () => {
  const ORDER = ['position', 'structure', 'correction', 'transport'];

  it('presents groups in a stable order, whichever are applicable', () => {
    // Not "position is always first" -- with an anchor selected both position
    // actions filter out and correction legitimately leads. The invariant is
    // that whatever survives stays in catalogue order.
    for (const c of [
      ctx(),
      ctx({ hasSelectedAnchor: true, anchorsInSpan: 3, hasNearbyAnchor: true }),
      ctx({ span: range(), anchorsInSpan: 2 }),
    ]) {
      const seen = assertionsFor(c).map((a) => ORDER.indexOf(a.group));
      expect(seen).toEqual([...seen].sort((x, y) => x - y));
    }
  });

  it('puts destructive actions last within their group', () => {
    const shown = assertionsFor(ctx({ hasSelectedAnchor: true, anchorsInSpan: 3 }));
    for (const group of ORDER) {
      const flags = shown.filter((a) => a.group === group).map((a) => !!a.destructive);
      expect(flags).toEqual([...flags].sort((x, y) => Number(x) - Number(y)));
    }
  });
});

describe('labels adapt to the span', () => {
  it('names how many measures a region covers', () => {
    const region = assertionsFor(ctx({ span: range(101, 103) })).find(
      (a) => a.id === 'region.free',
    );
    expect(region?.label(ctx({ span: range(101, 103) }))).toContain('3 measures');
  });

  it('singularises a one-measure correction', () => {
    const one = ctx({ span: point() });
    const many = ctx({ span: range(10, 12) });
    const wrong = ASSERTIONS.find((a) => a.id === 'measure.wrong')!;
    expect(wrong.label(one)).toBe('This measure is mislabelled');
    expect(wrong.label(many)).toContain('3 measures');
  });
});

describe('span description', () => {
  it('reads as a musician would say it', () => {
    expect(describeSpan(point(42), '2')).toBe('m. 42 · beat 2');
    expect(describeSpan(range(101, 103))).toBe('m. 101–103 · 3 measures');
  });

  it('counts inclusively', () => {
    expect(spanLength(range(101, 103))).toBe(3);
    expect(spanLength(point(42))).toBe(1);
  });
});
