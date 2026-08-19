<script lang="ts">
  /**
   * Score integrity: bundling, cleaning, root-cause analysis, label checking.
   *
   * Its own view because it is a different job from rehearsing. There is no
   * piano in the loop, the work is offline and iterative, and its failures are
   * about provenance rather than timing -- a measure number that means two
   * different bars, an OMR pass that disagrees with the timeline. None of that
   * belongs beside a live cursor.
   *
   * The whole subject is DISAGREEMENT BETWEEN SOURCES, so the view leads with
   * where the sources contradict each other and anchors every claim to the
   * measures it concerns. Findings that name measures paint them on the strip,
   * because "measures 113-126 are unmapped" is a shape, not a sentence, and the
   * previous failure here was handing over prose that could not be acted on.
   */
  import { onMount } from 'svelte';
  import { getScoreBundleHealth } from './generated';

  export let pieceId = 'chopin_op11';
  export let movement = 2;
  /** Highest measure NUMBER, so the strip spans the piece even with zero takes.
      Not a count of rows: coverage lists only solo measures (96 here) while
      numbers run to 126, and sizing by the count silently dropped every
      finding past m.96 off the end of the strip. */
  export let measureCount = 126;

  type Finding = { code: string; severity: string; message: string; measures: string[] };

  let findings: Finding[] = [];
  let missingArtifacts: string[] = [];
  let loading = true;
  let error = '';
  let selected: Finding | null = null;

  $: errors = findings.filter((f) => f.severity === 'error');
  $: warnings = findings.filter((f) => f.severity === 'warning');
  // Measures implicated by the selected finding, or by every finding when none
  // is selected -- so opening the view already shows where the trouble is.
  $: highlighted = new Set(
    (selected ? [selected] : findings).flatMap((f) => f.measures.map((m) => Number(m))),
  );
  $: highlightedSeverity = new Map(
    (selected ? [selected] : findings).flatMap((f) =>
      f.measures.map((m) => [Number(m), f.severity] as const),
    ),
  );

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      const response = await getScoreBundleHealth({
        path: { movement },
        query: { piece_id: pieceId },
      });
      findings = (response.findings ?? []) as Finding[];
      missingArtifacts = response.missing_artifacts ?? [];
    } catch (e) {
      error = `Could not read bundle health: ${e}`;
    } finally {
      loading = false;
    }
  }

  function toggle(finding: Finding) {
    selected = selected === finding ? null : finding;
  }

  function measureRanges(measures: string[]): string {
    const numbers = measures.map(Number).filter(Number.isFinite).sort((a, b) => a - b);
    if (!numbers.length) return 'whole bundle';
    // Runs read as ranges: "113-126" is one fact, thirteen numbers are not.
    const runs: string[] = [];
    let start = numbers[0];
    let previous = numbers[0];
    for (const n of numbers.slice(1)) {
      if (n === previous + 1) {
        previous = n;
        continue;
      }
      runs.push(start === previous ? `${start}` : `${start}–${previous}`);
      start = previous = n;
    }
    runs.push(start === previous ? `${start}` : `${start}–${previous}`);
    return `m. ${runs.join(', ')}`;
  }
</script>

<section class="integrity">
  <header class="integrity-head">
    <div>
      <h2>Score integrity</h2>
      <p class="sub">Where this bundle's sources disagree with each other.</p>
    </div>
    <div class="tally">
      <span class="count count-error" class:muted={!errors.length}>{errors.length}</span>
      <small>must decide</small>
      <span class="count count-warn" class:muted={!warnings.length}>{warnings.length}</span>
      <small>worth checking</small>
    </div>
  </header>

  {#if loading}
    <p class="state">Checking the bundle…</p>
  {:else if error}
    <p class="state state-error">{error}</p>
  {:else}
    <!-- The strip spans the piece regardless of takes: this view exists before
         any rehearsal has happened. -->
    <div class="strip" role="img" aria-label="Measures affected by findings">
      {#each Array(measureCount) as _, i}
        {@const measure = i + 1}
        <span
          class="cell"
          class:hit={highlighted.has(measure)}
          class:hit-error={highlightedSeverity.get(measure) === 'error'}
          title={highlighted.has(measure) ? `m. ${measure} — implicated` : `m. ${measure}`}
        ></span>
      {/each}
    </div>

    {#if missingArtifacts.length}
      <div class="finding finding-error">
        <strong>{missingArtifacts.length} artifacts have not been pulled</strong>
        <p>Nothing downstream can be trusted until these exist.</p>
        <code>{missingArtifacts.join(', ')}</code>
      </div>
    {/if}

    {#if !findings.length && !missingArtifacts.length}
      <p class="state state-ok">No source disagreements. Every derived view agrees.</p>
    {/if}

    {#each findings as finding (finding.code)}
      <button
        type="button"
        class="finding finding-{finding.severity}"
        class:is-selected={selected === finding}
        on:click={() => toggle(finding)}
      >
        <span class="finding-head">
          <strong>{measureRanges(finding.measures)}</strong>
          <span class="badge badge-{finding.severity}">
            {finding.severity === 'error' ? 'must decide' : 'worth checking'}
          </span>
        </span>
        <p>{finding.message}</p>
        <small class="code">{finding.code}</small>
      </button>
    {/each}
  {/if}
</section>

<style>
  .integrity {
    display: flex;
    flex-direction: column;
    gap: 0.85rem;
  }

  .integrity-head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
  }

  h2 {
    margin: 0;
    font-size: 1.1rem;
  }

  .sub {
    margin: 0.15rem 0 0;
    font-size: 0.82rem;
    opacity: 0.72;
  }

  .tally {
    display: flex;
    align-items: baseline;
    gap: 0.35rem;
    font-size: 0.72rem;
    opacity: 0.85;
  }

  .count {
    font-size: 1.3rem;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }

  .count-error {
    color: var(--ember, #d1483a);
  }

  .count-warn {
    color: var(--brass-strong, #c9a227);
  }

  .muted {
    opacity: 0.35;
  }

  .strip {
    display: flex;
    gap: 1px;
    height: 18px;
  }

  .cell {
    flex: 1 1 0;
    background: rgba(255, 255, 255, 0.09);
    border-radius: 1px;
  }

  .hit {
    background: var(--brass-strong, #c9a227);
  }

  .hit-error {
    background: var(--ember, #d1483a);
  }

  .finding {
    display: block;
    width: 100%;
    text-align: left;
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-left-width: 3px;
    border-radius: var(--radius-sm, 6px);
    background: rgba(255, 255, 255, 0.03);
    padding: 0.6rem 0.7rem;
    cursor: pointer;
    font: inherit;
    color: inherit;
  }

  .finding-error {
    border-left-color: var(--ember, #d1483a);
  }

  .finding-warning {
    border-left-color: var(--brass-strong, #c9a227);
  }

  .is-selected {
    background: rgba(255, 255, 255, 0.08);
  }

  .finding-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
  }

  .finding p {
    margin: 0.3rem 0 0.2rem;
    font-size: 0.84rem;
    line-height: 1.4;
    opacity: 0.88;
  }

  .badge {
    font-size: 0.64rem;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    padding: 0.1rem 0.4rem;
    border-radius: 999px;
    white-space: nowrap;
  }

  .badge-error {
    background: rgba(209, 72, 58, 0.2);
    color: var(--ember, #d1483a);
  }

  .badge-warning {
    background: rgba(201, 162, 39, 0.18);
    color: var(--brass-strong, #c9a227);
  }

  .code {
    font-family: ui-monospace, monospace;
    font-size: 0.66rem;
    opacity: 0.45;
  }

  .state {
    font-size: 0.86rem;
    opacity: 0.75;
  }

  .state-ok {
    color: var(--ready, #7fae67);
  }

  .state-error {
    color: var(--ember, #d1483a);
  }
</style>
