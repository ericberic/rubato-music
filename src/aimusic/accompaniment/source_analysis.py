"""Analyze raw score source files before canonical bundle conversion."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import mido


@dataclass(frozen=True)
class FileSummary:
    path: Path
    bytes: int
    sha256: str


@dataclass(frozen=True)
class MidiTrackSummary:
    index: int
    name: str | None
    end_tick: int
    note_on_count: int
    channels: tuple[int, ...]
    pitch_min: int | None
    pitch_max: int | None
    programs: tuple[int, ...]


@dataclass(frozen=True)
class MidiSummary:
    format_type: int
    ticks_per_beat: int
    track_count: int
    length_seconds: float
    tracks: tuple[MidiTrackSummary, ...]


@dataclass(frozen=True)
class MusicXmlPartSummary:
    id: str
    name: str
    abbreviation: str | None
    measure_count: int
    midi_programs: tuple[int, ...]


@dataclass(frozen=True)
class MusicXmlSummary:
    version: str | None
    work_title: str | None
    composer: str | None
    software: tuple[str, ...]
    encoding_date: str | None
    part_count: int
    measure_count: int | None
    parts: tuple[MusicXmlPartSummary, ...]


@dataclass(frozen=True)
class PdfSummary:
    page_count: int | None


@dataclass(frozen=True)
class ScoreSourceAnalysis:
    source_dir: Path
    files: tuple[FileSummary, ...]
    midi: MidiSummary
    musicxml: MusicXmlSummary
    pdf: PdfSummary


def analyze_score_source_dir(source_dir: Path | str) -> ScoreSourceAnalysis:
    root = Path(source_dir)
    midi_path = root / "score.mid"
    mxl_path = root / "score.mxl"
    pdf_path = root / "score.pdf"
    for path in (midi_path, mxl_path, pdf_path):
        if not path.exists():
            raise FileNotFoundError(path)
    return ScoreSourceAnalysis(
        source_dir=root,
        files=tuple(_file_summary(path) for path in (midi_path, mxl_path, pdf_path)),
        midi=analyze_midi(midi_path),
        musicxml=analyze_mxl(mxl_path),
        pdf=PdfSummary(page_count=count_pdf_pages(pdf_path)),
    )


def analyze_midi(path: Path | str) -> MidiSummary:
    midi = mido.MidiFile(path, clip=True)
    tracks: list[MidiTrackSummary] = []
    for index, track in enumerate(midi.tracks):
        abs_ticks = 0
        name: str | None = None
        note_on_count = 0
        channels: set[int] = set()
        pitches: list[int] = []
        programs: set[int] = set()
        for msg in track:
            abs_ticks += msg.time
            if msg.type == "track_name" and name is None:
                name = msg.name
            if hasattr(msg, "channel"):
                channels.add(int(msg.channel))
            if msg.type == "program_change":
                programs.add(int(msg.program))
            if msg.type == "note_on" and msg.velocity > 0:
                note_on_count += 1
                pitches.append(int(msg.note))
        tracks.append(
            MidiTrackSummary(
                index=index,
                name=name,
                end_tick=abs_ticks,
                note_on_count=note_on_count,
                channels=tuple(sorted(channels)),
                pitch_min=min(pitches) if pitches else None,
                pitch_max=max(pitches) if pitches else None,
                programs=tuple(sorted(programs)),
            )
        )
    return MidiSummary(
        format_type=midi.type,
        ticks_per_beat=midi.ticks_per_beat,
        track_count=len(midi.tracks),
        length_seconds=float(midi.length),
        tracks=tuple(tracks),
    )


def analyze_mxl(path: Path | str) -> MusicXmlSummary:
    with ZipFile(path) as archive:
        xml_files = [
            name
            for name in archive.namelist()
            if name.endswith(".xml") and not name.startswith("META-INF/")
        ]
        if not xml_files:
            raise ValueError(f"No valid MusicXML root file found in MXL archive: {path}")
        xml_name = xml_files[0]
        root = ET.fromstring(archive.read(xml_name))

    ns = {"m": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}

    def q(name: str) -> str:
        return f"m:{name}" if ns else name

    def find(node: ET.Element, path: str) -> ET.Element | None:
        return node.find(path, ns) if ns else node.find(path)

    def findall(node: ET.Element, path: str) -> list[ET.Element]:
        return node.findall(path, ns) if ns else node.findall(path)

    def text(node: ET.Element, path: str) -> str | None:
        found = find(node, path)
        return found.text.strip() if found is not None and found.text else None

    composer = None
    software: list[str] = []
    encoding_date = None
    identification = find(root, q("identification"))
    if identification is not None:
        for creator in findall(identification, q("creator")):
            if creator.attrib.get("type") == "composer" and creator.text:
                composer = creator.text.strip()
        encoding = find(identification, q("encoding"))
        if encoding is not None:
            for child in list(encoding):
                tag = child.tag.split("}")[-1]
                value = child.text.strip() if child.text else None
                if tag == "software" and value:
                    software.append(value)
                if tag == "encoding-date" and value:
                    encoding_date = value

    measure_counts: dict[str, int] = {}
    for part in findall(root, q("part")):
        measure_counts[part.attrib["id"]] = len(findall(part, q("measure")))

    parts: list[MusicXmlPartSummary] = []
    part_list = find(root, q("part-list"))
    if part_list is not None:
        for score_part in findall(part_list, q("score-part")):
            part_id = score_part.attrib["id"]
            programs: list[int] = []
            for midi_instrument in findall(score_part, q("midi-instrument")):
                program_text = text(midi_instrument, q("midi-program"))
                if program_text is not None:
                    programs.append(int(program_text))
            parts.append(
                MusicXmlPartSummary(
                    id=part_id,
                    name=text(score_part, q("part-name")) or part_id,
                    abbreviation=text(score_part, q("part-abbreviation")),
                    measure_count=measure_counts.get(part_id, 0),
                    midi_programs=tuple(sorted(set(programs))),
                )
            )

    distinct_measure_counts = set(measure_counts.values())
    return MusicXmlSummary(
        version=root.attrib.get("version"),
        work_title=text(root, f"{q('work')}/{q('work-title')}"),
        composer=composer,
        software=tuple(software),
        encoding_date=encoding_date,
        part_count=len(parts),
        measure_count=distinct_measure_counts.pop() if len(distinct_measure_counts) == 1 else None,
        parts=tuple(parts),
    )


def count_pdf_pages(path: Path | str) -> int | None:
    # Good enough for MuseScore-generated PDFs and avoids a runtime PDF dependency.
    data = Path(path).read_bytes()
    matches = re.findall(rb"/Type\s*/Page\b", data)
    return len(matches) if matches else None


def _file_summary(path: Path) -> FileSummary:
    data = path.read_bytes()
    return FileSummary(path=path, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
