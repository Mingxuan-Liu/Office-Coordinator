"""Infeasibility diagnostics, second-round scoping, and the pre-deadline check.

When the K-floor cannot be met the run fails (invariant I7). A bare "infeasible"
would be useless, so this module answers the questions the coordinator will
immediately have:

  * How many people *can* be seated within their top K?
  * Exactly which group is over-subscribed, and on which desks?
  * Who is definitely stuck, versus who is merely at risk?
  * How much wider would people have had to rank for this to work?
  * Who needs to re-rank, and what is genuinely still available to them?

The blocking-set extraction is Konig's theorem applied to a maximum matching;
see matching.py for the argument. Nothing here is heuristic except the greedy
minimalisation, which is documented as producing a minimal (not minimum) set.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import matching, scoring
from .types import BlockingSet, DeskId, Infeasibility, PersonId, Problem


# --------------------------------------------------------------------------
# Diagnosis
# --------------------------------------------------------------------------


def _extended_rank(problem: Problem, rng: np.random.Generator) -> np.ndarray:
    """Each person's submitted ranks, extended over their remaining ELIGIBLE
    desks in a seeded order.

    This is how we answer "how close were we?". It is explicitly hypothetical:
    ranks past K are invented by us, not chosen by the student, so an assignment
    built on them is never emitted. It exists only so the coordinator can say
    "you would all have needed to rank about seven desks" instead of "it didn't
    work".

    The order is seeded rather than by desk index so the reported K_min does not
    silently depend on how rooms.json happens to be sorted.
    """
    rank = problem.rank.copy().astype(np.int16)
    eligible = problem.eligible
    if eligible is None:
        eligible = problem.allowed

    for i in range(problem.n_people):
        unranked = [
            j for j in range(problem.n_desks) if eligible[i, j] and rank[i, j] < 1
        ]
        if not unranked:
            continue
        order = rng.permutation(len(unranked))
        for step, idx in enumerate(order.tolist(), start=problem.k + 1):
            rank[i, unranked[idx]] = step
    return rank


def _min_feasible_k_extended(
    problem: Problem, rng: np.random.Generator
) -> int | None:
    ext = _extended_rank(problem, rng)
    eligible = problem.eligible if problem.eligible is not None else problem.allowed
    # Feasibility is monotone in K, so sweep upward and stop at the first hit.
    # The ceiling is n_desks: past that there are no more edges to add.
    for k_try in range(problem.k + 1, problem.n_desks + 1):
        allowed = eligible & (ext >= 1) & (ext <= k_try)
        if matching.has_perfect_left_matching(allowed):
            return k_try
    return None


def diagnose(problem: Problem, seed_string: str) -> Infeasibility:
    """Full structural diagnosis of a failed K-floor solve."""
    rng = scoring.make_rng(seed_string)

    adj = matching.adjacency(problem.allowed)
    match_left, _ = matching.hopcroft_karp(adj, problem.n_desks)
    max_satisfiable = int((match_left >= 0).sum())

    def _as_blocking(lefts, rights, *, minimal: bool) -> BlockingSet:
        return BlockingSet(
            people=tuple(problem.people[i] for i in lefts),
            names=tuple(problem.person_names[problem.people[i]] for i in lefts),
            desks=tuple(problem.desks[j] for j in rights),
            desk_labels=tuple(problem.desk_labels[problem.desks[j]] for j in rights),
            minimal=minimal,
        )

    # One pair of statements per independent over-subscribed group: the whole
    # group (the number to act on) followed by a minimal violator inside it (the
    # tightest true claim). Kept per-component rather than merged globally --
    # two unrelated groups must stay two statements, or the coordinator sends
    # twice as many students back to re-rank as necessary.
    blocking: list[BlockingSet] = []
    for full, full_n, minimal, minimal_n in matching.blocking_groups(
        problem.allowed, rng
    ):
        if minimal and minimal != full:
            blocking.append(_as_blocking(full, full_n, minimal=False))
            blocking.append(_as_blocking(minimal, minimal_n, minimal=True))
        else:
            # The group is already as small as it gets; one statement suffices.
            blocking.append(_as_blocking(full, full_n, minimal=True))

    always_idx, sometimes_idx = matching.unmatched_analysis(problem.allowed)

    k_min_sub = matching.min_feasible_k(
        problem.rank.astype(np.int16), problem.allowed, problem.k
    )
    k_min_ext = _min_feasible_k_extended(problem, scoring.make_rng(seed_string))

    return Infeasibility(
        n_people=problem.n_people,
        n_desks=problem.n_desks,
        k=problem.k,
        max_satisfiable=max_satisfiable,
        blocking_sets=tuple(blocking),
        k_min_submitted=k_min_sub,
        k_min_extended=k_min_ext,
        always_unmatched=tuple(problem.people[i] for i in always_idx),
        sometimes_unmatched=tuple(problem.people[i] for i in sometimes_idx),
        seed_string=seed_string,
    )


# --------------------------------------------------------------------------
# Round-2 scoping
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Round2Entry:
    email: PersonId
    name: str
    reason: str
    current_choices: tuple[DeskId, ...]
    available_desks: tuple[DeskId, ...]
    available_labels: tuple[str, ...]
    suggested_min_ranks: int


def build_round2(
    problem: Problem, diagnosis: Infeasibility
) -> tuple[Round2Entry, ...]:
    """Scope the second round to the people who actually have to act.

    Deliberately does NOT finalise anyone else's assignment. Publishing partial
    results mid-process would leak information and hand the un-affected students
    a reason to think the outcome was already decided; it would also create a
    strategic incentive in round two that does not exist in round one.

    `available_desks` is every desk still in the pool that this person is
    eligible for -- including ones they already ranked, since the point is to
    show them the full field and ask for a longer list.
    """
    eligible = problem.eligible if problem.eligible is not None else problem.allowed
    index = {e: i for i, e in enumerate(problem.people)}

    affected: dict[PersonId, str] = {}
    for bs in diagnosis.blocking_sets:
        for email in bs.people:
            affected.setdefault(
                email,
                f"part of a group of {len(bs.people)} people whose choices span "
                f"only {len(bs.desks)} desks",
            )
    for email in diagnosis.always_unmatched:
        affected[email] = "no maximum matching can seat them within their top K"
    for email in diagnosis.sometimes_unmatched:
        affected.setdefault(email, "at risk: some optimal outcomes leave them out")

    # How many extra ranks would have cleared it, if we know.
    extra = 0
    if diagnosis.k_min_extended is not None:
        extra = max(0, diagnosis.k_min_extended - problem.k)

    out: list[Round2Entry] = []
    for email in sorted(affected):
        i = index[email]
        avail = [j for j in range(problem.n_desks) if eligible[i, j]]
        ranked = {
            int(problem.rank[i, j]): problem.desks[j]
            for j in range(problem.n_desks)
            if problem.rank[i, j] >= 1
        }
        out.append(
            Round2Entry(
                email=email,
                name=problem.person_names[email],
                reason=affected[email],
                current_choices=tuple(ranked[r] for r in sorted(ranked)),
                available_desks=tuple(problem.desks[j] for j in avail),
                available_labels=tuple(problem.desk_labels[problem.desks[j]] for j in avail),
                suggested_min_ranks=problem.k + max(extra, 1),
            )
        )
    return tuple(out)


def write_round2(
    path_json: Path, path_csv: Path, problem: Problem, diagnosis: Infeasibility
) -> tuple[Round2Entry, ...]:
    entries = build_round2(problem, diagnosis)

    doc = {
        "reason": "K-floor infeasible; these students must extend their rankings.",
        "k": problem.k,
        "suggested_k": (
            diagnosis.k_min_extended if diagnosis.k_min_extended is not None
            else problem.k + 1
        ),
        "deficiency": diagnosis.deficiency,
        "seed_string": diagnosis.seed_string,
        "students": [
            {
                "email": e.email,
                "name": e.name,
                "reason": e.reason,
                "current_choices": list(e.current_choices),
                "available_desks": [
                    {"id": d, "label": lbl}
                    for d, lbl in zip(e.available_desks, e.available_labels)
                ],
                "suggested_min_ranks": e.suggested_min_ranks,
            }
            for e in entries
        ],
    }
    path_json.write_text(
        json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    import csv

    with path_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["name", "email", "reason", "current_choices", "available_desks",
             "suggested_min_ranks"]
        )
        for e in entries:
            writer.writerow([
                e.name, e.email, e.reason,
                " ".join(e.current_choices),
                " ".join(e.available_desks),
                e.suggested_min_ranks,
            ])
    return entries


def diagnosis_to_dict(diagnosis: Infeasibility) -> dict:
    return {
        "feasible": False,
        "k": diagnosis.k,
        "n_people": diagnosis.n_people,
        "n_desks": diagnosis.n_desks,
        "max_satisfiable": diagnosis.max_satisfiable,
        "deficiency": diagnosis.deficiency,
        "k_min_submitted": diagnosis.k_min_submitted,
        "k_min_extended": diagnosis.k_min_extended,
        "k_min_extended_note": (
            "Hypothetical. Ranks beyond K were filled in by the solver in a "
            "seeded order over each person's eligible desks, NOT chosen by the "
            "student. No assignment is ever produced from these."
        ),
        "always_unmatched": list(diagnosis.always_unmatched),
        "sometimes_unmatched": list(diagnosis.sometimes_unmatched),
        "seed_string": diagnosis.seed_string,
        "blocking_sets": [
            {
                "shortfall": bs.shortfall,
                "people": [
                    {"email": e, "name": n} for e, n in zip(bs.people, bs.names)
                ],
                "desks": [
                    {"id": d, "label": lbl}
                    for d, lbl in zip(bs.desks, bs.desk_labels)
                ],
            }
            for bs in diagnosis.blocking_sets
        ],
    }


# --------------------------------------------------------------------------
# Pre-deadline feasibility check
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightResult:
    would_succeed: bool
    n_responded: int
    n_outstanding: int
    max_satisfiable_now: int
    blocking_sets: tuple[BlockingSet, ...]
    hot_desks: tuple[tuple[DeskId, str, int], ...]   # (id, label, first-choice count)
    zone_pressure: tuple[tuple[str, int, int], ...]  # (zone, n_people_eligible_only_here, n_desks)
    messages: tuple[str, ...]

    def render(self) -> str:
        lines = [
            f"Responses in: {self.n_responded}   still outstanding: {self.n_outstanding}",
            f"Of those who have responded, at most {self.max_satisfiable_now} can "
            f"currently get a top-K desk.",
        ]
        lines.append(
            "STATUS: on track." if self.would_succeed
            else "STATUS: would FAIL if the form closed now."
        )
        for bs in self.blocking_sets:
            lines.append("  " + bs.render().replace("\n", "\n  "))
        if self.hot_desks:
            lines.append("Most-contested desks (first-choice votes so far):")
            for desk_id, label, count in self.hot_desks:
                lines.append(f"    {desk_id} ({label}): {count}")
        lines.extend("  " + m for m in self.messages)
        return "\n".join(lines)


def preflight(
    problem: Problem,
    n_outstanding: int,
    seed_string: str,
    top_n_hot: int = 8,
) -> PreflightResult:
    """Run the structural analysis on a partial response set.

    Intended to be run daily while the form is open, so the coordinator nudges
    people *before* the deadline rather than discovering the problem after it.

    Non-responders are simply absent from `problem`; they are not modelled as
    unconstrained placeholders, because doing so would mask exactly the failure
    we are looking for. A group of responders who already collide will still
    collide once everyone else submits -- adding more people can only add
    competition for the same desks, never relieve it. So a failure here is a
    real failure; a pass here is not yet a guarantee, and the message says so.
    """
    rng = scoring.make_rng(seed_string)
    adj = matching.adjacency(problem.allowed)
    match_left, _ = matching.hopcroft_karp(adj, problem.n_desks)
    max_now = int((match_left >= 0).sum())
    feasible = max_now == problem.n_people

    blocking: list[BlockingSet] = []
    if not feasible:
        for lefts, rights in matching.hall_violators(problem.allowed, rng):
            blocking.append(
                BlockingSet(
                    people=tuple(problem.people[i] for i in lefts),
                    names=tuple(problem.person_names[problem.people[i]] for i in lefts),
                    desks=tuple(problem.desks[j] for j in rights),
                    desk_labels=tuple(problem.desk_labels[problem.desks[j]] for j in rights),
                )
            )

    first_choice = (problem.rank == 1).sum(axis=0)
    order = sorted(
        range(problem.n_desks),
        key=lambda j: (-int(first_choice[j]), problem.desks[j]),
    )
    hot = tuple(
        (problem.desks[j], problem.desk_labels[problem.desks[j]], int(first_choice[j]))
        for j in order[:top_n_hot]
        if first_choice[j] > 0
    )

    messages: list[str] = []
    if feasible and n_outstanding:
        messages.append(
            f"{n_outstanding} people have not submitted yet. Passing now does not "
            f"guarantee passing later -- more responders can only add competition."
        )
    if not feasible:
        messages.append(
            "Contact the people named above and ask them to widen their choices. "
            "They do not need to change their top pick, only to rank further down."
        )
    return PreflightResult(
        would_succeed=feasible,
        n_responded=problem.n_people,
        n_outstanding=n_outstanding,
        max_satisfiable_now=max_now,
        blocking_sets=tuple(blocking),
        hot_desks=hot,
        zone_pressure=(),
        messages=tuple(messages),
    )
