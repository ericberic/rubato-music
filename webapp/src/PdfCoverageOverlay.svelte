<script lang="ts">
  // Coverage face - the score with the overlay (design doc §3.2, roadmap item 7).
  //
  // Level 0: a measure-strip heatmap (reuses the same `.measure-block` /
  // `.block-{state}` vocabulary as the existing movement coverage grid in
  // App.svelte, for visual consistency) that doubles as the page scrubber --
  // clicking a measure jumps the PDF below to the page containing it.
  //
  // Level 2: the PDF renders via pdf.js onto a canvas; an absolutely
  // positioned SVG draws the exact Audiveris GRID-stage measure boxes joined
  // with `coverage.json`. The generic fallback still subdivides a Level-1
  // system band evenly when an older bundle has no x-splits.
  //
  // Palette (VISION_AND_UX_DESIGN.md aesthetic law + this doc's one new
  // color duty): `--ready` green = covered, dim ivory hatch = uncovered
  // solo, neutral = tutti. Never red -- missing coverage is a to-do, not an
  // error.
  import { createEventDispatcher, onDestroy, onMount } from 'svelte';
  import { measureProgress } from './scorePosition';
  import type { ScoreSpan } from './scoreAssertions';
  import {
    addAnchor as addAnchorRequest,
    clearAnchorsInMeasure as clearAnchorsInMeasureRequest,
    deleteAnchor as deleteAnchorRequest,
    listAnchors,
    moveAnchor as moveAnchorRequest,
    restoreAnchors as restoreAnchorsRequest,
  } from './generated';
  import { zMeasureBoxesResponse } from './generated/zod.gen';
  import type {
    Anchor,
    CoverageDoc,
    CoverageMeasure,
    MeasureBoxesResponse,
    MeasureBoxSystemResponse,
    MixRegionOutput,
  } from './generated';

  export let pieceId: string;
  export let movement: number;
  export let coverage: CoverageDoc | null = null;
  export let currentPage = 1;
  export let currentMeasure: number | null = null;
  export let preturnMeasure: number | null = null;
  export let currentScoreBeat: number | null = null;
  export let positionLabel = 'Current location unavailable';
  export let latestTakeStartMeasure: number | null = null;
  export let latestTakeEndMeasure: number | null = null;
  export let latestTakeStartScoreBeat: number | null = null;
  export let latestTakeEndScoreBeat: number | null = null;
  export let selectedMeasure: number | null = null;
  export let takeCountsByMeasure: Record<number, number> = {};
  // Measures where the system needs something from the performer, keyed by
  // measure. Amber and green already carry *rehearsal* uncertainty (how much
  // have I played this); this is a different axis -- the score data itself is
  // in question and only a human can settle it. One glyph rather than another
  // colour, so the two never compete for the same visual channel.
  export let needsInfo: Record<number, { severity: string; reason: string }> = {};
  // Performance view: strip the rehearsal chrome (coverage strip, guide,
  // annotations, take boxes) down to the page plus the live red cursor. The
  // page is fit to the viewport *width* (`fitWidthPx`) so the engraving is
  // large and readable on a landscape screen, and the taller-than-tall page
  // scrolls vertically inside a `fitHeightPx` viewport, auto-following the
  // cursor's system. Auto page-follow (below) still turns pages across pages.
  export let performanceMode = false;
  export let mixAuthoringMode = false;
  export let dataAnnotationMode = false;
  export let timingReviewMode = false;
  type TimingReviewState = 'needs-listening' | 'spot-check' | 'reviewed';
  export let timingReview: Record<number, TimingReviewState> = {};
  export let showDataOverlay = false;
  export let showMixPlan = false;
  export let mixRegions: MixRegionOutput[] = [];
  export let mixDraftStartTick: number | null = null;
  export let mixSelectionStartTick: number | null = null;
  export let mixSelectionEndTick: number | null = null;
  export let fitHeightPx: number | null = null;
  export let fitWidthPx: number | null = null;
  // Data mode scrolls the score shell independently from the page. Passing the
  // owning element keeps this component layout-agnostic while letting measure
  // selection center the active system in the pane that can actually move.
  export let scrollContainer: HTMLElement | null = null;

  const dispatch = createEventDispatcher<{
    selectMeasure: { measure: number };
    contextMeasure: {
      measure: number;
      scoreBeat: number;
      clientX: number;
      clientY: number;
      selectedAnchor: Anchor | null;
      nearestAnchor: Anchor | null;
      anchorsInMeasure: Anchor[];
      // A right-click on the score is a point; a drag across the strip is a
      // range. The menu acts on whichever it is given.
      span?: ScoreSpan;
    };
    anchorChanged: {
      kind: 'move';
      before: Anchor;
      after: Anchor;
      clientX: number;
      clientY: number;
    };
    anchorChangeFailed: {
      message: string;
      clientX: number;
      clientY: number;
    };
    mixSelection: {
      startTick: number;
      endTick: number;
      startMeasure: number;
      endMeasure: number;
      extend: boolean;
    };
    mixContext: {
      scoreTick: number;
      measure: number;
      clientX: number;
      clientY: number;
      regionId: string | null;
    };
  }>();

  let measureBoxes: MeasureBoxesResponse | null = null;
  let pdfAvailable = true;
  let geometryAvailable = true;
  // Structural beat anchors: the pianist marks strong beats where the orchestra
  // should hit reactively (see docs/concepts/rehearsal-model-workflow.md).
  const CANONICAL_PPQ = 960;
  let anchors: Anchor[] = [];
  let anchorsLoaded = false;
  let anchorDrag: {
    anchor: Anchor;
    measure: number;
    pointerId: number;
    startClientX: number;
    previewScoreBeat: number;
    moved: boolean;
  } | null = null;
  type MixPoint = { scoreTick: number; measure: number };
  let mixDrag: { pointerId: number; start: MixPoint; moved: boolean } | null = null;
  let pdfErrorMessage = '';
  let geometryMessage = '';
  let pdfDoc: any = null;
  let pageCount = 1;
  let canvasEl: HTMLCanvasElement;
  let pageWrapEl: HTMLDivElement | null = null;
  let containerWidth = 0;
  let renderToken = 0;
  let renderedPage = 0;

  // Rehearsal used to render the page at a fixed 760px because the score shared
  // its row with a side panel. The score owns the full width now (issue #153),
  // so a fixed cap just left a band of empty space beside the engraving and made
  // the notes smaller than they needed to be. Fill the host instead, with the
  // old value as the floor for narrow windows and a ceiling so a small page is
  // not upscaled into mush.
  $: positionActive = currentMeasure !== null || currentScoreBeat !== null;

  const MAX_DISPLAY_WIDTH = 760;
  const MAX_REHEARSAL_WIDTH = 1400;
  let hostWidthPx = 0;
  $: rehearsalWidthPx = Math.min(
    Math.max(hostWidthPx || MAX_DISPLAY_WIDTH, MAX_DISPLAY_WIDTH),
    MAX_REHEARSAL_WIDTH,
  );

  $: measureStateByNumber = new Map((coverage?.measures ?? []).map((m) => [m.measure, m]));
  $: measureToPage = buildMeasureToPageIndex(measureBoxes);
  // The strip and the page controls answered the same question -- where am I
  // globally -- and answering it twice meant scrolling to the bottom to turn a
  // page, then back up to see where that left you. The strip now carries page
  // structure itself. Grouping is derived from the same measure->page index the
  // automatic page turn uses, so the two can never disagree.
  $: pageGroups = buildPageGroups(coverage?.measures ?? [], measureToPage);
  $: currentPageSystems =
    measureBoxes?.pages.find((p) => p.page === currentPage)?.systems ?? [];
  // Precomputed here (not called inline from the template's {#each}) so the
  // per-measure segment math runs once per actual dependency change instead
  // of on every unrelated re-render -- `measureStateByNumber` is passed
  // explicitly rather than closed over so it's part of this statement's
  // own tracked dependencies (a closed-over reactive variable wouldn't be).
  $: pageSegments = currentPageSystems.map((system) => ({
    system,
    segments: segmentsForSystem(
      system,
      measureStateByNumber,
      takeCountsByMeasure,
      timingReviewMode,
      timingReview,
    ),
  }));
  $: firstLatestMeasureOnPage =
    latestTakeStartMeasure !== null && latestTakeEndMeasure !== null
      ? pageSegments
          .flatMap(({ segments }) => segments)
          .find(
            (segment) =>
              segment.measure >= latestTakeStartMeasure! &&
              segment.measure <= latestTakeEndMeasure!,
          )?.measure ?? null
      : null;

  let lastAutoMeasure: number | null = null;
  // Playback/live position wins, but when idle the score must show the page
  // containing the suggested/selected rehearsal target.  A card saying
  // “m. 22” while the PDF remains on page 1 has no spatial meaning.
  $: autoMeasure = currentMeasure ?? preturnMeasure ?? selectedMeasure;
  $: if (autoMeasure !== null && autoMeasure !== lastAutoMeasure) {
    const page = measureToPage.get(autoMeasure);
    if (page) {
      currentPage = page;
      // Geometry loads independently from live position.  Remember the
      // measure only after it resolves so a position received first is
      // retried when the measure-to-page index arrives.
      lastAutoMeasure = autoMeasure;
    }
  }

  type StripMeasure = NonNullable<typeof coverage>['measures'][number];

  function buildPageGroups(
    measures: readonly StripMeasure[],
    toPage: Map<number, number>,
  ): { page: number; measures: StripMeasure[] }[] {
    const groups: { page: number; measures: StripMeasure[] }[] = [];
    for (const measure of measures) {
      // Measures with no geometry yet stay with the run they follow rather than
      // opening a spurious group; an unplaced measure is not a page break.
      const page = toPage.get(measure.measure) ?? groups[groups.length - 1]?.page ?? 1;
      const last = groups[groups.length - 1];
      if (!last || last.page !== page) groups.push({ page, measures: [measure] });
      else last.measures.push(measure);
    }
    return groups;
  }

  function buildMeasureToPageIndex(boxes: MeasureBoxesResponse | null): Map<number, number> {
    const index = new Map<number, number>();
    if (!boxes) return index;
    for (const page of boxes.pages) {
      for (const system of page.systems) {
        for (let m = system.first_measure; m <= system.last_measure; m++) {
          index.set(m, page.page);
        }
      }
    }
    return index;
  }

  function selectMeasure(measureNumber: number) {
    clearNativeScoreSelection();
    const page = measureToPage.get(measureNumber);
    if (page) currentPage = page;
    dispatch('selectMeasure', { measure: measureNumber });
  }

  function clearNativeScoreSelection() {
    if (typeof window !== 'undefined') window.getSelection()?.removeAllRanges();
  }

  // Dragging across the strip selects a span. The strip is already the global
  // navigator and stays visible when the PDF is scrolled away, so it is the one
  // place a range can always be drawn -- a cadenza is three measures that may
  // straddle a page break, which is awkward to express on the page itself.
  // Click sets one end, shift-click the other. This is the reliable way to
  // span: a drag has to travel the whole distance, and the strip wraps into a
  // row per page, so selecting m.60-140 means dragging off the end of one row
  // and onto the next. Shift-click does not care about layout, page breaks or
  // system breaks -- which is exactly the case a free region tends to be.
  // Dragging still works and stays the quicker gesture for a few adjacent bars.
  let spanAnchor: number | null = null;
  let stripDrag: { from: number; to: number } | null = null;
  let pendingShiftSpan: { from: number; to: number } | null = null;
  $: stripSpanFrom = stripDrag ? Math.min(stripDrag.from, stripDrag.to) : null;
  $: stripSpanTo = stripDrag ? Math.max(stripDrag.from, stripDrag.to) : null;

  function beginStripDrag(measure: number, event: PointerEvent) {
    if (event.button !== 0) return;
    event.preventDefault();
    clearNativeScoreSelection();
    // Shift completes a span, but the emit waits for pointerup. Opening the
    // menu on pointerdown does not survive: a window-level pointerdown handler
    // closes any menu whose click landed outside it, and that is the very event
    // that opened this one. Dragging never hit this because it ends on pointerup.
    pendingShiftSpan =
      event.shiftKey && spanAnchor !== null && spanAnchor !== measure
        ? { from: Math.min(spanAnchor, measure), to: Math.max(spanAnchor, measure) }
        : null;
    stripDrag = { from: measure, to: measure };
    (event.currentTarget as HTMLElement).setPointerCapture?.(event.pointerId);
  }

  function emitSpan(from: number, to: number, event: { clientX: number; clientY: number }) {
    dispatch('contextMeasure', {
      measure: from,
      scoreBeat: 0,
      clientX: event.clientX,
      clientY: event.clientY,
      selectedAnchor: null,
      nearestAnchor: null,
      anchorsInMeasure: anchors.filter((a) => a.measure >= from && a.measure <= to),
      span: { kind: 'range', fromMeasure: from, toMeasure: to },
    });
  }

  function extendStripDrag(measure: number) {
    if (stripDrag) stripDrag = { ...stripDrag, to: measure };
  }

  function endStripDrag(event: PointerEvent) {
    const drag = stripDrag;
    const shiftSpan = pendingShiftSpan;
    stripDrag = null;
    pendingShiftSpan = null;
    if (shiftSpan) {
      spanAnchor = null;
      emitSpan(shiftSpan.from, shiftSpan.to, event);
      return;
    }
    if (!drag) return;
    const from = Math.min(drag.from, drag.to);
    const to = Math.max(drag.from, drag.to);
    // A drag that never left one measure is a click: select it, and remember it
    // as the anchor so a later shift-click can complete a span from here.
    if (from === to) {
      spanAnchor = from;
      selectMeasure(from);
      return;
    }
    spanAnchor = null;
    emitSpan(from, to, event);
  }

  function selectMeasureByKey(event: KeyboardEvent, measureNumber: number) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      selectMeasure(measureNumber);
    }
  }

  function contextForMeasure(
    measureNumber: number,
    scoreBeat: number,
    selectedAnchor: Anchor | null,
  ) {
    const anchorsInMeasure = anchors.filter((anchor) => anchor.measure === measureNumber);
    const nearestAnchor =
      selectedAnchor ??
      anchorsInMeasure.reduce<Anchor | null>((nearest, candidate) => {
        if (nearest === null) return candidate;
        return Math.abs(candidate.score_tick / CANONICAL_PPQ - scoreBeat) <
          Math.abs(nearest.score_tick / CANONICAL_PPQ - scoreBeat)
          ? candidate
          : nearest;
      }, null);
    return { selectedAnchor, nearestAnchor, anchorsInMeasure };
  }

  function scoreBeatAtPointer(
    event: Pick<MouseEvent, 'clientX'>,
    seg: {
      measure: number;
      x0: number;
      width: number;
      scoreStartTick?: number | null;
      scoreEndTick?: number | null;
      beats?: { beat_in_measure: number; x: number }[];
    },
    coordinateElement: Element,
  ) {
    const rect = coordinateElement.getBoundingClientRect();
    const clickX = (event.clientX - rect.left) / (rect.width || 1);
    return beatFromX(seg, measureStateByNumber.get(seg.measure), clickX);
  }

  function dispatchMeasureMenu({
    event,
    seg,
    selectedAnchor = null,
  }: {
    event: MouseEvent | PointerEvent;
    seg: {
      measure: number;
      x0: number;
      width: number;
      beats?: { beat_in_measure: number; x: number }[];
    };
    selectedAnchor?: Anchor | null;
  }) {
    const svg = (event.currentTarget as SVGElement)?.ownerSVGElement;
    const coordinateElement = svg ?? canvasEl;
    if (!coordinateElement) return;
    const scoreBeat =
      selectedAnchor?.score_tick !== undefined
        ? selectedAnchor.score_tick / CANONICAL_PPQ
        : scoreBeatAtPointer(event, seg, coordinateElement);
    const context = contextForMeasure(seg.measure, scoreBeat, selectedAnchor);
    selectMeasure(seg.measure);
    dispatch('contextMeasure', {
      measure: seg.measure,
      scoreBeat,
      clientX: event.clientX,
      clientY: event.clientY,
      ...context,
    });
  }

  function openMeasureMenu(
    event: MouseEvent,
    seg: {
      measure: number;
      x0: number;
      width: number;
      beats?: { beat_in_measure: number; x: number }[];
    },
  ) {
    event.preventDefault();
    dispatchMeasureMenu({ event, seg });
  }

  function mixPointAt(event: Pick<PointerEvent | MouseEvent, 'clientX' | 'clientY'>): MixPoint | null {
    if (!canvasEl) return null;
    const rect = canvasEl.getBoundingClientRect();
    const x = (event.clientX - rect.left) / (rect.width || 1);
    const y = (event.clientY - rect.top) / (rect.height || 1);
    for (const { system, segments } of pageSegments) {
      if (y < system.y0 || y > system.y1) continue;
      const segment = segments.find((item) => x >= item.x0 && x <= item.x0 + item.width);
      if (!segment) continue;
      const scoreBeat = beatFromX(segment, measureStateByNumber.get(segment.measure), x);
      return { scoreTick: Math.round(scoreBeat * CANONICAL_PPQ), measure: segment.measure };
    }
    return null;
  }

  function beginMixSelection(event: PointerEvent) {
    // Chromium can otherwise start a native selection on the canvas before
    // either a click or a contextmenu selects the measure. The selection
    // highlight paints the entire replaced canvas blue, making the engraving
    // appear to disappear. Right-button pointerdown needs the same guard as
    // left-button pointerdown; contextmenu arrives too late to prevent it.
    if (event.button === 2) {
      event.preventDefault();
      clearNativeScoreSelection();
      return;
    }
    if (event.button !== 0) return;
    event.preventDefault();
    clearNativeScoreSelection();
    if (!mixAuthoringMode) return;
    const point = mixPointAt(event);
    if (!point) return;
    (event.currentTarget as Element).setPointerCapture(event.pointerId);
    mixDrag = { pointerId: event.pointerId, start: point, moved: false };
  }

  function moveMixSelection(event: PointerEvent) {
    if (!mixDrag || mixDrag.pointerId !== event.pointerId) return;
    const point = mixPointAt(event);
    if (!point) return;
    mixDrag = { ...mixDrag, moved: mixDrag.moved || point.scoreTick !== mixDrag.start.scoreTick };
  }

  function finishMixSelection(event: PointerEvent) {
    if (!mixDrag || mixDrag.pointerId !== event.pointerId) return;
    const drag = mixDrag;
    mixDrag = null;
    const end = mixPointAt(event) ?? drag.start;
    dispatch('mixSelection', {
      startTick: Math.min(drag.start.scoreTick, end.scoreTick),
      endTick: Math.max(drag.start.scoreTick, end.scoreTick),
      startMeasure: drag.start.scoreTick <= end.scoreTick ? drag.start.measure : end.measure,
      endMeasure: drag.start.scoreTick <= end.scoreTick ? end.measure : drag.start.measure,
      extend: event.shiftKey,
    });
  }

  function openMixContext(event: MouseEvent, regionId: string | null = null) {
    if (!mixAuthoringMode) return;
    event.preventDefault();
    event.stopPropagation();
    const point = mixPointAt(event);
    if (!point) return;
    dispatch('mixContext', {
      ...point,
      clientX: event.clientX,
      clientY: event.clientY,
      regionId,
    });
  }

  function mixGeometry(
    seg: {
      measure: number;
      x0: number;
      width: number;
      scoreStartTick?: number | null;
      scoreEndTick?: number | null;
      beats?: { beat_in_measure: number; x: number; confidence: number }[];
    },
    startTick: number,
    endTick: number,
  ): { x0: number; width: number } | null {
    const measure = measureStateByNumber.get(seg.measure);
    const measureStart = measure
      ? Math.round(measure.start_beat * CANONICAL_PPQ)
      : seg.scoreStartTick;
    const measureEnd = measure
      ? Math.round(measure.end_beat * CANONICAL_PPQ)
      : seg.scoreEndTick;
    if (
      measureStart === null || measureStart === undefined ||
      measureEnd === null || measureEnd === undefined
    ) return null;
    // Score regions are half-open [start, end): a cue beginning exactly at a
    // barline belongs to the following measure, while its end cap belongs to
    // the preceding one. This avoids duplicate zero-width marks at barlines.
    if (endTick <= measureStart || startTick >= measureEnd) return null;
    const startBeat = Math.max(startTick, measureStart) / CANONICAL_PPQ;
    const endBeat = Math.min(endTick, measureEnd) / CANONICAL_PPQ;
    const x0 = cursorX(seg, startBeat);
    return { x0, width: Math.max(0.004, cursorX(seg, endBeat) - x0) };
  }

  function mixEnvelopeGeometry(
    seg: Parameters<typeof mixGeometry>[0],
    system: { y0: number; y1: number },
    region: MixRegionOutput,
  ): { x0: number; x1: number; y0: number; y1: number; hasStart: boolean; hasEnd: boolean } | null {
    const box = mixGeometry(seg, region.start_tick, region.end_tick);
    if (!box) return null;
    const measure = measureStateByNumber.get(seg.measure);
    const bounds = segmentScoreBounds(seg, measure);
    if (!bounds) return null;
    const segmentStartTick = Math.round(bounds.start * CANONICAL_PPQ);
    const segmentEndTick = Math.round(bounds.end * CANONICAL_PPQ);
    // Keep automation horizontal in a dedicated typographic lane above the
    // system, like an octave bracket. Levels live in the endpoint editor; a
    // sloped graph both implied a pitch contour and could cross the notation.
    const laneTop = Math.max(0.004, system.y0 - 0.026);
    return {
      x0: box.x0,
      x1: box.x0 + box.width,
      y0: laneTop,
      y1: laneTop,
      hasStart: region.start_tick >= segmentStartTick && region.start_tick < segmentEndTick,
      hasEnd: region.end_tick > segmentStartTick && region.end_tick <= segmentEndTick,
    };
  }

  function mixGestureIcon(gesture: MixRegionOutput['gesture']): string {
    if (gesture === 'swell') return '∿';
    if (gesture === 'fade') return '↗';
    if (gesture === 'feature') return '◆';
    if (gesture === 'bed') return '—';
    return '◇';
  }

  function mixRegionIsActive(region: MixRegionOutput): boolean {
    if (currentScoreBeat === null || currentScoreBeat === undefined) return false;
    const tick = Math.round(currentScoreBeat * CANONICAL_PPQ);
    return tick >= region.start_tick && tick <= region.end_tick;
  }

  function coverageTitle(measure: CoverageMeasure | undefined, passCount: number): string {
    if (!measure) return 'Score role is still being recognized';
    const targetObservations = coverage?.n_target ?? 3;
    const qualityTarget = coverage?.quality_target ?? 0.5;
    if (measure.accompaniment_required === false) {
      return 'Ready: the orchestra has no notes to play in this measure';
    }
    if (measure.scope === 'tutti') {
      return 'Ready: the orchestra leads this measure';
    }
    if (measure.state === 'covered') {
      return `Ready evidence: every half-beat has at least ${targetObservations} observations and ${Math.round(qualityTarget * 100)}% alignment quality`;
    }
    if (measure.observed) {
      return `Learning: weakest half-beat has ${measure.min_n ?? 0} of ${targetObservations} observations and ${Math.round((measure.min_quality ?? 0) * 100)}% alignment quality; green requires both targets`;
    }
    if (measure.scope === 'solo' || measure.solo) {
      return 'Needs a first aligned pass';
    }
    return passCount > 0 ? 'Observed orchestral passage' : 'Orchestral passage; no piano take requested';
  }

  function timingReviewTitle(
    measureNumber: number,
    reviewState: Record<number, TimingReviewState>,
  ): string {
    const state = reviewState[measureNumber];
    if (state === 'needs-listening') {
      return 'Needs listening: independent score alignments disagree with the current timing map';
    }
    if (state === 'spot-check') {
      return 'Spot check: sampled across the movement to catch shared drift';
    }
    if (state === 'reviewed') return 'Heard and confirmed in this review session';
    return 'Not currently flagged; click to audition anyway';
  }

  function segmentsForSystem(
    system: MeasureBoxSystemResponse,
    stateByNumber: Map<number, CoverageMeasure>,
    passCounts: Record<number, number>,
    reviewMode: boolean,
    reviewState: Record<number, TimingReviewState>,
  ) {
    if (system.measures?.length) {
      return system.measures.map((box) => {
        const measureState = stateByNumber.get(box.measure);
        const { fill, hatch } = colorForState(
          measureState,
          box.measure,
          reviewMode,
          reviewState,
        );
        return {
          measure: box.measure,
          x0: box.x0,
          width: box.x1 - box.x0,
          scoreStartTick: box.score_start_tick,
          scoreEndTick: box.score_end_tick,
          beats: box.beats,
          fill,
          hatch,
          title: `Measure ${box.measure} · ${reviewMode ? timingReviewTitle(box.measure, reviewState) : coverageTitle(measureState, passCounts[box.measure] ?? 0)}`,
        };
      });
    }
    const count = system.last_measure - system.first_measure + 1;
    const width = count > 0 ? 1 / count : 1;
    const segments: {
      measure: number;
      x0: number;
      width: number;
      scoreStartTick: number | null;
      scoreEndTick: number | null;
      beats: { beat_in_measure: number; x: number; confidence: number }[];
      fill: string;
      hatch: boolean;
      title: string;
    }[] = [];
    for (let i = 0; i < count; i++) {
      const measureNumber = system.first_measure + i;
      const measureState = stateByNumber.get(measureNumber);
      const { fill, hatch } = colorForState(
        measureState,
        measureNumber,
        reviewMode,
        reviewState,
      );
      segments.push({
        measure: measureNumber,
        x0: i * width,
        width,
        scoreStartTick: null,
        scoreEndTick: null,
        beats: [],
        fill,
        hatch,
        title: `Measure ${measureNumber} · ${reviewMode ? timingReviewTitle(measureNumber, reviewState) : coverageTitle(measureState, passCounts[measureNumber] ?? 0)}`,
      });
    }
    return segments;
  }

  function isInLatestTake(
    measure: number,
    startMeasure: number | null,
    endMeasure: number | null,
  ): boolean {
    return (
      startMeasure != null &&
      endMeasure != null &&
      measure >= startMeasure &&
      measure <= endMeasure
    );
  }

  function latestTakeGeometry(
    seg: { measure: number; x0: number; width: number },
    startMeasure: number | null,
    endMeasure: number | null,
    startScoreBeat: number | null,
    endScoreBeat: number | null,
  ) {
    const measure = measureStateByNumber.get(seg.measure);
    let start = 0;
    let end = 1;
    if (seg.measure === startMeasure) {
      start = measureProgress(startScoreBeat, measure);
    }
    if (seg.measure === endMeasure) {
      end = measureProgress(endScoreBeat, measure);
    }
    if (seg.measure === startMeasure && seg.measure === endMeasure) {
      end = Math.max(start, end);
    }
    return {
      x0: seg.x0 + seg.width * start,
      width: seg.width * Math.max(0, end - start),
    };
  }

  function latestTakeBoxForSystem(
    segments: { measure: number; x0: number; width: number }[],
    startMeasure: number | null,
    endMeasure: number | null,
    startScoreBeat: number | null,
    endScoreBeat: number | null,
  ): { x0: number; width: number } | null {
    const included = segments.filter((segment) =>
      isInLatestTake(segment.measure, startMeasure, endMeasure),
    );
    if (!included.length) return null;
    const first = latestTakeGeometry(
      included[0],
      startMeasure,
      endMeasure,
      startScoreBeat,
      endScoreBeat,
    );
    const last = latestTakeGeometry(
      included[included.length - 1],
      startMeasure,
      endMeasure,
      startScoreBeat,
      endScoreBeat,
    );
    return {
      x0: first.x0,
      width: Math.max(0, last.x0 + last.width - first.x0),
    };
  }

  function cursorX(
    seg: {
      measure: number;
      x0: number;
      width: number;
      scoreStartTick?: number | null;
      scoreEndTick?: number | null;
      beats?: { beat_in_measure: number; x: number; confidence: number }[];
    },
    scoreBeat: number | null,
  ): number {
    const measure = measureStateByNumber.get(seg.measure);
    const scoreBounds = segmentScoreBounds(seg, measure);
    if (scoreBeat !== null && scoreBounds && seg.beats && seg.beats.length >= 2) {
      const localBeat = Math.max(0, scoreBeat - scoreBounds.start);
      const knots = beatKnots(seg, measure).sort((left, right) => left.beat - right.beat);
      if (localBeat <= knots[0].beat) return knots[0].x;
      for (let index = 1; index < knots.length; index += 1) {
        const left = knots[index - 1];
        const right = knots[index];
        if (localBeat > right.beat) continue;
        const span = right.beat - left.beat;
        const ratio = span <= 0 ? 1 : (localBeat - left.beat) / span;
        return left.x + ratio * (right.x - left.x);
      }
      return knots[knots.length - 1].x;
    }
    if (
      scoreBeat !== null &&
      typeof seg.scoreStartTick === 'number' &&
      typeof seg.scoreEndTick === 'number'
    ) {
      const startBeat = seg.scoreStartTick / CANONICAL_PPQ;
      const endBeat = seg.scoreEndTick / CANONICAL_PPQ;
      const ratio = Math.max(
        0,
        Math.min(1, (scoreBeat - startBeat) / (endBeat - startBeat || 1)),
      );
      return seg.x0 + seg.width * ratio;
    }
    return seg.x0 + seg.width * measureProgress(scoreBeat, measure);
  }

  function beatKnots(
    seg: {
      x0: number;
      width: number;
      scoreStartTick?: number | null;
      scoreEndTick?: number | null;
      beats?: { beat_in_measure: number; x: number }[];
    },
    measure: CoverageMeasure | undefined,
  ): { beat: number; x: number }[] {
    const bounds = segmentScoreBounds(seg, measure);
    const duration = bounds ? bounds.end - bounds.start : 1;
    const right = seg.x0 + seg.width;
    // The per-beat x positions are a separate artifact joined to these boxes,
    // and on ~21% of measures the join is wrong: the beats carry another
    // measure's coordinates and fall entirely OUTSIDE this box. Interpolating
    // between the box edge and a foreign beat position makes the cursor lurch
    // backward and forward even though the score beat advances perfectly
    // smoothly -- the "bouncing" that looked like a follower fault but is not.
    // The beat is monotonic; only this mapping was. So: keep only beats that
    // actually lie within the box (the good 79% stay pixel-accurate), and where
    // they don't, fall back to uniform interpolation across the box (smooth,
    // roughly right) rather than trusting foreign data. A final running max is a
    // belt-and-suspenders guard against any residual disorder. Measured on a
    // real run this takes within-measure backward pixel motion from 127 to 0.
    const trustworthy = (seg.beats ?? [])
      .filter((beat) => beat.beat_in_measure > 0 && beat.beat_in_measure < duration)
      .filter((beat) => beat.x >= seg.x0 - 1e-9 && beat.x <= right + 1e-9)
      .map((beat) => ({ beat: beat.beat_in_measure, x: beat.x }));
    const raw = [
      { beat: 0, x: seg.x0 },
      ...trustworthy,
      { beat: duration, x: right },
    ].sort((a, b) => a.beat - b.beat);
    let maxX = -Infinity;
    return raw.map((knot) => {
      maxX = Math.max(maxX, knot.x);
      return { beat: knot.beat, x: maxX };
    });
  }

  function segmentScoreBounds(
    seg: { scoreStartTick?: number | null; scoreEndTick?: number | null },
    measure: CoverageMeasure | undefined,
  ): { start: number; end: number } | null {
    if (measure) return { start: measure.start_beat, end: measure.end_beat };
    if (
      typeof seg.scoreStartTick === 'number' &&
      typeof seg.scoreEndTick === 'number'
    ) {
      return {
        start: seg.scoreStartTick / CANONICAL_PPQ,
        end: seg.scoreEndTick / CANONICAL_PPQ,
      };
    }
    return null;
  }

  function colorForState(
    m: CoverageMeasure | undefined,
    measureNumber: number,
    reviewMode: boolean,
    reviewState: Record<number, TimingReviewState>,
  ): { fill: string; hatch: boolean } {
    if (reviewMode) {
      const state = reviewState[measureNumber];
      if (state === 'needs-listening') return { fill: 'var(--brass)', hatch: false };
      if (state === 'reviewed') return { fill: 'var(--ready)', hatch: false };
      return { fill: 'transparent', hatch: false };
    }
    if (!m) {
      // Tutti (or unknown): neutral, no wash -- never red, never a to-do.
      return { fill: 'transparent', hatch: false };
    }
    if (m.accompaniment_required === false || m.scope === 'tutti') {
      return { fill: 'var(--ready)', hatch: false };
    }
    // A machine-draft bundle may not yet make the solo/tutti claim, but an
    // aligned take is still honest evidence that Eric rehearsed this printed
    // region. Keep that observed wash visible without promoting it to mature
    // (n-target) coverage.
    if (m.observed && m.state === 'covered') {
      return { fill: 'var(--ready)', hatch: false };
    }
    if (m.observed || m.state === 'touched') {
      return { fill: 'var(--brass)', hatch: false };
    }
    if (m.scope !== 'solo' && !m.solo) {
      return { fill: 'transparent', hatch: false };
    }
    // uncovered solo: dim ivory hatch, not a flat fill and never red.
    return { fill: 'url(#coverage-hatch)', hatch: true };
  }

  function stripState(
    m: CoverageMeasure,
    reviewMode: boolean,
    reviewState: Record<number, TimingReviewState>,
  ): CoverageMeasure['state'] | 'ready' | 'tutti' | 'unknown' | 'timing-needs-listening' | 'timing-spot-check' | 'timing-reviewed' | 'timing-unchecked' {
    if (reviewMode) {
      const state = reviewState[m.measure];
      if (state === 'needs-listening') return 'timing-needs-listening';
      if (state === 'spot-check') return 'timing-spot-check';
      if (state === 'reviewed') return 'timing-reviewed';
      return 'timing-unchecked';
    }
    if (m.accompaniment_required === false || m.scope === 'tutti') return 'ready';
    if (m.observed) return m.state;
    if (m.scope === 'unknown') return 'unknown';
    return m.scope === 'solo' || m.solo ? m.state : 'tutti';
  }

  async function loadAnchors() {
    try {
      const response = await listAnchors({
        path: { movement },
        query: { piece_id: pieceId },
      });
      anchors = response.anchors ?? [];
      anchorsLoaded = true;
    } catch (error) {
      console.error('Error loading anchors:', error);
    }
  }

  $: if (dataAnnotationMode && !anchorsLoaded) void loadAnchors();

  export async function addAnchorAt(
    measureNumber: number,
    scoreBeat: number,
  ): Promise<Anchor | null> {
    const scoreTick = Math.round(scoreBeat * CANONICAL_PPQ);
    try {
      const response = await addAnchorRequest({
        path: { movement },
        query: { piece_id: pieceId },
        body: {
          score_tick: scoreTick,
          measure: measureNumber,
          label: '',
        },
      });
      anchors = response.anchors ?? [];
      return anchors.find((anchor) => anchor.score_tick === scoreTick) ?? null;
    } catch (error) {
      console.error('Error adding anchor:', error);
      return null;
    }
  }

  export async function removeAnchorAt(scoreTick: number): Promise<Anchor | null> {
    const removed = anchors.find((anchor) => anchor.score_tick === scoreTick) ?? null;
    try {
      const response = await deleteAnchorRequest({
        path: { movement, score_tick: scoreTick },
        query: { piece_id: pieceId },
      });
      anchors = response.anchors ?? [];
      return removed;
    } catch (error) {
      console.error('Error removing anchor:', error);
      return null;
    }
  }

  export async function moveAnchorTo(
    scoreTick: number,
    measureNumber: number,
    scoreBeat: number,
  ): Promise<{ before: Anchor; after: Anchor } | null> {
    const before = anchors.find((anchor) => anchor.score_tick === scoreTick);
    if (!before) return null;
    const newScoreTick = Math.round(scoreBeat * CANONICAL_PPQ);
    try {
      const response = await moveAnchorRequest({
        path: { movement, score_tick: scoreTick },
        query: { piece_id: pieceId },
        body: {
          new_score_tick: newScoreTick,
          measure: measureNumber,
          label: before.label,
        },
      });
      anchors = response.anchors ?? [];
      const after = anchors.find((anchor) => anchor.score_tick === newScoreTick);
      return after ? { before, after } : null;
    } catch (error) {
      console.error('Error moving anchor:', error);
      return null;
    }
  }

  export async function clearAnchorsAtMeasure(measureNumber: number): Promise<Anchor[] | null> {
    const removed = anchors.filter((anchor) => anchor.measure === measureNumber);
    try {
      const response = await clearAnchorsInMeasureRequest({
        path: { movement },
        query: { piece_id: pieceId, measure: measureNumber },
      });
      anchors = response.anchors ?? [];
      return removed;
    } catch (error) {
      console.error('Error clearing anchors:', error);
      return null;
    }
  }

  export async function restoreAnchors(restored: Anchor[]): Promise<boolean> {
    try {
      const response = await restoreAnchorsRequest({
        path: { movement },
        query: { piece_id: pieceId },
        body: { anchors: restored },
      });
      anchors = response.anchors ?? [];
      return true;
    } catch (error) {
      console.error('Error restoring anchors:', error);
      await loadAnchors();
      return false;
    }
  }

  // Inverse of `cursorX`: turn a click x (0..1 viewBox space) into a score beat.
  function beatFromX(
    seg: {
      x0: number;
      width: number;
      scoreStartTick?: number | null;
      scoreEndTick?: number | null;
      beats?: { beat_in_measure: number; x: number }[];
    },
    measure: CoverageMeasure | undefined,
    clickX: number,
  ): number {
    const scoreBounds = segmentScoreBounds(seg, measure);
    if (scoreBounds && seg.beats && seg.beats.length >= 2) {
      const knots = beatKnots(seg, measure).sort((left, right) => left.x - right.x);
      if (clickX <= knots[0].x) return scoreBounds.start + knots[0].beat;
      for (let index = 1; index < knots.length; index += 1) {
        if (clickX <= knots[index].x) {
          const span = knots[index].x - knots[index - 1].x || 1;
          const t = (clickX - knots[index - 1].x) / span;
          const localBeat =
            knots[index - 1].beat + t * (knots[index].beat - knots[index - 1].beat);
          return scoreBounds.start + localBeat;
        }
      }
      return scoreBounds.start + knots[knots.length - 1].beat;
    }
    const fraction = Math.max(0, Math.min(1, (clickX - seg.x0) / (seg.width || 1)));
    const start = measure?.start_beat ??
      (typeof seg.scoreStartTick === 'number' ? seg.scoreStartTick / CANONICAL_PPQ : 0);
    const end = measure?.end_beat ??
      (typeof seg.scoreEndTick === 'number' ? seg.scoreEndTick / CANONICAL_PPQ : start + 1);
    return start + fraction * (end - start);
  }

  function anchorDisplayBeat(anchor: Anchor): number {
    if (anchorDrag?.anchor.score_tick === anchor.score_tick) {
      return anchorDrag.previewScoreBeat;
    }
    return anchor.score_tick / CANONICAL_PPQ;
  }

  function startAnchorDrag(
    event: PointerEvent,
    anchor: Anchor,
    seg: {
      measure: number;
      x0: number;
      width: number;
      beats?: { beat_in_measure: number; x: number }[];
    },
  ) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    (event.currentTarget as Element).setPointerCapture(event.pointerId);
    anchorDrag = {
      anchor,
      measure: seg.measure,
      pointerId: event.pointerId,
      startClientX: event.clientX,
      previewScoreBeat: anchor.score_tick / CANONICAL_PPQ,
      moved: false,
    };
  }

  function updateAnchorDrag(
    event: PointerEvent,
    seg: {
      measure: number;
      x0: number;
      width: number;
      beats?: { beat_in_measure: number; x: number }[];
    },
  ) {
    if (!anchorDrag || anchorDrag.pointerId !== event.pointerId) return;
    if (!canvasEl) return;
    const moved = anchorDrag.moved || Math.abs(event.clientX - anchorDrag.startClientX) >= 4;
    anchorDrag = {
      ...anchorDrag,
      previewScoreBeat: scoreBeatAtPointer(event, seg, canvasEl),
      moved,
    };
  }

  async function finishAnchorDrag(
    event: PointerEvent,
    seg: {
      measure: number;
      x0: number;
      width: number;
      beats?: { beat_in_measure: number; x: number }[];
    },
  ) {
    if (!anchorDrag || anchorDrag.pointerId !== event.pointerId) return;
    event.preventDefault();
    event.stopPropagation();
    const drag = anchorDrag;
    anchorDrag = null;
    if (!drag.moved) {
      dispatchMeasureMenu({ event, seg, selectedAnchor: drag.anchor });
      return;
    }
    const result = await moveAnchorTo(
      drag.anchor.score_tick,
      drag.measure,
      drag.previewScoreBeat,
    );
    if (result) {
      dispatch('anchorChanged', {
        kind: 'move',
        before: result.before,
        after: result.after,
        clientX: event.clientX,
        clientY: event.clientY,
      });
    } else {
      dispatch('anchorChangeFailed', {
        message: 'That anchor could not be moved. Another anchor may already occupy that beat.',
        clientX: event.clientX,
        clientY: event.clientY,
      });
    }
  }

  async function loadMeasureBoxes() {
    try {
      // A plain `fetch`, not the generated SDK function: the generated
      // client's response style/throw behavior is fixed at codegen time
      // (design doc §2.2), but this call needs the pre-existing "not built
      // yet" 404 to stay a soft hint rather than a hard error -- the one
      // place that per-status-code branching still matters. The generated
      // type and Zod schema (not a hand-written mirror or `any`) still do
      // the actual shape validation below.
      const response = await fetch(
        `/api/scores/${movement}/measure_boxes?piece_id=${encodeURIComponent(pieceId)}`
      );
      if (response.status === 404) {
        geometryAvailable = false;
        geometryMessage = 'Measure highlighting is waiting for reviewed score geometry.';
        return;
      }
      if (!response.ok) {
        geometryAvailable = false;
        geometryMessage = `Measure highlighting is unavailable (${response.status}).`;
        return;
      }
      measureBoxes = zMeasureBoxesResponse.parse(await response.json());
      if (measureBoxes.review_state === 'machine') {
        geometryMessage = 'Score locations are estimates until musical review.';
      }
    } catch (error) {
      geometryAvailable = false;
      geometryMessage = 'Measure highlighting could not be loaded.';
      console.error('Error loading measure_boxes.json:', error);
    }
  }

  let isDestroyed = false;

  async function loadPdf() {
    try {
      const pdfjsLib = await import('pdfjs-dist');
      const workerUrl = (await import('pdfjs-dist/build/pdf.worker.min.mjs?url')).default;
      pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl;

      const url = `/api/scores/${movement}/pdf?piece_id=${encodeURIComponent(pieceId)}`;
      const loadingTask = pdfjsLib.getDocument(url);
      const doc = await loadingTask.promise;
      if (isDestroyed) {
        // The component unmounted while the (potentially large, 98-page)
        // document was still loading -- onDestroy already ran and had
        // nothing to destroy, so this is the only chance to free it.
        void doc.destroy();
        return;
      }
      pageCount = doc.numPages;
      if (currentPage > pageCount) currentPage = 1;
      // Assigning `pdfDoc` (rather than rendering here directly) is what
      // triggers the `$:` block below to do the first render -- there must
      // be exactly one call site for `renderPage`, or two renders can start
      // concurrently against the same canvas 2D context. pdf.js does not
      // handle that gracefully: it does not reliably throw, it wedges the
      // render pipeline (reproduced -- two racing `renderPage` calls, one
      // from here and one from the reactive block, hung indefinitely with
      // no error, even though either call alone renders in well under
      // 100ms).
      pdfDoc = doc;
    } catch (error) {
      pdfAvailable = false;
      pdfErrorMessage = 'The performer score is unavailable. Pull the Movement 2 DVC artifact.';
      console.error('Error loading score PDF:', error);
    }
  }

  let currentRenderTask: { promise: Promise<void>; cancel: () => void } | null = null;
  let lastRenderedDoc: any = null;
  let lastRenderedPage: number | null = null;
  let lastRenderedCanvas: HTMLCanvasElement | null = null;
  let lastRenderedScaleKey: string | null = null;

  // The render scale depends on the display mode: rehearsal fits to a fixed
  // logical width; performance view fits the viewport width so notes are large.
  // Keyed so `renderPage` re-rasterizes when the mode or available width
  // changes, not only when the page does.
  $: scaleKey = performanceMode
    ? `perf:${Math.round(fitWidthPx ?? 0)}`
    : `width:${Math.round(rehearsalWidthPx / 20)}`;

  async function renderPage(pageNum: number) {
    if (!pdfDoc || !canvasEl) return;
    // Idempotency lives here (not in the `$:` block below) so the reactive
    // statement can stay a plain, single-dependency-set expression -- once
    // this component re-renders for any unrelated reason, `$: if (pdfDoc &&
    // canvasEl) { void renderPage(currentPage); }` fires again, and the
    // no-op return below makes that cheap instead of reopening the
    // concurrent-render hazard the comment above describes. `canvasEl` is
    // tracked alongside doc/page so that if Svelte ever recreates the
    // canvas element (e.g. a future change to the `{#if}` around it), a
    // same-doc-same-page re-fire still repaints the new element instead of
    // leaving it blank.
    if (
      pdfDoc === lastRenderedDoc &&
      pageNum === lastRenderedPage &&
      canvasEl === lastRenderedCanvas &&
      scaleKey === lastRenderedScaleKey
    ) {
      return;
    }
    lastRenderedDoc = pdfDoc;
    lastRenderedPage = pageNum;
    lastRenderedCanvas = canvasEl;
    lastRenderedScaleKey = scaleKey;
    renderedPage = 0;

    if (currentRenderTask) {
      currentRenderTask.cancel();
      currentRenderTask = null;
    }
    const myToken = ++renderToken;
    let task: { promise: Promise<void>; cancel: () => void } | null = null;
    try {
      // getPage (like task.promise below) can reject -- a destroyed
      // document, a worker that died mid-flight -- and renderPage is always
      // invoked fire-and-forget (`void renderPage(...)` from the reactive
      // block below), so the whole body is one try/catch rather than just
      // wrapping task.promise: an uncaught rejection from getPage would be
      // just as unhandled as one from the render call.
      const page = await pdfDoc.getPage(pageNum);
      // The component can unmount (or `canvasEl` can go away) while this
      // `getPage` await is in flight; without this guard the width/height
      // assignments just below would throw on a null canvas.
      if (isDestroyed || !canvasEl) return;
      const naturalViewport = page.getViewport({ scale: 1 });
      let scale: number;
      if (performanceMode && fitWidthPx && fitWidthPx > 0) {
        // Fill the viewport width so the engraving is large; the tall page then
        // scrolls vertically. Cap at 3x so a small page stays crisp.
        scale = Math.min(fitWidthPx / naturalViewport.width, 3.0);
      } else {
        scale = Math.min(rehearsalWidthPx / naturalViewport.width, 2.0);
      }
      const viewport = page.getViewport({ scale });
      if (myToken !== renderToken) return; // a newer page render started meanwhile

      // Size the canvas's backing store at device-pixel resolution (2x/3x
      // on Retina/high-DPI screens) so score engraving stays crisp instead
      // of rendering at CSS-pixel resolution and getting blurred on
      // upscale -- `containerWidth` (which drives the wrapper's max-width,
      // and the CSS max-width:100%/height:auto on the canvas itself) stays
      // in logical pixels, so displayed size is unaffected.
      const dpr = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1;
      canvasEl.width = viewport.width * dpr;
      canvasEl.height = viewport.height * dpr;
      containerWidth = viewport.width;

      const ctx = canvasEl.getContext('2d');
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const renderTask = page.render({ canvasContext: ctx, viewport });
      task = renderTask;
      currentRenderTask = renderTask;
      await renderTask.promise;
      if (myToken === renderToken) renderedPage = pageNum;
    } catch (error: any) {
      if (error?.name !== 'RenderingCancelledException') {
        console.error('PDF page render failed:', error);
      }
    } finally {
      if (task && currentRenderTask === task) currentRenderTask = null;
    }
  }

  $: if (pdfDoc && canvasEl && scaleKey) {
    // `scaleKey` is listed so a mode/width change re-rasterizes the same page
    // at the new scale; `renderPage` still no-ops when nothing relevant moved.
    void renderPage(currentPage);
  }

  // Keep the active system in view as the cursor advances -- in EVERY mode, not
  // only the performance view. The page auto-turns (see `autoMeasure` above) in
  // all modes, so gating the *scroll* on performanceMode meant that during a
  // recording take the page would flip and leave the cursor below the fold with
  // nothing bringing it back. The performer is at the piano and cannot go
  // hunting for it.
  //
  // Two scroll containers, because the two layouts scroll differently:
  //   - performance view: the wrap is fit-to-width with its own vertical
  //     scroll, so scroll the wrap.
  //   - split Data view: the owning score shell scrolls independently.
  //   - ordinary rehearsal view: the window scrolls.
  $: if (pageWrapEl && renderedPage === currentPage) {
    scrollActiveSystemIntoView(autoMeasure, currentPage, pageSegments, scrollContainer);
  }

  function scrollActiveSystemIntoView(
    measure: number | null,
    _page: number,
    segmentsBySystem: { system: MeasureBoxSystemResponse; segments: unknown[] }[],
    owningScrollContainer: HTMLElement | null,
  ) {
    const wrap = pageWrapEl;
    if (!wrap || measure === null) return;
    const active = segmentsBySystem.find(({ segments }) =>
      (segments as { measure: number }[]).some((seg) => seg.measure === measure),
    );
    if (!active) return;
    const center = (active.system.y0 + active.system.y1) / 2;
    requestAnimationFrame(() => {
      const el = pageWrapEl;
      if (!el) return;
      if (performanceMode) {
        const target = center * el.scrollHeight - el.clientHeight / 2;
        const max = el.scrollHeight - el.clientHeight;
        el.scrollTo({ top: Math.max(0, Math.min(max, target)), behavior: 'smooth' });
        return;
      }
      const box = el.getBoundingClientRect();
      const systemViewportY = box.top + center * box.height;
      const containerOverflow = owningScrollContainer
        ? window.getComputedStyle(owningScrollContainer).overflowY
        : '';
      if (
        owningScrollContainer &&
        ['auto', 'scroll', 'overlay'].includes(containerOverflow) &&
        owningScrollContainer.scrollHeight > owningScrollContainer.clientHeight
      ) {
        const containerBox = owningScrollContainer.getBoundingClientRect();
        const delta =
          systemViewportY - (containerBox.top + owningScrollContainer.clientHeight / 2);
        if (Math.abs(delta) < 24) return;
        owningScrollContainer.scrollBy({ top: delta, behavior: 'smooth' });
        return;
      }
      // Ordinary rehearsal view: translate the system's normalized position
      // within the page into document space and center it in the viewport.
      const delta = systemViewportY - window.innerHeight / 2;
      if (Math.abs(delta) < 24) return;
      window.scrollBy({ top: delta, behavior: 'smooth' });
    });
  }

  function goToPrevPage() {
    if (currentPage > 1) currentPage -= 1;
  }

  function goToNextPage() {
    if (currentPage < pageCount) currentPage += 1;
  }

  onMount(() => {
    // Geometry is progressive enrichment.  The performer must always be able
    // to read the score while OMR/manual anchors are still being reviewed.
    void loadMeasureBoxes();
    void loadPdf();
    if (dataAnnotationMode) void loadAnchors();
  });

  onDestroy(() => {
    isDestroyed = true;
    renderToken += 1;
    if (currentRenderTask) {
      currentRenderTask.cancel();
      currentRenderTask = null;
    }
    if (pdfDoc) {
      void pdfDoc.destroy();
    }
  });
</script>

<div
  class="pdf-coverage-overlay"
  class:performance-mode={performanceMode}
  class:timing-review-mode={timingReviewMode}
  bind:clientWidth={hostWidthPx}
>
  {#if !pdfAvailable}
    <p class="hint">{pdfErrorMessage || 'Performer score not available for this movement yet.'}</p>
  {:else}
    {#if showDataOverlay && !performanceMode && geometryAvailable && measureBoxes}
      <details class="score-guide" data-testid="score-guide" aria-label="Score overlay guide">
        {#if timingReviewMode}
          <summary>Timing marks</summary>
          <span class="guide-item"><i class="guide-swatch guide-recorded"></i>Listen</span>
          <span class="guide-item"><i class="guide-swatch guide-spot-check"></i>Spot check</span>
          <span class="guide-item"><i class="guide-swatch guide-repeated"></i>Confirmed</span>
        {:else}
          <summary>What the score shows</summary>
          <span class="guide-item"><i class="guide-swatch guide-recorded"></i>Amber · learning; a half-beat is below {coverage?.n_target ?? 3} observations or {Math.round((coverage?.quality_target ?? 0.5) * 100)}% alignment quality</span>
          <span class="guide-item"><i class="guide-swatch guide-repeated"></i>Green · ready; the orchestra leads, has nothing to play, or every half-beat meets both evidence targets</span>
          <span class="guide-item"><i class="guide-swatch guide-needed"></i>Gray hatch · no aligned take yet</span>
          <span class="guide-item"><i class="guide-swatch guide-unknown"></i>No overlay · recognition pending</span>
          <span class="guide-item"><i class="guide-swatch guide-newest"></i>Dashed outline · newest recording</span>
          <span class="guide-item"><i class="guide-swatch guide-now"></i>Red line · playing now</span>
          <small>Color is rehearsal guidance, not an alignment error. Record again to strengthen amber; another green pass is optional. Right-click a measure for actions.</small>
        {/if}
      </details>
      <!-- Both of these were standing sentences of explanation above the score.
           The estimate caveat now rides on the score guide (which is where the
           rest of the "what am I looking at" copy lives), and the anchor help
           is a tooltip on its own count -- the instruction only matters at the
           moment you go looking for it. -->
      {#if !timingReviewMode}
        <p class="anchor-help" title="Right-click the exact bass onset to add, move, or remove an anchor.{geometryMessage ? ' ' + geometryMessage : ''}">
          <span>◆</span>
          {anchors.length}
          {anchors.length === 1 ? 'anchor' : 'anchors'}
        </p>
      {/if}
      <div class="measure-strip" role="group" aria-label={timingReviewMode ? 'Measure timing review and page scrubber' : 'Measure coverage and page scrubber'}>
        {#each pageGroups as group (group.page)}
          <div class="page-group" class:page-group-current={group.page === currentPage}>
            <div class="page-cells">
              {#each group.measures as m (m.measure)}
                <button
                  type="button"
                  class="strip-cell strip-{stripState(m, timingReviewMode, timingReview)}"
                  class:strip-latest={!timingReviewMode && isInLatestTake(
                    m.measure,
                    latestTakeStartMeasure,
                    latestTakeEndMeasure,
                  )}
                  class:strip-current={currentMeasure === m.measure}
                  title="Measure {m.measure} — page {group.page} ({timingReviewMode ? timingReviewTitle(m.measure, timingReview) : stripState(m, timingReviewMode, timingReview)})"
                  class:strip-selected={selectedMeasure === m.measure}
                  class:strip-in-span={stripSpanFrom !== null &&
                    stripSpanTo !== null &&
                    m.measure >= stripSpanFrom &&
                    m.measure <= stripSpanTo}
                  class:strip-span-anchor={spanAnchor === m.measure && stripDrag === null}
                  on:pointerdown={(e) => beginStripDrag(m.measure, e)}
                  on:pointerenter={() => extendStripDrag(m.measure)}
                  on:pointerup={endStripDrag}
                ></button>
              {/each}
            </div>
            <!-- The label is the page control. Clicking it turns the page
                 without disturbing the selected measure, so the strip answers
                 "where am I" and "take me there" in one place. -->
            <button
              type="button"
              class="page-tick"
              aria-label="Go to page {group.page} (measures {group.measures[0]
                ?.measure}–{group.measures[group.measures.length - 1]?.measure})"
              aria-current={group.page === currentPage ? 'page' : undefined}
              on:click={() => (currentPage = group.page)}
            >{group.page}</button>
          </div>
        {/each}
      </div>
    {:else if showDataOverlay && !performanceMode && geometryMessage}
      <p class="geometry-hint">{geometryMessage} The reduction remains available for rehearsal.</p>
    {:else if mixAuthoringMode && geometryMessage}
      <p class="geometry-hint">{geometryMessage} Mix ranges use the best available score map.</p>
    {/if}

    <div
      class="position-readout"
      class:position-active={positionActive}
      class:position-empty={!positionActive}
      data-testid="score-position-cursor"
      data-score-beat={currentScoreBeat ?? ''}
      data-score-measure={currentMeasure ?? ''}
      data-page={currentPage}
      role="status"
      aria-live="polite"
      aria-label={positionLabel}
    >
      <!-- The element itself always exists: `data-score-beat` is the cursor
           contract the tests (and any future instrumentation) read. But when
           there is no position, a full-width bar announcing that a position
           will appear later is pure noise -- the masthead readout already says
           IDLE. So it only draws itself once it has something to say. -->
      {#if positionActive}
        <span class="position-pip" aria-hidden="true"></span>
        <span>
          <small>{positionLabel}</small>
        </span>
      {/if}
    </div>

    {#if showDataOverlay && !timingReviewMode && !performanceMode && latestTakeStartMeasure !== null && latestTakeEndMeasure !== null}
      <p
        class="latest-take-key"
        data-testid="latest-take-span"
        data-score-start={latestTakeStartScoreBeat ?? ''}
        data-score-end={latestTakeEndScoreBeat ?? ''}
        aria-label="Newest recording, measures {latestTakeStartMeasure} through {latestTakeEndMeasure}"
      >
        <strong>Newest recording</strong>
        <span>Dashed outline on the score · m. {latestTakeStartMeasure}–{latestTakeEndMeasure}</span>
      </p>
    {/if}

    <div
      class="pdf-page-wrap"
      bind:this={pageWrapEl}
      data-testid="performer-score-page"
      style="max-width: {containerWidth}px; width: 100%;{performanceMode && fitHeightPx
        ? ` max-height: ${fitHeightPx}px; overflow-y: auto;`
        : ''}"
    >
      <!--
        The canvas and its SVG overlay share this inner wrapper so the overlay's
        0..1 coordinate box always maps to the *canvas* height. The outer wrap
        owns the performance-mode scroll (max-height + overflow-y); if the SVG
        sized to that scroll viewport instead, the cursor and boxes would
        compress vertically once the page is taller than the viewport.
      -->
      <div class="pdf-page-canvas">
      <canvas
        bind:this={canvasEl}
        data-rendered-page={renderedPage}
        draggable="false"
        aria-hidden="true"
      ></canvas>
      {#if geometryAvailable && measureBoxes && renderedPage === currentPage}
      <svg class="pdf-overlay-svg" viewBox="0 0 1 1" preserveAspectRatio="none">
        <defs>
          <pattern
            id="coverage-hatch"
            width="0.02"
            height="0.02"
            patternUnits="objectBoundingBox"
            patternTransform="rotate(45)"
          >
            <rect width="0.02" height="0.02" fill="transparent" />
            <line x1="0" y1="0" x2="0" y2="0.02" stroke="var(--ink-dim)" stroke-width="0.006" />
          </pattern>
        </defs>
        {#each pageSegments as { system, segments }, systemIndex}
          {#each segments as seg}
            <rect
              data-testid={`score-measure-${seg.measure}`}
              data-measure={seg.measure}
              data-page={currentPage}
              data-system={systemIndex}
              x={seg.x0}
              y={system.y0}
              width={seg.width}
              height={system.y1 - system.y0}
              fill={showDataOverlay ? seg.fill : 'transparent'}
              fill-opacity={showDataOverlay ? seg.hatch ? 0.6 : 0.3 : 0}
              stroke={showDataOverlay ? 'var(--stage-hairline)' : 'transparent'}
              stroke-width="1"
              vector-effect="non-scaling-stroke"
              class:measure-selected={showDataOverlay && selectedMeasure === seg.measure}
              class:mix-measure-target={mixAuthoringMode}
              class="measure-hit-target"
              role="button"
              tabindex="0"
              aria-haspopup={timingReviewMode ? 'menu' : mixAuthoringMode ? undefined : 'menu'}
              aria-label={mixAuthoringMode
                ? `Mix range in measure ${seg.measure}`
                : dataAnnotationMode
                  ? seg.title
                  : `Measure ${seg.measure}`}
              on:pointerdown={beginMixSelection}
              on:pointermove={moveMixSelection}
              on:pointerup={finishMixSelection}
              on:pointercancel={() => (mixDrag = null)}
              on:dragstart|preventDefault
              on:click={() => !mixAuthoringMode && selectMeasure(seg.measure)}
              on:contextmenu={(event) => {
                if (timingReviewMode) openMeasureMenu(event, seg);
                else if (mixAuthoringMode) openMixContext(event);
                else openMeasureMenu(event, seg);
              }}
              on:keydown={(event) => !mixAuthoringMode && selectMeasureByKey(event, seg.measure)}
            >
              <title>{mixAuthoringMode ? `Measure ${seg.measure}; drag or shift-click to select a mixing range` : timingReviewMode ? `Measure ${seg.measure}; click to select, right-click to play` : dataAnnotationMode ? `${seg.title}; right-click a bass onset to edit reactive anchors` : `Measure ${seg.measure}; click for passage actions`}</title>
            </rect>
            {#if showMixPlan}
              {#each mixRegions as region (region.region_id)}
                {@const envelope = mixEnvelopeGeometry(seg, system, region)}
                {#if envelope}
                  <path
                    d={`M ${envelope.x0} ${envelope.y0 + 0.008} L ${envelope.x0} ${envelope.y0} L ${envelope.x1} ${envelope.y1} L ${envelope.x1} ${envelope.y1 + 0.008}`}
                    class="mix-automation-bracket"
                    class:mix-region-disabled={!region.enabled}
                    class:mix-performance-cue={!mixAuthoringMode}
                    class:mix-cue-active={mixRegionIsActive(region)}
                    data-testid="mix-region-{region.region_id}"
                    data-region-id={region.region_id}
                    role="button"
                    tabindex="0"
                    aria-label={`${region.gesture} mix region ${region.region_id}`}
                    on:contextmenu={(event) => openMixContext(event, region.region_id)}
                  >
                    <title>{region.gesture} · {region.routes.map((route) => route.zone_id).join(', ')}</title>
                  </path>
                  {#if envelope.hasStart}
                    <path
                      d={`M ${envelope.x0 - 0.007} ${envelope.y0 - 0.014} L ${envelope.x0 + 0.007} ${envelope.y0 - 0.014} L ${envelope.x0} ${envelope.y0 - 0.001} Z`}
                      class="mix-control-point"
                      class:mix-performance-cue={!mixAuthoringMode}
                      role="button"
                      tabindex={mixAuthoringMode ? 0 : -1}
                      aria-label={`Edit ${region.gesture} start point`}
                      on:contextmenu={(event) => openMixContext(event, region.region_id)}
                    />
                  {/if}
                  {#if envelope.hasEnd}
                    <path
                      d={`M ${envelope.x1 - 0.007} ${envelope.y1 - 0.014} L ${envelope.x1 + 0.007} ${envelope.y1 - 0.014} L ${envelope.x1} ${envelope.y1 - 0.001} Z`}
                      class="mix-control-point mix-control-point-end"
                      class:mix-performance-cue={!mixAuthoringMode}
                      role="button"
                      tabindex={mixAuthoringMode ? 0 : -1}
                      aria-label={`Edit ${region.gesture} end point`}
                      on:contextmenu={(event) => openMixContext(event, region.region_id)}
                    />
                  {/if}
                {/if}
              {/each}
              {#if mixAuthoringMode && mixSelectionStartTick !== null && mixSelectionEndTick !== null}
                {@const selectionBox = mixGeometry(seg, mixSelectionStartTick, mixSelectionEndTick)}
                {#if selectionBox}
                  <rect
                    x={selectionBox.x0}
                    y={system.y0}
                    width={selectionBox.width}
                    height={system.y1 - system.y0}
                    class="mix-selection-band"
                    data-testid="mix-selection"
                  />
                {/if}
              {/if}
              {#if mixAuthoringMode && mixDraftStartTick !== null}
                {@const draftPoint = mixGeometry(seg, mixDraftStartTick, mixDraftStartTick + 1)}
                {#if draftPoint}
                  <path
                    d={`M ${draftPoint.x0 - 0.007} ${Math.max(0.004, system.y0 - 0.04)} L ${draftPoint.x0 + 0.007} ${Math.max(0.004, system.y0 - 0.04)} L ${draftPoint.x0} ${Math.max(0.004, system.y0 - 0.027)} Z`}
                    class="mix-control-point mix-draft-point"
                    data-testid="mix-draft-start"
                  >
                    <title>Mix effect start</title>
                  </path>
                {/if}
              {/if}
            {/if}
            {#if currentMeasure === seg.measure}
              <line
                data-testid="score-position-line"
                data-score-beat={currentScoreBeat ?? ''}
                data-measure={seg.measure}
                data-page={currentPage}
                data-system={systemIndex}
                data-cursor-x={cursorX(seg, currentScoreBeat)}
                x1={cursorX(seg, currentScoreBeat)}
                x2={cursorX(seg, currentScoreBeat)}
                y1={system.y0 - 0.01}
                y2={system.y1 + 0.01}
                class="position-cursor-line"
                vector-effect="non-scaling-stroke"
              />
            {/if}
          {/each}
          {@const takeBox = latestTakeBoxForSystem(
            segments,
            latestTakeStartMeasure,
            latestTakeEndMeasure,
            latestTakeStartScoreBeat,
            latestTakeEndScoreBeat,
          )}
          {#if takeBox && showDataOverlay && !timingReviewMode}
            <rect
              x={takeBox.x0}
              y={system.y0}
              width={takeBox.width}
              height={system.y1 - system.y0}
              class="latest-take-box"
              vector-effect="non-scaling-stroke"
            />
          {/if}
        {/each}
      </svg>
      <div class="score-annotations">
        {#each pageSegments as { system, segments }}
          {#each segments as seg}
            {#if dataAnnotationMode && !performanceMode}
            {#each anchors.filter((anchor) => anchor.measure === seg.measure) as anchor (anchor.score_tick)}
              <button
                type="button"
                class="anchor-marker"
                class:is-dragging={anchorDrag?.anchor.score_tick === anchor.score_tick}
                data-testid="anchor-marker"
                aria-label={`Reactive anchor in measure ${anchor.measure}; drag to move, click for actions`}
                title="Reactive anchor — drag to move, click or right-click for actions"
                style="left: {cursorX(seg, anchorDisplayBeat(anchor)) * 100}%; top: {system.y0 * 100}%;"
                on:pointerdown={(event) => startAnchorDrag(event, anchor, seg)}
                on:pointermove={(event) => updateAnchorDrag(event, seg)}
                on:pointerup={(event) => finishAnchorDrag(event, seg)}
                on:pointercancel={() => (anchorDrag = null)}
                on:contextmenu={(event) => {
                  event.preventDefault();
                  event.stopPropagation();
                  dispatchMeasureMenu({ event, seg, selectedAnchor: anchor });
                }}
                on:click={(event) => {
                  if (event.detail === 0) {
                    dispatchMeasureMenu({ event, seg, selectedAnchor: anchor });
                  }
                }}
              ></button>
            {/each}
            {/if}
            <span
              class="measure-number-label"
              aria-hidden="true"
              class:is-selected={selectedMeasure === seg.measure}
              data-testid="score-measure-label-{seg.measure}"
              style="left: {seg.x0 * 100}%; top: {system.y0 * 100}%;"
            >m. {seg.measure}{#if showDataOverlay && !timingReviewMode && (takeCountsByMeasure[seg.measure] ?? 0) > 0}<b class="measure-pass-count">{takeCountsByMeasure[seg.measure]} {(takeCountsByMeasure[seg.measure] ?? 0) === 1 ? 'pass' : 'passes'}</b>{/if}{#if showDataOverlay && !timingReviewMode && needsInfo[seg.measure]}<b
                class="needs-info needs-{needsInfo[seg.measure].severity}"
                title={needsInfo[seg.measure].reason}
              >{needsInfo[seg.measure].severity === 'error' ? '?' : '~'}</b>{/if}{#if timingReviewMode && timingReview[seg.measure]}<b
                class="timing-review-mark timing-{timingReview[seg.measure]}"
                title={timingReviewTitle(seg.measure, timingReview)}
              >{timingReview[seg.measure] === 'needs-listening' ? '!' : timingReview[seg.measure] === 'reviewed' ? '✓' : '•'}</b>{/if}</span>
            {#if !mixAuthoringMode && selectedMeasure === seg.measure}
              <span
                class="score-badge target-badge"
                data-testid="score-target-badge"
                style="left: {seg.x0 * 100}%; top: {system.y0 * 100}%;"
              >{timingReviewMode ? `Audition m. ${seg.measure}` : 'Selected passage'}</span>
            {/if}
            {#if showDataOverlay && !timingReviewMode && firstLatestMeasureOnPage === seg.measure}
              <span
                class="score-badge newest-badge"
                data-testid="score-newest-take-badge"
                style="left: {seg.x0 * 100}%; top: {system.y1 * 100}%;"
              >Newest recording</span>
            {/if}
            {#if currentMeasure === seg.measure}
              <span
                class="score-badge now-badge"
                data-testid="score-now-badge"
                style="left: {cursorX(seg, currentScoreBeat) * 100}%; top: {system.y0 * 100}%;"
              >Now</span>
            {/if}
            {#if showMixPlan}
              {#each mixRegions as region (region.region_id)}
                {@const envelope = mixEnvelopeGeometry(seg, system, region)}
                {#if envelope?.hasStart}
                  <span
                    class="mix-cue-icon"
                    class:mix-performance-cue={!mixAuthoringMode}
                    class:mix-cue-active={mixRegionIsActive(region)}
                    data-testid="mix-cue-icon-{region.region_id}"
                    title={`${region.gesture} · ${region.routes.map((route) => route.zone_id).join(', ')}`}
                    style="left: {envelope.x0 * 100}%; top: {envelope.y0 * 100}%;"
                  >{mixGestureIcon(region.gesture)}</span>
                {/if}
              {/each}
            {/if}
          {/each}
        {/each}
      </div>
      {/if}
      </div>
    </div>

    <div class="pdf-page-controls" class:performance-controls={performanceMode}>
      <button type="button" class="btn btn-ghost" on:click={goToPrevPage} disabled={currentPage <= 1}>
        &#9664;
      </button>
      <span class="page-indicator">page {currentPage} / {pageCount}</span>
      <button
        type="button"
        class="btn btn-ghost"
        on:click={goToNextPage}
        disabled={currentPage >= pageCount}
      >
        &#9654;
      </button>
    </div>
  {/if}
</div>

<style>
  .pdf-coverage-overlay {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  /* Performance view: center the width-filled page horizontally and trim the
     inter-element gaps. The page wrap owns its own vertical scroll (set inline
     from fitHeightPx). Chrome is removed in markup above. */
  .pdf-coverage-overlay.performance-mode {
    align-items: center;
    gap: 0.35rem;
  }

  .performance-mode .pdf-page-wrap {
    scrollbar-width: thin;
  }

  .performance-mode .position-readout {
    font-size: 0.85rem;
  }

  .performance-controls {
    opacity: 0.55;
  }

  .performance-controls:hover {
    opacity: 1;
  }

  .hint {
    color: var(--ink-dim);
    font-size: 0.85rem;
    margin: 0.5rem 0 0;
  }

  .geometry-hint {
    color: var(--ink-muted);
    font-size: 0.85rem;
    margin: 0;
  }

  /* Collapsed by default so the legend is available but not always shouting;
     opening reveals the swatches in the same wrapped-flex layout as before. */
  .score-guide {
    padding: 0.55rem 0.8rem;
    border: 1px solid var(--stage-hairline);
    border-radius: var(--radius-sm);
    background: var(--stage-panel-raised);
    color: var(--ink-muted);
    font-size: 0.72rem;
  }

  .score-guide[open] {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem 1rem;
  }

  .score-guide summary {
    cursor: pointer;
    color: var(--ink);
    font-family: 'Fraunces', Georgia, serif;
    font-size: 0.82rem;
    list-style: revert;
  }

  .score-guide[open] summary {
    flex-basis: 100%;
    margin-bottom: 0.15rem;
  }

  .score-guide small {
    flex-basis: 100%;
    color: var(--ink-dim);
  }

  .guide-item {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    white-space: nowrap;
  }

  .guide-swatch {
    display: inline-block;
    width: 1.25rem;
    height: 0.8rem;
    flex: 0 0 auto;
    border-radius: 2px;
  }

  .guide-recorded {
    background: rgba(216, 169, 76, 0.65);
  }

  .guide-repeated {
    background: rgba(127, 174, 103, 0.75);
  }

  .guide-spot-check {
    border: 1px solid rgba(244, 239, 230, 0.55);
    background: transparent;
  }

  .guide-needed {
    background: repeating-linear-gradient(
      45deg,
      rgba(94, 96, 105, 0.6),
      rgba(94, 96, 105, 0.6) 2px,
      transparent 2px,
      transparent 5px
    );
    border: 1px solid rgba(244, 239, 230, 0.2);
  }

  .guide-unknown {
    border: 1px dotted var(--ink-dim);
    background: transparent;
  }

  .guide-newest {
    border: 2px dashed var(--brass-strong);
  }

  .guide-now {
    width: 3px;
    margin: 0 0.55rem;
    background: var(--ember);
  }

  .measure-strip {
    display: flex;
    flex-wrap: wrap;
    /* Gap between PAGES, not between measures: the space itself is the page
       division, which is what makes the structure readable without hovering. */
    gap: 0 6px;
    row-gap: 0.9rem;
    border-radius: var(--radius-sm);
    overflow: visible;
    align-items: flex-start;
  }

  .timing-review-mode .measure-strip {
    position: sticky;
    top: 0;
    z-index: 12;
    padding: 0.45rem 0.35rem 0.35rem;
    background: rgba(28, 30, 38, 0.96);
    box-shadow: 0 6px 12px rgba(18, 19, 23, 0.7);
    backdrop-filter: blur(6px);
  }

  .page-group {
    display: flex;
    flex-direction: column;
    gap: 2px;
    /* Groups share the row in proportion to how many measures they hold, so a
       page's width on the strip reflects its share of the piece. */
    flex: 1 1 0;
    min-width: 0;
  }

  .page-cells {
    display: flex;
    gap: 1px;
  }

  .page-tick {
    border: none;
    background: none;
    padding: 0;
    font: inherit;
    font-size: 0.62rem;
    line-height: 1;
    font-variant-numeric: tabular-nums;
    color: var(--ink-soft, #8a8378);
    cursor: pointer;
    text-align: center;
    border-top: 1px solid var(--rule, rgba(0, 0, 0, 0.18));
    padding-top: 2px;
    opacity: 0.75;
  }

  .page-tick:hover {
    opacity: 1;
    color: var(--ink);
  }

  .page-group-current .page-tick {
    opacity: 1;
    font-weight: 700;
    color: var(--ink);
    border-top-color: var(--ember, #d1483a);
    border-top-width: 2px;
    padding-top: 1px;
  }

  .strip-cell {
    flex: 1 0 3px;
    min-width: 3px;
    height: 14px;
    border: none;
    cursor: pointer;
    padding: 0;
    position: relative;
  }

  .strip-in-span {
    /* The span being drawn. Brass rather than the ember used for "you are
       here", so a selection never reads as a position. */
    box-shadow: inset 0 0 0 2px var(--brass-strong, #c9a227);
    transform: scaleY(1.35);
    z-index: 1;
  }

  .strip-span-anchor {
    /* One end of a pending shift-click span. Dashed so it reads as "waiting
       for the other end" rather than as a completed selection. */
    box-shadow: inset 0 0 0 2px var(--brass-strong, #c9a227);
    outline: 1px dashed var(--brass-strong, #c9a227);
    outline-offset: 1px;
  }

  .strip-latest {
    box-shadow: inset 0 0 0 2px var(--brass-strong);
  }

  .strip-current {
    z-index: 2;
    transform: scaleY(1.65);
    background: var(--ink) !important;
    box-shadow: 0 0 0 2px var(--ember), 0 0 12px rgba(209, 72, 58, 0.75);
  }

  .strip-covered {
    background: var(--ready);
    opacity: 0.75;
  }

  .strip-ready {
    background: var(--ready);
    opacity: 0.75;
  }

  .strip-touched {
    background: var(--brass);
    opacity: 0.6;
  }

  .strip-uncovered {
    background: var(--ink-dim);
    opacity: 0.5;
  }

  .strip-tutti {
    background: transparent;
    border: 1px solid var(--stage-hairline);
  }

  .strip-unknown {
    background: transparent;
    border: 1px dotted var(--ink-dim);
    opacity: 0.45;
  }

  .strip-timing-needs-listening {
    background: var(--brass);
    min-width: 7px;
    height: 18px;
    opacity: 1;
    box-shadow: 0 0 0 2px var(--brass-strong), 0 0 8px rgba(216, 169, 76, 0.7);
    z-index: 1;
  }

  .strip-timing-spot-check {
    min-width: 5px;
    height: 18px;
    background: rgba(244, 239, 230, 0.24);
    border: 2px solid rgba(244, 239, 230, 0.85);
    opacity: 1;
    z-index: 1;
  }

  .strip-timing-reviewed {
    background: var(--ready);
    opacity: 0.85;
  }

  .strip-timing-unchecked {
    background: transparent;
    border: 1px solid var(--stage-hairline);
    opacity: 0.45;
  }

  .pdf-page-wrap {
    position: relative;
    max-width: 100%;
    margin-inline: auto;
    background: var(--stage-panel);
    border-radius: var(--radius-md);
    overflow: hidden;
    /*
     * The PDF canvas and its SVG hit targets are one score control.  Without
     * this, a tiny pointer movement while choosing a measure can make Chromium
     * select the replaced canvas element and paint the entire score native
     * blue.  That looks like the PDF disappeared even though the pixels are
     * still present underneath the browser selection highlight.
     */
    user-select: none;
    -webkit-user-select: none;
  }

  .position-readout.position-empty {
    display: none;
  }

  .position-readout {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    min-height: 3.25rem;
    padding: 0.65rem 0.9rem;
    border: 1px solid var(--stage-hairline);
    border-radius: var(--radius-md);
    background: var(--stage-panel-raised);
    color: var(--ink-muted);
  }

  .position-readout.position-active {
    color: var(--ink);
    border-color: rgba(209, 72, 58, 0.6);
    box-shadow: inset 3px 0 0 var(--ember);
  }

  .position-readout > span:last-child {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.35rem 0.65rem;
  }


  .position-readout small {
    color: var(--ink-dim);
    font-size: 0.68rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .position-pip {
    width: 0.7rem;
    height: 0.7rem;
    flex: 0 0 auto;
    border: 2px solid currentColor;
    border-radius: 50%;
  }

  .position-active .position-pip {
    background: var(--ember);
    border-color: var(--ember);
    box-shadow: 0 0 0 4px rgba(209, 72, 58, 0.18);
  }

  .latest-take-key {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    margin: -0.25rem 0 0;
    color: var(--ink-muted);
    font-size: 0.78rem;
  }

  .latest-take-key strong {
    padding: 0.18rem 0.42rem;
    border: 1px dashed var(--brass-strong);
    border-radius: 3px;
    color: var(--brass-strong);
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }

  /*
   * Sized to the canvas (not the scroll viewport) so the absolutely-positioned
   * SVG overlay and annotation layer share the canvas's exact box in both the
   * normal and full-screen (scrolling) score views.
   */
  .pdf-page-canvas {
    position: relative;
    display: block;
    line-height: 0;
  }

  .pdf-page-wrap canvas {
    display: block;
    max-width: 100%;
    height: auto;
    user-select: none;
    -webkit-user-select: none;
    -webkit-user-drag: none;
  }

  .pdf-overlay-svg {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
    user-select: none;
    -webkit-user-select: none;
    z-index: 1;
  }

  .score-annotations {
    position: absolute;
    inset: 0;
    z-index: 2;
    pointer-events: none;
  }

  .measure-number-label,
  .score-badge {
    position: absolute;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }

  .measure-number-label {
    transform: translate(2px, calc(-100% - 3px));
    padding: 1px 4px;
    border-radius: 3px;
    background: rgba(18, 19, 23, 0.84);
    color: var(--ink);
    font-size: clamp(8px, 0.72vw, 11px);
    line-height: 1.35;
  }

  .measure-number-label.is-selected {
    background: var(--brass-strong);
    color: var(--stage);
    font-weight: 800;
  }

  .needs-info {
    /* Deliberately a glyph, not a fill: coverage already owns colour here, and
       a third shade would read as a third degree of "rehearsed" rather than as
       a different question. "?" needs a decision, "~" is approximate. */
    display: inline-block;
    margin-left: 0.25em;
    padding: 0 0.28em;
    border-radius: 3px;
    font-weight: 700;
    font-style: normal;
    line-height: 1.2;
    cursor: help;
  }

  .needs-error {
    background: var(--ember, #d1483a);
    color: #fff;
  }

  .needs-warning {
    background: var(--brass-strong, #c9a227);
    color: #1b1a17;
  }

  .timing-review-mark {
    display: inline-grid;
    place-items: center;
    min-width: 1.15em;
    height: 1.15em;
    margin-left: 0.25em;
    border-radius: 999px;
    font-size: 0.86em;
    font-weight: 900;
    font-style: normal;
  }

  .timing-needs-listening {
    background: var(--brass-strong, #c9a227);
    color: var(--stage, #121317);
  }

  .timing-spot-check {
    border: 1px solid var(--ink-muted, #aca49b);
    color: var(--ink, #f4efe6);
  }

  .timing-reviewed {
    background: var(--ready, #7fae67);
    color: var(--ready-ink, #10240a);
  }

  .measure-pass-count {
    margin-left: 4px;
    padding-left: 4px;
    border-left: 1px solid currentColor;
    font-size: 0.85em;
    font-weight: 700;
    opacity: 0.85;
  }

  .score-badge {
    padding: 2px 5px;
    border-radius: 3px;
    font-size: clamp(8px, 0.68vw, 10px);
    font-weight: 800;
    line-height: 1.3;
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }

  .target-badge {
    transform: translate(4px, 5px);
    background: var(--brass-strong);
    color: var(--stage);
    box-shadow: 0 1px 5px rgba(18, 19, 23, 0.5);
  }

  .newest-badge {
    transform: translate(4px, calc(-100% - 4px));
    border: 1px dashed var(--brass-strong);
    background: rgba(18, 19, 23, 0.9);
    color: var(--brass-strong);
  }

  .now-badge {
    transform: translate(-50%, 5px);
    background: var(--ember);
    color: white;
    box-shadow: 0 1px 5px rgba(18, 19, 23, 0.55);
  }

  .measure-hit-target {
    cursor: pointer;
    pointer-events: auto;
  }

  .measure-hit-target.mix-measure-target {
    cursor: crosshair;
    touch-action: none;
  }

  .mix-automation-bracket {
    fill: none;
    stroke: #147d88;
    stroke-width: 2.25;
    stroke-linecap: round;
    stroke-linejoin: round;
    vector-effect: non-scaling-stroke;
    pointer-events: auto;
    cursor: context-menu;
    filter: drop-shadow(0 1px 1px rgba(18, 19, 23, 0.7));
  }

  .mix-automation-bracket.mix-performance-cue {
    stroke: rgba(24, 93, 101, 0.82);
    stroke-width: 1.6;
    pointer-events: none;
  }

  .mix-automation-bracket.mix-cue-active {
    stroke: var(--brass-strong);
    stroke-width: 2.5;
  }

  .mix-automation-bracket.mix-region-disabled {
    opacity: 0.45;
    stroke-dasharray: 4 3;
  }

  .mix-control-point {
    fill: rgba(248, 246, 239, 0.96);
    stroke: #147d88;
    stroke-width: 2;
    vector-effect: non-scaling-stroke;
    pointer-events: auto;
    cursor: context-menu;
  }

  .mix-control-point-end {
    fill: #147d88;
  }

  .mix-control-point.mix-performance-cue {
    stroke: rgba(24, 93, 101, 0.86);
    pointer-events: none;
  }

  .mix-draft-point {
    fill: var(--brass-strong);
    stroke: #6d4c13;
    pointer-events: none;
  }

  .mix-cue-icon {
    position: absolute;
    transform: translate(-50%, calc(-100% - 4px));
    display: none;
    place-items: center;
    min-width: 1.35rem;
    height: 1.2rem;
    padding: 0 0.2rem;
    border: 1px solid #147d88;
    border-radius: 999px;
    background: rgba(18, 19, 23, 0.92);
    color: #126c75;
    font: 800 0.72rem/1 system-ui, sans-serif;
    pointer-events: none;
    box-shadow: 0 1px 4px rgba(18, 19, 23, 0.45);
  }

  .mix-cue-icon.mix-performance-cue {
    display: grid;
    min-width: 1rem;
    height: 1rem;
    border-color: rgba(41, 106, 112, 0.58);
    background: rgba(248, 246, 239, 0.88);
    color: #225d62;
    font-size: 0.62rem;
    box-shadow: none;
  }

  .mix-cue-icon.mix-cue-active {
    border-color: var(--brass-strong);
    background: var(--brass-strong);
    color: var(--stage);
  }

  .mix-selection-band {
    fill: transparent;
    stroke: var(--brass-strong);
    stroke-width: 2;
    stroke-dasharray: 5 3;
    vector-effect: non-scaling-stroke;
    pointer-events: none;
  }

  .measure-hit-target.measure-selected {
    stroke: var(--brass-strong);
    stroke-width: 4;
    filter: drop-shadow(0 0 3px rgba(216, 169, 76, 0.8));
  }

  .strip-selected {
    outline: 2px solid var(--brass-strong);
    outline-offset: 2px;
  }

  .latest-take-box {
    fill: transparent;
    stroke: var(--brass-strong);
    stroke-width: 2.5;
    stroke-dasharray: 7 4;
    pointer-events: none;
  }

  .position-cursor-line {
    stroke: var(--ember);
    stroke-width: 5;
    filter: drop-shadow(0 0 3px rgba(18, 19, 23, 0.95));
    pointer-events: none;
  }

  .anchor-marker {
    --anchor-blue: #45b8d8;
    position: absolute;
    width: 26px;
    height: 24px;
    padding: 0;
    border: 0;
    background: transparent;
    pointer-events: auto;
    cursor: ew-resize;
    /* Dedicated locator rail above measure labels; never cover the notes or
       the "Selected passage" badge inside the system. */
    transform: translate(-50%, -100%);
    filter: drop-shadow(0 2px 3px rgba(18, 19, 23, 0.8));
    touch-action: none;
  }

  .anchor-marker::before {
    content: '';
    position: absolute;
    inset: 4px 5px 2px;
    background: var(--anchor-blue);
    clip-path: polygon(8% 12%, 92% 12%, 50% 96%);
  }

  .anchor-marker:hover::before,
  .anchor-marker.is-dragging::before {
    background: #72d5ec;
  }

  .anchor-marker:focus-visible {
    outline: 2px solid white;
    outline-offset: -3px;
    border-radius: 8px;
  }

  .anchor-help {
    display: inline-flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.35rem 0.55rem;
    margin: 0.35rem 0 0.55rem;
    color: var(--ink-dim);
    font-size: 0.76rem;
    line-height: 1.35;
  }

  .anchor-help span {
    color: #72d5ec;
    font-weight: 750;
  }


  .pdf-page-controls {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 1rem;
  }

  .page-indicator {
    font-variant-numeric: tabular-nums;
    color: var(--ink-muted);
  }
</style>
