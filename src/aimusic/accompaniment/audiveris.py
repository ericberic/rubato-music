"""Build-time Audiveris adapter for scanned score sources.

Audiveris is intentionally not a Rubato runtime dependency.  This adapter
keeps the reproducible command construction in code while allowing a user or
agent to install/run the official application separately.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AudiverisRun:
    input_pdf: Path
    output_dir: Path
    command: tuple[str, ...]
    artifacts: tuple[Path, ...]


@dataclass(frozen=True)
class OmrRepairReport:
    """What the duplicate-head/phantom-chord repair removed, per sheet."""

    removed_chords: dict[int, tuple[str, ...]]
    removed_stems: dict[int, tuple[str, ...]]
    removed_relations: int

    @property
    def touched_sheets(self) -> tuple[int, ...]:
        return tuple(sorted(set(self.removed_chords) | set(self.removed_stems)))

    @property
    def total_removed_inters(self) -> int:
        return sum(len(v) for v in self.removed_chords.values()) + sum(
            len(v) for v in self.removed_stems.values()
        )


def find_audiveris_executable(explicit: Path | str | None = None) -> Path:
    """Find the CLI launcher without requiring a system-wide Java install."""

    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit).expanduser())
    if value := os.environ.get("AUDIVERIS_BIN"):
        candidates.append(Path(value).expanduser())
    if discovered := shutil.which("audiveris"):
        candidates.append(Path(discovered))
    candidates.extend(
        [
            Path("/Applications/Audiveris.app/Contents/MacOS/Audiveris"),
            Path("/Volumes/Audiveris/Audiveris.app/Contents/MacOS/Audiveris"),
        ]
    )

    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    raise FileNotFoundError(
        "Audiveris executable not found. Install the official application or "
        "set AUDIVERIS_BIN to its CLI launcher."
    )


def audiveris_command(executable: Path, input_pdf: Path, output_dir: Path) -> tuple[str, ...]:
    """Return the deterministic full-book transcription/export command."""

    return (
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


def audiveris_layout_command(
    executable: Path, input_pdf: Path, output_dir: Path
) -> tuple[str, ...]:
    """Return the fast structural-recognition command used for display maps."""

    return (
        str(executable),
        "-batch",
        "-step",
        "GRID",
        "-save",
        "-swap",
        "-output",
        str(output_dir),
        "--",
        str(input_pdf),
    )


def audiveris_reexport_command(
    executable: Path, omr_path: Path, output_dir: Path
) -> tuple[str, ...]:
    """Resume a *saved* `.omr` book through PAGE and re-export MusicXML.

    Input is the `.omr`, not a PDF, and there is **no** `-force`: Audiveris
    resumes from the persisted SIG at `LINKS`, so any hand-edit to the
    recognized inters survives. `-force` would re-run `HEADS` and re-detect the
    very duplicate the repair removed. See `repair_phantom_chord_omr`.
    """

    return (
        str(executable),
        "-batch",
        "-step",
        "PAGE",
        "-export",
        "-output",
        str(output_dir),
        "--",
        str(omr_path),
    )


# A single spurious glyph can abort export for a whole sheet and every sheet
# after it (Audiveris builds a phantom chord over a duplicated notehead with a
# zero-length stem at (0,0); the chord belongs to no measure and
# `Measure.getClefBefore(null)` throws at PAGE). These two patterns identify the
# junk structurally: a chord whose bounds sit at the page origin, and a stem
# with no extent. See docs/sources/joseffy-reduction.md and the score-
# localization skill for the full write-up.
_DEGENERATE_CHORD = re.compile(
    r'[ \t]*<head-chord\b[^>]*\bid="(?P<id>\d+)"[^>]*>\s*'
    r'<bounds x="0" y="0"[^>]*/>\s*</head-chord>\n?'
)
_ZERO_SIZE_STEM = re.compile(
    r'[ \t]*<stem\b[^>]*\bid="(?P<id>\d+)"[^>]*>\s*'
    r'<bounds x="\d+" y="\d+" w="0" h="0"[^>]*/>.*?</stem>\n?',
    re.S,
)
_RELATION_BLOCK = re.compile(r'[ \t]*<relation\b[^>]*>.*?</relation>\n?', re.S)
_SHEET_MEMBER = re.compile(r"^sheet#(?P<n>\d+)/sheet#\d+\.xml$")


def _repair_sheet_xml(xml: str) -> tuple[str, list[str], list[str], int]:
    """Strip degenerate chords/stems and every relation that references them."""

    chord_ids = [m.group("id") for m in _DEGENERATE_CHORD.finditer(xml)]
    stem_ids = [m.group("id") for m in _ZERO_SIZE_STEM.finditer(xml)]
    dead_ids = set(chord_ids) | set(stem_ids)
    if not dead_ids:
        return xml, [], [], 0

    xml = _DEGENERATE_CHORD.sub("", xml)
    xml = _ZERO_SIZE_STEM.sub("", xml)

    removed_relations = 0

    def _drop_relation(match: re.Match[str]) -> str:
        nonlocal removed_relations
        block = match.group(0)
        if any(f'"{dead}"' in block for dead in dead_ids):
            removed_relations += 1
            return ""
        return block

    xml = _RELATION_BLOCK.sub(_drop_relation, xml)
    return xml, chord_ids, stem_ids, removed_relations


def repair_phantom_chord_omr(omr_path: Path | str, output_path: Path | str) -> OmrRepairReport:
    """Write a repaired copy of a transcription `.omr`, removing phantom chords.

    Operates only on `.omr` books saved with `-swap` (so per-sheet SIG XML is
    present). Removes every degenerate `head-chord` (bounds at the page origin),
    every zero-size `stem`, and all `relation` edges that reference them; the
    real noteheads survive on their healthy twin chord. Idempotent: re-running on
    an already-clean book removes nothing. Re-export with
    `audiveris_reexport_command` (no `-force`) to get a clean MusicXML.
    """

    source = Path(omr_path).expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Audiveris .omr not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    removed_chords: dict[int, tuple[str, ...]] = {}
    removed_stems: dict[int, tuple[str, ...]] = {}
    total_relations = 0

    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        patched: dict[str, bytes] = {}
        for info in members:
            match = _SHEET_MEMBER.match(info.filename)
            if match is None:
                continue
            xml = archive.read(info.filename).decode("utf-8")
            new_xml, chords, stems, relations = _repair_sheet_xml(xml)
            if chords or stems:
                sheet = int(match.group("n"))
                if chords:
                    removed_chords[sheet] = tuple(chords)
                if stems:
                    removed_stems[sheet] = tuple(stems)
                total_relations += relations
                patched[info.filename] = new_xml.encode("utf-8")

        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as out:
            for info in members:
                payload = patched.get(info.filename)
                out.writestr(info, payload if payload is not None else archive.read(info.filename))

    return OmrRepairReport(
        removed_chords=removed_chords,
        removed_stems=removed_stems,
        removed_relations=total_relations,
    )


def repair_and_reexport_omr(
    omr_path: Path | str,
    output_dir: Path | str,
    *,
    executable: Path | str | None = None,
) -> tuple[OmrRepairReport, AudiverisRun]:
    """Repair a transcription `.omr` in place-of and re-export a clean MusicXML."""

    output_path = Path(output_dir).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    patched_omr = output_path / "book.omr"
    report = repair_phantom_chord_omr(omr_path, patched_omr)

    resolved_executable = find_audiveris_executable(executable)
    command = audiveris_reexport_command(resolved_executable, patched_omr, output_path)
    subprocess.run(command, check=True)
    artifacts = tuple(sorted(path for path in output_path.rglob("*") if path.is_file()))
    run = AudiverisRun(
        input_pdf=patched_omr,
        output_dir=output_path,
        command=command,
        artifacts=artifacts,
    )
    return report, run


def transcribe_score_pdf(
    input_pdf: Path | str,
    output_dir: Path | str,
    *,
    executable: Path | str | None = None,
) -> AudiverisRun:
    """Run Audiveris and return every output artifact it created.

    The caller owns review of the `.omr` project and exported MusicXML.  A
    successful process exit means the pipeline ran, not that every recognized
    measure is musically correct.
    """

    input_path = Path(input_pdf).expanduser().resolve()
    output_path = Path(output_dir).expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input score PDF not found: {input_path}")
    output_path.mkdir(parents=True, exist_ok=True)

    resolved_executable = find_audiveris_executable(executable)
    command = audiveris_command(resolved_executable, input_path, output_path)
    subprocess.run(command, check=True)
    artifacts = tuple(sorted(path for path in output_path.rglob("*") if path.is_file()))
    return AudiverisRun(
        input_pdf=input_path,
        output_dir=output_path,
        command=command,
        artifacts=artifacts,
    )


def extract_score_layout(
    input_pdf: Path | str,
    output_dir: Path | str,
    *,
    executable: Path | str | None = None,
) -> AudiverisRun:
    """Run structural recognition only, producing reviewable OMR geometry."""

    input_path = Path(input_pdf).expanduser().resolve()
    output_path = Path(output_dir).expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input score PDF not found: {input_path}")
    output_path.mkdir(parents=True, exist_ok=True)
    resolved_executable = find_audiveris_executable(executable)
    command = audiveris_layout_command(resolved_executable, input_path, output_path)
    subprocess.run(command, check=True)
    artifacts = tuple(sorted(path for path in output_path.rglob("*") if path.is_file()))
    return AudiverisRun(
        input_pdf=input_path,
        output_dir=output_path,
        command=command,
        artifacts=artifacts,
    )


__all__ = [
    "AudiverisRun",
    "OmrRepairReport",
    "audiveris_command",
    "audiveris_layout_command",
    "audiveris_reexport_command",
    "extract_score_layout",
    "find_audiveris_executable",
    "repair_and_reexport_omr",
    "repair_phantom_chord_omr",
    "transcribe_score_pdf",
]
