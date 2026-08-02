"""The assignment solve.

Pure function of (Problem, seed_string, backend). No I/O, no clock, no override
path -- invariant I2. If the coordinator wants a different answer they have to
change an input file, and that is visible in git.

The solve is guarded three independent ways against ever emitting a desk outside
someone's top K:

  1. Structural -- `allowed` contains no such cell, so the matrix cannot express
     the violation in the first place (problem.py).
  2. Feasibility -- a Hopcroft-Karp check runs *before* any cost minimisation. If
     no complete assignment exists within top-K we raise, we do not approximate.
  3. Post-hoc -- every returned cell is asserted allowed, and the achieved total
     is asserted equal to the jitter-free optimum.

Belt, braces, and a second pair of braces. This is the invariant the whole
process is sold on (invariant I4/I7), so it gets three checks rather than one.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np

from . import diagnostics, matching, scoring
from .errors import DeterminismError, InfeasibleError
from .types import Assignment, Problem, Solution


class AssignmentBackend(Protocol):
    """Swap-in point for a different solver.

    If side constraints ever appear (couples, a capacity per zone, a
    must-not-sit-next-to rule), an ILP formulation is the natural escape hatch
    and only this interface has to be satisfied -- nothing upstream changes.
    """

    name: str

    def solve(self, weights: np.ndarray) -> np.ndarray:
        """Maximise the total of `weights` over an injective row->column map.

        Returns an int array `cols` with cols[i] = column assigned to row i.
        Requires n_rows <= n_cols; callers guarantee that via a feasibility
        check first.
        """
        ...


class ScipyJVBackend:
    """scipy.optimize.linear_sum_assignment -- Jonker-Volgenant.

    Exact, O(n^3) worst case, and for a department-sized problem (tens of rows)
    it finishes far faster than the CSV takes to parse. There is no reason to
    reach for anything else unless the problem gains side constraints.
    """

    name = "scipy-jv"

    def solve(self, weights: np.ndarray) -> np.ndarray:
        from scipy.optimize import linear_sum_assignment

        rows, cols = linear_sum_assignment(weights, maximize=True)
        if not np.array_equal(rows, np.arange(weights.shape[0])):
            # Rectangular input with more rows than columns; the feasibility
            # pre-check should have made this unreachable.
            raise DeterminismError(
                "backend did not return one column per row; the feasibility "
                "pre-check should have prevented this"
            )
        return np.asarray(cols, dtype=np.int64)


class PuLPBackend:
    """ILP formulation, for when side constraints arrive.

    Not used by default. Kept working (and exercised by the test suite when PuLP
    is installed) so that the escape hatch is real rather than aspirational.
    """

    name = "pulp-cbc"

    def solve(self, weights: np.ndarray) -> np.ndarray:
        import pulp

        n_rows, n_cols = weights.shape
        prob = pulp.LpProblem("assignment", pulp.LpMaximize)

        # PuLP 4.0 moves variable creation onto the model and deprecates the
        # LpVariable(...) constructor. pyproject allows pulp>=2.7, so support
        # both rather than emitting a deprecation warning per variable (which at
        # department scale is hundreds of lines of noise that would bury a real
        # warning).
        if hasattr(prob, "add_variable"):
            def _var(name: str):
                return prob.add_variable(name, 0, 1, cat="Binary")
        else:  # pragma: no cover - depends on the installed pulp
            def _var(name: str):
                return pulp.LpVariable(name, cat="Binary")

        x = {
            (i, j): _var(f"x_{i}_{j}")
            for i in range(n_rows)
            for j in range(n_cols)
        }
        prob += pulp.lpSum(float(weights[i, j]) * x[i, j] for i, j in x)
        for i in range(n_rows):
            prob += pulp.lpSum(x[i, j] for j in range(n_cols)) == 1
        for j in range(n_cols):
            prob += pulp.lpSum(x[i, j] for i in range(n_rows)) <= 1
        # PULP_CBC_CMD is likewise deprecated in favour of COIN_CMD. Prefer
        # whichever the installed pulp actually wants, and fall back to letting
        # pulp choose its own default solver if neither is importable.
        cmd = None
        for name in ("COIN_CMD", "PULP_CBC_CMD"):
            factory = getattr(pulp, name, None)
            if factory is None:
                continue
            try:
                candidate = factory(msg=False)
            except Exception:  # solver binary not present
                continue
            if candidate.available():
                cmd = candidate
                break
        status = prob.solve(cmd) if cmd is not None else prob.solve()
        if pulp.LpStatus[status] != "Optimal":
            raise DeterminismError(f"ILP backend returned status {pulp.LpStatus[status]}")
        cols = np.full(n_rows, -1, dtype=np.int64)
        for (i, j), var in x.items():
            if var.value() is not None and var.value() > 0.5:
                cols[i] = j
        if (cols < 0).any():
            raise DeterminismError("ILP backend left a row unassigned")
        return cols


BACKENDS: dict[str, type] = {
    ScipyJVBackend.name: ScipyJVBackend,
    PuLPBackend.name: PuLPBackend,
}


def get_backend(name: str | None) -> AssignmentBackend:
    cls = BACKENDS.get(name or ScipyJVBackend.name)
    if cls is None:
        raise ValueError(
            f"unknown solver backend {name!r}; available: {', '.join(sorted(BACKENDS))}"
        )
    return cls()  # type: ignore[return-value]


# --------------------------------------------------------------------------


def _big_m(points: np.ndarray, n_rows: int) -> int:
    """A weight so bad that no optimal solution would ever use a forbidden cell.

    Deliberately not -inf. Infinities in a cost matrix make the solver's
    behaviour depend on library internals, and a silent NaN would be
    catastrophic here. A finite sentinel keeps the arithmetic ordinary and lets
    us *check* afterwards that no forbidden cell was used -- which is the
    property we actually care about.

    Any complete assignment on allowed cells scores >= n_rows * 1 > 0 (curve
    values are strictly positive). One forbidden cell costs more than the best
    possible complete assignment could ever earn, so it can never appear in an
    optimum when a feasible alternative exists.
    """
    best_possible = int(points.max(initial=0)) * max(n_rows, 1)
    return -(best_possible + 1)


def solve(
    problem: Problem,
    seed_string: str,
    backend: AssignmentBackend | str | None = None,
) -> Solution:
    """Solve, or raise InfeasibleError with a full diagnosis."""
    if isinstance(backend, str) or backend is None:
        backend = get_backend(backend)

    n, m = problem.n_people, problem.n_desks

    if n == 0:
        return Solution(
            assignments=(), total_points_scaled=0, scale=problem.scale,
            curve_name=problem.curve_name, seed_string=seed_string,
            seed_int=scoring.seed_int(seed_string), k=problem.k,
            backend=backend.name, free_desks=problem.desks,
        )

    # --- Guard 2: feasibility before optimisation ------------------------
    if not matching.has_perfect_left_matching(problem.allowed):
        raise InfeasibleError(diagnostics.diagnose(problem, seed_string))

    rng = scoring.make_rng(seed_string)

    # --- Tie-break (a): seeded permutation -------------------------------
    # Solver output can depend on input ordering. Rather than pretend it does
    # not, we make that dependence a published, seeded choice instead of an
    # artefact of however the roster happened to be sorted.
    row_perm = rng.permutation(n)
    col_perm = rng.permutation(m)

    points_p = problem.points[np.ix_(row_perm, col_perm)]
    allowed_p = problem.allowed[np.ix_(row_perm, col_perm)]

    # --- Tie-break (b): jitter, with the bound asserted ------------------
    epsilon = scoring.jitter_epsilon(n)
    scoring.assert_jitter_bound(problem.points, n, epsilon)
    jitter = scoring.jitter_matrix(rng, (n, m), epsilon)

    # --- Guard 3a: forbidden cells get a finite, provably-losing weight --
    big_m = _big_m(problem.points, n)
    base = np.where(allowed_p, points_p.astype(np.float64), float(big_m))
    weights = np.where(allowed_p, base + jitter, base)

    cols_p = backend.solve(weights)

    # Reference solve with no jitter, to prove the jitter changed nothing.
    cols_ref = backend.solve(base)

    # --- Un-permute ------------------------------------------------------
    inv_col = np.empty(m, dtype=np.int64)
    inv_col[col_perm] = np.arange(m)
    assign_col = np.empty(n, dtype=np.int64)
    for pi, pj in enumerate(cols_p):
        assign_col[row_perm[pi]] = col_perm[pj]

    # --- Guard 3b: assertions -------------------------------------------
    total = 0
    for i, j in enumerate(assign_col):
        if not problem.allowed[i, j]:
            raise DeterminismError(
                f"solver returned a forbidden pairing: {problem.people[i]} -> "
                f"{problem.desks[j]}. This must never happen; the result is void."
            )
        r = int(problem.rank[i, j])
        if not 1 <= r <= problem.k:
            raise DeterminismError(
                f"K-floor violated: {problem.people[i]} was assigned "
                f"{problem.desks[j]} at rank {r} (K={problem.k})."
            )
        total += int(problem.points[i, j])

    ref_total = int(sum(base[i, j] for i, j in enumerate(cols_ref)))
    if total != ref_total:
        raise DeterminismError(
            f"tie-break jitter changed the optimum: jittered solution scores "
            f"{total} but the jitter-free optimum is {ref_total}. The epsilon "
            f"bound has failed and the result must not be used."
        )

    assignments = tuple(
        sorted(
            (
                Assignment(
                    email=problem.people[i],
                    name=problem.person_names[problem.people[i]],
                    desk_id=problem.desks[j],
                    desk_label=problem.desk_labels[problem.desks[j]],
                    rank_received=int(problem.rank[i, j]),
                    points=int(problem.points[i, j]),
                )
                for i, j in enumerate(assign_col)
            ),
            key=lambda a: a.email,
        )
    )

    taken = {int(j) for j in assign_col}
    free = tuple(problem.desks[j] for j in range(m) if j not in taken)

    return Solution(
        assignments=assignments,
        total_points_scaled=total,
        scale=problem.scale,
        curve_name=problem.curve_name,
        seed_string=seed_string,
        seed_int=scoring.seed_int(seed_string),
        k=problem.k,
        backend=backend.name,
        free_desks=free,
    )


def brute_force_optimum(problem: Problem) -> int | None:
    """Exhaustive optimum, for tests only. Returns None if infeasible.

    Exists so the test suite can confirm the JV result is genuinely optimal on
    small instances rather than merely self-consistent.
    """
    from itertools import permutations

    n, m = problem.n_people, problem.n_desks
    if n == 0:
        return 0
    if n > m:
        return None
    best: int | None = None
    for combo in permutations(range(m), n):
        total = 0
        ok = True
        for i, j in enumerate(combo):
            if not problem.allowed[i, j]:
                ok = False
                break
            total += int(problem.points[i, j])
        if ok and (best is None or total > best):
            best = total
    return best
