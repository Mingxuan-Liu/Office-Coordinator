"""In-memory data model. Frozen dataclasses, no logic beyond trivial accessors.

This module is the contract between every other module. Changing a field here
means changing docs/SPEC.md §4.1 in the same commit.

Design notes that matter for determinism (invariant I3):
  * Collections that reach output are tuples or sorted tuples, never sets.
  * Where a set is semantically right (zone membership), it is a frozenset and
    is sorted at every point where it is serialised or printed.
  * Nothing here reads the clock, the filesystem, or the environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Mapping, Sequence

import numpy as np

# Semantic aliases. These are all plain strings; the aliases exist so signatures
# say what they mean.
PersonId = str   # lower-cased email — the primary key for a human
DeskId = str     # stable desk id from rooms.json, e.g. "D14"
ZoneId = str     # arbitrary string from rooms.json:zones
RoomId = str


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Desk:
    id: DeskId
    label: str
    zone: ZoneId
    room_id: RoomId
    shape_kind: str                      # "rect" | "polygon"
    shape: tuple[float, ...] | tuple[tuple[float, float], ...]
    available: bool = True               # False = administratively out of the pool
    notes: str = ""

    def centroid(self) -> tuple[float, float]:
        """Coordinate-space centroid; used to place heatmap labels."""
        if self.shape_kind == "rect":
            x, y, w, h = self.shape  # type: ignore[misc]
            return (x + w / 2.0, y + h / 2.0)
        pts = self.shape  # type: ignore[assignment]
        return (
            sum(p[0] for p in pts) / len(pts),   # type: ignore[index]
            sum(p[1] for p in pts) / len(pts),   # type: ignore[index]
        )


@dataclass(frozen=True)
class Zone:
    id: ZoneId
    label: str
    color: str = "#666666"


@dataclass(frozen=True)
class Feature:
    """A non-selectable piece of the floor plan: wall, door, room, window,
    partition, furniture, or the room outline.

    Features exist so a student can see *why* a desk is desirable -- next to the
    window, or backing onto the telecom closet -- and so the map still reads as a
    room when the floor plan image is missing. They are never clickable and never
    enter the solve; nothing here has a zone.
    """

    id: str
    kind: str                             # see validate.KNOWN_FEATURE_KINDS
    label: str
    shape_kind: str                       # "rect" | "polygon" | "polyline"
    shape: tuple[float, ...] | tuple[tuple[float, float], ...]
    note: str = ""
    #: Doors only: the corner the door is hinged on, lower-cased --
    #: "nw" | "ne" | "sw" | "se" (see validate.DOOR_SWINGS). Empty when the key
    #: is absent, which the renderers read as the default "sw". Ignored on any
    #: other kind; the validator warns about that case.
    swing: str = ""


@dataclass(frozen=True)
class Room:
    id: RoomId
    label: str
    image: str                            # path relative to the config dir
    image_size: tuple[int, int]           # (width_px, height_px)
    desks: tuple[Desk, ...]
    features: tuple[Feature, ...] = ()


@dataclass(frozen=True)
class Rooms:
    schema_version: int
    coord_space: str                      # "normalized" | "pixels"
    zones: Mapping[ZoneId, Zone]
    rooms: tuple[Room, ...]

    @property
    def all_desks(self) -> tuple[Desk, ...]:
        return tuple(d for r in self.rooms for d in r.desks)

    def desk(self, desk_id: DeskId) -> Desk:
        for d in self.all_desks:
            if d.id == desk_id:
                return d
        raise KeyError(desk_id)

    @property
    def desk_ids(self) -> tuple[DeskId, ...]:
        return tuple(d.id for d in self.all_desks)


@dataclass(frozen=True)
class EligibilityRule:
    id: str
    when: Mapping[str, Any]               # predicate; see SPEC §2.2
    allow_zones: tuple[ZoneId, ...] | str  # tuple, or the literal "*"
    reason: str = ""

    @property
    def is_catch_all(self) -> bool:
        return len(self.when) == 0


@dataclass(frozen=True)
class Eligibility:
    schema_version: int
    rules: tuple[EligibilityRule, ...]
    #: The candidacy vocabulary the form offers, in the order it offers it. Not
    #: an input to any decision here -- the rules alone decide zones, and a
    #: value absent from this list is matched by the catch-all like any other.
    #: It exists because the rule table only has to name a cohort it treats
    #: specially, so it cannot be read as the list of words a person may choose
    #: between. Empty is legal and means the form falls back to whatever
    #: candidacy values the roster happens to carry.
    candidacy_options: tuple[str, ...] = ()


@dataclass(frozen=True)
class Person:
    """A roster row. `attributes` holds every column verbatim (including extras)
    so eligibility predicates can reference columns this code has never heard of."""

    email: PersonId
    name: str
    year: int
    candidacy: str
    keeps_desk: bool
    current_desk: DeskId | None
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Roster:
    people: tuple[Person, ...]

    def by_email(self, email: PersonId) -> Person | None:
        for p in self.people:
            if p.email == email:
                return p
        return None

    @property
    def emails(self) -> tuple[PersonId, ...]:
        return tuple(p.email for p in self.people)


@dataclass(frozen=True)
class Scoring:
    schema_version: int
    curves: Mapping[str, tuple[Fraction, ...]]
    primary_curve: str
    comparison_curves: tuple[str, ...]
    tie_break_seed: str
    seed_committed_at: str | None = None
    sensitivity_seeds: tuple[str, ...] = ()
    #: When set, the tie-break seed is the cycle year as a string ("2026") and
    #: `tie_break_seed` is ignored. See `resolved_seed()`.
    seed_year: int | None = None
    #: True when `seed_year` was filled in from the clock rather than written in
    #: the config. Purely informational; the resolved value is what gets used and
    #: recorded, so a later re-run reproduces regardless.
    seed_year_from_clock: bool = False

    def resolved_seed(self) -> str:
        """The seed string the solver actually uses.

        Using the calendar year means the seed changes every cycle without the
        coordinator choosing it, which removes seed-shopping as a possibility
        rather than merely discouraging it.

        The year is resolved ONCE, at the start of a run, and written into
        `results.json`. It is never read from the clock inside the solve. If it
        were, re-running the 2026 cycle in January 2027 would silently produce a
        different assignment and every published hash would stop verifying --
        which would break invariant I3, the property the whole audit rests on.
        """
        if self.seed_year is not None:
            return str(self.seed_year)
        return self.tie_break_seed

    @property
    def k(self) -> int:
        """K is derived, never declared. Invariant I1."""
        return len(self.curves[self.primary_curve])

    def curve(self, name: str | None = None) -> tuple[Fraction, ...]:
        return self.curves[name or self.primary_curve]


@dataclass(frozen=True)
class Config:
    rooms: Rooms
    eligibility: Eligibility
    roster: Roster
    scoring: Scoring
    source_dir: str
    file_hashes: Mapping[str, str]        # filename -> sha256 hex
    warnings: tuple[str, ...] = ()

    @property
    def k(self) -> int:
        return self.scoring.k


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Submission:
    submission_id: str
    timestamp: str                        # ISO-8601 with offset, as submitted
    email: PersonId
    name: str
    year: int
    candidacy: str
    choices: tuple[DeskId, ...]           # length K, distinct, rank order
    client_version: str = ""
    auth_method: str = ""
    file_row: int = -1                    # 0-based data-row index, for tie-breaks


@dataclass(frozen=True)
class Responses:
    """All submissions plus the resolved latest-per-person view."""

    submissions: tuple[Submission, ...]   # every row, in file order
    latest: Mapping[PersonId, Submission]  # resolved per SPEC §3.2
    k: int
    source_path: str
    sha256: str
    warnings: tuple[str, ...] = ()

    @property
    def superseded(self) -> tuple[Submission, ...]:
        keep = {s.submission_id for s in self.latest.values()}
        return tuple(s for s in self.submissions if s.submission_id not in keep)


# --------------------------------------------------------------------------
# Problem / Solution
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    """The solve-ready numeric problem.

    `points` is exact integers (SPEC §5.3) — the whole jitter bound rests on that.
    `allowed` already encodes top-K ∧ zone ∧ pool, so invariant I4 is structural.
    """

    people: tuple[PersonId, ...]          # row order, deterministic (sorted)
    desks: tuple[DeskId, ...]             # column order, deterministic (sorted)
    allowed: np.ndarray                   # bool (n_people, n_desks)
    points: np.ndarray                    # int64 (n_people, n_desks); 0 where !allowed
    rank: np.ndarray                      # int8; 1..K where allowed, -1 elsewhere
    scale: int                            # integerisation factor applied to the curve
    curve_name: str
    k: int
    person_names: Mapping[PersonId, str]
    desk_labels: Mapping[DeskId, str]
    dropped_choices: tuple[tuple[PersonId, DeskId, str], ...] = ()
    #: Zone eligibility alone, ignoring the top-K restriction. The diagnostics
    #: need it to answer "could this person have been seated if they had ranked
    #: more widely?", which is a different question from "did their five
    #: choices work out".
    eligible: np.ndarray | None = None

    @property
    def n_people(self) -> int:
        return len(self.people)

    @property
    def n_desks(self) -> int:
        return len(self.desks)


@dataclass(frozen=True)
class Assignment:
    email: PersonId
    name: str
    desk_id: DeskId
    desk_label: str
    rank_received: int                    # 1..K
    points: int                           # scaled integer points


@dataclass(frozen=True)
class Solution:
    assignments: tuple[Assignment, ...]   # sorted by email
    total_points_scaled: int
    scale: int
    curve_name: str
    seed_string: str
    seed_int: int
    k: int
    backend: str
    unassigned_people: tuple[PersonId, ...] = ()   # always empty on a valid run
    free_desks: tuple[DeskId, ...] = ()

    @property
    def total_points(self) -> Fraction:
        return Fraction(self.total_points_scaled, self.scale)

    def rank_histogram(self) -> tuple[int, ...]:
        """counts[i] = number of people who received their (i+1)-th choice."""
        counts = [0] * self.k
        for a in self.assignments:
            counts[a.rank_received - 1] += 1
        return tuple(counts)

    def desk_of(self, email: PersonId) -> DeskId | None:
        for a in self.assignments:
            if a.email == email:
                return a.desk_id
        return None


# --------------------------------------------------------------------------
# Infeasibility diagnostics
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BlockingSet:
    """A Hall's-condition violator: |people| > |desks they can collectively reach|."""

    people: tuple[PersonId, ...]          # sorted
    names: tuple[str, ...]
    desks: tuple[DeskId, ...]             # sorted; the full neighbourhood N(S)
    desk_labels: tuple[str, ...]
    minimal: bool = True

    @property
    def shortfall(self) -> int:
        return len(self.people) - len(self.desks)

    def render(self) -> str:
        who = ", ".join(f"{n} <{e}>" for n, e in zip(self.names, self.people))
        what = ", ".join(f"{d} ({lbl})" for d, lbl in zip(self.desks, self.desk_labels))
        kind = (
            "smallest group that is still over-subscribed"
            if self.minimal
            else "FULL over-subscribed group -- this is the one to act on"
        )
        return (
            f"{len(self.people)} people can only reach {len(self.desks)} desks "
            f"between them (short by {self.shortfall}) "
            f"[{kind}]:\n"
            f"    people: {who}\n"
            f"    desks:  {what}"
        )


@dataclass(frozen=True)
class Infeasibility:
    n_people: int
    n_desks: int
    k: int
    max_satisfiable: int
    blocking_sets: tuple[BlockingSet, ...]
    k_min_submitted: int | None           # None when infeasible even at K
    k_min_extended: int | None            # hypothetical; see SPEC §6.1
    always_unmatched: tuple[PersonId, ...]  # unmatched in EVERY maximum matching
    sometimes_unmatched: tuple[PersonId, ...]  # unmatched in SOME maximum matching
    seed_string: str = ""

    @property
    def deficiency(self) -> int:
        return self.n_people - self.max_satisfiable

    def summary(self) -> str:
        lines = [
            f"INFEASIBLE at K={self.k}: no assignment exists in which all "
            f"{self.n_people} people get one of their top {self.k} desks.",
            f"  At most {self.max_satisfiable} of {self.n_people} can be satisfied "
            f"({self.deficiency} short).",
        ]
        if self.k_min_extended is not None:
            lines.append(
                f"  Hypothetically feasible at K={self.k_min_extended} if rankings "
                f"were extended (see diagnostics.json; this is a diagnostic only)."
            )
        else:
            lines.append(
                "  Not feasible at any K — there are structurally too few eligible "
                "desks for this group."
            )
        for bs in self.blocking_sets:
            lines.append("  " + bs.render().replace("\n", "\n  "))
        return "\n".join(lines)
