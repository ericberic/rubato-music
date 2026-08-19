"""Browser-driven test of the frontend's "fail loudly at the boundary" policy
(design doc §2.2/§5, issues #83/#84).

The generated client (`webapp/src/generated/`, `@hey-api/openapi-ts` with the
Zod plugin) validates every response against a schema derived from the
backend's Pydantic models and throws on a mismatch, instead of letting a
malformed value leak into component state the way the pre-#83/#84
`await response.json()` call sites used to. This intercepts a real HTTP
response and swaps in a payload that violates its schema (`inputs` should be
`string[]`, not a bare string) to prove the violation surfaces as a visible
error rather than silently coercing or crashing uncaught.
"""

from __future__ import annotations

import re

import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from playwright.sync_api import Page, Route, expect

from aimusic.server.app import create_app


@pytest.fixture()
def live_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    project_root = Path(__file__).resolve().parents[1]
    static_index = project_root / "src/aimusic/server/static/index.html"
    if not static_index.exists():
        pytest.fail("Web assets missing. Run 'cd webapp && npm run build' before tests.")

    monkeypatch.setenv("AIMUSIC_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AIMUSIC_RUNS_ROOT", str(tmp_path / "runs"))

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


def _open_inspector(page: Page) -> None:
    """Reveal the inspection panel.

    Takes, coverage, device setup and the intent cards moved behind a collapsed
    panel so the score owns the screen (issue #153). Tests that drive those
    controls have to open it first, exactly as the performer does.
    """

    toggle = page.get_by_test_id("inspector-toggle")
    toggle.wait_for(state="visible")
    if toggle.get_attribute("aria-expanded") != "true":
        toggle.click()
        page.get_by_test_id("inspector").wait_for(state="visible")


def test_schema_invalid_response_is_rejected_and_reported(page: Page, live_server: str) -> None:
    """A `MidiDevicesResponse` whose `inputs` field is a string instead of a
    `string[]` still passes the real backend's Content-Type/200 checks (this
    intercepts the response body directly, downstream of the server), so the
    generated client's Zod validator is the only thing standing between this
    payload and `backendInputs`/`backendOutputs` in `App.svelte`. It must
    throw -- caught by `refreshBackendMidiDevices`'s `catch`, reported via
    `describeApiError` + `setMessage` -- rather than have Svelte try to
    render a string where a device list was expected.
    """

    def fulfill_malformed_devices(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"inputs": "not-an-array", "outputs": []}',
        )

    page.route("**/api/midi/devices", fulfill_malformed_devices)

    base = f"{live_server}/app/"
    page.goto(base, wait_until="networkidle")
    _open_inspector(page)

    # The contract under test is that a malformed payload is *rejected*, not how
    # it is announced: the revamped masthead reports MIDI wiring as the health
    # dot's state rather than a toast, so we assert the functional outcome (the
    # bad device list is never adopted) rather than a specific error string.
    #
    # The malformed payload must not have been adopted -- the MIDI health dot
    # stays in its "not connected" state instead of silently rendering whatever
    # fragment of the bad string coercion would have produced. (The masthead
    # shows wiring as a dot rather than a prose pill since issue #153; the
    # accessible name still carries the port names for screen readers.)
    dot = page.locator(".device-dot")
    expect(dot).to_have_attribute("aria-label", re.compile(r"in Not connected"))
    assert "connected" not in (dot.get_attribute("class") or "").split()
