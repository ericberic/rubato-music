from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from aimusic.accompaniment.reduction_pdf import PdfPageWindow, extract_pdf_page_window


def _write_blank_pdf(path: Path, page_count: int) -> None:
    writer = PdfWriter()
    for page_number in range(page_count):
        writer.add_blank_page(width=600 + page_number, height=800 + page_number)
    with path.open("wb") as handle:
        writer.write(handle)


def test_extract_pdf_page_window_uses_one_based_inclusive_pages(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    output = tmp_path / "movement.pdf"
    _write_blank_pdf(source, 6)

    count = extract_pdf_page_window(source, output, first_page=2, last_page=4)

    assert count == 3
    result = PdfReader(str(output))
    assert len(result.pages) == 3
    assert float(result.pages[0].mediabox.width) == 601
    assert float(result.pages[-1].mediabox.width) == 603


@pytest.mark.parametrize(
    ("first_page", "last_page", "message"),
    [
        (0, 2, "first_page"),
        (3, 2, "last_page"),
        (1, 7, "exceeds"),
    ],
)
def test_page_window_validation_rejects_invalid_ranges(
    first_page: int, last_page: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        PdfPageWindow(first_page, last_page).validate(page_count=6)


def test_extract_pdf_page_window_rejects_in_place_rewrite(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _write_blank_pdf(source, 2)

    with pytest.raises(ValueError, match="different files"):
        extract_pdf_page_window(source, source, first_page=1, last_page=1)
