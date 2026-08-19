import json
import zipfile
from pathlib import Path

import pytest

from aimusic.accompaniment.omr_layout import extract_omr_display_layout
from scripts.build_display_map import main as build_display_map


def _sheet_xml(*, width: int = 1000, height: int = 2000, left: int = 100) -> str:
    return f"""\
<sheet>
  <picture width="{width}" height="{height}"/>
  <scale><interline main="20"/></scale>
  <page>
    <system id="1">
      <stack id="1" left="{left}" right="400"/>
      <stack id="2" left="400" right="900"/>
      <part id="1"><staff id="1"><lines>
        <line><point x="100" y="200"/><point x="900" y="205"/></line>
        <line><point x="100" y="280"/><point x="900" y="285"/></line>
      </lines></staff></part>
      <part id="2"><staff id="2"><lines>
        <line><point x="100" y="500"/><point x="900" y="505"/></line>
        <line><point x="100" y="580"/><point x="900" y="585"/></line>
      </lines></staff></part>
    </system>
  </page>
</sheet>
"""


def _write_omr(path: Path, sheets: list[tuple[int, str]]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for page, xml in sheets:
            archive.writestr(f"sheet#{page}/sheet#{page}.xml", xml)


def test_extracts_normalized_sequential_measure_boxes(tmp_path: Path) -> None:
    omr = tmp_path / "score.omr"
    _write_omr(omr, [(1, _sheet_xml()), (2, _sheet_xml(left=150))])

    result = extract_omr_display_layout(omr)

    assert result.schema_version == 2
    assert result.page_count == 2
    assert result.box_count == 4
    assert result.review_state == "machine"
    assert [box.box_index for box in result.boxes] == [0, 1, 2, 3]
    assert result.boxes[0].page == 1
    assert result.boxes[0].x0 == 0.1
    assert result.boxes[0].x1 == 0.4
    assert result.boxes[0].y0 == 0.085
    assert result.boxes[0].y1 == 0.3075
    assert result.boxes[2].page == 2
    assert result.boxes[2].x0 == 0.15


def test_rejects_noncontiguous_sheets(tmp_path: Path) -> None:
    omr = tmp_path / "score.omr"
    _write_omr(omr, [(1, _sheet_xml()), (3, _sheet_xml())])

    with pytest.raises(ValueError, match="contiguous"):
        extract_omr_display_layout(omr)


def test_rejects_invalid_stack_geometry(tmp_path: Path) -> None:
    omr = tmp_path / "score.omr"
    _write_omr(omr, [(1, _sheet_xml(left=950))])

    with pytest.raises(ValueError, match="Invalid measure stack bounds"):
        extract_omr_display_layout(omr)


def test_extracts_grid_stage_barlines_without_measure_stacks(tmp_path: Path) -> None:
    xml = (
        _sheet_xml()
        .replace(
            '<stack id="1" left="100" right="400"/>\n      <stack id="2" left="400" right="900"/>',
            "",
        )
        .replace(
            "</system>",
            """<sig><inters>
        <barline id="10"><median><p1 x="100" y="1"/></median></barline>
        <barline id="11"><median><p1 x="400" y="1"/></median></barline>
        <barline id="12"><median><p1 x="900" y="1"/></median></barline>
      </inters></sig></system>""",
        )
        .replace('<staff id="1">', '<staff id="1"><barlines>10 11 12</barlines>')
        .replace('<staff id="2">', '<staff id="2"><barlines>10 11 12</barlines>')
    )
    omr = tmp_path / "grid.omr"
    _write_omr(omr, [(1, xml)])

    result = extract_omr_display_layout(omr)

    assert result.box_count == 2
    assert [(box.x0, box.x1) for box in result.boxes] == [(0.1, 0.4), (0.4, 0.9)]


def test_display_map_builder_rejects_removed_schema_v1_layout(tmp_path: Path) -> None:
    layout = tmp_path / "legacy-layout.json"
    layout.write_text(
        json.dumps({"schema_version": 1, "measure_count": 1, "measures": []}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schema version 2"):
        build_display_map(
            [
                str(layout),
                str(tmp_path / "timeline.json"),
                str(tmp_path / "output.json"),
                "--mapping-id",
                "test-map",
                "--source-id",
                "test-source",
            ]
        )
