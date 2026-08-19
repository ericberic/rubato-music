#!/usr/bin/env python3
"""Overlay measure boxes and beat markers on the score PDF for eyeball checking.

Draws, on top of the engraving:
  - each measure's box (from the Audiveris grid pass), labelled with its number;
  - a small down-pointing triangle at every beat position (from the beat map),
    so a mismatch between a beat marker and the note it should sit under is
    obvious at a glance.

All geometry is page-normalized [0,1] (see docs/concepts/score-coordinate-
systems.md). reportlab's origin is bottom-left with y up, so y is flipped from
the top-origin fractions.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import Color
from reportlab.pdfgen import canvas


def overlay_page(width: float, height: float, systems: list[dict]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    box_stroke = Color(0.85, 0.25, 0.20, alpha=0.9)  # ember
    box_fill = Color(0.85, 0.25, 0.20, alpha=0.05)
    label_col = Color(0.85, 0.25, 0.20, alpha=1.0)
    # Beat triangle colour encodes geometry confidence: green = a clean staff
    # pinned it, amber = weaker, grey = fell back to even spacing.
    def beat_color(conf: float) -> Color:
        if conf >= 0.75:
            return Color(0.10, 0.55, 0.20, alpha=0.95)   # green
        if conf >= 0.35:
            return Color(0.85, 0.55, 0.10, alpha=0.95)   # amber
        return Color(0.45, 0.45, 0.45, alpha=0.9)        # grey (fallback)

    for system in systems:
        # y fractions are from the page top; flip for reportlab's bottom origin.
        y_top = (1.0 - system["y0"]) * height
        y_bot = (1.0 - system["y1"]) * height
        for measure in system["measures"]:
            x0 = measure["x0"] * width
            x1 = measure["x1"] * width
            c.setStrokeColor(box_stroke)
            c.setFillColor(box_fill)
            c.setLineWidth(0.8)
            c.rect(x0, y_bot, x1 - x0, y_top - y_bot, stroke=1, fill=1)
            # measure number at the box's top-left, just above the staff band
            c.setFillColor(label_col)
            c.setFont("Helvetica-Bold", 8)
            c.drawString(x0 + 1.5, y_top + 2, str(measure["measure"]))
            # Which staff located these beats -- small and grey so it does not
            # crowd the number. "P1:2" = part P1, staff 2 (the piano left hand).
            staff = measure.get("beat_staff")
            if staff:
                c.setFillColor(Color(0.4, 0.4, 0.4, alpha=0.9))
                c.setFont("Helvetica", 5)
                c.drawString(x0 + 1.5, y_top + 8.5, staff)
            # beat triangles pointing DOWN onto the staff from the top edge
            for beat in measure.get("beats", []):
                bx = beat["x"] * width
                s = 3.2  # triangle half-width, points
                tip_y = y_top
                base_y = y_top + 7
                col = beat_color(beat.get("confidence", 1.0))
                c.setFillColor(col)
                c.setStrokeColor(col)
                c.setLineWidth(0.7)
                path = c.beginPath()
                path.moveTo(bx, tip_y)          # tip on the staff
                path.lineTo(bx - s, base_y)
                path.lineTo(bx + s, base_y)
                path.close()
                # Solid triangle = a real note sat on this beat; hollow = the
                # position was interpolated or evenly spaced (no note there).
                anchored = beat.get("anchored", True)
                c.drawPath(path, stroke=1, fill=1 if anchored else 0)
                c.setFont("Helvetica", 5)
                c.drawCentredString(bx, base_y + 1.5, _beat_label(beat["beat_in_measure"]))
    c.showPage()
    c.save()
    return buf.getvalue()


def _beat_label(beat_in_measure: float) -> str:
    # beat_in_measure is 0-based; show the musician's 1-based beat.
    value = beat_in_measure + 1.0
    return f"{value:g}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", required=True, help="the score PDF")
    ap.add_argument("--boxes", required=True, help="measure_boxes JSON (from the API)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    boxes = json.loads(Path(args.boxes).read_text(encoding="utf-8"))
    by_page = {page["page"]: page["systems"] for page in boxes["pages"]}

    reader = PdfReader(args.pdf)
    writer = PdfWriter()
    for index, page in enumerate(reader.pages, start=1):
        media = page.mediabox
        w, h = float(media.width), float(media.height)
        systems = by_page.get(index, [])
        if systems:
            overlay_pdf = PdfReader(io.BytesIO(overlay_page(w, h, systems)))
            page.merge_page(overlay_pdf.pages[0])
        writer.add_page(page)

    with open(args.out, "wb") as handle:
        writer.write(handle)
    print(f"wrote {args.out} ({len(reader.pages)} pages, "
          f"{sum(len(s['measures']) for ss in by_page.values() for s in ss)} measures)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
