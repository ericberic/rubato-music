"""Headless browser test for the MIDI console."""

from __future__ import annotations

import re
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from playwright.sync_api import Page, expect
from pypdf import PdfWriter

from aimusic.core import paths
from aimusic.server.app import create_app
from tests.oguri_guard import requires_oguri_derived


@pytest.fixture()
def live_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    project_root = Path(__file__).resolve().parents[1]
    static_index = project_root / "src/aimusic/server/static/index.html"
    if not static_index.exists():
        pytest.fail("Web assets missing. Run 'cd webapp && npm run build' before tests.")

    data_root = tmp_path / "data"
    runs_root = tmp_path / "runs"
    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(data_root))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(runs_root))

    movement_two_pdf = paths.score_display_pdf_path("chopin_op11", 2)
    if not movement_two_pdf.exists():
        fixture_pdf = tmp_path / "movement-two-fixture.pdf"
        writer = PdfWriter()
        for _ in range(15):
            writer.add_blank_page(width=612, height=792)
        with fixture_pdf.open("wb") as output:
            writer.write(output)
        original_score_pdf = paths.score_display_pdf_path

        def score_pdf(piece_id: str, movement: int) -> Path:
            if (piece_id, movement) == ("chopin_op11", 2):
                return fixture_pdf
            return original_score_pdf(piece_id, movement)

        monkeypatch.setattr(paths, "score_display_pdf_path", score_pdf)

    app = create_app()
    port = _find_open_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_for(lambda: _server_ready(port), timeout=5)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _find_open_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _server_ready(port: int) -> bool:
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/", timeout=0.2)
        return response.status_code < 500
    except httpx.HTTPError:
        return False


def _wait_for(predicate, timeout: float) -> None:
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        if predicate():
            return
        time.sleep(0.05)
    raise TimeoutError("Server did not start in time")


def test_create_upload_and_status(page: Page, live_server: str):
    base = f"{live_server}/app/"
    page.goto(base, wait_until="networkidle")

    # Session management lives in the collapsed Library drawer.
    page.locator("details.library > summary").click()

    session_input = page.locator("#session-id-input")
    session_input.wait_for()
    session_input.fill("web-e2e")
    page.get_by_role("button", name="Create Slot").click()
    expect(page.locator(".toast")).to_have_text("Ready to use web-e2e for accompaniment")

    upload = page.locator("input[data-testid='upload-input']")
    upload.set_input_files(str(Path("tests/fixtures/simple.mid").resolve()))

    ready_spans = page.locator(".accompanist-status span.ready")
    expect(ready_spans.first).to_have_text("solo.mid")

    playback_row = page.locator(".accompanist-status .playback-row").first
    expect(playback_row).to_contain_text("Solo recording")
    download_link = playback_row.locator("a")
    expect(download_link).to_have_attribute("href", "/api/sessions/web-e2e/midi/solo")


def test_score_coverage_overlay_renders_pdf_with_machine_geometry(page: Page, live_server: str):
    """The Movement 2 reduction and machine-review geometry render together.

    Regression coverage for a real bug found while building this feature:
    `PdfCoverageOverlay` used to both assign `pdfDoc` (which a reactive
    statement watches to trigger the first render) *and* call `renderPage`
    directly from the same load path, racing two `page.render()` calls
    against one canvas 2D context. pdf.js does not fail loudly when that
    happens -- it wedges the render pipeline forever with no thrown error,
    which froze the whole page (this exact test's locator waits started
    timing out). This test drives the real render + page-navigation path
    end-to-end so that regression cannot land silently again.
    """

    base = f"{live_server}/app/"
    page.goto(base, wait_until="networkidle")

    reduction = (
        Path(__file__).resolve().parents[1]
        / "data/scores/chopin_op11_movement_2/source/joseffy_reduction_movement2.pdf"
    )
    if not reduction.exists():
        pytest.skip("Movement 2 DVC display-score artifact has not been pulled")

    # The score no longer introduces itself in prose (issue #153): the kicker,
    # the "Rehearsal score" title and the paragraph describing the reduction sat
    # above the engraving competing with it, and the estimate caveat moved onto
    # the anchor control as a tooltip. The heading survives for assistive tech,
    # so assert it is *present and named*, not painted.
    heading = page.get_by_role("heading", name=re.compile("Rehearsal score"), include_hidden=True)
    expect(heading).to_have_count(1)
    expect(page.get_by_text("Joseffy two-piano reduction", exact=False)).to_have_count(0)

    page_indicator = page.locator(".page-indicator")
    expect(page_indicator).to_have_text("page 1 / 15", timeout=15000)

    canvas = page.locator(".pdf-page-wrap canvas")
    expect(canvas).to_be_visible()
    expect(canvas).to_have_attribute("data-rendered-page", "1", timeout=15000)
    expect(page.locator(".pdf-overlay-svg > rect")).to_have_count(18)

    def pixel_counts() -> dict:
        return canvas.evaluate(
            """(el) => {
                const ctx = el.getContext('2d');
                const data = ctx.getImageData(0, 0, el.width, el.height).data;
                let white = 0, ink = 0;
                for (let i = 0; i < data.length; i += 4) {
                    const [r, g, b] = [data[i], data[i + 1], data[i + 2]];
                    if (r > 240 && g > 240 && b > 240) white++;
                    else if (r < 128 && g < 128 && b < 128) ink++;
                }
                return { white, ink, total: data.length / 4 };
            }"""
        )

    # A real rendered score page is mostly white paper with some black ink --
    # not a blank/solid canvas (which is what the double-render deadlock
    # produced: a canvas sized correctly but never actually painted).
    counts = pixel_counts()
    assert counts["white"] > counts["total"] * 0.3
    assert counts["ink"] > 0

    # exact=True: the Take Capture deck's cue/rehearsal buttons also start
    # with "▶" (design doc §3.1), so a substring match here would be ambiguous
    # with Playwright's strict mode.
    next_button = page.get_by_role("button", name="▶", exact=True)
    next_button.click()
    expect(page_indicator).to_have_text("page 2 / 15")
    expect(canvas).to_have_attribute("data-rendered-page", "2", timeout=15000)

    # Re-render after navigating must complete too (the deadlock reproduced
    # equally on subsequent page changes, not just the first render).
    counts_page_2 = pixel_counts()
    assert counts_page_2["white"] > counts_page_2["total"] * 0.3


@requires_oguri_derived
def test_mixing_is_separate_persistent_and_revisioned(page: Page, live_server: str):
    page.goto(f"{live_server}/app/", wait_until="networkidle")
    expect(page.get_by_test_id("workspace-switcher")).to_be_visible()
    assert page.get_by_test_id("workspace-switcher").inner_text().split() == [
        "◇",
        "Data",
        "∿",
        "Mix",
        "▶",
        "Perform",
    ]
    expect(page.get_by_test_id("mode-perform")).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_test_id("score-measure-label-1")).to_be_visible(timeout=15000)
    expect(page.get_by_test_id("score-guide")).to_have_count(0)
    expect(page.locator("[data-testid^='mix-region-']")).to_have_count(0)
    page.get_by_test_id("mode-mixing").click()

    expect(page.get_by_test_id("mix-mode-identity")).to_contain_text("Main spatial mix")
    expect(page.get_by_test_id("mode-mixing")).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_test_id("score-guide")).to_have_count(0)
    expect(page.get_by_test_id("anchor-marker")).to_have_count(0)
    expect(page.get_by_test_id("base-mix")).to_have_count(0)
    expect(page.get_by_test_id("score-measure-label-1")).to_be_visible()

    page.get_by_test_id("score-measure-1").click(button="right", position={"x": 12, "y": 12})
    point_menu = page.get_by_test_id("mix-context-menu")
    expect(point_menu.get_by_role("button", name="Start effect here")).to_be_visible()
    expect(point_menu.get_by_role("button", name="End effect here")).to_be_disabled()
    point_menu.get_by_role("button", name="Start effect here").click()
    expect(page.get_by_test_id("mix-draft-start")).to_be_visible()

    first = page.get_by_test_id("score-measure-1")
    second = page.get_by_test_id("score-measure-2")
    expect(first).to_be_visible(timeout=15000)
    first_box = first.bounding_box()
    second_box = second.bounding_box()
    assert first_box is not None and second_box is not None
    page.mouse.move(first_box["x"] + first_box["width"] * 0.25, first_box["y"] + 8)
    page.mouse.down()
    page.mouse.move(second_box["x"] + second_box["width"] * 0.75, second_box["y"] + 8)
    page.mouse.up()
    expect(page.get_by_test_id("mix-selection").first).to_be_visible()
    expect(page.get_by_role("button", name="▶ Audition live")).to_be_disabled()
    expect(page.get_by_text("Live BBCSO zone needs configuration and calibration.")).to_be_visible()

    page.get_by_role("button", name="Create mix region").click()
    menu = page.get_by_test_id("mix-context-menu")
    expect(menu).to_be_visible()
    menu.get_by_role("combobox", name="Mix gesture").select_option("feature")
    menu.get_by_label("Mix stem or score part").fill("oboe")
    menu.get_by_role("slider", name="Region start volume").fill("10")
    menu.get_by_role("slider", name="Region end volume").fill("90")
    menu.get_by_role("button", name="Create region").click()
    expect(page.locator("[data-testid^='mix-region-']").first).to_be_visible()
    program_response = page.request.get(
        f"{live_server}/api/mix/programs/main?piece_id=chopin_op11&movement=2"
    )
    assert program_response.ok
    authored = next(
        region
        for region in program_response.json()["regions"]
        if region["region_id"].startswith("region_")
    )
    assert authored["gesture"] == "feature"
    assert authored["routes"][0]["stem_ids"] == ["oboe"]
    assert len(authored["routes"][0]["envelope"]) == 4
    assert authored["routes"][0]["envelope"][-1]["level"] == 10

    page.get_by_test_id("mode-perform").click()
    expect(page.get_by_test_id("mode-perform")).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_test_id("score-guide")).to_have_count(0)
    expect(page.locator("[data-testid^='mix-region-']")).to_have_count(0)
    page.get_by_test_id("layer-toggle").click()
    page.get_by_role("checkbox", name="Mix cues").check()
    expect(page.locator("[data-testid^='mix-region-']").first).to_be_visible()
    page.get_by_test_id("layer-toggle").click()
    expect(page.get_by_test_id("score-measure-label-1")).to_be_visible()
    page.get_by_test_id("mode-data").click()
    expect(page.get_by_test_id("mode-data")).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_test_id("score-guide")).to_be_visible()
    expect(page.get_by_test_id("score-measure-label-1")).to_be_visible()
    expect(page.get_by_test_id("data-workspace-panel")).to_be_visible()
    page.reload(wait_until="networkidle")
    page.get_by_test_id("mode-mixing").click()
    expect(page.locator("[data-testid^='mix-region-']").first).to_be_visible()
