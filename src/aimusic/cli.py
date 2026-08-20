"""Command-line tools for Rubato local rehearsal."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from aimusic.accompaniment.live_midi import (
    inspect_midi_cue,
    panic,
    play_midi,
    play_midi_cue,
    record_midi,
)
from aimusic.accompaniment.midi_ports import list_midi_ports
from aimusic.accompaniment.offline_alignment import (
    AlignmentResult,
    write_alignment_artifacts,
)
from aimusic.accompaniment.offline_render import align_midi_inputs, render_offline_midi
from aimusic.accompaniment.oguri import (
    DEFAULT_ORCHESTRA_VOLUME,
    PIANO_TRACK_MARKER,
    oguri_movement,
)
from aimusic.accompaniment.oguri_extract import extract_oguri_movement
from aimusic.audio.live_config import (
    LiveVstZoneConfig,
    VstInstrumentBinding,
    load_live_audio_config,
    upsert_live_vst_zone,
)
from aimusic.audio.orchestra_live_audition import play_oguri_bbcso_live
from aimusic.audio.orchestra_render import (
    default_plugin_state_dir,
    render_oguri_bbcso_orchestra,
    score_cue_window,
)
from aimusic.audio.plugin_host import (
    DEFAULT_BBCSO_VST3_PATH,
    audio_output_devices,
    capture_plugin_state,
    play_audio_file_default,
)
from aimusic.audio.reaper_setup import (
    DEFAULT_REAPER_APP,
    DEFAULT_REAPER_MIDI_PORT,
    create_reaper_project,
    default_reaper_heartbeat_path,
    default_reaper_project_path,
    install_reaper_bridge,
    install_reaper_startup,
    migrate_zone_to_reaper,
)
from aimusic.core import paths
from aimusic.takes.migration import inventory_tree, migrate_tree


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rubato")
    subparsers = parser.add_subparsers(required=True)

    devices = subparsers.add_parser("devices", help="List MIDI input/output ports")
    devices.set_defaults(func=_devices)

    record = subparsers.add_parser("record", help="Record a MIDI take from a hardware input")
    record.add_argument("--in", dest="input_name", required=True, help="MIDI input port name")
    record.add_argument("--session", default=None, help="Session/run id for the saved take")
    record.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="Optional fixed recording duration",
    )
    record.add_argument("--out", dest="output_path", default=None, help="Explicit output .mid path")
    record.set_defaults(func=_record)

    play = subparsers.add_parser("play-oguri", help="Play an Oguri orchestra-only movement")
    play.add_argument("--movement", type=int, default=2, choices=[2])
    play.add_argument("--out", dest="output_name", required=True, help="MIDI output port name")
    play.add_argument("--volume", type=float, default=DEFAULT_ORCHESTRA_VOLUME)
    play.add_argument("--seconds", type=float, default=None, help="Optional max playback seconds")
    play.add_argument("--include-piano", action="store_true", help="Do not suppress PIANO SOLO")
    play.set_defaults(func=_play_oguri)

    inspect_cue = subparsers.add_parser(
        "inspect-oguri-cue",
        help="Print event counts for an Oguri orchestra cue without playing it",
    )
    inspect_cue.add_argument("--movement", type=int, default=2, choices=[2])
    inspect_cue.add_argument("--cue-seconds", type=float, default=None)
    inspect_cue.add_argument("--release-tail-seconds", type=float, default=2.0)
    inspect_cue.add_argument("--volume", type=float, default=DEFAULT_ORCHESTRA_VOLUME)
    inspect_cue.add_argument(
        "--include-piano",
        action="store_true",
        help="Do not suppress PIANO SOLO",
    )
    inspect_cue.set_defaults(func=_inspect_oguri_cue)

    play_cue = subparsers.add_parser(
        "play-oguri-cue",
        help="Play an Oguri orchestra cue ending at the first piano entry",
    )
    play_cue.add_argument("--movement", type=int, default=2, choices=[2])
    play_cue.add_argument("--out", dest="output_name", required=True, help="MIDI output port name")
    play_cue.add_argument("--cue-seconds", type=float, default=None)
    play_cue.add_argument("--release-tail-seconds", type=float, default=2.0)
    play_cue.add_argument("--volume", type=float, default=DEFAULT_ORCHESTRA_VOLUME)
    play_cue.add_argument("--include-piano", action="store_true", help="Do not suppress PIANO SOLO")
    play_cue.set_defaults(func=_play_oguri_cue)

    audio_devices = subparsers.add_parser(
        "audio-devices",
        help="List Pedalboard/CoreAudio output devices",
    )
    audio_devices.set_defaults(func=_audio_devices)

    capture_state = subparsers.add_parser(
        "capture-bbcso-state",
        help="Open BBCSO, choose a patch, and save reusable plug-in state",
    )
    capture_state.add_argument(
        "--plugin",
        type=Path,
        default=DEFAULT_BBCSO_VST3_PATH,
        help="BBCSO VST3 bundle path",
    )
    capture_state.add_argument("--out", type=Path, required=True, help="State file to write")
    capture_state.add_argument(
        "--from-state",
        type=Path,
        default=None,
        help="Open from a captured state so Next/Previous can select an adjacent preset",
    )
    capture_state.set_defaults(func=_capture_bbcso_state)

    orchestra = subparsers.add_parser(
        "audition-bbcso-orchestra",
        help="Render Oguri orchestral parts through matching BBCSO patches",
    )
    orchestra.add_argument("--movement", type=int, default=2, choices=[2])
    orchestra.add_argument("--start-measure", type=int, default=43)
    orchestra.add_argument(
        "--end-measure",
        type=int,
        default=46,
        help="Exclusive printed-measure boundary (46 renders measures 43-45)",
    )
    orchestra.add_argument(
        "--plugin", type=Path, default=DEFAULT_BBCSO_VST3_PATH, help="BBCSO VST3 bundle"
    )
    orchestra.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="Captured BBCSO state directory (defaults to Rubato machine state)",
    )
    orchestra.add_argument(
        "--wav-out",
        type=Path,
        default=Path("/tmp/rubato-bbcso-orchestra-m43-45.wav"),
    )
    orchestra.add_argument("--volume", type=float, default=DEFAULT_ORCHESTRA_VOLUME)
    orchestra.add_argument("--mix-gain", type=float, default=0.65)
    orchestra.add_argument(
        "--play-default",
        action="store_true",
        help="Play the completed WAV through the current macOS default output",
    )
    orchestra.set_defaults(func=_audition_bbcso_orchestra)

    live_orchestra = subparsers.add_parser(
        "play-bbcso-live",
        help="Preload BBCSO instruments and stream an Oguri MIDI cue to CoreAudio",
    )
    live_orchestra.add_argument("--movement", type=int, default=2, choices=[2])
    live_orchestra.add_argument("--start-measure", type=int, default=43)
    live_orchestra.add_argument("--end-measure", type=int, default=46)
    live_orchestra.add_argument(
        "--device",
        default=None,
        help="Exact CoreAudio output name (defaults to the current output device)",
    )
    live_orchestra.add_argument("--plugin", type=Path, default=DEFAULT_BBCSO_VST3_PATH)
    live_orchestra.add_argument("--state-dir", type=Path, default=None)
    live_orchestra.add_argument("--volume", type=float, default=DEFAULT_ORCHESTRA_VOLUME)
    live_orchestra.add_argument("--mix-gain", type=float, default=0.65)
    live_orchestra.add_argument("--block-size", type=int, default=512)
    live_orchestra.set_defaults(func=_play_bbcso_live)

    configure_vst = subparsers.add_parser(
        "configure-live-vst-zone",
        help="Bind a named mix zone/stem to a live BBCSO patch and CoreAudio output",
    )
    configure_vst.add_argument("--zone-id", default="room_center")
    configure_vst.add_argument("--label", default="BBCSO · room center")
    configure_vst.add_argument("--device", required=True, help="Exact CoreAudio output name")
    configure_vst.add_argument(
        "--position", default="wide center soundbar", help="Human room-position label"
    )
    configure_vst.add_argument("--instrument-id", required=True)
    configure_vst.add_argument(
        "--stem",
        action="append",
        required=True,
        help="Score part/stem rendered by this patch; repeatable",
    )
    configure_vst.add_argument(
        "--plugin", type=Path, default=DEFAULT_BBCSO_VST3_PATH, help="BBCSO VST3 bundle"
    )
    configure_vst.add_argument(
        "--plugin-state", type=Path, required=True, help="Captured BBCSO patch state"
    )
    configure_vst.add_argument("--midi-channel", type=int, default=0)
    configure_vst.add_argument("--advance-ms", type=float, default=59.0)
    configure_vst.add_argument("--residual-p95-ms", type=float, required=True)
    configure_vst.add_argument("--tolerance-ms", type=float, default=40.0)
    configure_vst.add_argument("--calibration-revision", required=True)
    configure_vst.add_argument("--sample-rate", type=float, default=48_000.0)
    configure_vst.add_argument("--block-size", type=int, default=512)
    configure_vst.add_argument("--prefill-blocks", type=int, default=2)
    configure_vst.set_defaults(func=_configure_live_vst_zone)

    configure_reaper = subparsers.add_parser(
        "configure-live-reaper-zone",
        help="Migrate an existing four-group live zone from Pedalboard to REAPER",
    )
    configure_reaper.add_argument("--zone-id", default="room_center")
    configure_reaper.add_argument("--app", type=Path, default=DEFAULT_REAPER_APP)
    configure_reaper.add_argument("--project", type=Path, default=default_reaper_project_path())
    configure_reaper.add_argument("--heartbeat", type=Path, default=default_reaper_heartbeat_path())
    configure_reaper.add_argument("--midi-port", default=DEFAULT_REAPER_MIDI_PORT)
    configure_reaper.set_defaults(func=_configure_live_reaper_zone)

    extract = subparsers.add_parser(
        "extract-oguri",
        help="Extract Oguri solo-reference and orchestra MIDI files",
    )
    extract.add_argument("--movement", type=int, default=2, choices=[2])
    extract.set_defaults(func=_extract_oguri)

    align = subparsers.add_parser(
        "align-midi",
        help="Align a performed solo MIDI file against a reference solo MIDI file",
    )
    align.add_argument("--reference", required=True, help="Reference solo MIDI path")
    align.add_argument("--performance", required=True, help="Performed solo MIDI path")
    align.add_argument("--run-id", required=True, help="Run id for trace/analysis outputs")
    align.add_argument(
        "--reference-track-marker",
        default=None,
        help="Optional track-name substring for selecting reference notes",
    )
    align.set_defaults(func=_align_midi)

    render = subparsers.add_parser(
        "render-offline",
        help="Align solo MIDI and retime accompaniment MIDI into a run output",
    )
    render.add_argument("--reference", required=True, help="Reference solo MIDI path")
    render.add_argument("--performance", required=True, help="Performed solo MIDI path")
    render.add_argument("--accompaniment", required=True, help="Accompaniment MIDI path")
    render.add_argument("--run-id", required=True, help="Run id for trace/analysis/output")
    render.add_argument(
        "--reference-track-marker",
        default=None,
        help="Optional track-name substring for selecting reference notes",
    )
    render.set_defaults(func=_render_offline)

    stop = subparsers.add_parser("panic", help="Send all-notes-off/all-sound-off")
    stop.add_argument("--out", dest="output_name", required=True, help="MIDI output port name")
    stop.set_defaults(func=_panic)

    migrate = subparsers.add_parser(
        "migrate-takes-v2",
        help="Plan or apply non-destructive take lifecycle v2 sidecar migration",
    )
    migrate.add_argument(
        "--root",
        default=None,
        help="Takes root (defaults to the configured AIMUSIC_DATA_ROOT/takes)",
    )
    migrate.add_argument(
        "--apply",
        action="store_true",
        help="Write take.v2.json/aligned.v2.json; default is dry-run",
    )
    migrate.add_argument("--canonical-ppq", type=int, default=960)
    migrate.set_defaults(func=_migrate_takes_v2)

    return parser


def _devices(_args: argparse.Namespace) -> int:
    ports = list_midi_ports()
    print("inputs:")
    for name in ports.inputs:
        print(f"  - {name}")
    print("outputs:")
    for name in ports.outputs:
        print(f"  - {name}")
    return 0


def _audio_devices(_args: argparse.Namespace) -> int:
    backend, default_output, outputs = audio_output_devices()
    print(f"backend={backend}")
    print(f"default_output={default_output}")
    print("outputs:")
    for name in outputs:
        print(f"  - {name}")
    if not outputs:
        print("  (none detected)")
    return 0


def _capture_bbcso_state(args: argparse.Namespace) -> int:
    initial_state = args.from_state.expanduser().resolve() if args.from_state is not None else None
    output = capture_plugin_state(
        args.plugin.expanduser(),
        args.out.expanduser().resolve(),
        initial_state_path=initial_state,
    )
    print(f"plugin_state={output}")
    return 0


def _audition_bbcso_orchestra(args: argparse.Namespace) -> int:
    movement = oguri_movement(args.movement)
    state_dir = (
        args.state_dir.expanduser().resolve()
        if args.state_dir is not None
        else default_plugin_state_dir()
    )
    window = score_cue_window(
        start_measure=args.start_measure,
        end_measure_exclusive=args.end_measure,
        movement=args.movement,
    )
    summary = render_oguri_bbcso_orchestra(
        movement.local_path,
        args.wav_out.expanduser().resolve(),
        window=window,
        state_dir=state_dir,
        plugin_path=args.plugin.expanduser().resolve(),
        volume=args.volume,
        mix_gain=args.mix_gain,
    )
    print(f"source_start_seconds={window.source_start_seconds:.3f}")
    print(f"source_end_seconds={window.source_end_seconds:.3f}")
    for stem in summary.stems:
        print(
            f"stem={stem.track_name} note_ons={stem.note_on_count} "
            f"events={stem.event_count} peak={stem.peak:.6f}"
        )
    print(f"note_on_count={summary.note_on_count}")
    print(f"peak={summary.peak:.6f}")
    print(f"wav={summary.output_path}")
    if args.play_default:
        play_audio_file_default(summary.output_path)
    return 0


def _play_bbcso_live(args: argparse.Namespace) -> int:
    movement = oguri_movement(args.movement)
    state_dir = (
        args.state_dir.expanduser().resolve()
        if args.state_dir is not None
        else default_plugin_state_dir()
    )
    device = args.device
    if device is None:
        _backend, device, _outputs = audio_output_devices()
    if device is None:
        raise RuntimeError("CoreAudio has no default output device")
    window = score_cue_window(
        start_measure=args.start_measure,
        end_measure_exclusive=args.end_measure,
        movement=args.movement,
    )
    print(f"output_device={device}", flush=True)
    summary = play_oguri_bbcso_live(
        movement.local_path,
        window=window,
        output_device_name=device,
        state_dir=state_dir,
        plugin_path=args.plugin.expanduser().resolve(),
        volume=args.volume,
        mix_gain=args.mix_gain,
        block_size=args.block_size,
        progress=lambda message: print(message, flush=True),
    )
    print(f"note_count={summary.note_count}")
    print(f"duration_seconds={summary.duration_seconds:.3f}")
    print(f"load_seconds={summary.load_seconds:.3f}")
    return 0


def _configure_live_vst_zone(args: argparse.Namespace) -> int:
    current = load_live_audio_config()
    prior = next((zone for zone in current.zones if zone.zone_id == args.zone_id), None)
    binding = VstInstrumentBinding(
        instrument_id=args.instrument_id,
        stem_ids=tuple(args.stem),
        plugin_path=args.plugin.expanduser().resolve(),
        plugin_state_path=args.plugin_state.expanduser().resolve(),
        midi_channel=args.midi_channel,
    )
    instruments = (
        tuple(item for item in prior.instruments if item.instrument_id != binding.instrument_id)
        if prior is not None
        else ()
    ) + (binding,)
    zone = LiveVstZoneConfig(
        zone_id=args.zone_id,
        label=args.label,
        output_device_name=args.device,
        acoustic_position=args.position,
        configured_output_advance_ms=args.advance_ms,
        residual_error_p95_ms=args.residual_p95_ms,
        timing_tolerance_ms=args.tolerance_ms,
        calibration_revision=args.calibration_revision,
        sample_rate=args.sample_rate,
        block_size=args.block_size,
        prefill_blocks=args.prefill_blocks,
        instruments=instruments,
    )
    upsert_live_vst_zone(zone)
    print(f"zone={zone.zone_id}")
    print(f"device={zone.output_device_name}")
    print(f"instrument={binding.instrument_id}")
    print(f"stems={','.join(binding.stem_ids)}")
    print(f"config={paths.live_audio_config_path()}")
    return 0


def _configure_live_reaper_zone(args: argparse.Namespace) -> int:
    current = load_live_audio_config()
    prior = next((zone for zone in current.zones if zone.zone_id == args.zone_id), None)
    if prior is None:
        raise RuntimeError(
            f"Live zone {args.zone_id!r} does not exist; configure its instrument bindings first"
        )
    app = args.app.expanduser().resolve()
    if not app.exists():
        raise FileNotFoundError(f"REAPER is missing: {app}")
    project = create_reaper_project(args.project.expanduser().resolve())
    zone = migrate_zone_to_reaper(
        prior,
        app_path=app,
        project_path=project,
        heartbeat_path=args.heartbeat.expanduser().resolve(),
        midi_port_name=args.midi_port,
    )
    config_path = paths.live_audio_config_path()
    backup_path = config_path.with_name("live-audio-zones.pedalboard-backup.json")
    if prior.renderer == "pedalboard" and config_path.exists() and not backup_path.exists():
        shutil.copy2(config_path, backup_path)
    upsert_live_vst_zone(zone)
    bridge = install_reaper_bridge(
        paths.project_root() / "reaper" / "rubato_reaper_bridge.lua"
    )
    startup = install_reaper_startup(paths.project_root() / "reaper" / "__startup.lua")
    print(f"zone={zone.zone_id}")
    print(f"renderer={zone.renderer}")
    print(f"midi_port={zone.midi_port_name}")
    print(f"project={zone.reaper_project_path}")
    print(f"heartbeat={zone.reaper_heartbeat_path}")
    print(f"bridge={bridge}")
    print(f"startup={startup}")
    print(f"config={config_path}")
    if backup_path.exists():
        print(f"pedalboard_backup={backup_path}")
    return 0


def _record(args: argparse.Namespace) -> int:
    output_path = _record_output_path(args.session, args.output_path)
    try:
        summary = record_midi(
            args.input_name,
            output_path,
            duration_seconds=args.seconds,
        )
    except KeyboardInterrupt:
        print("recording interrupted", file=sys.stderr)
        return 130
    print(
        f"recorded {summary.message_count} MIDI messages "
        f"over {summary.duration_seconds:.2f}s to {summary.path}"
    )
    return 0


def _record_output_path(session_id: str | None, output_path: str | None) -> Path:
    if output_path:
        return Path(output_path).expanduser().resolve()
    session = session_id or "manual_take"
    return paths.run_input_dir(session) / "solo.mid"


def _play_oguri(args: argparse.Namespace) -> int:
    movement = oguri_movement(args.movement)
    skip = () if args.include_piano else (PIANO_TRACK_MARKER,)
    summary = play_midi(
        movement.local_path,
        args.output_name,
        volume=args.volume,
        max_seconds=args.seconds,
        skip_track_name_contains=skip,
    )
    print(
        f"played {summary.event_count} events from {summary.source_path} "
        f"for {summary.duration_seconds:.2f}s at volume={summary.volume:.2f}"
    )
    return 0


def _cue_args(args: argparse.Namespace):
    movement = oguri_movement(args.movement)
    cue_seconds = args.cue_seconds or movement.default_cue_seconds
    cue_start = max(0.0, movement.first_solo_entry_seconds - cue_seconds)
    skip = () if args.include_piano else (PIANO_TRACK_MARKER,)
    return movement, cue_seconds, cue_start, skip


def _inspect_oguri_cue(args: argparse.Namespace) -> int:
    movement, cue_seconds, cue_start, skip = _cue_args(args)
    summary = inspect_midi_cue(
        movement.local_path,
        start_seconds=cue_start,
        duration_seconds=cue_seconds,
        release_tail_seconds=args.release_tail_seconds,
        volume=args.volume,
        skip_track_name_contains=skip,
    )
    print(f"source={summary.source_path}")
    print(f"cue_start_seconds={summary.start_seconds:.3f}")
    print(f"cue_duration_seconds={cue_seconds:.3f}")
    print(f"release_tail_seconds={args.release_tail_seconds:.3f}")
    print(f"actual_duration_seconds={summary.duration_seconds:.3f}")
    print(f"event_count={summary.event_count}")
    print(f"note_on_count={summary.note_on_count}")
    print(f"first_note_on_seconds={summary.first_note_on_seconds}")
    return 0


def _play_oguri_cue(args: argparse.Namespace) -> int:
    movement, cue_seconds, cue_start, skip = _cue_args(args)
    summary = play_midi_cue(
        movement.local_path,
        args.output_name,
        start_seconds=cue_start,
        duration_seconds=cue_seconds,
        release_tail_seconds=args.release_tail_seconds,
        volume=args.volume,
        skip_track_name_contains=skip,
    )
    print(
        f"played cue events={summary.event_count} note_ons={summary.note_on_count} "
        f"from {summary.source_path} for {summary.duration_seconds:.2f}s "
        f"at volume={summary.volume:.2f}"
    )
    return 0


def _extract_oguri(args: argparse.Namespace) -> int:
    movement = oguri_movement(args.movement)
    if not movement.local_path.is_file():
        print(
            "error: the Oguri source MIDI is not present at\n"
            f"  {movement.local_path}\n"
            "It is a private, non-redistributable kunstderfuge.com performance and "
            "is not committed to this repository. Obtain it for your own private use "
            "and place it at the path above, then re-run this command. See "
            "docs/OPEN_SOURCE_READINESS.md.",
            file=sys.stderr,
        )
        return 1
    summary = extract_oguri_movement(movement)
    print(f"solo_reference={summary.solo_reference_path}")
    print(f"orchestra_accompaniment={summary.orchestra_accompaniment_path}")
    print(f"solo_notes={summary.solo_note_count} orchestra_notes={summary.orchestra_note_count}")

    # The derived MIDI is not redistributable (it descends from the private
    # Oguri source), so it is git-ignored and must be regenerated locally. The
    # DVC-managed score bundle consumed at runtime keeps its own copy under
    # ``derived/``; mirror the freshly extracted files there so a single
    # ``extract-oguri`` run repopulates every location the app reads.
    try:
        bundle_derived = paths.score_bundle_dir("chopin_op11", args.movement) / "derived"
    except ValueError:
        bundle_derived = None
    if bundle_derived is not None and bundle_derived.parent.exists():
        bundle_derived.mkdir(parents=True, exist_ok=True)
        for produced in (summary.solo_reference_path, summary.orchestra_accompaniment_path):
            mirrored = bundle_derived / produced.name
            if produced.resolve() != mirrored.resolve():
                shutil.copy2(produced, mirrored)
                print(f"mirrored={mirrored}")
    return 0


def _align_midi(args: argparse.Namespace) -> int:
    alignment = _align_midi_inputs(args.reference, args.performance, args.reference_track_marker)
    run_id = args.run_id
    trace_path = paths.run_trace_dir(run_id) / "alignment.jsonl"
    metrics_path = paths.run_analysis_dir(run_id) / "metrics.json"
    write_alignment_artifacts(alignment, trace_path=trace_path, metrics_path=metrics_path)
    print(f"alignment_trace={trace_path}")
    print(f"metrics={metrics_path}")
    print(
        f"pitch_matches={alignment.summary.pitch_match_count} "
        f"mismatches={alignment.summary.pitch_mismatch_count} "
        f"extra={alignment.summary.extra_performance_note_count} "
        f"missing={alignment.summary.missing_reference_note_count}"
    )
    return 0


def _render_offline(args: argparse.Namespace) -> int:
    run_id = args.run_id
    try:
        result = render_offline_midi(
            reference_path=args.reference,
            performance_path=args.performance,
            accompaniment_path=args.accompaniment,
            run_id=run_id,
            reference_track_marker=args.reference_track_marker,
        )
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        print(f"alignment_trace={paths.run_trace_dir(run_id) / 'alignment.jsonl'}", file=sys.stderr)
        print(f"metrics={paths.run_analysis_dir(run_id) / 'metrics.json'}", file=sys.stderr)
        return 1
    print(f"alignment_trace={result.trace_path}")
    print(f"metrics={result.metrics_path}")
    print(f"accompaniment={result.output_path}")
    print(f"pitch_matches={result.alignment.summary.pitch_match_count}")
    return 0


def _align_midi_inputs(
    reference_path: str,
    performance_path: str,
    reference_track_marker: str | None,
) -> AlignmentResult:
    return align_midi_inputs(
        reference_path,
        performance_path,
        reference_track_marker=reference_track_marker,
    )


def _panic(args: argparse.Namespace) -> int:
    panic(args.output_name)
    print(f"sent all-notes-off to {args.output_name}")
    return 0


def _migrate_takes_v2(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve() if args.root else paths.takes_root()
    if args.canonical_ppq <= 0:
        print("error: --canonical-ppq must be positive", file=sys.stderr)
        return 2
    before = inventory_tree(root)
    results = migrate_tree(root, apply=args.apply, canonical_ppq=args.canonical_ppq)
    for result in results:
        suffix = f" ({result.note})" if result.note else ""
        print(f"{result.action}: {result.take_directory}{suffix}")
    changed = sum(result.action in {"would_write", "written"} for result in results)
    mode = "apply" if args.apply else "dry-run"
    print(f"mode={mode} scanned={len(results)} changed={changed}")
    report = inventory_tree(root) if args.apply else before
    print(
        "compatibility "
        f"take_dirs={report.take_directories} v1_only={report.v1_only} "
        f"v2_only={report.v2_only} dual={report.dual} "
        f"v1_fallback_required={report.v1_fallback_required} "
        f"ready_to_retire_v1_reads={str(report.ready_to_retire_v1_reads).lower()}"
    )
    print(
        "alignment_compatibility "
        f"v1_only={report.aligned_v1_only} v2_only={report.aligned_v2_only} "
        f"dual={report.aligned_dual}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
