#!/usr/bin/env python3
"""Ground-truth tooling for score localization (see .agents/skills/score-localization).

Every subcommand here answers one question with **independent provenance**: the
two sides of each comparison never come from the same derivation. That property
is the whole point -- Rubato previously shipped a beat map that was a measure
wrong while every self-consistent check passed.
"""

from __future__ import annotations

import argparse
import collections
import difflib
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import mido

PITCH_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_STEP = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


# ---------------------------------------------------------------- score reading


def _load_musicxml_root(path: Path) -> ElementTree.Element:
    if path.suffix == ".mxl":
        with zipfile.ZipFile(path) as archive:
            names = [
                n for n in archive.namelist() if n.endswith(".xml") and "META-INF" not in n
            ]
            if not names:
                raise SystemExit(f"{path}: no score xml inside the .mxl container")
            return ElementTree.fromstring(archive.read(names[0]))
    return ElementTree.parse(path).getroot()


def _midi_of(pitch_el: ElementTree.Element) -> int:
    octave = int(pitch_el.findtext("octave"))
    step = _STEP[pitch_el.findtext("step")]
    alter = int(pitch_el.findtext("alter") or 0)
    return (octave + 1) * 12 + step + alter


def measure_contents(path: Path | str) -> dict[str, dict[str, list[tuple[float, int]]]]:
    """part id -> measure number -> [(beat_offset, midi_pitch)].

    Beat offsets honour <backup>/<forward> and chords, so multi-staff parts do
    not accumulate a bogus running position. Grace notes are attached to the
    beat they precede rather than advancing time.
    """

    root = _load_musicxml_root(Path(path))
    out: dict[str, dict[str, list[tuple[float, int]]]] = {}
    for part in root.findall("part"):
        pid = part.get("id") or "?"
        by_measure: dict[str, list[tuple[float, int]]] = {}
        divisions = 1.0
        for measure in part.findall("measure"):
            number = measure.get("number") or "?"
            div_text = measure.findtext("attributes/divisions")
            if div_text:
                divisions = float(div_text)
            position = 0.0
            previous = 0.0
            notes: list[tuple[float, int]] = []
            for el in measure:
                if el.tag == "backup":
                    position -= float(el.findtext("duration") or 0) / divisions
                elif el.tag == "forward":
                    position += float(el.findtext("duration") or 0) / divisions
                elif el.tag == "note":
                    is_chord = el.find("chord") is not None
                    is_grace = el.find("grace") is not None
                    at = previous if is_chord else position
                    pitch_el = el.find("pitch")
                    if pitch_el is not None:
                        notes.append((round(at, 4), _midi_of(pitch_el)))
                    if not is_chord and not is_grace:
                        previous = position
                        position += float(el.findtext("duration") or 0) / divisions
            by_measure[number] = sorted(notes)
        out[pid] = by_measure
    return out


def _parse_range(spec: str) -> list[str]:
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        return [str(n) for n in range(int(lo), int(hi) + 1)]
    return [s.strip() for s in spec.split(",")]


# ------------------------------------------------------------- midi extraction


def midi_onsets(path: Path, track_filter: str | None = None) -> list[tuple[float, int, str]]:
    """[(seconds, pitch, track_name)] honouring the file's tempo map."""

    midi = mido.MidiFile(path)
    ppq = midi.ticks_per_beat
    tempos: list[tuple[int, int]] = []
    for track in midi.tracks:
        tick = 0
        for message in track:
            tick += message.time
            if message.type == "set_tempo":
                tempos.append((tick, message.tempo))
    tempos.sort()

    def seconds(target: int) -> float:
        total = 0.0
        last = 0
        current = 500000
        for tick, tempo in tempos:
            if tick >= target:
                break
            total += mido.tick2second(tick - last, ppq, current)
            last, current = tick, tempo
        return total + mido.tick2second(target - last, ppq, current)

    out: list[tuple[float, int, str]] = []
    for track in midi.tracks:
        name = (track.name or "").strip()
        if track_filter and track_filter.lower() not in name.lower():
            continue
        tick = 0
        for message in track:
            tick += message.time
            if message.type == "note_on" and message.velocity > 0:
                out.append((seconds(tick), message.note, name))
    out.sort()
    return out


# ------------------------------------------------------------------- commands


def cmd_inventory(args: argparse.Namespace) -> int:
    root = Path(args.bundle_root)
    print(f"# {root}")
    for sub in ("source", "derived"):
        directory = root / sub
        if not directory.is_dir():
            continue
        print(f"\n## {sub}/")
        for item in sorted(directory.rglob("*")):
            if item.is_dir() or item.suffix == ".dvc":
                continue
            size = item.stat().st_size
            note = ""
            if item.suffix in {".mxl", ".musicxml", ".xml"}:
                try:
                    contents = measure_contents(item)
                    counts = {p: len(m) for p, m in contents.items()}
                    note = f"  <- MusicXML, measures per part: {counts}"
                except Exception as exc:  # noqa: BLE001 - inventory must not abort
                    note = f"  <- unreadable MusicXML ({exc})"
            elif item.suffix == ".mid":
                try:
                    midi = mido.MidiFile(item)
                    names = [t.name.strip() for t in midi.tracks if (t.name or "").strip()]
                    note = f"  <- MIDI, ppq={midi.ticks_per_beat}, tracks={names}"
                except Exception as exc:  # noqa: BLE001
                    note = f"  <- unreadable MIDI ({exc})"
            print(f"  {item.relative_to(root).as_posix():60s} {size:>10,}{note}")
    return 0


def cmd_measure_content(args: argparse.Namespace) -> int:
    contents = measure_contents(Path(args.score))
    wanted = _parse_range(args.measures)
    for pid, by_measure in contents.items():
        if args.part and pid != args.part:
            continue
        print(f"## part {pid}")
        for number in wanted:
            notes = by_measure.get(number)
            if notes is None:
                print(f"  m.{number}: (absent)")
                continue
            by_beat: dict[int, list[str]] = collections.defaultdict(list)
            for offset, pitch in notes:
                by_beat[int(offset) + 1].append(PITCH_NAMES[pitch % 12])
            summary = " | ".join(
                f"b{beat}: {','.join(names)}" for beat, names in sorted(by_beat.items())
            )
            print(f"  m.{number}: {summary}")
    return 0


def cmd_fingerprint(args: argparse.Namespace) -> int:
    """Find pitch classes unique to one measure within a neighbourhood.

    This is the automated form of what a performer does by ear: a note that
    occurs in only one bar nearby discriminates far better than a whole-bar
    match of ordinary sequential material.
    """

    contents = measure_contents(Path(args.score))
    wanted = _parse_range(args.measures)
    for pid, by_measure in contents.items():
        if args.part and pid != args.part:
            continue
        present = {
            number: {pitch % 12 for _, pitch in by_measure.get(number, [])} for number in wanted
        }
        print(f"## part {pid}")
        for number in wanted:
            mine = present.get(number) or set()
            others: set[int] = set()
            for other in wanted:
                if other != number:
                    others |= present.get(other) or set()
            unique = sorted(mine - others)
            label = ", ".join(PITCH_NAMES[p] for p in unique) if unique else "(none)"
            print(f"  m.{number}: unique pitch classes -> {label}")
    return 0


def cmd_find_bar(args: argparse.Namespace) -> int:
    """Locate a notated measure inside a performance by pitch sequence.

    Deliberately unconstrained: no existing warp is consulted, so this can
    contradict the beat map. Reports the runner-up so an ambiguous match is
    visible instead of being silently accepted.
    """

    contents = measure_contents(Path(args.score))
    part = args.part or next(iter(contents))
    by_measure = contents[part]
    target = by_measure.get(args.measure)
    if not target:
        raise SystemExit(f"measure {args.measure} not found in part {part}")
    wanted = [pitch for _, pitch in target]
    onsets = midi_onsets(Path(args.performance), args.track)
    if not onsets:
        raise SystemExit("no onsets in performance (check --track)")

    scored: list[tuple[float, float]] = []
    for index in range(len(onsets)):
        window = [pitch for _, pitch, _ in onsets[index : index + len(wanted)]]
        ratio = difflib.SequenceMatcher(None, wanted, window, autojunk=False).ratio()
        scored.append((ratio, onsets[index][0]))
    scored.sort(reverse=True)
    best_ratio, best_time = scored[0]
    runner = next(((r, t) for r, t in scored if abs(t - best_time) > args.separation), (0.0, 0.0))
    print(f"part {part} m.{args.measure}: {len(wanted)} notated onsets")
    print(f"  best match   ratio {best_ratio:.3f} at {best_time:.3f}s")
    print(f"  runner-up    ratio {runner[0]:.3f} at {runner[1]:.3f}s")
    print(f"  margin       {best_ratio - runner[0]:+.3f}")
    if best_ratio - runner[0] < args.min_margin:
        print("  VERDICT: AMBIGUOUS -- do not accept without independent evidence")
        return 1
    print("  VERDICT: decisive")
    return 0


def cmd_irregular_measures(args: argparse.Namespace) -> int:
    """Flag measures whose duration differs from a full bar.

    Two adjacent half-bars are the signature of a bar split across a system
    break that the exporter numbered twice. Parangonar cannot detect this --
    it never sees measures -- so it must be caught here, before any
    score_beat -> measure conversion inherits the extra boundary.
    """

    root = _load_musicxml_root(Path(args.score))
    beats_per_bar = float(args.beats)
    findings = 0
    for part in root.findall("part"):
        divisions = 1.0
        rows: list[tuple[str, float]] = []
        for measure in part.findall("measure"):
            div_text = measure.findtext("attributes/divisions")
            if div_text:
                divisions = float(div_text)
            if measure.get("implicit") == "yes":
                continue
            position = 0.0
            longest = 0.0
            for el in measure:
                if el.tag == "backup":
                    position -= float(el.findtext("duration") or 0)
                elif el.tag == "forward":
                    position += float(el.findtext("duration") or 0)
                elif el.tag == "note" and el.find("chord") is None and el.find("grace") is None:
                    position += float(el.findtext("duration") or 0)
                longest = max(longest, position)
            beats = longest / divisions
            if abs(beats - beats_per_bar) > 0.05:
                rows.append((measure.get("number") or "?", round(beats, 3)))
        if rows:
            print(f"## part {part.get('id')}: {len(rows)} irregular measure(s)")
            for number, beats in rows:
                print(f"   m.{number}: {beats} beats (expected {beats_per_bar})")
            findings += len(rows)
    if not findings:
        print("no irregular measures")
        return 0
    print("\nAdjacent short measures summing to a full bar are a split bar: merge them.")
    return 1


def cmd_annotate(args: argparse.Namespace) -> int:
    """Render a score PDF with every measure box drawn and labelled.

    Built because ad-hoc annotation wasted a great deal of the performer's time:
    versions shipped with invisible labels, stale filenames the browser cached,
    and references to markers that lived in a different document. The rules
    baked in here are those failures:

    - always label every box visibly (point text, ASCII only -- `insert_textbox`
      clips and non-ASCII renders as '?');
    - write to a NEW filename each run so a cached tab cannot show the old one;
    - include a per-page running total so the reader can verify a count without
      recounting from the top;
    - never reference markers that are not drawn on the page being sent.
    """

    import pymupdf  # imported lazily: offline tooling only, not a server dependency

    bundle = Path(args.bundle_root)
    display = _read_json_file(bundle / "derived/display_map.machine.json")
    pdf = Path(args.pdf) if args.pdf else _sole_display_pdf(bundle)
    boxes_by_page: dict[int, list[dict]] = {}
    for box in display.get("boxes", []):
        boxes_by_page.setdefault(box["page"], []).append(box)

    doc = pymupdf.open(pdf)
    out = pymupdf.open()
    pages = (
        range(1, doc.page_count + 1)
        if not args.pages
        else [int(x) for x in args.pages.split(",")]
    )
    orange = (0.9, 0.35, 0.0)
    running = 0
    for page_no in pages:
        out.insert_pdf(doc, from_page=page_no - 1, to_page=page_no - 1)
        page = out[-1]
        rect = page.rect
        boxes = sorted(boxes_by_page.get(page_no, []), key=lambda b: (b["system"], b["x0"]))
        running += len(boxes)
        page.draw_rect(pymupdf.Rect(0, 0, rect.width, 26), color=None, fill=(1, 1, 1))
        header = (
            f"page {page_no}/{doc.page_count}   bars here: {len(boxes)}   "
            + (
                f"m.{boxes[0]['measure_label']} - m.{boxes[-1]['measure_label']}   "
                f"(running {running} of {len(display.get('boxes', []))})"
                if boxes
                else "NO BOXES ON THIS PAGE"
            )
        )
        page.insert_text((16, 17), header, fontsize=11, fontname="hebo")
        for box in boxes:
            x0, y0 = box["x0"] * rect.width, box["y0"] * rect.height
            page.draw_rect(
                pymupdf.Rect(x0, y0, box["x1"] * rect.width, box["y1"] * rect.height),
                color=orange,
                width=1.3,
            )
            page.draw_rect(
                pymupdf.Rect(x0 + 1.5, y0 - 14, x0 + 37, y0 - 1),
                color=orange,
                fill=(1, 1, 1),
                width=0.7,
            )
            page.insert_text(
                (x0 + 4, y0 - 4), box["measure_label"], fontsize=9.5,
                color=orange, fontname="hebo",
            )
    out.save(args.out)
    print(f"wrote {args.out}: {out.page_count} page(s), {running} boxes labelled")
    return 0


def _read_json_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sole_display_pdf(bundle: Path) -> Path:
    pdfs = sorted((bundle / "source").glob("*.pdf"))
    if len(pdfs) != 1:
        raise SystemExit(
            f"{len(pdfs)} PDFs in {bundle}/source; pass --pdf to choose one: "
            + ", ".join(p.name for p in pdfs)
        )
    return pdfs[0]


def cmd_cross_validate(args: argparse.Namespace) -> int:
    """Compare two independent alignments against the shipped beat map.

    A single alignment is wrong at the two-second scale often enough to matter,
    and you cannot tell which bars from one alignment alone. Two independent
    ones intersect: a bar where BOTH disagree with the beat map is a real
    defect, and a bar flagged by only one is that alignment's own error. On the
    Chopin bundle this cut a nine-bar defect list to three.
    """

    a = _read_json_file(Path(args.alignment_a))["measure_downbeat_seconds"]
    b = _read_json_file(Path(args.alignment_b))["measure_downbeat_seconds"]
    beat_map = _read_json_file(Path(args.beat_map))
    anchors = {
        int(x["measure_label"]): x["source_seconds"]
        for x in beat_map.get("anchors", [])
        if x.get("beat_in_measure") == 0.0
    }
    downbeats_a = {int(k): v for k, v in a.items()}
    downbeats_b = {int(k): v for k, v in b.items()}
    both, only_a, only_b = [], [], []
    for measure, t in sorted(anchors.items()):
        if measure not in downbeats_a or measure not in downbeats_b:
            continue
        da, db = t - downbeats_a[measure], t - downbeats_b[measure]
        if abs(da) > args.tolerance and abs(db) > args.tolerance:
            both.append((measure, t, downbeats_a[measure], downbeats_b[measure]))
        elif abs(da) > args.tolerance:
            only_a.append(measure)
        elif abs(db) > args.tolerance:
            only_b.append(measure)
    agreement = [abs(downbeats_a[m] - downbeats_b[m]) for m in downbeats_a if m in downbeats_b]
    agreement.sort()
    print(
        f"the two alignments agree with each other: median "
        f"{agreement[len(agreement) // 2]:.3f}s over {len(agreement)} bars"
    )
    print(f"\nCONFIRMED defects (both disagree with the beat map by >{args.tolerance}s):")
    for measure, t, ta, tb in both:
        print(f"   m.{measure:<4} beat map {t:8.2f}   A {ta:8.2f}   B {tb:8.2f}")
    if not both:
        print("   none")
    print(f"\nflagged by A only (likely A's own error): {only_a}")
    print(f"flagged by B only (likely B's own error):  {only_b}")
    return 1 if both else 0


_CHORD_TEMPLATES = {
    "dim7": (0, 3, 6, 9),
    "dom7": (0, 4, 7, 10),
    "maj": (0, 4, 7),
    "min": (0, 3, 7),
    "half-dim7": (0, 3, 6, 10),
    "aug": (0, 4, 8),
}


def _name_harmony(pitch_classes: set[int]) -> str:
    """Best chord label for a pitch-class set, or '' when nothing fits.

    Uses music21 where available -- it names far more than the handful of
    templates below, including inversions, added tones and enharmonic spellings
    that matter for identifying a bar. The templates remain as a fallback so
    this command still works without the `analysis` extra installed.
    """

    if not pitch_classes:
        return ""
    try:
        from music21 import chord as m21chord

        name = m21chord.Chord(sorted(pitch_classes)).pitchedCommonName
        if name and "unknown" not in name.lower():
            return name
    except Exception:  # noqa: BLE001 - analysis extra is optional
        pass
    if not 3 <= len(pitch_classes) <= 4:
        return ""
    for name, template in _CHORD_TEMPLATES.items():
        if len(template) != len(pitch_classes):
            continue
        for root in range(12):
            if {(root + i) % 12 for i in template} == pitch_classes:
                return f"{PITCH_NAMES[root]} {name}"
    return ""


def cmd_describe(args: argparse.Namespace) -> int:
    """Say what is distinctive about each measure, without asking a human.

    Written because the obvious musical features were computable from the score
    all along and were not being computed: which parts are silent, what harmony
    a bar spells, the melodic contour of its top voice, and which pitches occur
    in it and in neither neighbour. A performer was repeatedly asked to describe
    bars whose fingerprints were sitting in the MusicXML.

    Absence is the strongest signal and the cheapest: "no piano in this bar" or
    "orchestra tacet" localizes a measure in a recording far more reliably than
    pitch content, which in chromatic music matches its neighbours almost
    perfectly.
    """

    contents = measure_contents(Path(args.score))
    wanted = _parse_range(args.measures)
    parts = list(contents)
    for number in wanted:
        print(f"m.{number}")
        silent = [p for p in parts if not contents[p].get(number)]
        sounding = [p for p in parts if contents[p].get(number)]
        if silent:
            print(f"   TACET: {', '.join(silent)}   (sounding: {', '.join(sounding) or 'nothing'})")
        allpcs: set[int] = set()
        for part in parts:
            notes = contents[part].get(number) or []
            if not notes:
                continue
            pcs = {p % 12 for _, p in notes}
            allpcs |= pcs
            top = [p for _, p in sorted(notes, key=lambda n: n[0])]
            contour = [PITCH_NAMES[p % 12] for p in top[:10]]
            label = _name_harmony(pcs)
            pcs_text = ",".join(sorted(PITCH_NAMES[c] for c in pcs))
            print(
                f"   {part}: {len(notes):>3} notes  pcs {{{pcs_text}}}"
                + (f"  = {label}" if label else "")
            )
            print(f"        contour {' '.join(contour)}")
        whole = _name_harmony(allpcs)
        if whole:
            print(f"   whole bar spells: {whole}")
        # pitches in this bar and in neither neighbour
        try:
            n = int(number)
        except ValueError:
            print()
            continue
        neighbours: set[int] = set()
        for other in (str(n - 1), str(n + 1)):
            for part in parts:
                neighbours |= {p % 12 for _, p in (contents[part].get(other) or [])}
        unique = sorted(allpcs - neighbours)
        print(
            "   UNIQUE vs neighbours: "
            + (", ".join(PITCH_NAMES[c] for c in unique) if unique else "(none)")
        )
        print()
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    from aimusic.accompaniment.score_health import check_score_bundle

    findings = check_score_bundle(Path(args.bundle_root))
    if not findings:
        print("no cross-artifact findings")
        return 0
    for finding in findings:
        print(f"[{finding.severity.upper():7s}] {finding.code}")
        print(f"  {finding.message}")
        if finding.measures:
            shown = ", ".join(finding.measures[:20])
            more = " ..." if len(finding.measures) > 20 else ""
            print(f"  measures: {shown}{more}")
    return 1 if any(f.severity == "error" for f in findings) else 0


# ------------------------------------------------ validate & correct the beat map
#
# These operate on the Oguri->measure/beat MIDI timing map, not on PDF geometry
# (see docs/concepts/score-coordinate-systems.md and the "Validate & correct"
# phase of the score-localization skill). The human supplies identity by ear;
# the exact time is read from the reference MIDI, never estimated.


def _map_downbeats(beat_map) -> dict[str, float]:
    return {
        anchor.measure_label: anchor.source_seconds
        for anchor in beat_map.anchors
        if abs(anchor.beat_in_measure) < 1e-6
    }


def cmd_worklist(args: argparse.Namespace) -> int:
    """The audition shortlist: bars two independent alignments both reject, plus a spine.

    This is what shrinks a 126-bar movement to the handful worth listening to.
    """

    from aimusic.accompaniment.beat_anchor import suspect_measures
    from aimusic.accompaniment.score_fusion import load_performance_beat_map

    a = json.loads(Path(args.alignment_a).read_text())["measure_downbeat_seconds"]
    b = json.loads(Path(args.alignment_b).read_text())["measure_downbeat_seconds"]
    beat_map = load_performance_beat_map(Path(args.beat_map))
    suspects = suspect_measures(
        {str(k): float(v) for k, v in a.items()},
        {str(k): float(v) for k, v in b.items()},
        _map_downbeats(beat_map),
        tolerance=args.tolerance,
        spine_step=args.spine_step,
    )
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "measure_label": s.measure_label,
                        "reason": s.reason,
                        "hypothesis_downbeat_seconds": s.hypothesis_downbeat_seconds,
                        "disagreement_seconds": s.disagreement_seconds,
                    }
                    for s in suspects
                ],
                indent=1,
            )
        )
        return 0
    defects = [s for s in suspects if s.reason == "both_disagree"]
    print(f"{len(suspects)} measures to audition ({len(defects)} confirmed defects + spine):")
    for s in suspects:
        disagree = f"  disagree {s.disagreement_seconds:5.1f}s" if s.disagreement_seconds else ""
        at = f"  ~{s.hypothesis_downbeat_seconds:7.2f}s" if s.hypothesis_downbeat_seconds else ""
        print(f"  m.{s.measure_label:<4} {s.reason:<13}{at}{disagree}")
    return 1 if defects else 0


def cmd_snap(args: argparse.Namespace) -> int:
    """Find the reference onset that carries a beat -- the machine's precise answer.

    The performer supplies the pitch (the note the score puts on this beat, read
    by ear/eye); this returns the exact onset near the beat's current hypothesis.
    Prints the candidates so an ambiguous pick is visible, not silently made.
    """

    from aimusic.accompaniment.beat_anchor import (
        beat_hypothesis,
        candidate_onsets,
        reference_onsets,
        snap_to_pitch,
    )
    from aimusic.accompaniment.score_fusion import load_performance_beat_map

    beat_map = load_performance_beat_map(Path(args.beat_map))
    hypothesis = beat_hypothesis(beat_map, args.measure, args.beat)
    if hypothesis is None:
        raise SystemExit(f"no beat {args.beat} in measure {args.measure!r}")
    onsets = reference_onsets(Path(args.performance), args.track)
    candidates = candidate_onsets(onsets, hypothesis.source_seconds, args.window)
    print(
        f"m.{args.measure} beat {args.beat}: hypothesis {hypothesis.source_seconds:.3f}s "
        f"(tick {hypothesis.source_midi_tick}, anchored={hypothesis.anchored})"
    )
    for candidate in candidates[:8]:
        name = PITCH_NAMES[candidate.pitch % 12]
        print(
            f"  {candidate.delta_seconds:+6.3f}s  {name:<2} (midi {candidate.pitch})  "
            f"tick {candidate.native_tick}"
        )
    if args.pitch is not None:
        result = snap_to_pitch(candidates, args.pitch)
        print(f"\nsnap to pitch {args.pitch} ({PITCH_NAMES[args.pitch % 12]}): {result.reason}")
        if result.chosen is not None:
            print(f"  -> tick {result.chosen.native_tick} at {result.chosen.seconds:.3f}s")
            return 1 if result.ambiguous else 0
        return 1
    return 0


def cmd_apply_anchors(args: argparse.Namespace) -> int:
    """Persist confirmed beat selections as human corrections (Mode B write path).

    Input JSON is a list of {measure_label, beat_in_measure, and either
    source_midi_tick or source_seconds, optional note}. Corrections override the
    machine map at projection time; the same file the in-app workflow writes.
    """

    from aimusic.accompaniment import beat_anchor
    from aimusic.accompaniment.score_fusion import (
        HumanCorrectionDocument,
        apply_human_corrections,
        load_human_corrections,
        load_performance_beat_map,
        source_midi_tick_at_seconds,
        write_human_corrections,
    )
    from aimusic.core import paths
    from aimusic.core.time import utc_now

    beat_map = load_performance_beat_map(Path(args.beat_map))
    selections = json.loads(Path(args.selections).read_text())
    new = []
    for item in selections:
        if "source_midi_tick" in item:
            tick = int(item["source_midi_tick"])
        elif "source_seconds" in item:
            tick = source_midi_tick_at_seconds(beat_map, float(item["source_seconds"]))
        else:
            raise SystemExit(f"selection needs source_midi_tick or source_seconds: {item}")
        new.append(
            beat_anchor.correction_from_selection(
                beat_map,
                str(item["measure_label"]),
                float(item["beat_in_measure"]),
                source_midi_tick=tick,
                created_at=utc_now(),
                note=item.get("note"),
            )
        )
    out = (
        Path(args.out)
        if args.out
        else paths.score_alignment_corrections_path(args.piece_id, args.movement)
    )
    existing = load_human_corrections(out)
    merged = beat_anchor.merge_corrections(existing.corrections if existing else (), tuple(new))
    document = HumanCorrectionDocument(
        piece_id=args.piece_id,
        movement=args.movement,
        timeline_id=beat_map.timeline_id,
        corrections=merged,
    )
    apply_human_corrections(beat_map, document)  # validates monotonicity before writing
    write_human_corrections(document, out)
    print(f"wrote {len(merged)} corrections ({len(new)} new/updated) to {out}")
    return 0


def cmd_propose_beat_anchors(args: argparse.Namespace) -> int:
    """For each beat of a measure, the map's time and (with a score) the beat's bass note.

    The bass note is the one the score puts on the beat -- the pitch to confirm
    by ear and snap to. Advisory: prefer the Audiveris MXL whose measures were
    repaired to 1-126 (canonical), since a score's own numbering can differ.
    """

    from aimusic.accompaniment.beat_anchor import beat_hypotheses
    from aimusic.accompaniment.score_fusion import load_performance_beat_map

    beat_map = load_performance_beat_map(Path(args.beat_map))
    bass_by_beat: dict[int, int] = {}
    if args.score:
        contents = measure_contents(Path(args.score))
        parts = list(contents)
        chosen = args.part or parts[0]
        for offset, pitch in contents.get(chosen, {}).get(args.measure, []):
            beat = int(round(offset))
            if beat not in bass_by_beat or pitch < bass_by_beat[beat]:
                bass_by_beat[beat] = pitch
    for hypothesis in beat_hypotheses(beat_map, args.measure):
        bass = bass_by_beat.get(int(round(hypothesis.beat_in_measure)))
        note = f"  bass {PITCH_NAMES[bass % 12]} (midi {bass})" if bass is not None else ""
        flag = "" if hypothesis.anchored else "  [interpolated -- nothing to audition]"
        print(
            f"  beat {hypothesis.beat_in_measure:>3}: {hypothesis.source_seconds:8.3f}s "
            f"tick {hypothesis.source_midi_tick}{note}{flag}"
        )
    return 0


def _load_corrections_or_empty(path, timeline_id: str):
    from aimusic.accompaniment.score_fusion import HumanCorrectionDocument, load_human_corrections

    if path and Path(path).exists():
        return load_human_corrections(Path(path))
    return HumanCorrectionDocument(
        piece_id="chopin_op11", movement=2, timeline_id=timeline_id, corrections=()
    )


def _print_beat_changes(changes) -> None:
    if not changes:
        print("  (no downstream beats change)")
        return
    for change in changes:
        print(
            f"  m.{change.measure_label:<4} beat {change.beat_in_measure:>3}: "
            f"{change.old_source_seconds:8.3f}s -> {change.new_source_seconds:8.3f}s "
            f"({change.delta_seconds:+.3f}s)"
        )


def cmd_preview(args: argparse.Namespace) -> int:
    """Dry-run: show what re-deriving after a hypothetical anchor WOULD change.

    Writes nothing. Compares the current committed state against the state with
    one extra anchor, so you can see the cascade before committing to it.
    """

    from aimusic.accompaniment import beat_anchor
    from aimusic.accompaniment.score_fusion import (
        load_performance_beat_map,
        source_midi_tick_at_seconds,
    )
    from aimusic.core.time import utc_now

    beat_map = load_performance_beat_map(Path(args.beat_map))
    existing = _load_corrections_or_empty(args.corrections, beat_map.timeline_id)
    if args.source_midi_tick is not None:
        tick = args.source_midi_tick
    elif args.source_seconds is not None:
        tick = source_midi_tick_at_seconds(beat_map, args.source_seconds)
    else:
        raise SystemExit("give --source-midi-tick or --source-seconds for the hypothetical anchor")
    hypo = beat_anchor.correction_from_selection(
        beat_map, args.measure, args.beat, source_midi_tick=tick, created_at=utc_now()
    )
    baseline = beat_anchor.rederive_beat_map(beat_map, existing)
    candidate = beat_anchor.merge_corrections(existing.corrections, (hypo,))
    hypo_doc = existing.model_copy(update={"corrections": candidate})
    proposed = beat_anchor.rederive_beat_map(beat_map, hypo_doc)
    changes = beat_anchor.diff_beat_maps(baseline, proposed)
    print(f"HYPOTHETICAL anchor m.{args.measure} beat {args.beat} -> tick {tick}")
    print(f"downstream beats that would move ({len(changes)}):")
    _print_beat_changes(changes)
    print("\n(nothing written -- commit with `apply-anchors`, then `rederive`)")
    return 0


def cmd_rederive(args: argparse.Namespace) -> int:
    """Re-space interpolated beats from the committed corrections; write to --out.

    Explicit and non-destructive: it never overwrites the machine beat map unless
    you point --out at it. Prints the diff so the change is visible.
    """

    from aimusic.accompaniment import beat_anchor
    from aimusic.accompaniment.score_fusion import (
        load_human_corrections,
        load_performance_beat_map,
        write_performance_beat_map,
    )

    beat_map = load_performance_beat_map(Path(args.beat_map))
    corrections = load_human_corrections(Path(args.corrections))
    if corrections is None:
        raise SystemExit(f"no corrections file at {args.corrections}")
    rederived = beat_anchor.rederive_beat_map(beat_map, corrections)
    changes = beat_anchor.diff_beat_maps(beat_map, rederived)
    print(f"re-derived from {len(corrections.corrections)} corrections; {len(changes)} moved:")
    _print_beat_changes(changes)
    write_performance_beat_map(rederived, Path(args.out))
    print(f"\nwrote {args.out}")
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    """Remove a committed correction (the most recent by default) -- the undo."""

    from aimusic.accompaniment.score_fusion import (
        load_human_corrections,
        write_human_corrections,
    )

    corrections = load_human_corrections(Path(args.corrections))
    if corrections is None or not corrections.corrections:
        print("no corrections to undo")
        return 0
    items = list(corrections.corrections)
    if args.all:
        removed, kept = items, []
    elif args.correction_id:
        removed = [c for c in items if c.correction_id == args.correction_id]
        kept = [c for c in items if c.correction_id != args.correction_id]
        if not removed:
            raise SystemExit(f"no correction with id {args.correction_id}")
    else:  # most recent by created_at
        newest = max(items, key=lambda c: c.created_at)
        removed, kept = [newest], [c for c in items if c is not newest]
    updated = corrections.model_copy(update={"corrections": tuple(kept)})
    write_human_corrections(updated, Path(args.corrections))
    for correction in removed:
        print(
            f"removed m.{correction.measure_label} beat {correction.beat_in_measure} "
            f"({correction.correction_id})"
        )
    print(f"{len(kept)} corrections remain")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("inventory", help="list bundle artifacts with measure/track summaries")
    p.add_argument("bundle_root")
    p.set_defaults(func=cmd_inventory)

    p = sub.add_parser("measure-content", help="print notated pitches per beat for measures")
    p.add_argument("score")
    p.add_argument("--measures", required=True, help="e.g. 95-98 or 95,96,97")
    p.add_argument("--part")
    p.set_defaults(func=cmd_measure_content)

    p = sub.add_parser("fingerprint", help="pitch classes unique to each measure in a range")
    p.add_argument("score")
    p.add_argument("--measures", required=True)
    p.add_argument("--part")
    p.set_defaults(func=cmd_fingerprint)

    p = sub.add_parser("find-bar", help="locate a notated measure in a performance MIDI")
    p.add_argument("score")
    p.add_argument("performance")
    p.add_argument("--measure", required=True)
    p.add_argument("--part")
    p.add_argument("--track", help="substring of the MIDI track name to search")
    p.add_argument("--separation", type=float, default=2.0, help="seconds; runner-up must differ")
    p.add_argument("--min-margin", type=float, default=0.10)
    p.set_defaults(func=cmd_find_bar)

    p = sub.add_parser("irregular-measures", help="flag split/short bars numbered twice")
    p.add_argument("score")
    p.add_argument("--beats", type=float, default=4.0, help="beats in a full bar (default 4)")
    p.set_defaults(func=cmd_irregular_measures)

    p = sub.add_parser("annotate", help="render a score PDF with every measure box labelled")
    p.add_argument("bundle_root")
    p.add_argument("--out", required=True, help="use a NEW filename each run; browsers cache")
    p.add_argument("--pdf", help="which source PDF (required when the bundle has several)")
    p.add_argument("--pages", help="comma-separated page numbers; default all")
    p.set_defaults(func=cmd_annotate)

    p = sub.add_parser("cross-validate", help="intersect two alignments against the beat map")
    p.add_argument("alignment_a")
    p.add_argument("alignment_b")
    p.add_argument("beat_map")
    p.add_argument("--tolerance", type=float, default=2.0, help="seconds")
    p.set_defaults(func=cmd_cross_validate)

    p = sub.add_parser("describe", help="what is distinctive about a measure")
    p.add_argument("score")
    p.add_argument("--measures", required=True)
    p.set_defaults(func=cmd_describe)

    p = sub.add_parser("health", help="cross-artifact consistency findings for a bundle")
    p.add_argument("bundle_root")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("worklist", help="audition shortlist: bars both alignments reject + spine")
    p.add_argument("alignment_a")
    p.add_argument("alignment_b")
    p.add_argument("beat_map")
    p.add_argument("--tolerance", type=float, default=2.0, help="seconds")
    p.add_argument("--spine-step", type=int, default=15, help="audition every Nth bar too (0=off)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_worklist)

    p = sub.add_parser("propose-beat-anchors", help="per-beat map time + the bass note if a score")
    p.add_argument("beat_map")
    p.add_argument("--measure", required=True, help="canonical measure label, e.g. 53")
    p.add_argument("--score", help="MusicXML for beat pitches (prefer the repaired Audiveris MXL)")
    p.add_argument("--part")
    p.set_defaults(func=cmd_propose_beat_anchors)

    p = sub.add_parser("snap", help="find the reference onset carrying a beat (identity->time)")
    p.add_argument("beat_map")
    p.add_argument("performance")
    p.add_argument("--measure", required=True)
    p.add_argument("--beat", type=float, required=True)
    p.add_argument("--pitch", type=int, help="target MIDI pitch (the score's note on this beat)")
    p.add_argument("--track", default="PIANO SOLO", help="substring of the reference track name")
    p.add_argument("--window", type=float, default=1.5, help="seconds around the hypothesis")
    p.set_defaults(func=cmd_snap)

    p = sub.add_parser("apply-anchors", help="persist confirmed beat selections as corrections")
    p.add_argument("beat_map")
    p.add_argument(
        "selections",
        help="JSON list of {measure_label, beat_in_measure, source_midi_tick|source_seconds}",
    )
    p.add_argument("--piece-id", default="chopin_op11")
    p.add_argument("--movement", type=int, default=2)
    p.add_argument("--out", help="corrections file (default: registered path for piece/movement)")
    p.set_defaults(func=cmd_apply_anchors)

    p = sub.add_parser("preview", help="dry-run: what re-derivation would change downstream")
    p.add_argument("beat_map")
    p.add_argument("--corrections", help="existing corrections file (optional)")
    p.add_argument("--measure", required=True, help="hypothetical anchor's measure label")
    p.add_argument("--beat", type=float, required=True)
    p.add_argument("--source-midi-tick", type=int)
    p.add_argument("--source-seconds", type=float)
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("rederive", help="re-space interpolated beats from corrections (to --out)")
    p.add_argument("beat_map")
    p.add_argument("corrections")
    p.add_argument("--out", required=True, help="destination (a new beat map file)")
    p.set_defaults(func=cmd_rederive)

    p = sub.add_parser("undo", help="remove a committed correction (most recent by default)")
    p.add_argument("corrections")
    p.add_argument("--correction-id", help="remove this specific correction")
    p.add_argument("--all", action="store_true", help="remove every correction")
    p.set_defaults(func=cmd_undo)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
