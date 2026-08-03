"""Adversarial inputs. Every one must behave *well*.

"Well" has a precise meaning here, and it is the whole point of the module:

  * either a **correct result** -- all post-solve invariants hold; or
  * a **specific, readable exception** from `deskmatch.errors`, carrying the
    documented exit code (SPEC §9) and a message that names the person, the
    file, or the desk the coordinator has to go and look at.

Two failure modes are unacceptable and are asserted against directly:

  * a traceback from inside numpy/scipy/csv -- `only_deskmatch_errors` turns any
    non-`DeskMatchError` into a test failure that prints where it came from;
  * a silently wrong answer -- every case that *does* solve is checked against
    `assert_solution_invariants` (I4, I5, I7) rather than merely "did not raise".

The readability of the error messages is a product requirement (the coordinator
is a grad student on a deadline, not a developer), so the message assertions are
first-class: they check that the text names the *specific* person/file/desk, not
just that some error occurred.

Sizes: every test is parameterised over N and K and derives its expectations
from the fixture it was handed. Nothing here knows that K is 5 (invariant I1).
"""

from __future__ import annotations

import contextlib
import csv
import datetime as dt
import io
import json
from pathlib import Path

import pytest

from conftest import (
    dump_csv_text,
    find_problem,
    find_warning,
    response_header,
    submission_row,
    write_text,
)
from deskmatch import problem as problem_mod
from deskmatch import responses as responses_mod
from deskmatch import scoring
from deskmatch import solve as solve_mod
from deskmatch import synth
from deskmatch.config import load_config
from deskmatch.errors import (
    ConfigError,
    DeskMatchError,
    InfeasibleError,
    ResponseError,
)
from deskmatch.types import Responses

# --------------------------------------------------------------------------
# Parameter grids. Deliberately spread across K so that nothing can pass by
# accidentally agreeing with the default K=5.
# --------------------------------------------------------------------------

K_VALUES: tuple[int, ...] = (2, 3, 5, 7)
SMALL_K: tuple[int, ...] = (2, 3, 5)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


@contextlib.contextmanager
def only_deskmatch_errors(what: str):
    """Anything that escapes must be one of ours, never a library traceback."""
    try:
        yield
    except DeskMatchError:
        raise
    except Exception as exc:  # pragma: no cover - only on a genuine regression
        pytest.fail(
            f"{what} raised {type(exc).__module__}.{type(exc).__name__}: {exc}\n"
            f"Everything a coordinator can cause must surface as a DeskMatchError "
            f"with a readable message (docs/SPEC.md §2, errors.py)."
        )


def _read_response_rows(path) -> tuple[list[str], list[dict[str, str]]]:
    """A response file as (header, rows-of-dicts).

    Through the csv module, not `str.split(",")`: a real export quotes any name
    with a comma in it, and hand-splitting silently shreds those rows into
    "has 4 fields but the header has 13".
    """
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def rejected_response_file(path, k: int | None):
    """`load_responses` must reject this file. Returns every collected problem.

    Wraps conftest's `load_response_problems` to also pin the exit code: a
    readable message that exits 0 would still send the coordinator's shell
    script merrily onwards (SPEC §9).
    """
    try:
        loaded = responses_mod.load_responses(str(path), k)
    except ResponseError as exc:
        assert exc.exit_code == 4, "a response-schema failure is exit 4 (SPEC §9)"
        assert exc.problems, "raised with no problems attached"
        assert str(exc).strip(), "raised with an empty message"
        return list(exc.problems)
    pytest.fail(
        f"load_responses({path}) accepted this file; the test expected rejection. "
        f"It produced {len(loaded.warnings)} warning(s):\n  "
        + "\n  ".join(loaded.warnings)
    )


def build_from_world_via(world_config_dir, world, curve_name=None):
    """`build_from_world`, but callable from a test that only took one fixture."""
    config_dir, responses_path = world_config_dir(world)
    config = load_config(config_dir)
    loaded = responses_mod.load_responses(str(responses_path), config.k)
    return config, problem_mod.build_problem(config, loaded, curve_name)


def int_curve_of(config) -> tuple[int, ...]:
    """The primary curve as exact integers, derived (never written down)."""
    values, _scale = scoring.integerise(config.scoring.curve())
    return values


def empty_responses(k: int, source: str = "<no submissions>") -> Responses:
    return Responses(submissions=(), latest={}, k=k, source_path=source, sha256="0" * 64)


def scenario_case(name: str, *, k: int, size: int) -> synth.SynthCase:
    """A named scenario built at (K, size) instead of at its defaults.

    An explicit adapter rather than `**kwargs`: each builder has its own
    constraints (`n_desks >= k`, `precandidate_desks < n_precandidates`, ...),
    and satisfying them in terms of the parameters is precisely what keeps the
    grid honest at every K.
    """
    seed = f"{name}-k{k}-n{size}"
    if name == "everyone_ranks_same_k_desks":
        return synth.everyone_ranks_same_k_desks(n_people=k + size, k=k, seed=seed)
    if name == "more_people_than_desks":
        return synth.more_people_than_desks(
            n_people=2 * k + size, n_desks=k + size, k=k, seed=seed
        )
    if name == "cohort_zone_starved":
        n_precandidates = k + 1
        return synth.cohort_zone_starved(
            n_people=n_precandidates + size,
            n_precandidates=n_precandidates,
            precandidate_desks=k,
            k=k,
            seed=seed,
        )
    if name == "empty_roster":
        return synth.empty_roster(n_desks=k + size, k=k, seed=seed)
    if name == "single_person":
        return synth.single_person(n_desks=k + size, k=k, seed=seed)
    if name == "duplicate_submissions":
        return synth.duplicate_submissions(n_people=k + size, k=k, extra=size, seed=seed)
    if name == "stale_desk_reference":
        return synth.stale_desk_reference(n_people=2 * k + size, k=k, seed=seed)
    if name == "all_keepers":
        return synth.all_keepers(n_people=k + size, k=k, seed=seed)
    if name == "exact_fit":
        return synth.exact_fit(n_people=k + size, k=k, seed=seed)
    if name == "tie_heavy":
        return synth.tie_heavy(n_blocks=1 + size, k=k, seed=seed)
    raise AssertionError(f"no adapter for scenario {name!r}; add one")


# ==========================================================================
# The umbrella property: nothing crashes, nothing lies
# ==========================================================================


@pytest.mark.parametrize("name", synth.scenario_names())
@pytest.mark.parametrize("k", SMALL_K)
def test_every_named_scenario_either_solves_correctly_or_fails_readably(
    name, k, world_config_dir, invariants
):
    """The whole adversarial set, end to end, at several K.

    This is the test that says "behaves well" out loud. Each scenario must reach
    exactly one of two states, and both are checked -- a scenario that raised the
    right exception type with an empty message would fail here, and so would one
    that produced an assignment violating the K-floor.
    """
    case = scenario_case(name, k=k, size=2)
    config_dir, responses_path = world_config_dir(case.world)

    with only_deskmatch_errors(f"scenario {name!r} at K={k}"):
        try:
            config = load_config(config_dir)
            responses = responses_mod.load_responses(str(responses_path), config.k)
            build = problem_mod.build_problem(config, responses)
            solution = solve_mod.solve(build.problem, config.scoring.tie_break_seed)
        except (ConfigError, ResponseError) as exc:
            assert exc.exit_code == 4, f"{name}: validation failures are exit 4 (SPEC §9)"
            assert exc.problems, f"{name}: raised with no problems attached"
            for problem in exc.problems:
                assert problem.where.strip(), f"{name}: a complaint with no location"
                assert problem.what.strip(), f"{name}: a complaint with no explanation"
            return
        except InfeasibleError as exc:
            assert exc.exit_code == 2, "infeasibility is exit 2 (SPEC §9)"
            assert exc.diagnosis.deficiency >= 1
            assert exc.diagnosis.max_satisfiable < exc.diagnosis.n_people
            assert "INFEASIBLE" in exc.diagnosis.summary()
            return

    assert build.problem.k == k == config.k
    invariants.all(build.problem, solution)


# ==========================================================================
# Everyone ranks the same K desks
# ==========================================================================


@pytest.mark.parametrize("k", K_VALUES)
@pytest.mark.parametrize("excess", (1, 2, 4))
def test_identical_rankings_are_infeasible_when_more_people_than_k(
    k, excess, build_from_world
):
    """n_people > K people naming the same K desks: Hall fails by exactly n - K.

    The desk pool is deliberately larger than K, so a diagnosis that blamed a
    shortage of furniture rather than a collision of preferences would be wrong
    and this test would catch it.
    """
    n_people = k + excess
    case = synth.everyone_ranks_same_k_desks(
        n_people=n_people, k=k, seed=f"same-k{k}-x{excess}"
    )
    config, build = build_from_world(case.world)
    problem = build.problem

    assert problem.n_people == n_people
    assert problem.n_desks > k, "the pool must be bigger than K or this proves nothing"

    common = {desk for choices in case.world.latest_choices().values() for desk in choices}
    assert len(common) == k, "fixture premise: everyone named the same K desks"

    with pytest.raises(InfeasibleError) as excinfo:
        solve_mod.solve(problem, config.scoring.tie_break_seed)

    diagnosis = excinfo.value.diagnosis
    assert excinfo.value.exit_code == 2
    assert diagnosis.max_satisfiable == k, "K desks can seat exactly K of them"
    assert diagnosis.deficiency == n_people - k
    assert diagnosis.k_min_submitted is None, "infeasible at K means None (SPEC §6.1)"
    assert diagnosis.blocking_sets, "an infeasible run must name someone"
    for blocking in diagnosis.blocking_sets:
        assert set(blocking.desks) == common
        assert blocking.shortfall >= 1
        assert set(blocking.people) <= set(problem.people)


@pytest.mark.parametrize("k", K_VALUES)
@pytest.mark.parametrize("shortfall", (0, 1))
def test_identical_rankings_fit_when_n_people_does_not_exceed_k(
    k, shortfall, build_from_world, invariants
):
    """The boundary: n_people <= K is feasible, and the ranks come out 1..n.

    Everyone shares one preference list, so the optimum hands out the top
    `n_people` ranks exactly once each -- a total this test computes from the
    curve rather than asserting as a number.
    """
    n_people = k - shortfall
    if n_people < 1:
        pytest.skip(f"K={k} leaves no people at shortfall {shortfall}")
    case = synth.everyone_ranks_same_k_desks(
        n_people=n_people, k=k, seed=f"fit-k{k}-s{shortfall}"
    )
    config, build = build_from_world(case.world)
    solution = solve_mod.solve(build.problem, config.scoring.tie_break_seed)

    invariants.all(build.problem, solution)
    curve = int_curve_of(config)
    assert solution.total_points_scaled == sum(curve[:n_people])
    assert solution.rank_histogram() == (1,) * n_people + (0,) * (k - n_people)


# ==========================================================================
# More people than desks
# ==========================================================================


@pytest.mark.parametrize("k", SMALL_K)
@pytest.mark.parametrize("excess", (1, 5))
def test_more_people_than_desks_fails_with_a_counted_warning(k, excess, build_from_world):
    """Invariant I8: n_people != n_desks is normal. Overflow must diagnose, not crash."""
    n_desks = k + 2
    n_people = n_desks + excess
    case = synth.more_people_than_desks(
        n_people=n_people, n_desks=n_desks, k=k, seed=f"over-k{k}-x{excess}"
    )
    config, build = build_from_world(case.world)
    problem = build.problem

    assert problem.n_people > problem.n_desks
    # The pigeonhole is called out before any matching happens, in numbers.
    find_warning(
        build.warnings, str(problem.n_people), str(problem.n_desks), "competing"
    )

    with pytest.raises(InfeasibleError) as excinfo:
        solve_mod.solve(problem, config.scoring.tie_break_seed)

    diagnosis = excinfo.value.diagnosis
    assert excinfo.value.exit_code == 2
    assert diagnosis.max_satisfiable <= problem.n_desks
    assert diagnosis.deficiency >= problem.n_people - problem.n_desks
    assert diagnosis.k_min_extended is None, (
        "no extension of anybody's ranking can conjure up a desk that does not exist"
    )
    assert "Not feasible at any K" in diagnosis.summary()


# ==========================================================================
# A cohort whose zone is too small for it
# ==========================================================================


@pytest.mark.parametrize(
    "k, n_precandidates, precandidate_desks",
    ((3, 6, 3), (3, 5, 4), (5, 8, 5)),
)
@pytest.mark.parametrize("n_candidates", (2, 4))
def test_zone_starved_cohort_blames_the_zone_not_the_building(
    k, n_precandidates, precandidate_desks, n_candidates, build_from_world
):
    """There are plenty of desks; only the eligibility rule makes this fail.

    So the diagnosis has to name the cohort and the desks in *their* zone. A
    blocking set containing a candidate, or a desk from the unrestricted zone,
    would mean the diagnostic is blaming the wrong thing.
    """
    n_people = n_precandidates + n_candidates
    case = synth.cohort_zone_starved(
        n_people=n_people,
        n_precandidates=n_precandidates,
        precandidate_desks=precandidate_desks,
        k=k,
        seed=f"starve-k{k}-p{n_precandidates}-d{precandidate_desks}-c{n_candidates}",
    )
    config, build = build_from_world(case.world)
    problem = build.problem

    assert problem.n_desks >= problem.n_people, (
        "fixture premise: no global shortage, so only the zone can be at fault"
    )

    precandidates = {
        email
        for email in problem.people
        if build.effective_people[email].candidacy.casefold() == "precandidate"
    }
    zone_of = {desk.id: desk.zone for desk in config.rooms.all_desks}
    from deskmatch import eligibility as elig

    cohort_zones = {
        zone
        for email in precandidates
        for zone in elig.allowed_zones(
            config.eligibility, config.rooms, build.effective_people[email]
        )
    }
    assert cohort_zones, "fixture premise: the cohort is confined to some zone"

    with pytest.raises(InfeasibleError) as excinfo:
        solve_mod.solve(problem, config.scoring.tie_break_seed)

    diagnosis = excinfo.value.diagnosis
    assert diagnosis.deficiency >= len(precandidates) - precandidate_desks

    cohort_only = [
        blocking
        for blocking in diagnosis.blocking_sets
        if set(blocking.people) <= precandidates
    ]
    assert cohort_only, (
        "no blocking set consists purely of the confined cohort; the diagnosis is "
        "not naming the group the eligibility rule actually squeezed:\n"
        + diagnosis.summary()
    )
    for blocking in cohort_only:
        assert {zone_of[desk] for desk in blocking.desks} <= cohort_zones
        assert blocking.shortfall >= 1
        assert all(name for name in blocking.names), "people are named, not just keyed"


# ==========================================================================
# Empty roster, and a pool of zero people
# ==========================================================================


@pytest.mark.parametrize("k", K_VALUES)
@pytest.mark.parametrize("n_desks_extra", (0, 3))
def test_empty_roster_is_a_readable_config_error(k, n_desks_extra, world_config_dir):
    """Zero people is degenerate, not undefined: it must not be a traceback."""
    case = synth.empty_roster(n_desks=k + n_desks_extra, k=k, seed=f"empty-k{k}")
    config_dir, _responses = world_config_dir(case.world)
    assert case.world.n_people == 0

    with only_deskmatch_errors("loading a header-only roster"):
        with pytest.raises(ConfigError) as excinfo:
            load_config(config_dir)

    assert excinfo.value.exit_code == 4
    problem = find_problem(excinfo.value.problems, where="roster.csv", what="no people")
    assert "roster.csv" in problem.where
    assert problem.hint, "an empty roster is a mistake worth explaining"


@pytest.mark.parametrize("k", K_VALUES)
@pytest.mark.parametrize("n_people", (1, 4))
def test_a_pool_of_zero_people_solves_to_an_empty_assignment(
    k, n_people, world_config_dir
):
    """Nobody in the pool: empty matrices, empty argmax, division by n_people.

    Reached with a real roster and zero submissions, which is what the morning
    the form opens looks like.
    """
    world = synth.generate(
        n_people,
        n_desks=k + n_people,
        k=k,
        seed=f"nopool-k{k}-n{n_people}",
        n_zones=1,
        eligibility_style="flat",
        keeper_frac=0.0,
    )
    config_dir, _responses = world_config_dir(world)
    config = load_config(config_dir)

    with only_deskmatch_errors("building and solving an empty pool"):
        build = problem_mod.build_problem(config, empty_responses(config.k))
        solution = solve_mod.solve(build.problem, config.scoring.tie_break_seed)

    assert build.problem.n_people == 0
    assert build.problem.n_desks > 0
    assert len(build.excluded_people) == n_people
    assert {p.reason for p in build.excluded_people} == {"no submission"}
    assert solution.assignments == ()
    assert solution.total_points_scaled == 0
    assert solution.rank_histogram() == (0,) * k
    assert set(solution.free_desks) == set(build.problem.desks)


# ==========================================================================
# Exactly one person
# ==========================================================================


@pytest.mark.parametrize("k", (1, 2, 3, 5, 7))
@pytest.mark.parametrize("spare_desks", (0, 1, 6))
def test_single_person_gets_their_first_choice(
    k, spare_desks, build_from_world, invariants
):
    """n = 1 breaks anything that assumed a population -- including the jitter
    bound, which is 1/(2*(n+1)) and must still hold at n = 1 (SPEC §5.4)."""
    case = synth.single_person(k=k, n_desks=k + spare_desks, seed=f"one-k{k}-s{spare_desks}")
    config, build = build_from_world(case.world)
    problem = build.problem
    assert problem.n_people == 1

    solution = solve_mod.solve(problem, config.scoring.tie_break_seed)
    invariants.all(problem, solution)

    (assignment,) = solution.assignments
    submitted = case.world.latest_choices()[assignment.email]
    assert assignment.desk_id == submitted[0], "with no competition, rank 1 is available"
    assert assignment.rank_received == 1
    assert solution.total_points_scaled == int_curve_of(config)[0]
    assert solution.rank_histogram() == (1,) + (0,) * (k - 1)


# ==========================================================================
# Duplicate submissions
# ==========================================================================


@pytest.mark.parametrize("k", SMALL_K)
@pytest.mark.parametrize("n_people, extra", ((3, 1), (6, 3)))
def test_latest_submission_wins_even_when_it_is_not_the_last_row(
    k, n_people, extra, world_config_dir
):
    """SPEC §3.2: latest per email by timestamp, ties broken by later file row.

    The rows are shuffled, so file order is *not* timestamp order and a loader
    that simply kept the last row it saw would get this wrong. `expected_latest`
    is the generator's independently-computed answer key.
    """
    # Several seeds, because "the winner is not the last row" is a property of
    # the shuffle: every seed must resolve correctly, and at least one must
    # actually exercise the out-of-order branch, or a loader that just kept the
    # last row it saw would slip through.
    seeds = tuple(f"dup-k{k}-n{n_people}-e{extra}-s{i}" for i in range(6))
    out_of_order_seeds: list[str] = []

    for seed in seeds:
        world = synth.duplicate_submissions(
            n_people=n_people, k=k, extra=extra, seed=seed
        ).world
        config_dir, responses_path = world_config_dir(world)
        config = load_config(config_dir)
        loaded = responses_mod.load_responses(str(responses_path), config.k)

        assert len(loaded.submissions) == n_people * (1 + extra)
        assert len(loaded.latest) == n_people

        # WHICH row won, by id and by content -- not merely "one of them".
        assert {email: sub.submission_id for email, sub in loaded.latest.items()} == dict(
            world.expected_latest
        ), f"{seed}: resolved a different submission than the generator's answer key"
        assert {email: sub.choices for email, sub in loaded.latest.items()} == dict(
            world.latest_choices()
        )
        assert len(loaded.superseded) == n_people * extra

        # Timestamps are compared as parsed datetimes, not as strings: the
        # ordering rule is chronological, and asserting on text would quietly
        # start testing the format instead.
        def sort_key(submission):
            return (dt.datetime.fromisoformat(submission.timestamp), submission.file_row)

        rows_by_email: dict[str, list[int]] = {}
        keys: dict[str, list[tuple[dt.datetime, int]]] = {}
        for submission in loaded.submissions:
            rows_by_email.setdefault(submission.email, []).append(submission.file_row)
            keys.setdefault(submission.email, []).append(sort_key(submission))

        for email, winner in loaded.latest.items():
            assert sort_key(winner) == max(keys[email]), (
                f"{seed}/{email}: the winner is not the max by (timestamp, file row)"
            )
            if winner.file_row != max(rows_by_email[email]):
                out_of_order_seeds.append(seed)

        for email in loaded.latest:
            find_warning(loaded.warnings, email, "submitted", "superseded")

    assert out_of_order_seeds, (
        f"none of {seeds} produced a winning row sitting ABOVE a superseded one, so "
        f"'take the last row for this email' would have passed this test"
    )


@pytest.mark.parametrize("k", SMALL_K)
def test_a_timestamp_tie_is_broken_by_the_later_file_position(k, response_file):
    """The tie-break branch, isolated: identical timestamps, decided by position.

    Also covers the mirror case -- the strictly-latest timestamp sitting earlier
    in the file than a superseded row -- so passing requires the loader to sort
    on (timestamp, file_row) and not on either alone.
    """
    desks = [f"D{i + 1:02d}" for i in range(2 * k)]
    tied = "2026-09-15T09:00:00-04:00"
    later = "2026-09-15T17:00:00-04:00"

    rows = [
        # tie@umich: same timestamp twice; the LOWER row in the file must win.
        submission_row(
            submission_id="tie-first", timestamp=tied, email="tie@umich.edu",
            choices=desks[:k],
        ),
        # out-of-order@umich: the winner is written FIRST, superseded row after.
        submission_row(
            submission_id="oo-winner", timestamp=later, email="out-of-order@umich.edu",
            choices=desks[:k],
        ),
        submission_row(
            submission_id="oo-loser", timestamp=tied, email="out-of-order@umich.edu",
            choices=desks[k:],
        ),
        submission_row(
            submission_id="tie-second", timestamp=tied, email="tie@umich.edu",
            choices=desks[k:],
        ),
    ]
    loaded = responses_mod.load_responses(str(response_file(rows, k=k)), k)

    assert loaded.latest["tie@umich.edu"].submission_id == "tie-second", (
        "an exact timestamp tie is broken by the LATER file position (SPEC §3.2)"
    )
    assert loaded.latest["tie@umich.edu"].choices == tuple(desks[k:])
    assert loaded.latest["out-of-order@umich.edu"].submission_id == "oo-winner", (
        "the latest timestamp wins even when it appears earlier in the file"
    )
    assert loaded.latest["out-of-order@umich.edu"].choices == tuple(desks[:k])
    assert {s.submission_id for s in loaded.superseded} == {"tie-first", "oo-loser"}


@pytest.mark.parametrize("k", SMALL_K)
def test_a_repeated_submission_id_is_an_error_naming_both_rows(k, response_file):
    """`submission_id` is the key superseded-row tracking hangs off (SPEC §3.1)."""
    desks = [f"D{i + 1:02d}" for i in range(2 * k)]
    rows = [
        submission_row(
            submission_id="same-id", timestamp="2026-09-15T09:00:00-04:00",
            email="a@umich.edu", choices=desks[:k],
        ),
        submission_row(
            submission_id="same-id", timestamp="2026-09-15T10:00:00-04:00",
            email="b@umich.edu", choices=desks[k:],
        ),
    ]
    problems = rejected_response_file(response_file(rows, k=k), k)
    complaint = find_problem(problems, where="b@umich.edu", what="'same-id'")
    assert "already" in complaint.what


# ==========================================================================
# Stale desk references
# ==========================================================================


@pytest.mark.parametrize("k", SMALL_K)
@pytest.mark.parametrize("n_people", (8, 12))
def test_stale_desk_choices_are_dropped_with_a_warning_naming_the_desk(
    k, n_people, world_config_dir, invariants
):
    """A desk that has left the pool costs that one choice, never the run.

    Three ways a desk can be gone -- held by a keeper, `"available": false`, and
    an id that is in no rooms.json at all -- and all three must be a *dropped
    choice with a reason*, not a KeyError.
    """
    case = synth.stale_desk_reference(
        n_people=n_people, k=k, seed=f"stale-k{k}-n{n_people}"
    )
    config, build = build_from_world_via(world_config_dir, case.world)
    problem = build.problem

    assert build.dropped_choices, "fixture premise: something must have been dropped"
    roster = set(config.roster.emails)
    known_desks = {desk.id for desk in config.rooms.all_desks}
    pool = set(problem.desks)

    rendered = build.render_warnings()
    column_of = {desk: j for j, desk in enumerate(problem.desks)}
    for email, desk_id, reason in build.dropped_choices:
        assert email in roster, "a dropped choice must name a real person"
        assert desk_id, "a dropped choice must name the desk"
        assert desk_id not in pool, "only out-of-pool desks may be dropped"
        assert reason.strip(), f"{email}/{desk_id} was dropped without saying why"
        assert email in rendered and desk_id in rendered, (
            "the warning the coordinator reads must name both"
        )
        # Whatever the id was, it never reaches the matrix.
        assert desk_id not in column_of
    assert pool <= known_desks

    with only_deskmatch_errors("solving after stale choices were dropped"):
        try:
            solution = solve_mod.solve(problem, config.scoring.tie_break_seed)
        except InfeasibleError as exc:
            assert exc.exit_code == 2
            return
    invariants.all(problem, solution)


def rewrite_choices(responses_path: Path, email: str, desks) -> None:
    """Replace one person's ranked desks in a response file, in place."""
    rows = list(csv.reader(io.StringIO(responses_path.read_text(encoding="utf-8"))))
    header, data = rows[0], [dict(zip(rows[0], row)) for row in rows[1:]]
    hit = 0
    for row in data:
        if row["email"] == email:
            hit += 1
            for rank, desk in enumerate(desks, start=1):
                row[f"choice_{rank}"] = desk
    assert hit, f"{email} has no rows in {responses_path}"
    write_text(responses_path, dump_csv_text(header, data))


@pytest.mark.parametrize("k", SMALL_K)
@pytest.mark.parametrize("n_people", (6, 11))
@pytest.mark.parametrize("flavour", ("nonexistent", "locked", "unavailable"))
def test_losing_every_choice_to_stale_desks_is_an_error_naming_the_person(
    k, n_people, flavour, world_config_dir
):
    """SPEC §3.4: dropping all K choices is an error, and it names who.

    All three ways a desk can leave the pool are exercised, because they take
    different branches in `problem.build_problem` and only one of them (an
    unknown id) would show up as a KeyError if it were unguarded.
    """
    world = synth.generate(
        n_people,
        n_desks=n_people + 3 * k,
        k=k,
        seed=f"starve-{flavour}-k{k}-n{n_people}",
        n_zones=1,
        eligibility_style="flat",
        n_keepers=k if flavour == "locked" else 0,
        unavailable_frac=(3 * k / (n_people + 3 * k)) if flavour == "unavailable" else 0.0,
    )
    config_dir, responses_path = world_config_dir(world)
    responses_path = Path(responses_path)
    config = load_config(config_dir)

    if flavour == "nonexistent":
        known = {desk.id for desk in config.rooms.all_desks}
        gone = [f"GHOST{i:03d}" for i in range(k)]
        assert not (set(gone) & known)
    elif flavour == "locked":
        gone = sorted(
            person.current_desk
            for person in config.roster.people
            if person.keeps_desk and person.current_desk
        )[:k]
    else:
        gone = sorted(d.id for d in config.rooms.all_desks if not d.available)[:k]
    assert len(gone) == k, f"fixture premise: need {k} out-of-pool desks, got {gone}"

    loaded = responses_mod.load_responses(str(responses_path), config.k)
    in_the_pool = sorted(
        email
        for email in loaded.latest
        if not config.roster.by_email(email).keeps_desk
    )
    assert in_the_pool, "fixture premise: somebody has to be left in the pool"
    victim = in_the_pool[0]
    rewrite_choices(responses_path, victim, gone)
    loaded = responses_mod.load_responses(str(responses_path), config.k)
    assert loaded.latest[victim].choices == tuple(gone)

    person = config.roster.by_email(victim)
    assert person is not None and not person.keeps_desk

    with only_deskmatch_errors("building a problem where someone lost every choice"):
        with pytest.raises(ResponseError) as excinfo:
            problem_mod.build_problem(config, loaded)

    assert excinfo.value.exit_code == 4
    complaint = find_problem(excinfo.value.problems, what="no valid choices")
    assert victim in complaint.where, "the message must name the person by email"
    assert person.name in complaint.where, "and by name -- the coordinator knows names"
    assert complaint.hint, "and must say what they have to do about it"


# ==========================================================================
# Keepers
# ==========================================================================


@pytest.mark.parametrize("k", SMALL_K)
@pytest.mark.parametrize("n_people", (2, 5))
def test_when_everyone_keeps_their_desk_nobody_is_in_the_pool(
    k, n_people, world_config_dir
):
    """Both sides of the problem go to zero at once (SPEC §3.4).

    A header-only response file is a legitimate consequence of this shape; the
    loader is entitled to reject it, so long as it says so readably. Either way
    the *solve* must reach an empty assignment rather than a traceback.
    """
    case = synth.all_keepers(n_people=n_people, k=k, seed=f"keep-k{k}-n{n_people}")
    config_dir, responses_path = world_config_dir(case.world)
    config = load_config(config_dir)

    assert all(person.keeps_desk for person in config.roster.people)
    held = {person.current_desk for person in config.roster.people}
    assert None not in held, "keeps_desk truthy requires current_desk (SPEC §2.3)"

    with only_deskmatch_errors("loading a response file with no submissions"):
        try:
            loaded = responses_mod.load_responses(str(responses_path), config.k)
        except ResponseError as exc:
            assert exc.exit_code == 4
            find_problem(exc.problems, where=str(responses_path), what="no submission rows")
            loaded = empty_responses(config.k, str(responses_path))

        build = problem_mod.build_problem(config, loaded)
        solution = solve_mod.solve(build.problem, config.scoring.tie_break_seed)

    assert build.problem.n_people == 0, "a keeper is not in the pool"
    assert {desk for desk, _email in build.locked_desks} == held
    assert not (set(build.problem.desks) & held), "held desks are out of the pool"
    assert solution.assignments == ()
    assert solution.rank_histogram() == (0,) * k
    for _desk, email in build.locked_desks:
        assert email not in build.problem.people


@pytest.mark.parametrize("k", SMALL_K)
@pytest.mark.parametrize("n_people", (2, 5))
def test_an_empty_desk_pool_is_a_readable_error(k, n_people, world_config_dir):
    """Every desk locked by a keeper: the pool is empty and the run must say so
    in numbers, rather than solving a 0-column matrix and calling it a success."""
    world = synth.generate(
        n_people,
        n_desks=n_people,          # exactly enough desks for the keepers, none spare
        k=k,
        seed=f"nopool-k{k}-n{n_people}",
        n_zones=1,
        eligibility_style="flat",
        n_keepers=n_people,
    )
    config_dir, _responses = world_config_dir(world)
    config = load_config(config_dir)
    assert len(config.rooms.all_desks) == n_people

    with only_deskmatch_errors("building a problem with no desks in the pool"):
        with pytest.raises(ResponseError) as excinfo:
            problem_mod.build_problem(config, empty_responses(config.k))

    assert excinfo.value.exit_code == 4
    complaint = find_problem(excinfo.value.problems, what="desk pool is empty")
    assert str(n_people) in complaint.hint, "say how the desks were accounted for"


# ==========================================================================
# Roster membership
# ==========================================================================


@pytest.mark.parametrize("k", SMALL_K)
@pytest.mark.parametrize("n_people", (3, 7))
def test_a_submission_from_someone_not_on_the_roster_is_an_error(
    k, n_people, world_config_dir
):
    """SPEC §3.3: membership is an ERROR, not a warning. Someone outside the
    department must not be able to walk into the pool."""
    world = synth.generate(
        n_people,
        n_desks=n_people + k,
        k=k,
        seed=f"intruder-k{k}-n{n_people}",
        n_zones=1,
        eligibility_style="flat",
        keeper_frac=0.0,
    )
    config_dir, responses_path = world_config_dir(world)
    config = load_config(config_dir)
    responses_path = Path(responses_path)

    rows = list(csv.reader(io.StringIO(responses_path.read_text(encoding="utf-8"))))
    header, data = rows[0], rows[1:]
    intruder = dict(zip(header, data[0]))
    intruder["email"] = "not.on.the.roster@umich.edu"
    intruder["submission_id"] = "intruder-row"
    data.append([intruder[column] for column in header])
    write_text(responses_path, dump_csv_text(header, [dict(zip(header, r)) for r in data]))

    loaded = responses_mod.load_responses(str(responses_path), config.k)
    assert "not.on.the.roster@umich.edu" in loaded.latest, (
        "the loader itself has no roster; membership is the problem builder's job"
    )

    with only_deskmatch_errors("building a problem with an off-roster submission"):
        with pytest.raises(ResponseError) as excinfo:
            problem_mod.build_problem(config, loaded)

    assert excinfo.value.exit_code == 4
    complaint = find_problem(excinfo.value.problems, what="not on the roster")
    assert "not.on.the.roster@umich.edu" in complaint.where
    assert str(responses_path) in complaint.where, "name the file it came from"
    assert "roster.csv" in complaint.hint


@pytest.mark.parametrize("k", SMALL_K)
@pytest.mark.parametrize("n_people, n_silent", ((5, 1), (9, 3)))
def test_a_roster_member_who_did_not_submit_is_a_warning_and_is_excluded(
    k, n_people, n_silent, world_config_dir, invariants
):
    """SPEC §3.3: no submission is a warning; they are excluded and listed."""
    world = synth.generate(
        n_people,
        n_desks=n_people + k,
        k=k,
        seed=f"silent-k{k}-n{n_people}",
        n_zones=1,
        eligibility_style="flat",
        keeper_frac=0.0,
    )
    config_dir, responses_path = world_config_dir(world)
    config = load_config(config_dir)
    responses_path = Path(responses_path)

    rows = list(csv.reader(io.StringIO(responses_path.read_text(encoding="utf-8"))))
    header, data = rows[0], rows[1:]
    kept = data[: len(data) - n_silent]
    dropped_emails = {row[header.index("email")] for row in data[len(data) - n_silent:]}
    write_text(responses_path, dump_csv_text(header, [dict(zip(header, r)) for r in kept]))

    loaded = responses_mod.load_responses(str(responses_path), config.k)
    with only_deskmatch_errors("building a problem with non-responders"):
        build = problem_mod.build_problem(config, loaded)

    assert build.problem.n_people == n_people - n_silent
    excluded = {person.email: person for person in build.excluded_people}
    assert set(excluded) == dropped_emails
    for email in dropped_emails:
        assert excluded[email].reason == "no submission"
        assert email not in build.problem.people
        find_warning(build.warnings, email, "did not submit", "excluded")

    # The rest of the run is unaffected: the remaining people still solve.
    solution = solve_mod.solve(build.problem, config.scoring.tie_break_seed)
    invariants.all(build.problem, solution)


# ==========================================================================
# K disagreement
# ==========================================================================


@pytest.mark.parametrize("k", SMALL_K)
@pytest.mark.parametrize("delta", (-1, 1, 2))
def test_k_in_the_header_must_match_the_scoring_curve(k, delta, response_file):
    """K is `len(curves[primary])` and is also counted from the header; if the
    two disagree the run stops and says which number came from which file."""
    file_k = k + delta
    if file_k < 1:
        pytest.skip("a response file with no choice columns is a different error")
    desks = [f"D{i + 1:02d}" for i in range(file_k)]
    rows = [
        submission_row(
            submission_id="s1",
            timestamp="2026-09-15T09:00:00-04:00",
            email="a@umich.edu",
            choices=desks,
        )
    ]
    path = response_file(rows, k=file_k)

    problems = rejected_response_file(path, k)
    complaint = find_problem(problems, where="header", what=f"declares K={file_k}")
    assert f"K={k} is required" in complaint.what
    assert "scoring.json" in complaint.hint, "say where the other number came from"
    assert str(path) in complaint.hint, "and name this file"


@pytest.mark.parametrize("k", SMALL_K)
@pytest.mark.parametrize("delta", (1, 2))
def test_k_disagreement_is_caught_again_when_the_problem_is_built(
    k, delta, world_config_dir
):
    """The same disagreement reached the other way round: a file loaded without
    a K (tooling with no config to hand) meeting a curve of a different length."""
    world = synth.generate(
        k + 2,
        n_desks=2 * (k + delta) + 2,
        k=k + delta,
        seed=f"kmix-{k}-{delta}",
        n_zones=1,
        eligibility_style="flat",
        keeper_frac=0.0,
    )
    config_dir, responses_path = world_config_dir(world)
    loaded = responses_mod.load_responses(str(responses_path), None)
    assert loaded.k == k + delta

    # Shorten every curve to K, leaving the response file at K + delta.
    scoring_doc = dict(world.scoring)
    scoring_doc["curves"] = {
        name: list(values)[:k] for name, values in world.scoring["curves"].items()
    }
    write_text(
        Path(config_dir) / "scoring.json",
        json.dumps(scoring_doc, indent=2, sort_keys=True) + "\n",
    )
    config = load_config(config_dir)
    assert config.k == k

    with only_deskmatch_errors("building a problem whose K disagrees with the curve"):
        with pytest.raises(ResponseError) as excinfo:
            problem_mod.build_problem(config, loaded)

    assert excinfo.value.exit_code == 4
    complaint = find_problem(excinfo.value.problems, what="choice columns")
    assert f"has {k + delta} choice columns" in complaint.what
    assert f"has {k} entries" in complaint.what
    assert config.scoring.primary_curve in complaint.what


# ==========================================================================
# Malformed rows
# ==========================================================================


@pytest.mark.parametrize("k", SMALL_K)
# `year` is deliberately absent from this list: it is collected but optional
# (candidacy alone decides zones), and test_an_absent_year_column_is_accepted
# covers a file without it.
@pytest.mark.parametrize("column", ("submission_id", "timestamp", "email", "candidacy"))
def test_a_missing_required_column_is_reported_once_against_the_header(
    k, column, response_file
):
    """One header complaint, not one per row: sixty copies of the same line is
    how the single useful message gets buried."""
    desks = [f"D{i + 1:02d}" for i in range(k)]
    header = [c for c in response_header(k) if c != column]
    rows = [
        submission_row(
            submission_id=f"s{i}",
            timestamp=f"2026-09-15T09:0{i}:00-04:00",
            email=f"p{i}@umich.edu",
            choices=desks,
        )
        for i in range(3)
    ]
    problems = rejected_response_file(response_file(rows, header=header), k)
    complaint = find_problem(problems, where="header", what=f"required column '{column}' is missing")
    assert "SPEC" in complaint.hint or "Required columns" in complaint.hint


@pytest.mark.parametrize("k", (2, 3, 5))
def test_a_desk_ranked_twice_in_one_submission_is_an_error(k, response_file):
    """SPEC §3.2: the K choices must be distinct."""
    desks = [f"D{i + 1:02d}" for i in range(k)]
    duplicated = list(desks)
    duplicated[-1] = duplicated[0]
    rows = [
        submission_row(
            submission_id="dup",
            timestamp="2026-09-15T09:00:00-04:00",
            email="twice@umich.edu",
            choices=duplicated,
        )
    ]
    problems = rejected_response_file(response_file(rows, k=k), k)
    complaint = find_problem(problems, where="twice@umich.edu", what=f"desk '{desks[0]}'")
    assert "ranked twice" in complaint.what
    assert "choice_1" in complaint.what and f"choice_{k}" in complaint.what
    assert "distinct" in complaint.hint


@pytest.mark.parametrize("k", (2, 3, 5))
@pytest.mark.parametrize("blank_rank", (1, -1))
def test_a_blank_desk_id_is_an_error_naming_the_column(k, blank_rank, response_file):
    rank = blank_rank if blank_rank > 0 else k
    desks = [f"D{i + 1:02d}" for i in range(k)]
    desks[rank - 1] = "   "
    rows = [
        submission_row(
            submission_id="blank",
            timestamp="2026-09-15T09:00:00-04:00",
            email="blank@umich.edu",
            choices=desks,
        )
    ]
    problems = rejected_response_file(response_file(rows, k=k), k)
    complaint = find_problem(problems, where="blank@umich.edu", what=f"'choice_{rank}' is empty")
    assert f"K={k}" in complaint.hint, "the hint must quote the K it derived"


@pytest.mark.parametrize("k", SMALL_K)
@pytest.mark.parametrize("year", ("third", "3.5", "N/A"))
def test_a_non_integer_year_is_a_warning_not_an_error(k, year, response_file):
    """`year` is recorded, not acted on, so a bad one must not stop the run.

    The form collects a year again, but candidacy alone decides zones, so an
    unparseable value -- in a legacy export or in a box somebody typed into --
    cannot change anyone's assignment. Refusing to seat the whole department over
    a field that cannot affect the outcome would be the wrong trade, but silently
    swallowing it would leave the coordinator with a mystery, so it warns and
    quotes the value.
    """
    desks = [f"D{i + 1:02d}" for i in range(k)]
    rows = [
        submission_row(
            submission_id="badyear",
            timestamp="2026-09-15T09:00:00-04:00",
            email="year@umich.edu",
            choices=desks,
            year=year,
        )
    ]
    loaded = responses_mod.load_responses(response_file(rows, k=k), k)
    assert len(loaded.latest) == 1, "the row must still load"
    assert loaded.latest["year@umich.edu"].year == 0, "an unusable year records as 0"
    blob = " ".join(loaded.warnings)
    assert "year" in blob and repr(year) in blob, (
        f"the warning must quote the offending value; got: {loaded.warnings}"
    )


@pytest.mark.parametrize("k", SMALL_K)
def test_an_absent_year_column_is_accepted(k, response_file):
    """An export from the cycle that did not collect `year` has no such column,
    and must still load. The column is optional in both directions."""
    desks = [f"D{i + 1:02d}" for i in range(k)]
    rows = [
        submission_row(
            submission_id="noyear",
            timestamp="2026-09-15T09:00:00-04:00",
            email="noyear@umich.edu",
            choices=desks,
        )
    ]
    path = response_file(rows, k=k)
    text = path.read_text(encoding="utf-8")
    header, *body = text.splitlines()
    cols = header.split(",")
    if "year" in cols:
        drop = cols.index("year")
        keep = lambda row: ",".join(
            v for i, v in enumerate(row.split(",")) if i != drop
        )
        path.write_text(
            "\n".join([keep(header), *(keep(r) for r in body)]) + "\n",
            encoding="utf-8",
        )
    loaded = responses_mod.load_responses(path, k)
    assert len(loaded.latest) == 1
    assert loaded.latest["noyear@umich.edu"].year == 0


@pytest.mark.parametrize("k", SMALL_K)
def test_a_submitted_year_cannot_change_anybodys_zones(k, config_case):
    """The year is collected and recorded. It must decide nothing. Ever.

    Today's `eligibility.json` keys on candidacy alone, which makes this
    property invisible: any year at all produces the same zones because no rule
    reads one. That is precisely why it needs a test -- the predicate grammar
    (SPEC §2.2) accepts `{"year": [1, 2]}`, so the invariant is one rule away
    from being load-bearing, and the failure it would produce is silent. The
    form tells the student that their year is recorded and does not change which
    desks they may rank; if a submitted year reached the rule table, that
    sentence would become false and people would be moved between zones by a box
    that promised not to.

    So: install a rule that *does* read `year`, then falsify every submitted
    year and demand that not one person's allowed zones move. The roster's value
    is the one that counts (SPEC §3.3), and the disagreement is still reported.

    This is a regression test. Before it existed, `_effective_person` applied the
    submitted year to the Person, and under this rule table falsifying the
    responses moved most of the department.
    """
    from dataclasses import replace as _replace

    from deskmatch import eligibility as elig_mod

    case = config_case(n_people=8, k=k)
    # A year-keyed rule, first, so it wins wherever it matches.
    case.eligibility["rules"].insert(
        0,
        {
            "id": "years_1_2_first_zone_only",
            "when": {"year": [1, 2]},
            "allow_zones": [case.zone_ids[0]],
            "reason": "Test rule: the first two years sit in one zone.",
        },
    )
    config = case.load()

    def zones_of(person):
        return elig_mod.allowed_zones(config.eligibility, config.rooms, person)

    # Non-vacuity: the rule table really is year-sensitive, so an *applied* year
    # would show up. Without this the test could pass by doing nothing.
    moved = [
        p.email
        for p in config.roster.people
        if zones_of(p)
        != zones_of(
            _replace(p, year=1, attributes={**dict(p.attributes), "year": 1})
        )
    ]
    assert moved, "the test rule never bites; the assertion below would be vacuous"

    columns, body = _read_response_rows(case.responses_path)
    assert "year" in columns, "the fixture's response file must carry a year column"

    def zones_from(rows, name: str) -> dict[str, tuple[str, ...]]:
        path = case.path / name
        write_text(path, dump_csv_text(columns, rows))
        built = problem_mod.build_problem(
            config, responses_mod.load_responses(path, k)
        )
        return {
            email: zones_of(person)
            for email, person in built.effective_people.items()
        }

    honest = zones_from(body, "responses_honest.csv")

    for index, claimed in enumerate(("1", "2", "9", "", "not a year")):
        lied = zones_from(
            [{**row, "year": claimed} for row in body],
            f"responses_year_{index}.csv",
        )
        assert lied == honest, (
            f"submitting year={claimed!r} changed somebody's allowed zones. The "
            f"year is recorded, never an input to eligibility (SPEC §3.1, §3.3); "
            f"candidacy alone decides where people may sit."
        )


@pytest.mark.parametrize("k", SMALL_K)
def test_a_year_disagreement_is_reported_but_not_applied(k, config_case):
    """Reported, so the coordinator can fix roster.csv; not applied, so it
    cannot move anybody. Both halves matter, and only together."""
    case = config_case(n_people=6, k=k)
    config = case.load()

    columns, body = _read_response_rows(case.responses_path)
    rows = []
    expected: dict[str, int] = {}
    for row in body:
        person = config.roster.by_email(row["email"].strip().lower())
        expected[person.email] = person.year
        # Nobody is ten years older than the roster thinks.
        rows.append({**row, "year": str(person.year + 10)})

    path = case.path / "responses_yearlie.csv"
    write_text(path, dump_csv_text(columns, rows))
    built = problem_mod.build_problem(config, responses_mod.load_responses(path, k))

    year_conflicts = [c for c in built.roster_conflicts if c.field == "year"]
    assert year_conflicts, "a year disagreement must still reach the coordinator"
    for conflict in year_conflicts:
        assert conflict.submitted_value == expected[conflict.email] + 10
        assert conflict.roster_value == expected[conflict.email]
        assert not conflict.applied, "a year conflict must not be applied"
        assert "roster value" in conflict.render()

    for email, person in built.effective_people.items():
        assert person.year == expected[email], (
            f"{email}: the roster's year must survive a submitted one"
        )
        assert person.attributes["year"] == expected[email], (
            f"{email}: the attributes the rule table sees must carry the "
            f"roster's year, not the submitted one"
        )


@pytest.mark.parametrize("k", SMALL_K)
@pytest.mark.parametrize("stamp", ("last tuesday", "", "2026-13-45T99:99:99"))
def test_an_unparseable_timestamp_is_an_error_naming_the_row(k, stamp, response_file):
    """Re-submission ordering depends on the timestamp; a bad one stops the run."""
    desks = [f"D{i + 1:02d}" for i in range(k)]
    rows = [
        submission_row(
            submission_id="badts",
            timestamp=stamp,
            email="stamp@umich.edu",
            choices=desks,
        )
    ]
    problems = rejected_response_file(response_file(rows, k=k), k)
    complaint = find_problem(problems, where="stamp@umich.edu", what="'timestamp'")
    assert "ordering" in complaint.hint
    if stamp.strip():
        assert "not a recognisable timestamp" in complaint.what
    else:
        assert "empty" in complaint.what


@pytest.mark.parametrize("k", SMALL_K)
@pytest.mark.parametrize("missing_fields", (1, 3))
def test_a_row_with_too_few_fields_is_an_error_naming_the_line(
    k, missing_fields, tmp_path
):
    """A ragged row cannot go through the DictWriter, so it is written by hand --
    which is exactly how a coordinator produces one."""
    header = list(response_header(k))
    good = submission_row(
        submission_id="ok",
        timestamp="2026-09-15T09:00:00-04:00",
        email="ok@umich.edu",
        choices=[f"D{i + 1:02d}" for i in range(k)],
    )
    lines = [",".join(header), ",".join(good.get(column, "") for column in header)]
    lines.append(",".join(["x"] * (len(header) - missing_fields)))
    path = write_text(tmp_path / f"ragged{k}_{missing_fields}.csv", "\n".join(lines) + "\n")

    problems = rejected_response_file(path, k)
    complaint = find_problem(problems, where="line 3", what="fields but the header has")
    assert f"has {len(header) - missing_fields} fields" in complaint.what
    assert str(len(header)) in complaint.what
    assert str(path) in complaint.where


@pytest.mark.parametrize("k", SMALL_K)
def test_a_blank_row_is_skipped_with_a_warning_not_an_error(k, tmp_path):
    """Spreadsheets grow blank rows. Refusing to run over one would be absurd,
    and silently eating one would be worse."""
    header = list(response_header(k))
    good = submission_row(
        submission_id="ok",
        timestamp="2026-09-15T09:00:00-04:00",
        email="ok@umich.edu",
        choices=[f"D{i + 1:02d}" for i in range(k)],
    )
    lines = [
        ",".join(header),
        ",".join(good.get(column, "") for column in header),
        "," * (len(header) - 1),
    ]
    path = write_text(tmp_path / f"blankrow{k}.csv", "\n".join(lines) + "\n")

    loaded = responses_mod.load_responses(str(path), k)
    assert len(loaded.submissions) == 1
    find_warning(loaded.warnings, "line 3", "entirely blank")
