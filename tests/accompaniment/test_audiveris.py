import zipfile
from pathlib import Path

import pytest

from aimusic.accompaniment.audiveris import (
    _repair_sheet_xml,
    audiveris_command,
    audiveris_layout_command,
    audiveris_reexport_command,
    find_audiveris_executable,
    repair_phantom_chord_omr,
)

# A minimal sheet SIG with one *healthy* chord (valid stem #200, head #300) and
# the phantom-chord defect: a duplicate head #301, a degenerate chord #100
# (bounds at the page origin), and a zero-size stem #101, wired with the same
# relation shapes Audiveris emits. Repair must drop #100/#101 and every relation
# touching them, and leave the healthy chord and its relations untouched.
_SHEET_WITH_PHANTOM = """<sheet>
  <head staff="1" id="300"><bounds x="533" y="2302" w="28" h="23"/></head>
  <head staff="1" id="301"><bounds x="533" y="2302" w="28" h="23"/></head>
  <stem id="200"><bounds x="533" y="2250" w="2" h="75"/></stem>
  <stem width="1.8" shape="STEM" grade="0.4" staff="1" id="101">
    <bounds x="0" y="0" w="0" h="0"/>
    <median><p1 x="534" y="2321"/><p2 x="534.1" y="2321"/></median>
  </stem>
  <head-chord grade="0.7" staff="1" id="99">
    <bounds x="533" y="2250" w="28" h="75"/>
  </head-chord>
  <head-chord grade="0.9" staff="1" id="100">
    <bounds x="0" y="0" w="561" h="2325"/>
  </head-chord>
  <relation source="99" target="300"><containment/></relation>
  <relation source="99" target="200"><chord-stem/></relation>
  <relation source="100" target="101"><chord-stem/></relation>
  <relation source="100" target="300"><containment/></relation>
  <relation source="301" target="100"><no-exclusion grade="1"/></relation>
  <relation source="300" target="101"><head-stem grade="0.4"/></relation>
</sheet>
"""


def test_audiveris_command_is_batch_reproducible(tmp_path: Path) -> None:
    executable = tmp_path / "Audiveris"
    input_pdf = tmp_path / "score.pdf"
    output_dir = tmp_path / "out"

    command = audiveris_command(executable, input_pdf, output_dir)

    assert command == (
        str(executable),
        "-batch",
        "-transcribe",
        "-export",
        "-save",
        "-swap",
        "-output",
        str(output_dir),
        "--",
        str(input_pdf),
    )


def test_find_audiveris_prefers_explicit_executable(tmp_path: Path) -> None:
    executable = tmp_path / "audiveris"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    assert find_audiveris_executable(executable) == executable.resolve()


def test_layout_command_stops_at_structural_grid(tmp_path: Path) -> None:
    command = audiveris_layout_command(
        tmp_path / "Audiveris", tmp_path / "score.pdf", tmp_path / "out"
    )

    assert command[1:6] == ("-batch", "-step", "GRID", "-save", "-swap")
    assert "-transcribe" not in command


def test_find_audiveris_rejects_missing_explicit_when_no_other_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AUDIVERIS_BIN", raising=False)
    monkeypatch.setattr("aimusic.accompaniment.audiveris.shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "aimusic.accompaniment.audiveris.Path.resolve",
        lambda self: tmp_path / self.name,
    )

    with pytest.raises(FileNotFoundError, match="Audiveris executable not found"):
        find_audiveris_executable(tmp_path / "missing")


def test_reexport_command_resumes_without_force(tmp_path: Path) -> None:
    command = audiveris_reexport_command(
        tmp_path / "Audiveris", tmp_path / "book.omr", tmp_path / "out"
    )
    # Resume the saved SIG through PAGE; NO -force (which would re-run HEADS and
    # re-detect the duplicate the repair removed), and input is the .omr.
    assert command[1:5] == ("-batch", "-step", "PAGE", "-export")
    assert "-force" not in command
    assert "-transcribe" not in command
    assert command[-1] == str(tmp_path / "book.omr")


def test_repair_sheet_xml_removes_only_the_phantom_defect() -> None:
    cleaned, chords, stems, relations = _repair_sheet_xml(_SHEET_WITH_PHANTOM)

    assert chords == ["100"]
    assert stems == ["101"]
    # four relations reference the phantom chord #100 or zero-size stem #101
    assert relations == 4  # 100->101, 100->300, 301->100, 300->101
    # phantom inters gone
    assert 'id="100"' not in cleaned
    assert 'id="101"' not in cleaned
    # healthy chord, stem, and both heads survive (heads live on the twin chord)
    for kept in ('id="99"', 'id="200"', 'id="300"', 'id="301"'):
        assert kept in cleaned
    # relations that only reference kept inters survive
    assert '<relation source="99" target="300">' in cleaned
    assert '<relation source="99" target="200">' in cleaned


def test_repair_phantom_chord_omr_patches_swapped_book(tmp_path: Path) -> None:
    source = tmp_path / "book.omr"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("book.xml", "<book/>")
        archive.writestr("sheet#14/sheet#14.xml", _SHEET_WITH_PHANTOM)
        archive.writestr("sheet#14/BINARY.png", b"\x89PNG\r\n")

    out = tmp_path / "repaired.omr"
    report = repair_phantom_chord_omr(source, out)

    assert report.touched_sheets == (14,)
    assert report.removed_chords == {14: ("100",)}
    assert report.removed_stems == {14: ("101",)}
    assert report.total_removed_inters == 2

    with zipfile.ZipFile(out) as archive:
        names = set(archive.namelist())
        assert {"book.xml", "sheet#14/sheet#14.xml", "sheet#14/BINARY.png"} <= names
        patched = archive.read("sheet#14/sheet#14.xml").decode("utf-8")
        assert 'id="100"' not in patched and 'id="101"' not in patched
        # untouched members are copied verbatim
        assert archive.read("sheet#14/BINARY.png") == b"\x89PNG\r\n"


def test_repair_phantom_chord_omr_is_idempotent_on_clean_book(tmp_path: Path) -> None:
    clean = (
        '<sheet>\n  <head-chord id="99">'
        '<bounds x="533" y="2250" w="28" h="75"/></head-chord>\n</sheet>\n'
    )
    source = tmp_path / "clean.omr"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("sheet#1/sheet#1.xml", clean)

    report = repair_phantom_chord_omr(source, tmp_path / "out.omr")

    assert report.touched_sheets == ()
    assert report.total_removed_inters == 0
    assert report.removed_relations == 0
