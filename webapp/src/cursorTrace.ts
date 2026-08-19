/**
 * Record what the cursor actually renders, frame by frame.
 *
 * The engine trace showed a stable position -- 10 backward steps in 978, none
 * larger than 0.29 beats -- while the performer watched the cursor "bounce
 * around, never finding where I am". Both can be true: three sources feed one
 * cursor (live, preview, hardware) and the UI picks by precedence, so the
 * displayed value can jump without any underlying position moving.
 *
 * There was no record of that. The engine logs what it believes; nothing logged
 * what was drawn. This closes the gap so the next report is diagnosable from
 * the run instead of from memory.
 */

// A page load stamps one epoch id. performance.now() is relative to load, so
// without this a reload mid-run interleaves two time origins in one file and a
// naive diff invents huge backward jumps that never happened on screen.
const EPOCH = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;

export interface CursorSample {
  /** Milliseconds since THIS page load; compare only within one epoch. */
  t: number;
  /** Per-page-load id; samples with different epochs are not time-comparable. */
  epoch: string;
  /** Wall clock (ms since 1970), monotone across loads for ordering. */
  wall: number;
  beat: number | null;
  measure: number | null;
  /** Which of the three sources won the precedence chain for this frame. */
  source: 'live' | 'preview' | 'hardware' | 'none';
  /** True when the position is dead-reckoned rather than read from playing. */
  projected: boolean;
  page: number | null;
}

const MAX_SAMPLES = 20000; // ~30 min at 10 Hz; a ring so a long run cannot grow without bound
const FLUSH_EVERY_MS = 5000;

let samples: CursorSample[] = [];
let pending: CursorSample[] = [];
let last: CursorSample | null = null;
let runId: string | null = null;
let timer: ReturnType<typeof setInterval> | null = null;

function changed(a: CursorSample | null, b: CursorSample): boolean {
  if (a === null) return true;
  // Record only real changes. A cursor that sits still is not what we are
  // hunting, and logging every frame would bury the jumps in noise.
  return (
    a.beat !== b.beat ||
    a.measure !== b.measure ||
    a.source !== b.source ||
    a.projected !== b.projected ||
    a.page !== b.page
  );
}

export function startCursorTrace(id: string): void {
  runId = id;
  samples = [];
  pending = [];
  last = null;
  if (timer === null) timer = setInterval(() => void flushCursorTrace(), FLUSH_EVERY_MS);
}

export function stopCursorTrace(): void {
  if (timer !== null) {
    clearInterval(timer);
    timer = null;
  }
  void flushCursorTrace();
  runId = null;
}

export function recordCursor(sample: Omit<CursorSample, 't' | 'epoch' | 'wall'>): void {
  if (runId === null) return;
  const full: CursorSample = {
    ...sample,
    t: Math.round(performance.now()),
    epoch: EPOCH,
    wall: Date.now(),
  };
  if (!changed(last, full)) return;
  last = full;
  samples.push(full);
  pending.push(full);
  if (samples.length > MAX_SAMPLES) samples = samples.slice(-MAX_SAMPLES);
}

export async function flushCursorTrace(): Promise<void> {
  if (runId === null || pending.length === 0) return;
  const batch = pending;
  pending = [];
  try {
    await fetch(`/api/ui/cursor-trace/${encodeURIComponent(runId)}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ samples: batch }),
    });
  } catch (error) {
    // Losing trace data must never disturb a performance. Put the batch back so
    // the next flush retries, but drop it rather than grow without bound.
    if (pending.length < 5000) pending = batch.concat(pending);
    console.warn('cursor trace flush failed', error);
  }
}

/** Everything recorded this session, for eyeballing without the server. */
export function cursorSamples(): readonly CursorSample[] {
  return samples;
}

/** Jumps larger than `threshold` beats, which is what a bouncing cursor is. */
export function cursorJumps(threshold = 0.5): { from: CursorSample; to: CursorSample }[] {
  const out: { from: CursorSample; to: CursorSample }[] = [];
  for (let i = 1; i < samples.length; i++) {
    const a = samples[i - 1];
    const b = samples[i];
    if (a.beat === null || b.beat === null) continue;
    if (Math.abs(b.beat - a.beat) >= threshold) out.push({ from: a, to: b });
  }
  return out;
}

// Exposed for inspection during a run: `__rubatoCursor.jumps()` lists every
// place the drawn cursor moved by half a beat or more, with the source that
// won each frame. Cheaper than asking anyone to remember what they saw.
declare global {
  interface Window {
    __rubatoCursor?: {
      record: typeof recordCursor;
      samples: typeof cursorSamples;
      jumps: typeof cursorJumps;
      start: typeof startCursorTrace;
      stop: typeof stopCursorTrace;
      flush: typeof flushCursorTrace;
    };
  }
}

if (typeof window !== 'undefined') {
  window.__rubatoCursor = {
    record: recordCursor,
    samples: cursorSamples,
    jumps: cursorJumps,
    start: startCursorTrace,
    stop: stopCursorTrace,
    flush: flushCursorTrace,
  };
}
