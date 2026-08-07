"""`deskmatch.synth` — the generator the rest of the suite is built on.

Everything else in this repository is tested against what this module emits, so
if the generator is wrong, every test that uses it is quietly wrong too. Three
properties therefore have to hold, and they are what this file asserts:

1. **It is deterministic** (invariant I3). Same arguments ⇒ byte-identical
   files, including the floor-plan PNGs. If the generator drifted, a "regression"
   in a downstream test would really be a change of input.

2. **What it emits is what the real loaders accept.** A generator that produced
   *almost* valid config would make the whole suite an exercise in testing a
   parallel universe. So the output goes through `config.load_config` and
   `responses.load_responses` — the real ones — and must come back with zero
   warnings, not merely zero errors.

3. **The `concentration` knob means what the docstring says it means.** It is
   documented as *literally* the between-person correlation of latent desk
   utility, with a closed form `E[tau] = (2/pi)·arcsin(c)` for Gaussian noise.
   That is a falsifiable claim about a number, so it is measured rather than
   assumed: `kendall_tau_table` is run across the range and checked for
   monotonicity, agreement with the closed form, and exactness at both endpoints.

Sizes are parameters throughout (invariant I1): N ∈ {1, 5, 35, 200} and
K ∈ {3, 5, 8} are swept, and nothing in here contains a literal 35.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import pytest

from conftest import find_warning
from deskmatch import responses as responses_mod
from deskmatch import synth, validate
from deskmatch.config import load_config

# --------------------------------------------------------------------------
# The size sweep. Nothing below hard-codes a dimension.
# --------------------------------------------------------------------------

POPULATIONS = [1, 5, 35, 200]
K_VALUES = [3, 5, 8]

#: Wall-clock budget for generate + write + load at the largest size in the
#: sweep. Measured cost is ~0.1 s; this is ~50x headroom, loose enough never to
#: flake on a loaded machine and tight enough that an accidental O(N^3) in the
#: generator or the validator cannot slip through.
TIME_BUDGET_SECONDS = 5.0

#: A small floor plan: the PNGs exist so `rooms.json:image` resolves, and their
#: content is never asserted on, so there is no reason to render a megapixel.
SMALL_IMAGE = (240, 160)


def files_under(root: Path) -> dict[str, bytes]:
    """Every file below `root`, keyed by POSIX-relative path. Sorted, so the
    comparison itself cannot depend on directory iteration order."""
    return {
        str(path.relative_to(root).as_posix()): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ==========================================================================
# 1. Determinism
# ==========================================================================


class TestDeterminism:
    @pytest.mark.parametrize("n_people", [1, 5, 35])
    @pytest.mark.parametrize("k", K_VALUES)
    def test_same_seed_gives_identical_text(self, n_people: int, k: int):
        kwargs = dict(k=k, seed="repeatable", image_size=SMALL_IMAGE)
        first = synth.generate(n_people, **kwargs)
        second = synth.generate(n_people, **kwargs)
        assert first.file_texts() == second.file_texts()

    @pytest.mark.parametrize("n_people,k", [(5, 3), (35, 5), (120, 8)])
    def test_same_seed_gives_byte_identical_files_on_disk(
        self, tmp_path, n_people: int, k: int
    ):
        """Including the PNGs: `write()` is the whole artefact, not just the JSON."""
        kwargs = dict(k=k, seed=f"bytes-{n_people}-{k}", image_size=SMALL_IMAGE)
        synth.generate(n_people, **kwargs).write(tmp_path / "a")
        synth.generate(n_people, **kwargs).write(tmp_path / "b")

        left, right = files_under(tmp_path / "a"), files_under(tmp_path / "b")
        assert sorted(left) == sorted(right)
        differing = [name for name in left if left[name] != right[name]]
        assert not differing, f"these files differ between two identical runs: {differing}"

    def test_an_integer_seed_and_its_string_form_are_different_worlds(self):
        """`seed=0` and `seed="0"` must not collide by accident."""
        as_int = synth.generate(8, k=5, seed=0, image_size=SMALL_IMAGE)
        as_str = synth.generate(8, k=5, seed="0", image_size=SMALL_IMAGE)
        assert as_int.file_texts()["responses.csv"] != as_str.file_texts()["responses.csv"]

    def test_a_different_seed_gives_a_different_world(self):
        first = synth.generate(20, k=5, seed="seed-a", image_size=SMALL_IMAGE)
        second = synth.generate(20, k=5, seed="seed-b", image_size=SMALL_IMAGE)
        assert first.file_texts()["responses.csv"] != second.file_texts()["responses.csv"]
        assert first.file_texts()["roster.csv"] != second.file_texts()["roster.csv"]
        # ...but the *shape* is unchanged: only the draws moved.
        assert first.n_people == second.n_people
        assert first.n_desks == second.n_desks

    def test_seeding_uses_sha256_not_the_builtin_hash(self):
        """`hash()` is salted per interpreter start; using it would be fatal (§5.5)."""
        import hashlib

        expected = int.from_bytes(
            hashlib.sha256("astro-2026".encode("utf-8")).digest()[:8], "big"
        )
        assert synth.seed_int("astro-2026") == expected
        assert synth.seed_int(1234) == 1234

    def test_the_clock_is_never_read(self):
        """Submission timestamps are a fixed base date plus fixed offsets."""
        world = synth.generate(6, k=3, seed="clock", image_size=SMALL_IMAGE)
        stamps = [row["timestamp"] for row in world.response_rows]
        assert stamps and all(s.startswith("2026-09-15T") for s in stamps)
        assert world.scoring["seed_committed_at"].startswith("2026-09-01T")

    def test_shuffled_rows_are_still_deterministic(self):
        kwargs = dict(
            k=5, seed="shuffled", shuffle_rows=True, resubmit_frac=1.0,
            extra_submissions=2, image_size=SMALL_IMAGE,
        )
        first = synth.generate(12, **kwargs)
        second = synth.generate(12, **kwargs)
        assert first.file_texts() == second.file_texts()
        assert first.expected_latest == second.expected_latest


# ==========================================================================
# 2. The real loaders accept it, cleanly
# ==========================================================================


class TestRealLoaderAcceptance:
    @pytest.mark.parametrize("n_people", POPULATIONS)
    @pytest.mark.parametrize("k", K_VALUES)
    @pytest.mark.parametrize("style", ["flat", "cohort"])
    def test_generated_config_loads_with_no_warnings(
        self, tmp_path, n_people: int, k: int, style: str
    ):
        world = synth.generate(
            n_people, k=k, seed=f"clean-{n_people}-{k}-{style}",
            eligibility_style=style, image_size=SMALL_IMAGE,
        )
        config_dir, responses_path = world.write(tmp_path / f"{style}{n_people}x{k}")

        config = load_config(config_dir)
        assert config.warnings == (), (
            "the generator's default output must be warning-free, otherwise a "
            "warning in a dry run means nothing:\n  " + "\n  ".join(config.warnings)
        )
        assert config.k == k
        assert len(config.roster.people) == n_people
        assert len(config.rooms.desk_ids) == world.n_desks

        responses = responses_mod.load_responses(str(responses_path), config.k)
        assert responses.warnings == (), "\n  ".join(responses.warnings)
        assert responses.k == k
        assert set(responses.latest) == set(world.pool_people)

    @pytest.mark.parametrize("coord_space", ["normalized", "pixels"])
    def test_both_coordinate_spaces_load(self, tmp_path, coord_space: str):
        world = synth.generate(
            12, k=5, seed=f"coords-{coord_space}", coord_space=coord_space,
            image_size=SMALL_IMAGE,
        )
        config_dir, _ = world.write(tmp_path / coord_space)
        config = load_config(config_dir)
        assert config.rooms.coord_space == coord_space
        assert config.warnings == ()

    def test_extra_roster_column_survives_the_real_loader(self, tmp_path):
        world = synth.generate(10, k=3, seed="extras", extra_columns=True,
                               image_size=SMALL_IMAGE)
        config_dir, _ = world.write(tmp_path)
        config = load_config(config_dir)
        assert all("advisor" in p.attributes for p in config.roster.people)

    def test_varied_boolean_tokens_are_all_understood(self, tmp_path):
        """SPEC §2.3's whole yes/no vocabulary, as emitted by the generator."""
        world = synth.generate(
            24, k=5, seed="tokens", vary_bool_tokens=True, keeper_frac=0.5,
            image_size=SMALL_IMAGE,
        )
        config_dir, _ = world.write(tmp_path)
        config = load_config(config_dir)
        keepers = {p.email for p in config.roster.people if p.keeps_desk}
        assert keepers == set(world.keeper_desks)
        assert config.warnings == ()

    def test_the_generators_latest_matches_the_real_loaders(self, tmp_path):
        """§3.2 is implemented twice; `expected_latest` is the answer key."""
        case = synth.duplicate_submissions(tmp_path, n_people=9, k=4, extra=3)
        config_dir, responses_path = case.as_paths()
        config = load_config(config_dir)
        loaded = responses_mod.load_responses(str(responses_path), config.k)
        resolved = {email: sub.submission_id for email, sub in loaded.latest.items()}
        assert resolved == dict(case.world.expected_latest)
        assert len(loaded.submissions) == len(case.world.response_rows)
        assert len(loaded.superseded) == len(case.world.response_rows) - 9

    def test_k_comes_from_a_supplied_scoring_document(self, tmp_path):
        """"When `scoring` is given, K comes from it, never from `k`."""
        scoring = synth.make_scoring(7, seed_string="k-from-scoring")
        world = synth.generate(6, k=2, scoring=scoring, seed="k-from-scoring",
                               image_size=SMALL_IMAGE)
        assert world.k == 7
        config_dir, responses_path = world.write(tmp_path)
        config = load_config(config_dir)
        assert config.k == 7
        assert responses_mod.load_responses(str(responses_path), 7).k == 7

    def test_the_real_config_can_be_reused_verbatim(self, tmp_path, real_config_dir):
        """`generate(rooms=…, scoring=…, roster_rows=…)` on the department's own files."""
        import csv

        rooms = json.loads((real_config_dir / "rooms.json").read_text())
        eligibility = json.loads((real_config_dir / "eligibility.json").read_text())
        scoring = json.loads((real_config_dir / "scoring.json").read_text())
        with open(real_config_dir / "roster.csv", newline="", encoding="utf-8") as handle:
            roster_rows = list(csv.DictReader(handle))
        if not roster_rows:
            # config/roster.csv ships EMPTY -- the domain-restricted link is the
            # membership check, so there is nobody to list. The point of this
            # test is that the department's own rooms / eligibility / scoring can
            # be handed to the generator verbatim, so supply people and keep
            # reusing the three files that matter.
            roster_rows = [
                {"name": f"Synthetic {i}", "email": f"synthetic{i}@umich.edu",
                 "year": str(1 + i % 5),
                 "candidacy": "precandidate" if i % 3 == 0 else "candidate",
                 "keeps_desk": "no", "current_desk": ""}
                for i in range(8)
            ]

        world = synth.generate(
            rooms=rooms, eligibility=eligibility, scoring=scoring,
            roster_rows=roster_rows, seed="reuse-real",
        )
        assert world.n_people == len(roster_rows)
        # Sum across every room: the real config has more than one, and
        # counting only the first silently under-tests the multi-room path.
        assert world.n_desks == sum(len(r["desks"]) for r in rooms["rooms"])
        assert world.k == len(scoring["curves"][scoring["primary_curve"]])

        config_dir, responses_path = world.write(tmp_path)
        config = load_config(config_dir)
        loaded = responses_mod.load_responses(str(responses_path), config.k)
        assert set(loaded.latest) <= set(config.roster.emails), (
            "a generated response must never invent an email the roster lacks"
        )


# ==========================================================================
# 3. Sizes and timings
# ==========================================================================


class TestSizesAndTimings:
    @pytest.mark.parametrize("n_people", POPULATIONS)
    @pytest.mark.parametrize("k", K_VALUES)
    def test_generate_write_and_load_stay_fast(self, tmp_path, n_people: int, k: int):
        start = time.perf_counter()
        world = synth.generate(
            n_people, k=k, seed=f"timing-{n_people}-{k}", image_size=SMALL_IMAGE
        )
        generated = time.perf_counter()
        config_dir, responses_path = world.write(tmp_path / f"n{n_people}k{k}")
        written = time.perf_counter()
        config = load_config(config_dir)
        loaded_config = time.perf_counter()
        responses = responses_mod.load_responses(str(responses_path), config.k)
        finished = time.perf_counter()

        timings = {
            "generate": generated - start,
            "write": written - generated,
            "load_config": loaded_config - written,
            "load_responses": finished - loaded_config,
        }
        assert finished - start < TIME_BUDGET_SECONDS, (
            f"N={n_people} K={k} took {finished - start:.2f}s "
            f"(budget {TIME_BUDGET_SECONDS}s): {timings}"
        )
        # ...and the run is real, not a fast no-op.
        assert len(config.roster.people) == n_people
        assert responses.k == k
        assert len(responses.submissions) == len(world.response_rows)

    @pytest.mark.parametrize("n_people", POPULATIONS)
    def test_desk_count_scales_with_the_population(self, n_people: int):
        """The default sizing must leave everyone K reachable desks."""
        world = synth.generate(n_people, k=5, seed=f"scale-{n_people}",
                               image_size=SMALL_IMAGE)
        assert world.n_desks >= n_people
        assert world.n_desks >= world.k * len(world.zone_ids)

    @pytest.mark.parametrize("n_rooms", [1, 2, 7])
    def test_desks_spread_over_several_rooms(self, tmp_path, n_rooms: int):
        world = synth.generate(
            40, k=5, seed=f"rooms-{n_rooms}", n_rooms=n_rooms, image_size=SMALL_IMAGE
        )
        assert len(world.rooms["rooms"]) == n_rooms
        config_dir, _ = world.write(tmp_path)
        config = load_config(config_dir)
        assert len(config.rooms.rooms) == n_rooms
        assert len(config.rooms.desk_ids) == len(set(config.rooms.desk_ids))

    @pytest.mark.parametrize("n_zones", [1, 2, 5])
    def test_zone_count_is_a_parameter(self, tmp_path, n_zones: int):
        world = synth.generate(
            30, k=3, seed=f"zones-{n_zones}", n_zones=n_zones, image_size=SMALL_IMAGE
        )
        assert len(world.zone_ids) == n_zones
        config_dir, _ = world.write(tmp_path)
        assert len(load_config(config_dir).rooms.zones) == n_zones


# ==========================================================================
# 4. rooms.json passes the real validator
# ==========================================================================


ROOM_SHAPES = [
    pytest.param(dict(n_desks=1, n_zones=1), id="1desk"),
    pytest.param(dict(n_desks=6, n_zones=2), id="6desks-2zones"),
    pytest.param(dict(n_desks=31, n_zones=2), id="31desks-like-the-real-one"),
    pytest.param(dict(n_desks=64, n_zones=4, n_rooms=3), id="64desks-3rooms"),
    pytest.param(dict(n_desks=225, n_zones=6, n_rooms=6), id="225desks-6rooms"),
    pytest.param(dict(n_desks=12, n_zones=3, coord_space="pixels"), id="pixel-space"),
    pytest.param(dict(n_desks=10, zone_sizes=(3, 7)), id="explicit-zone-sizes"),
    pytest.param(
        dict(n_desks=8, n_zones=2, image_size=(1920, 400)), id="wide-image"
    ),
]


class TestMakeRoomsAgainstTheRealValidator:
    @pytest.mark.parametrize("kwargs", ROOM_SHAPES)
    def test_generated_rooms_validate_clean(self, kwargs: dict):
        payload = synth.make_rooms(**kwargs)
        ctx = validate.validate_rooms(payload)  # config_dir=None: pure-data checks
        assert ctx.ok, "\n".join(p.render() for p in ctx.problems)
        assert ctx.warnings == [], "\n".join(w.render() for w in ctx.warnings)

    @pytest.mark.parametrize("kwargs", ROOM_SHAPES)
    def test_desk_ids_are_unique_and_every_zone_is_used(self, kwargs: dict):
        payload = synth.make_rooms(**kwargs)
        ids, zone_of = synth.rooms_desk_index(payload)
        assert len(ids) == len(set(ids)) == kwargs["n_desks"]
        declared = {z for z in payload["zones"] if not z.startswith("_")}
        assert set(zone_of.values()) == declared

    @pytest.mark.parametrize("kwargs", ROOM_SHAPES)
    def test_no_two_desks_overlap(self, kwargs: dict):
        """The grid layout must not produce the overlap warning at any size."""
        payload = synth.make_rooms(**kwargs)
        for room in payload["rooms"]:
            desks = room["desks"]
            for i in range(len(desks)):
                for j in range(i + 1, len(desks)):
                    hit, _fraction = validate.shapes_overlap(
                        "rect", desks[i]["shape"]["rect"],
                        "rect", desks[j]["shape"]["rect"],
                    )
                    assert not hit, f"{desks[i]['id']} overlaps {desks[j]['id']}"

    def test_unavailable_desks_are_marked_and_validate(self):
        payload = synth.make_rooms(10, n_zones=2, unavailable=["D03", "D07"])
        assert synth.rooms_unavailable(payload) == ("D03", "D07")
        assert validate.validate_rooms(payload).ok

    def test_generated_eligibility_and_scoring_validate_clean(self):
        payload = synth.make_rooms(24, n_zones=3)
        zone_ids = tuple(payload["zones"])
        columns = list(synth.ROSTER_BASE_FIELDS)
        for style in ("flat", "cohort"):
            ctx = validate.validate_eligibility(
                synth.make_eligibility(zone_ids, style=style), zone_ids, columns
            )
            assert ctx.ok, "\n".join(p.render() for p in ctx.problems)
            assert ctx.warnings == [], "\n".join(w.render() for w in ctx.warnings)
        for k in K_VALUES:
            ctx = validate.validate_scoring(synth.make_scoring(k, seed_string="ok-seed"))
            assert ctx.ok, "\n".join(p.render() for p in ctx.problems)
            assert ctx.warnings == [], "\n".join(w.render() for w in ctx.warnings)
            assert synth.scoring_k(synth.make_scoring(k, seed_string="ok-seed")) == k

    @pytest.mark.parametrize(
        "kwargs,message",
        [
            (dict(n_desks=-1), "n_desks must be >= 0"),
            (dict(n_desks=4, n_zones=9), "n_zones must be between 1 and n_desks"),
            (dict(n_desks=4, n_rooms=9), "n_rooms must be between 1 and n_desks"),
            (dict(n_desks=4, zone_sizes=(1, 1)), "zone_sizes must sum to n_desks"),
            (dict(n_desks=4, coord_space="polar"), "coord_space must be"),
            (dict(n_desks=4, image_size=(0, 10)), "image_size must be positive"),
        ],
    )
    def test_generator_misuse_is_a_valueerror_not_a_bad_file(self, kwargs, message):
        with pytest.raises(ValueError, match=message):
            synth.make_rooms(**kwargs)


# ==========================================================================
# 5. The concentration knob
# ==========================================================================

CONCENTRATIONS = (0.0, 0.25, 0.5, 0.75, 1.0)

#: Enough replicates that the empirical mean lands on the closed form, cheap
#: enough to run in a couple of seconds. Everything is seeded, so the numbers
#: below are fixed, not sampled afresh on every run.
TAU_PEOPLE = 30
TAU_DESKS = 30
TAU_K = 5
TAU_REPLICATES = 4
TAU_SEED = "tau-check"

#: Slack against the closed form `(2/pi)·arcsin(c)`. The observed worst-case
#: deviation at this seed and sample size is ~0.02; 0.05 leaves a wide margin
#: while still being far smaller than the ~0.16 gap between adjacent rows.
TAU_TOLERANCE = 0.05

#: Minimum increase demanded between adjacent rows. The smallest true gap in the
#: table is (2/pi)(arcsin 0.25 - arcsin 0) ≈ 0.161, so 0.05 is a real assertion
#: about monotonicity and not a restatement of "the numbers are not equal".
TAU_MIN_STEP = 0.05


@pytest.fixture(scope="module")
def tau_table():
    return synth.kendall_tau_table(
        concentrations=CONCENTRATIONS,
        n_people=TAU_PEOPLE,
        n_desks=TAU_DESKS,
        k=TAU_K,
        seed=TAU_SEED,
        replicates=TAU_REPLICATES,
    )


class TestConcentrationKnob:
    def test_table_is_reported(self, tau_table):
        """Print the measured table; `-s` shows it, and a failure shows it too."""
        print("\n" + synth.render_tau_table(tau_table))
        assert len(tau_table) == len(CONCENTRATIONS)

    def test_mean_kendall_tau_increases_monotonically(self, tau_table):
        steps = [
            (b.concentration, b.mean_tau - a.mean_tau)
            for a, b in zip(tau_table, tau_table[1:])
        ]
        assert all(step >= TAU_MIN_STEP for _c, step in steps), (
            "mean pairwise Kendall tau must increase with concentration:\n"
            + synth.render_tau_table(tau_table)
        )

    def test_top_k_overlap_increases_monotonically(self, tau_table):
        """The operationally meaningful measure: collisions inside the top K."""
        steps = [
            b.mean_topk_overlap - a.mean_topk_overlap
            for a, b in zip(tau_table, tau_table[1:])
        ]
        assert all(step > 0 for step in steps), synth.render_tau_table(tau_table)

    def test_zero_concentration_is_uncorrelated(self, tau_table):
        row = tau_table[0]
        assert row.concentration == 0.0
        assert abs(row.mean_tau) < TAU_TOLERANCE, (
            f"c=0 must give independent rankings, measured tau={row.mean_tau:.4f}"
        )
        # A random pair of top-K lists of size K out of n_desks overlaps by
        # K/n_desks on average; anything near 1 would mean the draws are shared.
        assert row.mean_topk_overlap == pytest.approx(TAU_K / TAU_DESKS, abs=0.05)

    def test_full_concentration_is_perfect_agreement(self, tau_table):
        row = tau_table[-1]
        assert row.concentration == 1.0
        assert row.mean_tau == pytest.approx(1.0, abs=1e-12)
        assert row.sd_tau == pytest.approx(0.0, abs=1e-12)
        assert row.mean_topk_overlap == pytest.approx(1.0, abs=1e-12)

    def test_the_closed_form_is_matched(self, tau_table):
        """`E[tau] = (2/pi)·arcsin(c)` — the reason Thurstone is the default."""
        for row in tau_table:
            predicted = (2.0 / math.pi) * math.asin(row.concentration)
            assert row.predicted_tau == pytest.approx(predicted)
            assert row.mean_tau == pytest.approx(predicted, abs=TAU_TOLERANCE), (
                f"c={row.concentration}: measured {row.mean_tau:.4f}, "
                f"closed form {predicted:.4f}\n" + synth.render_tau_table(tau_table)
            )

    def test_c_equals_one_gives_literally_identical_rankings(self):
        """Exact, not statistical: at c=1 every person's order is the same list."""
        for n_desks in (7, 40):
            rng = synth._rng(f"identical-{n_desks}")
            utilities = synth.latent_utilities(rng, 12, n_desks, 1.0)
            orders = synth.preference_orders(
                utilities, tuple(f"D{i:03d}" for i in range(n_desks))
            )
            assert len(set(orders)) == 1, "c=1 must give one shared ranking"

    def test_c_equals_zero_gives_distinct_rankings(self):
        rng = synth._rng("distinct")
        utilities = synth.latent_utilities(rng, 12, 30, 0.0)
        orders = synth.preference_orders(utilities, tuple(f"D{i:03d}" for i in range(30)))
        assert len(set(orders)) == 12, "independent draws must not collide"

    def test_the_rng_stream_position_does_not_depend_on_the_knob(self):
        """Draws happen unconditionally, so c can be changed in isolation."""
        drawn = []
        for c in (0.0, 0.5, 1.0):
            rng = synth._rng("stream")
            synth.latent_utilities(rng, 9, 20, c)
            drawn.append(rng.random())
        assert len(set(drawn)) == 1, "changing c must not shift the RNG stream"

    def test_plackett_luce_is_monotone_too(self):
        rows = synth.kendall_tau_table(
            concentrations=(0.0, 0.5, 1.0),
            n_people=20, n_desks=20, k=5, seed="tau-pl",
            noise="plackett_luce", replicates=2,
        )
        assert all(row.predicted_tau is None for row in rows), (
            "the closed form is Gaussian-only and must not be claimed for Gumbel"
        )
        assert [round(r.mean_tau, 6) for r in rows] == sorted(
            round(r.mean_tau, 6) for r in rows
        )
        assert abs(rows[0].mean_tau) < 0.1
        assert rows[-1].mean_tau == pytest.approx(1.0, abs=1e-12)

    @pytest.mark.parametrize("bad", [-0.01, 1.01])
    def test_concentration_outside_the_unit_interval_is_rejected(self, bad: float):
        with pytest.raises(ValueError, match=r"concentration must be in \[0, 1\]"):
            synth.latent_utilities(synth._rng("bad"), 3, 5, bad)

    def test_unknown_noise_family_is_rejected(self):
        with pytest.raises(ValueError, match="noise must be one of"):
            synth.latent_utilities(synth._rng("bad"), 3, 5, 0.5, noise="cauchy")

    @pytest.mark.parametrize("concentration", CONCENTRATIONS)
    def test_higher_concentration_reaches_the_generated_responses(
        self, tmp_path, concentration: float
    ):
        """The knob has to survive the whole generator, not just the utilities."""
        world = synth.generate(
            20, k=5, n_desks=30, seed="knob-e2e", concentration=concentration,
            eligibility_style="flat", keeper_frac=0.0, image_size=SMALL_IMAGE,
        )
        choices = world.latest_choices()
        first_picks = {tuple(v)[0] for v in choices.values()}
        if concentration == 1.0:
            assert len(first_picks) == 1, "everyone must want the same desk at c=1"
        elif concentration == 0.0:
            assert len(first_picks) > 1


# ==========================================================================
# 6. Exact arithmetic and the reference implementations
# ==========================================================================


class TestExactArithmetic:
    @pytest.mark.parametrize("k", K_VALUES)
    def test_curve_to_integers_is_exact(self, k: int):
        scoring = synth.make_scoring(k, seed_string="exact")
        for name, values in scoring["curves"].items():
            points, scale = synth.curve_to_integers(values)
            assert len(points) == k
            assert all(isinstance(p, int) for p in points)
            for value, point in zip(values, points):
                assert point == pytest.approx(float(value) * scale)
            assert points == tuple(sorted(points, reverse=True))
            assert all(p > 0 for p in points), f"{name} must stay strictly positive"

    def test_the_concave_curve_exercises_the_rationalisation_path(self):
        """`concave` uses halves on purpose, so §5.3 is not dead code in tests."""
        scoring = synth.make_scoring(5, seed_string="halves")
        points, scale = synth.curve_to_integers(scoring["curves"]["concave"])
        assert scale == 2
        assert points == (10, 9, 8, 7, 6)

    def test_the_decimal_literal_is_read_not_the_binary_float(self):
        """`Fraction(str(v))`, so 0.1 scales by 10 and not by 2**55."""
        points, scale = synth.curve_to_integers([1, 0.1])
        assert (points, scale) == ((10, 1), 10)

    def test_a_non_terminating_decimal_explodes_the_scale(self):
        """Why SPEC §2.4 rejects these at validation rather than here.

        `curve_to_integers` is exact whatever it is handed, so 1/3 written out
        to float precision does not *fail* — it produces a scale factor of 10**16
        that would overflow the int64 points matrix downstream. The validator is
        the thing that has to say no, and it does (see test_config_responses.py::
        test_scoring_rule_is_enforced[curve-value-non-terminating]).
        """
        _points, scale = synth.curve_to_integers([1, 1 / 3])
        assert scale > validate.MAX_CURVE_DENOMINATOR


class TestReferenceProblem:
    def test_tie_heavy_has_exactly_the_claimed_number_of_optima(self):
        """`(K!)**n_blocks`, counted by exhaustive search, not asserted."""
        for k, n_blocks in ((3, 2), (4, 1)):
            case = synth.tie_heavy(k=k, n_blocks=n_blocks)
            best, optima, feasible = synth.verify_tie_heavy(case)
            assert optima == math.factorial(k) ** n_blocks
            assert feasible == optima, "every feasible assignment must be optimal here"
            curve = case.world.scoring["curves"][case.world.scoring["primary_curve"]]
            points, _scale = synth.curve_to_integers(curve)
            assert best == n_blocks * sum(points)

    def test_exact_fit_has_a_unique_optimum(self):
        case = synth.exact_fit(n_people=6, k=5)
        _people, _desks, allowed, points = synth.reference_problem(case.world)
        best, optima = synth.count_optimal_assignments(points, allowed)
        assert optima == 1, "exact_fit is the complement of tie_heavy"
        assert best == 6 * int(points.max())

    def test_reference_problem_agrees_with_the_real_problem_builder(self, tmp_path):
        """The generator's mirror of §5.1 must match `problem.build_problem`."""
        from deskmatch import problem as problem_mod

        world = synth.generate(
            9, k=4, seed="mirror", concentration=0.3, keeper_frac=0.2,
            unavailable_frac=0.1, image_size=SMALL_IMAGE,
        )
        config_dir, responses_path = world.write(tmp_path)
        config = load_config(config_dir)
        loaded = responses_mod.load_responses(str(responses_path), config.k)
        build = problem_mod.build_problem(config, loaded)

        people, desks, allowed, points = synth.reference_problem(world)
        assert build.problem.people == people
        assert build.problem.desks == desks
        assert (build.problem.allowed == allowed).all()
        assert (build.problem.points == points).all()

    def test_brute_force_refuses_to_pretend_to_hang(self):
        import numpy as np

        with pytest.raises(ValueError, match="brute force refuses"):
            synth.count_optimal_assignments(
                np.zeros((11, 11), dtype=np.int64),
                np.ones((11, 11), dtype=bool),
            )


# ==========================================================================
# 7. The scenario builders
# ==========================================================================


class TestScenarios:
    def test_every_scenario_is_registered_and_sorted(self):
        assert synth.scenario_names() == tuple(sorted(synth.SCENARIOS))
        assert set(synth.SCENARIOS) >= {
            "everyone_ranks_same_k_desks", "more_people_than_desks",
            "cohort_zone_starved", "empty_roster", "single_person",
            "duplicate_submissions", "stale_desk_reference",
        }, "the README names these adversarial cases"

    @pytest.mark.parametrize("name", synth.scenario_names())
    def test_every_scenario_is_deterministic(self, name: str):
        first = synth.SCENARIOS[name]().world.file_texts()
        second = synth.SCENARIOS[name]().world.file_texts()
        assert first == second

    @pytest.mark.parametrize("name", synth.scenario_names())
    def test_every_scenario_describes_itself(self, name: str):
        case = synth.SCENARIOS[name]()
        assert case.name == name
        assert case.description.strip()
        assert case.expectation.strip(), "a scenario must say what should happen to it"

    @pytest.mark.parametrize("name", synth.scenario_names())
    def test_scenario_files_can_be_written(self, tmp_path, name: str):
        case = synth.SCENARIOS[name]()
        config_dir, responses_path = case.world.write(
            tmp_path / name, write_images=False
        )
        assert (config_dir / "rooms.json").is_file()
        assert responses_path.is_file()
        # Whatever the scenario is *about*, rooms.json is always well-formed.
        payload = json.loads((config_dir / "rooms.json").read_text())
        assert validate.validate_rooms(payload).ok

    def test_an_unwritten_case_says_so_rather_than_returning_none(self):
        case = synth.single_person()
        with pytest.raises(ValueError, match="built in memory only"):
            case.as_paths()

    def test_scenario_sizes_are_parameters(self):
        for n_people in (4, 11):
            for k in (2, 5):
                case = synth.everyone_ranks_same_k_desks(n_people=n_people, k=k)
                assert case.world.n_people == n_people
                assert case.world.k == k
                choices = set(case.world.latest_choices().values())
                assert len(choices) == 1, "everyone submits the identical list"

    @pytest.mark.parametrize(
        "n_people,n_precandidates,precandidate_desks,k",
        [(16, 10, 5, 4), (20, 12, 6, 5), (9, 6, 2, 3)],
    )
    def test_zone_starvation_is_about_the_zone_not_the_desk_count(
        self, tmp_path, n_people: int, n_precandidates: int, precandidate_desks: int, k: int
    ):
        """Only the eligibility rule makes this fail: the candidates are fine.

        A solver that reported "not enough desks" here would be answering the
        wrong question, so the fixture has to make the cohort the only cause.
        """
        case = synth.cohort_zone_starved(
            tmp_path / f"starved{n_people}",
            n_people=n_people, n_precandidates=n_precandidates,
            precandidate_desks=precandidate_desks, k=k,
        )
        world = case.world
        precandidates = [
            row["email"] for row in world.roster_rows
            if row["candidacy"] == "precandidate"
        ]
        assert len(precandidates) == n_precandidates

        zones = {z for e in precandidates for z in world.allowed_zones[e]}
        assert len(zones) == 1, "precandidates must be confined to one zone"
        starved = zones.pop()
        in_zone = [d for d, z in world.zone_of_desk.items() if z == starved]
        assert len(in_zone) == precandidate_desks < n_precandidates

        # ...while everybody else has room to spare, so the shortage is local.
        others = world.n_desks - precandidate_desks
        assert others >= n_people - n_precandidates


# ==========================================================================
# 8. The generator's own CLI
# ==========================================================================


class TestSynthCli:
    def test_emit_writes_a_usable_directory(self, tmp_path, capsys):
        code = synth.main(
            ["emit", "--out", str(tmp_path / "dry"), "-n", "12", "--seed", "cli-demo",
             "--no-images"]
        )
        assert code == 0
        config_dir = tmp_path / "dry" / "config"
        assert (config_dir / "roster.csv").is_file()
        config = load_config(config_dir)
        assert len(config.roster.people) == 12
        # --no-images means the validator has something to warn about, and it must.
        find_warning(list(config.warnings), "floor-plan image", "does not exist")

    def test_emit_is_deterministic_across_invocations(self, tmp_path):
        for name in ("one", "two"):
            synth.main(
                ["emit", "--out", str(tmp_path / name), "-n", "9", "--seed", "cli-det",
                 "--no-images"]
            )
        assert files_under(tmp_path / "one") == files_under(tmp_path / "two")

    @pytest.mark.parametrize(
        "argv",
        [
            ["-n", "5", "--concentration", "5"],      # outside [0, 1]
            ["-n", "5", "--n-desks", "4", "--zones", "9"],  # more zones than desks
            ["-n", "-3"],                              # negative population
        ],
    )
    def test_bad_arguments_exit_one_rather_than_traceback(self, tmp_path, argv):
        out = tmp_path / ("bad" + "".join(a for a in argv if a.isalnum()))
        assert synth.main(["emit", "--out", str(out), "--no-images", *argv]) == 1

    def test_tau_table_command_runs(self, capsys):
        assert synth.main(
            ["tau-table", "--n-people", "8", "--n-desks", "8", "--replicates", "1",
             "--concentrations", "0,1"]
        ) == 0
        out = capsys.readouterr().out
        assert "strictly increasing in tau:            True" in out
