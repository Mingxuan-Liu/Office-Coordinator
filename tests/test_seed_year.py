"""The tie-break seed is the cycle year — without letting the clock into the solve.

Using the calendar year as the seed is what makes it change annually and makes it
unshoppable: nobody chooses it. But a seed derived from the clock is a direct
threat to invariant I3, because "same inputs ⇒ byte-identical results.json" stops
being true the moment the answer depends on *when* you ask.

The containment is: the year is resolved exactly once, in `config._build_scoring`,
and written into `results.json`. Nothing downstream reads a clock, and `verify`
uses the seed recorded in the published file rather than today's year.

These tests exist because that containment is invisible — the code looks fine
either way, and the failure would not appear until someone re-ran a published
cycle in a later year and was told the results did not match. So the clock is
faked and the property is checked directly.
"""

from __future__ import annotations

import datetime as real_datetime
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPO_ROOT, SOLVER_DIR
from deskmatch.config import load_config
from deskmatch.errors import ConfigError

# A script that fakes the system clock, then runs the deskmatch CLI. Faking has
# to happen in a subprocess: the year is read at config-load time, and patching
# it in-process after other tests have already imported things is exactly the
# kind of order-dependence that makes a suite flaky.
_FAKE_CLOCK_RUNNER = """
import datetime as _dt, sys
sys.path.insert(0, {solver!r})

_YEAR = {year!r}

class _FrozenDatetime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return _dt.datetime({year!r}, 6, 15, 12, 0, 0, tzinfo=tz)

    @classmethod
    def utcnow(cls):
        return _dt.datetime({year!r}, 6, 15, 12, 0, 0)

_dt.datetime = _FrozenDatetime

from deskmatch.cli import main
raise SystemExit(main(sys.argv[1:]))
"""


def _run_at_year(tmp_path: Path, year: int, argv: list[str]):
    script = tmp_path / f"run_{year}.py"
    script.write_text(
        _FAKE_CLOCK_RUNNER.format(solver=str(SOLVER_DIR), year=year), encoding="utf-8"
    )
    return subprocess.run(
        [sys.executable, str(script), *argv],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=600,
    )


@pytest.fixture
def real_inputs(real_config_dir, real_responses_csv):
    if not real_responses_csv.is_file():
        pytest.skip("the shipped response export is not present")
    return real_config_dir, real_responses_csv


def _write_scoring(config_dir: Path, **overrides) -> None:
    path = config_dir / "scoring.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    for key, value in overrides.items():
        if value is None:
            doc.pop(key, None)
        else:
            doc[key] = value
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


class TestSeedResolution:
    def test_auto_resolves_to_the_current_year(self, real_config):
        scoring = real_config.scoring
        if scoring.seed_year is None:
            pytest.skip("the shipped config does not use seed_year")
        assert scoring.resolved_seed() == str(scoring.seed_year)
        if scoring.seed_year_from_clock:
            assert scoring.seed_year == real_datetime.date.today().year

    def test_a_pinned_year_is_used_verbatim(self, config_case):
        case = config_case()
        _write_scoring(case.path, seed_year=1999, tie_break_seed=None)
        scoring = load_config(case.path).scoring
        assert scoring.seed_year == 1999
        assert scoring.resolved_seed() == "1999"
        assert not scoring.seed_year_from_clock, "a pinned year is not from the clock"

    def test_without_seed_year_the_string_seed_governs(self, config_case):
        case = config_case()
        _write_scoring(case.path, seed_year=None, tie_break_seed="a-string")
        scoring = load_config(case.path).scoring
        assert scoring.seed_year is None
        assert scoring.resolved_seed() == "a-string"

    @pytest.mark.parametrize("bad", (1200, 3500, "nineteen", 20.26))
    def test_an_implausible_seed_year_is_rejected(self, config_case, bad):
        case = config_case()
        _write_scoring(case.path, seed_year=bad, tie_break_seed=None)
        with pytest.raises(ConfigError) as excinfo:
            load_config(case.path)
        blob = str(excinfo.value)
        assert "seed_year" in blob
        assert "auto" in blob, "the message should name the accepted string form"

    def test_a_leftover_string_seed_is_flagged_as_dead_config(self, config_case):
        case = config_case()
        _write_scoring(case.path, seed_year=2026, tie_break_seed="stale")
        config = load_config(case.path)
        assert config.scoring.resolved_seed() == "2026"
        blob = " ".join(config.warnings)
        assert "tie_break_seed" in blob and "IGNORED" in blob, (
            f"a seed that looks live but is not must be called out: {config.warnings}"
        )


class TestTheClockCannotReachTheSolve:
    """The whole point. These are the tests that would have caught it."""

    def test_a_fresh_run_uses_the_faked_year(self, tmp_path, real_inputs):
        config_dir, _responses = real_inputs
        proc = _run_at_year(tmp_path, 2031, ["validate", "--config", str(config_dir)])
        assert proc.returncode == 0, proc.stderr
        assert "'2031'" in proc.stdout, (
            f"a run in 2031 should seed with 2031; got:\n{proc.stdout}"
        )

    def test_results_published_in_one_year_still_verify_in_a_later_one(
        self, tmp_path, real_inputs
    ):
        """The regression that matters: publish in 2026, verify in 2030."""
        config_dir, responses = real_inputs

        out = tmp_path / "published"
        published = _run_at_year(
            tmp_path, 2026,
            ["solve", "--config", str(config_dir), "--responses", str(responses),
             "--out", str(out), "--trials", "50"],
        )
        assert published.returncode == 0, published.stderr
        doc = json.loads((out / "results.json").read_text(encoding="utf-8"))
        assert doc["provenance"]["seed_string"] == "2026"

        verified = _run_at_year(
            tmp_path, 2030,
            ["verify", "--config", str(config_dir), "--responses", str(responses),
             "--results", str(out / "results.json")],
        )
        assert verified.returncode == 0, (
            "results published under seed_year=auto must still verify in a later "
            f"year -- verify must use the RECORDED seed, not today's.\n"
            f"stdout:\n{verified.stdout}\nstderr:\n{verified.stderr}"
        )
        assert "VERIFIED" in verified.stdout

    def test_two_different_years_really_do_produce_different_seeds(
        self, tmp_path, real_inputs
    ):
        """Guards against the opposite failure: a seed that silently never
        changes would make this whole feature a no-op."""
        config_dir, responses = real_inputs
        seeds = set()
        for year in (2026, 2027):
            out = tmp_path / f"y{year}"
            proc = _run_at_year(
                tmp_path, year,
                ["solve", "--config", str(config_dir), "--responses", str(responses),
                 "--out", str(out), "--trials", "50"],
            )
            assert proc.returncode == 0, proc.stderr
            doc = json.loads((out / "results.json").read_text(encoding="utf-8"))
            seeds.add(doc["provenance"]["seed_string"])
        assert seeds == {"2026", "2027"}

    def test_the_same_year_twice_is_byte_identical(self, tmp_path, real_inputs):
        config_dir, responses = real_inputs
        digests = []
        for run in ("a", "b"):
            out = tmp_path / run
            proc = _run_at_year(
                tmp_path, 2028,
                ["solve", "--config", str(config_dir), "--responses", str(responses),
                 "--out", str(out), "--trials", "50"],
            )
            assert proc.returncode == 0, proc.stderr
            digests.append((out / "results.json").read_bytes())
        assert digests[0] == digests[1], "I3 must hold under a clock-derived seed"

    def test_pinning_the_year_makes_the_clock_irrelevant(self, tmp_path, real_inputs):
        """Pinning is the documented way to re-run an old cycle."""
        config_dir, responses = real_inputs
        pinned = tmp_path / "config_pinned"
        import shutil

        shutil.copytree(config_dir, pinned)
        _write_scoring(pinned, seed_year=2026, tie_break_seed=None)

        outputs = []
        for year in (2026, 2033):
            out = tmp_path / f"pin{year}"
            proc = _run_at_year(
                tmp_path, year,
                ["solve", "--config", str(pinned), "--responses", str(responses),
                 "--out", str(out), "--trials", "50"],
            )
            assert proc.returncode == 0, proc.stderr
            outputs.append((out / "results.json").read_bytes())
        assert outputs[0] == outputs[1], (
            "with seed_year pinned, the wall-clock year must not change anything"
        )
