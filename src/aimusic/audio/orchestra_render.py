"""Offline multi-instance BBCSO rendering for an Oguri orchestral cue.

BBC Symphony Orchestra is a single-patch instrument: unlike a General MIDI
synth, one plug-in instance cannot render unlike score parts at once.  This
module gives each sounding Oguri section its own captured BBCSO state, renders
the stems independently, and sums them into one audition WAV.
"""

from __future__ import annotations

import gc
from dataclasses import dataclass
from pathlib import Path

import mido
import numpy as np

from aimusic.accompaniment.live_midi import midi_cue_events
from aimusic.accompaniment.rehearsal_position import score_projection
from aimusic.audio.plugin_host import (
    DEFAULT_BBCSO_VST3_PATH,
    AudioDependencyError,
)
from aimusic.core import paths

DEFAULT_SAMPLE_RATE = 48_000.0
DEFAULT_BUFFER_SIZE = 512
DEFAULT_RENDER_TAIL_SECONDS = 3.0
DEFAULT_MIX_GAIN = 0.65
MAX_OUTPUT_PEAK = 0.98


@dataclass(frozen=True)
class BbcsoPart:
    track_name: str
    state_filename: str


OGURI_MOVEMENT_2_PARTS: tuple[BbcsoPart, ...] = (
    BbcsoPart("Violini I", "bbcso-violins1-long.state"),
    BbcsoPart("Violini II", "bbcso-violins2-long.state"),
    BbcsoPart("Viole", "bbcso-violas-long.state"),
    BbcsoPart("Violoncelli", "bbcso-cellos-long.state"),
    BbcsoPart("Contrabassi", "bbcso-basses-long.state"),
    BbcsoPart("Corni (E)", "bbcso-horns-long.state"),
    BbcsoPart("Flauti", "bbcso-flutes-long.state"),
    BbcsoPart("Clarinetti (C)", "bbcso-clarinets-long.state"),
    BbcsoPart("Fagotti", "bbcso-bassoons-long.state"),
)


@dataclass(frozen=True)
class ScoreCueWindow:
    start_measure: int
    end_measure_exclusive: int
    source_start_seconds: float
    source_end_seconds: float

    @property
    def duration_seconds(self) -> float:
        return self.source_end_seconds - self.source_start_seconds


@dataclass(frozen=True)
class OrchestraStemSummary:
    track_name: str
    state_path: Path
    event_count: int
    note_on_count: int
    peak: float


@dataclass(frozen=True)
class OrchestraRenderSummary:
    output_path: Path
    plugin_path: Path
    stems: tuple[OrchestraStemSummary, ...]
    duration_seconds: float
    sample_rate: float
    peak_before_safety_gain: float
    peak: float

    @property
    def note_on_count(self) -> int:
        return sum(stem.note_on_count for stem in self.stems)


def default_plugin_state_dir() -> Path:
    """Return the machine-local directory used for captured BBCSO states."""

    return paths.state_root() / "plugin-states"


def score_cue_window(
    *,
    start_measure: int,
    end_measure_exclusive: int,
    piece_id: str = "chopin_op11",
    movement: int = 2,
) -> ScoreCueWindow:
    """Translate printed measure boundaries into Oguri source seconds."""

    if start_measure < 1:
        raise ValueError("start_measure must be at least 1")
    if end_measure_exclusive <= start_measure:
        raise ValueError("end_measure_exclusive must be greater than start_measure")
    projection = score_projection(piece_id, movement)
    measure_count = len(projection.timeline.document.measures)
    if end_measure_exclusive - 1 >= measure_count:
        raise ValueError("cue end is outside the score")

    start_tick = projection.timeline.tick_at(start_measure - 1)
    end_tick = projection.timeline.tick_at(end_measure_exclusive - 1)
    return ScoreCueWindow(
        start_measure=start_measure,
        end_measure_exclusive=end_measure_exclusive,
        source_start_seconds=projection.source_seconds_at_score_tick(start_tick),
        source_end_seconds=projection.source_seconds_at_score_tick(end_tick),
    )


def missing_plugin_states(
    state_dir: Path,
    *,
    parts: tuple[BbcsoPart, ...] = OGURI_MOVEMENT_2_PARTS,
) -> tuple[Path, ...]:
    """Return all required state paths that have not been captured yet."""

    return tuple(
        state_dir / part.state_filename
        for part in parts
        if not (state_dir / part.state_filename).is_file()
    )


def render_oguri_bbcso_orchestra(
    midi_path: Path,
    output_path: Path,
    *,
    window: ScoreCueWindow,
    state_dir: Path | None = None,
    plugin_path: Path = DEFAULT_BBCSO_VST3_PATH,
    parts: tuple[BbcsoPart, ...] = OGURI_MOVEMENT_2_PARTS,
    volume: float = 0.75,
    sample_rate: float = DEFAULT_SAMPLE_RATE,
    buffer_size: int = DEFAULT_BUFFER_SIZE,
    render_tail_seconds: float = DEFAULT_RENDER_TAIL_SECONDS,
    mix_gain: float = DEFAULT_MIX_GAIN,
) -> OrchestraRenderSummary:
    """Render each Oguri section through its BBCSO patch and mix one WAV."""

    state_dir = state_dir or default_plugin_state_dir()
    _validate_render_inputs(
        midi_path=midi_path,
        plugin_path=plugin_path,
        state_dir=state_dir,
        parts=parts,
        sample_rate=sample_rate,
        buffer_size=buffer_size,
        mix_gain=mix_gain,
    )
    pedalboard, audio_file = _pedalboard_api()
    duration = window.duration_seconds + max(0.0, render_tail_seconds)
    frame_count = int(round(duration * sample_rate))
    mixed = np.zeros((2, frame_count), dtype=np.float32)
    summaries: list[OrchestraStemSummary] = []

    for part in parts:
        events = midi_cue_events(
            midi_path,
            volume=volume,
            start_seconds=window.source_start_seconds,
            duration_seconds=window.duration_seconds,
            release_tail_seconds=render_tail_seconds,
            include_track_names=(part.track_name,),
        )
        messages = _pedalboard_midi_messages(events)
        plugin = pedalboard.load_plugin(str(plugin_path), initialization_timeout=60.0)
        if not plugin.is_instrument:
            raise ValueError(f"plug-in is not an instrument: {plugin_path}")
        state_path = state_dir / part.state_filename
        plugin.raw_state = state_path.read_bytes()
        stem = np.asarray(
            plugin.process(
                messages,
                duration=duration,
                sample_rate=sample_rate,
                num_channels=2,
                buffer_size=buffer_size,
                reset=True,
            ),
            dtype=np.float32,
        )
        stem = _coerce_stereo_frames(stem, frame_count)
        stem_peak = _peak(stem)
        mixed += stem
        summaries.append(
            OrchestraStemSummary(
                track_name=part.track_name,
                state_path=state_path,
                event_count=len(events),
                note_on_count=sum(
                    message.type == "note_on" and message.velocity > 0
                    for _, message in events
                ),
                peak=stem_peak,
            )
        )
        del stem, plugin
        gc.collect()

    mixed *= mix_gain
    peak_before_safety_gain = _peak(mixed)
    if peak_before_safety_gain > MAX_OUTPUT_PEAK:
        mixed *= MAX_OUTPUT_PEAK / peak_before_safety_gain
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with audio_file(str(output_path), "w", sample_rate, 2) as output:
        output.write(mixed)
    return OrchestraRenderSummary(
        output_path=output_path,
        plugin_path=plugin_path,
        stems=tuple(summaries),
        duration_seconds=duration,
        sample_rate=sample_rate,
        peak_before_safety_gain=peak_before_safety_gain,
        peak=_peak(mixed),
    )


def _validate_render_inputs(
    *,
    midi_path: Path,
    plugin_path: Path,
    state_dir: Path,
    parts: tuple[BbcsoPart, ...],
    sample_rate: float,
    buffer_size: int,
    mix_gain: float,
) -> None:
    if not midi_path.is_file():
        raise FileNotFoundError(f"MIDI source is missing: {midi_path}")
    if not plugin_path.exists():
        raise FileNotFoundError(f"VST3 plug-in is missing: {plugin_path}")
    missing = missing_plugin_states(state_dir, parts=parts)
    if missing:
        joined = "\n  - ".join(str(path) for path in missing)
        raise FileNotFoundError(f"BBCSO plug-in states are missing:\n  - {joined}")
    if not parts:
        raise ValueError("at least one orchestra part is required")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if buffer_size <= 0:
        raise ValueError("buffer_size must be positive")
    if mix_gain <= 0:
        raise ValueError("mix_gain must be positive")


def _pedalboard_api():
    try:
        import pedalboard
        from pedalboard.io import AudioFile
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise AudioDependencyError(
            "Pedalboard is not installed; run `uv sync --extra audio`."
        ) from exc
    return pedalboard, AudioFile


def _pedalboard_midi_messages(
    events: list[tuple[float, mido.Message]],
) -> list[tuple[bytes, float]]:
    supported = {"note_on", "note_off", "control_change", "pitchwheel", "aftertouch"}
    rendered: list[tuple[bytes, float]] = [
        (bytes(mido.Message("control_change", channel=0, control=7, value=127).bytes()), 0.0),
        (bytes(mido.Message("control_change", channel=0, control=11, value=127).bytes()), 0.0),
    ]
    for timestamp, message in events:
        if message.type not in supported:
            continue
        normalized = message.copy(channel=0, time=0)
        rendered.append((bytes(normalized.bytes()), max(0.0, timestamp)))
    return rendered


def _coerce_stereo_frames(audio: np.ndarray, frame_count: int) -> np.ndarray:
    if audio.ndim != 2 or audio.shape[0] != 2:
        raise ValueError(f"BBCSO returned unexpected audio shape: {audio.shape}")
    if audio.shape[1] == frame_count:
        return audio
    result = np.zeros((2, frame_count), dtype=np.float32)
    copied = min(frame_count, audio.shape[1])
    result[:, :copied] = audio[:, :copied]
    return result


def _peak(audio: np.ndarray) -> float:
    return float(np.max(np.abs(audio))) if audio.size else 0.0


__all__ = [
    "BbcsoPart",
    "OGURI_MOVEMENT_2_PARTS",
    "OrchestraRenderSummary",
    "OrchestraStemSummary",
    "ScoreCueWindow",
    "default_plugin_state_dir",
    "missing_plugin_states",
    "render_oguri_bbcso_orchestra",
    "score_cue_window",
]
