"""Baselines to compare the optimal assignment against.

The point of these is to answer the question everyone actually has -- "was this
better than just drawing lots?" -- with a number rather than an assurance.

Random serial dictatorship (RSD) is the right baseline because it is very close
to what the department was doing before: people arrive in an effectively random
order (determined by internet speed and who happened to be at their desk) and
each takes the best desk still free. Modelling the old process and showing where
the optimum falls in its distribution is the most honest comparison available.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import scoring
from .types import Problem


@dataclass(frozen=True)
class BaselineResult:
    name: str
    totals: np.ndarray            # (n_trials,) achieved total points, scaled
    rank_counts: np.ndarray       # (n_trials, k) rank histogram per trial
    unassigned: np.ndarray        # (n_trials,) people left with nothing in top-K
    complete_fraction: float      # fraction of trials that seated everyone
    n_trials: int

    def percentile_of(self, value: float) -> float:
        """Where `value` falls in this distribution, 0-100."""
        if self.totals.size == 0:
            return float("nan")
        return float((self.totals < value).mean() * 100.0)

    def mean_rank_distribution(self) -> np.ndarray:
        """Average count per rank across trials."""
        return self.rank_counts.mean(axis=0)


def random_serial_dictatorship(
    problem: Problem,
    n_trials: int,
    seed_string: str,
) -> BaselineResult:
    """Monte-Carlo the old process.

    Each trial: shuffle the people, then walk the order letting each person take
    their highest-ranked desk that is still free. A person whose whole top-K is
    already gone ends the trial unassigned -- which is exactly the failure mode
    the optimal solve refuses to produce, so counting it is informative.

    Seeded off `seed_string` with a distinct suffix so the baseline draws cannot
    correlate with the tie-break draws. Same seed, same figure, forever.
    """
    n, m = problem.n_people, problem.n_desks
    k = problem.k
    rng = scoring.make_rng(seed_string + "::rsd-baseline")

    totals = np.zeros(n_trials, dtype=np.int64)
    rank_counts = np.zeros((n_trials, k), dtype=np.int32)
    unassigned = np.zeros(n_trials, dtype=np.int32)

    if n == 0:
        return BaselineResult("random serial dictatorship", totals, rank_counts,
                              unassigned, 1.0, n_trials)

    # Precompute each person's desk list in rank order; the inner loop is hot.
    prefs: list[list[int]] = []
    for i in range(n):
        ranked = [(int(problem.rank[i, j]), j) for j in range(m) if problem.rank[i, j] >= 1]
        ranked.sort()
        prefs.append([j for _, j in ranked])

    points = problem.points

    for t in range(n_trials):
        order = rng.permutation(n)
        taken = np.zeros(m, dtype=bool)
        total = 0
        for i in order.tolist():
            for r, j in enumerate(prefs[i], start=1):
                if not taken[j]:
                    taken[j] = True
                    total += int(points[i, j])
                    rank_counts[t, r - 1] += 1
                    break
            else:
                unassigned[t] += 1
        totals[t] = total

    complete = float((unassigned == 0).mean())
    return BaselineResult(
        "random serial dictatorship", totals, rank_counts, unassigned, complete, n_trials
    )


def uniform_random_assignment(
    problem: Problem, n_trials: int, seed_string: str
) -> BaselineResult:
    """A pure lottery over *valid* assignments-ish: shuffle desks onto people
    ignoring preference entirely, counting only the ones that land in someone's
    top-K.

    Weaker and less realistic than RSD, but it brackets the comparison from
    below: RSD is "everyone grabs greedily", this is "a hat". Included so the
    report can show the optimum against both a naive and a realistic baseline.
    """
    n, m = problem.n_people, problem.n_desks
    k = problem.k
    rng = scoring.make_rng(seed_string + "::uniform-baseline")

    totals = np.zeros(n_trials, dtype=np.int64)
    rank_counts = np.zeros((n_trials, k), dtype=np.int32)
    unassigned = np.zeros(n_trials, dtype=np.int32)

    if n == 0 or m == 0:
        return BaselineResult("uniform random", totals, rank_counts, unassigned,
                              1.0, n_trials)

    for t in range(n_trials):
        cols = rng.permutation(m)[:n]
        total = 0
        for i, j in enumerate(cols.tolist()):
            r = int(problem.rank[i, j])
            if 1 <= r <= k:
                total += int(problem.points[i, j])
                rank_counts[t, r - 1] += 1
            else:
                unassigned[t] += 1
        totals[t] = total

    return BaselineResult(
        "uniform random", totals, rank_counts, unassigned,
        float((unassigned == 0).mean()), n_trials
    )


def alternative_seed_outcomes(
    problem: Problem, seeds: list[str], backend: str | None = None
) -> list[tuple[str, tuple[int, ...], int, int]]:
    """Re-solve under other seeds. Returns (seed, rank_histogram, total, n_moved).

    `n_moved` counts people who got a different desk than under the primary
    seed. It is the number that makes the tie-break's real influence concrete:
    if it is zero across every alternative seed, the seed did not matter at all
    this year, and the report should say so plainly.
    """
    from . import solve as solve_mod

    if not seeds:
        return []
    reference = solve_mod.solve(problem, seeds[0], backend)
    ref_map = {a.email: a.desk_id for a in reference.assignments}

    out = []
    for seed in seeds:
        sol = solve_mod.solve(problem, seed, backend)
        moved = sum(1 for a in sol.assignments if ref_map.get(a.email) != a.desk_id)
        out.append((seed, sol.rank_histogram(), sol.total_points_scaled, moved))
    return out


def alternative_curve_outcomes(
    config, responses, seed_string: str, curve_names: list[str], backend: str | None = None
) -> list[tuple[str, tuple[int, ...], int, int]]:
    """Re-solve under other scoring curves.

    Returns (curve, rank_histogram, total_scaled, n_moved_vs_primary).

    This is the sensitivity check the coordinator asked for: if the assignment
    barely moves between a linear and a convex curve, the choice of curve was
    not doing the deciding, and that is worth showing people. If it moves a lot,
    they need to know before publishing.
    """
    from . import problem as problem_mod
    from . import solve as solve_mod

    primary = config.scoring.primary_curve
    ref_report = problem_mod.build_problem(config, responses, primary)
    ref = solve_mod.solve(ref_report.problem, seed_string, backend)
    ref_map = {a.email: a.desk_id for a in ref.assignments}

    out = []
    for name in curve_names:
        report = problem_mod.build_problem(config, responses, name)
        sol = solve_mod.solve(report.problem, seed_string, backend)
        moved = sum(1 for a in sol.assignments if ref_map.get(a.email) != a.desk_id)
        out.append((name, sol.rank_histogram(), sol.total_points_scaled, moved))
    return out
