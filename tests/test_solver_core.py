"""The solver core: optimality, the K-floor, zones, determinism, and matching.

What this file is for, in one sentence: the department is being asked to trust
an assignment it cannot check by hand, so every claim the README makes about the
solve is re-derived here from the matrix rather than taken on the solver's word.

Organised as:

  1. Optimality      -- the Jonker-Volgenant answer equals an exhaustive optimum,
                        over hundreds of random instances at many (N, M, K).
  2. Invariants      -- I4 (top-K) and I5 (zone) on every Solution produced
                        anywhere in this file, via `conftest.assert_solution_invariants`.
  3. Determinism     -- same (problem, seed) is field-for-field identical (I3),
                        and different seeds move the assignment but never the total.
  4. The jitter bound-- I6, including the runtime guard and the n*eps < 1 algebra.
  5. Integerisation  -- SPEC §5.3, the exactness the jitter bound rests on.
  6. Forbidden cells -- the solver never takes a masked cell, even when the
                        unmasked optimum would.
  7. Degenerate sizes-- 0, 1, exact fit, and over-subscribed (I8 in both directions).
  8. matching.py     -- Hopcroft-Karp against scipy, `unmatched_analysis` against
                        exhaustive enumeration, Hall violators, and the K sweep.

Nothing here hard-codes a problem dimension. Every test is parameterised over
N, M and K, because a test that only passes at one size is not testing I1.
"""

from __future__ import annotations

import math
from fractions import Fraction

import numpy as np
import pytest

from deskmatch import matching, scoring, synth
from deskmatch import solve as solve_mod
from deskmatch.errors import DeterminismError, InfeasibleError
from deskmatch.types import Problem as SolveProblem

from conftest import (
    CURVE_KINDS,
    REAL_CONFIG_EXPECTED,
    assert_solution_invariants,
    assert_solutions_identical,
    build_problem_from_prefs,
    expected_scale,
    is_feasible,
    make_curve,
    random_problem,
    solution_signature,
    stable_rng,
)

SEED = "test-seed-committed-before-the-form-opened"


# --------------------------------------------------------------------------
# Shapes. Chosen so an exhaustive optimum stays cheap: brute force is
# P(M, N) permutations, so M <= 7 keeps the worst case at 2520.
# --------------------------------------------------------------------------

SHAPES: tuple[tuple[int, int, int], ...] = (
    # (n_people, n_desks, K). Deliberately mixed:
    #   * n < m, n == m and n > m, because I8 says either direction is normal;
    #   * K == 1 shapes, where collisions make infeasibility routine;
    #   * K == M shapes, where everyone ranks everything.
    (0, 3, 2),
    (1, 1, 1),
    (1, 4, 3),
    (2, 1, 1),
    (2, 2, 2),
    (2, 5, 2),
    (3, 2, 2),
    (3, 3, 3),
    (3, 4, 1),
    (3, 5, 2),
    (3, 6, 4),
    (4, 3, 2),
    (4, 4, 4),
    (4, 5, 1),
    (4, 6, 3),
    (4, 7, 2),
    (5, 4, 3),
    (5, 5, 5),
    (5, 6, 2),
    (5, 7, 4),
    (6, 4, 3),
)

SHAPE_IDS = tuple(f"n{n}-m{m}-k{k}" for n, m, k in SHAPES)

#: Random instances per shape. 21 shapes x 20 = 420 instances per property sweep.
INSTANCES_PER_SHAPE = 20
ZONED_INSTANCES_PER_SHAPE = 15


def feasible_random_problem(*seed_parts, max_attempts: int = 60, **kwargs) -> SolveProblem:
    """A random instance that admits a complete top-K assignment.

    Deterministic: attempts are indexed, not retried at random. Raises rather
    than skipping if no feasible instance turns up, because a silent skip here
    would quietly delete a test.
    """
    for attempt in range(max_attempts):
        problem = random_problem(*seed_parts, attempt, **kwargs)
        if is_feasible(problem):
            return problem
    raise AssertionError(
        f"no feasible instance in {max_attempts} attempts for {kwargs}; "
        f"widen the shape or the test is measuring nothing"
    )


# ==========================================================================
# 1. Optimality: JV == exhaustive optimum
# ==========================================================================


@pytest.mark.parametrize(("n_people", "n_desks", "k"), SHAPES, ids=SHAPE_IDS)
def test_jv_total_equals_brute_force_optimum(n_people, n_desks, k):
    """The scipy JV result is the true global optimum, not merely self-consistent.

    Also pins the *other* half of the contract: where no complete top-K
    assignment exists, `solve` must raise rather than return the best partial
    thing it found (invariant I7). Brute force and the solver must agree on
    which of those two worlds each instance is in.
    """
    feasible_seen = infeasible_seen = 0

    for trial in range(INSTANCES_PER_SHAPE):
        kind = CURVE_KINDS[trial % len(CURVE_KINDS)]
        problem = random_problem(
            "jv-vs-brute-force", trial,
            n_people=n_people, n_desks=n_desks, k=k, curve_kind=kind,
        )
        best = solve_mod.brute_force_optimum(problem)

        if best is None:
            infeasible_seen += 1
            with pytest.raises(InfeasibleError):
                solve_mod.solve(problem, SEED)
            assert not is_feasible(problem), (
                "brute force found no complete assignment but Hopcroft-Karp did"
            )
            continue

        feasible_seen += 1
        assert is_feasible(problem)
        solution = solve_mod.solve(problem, SEED)
        assert solution.total_points_scaled == best, (
            f"JV scored {solution.total_points_scaled} but the exhaustive optimum "
            f"is {best} (shape n={n_people} m={n_desks} K={k}, curve {kind}, trial {trial})"
        )
        assert_solution_invariants(problem, solution)

    assert feasible_seen + infeasible_seen == INSTANCES_PER_SHAPE


@pytest.mark.parametrize(("n_people", "n_desks", "k"), SHAPES, ids=SHAPE_IDS)
@pytest.mark.parametrize("n_zones", (2, 3))
def test_jv_optimum_under_zone_restrictions(n_people, n_desks, k, n_zones):
    """The same optimality claim, with eligibility actually biting.

    With zones on, people routinely rank desks they may not have, so the matrix
    is sparser and the masked cells are the ones a careless solver would use.
    Every successful solve is checked against I4 *and* I5.
    """
    if n_desks < n_zones:
        pytest.skip(f"{n_desks} desks cannot be split across {n_zones} zones")

    dropped_choice_seen = 0
    solved = 0
    feasible_seen = 0

    for trial in range(ZONED_INSTANCES_PER_SHAPE):
        kind = CURVE_KINDS[trial % len(CURVE_KINDS)]
        problem = random_problem(
            "zoned-jv", trial, n_zones, n_people=n_people, n_desks=n_desks, k=k,
            n_zones=n_zones, restricted_frac=0.6, curve_kind=kind,
        )
        # Every person ranks exactly min(K, M) desks, so fewer allowed cells than
        # that means eligibility dropped at least one of their choices.
        wanted = min(k, n_desks)
        dropped_choice_seen += int(
            bool(np.any(problem.allowed.sum(axis=1) < wanted)) and n_people > 0
        )

        best = solve_mod.brute_force_optimum(problem)
        if best is None:
            with pytest.raises(InfeasibleError):
                solve_mod.solve(problem, SEED)
            continue

        feasible_seen += 1
        solution = solve_mod.solve(problem, SEED)
        assert solution.total_points_scaled == best
        assert_solution_invariants(problem, solution)
        solved += 1

    assert solved == feasible_seen, "a feasible instance was not solved"
    if n_people:
        assert dropped_choice_seen > 0, (
            "no instance in this sweep had a choice dropped by eligibility, so the "
            "zone constraint was never actually exercised"
        )


def test_random_instance_generator_produces_both_feasible_and_infeasible():
    """Guard against the property sweeps quietly testing only one branch.

    Both sweeps above short-circuit on infeasible instances, so if the generator
    ever produced only one kind of instance the optimality assertions would
    silently stop running. This counts them.
    """
    plain_feasible = plain_infeasible = 0
    for n_people, n_desks, k in SHAPES:
        for trial in range(INSTANCES_PER_SHAPE):
            problem = random_problem(
                "jv-vs-brute-force", trial,
                n_people=n_people, n_desks=n_desks, k=k,
                curve_kind=CURVE_KINDS[trial % len(CURVE_KINDS)],
            )
            if is_feasible(problem):
                plain_feasible += 1
            else:
                plain_infeasible += 1
    assert plain_feasible > 0 and plain_infeasible > 0, (plain_feasible, plain_infeasible)
    assert plain_feasible + plain_infeasible == len(SHAPES) * INSTANCES_PER_SHAPE

    zoned_feasible = zoned_infeasible = 0
    for n_zones in (2, 3):
        for n_people, n_desks, k in SHAPES:
            if n_desks < n_zones:
                continue
            for trial in range(ZONED_INSTANCES_PER_SHAPE):
                problem = random_problem(
                    "zoned-jv", trial, n_zones, n_people=n_people, n_desks=n_desks, k=k,
                    n_zones=n_zones, restricted_frac=0.6,
                    curve_kind=CURVE_KINDS[trial % len(CURVE_KINDS)],
                )
                if is_feasible(problem):
                    zoned_feasible += 1
                else:
                    zoned_infeasible += 1
    assert zoned_feasible > 0 and zoned_infeasible > 0, (zoned_feasible, zoned_infeasible)


# ==========================================================================
# 2. The invariants on a hand-written, readable instance
# ==========================================================================


def test_declarative_instance_reads_as_written(make_problem, invariants):
    """A sanity check on the fixture itself: three people, three desks, K=2."""
    problem = make_problem([[0, 1], [0, 1], [2, 0]])
    assert problem.n_people == 3
    assert problem.n_desks == 3
    assert problem.k == 2
    assert problem.desks == ("D01", "D02", "D03")

    solution = solve_mod.solve(problem, SEED)
    invariants.all(problem, solution)
    # Two people want D01 first; one of them must take D02 (rank 2). The third
    # person's first choice, D03, is uncontested. 2 + 1 + 2 = 5 with curve [2, 1].
    assert solution.total_points_scaled == 5
    assert solution.total_points_scaled == solve_mod.brute_force_optimum(problem)
    assert solution.rank_histogram() == (2, 1)


@pytest.mark.parametrize("k", (1, 2, 3, 5))
def test_zone_ineligible_choices_never_enter_the_matrix(make_problem, k):
    """I5 is structural: a ranked desk in a forbidden zone is not a cell at all."""
    n_desks = k + 2
    zones = ["open"] * n_desks
    zones[0] = "vip"
    problem = make_problem(
        [list(range(k)), list(range(k))],
        desks=n_desks,
        k=k,
        zone_of_desk=zones,
        allowed_zones=[("open",), "*"],
    )
    assert not problem.allowed[0, 0], "a forbidden zone produced an allowed cell"
    assert not problem.eligible[0, 0]
    assert problem.rank[0, 0] == -1, "a dropped choice must not keep its rank"
    assert problem.eligible[1, 0] and problem.allowed[1, 0]
    assert np.all(problem.allowed <= problem.eligible)


# ==========================================================================
# 3. Determinism and seed variation
# ==========================================================================


@pytest.mark.parametrize("n_people", (1, 2, 5, 9, 14))
@pytest.mark.parametrize("curve_kind", CURVE_KINDS)
def test_same_problem_and_seed_is_field_for_field_identical(n_people, curve_kind):
    """Invariant I3, at several N and on every curve family."""
    k = min(5, max(1, n_people))
    problem = feasible_random_problem(
        "determinism", curve_kind,
        n_people=n_people, n_desks=n_people + 4, k=k, curve_kind=curve_kind,
    )
    first = solve_mod.solve(problem, SEED)
    second = solve_mod.solve(problem, SEED)
    assert_solutions_identical(first, second, context=f"N={n_people} curve={curve_kind}")
    assert_solution_invariants(problem, first)

    # A freshly built but identical Problem object must also agree: nothing may
    # ride on object identity or on numpy buffer reuse.
    rebuilt = feasible_random_problem(
        "determinism", curve_kind,
        n_people=n_people, n_desks=n_people + 4, k=k, curve_kind=curve_kind,
    )
    assert np.array_equal(rebuilt.allowed, problem.allowed)
    assert_solutions_identical(solve_mod.solve(rebuilt, SEED), first, context="rebuilt")


def test_determinism_on_the_real_config(real_problem):
    """The shipped config, twice."""
    first = solve_mod.solve(real_problem, SEED)
    second = solve_mod.solve(real_problem, SEED)
    assert_solutions_identical(first, second, context="real config")
    assert_solution_invariants(real_problem, first)


def test_real_config_produces_the_recorded_answer(real_config, real_problem):
    """The regression pin for the department's actual data.

    Every expectation here was produced by this pipeline and checked by hand;
    if one of them moves, something in the solve moved with it.
    """
    assert real_problem.n_people == REAL_CONFIG_EXPECTED.n_people
    assert real_problem.n_desks == REAL_CONFIG_EXPECTED.n_desks
    assert real_problem.k == REAL_CONFIG_EXPECTED.k
    assert real_problem.k == len(real_config.scoring.curve()), "K must be derived, not declared"
    assert real_problem.scale == REAL_CONFIG_EXPECTED.scale

    solution = solve_mod.solve(real_problem, real_config.scoring.tie_break_seed)
    assert_solution_invariants(real_problem, solution)
    assert solution.rank_histogram() == REAL_CONFIG_EXPECTED.rank_histogram
    assert solution.total_points_scaled == REAL_CONFIG_EXPECTED.total_points_scaled


@pytest.mark.parametrize(("n_blocks", "k"), ((2, 3), (3, 2), (2, 4)))
def test_seed_varies_the_assignment_but_never_the_total(n_blocks, k, build_from_world):
    """SPEC §5.4: the published seed chooses *among* exact ties, and only that.

    `synth.tie_heavy` builds n_blocks disjoint blocks of K people over K desks,
    so every feasible assignment scores the identical total and there are
    exactly (K!)**n_blocks of them. That makes this the one fixture where the
    tie-break is the only thing deciding the output.

    Two assertions, and the second is the real invariant:
      * more than one distinct outcome across the seeds (the seed does something);
      * one single total across every seed (the seed decides nothing that matters).
    """
    case = synth.tie_heavy(n_blocks=n_blocks, k=k)
    _config, build = build_from_world(case.world)
    problem = build.problem

    assert problem.n_people == n_blocks * k

    # The scenario's own claim, confirmed exhaustively rather than asserted.
    best, n_optima, n_feasible = synth.verify_tie_heavy(case)
    assert n_optima == math.factorial(k) ** n_blocks
    assert n_feasible == n_optima, "some feasible assignment is not optimal; not tie-heavy"

    n_seeds = 40
    signatures = set()
    totals = set()
    for index in range(n_seeds):
        solution = solve_mod.solve(problem, f"{SEED}/tie-break-{index}")
        assert_solution_invariants(problem, solution)
        signatures.add(solution_signature(solution))
        totals.add(solution.total_points_scaled)

    assert len(totals) == 1, (
        f"the tie-break changed the optimal total across seeds: {sorted(totals)}. "
        f"That is invariant I6 failing, and the result must not be used."
    )
    assert totals == {best}, f"solver total {totals} != exhaustive optimum {best}"
    assert len(signatures) > 1, (
        f"{n_seeds} different seeds produced one single outcome on an instance with "
        f"{n_optima} tied optima; the tie-break is not doing anything"
    )
    assert len(signatures) <= n_optima


@pytest.mark.parametrize(("n_people", "k"), ((6, 4), (8, 5), (5, 5)))
def test_unique_optimum_is_seed_invariant(n_people, k, build_from_world):
    """The complement of the tie-heavy test.

    `synth.exact_fit` has exactly one optimal assignment, so the seed must make
    no difference at all. A seed that changed the answer here would mean jitter
    was outranking a genuine preference difference.
    """
    case = synth.exact_fit(n_people=n_people, k=k)
    config, build = build_from_world(case.world)
    problem = build.problem
    assert problem.n_people == problem.n_desks == n_people, "exact_fit must be square"

    curve = config.scoring.curve()
    int_curve, _scale = scoring.integerise(curve)

    reference = solve_mod.solve(problem, SEED)
    assert_solution_invariants(problem, reference)
    assert reference.rank_histogram() == (n_people,) + (0,) * (k - 1)
    assert reference.total_points_scaled == n_people * int_curve[0]

    for index in range(12):
        other = solve_mod.solve(problem, f"a-completely-different-seed-{index}")
        assert solution_signature(other) == solution_signature(reference), (
            "the seed changed an assignment that has a unique optimum"
        )


# ==========================================================================
# 4. The jitter bound (invariant I6)
# ==========================================================================


def test_assert_jitter_bound_rejects_a_non_integer_matrix():
    """The proof needs integer points; a float matrix has no minimum gap of 1."""
    points = np.array([[5.0, 4.0], [3.0, 2.0]], dtype=np.float64)
    with pytest.raises(DeterminismError, match="integral"):
        scoring.assert_jitter_bound(points, 2, scoring.jitter_epsilon(2))

    # ... and the integer version of the same matrix is accepted.
    scoring.assert_jitter_bound(points.astype(np.int64), 2, scoring.jitter_epsilon(2))


@pytest.mark.parametrize("n", (1, 2, 3, 10, 35, 1000))
@pytest.mark.parametrize("factor", (1.0, 1.5, 4.0, 100.0))
def test_assert_jitter_bound_rejects_too_large_an_epsilon(n, factor):
    """Anything with n*eps >= 1 must be refused, including the exact boundary."""
    points = np.full((3, 4), 5, dtype=np.int64)
    epsilon = factor / n
    assert not (n * epsilon < 1.0), "this test's own premise is wrong"
    with pytest.raises(DeterminismError, match="jitter bound violated"):
        scoring.assert_jitter_bound(points, n, epsilon)


@pytest.mark.parametrize("n", (0, 1, 2, 5, 35, 1000, 100_000, 10_000_000))
def test_jitter_epsilon_satisfies_the_bound_at_every_n(n):
    """The inequality the whole tie-break rests on, checked as arithmetic.

    SPEC §5.4 uses eps = 1/(2*(n+1)), so n*eps = n/(2n+2) < 1/2 -- a factor of
    two inside the requirement even before floating-point slop.
    """
    epsilon = scoring.jitter_epsilon(n)
    assert epsilon > 0.0
    assert n * epsilon < 1.0, f"n*eps = {n * epsilon} at n={n}"
    assert n * epsilon < 0.5, "the documented factor-of-two margin is gone"
    assert epsilon == 1.0 / (2 * (n + 1))

    points = np.full((min(n, 8) or 1, 4), 5, dtype=np.int64)
    scoring.assert_jitter_bound(points, n, epsilon)   # must not raise


def test_jitter_epsilon_rejects_a_negative_population():
    with pytest.raises(ValueError):
        scoring.jitter_epsilon(-1)


def test_assert_jitter_bound_rejects_a_matrix_too_large_for_float64():
    """The second half of the runtime guard: 1 ulp must stay below epsilon."""
    n = 1000
    points = np.full((4, 4), 10**15, dtype=np.int64)
    with pytest.raises(DeterminismError, match="float64"):
        scoring.assert_jitter_bound(points, n, scoring.jitter_epsilon(n))


@pytest.mark.parametrize("n", (1, 4, 16, 64))
def test_jitter_matrix_stays_inside_zero_to_epsilon(n):
    """The draw the bound assumes: independent Uniform[0, eps) per cell."""
    epsilon = scoring.jitter_epsilon(n)
    jitter = scoring.jitter_matrix(scoring.make_rng(SEED), (n, n + 2), epsilon)
    assert jitter.shape == (n, n + 2)
    assert np.all(jitter >= 0.0)
    assert np.all(jitter < epsilon)
    # Total jitter on any assignment picks n cells, so it is bounded by n*eps < 1.
    assert float(np.sort(jitter, axis=None)[-n:].sum()) < 1.0
    # Same seed, same draw.
    again = scoring.jitter_matrix(scoring.make_rng(SEED), (n, n + 2), epsilon)
    assert np.array_equal(jitter, again)


@pytest.mark.parametrize(("n_people", "n_desks", "k"), SHAPES[1:], ids=SHAPE_IDS[1:])
def test_jitter_never_changes_the_optimum(n_people, n_desks, k):
    """A jittered solve and a jitter-free exhaustive optimum must score the same.

    `solve()` asserts this internally against its own jitter-free reference
    solve; this checks it against brute force, which shares no code with it.
    """
    checked = 0
    for trial in range(6):
        problem = random_problem(
            "jitter-neutral", trial, n_people=n_people, n_desks=n_desks, k=k,
            curve_kind=CURVE_KINDS[trial % len(CURVE_KINDS)],
        )
        best = solve_mod.brute_force_optimum(problem)
        if best is None:
            continue
        for seed_index in range(4):
            solution = solve_mod.solve(problem, f"{SEED}/jitter-{seed_index}")
            assert solution.total_points_scaled == best
            assert_solution_invariants(problem, solution)
        checked += 1
    if n_people <= n_desks:
        assert checked > 0, "every instance was infeasible; the jitter was never exercised"


# ==========================================================================
# 5. Exact integerisation (SPEC §5.3)
# ==========================================================================


@pytest.mark.parametrize("kind", CURVE_KINDS)
@pytest.mark.parametrize("k", (1, 2, 3, 5, 8))
def test_integerise_is_exact_at_every_k(kind, k):
    curve = make_curve(k, kind)
    points, scale = scoring.integerise(curve)

    assert scale == expected_scale(k, kind)
    assert len(points) == k
    for value, scaled in zip(curve, points):
        assert isinstance(scaled, int)
        assert Fraction(scaled) == value * scale, "integerise() is not exact"
    assert all(a > b for a, b in zip(points, points[1:])), "curve stopped decreasing"
    assert all(p > 0 for p in points), "SPEC §2.4 requires strictly positive values"


def test_integerise_decimal_curve_uses_the_lcm_of_denominators():
    """4.5 is 9/2, so the curve scales by exactly 2 -- not by 2**52."""
    curve = tuple(Fraction(str(v)) for v in (5, 4.5, 4, 3.5, 3))
    assert curve[1] == Fraction(9, 2)

    points, scale = scoring.integerise(curve)
    assert scale == 2
    assert points == (10, 9, 8, 7, 6)
    assert Fraction(points[1], scale) == Fraction(9, 2)


def test_integerise_leaves_an_integer_curve_alone():
    for curve in ((5, 4, 3, 2, 1), (16, 8, 4, 2, 1), (1,), (100, 1)):
        points, scale = scoring.integerise(tuple(Fraction(v) for v in curve))
        assert scale == 1, f"an already-integral curve was rescaled: {curve}"
        assert points == curve


def test_integerise_clears_mixed_denominators():
    points, scale = scoring.integerise((Fraction(1, 2), Fraction(1, 3)))
    assert (points, scale) == ((3, 2), 6)


def test_integerise_rejects_an_empty_curve():
    with pytest.raises(ValueError):
        scoring.integerise(())


def test_real_config_decimal_curve_is_read_as_a_decimal(real_config):
    """The loader must read `4.5` as 9/2, not as the nearest binary float.

    Reading it as a float would give a denominator of 2**52, an astronomically
    large scale, and a score matrix that trips the float64 guard.
    """
    concave = real_config.scoring.curve("concave")
    assert concave[1] == Fraction(9, 2)
    points, scale = scoring.integerise(concave)
    assert scale == 2
    assert points == (10, 9, 8, 7, 6)


@pytest.mark.parametrize("kind", CURVE_KINDS)
@pytest.mark.parametrize("k", (2, 4))
def test_scaled_points_are_integers_end_to_end(kind, k):
    """The matrix that reaches `solve()` is integral, which is what I6 needs."""
    problem = feasible_random_problem(
        "integral-matrix", kind, n_people=4, n_desks=8, k=k, curve_kind=kind,
    )
    assert np.issubdtype(problem.points.dtype, np.integer)
    assert problem.scale == expected_scale(k, kind)
    scoring.assert_jitter_bound(
        problem.points, problem.n_people, scoring.jitter_epsilon(problem.n_people)
    )
    solution = solve_mod.solve(problem, SEED)
    assert_solution_invariants(problem, solution)
    assert solution.total_points == Fraction(solution.total_points_scaled, problem.scale)


# ==========================================================================
# 6. Forbidden cells
# ==========================================================================


def _greedy_temptation(n_people: int, curve_kind: str = "linear"):
    """An instance whose *unmasked* optimum uniquely uses a forbidden cell.

    Desk D01 sits in a zone person 0 may not use. Person 0 ranks it first
    anyway (a stale form, a config change, a bug upstream -- SPEC §3.4 says the
    choice is dropped, not honoured). Everyone else ranks their own desk first
    and D01 second.

    Ignoring the mask scores n * curve[0]; obeying it costs two people a rank.
    So a solver that quietly used the forbidden cell would score strictly
    better, which is exactly what makes this a test rather than a formality.
    """
    zones = ["vip"] + ["open"] * (n_people - 1)
    prefs = [[0, 1]] + [[i, 0] for i in range(1, n_people)]
    masked = build_problem_from_prefs(
        prefs, desks=n_people, k=2, curve_kind=curve_kind,
        zone_of_desk=zones, allowed_zones=[("open",)] + ["*"] * (n_people - 1),
    )
    unmasked = build_problem_from_prefs(
        prefs, desks=n_people, k=2, curve_kind=curve_kind,
    )
    return masked, unmasked


@pytest.mark.parametrize("n_people", (2, 3, 5, 6))
@pytest.mark.parametrize("curve_kind", ("linear", "convex", "halves"))
def test_solver_refuses_a_forbidden_cell_a_greedy_solver_would_take(n_people, curve_kind):
    masked, unmasked = _greedy_temptation(n_people, curve_kind)

    # The temptation is real: unmasked, the optimum hands person 0 desk D01.
    unmasked_best = solve_mod.brute_force_optimum(unmasked)
    unmasked_solution = solve_mod.solve(unmasked, SEED)
    assert unmasked_solution.total_points_scaled == unmasked_best
    assert unmasked_solution.desk_of(unmasked.people[0]) == "D01", (
        "the fixture is wrong: the unmasked optimum does not use the cell that "
        "eligibility forbids, so nothing is being tested"
    )

    masked_best = solve_mod.brute_force_optimum(masked)
    assert masked_best is not None and masked_best < unmasked_best, (
        "obeying the mask must cost something here, or the test is vacuous"
    )

    solution = solve_mod.solve(masked, SEED)
    assert_solution_invariants(masked, solution)
    assert solution.total_points_scaled == masked_best
    assert solution.desk_of(masked.people[0]) != "D01", (
        "the solver assigned a desk in a zone the person may not sit in"
    )


@pytest.mark.parametrize("n_people", (2, 3, 6))
def test_masking_into_infeasibility_raises_rather_than_cheating(n_people):
    """When the mask makes it impossible, the run fails -- it never relaxes I5.

    n people are confined to a zone holding n-1 desks. There are plenty of
    desks overall; only eligibility makes it fail, and it must fail loudly.
    """
    n_desks = n_people + 3
    tight = n_people - 1
    k = min(tight, 4)
    zones = ["cohort"] * tight + ["other"] * (n_desks - tight)
    # Rotate the rankings so the cohort collectively reaches every cohort desk:
    # the deficiency must come from the zone being too small, not from the group
    # having failed to name the desks it is allowed.
    prefs = [[(i + offset) % tight for offset in range(k)] for i in range(n_people)]

    problem = build_problem_from_prefs(
        prefs, desks=n_desks, k=k,
        zone_of_desk=zones, allowed_zones=[("cohort",)] * n_people,
    )
    assert solve_mod.brute_force_optimum(problem) is None

    with pytest.raises(InfeasibleError) as excinfo:
        solve_mod.solve(problem, SEED)

    diagnosis = excinfo.value.diagnosis
    assert diagnosis.max_satisfiable == tight
    assert diagnosis.deficiency == n_people - tight
    assert diagnosis.blocking_sets, "an infeasible run must name who is blocking whom"


@pytest.mark.parametrize(("n_people", "n_desks", "k"), SHAPES[1:], ids=SHAPE_IDS[1:])
def test_no_forbidden_pairing_in_any_random_zoned_solve(n_people, n_desks, k):
    """The blanket statement: across many masked instances, never a masked cell."""
    if n_people > n_desks:
        pytest.skip("over-subscribed: there is no successful solve to inspect")
    checked = 0
    for trial in range(12):
        problem = random_problem(
            "no-forbidden", trial, n_people=n_people, n_desks=n_desks, k=k,
            n_zones=min(3, n_desks), restricted_frac=0.75,
        )
        try:
            solution = solve_mod.solve(problem, f"{SEED}/{trial}")
        except InfeasibleError:
            continue
        row_of = {e: i for i, e in enumerate(problem.people)}
        col_of = {d: j for j, d in enumerate(problem.desks)}
        for a in solution.assignments:
            assert problem.allowed[row_of[a.email], col_of[a.desk_id]]
            assert problem.eligible[row_of[a.email], col_of[a.desk_id]]
        assert_solution_invariants(problem, solution)
        checked += 1
    if k > 1:
        # At K=1 a single zone-blocked choice leaves the person with nothing, so
        # a whole sweep can legitimately be infeasible; above K=1 it cannot.
        assert checked > 0, "every instance in this sweep was infeasible; nothing checked"


# ==========================================================================
# 7. Backends
# ==========================================================================


def _pulp_backend_or_skip():
    pytest.importorskip("pulp", reason="the ILP escape hatch is an optional extra")
    import pulp

    if not pulp.PULP_CBC_CMD(msg=False).available():
        pytest.skip("pulp is installed but the CBC solver binary is not available")
    return solve_mod.PuLPBackend()


@pytest.mark.parametrize(("n_people", "n_desks", "k"), ((3, 5, 3), (5, 6, 4), (6, 6, 3)))
def test_pulp_and_scipy_backends_reach_the_same_total(n_people, n_desks, k):
    """The documented escape hatch must actually be equivalent, not merely present.

    Assignments may differ -- both backends are free to pick a different member
    of a tie -- so the TOTAL is what is compared.
    """
    pulp_backend = _pulp_backend_or_skip()
    for trial in range(5):
        problem = feasible_random_problem(
            "backend-parity", trial, n_people=n_people, n_desks=n_desks, k=k,
        )
        jv = solve_mod.solve(problem, SEED, solve_mod.ScipyJVBackend())
        ilp = solve_mod.solve(problem, SEED, pulp_backend)

        assert jv.total_points_scaled == ilp.total_points_scaled
        assert jv.total_points_scaled == solve_mod.brute_force_optimum(problem)
        assert jv.backend == "scipy-jv" and ilp.backend == "pulp-cbc"
        assert_solution_invariants(problem, jv)
        assert_solution_invariants(problem, ilp)


def test_backend_registry_and_lookup():
    assert set(solve_mod.BACKENDS) == {"scipy-jv", "pulp-cbc"}
    assert solve_mod.get_backend(None).name == "scipy-jv"
    assert solve_mod.get_backend("scipy-jv").name == "scipy-jv"
    with pytest.raises(ValueError, match="unknown solver backend"):
        solve_mod.get_backend("does-not-exist")


# ==========================================================================
# 8. Degenerate and boundary sizes (invariant I8)
# ==========================================================================


@pytest.mark.parametrize("n_desks", (0, 1, 7))
@pytest.mark.parametrize("k", (1, 3, 5))
def test_zero_people(make_problem, invariants, n_desks, k):
    """No people at all: succeed with nothing assigned, never divide by N."""
    problem = make_problem([], desks=n_desks, k=k)
    assert problem.n_people == 0
    assert problem.n_desks == n_desks

    assert solve_mod.brute_force_optimum(problem) == 0
    solution = solve_mod.solve(problem, SEED)
    invariants.all(problem, solution)

    assert solution.assignments == ()
    assert solution.total_points_scaled == 0
    assert solution.rank_histogram() == (0,) * k
    assert set(solution.free_desks) == set(problem.desks)


@pytest.mark.parametrize("k", (1, 2, 5))
@pytest.mark.parametrize("n_desks", (1, 2, 9))
def test_single_person_gets_their_first_choice(make_problem, k, n_desks):
    """N=1 breaks anything that assumes a population."""
    if n_desks < 1:
        pytest.skip("need at least one desk")
    prefs = [list(range(min(k, n_desks)))]
    problem = make_problem(prefs, desks=n_desks, k=k)
    int_curve, _scale = scoring.integerise(make_curve(k, "linear"))

    solution = solve_mod.solve(problem, SEED)
    assert_solution_invariants(problem, solution)
    assert solution.total_points_scaled == int_curve[0]
    assert solution.rank_histogram() == (1,) + (0,) * (k - 1)
    assert solution.assignments[0].desk_id == problem.desks[0]
    # The bound at N=1 is eps = 1/(2*2); it still has to hold.
    scoring.assert_jitter_bound(problem.points, 1, scoring.jitter_epsilon(1))


@pytest.mark.parametrize("k", (2, 3, 5))
def test_single_person_via_the_synth_scenario(build_from_world, k):
    case = synth.single_person(k=k)
    config, build = build_from_world(case.world)
    problem = build.problem
    assert problem.n_people == 1
    solution = solve_mod.solve(problem, config.scoring.tie_break_seed)
    assert_solution_invariants(problem, solution)
    assert solution.rank_histogram() == (1,) + (0,) * (k - 1)


@pytest.mark.parametrize("n", (1, 2, 5, 8))
def test_exact_fit_leaves_no_desk_free(build_from_world, n):
    """n_people == n_desks. Every desk is taken and nobody is left over."""
    k = min(n, 4) or 1
    case = synth.exact_fit(n_people=max(n, k), k=k)
    config, build = build_from_world(case.world)
    problem = build.problem
    assert problem.n_people == problem.n_desks

    solution = solve_mod.solve(problem, config.scoring.tie_break_seed)
    assert_solution_invariants(problem, solution)
    assert solution.free_desks == ()
    assert {a.desk_id for a in solution.assignments} == set(problem.desks)


@pytest.mark.parametrize(("n_people", "n_desks"), ((2, 1), (9, 6), (14, 8)))
def test_more_people_than_desks_raises_and_returns_nothing(n_people, n_desks):
    """Invariant I7: over-subscription fails; it never returns a partial answer."""
    k = min(3, n_desks)
    problem = random_problem(
        "over-subscribed", n_people=n_people, n_desks=n_desks, k=k,
    )
    assert problem.n_people > problem.n_desks
    assert solve_mod.brute_force_optimum(problem) is None

    with pytest.raises(InfeasibleError) as excinfo:
        solve_mod.solve(problem, SEED)

    diagnosis = excinfo.value.diagnosis
    assert diagnosis.n_people == n_people
    assert diagnosis.deficiency >= n_people - n_desks, "pigeonhole says at least this many"
    assert diagnosis.max_satisfiable <= n_desks
    assert "INFEASIBLE" in diagnosis.summary()


@pytest.mark.parametrize(("n_people", "n_desks"), ((14, 8), (7, 5)))
def test_more_people_than_desks_via_the_synth_scenario(build_from_world, n_people, n_desks):
    case = synth.more_people_than_desks(n_people=n_people, n_desks=n_desks, k=3)
    config, build = build_from_world(case.world)
    problem = build.problem
    with pytest.raises(InfeasibleError) as excinfo:
        solve_mod.solve(problem, config.scoring.tie_break_seed)
    assert excinfo.value.diagnosis.deficiency >= problem.n_people - problem.n_desks
    assert excinfo.value.exit_code == 2, "SPEC §9: infeasible is exit code 2"


@pytest.mark.parametrize("n_people", (2, 4, 7))
def test_everyone_ranking_the_same_desks_is_infeasible_not_approximated(
    build_from_world, n_people
):
    """The README's worst case: correlated preferences, plenty of furniture."""
    k = 3
    case = synth.everyone_ranks_same_k_desks(n_people=n_people, k=k, n_desks=n_people + 4)
    config, build = build_from_world(case.world)
    problem = build.problem
    assert problem.n_desks > k, "the point is a preference collision, not a desk shortage"

    if n_people <= k:
        solution = solve_mod.solve(problem, config.scoring.tie_break_seed)
        assert_solution_invariants(problem, solution)
        return

    with pytest.raises(InfeasibleError) as excinfo:
        solve_mod.solve(problem, config.scoring.tie_break_seed)
    diagnosis = excinfo.value.diagnosis
    assert diagnosis.max_satisfiable == k
    assert diagnosis.deficiency == n_people - k


# ==========================================================================
# 9. matching.py
# ==========================================================================


def _random_bipartite(*seed_parts, n_left: int, n_right: int, density: float) -> np.ndarray:
    return stable_rng(*seed_parts, n_left, n_right, density).random((n_left, n_right)) < density


MATCHING_SHAPES: tuple[tuple[int, int], ...] = (
    (1, 1), (2, 3), (3, 2), (4, 4), (5, 3), (6, 8), (8, 6), (10, 10), (12, 7),
)


@pytest.mark.parametrize(("n_left", "n_right"), MATCHING_SHAPES)
def test_hopcroft_karp_matches_scipy_cardinality(n_left, n_right):
    """The package's Hopcroft-Karp against scipy's, on random graphs.

    Cardinality is the invariant; the matchings themselves may legitimately
    differ, since a maximum matching is not unique. The returned matching is
    separately checked to be a real, injective matching over existing edges.
    """
    csgraph = pytest.importorskip("scipy.sparse.csgraph")
    from scipy.sparse import csr_matrix

    sizes_seen = set()
    for trial in range(40):
        density = 0.05 + 0.9 * (trial % 10) / 10.0
        allowed = _random_bipartite("hk-vs-scipy", trial, n_left=n_left, n_right=n_right,
                                    density=density)
        adj = matching.adjacency(allowed)
        match_left, match_right = matching.hopcroft_karp(adj, n_right)

        ours = int((match_left >= 0).sum())
        theirs = int(
            (csgraph.maximum_bipartite_matching(
                csr_matrix(allowed.astype(np.int8)), perm_type="column"
            ) >= 0).sum()
        )
        assert ours == theirs, (
            f"Hopcroft-Karp found {ours}, scipy found {theirs} on "
            f"{allowed.astype(int).tolist()}"
        )
        sizes_seen.add(ours)

        # It must also *be* a matching: real edges, and injective both ways.
        for u, v in enumerate(match_left.tolist()):
            if v >= 0:
                assert allowed[u, v], "matched a non-edge"
                assert int(match_right[v]) == u, "match_left and match_right disagree"
        for v, u in enumerate(match_right.tolist()):
            if u >= 0:
                assert allowed[u, v]
                assert int(match_left[u]) == v
        assert len({v for v in match_left.tolist() if v >= 0}) == ours, "a desk was reused"

    assert len(sizes_seen) > 1, "every graph in this sweep had the same matching size"


@pytest.mark.parametrize(("n_left", "n_right"), MATCHING_SHAPES)
def test_has_perfect_left_matching_agrees_with_the_matching_size(n_left, n_right):
    for trial in range(20):
        allowed = _random_bipartite("perfect-left", trial, n_left=n_left, n_right=n_right,
                                    density=0.1 + 0.08 * trial)
        adj = matching.adjacency(allowed)
        expected = matching.matching_size(adj, n_right) == n_left
        assert matching.has_perfect_left_matching(allowed) is expected


# ---- exhaustive reference implementations, for the small-graph tests ------


def _all_maximum_matchings(adj, n_right: int) -> list[frozenset[int]]:
    """Every maximum matching, as the frozenset of LEFT vertices it covers.

    Exhaustive by construction, so callers must keep the graph tiny. This is the
    reference `matching.unmatched_analysis` is checked against: it shares no
    code with it, which is the whole point.
    """
    n_left = len(adj)
    covered: list[frozenset[int]] = []
    best = 0

    def walk(u: int, used_right: frozenset[int], matched: frozenset[int]) -> None:
        nonlocal best
        if u == n_left:
            if len(matched) > best:
                best = len(matched)
                covered.clear()
            if len(matched) == best:
                covered.append(matched)
            return
        walk(u + 1, used_right, matched)                    # leave u unmatched
        for v in adj[u]:
            if v not in used_right:
                walk(u + 1, used_right | {v}, matched | {u})

    walk(0, frozenset(), frozenset())
    return [s for s in covered if len(s) == best]


TINY_SHAPES: tuple[tuple[int, int], ...] = ((1, 1), (2, 2), (3, 2), (3, 4), (4, 3), (5, 4))


@pytest.mark.parametrize(("n_left", "n_right"), TINY_SHAPES)
def test_unmatched_analysis_matches_exhaustive_enumeration(n_left, n_right):
    """`always_unmatched` / `sometimes_unmatched` against every maximum matching.

    These two sets drive who the coordinator has to go and talk to, so they are
    checked against ground truth rather than against a cheaper heuristic.
    """
    saw_always = saw_sometimes_only = 0

    for trial in range(60):
        density = 0.05 + 0.9 * (trial % 12) / 12.0
        allowed = _random_bipartite("unmatched", trial, n_left=n_left, n_right=n_right,
                                    density=density)
        adj = matching.adjacency(allowed)
        maximum_matchings = _all_maximum_matchings(adj, n_right)
        assert maximum_matchings

        best = len(maximum_matchings[0])
        assert best == matching.matching_size(adj, n_right)

        expected_sometimes = sorted(
            u for u in range(n_left) if any(u not in cover for cover in maximum_matchings)
        )
        expected_always = sorted(
            u for u in range(n_left) if all(u not in cover for cover in maximum_matchings)
        )

        always, sometimes = matching.unmatched_analysis(allowed)
        assert sometimes == expected_sometimes, (
            f"sometimes_unmatched wrong on {allowed.astype(int).tolist()}"
        )
        assert always == expected_always, (
            f"always_unmatched wrong on {allowed.astype(int).tolist()}"
        )
        assert set(always) <= set(sometimes), "always_unmatched must be a subset"

        saw_always += bool(always)
        saw_sometimes_only += bool(set(sometimes) - set(always))

    if n_left >= 2 and n_right >= 2:
        # A 1x1 graph cannot distinguish the two: its single vertex is either
        # always matched or never matchable, so there is no at-risk-only case.
        assert saw_always > 0, "no graph in this sweep had a structurally unseatable vertex"
        assert saw_sometimes_only > 0, "no graph in this sweep had an at-risk-only vertex"


@pytest.mark.parametrize(("n_left", "n_right"), MATCHING_SHAPES)
def test_hall_violators_report_true_violations(n_left, n_right):
    """Every reported blocking set really is over-subscribed, and N(S) is right.

    This is the half of the claim the coordinator reads out loud to students, so
    it has to be exactly true: "these |S| people can only reach these |N(S)|
    desks between them".
    """
    reported = 0
    for trial in range(40):
        density = 0.05 + 0.5 * (trial % 8) / 8.0
        allowed = _random_bipartite("hall", trial, n_left=n_left, n_right=n_right,
                                    density=density)
        adj = matching.adjacency(allowed)
        violators = matching.hall_violators(allowed, stable_rng("hall-order", trial))

        if matching.has_perfect_left_matching(allowed):
            assert violators == [], "a feasible instance reported a blocking set"
            continue

        assert violators, "an infeasible instance must name at least one blocking set"
        for lefts, neighbourhood in violators:
            reported += 1
            assert lefts == sorted(set(lefts)), "blocking sets must be sorted and distinct"
            assert neighbourhood == matching.neighbourhood(adj, lefts), (
                "the reported N(S) is not the neighbourhood of S"
            )
            assert len(neighbourhood) < len(lefts), (
                f"|N(S)|={len(neighbourhood)} is not < |S|={len(lefts)}: this set is "
                f"not a Hall violator at all"
            )
            assert matching.is_violator(adj, lefts)
    assert reported > 0, "no blocking set was produced in this sweep"


#: A hand-picked instance, smallest by (rows, columns, edges) over an exhaustive
#: search of every graph up to 5x4. People 3 and 4 both reach only desk 0, so
#: {3, 4} is the tight statement; `hall_violators` reports {1, 3, 4}.
MINIMALITY_REGRESSION = (
    (1, 0, 1),
    (0, 1, 0),
    (0, 1, 1),
    (1, 0, 0),
    (1, 0, 0),
)


def _non_minimal_violators(allowed: np.ndarray, rng) -> list[tuple[list[int], int]]:
    adj = matching.adjacency(allowed)
    out = []
    for lefts, _neighbourhood in matching.hall_violators(allowed, rng):
        for u in lefts:
            if matching.is_violator(adj, [x for x in lefts if x != u]):
                out.append((list(lefts), u))
                break
    return out


def test_hall_violators_are_minimal():
    """Removing any single member must leave a non-violator (SPEC §6.1).

    SPEC §6.1: "`S` is then **greedily minimalised** -- repeatedly try removing
    each member, keep the removal if the set is still a violator -- so the
    coordinator gets the tightest possible statement." `minimal_violator`'s own
    docstring restates it: "no proper subset of it that we can reach by single
    removals is still a violator".

    This matters because the blocking set is read out to students by name. A set
    that still contains a smaller violator is a true statement about the wrong
    group -- telling five people to re-rank when two of them are the whole
    problem, and scoping round two to the wrong cohort (diagnostics.build_round2
    takes the union of these sets verbatim).

    One test rather than a parameterised family, so a failure carries the whole
    picture: the deterministic reproducer plus the rate over a random sweep.
    """
    fixed = np.array(MINIMALITY_REGRESSION, dtype=bool)
    adj = matching.adjacency(fixed)
    assert not matching.has_perfect_left_matching(fixed)
    assert matching.is_violator(adj, [3, 4]), "the reproducer itself must contain a violator"
    deterministic = _non_minimal_violators(fixed, None)

    non_minimal: list[tuple[tuple[int, int], list[int], int]] = []
    checked = 0
    for n_left, n_right in MATCHING_SHAPES:
        for trial in range(40):
            density = 0.05 + 0.5 * (trial % 8) / 8.0
            allowed = _random_bipartite("hall", trial, n_left=n_left, n_right=n_right,
                                        density=density)
            checked += len(matching.hall_violators(allowed, None))
            for lefts, dropped in _non_minimal_violators(allowed, None):
                non_minimal.append(((n_left, n_right), lefts, dropped))

    assert checked > 0, "no blocking set was produced in the sweep"
    assert not deterministic and not non_minimal, (
        "hall_violators returned a blocking set that is not minimal.\n"
        f"  deterministic reproducer {[list(r) for r in MINIMALITY_REGRESSION]}:\n"
        + "".join(
            f"      returned {lefts}, but dropping {u} leaves "
            f"{[x for x in lefts if x != u]}, still a Hall violator\n"
            for lefts, u in deterministic
        )
        + f"  random sweep: {len(non_minimal)} of {checked} reported set(s) "
        f"are not minimal"
        + (f"; first at shape {non_minimal[0][0]}: {non_minimal[0][1]} "
           f"minus {non_minimal[0][2]}" if non_minimal else "")
    )


@pytest.mark.parametrize(("n_left", "n_right"), MATCHING_SHAPES)
def test_minimal_violator_returns_a_violator_or_nothing(n_left, n_right):
    """`minimal_violator` never turns a violator into a non-violator."""
    for trial in range(25):
        allowed = _random_bipartite("minimal", trial, n_left=n_left, n_right=n_right,
                                    density=0.05 + 0.4 * (trial % 6) / 6.0)
        adj = matching.adjacency(allowed)
        everyone = list(range(n_left))
        result = matching.minimal_violator(adj, everyone, stable_rng("minimal-order", trial))
        if matching.is_violator(adj, everyone):
            assert result, "a violator was minimalised away to nothing"
            assert matching.is_violator(adj, result)
            assert set(result) <= set(everyone)
            assert result == sorted(result)
        else:
            assert result == []


@pytest.mark.parametrize(("n_people", "n_desks", "k"), SHAPES[1:], ids=SHAPE_IDS[1:])
def test_min_feasible_k_is_the_minimum_and_monotone(n_people, n_desks, k):
    """Feasibility only ever improves with K, and `min_feasible_k` finds the first.

    Checked three ways: the answer is feasible, every smaller K' is not, and
    every larger K' still is. The last one is the monotonicity that lets the
    implementation stop at the first success instead of sweeping the whole range.
    """
    found = missing = 0

    for trial in range(10):
        problem = random_problem(
            "kmin", trial, n_people=n_people, n_desks=n_desks, k=k,
            n_zones=min(2, n_desks), restricted_frac=0.4,
        )
        rank = problem.rank.astype(np.int16)
        eligible = problem.eligible

        def feasible_at(k_try: int) -> bool:
            return matching.has_perfect_left_matching(
                eligible & (rank >= 1) & (rank <= k_try)
            )

        k_min = matching.min_feasible_k(rank, eligible, k)

        if k_min is None:
            missing += 1
            assert not any(feasible_at(k_try) for k_try in range(1, k + 1))
            # Not feasible at K, and no rank exceeds K, so widening cannot help.
            assert matching.min_feasible_k(rank, eligible, k + 5) is None
            continue

        found += 1
        assert 1 <= k_min <= k
        assert feasible_at(k_min), "the reported K_min is not actually feasible"
        for k_try in range(1, k_min):
            assert not feasible_at(k_try), f"K={k_try} < K_min={k_min} is feasible"
        for k_try in range(k_min, k + 1):
            assert feasible_at(k_try), f"feasibility is not monotone in K at K={k_try}"
        assert matching.min_feasible_k(rank, eligible, k + 5) == k_min, (
            "widening the ceiling changed the minimum"
        )
        assert matching.min_feasible_k(rank, eligible, k_min) == k_min

    assert found + missing == 10


def test_min_feasible_k_on_a_hand_built_ladder():
    """A case where the answer is obvious by inspection, at several sizes."""
    for n in (1, 2, 3, 5):
        # Person i ranks desk 0 first and desk i second: everybody's first choice
        # collides, so K=1 fails for n > 1 and K=2 succeeds.
        rank = np.full((n, n), -1, dtype=np.int16)
        for i in range(n):
            rank[i, 0] = 1
            rank[i, i] = 2
        rank[0, 0] = 1
        eligible = np.ones((n, n), dtype=bool)

        expected = 1 if n == 1 else 2
        assert matching.min_feasible_k(rank, eligible, n) == expected
        assert matching.min_feasible_k(rank, eligible, 1) == (1 if n == 1 else None)


def test_adjacency_is_sorted_and_rejects_non_2d():
    allowed = np.array([[1, 0, 1], [1, 1, 0]], dtype=bool)
    assert matching.adjacency(allowed) == [[0, 2], [0, 1]]
    with pytest.raises(ValueError, match="2-D"):
        matching.adjacency(np.ones(4, dtype=bool))


@pytest.mark.parametrize(("n_left", "n_right"), TINY_SHAPES)
def test_alternating_reachable_yields_a_hall_violator(n_left, n_right):
    """Konig's theorem, as matching.py's docstring states it.

    Whenever the matching is not left-perfect, the left vertices reachable by
    alternating paths from the unmatched ones form a Hall violator. Everything
    downstream -- the blocking sets, the round-2 scoping -- rests on this.
    """
    exercised = 0
    for trial in range(40):
        allowed = _random_bipartite("konig", trial, n_left=n_left, n_right=n_right,
                                    density=0.05 + 0.5 * (trial % 8) / 8.0)
        adj = matching.adjacency(allowed)
        match_left, match_right = matching.hopcroft_karp(adj, n_right)
        if int((match_left >= 0).sum()) == n_left:
            continue
        reachable_left, reachable_right = matching.alternating_reachable(
            adj, match_left, match_right
        )
        assert reachable_left == sorted(reachable_left)
        assert reachable_right == sorted(reachable_right)
        assert matching.neighbourhood(adj, reachable_left) == reachable_right
        assert matching.is_violator(adj, reachable_left), (
            "the alternating-reachable set is not a Hall violator, so Konig's "
            "theorem is being applied wrongly"
        )
        exercised += 1
    assert exercised > 0, "no infeasible graph in this sweep"
