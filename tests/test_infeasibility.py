"""Infeasibility: the diagnosis, the round-2 scoping, and the pre-deadline check.

When the K-floor cannot be met the run fails (invariant I7) and everything the
coordinator gets instead is produced by `deskmatch.diagnostics`. That output is
not decoration -- it is the thing they act on, by walking up to named people and
asking them to re-rank -- so it is tested to the same standard as the assignment:

  * **Known-answer tests.** `blocked_world` builds instances whose Hall
    violators, deficiency and maximum matching are known *by construction*, so
    "diagnose finds exactly it" is a real claim and not a re-statement of
    whatever the code happened to return.
  * **Independent oracles.** Maximum matching size is cross-checked against
    scipy, and `always/sometimes_unmatched` and `k_min` against exhaustive
    enumeration. A bug would have to be present in both to pass.
  * **Re-derivation.** `k_min_extended` is hypothetical, so the test rebuilds
    the extension from the same seed and checks the instance really is feasible
    there and really is not one rank lower.

Sizes are parameters throughout; nothing here knows K is 5 (invariant I1).
"""

from __future__ import annotations

import itertools
import json

import numpy as np
import pytest

from conftest import stable_rng
from deskmatch import cli, diagnostics, matching, problem as problem_mod
from deskmatch import responses as responses_mod
from deskmatch import scoring
from deskmatch import solve as solve_mod
from deskmatch import synth
from deskmatch.config import load_config
from deskmatch.errors import InfeasibleError
from deskmatch.types import Infeasibility, Responses, Solution

K_VALUES: tuple[int, ...] = (2, 3, 5)
SEED = "diagnostics-test-seed"


# ==========================================================================
# Instances whose Hall violators are known before the code runs
# ==========================================================================


class BlockedCase:
    """A world whose blocking sets, deficiency and matching size are known.

    Construction: `n_blocks` independent groups of exactly ``K + 1`` people who
    all submit the *same* K desks, plus `n_free` people who each submit their
    own private K desks that nobody else names.

    Why the blocking sets are known: within a group, any K of the K+1 people fit
    (there are K desks), and all K+1 do not. So the group is a Hall violator and
    every proper subset of it is not -- i.e. it is the *unique minimal* violator
    inside that group, which is exactly what `minimal_violator` must return.
    Free people are matched in every maximum matching and can never appear in a
    violator, because no one else can reach their desks.

        n_people = n_blocks * (K + 1) + n_free + n_keepers
        n_desks  = (n_blocks + n_free) * K + spare        (spare >= keepers + blocked)
        deficiency = n_blocks
    """

    def __init__(
        self,
        *,
        k: int,
        n_blocks: int = 1,
        n_free: int = 1,
        n_keepers: int = 0,
        n_unavailable: int = 0,
        spare: int = 0,
        seed: str = "blocked",
    ) -> None:
        assert k >= 1 and n_blocks >= 1 and n_free >= 0
        assert spare >= n_keepers + n_unavailable, "spare must cover keepers/blocked desks"

        self.k = k
        self.n_blocks = n_blocks
        self.n_free = n_free
        n_block_people = n_blocks * (k + 1)
        n_people = n_block_people + n_free + n_keepers
        n_desks = (n_blocks + n_free) * k + spare

        desk_ids = tuple(f"D{i + 1:0{max(2, len(str(n_desks)))}d}" for i in range(n_desks))
        # Keepers and unavailable desks come off the END, past anything anyone
        # ranks, so they can only ever shrink the pool.
        keeper_ids = desk_ids[n_desks - n_keepers:] if n_keepers else ()
        blocked_ids = (
            desk_ids[n_desks - n_keepers - n_unavailable: n_desks - n_keepers]
            if n_unavailable
            else ()
        )
        rooms = synth.make_rooms(n_desks, n_zones=1, unavailable=blocked_ids)

        keeper_of_index = {
            n_people - n_keepers + offset: desk for offset, desk in enumerate(keeper_ids)
        }
        roster_rows = [
            {
                "name": f"Blocked Person {i:03d}",
                "email": f"blocked{i:03d}@umich.edu",
                "year": str(1 + (i % 5)),
                "candidacy": "candidate",
                "keeps_desk": "yes" if i in keeper_of_index else "no",
                "current_desk": keeper_of_index.get(i, ""),
            }
            for i in range(n_people)
        ]

        def choices(index, row, eligible, full_order):
            # `eligible` is what the generator has already established is in the
            # pool and permitted for this person, so slicing it means the
            # construction survives keepers and unavailable desks without the
            # test having to predict which ids they landed on.
            pool = sorted(eligible)
            if index < n_block_people:
                start = (index // (k + 1)) * k
            else:
                offset = index - n_block_people
                if offset >= n_free:
                    return None  # a keeper: no submission is generated for them
                start = (n_blocks + offset) * k
            picked = tuple(pool[start: start + k])
            assert len(picked) == k, "not enough pool desks for this structure"
            return picked

        self.world = synth.generate(
            rooms=rooms,
            roster_rows=roster_rows,
            k=k,
            seed=seed,
            concentration=0.0,
            eligibility_style="flat",
            choices_fn=choices,
        )

        latest = self.world.latest_choices()
        blocks: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        for block in range(n_blocks):
            emails = tuple(
                sorted(
                    roster_rows[i]["email"]
                    for i in range(block * (k + 1), (block + 1) * (k + 1))
                )
            )
            submitted = {tuple(sorted(latest[email])) for email in emails}
            # The fixture's own premise. Asserted, so a generator change makes
            # this fixture fail loudly instead of quietly going vacuous.
            assert len(submitted) == 1, "block members did not submit identical desks"
            desks = submitted.pop()
            assert len(desks) == k
            blocks.append((emails, desks))

        ranked = [d for _people, desks in blocks for d in desks]
        assert len(set(ranked)) == len(ranked), "blocks must not share desks"

        self.blocks = tuple(blocks)
        self.free_people = tuple(
            sorted(
                roster_rows[i]["email"]
                for i in range(n_block_people, n_block_people + n_free)
            )
        )
        self.keeper_desks = {
            roster_rows[i]["email"]: desk for i, desk in keeper_of_index.items()
        }
        self.unavailable_desks = tuple(blocked_ids)

    @property
    def deficiency(self) -> int:
        return self.n_blocks

    @property
    def expected_blocking_sets(self) -> set[tuple[tuple[str, ...], tuple[str, ...]]]:
        return set(self.blocks)

    @property
    def blocked_people(self) -> frozenset[str]:
        return frozenset(e for people, _desks in self.blocks for e in people)


def shared_prefix_world(*, n_people: int, k: int, shared: int, spare: int = 2, seed="prefix"):
    """Everyone shares their first `shared` choices, then diverges.

    Feasible at K' = shared + 1 and at nothing smaller (when n_people > shared),
    because below that the entire roster reaches only `shared` desks. That makes
    the true `k_min_submitted` known in advance *and* different from 1, which is
    the only way to distinguish a working K sweep from one that always says 1.
    """
    assert 1 <= shared < k
    n_desks = shared + n_people * (k - shared) + spare

    def choices(index, row, eligible, full_order):
        pool = sorted(eligible)
        start = shared + index * (k - shared)
        picked = tuple(pool[:shared]) + tuple(pool[start: start + (k - shared)])
        assert len(set(picked)) == k, "the divergent tails must not overlap"
        return picked

    return synth.generate(
        n_people,
        n_desks=n_desks,
        k=k,
        seed=seed,
        concentration=0.0,
        n_zones=1,
        eligibility_style="flat",
        keeper_frac=0.0,
        choices_fn=choices,
    )


def build(world, world_config_dir):
    """world -> (config, BuildReport) through the real loaders."""
    config_dir, responses_path = world_config_dir(world)
    config = load_config(config_dir)
    loaded = responses_mod.load_responses(str(responses_path), config.k)
    return config, problem_mod.build_problem(config, loaded)


def diagnose_infeasible(problem, seed: str = SEED) -> Infeasibility:
    """Solve, require failure, and return the diagnosis the failure carried.

    Going through `solve()` rather than calling `diagnose()` directly is the
    point: SPEC §5.2 says feasibility is decided *before* any cost minimisation,
    and I7 says the run fails rather than degrading.
    """
    with pytest.raises(InfeasibleError) as excinfo:
        result = solve_mod.solve(problem, seed)
        pytest.fail(f"solve() returned {result!r} on an infeasible instance")
    assert excinfo.value.exit_code == 2, "infeasible is exit code 2 (SPEC §9)"
    return excinfo.value.diagnosis


# --------------------------------------------------------------------------
# Independent oracles
# --------------------------------------------------------------------------


def scipy_matching_size(allowed: np.ndarray) -> int:
    """Maximum matching size according to scipy, not to deskmatch."""
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import maximum_bipartite_matching

    if allowed.shape[0] == 0 or allowed.shape[1] == 0:
        return 0
    matched = maximum_bipartite_matching(csr_matrix(allowed.astype(bool)), perm_type="column")
    return int((matched >= 0).sum())


def enumerate_max_matched_rows(allowed: np.ndarray) -> tuple[int, list[frozenset[int]]]:
    """Exhaustively: (nu, every row-set that some maximum matching covers).

    Exponential on purpose. It is the oracle the package's clever routines are
    checked against, so it must share no code with them.
    """
    n_rows, n_cols = allowed.shape
    for size in range(min(n_rows, n_cols), -1, -1):
        found: list[frozenset[int]] = []
        for rows in itertools.combinations(range(n_rows), size):
            for cols in itertools.permutations(range(n_cols), size):
                if all(allowed[r, c] for r, c in zip(rows, cols)):
                    found.append(frozenset(rows))
                    break
        if found:
            return size, found
    return 0, [frozenset()]


def is_violator(allowed: np.ndarray, rows) -> bool:
    """|N(S)| < |S|, computed from the matrix rather than from matching.py."""
    rows = list(rows)
    if not rows:
        return False
    neighbourhood = np.any(allowed[rows, :], axis=0)
    return int(neighbourhood.sum()) < len(rows)


def assert_blocking_set_is_a_minimal_violator(problem, blocking) -> None:
    """The claim `BlockingSet` makes, re-derived from the matrix.

    `people` really do reach only `desks`, and they really are more numerous.

    Minimality is only claimed for sets flagged `minimal`. The diagnosis also
    reports one deliberately NON-minimal set -- the full over-subscribed group,
    whose shortfall equals the deficiency -- because the smallest true statement
    can badly understate how many people have to re-rank. That set is checked
    for truth here and for width in
    `test_the_widest_blocking_set_carries_the_full_deficiency`.
    """
    index = {email: i for i, email in enumerate(problem.people)}
    rows = [index[email] for email in blocking.people]
    reached = sorted(
        problem.desks[j]
        for j in np.flatnonzero(np.any(problem.allowed[rows, :], axis=0)).tolist()
    )
    assert tuple(reached) == tuple(sorted(blocking.desks)), (
        "BlockingSet.desks is not N(S) for the people it names"
    )
    assert len(blocking.people) > len(blocking.desks)
    assert blocking.shortfall == len(blocking.people) - len(blocking.desks)
    if blocking.minimal:
        for dropped in rows:
            smaller = [r for r in rows if r != dropped]
            assert not is_violator(problem.allowed, smaller), (
                f"the reported set is not minimal: dropping "
                f"{problem.people[dropped]} leaves a violator"
            )
    assert blocking.names == tuple(problem.person_names[e] for e in blocking.people)
    assert blocking.desk_labels == tuple(problem.desk_labels[d] for d in blocking.desks)
    rendered = blocking.render()
    for email in blocking.people:
        assert email in rendered, "the rendered blocking set must name people"
    for desk in blocking.desks:
        assert desk in rendered, "and desks"


def subset_responses(responses: Responses, emails) -> Responses:
    keep = frozenset(emails)
    return Responses(
        submissions=tuple(s for s in responses.submissions if s.email in keep),
        latest={e: s for e, s in responses.latest.items() if e in keep},
        k=responses.k,
        source_path=responses.source_path,
        sha256=responses.sha256,
        warnings=responses.warnings,
    )


# ==========================================================================
# Blocking sets
# ==========================================================================


@pytest.mark.parametrize("k", K_VALUES)
@pytest.mark.parametrize("n_free", (0, 1, 3))
def test_diagnose_finds_exactly_the_known_hall_violator(k, n_free, world_config_dir):
    """One block of K+1 people over K desks: the answer is known in advance."""
    case = BlockedCase(k=k, n_blocks=1, n_free=n_free, seed=f"one-k{k}-f{n_free}")
    _config, report = build(case.world, world_config_dir)
    problem = report.problem

    diagnosis = diagnose_infeasible(problem)

    assert len(diagnosis.blocking_sets) == 1, diagnosis.summary()
    (blocking,) = diagnosis.blocking_sets
    expected_people, expected_desks = case.blocks[0]
    assert blocking.people == expected_people, "the wrong people were named"
    assert blocking.desks == expected_desks, "the wrong desks were named"
    assert blocking.names == tuple(problem.person_names[e] for e in expected_people)
    assert len(blocking.people) == k + 1 and len(blocking.desks) == k
    assert blocking.shortfall == 1
    assert_blocking_set_is_a_minimal_violator(problem, blocking)

    # The free people are matched in every maximum matching, so they must not
    # be named -- naming them would send the wrong students to re-rank.
    for email in case.free_people:
        assert email not in blocking.people


@pytest.mark.parametrize("k", (2, 3))
@pytest.mark.parametrize("n_blocks", (2, 3))
def test_independent_violators_are_reported_as_separate_statements(
    k, n_blocks, world_config_dir
):
    """SPEC §6.1: one statement per connected deficiency component.

    Merging them into a single giant set would be true but useless -- the
    coordinator would be told to talk to twice as many people as necessary.
    """
    case = BlockedCase(k=k, n_blocks=n_blocks, n_free=1, seed=f"multi-k{k}-b{n_blocks}")
    _config, report = build(case.world, world_config_dir)
    problem = report.problem

    diagnosis = diagnose_infeasible(problem)

    # Independent components must stay separate statements. Compare on the
    # minimal sets: each component also carries a "full group" statement when
    # the component is larger than its minimal violator.
    found = {(bs.people, bs.desks) for bs in diagnosis.blocking_sets if bs.minimal}
    assert found == case.expected_blocking_sets
    assert not [bs for bs in diagnosis.blocking_sets
                if not bs.minimal and set(bs.people) > set().union(
                    *(set(m.people) for m in diagnosis.blocking_sets if m.minimal))], (
        "a 'full group' statement must not merge independent components")
    assert diagnosis.deficiency == case.deficiency == n_blocks
    for blocking in diagnosis.blocking_sets:
        assert_blocking_set_is_a_minimal_violator(problem, blocking)
    summary = diagnosis.summary()
    assert summary.count("can only reach") >= n_blocks


@pytest.mark.parametrize(
    "scenario",
    ("everyone_ranks_same_k_desks", "more_people_than_desks", "cohort_zone_starved"),
)
@pytest.mark.parametrize("k", (3, 5))
def test_every_reported_blocking_set_is_true_and_minimal(
    scenario, k, world_config_dir
):
    """The same claim on instances nobody designed to be tidy."""
    if scenario == "everyone_ranks_same_k_desks":
        world = synth.everyone_ranks_same_k_desks(n_people=k + 3, k=k, seed=f"a{k}").world
    elif scenario == "more_people_than_desks":
        world = synth.more_people_than_desks(
            n_people=3 * k, n_desks=k + 2, k=k, seed=f"b{k}"
        ).world
    else:
        world = synth.cohort_zone_starved(
            n_people=2 * k + 4,
            n_precandidates=k + 2,
            precandidate_desks=k,
            k=k,
            seed=f"c{k}",
        ).world

    _config, report = build(world, world_config_dir)
    diagnosis = diagnose_infeasible(report.problem)

    assert diagnosis.blocking_sets, "an infeasible run must name somebody"
    for blocking in diagnosis.blocking_sets:
        assert_blocking_set_is_a_minimal_violator(report.problem, blocking)


@pytest.mark.parametrize("k", K_VALUES)
@pytest.mark.parametrize(
    "scenario", ("everyone_ranks_same_k_desks", "more_people_than_desks",
                 "cohort_zone_starved"),
)
def test_the_widest_blocking_set_carries_the_full_deficiency(
    scenario, k, world_config_dir
):
    """Exactly one reported set is the full over-subscribed group.

    Why this matters: with nine people all ranking the same five desks, a
    *minimal* violator is some six of them, short by one. Acting on that alone,
    the coordinator asks one person to re-rank when four have to. So the
    diagnosis must also carry the widest set, whose shortfall equals the
    deficiency exactly, and must lead with it.
    """
    if scenario == "everyone_ranks_same_k_desks":
        world = synth.everyone_ranks_same_k_desks(n_people=k + 3, k=k, seed=f"w{k}").world
    elif scenario == "more_people_than_desks":
        world = synth.more_people_than_desks(
            n_people=3 * k, n_desks=k + 2, k=k, seed=f"x{k}"
        ).world
    else:
        world = synth.cohort_zone_starved(
            n_people=2 * k + 4, n_precandidates=k + 2, precandidate_desks=k,
            k=k, seed=f"y{k}",
        ).world

    _config, report = build(world, world_config_dir)
    diagnosis = diagnose_infeasible(report.problem)

    widest = [b for b in diagnosis.blocking_sets if not b.minimal]
    assert len(widest) == 1, (
        "expected exactly one non-minimal (widest) blocking set, got "
        f"{len(widest)}"
    )
    assert widest[0].shortfall == diagnosis.deficiency, (
        f"the widest blocking set is short by {widest[0].shortfall} but the "
        f"deficiency is {diagnosis.deficiency}; it is not actually the full group"
    )
    assert diagnosis.blocking_sets[0] is widest[0], (
        "the widest set must be reported FIRST -- it is the number to act on"
    )
    # It is a genuine violator, and no smaller reported set exceeds it.
    assert len(widest[0].people) > len(widest[0].desks)
    for other in diagnosis.blocking_sets[1:]:
        assert other.shortfall <= widest[0].shortfall
        assert set(other.people) <= set(widest[0].people), (
            "a minimal set should be contained in the full over-subscribed group"
        )
    # And the headline text must distinguish the two, or the reader cannot tell
    # which number to act on.
    assert "FULL" in widest[0].render()
    assert "act on" in diagnosis.summary()


# ==========================================================================
# max_satisfiable and deficiency
# ==========================================================================


@pytest.mark.parametrize("k", K_VALUES)
@pytest.mark.parametrize("n_blocks, n_free", ((1, 2), (2, 0)))
def test_max_satisfiable_is_the_true_maximum_matching(
    k, n_blocks, n_free, world_config_dir
):
    """Cross-checked three ways: by construction, by scipy, and by deficiency."""
    case = BlockedCase(
        k=k, n_blocks=n_blocks, n_free=n_free, seed=f"max-k{k}-{n_blocks}-{n_free}"
    )
    _config, report = build(case.world, world_config_dir)
    problem = report.problem
    diagnosis = diagnose_infeasible(problem)

    # By construction: each block seats K of its K+1, each free person seats.
    expected = n_blocks * k + n_free
    assert diagnosis.max_satisfiable == expected
    # By an independent implementation.
    assert diagnosis.max_satisfiable == scipy_matching_size(problem.allowed)
    # And the definition of deficiency, which the dataclass computes.
    assert diagnosis.deficiency == problem.n_people - diagnosis.max_satisfiable
    assert diagnosis.deficiency == case.deficiency
    assert diagnosis.n_people == problem.n_people
    assert diagnosis.n_desks == problem.n_desks


@pytest.mark.parametrize("trial", range(12))
def test_max_satisfiable_matches_scipy_on_random_small_instances(
    trial, make_random_problem
):
    """Random shapes, including feasible ones, against scipy's Hopcroft-Karp."""
    rng = stable_rng("max-satisfiable", trial)
    n_people = int(rng.integers(1, 8))
    n_desks = int(rng.integers(1, 8))
    k = int(rng.integers(1, min(n_desks, 5) + 1))
    problem = make_random_problem(
        "infeasibility", trial, n_people=n_people, n_desks=n_desks, k=k, n_zones=2,
        restricted_frac=0.5,
    )
    diagnosis = diagnostics.diagnose(problem, SEED)

    expected = scipy_matching_size(problem.allowed)
    brute, _sets = enumerate_max_matched_rows(problem.allowed)
    assert diagnosis.max_satisfiable == expected == brute
    assert diagnosis.deficiency == problem.n_people - diagnosis.max_satisfiable
    assert (diagnosis.deficiency == 0) == (
        matching.has_perfect_left_matching(problem.allowed)
    )


# ==========================================================================
# K_min
# ==========================================================================


@pytest.mark.parametrize("k", K_VALUES)
@pytest.mark.parametrize("n_blocks", (1, 2))
def test_k_min_submitted_is_none_when_infeasible_at_k(k, n_blocks, world_config_dir):
    """Feasibility is monotone in K', so failing at K means failing below it."""
    case = BlockedCase(k=k, n_blocks=n_blocks, n_free=1, seed=f"kmin-none-{k}-{n_blocks}")
    _config, report = build(case.world, world_config_dir)
    problem = report.problem
    diagnosis = diagnose_infeasible(problem)

    assert diagnosis.k_min_submitted is None
    # Re-derived: no K' <= K admits a complete assignment either.
    for k_try in range(1, problem.k + 1):
        allowed = problem.allowed & (problem.rank >= 1) & (problem.rank <= k_try)
        assert scipy_matching_size(allowed) < problem.n_people


@pytest.mark.parametrize("k", (3, 5))
@pytest.mark.parametrize("shared", (1, 2))
@pytest.mark.parametrize("n_people", (4, 7))
def test_k_min_submitted_is_the_true_minimum_when_feasible(
    k, shared, n_people, world_config_dir
):
    """Everyone shares their first `shared` picks, so the answer is `shared + 1`.

    Cross-checked against a scipy sweep as well as against the construction, so
    a K sweep that always answered 1 (or always K) fails twice over.
    """
    if shared >= k:
        pytest.skip("the shared prefix must be shorter than K")
    world = shared_prefix_world(
        n_people=n_people, k=k, shared=shared, seed=f"kmin-{k}-{shared}-{n_people}"
    )
    _config, report = build(world, world_config_dir)
    problem = report.problem
    assert matching.has_perfect_left_matching(problem.allowed), (
        "fixture premise: this instance is feasible at K"
    )

    diagnosis = diagnostics.diagnose(problem, SEED)
    expected = shared + 1 if n_people > shared else 1
    assert diagnosis.k_min_submitted == expected
    assert diagnosis.deficiency == 0

    # Independent sweep: feasible at k_min, infeasible at everything below it.
    for k_try in range(1, problem.k + 1):
        allowed = problem.allowed & (problem.rank >= 1) & (problem.rank <= k_try)
        feasible = scipy_matching_size(allowed) == problem.n_people
        assert feasible == (k_try >= expected), f"disagreement at K'={k_try}"


@pytest.mark.parametrize("trial", range(10))
def test_k_min_submitted_matches_an_independent_sweep(trial, make_random_problem):
    rng = stable_rng("kmin-sweep", trial)
    n_people = int(rng.integers(1, 7))
    n_desks = int(rng.integers(2, 8))
    k = int(rng.integers(1, min(n_desks, 4) + 1))
    problem = make_random_problem(
        "kmin", trial, n_people=n_people, n_desks=n_desks, k=k
    )
    expected = None
    for k_try in range(1, k + 1):
        allowed = problem.allowed & (problem.rank >= 1) & (problem.rank <= k_try)
        if scipy_matching_size(allowed) == problem.n_people:
            expected = k_try
            break
    assert diagnostics.diagnose(problem, SEED).k_min_submitted == expected


@pytest.mark.parametrize("k", K_VALUES)
@pytest.mark.parametrize("n_free", (1, 2))
def test_k_min_extended_is_past_k_and_the_instance_really_is_feasible_there(
    k, n_free, world_config_dir
):
    """SPEC §6.1: the extension is invented, so the number had better be checkable.

    The test rebuilds the extension from the same seed -- which is only possible
    because the ordering is seeded rather than arbitrary -- and confirms both
    halves of the claim: feasible at `k_min_extended`, and not at anything
    between K and it.
    """
    case = BlockedCase(k=k, n_blocks=1, n_free=n_free, spare=2 * k, seed=f"ext-{k}-{n_free}")
    _config, report = build(case.world, world_config_dir)
    problem = report.problem
    diagnosis = diagnose_infeasible(problem)

    assert diagnosis.k_min_extended is not None, (
        "with spare desks in the pool, some wider ranking must work"
    )
    assert diagnosis.k_min_extended >= problem.k + 1
    assert diagnosis.k_min_extended <= problem.n_desks

    extended = diagnostics._extended_rank(problem, scoring.make_rng(diagnosis.seed_string))
    assert diagnosis.seed_string == SEED, "the diagnosis records the seed it used"
    eligible = problem.eligible if problem.eligible is not None else problem.allowed
    # Ranks 1..K must survive the extension untouched: the student chose those.
    submitted = problem.rank >= 1
    assert np.array_equal(extended[submitted], problem.rank[submitted])

    for k_try in range(problem.k, diagnosis.k_min_extended + 1):
        allowed = eligible & (extended >= 1) & (extended <= k_try)
        feasible = scipy_matching_size(allowed) == problem.n_people
        assert feasible == (k_try == diagnosis.k_min_extended), (
            f"k_min_extended={diagnosis.k_min_extended} but K'={k_try} is "
            f"{'feasible' if feasible else 'infeasible'}"
        )

    assert str(diagnosis.k_min_extended) in diagnosis.summary()
    assert "diagnostic only" in diagnosis.summary()


@pytest.mark.parametrize("k", K_VALUES)
@pytest.mark.parametrize("excess", (1, 3))
def test_k_min_extended_is_none_when_no_ranking_could_have_worked(
    k, excess, world_config_dir
):
    """More people than desks: the extension cannot invent furniture."""
    n_desks = k + 1
    world = synth.more_people_than_desks(
        n_people=n_desks + excess, n_desks=n_desks, k=k, seed=f"noext-{k}-{excess}"
    ).world
    _config, report = build(world, world_config_dir)
    diagnosis = diagnose_infeasible(report.problem)

    assert diagnosis.k_min_extended is None
    assert diagnosis.k_min_submitted is None
    assert "Not feasible at any K" in diagnosis.summary()


# ==========================================================================
# always_unmatched vs sometimes_unmatched
# ==========================================================================


@pytest.mark.parametrize("trial", range(40))
def test_unmatched_analysis_matches_exhaustive_enumeration(trial):
    """The semantics, against every maximum matching there is.

    * `sometimes_unmatched` -- SOME maximum matching leaves them out.
    * `always_unmatched`    -- NO maximum matching covers them.

    Random shapes including empty rows, which is the only way a bipartite left
    vertex can be always-unmatched: given any maximum matching missing u and any
    edge (u, v), swapping v's partner for u yields another maximum matching, so
    a vertex with an edge is always coverable. The oracle discovers that rather
    than assuming it.
    """
    rng = stable_rng("unmatched", trial)
    n_rows = int(rng.integers(1, 6))
    n_cols = int(rng.integers(1, 6))
    density = float(rng.choice([0.15, 0.35, 0.6, 0.9]))
    allowed = rng.random((n_rows, n_cols)) < density

    nu, covered_sets = enumerate_max_matched_rows(allowed)
    expected_sometimes = sorted(
        u for u in range(n_rows) if any(u not in s for s in covered_sets)
    )
    expected_always = sorted(
        u for u in range(n_rows) if all(u not in s for s in covered_sets)
    )

    always, sometimes = matching.unmatched_analysis(allowed)
    assert matching.matching_size(matching.adjacency(allowed), n_cols) == nu
    assert sometimes == expected_sometimes
    assert always == expected_always
    assert set(always) <= set(sometimes), "always_unmatched must be a subset"


@pytest.mark.parametrize("k", (2, 3))
@pytest.mark.parametrize("n_free", (0, 2))
def test_diagnosis_unmatched_semantics_on_a_real_problem(k, n_free, world_config_dir):
    """The same two sets, on a pipeline-built Problem, against the same oracle."""
    case = BlockedCase(k=k, n_blocks=1, n_free=n_free, seed=f"unm-{k}-{n_free}")
    _config, report = build(case.world, world_config_dir)
    problem = report.problem
    diagnosis = diagnose_infeasible(problem)

    _nu, covered = enumerate_max_matched_rows(problem.allowed)
    expected_sometimes = {
        problem.people[u]
        for u in range(problem.n_people)
        if any(u not in s for s in covered)
    }
    assert set(diagnosis.sometimes_unmatched) == expected_sometimes
    assert set(diagnosis.sometimes_unmatched) == case.blocked_people, (
        "exactly the over-subscribed block is at risk; the free people are not"
    )
    assert diagnosis.always_unmatched == (), (
        "nobody is structurally stuck here: every one of the K+1 is seated by "
        "some maximum matching"
    )
    for email in case.free_people:
        assert email not in diagnosis.sometimes_unmatched


@pytest.mark.parametrize("k", (2, 3))
def test_a_person_with_no_reachable_desk_is_always_unmatched(k):
    """`always_unmatched` is not dead code -- it fires for an empty row.

    The pipeline cannot produce this shape (a person whose choices are all
    dropped is a `ResponseError` naming them, which is asserted in
    test_adversarial), so the matrix is built directly. The routine still has to
    be right, because it is what the diagnosis reports.
    """
    n_others = k + 1
    allowed = np.zeros((n_others + 1, k), dtype=bool)
    allowed[:n_others, :] = True          # k+1 people over k desks
    # the last row has no edges at all

    always, sometimes = matching.unmatched_analysis(allowed)
    assert always == [n_others]
    assert set(sometimes) == set(range(n_others + 1))
    assert set(always) <= set(sometimes)


# ==========================================================================
# Round 2
# ==========================================================================


def affected_emails(diagnosis: Infeasibility) -> set[str]:
    return (
        {email for bs in diagnosis.blocking_sets for email in bs.people}
        | set(diagnosis.always_unmatched)
        | set(diagnosis.sometimes_unmatched)
    )


@pytest.mark.parametrize("k", (2, 3, 5))
@pytest.mark.parametrize(
    "n_blocks, n_free, n_keepers, n_unavailable",
    ((1, 2, 0, 0), (1, 1, 2, 1), (2, 1, 1, 2)),
)
def test_round2_lists_exactly_the_affected_students(
    k, n_blocks, n_free, n_keepers, n_unavailable, world_config_dir
):
    """SPEC §6.2: the people who must act, and only them.

    Listing an unaffected student wastes their afternoon and leaks that they
    were at risk when they were not; omitting one means the re-run fails again.
    """
    case = BlockedCase(
        k=k,
        n_blocks=n_blocks,
        n_free=n_free,
        n_keepers=n_keepers,
        n_unavailable=n_unavailable,
        spare=n_keepers + n_unavailable + k,
        seed=f"r2-{k}-{n_blocks}-{n_free}-{n_keepers}-{n_unavailable}",
    )
    config, report = build(case.world, world_config_dir)
    problem = report.problem
    diagnosis = diagnose_infeasible(problem)

    entries = diagnostics.build_round2(problem, diagnosis)
    listed = [entry.email for entry in entries]

    assert listed == sorted(listed), "round-2 output must be deterministic order"
    assert set(listed) == affected_emails(diagnosis)
    assert case.blocked_people <= set(listed), "the blocked group must be contacted"
    for email in case.keeper_desks:
        assert email not in listed, "a keeper is not in the pool and cannot re-rank"

    for entry in entries:
        assert entry.name == problem.person_names[entry.email]
        assert entry.reason.strip(), f"{entry.email} was listed with no reason"
        assert entry.suggested_min_ranks > problem.k, (
            "asking for the same K ranks again would change nothing"
        )


@pytest.mark.parametrize("k", (2, 3, 5))
@pytest.mark.parametrize("n_keepers, n_unavailable", ((0, 0), (2, 2)))
def test_round2_only_offers_desks_that_are_eligible_and_still_in_the_pool(
    k, n_keepers, n_unavailable, world_config_dir
):
    """Offering a desk that is gone is worse than offering none: the student
    ranks it, the solver drops it, and they silently lose a rank."""
    from deskmatch import eligibility as elig

    case = BlockedCase(
        k=k,
        n_blocks=1,
        n_free=2,
        n_keepers=n_keepers,
        n_unavailable=n_unavailable,
        spare=n_keepers + n_unavailable + k,
        seed=f"r2pool-{k}-{n_keepers}-{n_unavailable}",
    )
    config, report = build(case.world, world_config_dir)
    problem = report.problem
    diagnosis = diagnose_infeasible(problem)
    entries = diagnostics.build_round2(problem, diagnosis)

    pool = set(problem.desks)
    locked = {desk for desk, _email in report.locked_desks}
    unavailable = set(report.unavailable_desks)
    assert len(locked) == n_keepers and len(unavailable) == n_unavailable, (
        "fixture premise: there really are desks outside the pool to get wrong"
    )
    assert not (pool & (locked | unavailable))

    all_desks = {desk.id: desk for desk in config.rooms.all_desks}
    for entry in entries:
        person = report.effective_people[entry.email]
        # Re-derived from the rule table, not from problem.eligible, which is
        # the same array build_round2 read.
        zones = set(elig.allowed_zones(config.eligibility, config.rooms, person))
        assert entry.available_desks, f"{entry.email} was offered nothing at all"
        assert list(entry.available_desks) == sorted(entry.available_desks)
        for desk_id in entry.available_desks:
            assert desk_id in pool, f"{entry.email} was offered {desk_id}, not in the pool"
            assert desk_id not in locked and desk_id not in unavailable
            assert all_desks[desk_id].zone in zones, (
                f"{entry.email} was offered {desk_id}, outside their zones"
            )
        assert entry.available_labels == tuple(
            problem.desk_labels[d] for d in entry.available_desks
        )
        # Their existing ranking comes back so the form can pre-fill it.
        row = problem.people.index(entry.email)
        ranked = {
            int(problem.rank[row, j]): problem.desks[j]
            for j in range(problem.n_desks)
            if problem.rank[row, j] >= 1
        }
        assert entry.current_choices == tuple(ranked[r] for r in sorted(ranked))
        assert set(entry.current_choices) <= set(entry.available_desks)


@pytest.mark.parametrize("k", (3, 5))
@pytest.mark.parametrize("n_candidates", (2, 5))
def test_round2_never_offers_a_desk_outside_the_students_own_zone(
    k, n_candidates, world_config_dir
):
    """The zoned version of the same claim, where getting it wrong is possible.

    In a single-zone world "every desk in the pool" and "every desk they are
    eligible for" are the same list, so nothing is being tested. Here the
    confined cohort must be offered *their* zone and nothing else, and the
    unrestricted students must be offered everything -- so both an over-generous
    and a stingy round-2 file fail.
    """
    from deskmatch import eligibility as elig

    n_precandidates = k + 2
    world = synth.cohort_zone_starved(
        n_people=n_precandidates + n_candidates,
        n_precandidates=n_precandidates,
        precandidate_desks=k,
        k=k,
        seed=f"r2zone-{k}-{n_candidates}",
    ).world
    config, report = build(world, world_config_dir)
    problem = report.problem
    diagnosis = diagnose_infeasible(problem)
    entries = diagnostics.build_round2(problem, diagnosis)

    zone_of = {desk.id: desk.zone for desk in config.rooms.all_desks}
    all_zones = {desk.zone for desk in config.rooms.all_desks}
    assert len(all_zones) > 1, "fixture premise: there is more than one zone to confuse"

    saw_a_restricted_student = False
    for entry in entries:
        person = report.effective_people[entry.email]
        zones = set(elig.allowed_zones(config.eligibility, config.rooms, person))
        expected = {desk for desk in problem.desks if zone_of[desk] in zones}
        forbidden = set(problem.desks) - expected
        if forbidden:
            saw_a_restricted_student = True
        assert set(entry.available_desks) == expected, (
            f"{entry.email} (zones {sorted(zones)}) was offered "
            f"{sorted(set(entry.available_desks) ^ expected)} in error"
        )
        assert not (set(entry.available_desks) & forbidden)

    assert saw_a_restricted_student, (
        "fixture premise: at least one listed student must be zone-restricted, "
        "or 'eligible' and 'in the pool' would be the same set"
    )


@pytest.mark.parametrize("k", (2, 3))
@pytest.mark.parametrize("n_blocks", (1, 2))
def test_write_round2_writes_valid_files_that_agree_with_build_round2(
    k, n_blocks, world_config_dir, tmp_path
):
    """The files are the deliverable, so parse them back and check them."""
    case = BlockedCase(k=k, n_blocks=n_blocks, n_free=1, spare=k, seed=f"r2w-{k}-{n_blocks}")
    _config, report = build(case.world, world_config_dir)
    problem = report.problem
    diagnosis = diagnose_infeasible(problem)

    json_path = tmp_path / "round2_input.json"
    csv_path = tmp_path / "round2_roster.csv"
    entries = diagnostics.write_round2(json_path, csv_path, problem, diagnosis)

    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["k"] == problem.k
    assert document["deficiency"] == diagnosis.deficiency
    assert document["seed_string"] == diagnosis.seed_string
    assert document["suggested_k"] > problem.k
    assert document["reason"].strip()

    students = document["students"]
    assert [s["email"] for s in students] == [e.email for e in entries]
    for student, entry in zip(students, entries):
        assert student["name"] == entry.name
        assert student["reason"] == entry.reason
        assert student["current_choices"] == list(entry.current_choices)
        assert [d["id"] for d in student["available_desks"]] == list(entry.available_desks)
        assert [d["label"] for d in student["available_desks"]] == list(
            entry.available_labels
        )
        assert student["suggested_min_ranks"] == entry.suggested_min_ranks

    import csv as csv_mod

    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv_mod.reader(handle))
    assert rows[0] == [
        "name", "email", "reason", "current_choices", "available_desks",
        "suggested_min_ranks",
    ]
    assert len(rows) == len(entries) + 1
    for row, entry in zip(rows[1:], entries):
        assert row[0] == entry.name
        assert row[1] == entry.email
        assert row[3].split() == list(entry.current_choices)
        assert row[4].split() == list(entry.available_desks)
        assert int(row[5]) == entry.suggested_min_ranks

    # Same inputs, same bytes (invariant I3 applies to the failure path too).
    before = (json_path.read_bytes(), csv_path.read_bytes())
    diagnostics.write_round2(json_path, csv_path, problem, diagnosis)
    assert (json_path.read_bytes(), csv_path.read_bytes()) == before


@pytest.mark.parametrize("k", (2, 3))
def test_diagnosis_to_dict_is_serialisable_and_carries_the_numbers(
    k, world_config_dir
):
    """`diagnostics.json` is what the coordinator forwards; it must be complete."""
    case = BlockedCase(k=k, n_blocks=1, n_free=1, spare=k, seed=f"dict-{k}")
    _config, report = build(case.world, world_config_dir)
    diagnosis = diagnose_infeasible(report.problem)

    document = diagnostics.diagnosis_to_dict(diagnosis)
    text = json.dumps(document, sort_keys=True)
    reparsed = json.loads(text)

    assert reparsed["feasible"] is False
    assert reparsed["k"] == diagnosis.k
    assert reparsed["max_satisfiable"] == diagnosis.max_satisfiable
    assert reparsed["deficiency"] == diagnosis.deficiency
    assert reparsed["k_min_submitted"] is None
    assert reparsed["k_min_extended"] == diagnosis.k_min_extended
    assert reparsed["sometimes_unmatched"] == list(diagnosis.sometimes_unmatched)
    assert "Hypothetical" in reparsed["k_min_extended_note"]
    blocking = reparsed["blocking_sets"]
    assert len(blocking) == len(diagnosis.blocking_sets)
    for entry, source in zip(blocking, diagnosis.blocking_sets):
        assert [p["email"] for p in entry["people"]] == list(source.people)
        assert [p["name"] for p in entry["people"]] == list(source.names)
        assert [d["id"] for d in entry["desks"]] == list(source.desks)
        assert entry["shortfall"] == source.shortfall


# ==========================================================================
# The pre-deadline check
# ==========================================================================


@pytest.mark.parametrize("k", (2, 3, 5))
@pytest.mark.parametrize("excess", (1, 3))
def test_preflight_reports_failure_and_the_cli_exits_3(
    k, excess, world_config_dir, capsys
):
    """SPEC §9: `check` exits 3 when the current responses would fail."""
    case = synth.everyone_ranks_same_k_desks(
        n_people=k + excess, k=k, seed=f"pre-{k}-{excess}"
    )
    config_dir, responses_path = world_config_dir(case.world)
    config = load_config(config_dir)
    loaded = responses_mod.load_responses(str(responses_path), config.k)
    report = problem_mod.build_problem(config, loaded)

    result = diagnostics.preflight(report.problem, 0, config.scoring.tie_break_seed)
    assert result.would_succeed is False
    assert result.max_satisfiable_now == k
    assert result.n_responded == report.problem.n_people
    assert result.blocking_sets, "a failing check must name who is colliding"
    for blocking in result.blocking_sets:
        assert_blocking_set_is_a_minimal_violator(report.problem, blocking)
    rendered = result.render()
    assert "would FAIL" in rendered
    assert any(email in rendered for email in report.problem.people)
    assert result.hot_desks, "say which desks are contested"
    hottest = result.hot_desks[0]
    assert hottest[2] == report.problem.n_people, (
        "everyone named the same first choice, so it has every first-choice vote"
    )

    exit_code = cli.main(
        ["check", "--config", str(config_dir), "--responses", str(responses_path)]
    )
    capsys.readouterr()
    assert exit_code == 3


@pytest.mark.parametrize("k", (2, 3, 5))
@pytest.mark.parametrize("n_people", (2, 6))
def test_preflight_passes_and_the_cli_exits_0_when_it_would_work(
    k, n_people, world_config_dir, capsys
):
    case = synth.exact_fit(n_people=max(n_people, k), k=k, seed=f"pass-{k}-{n_people}")
    config_dir, responses_path = world_config_dir(case.world)
    config = load_config(config_dir)
    loaded = responses_mod.load_responses(str(responses_path), config.k)
    report = problem_mod.build_problem(config, loaded)

    result = diagnostics.preflight(report.problem, 0, config.scoring.tie_break_seed)
    assert result.would_succeed is True
    assert result.max_satisfiable_now == report.problem.n_people
    assert result.blocking_sets == ()
    assert "on track" in result.render()

    exit_code = cli.main(
        ["check", "--config", str(config_dir), "--responses", str(responses_path)]
    )
    capsys.readouterr()
    assert exit_code == 0


@pytest.mark.parametrize("k", (2, 3))
def test_a_passing_check_with_outstanding_responders_says_it_is_not_a_guarantee(
    k, world_config_dir
):
    """Honesty requirement from SPEC §6.3: passing now is not passing later."""
    case = synth.exact_fit(n_people=k + 2, k=k, seed=f"outstanding-{k}")
    config, report = build(case.world, world_config_dir)
    outstanding = 3

    result = diagnostics.preflight(
        report.problem, outstanding, config.scoring.tie_break_seed
    )
    assert result.would_succeed
    assert result.n_outstanding == outstanding
    assert any(str(outstanding) in message for message in result.messages)
    assert any("not submitted" in message for message in result.messages)
    assert str(outstanding) in result.render()


@pytest.mark.parametrize("k", (2, 3, 5))
@pytest.mark.parametrize("n_people", (8, 13))
@pytest.mark.parametrize("concentration", (0.85, 1.0))
def test_more_responders_can_never_turn_a_failing_check_into_a_passing_one(
    k, n_people, concentration, world_config_dir
):
    """Monotonicity, which is the whole justification for running `check` early.

    Adding a person adds a row: the maximum matching can grow by at most one
    while the head count grows by exactly one, so a deficiency can never shrink.
    The chain is built in a seeded random order so the property is not being
    tested only in roster order.
    """
    world = synth.generate(
        n_people,
        n_desks=n_people + k,
        k=k,
        seed=f"mono-{k}-{n_people}-{concentration}",
        concentration=concentration,
        n_zones=1,
        eligibility_style="flat",
        keeper_frac=0.0,
    )
    config, report = build(world, world_config_dir)
    loaded = responses_mod.load_responses(
        str(world_config_dir(world)[1]), config.k
    )

    rng = stable_rng("monotone", k, n_people, concentration)
    order = sorted(loaded.latest)
    order = [order[i] for i in rng.permutation(len(order)).tolist()]

    failed_at: int | None = None
    for size in range(1, len(order) + 1):
        partial = subset_responses(loaded, order[:size])
        sub_report = problem_mod.build_problem(config, partial)
        result = diagnostics.preflight(
            sub_report.problem, len(order) - size, config.scoring.tie_break_seed
        )
        assert sub_report.problem.n_people == size
        if not result.would_succeed:
            if failed_at is None:
                failed_at = size
        else:
            assert failed_at is None, (
                f"the check passed at {size} responders after failing at "
                f"{failed_at}: adding responders relieved competition, which is "
                f"impossible -- the earlier collision cannot have gone away"
            )

    if concentration == 1.0:
        # Perfectly correlated tastes: every responder submits the identical K
        # desks, so the check must start failing at exactly K + 1 responders.
        # Without this the loop above would be satisfied by a check that never
        # fails at all.
        assert failed_at == k + 1, (
            f"with identical rankings the check should first fail at {k + 1} "
            f"responders, not at {failed_at}"
        )

    # And the final state agrees with what a full solve would do.
    if failed_at is None:
        solve_mod.solve(report.problem, config.scoring.tie_break_seed)
    else:
        diagnose_infeasible(report.problem, config.scoring.tie_break_seed)


# ==========================================================================
# I7: the run fails; it never degrades
# ==========================================================================


@pytest.mark.parametrize("k", K_VALUES)
@pytest.mark.parametrize(
    "flavour", ("identical-rankings", "overflow", "zone-starved", "constructed-block")
)
def test_infeasible_never_returns_a_partial_assignment(k, flavour, world_config_dir):
    """Invariant I7 and SPEC §9: exit code 2, a diagnosis, and no Solution.

    The one thing this system is sold on is that it never quietly hands someone
    their ninth choice, so 'raised the right exception' is asserted alongside
    'produced no assignment object at all'.
    """
    if flavour == "identical-rankings":
        world = synth.everyone_ranks_same_k_desks(
            n_people=k + 2, k=k, seed=f"i7a-{k}"
        ).world
    elif flavour == "overflow":
        world = synth.more_people_than_desks(
            n_people=2 * k + 3, n_desks=k + 1, k=k, seed=f"i7b-{k}"
        ).world
    elif flavour == "zone-starved":
        world = synth.cohort_zone_starved(
            n_people=2 * k + 3,
            n_precandidates=k + 2,
            precandidate_desks=k,
            k=k,
            seed=f"i7c-{k}",
        ).world
    else:
        world = BlockedCase(k=k, n_blocks=1, n_free=1, seed=f"i7d-{k}").world

    _config, report = build(world, world_config_dir)
    problem = report.problem

    outcome: Solution | None = None
    try:
        outcome = solve_mod.solve(problem, SEED)
    except InfeasibleError as exc:
        assert exc.exit_code == 2
        assert isinstance(exc.diagnosis, Infeasibility)
        assert exc.diagnosis.deficiency >= 1
        assert exc.diagnosis.max_satisfiable < problem.n_people
        assert exc.diagnosis.k == problem.k
        assert str(exc) == exc.diagnosis.summary()
        assert exc.diagnosis.blocking_sets
    assert outcome is None, (
        "solve() returned an assignment on an infeasible instance; I7 says the "
        "run must fail rather than seat some people and drop the rest"
    )

    # Calling it again must behave identically -- no state was consumed.
    with pytest.raises(InfeasibleError):
        solve_mod.solve(problem, SEED)


@pytest.mark.parametrize("k", (2, 3))
@pytest.mark.parametrize("n_free", (1, 2))
def test_brute_force_agrees_about_which_instances_have_no_answer(
    k, n_free, world_config_dir
):
    """`solve.brute_force_optimum` is the package's own exhaustive oracle.

    It returns None exactly when no complete assignment exists, so it decides
    feasibility by enumeration rather than by Hopcroft-Karp -- an independent
    second opinion on the only question that makes the run fail.
    """
    infeasible = BlockedCase(k=k, n_blocks=1, n_free=n_free, seed=f"bf-{k}-{n_free}")
    _config, report = build(infeasible.world, world_config_dir)
    problem = report.problem
    assert problem.n_people <= 8, "keep the exhaustive oracle cheap"

    diagnose_infeasible(problem)
    assert solve_mod.brute_force_optimum(problem) is None, (
        "exhaustive search found a complete assignment that solve() refused"
    )

    feasible = synth.exact_fit(n_people=k + 1, k=k, seed=f"bf-ok-{k}-{n_free}").world
    config, ok_report = build(feasible, world_config_dir)
    solution = solve_mod.solve(ok_report.problem, config.scoring.tie_break_seed)
    assert solve_mod.brute_force_optimum(ok_report.problem) == solution.total_points_scaled


@pytest.mark.parametrize("k", K_VALUES)
@pytest.mark.parametrize("n_people", (1, 4, 9))
def test_a_feasible_instance_seats_everybody(
    k, n_people, world_config_dir, invariants
):
    """The complement: when it does succeed, nobody is left out (I7 again)."""
    case = synth.exact_fit(n_people=max(n_people, k), k=k, seed=f"full-{k}-{n_people}")
    config, report = build(case.world, world_config_dir)
    solution = solve_mod.solve(report.problem, config.scoring.tie_break_seed)

    invariants.all(report.problem, solution)
    assert solution.unassigned_people == ()
    assert len(solution.assignments) == report.problem.n_people
    assert diagnostics.diagnose(report.problem, SEED).deficiency == 0
