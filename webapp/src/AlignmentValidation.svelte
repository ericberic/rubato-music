<script lang="ts">
  // Alignment validation: audition the Oguri->measure/beat MIDI timing map and
  // correct it. This surface is deliberately about MIDI *timing* (when each beat
  // sounds), NOT PDF geometry (where a beat is drawn). The human supplies
  // identity by ear against the Joseffy score; the exact onset comes from the
  // reference MIDI, never a hand-measured time. See the score-localization
  // skill, "Validate & Correct an Existing Beat Map".
  import { onDestroy, onMount } from 'svelte';

  const PIECE = 'chopin_op11';
  const MOVEMENT = 2;
  const API = `/api/scores/${MOVEMENT}`;
  const PIECE_Q = `piece_id=${PIECE}`;

  // When embedded in the Data workspace, drop the standalone chrome (own header
  // and PDF) and instead drive the shared score above via onSelectMeasure, so
  // there is one score, not two.
  export let embedded = false;
  export let onSelectMeasure: ((measureLabel: string) => void) | null = null;
  export let requestedMeasure: string | null = null;
  type TimingReviewState = 'needs-listening' | 'spot-check' | 'reviewed';
  export let onReviewStateChange: ((state: Record<number, TimingReviewState>) => void) | null = null;
  export let outputName: string | null = null;
  export let volume = 0.75;
  export let metronomeVolume = 0.3;
  // False when the Timing facet is hidden (another Data tab is showing) so we
  // can halt playback instead of looping audio behind a hidden panel.
  export let active = true;

  type WorklistMeasure = {
    measure_label: string;
    reason: 'both_disagree' | 'spine';
    hypothesis_downbeat_seconds: number | null;
    disagreement_seconds: number | null;
  };
  type Candidate = { source_midi_tick: number; source_seconds: number; pitch: number; delta_seconds: number };
  type Beat = {
    beat_in_measure: number;
    source_seconds: number;
    source_midi_tick: number;
    anchored: boolean;
    confidence: number;
    pdf_x: number | null;
    candidates: Candidate[];
  };
  type Audition = {
    measure_label: string;
    measure_start_seconds: number;
    measure_end_seconds: number;
    beat_period_seconds: number;
    pdf_page: number | null;
    review_state: string;
    beats: Beat[];
    solo_notes: { pitch: number; source_seconds: number }[];
    orchestra_groups: {
      source_midi_tick: number;
      source_seconds: number;
      pitches: number[];
      instruments: string[];
      delta_seconds: number;
    }[];
  };

  let worklist: WorklistMeasure[] = [];
  let worklistDetail: string | null = null;
  let selected: string | null = null;
  let audition: Audition | null = null;
  let loading = false;
  let message = '';
  let looping = false;
  let playing = false;
  let boundaryOpen = false;
  let pulseOpen = false;
  let selectedBeat: number | null = null;
  let measureAttackGroups: Audition['orchestra_groups'] = [];
  const done = new Set<string>();

  const NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
  const noteName = (p: number) => `${NOTE_NAMES[p % 12]}${Math.floor(p / 12) - 1}`;

  let ctx: AudioContext | null = null;
  let loopTimer: ReturnType<typeof setTimeout> | null = null;

  function audioContext(): AudioContext {
    if (!ctx) ctx = new (window.AudioContext || (window as any).webkitAudioContext)();
    return ctx;
  }

  async function api<T>(url: string): Promise<T> {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.json();
  }

  async function loadWorklist() {
    loading = true;
    message = '';
    try {
      const body = await api<{ available: boolean; detail: string | null; measures: WorklistMeasure[] }>(
        `${API}/alignment-worklist?${PIECE_Q}`,
      );
      worklist = body.measures;
      worklistDetail = body.available ? null : body.detail;
      emitReviewState();
    } catch (err) {
      message = `Could not load worklist: ${err}`;
    } finally {
      loading = false;
    }
  }

  function emitReviewState() {
    const state: Record<number, TimingReviewState> = {};
    for (const item of worklist) {
      const number = parseInt(item.measure_label, 10);
      if (!Number.isFinite(number)) continue;
      state[number] = item.reason === 'both_disagree' ? 'needs-listening' : 'spot-check';
    }
    for (const label of done) {
      const number = parseInt(label, 10);
      if (Number.isFinite(number)) state[number] = 'reviewed';
    }
    onReviewStateChange?.(state);
  }

  async function select(measure: string, preserveCorrectionTool = false) {
    await stopAudition();
    selected = measure;
    audition = null;
    if (!preserveCorrectionTool) {
      boundaryOpen = false;
      pulseOpen = false;
      selectedBeat = null;
    }
    message = '';
    onSelectMeasure?.(measure); // drive the shared score when embedded
    try {
      audition = await api<Audition>(`${API}/beat-audition/${measure}?${PIECE_Q}`);
    } catch (err) {
      message = `Could not load measure ${measure}: ${err}`;
    }
  }

  // Candidate-note previews stay in the browser. The actual count-in,
  // orchestra, and beat clicks are sent to one backend MIDI clock so the
  // validation surface cannot introduce browser-vs-hardware timing drift.

  const freq = (midi: number) => 440 * Math.pow(2, (midi - 69) / 12);

  function tone(at: number, midi: number, dur = 0.22) {
    const c = audioContext();
    const osc = c.createOscillator();
    const gain = c.createGain();
    osc.type = 'triangle';
    osc.frequency.value = freq(midi);
    gain.gain.setValueAtTime(0.0001, at);
    gain.gain.exponentialRampToValueAtTime(0.25, at + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001, at + dur);
    osc.connect(gain).connect(c.destination);
    osc.start(at);
    osc.stop(at + dur + 0.02);
  }

  function totalDuration(a: Audition): number {
    const countIn = 4 * a.beat_period_seconds;
    return countIn + (a.measure_end_seconds - a.measure_start_seconds);
  }

  async function playAudition() {
    if (!audition || !outputName) {
      message = 'Connect and select a MIDI output before auditioning the orchestra.';
      return;
    }
    clearLoopTimer();
    playing = true;
    message = '';
    const run = async () => {
      if (!playing || !audition) return;
      const current = audition;
      const res = await fetch(
        `${API}/beat-audition/${current.measure_label}/play?${PIECE_Q}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            output_name: outputName,
            volume,
            metronome_volume: metronomeVolume,
          }),
        },
      );
      if (!res.ok) {
        const detail = await responseDetail(res);
        if (res.status === 409 && detail.includes('alignment_audition')) {
          // The audible pass can be complete while CoreMIDI/VST teardown still
          // owns the managed-job slot for a fraction of a second. This is not a
          // failed audition: wait for the actual hardware state before retrying.
          loopTimer = setTimeout(() => void retryWhenIdle(run), 150);
          return;
        }
        playing = false;
        message = `Could not play m.${current.measure_label}. ${detail}`;
        return;
      }
      const ms = (totalDuration(current) + 0.6) * 1000;
      loopTimer = setTimeout(() => void finishOrLoop(run), ms);
    };
    await run();
  }

  async function responseDetail(res: Response): Promise<string> {
    const raw = await res.text();
    try {
      const parsed = JSON.parse(raw);
      return typeof parsed?.detail === 'string' ? parsed.detail : 'Please try again.';
    } catch {
      return raw || 'Please try again.';
    }
  }

  async function hardwareIsIdle(): Promise<boolean> {
    try {
      const status = await api<{ running: boolean }>('/api/hardware/status');
      return !status.running;
    } catch {
      // A transient status-read failure should not manufacture another play
      // request. The next poll can recover without exposing transport internals.
      return false;
    }
  }

  async function retryWhenIdle(run: () => Promise<void>) {
    if (!playing) return;
    if (await hardwareIsIdle()) {
      await run();
      return;
    }
    loopTimer = setTimeout(() => void retryWhenIdle(run), 150);
  }

  async function finishOrLoop(run: () => Promise<void>) {
    if (!playing) return;
    if (!looping) {
      playing = false;
      return;
    }
    await retryWhenIdle(run);
  }

  export async function playMeasure(measureLabel: string) {
    if (selected !== measureLabel || !audition) await select(measureLabel);
    await playAudition();
  }

  function clearLoopTimer() {
    if (loopTimer) clearTimeout(loopTimer);
    loopTimer = null;
  }

  async function stopAudition() {
    const wasPlaying = playing;
    playing = false;
    clearLoopTimer();
    if (!wasPlaying) return;
    try {
      await fetch('/api/hardware/stop', { method: 'POST' });
    } catch {
      // The server may already have completed the short measure; local state
      // is still stopped and the global Silence control remains available.
    }
  }

  // Halt playback when this facet is hidden behind another Data tab.
  $: if (!active && playing) void stopAudition();

  // Leaving Data mode unmounts this: stop the loop and release the AudioContext
  // so nothing keeps scheduling audio in the background.
  onDestroy(() => {
    void stopAudition();
    if (ctx) {
      void ctx.close();
      ctx = null;
    }
  });

  async function playPitch(midi: number) {
    const c = audioContext();
    await c.resume();
    tone(c.currentTime + 0.05, midi, 0.4);
  }

  async function playChord(pitches: number[]) {
    const c = audioContext();
    await c.resume();
    for (const pitch of pitches) tone(c.currentTime + 0.05, pitch, 0.5);
  }

  const chordName = (pitches: number[]) => pitches.map(noteName).join(' · ');

  function boundaryPosition(delta: number): string {
    if (delta < -0.25) return 'before the current measure-start click';
    if (delta > 0.25) return 'after the current measure-start click';
    return 'at the current measure-start click';
  }

  $: if (active && requestedMeasure && requestedMeasure !== selected) {
    void select(requestedMeasure);
  }

  $: measureAttackGroups = audition
    ? audition.orchestra_groups.filter(
        (group) =>
          group.source_seconds >= audition!.measure_start_seconds - 0.35 &&
          group.source_seconds < audition!.measure_end_seconds - 0.2,
      )
    : [];

  function timelinePercent(sourceSeconds: number): number {
    if (!audition) return 0;
    const duration = Math.max(0.001, audition.measure_end_seconds - audition.measure_start_seconds);
    return Math.max(0, Math.min(100, ((sourceSeconds - audition.measure_start_seconds) / duration) * 100));
  }

  function attackLetter(index: number): string {
    return String.fromCharCode(65 + index);
  }

  function selectedReviewLabel(): string {
    const item = worklist.find((candidate) => candidate.measure_label === selected);
    if (selected && done.has(selected)) return 'Heard and confirmed';
    if (item?.reason === 'both_disagree') return 'Needs listening';
    if (item?.reason === 'spine') return 'Spot check';
    return 'Opened from score';
  }

  // ------------------------------- corrections (both write through one endpoint) --

  async function postCorrection(
    measure: number,
    beat: number,
    sourceSeconds: number,
    sourceMidiTick: number,
    note: string,
  ) {
    const res = await fetch(`${API}/alignment-corrections?${PIECE_Q}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_seconds: sourceSeconds,
        source_midi_tick: sourceMidiTick, // exact onset the performer picked
        measure,
        beat_in_measure: beat,
        note,
      }),
    });
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.json();
  }

  function confirmCorrect() {
    if (!selected) return;
    done.add(selected);
    emitReviewState();
    message = `m.${selected} confirmed as heard.`;
  }

  async function pickBeatAttack(
    beatInMeasure: number,
    group: Audition['orchestra_groups'][number],
  ) {
    if (!audition) return;
    const measureLabel = audition.measure_label;
    try {
      await postCorrection(
        parseInt(measureLabel, 10),
        beatInMeasure,
        group.source_seconds,
        group.source_midi_tick,
        `printed beat ${beatInMeasure + 1} starts on orchestral attack ${chordName(group.pitches)}`,
      );
      selectedBeat = null;
      await loadWorklist();
      await select(measureLabel, true);
      pulseOpen = true;
      message = `Beat ${beatInMeasure + 1} now lands on attack ${chordName(group.pitches)}. The exact MIDI onset was saved automatically.`;
    } catch (err) {
      message = `Beat correction failed: ${err}`;
    }
  }

  async function pickMeasureStart(group: Audition['orchestra_groups'][number]) {
    if (!audition) return;
    const measureLabel = audition.measure_label;
    try {
      // The performer identifies the musical boundary; the earliest note-on in
      // the grouped/rolled attack contributes the exact native MIDI tick.
      await postCorrection(
        parseInt(measureLabel, 10),
        0,
        group.source_seconds,
        group.source_midi_tick,
        `m.${measureLabel} starts on orchestral attack ${chordName(group.pitches)}`,
      );
      await select(measureLabel);
      message = `m.${measureLabel} now starts on ${chordName(group.pitches)}. Everything before that attack remains in the previous measure.`;
    } catch (err) {
      message = `Boundary correction failed: ${err}`;
    }
  }

  onMount(loadWorklist);

  $: needListening = worklist.filter((item) => item.reason === 'both_disagree');
  $: spotChecks = worklist.filter((item) => item.reason === 'spine');
</script>

<div class="wrap" class:embedded>
  {#if !embedded}
    <header>
      <h1>Alignment validation</h1>
      <p class="sub">Choose a measure, listen, and compare it with the score.</p>
    </header>
  {/if}

  <div class="cols" class:embedded>
    <nav class="review-targets" aria-label="Timing review targets">
      {#if worklistDetail}
        <p class="unavailable">{worklistDetail}</p>
      {:else if loading}
        <span>Loading…</span>
      {:else}
        <span class="review-label">Review</span>
        {#each needListening as item (item.measure_label)}
          <button class="review-target needs-listening" on:click={() => select(item.measure_label)}>
            ! m.{item.measure_label}
          </button>
        {/each}
        {#each spotChecks as item (item.measure_label)}
          <button class="review-target spot-check" on:click={() => select(item.measure_label)}>
            m.{item.measure_label}
          </button>
        {/each}
      {/if}
    </nav>

    <main>
      {#if message}<div class="message">{message}</div>{/if}

      {#if !selected}
        <div class="placeholder">Select a measure.</div>
      {:else if !audition}
        <div class="placeholder">Loading m.{selected}…</div>
      {:else}
        <div class="measure-head">
          <h2>m.{audition.measure_label}</h2>
          {#if selectedReviewLabel() !== 'Opened from score'}
            <span class="badge">{selectedReviewLabel()}</span>
          {/if}
          <span class="tempo">♩ ≈ {(60 / audition.beat_period_seconds).toFixed(0)} bpm</span>
        </div>

        {#if audition.pdf_page && !embedded}
          <iframe
            class="score"
            title="Joseffy m.{audition.measure_label}"
            src={`/api/scores/${MOVEMENT}/pdf?${PIECE_Q}#page=${audition.pdf_page}&view=FitH`}
          ></iframe>
          <p class="score-hint">Joseffy page {audition.pdf_page}. Find m.{audition.measure_label} and read its notes.</p>
        {/if}

        <div class="transport">
          {#if playing}
            <button class="primary" on:click={stopAudition}>■ Stop</button>
          {:else}
            <button
              class="primary"
              aria-label="Play m.{audition.measure_label} with count-in"
              title="Four-click lead-in"
              on:click={playAudition}
              disabled={!outputName}
            >▶ Play</button>
          {/if}
          <label class="loop"><input type="checkbox" bind:checked={looping} /> loop</label>
        </div>

        {#if audition.orchestra_groups.length}
          <section class="boundary-tool">
            <button class="boundary-toggle" on:click={() => (boundaryOpen = !boundaryOpen)}>
              {boundaryOpen ? 'Close measure-start correction' : 'Measure starts on the wrong sound?'}
            </button>
            {#if boundaryOpen}
              <div class="boundary-panel">
                <h3>Which orchestral attack begins printed m.{audition.measure_label}?</h3>
                <p>
                  Match the score to a sound—not “early” or “late.” Everything before your choice
                  belongs to the previous measure; Rubato records the attack's exact MIDI tick.
                </p>
                <div class="boundary-groups">
                  {#each audition.orchestra_groups as group (group.source_midi_tick)}
                    <div class="boundary-group">
                      <button class="mini" on:click={() => playChord(group.pitches)}>▶</button>
                      <div class="boundary-sound">
                        <strong>{chordName(group.pitches)}</strong>
                        <span>{group.instruments.slice(0, 3).join(' · ') || 'Orchestra'}</span>
                        <small>{boundaryPosition(group.delta_seconds)}</small>
                      </div>
                      <button class="pick" on:click={() => pickMeasureStart(group)}>
                        This starts m.{audition.measure_label}
                      </button>
                    </div>
                  {/each}
                </div>
              </div>
            {/if}
          </section>
        {/if}

        <section class="pulse-tool">
          <button class="boundary-toggle" on:click={() => (pulseOpen = !pulseOpen)}>
            {pulseOpen ? 'Close beat correction' : 'Clicks don’t line up with the orchestra?'}
          </button>
          {#if pulseOpen}
            <div class="pulse-panel">
              <h3>Put the four numbered clicks on the four printed beats</h3>
              <p>
                The upper row is the current metronome. The lettered marks are orchestral attacks,
                in the same left-to-right order you hear them. Choose a beat, then choose the attack
                that lands on that beat. Rubato saves the exact MIDI onset—no timestamps or dragging required.
              </p>

              <div class="timing-ruler" aria-label="Current beat clicks and orchestral attacks across the measure">
                <div class="ruler-line"></div>
                {#each audition.beats as beat (beat.beat_in_measure)}
                  <button
                    class="beat-marker"
                    class:is-active={selectedBeat === beat.beat_in_measure}
                    style={`left: ${timelinePercent(beat.source_seconds)}%`}
                    aria-label={`Select printed beat ${beat.beat_in_measure + 1}`}
                    aria-pressed={selectedBeat === beat.beat_in_measure}
                    on:click={() => (selectedBeat = beat.beat_in_measure)}
                  >{beat.beat_in_measure + 1}</button>
                {/each}
                {#each measureAttackGroups as group, index (group.source_midi_tick)}
                  <button
                    class="attack-marker"
                    style={`left: ${timelinePercent(group.source_seconds)}%`}
                    aria-label={`Attack ${attackLetter(index)}: ${chordName(group.pitches)}`}
                    disabled={selectedBeat === null}
                    on:click={() => selectedBeat !== null && pickBeatAttack(selectedBeat, group)}
                  >{attackLetter(index)}</button>
                {/each}
              </div>
              <div class="ruler-key">
                <span><i class="beat-dot"></i> numbered metronome clicks</span>
                <span><i class="attack-dot"></i> orchestral attacks</span>
              </div>

              <div class="beat-picker" aria-label="Printed beat to correct">
                <span>Correct:</span>
                {#each audition.beats as beat (beat.beat_in_measure)}
                  <button
                    class:is-active={selectedBeat === beat.beat_in_measure}
                    aria-pressed={selectedBeat === beat.beat_in_measure}
                    on:click={() => (selectedBeat = beat.beat_in_measure)}
                  >Beat {beat.beat_in_measure + 1}</button>
                {/each}
              </div>

              {#if selectedBeat === null}
                <p class="pulse-prompt">First choose the printed beat whose click is wrong.</p>
              {:else}
                <p class="pulse-prompt">Now choose the lettered orchestral attack that belongs on beat {selectedBeat + 1}.</p>
              {/if}

              <div class="attack-list">
                {#each measureAttackGroups as group, index (group.source_midi_tick)}
                  <button
                    class="attack-choice"
                    disabled={selectedBeat === null}
                    on:click={() => selectedBeat !== null && pickBeatAttack(selectedBeat, group)}
                  >
                    <b>{attackLetter(index)}</b>
                    <span><strong>{chordName(group.pitches)}</strong><small>{group.instruments.slice(0, 4).join(' · ') || 'Orchestra'}</small></span>
                    <em>{selectedBeat === null ? 'choose a beat first' : `put beat ${selectedBeat + 1} here`}</em>
                  </button>
                {/each}
              </div>
            </div>
          {/if}
        </section>

        <div class="verdicts">
          <button class="ok" on:click={confirmCorrect}>Sounds right ✓</button>
        </div>
      {/if}
    </main>
  </div>
</div>

<style>
  /* Dark-first to match the app's fixed concert-hall theme (app.css sets a
     near-black stage and ivory ink regardless of OS scheme), reusing its
     palette variables so this surface belongs to the same room. */
  .wrap {
    max-width: 1100px;
    margin: 0 auto;
    padding: 1.25rem;
    color: var(--ink, #f4efe6);
    min-height: 100vh;
    box-sizing: border-box;
  }
  /* Embedded in the Data workspace panel: no page chrome, flows in the panel. */
  .wrap.embedded { max-width: none; margin: 0; padding: 0; min-height: 0; }
  header h1 { margin: 0 0 0.25rem; font-size: 1.4rem; color: var(--ink, #f4efe6); }
  .sub { margin: 0 0 1rem; color: var(--ink-muted, #aca49b); font-size: 0.9rem; line-height: 1.4; max-width: 70ch; }
  .cols { display: grid; grid-template-columns: 1fr; gap: 0.75rem; align-items: start; }
  .cols.embedded { grid-template-columns: 1fr; gap: 0.75rem; }
  .unavailable { font-size: 0.82rem; color: var(--ink-muted, #aca49b); }
  .placeholder { color: var(--ink-dim, #6f6a63); padding: 2rem 0; }
  .message { background: var(--stage-panel-raised, #23252f); border: 1px solid var(--brass, #d8a94c); color: var(--ink, #f4efe6); padding: 0.5rem 0.75rem; border-radius: 8px; margin-bottom: 0.75rem; font-size: 0.85rem; }
  .measure-head { display: flex; align-items: baseline; gap: 0.6rem; }
  .measure-head h2 { margin: 0; font-size: 1.3rem; color: var(--ink, #f4efe6); }
  .badge { font-size: 0.7rem; text-transform: uppercase; background: rgba(244,239,230,0.08); padding: 0.1rem 0.45rem; border-radius: 4px; color: var(--ink-muted, #aca49b); }
  .tempo { color: var(--ink-muted, #aca49b); font-size: 0.82rem; margin-left: auto; }
  .score { width: 100%; height: 420px; border: 1px solid var(--stage-hairline, rgba(244,239,230,0.12)); border-radius: 8px; margin: 0.6rem 0 0.25rem; background: #fff; }
  .score-hint { font-size: 0.78rem; color: var(--ink-muted, #aca49b); margin: 0 0 0.75rem; }
  .transport { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.75rem; }
  .primary { background: var(--brass, #d8a94c); color: var(--brass-ink, #241a06); border: none; padding: 0.5rem 0.9rem; border-radius: 8px; cursor: pointer; font-size: 0.9rem; font-weight: 600; }
  .loop { font-size: 0.82rem; color: var(--ink-muted, #aca49b); display: flex; align-items: center; gap: 0.25rem; }
  .boundary-tool { margin: 0 0 0.85rem; }
  .boundary-toggle { border: 0; padding: 0; background: none; color: var(--brass-strong, #e9bd63); cursor: pointer; font: inherit; font-size: 0.82rem; text-decoration: underline; }
  .boundary-panel { margin-top: 0.55rem; padding: 0.85rem; border: 1px solid rgba(216,169,76,0.4); border-radius: 10px; background: rgba(216,169,76,0.07); }
  .boundary-panel h3 { margin: 0; color: var(--ink, #f4efe6); font-size: 0.95rem; }
  .boundary-panel > p { margin: 0.3rem 0 0.7rem; color: var(--ink-muted, #aca49b); font-size: 0.8rem; line-height: 1.4; max-width: 68ch; }
  .boundary-groups { display: grid; gap: 0.4rem; max-height: 19rem; overflow-y: auto; }
  .boundary-group { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 0.65rem; padding: 0.5rem 0.55rem; border: 1px solid var(--stage-hairline, rgba(244,239,230,0.09)); border-radius: 8px; background: var(--stage-panel, #1c1e26); }
  .boundary-sound { display: grid; min-width: 0; gap: 0.08rem; }
  .boundary-sound strong { overflow: hidden; color: var(--ink, #f4efe6); font-size: 0.84rem; text-overflow: ellipsis; white-space: nowrap; }
  .boundary-sound span, .boundary-sound small { overflow: hidden; color: var(--ink-muted, #aca49b); font-size: 0.7rem; text-overflow: ellipsis; white-space: nowrap; }
  .boundary-sound small { color: var(--ink-dim, #6f6a63); }
  .mini { width: 1.7rem; border: 1px solid var(--stage-hairline-strong, rgba(244,239,230,0.16)); border-radius: 4px; background: var(--stage-panel-raised, #23252f); color: var(--ink, #f4efe6); }
  .pick { border: 1px solid var(--brass, #d8a94c); color: var(--brass-strong, #e9bd63); background: transparent; border-radius: 6px; padding: 0.2rem 0.55rem; font-size: 0.8rem; }
  .verdicts { display: flex; align-items: center; gap: 1.25rem; padding-top: 0.6rem; border-top: 1px solid var(--stage-hairline, rgba(244,239,230,0.09)); }
  .ok { background: var(--ready, #7fae67); color: var(--ready-ink, #10240a); border: none; padding: 0.5rem 0.9rem; border-radius: 8px; cursor: pointer; font-weight: 600; }
  /* The score strip is the overview; this is only its compact, explicit index. */
  .cols { grid-template-columns: 1fr; gap: 0.75rem; }
  .review-targets { display: flex; align-items: center; flex-wrap: wrap; gap: 0.35rem; color: var(--ink-muted, #aca49b); font-size: 0.72rem; }
  .review-label { margin-right: 0.15rem; color: var(--ink-dim, #6f6a63); text-transform: uppercase; letter-spacing: 0.08em; }
  .review-target { border-radius: 999px; padding: 0.2rem 0.45rem; background: transparent; cursor: pointer; font: inherit; font-variant-numeric: tabular-nums; }
  .review-target.needs-listening { border: 1px solid var(--brass-strong, #e9bd63); background: rgba(216,169,76,0.14); color: var(--brass-strong, #e9bd63); font-weight: 800; }
  .review-target.spot-check { border: 1px solid rgba(244,239,230,0.4); color: var(--ink-muted, #aca49b); }

  .pulse-tool { margin: 0 0 0.85rem; }
  .pulse-panel { margin-top: 0.55rem; padding: 0.9rem; border: 1px solid rgba(216,169,76,0.4); border-radius: 10px; background: rgba(216,169,76,0.07); }
  .pulse-panel h3 { margin: 0; color: var(--ink, #f4efe6); font-size: 0.95rem; }
  .pulse-panel > p { margin: 0.3rem 0 0.75rem; color: var(--ink-muted, #aca49b); font-size: 0.8rem; line-height: 1.45; max-width: 76ch; }
  .timing-ruler { position: relative; height: 5.2rem; margin: 0.5rem 0 0.2rem; border-inline: 1px solid var(--stage-hairline-strong, rgba(244,239,230,0.16)); }
  .ruler-line { position: absolute; top: 2.55rem; left: 0; right: 0; height: 1px; background: var(--stage-hairline-strong, rgba(244,239,230,0.16)); }
  .beat-marker, .attack-marker { position: absolute; transform: translateX(-50%); border-radius: 999px; cursor: pointer; font-weight: 800; }
  .beat-marker { top: 0.25rem; width: 2rem; height: 2rem; border: 2px solid var(--brass-strong, #e9bd63); background: var(--stage-panel, #1c1e26); color: var(--brass-strong, #e9bd63); }
  .beat-marker::after { content: ''; position: absolute; left: 50%; top: 100%; height: 0.45rem; border-left: 1px solid var(--brass-strong, #e9bd63); }
  .beat-marker.is-active { background: var(--brass-strong, #e9bd63); color: var(--brass-ink, #241a06); box-shadow: 0 0 0 3px rgba(216,169,76,0.2); }
  .attack-marker { bottom: 0.15rem; min-width: 1.75rem; height: 1.75rem; border: 1px solid var(--ready, #7fae67); background: var(--stage-panel-raised, #23252f); color: var(--ready, #7fae67); }
  .attack-marker::before { content: ''; position: absolute; left: 50%; bottom: 100%; height: 0.55rem; border-left: 1px solid var(--ready, #7fae67); }
  .attack-marker:disabled { cursor: default; opacity: 0.8; }
  .ruler-key { display: flex; flex-wrap: wrap; gap: 0.8rem; color: var(--ink-dim, #6f6a63); font-size: 0.7rem; }
  .ruler-key span { display: inline-flex; align-items: center; gap: 0.3rem; }
  .ruler-key i { width: 0.6rem; height: 0.6rem; border-radius: 999px; }
  .beat-dot { border: 2px solid var(--brass-strong, #e9bd63); }
  .attack-dot { border: 1px solid var(--ready, #7fae67); }
  .beat-picker { display: flex; flex-wrap: wrap; align-items: center; gap: 0.35rem; margin-top: 0.8rem; color: var(--ink-muted, #aca49b); font-size: 0.76rem; }
  .beat-picker button { border: 1px solid var(--stage-hairline-strong, rgba(244,239,230,0.16)); border-radius: 999px; padding: 0.3rem 0.55rem; background: var(--stage-panel, #1c1e26); color: var(--ink, #f4efe6); cursor: pointer; }
  .beat-picker button.is-active { border-color: var(--brass-strong, #e9bd63); background: var(--brass-strong, #e9bd63); color: var(--brass-ink, #241a06); }
  .pulse-prompt { color: var(--brass-strong, #e9bd63) !important; font-weight: 600; }
  .attack-list { display: grid; gap: 0.35rem; }
  .attack-choice { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 0.65rem; width: 100%; padding: 0.45rem 0.55rem; border: 1px solid var(--stage-hairline, rgba(244,239,230,0.09)); border-radius: 8px; background: var(--stage-panel, #1c1e26); color: var(--ink, #f4efe6); text-align: left; cursor: pointer; }
  .attack-choice:disabled { cursor: default; opacity: 0.62; }
  .attack-choice > b { display: grid; place-items: center; width: 1.7rem; height: 1.7rem; border: 1px solid var(--ready, #7fae67); border-radius: 999px; color: var(--ready, #7fae67); }
  .attack-choice > span { display: grid; min-width: 0; gap: 0.08rem; }
  .attack-choice strong, .attack-choice small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .attack-choice small { color: var(--ink-muted, #aca49b); font-size: 0.68rem; }
  .attack-choice em { color: var(--brass-strong, #e9bd63); font-size: 0.72rem; font-style: normal; }
</style>
