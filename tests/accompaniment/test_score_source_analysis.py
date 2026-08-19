from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml

from aimusic.accompaniment.source_analysis import (
    analyze_mxl,
    analyze_score_source_dir,
    count_pdf_pages,
)

SOURCE_DIR = Path("assets/scores/chopin_op11_i_allegro_maestoso/source")


def test_chopin_source_files_are_readable() -> None:
    analysis = analyze_score_source_dir(SOURCE_DIR)

    assert {summary.path.name for summary in analysis.files} == {
        "score.mid",
        "score.mxl",
        "score.pdf",
    }
    assert analysis.musicxml.work_title == "Piano Concerto No. 1 in E Minor"
    assert analysis.musicxml.composer == "Frederic Chopin"
    assert analysis.musicxml.part_count == 15
    assert analysis.musicxml.measure_count == 690
    assert analysis.midi.format_type == 1
    assert analysis.midi.ticks_per_beat == 480
    assert analysis.midi.track_count == 16
    assert analysis.pdf.page_count == 98


def test_chopin_source_manifest_matches_files() -> None:
    analysis = analyze_score_source_dir(SOURCE_DIR)
    manifest = yaml.safe_load((SOURCE_DIR / "source_manifest.yaml").read_text(encoding="utf-8"))

    actual = {summary.path.name: summary for summary in analysis.files}

    for filename, expected in manifest["files"].items():
        assert actual[filename].bytes == expected["bytes"]
        assert actual[filename].sha256 == expected["sha256"]


def test_chopin_midi_musicxml_known_piano_split() -> None:
    analysis = analyze_score_source_dir(SOURCE_DIR)

    xml_piano_parts = [part for part in analysis.musicxml.parts if part.name == "Piano solo"]
    midi_piano_tracks = [track for track in analysis.midi.tracks if track.name == "Piano solo"]

    assert len(xml_piano_parts) == 1
    assert len(midi_piano_tracks) == 2
    assert sum(track.note_on_count for track in midi_piano_tracks) == 9356


def test_pdf_page_counter_handles_committed_score() -> None:
    assert count_pdf_pages(SOURCE_DIR / "score.pdf") == 98


def test_mxl_without_musicxml_root_has_clear_error(tmp_path: Path) -> None:
    bad_mxl = tmp_path / "bad.mxl"
    with ZipFile(bad_mxl, "w") as archive:
        archive.writestr("META-INF/container.xml", "<container />")

    with pytest.raises(ValueError, match="No valid MusicXML root file"):
        analyze_mxl(bad_mxl)
