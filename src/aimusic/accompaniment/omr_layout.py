"""Extract reviewed display geometry from an Audiveris ``.omr`` project.

Audiveris' MusicXML export is intentionally lossy.  The saved OMR project,
however, retains scan-space page dimensions, staff lines, systems, and measure
stack barlines.  This module converts that evidence into normalized measure
boxes without treating recognition output as canonical score semantics.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median

_SHEET_XML_PATTERN = re.compile(r"^sheet#(?P<number>\d+)/sheet#\d+\.xml$")


@dataclass(frozen=True)
class OmrLayoutBox:
    """One detected PDF rectangle before it is joined to score semantics."""

    box_index: int
    page: int
    system: int
    # PAGE-NORMALIZED measure bounding box: fractions of the page image in
    # [0, 1], origin at the page's top-left. x0/x1 are divided by the sheet
    # pixel width and y0/y1 by the sheet pixel height at extraction time, so the
    # stored values are resolution-independent, NOT raw scan pixels. Source is
    # the Audiveris *grid* pass (barline + staff-line geometry) -- a different
    # pass and frame from the note default_x. See
    # docs/concepts/score-coordinate-systems.md.
    x0: float
    x1: float
    y0: float
    y1: float


@dataclass(frozen=True)
class OmrDisplayLayout:
    schema_version: int
    source_kind: str
    review_state: str
    page_count: int
    box_count: int
    boxes: tuple[OmrLayoutBox, ...]


def extract_omr_display_layout(omr_path: Path | str) -> OmrDisplayLayout:
    """Extract zero-based sequential boxes in normalized page coordinates."""

    path = Path(omr_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Audiveris OMR project not found: {path}")

    boxes: list[OmrLayoutBox] = []
    with zipfile.ZipFile(path) as archive:
        sheet_members: list[tuple[int, str]] = []
        for name in archive.namelist():
            if match := _SHEET_XML_PATTERN.match(name):
                sheet_members.append((int(match.group("number")), name))
        sheet_members.sort()
        if not sheet_members:
            raise ValueError(f"No sheet XML found in Audiveris project: {path}")

        expected_page = 1
        box_index = 0
        for page_number, member_name in sheet_members:
            if page_number != expected_page:
                raise ValueError(
                    f"Audiveris sheets must be contiguous: expected page "
                    f"{expected_page}, found {page_number}"
                )
            root = ET.fromstring(archive.read(member_name))
            picture = root.find("picture")
            if picture is None:
                raise ValueError(f"Sheet {page_number} has no picture dimensions")
            width = float(picture.attrib["width"])
            height = float(picture.attrib["height"])
            if width <= 0 or height <= 0:
                raise ValueError(f"Sheet {page_number} has invalid dimensions")

            interline_element = root.find("./scale/interline")
            interline = (
                float(interline_element.attrib.get("main", 0))
                if interline_element is not None
                else 0.0
            )
            vertical_padding = max(1.0, interline * 1.5)

            systems = root.findall("./page/system")
            if not systems:
                raise ValueError(f"Sheet {page_number} has no recognized systems")
            for system_number, system in enumerate(systems, start=1):
                y_values = [
                    float(point.attrib["y"])
                    for point in system.findall("./part/staff/lines/line/point")
                ]
                if not y_values:
                    raise ValueError(
                        f"Sheet {page_number} system {system_number} has no staff-line geometry"
                    )
                y0 = max(0.0, min(y_values) - vertical_padding) / height
                y1 = min(height, max(y_values) + vertical_padding) / height

                boundaries = _system_measure_boundaries(system, interline=interline)
                if len(boundaries) < 2:
                    raise ValueError(
                        f"Sheet {page_number} system {system_number} has fewer than "
                        "two reliable barline boundaries"
                    )
                for left, right in zip(boundaries, boundaries[1:]):
                    if not 0 <= left < right <= width:
                        raise ValueError(
                            f"Invalid measure stack bounds on sheet {page_number}: "
                            f"left={left}, right={right}, width={width}"
                        )
                    boxes.append(
                        OmrLayoutBox(
                            box_index=box_index,
                            page=page_number,
                            system=system_number,
                            x0=round(left / width, 6),
                            x1=round(right / width, 6),
                            y0=round(y0, 6),
                            y1=round(y1, 6),
                        )
                    )
                    box_index += 1
            expected_page += 1

    return OmrDisplayLayout(
        schema_version=2,
        source_kind="audiveris-omr-layout-evidence",
        review_state="machine",
        page_count=expected_page - 1,
        box_count=len(boxes),
        boxes=tuple(boxes),
    )


def _system_measure_boundaries(system: ET.Element, *, interline: float) -> list[float]:
    """Read stack bounds or reconstruct them from GRID-stage barlines.

    Audiveris creates explicit ``stack`` elements at its later MEASURES stage.
    For display layout we can stop much earlier at GRID and cluster the barlines
    already detected independently on each staff. This avoids running note and
    rhythm recognition merely to recover page geometry.
    """

    stacks = system.findall("stack")
    if stacks:
        return [
            float(stacks[0].attrib["left"]),
            *(float(stack.attrib["right"]) for stack in stacks),
        ]

    barline_x_by_id: dict[str, float] = {}
    for barline in system.findall("./sig/inters/barline"):
        identifier = barline.attrib.get("id")
        median_element = barline.find("median")
        points = median_element.findall("p1") if median_element is not None else []
        if identifier and points:
            barline_x_by_id[identifier] = float(points[0].attrib["x"])
        elif identifier and (bounds := barline.find("bounds")) is not None:
            barline_x_by_id[identifier] = float(bounds.attrib["x"]) + float(bounds.attrib["w"]) / 2

    staff_count = 0
    observations: list[float] = []
    for staff in system.findall("./part/staff"):
        identifiers = (staff.findtext("barlines") or "").split()
        xs = [
            barline_x_by_id[identifier]
            for identifier in identifiers
            if identifier in barline_x_by_id
        ]
        if xs:
            staff_count += 1
            observations.extend(xs)
    if staff_count == 0:
        return []

    tolerance = max(2.0, interline)
    clusters: list[list[float]] = []
    for x in sorted(observations):
        if not clusters or x - median(clusters[-1]) > tolerance:
            clusters.append([x])
        else:
            clusters[-1].append(x)
    minimum_support = math.ceil(staff_count / 2)
    return [float(median(cluster)) for cluster in clusters if len(cluster) >= minimum_support]


def write_omr_display_layout(
    display_layout: OmrDisplayLayout, output_path: Path | str
) -> Path:
    """Atomically serialize raw OMR layout evidence as reviewable JSON."""

    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(display_layout)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=output.parent, delete=False, encoding="utf-8"
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        temp_path.replace(output)
    except BaseException:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise
    return output


__all__ = [
    "OmrDisplayLayout",
    "OmrLayoutBox",
    "extract_omr_display_layout",
    "write_omr_display_layout",
]
