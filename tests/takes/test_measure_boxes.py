"""Tests for the Level-1 measure-geometry builder (design doc §3.3, roadmap item 7)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfWriter

from aimusic.core import paths
from aimusic.takes import measure_boxes

REPO_ROOT = Path(__file__).resolve().parents[2]


def _measure_xml(
    number: int, *, time_signature: bool = False, break_kind: str | None = None
) -> str:
    attributes = ""
    if time_signature:
        attributes = (
            "<attributes><divisions>1</divisions>"
            "<time><beats>4</beats><beat-type>4</beat-type></time></attributes>"
        )
    print_elem = ""
    if break_kind == "page":
        print_elem = '<print new-page="yes"/>'
    elif break_kind == "system":
        print_elem = '<print new-system="yes"/>'
    return (
        f'<measure number="{number}">{attributes}{print_elem}'
        "<note><rest/><duration>4</duration></note></measure>"
    )


def _write_mxl(path: Path, measures: list[tuple[int, str | None]]) -> None:
    """Write a minimal single-part MusicXML (.mxl) with the given break markers.

    `measures` is a list of (measure_number, break_kind) where break_kind is
    one of "page", "system", or None. The first measure always gets a 4/4
    time signature so `parse_mxl_measures` has something to walk.
    """

    measures_xml = "".join(
        _measure_xml(num, time_signature=(idx == 0), break_kind=kind)
        for idx, (num, kind) in enumerate(measures)
    )
    score_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<score-partwise version="4.0">'
        "<part-list>"
        '<score-part id="P1"><part-name>Piano</part-name></score-part>'
        "</part-list>"
        f'<part id="P1">{measures_xml}</part>'
        "</score-partwise>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        # Deliberately no META-INF/container.xml -- exercises the fallback
        # that searches for any top-level *.xml file, matching what
        # `_parse_system_breaks` and `parse_mxl_measures` both support.
        z.writestr("test_score.xml", score_xml)


def _write_mxl_with_broken_container(path: Path, measures: list[tuple[int, str | None]]) -> None:
    """Like `_write_mxl`, but with a `META-INF/container.xml` present that has
    no usable `full-path` rootfile -- forces the fallback "any *.xml" search,
    which must not pick `META-INF/container.xml` itself back up."""

    measures_xml = "".join(
        _measure_xml(num, time_signature=(idx == 0), break_kind=kind)
        for idx, (num, kind) in enumerate(measures)
    )
    score_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<score-partwise version="4.0">'
        "<part-list>"
        '<score-part id="P1"><part-name>Piano</part-name></score-part>'
        "</part-list>"
        f'<part id="P1">{measures_xml}</part>'
        "</score-partwise>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", '<container><rootfiles/></container>')
        z.writestr("test_score.xml", score_xml)


def _write_pdf(path: Path, n_pages: int) -> None:
    writer = PdfWriter()
    for _ in range(n_pages):
        writer.add_blank_page(width=612, height=792)
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    writer.write(buf)
    path.write_bytes(buf.getvalue())


def test_build_measure_boxes_ignores_meta_inf_in_fallback_search(tmp_path: Path) -> None:
    mxl_path = tmp_path / "score.mxl"
    pdf_path = tmp_path / "score.pdf"
    _write_mxl_with_broken_container(mxl_path, [(1, None), (2, "page"), (3, None)])
    _write_pdf(pdf_path, n_pages=2)

    # Would raise (no <part> found, or a container-xml parse mismatch) if
    # the fallback picked META-INF/container.xml up as the score file.
    data = measure_boxes.build_measure_boxes(mxl_path, pdf_path)
    assert data["page_count"] == 2


def test_build_measure_boxes_two_pages_one_system_each(tmp_path: Path) -> None:
    mxl_path = tmp_path / "score.mxl"
    pdf_path = tmp_path / "score.pdf"
    # Page 1: measures 1-3. Page 2 (new-page at measure 4): measures 4-6.
    _write_mxl(
        mxl_path,
        [
            (1, None),
            (2, None),
            (3, None),
            (4, "page"),
            (5, None),
            (6, None),
        ],
    )
    _write_pdf(pdf_path, n_pages=2)

    data = measure_boxes.build_measure_boxes(mxl_path, pdf_path)

    assert data["page_count"] == 2
    assert data["level"] == 1
    assert len(data["pages"]) == 2

    page1 = data["pages"][0]
    assert page1["page"] == 1
    assert len(page1["systems"]) == 1
    assert page1["systems"][0]["first_measure"] == 1
    assert page1["systems"][0]["last_measure"] == 3

    page2 = data["pages"][1]
    assert page2["page"] == 2
    assert len(page2["systems"]) == 1
    assert page2["systems"][0]["first_measure"] == 4
    assert page2["systems"][0]["last_measure"] == 6


def test_build_measure_boxes_system_break_stays_on_same_page(tmp_path: Path) -> None:
    mxl_path = tmp_path / "score.mxl"
    pdf_path = tmp_path / "score.pdf"
    # One page, two systems: measures 1-3, then a system (not page) break at 4.
    _write_mxl(
        mxl_path,
        [
            (1, None),
            (2, None),
            (3, None),
            (4, "system"),
            (5, None),
        ],
    )
    _write_pdf(pdf_path, n_pages=1)

    data = measure_boxes.build_measure_boxes(mxl_path, pdf_path)

    assert data["page_count"] == 1
    page1 = data["pages"][0]
    assert len(page1["systems"]) == 2
    assert (page1["systems"][0]["first_measure"], page1["systems"][0]["last_measure"]) == (1, 3)
    assert (page1["systems"][1]["first_measure"], page1["systems"][1]["last_measure"]) == (4, 5)


def test_build_measure_boxes_bands_are_normalized_and_non_overlapping(tmp_path: Path) -> None:
    mxl_path = tmp_path / "score.mxl"
    pdf_path = tmp_path / "score.pdf"
    _write_mxl(
        mxl_path,
        [(1, None), (2, "system"), (3, "system")],
    )
    _write_pdf(pdf_path, n_pages=1)

    data = measure_boxes.build_measure_boxes(mxl_path, pdf_path)
    systems = data["pages"][0]["systems"]
    assert len(systems) == 3

    previous_y1 = None
    for system in systems:
        assert 0.0 <= system["y0"] < system["y1"] <= 1.0
        if previous_y1 is not None:
            assert system["y0"] == pytest.approx(previous_y1)
        previous_y1 = system["y1"]


def test_build_measure_boxes_first_page_gets_larger_top_margin(tmp_path: Path) -> None:
    mxl_path = tmp_path / "score.mxl"
    pdf_path = tmp_path / "score.pdf"
    _write_mxl(mxl_path, [(1, None), (2, "page"), (3, None)])
    _write_pdf(pdf_path, n_pages=2)

    data = measure_boxes.build_measure_boxes(mxl_path, pdf_path)
    assert data["pages"][0]["systems"][0]["y0"] == measure_boxes.FIRST_PAGE_TOP_MARGIN
    assert data["pages"][1]["systems"][0]["y0"] == measure_boxes.DEFAULT_TOP_MARGIN


def test_build_measure_boxes_raises_on_pdf_page_count_mismatch(tmp_path: Path) -> None:
    mxl_path = tmp_path / "score.mxl"
    pdf_path = tmp_path / "score.pdf"
    _write_mxl(mxl_path, [(1, None), (2, "page"), (3, None)])
    _write_pdf(pdf_path, n_pages=1)  # MusicXML implies 2 pages

    with pytest.raises(ValueError, match="pages"):
        measure_boxes.build_measure_boxes(mxl_path, pdf_path)


def test_build_measure_boxes_raises_on_empty_page_systems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_group_into_pages` should never produce an empty page, but a defensive
    guard (not a `ZeroDivisionError`) must fire if it ever does."""

    mxl_path = tmp_path / "score.mxl"
    pdf_path = tmp_path / "score.pdf"
    _write_mxl(mxl_path, [(1, None), (2, "page"), (3, None)])
    _write_pdf(pdf_path, n_pages=2)

    monkeypatch.setattr(
        measure_boxes, "_group_into_pages", lambda breaks: [[(1, 3)], []]
    )

    with pytest.raises(ValueError, match="no systems"):
        measure_boxes.build_measure_boxes(mxl_path, pdf_path)


def test_build_measure_boxes_raises_on_missing_pdf(tmp_path: Path) -> None:
    mxl_path = tmp_path / "score.mxl"
    _write_mxl(mxl_path, [(1, None)])

    with pytest.raises(FileNotFoundError):
        measure_boxes.build_measure_boxes(mxl_path, tmp_path / "missing.pdf")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"top_margin": 1.2},
        {"top_margin": -0.1},
        {"bottom_margin": 1.0},
        {"first_page_top_margin": 1.0},
        {"top_margin": 0.6, "bottom_margin": 0.5},
        {"first_page_top_margin": 0.6, "bottom_margin": 0.5},
    ],
)
def test_build_measure_boxes_rejects_invalid_margins(
    tmp_path: Path, kwargs: dict[str, float]
) -> None:
    mxl_path = tmp_path / "score.mxl"
    pdf_path = tmp_path / "score.pdf"
    _write_mxl(mxl_path, [(1, None)])
    _write_pdf(pdf_path, n_pages=1)

    with pytest.raises(ValueError, match="margin"):
        measure_boxes.build_measure_boxes(mxl_path, pdf_path, **kwargs)


def test_write_measure_boxes_writes_derived_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_dir = tmp_path / "fake_bundle"
    mxl_path = bundle_dir / "source" / "score.mxl"
    pdf_path = bundle_dir / "source" / "score.pdf"
    _write_mxl(mxl_path, [(1, None), (2, "page"), (3, None)])
    _write_pdf(pdf_path, n_pages=2)

    monkeypatch.setattr(paths, "score_bundle_dir", lambda piece_id, movement: bundle_dir)

    out_path = measure_boxes.write_measure_boxes("fake_piece", 1)

    assert out_path == bundle_dir / "derived" / "measure_boxes.json"
    assert out_path.exists()

    import json

    data = json.loads(out_path.read_text())
    assert data["page_count"] == 2


def test_build_measure_boxes_on_real_movement_1_bundle() -> None:
    bundle_dir = REPO_ROOT / "assets" / "scores" / "chopin_op11_i_allegro_maestoso"
    mxl_path = bundle_dir / "source" / "score.mxl"
    pdf_path = bundle_dir / "source" / "score.pdf"
    if not mxl_path.exists() or not pdf_path.exists():
        pytest.skip("Real movement-1 score bundle not present in this checkout")

    data = measure_boxes.build_measure_boxes(mxl_path, pdf_path)

    assert data["page_count"] == 98
    total_systems = sum(len(page["systems"]) for page in data["pages"])
    assert total_systems == 140

    first_system = data["pages"][0]["systems"][0]
    assert first_system["first_measure"] == 0

    last_system = data["pages"][-1]["systems"][-1]
    assert last_system["last_measure"] == 689
