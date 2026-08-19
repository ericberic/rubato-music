"""Convert MusicXML/MXL excerpts into Rubato score bundles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import yaml

from aimusic.accompaniment.score_bundle import ScoreBundle

_STEP_TO_SEMITONE = {
    "C": 0,
    "D": 2,
    "E": 4,
    "F": 5,
    "G": 7,
    "A": 9,
    "B": 11,
}


@dataclass(frozen=True)
class MusicXmlExcerptConfig:
    source_mxl: Path
    output_dir: Path
    start_measure: int
    end_measure: int
    solo_part_name: str = "Piano solo"
    title: str = "Chopin Op. 11 I excerpt"
    composer: str = "Frederic Chopin"
    version: str = "generated-test"
    default_tempo_bpm: float = 120.0


def convert_mxl_excerpt_to_bundle(
    source_mxl: Path | str,
    output_dir: Path | str,
    *,
    start_measure: int,
    end_measure: int,
    solo_part_name: str = "Piano solo",
    title: str = "Chopin Op. 11 I excerpt",
    composer: str = "Frederic Chopin",
    version: str = "generated-test",
    default_tempo_bpm: float = 120.0,
) -> ScoreBundle:
    """Convert a MusicXML measure window into a canonical score bundle.

    The converter is intentionally conservative: it extracts pitched notes with
    beat positions and durations, maps the configured solo part to `solo`, maps
    all other sounding parts to `accompaniment`, and writes the small bundle to
    `output_dir` for normal `ScoreBundle.load` validation.
    """

    if end_measure < start_measure:
        raise ValueError("end_measure must be >= start_measure")
    config = MusicXmlExcerptConfig(
        source_mxl=Path(source_mxl),
        output_dir=Path(output_dir),
        start_measure=start_measure,
        end_measure=end_measure,
        solo_part_name=solo_part_name,
        title=title,
        composer=composer,
        version=version,
        default_tempo_bpm=default_tempo_bpm,
    )
    root = _load_mxl_root(config.source_mxl)
    part_names = _part_names(root)
    solo_part_ids = {
        part_id for part_id, name in part_names.items() if name == config.solo_part_name
    }
    if not solo_part_ids:
        raise ValueError(f"Could not find solo part named: {config.solo_part_name}")

    measure_lengths = _measure_lengths(root, config.start_measure, config.end_measure)
    measure_offsets = _measure_offsets(measure_lengths)
    events = _extract_events(root, part_names, solo_part_ids, measure_offsets, config)
    if not events:
        raise ValueError("No pitched events found in requested excerpt")

    used_part_ids = {event["part_id"] for event in events}
    output = config.output_dir
    output.mkdir(parents=True, exist_ok=True)
    _write_metadata(output, config)
    _write_parts(output, part_names, solo_part_ids, used_part_ids)
    _write_events(output, events)
    end_beat = max(event["beat"] + event["duration_beats"] for event in events)
    _write_sections(output, config, end_beat)
    _write_instrument_map(output, used_part_ids - solo_part_ids)
    return ScoreBundle.load(output)


def _load_mxl_root(path: Path) -> ET.Element:
    with ZipFile(path) as archive:
        xml_files = [
            name
            for name in archive.namelist()
            if (name.endswith(".xml") or name.endswith(".musicxml"))
            and not name.startswith("META-INF/")
        ]
        if not xml_files:
            raise ValueError(f"No valid MusicXML root file found in MXL archive: {path}")
        return ET.fromstring(archive.read(xml_files[0]))


def _part_names(root: ET.Element) -> dict[str, str]:
    part_list = root.find("part-list")
    if part_list is None:
        raise ValueError("MusicXML file has no part-list")
    names: dict[str, str] = {}
    for score_part in part_list.findall("score-part"):
        part_id = score_part.attrib["id"]
        name = score_part.findtext("part-name") or part_id
        names[part_id] = name
    return names


def _measure_lengths(root: ET.Element, start_measure: int, end_measure: int) -> dict[int, float]:
    lengths: dict[int, float] = {measure: 0.0 for measure in range(start_measure, end_measure + 1)}
    for part in root.findall("part"):
        divisions = 1
        for measure in part.findall("measure"):
            measure_number = _measure_number(measure)
            if (
                measure_number is None
                or measure_number < start_measure
                or measure_number > end_measure
            ):
                divisions = _updated_divisions(measure, divisions)
                continue
            local_position = 0.0
            max_position = 0.0
            for child in list(measure):
                if child.tag == "attributes":
                    divisions = _divisions_from_attributes(child, divisions)
                elif child.tag == "backup":
                    local_position -= _duration_to_beats(child.findtext("duration"), divisions)
                elif child.tag == "forward":
                    local_position += _duration_to_beats(child.findtext("duration"), divisions)
                    max_position = max(max_position, local_position)
                elif child.tag == "note":
                    duration = _duration_to_beats(child.findtext("duration"), divisions)
                    if child.find("chord") is None:
                        max_position = max(max_position, local_position + duration)
                        local_position += duration
            lengths[measure_number] = max(lengths[measure_number], max_position)
    return lengths


def _measure_offsets(measure_lengths: dict[int, float]) -> dict[int, float]:
    offsets: dict[int, float] = {}
    current = 0.0
    for measure in sorted(measure_lengths):
        offsets[measure] = current
        current += measure_lengths[measure]
    return offsets


def _extract_events(
    root: ET.Element,
    part_names: dict[str, str],
    solo_part_ids: set[str],
    measure_offsets: dict[int, float],
    config: MusicXmlExcerptConfig,
) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    event_counts: dict[str, int] = {}
    for part in root.findall("part"):
        part_id = part.attrib["id"]
        divisions = 1
        for measure in part.findall("measure"):
            measure_number = _measure_number(measure)
            if measure_number is None:
                divisions = _updated_divisions(measure, divisions)
                continue
            if measure_number < config.start_measure or measure_number > config.end_measure:
                divisions = _updated_divisions(measure, divisions)
                continue
            local_position = 0.0
            for child in list(measure):
                if child.tag == "attributes":
                    divisions = _divisions_from_attributes(child, divisions)
                elif child.tag == "backup":
                    local_position -= _duration_to_beats(child.findtext("duration"), divisions)
                elif child.tag == "forward":
                    local_position += _duration_to_beats(child.findtext("duration"), divisions)
                elif child.tag == "note":
                    duration = _duration_to_beats(child.findtext("duration"), divisions)
                    is_chord_note = child.find("chord") is not None
                    pitch = _midi_pitch(child)
                    if pitch is not None:
                        role = "solo" if part_id in solo_part_ids else "accompaniment"
                        event_number = event_counts.get(part_id, 0)
                        event_counts[part_id] = event_number + 1
                        events.append(
                            {
                                "event_id": f"{part_id}_{event_number:04d}",
                                "measure": measure_number,
                                "beat": round(measure_offsets[measure_number] + local_position, 6),
                                "part_id": part_id,
                                "role": role,
                                "pitch": pitch,
                                "velocity": 72 if role == "solo" else 64,
                                "duration_beats": max(round(duration, 6), 0.000001),
                                "source_refs": {
                                    "source": "musicxml",
                                    "part_name": part_names.get(part_id, part_id),
                                    "measure": measure_number,
                                },
                            }
                        )
                    if not is_chord_note:
                        local_position += duration
    return sorted(events, key=lambda item: (float(item["beat"]), str(item["event_id"])))


def _updated_divisions(measure: ET.Element, current: int) -> int:
    attributes = measure.find("attributes")
    return _divisions_from_attributes(attributes, current) if attributes is not None else current


def _divisions_from_attributes(attributes: ET.Element, current: int) -> int:
    divisions_text = attributes.findtext("divisions")
    if not divisions_text:
        return current
    try:
        divisions = int(divisions_text)
    except ValueError:
        return current
    return divisions if divisions > 0 else current


def _duration_to_beats(duration_text: str | None, divisions: int) -> float:
    if not duration_text or divisions <= 0:
        return 0.0
    try:
        return int(duration_text) / divisions
    except ValueError:
        return 0.0


def _midi_pitch(note: ET.Element) -> int | None:
    if note.find("rest") is not None:
        return None
    pitch = note.find("pitch")
    if pitch is None:
        return None
    step = pitch.findtext("step")
    octave_text = pitch.findtext("octave")
    if step is None or octave_text is None or step not in _STEP_TO_SEMITONE:
        return None
    try:
        alter = int(float(pitch.findtext("alter") or 0))
        octave = int(octave_text)
    except ValueError:
        return None
    return (octave + 1) * 12 + _STEP_TO_SEMITONE[step] + alter


def _measure_number(measure: ET.Element) -> int | None:
    number = measure.attrib.get("number")
    if number is None or not number.isdigit():
        return None
    return int(number)


def _write_metadata(output: Path, config: MusicXmlExcerptConfig) -> None:
    payload = {
        "piece_id": f"chopin_op11_i_m{config.start_measure}_{config.end_measure}",
        "title": config.title,
        "composer": config.composer,
        "version": config.version,
        "source": str(config.source_mxl),
    }
    (output / "metadata.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def _write_parts(
    output: Path, part_names: dict[str, str], solo_part_ids: set[str], used_part_ids: set[str]
) -> None:
    parts = []
    for part_id, name in part_names.items():
        if part_id not in used_part_ids:
            continue
        parts.append(
            {
                "id": part_id,
                "name": name,
                "abbreviation": None,
                "role": "solo" if part_id in solo_part_ids else "accompaniment",
            }
        )
    (output / "parts.yaml").write_text(
        yaml.safe_dump({"parts": parts}, sort_keys=False),
        encoding="utf-8",
    )


def _write_events(output: Path, events: list[dict[str, object]]) -> None:
    lines = [json.dumps(event, sort_keys=True, separators=(",", ":")) for event in events]
    (output / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_sections(output: Path, config: MusicXmlExcerptConfig, end_beat: float) -> None:
    payload = {
        "piece_id": f"chopin_op11_i_m{config.start_measure}_{config.end_measure}",
        "sections": [
            {
                "id": "converted_excerpt",
                "start_beat": 0,
                "end_beat": end_beat + 1.0,
                "mode": "FOLLOW",
                "tempo_bpm": config.default_tempo_bpm,
            }
        ],
    }
    (output / "sections.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_instrument_map(output: Path, accompaniment_part_ids: set[str]) -> None:
    instruments = []
    channel = 0
    for part_id in sorted(accompaniment_part_ids):
        if channel == 9:
            channel = (channel + 1) % 16
        instruments.append(
            {
                "part_id": part_id,
                "channel": channel,
                "program": 48,
                "name": "String Ensemble 1",
                "volume": 90,
            }
        )
        channel = (channel + 1) % 16
    (output / "instrument_map.yaml").write_text(
        yaml.safe_dump({"instruments": instruments}, sort_keys=False),
        encoding="utf-8",
    )
