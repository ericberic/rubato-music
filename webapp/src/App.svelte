<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte';
  import { Midi } from '@tonejs/midi';
  import * as Tone from 'tone';
  import PdfCoverageOverlay from './PdfCoverageOverlay.svelte';
  import ScoreIntegrityView from './ScoreIntegrityView.svelte';
  import AlignmentValidation from './AlignmentValidation.svelte';
  import {
    interpolatePosition,
    transportElapsedAtScoreBeat,
    transportPositionAt,
    transportPositionAtElapsed,
  } from './scorePosition';
  import { isHardwarePianoPort, pickBestPort } from './midiPorts';
  import {
    recordCursor,
    startCursorTrace,
    stopCursorTrace,
  } from './cursorTrace';
  import {
    assertionsFor,
    describeSpan,
    type AssertionContext,
    type ScoreSpan,
  } from './scoreAssertions';
  import { connectTakeEvents, type TakeEvent } from './takeEvents';
  import {
    groupTakesByEntry,
    takeCountsByMeasure,
    takeEntryMeasure,
    type PassageTakeGroup,
  } from './passageGroups';
  import {
    analyzePassageTakes,
    createSession as createSessionRequest,
    createMixProgram,
    createMixRegion,
    createScoreAlignmentCorrection,
    deleteTake,
    deleteMixRegion,
    downloadMidi,
    downloadTakeReviewMidi,
    getCoverage,
    getLivePerformancePlan,
    getScoreBundleHealth,
    getSession,
    getTakeScoreTransport,
    hardwareStatus as fetchHardwareStatus,
    hardwarePanic,
    keepPerformanceRecording,
    liveRuntimeStatus,
    listSessions,
    listMixPrograms,
    listMixZones,
    listTakes,
    midiDevices,
    orchestraRendererStatus,
    playOguri,
    playSessionMidi,
    playTakeReview,
    preloadOrchestraRenderer,
    prepareTakeReview,
    rebindMixProgram,
    renderOfflineSession,
    retryTakeAlignment,
    restoreTake,
    resolveTake as resolveTakeRequest,
    shutdownServer,
    startHardwareRecord,
    startHardwareRecordWithCue,
    startLiveRuntimeFollow,
    startMixAudition,
    stopHardwareJob,
    stopLiveRuntime,
    stopMixAudition as stopMixAuditionRequest,
    uploadMidi,
    calibrateLiveRuntimeLatency,
    updateLiveRuntimeOutputAdvance,
    updateLiveRuntimeTempo,
    updateLiveRuntimeVolume,
    listFreeRegions,
    declareFreeRegion,
    clearFreeRegion,
    undoMixProgram,
    unloadOrchestraRenderer,
    updateMixRegion,
  } from './generated';
  import type {
    Anchor,
    CoverageDoc,
    FreeRegion,
    CoverageMeasure,
    LiveLatencyCalibrationResponse,
    LiveStatusResponse,
    LivePerformancePlanResponse,
    MixProgram,
    MixRegionInput,
    OrchestraRendererStatus,
    PassageAnalysisResponse,
    RuntimeStatus,
    ScorePositionResponse,
    SessionResponse,
    SessionStatusResponse,
    TakeResponse,
    ZoneConfig,
  } from './generated';

  const CANONICAL_PPQ = 960;

  let sessions: SessionResponse[] = [];
  let sessionId = '';
  let status: SessionStatusResponse | null = null;
  let newSessionInput = '';
  let message = '';
  let messageTimer: ReturnType<typeof setTimeout> | null = null;
  let playerMidi: Midi | null = null;
  let playerBuffer: ArrayBuffer | null = null;
  let playerLabel = 'No MIDI loaded';
  let playerDuration = 0;
  let playerPosition = 0;
  let playbackOffset = 0;
  let isPlaying = false;
  let loopEnabled = false;
  let playbackTimer: ReturnType<typeof setInterval> | null = null;
  let currentSynth: Tone.PolySynth | null = null;
  let orchestraGain: Tone.Gain | null = null;
  let orchestraVolume = 20;
  let confirmedOrchestraVolume = 75;
  let metronomeVolume = 30;
  let orchestraTempoBpm = 76;
  let confirmedOrchestraTempoBpm = 76;
  let orchestraTempoCustomized = false;
  let yamahaOutputAdvanceMs = 0;
  let confirmedOutputAdvanceMs = 0;
  let latencyCalibrating = false;
  let latencyCalibration: LiveLatencyCalibrationResponse | null = null;
  let liveTempoTimer: ReturnType<typeof setTimeout> | null = null;
  let liveTempoEditRevision = 0;
  let liveVolumeTimer: ReturnType<typeof setTimeout> | null = null;
  let liveVolumeEditRevision = 0;
  let liveOutputAdvanceTimer: ReturnType<typeof setTimeout> | null = null;
  let liveOutputAdvanceEditRevision = 0;
  let playbackLoading: string | null = null;
  let creatingSlot = false;
  let endingSession = false;
  let backendInputs: string[] = [];
  let backendOutputs: string[] = [];
  let runtimeStatus: RuntimeStatus | null = null;
  let rendererStatus: OrchestraRendererStatus = {
    state: 'not_loaded',
    updated_at_monotonic: 0,
    message: 'REAPER orchestra is not connected',
  };
  let preloadBbcsoOnStartup = true;
  let rendererPreloadBusy = false;
  let livePlan: LivePerformancePlanResponse | null = null;
  let firstEntrancePlan: LivePerformancePlanResponse | null = null;
  let livePlanRequestGeneration = 0;
  let rehearsalPlan: LivePerformancePlanResponse | null = null;
  let rehearsalPlanMeasure: number | null = null;
  let goingLive = false;
  let stoppingLive = false;
  let liveStartupStartedAtMs: number | null = null;
  let activePerformanceRecordingId = '';
  let pendingPerformanceRecordingId = '';
  let keepingPerformanceRecording = false;
  let missingScoreArtifacts: string[] = [];
  let midiBackendAvailable = true;
  let selectedBackendInput = '';
  // Sentinel for "no MIDI output": the orchestra sounds only through live audio
  // zones (e.g. BBCSO to a room speaker), with no MIDI copy to double it. Valid
  // for live follow and live mix audition; recording/calibration still need a real port.
  const NO_MIDI_OUTPUT = 'none';
  let selectedBackendOutput = '';
  let hardwareStatus: LiveStatusResponse = {
    phase: 'idle',
    kind: 'idle',
    running: false,
    message: 'Idle',
  };
  let hardwareRecordSeconds = '';
  // The Oguri MIDI audition is a short diagnostic, not the live BBCSO path.
  // Keep it bounded and out of the primary rehearsal controls.
  const OGURI_SOUNDCHECK_SECONDS = 12;
  let hardwareReviewLoading = false;
  let offlineRenderLoading = false;
  let takes: TakeResponse[] = [];
  let retryingAlignmentTakeId: string | null = null;
  let coverageData: CoverageDoc | null = null;
  let takesRefreshGeneration = 0;
  let coverageRefreshGeneration = 0;
  let hardwareEventGeneration = 0;
  // Explicit capture state machine (design doc §5's "Auto-arm interaction"
  // constraint): idle -> starting -> lead-in -> recording -> idle, or
  // starting -> recording directly for a free take. Keeping this separate from `hardwareStatus`
  // (server truth) is what lets the lead-in state exist at all -- the
  // recording *starts* at cue start server-side, but must keep reading as
  // "Lead-in" here until the musical entry point, not the moment the HTTP
  // call returns.
  type CaptureState = 'idle' | 'starting' | 'lead-in' | 'recording';
  let captureState: CaptureState = 'idle';
  let captureMode: 'free' | 'from-the-top' | 'from-position' = 'free';
  let captureTargetMeasure: number | null = null;
  let leadInSecondsRemaining = 0;
  let leadInTimer: ReturnType<typeof setInterval> | null = null;
  let elapsedSeconds = 0;
  let elapsedTimer: ReturnType<typeof setInterval> | null = null;
  // The after-take toast (design doc §3.1): appears on `take:recording_stopped`
  // and resolves in place on `take:alignment_done` for the same take_id,
  // rather than being clobbered by whatever `setMessage` call happens next
  // (e.g. starting the very next take). `resolved: false` means "still
  // placing it in the score" and is not auto-dismissed; only a resolved
  // toast gets a dismiss timer.
  let captureToast: { takeId: string; text: string; resolved: boolean } | null = null;
  let captureToastTimer: ReturnType<typeof setTimeout> | null = null;
  let disconnectTakeEvents: (() => void) | null = null;
  let scoreOverlayPage = 1;
  // Performance view: a distraction-free full-viewport score for playing to.
  // `performanceHeightPx` is the height budget handed to the overlay so it can
  // fit one whole page; it tracks the viewport while the view is open.
  let performanceView = false;
  let performanceHeightPx: number | null = null;
  let performanceWidthPx: number | null = null;
  let scoreShellEl: HTMLElement | null = null;
  let scoreStageEl: HTMLDivElement | null = null;
  let scoreOverlayEl: PdfCoverageOverlay | null = null;
  let selectedRehearsalMeasureNumber: number | null = null;
  let liveStartMeasureNumber: number | null = null;
  let suggestionContext = '';
  let selectedForTakeId = '';
  let positionClockMs = Date.now();
  let positionClockTimer: ReturnType<typeof setInterval> | null = null;
  let previewPositionTakeId: string | null = null;
  let selectedReviewTakeId = '';
  let passageAnalysis: PassageAnalysisResponse | null = null;
  let passageAnalysisContext = '';
  let orchestraCueEnabled = true;
  let correctionSourceSeconds: number | null = null;
  let correctionBeatInMeasure = 0;
  let savingAlignmentCorrection = false;
  type ScoreContextMenu = {
    measure: number;
    scoreBeat: number;
    x: number;
    y: number;
    maxHeight: number;
    selectedAnchor: Anchor | null;
    nearestAnchor: Anchor | null;
    anchorsInMeasure: Anchor[];
    // The span the menu acts on. A right-click is a point; a drag across the
    // measure strip is a range. Everything the menu offers is an assertion
    // about this span, which is what lets one menu serve every workflow.
    span: ScoreSpan;
    isFreeRegion: boolean;
  };
  type AnchorUndo =
    | { kind: 'add'; anchor: Anchor }
    | { kind: 'delete'; anchor: Anchor }
    | { kind: 'move'; before: Anchor; after: Anchor }
    | { kind: 'clear'; anchors: Anchor[] };
  let scoreContextMenu: ScoreContextMenu | null = null;
  let scoreContextMenuEl: HTMLDivElement | null = null;
  let anchorNotice: {
    text: string;
    x: number;
    y: number;
    undo: AnchorUndo | null;
    error: boolean;
  } | null = null;
  let alignedPassesChronological: TakeResponse[] = [];
  let contextMeasure: CoverageMeasure | null = null;
  let contextGuidance: { label: string; explanation: string; action: string } | null = null;
  let contextTake: TakeResponse | null = null;
  // The inspection panel (takes, coverage, device setup, intents) starts closed.
  // Playing is the default activity and the score is the whole surface for it;
  // data grooming is a deliberate, occasional mode you opt into.
  let inspectorOpen = false;
  let rehearsalDetailsEl: HTMLDetailsElement | null = null;
  let mixAuthoringMode = false;
  let layerMenuOpen = false;
  let mixProgram: MixProgram | null = null;
  let mixZones: ZoneConfig[] = [];
  let mixBusy = false;
  let mixSelection: {
    startTick: number;
    endTick: number;
    startMeasure: number;
    endMeasure: number;
  } | null = null;
  let mixSelectionAnchor: { tick: number; measure: number } | null = null;
  let mixMenu: {
    x: number;
    y: number;
    regionId: string | null;
    kind: 'point' | 'editor';
    scoreTick: number;
    measure: number;
  } | null = null;
  let mixGesture: 'swell' | 'fade' | 'bed' | 'feature' | 'custom' = 'swell';
  let mixZoneId = 'room_center';
  let mixStemId = 'orchestra';
  let mixStartLevel = 15;
  let mixEndLevel = 100;
  let mixEnabled = true;
  let mixAuditionBusy = false;

  $: mixAuditionActive =
    hardwareStatus.running && hardwareStatus.kind === 'mix_audition';

  type WorkspaceMode = 'perform' | 'data' | 'mixing';
  let workspaceMode: WorkspaceMode = 'perform';
  let overlayVisibility: Record<WorkspaceMode, { data: boolean; mix: boolean }> = {
    data: { data: true, mix: false },
    mixing: { data: false, mix: true },
    perform: { data: false, mix: false },
  };
  $: workspaceMode = mixAuthoringMode
    ? 'mixing' as WorkspaceMode
    : route === 'integrity'
      ? 'data' as WorkspaceMode
      : 'perform' as WorkspaceMode;

  // The Data workspace has two peer facets of one measure/beat ground truth:
  // timing (Oguri MIDI -> beat, audition & correct) and geometry/sources
  // (Audiveris/PDF integrity findings). Default to timing -- the active task.
  let dataFacet: 'timing' | 'integrity' = 'timing';
  type TimingReviewState = 'needs-listening' | 'spot-check' | 'reviewed';
  let timingReviewByMeasure: Record<number, TimingReviewState> = {};
  let dataAuditionMeasure: string | null = null;
  let timingValidationEl: AlignmentValidation | null = null;

  function liveVstZoneReady(): boolean {
    return mixZones.some(
      (zone) => zone.renderer_id === 'bbcso_vst3' && zone.health === 'ready',
    );
  }

  function mixRevision(): number {
    return mixProgram?.revision ?? 1;
  }

  function mixScoreIsStale(): boolean {
    return mixProgram?.score_identity_status === 'stale';
  }

  function mixMeasureForTick(scoreTick: number, fallback: number): number {
    const scoreBeat = scoreTick / CANONICAL_PPQ;
    return coverageData?.measures.find(
      (measure) => scoreBeat >= measure.start_beat && scoreBeat <= measure.end_beat,
    )?.measure ?? fallback;
  }

  async function refreshMixForDisplay() {
    try {
      const [programs, zones] = await Promise.all([
        listMixPrograms({ query: { piece_id: 'chopin_op11', movement: 2 } }),
        listMixZones(),
      ]);
      mixProgram = programs.programs[0] ?? null;
      mixZones = zones.zones;
    } catch (error) {
      console.error('Could not load the performance mix overlay:', error);
    }
  }

  async function enterMixAuthoring() {
    if (hardwareStatus.running || liveRuntimeActive || captureState !== 'idle' || isPlaying) {
      setMessage('Stop playback or recording before authoring the mix.');
      return;
    }
    mixBusy = true;
    try {
      await refreshMixForDisplay();
      mixProgram = mixProgram ?? await createMixProgram({
        body: {
          piece_id: 'chopin_op11',
          movement: 2,
          program_id: 'main',
          name: 'Main spatial mix',
        },
      });
      localStorage.setItem('rubato-mix-program-id', mixProgram.program_id);
      mixAuthoringMode = true;
      inspectorOpen = false;
      performanceView = false;
      scoreContextMenu = null;
      anchorNotice = null;
      setMessage('Mixing ready. Drag a score range or right-click an automation point.');
    } catch (error) {
      setMessage(describeApiError(error, 'Could not open mix authoring'));
    } finally {
      mixBusy = false;
    }
  }

  function leaveMixAuthoring() {
    if (mixAuditionActive) {
      setMessage('Stop the live mix audition before leaving Mix mode.');
      return;
    }
    mixAuthoringMode = false;
    mixSelection = null;
    mixSelectionAnchor = null;
    mixMenu = null;
    setMessage('Mix saved. Perform score restored.');
  }

  function setOverlayVisibility(layer: 'data' | 'mix', event: Event) {
    const input = event.currentTarget;
    if (!(input instanceof HTMLInputElement)) return;
    overlayVisibility = {
      ...overlayVisibility,
      [workspaceMode]: {
        ...overlayVisibility[workspaceMode],
        [layer]: input.checked,
      },
    };
  }

  async function switchWorkspace(next: WorkspaceMode) {
    if (next === workspaceMode) return;
    if (
      next === 'mixing' &&
      (hardwareStatus.running || liveRuntimeActive || captureState !== 'idle' || isPlaying)
    ) {
      setMessage('Stop the active performance before switching workspaces.');
      return;
    }
    if (next === 'mixing') {
      await enterMixAuthoring();
      return;
    }
    if (mixAuthoringMode) {
      if (mixAuditionActive) {
        setMessage('Stop the live mix audition before switching workspaces.');
        return;
      }
      leaveMixAuthoring();
    }
    inspectorOpen = false;
    performanceView = false;
    scoreContextMenu = null;
    goToRoute(next === 'data' ? 'integrity' : 'rehearsal');
    setMessage(next === 'data' ? 'Data labels and score corrections visible.' : 'Perform workspace ready.');
  }

  async function startSelectedMixAudition() {
    if (!mixProgram || !mixSelection || !selectedBackendOutput || mixAuditionBusy) return;
    if (!liveVstZoneReady()) {
      setMessage('Configure and calibrate the room orchestra renderer before auditioning it live.');
      return;
    }
    mixMenu = null;
    mixAuditionBusy = true;
    try {
      runtimeStatus = await startMixAudition({
        body: {
          piece_id: mixProgram.piece_id,
          movement: mixProgram.movement,
          program_id: mixProgram.program_id,
          expected_revision: mixRevision(),
          bundle_id: 'chopin_op11_movement_2',
          revision: null,
          output_name:
            selectedBackendOutput === NO_MIDI_OUTPUT ? '' : selectedBackendOutput,
          start_tick: mixSelection.startTick,
          end_tick: mixSelection.endTick,
          tempo_bpm: orchestraTempoBpm,
          volume: orchestraVolume / 100,
        },
      });
      setMessage(
        selectedBackendOutput === NO_MIDI_OUTPUT
          ? 'Live mix audition started through the LG soundbar.'
          : 'Live mix audition started through Keyboard and the configured orchestra renderer.',
      );
    } catch (error) {
      setMessage(describeApiError(error, 'Could not start the live mix audition'));
    } finally {
      mixAuditionBusy = false;
    }
  }

  async function stopSelectedMixAudition() {
    if (mixAuditionBusy) return;
    mixAuditionBusy = true;
    try {
      runtimeStatus = await stopMixAuditionRequest();
      setMessage('Live mix audition stopped.');
    } catch (error) {
      setMessage(describeApiError(error, 'Could not stop the live mix audition'));
    } finally {
      mixAuditionBusy = false;
    }
  }

  async function reloadMixProgram() {
    if (!mixProgram) return;
    const programs = await listMixPrograms({
      query: { piece_id: mixProgram.piece_id, movement: mixProgram.movement },
    });
    mixProgram = programs.programs.find((program) => program.program_id === mixProgram?.program_id) ?? null;
  }

  async function rebindCurrentMixProgram() {
    if (!mixProgram || !mixScoreIsStale()) return;
    mixBusy = true;
    try {
      mixProgram = await rebindMixProgram({
        path: { program_id: mixProgram.program_id },
        query: { piece_id: mixProgram.piece_id, movement: mixProgram.movement },
        body: { expected_revision: mixRevision() },
      });
      setMessage('Mix program rebound to the current score after review.');
    } catch (error) {
      setMessage(describeApiError(error, 'Could not rebind the mix program'));
      await reloadMixProgram();
    } finally {
      mixBusy = false;
    }
  }

  function handleMixSelection(event: CustomEvent<{
    startTick: number;
    endTick: number;
    startMeasure: number;
    endMeasure: number;
    extend: boolean;
  }>) {
    const point = event.detail;
    if (point.extend && mixSelectionAnchor) {
      mixSelection = {
        startTick: Math.min(mixSelectionAnchor.tick, point.endTick),
        endTick: Math.max(mixSelectionAnchor.tick, point.endTick),
        startMeasure: Math.min(mixSelectionAnchor.measure, point.endMeasure),
        endMeasure: Math.max(mixSelectionAnchor.measure, point.endMeasure),
      };
    } else {
      mixSelectionAnchor = { tick: point.startTick, measure: point.startMeasure };
      mixSelection = {
        startTick: point.startTick,
        endTick: point.endTick > point.startTick ? point.endTick : point.startTick + CANONICAL_PPQ,
        startMeasure: point.startMeasure,
        endMeasure: point.endMeasure,
      };
    }
    mixMenu = null;
  }

  function handleMixContext(event: CustomEvent<{
    scoreTick: number;
    measure: number;
    clientX: number;
    clientY: number;
    regionId: string | null;
  }>) {
    const selected = mixProgram?.regions?.find((region) => region.region_id === event.detail.regionId);
    if (selected) {
      mixSelection = {
        startTick: selected.start_tick,
        endTick: selected.end_tick,
        startMeasure: mixMeasureForTick(selected.start_tick, event.detail.measure),
        endMeasure: mixMeasureForTick(selected.end_tick, event.detail.measure),
      };
      mixGesture = selected.gesture ?? 'custom';
      const route = selected.routes[0];
      mixZoneId = route.zone_id;
      mixStemId = route.stem_ids[0] ?? 'orchestra';
      mixStartLevel = Math.round(route.envelope?.[0]?.level ?? route.level ?? 100);
      const lastEnvelopePoint = route.envelope?.[Math.max(0, (route.envelope?.length ?? 1) - 1)];
      mixEndLevel = Math.round(lastEnvelopePoint?.level ?? route.level ?? 100);
      mixEnabled = selected.enabled ?? true;
    }
    mixMenu = {
      x: event.detail.clientX,
      y: event.detail.clientY,
      regionId: event.detail.regionId,
      kind: selected ? 'editor' : 'point',
      scoreTick: event.detail.scoreTick,
      measure: event.detail.measure,
    };
  }

  function setMixControlStart() {
    if (!mixMenu) return;
    mixSelectionAnchor = { tick: mixMenu.scoreTick, measure: mixMenu.measure };
    mixSelection = null;
    mixMenu = null;
    setMessage(`Mix start set at m. ${mixSelectionAnchor.measure}. Right-click the ending beat.`);
  }

  function setMixControlEnd() {
    if (!mixMenu || !mixSelectionAnchor) return;
    const end = { tick: mixMenu.scoreTick, measure: mixMenu.measure };
    const startTick = Math.min(mixSelectionAnchor.tick, end.tick);
    let endTick = Math.max(mixSelectionAnchor.tick, end.tick);
    if (endTick === startTick) endTick += CANONICAL_PPQ;
    mixSelection = {
      startTick,
      endTick,
      startMeasure: Math.min(mixSelectionAnchor.measure, end.measure),
      endMeasure: Math.max(mixSelectionAnchor.measure, end.measure),
    };
    mixMenu = {
      ...mixMenu,
      kind: 'editor',
      regionId: null,
    };
  }

  function openMixEditorForSelection() {
    if (!mixSelection) return;
    mixMenu = {
      x: 80,
      y: 230,
      regionId: null,
      kind: 'editor',
      scoreTick: mixSelection.startTick,
      measure: mixSelection.startMeasure,
    };
  }

  function authoredRegion(regionId: string): MixRegionInput | null {
    if (!mixSelection) return null;
    const startTick = mixSelection.startTick;
    const endTick = Math.max(startTick + 1, mixSelection.endTick);
    const span = endTick - startTick;
    const featureAttackTick = startTick + Math.max(1, Math.round(span * 0.15));
    const featureReleaseTick = endTick - Math.max(1, Math.round(span * 0.2));
    const envelope = mixGesture === 'bed'
      ? [
          { score_tick: startTick, level: mixEndLevel, curve: 'hold' as const },
          { score_tick: endTick, level: mixEndLevel, curve: 'hold' as const },
        ]
      : mixGesture === 'feature' && featureAttackTick < featureReleaseTick
        ? [
            { score_tick: startTick, level: mixStartLevel, curve: 'equal_power' as const },
            { score_tick: featureAttackTick, level: mixEndLevel, curve: 'hold' as const },
            { score_tick: featureReleaseTick, level: mixEndLevel, curve: 'equal_power' as const },
            { score_tick: endTick, level: mixStartLevel, curve: 'equal_power' as const },
          ]
        : [
            {
              score_tick: startTick,
              level: mixStartLevel,
              curve: mixGesture === 'custom' ? 'linear' as const : 'equal_power' as const,
            },
            {
              score_tick: endTick,
              level: mixEndLevel,
              curve: mixGesture === 'custom' ? 'linear' as const : 'equal_power' as const,
            },
          ];
    return {
      region_id: regionId,
      start_tick: startTick,
      end_tick: endTick,
      gesture: mixGesture,
      enabled: mixEnabled,
      routes: [{
        zone_id: mixZoneId,
        stem_ids: [mixStemId.trim() || 'orchestra'],
        level: mixEndLevel,
        fallback_zone_id: mixZoneId === 'yamaha_anchor' ? null : 'yamaha_anchor',
        envelope,
      }],
    };
  }

  function applyMixGesturePreset(event: Event) {
    const select = event.currentTarget;
    if (!(select instanceof HTMLSelectElement)) return;
    mixGesture = select.value as typeof mixGesture;
    if (mixGesture === 'swell') [mixStartLevel, mixEndLevel] = [15, 100];
    if (mixGesture === 'fade') [mixStartLevel, mixEndLevel] = [100, 15];
    if (mixGesture === 'bed') [mixStartLevel, mixEndLevel] = [30, 30];
    if (mixGesture === 'feature') [mixStartLevel, mixEndLevel] = [25, 100];
  }

  async function saveMixRegion() {
    if (!mixProgram || !mixSelection) return;
    mixBusy = true;
    const existingId = mixMenu?.regionId;
    const regionId = existingId ?? `region_${Date.now()}`;
    const region = authoredRegion(regionId);
    if (!region) return;
    try {
      mixProgram = existingId
        ? await updateMixRegion({
            path: { program_id: mixProgram.program_id, region_id: existingId },
            query: { piece_id: mixProgram.piece_id, movement: mixProgram.movement },
            body: { expected_revision: mixRevision(), region },
          })
        : await createMixRegion({
            path: { program_id: mixProgram.program_id },
            query: { piece_id: mixProgram.piece_id, movement: mixProgram.movement },
            body: { expected_revision: mixRevision(), region },
          });
      setMessage(existingId ? 'Mix region updated.' : 'Mix region created.');
      mixMenu = null;
      mixSelection = null;
      mixSelectionAnchor = null;
    } catch (error) {
      const detail = describeApiError(error, 'Could not save the mix region');
      setMessage(detail.toLowerCase().includes('overlap')
        ? 'This output already has automation here. Right-click its bracket to edit it, or choose another output.'
        : detail);
      await reloadMixProgram();
    } finally {
      mixBusy = false;
    }
  }

  async function removeMixRegion() {
    if (!mixProgram || !mixMenu?.regionId) return;
    mixBusy = true;
    try {
      mixProgram = await deleteMixRegion({
        path: { program_id: mixProgram.program_id, region_id: mixMenu.regionId },
        query: {
          piece_id: mixProgram.piece_id,
          movement: mixProgram.movement,
          expected_revision: mixRevision(),
        },
      });
      mixMenu = null;
      mixSelection = null;
      mixSelectionAnchor = null;
      setMessage('Mix region deleted. Undo is available.');
    } catch (error) {
      setMessage(describeApiError(error, 'Could not delete the mix region'));
      await reloadMixProgram();
    } finally {
      mixBusy = false;
    }
  }

  async function undoMixEdit() {
    if (!mixProgram) return;
    mixBusy = true;
    try {
      mixProgram = await undoMixProgram({
        path: { program_id: mixProgram.program_id },
        query: { piece_id: mixProgram.piece_id, movement: mixProgram.movement },
        body: { expected_revision: mixRevision() },
      });
      mixSelection = null;
      mixSelectionAnchor = null;
      mixMenu = null;
      setMessage('Last durable mix revision restored.');
    } catch (error) {
      setMessage(describeApiError(error, 'Could not undo the mix edit'));
    } finally {
      mixBusy = false;
    }
  }

  function hardwareScorePosition(
    status: LiveStatusResponse,
    nowMs: number,
  ): ScorePositionResponse | null {
    if (!status.running) return null;
    return transportPositionAt(
      status.score_transport,
      status.started_at,
      nowMs,
      coverageData?.measures ?? [],
    );
  }

  function suggestedNextMeasure(
    measures: CoverageMeasure[],
    afterMeasure: number,
  ): CoverageMeasure | null {
    const targets = measures.filter(
      (measure) => measure.scope === 'solo' && measure.state !== 'covered',
    );
    return (
      targets.find((measure) => measure.measure > afterMeasure && !measure.observed) ??
      targets.find((measure) => !measure.observed) ??
      targets[0] ??
      null
    );
  }

  function selectRehearsalMeasureNumber(measure: number) {
    selectedRehearsalMeasureNumber = measure;
    liveStartMeasureNumber = measure;
    selectedForTakeId = latestTake?.take_id ?? 'no-take';
    void refreshRehearsalPlan(measure);
  }

  // Data-mode (alignment validation) measure selection: only highlight and
  // page-follow the score. Deliberately does NOT touch rehearsal state
  // (liveStartMeasureNumber / take selection / plan fetches) -- auditing timing
  // must not change where a later live performance would start. Non-numeric
  // labels (split bars like "106a") are ignored rather than becoming NaN.
  function selectDataMeasure(label: string) {
    const measure = parseInt(label, 10);
    if (!Number.isFinite(measure)) return;
    selectedRehearsalMeasureNumber = measure;
    dataAuditionMeasure = String(measure);
  }

  function updateTimingReviewState(state: Record<number, TimingReviewState>) {
    timingReviewByMeasure = state;
  }

  function selectRehearsalMeasure(event: CustomEvent<{ measure: number }>) {
    if (workspaceMode === 'data') {
      if (dataFacet === 'timing') selectDataMeasure(String(event.detail.measure));
      else selectedRehearsalMeasureNumber = event.detail.measure;
      return;
    }
    selectRehearsalMeasureNumber(event.detail.measure);
  }

  function selectTakeOnScore(take: TakeResponse) {
    if (!take.score_span) return;
    selectedReviewTakeId = take.take_id;
    selectRehearsalMeasureNumber(
      takeEntryMeasure(take, coverageData?.measures ?? []) ??
        take.score_span.start.measure_index + 1,
    );
  }

  function passageRangeLabel(group: PassageTakeGroup): string {
    if (group.startMeasure === null || group.endMeasure === null) {
      return 'waiting for score placement';
    }
    return group.startMeasure === group.endMeasure
      ? `covers m. ${group.startMeasure}`
      : `covers mm. ${group.startMeasure}–${group.endMeasure}`;
  }

  function passageTitle(group: PassageTakeGroup): string {
    return group.entryMeasure === null
      ? 'Needs score placement'
      : `From measure ${group.entryMeasure}`;
  }

  function alignedPasses(group: PassageTakeGroup): TakeResponse[] {
    return group.takes.filter(
      (take) => take.status === 'aligned' && take.disposition !== 'discarded',
    );
  }

  function passNumber(group: PassageTakeGroup, take: TakeResponse): number {
    return [...alignedPasses(group)].reverse().findIndex(
      (candidate) => candidate.take_id === take.take_id,
    ) + 1;
  }

  function passageGroupSummary(group: PassageTakeGroup): string {
    const alignedCount = alignedPasses(group).length;
    const needsAttention = group.takes.filter(
      (take) => take.status !== 'aligned' && take.status !== 'discarded',
    ).length;
    const excludedCount = group.takes.filter(
      (take) => take.status === 'discarded' || take.disposition === 'discarded',
    ).length;
    const parts = [
      `${alignedCount} aligned ${alignedCount === 1 ? 'pass' : 'passes'}`,
    ];
    if (needsAttention) {
      parts.push(`${needsAttention} ${needsAttention === 1 ? 'attempt needs' : 'attempts need'} attention`);
    }
    if (excludedCount) {
      parts.push(`${excludedCount} excluded`);
    }
    parts.push(passageRangeLabel(group));
    return parts.join(' · ');
  }

  function takeRowTitle(group: PassageTakeGroup, take: TakeResponse): string {
    if (take.status === 'aligned' && take.disposition !== 'discarded') {
      return `Pass ${passNumber(group, take)} of ${alignedPasses(group).length} at this location`;
    }
    if (take.status === 'discarded' || take.disposition === 'discarded') {
      return 'Excluded recording';
    }
    return 'Attempt needs attention';
  }

  function passLocationLabel(take: TakeResponse): string {
    const entry = takeEntryMeasure(take, coverageData?.measures ?? []);
    return entry === null ? 'unplaced recording' : `pass from measure ${entry}`;
  }

  function selectPassageGroup(group: PassageTakeGroup) {
    if (group.entryMeasure !== null) {
      selectedReviewTakeId = group.takes[0]?.take_id ?? '';
      selectRehearsalMeasureNumber(group.entryMeasure);
    }
  }

  function selectGroupRecording(group: PassageTakeGroup, takeId: string) {
    selectedReviewTakeId = takeId;
    if (group.entryMeasure !== null) {
      selectRehearsalMeasureNumber(group.entryMeasure);
      selectedReviewTakeId = takeId;
    }
  }

  function selectGroupRecordingEvent(group: PassageTakeGroup, event: Event) {
    selectGroupRecording(group, (event.currentTarget as HTMLSelectElement).value);
  }

  function selectedRecordingForGroup(group: PassageTakeGroup): TakeResponse {
    return group.takes.find((take) => take.take_id === selectedReviewTakeId) ?? group.takes[0];
  }

  function alignmentRatingLabel(take: TakeResponse): string {
    if (!take.alignment) return 'Alignment pending';
    if (take.alignment.rating === 'strong') return 'Strong alignment';
    if (take.alignment.rating === 'usable') return 'Usable alignment';
    return 'Needs alignment review';
  }

  function alignmentPercent(take: TakeResponse): number | null {
    return take.alignment ? Math.round(take.alignment.note_match_rate * 100) : null;
  }

  function passagePassNumber(take: TakeResponse): number | null {
    const index = alignedPassesChronological.findIndex(
      (candidate) => candidate.take_id === take.take_id,
    );
    return index < 0 ? null : index + 1;
  }

  function recordingOptionLabel(take: TakeResponse, localPassNumber: number | null = null): string {
    const passLabel = localPassNumber ? `Pass ${localPassNumber}` : 'Recording';
    if (take.note_on_count === 0) {
      return `Incomplete attempt · ${formatRecordedTime(take.recorded_at)} local time · no notes`;
    }
    if (take.status === 'aligned' && take.score_span) {
      const match = alignmentPercent(take);
      return `${passLabel} · ${alignmentRatingLabel(take)}${match === null ? '' : ` (${match}% score match)`} · ${formatRecordedTime(take.recorded_at)}`;
    }
    return `${passLabel} · ${take.analysis_state} · ${formatRecordedTime(take.recorded_at)} · ${take.note_on_count} notes`;
  }

  function coverageGuidance(
    measure: CoverageMeasure | null,
    targetObservations = coverageData?.n_target ?? 3,
    qualityTarget = coverageData?.quality_target ?? 0.5,
  ): {
    label: string;
    explanation: string;
    action: string;
  } {
    if (!measure) {
      return {
        label: 'Score role pending',
        explanation: 'Rubato has not classified this printed measure yet.',
        action: 'No rehearsal action is being requested.',
      };
    }
    if (measure.accompaniment_required === false) {
      return {
        label: 'Ready — piano only',
        explanation: 'The score gives the orchestra no notes to play in this measure.',
        action: 'No orchestral rehearsal evidence is needed here.',
      };
    }
    if (measure.scope === 'tutti') {
      return {
        label: 'Ready — orchestra leads',
        explanation: 'The score assigns this passage to the orchestra rather than the solo piano.',
        action: 'No additional piano take is requested.',
      };
    }
    if (measure.state === 'covered') {
      return {
        label: 'Enough rehearsal evidence',
        explanation: `Every half-beat has at least ${targetObservations} observations and ${Math.round(qualityTarget * 100)}% alignment quality.`,
        action: 'Another pass is optional; record again only if you want another interpretation.',
      };
    }
    if (measure.observed) {
      const observationCount = measure.min_n ?? 0;
      const weakestQuality = measure.min_quality ?? 0;
      const countReady = observationCount >= targetObservations;
      const qualityReady = weakestQuality >= qualityTarget;
      let explanation: string;
      if (countReady && !qualityReady) {
        explanation = `All half-beats have ${targetObservations}+ observations, but the weakest alignment quality is ${Math.round(weakestQuality * 100)}%; green requires ${Math.round(qualityTarget * 100)}%.`;
      } else if (!countReady && qualityReady) {
        explanation = `Alignment quality clears ${Math.round(qualityTarget * 100)}%, but the least-observed half-beat has ${observationCount} of ${targetObservations} target observations.`;
      } else {
        explanation = `The weakest half-beat has ${observationCount} of ${targetObservations} observations and ${Math.round(weakestQuality * 100)}% alignment quality; green requires both targets.`;
      }
      return {
        label: 'Still learning',
        explanation,
        action: 'One more clean pass through this measure can strengthen the model.',
      };
    }
    if (measure.scope === 'solo' || measure.solo) {
      return {
        label: 'Needs a first pass',
        explanation: 'No aligned piano take currently covers this measure.',
        action: 'Record a pass from here or from an earlier musical entrance.',
      };
    }
    return {
      label: 'Orchestral passage',
      explanation: 'This is not currently classified as a solo rehearsal target.',
      action: 'No additional piano take is requested.',
    };
  }

  function takeAtMeasure(
    measureNumber: number,
    availableTakes = takes,
    coverage = coverageData,
  ): TakeResponse | null {
    const measure = coverage?.measures.find((candidate) => candidate.measure === measureNumber);
    if (!measure) return null;
    return (
      availableTakes
        .filter((take) => {
          const span = take.score_span;
          return (
            take.status === 'aligned' &&
            take.disposition === 'kept' &&
            !!span &&
            measure.start_beat < span.end.score_beat &&
            measure.end_beat > span.start.score_beat
          );
        })
        .sort((left, right) => Date.parse(right.recorded_at) - Date.parse(left.recorded_at))[0] ?? null
    );
  }

  function openScoreContextMenu(
    event: CustomEvent<{
      measure: number;
      scoreBeat: number;
      clientX: number;
      clientY: number;
      selectedAnchor: Anchor | null;
      nearestAnchor: Anchor | null;
      anchorsInMeasure: Anchor[];
      span?: ScoreSpan;
    }>,
  ) {
    const timingMenu = workspaceMode === 'data' && dataFacet === 'timing';
    const menuWidth = timingMenu ? 190 : 340;
    const menuHeight = timingMenu ? 120 : 560;
    const position = scorePopupPosition(
      event.detail.clientX,
      event.detail.clientY,
      menuWidth,
      menuHeight,
    );
    anchorNotice = null;
    scoreContextMenu = {
      measure: event.detail.measure,
      scoreBeat: event.detail.scoreBeat,
      ...position,
      selectedAnchor: event.detail.selectedAnchor,
      nearestAnchor: event.detail.nearestAnchor,
      anchorsInMeasure: event.detail.anchorsInMeasure,
      span: event.detail.span ?? {
        kind: 'point',
        fromMeasure: event.detail.measure,
        toMeasure: event.detail.measure,
        beat: event.detail.scoreBeat,
      },
      isFreeRegion: freeRegionCovers(event.detail.measure),
    };
    if (timingMenu) {
      // Timing review selects only the audition target. It must not quietly
      // mutate the later rehearsal entry point or prepare a follower plan.
      selectDataMeasure(String(event.detail.measure));
    } else {
      // A right-clicked measure is also the candidate recording start. Resolve
      // its section/follower plan while the menu is open so the performer sees
      // what will happen before the record action becomes available.
      selectRehearsalMeasureNumber(event.detail.measure);
    }
    void tick().then(() =>
      scoreContextMenuEl
        ?.querySelector<HTMLButtonElement>('button')
        ?.focus({ preventScroll: true }),
    );
  }

  function closeScoreContextMenu() {
    scoreContextMenu = null;
  }

  const MOVEMENT = 2;

  // Two workflows, not three. Bundling/cleaning/integrity is its own job --
  // offline, iterative, no piano in the loop, failures about provenance rather
  // than timing -- so it gets its own route. Rehearsal and live performance are
  // the SAME act at different stakes (same follower, sections and anchors,
  // differing only in whether a take is recorded), so they stay one view;
  // `performanceView` is presentation, not a third workflow.
  type WorkflowRoute = 'rehearsal' | 'integrity';
  let route: WorkflowRoute = readRoute();

  function readRoute(): WorkflowRoute {
    return window.location.hash.replace(/^#\/?/, '') === 'integrity' ? 'integrity' : 'rehearsal';
  }

  function goToRoute(next: WorkflowRoute) {
    route = next;
    window.location.hash = next === 'integrity' ? '#/integrity' : '';
  }

  let freeRegions: FreeRegion[] = [];

  // The integrity findings, projected onto the score the performer is reading.
  // Same source as the integrity view, so the two can never disagree about
  // which bars are in question.
  let needsInfo: Record<number, { severity: string; reason: string }> = {};

  async function refreshNeedsInfo() {
    try {
      const health = await getScoreBundleHealth({
        path: { movement: MOVEMENT },
        query: { piece_id: 'chopin_op11' },
      });
      const next: Record<number, { severity: string; reason: string }> = {};
      for (const finding of health.findings ?? []) {
        for (const raw of finding.measures ?? []) {
          const measure = Number(raw);
          if (!Number.isFinite(measure)) continue;
          // An error outranks a warning: if a bar is both weak and disputed,
          // the decision is the thing worth surfacing.
          if (next[measure]?.severity === 'error') continue;
          next[measure] = { severity: finding.severity, reason: finding.message };
        }
      }
      needsInfo = next;
    } catch (error) {
      console.error('Could not load score health:', error);
    }
  }

  // What is true about the span the menu is pointing at. Kept as one object so
  // the catalogue decides what to offer and the template only renders.
  $: contextAssertionState = {
    span: scoreContextMenu?.span ?? { kind: 'point' as const, fromMeasure: 1, toMeasure: 1 },
    hasSelectedAnchor: !!scoreContextMenu?.selectedAnchor,
    hasNearbyAnchor: !!scoreContextMenu?.nearestAnchor,
    anchorsInSpan: scoreContextMenu?.anchorsInMeasure.length ?? 0,
    isFreeRegion: scoreContextMenu?.isFreeRegion ?? false,
    canStartLive:
      !!selectedBackendInput &&
      !!selectedBackendOutput &&
      !goingLive &&
      !hardwareStatus.running,
  } satisfies AssertionContext;
  $: contextAssertions = scoreContextMenu ? assertionsFor(contextAssertionState) : [];

  function freeRegionCovers(measure: number): boolean {
    return freeRegions.some((r) => measure >= r.from_measure && measure <= r.to_measure);
  }

  async function refreshFreeRegions() {
    try {
      const response = await listFreeRegions({ path: { movement: MOVEMENT } });
      freeRegions = response.regions ?? [];
    } catch (error) {
      console.error('Could not load free regions:', error);
    }
  }

  // One dispatcher for every assertion. A new workflow adds a case here and an
  // entry in the catalogue -- it does not add a section to the menu, which is
  // what kept the old menu growing with each feature.
  async function applyAssertion(id: string) {
    const menu = scoreContextMenu;
    if (!menu) return;
    const span = menu.span;
    switch (id) {
      case 'anchor.add':
        await addContextAnchor();
        return;
      case 'anchor.move':
        await moveNearestContextAnchor();
        return;
      case 'anchor.remove':
        await deleteSelectedContextAnchor();
        return;
      case 'anchor.clearSpan':
        await clearContextMeasureAnchors();
        return;
      case 'live.start':
        await goLiveFromContextMenu();
        return;
      case 'region.free': {
        closeScoreContextMenu();
        try {
          const response = await declareFreeRegion({
            path: { movement: MOVEMENT },
            body: {
              from_measure: span.fromMeasure,
              to_measure: span.toMeasure,
              label: 'Free region',
            },
          });
          freeRegions = response.regions ?? freeRegions;
          setMessage(`Marked m. ${span.fromMeasure}–${span.toMeasure} as a free region.`);
        } catch (error) {
          setMessage(`Could not mark that span: ${error}`);
        }
        return;
      }
      case 'region.free.clear': {
        closeScoreContextMenu();
        const region = freeRegions.find(
          (r) => span.fromMeasure >= r.from_measure && span.fromMeasure <= r.to_measure,
        );
        if (!region) return;
        try {
          const response = await clearFreeRegion({
            path: { movement: MOVEMENT, from_measure: region.from_measure },
          });
          freeRegions = response.regions ?? [];
          setMessage(`m. ${region.from_measure}–${region.to_measure} follows normally again.`);
        } catch (error) {
          setMessage(`Could not clear that region: ${error}`);
        }
        return;
      }
      case 'measure.missing':
      case 'measure.wrong':
      case 'measure.spurious': {
        // Reuses the existing correction surface rather than adding a second
        // one. That panel resolves a claim by ear -- "this sound is this beat"
        // -- which is the only evidence that settles a layout disagreement.
        // Selecting the measure here is what prefills it.
        closeScoreContextMenu();
        selectRehearsalMeasureNumber(span.fromMeasure);
        correctionBeatInMeasure = Math.max(0, Math.min(3, Math.round((span.beat ?? 1) - 1)));
        setMessage(
          `m. ${span.fromMeasure} selected. Play the passage, then confirm the beat in ` +
            `“Correct the score alignment” to record it.`,
          7000,
        );
        return;
      }
      default:
        closeScoreContextMenu();
    }
  }

  function scoreBeatLabel(measureNumber: number, scoreBeat: number): string {
    const measure = coverageData?.measures.find((candidate) => candidate.measure === measureNumber);
    if (!measure) return scoreBeat.toFixed(2);
    return (scoreBeat - measure.start_beat + 1).toFixed(2).replace(/\.00$/, '');
  }

  function scorePopupPosition(
    clientX: number,
    clientY: number,
    width: number,
    height: number,
  ): { x: number; y: number; maxHeight: number } {
    const stage = scoreStageEl;
    if (!stage) {
      return {
        x: Math.max(12, Math.min(clientX, window.innerWidth - width - 12)),
        y: Math.max(12, Math.min(clientY, window.innerHeight - height - 12)),
        maxHeight: Math.max(0, Math.min(height, window.innerHeight - 24)),
      };
    }
    const stageRect = stage.getBoundingClientRect();
    const shellRect = scoreShellEl?.getBoundingClientRect() ?? {
      top: 0,
      right: window.innerWidth,
      bottom: window.innerHeight,
      left: 0,
    };
    const visibleTop = Math.max(0, shellRect.top - stageRect.top);
    const visibleBottom = Math.min(stageRect.height, shellRect.bottom - stageRect.top);
    const visibleHeight = Math.max(0, visibleBottom - visibleTop);
    const fittedHeight = Math.min(height, Math.max(0, visibleHeight - 24));
    return {
      x: Math.max(12, Math.min(clientX - stageRect.left, stageRect.width - width - 12)),
      y: Math.max(
        visibleTop + 12,
        Math.min(clientY - stageRect.top, visibleBottom - fittedHeight - 12),
      ),
      maxHeight: fittedHeight,
    };
  }

  function showAnchorNotice(
    text: string,
    undo: AnchorUndo | null,
    options: { error?: boolean; clientX?: number; clientY?: number } = {},
  ) {
    const sourceX = options.clientX ?? scoreContextMenu?.x ?? window.innerWidth / 2;
    const sourceY = options.clientY ?? scoreContextMenu?.y ?? window.innerHeight / 2;
    const position =
      options.clientX !== undefined || !scoreContextMenu
        ? scorePopupPosition(sourceX, sourceY, 300, 90)
        : { x: sourceX, y: sourceY };
    anchorNotice = {
      text,
      undo,
      error: options.error ?? false,
      ...position,
    };
    closeScoreContextMenu();
  }

  async function addContextAnchor() {
    if (!scoreContextMenu || !scoreOverlayEl) return;
    const context = scoreContextMenu;
    const created = await scoreOverlayEl.addAnchorAt(context.measure, context.scoreBeat);
    if (!created) {
      showAnchorNotice('Rubato could not add that anchor.', null, { error: true });
      return;
    }
    showAnchorNotice(
      `Anchor added at m. ${context.measure}, beat ${scoreBeatLabel(context.measure, context.scoreBeat)}.`,
      { kind: 'add', anchor: created },
    );
  }

  async function moveNearestContextAnchor() {
    const nearestAnchor = scoreContextMenu?.nearestAnchor;
    if (!scoreContextMenu || !nearestAnchor || !scoreOverlayEl) return;
    const context = scoreContextMenu;
    const result = await scoreOverlayEl.moveAnchorTo(
      nearestAnchor.score_tick,
      context.measure,
      context.scoreBeat,
    );
    if (!result) {
      showAnchorNotice('Rubato could not move that anchor.', null, { error: true });
      return;
    }
    showAnchorNotice(
      `Anchor moved to m. ${context.measure}, beat ${scoreBeatLabel(context.measure, context.scoreBeat)}.`,
      { kind: 'move', ...result },
    );
  }

  async function deleteSelectedContextAnchor() {
    const selectedAnchor = scoreContextMenu?.selectedAnchor;
    if (!scoreContextMenu || !selectedAnchor || !scoreOverlayEl) return;
    const context = scoreContextMenu;
    const removed = await scoreOverlayEl.removeAnchorAt(selectedAnchor.score_tick);
    if (!removed) {
      showAnchorNotice('Rubato could not remove that anchor.', null, { error: true });
      return;
    }
    showAnchorNotice(`Anchor removed from m. ${removed.measure}.`, {
      kind: 'delete',
      anchor: removed,
    });
  }

  async function clearContextMeasureAnchors() {
    if (!scoreContextMenu || !scoreOverlayEl) return;
    const context = scoreContextMenu;
    const removed = await scoreOverlayEl.clearAnchorsAtMeasure(context.measure);
    if (!removed) {
      showAnchorNotice('Rubato could not clear those anchors.', null, { error: true });
      return;
    }
    showAnchorNotice(
      `${removed.length} anchor${removed.length === 1 ? '' : 's'} removed from m. ${context.measure}.`,
      { kind: 'clear', anchors: removed },
    );
  }

  function handleAnchorChanged(
    event: CustomEvent<{
      kind: 'move';
      before: Anchor;
      after: Anchor;
      clientX: number;
      clientY: number;
    }>,
  ) {
    showAnchorNotice(
      `Anchor moved within m. ${event.detail.after.measure}.`,
      {
        kind: 'move',
        before: event.detail.before,
        after: event.detail.after,
      },
      {
        clientX: event.detail.clientX,
        clientY: event.detail.clientY,
      },
    );
  }

  function handleAnchorChangeFailed(
    event: CustomEvent<{ message: string; clientX: number; clientY: number }>,
  ) {
    showAnchorNotice(event.detail.message, null, {
      error: true,
      clientX: event.detail.clientX,
      clientY: event.detail.clientY,
    });
  }

  async function undoAnchorChange() {
    const undo = anchorNotice?.undo;
    if (!undo || !scoreOverlayEl) return;
    let succeeded = false;
    if (undo.kind === 'add') {
      succeeded = (await scoreOverlayEl.removeAnchorAt(undo.anchor.score_tick)) !== null;
    } else if (undo.kind === 'delete') {
      succeeded = await scoreOverlayEl.restoreAnchors([undo.anchor]);
    } else if (undo.kind === 'move') {
      succeeded =
        (await scoreOverlayEl.moveAnchorTo(
          undo.after.score_tick,
          undo.before.measure,
          undo.before.score_tick / CANONICAL_PPQ,
        )) !== null;
    } else {
      succeeded = await scoreOverlayEl.restoreAnchors(undo.anchors);
    }
    anchorNotice = {
      ...(anchorNotice ?? {
        x: window.innerWidth / 2,
        y: window.innerHeight / 2,
        error: false,
      }),
      text: succeeded ? 'Anchor change undone.' : 'Rubato could not undo that anchor change.',
      undo: null,
      error: !succeeded,
    };
  }

  async function reviewMeasureFromMenu(measure: number) {
    const take = takeAtMeasure(measure);
    if (take) selectedReviewTakeId = take.take_id;
    selectRehearsalMeasureNumber(measure);
    closeScoreContextMenu();
    inspectorOpen = true;
    await tick();
    if (rehearsalDetailsEl) rehearsalDetailsEl.open = true;
  }

  function reviewContextMeasureFromMenu() {
    if (scoreContextMenu) void reviewMeasureFromMenu(scoreContextMenu.measure);
  }

  async function recordMeasureFromMenu(measure: number) {
    selectRehearsalMeasureNumber(measure);
    closeScoreContextMenu();
    await tick();
    if (orchestraCueEnabled) {
      await refreshRehearsalPlan(measure);
    }
    await startSelectedPassage();
  }

  async function recordContextMeasureFromMenu() {
    if (scoreContextMenu) await recordMeasureFromMenu(scoreContextMenu.measure);
  }

  function playMeasureFromMenu(measure: number, variant: 'solo' | 'ensemble') {
    const take = takeAtMeasure(measure);
    closeScoreContextMenu();
    if (!take) {
      setMessage(`No aligned recording covers measure ${measure} yet.`);
      return;
    }
    void hearAlignedTake(take, variant);
  }

  function playContextMeasureFromMenu(variant: 'solo' | 'ensemble') {
    if (scoreContextMenu) playMeasureFromMenu(scoreContextMenu.measure, variant);
  }

  async function playTimingMeasureFromMenu() {
    const measure = scoreContextMenu?.measure;
    closeScoreContextMenu();
    if (measure === undefined) return;
    await tick();
    await timingValidationEl?.playMeasure(String(measure));
  }

  function prepareAlignmentCorrection(event: Event) {
    const details = event.currentTarget as HTMLDetailsElement;
    if (!details.open || !displayedScorePosition) return;
    correctionSourceSeconds = displayedScorePosition.source_seconds;
    correctionBeatInMeasure = Math.max(
      0,
      Math.min(3, Math.round(displayedScorePosition.beat_in_measure)),
    );
    selectRehearsalMeasureNumber(displayedScorePosition.measure_index + 1);
  }

  async function saveAlignmentCorrection() {
    if (correctionSourceSeconds === null || !selectedRehearsalMeasureNumber) {
      setMessage('Start playback, then choose the score measure for the sound you heard.');
      return;
    }
    savingAlignmentCorrection = true;
    try {
      const relevantTake = previewPositionTake ?? selectedReviewTake;
      const result = await createScoreAlignmentCorrection({
        path: { movement: 2 },
        query: { piece_id: 'chopin_op11' },
        body: {
          source_seconds: correctionSourceSeconds,
          measure: selectedRehearsalMeasureNumber,
          beat_in_measure: correctionBeatInMeasure,
          ...(relevantTake ? { take_id: relevantTake.take_id } : {}),
        },
      });
      setMessage(
        `Anchor saved: that sound is m. ${result.measure_label}, beat ${result.beat_in_measure + 1}.`,
      );
      correctionSourceSeconds = null;
      if (result.reanalysis_queued) await refreshTakes();
    } catch (error) {
      setMessage(describeApiError(error, 'Could not save the score anchor'));
    } finally {
      savingAlignmentCorrection = false;
    }
  }

  function generatePerformanceId(): string {
    const now = new Date();
    const pad = (n: number) => n.toString().padStart(2, '0');
    const yyyy = now.getUTCFullYear();
    const mm = pad(now.getUTCMonth() + 1);
    const dd = pad(now.getUTCDate());
    const hh = pad(now.getUTCHours());
    const min = pad(now.getUTCMinutes());
    const ss = pad(now.getUTCSeconds());
    const hex = Math.floor(Math.random() * 0x10000).toString(16).padStart(4, '0');
    return `performance-${yyyy}${mm}${dd}T${hh}${min}${ss}Z-${hex}`;
  }

  async function refreshTakes() {
    const generation = ++takesRefreshGeneration;
    try {
      // `TakeListResponse.takes` is a required field (never absent) once
      // validated at the boundary -- `data.takes ?? []` guarded against an
      // untyped `any` shape, not a real optional value (design doc §3).
      const data = await listTakes({ query: { piece_id: 'chopin_op11', movement: 2 } });
      if (generation === takesRefreshGeneration) {
        takes = data.takes;
        const newest = data.takes.length ? data.takes[data.takes.length - 1] : null;
        if (newest?.status === 'aligned' && !newest.review) {
          void ensureTakeReview(newest);
        }
      }
    } catch (error) {
      console.error('Error refreshing takes:', error);
    }
  }

  async function refreshPassageAnalysis(takeIds: string[], context: string) {
    try {
      const result = await analyzePassageTakes({
        body: { piece_id: 'chopin_op11', movement: 2, take_ids: takeIds },
      });
      if (passageAnalysisContext === context) passageAnalysis = result;
    } catch (error) {
      if (passageAnalysisContext === context) passageAnalysis = null;
      console.error('Error analyzing repeated passage takes:', error);
    }
  }

  async function refreshCoverage() {
    const generation = ++coverageRefreshGeneration;
    try {
      const nextCoverage = await getCoverage({
        path: { movement: 2 },
        query: { piece_id: 'chopin_op11' },
      });
      if (generation === coverageRefreshGeneration) {
        coverageData = nextCoverage;
      }
    } catch (error) {
      console.error('Error fetching coverage:', error);
    }
  }

  async function refreshScoreBundleHealth() {
    try {
      const health = await getScoreBundleHealth({
        path: { movement: 2 },
        query: { piece_id: 'chopin_op11' },
      });
      missingScoreArtifacts = health.missing_artifacts;
    } catch (error) {
      // Read-only diagnostic; a failure here shouldn't block the rest of the
      // Ready-face snapshot from loading.
      console.error('Error checking score bundle health:', error);
    }
  }

  function resolvedCaptureToastText(event: Extract<TakeEvent, { type: 'take:alignment_done' }>) {
    if (event.analysis_state === 'failed') {
      return 'Take kept · placement hit a software error · retry available';
    }
    if (event.analysis_state === 'ambiguous') {
      return 'Take kept · choose between two possible score locations';
    }
    if (event.analysis_state === 'unalignable') {
      return 'Take kept · not enough musical match to place it yet';
    }
    if (event.score_start_beat != null && event.score_end_beat != null) {
      const start = Math.round(event.score_start_beat);
      const end = Math.round(event.score_end_beat);
      return `Take · beat ${start}–${end} · kept ✓`;
    }
    return 'Take · kept ✓';
  }

  function handleTakeEvent(event: TakeEvent) {
    if (event.type === 'take:recording_stopped') {
      if (captureToastTimer) {
        clearTimeout(captureToastTimer);
        captureToastTimer = null;
      }
      captureToast = {
        takeId: event.take_id,
        // `duration_seconds` is a required field on `TakeRecordingStopped`
        // (Zod-validated at the WS boundary, takeEvents.ts) -- no longer a
        // guess (design doc §3).
        text: `Take · ${formatTime(event.duration_seconds)} · placing it in the score…`,
        resolved: false,
      };
      void refreshTakes();
    } else if (event.type === 'take:alignment_done') {
      void refreshTakes();
      if (captureToast && captureToast.takeId === event.take_id) {
        captureToast = { ...captureToast, text: resolvedCaptureToastText(event), resolved: true };
        if (captureToastTimer) clearTimeout(captureToastTimer);
        captureToastTimer = setTimeout(() => {
          captureToast = null;
          captureToastTimer = null;
        }, 3000);
      }
    } else if (event.type === 'take:review_status') {
      void refreshTakes();
      if (event.state === 'failed') {
        setMessage(event.error || 'Could not prepare the orchestral review');
      }
    } else if (event.type === 'hardware:status') {
      hardwareEventGeneration += 1;
      applyHardwareStatus(event.status);
      if (isTerminalHardwarePhase(event.status.phase)) {
        if (
          activePerformanceRecordingId &&
          event.status.session_id === activePerformanceRecordingId
        ) {
          const completedRecordingId = activePerformanceRecordingId;
          resetCaptureState();
          if (event.status.phase === 'completed') {
            void offerPerformanceRecording(completedRecordingId);
          } else if (event.status.phase === 'failed') {
            pendingPerformanceRecordingId = '';
            setMessage('The orchestra could not start. No performance was recorded.', 8000);
          }
        }
        void refreshTerminalArtifacts();
      }
    } else if (
      event.type === 'coverage:materialized' &&
      event.piece_id === 'chopin_op11' &&
      event.movement === 2
    ) {
      void refreshCoverage();
    } else if (event.type === 'runtime:status') {
      runtimeStatus = event.status;
    } else if (event.type === 'runtime:renderer_status') {
      rendererStatus = event.status;
    }
  }

  function applyHardwareStatus(nextStatus: LiveStatusResponse, requestGeneration?: number) {
    if (requestGeneration != null && requestGeneration !== hardwareEventGeneration) return;
    hardwareStatus = nextStatus;
    // Live run IDs own trace directories and ephemeral session MIDI. They are
    // still not the performer's selected durable review session; treating one
    // as that selection caused terminal refreshes to request
    // `/sessions/live-…` and overwrite the performer's selected review slot.
    if (nextStatus.session_id && !nextStatus.kind.startsWith('live_')) {
      sessionId = nextStatus.session_id;
    }
  }

  function isTerminalHardwarePhase(phase: LiveStatusResponse['phase']): boolean {
    return phase === 'idle' || phase === 'completed' || phase === 'failed';
  }

  async function refreshTerminalArtifacts() {
    await Promise.all([refreshSessions(), refreshTakes()]);
    if (sessionId) {
      await fetchStatus();
    }
  }

  function clearLeadInTimer() {
    if (leadInTimer) {
      clearInterval(leadInTimer);
      leadInTimer = null;
    }
  }

  function clearElapsedTimer() {
    if (elapsedTimer) {
      clearInterval(elapsedTimer);
      elapsedTimer = null;
    }
  }

  // `started_at` is a Pydantic `datetime` field end to end (design doc §1.3):
  // the generated client's Zod response validator throws before any of this
  // code ever sees a malformed ISO string, so there is no NaN case left to
  // guard against. The only remaining state is the field being absent (no
  // job has started yet), which resolves to "now" -- a real default, not a
  // corruption fallback.
  function startedAtMsOrNow(iso: string | null | undefined): number {
    return iso ? new Date(iso).getTime() : Date.now();
  }

  // Ticks the lead-in countdown against wall-clock time (design doc §3.3:
  // "your entry in 5 s", derived from the actual -- possibly truncated --
  // cue length the server reported, not the requested one) and flips the
  // display over to the recording state once the entry point is reached.
  function startLeadInCountdown(totalSeconds: number, startedAtMs: number) {
    clearLeadInTimer();
    const tick = () => {
      const remaining = totalSeconds - (Date.now() - startedAtMs) / 1000;
      if (remaining <= 0) {
        leadInSecondsRemaining = 0;
        clearLeadInTimer();
        captureState = 'recording';
        startElapsedTimer();
        return;
      }
      leadInSecondsRemaining = remaining;
    };
    tick();
    leadInTimer = setInterval(tick, 100);
  }

  function startElapsedTimer(startedAtMs: number = Date.now()) {
    clearElapsedTimer();
    const tick = () => {
      // Clamped for the reload-recovery case: if a page reload happens to
      // land before a cued take's musical entry, the computed entry point
      // is still in the future, which would otherwise show negative time.
      elapsedSeconds = Math.max(0, (Date.now() - startedAtMs) / 1000);
    };
    tick();
    elapsedTimer = setInterval(tick, 200);
  }

  function resetCaptureState() {
    clearLeadInTimer();
    clearElapsedTimer();
    captureState = 'idle';
    activePerformanceRecordingId = '';
    captureTargetMeasure = null;
  }

  async function offerPerformanceRecording(recordingId: string) {
    if (!recordingId) return;
    try {
      const blob = (await downloadMidi({
        path: { session_id: recordingId, variant: 'solo' },
      })) as Blob;
      const midi = new Midi(await blob.arrayBuffer());
      const noteCount = midi.tracks.reduce((total, track) => total + track.notes.length, 0);
      if (noteCount === 0) {
        pendingPerformanceRecordingId = '';
        setMessage('Stopped before any piano notes were played. No take was created.', 5000);
        return;
      }
    } catch {
      // Preserve the scratch decision when inspection is unavailable. The
      // recording may still exist, and silently hiding it would risk data loss.
    }
    inspectorOpen = false;
    pendingPerformanceRecordingId = recordingId;
  }

  async function startUncuedTake(target: CoverageMeasure | null) {
    if (!selectedBackendInput) {
      setMessage('No backend MIDI input selected');
      return;
    }
    captureMode = target ? 'from-position' : 'free';
    captureTargetMeasure = target?.measure ?? null;
    captureState = 'starting';
    const recordingId = generatePerformanceId();
    activePerformanceRecordingId = recordingId;
    try {
      const hardwareGeneration = hardwareEventGeneration;
      const duration = Number.parseFloat(hardwareRecordSeconds);
      const status = await startHardwareRecord({
        body: {
          input_name: selectedBackendInput,
          session_id: recordingId,
          ...(target ? { target_score_beat: target.start_beat } : {}),
          ...(Number.isFinite(duration) && duration > 0 ? { duration_seconds: duration } : {}),
        },
      });
      lastReviewedTakeId = null;
      captureState = 'recording';
      startElapsedTimer();
      applyHardwareStatus(status, hardwareGeneration);
      setMessage(
        target
          ? `Recording now. Your take is anchored at measure ${target.measure}.`
          : 'Recording started. Play when ready.',
      );
    } catch (error) {
      setMessage(describeApiError(error, 'Failed to start recording'));
      resetCaptureState();
    }
  }

  async function startFreeTake() {
    await startUncuedTake(null);
  }

  async function startCuedTake(target: CoverageMeasure | null) {
    if (!selectedBackendInput) {
      setMessage('No backend MIDI input selected');
      return;
    }
    if (!selectedBackendOutput) {
      setMessage('No backend MIDI output selected for orchestra cue');
      return;
    }
    const recordingId = generatePerformanceId();
    activePerformanceRecordingId = recordingId;
    lastReviewedTakeId = null;
    captureMode = target ? 'from-position' : 'from-the-top';
    captureTargetMeasure = target?.measure ?? null;
    captureState = 'starting';
    try {
      const hardwareGeneration = hardwareEventGeneration;
      const duration = Number.parseFloat(hardwareRecordSeconds);
      const body = await startHardwareRecordWithCue({
        body: {
          input_name: selectedBackendInput,
          output_name: selectedBackendOutput,
          session_id: recordingId,
          movement: 2,
          ...(target ? { target_score_beat: target.start_beat } : {}),
          volume: orchestraVolume / 100,
          output_advance_ms: yamahaOutputAdvanceMs,
          ...(Number.isFinite(duration) && duration > 0 ? { duration_seconds: duration } : {}),
        },
      });
      applyHardwareStatus(body, hardwareGeneration);
      captureTargetMeasure = body.entry_measure;
      captureState = 'lead-in';
      startLeadInCountdown(body.actual_cue_seconds, startedAtMsOrNow(body.started_at));
      setMessage(
        `Orchestra starts at measure ${body.cue_start_measure}; the live follower takes over as you play.`,
      );
    } catch (error) {
      setMessage(describeApiError(error, 'Failed to start cued recording'));
      resetCaptureState();
    }
  }

  async function startFromTheTop() {
    if (fromTheTopDisabledReason) {
      setMessage(fromTheTopDisabledReason);
      return;
    }
    await startCuedTake(null);
  }

  async function startSelectedPassage() {
    if (!selectedRehearsalMeasure) {
      setMessage('Choose a highlighted measure on the score first.');
      return;
    }
    if (recordPassDisabledReason) {
      setMessage(recordPassDisabledReason);
      return;
    }
    if (orchestraCueEnabled) {
      await startCuedTake(selectedRehearsalMeasure);
    } else {
      await startUncuedTake(selectedRehearsalMeasure);
    }
  }

  async function retryAlignment(take: TakeResponse) {
    retryingAlignmentTakeId = take.take_id;
    try {
      await retryTakeAlignment({
        path: { take_id: take.take_id },
        query: { piece_id: take.piece_id, movement: take.movement },
      });
      setMessage('Retrying score placement. Your recording and selected start are preserved.');
      await refreshTakes();
    } catch (error) {
      setMessage(describeApiError(error, 'Could not retry score placement'));
    } finally {
      retryingAlignmentTakeId = null;
    }
  }

  // A cancelled lead-in contains no pianist performance, so it remains scratch
  // and does not ask the performer for a keep/discard decision.
  async function cancelLeadIn() {
    resetCaptureState();
    try {
      const hardwareGeneration = hardwareEventGeneration;
      applyHardwareStatus(await stopHardwareJob(), hardwareGeneration);
    } finally {
      setMessage('Lead-in cancelled.', 1500);
    }
  }

  async function stopTake() {
    const recordingId =
      activePerformanceRecordingId || hardwareStatus?.session_id || runtimeStatus?.run_id || '';
    if (!recordingId) {
      setMessage('No active recording session found to stop');
      return;
    }
    try {
      const hardwareGeneration = hardwareEventGeneration;
      applyHardwareStatus(await stopHardwareJob(), hardwareGeneration);
      resetCaptureState();
      void offerPerformanceRecording(recordingId);
      setMessage('Performance stopped.', 3000);
    } catch (error) {
      setMessage(describeApiError(error, 'Failed to stop recording'));
    }
  }

  async function playTake(take: TakeResponse) {
    if (!take.midi_url) {
      setMessage('No MIDI file available for this take');
      return;
    }
    playbackLoading = take.take_id;
    try {
      const positionedTake = await takeWithScoreTransport(take);
      const response = await fetch(take.midi_url);
      if (!response.ok) {
        throw new Error('MIDI file not found');
      }
      const buffer = await response.arrayBuffer();
      loadMidiForPlayer(buffer, `${take.take_id}.mid`, positionedTake.take_id);
      await playPlayerMidi();
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      playbackLoading = null;
    }
  }

  async function ensureTakeReview(take: TakeResponse, retry = false) {
    if (reviewRequestingId === take.take_id) return;
    if (!retry && take.review && take.review.state !== 'failed') return;
    reviewRequestingId = take.take_id;
    try {
      const nextReview = await prepareTakeReview({
        path: { take_id: take.take_id },
        body: { piece_id: take.piece_id, movement: 2 },
      });
      takes = takes.map((item) =>
        item.take_id === take.take_id ? { ...item, review: nextReview } : item,
      );
    } catch (error) {
      setMessage(describeApiError(error, 'Could not prepare the orchestral review'));
    } finally {
      reviewRequestingId = null;
    }
  }

  async function hearTakeWithOrchestra(take: TakeResponse) {
    await hearAlignedTake(take, 'ensemble');
  }

  type ReviewVariant = 'solo' | 'ensemble';

  function reviewStartScoreBeat(take: TakeResponse): number | null {
    if (!take.score_span) return null;
    const selectedBeat = selectedRehearsalMeasure?.start_beat;
    if (
      selectedBeat !== undefined &&
      selectedBeat >= take.score_span.start.score_beat &&
      selectedBeat <= take.score_span.end.score_beat
    ) {
      return selectedBeat;
    }
    return take.score_span.start.score_beat;
  }

  async function hearAlignedTake(take: TakeResponse, variant: ReviewVariant) {
    if (take.review?.state !== 'ready') {
      await ensureTakeReview(take, take.review?.state === 'failed');
      return;
    }
    if (!selectedBackendOutput) {
      setMessage('Select the Clavinova output first');
      return;
    }
    try {
      const hardwareGeneration = hardwareEventGeneration;
      applyHardwareStatus(
        await playTakeReview({
          path: { take_id: take.take_id },
          query: { piece_id: take.piece_id, movement: take.movement },
          body: {
            output_name: selectedBackendOutput,
            volume: orchestraVolume / 100,
            variant,
            start_score_beat: reviewStartScoreBeat(take),
          },
        }),
        hardwareGeneration,
      );
      lastReviewedTakeId = take.take_id;
      setMessage(
        variant === 'ensemble'
          ? 'Playing this take with orchestra from the selected passage'
          : 'Playing this take alone from the selected passage',
      );
    } catch (error) {
      setMessage(describeApiError(error, 'Could not play the aligned review'));
    }
  }

  async function previewTakeWithOrchestra(take: TakeResponse) {
    await previewAlignedTake(take, 'ensemble');
  }

  async function previewAlignedTake(take: TakeResponse, variant: ReviewVariant) {
    if (take.review?.state !== 'ready') return;
    playbackLoading = `review-${variant}-${take.take_id}`;
    try {
      const positionedTake = await takeWithScoreTransport(take);
      const blob = (await downloadTakeReviewMidi({
        path: { take_id: take.take_id },
        query: { piece_id: take.piece_id, movement: take.movement, variant },
      })) as Blob;
      loadMidiForPlayer(
        await blob.arrayBuffer(),
        variant === 'ensemble' ? `Take + orchestra` : `Take only`,
        positionedTake.take_id,
      );
      const startBeat = reviewStartScoreBeat(positionedTake);
      if (startBeat !== null) {
        const startElapsed = transportElapsedAtScoreBeat(
          positionedTake.score_transport,
          startBeat,
        );
        if (startElapsed !== null) seekPlayer(startElapsed);
      }
      await playPlayerMidi();
      lastReviewedTakeId = take.take_id;
    } catch (error) {
      setMessage(describeApiError(error, 'Could not preview the aligned review'));
    } finally {
      playbackLoading = null;
    }
  }

  async function takeWithScoreTransport(take: TakeResponse): Promise<TakeResponse> {
    if (take.score_transport) return take;
    const scoreTransport = await getTakeScoreTransport({
      path: { take_id: take.take_id },
      query: { piece_id: take.piece_id, movement: take.movement },
    });
    const positionedTake = { ...take, score_transport: scoreTransport };
    takes = takes.map((item) => (item.take_id === take.take_id ? positionedTake : item));
    return positionedTake;
  }

  async function toggleDiscardTake(take: TakeResponse) {
    try {
      const request = {
        path: { take_id: take.take_id },
        query: { piece_id: take.piece_id, movement: take.movement },
      };
      const updated = take.status === 'discarded'
        ? await restoreTake(request)
        : await deleteTake(request);
      setMessage(updated.status === 'discarded' ? 'Pass excluded from learning' : 'Pass restored');
      await refreshTakes();
    } catch (error) {
      setMessage(describeApiError(error, 'Failed to toggle take status'));
    }
  }

  let resolvingTakeId: string | null = null;
  let reviewRequestingId: string | null = null;
  let lastReviewedTakeId: string | null = null;

  async function resolveTake(take: TakeResponse, candidateIndex: number) {
    if (resolvingTakeId) return;
    resolvingTakeId = take.take_id;
    try {
      const updated = await resolveTakeRequest({
        path: { take_id: take.take_id },
        body: {
          piece_id: take.piece_id,
          movement: take.movement,
          candidate_index: candidateIndex,
        },
      });
      if (updated.status === 'aligned') {
        setMessage('Take placed');
      } else if (updated.status === 'unalignable') {
        setMessage('Take still could not be placed');
      } else {
        // The resolve runs on the background alignment worker (design
        // doc §2.7) -- this response is just the immediate "aligning"
        // status, not the final outcome yet.
        setMessage('Placing take…');
      }
      await refreshTakes();
    } catch (error) {
      setMessage(describeApiError(error, 'Failed to resolve take'));
    } finally {
      resolvingTakeId = null;
    }
  }

  async function discardAmbiguousTake(take: TakeResponse) {
    if (resolvingTakeId) return;
    resolvingTakeId = take.take_id;
    try {
      await toggleDiscardTake(take);
    } finally {
      resolvingTakeId = null;
    }
  }

  // `recorded_at` is a required, Zod-validated `datetime` field on
  // `TakeResponse` (design doc §1.3/§3) -- never absent, never unparseable
  // once a take reaches this component, so the NaN/empty-string fallbacks
  // that used to guess around a possibly-malformed string are gone.
  function formatRecordedTime(isoString: string) {
    const d = new Date(isoString);
    return d.toLocaleTimeString(undefined, {
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    });
  }

  function formatRecordedMoment(isoString: string) {
    const d = new Date(isoString);
    return `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} · ${formatRecordedTime(isoString)}`;
  }

  $: hardwareReady = backendOutputs.length > 0;
  $: liveRuntimeActive =
    runtimeStatus !== null && runtimeStatus.phase !== 'completed' && runtimeStatus.phase !== 'failed';
  // When anything starts sounding, the score comes back to the performer.
  //
  // The controls that start a take live in the inspection panel below the
  // score, so pressing one leaves the page scrolled to the panel -- and the
  // take then begins with the score, the cursor and the beat readout all off
  // screen. The performer is at the piano and cannot chase it. Any transition
  // into a sounding state pulls the score back into view.
  // Any state where notes sound and the cursor moves -- recording, lead-in,
  // live following, review playback. 'starting' counts too: the score should
  // already be on its way back while the start request is in flight, not a beat
  // late once the take is confirmed.
  $: scoreIsSounding =
    captureState === 'starting' ||
    captureState === 'recording' ||
    captureState === 'lead-in' ||
    liveRuntimeActive ||
    isPlaying ||
    hardwareStatus?.running === true;
  // The edge detection lives inside a plain function on purpose. Reading and
  // writing `scoreWasSounding` directly in a reactive block would make that
  // block depend on its own assignment, so it would invalidate itself on every
  // run -- a reactive loop that starves the rest of the graph and freezes the
  // score cursor mid-playback.
  let scoreWasSounding = false;
  $: notifySoundingChanged(scoreIsSounding);

  function notifySoundingChanged(sounding: boolean) {
    if (sounding && !scoreWasSounding) scrollScoreIntoView();
    scoreWasSounding = sounding;
  }

  function scrollScoreIntoView() {
    requestAnimationFrame(() => {
      scoreStageEl?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  }

  $: liveStateWord = runtimeStatus?.phase === 'preparing'
    ? 'Loading'
    : runtimeStatus
      ? runtimeStatus.state_word
      : 'Not live';
  // One word for the masthead instrument. Recording state outranks follower
  // state because it is the thing with consequences for the take.
  $: transportStateWord =
    captureState === 'recording'
      ? 'REC'
      : captureState === 'lead-in'
        ? 'LEAD-IN'
        : liveRuntimeActive
          ? liveStateWord.toUpperCase()
          : hardwareStatus?.running
            ? 'PLAYING'
            : 'IDLE';
  $: ambiguousTakes = takes.filter((t) => t.status === 'ambiguous');
  $: latestTake = takes.length ? takes[takes.length - 1] : null;
  $: latestTakeSpan = latestTake?.score_span ?? null;
  $: selectedRehearsalMeasure =
    coverageData?.measures.find(
      (measure) => measure.measure === selectedRehearsalMeasureNumber,
    ) ?? null;
  $: selectedMeasureGuidance = coverageGuidance(selectedRehearsalMeasure);
  // The selected passage owns one compact recording picker. Include kept
  // aligned passes crossing the measure plus incomplete attempts explicitly
  // started there, so a stopped/empty retry does not disappear into a global
  // list that the performer has to decipher.
  $: passageRecordings = takes.filter((take) => {
    const span = take.score_span;
    const enteredHere =
      selectedRehearsalMeasureNumber !== null &&
      takeEntryMeasure(take, coverageData?.measures ?? []) ===
        selectedRehearsalMeasureNumber;
    const crossesHere =
      take.status === 'aligned' &&
      take.disposition === 'kept' &&
      !!span &&
      !!selectedRehearsalMeasure &&
      selectedRehearsalMeasure.start_beat < span.end.score_beat &&
      selectedRehearsalMeasure.end_beat > span.start.score_beat;
    return enteredHere || crossesHere;
  }).sort(
    (left, right) => Date.parse(right.recorded_at) - Date.parse(left.recorded_at),
  );
  $: alignedPassesChronological = passageRecordings
    .filter((candidate) => candidate.status === 'aligned')
    .slice()
    .sort((left, right) => Date.parse(left.recorded_at) - Date.parse(right.recorded_at));
  $: contextMeasure = scoreContextMenu
    ? coverageData?.measures.find(
        (candidate) => candidate.measure === scoreContextMenu?.measure,
      ) ?? null
    : null;
  $: contextGuidance = scoreContextMenu
    ? coverageGuidance(
        contextMeasure,
        coverageData?.n_target ?? 3,
        coverageData?.quality_target ?? 0.5,
      )
    : null;
  $: contextTake = scoreContextMenu
    ? takeAtMeasure(scoreContextMenu.measure, takes, coverageData)
    : null;
  $: if (!passageRecordings.some((take) => take.take_id === selectedReviewTakeId)) {
    selectedReviewTakeId = passageRecordings.length
      ? passageRecordings[0].take_id
      : '';
  }
  $: selectedReviewTake =
    passageRecordings.find((take) => take.take_id === selectedReviewTakeId) ?? null;
  $: selectedEntryPasses = passageRecordings.filter(
    (take) =>
      take.status === 'aligned' &&
      take.disposition === 'kept' &&
      takeEntryMeasure(take, coverageData?.measures ?? []) === selectedRehearsalMeasureNumber,
  );
  $: {
    const context = `${selectedRehearsalMeasureNumber ?? 'none'}:${selectedEntryPasses
      .map((take) => take.take_id)
      .sort()
      .join(',')}`;
    if (context !== passageAnalysisContext) {
      passageAnalysisContext = context;
      passageAnalysis = null;
      if (selectedEntryPasses.length > 0) {
        void refreshPassageAnalysis(
          selectedEntryPasses.map((take) => take.take_id),
          context,
        );
      }
    }
  }
  $: passageTakeGroups = groupTakesByEntry(takes, coverageData?.measures ?? []);
  $: scoreTakeCounts = takeCountsByMeasure(takes, coverageData?.measures ?? []);
  $: selectedMeasurePassCount = selectedRehearsalMeasureNumber === null
    ? 0
    : Math.max(
        selectedEntryPasses.length,
        scoreTakeCounts[selectedRehearsalMeasureNumber] ?? 0,
      );
  $: if (coverageData) {
    const takeId = latestTake?.take_id ?? 'no-take';
    const context = `${takeId}:${coverageData.revision}:${coverageData.algorithm_revision}`;
    if (suggestionContext !== context) {
      // Coverage can materialize after the take list. Re-run the automatic
      // suggestion when that happens, while preserving an explicit click for
      // the current take. A successfully aligned take advances the target;
      // an incomplete attempt stays at its selected entry so retry is local.
      if (selectedRehearsalMeasureNumber === null || selectedForTakeId !== takeId) {
        const retryEntry =
          latestTake &&
          latestTake.status !== 'aligned' &&
          latestTake.status !== 'discarded'
            ? takeEntryMeasure(latestTake, coverageData.measures)
            : null;
        const suggestion = suggestedNextMeasure(
          coverageData.measures,
          latestTakeSpan?.end.measure_index != null
            ? latestTakeSpan.end.measure_index + 1
            : 0,
        );
        const nextMeasure = retryEntry ?? suggestion?.measure ?? null;
        selectedRehearsalMeasureNumber = nextMeasure;
        rehearsalPlanMeasure = nextMeasure;
        rehearsalPlan = null;
        if (nextMeasure !== null) void refreshRehearsalPlan(nextMeasure);
        liveStartMeasureNumber = null;
        selectedForTakeId = takeId;
      }
      suggestionContext = context;
    }
  }
  $: latestTakeUrl = status?.playback.find((item) => item.variant === 'solo')?.url ?? null;
  $: isHardwareRecording =
    hardwareStatus.kind === 'record' ||
    hardwareStatus.kind === 'record_with_cue' ||
    (hardwareStatus.kind === 'live_follow' && /^t\d{8}T/.test(hardwareStatus.session_id ?? ''));
  // Oguri orchestra-only playback has no take/session owner. Review playback
  // carries its take ID, so it must not relabel the Listen intent as active.
  $: isOrchestraOnlyPlaying =
    hardwareStatus.running && hardwareStatus.kind === 'playback' && !hardwareStatus.session_id;
  $: performanceState = liveStartupPending
    ? 'idle'
    : liveRuntimeActive
    ? 'playing'
    : isHardwareRecording
    ? 'recording'
    : hardwareStatus.kind === 'playback' || hardwareStatus.kind === 'stopping'
      ? 'playing'
      : isPlaying
        ? 'playing'
        : 'idle';
  $: stateLabel = liveStartupPending
    ? 'Loading orchestra'
    : liveRuntimeActive
    ? liveStateWord
    : isHardwareRecording
    ? 'Recording'
    : hardwareStatus.kind === 'playback'
      ? 'Playing'
      : hardwareStatus.kind === 'stopping'
        ? 'Stopping…'
        : isPlaying
          ? 'Previewing'
          : 'Ready';
  $: performerSituation =
    liveRuntimeActive || captureState !== 'idle' || hardwareStatus.running || isPlaying
      ? 'live'
      : latestTake
        ? 'after'
        : 'ready';
  $: selectedInputName = selectedBackendInput || 'Not connected';
  $: selectedOutputName =
    selectedBackendOutput === NO_MIDI_OUTPUT
      ? 'LG soundbar (REAPER)'
      : selectedBackendOutput || 'Not connected';
  $: orchestraUsesLiveVst = selectedBackendOutput === NO_MIDI_OUTPUT;
  $: liveStartupFailure =
    runtimeStatus?.phase === 'failed'
      ? runtimeStatus.message ?? 'The orchestra renderer failed.'
      : hardwareStatus.phase === 'failed' && hardwareStatus.kind === 'live_follow'
      ? hardwareStatus.message.replace(/^Live MIDI job failed:\s*/, '')
      : '';
  $: rendererPreloadPending = ['loading', 'opening_audio'].includes(
    rendererStatus.state ?? 'not_loaded',
  );
  $: liveStartupPending =
    orchestraUsesLiveVst &&
    (goingLive ||
      (liveRuntimeActive &&
        runtimeStatus?.phase === 'preparing' &&
        ['loading', 'opening_audio'].includes(runtimeStatus.orchestra_renderer_state ?? 'loading')));
  $: liveStartupElapsedSeconds = liveStartupStartedAtMs === null
    ? 0
    : Math.max(0, Math.floor((positionClockMs - liveStartupStartedAtMs) / 1000));
  $: orchestraRendererReady =
    orchestraUsesLiveVst
      ? rendererStatus.state === 'ready' ||
        (liveRuntimeActive &&
          runtimeStatus?.phase !== 'preparing' &&
          runtimeStatus?.orchestra_renderer_state === 'ready')
      : !!selectedBackendOutput;
  $: rendererPreloadFailure =
    orchestraUsesLiveVst && ['failed', 'unavailable'].includes(rendererStatus.state ?? 'not_loaded')
      ? rendererStatus.message ?? 'REAPER orchestra is unavailable'
      : '';
  $: orchestraReadinessLabel = liveStartupFailure || rendererPreloadFailure
    ? 'Failed'
    : liveStartupPending || (orchestraUsesLiveVst && rendererPreloadPending)
      ? 'Loading'
      : orchestraRendererReady
        ? 'Ready'
        : 'Not loaded';
  $: orchestraReadinessDetail = liveStartupFailure
    ? `Orchestra did not start: ${liveStartupFailure}. No performance was recorded.`
    : rendererPreloadFailure
      ? rendererPreloadFailure
    : !orchestraUsesLiveVst && selectedBackendOutput
      ? `Keyboard MIDI output is ready · orchestra ${orchestraVolume}%`
    : liveStartupPending
      ? `${runtimeStatus?.message ?? 'Connecting the REAPER orchestra'} · ${liveStartupElapsedSeconds}s`
      : orchestraUsesLiveVst && rendererPreloadPending
        ? rendererStatus.message
      : orchestraRendererReady
        ? `REAPER streaming BBCSO to LG soundbar · ${orchestraVolume}%`
        : preloadBbcsoOnStartup
          ? 'REAPER connection has not started yet'
          : `REAPER will connect after Go live · LG soundbar · ${orchestraVolume}%`;
  // One explanation for every disabled Record control. A greyed-out button with
  // no stated cause was the single most confusing thing in the capture flow:
  // the reason (a dropped MIDI port, or a capture still starting) was invisible.
  $: recordPassDisabledReason = !selectedBackendInput
    ? 'No MIDI input selected — reconnect the piano or pick an input in Setup.'
    : orchestraCueEnabled && !selectedBackendOutput
      ? 'Orchestra cue is on but no MIDI output is selected — pick one, or turn the cue off.'
      : orchestraCueEnabled &&
          selectedRehearsalMeasureNumber !== null &&
          (rehearsalPlanMeasure !== selectedRehearsalMeasureNumber || !rehearsalPlan)
        ? 'Preparing the orchestra and follower plan…'
      : captureState === 'starting'
        ? 'A take is still starting…'
        : captureState === 'recording'
          ? 'A take is already recording — stop it first.'
          : '';

  $: fromTheTopDisabledReason = !selectedBackendInput
    ? 'needs the orchestra input'
    : !selectedBackendOutput
      ? 'needs the orchestra output'
      : !firstEntrancePlan
        ? 'preparing the orchestra and follower plan'
      : '';
  $: activeHardwareScorePosition = hardwareScorePosition(hardwareStatus, positionClockMs);
  $: activeHardwarePreturnPosition = hardwareScorePosition(
    hardwareStatus,
    positionClockMs + 750,
  );
  $: previewPositionTake = previewPositionTakeId
    ? takes.find((take) => take.take_id === previewPositionTakeId) ?? null
    : null;
  $: activePreviewScorePosition =
    isPlaying && previewPositionTake?.score_span && playerDuration > 0
      ? previewPositionTake.score_transport
        ? transportPositionAtElapsed(
            previewPositionTake.score_transport,
            playerPosition,
            coverageData?.measures ?? [],
          )
        : interpolatePosition(
            previewPositionTake.score_span.start,
            previewPositionTake.score_span.end,
            playerPosition / playerDuration,
            coverageData?.measures ?? [],
          )
      : null;
  $: activePreviewPreturnPosition =
    isPlaying && previewPositionTake?.score_span && playerDuration > 0
      ? previewPositionTake.score_transport
        ? transportPositionAtElapsed(
            previewPositionTake.score_transport,
            playerPosition + 0.75,
            coverageData?.measures ?? [],
          )
        : interpolatePosition(
            previewPositionTake.score_span.start,
            previewPositionTake.score_span.end,
            Math.min(1, (playerPosition + 0.75) / playerDuration),
            coverageData?.measures ?? [],
          )
      : null;
  $: activeLiveScorePosition = liveRuntimeActive ? runtimeStatus?.score_position ?? null : null;
  // The follower outranks the transport projection.
  //
  // `activeHardwareScorePosition` is an OPEN-LOOP projection: wall clock since
  // the take started, times a fixed `tempo_scale`. It never looks at what the
  // performer actually played. Preferring it meant that the moment a follower
  // existed, the cursor still ran on a metronome -- so any rubato accumulated
  // as visible drift (Eric, 2026-07-29: "the cursor slowly drifted faster
  // ahead... ahead by at least one beat by m.17"). The follower's estimate is
  // the only position derived from the performer, so when it is live it wins;
  // the projection remains the fallback for playback and plain recording.
  //
  // Cue-recording now runs through the live runtime, so its follower estimate
  // naturally wins. Open-loop hardware transport remains only a fallback for
  // playback and plain recording; there is no take-level authority override.
  // The last position that came from actually following the performer. Held so
  // a momentary gap in live updates cannot fall through to dead reckoning.
  let lastFollowedPosition: ScorePositionResponse | null = null;
  $: if (activeLiveScorePosition) lastFollowedPosition = activeLiveScorePosition;
  $: if (!liveRuntimeActive) lastFollowedPosition = null;

  // Precedence, and why it is not a plain ?? chain any more.
  //
  // activeHardwareScorePosition is dead reckoning: transportPositionAt projects
  // the PLANNED transport from wall-clock elapsed since started_at and never
  // consults the follower. During a run that follows the performer it can only
  // be wrong, and it ticks at 10 Hz, so letting it fill a gap in live updates
  // puts a confident, moving, incorrect cursor on the score -- which reads as
  // the cursor wandering off and snapping back rather than as missing data.
  //
  // While a run is live the followed position is authoritative. If it is
  // momentarily absent the last followed value is held instead, which is honest:
  // a still cursor says "no news", a drifting one asserts something untrue.
  $: displayedScorePosition = liveRuntimeActive
    ? activeLiveScorePosition ?? lastFollowedPosition ?? activePreviewScorePosition
    : activePreviewScorePosition ?? activeHardwareScorePosition;
  // True when the shown position is a dead-reckoned projection rather than a
  // read of the performer. The readout says so, so a drifting cursor is never
  // mistaken for a tracking one.
  $: displayedPositionIsProjected =
    displayedScorePosition !== null &&
    ((displayedScorePosition === activeHardwareScorePosition &&
      activePreviewScorePosition === null) ||
      // Held from an earlier update rather than freshly followed: still not a
      // live read of the performer, and the readout must not claim otherwise.
      (liveRuntimeActive &&
        activeLiveScorePosition === null &&
        displayedScorePosition === lastFollowedPosition));
  $: displayedScoreBeat =
    displayedScorePosition?.score_beat ?? (liveRuntimeActive ? runtimeStatus?.score_beat ?? null : null);
  // Record what is actually drawn. The engine already logs what it believes;
  // without this there is no way to tell a cursor that jumped from a position
  // that moved, which is precisely the report that could not be diagnosed.
  $: cursorSource = activeLiveScorePosition
    ? 'live'
    : activePreviewScorePosition
      ? 'preview'
      : activeHardwareScorePosition
        ? 'hardware'
        : 'none';
  $: recordCursor({
    beat: displayedScoreBeat ?? null,
    measure: displayedScoreMeasure ?? null,
    source: cursorSource as 'live' | 'preview' | 'hardware' | 'none',
    projected: displayedPositionIsProjected,
    page: scoreOverlayPage ?? null,
  });

  let tracedRunId: string | null = null;
  // Follows the run, not the page: a trace that starts on load would mix
  // idle scrubbing into the performance it is meant to explain.
  $: {
    const id = liveRuntimeActive ? runtimeStatus?.run_id ?? null : null;
    if (id !== tracedRunId) {
      if (tracedRunId) stopCursorTrace();
      if (id) startCursorTrace(id);
      tracedRunId = id;
    }
  }

  $: displayedScoreMeasure = displayedScorePosition
    ? displayedScorePosition.measure_index + 1
    : null;
  $: preturnPosition =
    activeHardwarePreturnPosition ?? activePreviewPreturnPosition ?? activeLiveScorePosition;
  $: preturnScoreMeasure =
    preturnPosition?.measure_index != null
      ? preturnPosition.measure_index + 1
      : displayedScoreMeasure;
  $: positionProjectionIsProvisional = hardwareStatus.score_transport
    ? !hardwareStatus.score_transport.canonical_position ||
      hardwareStatus.score_transport.mapping_review_state !== 'reviewed'
    : previewPositionTake?.score_span
      ? !previewPositionTake.score_span.canonical_position ||
        previewPositionTake.score_span.mapping_review_state !== 'reviewed'
      : activeLiveScorePosition
        ? !activeLiveScorePosition.canonical_position ||
          activeLiveScorePosition.mapping_review_state !== 'reviewed'
      : true;
  $: scorePositionContext = captureState === 'lead-in'
    ? 'Orchestra lead-in'
    : captureState === 'recording'
      ? 'Recording'
      : hardwareStatus.running
        ? 'Orchestra playback'
        : isPlaying
          ? 'Browser preview'
          : liveRuntimeActive
            ? liveStateWord
            : 'Current location';
  // Beat is *floored*, never rounded. beat_in_measure runs [0, 4) in 4/4, so
  // 3.999 just before a barline used to render as "beat 5.0" for one frame --
  // a beat that cannot exist -- before snapping back to 1. Flooring reads the
  // beat the performer is actually in.
  $: scoreBeatInMeasure = displayedScorePosition
    ? Math.floor(displayedScorePosition.beat_in_measure) + 1
    : null;
  $: scorePositionLabel = displayedScorePosition
    ? `${scorePositionContext} · measure ${displayedScorePosition.measure_label} · beat ${scoreBeatInMeasure}`
    : displayedScoreBeat !== null
      ? `${scorePositionContext} · reference beat ${displayedScoreBeat.toFixed(1)} · display projection unavailable`
      : captureState === 'recording'
        ? 'Recording · locating your playing after this take'
        : 'Current location will appear when the orchestra, a review, or live following starts';
  // Recovers capture state after a page reload mid-take: `hardwareStatus`
  // comes back from the server as actually recording, but `captureState`
  // (client-only) restarts at 'idle', which would otherwise strand the
  // deck showing "Record Take" while a take is running -- and route Space
  // through the stage's generic stop instead of the take store. Guarded on
  // `captureState === 'idle'` so it never fires mid-session, since our own
  // start functions already set captureState before hardwareStatus settles.
  $: if (
    hardwareStatus.running &&
    captureState === 'idle' &&
    isHardwareRecording
  ) {
    activePerformanceRecordingId =
      hardwareStatus.session_id || activePerformanceRecordingId;
    if (
      (hardwareStatus.kind === 'record_with_cue' || hardwareStatus.kind === 'live_follow') &&
      hardwareStatus.started_at
    ) {
      captureMode = 'from-the-top';
      // The REST recovery snapshot does not include the original start
      // response's semantic cue span. Recover conservatively as recording;
      // the orchestra and score transport remain authoritative server-side.
      captureState = 'recording';
      startElapsedTimer(startedAtMsOrNow(hardwareStatus.started_at));
    } else {
      captureMode = 'free';
      captureState = 'recording';
      startElapsedTimer(startedAtMsOrNow(hardwareStatus.started_at));
    }
  }

  onMount(() => {
    void refreshFreeRegions();
    void refreshNeedsInfo();
    userPreferredInput = localStorage.getItem('rubato-piano-input') ?? '';
    userPreferredOutput = localStorage.getItem('rubato-orchestra-output') ?? '';
    preloadBbcsoOnStartup =
      localStorage.getItem('rubato-preload-bbcso-on-startup') !== 'false';
    void refreshRendererStatus(true);
    const savedVolume = Number.parseInt(
      localStorage.getItem('rubato-orchestra-volume') ?? '',
      10,
    );
    if (Number.isFinite(savedVolume) && savedVolume >= 0 && savedVolume <= 100) {
      orchestraVolume = savedVolume;
      confirmedOrchestraVolume = savedVolume;
    }
    const savedMetronomeVolume = Number.parseInt(
      localStorage.getItem('rubato-metronome-volume') ?? '',
      10,
    );
    if (
      Number.isFinite(savedMetronomeVolume) &&
      savedMetronomeVolume >= 0 &&
      savedMetronomeVolume <= 100
    ) {
      metronomeVolume = savedMetronomeVolume;
    }
    const savedTempo = Number.parseInt(localStorage.getItem('rubato-orchestra-tempo-bpm') ?? '', 10);
    if (Number.isFinite(savedTempo) && savedTempo >= 40 && savedTempo <= 200) {
      orchestraTempoBpm = savedTempo;
      confirmedOrchestraTempoBpm = savedTempo;
      orchestraTempoCustomized = true;
    }
    const savedOutputAdvance = Number.parseInt(
      localStorage.getItem('rubato-yamaha-output-advance-ms') ?? '',
      10,
    );
    if (
      Number.isFinite(savedOutputAdvance) &&
      savedOutputAdvance >= 0 &&
      savedOutputAdvance <= 100
    ) {
      yamahaOutputAdvanceMs = Math.round(savedOutputAdvance / 10) * 10;
      confirmedOutputAdvanceMs = yamahaOutputAdvanceMs;
    }
    localStorage.removeItem('rubato-orchestra-tempo');
    positionClockTimer = setInterval(() => {
      positionClockMs = Date.now();
    }, 100);
    // REST provides the initial/recovery snapshot; lifecycle changes arrive
    // through the socket. The same snapshot runs after every reconnect so a
    // connection gap cannot strand the cockpit on a missed transition.
    disconnectTakeEvents = connectTakeEvents(
      handleTakeEvent,
      () => setMessage('Received a malformed update from the server — some state may be stale.', 5000),
      () => void refreshServerSnapshot(),
    );
  });

  function saveOrchestraTempo() {
    orchestraTempoBpm = Math.max(40, Math.min(200, Math.round(orchestraTempoBpm)));
    orchestraTempoCustomized = true;
    localStorage.setItem('rubato-orchestra-tempo-bpm', String(orchestraTempoBpm));
    liveTempoEditRevision += 1;
    const revision = liveTempoEditRevision;
    const requestedTempoBpm = orchestraTempoBpm;
    const liveRunning =
      (runtimeStatus && ['preparing', 'listening', 'active'].includes(runtimeStatus.phase)) ||
      (hardwareStatus.running && hardwareStatus.kind === 'live_follow');
    if (liveRunning) {
      if (liveTempoTimer) clearTimeout(liveTempoTimer);
      liveTempoTimer = setTimeout(
        () => void sendLiveTempo(requestedTempoBpm, revision),
        120,
      );
    }
  }

  function saveYamahaOutputAdvance() {
    // Snap to 10 ms: calibration is coarse and by-ear, so finer steps only add
    // noise. The value persists for the next run and applies live if one runs.
    yamahaOutputAdvanceMs = Math.max(
      0,
      Math.min(100, Math.round(yamahaOutputAdvanceMs / 10) * 10),
    );
    localStorage.setItem(
      'rubato-yamaha-output-advance-ms',
      String(yamahaOutputAdvanceMs),
    );
    liveOutputAdvanceEditRevision += 1;
    const revision = liveOutputAdvanceEditRevision;
    const requestedAdvance = yamahaOutputAdvanceMs;
    const liveRunning =
      (runtimeStatus && ['preparing', 'listening', 'active'].includes(runtimeStatus.phase)) ||
      (hardwareStatus.running && hardwareStatus.kind === 'live_follow');
    if (liveRunning) {
      if (liveOutputAdvanceTimer) clearTimeout(liveOutputAdvanceTimer);
      liveOutputAdvanceTimer = setTimeout(
        () => void sendLiveOutputAdvance(requestedAdvance, revision),
        80,
      );
    } else {
      confirmedOutputAdvanceMs = yamahaOutputAdvanceMs;
    }
  }

  async function sendLiveOutputAdvance(requestedAdvanceMs: number, revision: number) {
    liveOutputAdvanceTimer = null;
    try {
      const applied = await updateLiveRuntimeOutputAdvance({
        body: { output_advance_ms: requestedAdvanceMs },
      });
      runtimeStatus = applied;
      if (revision === liveOutputAdvanceEditRevision) {
        confirmedOutputAdvanceMs = Math.round(
          applied.orchestra_output_advance_ms ?? requestedAdvanceMs,
        );
      }
    } catch (error) {
      if (revision === liveOutputAdvanceEditRevision) {
        yamahaOutputAdvanceMs = confirmedOutputAdvanceMs;
        localStorage.setItem(
          'rubato-yamaha-output-advance-ms',
          String(confirmedOutputAdvanceMs),
        );
      }
      setMessage(describeApiError(error, 'Could not change the Keyboard output advance'));
    }
  }

  async function runLatencyCalibration() {
    if (!selectedBackendInput || !selectedBackendOutput) {
      setMessage('Select a Keyboard input and output before calibrating latency.');
      return;
    }
    latencyCalibrating = true;
    latencyCalibration = null;
    try {
      const result = await calibrateLiveRuntimeLatency({
        body: {
          input_name: selectedBackendInput,
          output_name: selectedBackendOutput,
          tempo_bpm: 90,
          beats: 16,
          metronome_volume: metronomeVolume / 100,
        },
      });
      latencyCalibration = result;
      if (result.suggested_output_advance_ms == null) {
        setMessage(result.message || 'Latency calibration did not find enough beats.');
      }
    } catch (error) {
      setMessage(describeApiError(error, 'Latency calibration did not complete'));
    } finally {
      latencyCalibrating = false;
    }
  }

  function applyLatencySuggestion() {
    const suggested = latencyCalibration?.suggested_output_advance_ms;
    if (suggested === null || suggested === undefined) {
      return;
    }
    yamahaOutputAdvanceMs = suggested;
    saveYamahaOutputAdvance();
  }

  function updatePerformanceHeight() {
    // Height of the scrolling page viewport (reserve room for the compact
    // heading bar and position readout); width the page is rasterized to fill.
    performanceHeightPx = Math.max(300, window.innerHeight - 132);
    performanceWidthPx = Math.max(320, window.innerWidth - 48);
  }

  function handlePerformanceKeydown(event: KeyboardEvent) {
    // Covers the CSS-overlay fallback when the Fullscreen API was unavailable
    // or denied; native fullscreen Esc is handled via `fullscreenchange`.
    if (event.key === 'Escape' && performanceView && !document.fullscreenElement) {
      exitPerformanceView();
    }
  }

  function handleFullscreenChange() {
    // The user pressed Esc or the browser left fullscreen: keep our state in
    // sync so the layout returns to the rehearsal deck.
    if (performanceView && !document.fullscreenElement) {
      exitPerformanceView();
    }
  }

  async function enterPerformanceView() {
    performanceView = true;
    updatePerformanceHeight();
    window.addEventListener('resize', updatePerformanceHeight);
    window.addEventListener('keydown', handlePerformanceKeydown);
    document.addEventListener('fullscreenchange', handleFullscreenChange);
    await tick();
    // Native fullscreen is best-effort: it hides the browser/OS chrome for a
    // music stand, but the fixed CSS overlay already delivers the view if the
    // request is denied (e.g. not triggered by a user gesture on some browsers).
    if (scoreShellEl && !document.fullscreenElement && scoreShellEl.requestFullscreen) {
      try {
        await scoreShellEl.requestFullscreen();
      } catch {
        // Fixed-overlay fallback remains in effect; nothing else to do.
      }
    }
    updatePerformanceHeight();
  }

  function exitPerformanceView() {
    performanceView = false;
    window.removeEventListener('resize', updatePerformanceHeight);
    window.removeEventListener('keydown', handlePerformanceKeydown);
    document.removeEventListener('fullscreenchange', handleFullscreenChange);
    if (document.fullscreenElement) {
      void document.exitFullscreen().catch(() => {});
    }
  }

  function togglePerformanceView() {
    if (performanceView) {
      exitPerformanceView();
    } else {
      void enterPerformanceView();
    }
  }

  function saveOrchestraVolume() {
    orchestraVolume = Math.max(0, Math.min(100, Math.round(orchestraVolume)));
    localStorage.setItem('rubato-orchestra-volume', String(orchestraVolume));
    liveVolumeEditRevision += 1;
    const revision = liveVolumeEditRevision;
    const requestedVolume = orchestraVolume;
    const liveRunning =
      (runtimeStatus && ['preparing', 'listening', 'active'].includes(runtimeStatus.phase)) ||
      (hardwareStatus.running && hardwareStatus.kind === 'live_follow');
    if (liveRunning) {
      if (liveVolumeTimer) clearTimeout(liveVolumeTimer);
      liveVolumeTimer = setTimeout(
        () => void sendLiveVolume(requestedVolume, revision),
        80,
      );
    }
  }

  function saveMetronomeVolume() {
    metronomeVolume = Math.max(0, Math.min(100, Math.round(metronomeVolume)));
    localStorage.setItem('rubato-metronome-volume', String(metronomeVolume));
  }

  async function sendLiveVolume(requestedVolume: number, revision: number) {
    liveVolumeTimer = null;
    try {
      const applied = await updateLiveRuntimeVolume({
        body: { volume: requestedVolume / 100 },
      });
      runtimeStatus = applied;
      if (revision === liveVolumeEditRevision) {
        confirmedOrchestraVolume = Math.round(
          (applied.orchestra_volume ?? requestedVolume / 100) * 100,
        );
      }
    } catch (error) {
      if (revision === liveVolumeEditRevision) {
        orchestraVolume = confirmedOrchestraVolume;
        localStorage.setItem('rubato-orchestra-volume', String(confirmedOrchestraVolume));
      }
      setMessage(describeApiError(error, 'Could not change the live orchestra volume'));
    }
  }

  async function sendLiveTempo(requestedTempoBpm: number, revision: number) {
    liveTempoTimer = null;
    try {
      const applied = await updateLiveRuntimeTempo({ body: { tempo_bpm: requestedTempoBpm } });
      runtimeStatus = applied;
      if (revision === liveTempoEditRevision) {
        confirmedOrchestraTempoBpm = requestedTempoBpm;
      }
    } catch (error) {
      if (revision === liveTempoEditRevision) {
        orchestraTempoBpm = confirmedOrchestraTempoBpm;
        localStorage.setItem(
          'rubato-orchestra-tempo-bpm',
          String(confirmedOrchestraTempoBpm),
        );
      }
      setMessage(describeApiError(error, 'Could not change the live tempo'));
    }
  }

  onDestroy(() => {
    window.removeEventListener('resize', updatePerformanceHeight);
    window.removeEventListener('keydown', handlePerformanceKeydown);
    document.removeEventListener('fullscreenchange', handleFullscreenChange);
    if (liveTempoTimer) {
      clearTimeout(liveTempoTimer);
      liveTempoTimer = null;
    }
    if (liveVolumeTimer) {
      clearTimeout(liveVolumeTimer);
      liveVolumeTimer = null;
    }
    if (liveOutputAdvanceTimer) {
      clearTimeout(liveOutputAdvanceTimer);
      liveOutputAdvanceTimer = null;
    }
    if (positionClockTimer) {
      clearInterval(positionClockTimer);
      positionClockTimer = null;
    }
    if (captureToastTimer) {
      clearTimeout(captureToastTimer);
      captureToastTimer = null;
    }
    clearLeadInTimer();
    clearElapsedTimer();
    disconnectTakeEvents?.();
  });

  function setMessage(text: string, timeout = 4000) {
    message = text;
    if (messageTimer) {
      clearTimeout(messageTimer);
      messageTimer = null;
    }
    if (timeout > 0) {
      messageTimer = setTimeout(() => {
        if (message === text) {
          message = '';
        }
        messageTimer = null;
      }, timeout);
    }
  }

  async function refreshSessions() {
    try {
      sessions = await listSessions();
    } catch (error) {
      setMessage(describeApiError(error, 'Failed to load sessions'));
    }
  }

  async function fetchStatus() {
    if (!sessionId) {
      status = null;
      return;
    }
    try {
      status = await getSession({ path: { session_id: sessionId } });
    } catch (error) {
      setMessage(describeApiError(error, 'Failed to fetch session status'));
    }
  }

  async function createSession() {
    try {
      const created = await createSessionRequest({
        body: newSessionInput ? { session_id: newSessionInput.trim() } : {},
      });
      sessionId = created.session_id;
      newSessionInput = '';
      await refreshSessions();
      await fetchStatus();
      setMessage(`Ready to use ${created.label ?? created.session_id} for accompaniment`);
    } catch (error) {
      setMessage(describeApiError(error, 'Failed to create MIDI session'));
    }
  }

  function handleSessionChange(event: Event) {
    const target = event.currentTarget as HTMLSelectElement | null;
    if (target) {
      sessionId = target.value;
      void fetchStatus();
    }
  }

  let userPreferredInput = '';
  let userPreferredOutput = '';

  function handleInputSelect(event: Event) {
    const target = event.currentTarget as HTMLSelectElement | null;
    if (target) {
      userPreferredInput = target.value;
      localStorage.setItem('rubato-piano-input', target.value);
    }
  }

  function handleOutputSelect(event: Event) {
    const target = event.currentTarget as HTMLSelectElement | null;
    if (target) {
      userPreferredOutput = target.value;
      localStorage.setItem('rubato-orchestra-output', target.value);
    }
  }

  async function refreshBackendMidiDevices(isManualClick: boolean = false) {
    if (hardwareStatus.running || goingLive || liveRuntimeActive) return;
    try {
      const devices = await midiDevices();
      backendInputs = devices.inputs;
      backendOutputs = devices.outputs;
      midiBackendAvailable = devices.backend_available;
      selectedBackendInput = pickBestPort(backendInputs, selectedBackendInput, userPreferredInput);
      selectedBackendOutput = userPreferredOutput === NO_MIDI_OUTPUT
        ? NO_MIDI_OUTPUT
        : pickBestPort(backendOutputs, selectedBackendOutput, userPreferredOutput);
      if (isManualClick) {
        if (!backendInputs.length && !backendOutputs.length) {
          setMessage('MIDI ports refreshed: no MIDI devices detected.');
        } else {
          const inputDesc = selectedBackendInput ? `input '${selectedBackendInput}'` : 'no inputs';
          const outputDesc = selectedBackendOutput ? `output '${selectedBackendOutput}'` : 'no outputs';
          setMessage(
            `MIDI ports refreshed: ${backendInputs.length} input(s), ${backendOutputs.length} output(s). Selected ${inputDesc}, ${outputDesc}.`,
          );
        }
      }
    } catch (error) {
      // Initial discovery stays quiet for ordinary backend/network absence,
      // but a schema-invalid success response is a contract violation and
      // must remain visible even before the performer clicks Refresh.
      if (isManualClick || (error instanceof Error && error.name === 'ZodError')) {
        setMessage(describeApiError(error, 'Backend could not list MIDI devices'));
      }
    }
  }

  async function refreshRuntimeStatus() {
    try {
      runtimeStatus = await liveRuntimeStatus();
    } catch (error) {
      console.error('Error fetching live runtime status:', error);
    }
  }

  async function refreshRendererStatus(startIfEnabled: boolean = false) {
    try {
      rendererStatus = await orchestraRendererStatus();
      if (
        startIfEnabled &&
        preloadBbcsoOnStartup &&
        !['loading', 'opening_audio', 'ready'].includes(rendererStatus.state ?? 'not_loaded')
      ) {
        rendererStatus = await preloadOrchestraRenderer({
          body: { program_id: localStorage.getItem('rubato-mix-program-id') ?? 'main' },
        });
      }
    } catch (error) {
      console.error('Error checking orchestra renderer status:', error);
    }
  }

  async function retryRendererPreload() {
    if (rendererPreloadBusy) return;
    rendererPreloadBusy = true;
    try {
      rendererStatus = await preloadOrchestraRenderer({
        body: {
          program_id: localStorage.getItem('rubato-mix-program-id') ?? 'main',
          force: true,
        },
      });
    } catch (error) {
      setMessage(describeApiError(error, 'Could not connect the REAPER orchestra'));
    } finally {
      rendererPreloadBusy = false;
    }
  }

  async function saveRendererPreloadSetting() {
    localStorage.setItem(
      'rubato-preload-bbcso-on-startup',
      preloadBbcsoOnStartup ? 'true' : 'false',
    );
    rendererPreloadBusy = true;
    try {
      rendererStatus = preloadBbcsoOnStartup
        ? await preloadOrchestraRenderer({
            body: { program_id: localStorage.getItem('rubato-mix-program-id') ?? 'main' },
          })
        : await unloadOrchestraRenderer();
    } catch (error) {
      setMessage(describeApiError(error, 'Could not change REAPER readiness'));
    } finally {
      rendererPreloadBusy = false;
    }
  }

  async function refreshLivePlan() {
    const generation = ++livePlanRequestGeneration;
    firstEntrancePlan = null;
    try {
      const resolvedPlan = await getLivePerformancePlan({
        query: { bundle_id: 'chopin_op11_movement_2' },
      });
      if (generation !== livePlanRequestGeneration) return;
      livePlan = resolvedPlan;
      if (!orchestraTempoCustomized) {
        orchestraTempoBpm = Math.round(livePlan.initial_tempo_bpm);
        confirmedOrchestraTempoBpm = orchestraTempoBpm;
      }
      const firstEntranceMeasure = livePlan.first_solo_entry?.measure_index != null
        ? livePlan.first_solo_entry.measure_index + 1
        : null;
      const resolvedFirstEntrancePlan = firstEntranceMeasure === null
        ? null
        : await getLivePerformancePlan({
            query: {
              bundle_id: 'chopin_op11_movement_2',
              start_measure: firstEntranceMeasure,
            },
          });
      if (generation !== livePlanRequestGeneration) return;
      firstEntrancePlan = resolvedFirstEntrancePlan;
      if (selectedRehearsalMeasureNumber !== null) {
        void refreshRehearsalPlan(selectedRehearsalMeasureNumber);
      }
    } catch (error) {
      if (generation === livePlanRequestGeneration) {
        livePlan = null;
        firstEntrancePlan = null;
      }
      console.error('Error resolving live performance plan:', error);
    }
  }

  let rehearsalPlanRequestGeneration = 0;

  async function refreshRehearsalPlan(measure: number) {
    const generation = ++rehearsalPlanRequestGeneration;
    rehearsalPlanMeasure = measure;
    rehearsalPlan = null;
    try {
      const plan = await getLivePerformancePlan({
        query: {
          bundle_id: 'chopin_op11_movement_2',
          start_measure: measure,
        },
      });
      if (generation === rehearsalPlanRequestGeneration) {
        rehearsalPlan = plan;
      }
    } catch (error) {
      if (generation === rehearsalPlanRequestGeneration) {
        rehearsalPlan = null;
      }
      console.error(`Error resolving rehearsal plan from measure ${measure}:`, error);
    }
  }

  function rehearsalLeadPlan(plan: LivePerformancePlanResponse): string {
    const start = plan.orchestra_start;
    const follow = plan.follow_start;
    if (!start) return 'Orchestra start unavailable.';
    const startMeasure = start.measure_index + 1;
    if (!follow || follow.score_beat <= start.score_beat + 0.01) {
      return `Orchestra cues from m. ${startMeasure} until you join.`;
    }
    const followMeasure = follow.measure_index + 1;
    const throughMeasure =
      follow.beat_in_measure > 0.01 ? followMeasure : Math.max(startMeasure, followMeasure - 1);
    return throughMeasure > startMeasure
      ? `Orchestra leads m. ${startMeasure}–${throughMeasure}.`
      : `Orchestra leads m. ${startMeasure}.`;
  }

  function rehearsalFollowPlan(plan: LivePerformancePlanResponse): string {
    const follow = plan.follow_start;
    const tempo = Math.round(plan.initial_tempo_bpm);
    const tempoCopy =
      plan.tempo_source === 'performance_profile'
        ? `♩ = ${tempo} learned`
        : `♩ = ${tempo} movement tempo`;
    if (!follow) return `No FOLLOW section after this point · ${tempoCopy}.`;
    const beat = Math.floor(follow.beat_in_measure) + 1;
    const support =
      plan.follow_prior_take_count === 1
        ? '1 prior take'
        : `${plan.follow_prior_take_count} prior takes`;
    return `Follows you from m. ${follow.measure_index + 1}, beat ${beat} · ${support} · ${tempoCopy}.`;
  }

  async function startLiveAtMeasure(startMeasure: number | null) {
    if (!selectedBackendInput || !selectedBackendOutput || goingLive || hardwareStatus.running) return;
    goingLive = true;
    liveStartupStartedAtMs = Date.now();
    const runId = `live-${Date.now()}`;
    activePerformanceRecordingId = runId;
    try {
      if (!livePlan) {
        await refreshLivePlan();
      }
      if (!livePlan) {
        throw new Error('The live performance plan is unavailable.');
      }
      runtimeStatus = await startLiveRuntimeFollow({
        body: {
          bundle_id: 'chopin_op11_movement_2',
          revision: null,
          input_name: selectedBackendInput,
          // Empty string means "no MIDI output" to the backend (orchestra plays
          // through live audio zones only) — same as null, but keeps the
          // generated client's string type.
          output_name:
            selectedBackendOutput === NO_MIDI_OUTPUT ? '' : selectedBackendOutput,
          start_measure: startMeasure,
          config: {
            run_id: runId,
            initial_tempo_bpm: orchestraTempoBpm,
            orchestra_volume: orchestraVolume / 100,
            output_advance_ms: yamahaOutputAdvanceMs,
            // The output picker is exclusive. Yamaha means symbolic MIDI to
            // the piano synth; LG means BBCSO with no doubled Yamaha copy.
            mix_enabled: selectedBackendOutput === NO_MIDI_OUTPUT,
            mix_program_id: localStorage.getItem('rubato-mix-program-id'),
            mix_program_revision: mixProgram?.revision ?? null,
          },
        },
      });
      confirmedOrchestraTempoBpm = orchestraTempoBpm;
      confirmedOrchestraVolume = orchestraVolume;
      setMessage(
        startMeasure !== null
          ? `Orchestra leading from measure ${startMeasure} — join when ready.`
          : livePlan.orchestra_starts_automatically
          ? `Live — orchestra leading from measure ${livePlan.orchestra_start?.measure_label ?? '1'}.`
          : 'Live — listening for your entry.',
      );
    } catch (error) {
      activePerformanceRecordingId = '';
      liveStartupStartedAtMs = null;
      setMessage(describeApiError(error, 'Could not start live FOLLOW'));
    } finally {
      goingLive = false;
    }
  }

  async function goLive() {
    await startLiveAtMeasure(liveStartMeasureNumber);
  }

  async function goLiveFromContextMenu() {
    if (!scoreContextMenu) return;
    const measure = scoreContextMenu.measure;
    selectRehearsalMeasureNumber(measure);
    closeScoreContextMenu();
    await startLiveAtMeasure(measure);
  }

  async function stopLive() {
    if (stoppingLive) return;
    stoppingLive = true;
    const recordingId = activePerformanceRecordingId || runtimeStatus?.run_id || '';
    try {
      runtimeStatus = await stopLiveRuntime();
      void offerPerformanceRecording(runtimeStatus?.run_id || recordingId);
      activePerformanceRecordingId = '';
      liveStartupStartedAtMs = null;
      setMessage('Live performance stopped.', 3000);
    } catch (error) {
      setMessage(describeApiError(error, 'Could not stop live FOLLOW'));
    } finally {
      stoppingLive = false;
    }
  }

  async function playPerformanceRecordingOnYamaha() {
    if (!pendingPerformanceRecordingId) return;
    if (!selectedBackendOutput) {
      setMessage('Select the Keyboard MIDI output first');
      return;
    }
    playbackLoading = `performance-yamaha-${pendingPerformanceRecordingId}`;
    try {
      const hardwareGeneration = hardwareEventGeneration;
      const status = await playSessionMidi({
        path: { session_id: pendingPerformanceRecordingId, variant: 'solo' },
        body: {
          output_name: selectedBackendOutput,
          volume: orchestraVolume / 100,
        },
      });
      applyHardwareStatus(status, hardwareGeneration);
      setMessage('Playing your recorded piano on the Keyboard');
    } catch (error) {
      setMessage(describeApiError(error, 'Could not play the performance on the Keyboard'));
    } finally {
      playbackLoading = null;
    }
  }

  async function previewPerformanceRecordingOnMac() {
    if (!pendingPerformanceRecordingId) return;
    playbackLoading = `performance-mac-${pendingPerformanceRecordingId}`;
    try {
      const blob = (await downloadMidi({
        path: { session_id: pendingPerformanceRecordingId, variant: 'solo' },
      })) as Blob;
      const buffer = await blob.arrayBuffer();
      loadMidiForPlayer(buffer, `${pendingPerformanceRecordingId}.mid`);
      await playPlayerMidi();
    } catch (error) {
      setMessage(describeApiError(error, 'Could not load the performance recording'));
    } finally {
      playbackLoading = null;
    }
  }

  async function keepPerformance() {
    if (!pendingPerformanceRecordingId || keepingPerformanceRecording) return;
    const recordingId = pendingPerformanceRecordingId;
    keepingPerformanceRecording = true;
    try {
      const kept = await keepPerformanceRecording({
        path: { recording_id: recordingId },
        query: { piece_id: 'chopin_op11', movement: 2 },
      });
      pendingPerformanceRecordingId = '';
      selectedReviewTakeId = kept.take_id;
      await refreshTakes();
      setMessage('Performance kept as a rehearsal take.');
    } catch (error) {
      setMessage(describeApiError(error, 'Could not keep the performance recording'));
    } finally {
      keepingPerformanceRecording = false;
    }
  }

  function focusRehearsalWorkflow() {
    document
      .querySelector<HTMLElement>('[data-testid="performer-score-shell"]')
      ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    setMessage('Choose a measure on the score, then use Record pass with or without its orchestra cue.');
  }

  async function refreshHardwareStatus() {
    const generation = hardwareEventGeneration;
    try {
      applyHardwareStatus(await fetchHardwareStatus(), generation);
    } catch (error) {
      console.error('Error refreshing hardware status:', error);
    }
  }

  function refreshServerSnapshot(): Promise<void> {
    // Called only after the socket subscription is open. Every reconnect gets
    // its own reconciliation; per-resource generations below prevent an
    // older response from overwriting a transition received in the meantime.
    return (async () => {
      await refreshHardwareStatus();
      await Promise.all([
        refreshSessions(),
        refreshBackendMidiDevices(),
        refreshTakes(),
        refreshCoverage(),
        refreshRuntimeStatus(),
        refreshRendererStatus(true),
        refreshLivePlan(),
        refreshScoreBundleHealth(),
        refreshMixForDisplay(),
      ]);
      if (sessionId) {
        await fetchStatus();
      }
    })();
  }

  async function playOguriMovement2() {
    if (!selectedBackendOutput) {
      setMessage('No backend MIDI output selected');
      return;
    }
    try {
      const hardwareGeneration = hardwareEventGeneration;
      applyHardwareStatus(await playOguri({
        body: {
          output_name: selectedBackendOutput,
          movement: 2,
          volume: orchestraVolume / 100,
          tempo_bpm: orchestraTempoBpm,
          duration_seconds: OGURI_SOUNDCHECK_SECONDS,
          orchestra_only: true,
        },
      }), hardwareGeneration);
      setMessage(hardwareStatus.message);
    } catch (error) {
      setMessage(describeApiError(error, 'Failed to play orchestra'));
    }
  }

  async function stopHardwarePlayback() {
    try {
      const hardwareGeneration = hardwareEventGeneration;
      applyHardwareStatus(await stopHardwareJob(), hardwareGeneration);
      setMessage(hardwareStatus.message);
    } catch (error) {
      setMessage(describeApiError(error, 'Failed to stop playback'));
    }
  }

  // Turns whatever the generated client throws -- a parsed FastAPI error
  // body (`{ detail: string | ValidationError[] }`), a `ZodError` from a
  // schema-invalid response, or a network `Error` -- into one user-facing
  // string, so every catch block above has exactly one way to report a
  // failure instead of each one inventing its own (design doc §2.2 "fail
  // loudly at the boundary").
  function describeApiError(error: unknown, fallback: string): string {
    if (error instanceof Error && error.name === 'ZodError') {
      return `Unexpected response from the server (${fallback.toLowerCase()}).`;
    }
    if (error && typeof error === 'object' && 'detail' in error) {
      const body = error as { detail?: unknown };
      if (typeof body.detail === 'string') {
        return body.detail;
      }
      if (Array.isArray(body.detail)) {
        return body.detail
          .map((item: { msg?: string } | string) => (typeof item === 'string' ? item : item.msg ?? String(item)))
          .join('; ');
      }
    }
    if (error instanceof Error && error.message) {
      return error.message;
    }
    if (typeof error === 'string' && error) {
      return error;
    }
    return fallback;
  }

  function formatSize(bytes?: number | null) {
    if (!bytes) return '';
    if (bytes < 1000) return `${bytes} B`;
    return `${(bytes / 1000).toFixed(1)} kB`;
  }

  async function uploadFileToSession(file: File) {
    if (!sessionId && !(await ensureSessionSlot(file.name))) return;
    try {
      status = await uploadMidi({ path: { session_id: sessionId }, body: { file } });
      setMessage(`Uploaded ${file.name} to accompanist session`);
    } catch (error) {
      setMessage(describeApiError(error, 'Upload failed'));
    }
  }

  async function handlePipelineUpload(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    if (!input.files || !input.files.length) return;
    const file = input.files[0];
    await uploadFileToSession(file);
    input.value = '';
  }

  function handleLocalFile(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    if (!input.files || !input.files.length) return;
    const file = input.files[0];
    const reader = new FileReader();
    reader.onload = () => {
      if (reader.result instanceof ArrayBuffer) {
        loadMidiForPlayer(reader.result, file.name);
      }
    };
    reader.readAsArrayBuffer(file);
    input.value = '';
  }

  function loadMidiForPlayer(buffer: ArrayBuffer, label: string, takeId: string | null = null) {
    try {
      const midi = new Midi(buffer);
      if (!midi.tracks.length) {
        setMessage('MIDI file has no notes');
        return;
      }
      stopPlayback();
      playerMidi = midi;
      playerBuffer = buffer;
      playerLabel = label;
      previewPositionTakeId = takeId;
      playerDuration = midi.duration;
      playerPosition = 0;
      playbackOffset = 0;
      setMessage(`Loaded ${label}`);
    } catch (error) {
      setMessage(`Failed to load MIDI: ${(error as Error).message}`);
    }
  }

  async function playPlayerMidi() {
    if (!playerMidi) {
      setMessage('Load a MIDI file first');
      return;
    }
    await Tone.start();
    stopPlayback();
    if (!orchestraGain) {
      orchestraGain = new Tone.Gain(orchestraVolume / 100).toDestination();
    }
    const synth = new Tone.PolySynth(Tone.Synth).connect(orchestraGain);
    const startAt = Tone.now() + 0.05;
    playerMidi.tracks.forEach((track) => {
      track.notes.forEach((note) => {
        if (note.time < playbackOffset) {
          return;
        }
        const relativeTime = note.time - playbackOffset;
        synth.triggerAttackRelease(note.name, note.duration, startAt + relativeTime, note.velocity);
      });
    });
    Tone.Transport.scheduleOnce(() => {
      if (loopEnabled) {
        playbackOffset = 0;
        void playPlayerMidi();
      } else {
        stopPlayback();
      }
    }, Math.max(0.1, playerMidi.duration - playbackOffset) + 0.5);
    Tone.Transport.start(startAt, 0);
    currentSynth = synth;
    isPlaying = true;
    scheduleProgress();
  }

  function pausePlayback() {
    if (!isPlaying) return;
    playerPosition = playbackOffset + (Tone.Transport.seconds || 0);
    playbackOffset = playerPosition;
    Tone.Transport.stop();
    Tone.Transport.cancel();
    disposeSynth();
    clearProgress();
    isPlaying = false;
  }

  function stopPlayback() {
    Tone.Transport.stop();
    Tone.Transport.cancel();
    disposeSynth();
    clearProgress();
    isPlaying = false;
    playerPosition = 0;
    playbackOffset = 0;
  }

  function disposeSynth() {
    if (currentSynth) {
      currentSynth.dispose();
      currentSynth = null;
    }
  }

  function scheduleProgress() {
    clearProgress();
    playbackTimer = setInterval(() => {
      const elapsed = Tone.Transport.seconds || 0;
      playerPosition = Math.min(playerDuration, playbackOffset + elapsed);
    }, 100);
  }

  function clearProgress() {
    if (playbackTimer) {
      clearInterval(playbackTimer);
      playbackTimer = null;
    }
  }

  function togglePlayPause() {
    if (isPlaying) {
      pausePlayback();
    } else {
      void playPlayerMidi();
    }
  }

  function seekPlayer(position: number) {
    if (!playerMidi) return;
    playbackOffset = Math.max(0, Math.min(playerDuration, position));
    playerPosition = playbackOffset;
    if (isPlaying) {
      void playPlayerMidi();
    }
  }

  function handleSeek(event: Event) {
    const target = event.currentTarget as HTMLInputElement | null;
    if (!target) return;
    seekPlayer(parseFloat(target.value));
  }

  function formatTime(seconds: number) {
    const mins = Math.floor(seconds / 60)
      .toString()
      .padStart(2, '0');
    const secs = Math.floor(seconds % 60)
      .toString()
      .padStart(2, '0');
    return `${mins}:${secs}`;
  }

  $: if (orchestraGain) {
    orchestraGain.gain.value = orchestraVolume / 100;
  }

  async function silence() {
    const recordingId = mixAuditionActive
      ? ''
      : activePerformanceRecordingId ||
        (liveRuntimeActive ? runtimeStatus?.run_id : '') ||
        (isHardwareRecording ? hardwareStatus.session_id : '') ||
        '';
    stopPlayback();
    resetCaptureState();
    if (liveRuntimeActive) {
      try {
        runtimeStatus = await stopLiveRuntime();
      } catch {
        // Best-effort: the hardware panic/stop calls below still cut sound.
      }
    }
    if (selectedBackendOutput) {
      try {
        const hardwareGeneration = hardwareEventGeneration;
        applyHardwareStatus(
          await hardwarePanic({ body: { output_name: selectedBackendOutput } }),
          hardwareGeneration,
        );
      } catch {
        // Best-effort: still attempt the stop call below.
      }
    }
    try {
      const hardwareGeneration = hardwareEventGeneration;
      applyHardwareStatus(await stopHardwareJob(), hardwareGeneration);
    } catch {
      // Best-effort.
    }
    if (recordingId) void offerPerformanceRecording(recordingId);
    setMessage('Silence — all sound stopped', 3000);
  }

  async function endSession() {
    if (endingSession) return;
    const confirmed = window.confirm(
      "End this Rubato session? The server will stop and you'll need to run the setup command again to restart.",
    );
    if (!confirmed) return;
    endingSession = true;
    try {
      await shutdownServer();
      setMessage('Shutting down — you can close this tab.');
    } catch (error) {
      endingSession = false;
      setMessage(describeApiError(error, 'Could not end the session'));
    }
  }

  async function loadLatestHardwareTake(autoplay: boolean) {
    if (!sessionId) {
      setMessage('No take session selected');
      return;
    }
    hardwareReviewLoading = true;
    try {
      await fetchStatus();
      if (!status?.files.solo) {
        setMessage('No recorded take available yet');
        return;
      }
      await loadVariantIntoPlayer('solo', autoplay);
      if (!autoplay) {
        setMessage('Loaded latest take into the MIDI Player');
      }
    } finally {
      hardwareReviewLoading = false;
    }
  }

  function loadLatestTake() {
    void loadLatestHardwareTake(false);
  }

  function playLatestTake() {
    void loadLatestHardwareTake(true);
  }

  async function renderOfflineAccompaniment(autoplay: boolean) {
    if (!sessionId) {
      setMessage('Select an accompanist session');
      return;
    }
    if (offlineRenderLoading) return;
    offlineRenderLoading = true;
    try {
      const rendered = await renderOfflineSession({
        path: { session_id: sessionId },
        body: { movement: 2 },
      });
      status = {
        session_id: rendered.session_id,
        files: rendered.files,
        playback: rendered.playback,
      };
      setMessage(
        `Rendered accompaniment: ${rendered.metrics.pitch_match_count} matched notes, ` +
          `${rendered.metrics.extra_performance_note_count} extra take notes`,
      );
      if (autoplay) {
        await loadVariantIntoPlayer('accompaniment', true);
      }
    } catch (error) {
      setMessage(describeApiError(error, 'Failed to render accompaniment'));
    } finally {
      offlineRenderLoading = false;
    }
  }

  async function sendLoadedToAccompanist() {
    if (!playerBuffer) {
      setMessage('Load or record a MIDI file first');
      return;
    }
    if (!sessionId && !(await ensureSessionSlot(playerLabel))) return;
    const file = new File([playerBuffer], playerLabel.replace(/\s+/g, '_'), { type: 'audio/midi' });
    await uploadFileToSession(file);
  }

  async function ensureSessionSlot(labelHint: string): Promise<boolean> {
    if (sessionId || creatingSlot) return true;
    creatingSlot = true;
    try {
      const base = sanitizeLabel(labelHint) || 'recording';
      const uniqueId = `${base}-${(crypto.randomUUID?.() || Math.random().toString(36).slice(2)).slice(0, 8)}`;
      const created = await createSessionRequest({ body: { session_id: uniqueId } });
      sessionId = created.session_id;
      await refreshSessions();
      await fetchStatus();
      setMessage(`Created accompanist session ${created.label ?? created.session_id}`);
      return true;
    } catch (error) {
      setMessage(describeApiError(error, 'Failed to create accompanist session'));
      return false;
    } finally {
      creatingSlot = false;
    }
  }

  function sanitizeLabel(label: string): string {
    return label
      .toLowerCase()
      .replace(/[^a-z0-9-_]+/g, '-')
      .replace(/-+/g, '-')
      .replace(/^-|-$/g, '')
      .slice(0, 48);
  }

  async function playVariant(variant: string) {
    await loadVariantIntoPlayer(variant, true);
  }

  async function playVariantOnHardware(variant: string) {
    if (!sessionId) {
      setMessage('Select an accompanist session');
      return;
    }
    if (!selectedBackendOutput) {
      setMessage('No backend MIDI output selected');
      return;
    }
    try {
      const hardwareGeneration = hardwareEventGeneration;
      applyHardwareStatus(await playSessionMidi({
        path: { session_id: sessionId, variant },
        body: {
          output_name: selectedBackendOutput,
          volume: orchestraVolume / 100,
        },
      }), hardwareGeneration);
      setMessage(hardwareStatus.message);
    } catch (error) {
      setMessage(describeApiError(error, 'Failed to play session MIDI'));
    }
  }

  async function loadVariantIntoPlayer(variant: string, autoplay: boolean) {
    if (!sessionId) {
      setMessage('Select an accompanist session');
      return;
    }
    playbackLoading = variant;
    try {
      // The backend documents this route's 200 response as a raw MIDI
      // stream (`response_class=FileResponse`, media_type "audio/midi"),
      // which OpenAPI/hey-api has no JSON schema for -- the generated type
      // is `unknown`, but the generated client's content-type sniffing
      // (design doc §2.2 applies to JSON bodies; binary bodies fall back to
      // `Blob` here) still gives us the right runtime value.
      const blob = (await downloadMidi({ path: { session_id: sessionId, variant } })) as Blob;
      const buffer = await blob.arrayBuffer();
      loadMidiForPlayer(buffer, `${variant}.mid`);
      if (autoplay) {
        await playPlayerMidi();
      }
    } catch (error) {
      setMessage(describeApiError(error, 'Playback file unavailable'));
    } finally {
      playbackLoading = null;
    }
  }

  // Space stops the active workflow, but never guesses an idle intent. The
  // three intent cards are the only idle launch points; Escape is Silence.
  async function primaryAction() {
    if (mixAuthoringMode) {
      if (mixAuditionActive) {
        await stopSelectedMixAudition();
      } else if (mixSelection) {
        await startSelectedMixAudition();
      } else {
        setMessage('Select a score range to audition through the live mix.');
      }
      return;
    }
    // Route through the deck's own capture-state transitions so every
    // performance reaches the same scratch-recording decision point.
    if (captureState === 'recording') {
      await stopTake();
      return;
    }
    if (captureState === 'lead-in') {
      await cancelLeadIn();
      return;
    }
    if (captureState === 'starting') {
      return;
    }
    if (liveRuntimeActive) {
      await stopLive();
      return;
    }
    if (hardwareStatus.running) {
      await stopHardwarePlayback();
      return;
    }
    if (isPlaying) {
      stopPlayback();
      return;
    }
    setMessage('Choose Perform live, Rehearse a passage, or Listen to orchestra only.');
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.repeat) return;
    if (mixAuthoringMode && (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'z') {
      event.preventDefault();
      void undoMixEdit();
      return;
    }
    if (event.key === 'Escape') {
      if (scoreContextMenu) {
        closeScoreContextMenu();
        return;
      }
      void silence();
      return;
    }
    const target = event.target;
    if (target instanceof Element && target.closest('input, select, textarea, button, a, summary, label')) return;
    if (event.code === 'Space') {
      event.preventDefault();
      void primaryAction();
    }
  }

  function handleWindowPointerDown(event: PointerEvent) {
    const target = event.target;
    if (scoreContextMenu && target instanceof Element && !target.closest('.score-context-menu')) {
      closeScoreContextMenu();
    }
    if (mixMenu && target instanceof Element && !target.closest('.mix-context-menu')) {
      mixMenu = null;
    }
    if (layerMenuOpen && target instanceof Element && !target.closest('.layer-menu')) {
      layerMenuOpen = false;
    }
  }
</script>

<svelte:window on:keydown={handleKeydown} on:pointerdown={handleWindowPointerDown} />

<div class="cockpit" class:mix-authoring={mixAuthoringMode} class:data-mode={workspaceMode === 'data'}>
  <header class="masthead">
    <div class="wordmark">
      <p class="wordmark-title">Rubato</p>
      <p class="wordmark-sub">Score workspace</p>
    </div>
    <nav class="workspace-switcher" aria-label="Workspace mode" data-testid="workspace-switcher">
      <button
        type="button"
        class:is-active={workspaceMode === 'data'}
        aria-pressed={workspaceMode === 'data'}
        data-testid="mode-data"
        on:click={() => switchWorkspace('data')}
      ><span aria-hidden="true">◇</span> Data</button>
      <button
        type="button"
        class:is-active={workspaceMode === 'mixing'}
        aria-pressed={workspaceMode === 'mixing'}
        data-testid="mode-mixing"
        disabled={mixBusy || hardwareStatus.running || captureState !== 'idle' || liveRuntimeActive}
        on:click={() => switchWorkspace('mixing')}
      ><span aria-hidden="true">∿</span> Mix</button>
      <button
        type="button"
        class:is-active={workspaceMode === 'perform'}
        aria-pressed={workspaceMode === 'perform'}
        data-testid="mode-perform"
        on:click={() => switchWorkspace('perform')}
      ><span aria-hidden="true">▶</span> Perform</button>
    </nav>
    <!-- The flight instruments. Always present, in a fixed position, legible
         from the piano bench. This is a literal read of the internal beat
         clock: whatever the transport/follower believes, this is it. The
         performer validates the system by ear against this readout, so it must
         never be hidden, never move, and never round the beat up past the bar
         (see `scoreBeatInMeasure`). -->
    <div class="masthead-status" data-testid="transport-readout">
      <span
        class="transport-state"
        class:recording={captureState === 'recording'}
        class:armed={captureState === 'lead-in'}
        data-testid="transport-state"
      >
        {#if captureState === 'recording' || captureState === 'lead-in'}
          <span class="rec-dot pulsing" aria-hidden="true"></span>
        {/if}
        {transportStateWord}
      </span>

      <span class="transport-clock" data-testid="transport-clock">
        {#if displayedScoreMeasure !== null}
          <span class="clock-field">
            <small>m.</small><strong data-testid="clock-measure">{displayedScoreMeasure}</strong>
          </span>
          <span class="clock-field">
            <small>beat</small><strong data-testid="clock-beat">{scoreBeatInMeasure ?? '–'}</strong>
          </span>
        {:else}
          <span class="clock-idle">—</span>
        {/if}
      </span>

      {#if captureState === 'recording' || captureState === 'lead-in'}
        <span class="transport-elapsed">{formatTime(elapsedSeconds)}</span>
      {/if}

      {#if displayedPositionIsProjected && displayedScoreMeasure !== null}
        <!-- Dead reckoning, not tracking. Say so rather than let a drifting
             cursor pass for a following one. -->
        <span class="transport-flag" title="Position is projected from the cue clock, not read from your playing.">
          projected
        </span>
      {/if}

      <span
        class="device-dot"
        class:connected={!!selectedBackendInput && !!selectedBackendOutput}
        title="In · {selectedInputName} / Out · {selectedOutputName}"
        aria-label="MIDI: in {selectedInputName}, out {selectedOutputName}"
      ></span>
    </div>
    {#if workspaceMode === 'mixing'}
      <div class="mix-masthead-identity" data-testid="mix-mode-identity">
        <strong>{mixProgram?.name ?? 'Spatial mix'}</strong>
        <span>r{mixProgram?.revision ?? '—'}</span>
      </div>
    {/if}
    <div class="layer-menu">
      <button
        type="button"
        class="inspector-toggle layer-toggle"
        data-testid="layer-toggle"
        aria-expanded={layerMenuOpen}
        on:click={() => (layerMenuOpen = !layerMenuOpen)}
      ><span aria-hidden="true">◫</span> Layers</button>
      {#if layerMenuOpen}
        <div class="layer-popover" role="dialog" aria-label="Score layers" on:pointerdown|stopPropagation>
          <strong>Score layers</strong>
          <label>
            <input
              type="checkbox"
              checked={overlayVisibility[workspaceMode].data}
              on:change={(event) => setOverlayVisibility('data', event)}
            />
            <span>Tracking data<small>coverage and take evidence</small></span>
          </label>
          <label>
            <input
              type="checkbox"
              checked={overlayVisibility[workspaceMode].mix}
              on:change={(event) => setOverlayVisibility('mix', event)}
            />
            <span>Mix cues<small>automation brackets and endpoints</small></span>
          </label>
          <small class="layer-mode-note">Saved for {workspaceMode === 'mixing' ? 'Mix' : workspaceMode === 'data' ? 'Data' : 'Perform'} view</small>
        </div>
      {/if}
    </div>
    {#if workspaceMode !== 'mixing'}
      <button
        type="button"
        class="inspector-toggle"
        data-testid="inspector-toggle"
        aria-expanded={inspectorOpen}
        title={workspaceMode === 'data'
          ? 'Show listening levels'
          : 'Set orchestra volume and audio routing'}
        on:click={() => (inspectorOpen = !inspectorOpen)}
      >
        {inspectorOpen ? 'Close sound' : `Sound · ${orchestraVolume}%`}
      </button>
    {/if}

    {#if captureState === 'recording' || captureState === 'lead-in'}
      <!-- While a take is running, the one control the performer needs is
           "stop this take" -- and they are at the piano with both hands busy,
           watching a cursor that moves through measures. It cannot live in a
           side panel or behind a right-click on a target that keeps changing.
           So the masthead's primary action becomes Stop Take for the duration,
           and Silence steps aside to a secondary control. -->
      <button
        type="button"
        class="stop-take-btn"
        on:click={captureState === 'lead-in' ? cancelLeadIn : stopTake}
        aria-label={captureState === 'lead-in'
          ? 'Stop the lead-in before the take begins. Shortcut: space.'
          : 'Stop this performance and choose what happens to its recording. Shortcut: space.'}
      >
        <span class="rec-dot pulsing" aria-hidden="true"></span>
        <span class="stop-take-copy">
          <strong>{captureState === 'lead-in' ? 'Cancel lead-in' : 'Stop take'}</strong>
          <small>
            {captureState === 'lead-in' ? 'before it starts' : formatTime(elapsedSeconds)} · space
          </small>
        </span>
      </button>
      <button
        type="button"
        class="silence-btn silence-btn-secondary"
        on:click={silence}
        aria-label="Silence. Stop all sound immediately."
      >
        <span class="silence-icon" aria-hidden="true">⏻</span>
      </button>
    {:else}
      <button
        type="button"
        class="silence-btn"
        on:click={silence}
        aria-label="Silence. Stop all sound and recording immediately."
      >
        <span class="silence-icon" aria-hidden="true">⏻</span>
        <span class="silence-label">Silence</span>
      </button>
    {/if}
  </header>

  {#if pendingPerformanceRecordingId && !liveRuntimeActive && captureState === 'idle'}
    <div
      class="live-recording-card performance-decision-card"
      data-testid="after-performance-recording"
      aria-live="assertive"
    >
      <div>
        <span>Performance recorded automatically</span>
        <strong>What would you like to do with it?</strong>
        <small>{pendingPerformanceRecordingId} · scratch recording until you keep it</small>
      </div>
      <div class="live-recording-actions">
        <button
          type="button"
          class="btn btn-primary"
          on:click={playPerformanceRecordingOnYamaha}
          disabled={playbackLoading === `performance-yamaha-${pendingPerformanceRecordingId}`}
        >
          {playbackLoading === `performance-yamaha-${pendingPerformanceRecordingId}`
            ? 'Loading…'
            : '▶ Hear piano on Keyboard'}
        </button>
        <button
          type="button"
          class="btn btn-outline"
          on:click={previewPerformanceRecordingOnMac}
          disabled={playbackLoading === `performance-mac-${pendingPerformanceRecordingId}`}
        >
          {playbackLoading === `performance-mac-${pendingPerformanceRecordingId}`
            ? 'Loading…'
            : 'Preview on Mac'}
        </button>
        <button
          type="button"
          class="btn btn-outline"
          on:click={keepPerformance}
          disabled={keepingPerformanceRecording}
        >
          {keepingPerformanceRecording ? 'Keeping…' : 'Keep for piano + orchestra review'}
        </button>
        <button
          type="button"
          class="btn btn-ghost"
          on:click={() => (pendingPerformanceRecordingId = '')}
        >Nothing for now</button>
      </div>
    </div>
  {/if}

  {#if missingScoreArtifacts.length}
    <p class="bundle-health-hint">
      {missingScoreArtifacts.length}
      {missingScoreArtifacts.length === 1 ? 'score artifact is' : 'score artifacts are'}
      still setting up, so some rehearsal-review detail may be missing — but you can
      still go live and record. The orchestra follows your playing regardless.
    </p>
  {/if}

  <main class="hall" class:data-split={workspaceMode === 'data'}>
    <!-- The score is the instrument. It gets the full width and the top of
         the page; everything else is inspection and lives behind a toggle.
         Rationale in issue #153: rehearsal, performing and listening are the
         same act -- play from a measure -- so the surface that matters is the
         one the performer actually reads. -->
    <section
      class="deck score-coordination"
      class:performance-fullscreen={performanceView}
      aria-labelledby="score-coverage-heading"
      data-testid="performer-score-shell"
      bind:this={scoreShellEl}
    >
      <div class="score-heading-row">
        <div>
          <!-- The score does not need to introduce itself. The kicker, the
               title and the paragraph explaining what a score is were three
               lines of prose above the one thing the performer came here to
               read. The heading stays for assistive tech only. -->
          <h2 id="score-coverage-heading" class="sr-only">Rehearsal score · II. Larghetto</h2>
        </div>
        <div class="score-heading-actions">
          {#if coverageData && workspaceMode === 'data' && dataFacet !== 'timing' && !performanceView}
            <div class="score-coverage-stat" aria-label="Rehearsal coverage">
              <strong data-testid="coverage-percent">
                {(coverageData.summary.percent_observed ?? coverageData.summary.percent_covered).toFixed(1)}%
              </strong>
              <span>rehearsed</span>
            </div>
          {/if}
          {#if workspaceMode === 'perform' && performanceView}
            <button
              type="button"
              class="btn btn-ghost performance-toggle"
              on:click={exitPerformanceView}
            >
              ↙ Exit full screen
            </button>
          {/if}
        </div>
      </div>

      {#if workspaceMode === 'perform' && !performanceView}
        <nav class="perform-action-rail" aria-label="Performance actions" data-testid="perform-action-rail">
          <button
            type="button"
            class="action-chip action-chip-primary"
            class:is-active={liveRuntimeActive}
            data-testid="perform-live"
            on:click={liveRuntimeActive ? stopLive : goLive}
            disabled={liveRuntimeActive
              ? stoppingLive
              : !selectedBackendInput ||
                !selectedBackendOutput ||
                goingLive ||
                hardwareStatus.running ||
                (orchestraUsesLiveVst && preloadBbcsoOnStartup && !orchestraRendererReady)}
          ><span aria-hidden="true">{liveStartupPending ? '×' : liveRuntimeActive ? '■' : '▶'}</span> {liveStartupPending ? 'Cancel loading' : liveRuntimeActive ? 'Stop live' : 'Go live'}</button>
          <button
            type="button"
            class="action-chip"
            on:click={() => selectedRehearsalMeasure ? startSelectedPassage() : startFreeTake()}
            disabled={!selectedBackendInput || (!!selectedRehearsalMeasure && orchestraCueEnabled && !selectedBackendOutput) || hardwareStatus.running}
          ><span aria-hidden="true">●</span> {selectedRehearsalMeasure ? `Record m. ${selectedRehearsalMeasure.measure}` : 'Record passage'}</button>
          <span class="action-rail-status">{selectedRehearsalMeasure ? `Selected m. ${selectedRehearsalMeasure.measure}` : 'Select a measure or range on the score'} · ♩ = {orchestraTempoBpm}</span>
        </nav>
        <div
          class="orchestra-readiness"
          class:is-loading={liveStartupPending || rendererPreloadPending}
          class:is-ready={orchestraRendererReady}
          class:is-error={!!liveStartupFailure || !!rendererPreloadFailure}
          role={liveStartupFailure || rendererPreloadFailure ? 'alert' : 'status'}
          aria-live="polite"
          data-testid="orchestra-readiness"
        >
          <span class="readiness-dot" aria-hidden="true"></span>
          <strong>Orchestra {orchestraReadinessLabel}</strong>
          <span>{orchestraReadinessDetail}</span>
          {#if (liveStartupPending || rendererPreloadPending) &&
            ((runtimeStatus?.orchestra_renderer_total_instruments ?? 0) > 0 ||
              (rendererStatus.total_instruments ?? 0) > 0)}
            <progress
              aria-label="REAPER orchestra tracks ready"
              value={liveStartupPending
                ? runtimeStatus?.orchestra_renderer_loaded_instruments ?? 0
                : rendererStatus.loaded_instruments ?? 0}
              max={liveStartupPending
                ? runtimeStatus?.orchestra_renderer_total_instruments ?? 1
                : rendererStatus.total_instruments ?? 1}
            ></progress>
          {/if}
          {#if rendererPreloadFailure && !liveRuntimeActive}
            <button
              type="button"
              class="readiness-retry"
              disabled={rendererPreloadBusy}
              on:click={retryRendererPreload}
            >Retry</button>
          {/if}
        </div>
      {/if}

      <div class="score-stage" bind:this={scoreStageEl}>
        {#if workspaceMode === 'perform' && !performanceView}
          <button
            type="button"
            class="score-fullscreen-btn"
            data-testid="performance-view-toggle"
            aria-label="Full screen score"
            title="Full screen score"
            on:click={enterPerformanceView}
          >
            <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none"
              stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M8 3H5a2 2 0 0 0-2 2v3" />
              <path d="M16 3h3a2 2 0 0 1 2 2v3" />
              <path d="M8 21H5a2 2 0 0 1-2-2v-3" />
              <path d="M16 21h3a2 2 0 0 0 2-2v-3" />
            </svg>
          </button>
        {/if}
        {#if mixAuthoringMode && mixProgram}
          {#if mixScoreIsStale()}
            <section class="mix-score-stale" role="alert" data-testid="mix-score-stale">
              <div>
                <strong>Score changed since this mix was authored</strong>
                <span>Regions remain readable. Review their score placement before rebinding or editing.</span>
              </div>
              <button
                type="button"
                class="btn btn-outline"
                disabled={mixBusy}
                on:click={rebindCurrentMixProgram}
              >Rebind after review</button>
            </section>
          {/if}
          <section class="mixing-toolbar" data-testid="mixing-toolbar">
            <span class="mixing-notation-key"><span class="notation-line" aria-hidden="true"></span> effect span · ▼ control point</span>
            <button
              type="button"
              class="btn btn-ghost mix-undo"
              on:click={undoMixEdit}
              disabled={mixBusy || mixScoreIsStale() || mixProgram.undo_parent_revision == null}
            >Undo</button>
          </section>
          <div class="mix-score-instruction" role="status">
            <span>{mixSelectionAnchor ? `Start set at m. ${mixSelectionAnchor.measure} · right-click an ending beat` : 'Right-click a beat to place a start or end point, or drag a range.'}</span>
            {#if mixSelection}
              <strong>m. {mixSelection.startMeasure}{mixSelection.endMeasure !== mixSelection.startMeasure ? `–${mixSelection.endMeasure}` : ''}</strong>
              {#if mixAuditionActive}
                <button
                  type="button"
                  class="btn btn-primary"
                  disabled={mixAuditionBusy}
                  on:click={stopSelectedMixAudition}
                >{mixAuditionBusy ? 'Stopping…' : '■ Stop live audition'}</button>
              {:else}
                <button
                  type="button"
                  class="btn btn-primary"
                  disabled={mixScoreIsStale()}
                  on:click={openMixEditorForSelection}
                >Create mix region</button>
                <button
                  type="button"
                  class="btn btn-outline"
                  disabled={mixAuditionBusy || mixScoreIsStale() || !selectedBackendOutput || !liveVstZoneReady()}
                  on:click={startSelectedMixAudition}
                >{mixAuditionBusy ? 'Starting…' : '▶ Audition live'}</button>
              {/if}
            {/if}
            {#if !liveVstZoneReady()}
              <span class="mix-zone-warning">Live orchestra zone needs configuration and calibration.</span>
            {/if}
          </div>
        {/if}
        <PdfCoverageOverlay
          bind:this={scoreOverlayEl}
          pieceId="chopin_op11"
          movement={2}
          coverage={coverageData}
          currentMeasure={displayedScoreMeasure}
          preturnMeasure={preturnScoreMeasure}
          currentScoreBeat={displayedScoreBeat}
          positionLabel={scorePositionLabel}
          latestTakeStartMeasure={latestTakeSpan ? latestTakeSpan.start.measure_index + 1 : null}
          latestTakeEndMeasure={latestTakeSpan ? latestTakeSpan.end.measure_index + 1 : null}
          latestTakeStartScoreBeat={latestTakeSpan?.start.score_beat ?? null}
          latestTakeEndScoreBeat={latestTakeSpan?.end.score_beat ?? null}
          selectedMeasure={selectedRehearsalMeasureNumber}
          takeCountsByMeasure={scoreTakeCounts}
          {needsInfo}
          bind:currentPage={scoreOverlayPage}
          performanceMode={performanceView}
          mixAuthoringMode={mixAuthoringMode}
          dataAnnotationMode={workspaceMode === 'data' && dataFacet === 'integrity'}
          timingReviewMode={workspaceMode === 'data' && dataFacet === 'timing'}
          timingReview={timingReviewByMeasure}
          showDataOverlay={overlayVisibility[workspaceMode].data}
          showMixPlan={overlayVisibility[workspaceMode].mix}
          mixRegions={mixProgram?.regions ?? []}
          mixDraftStartTick={mixSelectionAnchor?.tick ?? null}
          mixSelectionStartTick={mixSelection?.startTick ?? null}
          mixSelectionEndTick={mixSelection?.endTick ?? null}
          fitHeightPx={performanceView ? performanceHeightPx : null}
          fitWidthPx={performanceView ? performanceWidthPx : null}
          scrollContainer={workspaceMode === 'data' ? scoreShellEl : null}
          on:selectMeasure={selectRehearsalMeasure}
          on:contextMeasure={openScoreContextMenu}
          on:anchorChanged={handleAnchorChanged}
          on:anchorChangeFailed={handleAnchorChangeFailed}
          on:mixSelection={handleMixSelection}
          on:mixContext={handleMixContext}
        />
        {#if anchorNotice}
          <div
            class:anchor-notice-error={anchorNotice.error}
            class="anchor-notice"
            role="status"
            data-testid="anchor-notice"
            style={`left: ${anchorNotice.x}px; top: ${anchorNotice.y}px;`}
            on:pointerdown|stopPropagation
          >
            <span>{anchorNotice.text}</span>
            {#if anchorNotice.undo}
              <button type="button" on:click={undoAnchorChange}>Undo</button>
            {/if}
            <button
              type="button"
              class="anchor-notice-close"
              aria-label="Dismiss anchor message"
              on:click={() => (anchorNotice = null)}
            >×</button>
          </div>
        {/if}

      <!-- Gated on the menu alone. contextGuidance describes a single beat, so it
           is null for a dragged or shift-clicked range -- requiring it here meant
           a span selection silently opened nothing. -->
      {#if scoreContextMenu && !mixAuthoringMode}
        <div
          bind:this={scoreContextMenuEl}
          class="score-context-menu"
          data-testid="score-context-menu"
          role="menu"
          tabindex="-1"
          aria-label={`Actions for measure ${scoreContextMenu.measure}`}
          style={`left: ${scoreContextMenu.x}px; top: ${scoreContextMenu.y}px; max-height: ${scoreContextMenu.maxHeight}px;`}
          on:pointerdown|stopPropagation
          on:contextmenu|preventDefault
        >
          {#if workspaceMode === 'data' && dataFacet === 'timing'}
            <div class="score-context-heading timing-context-heading">
              <strong>m.{scoreContextMenu.measure}</strong>
            </div>
            <button
              type="button"
              role="menuitem"
              class="btn btn-primary timing-context-play"
              on:click={playTimingMeasureFromMenu}
              disabled={!selectedBackendOutput}
            >▶ Play</button>
          {:else}
          <div class="score-context-heading">
            <!-- The heading names the span, not a beat: a dragged range has no
                 single beat, and asking for one produced "beat -435". -->
            <span>
              {describeSpan(
                scoreContextMenu.span,
                scoreContextMenu.span.kind === 'point'
                  ? scoreBeatLabel(scoreContextMenu.measure, scoreContextMenu.scoreBeat)
                  : undefined,
              )}
            </span>
            {#if workspaceMode === 'data' && scoreContextMenu.span.kind === 'point' && contextGuidance}
              <strong>{contextGuidance.label}</strong>
            {/if}
          </div>
          {#if workspaceMode === 'data' && contextGuidance}
            <p>{contextGuidance.explanation}</p>
            <small>{contextGuidance.action}</small>
          {/if}
          <!-- One list, built from the assertion catalogue. Adding a workflow
               adds an entry there and a case in applyAssertion; it does not add
               a section here, which is how this menu kept growing per feature. -->
          {#if workspaceMode === 'data'}
            <div class="score-context-assertions">
              {#each contextAssertions as assertion (assertion.id)}
                <button
                  type="button"
                  role="menuitem"
                  data-testid={`menu-assertion-${assertion.id}`}
                  class="btn assertion"
                  class:btn-primary={!assertion.destructive && assertion.group === 'position'}
                  class:btn-ghost={assertion.destructive || assertion.group !== 'position'}
                  class:assertion-destructive={assertion.destructive}
                  on:click={() => applyAssertion(assertion.id)}
                >
                  <span class="assertion-label">{assertion.label(contextAssertionState)}</span>
                  {#if assertion.hint}
                    <small class="assertion-hint">{assertion.hint}</small>
                  {/if}
                </button>
              {/each}
            </div>
          {/if}
          {#if workspaceMode === 'perform'}
          <div class="score-context-assertions">
            {#each contextAssertions.filter((assertion) => assertion.id === 'live.start') as assertion (assertion.id)}
              <button
                type="button"
                role="menuitem"
                data-testid={`menu-assertion-${assertion.id}`}
                class="btn btn-primary assertion"
                on:click={() => applyAssertion(assertion.id)}
              >
                <span class="assertion-label">{assertion.label(contextAssertionState)}</span>
                {#if assertion.hint}
                  <small class="assertion-hint">{assertion.hint}</small>
                {/if}
              </button>
            {/each}
          </div>
          <div class="score-context-subheading passage-actions-heading">
            <strong>Passage actions</strong>
          </div>
          <div class="score-context-actions">
            <button
              type="button"
              role="menuitem"
              class="btn btn-primary"
              on:click={recordContextMeasureFromMenu}
              disabled={!!recordPassDisabledReason}
              title={recordPassDisabledReason ||
                'The orchestra starts on this measure. Come in whenever you are ready.'}
              aria-describedby={recordPassDisabledReason ? 'record-pass-blocked' : undefined}
            >● Start orchestra here &amp; record</button>
            {#if recordPassDisabledReason}
              <p id="record-pass-blocked" class="hint">{recordPassDisabledReason}</p>
            {:else if rehearsalPlan}
              <p class="hint" data-testid="context-recording-plan">
                {rehearsalLeadPlan(rehearsalPlan)}
                {rehearsalFollowPlan(rehearsalPlan)}
                Stop from the top bar or press space.
              </p>
            {:else}
              <p class="hint">Preparing the orchestra and follower plan…</p>
            {/if}
            {#if contextTake?.review?.state === 'ready'}
              <button
                type="button"
                role="menuitem"
                class="btn btn-ghost"
                on:click={() => playContextMeasureFromMenu('solo')}
                disabled={!selectedBackendOutput || hardwareStatus.running}
              >▶ Hear latest take</button>
              <button
                type="button"
                role="menuitem"
                class="btn btn-ghost"
                on:click={() => playContextMeasureFromMenu('ensemble')}
                disabled={!selectedBackendOutput || hardwareStatus.running}
              >▶ Hear take + orchestra</button>
            {/if}
            <button
              type="button"
              role="menuitem"
              class="btn btn-ghost"
              on:click={reviewContextMeasureFromMenu}
            >Open passage details</button>
          </div>
          {/if}
          {/if}
        </div>
      {/if}
      {#if mixMenu && mixProgram}
        <button
          type="button"
          class="mix-modal-scrim"
          aria-label="Close mixing controls"
          on:click={() => (mixMenu = null)}
        ></button>
        <div
          class="score-context-menu mix-context-menu"
          class:mix-point-menu={mixMenu.kind === 'point'}
          data-testid="mix-context-menu"
          role="dialog"
          aria-label={mixMenu.kind === 'point' ? 'Attach a mix control point' : mixMenu.regionId ? 'Edit mix region' : 'Create mix region'}
          style={`left: ${mixMenu.x}px; top: ${mixMenu.y}px;`}
          on:pointerdown|stopPropagation
          on:contextmenu|preventDefault
        >
          {#if mixMenu.kind === 'point'}
            <div class="score-context-heading">
              <span>Attach at this beat</span>
              <strong>m. {mixMenu.measure}</strong>
            </div>
            <div class="mix-point-actions">
              <button type="button" class="btn btn-primary" on:click={setMixControlStart}>
                <span aria-hidden="true">▽</span> Start effect here
              </button>
              <button type="button" class="btn btn-outline" on:click={setMixControlEnd} disabled={!mixSelectionAnchor}>
                <span aria-hidden="true">▼</span> End effect here
              </button>
            </div>
            <small>{mixSelectionAnchor ? `Start is at m. ${mixSelectionAnchor.measure}. Ending here opens the effect controls.` : 'Choose a start, then right-click the ending beat.'}</small>
          {:else}
            <div class="score-context-heading">
              <span>{mixMenu.regionId ? 'Edit automation' : 'New automation'}</span>
              <strong>{mixSelection ? `m. ${mixSelection.startMeasure}${mixSelection.endMeasure !== mixSelection.startMeasure ? `–${mixSelection.endMeasure}` : ''}` : ''}</strong>
            </div>
            <div class="mix-menu-grid">
              <label>
                Gesture
                <select value={mixGesture} on:change={applyMixGesturePreset} aria-label="Mix gesture">
                  <option value="swell">Swell</option>
                  <option value="fade">Fade</option>
                  <option value="bed">Bed</option>
                  <option value="feature">Feature</option>
                  <option value="custom">Custom</option>
                </select>
              </label>
              <label>
                Output
                <select bind:value={mixZoneId} aria-label="Mix output zone">
                  {#each mixZones as zone (zone.zone_id)}
                    <option value={zone.zone_id}>{zone.label} · {zone.health.replace(/_/g, ' ')}</option>
                  {/each}
                </select>
              </label>
              <label class="mix-menu-stem">
                Part
                <input
                  type="text"
                  bind:value={mixStemId}
                  list="mix-stem-options"
                  aria-label="Mix stem or score part"
                />
                <datalist id="mix-stem-options">
                  <option value="orchestra">Whole orchestra</option>
                  <option value="oboe">Oboe / exact part ID</option>
                  <option value="strings">Strings / exact part ID</option>
                  <option value="winds">Winds / exact part ID</option>
                </datalist>
              </label>
            </div>
            <p class="mix-interpolation-note"><span class="notation-line" aria-hidden="true"></span> Rubato interpolates continuously between these two points.</p>
            <label class="mix-menu-level">
              <span><i class="endpoint-dot" aria-hidden="true"></i>{mixGesture === 'feature' ? 'Start floor' : mixGesture === 'bed' ? 'Start bed' : 'Start point'}</span>
              <input type="range" min="0" max="100" bind:value={mixStartLevel} aria-label="Region start volume" />
              <output>{mixStartLevel}%</output>
            </label>
            <label class="mix-menu-level">
              <span><i class="endpoint-dot endpoint-dot-end" aria-hidden="true"></i>{mixGesture === 'feature' ? 'End peak' : mixGesture === 'bed' ? 'End bed' : 'End point'}</span>
              <input type="range" min="0" max="100" bind:value={mixEndLevel} aria-label="Region end volume" />
              <output>{mixEndLevel}%</output>
            </label>
            <label class="mix-enabled-control">
              <input type="checkbox" bind:checked={mixEnabled} /> Enabled
            </label>
            {#if mixZoneId !== 'yamaha_anchor'}
              {@const selectedZone = mixZones.find((zone) => zone.zone_id === mixZoneId)}
              {#if selectedZone?.health !== 'ready'}
                <p class="mix-zone-warning">Needs calibration · live policy falls back to Keyboard.</p>
              {/if}
            {/if}
            <div class="score-context-actions">
              <button type="button" class="btn btn-primary" on:click={saveMixRegion} disabled={mixBusy || mixScoreIsStale()}>
                {mixBusy ? 'Saving…' : mixMenu.regionId ? 'Save region' : 'Create region'}
              </button>
              {#if mixMenu.regionId}
                <button type="button" class="btn btn-ghost btn-anchor-remove" on:click={removeMixRegion} disabled={mixBusy}>Delete</button>
              {/if}
              <button type="button" class="btn btn-ghost" on:click={() => (mixMenu = null)}>Cancel</button>
            </div>
          {/if}
        </div>
      {/if}
      </div>

      {#if workspaceMode === 'data' && (displayedScorePosition || correctionSourceSeconds !== null)}
        <details
          class="alignment-correction"
          data-testid="alignment-correction"
          on:toggle={prepareAlignmentCorrection}
        >
          <summary>Cursor not on the sound you heard?</summary>
          <p>
            This is the fallback for a major alignment miss. At the wrong audible beat,
            open this control to freeze that moment, select its measure on the score,
            then choose the beat below.
          </p>
          {#if correctionSourceSeconds !== null && selectedRehearsalMeasureNumber}
            <div class="alignment-correction-target" role="group" aria-label="Correct beat">
              <strong>That sound belongs at m. {selectedRehearsalMeasureNumber}, beat</strong>
              {#each [0, 1, 2, 3] as beat}
                <button
                  type="button"
                  class="btn btn-ghost"
                  class:is-selected={correctionBeatInMeasure === beat}
                  aria-pressed={correctionBeatInMeasure === beat}
                  on:click={() => (correctionBeatInMeasure = beat)}
                >{beat + 1}</button>
              {/each}
              <button
                type="button"
                class="btn btn-primary"
                on:click={saveAlignmentCorrection}
                disabled={savingAlignmentCorrection}
              >{savingAlignmentCorrection ? 'Saving…' : 'Anchor this beat'}</button>
            </div>
          {:else}
            <small>Start orchestra or take playback first so Rubato can freeze the audible moment.</small>
          {/if}
        </details>
      {/if}

      {#if workspaceMode === 'data' && dataFacet !== 'timing' && coverageData}
        <details class="coverage-details-drawer">
          <summary>
            Coverage by measure · {coverageData.summary.covered} covered ·
            {coverageData.summary.touched} touched · {coverageData.summary.uncovered} uncovered
          </summary>
          <div class="coverage-grid">
            {#each coverageData.measures as measure (measure.measure)}
              <button
                type="button"
                class="measure-block block-{measure.state}"
                class:measure-selected={selectedRehearsalMeasureNumber === measure.measure}
                title="Measure {measure.measure} ({measure.state}){measure.solo ? ': ' + (measure.min_n ?? 0) + ' take(s)' : ''}"
                on:click={() => selectRehearsalMeasureNumber(measure.measure)}
              >
                <span class="measure-num">{measure.measure}</span>
              </button>
            {/each}
          </div>
        </details>
      {/if}
    </section>

    {#if workspaceMode === 'data'}
      <section class="deck data-workspace-panel" data-testid="data-workspace-panel">
        <div class="data-panel-heading">
          <h2>Alignment</h2>
        </div>
        <div class="data-facet-tabs" role="tablist" aria-label="Data facet">
          <button
            type="button"
            role="tab"
            class="data-facet-tab"
            class:is-active={dataFacet === 'timing'}
            aria-selected={dataFacet === 'timing'}
            on:click={() => (dataFacet = 'timing')}
          >Timing</button>
          <button
            type="button"
            role="tab"
            class="data-facet-tab"
            class:is-active={dataFacet === 'integrity'}
            aria-selected={dataFacet === 'integrity'}
            on:click={() => (dataFacet = 'integrity')}
          >Geometry</button>
        </div>
        <!-- Both facets stay mounted (toggled with `hidden`) so switching tabs
             preserves the Timing review progress and does not refetch. -->
        <div class="data-facet-panel" hidden={dataFacet !== 'timing'}>
          <AlignmentValidation
            bind:this={timingValidationEl}
            embedded={true}
            active={dataFacet === 'timing'}
            onSelectMeasure={selectDataMeasure}
            requestedMeasure={dataAuditionMeasure}
            onReviewStateChange={updateTimingReviewState}
            outputName={selectedBackendOutput}
            volume={orchestraVolume / 100}
            metronomeVolume={metronomeVolume / 100}
          />
        </div>
        <div class="data-facet-panel" hidden={dataFacet !== 'integrity'}>
          <ScoreIntegrityView
            pieceId="chopin_op11"
            movement={MOVEMENT}
            measureCount={Math.max(
              126,
              ...(coverageData?.measures ?? []).map((m) => m.measure),
            )}
          />
        </div>
      </section>
    {/if}

    {#if inspectorOpen && workspaceMode === 'data'}
      <aside class="inspector compact-controls" data-testid="inspector">
        <section class="stage global-controls-stage" aria-labelledby="listening-levels-heading">
          <p class="stage-eyebrow">Global controls</p>
          <h2 id="listening-levels-heading">Listening levels</h2>
          <div class="listening-levels">
            <div class="volume-console">
              <label class="volume-label" for="data-orchestra-volume">Orchestra</label>
              <input
                id="data-orchestra-volume"
                class="fader"
                type="range"
                min="0"
                max="100"
                step="1"
                bind:value={orchestraVolume}
                on:input={saveOrchestraVolume}
                style="--fill: {orchestraVolume}%"
                aria-label="Orchestra volume"
              />
              <output for="data-orchestra-volume" class="volume-readout">{orchestraVolume}</output>
            </div>
            <div class="volume-console">
              <label class="volume-label" for="data-metronome-volume">Metronome</label>
              <input
                id="data-metronome-volume"
                class="fader"
                type="range"
                min="0"
                max="100"
                step="1"
                bind:value={metronomeVolume}
                on:input={saveMetronomeVolume}
                style="--fill: {metronomeVolume}%"
                aria-label="Metronome volume"
              />
              <output for="data-metronome-volume" class="volume-readout">{metronomeVolume}</output>
            </div>
          </div>
          <p class="tempo-hint">Shared by alignment auditions and metronome-based latency checks. Saved on this Mac.</p>
        </section>
      </aside>
    {/if}

    {#if inspectorOpen && workspaceMode === 'perform'}
      <aside
        class="inspector sound-drawer"
        data-testid="inspector"
        aria-labelledby="sound-heading"
      >
      <section class="stage sound-panel" aria-labelledby="sound-heading">
        <div class="sound-drawer-heading">
          <div>
            <p class="stage-eyebrow">Rehearsal output</p>
            <h2 id="sound-heading">Sound</h2>
          </div>
          <button
            type="button"
            class="drawer-close"
            aria-label="Close sound controls"
            on:click={() => (inspectorOpen = false)}
          >×</button>
        </div>

        <p class="sound-route-summary">
          {selectedBackendOutput === NO_MIDI_OUTPUT
            ? 'Piano stays on the Keyboard · REAPER/BBCSO orchestra goes to the LG soundbar.'
            : `Piano input and orchestra MIDI use ${selectedOutputName}.`}
        </p>
        <div
          class="sound-status"
          class:is-error={!hardwareStatus.running && !liveRuntimeActive && !!(liveStartupFailure || rendererPreloadFailure)}
          role="status"
          aria-live="polite"
        >
          {#if hardwareStatus.running || liveRuntimeActive}
            <span class="state-word word-{performanceState}">{stateLabel}</span>
            <span class="state-detail">{hardwareStatus.message}</span>
          {:else}
            <span class="state-word">Orchestra {orchestraReadinessLabel}</span>
            <span class="state-detail">{orchestraReadinessDetail}</span>
          {/if}
        </div>

        {#if hardwareStatus.running && !liveRuntimeActive && !isOrchestraOnlyPlaying && !isHardwareRecording}
          <button
            type="button"
            class="t-btn t-stop"
            on:click={stopHardwarePlayback}
          >
            <span class="t-icon" aria-hidden="true">■</span>
            Stop current playback
          </button>
        {/if}

        <div class="listening-levels">
          <p class="controls-group-title">Levels</p>
          <div class="volume-console">
            <label class="volume-label" for="orchestra-volume">Orchestra</label>
            <input
              id="orchestra-volume"
              class="fader"
              type="range"
              min="0"
              max="100"
              step="1"
              bind:value={orchestraVolume}
              on:input={saveOrchestraVolume}
              style="--fill: {orchestraVolume}%"
              aria-label="Orchestra volume"
            />
            <output for="orchestra-volume" class="volume-readout">{orchestraVolume}</output>
          </div>
          <div class="volume-console">
            <label class="volume-label" for="metronome-volume">Metronome</label>
            <input
              id="metronome-volume"
              class="fader"
              type="range"
              min="0"
              max="100"
              step="1"
              bind:value={metronomeVolume}
              on:input={saveMetronomeVolume}
              style="--fill: {metronomeVolume}%"
              aria-label="Metronome volume"
            />
            <output for="metronome-volume" class="volume-readout">{metronomeVolume}</output>
          </div>
        </div>
        <p class="tempo-hint">
          Orchestra preserves the score's internal dynamics{liveRuntimeActive ? ` · live at ${Math.round((runtimeStatus?.orchestra_volume ?? orchestraVolume / 100) * 100)}%` : ''}. Metronome applies to alignment auditions and latency checks.
        </p>

        <div class="volume-console tempo-console">
          <label class="volume-label" for="orchestra-tempo">Lead tempo</label>
          <input
            id="orchestra-tempo"
            class="fader"
            type="range"
            min="40"
            max="200"
            step="1"
            bind:value={orchestraTempoBpm}
            on:input={saveOrchestraTempo}
            style="--fill: {(orchestraTempoBpm - 40) / 1.6}%"
            aria-label="Orchestra tempo in quarter-note beats per minute"
          />
          <label class="metronome-input" for="orchestra-tempo-number">
            <span aria-hidden="true">♩ =</span>
            <input
              id="orchestra-tempo-number"
              type="number"
              min="40"
              max="200"
              step="1"
              bind:value={orchestraTempoBpm}
              on:input={saveOrchestraTempo}
              aria-label="Quarter-note tempo in beats per minute"
            />
          </label>
        </div>
        <p class="tempo-hint">
          {liveRuntimeActive && runtimeStatus?.section_mode === 'FOLLOW'
            ? `Following your playing now · ♩ = ${orchestraTempoBpm} is saved for the next orchestra-led passage.`
            : `Quarter-note BPM for orchestra-led passages and interludes${liveRuntimeActive ? ' · applied now.' : '.'}`}
        </p>

        <details class="hardware-timing">
          <summary>Advanced Keyboard timing</summary>
          <div class="volume-console hardware-timing-control">
            <label class="volume-label" for="yamaha-output-advance">Output advance</label>
            <input
              id="yamaha-output-advance"
              class="fader"
              type="range"
              min="0"
              max="100"
              step="10"
              bind:value={yamahaOutputAdvanceMs}
              on:input={saveYamahaOutputAdvance}
              style="--fill: {yamahaOutputAdvanceMs}%"
              aria-label="Keyboard output advance in milliseconds"
            />
            <output for="yamaha-output-advance" class="volume-readout">
              {yamahaOutputAdvanceMs} ms
            </output>
          </div>
          <p class="tempo-hint">
            Fires the orchestra this many milliseconds earlier to offset a repeatable
            Keyboard/Mac output delay. {liveRuntimeActive
              ? 'Applied live — nudge it until the orchestra locks with your playing.'
              : 'Applies live and to the next run; dial it in by ear during a performance.'}
            Leave at 0 unless the orchestra is consistently late; this cannot repair tracking
            drift or a wrong score position.
          </p>
        </details>

        <div class="calibration-panel">
          <div class="calibration-console">
            <span class="calibration-title">Latency calibration</span>
            <button
              type="button"
              class="btn btn-ghost"
              data-testid="latency-calibrate"
              on:click={runLatencyCalibration}
              disabled={latencyCalibrating || !selectedBackendInput || !selectedBackendOutput}
            >
              {latencyCalibrating ? 'Playing 4 counts in, then 12 to measure — keep pressing one key…' : 'Measure with a metronome'}
            </button>
            {#if latencyCalibration}
              <div class="calibration-result" data-testid="latency-calibration-result">
                <p>
                  {#if latencyCalibration.suggested_output_advance_ms != null}
                    <span class="calibration-badge" class:is-confident={latencyCalibration.confident}>
                      {latencyCalibration.confident ? '✓ 95% confident' : 'needs a steadier pass'}
                    </span>
                  {/if}
                  {latencyCalibration.message}
                </p>
                {#if latencyCalibration.suggested_output_advance_ms != null}
                  <button
                    type="button"
                    class="btn btn-primary"
                    data-testid="latency-apply"
                    on:click={applyLatencySuggestion}
                  >
                    Apply {latencyCalibration.suggested_output_advance_ms} ms
                  </button>
                {/if}
              </div>
            {/if}
          </div>
          <p class="tempo-hint">
            Plays a steady 90 BPM click through the Keyboard: 4 counts in, then 12 measured
            beats. Keep pressing one key (any key), locked by ear. Estimates the systemic
            latency with a 95% interval; anticipation makes it a lower bound, so fine-tune
            by ear.
          </p>
        </div>

        <div class="soundcheck">
          <label class="sc-field">
            <span class="sc-label">Piano input</span>
            <select
              bind:value={selectedBackendInput}
              on:change={handleInputSelect}
              disabled={!backendInputs.length || hardwareStatus.running || goingLive || liveRuntimeActive}
            >
              {#if !backendInputs.length}
                <option value="">No inputs found</option>
              {:else}
                {#each backendInputs as input}
                  <option value={input}>{input}</option>
                {/each}
              {/if}
            </select>
          </label>
          <label class="sc-field">
            <span class="sc-label">Orchestra output</span>
            <select
              bind:value={selectedBackendOutput}
              on:change={handleOutputSelect}
              disabled={hardwareStatus.running || goingLive || liveRuntimeActive}
            >
              {#if !backendOutputs.length}
                <option value="">No outputs found</option>
              {:else}
                <option value="">Select output…</option>
                {#each backendOutputs as output}
                  <option value={output}>{output === 'Clavinova' ? 'Keyboard speakers' : output}</option>
                {/each}
              {/if}
              <option value={NO_MIDI_OUTPUT}>LG soundbar · REAPER/BBCSO</option>
            </select>
          </label>
          <label class="preload-setting">
            <input
              type="checkbox"
              bind:checked={preloadBbcsoOnStartup}
              on:change={() => void saveRendererPreloadSetting()}
              disabled={hardwareStatus.running || goingLive || liveRuntimeActive || rendererPreloadBusy}
            />
            <span>
              <strong>Keep REAPER ready</strong>
              <small>Connect to the orchestra host when Rubato opens</small>
            </span>
          </label>
          <button
            type="button"
            class="btn btn-ghost"
            disabled={hardwareStatus.running || goingLive || liveRuntimeActive}
            on:click={() => void refreshBackendMidiDevices(true)}
          >
            Refresh devices
          </button>
        </div>
        {#if !backendInputs.length && !backendOutputs.length}
          {#if !midiBackendAvailable}
            <p class="hint stage-hint">
              Rubato started without its MIDI backend. End this session and restart with the standard setup.
            </p>
          {:else}
            <p class="hint stage-hint">
              No MIDI ports detected — check the USB connection and click Refresh devices.
            </p>
          {/if}
        {:else if !hardwareReady}
          <p class="hint stage-hint">No backend MIDI output detected. Connect the Keyboard and refresh devices.</p>
        {/if}
        <p class="local-control-note">
          Rubato does not switch the Keyboard's own sound. If the piano keys are silent, enable Local Control on the piano itself.
        </p>

      </section>

      <details class="rehearsal-details" bind:this={rehearsalDetailsEl}>
        <summary>Rehearsal recordings and playback</summary>
      <div class="wings">
        <section class="deck take-capture-deck" aria-labelledby="take-heading" data-testid="rehearsal-controls">
          <h2 id="take-heading">Take Capture</h2>

          {#if selectedRehearsalMeasure}
            <div class="next-take-target" data-testid="next-take-target">
              <div class="passage-target-copy">
                <span>Selected score location</span>
                <strong>Record from m. {selectedRehearsalMeasure.measure}</strong>
                <small class="target-score-link">This score location stays attached to the recording.</small>
                <small>
                  {selectedMeasurePassCount > 0
                    ? `${selectedMeasurePassCount} ${selectedMeasurePassCount === 1 ? 'pass' : 'passes'} from or through here · another pass builds confidence`
                    : 'No pass recorded here yet'}
                </small>
              </div>
              <div class="recording-plan" data-testid="recording-plan">
                <span>This pass</span>
                {#if !orchestraCueEnabled}
                  <strong>Solo recording from m. {selectedRehearsalMeasure.measure}.</strong>
                  <small>The orchestra stays silent; Rubato places the take in the score afterward.</small>
                {:else if rehearsalPlan}
                  <strong>{rehearsalLeadPlan(rehearsalPlan)}</strong>
                  <small>{rehearsalFollowPlan(rehearsalPlan)}</small>
                {:else}
                  <strong>Preparing the orchestra and follower plan…</strong>
                  <small>Rubato will show the authority handoff and learned tempo before recording.</small>
                {/if}
              </div>
              <div class="passage-record-control" aria-label="Record this passage">
                <label class="cue-switch">
                  <input type="checkbox" bind:checked={orchestraCueEnabled} disabled={captureState !== 'idle'} />
                  <span class="cue-switch-copy">
                    <strong>Orchestra cue</strong>
                    <small>
                      {orchestraCueEnabled
                        ? `On · orchestra starts at m. ${selectedRehearsalMeasure.measure} and keeps playing — come in whenever you are ready`
                        : `Off · recording starts immediately at m. ${selectedRehearsalMeasure.measure}`}
                    </small>
                  </span>
                  <span class:enabled={orchestraCueEnabled} class="cue-switch-state">
                    {orchestraCueEnabled ? 'On' : 'Off'}
                  </span>
                </label>
                <button
                  type="button"
                  class="btn btn-primary passage-record-button"
                  on:click={startSelectedPassage}
                  disabled={!!recordPassDisabledReason}
                  title={recordPassDisabledReason || 'Record a pass from this measure'}
                >
                  ● Record pass
                </button>
              </div>
            </div>

            <div class="passage-review-card" data-testid="passage-review-card">
              <div class="passage-review-heading">
                <div>
                  <span>Passage recordings</span>
                  <strong>
                    Measure {selectedRehearsalMeasure.measure} · {selectedMeasurePassCount}
                    aligned {selectedMeasurePassCount === 1 ? 'pass' : 'passes'}
                  </strong>
                </div>
                {#if passageRecordings.length > 0}
                  <label class="review-take-picker">
                    Recording
                    <select bind:value={selectedReviewTakeId} aria-label="Recording at selected passage">
                      {#each passageRecordings as take}
                        <option value={take.take_id}>
                          {recordingOptionLabel(take, passagePassNumber(take))}
                        </option>
                      {/each}
                    </select>
                  </label>
                {/if}
              </div>

              <div class="measure-guidance measure-guidance-{selectedRehearsalMeasure.state}" data-testid="measure-guidance">
                <strong>{selectedMeasureGuidance.label}</strong>
                <span>{selectedMeasureGuidance.explanation}</span>
                <small>{selectedMeasureGuidance.action}</small>
              </div>

              {#if selectedReviewTake}
                {#if selectedReviewTake.alignment}
                  <div class="alignment-quality" data-testid="alignment-quality">
                    <strong>{alignmentRatingLabel(selectedReviewTake)} · {alignmentPercent(selectedReviewTake)}% score-note match</strong>
                    <span>
                      {selectedReviewTake.alignment.matched_notes} notes matched ·
                      {selectedReviewTake.alignment.extra_notes} played notes unmatched ·
                      {selectedReviewTake.alignment.missing_notes} score notes not played
                    </span>
                    <small>
                      {selectedReviewTake.alignment.ambiguous
                        ? 'Rubato found more than one plausible score location; review is needed.'
                        : 'Placed unambiguously in the score and included in learning.'}
                    </small>
                  </div>
                {/if}
                <p class="passage-review-summary">
                  {#if selectedReviewTake.note_on_count === 0}
                    This attempt stopped before the Clavinova sent any piano notes. It is not being used for learning.
                  {:else if selectedReviewTake.score_span}
                    Recorded at {formatRecordedTime(selectedReviewTake.recorded_at)} local time · covers mm.
                    {selectedReviewTake.score_span.start.measure_label}–{selectedReviewTake.score_span.end.measure_label}.
                    Playback begins at m. {selectedRehearsalMeasure.measure}, and the red cursor follows the same alignment.
                  {:else}
                    Recorded at {formatRecordedTime(selectedReviewTake.recorded_at)} local time · {selectedReviewTake.note_on_count} notes · score placement {selectedReviewTake.analysis_state}.
                  {/if}
                </p>
                {#if selectedReviewTake.note_on_count === 0}
                  <p class="after-error">Nothing musical was captured. Leave orchestra cue on and record this passage again.</p>
                {:else if selectedReviewTake.status === 'aligned' && selectedReviewTake.review?.state === 'ready'}
                  <div class="review-output-group">
                    <span>On Yamaha</span>
                    <button
                      type="button"
                      class="btn btn-outline"
                      aria-label={`Hear ${passLocationLabel(selectedReviewTake)} alone on Yamaha from measure ${selectedRehearsalMeasure.measure}`}
                      on:click={() => hearAlignedTake(selectedReviewTake, 'solo')}
                      disabled={!selectedBackendOutput || hardwareStatus.running}
                    >▶ Take only</button>
                    <button
                      type="button"
                      class="btn btn-primary"
                      aria-label={`Hear ${passLocationLabel(selectedReviewTake)} with orchestra on Yamaha from measure ${selectedRehearsalMeasure.measure}`}
                      on:click={() => hearAlignedTake(selectedReviewTake, 'ensemble')}
                      disabled={!selectedBackendOutput || hardwareStatus.running}
                    >▶ Take + orchestra</button>
                  </div>
                  {#if !selectedBackendOutput}
                    <small>Select Yamaha out in Sound Check for hardware playback.</small>
                  {/if}
                {:else if selectedReviewTake.status === 'aligned' && selectedReviewTake.review?.state === 'failed'}
                  <p class="after-error">{selectedReviewTake.review.error || 'Playback preparation needs another try.'}</p>
                  <button type="button" class="btn btn-primary" on:click={() => ensureTakeReview(selectedReviewTake, true)}>
                    Retry playback preparation
                  </button>
                {:else if selectedReviewTake.status === 'aligned'}
                  <p class="after-progress">Preparing synchronized take playback…</p>
                {:else}
                  <p class="after-error">This recording needs attention before it can be played with the orchestra.</p>
                {/if}

                <details class="recording-options">
                  <summary>Recording options</summary>
                  <div class="recording-option-actions">
                    {#if selectedReviewTake.status === 'aligned' && selectedReviewTake.review?.state === 'ready'}
                      <button
                        type="button"
                        class="btn btn-ghost"
                        aria-label={`Preview ${passLocationLabel(selectedReviewTake)} alone in this browser from measure ${selectedRehearsalMeasure.measure}`}
                        on:click={() => previewAlignedTake(selectedReviewTake, 'solo')}
                        disabled={playbackLoading === `review-solo-${selectedReviewTake.take_id}`}
                      >Preview piano in browser</button>
                      <button
                        type="button"
                        class="btn btn-ghost"
                        aria-label={`Preview ${passLocationLabel(selectedReviewTake)} with orchestra in this browser from measure ${selectedRehearsalMeasure.measure}`}
                        on:click={() => previewAlignedTake(selectedReviewTake, 'ensemble')}
                        disabled={playbackLoading === `review-ensemble-${selectedReviewTake.take_id}`}
                      >Preview together in browser</button>
                    {/if}
                    <button type="button" class="btn btn-ghost" on:click={() => toggleDiscardTake(selectedReviewTake)}>
                      {selectedReviewTake.status === 'discarded'
                        ? 'Restore recording'
                        : selectedReviewTake.note_on_count === 0
                          ? 'Dismiss incomplete attempt'
                          : 'Exclude from learning'}
                    </button>
                  </div>
                  <small>Recordings save automatically in the Rubato workspace. Excluded passes stay recoverable.</small>
                </details>
              {:else}
                <p class="passage-review-summary">
                  No recording starts or passes through measure {selectedRehearsalMeasure.measure} yet.
                </p>
              {/if}
              {#if passageAnalysis && passageAnalysis.take_count > 0}
                <div class="passage-learning-summary" data-testid="passage-learning-summary">
                  <strong>
                    Learning confidence {Math.round(passageAnalysis.confidence * 100)}% from {passageAnalysis.take_count} passes
                  </strong>
                  <span>
                    {passageAnalysis.evidence_multiplier.toFixed(1)}× the usable evidence of one pass ·
                    {passageAnalysis.common_cell_count} shared timing observations
                  </span>
                  {#if passageAnalysis.learned_tempo_bpm}
                    <span>
                      Typical pace {passageAnalysis.learned_tempo_bpm} BPM · usual take-to-take timing variation
                      {passageAnalysis.typical_tempo_variation_percent}%
                    </span>
                  {/if}
                  {#if passageAnalysis.high_variance_points.length > 0}
                    <span>
                      Expressive variation is concentrated near
                      {passageAnalysis.high_variance_points
                        .slice(0, 3)
                        .map((point) => `m. ${point.measure} beat ${point.beat}`)
                        .join(', ')}.
                    </span>
                  {/if}
                  <span class="passage-recommendation">
                    {coverageGuidance(selectedRehearsalMeasure).action}
                  </span>
                </div>
              {/if}
            </div>
          {:else}
            <p class="target-hint">Choose any numbered passage on the score to rehearse from there.</p>
          {/if}

          {#if captureState === 'starting'}
            <div class="capture-lead-in" aria-live="polite">
              <p class="lead-in-word">Starting orchestra…</p>
              <p class="lead-in-detail">Locking the MIDI devices for this take.</p>
            </div>
          {:else if captureState === 'lead-in'}
            <div class="capture-lead-in">
              <p class="lead-in-word">Lead-in</p>
              <p class="lead-in-detail">
                Orchestra playing · your entry{captureTargetMeasure ? ` at measure ${captureTargetMeasure}` : ''}
                in {Math.max(0, leadInSecondsRemaining).toFixed(1)}s
                · orchestra hands off at your entry
              </p>
              <button type="button" class="btn btn-ghost" on:click={cancelLeadIn}>Cancel</button>
            </div>
          {:else if captureState === 'recording'}
            <div class="capture-recording">
              <p class="mode-chip">
                ● Recording · {captureMode === 'from-the-top'
                  ? 'from the top'
                  : captureMode === 'from-position'
                    ? `from measure ${captureTargetMeasure}`
                    : 'free'}
              </p>
              <p class="elapsed-display">{formatTime(elapsedSeconds)}</p>
              <button
                type="button"
                class="t-btn t-record is-recording"
                on:click={stopTake}
                style="width: 100%; max-width: 320px;"
              >
                <span class="rec-dot pulsing" aria-hidden="true"></span>
                Stop Take (space)
              </button>
            </div>
          {:else}
            {#if !selectedRehearsalMeasure}
            <div class="capture-action-row" style="margin: 1.5rem 0; display: flex; justify-content: center;">
              {#if hardwareStatus.kind === 'stopping'}
                <button type="button" class="t-btn t-record" disabled style="width: 100%; max-width: 320px;">
                  <span class="rec-dot" aria-hidden="true"></span>
                  Stopping…
                </button>
              {:else}
                <button
                  type="button"
                  class="t-btn t-record"
                  on:click={startFreeTake}
                  disabled={!selectedBackendInput}
                  style="width: 100%; max-width: 320px; border-color: var(--ember);"
                >
                  <span class="rec-dot" style="background-color: var(--ember);" aria-hidden="true"></span>
                  Record Take
                </button>
              {/if}
            </div>

            <div class="capture-secondary-row">
              <div>
                {#if firstEntrancePlan}
                  <p class="hint" data-testid="first-entrance-recording-plan">
                    {rehearsalLeadPlan(firstEntrancePlan)}
                    {rehearsalFollowPlan(firstEntrancePlan)}
                  </p>
                {:else}
                  <p class="hint">Preparing the orchestra and follower plan…</p>
                {/if}
                <button
                  type="button"
                  class="btn btn-ghost"
                  on:click={startFromTheTop}
                  disabled={!!fromTheTopDisabledReason || captureState !== 'idle'}
                  title={fromTheTopDisabledReason || undefined}
                >
                  Record first piano entrance with orchestra
                </button>
              </div>
            </div>
            {/if}

            <details class="capture-options">
              <summary>Capture options</summary>
              <label class="duration-field">
                Auto-stop after <input
                  type="number"
                  min="1"
                  max="3600"
                  bind:value={hardwareRecordSeconds}
                  placeholder="none"
                /> s
              </label>
            </details>
            {#if !backendInputs.length}
              <p class="hint">No backend MIDI input detected. Connect the Yamaha and refresh devices.</p>
            {/if}
          {/if}

          {#if captureState === 'idle' && latestTake}
            <div class="after-card" data-testid="after-take-card" aria-live="polite">
              <p class="after-eyebrow">After this take</p>
              <div class="after-heading-row">
                <div>
                  <h3>
                    {latestTake.note_on_count === 0
                      ? 'No piano notes were recorded'
                      : latestTake.score_span
                      ? `Pass filed from m. ${takeEntryMeasure(latestTake, coverageData?.measures ?? []) ?? latestTake.score_span.start.measure_label}`
                      : 'Recording added to your rehearsal library'}
                  </h3>
                  <p class="after-summary">
                    {Math.round(latestTake.duration_seconds)}s · {latestTake.note_on_count} notes
                  </p>
                </div>
                <span class="take-status-badge badge-{latestTake.analysis_state}">{latestTake.analysis_state}</span>
              </div>

              {#if latestTake.status === 'aligning' || latestTake.status === 'captured'}
                <p class="after-progress">Placing it in the score…</p>
              {:else if latestTake.status === 'aligned'}
                {#if !latestTake.review || latestTake.review.state === 'queued' || latestTake.review.state === 'running'}
                  <p class="after-progress">
                    {reviewRequestingId === latestTake.take_id ? 'Starting orchestral review…' : 'Preparing orchestra playback…'}
                  </p>
                {:else if latestTake.review.state === 'failed'}
                  <p class="after-error">{latestTake.review.error || 'The orchestral review needs another try.'}</p>
                  <button
                    type="button"
                    class="btn btn-primary"
                    on:click={() => ensureTakeReview(latestTake, true)}
                    disabled={reviewRequestingId === latestTake.take_id}
                  >
                    {reviewRequestingId === latestTake.take_id ? 'Retrying…' : 'Retry orchestral review'}
                  </button>
                {:else}
                  <p class="after-ready">Your timing is aligned. Hear the recorded piano and orchestra together.</p>
                  <div class="after-primary-actions">
                    <button
                      type="button"
                      class="t-btn t-play"
                      aria-label={`Hear ${passLocationLabel(latestTake)} with orchestra on Yamaha`}
                      on:click={() => hearTakeWithOrchestra(latestTake)}
                      disabled={!selectedBackendOutput || hardwareStatus.running}
                    >
                      <span class="t-icon" aria-hidden="true">▶</span>
                      Hear take + orchestra
                    </button>
                    <button
                      type="button"
                      class="btn btn-ghost"
                      on:click={() => previewTakeWithOrchestra(latestTake)}
                      disabled={playbackLoading === `review-ensemble-${latestTake.take_id}`}
                    >
                      {playbackLoading === `review-ensemble-${latestTake.take_id}` ? 'Loading…' : 'Preview here'}
                    </button>
                  </div>
                {/if}
              {:else if latestTake.analysis_state === 'failed'}
                <p class="after-error">
                  Score placement hit a software error. Your recording{latestTake.cue?.kind === 'from_position' || latestTake.placement_hint
                    ? ' and selected starting measure'
                    : ''} are preserved.
                </p>
                {#if latestTake.failure?.retryable}
                  <button
                    type="button"
                    class="btn btn-primary"
                    on:click={() => retryAlignment(latestTake)}
                    disabled={retryingAlignmentTakeId === latestTake.take_id}
                  >
                    {retryingAlignmentTakeId === latestTake.take_id ? 'Retrying placement…' : 'Retry score placement'}
                  </button>
                {/if}
              {:else if latestTake.status === 'ambiguous'}
                <p class="after-error">Choose the location below so Rubato can prepare the orchestra.</p>
              {:else if latestTake.note_on_count === 0}
                <p class="after-error">
                  This attempt stopped before the Clavinova sent a note. The selected score location is preserved; try the passage again with orchestra cue on.
                </p>
              {:else}
                <p class="after-error">Not enough of the played notes matched the selected passage yet. The recording is preserved in Rehearsal passages.</p>
              {/if}

              <div class="after-secondary-actions">
                {#if latestTake.note_on_count > 0}
                  <button
                    type="button"
                    class="btn btn-ghost"
                    on:click={() => latestTake.review?.state === 'ready'
                      ? previewAlignedTake(latestTake, 'solo')
                      : playTake(latestTake)}
                  >
                    Solo only
                  </button>
                {/if}
                <button
                  type="button"
                  class="btn btn-outline"
                  on:click={() => selectedRehearsalMeasure ? startSelectedPassage() : startFreeTake()}
                  disabled={!selectedBackendInput || (!!selectedRehearsalMeasure && orchestraCueEnabled && !selectedBackendOutput) || hardwareStatus.running}
                >
                  {latestTake.note_on_count === 0
                    ? `Try again from m. ${selectedRehearsalMeasure?.measure ?? ''}`
                    : selectedRehearsalMeasure
                      ? `Record another pass from m. ${selectedRehearsalMeasure.measure}`
                      : 'Record another'}
                </button>
              </div>
            </div>
          {/if}

          <!-- Score-location rehearsal library -->
          <div class="takes-list-container">
            <div class="take-bank-heading">
              <div>
                <h3>Rehearsal passages</h3>
                <p>
                  {passageTakeGroups.length} {passageTakeGroups.length === 1 ? 'score location' : 'score locations'} ·
                  {takes.length} {takes.length === 1 ? 'recording' : 'recordings'}
                </p>
              </div>
            </div>
            <p class="take-bank-explainer">
              Recordings are filed by where you entered in the score. Repeated passes from the same location stay together and build confidence; each recording remains available to hear or exclude.
            </p>
            {#if takes.length === 0}
              <p class="hint">No passages recorded yet. Choose a measure on the score to begin.</p>
            {:else}
              <div class="takes-scroll-list">
                {#each passageTakeGroups as group (group.key)}
                  {@const selectedTake = selectedRecordingForGroup(group)}
                  <section
                    class="passage-take-group"
                    class:is-selected={group.entryMeasure === selectedRehearsalMeasureNumber}
                    data-testid="passage-take-group"
                    data-entry-measure={group.entryMeasure ?? ''}
                  >
                    <div class="passage-group-heading">
                      <div>
                        <span class="passage-group-kicker">Score location</span>
                        <h4>{passageTitle(group)}</h4>
                        <p>
                          {passageGroupSummary(group)}
                        </p>
                      </div>
                      {#if group.entryMeasure !== null}
                        <button
                          type="button"
                          class="btn btn-ghost passage-group-jump"
                          aria-label={`Show measure ${group.entryMeasure} on score`}
                          on:click={() => selectPassageGroup(group)}
                        >Show on score</button>
                      {/if}
                    </div>

                    <div class="compact-recording-row">
                      <label>
                        <span class="sr-only">Recording at {passageTitle(group)}</span>
                        <select
                          value={selectedTake?.take_id ?? ''}
                          aria-label={`Choose recording ${passageTitle(group).toLowerCase()}`}
                          on:change={(event) => selectGroupRecordingEvent(group, event)}
                        >
                          {#each group.takes as take (take.take_id)}
                            <option value={take.take_id}>{recordingOptionLabel(take, passNumber(group, take))}</option>
                          {/each}
                        </select>
                      </label>
                      <div class="compact-recording-actions" aria-label="Recording actions">
                        {#if selectedTake?.status === 'aligned' && selectedTake.review?.state === 'ready'}
                          <button
                            type="button"
                            class="icon-action"
                            title="Hear piano only on Yamaha"
                            aria-label={`Hear ${passageTitle(group).toLowerCase()} piano only on Yamaha`}
                            on:click={() => hearAlignedTake(selectedTake, 'solo')}
                            disabled={!selectedBackendOutput || hardwareStatus.running}
                          >♩</button>
                          <button
                            type="button"
                            class="icon-action primary"
                            title="Hear take with orchestra on Yamaha"
                            aria-label={`Hear ${passageTitle(group).toLowerCase()} with orchestra on Yamaha`}
                            on:click={() => hearAlignedTake(selectedTake, 'ensemble')}
                            disabled={!selectedBackendOutput || hardwareStatus.running}
                          >▶</button>
                        {:else if selectedTake?.status === 'aligned'}
                          <button
                            type="button"
                            class="icon-action"
                            title="Prepare playback"
                            aria-label={`Prepare playback for ${passageTitle(group).toLowerCase()}`}
                            on:click={() => ensureTakeReview(selectedTake, selectedTake.review?.state === 'failed')}
                          >↻</button>
                        {/if}
                        <button
                          type="button"
                          class="icon-action"
                          title="Show passage and recording details"
                          aria-label={`Show ${passageTitle(group).toLowerCase()} on the score and open its recording details`}
                          on:click={() => selectGroupRecording(group, selectedTake?.take_id ?? '')}
                        >⌖</button>
                      </div>
                    </div>
                  </section>
                {/each}
              </div>
            {/if}
          </div>
        </section>

        <section class="deck" aria-labelledby="preview-heading">
          <h2 id="preview-heading">Browser playback</h2>
          <p class="deck-subtitle">
            Choose a passage and recording above, then use its browser-preview options. This transport follows the red score cursor and can be scrubbed or looped.
          </p>

          <div class="transport-meta">
            <span class="transport-eyebrow">Loaded</span>
            <strong>{playerLabel}</strong>
          </div>
          <div class="transport-controls">
            <button
              type="button"
              class="btn btn-round"
              on:click={togglePlayPause}
              disabled={!playerMidi}
              aria-label={isPlaying ? 'Pause' : 'Play'}
            >
              <span aria-hidden="true">{isPlaying ? '⏸' : '▶'}</span>
            </button>
            <div class="scrub">
              <span class="time">{formatTime(playerPosition)}</span>
              <input
                type="range"
                min="0"
                max={playerDuration || 1}
                step="0.01"
                value={playerPosition}
                on:input={handleSeek}
                disabled={!playerMidi}
                aria-label="Playback position"
              />
              <span class="time">{formatTime(playerDuration)}</span>
            </div>
            <label class="loop-toggle">
              <input type="checkbox" bind:checked={loopEnabled} /> Loop
            </label>
          </div>

          <div class="preview-file-row">
            <label class="file-button">
              <input type="file" accept=".mid" on:change={handleLocalFile} />
              Open MIDI File…
            </label>
          </div>
        </section>
      </div>
      </details>
      </aside>
    {/if}

    {#if workspaceMode === 'perform'}
    <details class="library">
      <summary>
        <span class="library-title">Library</span>
        <span class="library-sub">sessions · uploads · rendered outputs</span>
      </summary>
      <div class="library-body">
        <form class="session-create" on:submit|preventDefault={createSession}>
          <input id="session-id-input" placeholder="optional session id" bind:value={newSessionInput} />
          <button type="submit" class="btn btn-primary">Create Slot</button>
        </form>

        <div class="session-select">
          <label class="field">
            Take / Session
            <select bind:value={sessionId} on:change={handleSessionChange}>
              <option value="" disabled selected hidden>Select slot</option>
              {#each sessions as session}
                <option value={session.session_id}>{session.label ?? session.session_id}</option>
              {/each}
            </select>
          </label>
          <button type="button" class="btn btn-ghost" on:click={fetchStatus} disabled={!sessionId}>Refresh</button>
        </div>

        <div class="session-file-actions">
          <label class="file-button">
            <input type="file" accept=".mid" on:change={handlePipelineUpload} data-testid="upload-input" />
            Upload Solo MIDI
          </label>
          <button type="button" class="btn btn-ghost" on:click={sendLoadedToAccompanist} disabled={!playerBuffer}>
            Use Loaded File as Solo
          </button>
        </div>

        {#if sessionId && status}
          <div class="accompanist-status">
            <div>
              <strong>Files:</strong>
              <span class:ready={status.files.solo}>solo.mid</span>
              <span class:ready={status.files.accompaniment}>accompaniment.mid</span>
            </div>
            <div>
              <h3>Outputs</h3>
              {#if status.playback.length}
                {#each status.playback as item}
                  <div class="playback-row">
                    <div>
                      <strong>{item.label}</strong>
                      <small>{formatSize(item.size_bytes)}</small>
                    </div>
                    <div class="playback-actions">
                      <button
                        type="button"
                        class="btn btn-ghost"
                        on:click={() => playVariant(item.variant)}
                        disabled={playbackLoading === item.variant}
                      >
                        {playbackLoading === item.variant ? 'Loading…' : 'Play'}
                      </button>
                      <button
                        type="button"
                        class="btn btn-ghost"
                        on:click={() => playVariantOnHardware(item.variant)}
                        disabled={!selectedBackendOutput || hardwareStatus.running}
                      >
                        Yamaha
                      </button>
                      <a href={item.url} download>Download</a>
                    </div>
                  </div>
                {/each}
              {:else}
                <p>No MIDI files available yet.</p>
              {/if}
            </div>
          </div>
        {:else if sessionId}
          <p class="muted">No files uploaded yet. Use the controls above to send a MIDI file.</p>
        {/if}

        <div class="end-session">
          <button
            type="button"
            class="btn btn-ghost btn-danger"
            on:click={endSession}
            disabled={endingSession}
          >
            {endingSession ? 'Ending session…' : 'End session'}
          </button>
          <p class="hint">
            Stops the Rubato server. You'll need to run the setup command again to restart.
          </p>
        </div>
      </div>
    </details>
    {/if}
  </main>

  {#if captureToast}
    <div class="toast capture-toast" class:resolved={captureToast.resolved} role="status">
      {captureToast.text}
    </div>
  {/if}

  {#if message}
    <div class="toast" role="status">{message}</div>
  {/if}
</div>

<style>
  .cockpit {
    max-width: 1380px;
    margin: 0 auto 5rem;
    padding: 0 1.5rem 3rem;
    display: flex;
    flex-direction: column;
    gap: 2rem;
  }

  /* ---- Masthead --------------------------------------------------------- */

  .masthead {
    position: sticky;
    top: 0;
    z-index: 30;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
    padding: 1.1rem 0.25rem;
    margin: 0 -0.25rem;
    background: rgba(18, 19, 23, 0.92);
    backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--stage-hairline);
  }

  .wordmark {
    display: flex;
    flex-direction: column;
    justify-content: center;
  }

  .wordmark-title {
    font-family: 'Fraunces', Georgia, serif;
    font-size: 1.6rem;
    font-weight: 600;
    letter-spacing: 0.01em;
    margin: 0;
    color: var(--ink);
  }

  .wordmark-sub {
    margin: 0.15rem 0 0;
    color: var(--ink-dim);
    font-size: 0.72rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .workspace-switcher {
    display: inline-flex;
    flex: none;
    padding: 3px;
    border: 1px solid var(--stage-hairline-strong);
    border-radius: 999px;
    background: rgba(8, 9, 12, 0.46);
  }

  .workspace-switcher button {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    min-height: 2rem;
    padding: 0.34rem 0.72rem;
    border: 0;
    border-radius: 999px;
    background: transparent;
    color: var(--ink-dim);
    font: inherit;
    font-size: 0.76rem;
    font-weight: 680;
    cursor: pointer;
    transition: background 140ms ease, color 140ms ease, box-shadow 140ms ease;
  }

  .workspace-switcher button:hover:not(:disabled) {
    color: var(--ink);
  }

  .workspace-switcher button.is-active {
    background: rgba(244, 239, 230, 0.11);
    color: var(--ink);
    box-shadow: inset 0 0 0 1px rgba(244, 239, 230, 0.08);
  }

  .workspace-switcher button[data-testid='mode-mixing'].is-active {
    background: rgba(92, 194, 201, 0.14);
    color: #9debed;
  }

  .layer-menu {
    position: relative;
    flex: none;
  }

  .layer-toggle {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }

  .layer-popover {
    position: absolute;
    z-index: 90;
    top: calc(100% + 0.5rem);
    right: 0;
    display: grid;
    gap: 0.7rem;
    width: 260px;
    padding: 0.85rem;
    border: 1px solid var(--stage-hairline-strong);
    border-radius: 12px;
    background: rgba(25, 27, 32, 0.98);
    box-shadow: 0 18px 55px rgba(0, 0, 0, 0.58);
  }

  .layer-popover > strong {
    color: var(--ink);
    font-size: 0.8rem;
  }

  .layer-popover label {
    display: grid;
    grid-template-columns: auto 1fr;
    align-items: start;
    gap: 0.6rem;
    color: var(--ink);
    cursor: pointer;
  }

  .layer-popover input {
    margin-top: 0.15rem;
  }

  .layer-popover label span {
    display: grid;
    gap: 0.1rem;
    font-size: 0.76rem;
  }

  .layer-popover label small,
  .layer-mode-note {
    color: var(--ink-dim);
    font-size: 0.66rem;
    font-weight: 500;
  }

  .layer-mode-note {
    padding-top: 0.5rem;
    border-top: 1px solid var(--stage-hairline);
  }

  .workspace-switcher button:disabled {
    opacity: 0.35;
    cursor: not-allowed;
  }

  .masthead-status {
    display: flex;
    gap: 0.85rem;
    flex-wrap: nowrap;
    align-items: center;
    justify-content: center;
    flex: 1;
    min-width: 0;
  }

  /* ---- Transport instruments -------------------------------------------- */

  .transport-state {
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    padding: 0.3rem 0.7rem;
    border-radius: 999px;
    border: 1px solid var(--stage-hairline);
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    color: var(--ink-dim);
    white-space: nowrap;
  }

  .transport-state.recording {
    color: #ffd7d2;
    border-color: rgba(220, 88, 72, 0.55);
    background: rgba(220, 88, 72, 0.16);
  }

  .transport-state.armed {
    color: var(--accent);
    border-color: rgba(216, 169, 76, 0.5);
    background: rgba(216, 169, 76, 0.12);
  }

  /* The beat clock. Tabular figures and a fixed min-width so the digits do not
     jitter the layout as they tick -- a number that moves horizontally while
     you are reading it is unreadable at a glance from the bench. */
  .transport-clock {
    display: inline-flex;
    align-items: baseline;
    gap: 0.9rem;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .clock-field {
    display: inline-flex;
    align-items: baseline;
    gap: 0.3rem;
  }

  .clock-field small {
    font-size: 0.68rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-dim);
  }

  .clock-field strong {
    font-size: 1.5rem;
    font-weight: 600;
    line-height: 1;
    color: var(--ink);
    min-width: 1.6ch;
    text-align: right;
  }

  .clock-idle {
    font-size: 1.5rem;
    line-height: 1;
    color: var(--ink-dim);
  }

  .transport-elapsed {
    font-variant-numeric: tabular-nums;
    font-size: 0.9rem;
    color: var(--ink-dim);
  }

  .transport-flag {
    padding: 0.2rem 0.5rem;
    border-radius: 999px;
    background: rgba(216, 169, 76, 0.14);
    border: 1px solid rgba(216, 169, 76, 0.4);
    color: var(--accent);
    font-size: 0.65rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    white-space: nowrap;
  }

  /* MIDI wiring is a health light, not a sentence. It only earns attention
     when it goes wrong. */
  .device-dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: var(--danger, #dc5848);
    flex: none;
  }

  .device-dot.connected {
    background: #5fbf7f;
  }

  .device-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.3rem 0.75rem;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 500;
    letter-spacing: 0.02em;
    border: 1px solid var(--stage-hairline);
    color: var(--ink-dim);
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .device-pill.connected {
    color: var(--ink-muted);
    border-color: var(--stage-hairline-strong);
  }

  .device-pill.connected::before {
    content: '';
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--ready);
    flex-shrink: 0;
  }

  /* Silence: always the same pixels, top right, ember. Muscle-memory space. */
  .silence-btn {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    background: var(--ember);
    color: white;
    border: none;
    border-radius: 999px;
    padding: 0.75rem 1.25rem;
    font-size: 1rem;
    font-weight: 700;
    letter-spacing: 0.02em;
    box-shadow: 0 8px 24px rgba(209, 72, 58, 0.4);
    flex-shrink: 0;
  }

  .silence-btn:hover {
    filter: brightness(1.1);
  }

  .silence-btn:active {
    transform: scale(0.97);
  }

  .silence-icon {
    font-size: 1.3rem;
    line-height: 1;
  }

  /* ---- The stage -------------------------------------------------------- */

  .hall {
    display: flex;
    flex-direction: column;
    gap: 1.5rem;
  }

  /* Data workspace = a fixed-height two-pane split so the score and the audition
     controls are visible at once: no scrolling the page up and down between the
     PDF and the tracking panel. Each pane scrolls internally. Falls back to the
     normal vertical stack below 1200px so dense orchestral notation retains a
     useful reading width beside the controls. */
  @media (min-width: 1200px) {
    .cockpit.data-mode {
      height: 100vh;
      margin-bottom: 0;
      padding-bottom: 0.75rem;
      gap: 1rem;
      overflow: hidden;
    }
    .hall.data-split {
      flex: 1;
      min-height: 0;
      flex-direction: row;
      align-items: stretch;
      gap: 1.25rem;
      overflow: hidden;
    }
    .hall.data-split > .score-coordination {
      flex: 1.6 1 0;
      min-width: 0;
      margin: 0;
      overflow-y: auto;
    }
    .hall.data-split > .data-workspace-panel {
      flex: 1 1 0;
      max-width: 32rem;
      min-width: 0;
      overflow-y: auto;
    }
  }

  /* ---- Inspection panel -------------------------------------------------- */

  .inspector-toggle {
    flex: none;
    padding: 0.45rem 1rem;
    border-radius: 999px;
    border: 1px solid var(--stage-hairline);
    background: transparent;
    color: var(--ink-dim);
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    cursor: pointer;
  }

  .inspector-toggle:hover {
    color: var(--ink);
    border-color: var(--ink-dim);
  }

  .inspector {
    display: flex;
    flex-direction: column;
    gap: 1.5rem;
    padding-top: 0.5rem;
    border-top: 1px solid var(--stage-hairline);
  }

  .sound-drawer {
    position: fixed;
    z-index: 70;
    top: 4.8rem;
    right: max(1rem, calc((100vw - 1380px) / 2 + 1.5rem));
    bottom: 1rem;
    width: min(27rem, calc(100vw - 2rem));
    overflow-y: auto;
    gap: 1rem;
    padding: 0;
    border: 1px solid var(--stage-hairline-strong);
    border-radius: 16px;
    background: rgba(20, 21, 25, 0.98);
    box-shadow: 0 24px 80px rgba(0, 0, 0, 0.68);
  }

  .sound-panel.stage {
    align-items: stretch;
    padding: 1.25rem;
    border: 0;
    border-radius: 0;
    box-shadow: none;
    text-align: left;
    background: transparent;
  }

  .sound-drawer-heading {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
  }

  .sound-drawer-heading h2 {
    margin: 0;
    font-family: 'Fraunces', Georgia, serif;
    font-size: 1.7rem;
    color: var(--ink);
  }

  .sound-drawer-heading .stage-eyebrow {
    margin-bottom: 0.25rem;
  }

  .drawer-close {
    display: grid;
    place-items: center;
    width: 2.25rem;
    height: 2.25rem;
    flex: none;
    border: 1px solid var(--stage-hairline);
    border-radius: 999px;
    background: transparent;
    color: var(--ink-dim);
    font: inherit;
    font-size: 1.35rem;
    cursor: pointer;
  }

  .drawer-close:hover {
    color: var(--ink);
    border-color: var(--ink-dim);
  }

  .sound-route-summary,
  .local-control-note {
    margin: 0.8rem 0 0;
    color: var(--ink-dim);
    font-size: 0.82rem;
    line-height: 1.45;
  }

  .sound-route-summary {
    padding: 0.75rem 0.85rem;
    border-radius: 10px;
    background: rgba(92, 194, 201, 0.08);
    color: #b8e8e9;
  }

  .sound-status {
    display: flex;
    align-items: baseline;
    gap: 0.65rem;
    margin-top: 0.8rem;
    color: var(--ink-dim);
  }

  .sound-status .state-word {
    flex: none;
    font: inherit;
    font-size: 0.78rem;
    font-weight: 750;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }

  .sound-status .state-detail {
    margin: 0;
    font-size: 0.78rem;
    line-height: 1.35;
  }

  .sound-status.is-error,
  .sound-status.is-error .state-word {
    color: #f0aaa7;
  }

  .sound-drawer .listening-levels {
    align-items: stretch;
    margin-top: 1.25rem;
  }

  .sound-drawer .volume-console {
    width: 100%;
    max-width: none;
  }

  .sound-drawer .soundcheck {
    display: grid;
    grid-template-columns: 1fr;
    align-items: stretch;
    justify-content: stretch;
    gap: 0.8rem;
    margin-top: 1.25rem;
    padding-top: 1.25rem;
  }

  .sound-drawer .sc-field select {
    width: 100%;
    max-width: none;
  }

  .rehearsal-details {
    margin: 0 1rem 1rem;
    border-top: 1px solid var(--stage-hairline);
  }

  .rehearsal-details > summary {
    padding: 1rem 0;
    color: var(--ink-dim);
    font-size: 0.8rem;
    font-weight: 650;
    cursor: pointer;
  }

  .rehearsal-details .wings {
    padding-bottom: 1rem;
  }

  .orchestra-readiness {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    min-height: 1.5rem;
    margin: -0.35rem 0 0.7rem;
    color: var(--ink-dim);
    font-size: 0.74rem;
  }

  .orchestra-readiness strong {
    color: var(--ink-muted);
    white-space: nowrap;
  }

  .readiness-dot {
    width: 0.46rem;
    height: 0.46rem;
    flex: 0 0 auto;
    border-radius: 50%;
    background: #737780;
  }

  .orchestra-readiness.is-loading .readiness-dot {
    background: var(--brass);
    box-shadow: 0 0 0 3px rgba(216, 169, 76, 0.12);
  }

  .orchestra-readiness.is-ready .readiness-dot {
    background: #8fbd78;
  }

  .orchestra-readiness.is-error,
  .orchestra-readiness.is-error strong {
    color: #f0aaa7;
  }

  .orchestra-readiness.is-error .readiness-dot {
    background: #d35854;
  }

  .orchestra-readiness progress {
    width: 4.5rem;
    height: 0.26rem;
    margin-left: auto;
    accent-color: var(--brass);
  }

  .readiness-retry {
    margin-left: auto;
    padding: 0;
    border: 0;
    background: transparent;
    color: currentColor;
    font: inherit;
    font-weight: 750;
    text-decoration: underline;
    text-underline-offset: 0.18em;
    cursor: pointer;
  }

  .readiness-retry:disabled {
    opacity: 0.5;
    cursor: wait;
  }


  .stage {
    background: var(--stage-panel);
    border: 1px solid var(--stage-hairline);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-panel);
    padding: 3rem 2rem 2.25rem;
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    background-image: radial-gradient(ellipse 70% 45% at 50% 0%, rgba(216, 169, 76, 0.07), transparent);
  }

  .stage-eyebrow {
    margin: 0 0 0.5rem;
    font-size: 0.8rem;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-dim);
  }

  .piece-title {
    font-family: 'Fraunces', Georgia, serif;
    font-weight: 600;
    font-size: clamp(2.2rem, 5.5vw, 3.4rem);
    line-height: 1.1;
    margin: 0 0 1.75rem;
    color: var(--ink);
  }

  .state-block {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  .state-word {
    font-family: 'Fraunces', Georgia, serif;
    font-size: clamp(1.6rem, 3.5vw, 2.2rem);
    font-weight: 500;
    letter-spacing: 0.01em;
    color: var(--ink-dim);
    transition: color 0.2s ease;
  }

  .word-playing {
    color: var(--brass-strong);
  }

  .word-recording {
    color: var(--ember-strong);
  }

  .pulse-dot {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: var(--ink-dim);
    flex-shrink: 0;
    transition: background 0.2s ease;
  }

  .dot-playing {
    background: var(--brass);
    box-shadow: 0 0 14px rgba(216, 169, 76, 0.6);
  }

  .dot-recording {
    background: var(--ember);
    box-shadow: 0 0 14px rgba(209, 72, 58, 0.6);
  }

  .pulse-dot.breathing {
    animation: breathe 1.6s ease-in-out infinite;
  }

  @keyframes breathe {
    0%,
    100% {
      opacity: 1;
      transform: scale(1);
    }
    50% {
      opacity: 0.55;
      transform: scale(0.78);
    }
  }

  .state-detail {
    margin: 0.4rem 0 2rem;
    color: var(--ink-dim);
    font-size: 0.85rem;
    min-height: 1.2em;
  }

  .live-recording-card {
    grid-column: 1 / -1;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.85rem 1rem;
    border: 1px solid rgba(127, 174, 103, 0.48);
    border-radius: var(--radius-md);
    background: rgba(127, 174, 103, 0.09);
  }

  .performance-decision-card {
    position: sticky;
    top: 5.5rem;
    z-index: 29;
    background: rgba(24, 31, 25, 0.98);
    box-shadow: 0 14px 38px rgba(0, 0, 0, 0.45);
  }

  .live-recording-card > div:first-child {
    display: grid;
    gap: 0.18rem;
  }

  .live-recording-card span,
  .live-recording-card small {
    color: var(--ink-muted);
    font-size: 0.76rem;
  }

  .live-recording-card strong {
    color: var(--ink);
    font-family: 'Fraunces', Georgia, serif;
  }

  .live-recording-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
  }

  .intent-card {
    position: relative;
    min-width: 0;
    display: grid;
    align-content: start;
    gap: 0.4rem;
    padding: 1rem;
    border: 1px solid var(--stage-hairline-strong);
    border-radius: var(--radius-md);
    background: var(--stage-panel-raised);
    color: var(--ink);
    text-align: left;
    transition: border-color 0.15s ease, background 0.15s ease, transform 0.05s ease;
  }

  .intent-card:hover:not(:disabled) {
    border-color: rgba(233, 189, 99, 0.65);
    background: rgba(216, 169, 76, 0.09);
  }

  .intent-card:active:not(:disabled) {
    transform: scale(0.99);
  }

  .intent-card:disabled {
    opacity: 0.45;
  }

  .intent-card-primary {
    border: 2px solid var(--brass);
    background: rgba(216, 169, 76, 0.1);
  }

  .intent-card.is-active {
    border-color: var(--ready);
    background: rgba(127, 174, 103, 0.1);
  }

  .intent-card strong {
    padding-right: 4.5rem;
    color: var(--ink);
    font-family: 'Fraunces', Georgia, serif;
    font-size: 1.08rem;
  }

  .intent-card > span:not(.intent-kicker):not(.intent-badge),
  .intent-card small {
    color: var(--ink-muted);
    font-size: 0.78rem;
    line-height: 1.4;
  }

  .intent-card small {
    margin-top: 0.3rem;
    color: var(--brass-strong);
    font-weight: 700;
  }

  .intent-kicker {
    color: var(--ink-dim);
    font-size: 0.66rem;
    font-weight: 750;
    letter-spacing: 0.11em;
    text-transform: uppercase;
  }

  .intent-meta {
    padding-top: 0.35rem;
    border-top: 1px solid var(--stage-hairline);
    font-size: 0.72rem !important;
  }

  .intent-badge {
    position: absolute;
    top: 0.8rem;
    right: 0.8rem;
    padding: 0.15rem 0.4rem;
    border: 1px solid var(--stage-hairline-strong);
    border-radius: 999px;
    color: var(--ink-muted);
    font-size: 0.58rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }

  /* Transport: the primary controls, ≥64 px tall, center stage. */
  .transport-row {
    display: flex;
    gap: 1rem;
    flex-wrap: wrap;
    align-items: center;
    justify-content: center;
  }

  .t-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.65rem;
    min-height: 68px;
    padding: 0 2.25rem;
    border-radius: 999px;
    font-size: 1.15rem;
    font-weight: 600;
    letter-spacing: 0.01em;
    border: 1px solid var(--stage-hairline-strong);
    background: var(--stage-panel-raised);
    color: var(--ink);
    transition: transform 0.05s ease, filter 0.15s ease;
  }

  .t-btn:hover:not(:disabled) {
    filter: brightness(1.12);
  }

  .t-btn:active:not(:disabled) {
    transform: scale(0.98);
  }

  .t-btn:disabled {
    opacity: 0.4;
  }

  .t-icon {
    font-size: 1rem;
  }

  .t-play {
    background: var(--brass);
    color: var(--brass-ink);
    border: none;
    padding: 0 2.75rem;
    font-size: 1.25rem;
    box-shadow: 0 10px 30px rgba(216, 169, 76, 0.25);
  }

  .t-record {
    background: transparent;
    color: var(--ember-strong);
    border: 1px solid rgba(209, 72, 58, 0.5);
  }

  .t-record:hover:not(:disabled) {
    background: rgba(209, 72, 58, 0.08);
  }

  .t-record.is-recording {
    background: var(--ember);
    color: white;
    border: none;
    box-shadow: 0 10px 30px rgba(209, 72, 58, 0.3);
  }

  .t-stop {
    background: transparent;
  }

  .rec-dot {
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: currentColor;
    display: inline-block;
    flex-shrink: 0;
  }

  .rec-dot.pulsing {
    animation: breathe 1.1s ease-in-out infinite;
  }

  .key-hints {
    margin: 1rem 0 0;
    font-size: 0.75rem;
    letter-spacing: 0.06em;
    color: var(--ink-dim);
  }

  /* Global listening levels: orchestra and click are the only top-level
     loudness controls; detailed timing stays under Advanced Yamaha timing. */
  .listening-levels {
    display: grid;
    gap: 0.65rem;
    width: min(100%, 560px);
    margin-top: 2.25rem;
  }

  .controls-group-title {
    margin: 0 0 -0.1rem;
    color: var(--ink-muted);
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .compact-controls .global-controls-stage {
    padding-block: 1.35rem;
  }

  .compact-controls .global-controls-stage h2 {
    margin: 0.15rem 0 0;
    font-family: 'Fraunces', Georgia, serif;
  }

  .compact-controls .listening-levels {
    margin-top: 0.9rem;
  }

  .listening-levels .volume-console {
    margin-top: 0;
  }

  .volume-console {
    display: grid;
    grid-template-columns: auto 1fr auto;
    align-items: center;
    gap: 1.25rem;
    width: min(100%, 560px);
    margin: 2.25rem 0 0;
    padding: 1.1rem 1.4rem;
    background: var(--stage-panel-raised);
    border: 1px solid var(--stage-hairline);
    border-radius: 999px;
  }

  .volume-label {
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-muted);
    white-space: nowrap;
  }

  .volume-readout {
    font-variant-numeric: tabular-nums;
    font-weight: 700;
    font-size: 1.15rem;
    color: var(--brass-strong);
    min-width: 3ch;
    text-align: right;
  }

  .tempo-console {
    margin-top: 0.65rem;
  }

  .metronome-input {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    color: var(--brass-strong);
    font-size: 1rem;
    font-weight: 700;
    white-space: nowrap;
  }

  .metronome-input input {
    width: 4.25rem;
    padding: 0.35rem 0.45rem;
    border: 1px solid var(--stage-hairline-strong);
    border-radius: var(--radius-sm);
    background: var(--stage-void);
    color: var(--brass-strong);
    font: inherit;
    font-variant-numeric: tabular-nums;
    text-align: right;
  }

  .tempo-hint {
    width: min(100%, 560px);
    margin: 0.35rem 0 0;
    color: var(--ink-dim);
    font-size: 0.75rem;
    text-align: center;
  }

  .hardware-timing {
    width: min(100%, 560px);
    margin-top: 1rem;
    color: var(--ink-muted);
    font-size: 0.78rem;
  }

  .calibration-panel {
    width: min(100%, 560px);
    margin-top: 0.75rem;
    color: var(--ink-muted);
    font-size: 0.78rem;
  }

  .calibration-title {
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--brass-strong);
  }

  .calibration-console {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.75rem;
    margin: 0.35rem 0 0.25rem;
  }

  .calibration-result {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
  }

  .calibration-result p {
    margin: 0;
  }

  .calibration-badge {
    display: inline-block;
    margin-right: 0.4rem;
    padding: 0.05rem 0.4rem;
    border-radius: 0.4rem;
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    background: var(--stage-hairline, rgba(255, 255, 255, 0.1));
    color: var(--ink-muted);
  }

  .calibration-badge.is-confident {
    background: color-mix(in srgb, var(--ready, #4caf50) 25%, transparent);
    color: var(--ready, #4caf50);
  }

  .hardware-timing summary {
    width: fit-content;
    margin: 0 auto;
    cursor: pointer;
  }

  .hardware-timing-control {
    margin-top: 0.75rem;
  }

  .fader {
    -webkit-appearance: none;
    appearance: none;
    width: 100%;
    height: 32px;
    margin: 0;
    background: transparent;
  }

  .fader::-webkit-slider-runnable-track {
    height: 8px;
    border-radius: 999px;
    background: linear-gradient(
      to right,
      var(--brass) 0%,
      var(--brass) var(--fill, 75%),
      rgba(244, 239, 230, 0.12) var(--fill, 75%),
      rgba(244, 239, 230, 0.12) 100%
    );
  }

  .fader::-webkit-slider-thumb {
    -webkit-appearance: none;
    appearance: none;
    width: 28px;
    height: 28px;
    margin-top: -10px;
    border-radius: 50%;
    background: var(--ink);
    border: 3px solid var(--brass);
    box-shadow: 0 3px 10px rgba(0, 0, 0, 0.5);
    cursor: grab;
  }

  .fader:active::-webkit-slider-thumb {
    cursor: grabbing;
  }

  .fader::-moz-range-track {
    height: 8px;
    border-radius: 999px;
    background: rgba(244, 239, 230, 0.12);
  }

  .fader::-moz-range-progress {
    height: 8px;
    border-radius: 999px;
    background: var(--brass);
  }

  .fader::-moz-range-thumb {
    width: 28px;
    height: 28px;
    border-radius: 50%;
    background: var(--ink);
    border: 3px solid var(--brass);
    box-shadow: 0 3px 10px rgba(0, 0, 0, 0.5);
    cursor: grab;
  }

  /* Sound check: quiet device row at the foot of the stage. */
  .soundcheck {
    display: flex;
    gap: 1.25rem;
    flex-wrap: wrap;
    align-items: end;
    justify-content: center;
    margin-top: 2.25rem;
    padding-top: 1.5rem;
    border-top: 1px solid var(--stage-hairline);
    width: 100%;
  }

  .sc-field {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    text-align: left;
    min-width: 0;
  }

  .sc-label {
    font-size: 0.72rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-dim);
  }

  .sc-field select {
    max-width: 220px;
  }

  .preload-setting {
    display: flex;
    align-items: center;
    gap: 0.65rem;
    min-height: 2.5rem;
    color: var(--ink-muted);
    text-align: left;
    cursor: pointer;
  }

  .preload-setting input {
    width: 1rem;
    height: 1rem;
    margin: 0;
    accent-color: var(--brass);
  }

  .preload-setting span {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
  }

  .preload-setting strong {
    font-size: 0.8rem;
  }

  .preload-setting small {
    color: var(--ink-dim);
    font-size: 0.72rem;
  }

  .stage-hint {
    margin-top: 1rem;
  }

  .bundle-health-hint {
    margin: 0;
    padding: 0.6rem 0.25rem;
    text-align: center;
    font-size: 0.85rem;
    color: var(--ink-dim);
    border-bottom: 1px solid var(--stage-hairline);
  }

  /* ---- Wings ------------------------------------------------------------ */

  .wings {
    display: grid;
    grid-template-columns: 1fr;
    gap: 1.5rem;
  }

  /* Wide screens: the score already spans the full column, so the only thing
     left to lay out is the inspection panel's own contents. The old rule here
     turned `.hall` into a two-column grid and pinned each deck to a numbered
     cell -- that side-by-side cockpit is what made the page too busy to read
     while playing (issue #153), and with the score first it also squeezed the
     engraving into ~60% of the window. */
  @media (min-width: 900px) {
    .inspector .wings {
      grid-template-columns: repeat(2, minmax(0, 1fr));
      align-items: start;
    }

    .inspector .library {
      grid-column: 1 / -1;
    }

    .intent-grid {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }

    .intent-card {
      min-height: 150px;
      padding: 0.8rem;
    }

    .stage .piece-title {
      font-size: 2rem;
      margin-bottom: 1rem;
    }

    .stage .state-word {
      font-size: 1.55rem;
    }

    .stage .state-detail {
      margin-bottom: 1rem;
    }

    .stage .volume-console,
    .stage .soundcheck {
      margin-top: 1.25rem;
    }
  }

  @media (max-width: 640px) {
    .sound-drawer {
      top: 4rem;
      right: 0;
      bottom: 0;
      width: 100vw;
      border-radius: 14px 14px 0 0;
    }
  }

  .deck {
    min-width: 0;
    background: var(--stage-panel);
    border: 1px solid var(--stage-hairline);
    border-radius: var(--radius-lg);
    padding: 1.5rem 1.5rem 1.75rem;
    box-shadow: var(--shadow-panel);
  }

  .score-coordination {
    padding: 1.75rem;
    background-image: radial-gradient(ellipse 90% 30% at 50% 0%, rgba(216, 169, 76, 0.06), transparent);
  }

  .alignment-correction {
    margin-top: 0.75rem;
    padding: 0.75rem 1rem;
    border: 1px solid var(--stage-hairline);
    border-radius: var(--radius-sm);
    color: var(--ink-muted);
  }

  .alignment-correction summary {
    cursor: pointer;
    color: var(--ink);
    font-weight: 650;
  }

  .alignment-correction p {
    max-width: 68ch;
    line-height: 1.45;
  }

  .alignment-correction-target {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
  }

  .alignment-correction-target strong {
    width: 100%;
  }

  .alignment-correction-target .is-selected {
    outline: 2px solid var(--brass-strong);
    color: var(--brass-strong);
  }

  .score-heading-row {
    display: flex;
    justify-content: space-between;
    align-items: start;
    gap: 1.5rem;
    margin-bottom: 1rem;
  }

  .score-heading-actions {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-shrink: 0;
  }

  .performance-toggle {
    white-space: nowrap;
  }

  .score-stage {
    position: relative;
  }

  /* Full-screen affordance where the eye already is: the score's top-right. */
  .score-fullscreen-btn {
    position: absolute;
    top: 0.6rem;
    right: 0.6rem;
    z-index: 6;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 2.3rem;
    height: 2.3rem;
    padding: 0;
    border-radius: 0.55rem;
    border: 1px solid var(--stage-hairline);
    background: color-mix(in srgb, var(--panel, #1c1813) 82%, transparent);
    color: var(--ink-muted);
    cursor: pointer;
    backdrop-filter: blur(4px);
    transition: color 120ms ease, border-color 120ms ease, transform 120ms ease;
  }

  .score-fullscreen-btn:hover {
    color: var(--ink);
    border-color: var(--brass-strong);
    transform: scale(1.05);
  }

  /* Performance view: lift the score out of the deck flow into a full-viewport
     surface so only the page and its live cursor remain. The overlay component
     fits one page to `fitHeightPx` and turns pages automatically. */
  .score-coordination.performance-fullscreen {
    position: fixed;
    inset: 0;
    /* Defeat the sticky/grid rules from the desktop media query so the overlay
       truly fills the viewport instead of inheriting a top offset / max-height. */
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    max-height: none;
    z-index: 1000;
    margin: 0;
    border-radius: 0;
    padding: 0.5rem 1rem;
    background: var(--surface, #14110c);
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  .score-coordination.performance-fullscreen .score-stage {
    flex: 1;
    min-height: 0;
  }

  .score-coordination.performance-fullscreen .score-heading-row {
    margin-bottom: 0.4rem;
    align-items: center;
  }

  .score-kicker {
    margin: 0 0 0.5rem;
    color: var(--brass-strong);
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .score-coverage-stat {
    display: flex;
    flex-direction: column;
    align-items: end;
    flex-shrink: 0;
    color: var(--ink-muted);
    font-size: 0.72rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .score-coverage-stat strong {
    color: var(--ready);
    font-family: 'Fraunces', Georgia, serif;
    font-size: 1.7rem;
    letter-spacing: 0;
  }

  .coverage-details-drawer {
    margin-top: 1rem;
    border-top: 1px solid var(--stage-hairline);
    padding-top: 1rem;
  }

  .coverage-details-drawer summary {
    cursor: pointer;
    color: var(--ink-muted);
    font-size: 0.85rem;
  }

  .deck h2 {
    font-family: 'Fraunces', Georgia, serif;
    font-weight: 600;
    font-size: 1.25rem;
    margin: 0 0 0.9rem;
    color: var(--ink);
  }

  .deck h3,
  .library h3 {
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--ink-muted);
    margin: 1rem 0 0.5rem;
  }

  .deck-subtitle {
    margin: -0.5rem 0 1rem;
    color: var(--ink-muted);
    font-size: 0.9rem;
  }

  .hint {
    color: var(--ink-dim);
    font-size: 0.85rem;
    margin: 0.5rem 0 0;
  }

  .muted {
    color: var(--ink-muted);
  }

  /* ---- Shared controls ---------------------------------------------------- */
  /* .btn/.btn-outline/.btn-ghost/.btn-primary/.btn-round moved to app.css
     (global) so non-App.svelte components -- e.g. PdfCoverageOverlay --
     can reuse the same look; Svelte's per-component style scoping would
     otherwise leave them unstyled there. */

  label.file-button {
    position: relative;
    overflow: hidden;
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    border-radius: var(--radius-md);
    border: 1px solid var(--stage-hairline-strong);
    padding: 0.6rem 0.9rem;
    background: var(--stage-panel-raised);
    cursor: pointer;
    font-weight: 500;
  }

  label.file-button input {
    position: absolute;
    inset: 0;
    opacity: 0;
    cursor: pointer;
  }

  .duration-field {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.85rem;
    color: var(--ink-muted);
    margin-top: 0.75rem;
  }

  .duration-field input[type='number'] {
    width: 4.5rem;
    border-radius: var(--radius-sm);
    border: 1px solid var(--stage-hairline-strong);
    background: var(--stage-panel-raised);
    color: var(--ink);
    padding: 0.4rem 0.5rem;
  }

  select,
  #session-id-input {
    border-radius: var(--radius-sm);
    border: 1px solid var(--stage-hairline-strong);
    background: var(--stage-panel-raised);
    color: var(--ink);
    padding: 0.55rem 0.65rem;
  }

  input[type='range']:not(.fader) {
    accent-color: var(--brass);
    height: 6px;
  }

  .loop-toggle {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.9rem;
    color: var(--ink-muted);
  }

  /* ---- Take deck ----------------------------------------------------------- */

  .capture-secondary-row {
    display: flex;
    justify-content: center;
    gap: 0.6rem;
    flex-wrap: wrap;
    margin-bottom: 1rem;
  }

  .capture-options {
    margin-bottom: 1rem;
  }

  .capture-options summary {
    cursor: pointer;
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--ink-muted);
  }

  .capture-lead-in,
  .capture-recording {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.5rem;
    margin: 1.5rem 0;
    text-align: center;
  }

  .lead-in-word {
    margin: 0;
    font-family: 'Fraunces', Georgia, serif;
    font-weight: 600;
    font-size: 1.1rem;
    color: var(--brass-strong);
  }

  .lead-in-detail {
    margin: 0 0 0.5rem;
    color: var(--ink-muted);
  }

  .mode-chip {
    margin: 0;
    font-size: 0.85rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--ember-strong);
  }

  .elapsed-display {
    margin: 0 0 0.5rem;
    font-variant-numeric: tabular-nums;
    font-size: 1.75rem;
    font-weight: 600;
    color: var(--ink);
  }

  .after-card {
    margin-top: 1.5rem;
    padding: 1.25rem;
    border: 1px solid rgba(216, 169, 76, 0.35);
    border-radius: var(--radius-md);
    background: linear-gradient(145deg, rgba(216, 169, 76, 0.1), var(--stage-panel-raised));
  }

  .after-eyebrow {
    margin: 0 0 0.45rem;
    color: var(--brass-strong);
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .after-heading-row {
    display: flex;
    align-items: start;
    justify-content: space-between;
    gap: 1rem;
  }

  .after-heading-row h3,
  .after-summary,
  .after-progress,
  .after-ready,
  .after-error {
    margin: 0;
  }

  .after-heading-row h3 {
    font-family: 'Fraunces', Georgia, serif;
    font-size: 1.35rem;
  }

  .after-summary,
  .after-progress,
  .after-ready,
  .after-error {
    margin-top: 0.45rem;
    color: var(--ink-muted);
  }

  .after-progress::before {
    content: '';
    display: inline-block;
    width: 0.55rem;
    height: 0.55rem;
    margin-right: 0.45rem;
    border-radius: 50%;
    background: var(--brass);
    animation: breathe 1.2s ease-in-out infinite;
  }

  .after-error {
    color: var(--ember-strong);
  }

  .after-primary-actions,
  .after-secondary-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.65rem;
    margin-top: 1rem;
  }

  .after-primary-actions .t-btn {
    min-width: min(100%, 280px);
  }

  .after-secondary-actions {
    padding-top: 0.85rem;
    border-top: 1px solid var(--stage-hairline);
  }

  .latest-take {
    margin-top: 1.25rem;
    padding-top: 1rem;
    border-top: 1px solid var(--stage-hairline);
  }

  .latest-take-meta {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    margin: 0 0 0.6rem;
  }

  .latest-take-actions {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin-bottom: 0.5rem;
  }

  /* ---- Preview deck ---------------------------------------------------------- */

  .transport-meta {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    margin-bottom: 0.75rem;
  }

  .transport-eyebrow {
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--ink-dim);
  }

  .transport-controls {
    display: flex;
    align-items: center;
    gap: 0.9rem;
    flex-wrap: wrap;
  }

  .scrub {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    flex: 1;
    min-width: 180px;
  }

  .scrub input[type='range'] {
    flex: 1;
  }

  .time {
    font-variant-numeric: tabular-nums;
    font-size: 0.85rem;
    color: var(--ink-muted);
  }

  .preview-file-row {
    margin-top: 1.1rem;
    padding-top: 1rem;
    border-top: 1px solid var(--stage-hairline);
    display: flex;
    gap: 0.6rem;
    flex-wrap: wrap;
  }

  /* ---- Library drawer ---------------------------------------------------------- */

  .library {
    /* The sticky performer score owns the rehearsal viewport, but must yield
       once the user intentionally reaches the below-workstation library. */
    position: relative;
    z-index: 11;
    background: var(--stage-panel);
    border: 1px solid var(--stage-hairline);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-panel);
  }

  .library summary {
    display: flex;
    align-items: baseline;
    gap: 0.9rem;
    padding: 1.25rem 1.5rem;
    cursor: pointer;
    list-style: none;
    user-select: none;
    -webkit-user-select: none;
  }

  .library summary::-webkit-details-marker {
    display: none;
  }

  .library summary::before {
    content: '▸';
    color: var(--ink-dim);
    font-size: 0.85rem;
    transition: transform 0.15s ease;
    align-self: center;
  }

  .library[open] summary::before {
    transform: rotate(90deg);
  }

  .library-title {
    font-family: 'Fraunces', Georgia, serif;
    font-weight: 600;
    font-size: 1.15rem;
    color: var(--ink);
  }

  .library-sub {
    font-size: 0.8rem;
    letter-spacing: 0.04em;
    color: var(--ink-dim);
  }

  .library-body {
    padding: 0.25rem 1.5rem 1.75rem;
    border-top: 1px solid var(--stage-hairline);
  }

  .session-create,
  .session-select {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 0.6rem;
    align-items: end;
    margin: 1rem 0 0;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    font-size: 0.85rem;
    color: var(--ink-muted);
    font-weight: 600;
  }

  .session-file-actions {
    display: flex;
    gap: 0.6rem;
    flex-wrap: wrap;
    margin: 1rem 0;
  }

  .accompanist-status {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    padding-top: 0.75rem;
    border-top: 1px solid var(--stage-hairline);
  }

  .accompanist-status span.ready {
    color: var(--ready);
    font-weight: 600;
  }

  .accompanist-status span:not(.ready) {
    color: var(--ink-dim);
  }

  .playback-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.6rem 0;
    border-bottom: 1px solid var(--stage-hairline);
    gap: 0.75rem;
    flex-wrap: wrap;
  }

  .playback-row small {
    color: var(--ink-dim);
    margin-left: 0.5rem;
  }

  .playback-actions {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }

  .playback-actions a {
    color: var(--brass-strong);
    font-size: 0.9rem;
    text-decoration: none;
  }
  .playback-actions a:hover {
    text-decoration: underline;
  }

  .end-session {
    margin-top: 1.5rem;
    padding-top: 1rem;
    border-top: 1px solid var(--stage-hairline);
  }

  .btn-danger {
    color: var(--ember-strong);
    border-color: var(--ember);
  }

  /* ---- Toast ------------------------------------------------------------------- */

  .toast {
    position: fixed;
    bottom: 24px;
    right: 24px;
    z-index: 40;
    background: var(--stage-panel-raised);
    border: 1px solid var(--stage-hairline-strong);
    color: var(--ink);
    padding: 1rem 1.5rem;
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-panel);
    max-width: min(90vw, 380px);
  }

  /* Stacks above the generic `.toast` so the two never overlap when both
     are visible (design doc §3.1's after-take toast vs. general messages). */
  .toast.capture-toast {
    bottom: 96px;
    border-color: var(--ember);
    transition: border-color 0.2s ease;
  }

  .toast.capture-toast.resolved {
    border-color: var(--ready);
  }

  /* ---- Reduced motion & narrow viewports ------------------------------------------ */

  @media (prefers-reduced-motion: reduce) {
    .pulse-dot.breathing,
    .rec-dot.pulsing {
      animation: none;
    }
    .library summary::before {
      transition: none;
    }
  }

  @media (max-width: 899px) {
    .masthead {
      flex-wrap: wrap;
    }

    .workspace-switcher {
      order: 3;
      width: 100%;
    }

    .workspace-switcher button {
      flex: 1;
      justify-content: center;
    }

    .score-coordination {
      position: sticky;
      top: 5.4rem;
      z-index: 10;
      max-height: 54vh;
      overflow: auto;
      overscroll-behavior: contain;
    }

    .score-coordination .score-context-menu {
      max-height: calc(54vh - 24px);
    }

    .score-coordination .mix-context-menu {
      max-height: calc(100vh - 24px);
    }

    .perform-action-rail,
    .mixing-toolbar {
      flex-wrap: wrap;
    }

    .action-rail-status {
      width: 100%;
      margin-left: 0;
      padding: 0.2rem 0.35rem;
    }
  }

  @media (max-width: 640px) {
    .stage {
      padding: 2rem 1.25rem 1.75rem;
    }
    .t-btn {
      min-height: 60px;
      padding: 0 1.5rem;
      font-size: 1.05rem;
      flex: 1 1 auto;
    }
    .t-play {
      padding: 0 1.75rem;
      font-size: 1.1rem;
    }
    .volume-console {
      gap: 0.85rem;
      padding: 0.9rem 1.1rem;
    }
    .masthead-status {
      display: none;
    }
    .score-heading-row {
      flex-direction: column;
      gap: 0.5rem;
    }
    .score-coverage-stat {
      align-items: start;
    }
    .score-coordination {
      padding: 1.25rem;
    }
    .data-panel-heading {
      align-items: start;
      flex-direction: column;
    }
    .mixing-notation-key {
      display: none;
    }
  }

  @media (max-width: 520px) {
    .silence-btn .silence-label {
      display: none;
    }
    .silence-btn {
      padding: 0.75rem;
    }
    .toast {
      left: 20px;
      right: 20px;
      max-width: none;
    }
  }

  .takes-list-container {
    margin-top: 1.5rem;
    border-top: 1px solid var(--stage-hairline);
    padding-top: 1rem;
  }

  .take-bank-heading {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
  }

  .take-bank-heading h3,
  .take-bank-heading p {
    margin: 0;
  }

  .take-bank-heading p,
  .take-bank-explainer {
    color: var(--ink-muted);
  }

  .take-bank-explainer {
    margin: 0.55rem 0 1rem;
    font-size: 0.84rem;
    line-height: 1.45;
  }

  .takes-scroll-list {
    display: flex;
    flex-direction: column;
    gap: 0.85rem;
    max-height: 430px;
    overflow-y: auto;
    padding-right: 0.25rem;
  }

  .passage-take-group {
    display: grid;
    gap: 0.65rem;
    padding: 0.75rem;
    border: 1px solid var(--stage-hairline-strong);
    border-radius: var(--radius-md);
    background: rgba(244, 239, 230, 0.025);
  }

  .passage-take-group.is-selected {
    border-color: var(--brass);
    box-shadow: inset 3px 0 0 var(--brass);
  }

  .passage-group-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
  }

  .passage-group-heading > div,
  .passage-passes {
    display: grid;
    gap: 0.35rem;
  }

  .passage-group-kicker {
    color: var(--ink-dim);
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .passage-group-heading h4,
  .passage-group-heading p {
    margin: 0;
  }

  .passage-group-heading h4 {
    color: var(--brass-strong);
    font-family: 'Fraunces', Georgia, serif;
    font-size: 1.1rem;
  }

  .passage-group-heading p {
    color: var(--ink-muted);
    font-size: 0.8rem;
  }

  .passage-group-jump {
    flex: 0 0 auto;
  }

  .compact-recording-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 0.55rem;
    align-items: center;
    border-top: 1px solid var(--stage-hairline);
    padding-top: 0.55rem;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }

  .compact-recording-row label,
  .compact-recording-row select {
    width: 100%;
    min-width: 0;
  }

  .compact-recording-actions {
    display: flex;
    gap: 0.3rem;
  }

  .score-context-menu {
    position: absolute;
    z-index: 80;
    box-sizing: border-box;
    width: min(340px, calc(100vw - 24px));
    max-height: calc(100vh - 9rem);
    overflow-y: auto;
    display: grid;
    gap: 0.55rem;
    padding: 0.85rem;
    border: 1px solid var(--brass);
    border-radius: var(--radius-md);
    background: var(--stage-panel-raised);
    box-shadow: 0 18px 50px rgba(0, 0, 0, 0.55);
  }

  .score-context-heading {
    display: grid;
    gap: 0.15rem;
  }

  .score-context-heading span {
    color: var(--brass-strong);
    font-size: 0.72rem;
    font-weight: 750;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .score-context-heading strong {
    color: var(--ink);
    font-family: 'Fraunces', Georgia, serif;
    font-size: 1.05rem;
  }

  .timing-context-heading {
    display: flex;
    align-items: center;
  }

  .timing-context-play {
    width: 100%;
  }

  .score-context-menu p,
  .score-context-menu small {
    margin: 0;
    color: var(--ink-muted);
    line-height: 1.4;
  }

  .score-context-menu p {
    font-size: 0.82rem;
  }

  .score-context-menu small {
    font-size: 0.75rem;
  }

  .score-context-actions {
    display: grid;
    gap: 0.35rem;
    padding-top: 0.35rem;
    border-top: 1px solid var(--stage-hairline);
  }

  .score-context-actions .btn {
    justify-content: flex-start;
    width: 100%;
  }

  .perform-action-rail {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    margin: -0.2rem 0 0.8rem;
    padding: 0.38rem;
    border: 1px solid var(--stage-hairline);
    border-radius: 12px;
    background: rgba(18, 19, 23, 0.52);
  }

  .action-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.44rem 0.7rem;
    border: 1px solid var(--stage-hairline-strong);
    border-radius: 8px;
    background: transparent;
    color: var(--ink-muted);
    font: inherit;
    font-size: 0.76rem;
    font-weight: 700;
    cursor: pointer;
  }

  .action-chip:hover:not(:disabled),
  .action-chip.is-active {
    border-color: rgba(216, 169, 76, 0.52);
    color: var(--ink);
    background: rgba(216, 169, 76, 0.08);
  }

  .action-chip-primary {
    border-color: rgba(127, 174, 103, 0.5);
    background: rgba(127, 174, 103, 0.1);
    color: #a7d394;
  }

  .action-chip:disabled {
    opacity: 0.38;
    cursor: not-allowed;
  }

  .action-rail-status {
    margin-left: auto;
    padding-right: 0.45rem;
    color: var(--ink-dim);
    font-size: 0.72rem;
    white-space: nowrap;
  }

  .data-workspace-panel {
    display: grid;
    align-content: start;
    gap: 0.65rem;
  }

  .data-panel-heading {
    display: flex;
    align-items: end;
    justify-content: space-between;
    gap: 0.5rem;
    padding-bottom: 0.45rem;
    border-bottom: 1px solid var(--stage-hairline);
  }

  .data-panel-heading h2 {
    margin: 0;
  }

  .data-panel-heading h2 {
    color: var(--ink);
    font-family: 'Fraunces', Georgia, serif;
    font-size: 1.35rem;
  }

  .data-facet-tabs {
    display: flex;
    gap: 0.4rem;
    margin: 0 0 0.4rem;
  }

  .data-facet-tab {
    border: 1px solid var(--stage-hairline, rgba(244, 239, 230, 0.12));
    background: var(--stage-panel, #1c1e26);
    color: var(--ink-muted, #aca49b);
    border-radius: var(--radius-sm, 8px);
    padding: 0.35rem 0.75rem;
    font: inherit;
    font-size: 0.82rem;
    cursor: pointer;
  }

  .data-facet-tab.is-active {
    border-color: var(--brass, #d8a94c);
    color: var(--ink, #f4efe6);
    background: var(--stage-panel-raised, #23252f);
  }

  .score-context-assertions {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    margin-top: 0.5rem;
  }

  .assertion {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.1rem;
    text-align: left;
    width: 100%;
  }

  .assertion-label {
    font-weight: 600;
  }

  .assertion-hint {
    font-size: 0.72rem;
    opacity: 0.72;
    font-weight: 400;
    line-height: 1.25;
  }

  .assertion-destructive {
    opacity: 0.78;
  }

  .assertion-destructive:hover {
    opacity: 1;
  }

  .score-context-subheading {
    display: grid;
    gap: 0.12rem;
  }

  .score-context-subheading strong {
    color: var(--brass-strong);
    font-size: 0.78rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .score-context-subheading span,
  .anchor-context-state {
    color: var(--ink-muted);
    font-size: 0.74rem !important;
  }

  .passage-actions-heading {
    padding-top: 0.25rem;
  }

  .btn-anchor-remove {
    color: var(--ember-strong);
    border-color: rgba(209, 72, 58, 0.45);
  }

  .anchor-notice {
    position: absolute;
    z-index: 90;
    width: min(300px, calc(100vw - 24px));
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto;
    align-items: center;
    gap: 0.55rem;
    padding: 0.7rem 0.8rem;
    border: 1px solid var(--brass);
    border-radius: var(--radius-sm);
    background: var(--stage-panel-raised);
    color: var(--ink);
    box-shadow: 0 14px 38px rgba(0, 0, 0, 0.5);
    font-size: 0.8rem;
    line-height: 1.35;
    pointer-events: none;
  }

  .anchor-notice button {
    border: 0;
    background: transparent;
    color: var(--brass-strong);
    cursor: pointer;
    font: inherit;
    font-weight: 750;
    pointer-events: auto;
  }

  .anchor-notice .anchor-notice-close {
    color: var(--ink-dim);
    font-size: 1.1rem;
  }

  .anchor-notice-error {
    border-color: var(--ember);
  }

  .stop-take-btn {
    display: inline-flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.55rem 1.1rem;
    border: 0;
    border-radius: 999px;
    background: var(--ready, #7fae67);
    color: #0d0e11;
    cursor: pointer;
    font: inherit;
    box-shadow: 0 0 0 4px rgba(127, 174, 103, 0.22);
  }

  .stop-take-btn:hover {
    filter: brightness(1.08);
  }

  .stop-take-copy {
    display: grid;
    gap: 0.05rem;
    text-align: left;
  }

  .stop-take-copy strong {
    font-size: 0.95rem;
    font-weight: 750;
    line-height: 1.1;
  }

  .stop-take-copy small {
    font-size: 0.72rem;
    opacity: 0.75;
    font-variant-numeric: tabular-nums;
  }

  .silence-btn-secondary {
    padding: 0.5rem 0.7rem;
    margin-left: 0.5rem;
    opacity: 0.75;
  }

  .icon-action {
    width: 2.35rem;
    height: 2.35rem;
    display: inline-grid;
    place-items: center;
    border: 1px solid var(--stage-hairline-strong);
    border-radius: 50%;
    color: var(--ink);
    background: transparent;
    cursor: pointer;
    font-size: 1rem;
  }

  .icon-action.primary {
    color: var(--stage-void);
    background: var(--brass-strong);
    border-color: var(--brass-strong);
  }

  .icon-action:disabled {
    cursor: not-allowed;
    opacity: 0.4;
  }

  .passage-learning-summary {
    display: grid;
    gap: 0.25rem;
    margin-top: 0.75rem;
    padding: 0.7rem 0.8rem;
    border-left: 3px solid var(--ready);
    background: rgba(132, 177, 108, 0.08);
    color: var(--ink-muted);
    font-size: 0.78rem;
  }

  .passage-learning-summary strong {
    color: var(--ink);
  }

  .passage-recommendation {
    margin-top: 0.2rem;
    color: var(--ink) !important;
    font-weight: 650;
  }

  .alignment-quality {
    display: grid;
    gap: 0.18rem;
    padding: 0.7rem 0.8rem;
    border-left: 3px solid var(--ready);
    background: rgba(127, 174, 103, 0.08);
  }

  .alignment-quality strong {
    color: var(--ink);
  }

  .alignment-quality span,
  .alignment-quality small {
    color: var(--ink-muted);
    font-size: 0.78rem;
    line-height: 1.4;
  }

  .measure-guidance {
    display: grid;
    gap: 0.18rem;
    padding: 0.7rem 0.8rem;
    border-left: 3px solid var(--brass);
    background: rgba(216, 169, 76, 0.08);
  }

  .measure-guidance-covered {
    border-left-color: var(--ready);
    background: rgba(127, 174, 103, 0.08);
  }

  .measure-guidance strong {
    color: var(--ink);
  }

  .measure-guidance span,
  .measure-guidance small {
    color: var(--ink-muted);
    font-size: 0.78rem;
    line-height: 1.4;
  }

  .take-row {
    display: grid;
    gap: 0.7rem;
    padding: 0.85rem;
    background: var(--stage-panel-raised);
    border: 1px solid var(--stage-hairline);
    border-radius: var(--radius-sm);
  }

  .take-card-header {
    display: grid;
    gap: 0.6rem;
  }

  .take-badges {
    display: flex;
    align-items: center;
    justify-content: flex-start;
    gap: 0.6rem;
    flex-wrap: wrap;
  }

  .take-row.discarded {
    opacity: 0.5;
  }

  .take-meta {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    gap: 0.35rem 0.75rem;
    min-width: 0;
  }

  .take-name {
    grid-column: 1 / -1;
    font-weight: 700;
    color: var(--ink);
  }

  .take-time-badge {
    font-size: 0.75rem;
    color: var(--ink-dim);
    background: rgba(244, 239, 230, 0.05);
    padding: 0.15rem 0.4rem;
    border-radius: 4px;
  }

  .take-stats {
    grid-column: 1 / -1;
    font-size: 0.8rem;
    color: var(--ink-muted);
  }

  .take-status-badge {
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    padding: 0.2rem 0.5rem;
    border-radius: 4px;
    letter-spacing: 0.04em;
  }

  .badge-captured {
    background: rgba(216, 169, 76, 0.15);
    color: var(--brass-strong);
  }

  .badge-aligning {
    background: rgba(255, 255, 255, 0.08);
    color: var(--ink-muted);
    animation: pulse-aligning 1.5s infinite ease-in-out;
  }

  .badge-aligned {
    background: rgba(127, 174, 103, 0.15);
    color: var(--ready);
  }

  .badge-ambiguous {
    background: rgba(209, 72, 58, 0.15);
    color: var(--ember-strong);
  }

  .badge-unalignable {
    background: rgba(255, 255, 255, 0.05);
    color: var(--ink-dim);
  }

  .badge-failed {
    background: rgba(209, 72, 58, 0.15);
    color: var(--ember-strong);
  }

  .badge-discarded {
    background: rgba(0, 0, 0, 0.2);
    color: var(--ink-dim);
  }

  @keyframes pulse-aligning {
    0%, 100% { opacity: 0.6; }
    50% { opacity: 1; }
  }

  .take-actions {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    flex-wrap: wrap;
  }

  .learning-badge {
    padding: 0.2rem 0.5rem;
    border-radius: 4px;
    background: rgba(127, 174, 103, 0.15);
    color: var(--ready);
    font-size: 0.72rem;
    font-weight: 650;
  }

  .learning-badge.pending {
    background: rgba(216, 169, 76, 0.15);
    color: var(--brass-strong);
  }

  .learning-badge.excluded {
    background: rgba(255, 255, 255, 0.05);
    color: var(--ink-dim);
  }

  .take-resolve-row {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    padding: 0.6rem 0.75rem;
    margin-top: -0.35rem;
    background: rgba(209, 72, 58, 0.08);
    border: 1px solid var(--stage-hairline);
    border-top: none;
    border-radius: 0 0 var(--radius-sm) var(--radius-sm);
  }

  .resolve-label {
    font-size: 0.8rem;
    color: var(--ink-muted);
  }

  .resolve-choices {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
  }

  .resolve-choice {
    padding: 0.4rem 0.7rem;
    font-size: 0.85rem;
    flex-direction: column;
    gap: 0.1rem;
  }

  .resolve-beat {
    font-size: 0.7rem;
    color: var(--ink-dim);
    font-weight: 400;
  }

  .btn-icon {
    width: 32px;
    height: 32px;
    padding: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 0.9rem;
    border-radius: var(--radius-sm);
  }

  .coverage-summary {
    display: flex;
    align-items: center;
    gap: 1.5rem;
    padding: 1rem;
    background: var(--stage-panel-raised);
    border: 1px solid var(--stage-hairline);
    border-radius: var(--radius-md);
    margin-bottom: 1.5rem;
  }

  .coverage-percentage {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    border-right: 1px solid var(--stage-hairline-strong);
    padding-right: 1.5rem;
  }

  .pct-val {
    font-size: 2.2rem;
    font-weight: 800;
    font-family: 'Fraunces', Georgia, serif;
    color: var(--ready);
  }

  .pct-label {
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--ink-dim);
    margin-top: -0.25rem;
  }

  .coverage-details {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    flex: 1;
  }

  .stat-main {
    font-size: 1.05rem;
    color: var(--ink);
  }

  .stat-breakdown {
    font-size: 0.82rem;
    color: var(--ink-muted);
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.3rem;
  }

  .badge-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
    flex-shrink: 0;
  }

  .dot-covered {
    background-color: var(--ready);
  }

  .dot-touched {
    background-color: var(--brass);
  }

  .dot-uncovered {
    background-color: var(--ink-dim);
  }

  .coverage-grid-container {
    margin-top: 1rem;
  }

  .coverage-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(40px, 1fr));
    gap: 0.35rem;
    max-height: 250px;
    overflow-y: auto;
    padding-right: 0.25rem;
  }

  .measure-block {
    aspect-ratio: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: var(--radius-sm);
    border: 1px solid var(--stage-hairline);
    font-size: 0.78rem;
    font-weight: 700;
    cursor: pointer;
    padding: 0;
    user-select: none;
    transition: transform 0.15s ease, filter 0.15s ease;
  }

  .measure-block.measure-selected {
    outline: 2px solid var(--brass-strong);
    outline-offset: 2px;
  }

  .next-take-target {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 1rem;
    margin-top: 1rem;
    padding: 0.85rem 1rem;
    border: 1px solid var(--brass);
    border-radius: var(--radius-md);
    background: rgba(216, 169, 76, 0.08);
  }

  .next-take-target > div {
    display: grid;
    gap: 0.1rem;
  }

  .next-take-target span,
  .next-take-target small,
  .target-hint {
    color: var(--ink-muted);
  }

  .next-take-target span {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.09em;
  }

  .next-take-target strong {
    color: var(--brass-strong);
    font-family: 'Fraunces', Georgia, serif;
    font-size: 1.2rem;
  }

  .next-take-target .target-score-link {
    color: var(--brass-strong);
    font-weight: 700;
  }

  .recording-plan {
    gap: 0.3rem !important;
    padding: 0.75rem 0.85rem;
    border-left: 3px solid var(--ready);
    border-radius: var(--radius-sm);
    background: rgba(127, 174, 103, 0.09);
  }

  .recording-plan span {
    color: var(--ready);
  }

  .recording-plan strong {
    color: var(--ink);
    font-family: inherit;
    font-size: 0.92rem;
  }

  .recording-plan small {
    line-height: 1.45;
  }

  .passage-record-control {
    width: min(100%, 390px);
  }

  .cue-switch {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.65rem;
    padding: 0.65rem 0.75rem;
    border: 1px solid var(--stage-hairline-strong);
    border-radius: var(--radius-sm);
    background: rgba(13, 14, 17, 0.22);
    cursor: pointer;
  }

  .cue-switch input {
    width: 1.15rem;
    height: 1.15rem;
    accent-color: var(--brass-strong);
  }

  .cue-switch-copy {
    display: grid;
    gap: 0.12rem;
    min-width: 0;
    text-transform: none !important;
    letter-spacing: normal !important;
  }

  .cue-switch-copy strong {
    color: var(--ink);
    font-family: inherit;
    font-size: 0.92rem;
  }

  .cue-switch-copy small {
    color: var(--ink-muted);
    font-size: 0.76rem;
    line-height: 1.35;
  }

  .cue-switch-state {
    color: var(--ink-dim);
    font-size: 0.75rem !important;
    font-weight: 700;
  }

  .cue-switch-state.enabled {
    color: var(--ready);
  }

  .passage-record-button {
    width: 100%;
    min-height: 48px;
    justify-content: center;
  }

  .passage-review-card {
    display: grid;
    gap: 0.8rem;
    margin-top: 0.85rem;
    padding: 1rem;
    border: 1px solid var(--stage-hairline-strong);
    border-radius: var(--radius-md);
    background: var(--stage-panel-raised);
  }

  .passage-review-heading,
  .review-output-group {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    flex-wrap: wrap;
  }

  .passage-review-heading {
    justify-content: space-between;
  }

  .passage-review-heading > div {
    display: grid;
    gap: 0.1rem;
  }

  .passage-review-heading span,
  .review-output-group > span,
  .review-take-picker,
  .passage-review-card small {
    color: var(--ink-muted);
    font-size: 0.78rem;
  }

  .passage-review-heading strong {
    color: var(--ink);
    font-family: 'Fraunces', Georgia, serif;
    font-size: 1.15rem;
  }

  .review-take-picker {
    display: grid;
    gap: 0.4rem;
    width: 100%;
  }

  .review-take-picker select {
    width: 100%;
    min-width: 0;
  }

  .recording-options {
    padding-top: 0.65rem;
    border-top: 1px solid var(--stage-hairline);
  }

  .recording-options summary {
    color: var(--ink-muted);
    cursor: pointer;
    font-size: 0.8rem;
  }

  .recording-option-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
    margin: 0.65rem 0 0.45rem;
  }

  .passage-review-summary {
    margin: 0;
    color: var(--ink-muted);
    line-height: 1.45;
  }

  .review-output-group > span {
    width: 100%;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }

  .review-output-group.secondary {
    padding-top: 0.65rem;
    border-top: 1px solid var(--stage-hairline);
  }

  .target-hint {
    margin: 0.75rem 0 0;
  }

  .measure-block:hover {
    transform: scale(1.08);
    filter: brightness(1.15);
  }

  .block-covered {
    background: rgba(127, 174, 103, 0.2);
    color: var(--ready);
    border-color: rgba(127, 174, 103, 0.4);
  }

  .block-touched {
    background: rgba(216, 169, 76, 0.15);
    color: var(--brass-strong);
    border-color: rgba(216, 169, 76, 0.3);
  }

  .block-uncovered {
    background: rgba(255, 255, 255, 0.03);
    color: var(--ink-dim);
    border-color: var(--stage-hairline);
  }

  .block-tutti {
    background: repeating-linear-gradient(
      45deg,
      transparent,
      transparent 4px,
      rgba(255, 255, 255, 0.03) 4px,
      rgba(255, 255, 255, 0.03) 8px
    );
    color: rgba(244, 239, 230, 0.25);
    border-color: rgba(244, 239, 230, 0.05);
  }

  .measure-num {
    font-variant-numeric: tabular-nums;
  }

  .mix-masthead-identity {
    display: grid;
    gap: 0.08rem;
    margin-left: auto;
    color: #8ee5e7;
    text-align: right;
  }

  .mix-masthead-identity strong {
    font-size: 0.84rem;
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }

  .mix-masthead-identity span {
    color: var(--ink-muted);
    font-size: 0.68rem;
  }

  .mixing-toolbar {
    display: flex;
    align-items: center;
    gap: 0.7rem;
    min-height: 2rem;
    margin-bottom: 0.45rem;
  }

  .mixing-notation-key {
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    color: var(--ink-dim);
    font-size: 0.7rem;
  }

  .notation-line {
    display: inline-block;
    width: 2rem;
    height: 0.55rem;
    border-top: 2px solid #78d6d8;
    border-left: 2px solid #78d6d8;
    transform: skewY(-8deg);
  }

  .mix-score-stale {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 0.8rem;
    padding: 0.75rem 1rem;
    border: 1px solid rgba(239, 207, 139, 0.45);
    border-radius: 10px;
    background: rgba(91, 66, 27, 0.34);
  }

  .mix-score-stale div {
    display: grid;
    gap: 0.2rem;
  }

  .mix-score-stale span {
    color: var(--ink-muted);
    font-size: 0.75rem;
  }

  .mix-score-instruction span {
    color: var(--ink-muted);
    font-size: 0.75rem;
  }

  .mix-menu-level output {
    color: #8ee5e7;
    font-variant-numeric: tabular-nums;
    text-align: right;
  }

  .mix-undo {
    margin-left: auto;
  }

  .mix-score-instruction {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.65rem;
    margin: 0 0 0.8rem;
  }

  .mix-score-instruction strong {
    color: var(--brass-strong);
    font-size: 0.78rem;
  }

  .mix-modal-scrim {
    position: fixed;
    z-index: 79;
    inset: 0;
    width: 100%;
    height: 100%;
    padding: 0;
    border: 0;
    background: rgba(4, 6, 8, 0.48);
    backdrop-filter: blur(2px);
    cursor: default;
  }

  .mix-context-menu {
    position: fixed;
    left: 50% !important;
    top: 50% !important;
    transform: translate(-50%, -50%);
    display: grid;
    gap: 0.75rem;
    width: min(420px, calc(100vw - 24px));
    max-height: min(680px, calc(100vh - 32px));
    border-color: rgba(142, 229, 231, 0.56);
    box-shadow: 0 26px 90px rgba(0, 0, 0, 0.72);
  }

  .mix-context-menu label {
    display: grid;
    gap: 0.3rem;
    color: var(--ink-muted);
    font-size: 0.75rem;
  }

  .mix-context-menu select {
    width: 100%;
  }

  .mix-menu-grid {
    display: grid;
    grid-template-columns: 0.9fr 1.25fr;
    gap: 0.6rem;
  }

  .mix-menu-stem {
    grid-column: 1 / -1;
  }

  .mix-point-actions {
    display: grid;
    gap: 0.45rem;
  }

  .mix-point-actions .btn {
    justify-content: flex-start;
  }

  .mix-point-menu {
    width: min(320px, calc(100vw - 24px));
  }

  .mix-point-menu > small {
    color: var(--ink-dim);
    font-size: 0.7rem;
    line-height: 1.4;
  }

  .mix-interpolation-note {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    padding: 0.48rem 0.6rem;
    border-radius: 8px;
    background: rgba(142, 229, 231, 0.06);
    color: var(--ink-muted) !important;
    font-size: 0.72rem !important;
  }

  .mix-interpolation-note .notation-line {
    flex: none;
    width: 1.65rem;
  }

  .mix-menu-level {
    grid-template-columns: 105px 1fr 3rem;
    align-items: center;
  }

  .mix-menu-level > span {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    color: var(--ink);
    font-weight: 650;
  }

  .endpoint-dot {
    display: inline-block;
    width: 0.55rem;
    height: 0.55rem;
    border: 2px solid #78d6d8;
    border-radius: 50%;
    background: var(--stage-panel-raised);
  }

  .endpoint-dot-end {
    background: #78d6d8;
  }

  .mix-enabled-control {
    display: flex !important;
    grid-template-columns: none !important;
    align-items: center;
  }

  .mix-zone-warning {
    margin: 0;
    color: #efcf8b;
    font-size: 0.76rem;
  }

  .cockpit.mix-authoring .score-coordination {
    border-color: rgba(142, 229, 231, 0.24);
  }
</style>
