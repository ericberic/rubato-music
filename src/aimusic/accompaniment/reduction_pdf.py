"""Build-time helpers for performer-facing score-reduction PDFs.

The display score is a UX artifact, not the canonical musical timeline.  This
module therefore does one deliberately narrow job: extract a reviewed page
window from a source PDF without inventing measure or beat semantics.  A later
bundle build joins the extracted pages to the canonical timeline through a
separate, reviewed display map.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PdfPageWindow:
    """One-based inclusive page window selected from a source PDF."""

    first_page: int
    last_page: int

    def validate(self, *, page_count: int) -> None:
        if self.first_page < 1:
            raise ValueError("first_page must be at least 1")
        if self.last_page < self.first_page:
            raise ValueError("last_page must be greater than or equal to first_page")
        if self.last_page > page_count:
            raise ValueError(
                f"page window {self.first_page}-{self.last_page} exceeds "
                f"the source PDF's {page_count} pages"
            )

    @property
    def page_count(self) -> int:
        return self.last_page - self.first_page + 1


def extract_pdf_page_window(
    source_pdf: Path | str,
    output_pdf: Path | str,
    *,
    first_page: int,
    last_page: int,
) -> int:
    """Extract a one-based inclusive page window and write it atomically.

    ``pypdf`` is imported lazily because it is a build/dev dependency.  The
    live accompanist runtime never needs to mutate PDFs.
    """

    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:  # pragma: no cover - dependency error path
        raise ImportError(
            "pypdf is required for score-source preparation; run "
            "`uv sync --extra dev`."
        ) from exc

    source = Path(source_pdf).expanduser().resolve()
    output = Path(output_pdf).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Source PDF not found: {source}")
    if source == output:
        raise ValueError("source_pdf and output_pdf must be different files")

    reader = PdfReader(str(source))
    window = PdfPageWindow(first_page=first_page, last_page=last_page)
    window.validate(page_count=len(reader.pages))

    writer = PdfWriter()
    for page_index in range(first_page - 1, last_page):
        writer.add_page(reader.pages[page_index])

    output.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            writer.write(handle)
        temp_path.replace(output)
    except BaseException:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise

    return window.page_count


__all__ = ["PdfPageWindow", "extract_pdf_page_window"]
