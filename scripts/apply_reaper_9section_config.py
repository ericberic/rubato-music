"""Rebuild the live REAPER zone as 9 range-correct BBCSO sections.

The original live rig collapsed the Oguri orchestra onto 4 BBCSO patches, which
routed double basses onto the Cellos patch (losing their low octave) and
flutes/bassoons onto Clarinets (wrong timbre). BBCSO Discover ships a dedicated
patch for every section this reduction needs, and all nine were already captured
under ``plugin-states/``. This script rewrites the ``room_center`` zone so each
Oguri part plays its own range- and timbre-correct patch.

Every part's full-movement pitch range fits its natural Discover patch (verified
against the Discover manual), so no note falls out of range.

Dry-run by default (prints the diff); pass ``--apply`` to write the machine
config. After applying, reinstall the 9-track bridge and re-run the REAPER
setup to create the 9 tracks and load each patch.
"""

from __future__ import annotations

import argparse
import json

from aimusic.audio.live_config import LiveAudioConfig
from aimusic.audio.live_reaper import _instrument_map
from aimusic.core import paths

PLUGIN_PATH = "/Library/Audio/Plug-Ins/VST3/BBC Symphony Orchestra.vst3"

# (midi_channel, instrument_id, Oguri stem, captured Discover patch state).
# The stem order follows the accompaniment MIDI's named sections.
SECTIONS: tuple[tuple[int, str, str, str], ...] = (
    (0, "violins_1", "accompaniment_t8_c2", "bbcso-violins1-long.state"),
    (1, "violins_2", "accompaniment_t9_c3", "bbcso-violins2-long.state"),
    (2, "violas", "accompaniment_t10_c4", "bbcso-violas-long.state"),
    (3, "cellos", "accompaniment_t11_c5", "bbcso-cellos-long.state"),
    (4, "basses", "accompaniment_t12_c6", "bbcso-basses-long.state"),
    (5, "horns", "accompaniment_t13_c7", "bbcso-horns-long.state"),
    (6, "flutes", "accompaniment_t14_c12", "bbcso-flutes-long.state"),
    (7, "clarinets", "accompaniment_t15_c14", "bbcso-clarinets-long.state"),
    (8, "bassoons", "accompaniment_t16_c15", "bbcso-bassoons-long.state"),
)


def build_instruments() -> list[dict[str, object]]:
    states_dir = paths.state_root() / "plugin-states"
    instruments: list[dict[str, object]] = []
    for channel, instrument_id, stem, state in SECTIONS:
        state_path = states_dir / state
        if not state_path.is_file():
            raise FileNotFoundError(f"missing captured patch state: {state_path}")
        instruments.append(
            {
                "instrument_id": instrument_id,
                "stem_ids": [stem],
                "plugin_path": PLUGIN_PATH,
                "plugin_state_path": str(state_path),
                "midi_channel": channel,
            }
        )
    return instruments


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the machine config")
    args = parser.parse_args()

    config_path = paths.live_audio_config_path()
    document = json.loads(config_path.read_text())
    reaper_zones = [zone for zone in document["zones"] if zone.get("renderer") == "reaper"]
    if len(reaper_zones) != 1:
        raise SystemExit(f"expected exactly one reaper zone, found {len(reaper_zones)}")
    zone = reaper_zones[0]
    zone["label"] = "Living room (LG) — REAPER/BBCSO 9-section"
    zone["instruments"] = build_instruments()

    # Validate with the real model and confirm the router maps every part once.
    parsed = LiveAudioConfig.model_validate(document)
    live_zone = next(z for z in parsed.zones if z.zone_id == "room_center")
    routes = _instrument_map(live_zone)
    print(f"{len(live_zone.instruments)} sections; {len(routes)} unique part routes:")
    for route in routes:
        print(f"  ch{route.channel}: {route.part_id} -> {route.name}")

    if args.apply:
        config_path.write_text(json.dumps(document, indent=2))
        print(f"\nAPPLIED to {config_path}")
        print("Next: reinstall the 9-track bridge and re-run the REAPER setup.")
    else:
        print("\nDry run. Re-run with --apply to write the config.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
