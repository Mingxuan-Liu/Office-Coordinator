"""Invariant I3 — the whole basis of the process.

    I3: Same `(responses, config, seed)` ⇒ byte-identical `results.json`.
        Always, on any machine, any OS, any supported Python.

Everything the README promises about trust reduces to this one property. The
coordinator is also a participant; the argument that they did not shop for a
favourable answer is *entirely* "here are the published inputs, re-run it
yourself and you will get the same bytes". If the output is only usually the
same, the process is only usually believable.

So determinism is tested here as a correctness property, four ways:

1. **Twice from the same inputs.** The obvious one, and the weakest: a shared
   interpreter can hide a lot.

2. **A committed golden file.** Catches the change that two runs in one process
   cannot: a *deliberate* edit that quietly moves the department's answer.
   Regenerating it requires an explicit environment variable, so nobody can
   "fix" a real regression by refreshing the baseline without noticing.

3. **Two subprocesses under different `PYTHONHASHSEED`s.** The single most
   valuable determinism test in the file. `hash()` of a str, and therefore the
   iteration order of any `set` of strings, is salted per interpreter start.
   Code that leaks set-iteration order into output passes every same-process
   test ever written and then produces a different answer on the coordinator's
   laptop. Nothing but a subprocess can catch it, and the test proves the
   variable actually took effect rather than assuming it did.

4. **`provenance.canonical_json`.** The serialiser is where I3 is *implemented*;
   every other module can be perfectly deterministic and the guarantee still
   evaporates if the last step sorts nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from conftest import REPO_ROOT, SOLVER_DIR, TESTS_DIR
from deskmatch import provenance
from pipeline import (
    PINNABLE_PROVENANCE_KEYS,
    PINNED_CONFIG_DISPLAY,
    PINNED_ENVIRONMENT,
    PINNED_RESPONSES_DISPLAY,
    run_pipeline,
)

PIPELINE_SCRIPT = TESTS_DIR / "pipeline.py"
GOLDEN_DIR = TESTS_DIR / "golden"
GOLDEN_INPUTS = GOLDEN_DIR / "inputs"
GOLDEN_CONFIG = GOLDEN_INPUTS / "config"
GOLDEN_RESPONSES = GOLDEN_INPUTS / "responses.csv"
GOLDEN_RESULTS = GOLDEN_DIR / "results.json"

#: Set to 1 to rewrite `tests/golden/results.json`. See `test_golden_results_json`.
REGENERATE_ENV = "DESKMATCH_UPDATE_GOLDEN"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def pinned_pipeline(config_dir, responses_path, out_dir) -> bytes:
    """Run the pipeline with the environment pinned and return results.json bytes."""
    summary = run_pipeline(config_dir, responses_path, out_dir, pin_environment=True)
    return Path(summary["results_path"]).read_bytes()


def strip_keys(doc: dict, top: tuple[str, ...] = (), inside_provenance: tuple[str, ...] = ()):
    """A copy of a results document with the named fields removed."""
    out = {k: v for k, v in doc.items() if k not in top}
    block = out.get("provenance")
    if isinstance(block, dict):
        out["provenance"] = {
            k: v for k, v in block.items() if k not in inside_provenance
        }
    return out


def run_in_subprocess(
    config_dir: Path,
    responses_path: Path,
    out_dir: Path,
    *,
    hash_seed: str,
) -> dict:
    """Run tests/pipeline.py in a fresh interpreter under `PYTHONHASHSEED`.

    `cwd` is pinned to the repository root so the one cwd-sensitive field in the
    document (`reproduce`, which renders paths relative to the working
    directory) is held constant and the comparison is about hashing alone.
    """
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = hash_seed
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SOLVER_DIR), env["PYTHONPATH"]] if env.get("PYTHONPATH") else [str(SOLVER_DIR)]
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(PIPELINE_SCRIPT),
            "--config", str(config_dir),
            "--responses", str(responses_path),
            "--out", str(out_dir),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"pipeline subprocess (PYTHONHASHSEED={hash_seed}) exited "
            f"{completed.returncode}\nstdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    return json.loads(completed.stdout.strip().splitlines()[-1])


# ==========================================================================
# 1. The same inputs, twice
# ==========================================================================


class TestSamePipelineTwice:
    def test_real_config_is_byte_identical_across_two_runs(
        self, tmp_path, real_config_dir, real_responses_csv
    ):
        first = run_pipeline(real_config_dir, real_responses_csv, tmp_path / "one")
        second = run_pipeline(real_config_dir, real_responses_csv, tmp_path / "two")

        left = Path(first["results_path"]).read_bytes()
        right = Path(second["results_path"]).read_bytes()
        assert left == right, "results.json must be byte-identical (I3)"
        assert first["canonical_sha256"] == second["canonical_sha256"]
        assert first["results_bytes_sha256"] == second["results_bytes_sha256"]

    def test_the_real_configs_answer_is_the_expected_one(
        self, tmp_path, real_config_dir, real_responses_csv
    ):
        """A guard on the fixture itself: if the shipped inputs change, say so."""
        summary = run_pipeline(real_config_dir, real_responses_csv, tmp_path)
        assert (summary["n_people"], summary["n_desks"]) == (9, 30)
        assert summary["rank_histogram"] == [7, 1, 1, 0, 0]
        assert summary["total_points_scaled"] == 42

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param(dict(n_people=7, k=3, n_desks=20), id="N7-K3"),
            pytest.param(dict(n_people=16, k=5, keeper_frac=0.25), id="N16-K5-keepers"),
            pytest.param(
                dict(n_people=24, k=8, resubmit_frac=0.5, shuffle_rows=True),
                id="N24-K8-resubmissions",
            ),
        ],
    )
    def test_synthetic_worlds_are_byte_identical_across_two_runs(
        self, tmp_path, synth_world_factory, kwargs
    ):
        world = synth_world_factory(
            kwargs.pop("n_people"), kwargs.pop("k"), concentration=0.2, **kwargs
        )
        config_dir, responses_path = world.write(tmp_path / "inputs")
        first = pinned_pipeline(config_dir, responses_path, tmp_path / "a")
        second = pinned_pipeline(config_dir, responses_path, tmp_path / "b")
        assert first == second

    def test_results_json_is_written_in_canonical_form(
        self, tmp_path, real_config_dir, real_responses_csv
    ):
        summary = run_pipeline(real_config_dir, real_responses_csv, tmp_path)
        raw = Path(summary["results_path"]).read_bytes()
        assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
        document = json.loads(raw)
        assert provenance.canonical_json(document) == raw, (
            "the file on disk must already be its own canonical serialisation"
        )
        # sort_keys=True is not decoration: it is what makes the bytes a function
        # of the content rather than of dict insertion order.
        assert list(document) == sorted(document)

    def test_roster_row_order_cannot_change_the_answer(
        self, tmp_path, synth_world_factory
    ):
        """A spreadsheet export re-sorted by name must not move anybody's desk."""
        world = synth_world_factory(14, 5, concentration=0.25)
        config_dir, responses_path = world.write(tmp_path / "forward")
        forward = pinned_pipeline(config_dir, responses_path, tmp_path / "a")

        shuffled_dir, shuffled_responses = world.write(tmp_path / "reversed")
        roster = (shuffled_dir / "roster.csv").read_text(encoding="utf-8").splitlines()
        (shuffled_dir / "roster.csv").write_text(
            "\n".join([roster[0], *reversed(roster[1:])]) + "\n",
            encoding="utf-8", newline="",
        )
        reordered = pinned_pipeline(shuffled_dir, shuffled_responses, tmp_path / "b")

        # Only the recorded hash of roster.csv may differ; the answer may not.
        ignored = ("roster.csv",)
        left = json.loads(forward)
        right = json.loads(reordered)
        for doc in (left, right):
            for name in ignored:
                doc["provenance"]["config_sha256"].pop(name)
        left = strip_keys(left, inside_provenance=("canonical_sha256",))
        right = strip_keys(right, inside_provenance=("canonical_sha256",))
        assert left == right

    def test_a_different_seed_changes_the_hash_visibly(self, tmp_path):
        """SPEC §8: seed shopping must be *visible*, which means the hash moves.

        `tie_heavy` is the right fixture: it is built so that every feasible
        assignment is exactly optimal, so the seed is the only thing choosing,
        and §5.4 says it may choose only among exact ties. Both halves of that
        sentence are asserted — the hash moves, the total does not.
        """
        from deskmatch import synth

        case = synth.tie_heavy(n_blocks=3, k=3)
        config_dir, responses_path = case.world.write(tmp_path / "inputs")
        first = json.loads(pinned_pipeline(config_dir, responses_path, tmp_path / "a"))

        scoring = json.loads((config_dir / "scoring.json").read_text())
        scoring["tie_break_seed"] = scoring["tie_break_seed"] + "-alternative"
        (config_dir / "scoring.json").write_text(
            json.dumps(scoring, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        second = json.loads(pinned_pipeline(config_dir, responses_path, tmp_path / "b"))

        assert first["seed_string"] != second["seed_string"]
        assert (
            first["provenance"]["canonical_sha256"]
            != second["provenance"]["canonical_sha256"]
        ), "changing the seed must be visible in the published hash"
        assert first["total_points_scaled"] == second["total_points_scaled"], (
            "the seed may only reshuffle among assignments that already tie (§5.4)"
        )
        moved = [
            a["email"]
            for a, b in zip(first["assignments"], second["assignments"])
            if a["desk_id"] != b["desk_id"]
        ]
        assert moved, "on a tie-heavy instance a new seed should move somebody"


# ==========================================================================
# 2. The golden file
# ==========================================================================


class TestGoldenFile:
    """A fixed config + fixed responses + fixed seed, and the bytes they produce.

    HOW TO REGENERATE, DELIBERATELY
    -------------------------------
    Do this only when you have *decided* that the output should change — a new
    field in results.json, a change to the scoring or tie-break rules, a
    `deskmatch.__version__` bump. Never to make a red test go green.

        cd <repo root>
        DESKMATCH_UPDATE_GOLDEN=1 PYTHONPATH=solver python3 -m pytest \\
            tests/test_determinism.py::TestGoldenFile::test_golden_results_json

    That run rewrites `tests/golden/results.json` **and then fails on purpose**,
    so a regeneration can never be mistaken for a passing test. Read the diff
    (`git diff tests/golden/results.json`), convince yourself every changed line
    is a change you intended, re-run without the variable, and commit the golden
    file in the same commit as the change that moved it.

    The inputs under `tests/golden/inputs/` are frozen literal files, NOT
    regenerated from `deskmatch.synth`. If they were regenerated, a change to the
    generator would silently change what the golden file is testing.

    WHAT IS PINNED, AND WHY
    -----------------------
    `python` / `numpy` / `scipy` are replaced with a constant before the document
    is written (see `tests/pipeline.py`). SPEC §7.1 deliberately puts those
    versions inside the canonical hash, which is right for an audit trail and
    intolerable for a committed baseline — `pip install -U numpy` would expire
    the golden file with no change to the answer. `deskmatch_version` is *not*
    pinned: it lives in a tracked file, so moving it is a repository change and
    should require a deliberate regeneration.

    `test_pinning_conceals_nothing_else` below proves the pinning cannot hide a
    regression: a live run and a pinned run of the same inputs are compared field
    by field, and only the pinned fields are permitted to differ.
    """

    def test_golden_inputs_are_committed(self):
        assert GOLDEN_CONFIG.is_dir(), f"{GOLDEN_CONFIG} is missing from the repository"
        assert GOLDEN_RESPONSES.is_file()
        for name in ("rooms.json", "eligibility.json", "roster.csv", "scoring.json"):
            assert (GOLDEN_CONFIG / name).is_file(), name

    def test_golden_results_json(self, tmp_path):
        produced = pinned_pipeline(GOLDEN_CONFIG, GOLDEN_RESPONSES, tmp_path)

        if os.environ.get(REGENERATE_ENV) == "1":
            GOLDEN_RESULTS.write_bytes(produced)
            pytest.fail(
                f"{GOLDEN_RESULTS} was REGENERATED because {REGENERATE_ENV}=1.\n"
                "This failure is deliberate. Review `git diff tests/golden/"
                "results.json`, confirm every changed line is intended, then "
                "re-run WITHOUT the environment variable and commit."
            )

        assert GOLDEN_RESULTS.is_file(), (
            f"{GOLDEN_RESULTS} is missing. If this is a first run, regenerate it "
            f"deliberately with {REGENERATE_ENV}=1 (see this class's docstring)."
        )
        expected = GOLDEN_RESULTS.read_bytes()
        if produced != expected:
            pytest.fail(
                "results.json no longer matches tests/golden/results.json.\n"
                "  golden  sha256: " + hashlib.sha256(expected).hexdigest() + "\n"
                "  current sha256: " + hashlib.sha256(produced).hexdigest() + "\n"
                + _diff_documents(json.loads(expected), json.loads(produced))
                + f"\nIf this change is intended, regenerate with {REGENERATE_ENV}=1."
            )

    def test_golden_inputs_have_not_been_edited(self):
        """The inputs are frozen; their hashes are recorded inside the golden."""
        recorded = json.loads(GOLDEN_RESULTS.read_bytes())["provenance"]
        for name, digest in recorded["config_sha256"].items():
            actual = hashlib.sha256((GOLDEN_CONFIG / name).read_bytes()).hexdigest()
            assert actual == digest, (
                f"tests/golden/inputs/config/{name} has been edited since the "
                f"golden results file was generated"
            )
        assert (
            hashlib.sha256(GOLDEN_RESPONSES.read_bytes()).hexdigest()
            == recorded["responses_sha256"]
        )

    def test_the_golden_case_is_worth_having(self):
        """A golden over a trivial instance certifies nothing."""
        document = json.loads(GOLDEN_RESULTS.read_bytes())
        assert document["n_people"] >= 8
        assert document["locked_desks"], "the case must exercise desk keepers"
        assert document["unavailable_desks"], "...and administratively held desks"
        assert document["dropped_choices"], "...and a choice dropped from the pool"
        assert document["roster_conflicts"], "...and a roster/submission conflict"
        assert document["provenance"]["responses_row_count"] > document["n_people"], (
            "...and at least one re-submission"
        )
        assert sum(document["rank_histogram"]) == document["n_people"]

    def test_golden_file_is_internally_consistent(self, tmp_path):
        """Its embedded `canonical_sha256` must be a hash of its own contents."""
        document = json.loads(GOLDEN_RESULTS.read_bytes())
        embedded = document["provenance"]["canonical_sha256"]
        assert provenance.compute_canonical_sha256(document) == embedded
        assert provenance.verify_results(GOLDEN_RESULTS, embedded) == embedded

    def test_pinning_conceals_nothing_else(self, tmp_path):
        """A live run and a pinned run may differ ONLY in the pinned fields."""
        pinned = json.loads(pinned_pipeline(GOLDEN_CONFIG, GOLDEN_RESPONSES, tmp_path / "p"))
        live_summary = run_pipeline(
            GOLDEN_CONFIG,
            GOLDEN_RESPONSES,
            tmp_path / "l",
            display_config=PINNED_CONFIG_DISPLAY,
            display_responses=PINNED_RESPONSES_DISPLAY,
        )
        live = json.loads(Path(live_summary["results_path"]).read_bytes())

        assert set(pinned) == set(live)
        for key in pinned:
            if key != "provenance":
                assert pinned[key] == live[key], f"top-level field {key!r} moved"

        differing = {
            key for key in pinned["provenance"]
            if pinned["provenance"][key] != live["provenance"][key]
        }
        assert differing <= PINNABLE_PROVENANCE_KEYS, (
            f"pinning changed fields it has no business changing: "
            f"{sorted(differing - PINNABLE_PROVENANCE_KEYS)}"
        )
        assert set(PINNED_ENVIRONMENT) <= differing, (
            "the pinned fields must actually be pinned; if they already match the "
            "live environment this test is vacuous"
        )


def _diff_documents(expected: dict, produced: dict, path: str = "$") -> str:
    """A short, readable field-by-field diff, for the golden failure message."""
    lines: list[str] = []
    if isinstance(expected, dict) and isinstance(produced, dict):
        for key in sorted(set(expected) | set(produced)):
            if key not in expected:
                lines.append(f"    + {path}.{key} = {produced[key]!r}")
            elif key not in produced:
                lines.append(f"    - {path}.{key} = {expected[key]!r}")
            elif expected[key] != produced[key]:
                lines.extend(
                    _diff_documents(expected[key], produced[key], f"{path}.{key}").splitlines()
                )
    elif expected != produced:
        lines.append(f"    ~ {path}\n        golden : {expected!r}\n        current: {produced!r}")
    return "\n".join(lines[:40]) or "    (no field-level difference; whitespace only)"


# ==========================================================================
# 3. PYTHONHASHSEED must not be able to reach the output
# ==========================================================================


class TestPythonHashSeedIndependence:
    """SPEC §5.5: "No `hash()` of strings (PYTHONHASHSEED-dependent)."

    A `set` of strings iterates in an order derived from the salted string hash.
    Code that iterates one on an output path is perfectly deterministic within a
    single interpreter and non-deterministic across machines — the exact failure
    mode I3 exists to forbid, and the exact failure mode no same-process test can
    see. Hence: real subprocesses, different seeds, compare the bytes.
    """

    @pytest.fixture(scope="class")
    def real_runs(self, tmp_path_factory, real_config_dir, real_responses_csv) -> dict:
        root = tmp_path_factory.mktemp("hashseed_real")
        return {
            seed: run_in_subprocess(
                real_config_dir, real_responses_csv, root / f"seed{seed}", hash_seed=seed
            )
            for seed in ("0", "1")
        }

    def test_the_environment_variable_actually_took_effect(self, real_runs):
        """Without this, "the two runs agree" could just mean nothing happened."""
        assert real_runs["0"]["pythonhashseed"] == "0"
        assert real_runs["1"]["pythonhashseed"] == "1"
        assert real_runs["0"]["str_hash_probe"] != real_runs["1"]["str_hash_probe"], (
            "PYTHONHASHSEED did not change hash('deskmatch'), so this test would "
            "be vacuous"
        )

    def test_results_are_identical_under_both_hash_seeds(self, real_runs):
        assert real_runs["0"]["results_bytes_sha256"] == real_runs["1"]["results_bytes_sha256"]
        assert real_runs["0"]["canonical_sha256"] == real_runs["1"]["canonical_sha256"]

    def test_the_files_themselves_are_byte_identical(
        self, tmp_path, real_config_dir, real_responses_csv
    ):
        left = run_in_subprocess(
            real_config_dir, real_responses_csv, tmp_path / "a", hash_seed="0"
        )
        right = run_in_subprocess(
            real_config_dir, real_responses_csv, tmp_path / "b", hash_seed="1"
        )
        assert (
            Path(left["results_path"]).read_bytes()
            == Path(right["results_path"]).read_bytes()
        )

    @pytest.fixture(scope="class")
    def golden_reference(self, tmp_path_factory) -> dict:
        return run_in_subprocess(
            GOLDEN_CONFIG,
            GOLDEN_RESPONSES,
            tmp_path_factory.mktemp("hashseed_golden_ref"),
            hash_seed="0",
        )

    @pytest.mark.parametrize("hash_seed", ["1", "12345", "random"])
    def test_the_golden_case_survives_every_hash_seed(
        self, tmp_path, golden_reference, hash_seed: str
    ):
        """The richer instance: keepers, unavailable desks, dropped choices,
        roster conflicts and re-submissions all involve collections that are
        easy to accidentally iterate as sets.
        """
        summary = run_in_subprocess(
            GOLDEN_CONFIG, GOLDEN_RESPONSES, tmp_path / hash_seed, hash_seed=hash_seed
        )
        assert summary["str_hash_probe"] != golden_reference["str_hash_probe"], (
            f"PYTHONHASHSEED={hash_seed} produced the same string hash as seed 0, "
            f"so this case proves nothing"
        )
        assert summary["canonical_sha256"] == golden_reference["canonical_sha256"]
        assert summary["results_bytes_sha256"] == golden_reference["results_bytes_sha256"]

    def test_the_package_never_calls_the_builtin_hash(self):
        """A static guard, parsed rather than grepped.

        `hash()` of a str is the mechanism the subprocess tests above are hunting
        for; catching a new call the moment it is written beats catching it the
        year it changes somebody's desk. The AST is used instead of a regex so
        that the many prose mentions of "hash()" in the docstrings -- the code is
        well commented about exactly this hazard -- are not false positives.
        """
        import ast

        offenders: list[str] = []
        for source in sorted((SOLVER_DIR / "deskmatch").glob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "hash"
                ):
                    offenders.append(f"{source.name}:{node.lineno}")
        assert not offenders, (
            "builtin hash() is salted by PYTHONHASHSEED (SPEC §5.5) and must "
            "never appear; use hashlib:\n  " + "\n  ".join(offenders)
        )
        # The guard must be looking at something.
        assert len(list((SOLVER_DIR / "deskmatch").glob("*.py"))) > 10


# ==========================================================================
# 4. provenance.canonical_json — where I3 is implemented
# ==========================================================================


class TestCanonicalJson:
    def test_key_order_does_not_affect_the_bytes(self):
        first = {"b": 1, "a": {"z": [1, 2], "y": 3}, "c": True}
        second = {"c": True, "a": {"y": 3, "z": [1, 2]}, "b": 1}
        assert provenance.canonical_json(first) == provenance.canonical_json(second)
        assert provenance.canonical_json(first) == b'{"a":{"y":3,"z":[1,2]},"b":1,"c":true}\n'

    def test_output_shape(self):
        raw = provenance.canonical_json({"k": [1, {"a": "é"}]})
        assert raw.endswith(b"\n")
        assert b", " not in raw and b": " not in raw, "compact separators"
        assert "é".encode("utf-8") in raw, "ensure_ascii=False, so text stays text"

    def test_list_order_is_preserved(self):
        """Sorting is for *sets*. A list's order is meaningful and must survive."""
        assert provenance.canonical_json([3, 1, 2]) == b"[3,1,2]\n"

    def test_sets_are_sorted_before_serialisation(self):
        assert provenance.canonical_json({"z": {"c", "a", "b"}}) == b'{"z":["a","b","c"]}\n'
        assert provenance.canonical_json(frozenset({3, 1, 2})) == b"[1,2,3]\n"

    def test_mixed_type_sets_still_get_a_total_order(self):
        raw = provenance.canonical_json({"mixed": {1, "a", 2.5}})
        assert raw.endswith(b"\n")
        assert provenance.canonical_json({"mixed": {2.5, "a", 1}}) == raw

    def test_tuples_serialise_as_arrays(self):
        assert provenance.canonical_json(("a", "b")) == b'["a","b"]\n'

    # -- Fractions ---------------------------------------------------------

    def test_integral_fractions_become_integers(self):
        """SPEC §5.3 rationalises curves to exact integers; keep them integral."""
        assert provenance.canonical_json(Fraction(10, 2)) == b"5\n"
        assert provenance.canonical_json(Fraction(42, 1)) == b"42\n"
        assert b"5.0" not in provenance.canonical_json(Fraction(10, 2))

    def test_non_integral_fractions_become_the_decimal_that_was_written(self):
        assert provenance.canonical_json(Fraction(9, 2)) == b"4.5\n"
        assert provenance.canonical_json(Fraction(1, 10)) == b"0.1\n"

    def test_a_curve_of_fractions_round_trips_as_a_curve_of_numbers(self):
        curve = [Fraction(5), Fraction(9, 2), Fraction(4), Fraction(7, 2), Fraction(3)]
        assert provenance.canonical_json({"curve_values": curve}) == (
            b'{"curve_values":[5,4.5,4,3.5,3]}\n'
        )

    def test_decimals_are_handled_exactly_too(self):
        assert provenance.canonical_json(Decimal("4.50")) == b"4.5\n"
        assert provenance.canonical_json(Decimal("5")) == b"5\n"

    def test_total_points_serialises_as_a_number(self):
        """`Solution.total_points` is a Fraction and lands in results.json."""
        assert provenance.canonical_json({"total_points": Fraction(84, 2)}) == (
            b'{"total_points":42}\n'
        )

    # -- numpy -------------------------------------------------------------

    @pytest.mark.parametrize(
        "value,expected",
        [
            (np.int64(7), b"7\n"),
            (np.int32(-3), b"-3\n"),
            (np.float64(2.5), b"2.5\n"),
            (np.bool_(True), b"true\n"),
            (np.str_("x"), b'"x"\n'),
            (np.array([1, 2, 3]), b"[1,2,3]\n"),
            (np.array([[1, 2], [3, 4]]), b"[[1,2],[3,4]]\n"),
        ],
    )
    def test_numpy_scalars_and_arrays(self, value, expected: bytes):
        assert provenance.canonical_json(value) == expected

    # -- refusals ----------------------------------------------------------

    def test_negative_zero_is_normalised(self):
        assert provenance.canonical_json(-0.0) == provenance.canonical_json(0.0) == b"0.0\n"

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_floats_are_refused_by_path(self, value: float):
        with pytest.raises(ValueError, match=r"\$\.a\.b\[1\]"):
            provenance.canonical_json({"a": {"b": [0.0, value]}})

    def test_bytes_are_refused_rather_than_guessed_at(self):
        with pytest.raises(TypeError, match="raw bytes"):
            provenance.canonical_json({"digest": b"\x00\x01"})

    def test_an_unknown_type_names_itself_and_its_path(self):
        class Custom:
            pass

        with pytest.raises(TypeError) as excinfo:
            provenance.canonical_json({"outer": [Custom()]})
        message = str(excinfo.value)
        assert "$.outer[0]" in message and "Custom" in message
        assert "default=str" in message, (
            "the message must say why stringifying would be wrong, not just that "
            "it is unsupported"
        )

    def test_float_mapping_keys_are_refused(self):
        with pytest.raises(TypeError, match="mapping key of type float"):
            provenance.canonical_json({1.0: "x"})

    def test_integer_and_boolean_keys_are_stringified_deliberately(self):
        assert provenance.canonical_json({1: "a", 2: "b"}) == b'{"1":"a","2":"b"}\n'
        assert provenance.canonical_json({True: "t"}) == b'{"true":"t"}\n'

    def test_two_keys_that_render_the_same_are_refused(self):
        with pytest.raises(ValueError, match="two mapping keys"):
            provenance.canonical_json({1: "a", "1": "b"})


# ==========================================================================
# 5. The self-referential canonical hash
# ==========================================================================


class TestCanonicalSha256:
    def _document(self) -> dict:
        return {
            "schema": "deskmatch/results/1",
            "assignments": [{"email": "ada@umich.edu", "desk_id": "D01"}],
            "total_points": Fraction(42),
            "provenance": {
                "seed_string": "astro-2026",
                "seed_int": 12345,
                "curve_values": [Fraction(5), Fraction(9, 2)],
            },
        }

    def test_the_hash_is_of_the_document_with_the_field_removed(self):
        """The one detail people get wrong. No value can contain its own hash."""
        document = self._document()
        computed = provenance.compute_canonical_sha256(document)
        by_hand = hashlib.sha256(provenance.canonical_json(document)).hexdigest()
        assert computed == by_hand, (
            "with no canonical_sha256 present yet, the two must coincide"
        )

        stamped, digest = provenance.stamp_canonical_sha256(document)
        assert digest == computed
        assert stamped["provenance"]["canonical_sha256"] == digest

        stripped = {k: v for k, v in stamped.items() if k != "provenance"}
        stripped["provenance"] = {
            k: v for k, v in stamped["provenance"].items() if k != "canonical_sha256"
        }
        assert hashlib.sha256(provenance.canonical_json(stripped)).hexdigest() == digest

    def test_stamping_is_idempotent(self):
        stamped, first = provenance.stamp_canonical_sha256(self._document())
        assert provenance.compute_canonical_sha256(stamped) == first
        restamped, second = provenance.stamp_canonical_sha256(stamped)
        assert second == first
        assert restamped == stamped

    def test_stamping_does_not_mutate_the_input(self):
        document = self._document()
        provenance.stamp_canonical_sha256(document)
        assert "canonical_sha256" not in document["provenance"]
        assert isinstance(document["total_points"], Fraction)

    def test_generated_at_is_excluded_from_the_hash(self):
        """§5.5: wall-clock time must never influence the reproducibility target."""
        without = provenance.compute_canonical_sha256(self._document())
        document = self._document()
        document["provenance"]["generated_at"] = "2026-09-20T11:00:00+00:00"
        assert provenance.compute_canonical_sha256(document) == without

        later = self._document()
        later["provenance"]["generated_at"] = "2031-01-01T00:00:00+00:00"
        assert provenance.compute_canonical_sha256(later) == without

    def test_the_excluded_key_list_is_exactly_the_documented_one(self):
        assert set(provenance.HASH_EXCLUDED_KEYS) == {"canonical_sha256", "generated_at"}

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda d: d.__setitem__("total_points", Fraction(43)), id="total"),
            pytest.param(
                lambda d: d["assignments"][0].__setitem__("desk_id", "D02"), id="desk"
            ),
            pytest.param(
                lambda d: d["provenance"].__setitem__("seed_string", "other"), id="seed"
            ),
            pytest.param(lambda d: d.__setitem__("extra", 1), id="new-field"),
        ],
    )
    def test_any_other_change_moves_the_hash(self, mutate):
        baseline = provenance.compute_canonical_sha256(self._document())
        document = self._document()
        mutate(document)
        assert provenance.compute_canonical_sha256(document) != baseline

    def test_a_bare_document_gets_the_hash_at_the_top_level(self):
        """diagnostics.json / round2_input.json have no provenance block."""
        stamped, digest = provenance.stamp_canonical_sha256({"deficiency": 2})
        assert stamped["canonical_sha256"] == digest

    def test_write_results_json_returns_the_verifiable_hash(self, tmp_path):
        path = tmp_path / "results.json"
        digest = provenance.write_results_json(path, self._document())
        raw = path.read_bytes()
        assert digest != hashlib.sha256(raw).hexdigest(), (
            "the canonical hash is of the document MINUS the field, so it cannot "
            "equal the hash of the bytes that contain it"
        )
        assert json.loads(raw)["provenance"]["canonical_sha256"] == digest
        assert provenance.verify_results(path, digest) == digest
        assert provenance.verify_results(path, f"SHA256:{digest.upper()}") == digest

    def test_verify_rejects_a_tampered_file(self, tmp_path):
        from deskmatch.errors import VerificationError

        path = tmp_path / "results.json"
        digest = provenance.write_results_json(path, self._document())
        document = json.loads(path.read_bytes())
        document["assignments"][0]["desk_id"] = "D99"
        path.write_bytes(provenance.canonical_json(document))

        with pytest.raises(VerificationError) as excinfo:
            provenance.verify_results(path, digest)
        report = str(excinfo.value)
        assert "canonical hash does not match" in report
        assert "INCONSISTENT" in report, (
            "the report must distinguish an edited file from a different run"
        )

    def test_verify_rejects_a_malformed_hash(self, tmp_path):
        from deskmatch.errors import VerificationError

        path = tmp_path / "results.json"
        provenance.write_results_json(path, self._document())
        with pytest.raises(VerificationError, match="not a 64-character hex"):
            provenance.verify_results(path, "nope")

    def test_reproduce_string_keeps_the_placeholder_but_renders_the_hash(self):
        stamped, digest = provenance.stamp_canonical_sha256(
            {"provenance": {"reproduce": "deskmatch solve --verify <hash>"}}
        )
        assert provenance.REPRODUCE_VERIFY_PLACEHOLDER in stamped["provenance"]["reproduce"]
        assert provenance.reproduce_command(stamped).endswith(digest)


class TestProvenanceBlock:
    def test_the_block_carries_every_spec_7_1_field(self, tmp_path, real_config_dir,
                                                    real_responses_csv):
        summary = run_pipeline(real_config_dir, real_responses_csv, tmp_path)
        block = json.loads(Path(summary["results_path"]).read_bytes())["provenance"]
        for field in (
            "seed_string", "seed_int", "curve", "curve_values", "K",
            "responses_sha256", "responses_row_count", "config_sha256",
            "canonical_sha256", "deskmatch_version", "python", "numpy", "scipy",
            "reproduce",
        ):
            assert field in block, f"SPEC §7.1 requires {field!r}"
        assert set(block["config_sha256"]) == {
            "rooms.json", "eligibility.json", "roster.csv", "scoring.json"
        }

    def test_config_hashes_are_over_the_raw_bytes(self, tmp_path, real_config_dir,
                                                  real_responses_csv):
        summary = run_pipeline(real_config_dir, real_responses_csv, tmp_path)
        block = json.loads(Path(summary["results_path"]).read_bytes())["provenance"]
        for name, digest in block["config_sha256"].items():
            assert digest == hashlib.sha256(
                (real_config_dir / name).read_bytes()
            ).hexdigest()
        assert block["responses_sha256"] == hashlib.sha256(
            real_responses_csv.read_bytes()
        ).hexdigest()

    def test_no_absolute_path_reaches_the_document(self, tmp_path, real_config_dir,
                                                   real_responses_csv):
        """An operator's home directory in the hash would break I3 across machines."""
        summary = run_pipeline(real_config_dir, real_responses_csv, tmp_path)
        text = Path(summary["results_path"]).read_text(encoding="utf-8")
        assert str(Path.home()) not in text
        assert str(tmp_path) not in text

    def test_missing_required_fields_are_refused(self):
        with pytest.raises(ValueError, match="missing required field"):
            provenance.build_provenance(seed_string="x")

    def test_extra_fields_cannot_overwrite_a_spec_field(self):
        with pytest.raises(ValueError, match="would overwrite a SPEC"):
            provenance.build_provenance(
                seed_string="x", seed_int=1, curve_name="c", curve_values=[1],
                k=1, responses_sha256="d", responses_row_count=1,
                config_sha256={}, extra={"K": 9},
            )

    def test_relative_display_paths_are_machine_independent(self):
        assert provenance.relative_display_path("config", is_dir=True) == "config/"
        assert provenance.relative_display_path("responses.csv", is_dir=False) == (
            "responses.csv"
        )
        # An absolute path outside the tree collapses to its basename rather than
        # leaking "/Users/<somebody>".
        rendered = provenance.relative_display_path(
            "/somewhere/else/entirely/responses.csv", base="/tmp/work", is_dir=False
        )
        assert rendered == "responses.csv"
