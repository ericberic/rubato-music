"""Skip guards for tests that depend on the non-redistributable Oguri MIDI.

The Chopin Movement 2 solo/orchestra reference MIDI is derived from a private
kunstderfuge.com performance (see ``docs/OPEN_SOURCE_READINESS.md``). Neither the
source nor its derivatives are committed to git, so they are:

* present in a normal local checkout (fetch the source, then run
  ``uv run python -m aimusic.cli extract-oguri --movement 2``), and
* absent in the scrubbed public snapshot / a fresh clone.

Tests that read those files are skipped when they are missing rather than
failing, so the public snapshot's ``pytest`` run stays green while local runs
keep full coverage. Apply at module scope::

    from tests.oguri_guard import requires_oguri_derived
    pytestmark = requires_oguri_derived

or to a single test::

    @requires_oguri_derived
    def test_needs_the_reference() -> None:
        ...
"""

from __future__ import annotations

import pytest

from aimusic.core import paths

_BUNDLE = paths.project_root() / "data" / "scores" / "chopin_op11_movement_2"

OGURI_SOURCE_PATH = _BUNDLE / "source" / "oguri_concerto_11_2.mid"
SOLO_REFERENCE_PATH = _BUNDLE / "derived" / "solo_reference.mid"
ORCHESTRA_ACCOMPANIMENT_PATH = _BUNDLE / "derived" / "orchestra_accompaniment.mid"

OGURI_SOURCE_PRESENT = OGURI_SOURCE_PATH.is_file()
OGURI_DERIVED_PRESENT = SOLO_REFERENCE_PATH.is_file() and ORCHESTRA_ACCOMPANIMENT_PATH.is_file()

_REGEN_HINT = "run `uv run python -m aimusic.cli extract-oguri --movement 2`"

requires_oguri_derived = pytest.mark.skipif(
    not OGURI_DERIVED_PRESENT,
    reason=f"Oguri-derived Movement 2 MIDI absent (non-redistributable; {_REGEN_HINT})",
)

requires_oguri_source = pytest.mark.skipif(
    not OGURI_SOURCE_PRESENT,
    reason="Oguri source MIDI absent (non-redistributable; fetch it for private use)",
)
