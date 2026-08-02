"""Turn (Config, Responses) into the solve-ready numeric Problem.

Every policy decision about *who is in the pool* and *which pairings are legal*
happens here and nowhere else, so there is a single place to audit. By the time
`solve()` sees a matrix, invariants I4 (top-K) and I5 (zone) are structural: no
cell exists that would violate them.

See docs/SPEC.md §3.3 (roster/submission conflicts) and §3.4 (the pool).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping

import numpy as np

from . import eligibility as elig
from . import scoring
from .errors import Problem as Complaint
from .errors import ResponseError
from .types import Config, DeskId, PersonId, Problem, Responses, Person


@dataclass(frozen=True)
class RosterConflict:
    email: PersonId
    field: str
    roster_value: object
    submitted_value: object

    def render(self) -> str:
        return (
            f"{self.email}: submitted {self.field}={self.submitted_value!r} but the "
            f"roster says {self.roster_value!r}. Using the submitted value; update "
            f"config/roster.csv and re-run if the roster is the correct one."
        )


@dataclass(frozen=True)
class ExcludedPerson:
    email: PersonId
    name: str
    reason: str


@dataclass(frozen=True)
class BuildReport:
    """Everything the coordinator needs to know about how the pool was formed.

    Carried alongside the Problem rather than inside it, because the Problem is
    the pure mathematical object the solver consumes and this is commentary.
    """

    problem: Problem
    roster_conflicts: tuple[RosterConflict, ...] = ()
    excluded_people: tuple[ExcludedPerson, ...] = ()
    locked_desks: tuple[tuple[DeskId, PersonId], ...] = ()
    unavailable_desks: tuple[DeskId, ...] = ()
    dropped_choices: tuple[tuple[PersonId, DeskId, str], ...] = ()
    warnings: tuple[str, ...] = ()
    effective_people: Mapping[PersonId, Person] = field(default_factory=dict)

    def render_warnings(self) -> str:
        lines: list[str] = []
        for c in self.roster_conflicts:
            lines.append("  roster conflict: " + c.render())
        for e in self.excluded_people:
            lines.append(f"  excluded: {e.name} <{e.email}> -- {e.reason}")
        for who, desk, why in self.dropped_choices:
            lines.append(f"  dropped choice: {who} ranked {desk} -- {why}")
        for w in self.warnings:
            lines.append("  " + w)
        return "\n".join(lines)


def _effective_person(person: Person, submission) -> tuple[Person, list[RosterConflict]]:
    """Apply SPEC §3.3: the submission wins on year/candidacy, and we record it.

    The roster is stale by design -- the coordinator said they will not know the
    real roster until the week they run this -- so a student correcting their own
    year on the form is the more trustworthy signal. But it changes which zones
    they may sit in, so it can never be silent.
    """
    conflicts: list[RosterConflict] = []
    year = person.year
    candidacy = person.candidacy

    if submission.year is not None and submission.year != person.year:
        conflicts.append(RosterConflict(person.email, "year", person.year, submission.year))
        year = submission.year

    sub_c = (submission.candidacy or "").strip()
    if sub_c and sub_c.casefold() != (person.candidacy or "").strip().casefold():
        conflicts.append(
            RosterConflict(person.email, "candidacy", person.candidacy, submission.candidacy)
        )
        candidacy = sub_c

    attrs = dict(person.attributes)
    attrs["year"] = year
    attrs["candidacy"] = candidacy
    return replace(person, year=year, candidacy=candidacy, attributes=attrs), conflicts


def build_problem(
    config: Config,
    responses: Responses,
    curve_name: str | None = None,
) -> BuildReport:
    """Assemble the Problem. Raises ResponseError for conditions the coordinator
    must fix; everything survivable becomes a warning."""

    curve_name = curve_name or config.scoring.primary_curve
    curve = config.scoring.curve(curve_name)
    int_curve, scale = scoring.integerise(curve)
    k = len(int_curve)

    if responses.k != k:
        raise ResponseError([
            Complaint(
                where=responses.source_path,
                what=f"has {responses.k} choice columns but scoring.json curve "
                     f"'{curve_name}' has {k} entries",
                hint="K is defined by the scoring curve length. Either the form "
                     "collected the wrong number of ranks or the curve changed "
                     "after collection.",
            )
        ])

    problems: list[Complaint] = []
    warnings: list[str] = []
    conflicts: list[RosterConflict] = []
    excluded: list[ExcludedPerson] = []
    dropped: list[tuple[PersonId, DeskId, str]] = []

    # ---- desk pool -------------------------------------------------------
    all_desks = {d.id: d for d in config.rooms.all_desks}

    locked: list[tuple[DeskId, PersonId]] = []
    for person in config.roster.people:
        if person.keeps_desk and person.current_desk:
            locked.append((person.current_desk, person.email))
    locked.sort()

    unavailable = tuple(sorted(d.id for d in config.rooms.all_desks if not d.available))
    locked_ids = {desk for desk, _ in locked}

    pool_desks = tuple(
        sorted(d for d in all_desks if d not in locked_ids and d not in set(unavailable))
    )
    if not pool_desks:
        problems.append(
            Complaint(
                where="config",
                what="the desk pool is empty",
                hint=f"{len(locked_ids)} desk(s) are held by people keeping their "
                     f"seat and {len(unavailable)} are marked unavailable, which "
                     f"accounts for all {len(all_desks)} desks.",
            )
        )

    # ---- people pool -----------------------------------------------------
    submitted = responses.latest
    unknown_emails = sorted(set(submitted) - set(config.roster.emails))
    for email in unknown_emails:
        # SPEC §3.3: not on the roster is an error, not a warning. Someone
        # outside the department must not be able to enter the pool.
        problems.append(
            Complaint(
                where=f"{responses.source_path}: {email}",
                what="submitted a ranking but is not on the roster",
                hint="Add them to config/roster.csv if they belong, or remove the "
                     "submission. The solver will not assign a desk to someone the "
                     "roster does not list.",
            )
        )

    pool_people: list[PersonId] = []
    effective: dict[PersonId, Person] = {}

    for person in config.roster.people:
        if person.keeps_desk:
            excluded.append(
                ExcludedPerson(
                    person.email, person.name,
                    f"keeping their current desk ({person.current_desk})",
                )
            )
            if person.email in submitted:
                warnings.append(
                    f"{person.name} <{person.email}> is marked as keeping desk "
                    f"{person.current_desk} but also submitted a ranking. The "
                    f"submission is ignored. Fix roster.csv if that is wrong."
                )
            continue
        sub = submitted.get(person.email)
        if sub is None:
            excluded.append(ExcludedPerson(person.email, person.name, "no submission"))
            warnings.append(
                f"{person.name} <{person.email}> did not submit and is excluded "
                f"from the pool."
            )
            continue
        eff, cs = _effective_person(person, sub)
        conflicts.extend(cs)
        effective[person.email] = eff
        pool_people.append(person.email)

    pool_people.sort()

    if problems:
        raise ResponseError(problems)

    # ---- matrices --------------------------------------------------------
    n_people, n_desks = len(pool_people), len(pool_desks)
    desk_index = {d: j for j, d in enumerate(pool_desks)}

    allowed = np.zeros((n_people, n_desks), dtype=bool)
    points = np.zeros((n_people, n_desks), dtype=np.int64)
    rank = np.full((n_people, n_desks), -1, dtype=np.int8)

    for i, email in enumerate(pool_people):
        person = effective[email]
        zones = set(elig.allowed_zones(config.eligibility, config.rooms, person))
        reason = elig.eligibility_reason(config.eligibility, person)
        sub = submitted[email]

        n_valid = 0
        for r, desk_id in enumerate(sub.choices, start=1):
            if desk_id not in all_desks:
                dropped.append((email, desk_id, "no such desk in rooms.json"))
                continue
            if desk_id not in desk_index:
                why = (
                    "held by someone keeping their current seat"
                    if desk_id in locked_ids
                    else "marked unavailable in rooms.json"
                )
                dropped.append((email, desk_id, why))
                continue
            desk = all_desks[desk_id]
            if desk.zone not in zones:
                # Should be impossible -- the form re-validates zones server-side
                # -- so if it happens the frontend and the config have drifted
                # apart and the coordinator needs to know.
                dropped.append(
                    (email, desk_id,
                     f"zone '{desk.zone}' not permitted for this person ({reason})")
                )
                continue
            j = desk_index[desk_id]
            allowed[i, j] = True
            points[i, j] = int_curve[r - 1]
            rank[i, j] = r
            n_valid += 1

        if n_valid == 0:
            problems.append(
                Complaint(
                    where=f"{person.name} <{email}>",
                    what="has no valid choices left after filtering",
                    hint="Every desk they ranked is either gone from the pool or "
                         "outside the zones they are eligible for. They must "
                         "re-rank before this can be solved.",
                )
            )

    if problems:
        raise ResponseError(problems)

    if n_people > n_desks:
        warnings.append(
            f"{n_people} people are competing for {n_desks} desks. A complete "
            f"assignment is impossible regardless of preferences."
        )

    # Zone eligibility ignoring top-K, needed by the infeasibility diagnostics.
    eligible = np.zeros((n_people, n_desks), dtype=bool)
    for i, email in enumerate(pool_people):
        zones = set(elig.allowed_zones(config.eligibility, config.rooms, effective[email]))
        for j, desk_id in enumerate(pool_desks):
            eligible[i, j] = all_desks[desk_id].zone in zones

    prob = Problem(
        people=tuple(pool_people),
        desks=pool_desks,
        allowed=allowed,
        points=points,
        rank=rank,
        eligible=eligible,
        scale=scale,
        curve_name=curve_name,
        k=k,
        person_names={e: effective[e].name for e in pool_people},
        desk_labels={d: all_desks[d].label for d in pool_desks},
        dropped_choices=tuple(dropped),
    )

    return BuildReport(
        problem=prob,
        roster_conflicts=tuple(conflicts),
        excluded_people=tuple(excluded),
        locked_desks=tuple(locked),
        unavailable_desks=unavailable,
        dropped_choices=tuple(dropped),
        warnings=tuple(warnings),
        effective_people=effective,
    )


def eligible_mask(report: BuildReport) -> np.ndarray:
    """Zone eligibility alone, ignoring the top-K restriction.

    Used by the diagnostics: to say "you could have been seated if you had
    ranked more widely" we have to know which desks were open to a person in
    principle, not just which ones they named.

    This is a thin accessor rather than a recomputation. The mask is built once
    in build_problem() and stored on the Problem; deriving it a second time here
    would be a second source of truth for the same rule, and the two could
    drift.
    """
    mask = report.problem.eligible
    if mask is None:  # pragma: no cover - build_problem always populates it
        raise ValueError(
            "Problem.eligible was not populated; it is set by build_problem()"
        )
    return mask
