"""Observable score-cursor contract shared by browser integration tests."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Page, expect


@dataclass(frozen=True)
class CursorAudit:
    score_beat: float
    measure: int
    page: int
    system: int
    normalized_x: float


def capture_cursor_audit(
    page: Page,
    *,
    expected_measure: int | None = None,
    screenshot_path: Path | None = None,
) -> CursorAudit:
    """Assert that readout, SVG measure, page, and red line tell one story."""

    readout = page.get_by_test_id("score-position-cursor")
    line = page.get_by_test_id("score-position-line")
    expect(readout).to_have_attribute("data-score-beat", re.compile(r"\d"))
    try:
        expect(line).to_have_count(1, timeout=15_000)
    except AssertionError as error:
        state = page.evaluate(
            """() => {
              const readout = document.querySelector('[data-testid="score-position-cursor"]');
              const canvas = document.querySelector(
                '[data-testid="performer-score-page"] canvas'
              );
              return {
                readoutMeasure: readout?.getAttribute('data-score-measure'),
                readoutPage: readout?.getAttribute('data-page'),
                renderedPage: canvas?.getAttribute('data-rendered-page'),
                visibleMeasures: Array.from(
                  document.querySelectorAll('[data-testid^="score-measure-"]')
                ).map((node) => node.getAttribute('data-measure')),
              };
            }"""
        )
        raise AssertionError(f"cursor line did not render: {state}") from error

    # Read every reactive surface in one browser task. Playback advances
    # continuously, so a sequence of independent locator reads can straddle a
    # real measure/page update and manufacture a mismatch that never rendered.
    snapshot = page.evaluate(
        """() => {
          const readout = document.querySelector('[data-testid="score-position-cursor"]');
          const line = document.querySelector('[data-testid="score-position-line"]');
          const measure = line?.getAttribute('data-measure') ?? '-1';
          const box = document.querySelector(`[data-testid="score-measure-${measure}"]`);
          const canvas = document.querySelector(
            '[data-testid="performer-score-page"] canvas',
          );
          const lineRect = line?.getBoundingClientRect();
          const measureRect = box?.getBoundingClientRect();
          return {
            scoreBeat: line?.getAttribute('data-score-beat'),
            measure,
            readoutBeat: readout?.getAttribute('data-score-beat'),
            readoutMeasure: readout?.getAttribute('data-score-measure'),
            renderedPage: canvas?.getAttribute('data-rendered-page'),
            linePage: line?.getAttribute('data-page'),
            system: line?.getAttribute('data-system'),
            boxPage: box?.getAttribute('data-page'),
            boxSystem: box?.getAttribute('data-system'),
            normalizedX: line?.getAttribute('data-cursor-x'),
            x0: box?.getAttribute('x'),
            width: box?.getAttribute('width'),
            linePixelX: lineRect?.x,
            measurePixelX: measureRect?.x,
            measurePixelWidth: measureRect?.width,
          };
        }"""
    )
    score_beat = float(snapshot["scoreBeat"] or "nan")
    measure = int(snapshot["measure"] or "-1")
    rendered_page = int(snapshot["renderedPage"] or "-1")
    line_page = int(snapshot["linePage"] or "-1")
    system = int(snapshot["system"] or "-1")
    normalized_x = float(snapshot["normalizedX"] or "nan")

    assert int(snapshot["readoutMeasure"] or "-1") == measure
    assert float(snapshot["readoutBeat"] or "nan") == score_beat
    assert line_page == rendered_page
    if expected_measure is not None:
        assert measure == expected_measure

    assert int(snapshot["boxPage"] or "-1") == line_page
    assert int(snapshot["boxSystem"] or "-1") == system
    x0 = float(snapshot["x0"] or "nan")
    width = float(snapshot["width"] or "nan")
    assert x0 - 1e-9 <= normalized_x <= x0 + width + 1e-9

    assert (
        float(snapshot["measurePixelX"]) - 1
        <= float(snapshot["linePixelX"])
        <= float(snapshot["measurePixelX"]) + float(snapshot["measurePixelWidth"]) + 1
    )

    if screenshot_path is not None:
        page.screenshot(path=str(screenshot_path), full_page=False)
        assert screenshot_path.stat().st_size > 0

    return CursorAudit(
        score_beat=score_beat,
        measure=measure,
        page=line_page,
        system=system,
        normalized_x=normalized_x,
    )
