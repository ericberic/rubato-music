"""Build `measure_boxes.json` geometry maps for the coverage PDF overlay.

Design doc: docs/design/REHEARSAL_TAKE_COVERAGE_DESIGN.md §3.3 ("The measure-geometry
map") and roadmap item 7. This module implements Level 1 only -- per-page
system bands (`y0`/`y1`, page-normalized 0-1) plus the first/last measure
number each system spans. Level 2 (per-measure x-splits) is deliberately out
of scope; the design doc explicitly says Level 1 needs only the y-extents and
measure range, and the roadmap warns against building x-splits early.

**Method (movement 1 specifically, priority order from §3.3):**

1. MusicXML `<print new-system="yes"/new-page="yes">` elements from the
   layout-compatible Movement 1 export, keyed to
   measure numbers, give exact system/page *membership* for free -- no OMR
   needed. Movement 1's `score.mxl` has these (confirmed: 139 break markers
   across 98 pages, 140 systems total, identical across all 15 parts since
   this is a single full-score export -- see `_parse_system_breaks`).
2. The *vertical* extent of each system on its page is not in the MusicXML
   (that lives in engraving-specific `<system-layout>`/`<page-layout>`
   defaults MuseScore does emit, but trusting them to map to true rendered
   pixels is exactly the "OMR-perfect geometry" trap the design doc tells us
   not to block on). Instead we take the pragmatic fallback the design doc
   explicitly sanctions: evenly divide each page's usable vertical band among
   its known system count. This is "coarse but honest" -- correct system
   membership and page assignment (from real data), approximate banding
   within the page (visually reasonable, never wrong about *which* measures
   are in view).
3. `parse_mxl_measures` (coverage.py) is reused to cross-check the total
   measure count the two independent readers (system-break walk vs. the
   existing per-measure beat parser) agree on, catching drift immediately
   per §3.3 point 3.

This is a one-off, rerunnable *build* step (see scripts/build_measure_boxes.py),
not runtime code -- it never executes in the live accompaniment path.
"""

from __future__ import annotations

import json
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

from aimusic.core import paths
from aimusic.takes.coverage import parse_mxl_measures

# Usable vertical band for an ordinary page: leaves room for header/footer
# and page-number furniture MuseScore prints outside the staff area.
DEFAULT_TOP_MARGIN = 0.08
DEFAULT_BOTTOM_MARGIN = 0.06

# Page 1 of the movement-1 PDF carries a title block (work title, composer,
# full instrument list) above the first system -- verified by inspecting the
# extracted page text, not guessed -- so its single system starts noticeably
# lower than every other page's first system. A bigger top margin on page 1
# keeps the coarse band from washing over the title text. This is a one-page
# special case, not a general rule; future bundles may need their own tuning
# via the anchor tool (§3.3 point 1) once that exists.
FIRST_PAGE_TOP_MARGIN = 0.30


@dataclass(frozen=True)
class _MeasureBreak:
    measure: int
    kind: str | None  # "page", "system", or None (continues current system)


def _parse_system_breaks(mxl_path: Path) -> list[_MeasureBreak]:
    """Walk the first part's measures in document order, in beat-map terms.

    Layout `<print>` elements are duplicated identically across every part
    in a full-score MusicXML export (verified for movement 1: 139 breaks,
    byte-identical attributes, across all 15 parts), so any single part is a
    faithful source -- we don't special-case "the piano part" the way
    `parse_mxl_measures` must (that function cares about note content, this
    one only cares about shared layout breaks).
    """

    if not mxl_path.exists():
        raise FileNotFoundError(f"MusicXML score file not found: {mxl_path}")

    with zipfile.ZipFile(mxl_path) as z:
        main_xml_path = None
        if "META-INF/container.xml" in z.namelist():
            container_xml = z.read("META-INF/container.xml")
            root = ET.fromstring(container_xml)
            rootfile = root.find(".//rootfile")
            if rootfile is not None and "full-path" in rootfile.attrib:
                main_xml_path = rootfile.attrib["full-path"]
        if main_xml_path is None:
            xml_names = [
                n
                for n in z.namelist()
                if (n.endswith(".xml") or n.endswith(".musicxml"))
                and not n.startswith("META-INF/")
            ]
            if not xml_names:
                raise ValueError("No MusicXML file found in the zip archive.")
            main_xml_path = xml_names[0]
        xml_data = z.read(main_xml_path)
        root = ET.fromstring(xml_data)

    part = root.find(".//part")
    if part is None:
        raise ValueError(f"No <part> element found in {mxl_path}")

    breaks: list[_MeasureBreak] = []
    for measure in part.findall("measure"):
        num_attr = measure.attrib.get("number")
        if num_attr is None:
            raise ValueError(f"Measure with no 'number' attribute in {mxl_path}")
        try:
            measure_num = int(num_attr)
        except ValueError as exc:
            raise ValueError(f"Non-integer measure number {num_attr!r} in {mxl_path}") from exc

        kind = None
        print_elem = measure.find("print")
        if print_elem is not None:
            if print_elem.attrib.get("new-page") == "yes":
                kind = "page"
            elif print_elem.attrib.get("new-system") == "yes":
                kind = "system"
        breaks.append(_MeasureBreak(measure=measure_num, kind=kind))

    if not breaks:
        raise ValueError(f"No measures found in {mxl_path}")
    return breaks


def _group_into_pages(breaks: list[_MeasureBreak]) -> list[list[tuple[int, int]]]:
    """Fold the ordered measure/break-kind walk into per-page system ranges.

    Returns one list per page; each entry is an inclusive (first_measure,
    last_measure) range for one system on that page, in reading order.
    """

    pages: list[list[tuple[int, int]]] = [[]]
    system_start = breaks[0].measure
    previous_measure = breaks[0].measure

    for measure_break in breaks[1:]:
        if measure_break.kind == "page":
            pages[-1].append((system_start, previous_measure))
            pages.append([])
            system_start = measure_break.measure
        elif measure_break.kind == "system":
            pages[-1].append((system_start, previous_measure))
            system_start = measure_break.measure
        previous_measure = measure_break.measure

    pages[-1].append((system_start, previous_measure))
    return pages


def _validate_pages(
    pages: list[list[tuple[int, int]]], mxl_path: Path
) -> None:
    """Cross-check the system-break walk against `parse_mxl_measures` (§3.3.3).

    Two independent readers of the same file (this module's print-break walk
    and coverage.py's per-measure beat parser) must agree on the measure
    range and on gap-free, monotonically increasing coverage of it -- any
    drift here means a malformed or unexpected MusicXML layout, not a
    trustworthy geometry map.
    """

    all_measures = parse_mxl_measures(mxl_path)
    expected_numbers = sorted(int(m["measure"]) for m in all_measures)

    walked_numbers: list[int] = []
    previous_last: int | None = None
    for page in pages:
        for first, last in page:
            if first > last:
                raise ValueError(f"System range is inverted: ({first}, {last})")
            if previous_last is not None and first != previous_last + 1:
                raise ValueError(
                    f"Non-contiguous system ranges: measure {previous_last} is "
                    f"followed by {first}, expected {previous_last + 1}"
                )
            walked_numbers.extend(range(first, last + 1))
            previous_last = last

    if walked_numbers != expected_numbers:
        raise ValueError(
            "System-break walk disagrees with parse_mxl_measures: "
            f"{len(walked_numbers)} measures walked vs. "
            f"{len(expected_numbers)} parsed from beats/time-signatures"
        )


def build_measure_boxes(
    mxl_path: Path,
    pdf_path: Path,
    *,
    pdf_relative_path: str = "source/score.pdf",
    top_margin: float = DEFAULT_TOP_MARGIN,
    bottom_margin: float = DEFAULT_BOTTOM_MARGIN,
    first_page_top_margin: float = FIRST_PAGE_TOP_MARGIN,
) -> dict[str, object]:
    """Build the Level-1 `measure_boxes.json` payload for one bundle.

    Raises `ValueError`/`FileNotFoundError` on any inconsistency rather than
    emitting silently-wrong geometry -- this is a build-time tool, not a
    runtime path, so failing loudly and refusing to write a bad artifact is
    strictly better than shipping one.
    """

    if not pdf_path.exists():
        raise FileNotFoundError(f"Score PDF not found: {pdf_path}")

    for name, value in (
        ("top_margin", top_margin),
        ("bottom_margin", bottom_margin),
        ("first_page_top_margin", first_page_top_margin),
    ):
        if not 0.0 <= value < 1.0:
            raise ValueError(f"{name} must be in [0.0, 1.0), got {value}")
    if top_margin + bottom_margin >= 1.0:
        raise ValueError(
            f"top_margin ({top_margin}) + bottom_margin ({bottom_margin}) must be < 1.0"
        )
    if first_page_top_margin + bottom_margin >= 1.0:
        raise ValueError(
            f"first_page_top_margin ({first_page_top_margin}) + bottom_margin "
            f"({bottom_margin}) must be < 1.0"
        )

    breaks = _parse_system_breaks(mxl_path)
    pages = _group_into_pages(breaks)
    _validate_pages(pages, mxl_path)

    # Imported here, not at module level: pypdf is a `dev`-extra dependency
    # (like music21 per the design doc, this is a build-time-only tool), and
    # this module lives in the core `aimusic` package. A top-level import
    # would make any future import of `aimusic.takes.measure_boxes` from a
    # production install (without the dev extra) raise ImportError even if
    # nothing on that path actually calls `build_measure_boxes`.
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    pdf_page_count = len(reader.pages)
    # This builder is valid only for a PDF and MusicXML explicitly paired as
    # layout representations of the same engraving. Page-count equality is a
    # local precondition of this print-break algorithm, not a Bundle v2 global
    # invariant: another display edition needs its own OMR/manual display map.
    if pdf_page_count != len(pages):
        raise ValueError(
            f"MusicXML layout implies {len(pages)} pages but {pdf_path} has "
            f"{pdf_page_count} pages -- these files are not layout-compatible; "
            "build a display map from matching layout evidence"
        )

    pages_out: list[dict[str, object]] = []
    for page_index, systems in enumerate(pages):
        page_number = page_index + 1
        # `_group_into_pages` always closes a page with at least one system
        # before opening the next, so this is currently unreachable -- kept
        # as a structural invariant guard rather than trusting that forever,
        # since a `ZeroDivisionError` here would be a much more confusing
        # failure than this message.
        if not systems:
            raise ValueError(f"Page {page_number} has no systems defined.")
        top = first_page_top_margin if page_number == 1 else top_margin
        usable_height = 1.0 - top - bottom_margin
        band_height = usable_height / len(systems)

        systems_out = []
        for system_index, (first_measure, last_measure) in enumerate(systems):
            y0 = top + system_index * band_height
            y1 = y0 + band_height
            systems_out.append(
                {
                    "y0": round(y0, 4),
                    "y1": round(y1, 4),
                    "first_measure": first_measure,
                    "last_measure": last_measure,
                }
            )
        pages_out.append({"page": page_number, "systems": systems_out})

    return {
        "pdf": pdf_relative_path,
        "page_count": pdf_page_count,
        "level": 1,
        "method": "musicxml-system-breaks+even-vertical-split",
        "pages": pages_out,
    }


def write_measure_boxes(piece_id: str, movement: int) -> Path:
    """Build and atomically write `measure_boxes.json` for a bundle.

    Looks up the bundle directory via `paths.score_bundle_dir` (the same
    per-movement registry `coverage.py` and `aligner.py` already use), reads
    `source/score.mxl` and `source/score.pdf`, and writes the result to
    `derived/measure_boxes.json`, matching the `derived/` convention already
    used for movement 2's rendered accompaniment/reference MIDI.
    """

    bundle_dir = paths.score_bundle_dir(piece_id, movement)
    mxl_path = bundle_dir / "source" / "score.mxl"
    pdf_path = bundle_dir / "source" / "score.pdf"

    data = build_measure_boxes(mxl_path, pdf_path)

    derived_dir = bundle_dir / "derived"
    derived_dir.mkdir(parents=True, exist_ok=True)
    out_path = derived_dir / "measure_boxes.json"

    tf_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=derived_dir, delete=False, encoding="utf-8"
        ) as tf:
            tf_path = Path(tf.name)
            json.dump(data, tf, indent=2, sort_keys=True)
            tf.write("\n")
        tf_path.replace(out_path)
    except BaseException:
        # A failure partway through (a disk-full json.dump, a killed
        # process) would otherwise leave a stray temp file behind in
        # `derived/` forever -- clean it up rather than orphan it.
        if tf_path is not None and tf_path.exists():
            tf_path.unlink()
        raise

    return out_path


__all__ = ["build_measure_boxes", "write_measure_boxes"]
