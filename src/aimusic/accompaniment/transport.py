"""Explicit transport state machine for the live engine.

Transport authority used to be an *implicit* machine: `process_update` and
`tick` each ran their own chain of ``if authority is ...`` branches, and any
state a chain forgot simply fell through. Two production bugs were exactly that
-- missing cells, invisible because no single place described the machine:

* ``FOLLOW`` had no ``tick`` branch, so a pianist who stopped playing never
  triggered the ``follower_coast_ms`` contract (silence produces no note, and
  only a note could leave ``FOLLOW``).
* the same shape hid a stranded follower position during silence.

This module makes the machine a table instead of a control-flow accident. Every
(state, event) pair must be declared, including the ones that deliberately do
nothing -- ``ignore(...)`` records *why* a cell is inert, which is precisely the
information the old fall-through destroyed. :func:`validate_total` refuses to
build a partial table, so forgetting a cell fails at construction rather than
silently at a fermata.

The handlers themselves live on the engine; this module owns only the shape of
the machine and the guarantee that it is total.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

R = TypeVar("R")


class TransportEvent(str, Enum):
    """The two ways transport can be driven."""

    #: A follower position arrived (from a played note, or drained later).
    UPDATE = "update"
    #: The control loop ticked; only wall-clock time has passed.
    TICK = "tick"


@dataclass(frozen=True)
class Inert:
    """A deliberately inert cell, with the reason it is inert.

    Distinguishing "nothing should happen here, because X" from "nobody wrote
    this branch" is the whole point: the first is a decision, the second is the
    bug class this table exists to prevent.
    """

    reason: str


Cell = Callable[..., R] | Inert


class TransportTable(Generic[R]):
    """A total (state x event) -> handler mapping."""

    def __init__(
        self,
        states: type[Enum],
        cells: dict[tuple[Enum, TransportEvent], Cell[R]],
    ) -> None:
        validate_total(states, cells)
        self._cells = dict(cells)

    def handler(self, state: Enum, event: TransportEvent) -> Cell[R]:
        return self._cells[(state, event)]

    def is_inert(self, state: Enum, event: TransportEvent) -> bool:
        return isinstance(self._cells[(state, event)], Inert)

    def reason(self, state: Enum, event: TransportEvent) -> str | None:
        cell = self._cells[(state, event)]
        return cell.reason if isinstance(cell, Inert) else None

    def describe(self) -> str:
        """Render the machine as a table -- the documentation the code lacked."""

        lines = []
        for state in sorted({s for s, _ in self._cells}, key=lambda s: s.value):
            for event in TransportEvent:
                cell = self._cells[(state, event)]
                what = (
                    f"inert ({cell.reason})"
                    if isinstance(cell, Inert)
                    else getattr(cell, "__name__", repr(cell))
                )
                lines.append(f"{state.value:18s} x {event.value:6s} -> {what}")
        return "\n".join(lines)


def validate_total(
    states: type[Enum],
    cells: dict[tuple[Enum, TransportEvent], Cell[R]],
) -> None:
    """Refuse a partial machine; report every missing cell at once."""

    missing = [
        (state, event)
        for state in states
        for event in TransportEvent
        if (state, event) not in cells
    ]
    if missing:
        rendered = ", ".join(f"{s.value}x{e.value}" for s, e in missing)
        raise ValueError(
            "transport state machine is not total; undeclared cells: "
            f"{rendered}. Declare a handler, or Inert(reason=...) if nothing "
            "should happen -- silent fall-through is how FOLLOW stopped "
            "honouring its coast contract."
        )
    unknown = [key for key in cells if key[0] not in set(states)]
    if unknown:
        raise ValueError(f"transport table declares unknown states: {unknown}")


__all__ = [
    "Cell",
    "Inert",
    "TransportEvent",
    "TransportTable",
    "validate_total",
]
