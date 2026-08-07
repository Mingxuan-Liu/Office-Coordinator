"""The roster must already reflect the pre-lock claims before a solve.

The two-phase process invites one silent mistake: run phase 1, skip
`tools/merge_keepers.py`, run phase 2. Nothing complains. The FORM still hides
claimed desks, because Code.gs reads the claim log directly, so phase 2 looks
right from every screen a human sees. But the solver reads the ROSTER, which
still says those desks are free, and it hands one to somebody else. The person
who was told "you are keeping this desk, there is nothing more for you to do"
finds out when the results are published.

`deskmatch solve --keepers <export>` is the gate. These tests are the reason to
trust it.
"""

from __future__ import annotations

import csv
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPO_ROOT, SOLVER_DIR
from deskmatch import keepers as keepers_mod
from deskmatch.config import load_config

HEADER = "claim_id,timestamp,email,name,desk_id,keeping,client_version"


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "deskmatch", *argv],
        capture_output=True, text=True, timeout=900, cwd=REPO_ROOT,
        env={"PYTHONPATH": str(SOLVER_DIR), "PATH": "/usr/bin:/bin"},
    )


def _log(path: Path, rows: list[str]) -> Path:
    path.write_text("\n".join([HEADER, *rows]) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def inputs(real_config_dir, real_responses_csv):
    if not real_responses_csv.is_file():
        pytest.skip("the shipped response export is not present")
    return real_config_dir, real_responses_csv


@pytest.fixture
def keeper(real_config):
    """A roster row that records a kept desk.

    config/roster.csv ships EMPTY, so this normally skips -- the cross-check it
    guards only has anything to reconcile when a roster exists. The tests that
    do not need one (below) build their claim logs directly.
    """
    who = [p for p in real_config.roster.people if p.keeps_desk and p.current_desk]
    if not who:
        pytest.skip("the shipped roster is empty, so there is nothing to reconcile")
    return who[0]


@pytest.fixture
def mover(real_config, real_responses_csv):
    """Somebody in the pool who is not keeping a desk.

    Taken from the responses when the roster is empty, which is the shipped
    state: the pool is whoever submitted.
    """
    who = [p for p in real_config.roster.people if not p.keeps_desk]
    if who:
        return who[0]

    from types import SimpleNamespace

    from deskmatch import responses as responses_mod

    if not real_responses_csv.is_file():
        pytest.skip("no roster and no response export to draw a person from")
    latest = responses_mod.load_responses(
        str(real_responses_csv), real_config.k
    ).latest
    if not latest:
        pytest.skip("no submissions to draw a person from")
    sub = latest[sorted(latest)[0]]
    return SimpleNamespace(
        email=sub.email, name=sub.name, keeps_desk=False, current_desk=None
    )


class TestTheGuard:
    def test_an_unmerged_claim_stops_the_run(self, tmp_path, inputs, mover, real_config):
        """The case that matters: somebody claimed a desk, the roster does not
        know, and without this the solver would give that desk away.

        Needs a config that HAS a roster. The shipped one is empty, and with no
        roster there is nothing for a claim to be out of sync with -- the claim
        log is simply used as the record. The guard exists for the other setup,
        where merge_keepers.py writes into a roster and can be skipped.
        """
        config_dir, responses = inputs
        config_dir = tmp_path / "config_with_roster"
        shutil.copytree(inputs[0], config_dir)
        (config_dir / "roster.csv").write_text(
            "name,email,year,candidacy,keeps_desk,current_desk\n"
            f"{mover.name},{mover.email},1,candidate,no,\n",
            encoding="utf-8",
        )
        real_config = load_config(config_dir)
        free = next(
            d.id for d in real_config.rooms.all_desks
            if d.id != (mover.current_desk or "")
        )
        log = _log(tmp_path / "k.csv", [
            f"k1,2026-09-05T10:00:00-04:00,{mover.email},{mover.name},{free},true,v1",
        ])
        proc = _run([
            "solve", "--config", str(config_dir), "--responses", str(responses),
            "--out", str(tmp_path / "out"), "--trials", "20", "--keepers", str(log),
        ])
        assert proc.returncode == 4, proc.stdout + proc.stderr
        blob = proc.stdout + proc.stderr
        assert mover.email in blob, "the message must name who is affected"
        assert free in blob, "and which desk"
        assert "merge_keepers" in blob, "and how to fix it"
        assert "Traceback" not in blob
        assert not (tmp_path / "out" / "results.json").exists(), (
            "nothing may be written when the inputs are known to be inconsistent"
        )

    def test_a_merged_claim_passes(self, tmp_path, inputs, keeper):
        config_dir, responses = inputs
        log = _log(tmp_path / "k.csv", [
            f"k1,2026-09-05T10:00:00-04:00,{keeper.email},{keeper.name},"
            f"{keeper.current_desk},true,v1",
        ])
        proc = _run([
            "solve", "--config", str(config_dir), "--responses", str(responses),
            "--out", str(tmp_path / "out"), "--trials", "20", "--keepers", str(log),
        ])
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "all reflected in the roster" in proc.stdout

    def test_a_release_after_the_merge_is_caught(self, tmp_path, inputs, keeper):
        """The other direction: the roster holds a desk out of the pool that
        nobody is actually keeping, so a desk silently goes unused."""
        config_dir, responses = inputs
        log = _log(tmp_path / "k.csv", [
            f"k1,2026-09-05T10:00:00-04:00,{keeper.email},{keeper.name},"
            f"{keeper.current_desk},true,v1",
            f"k2,2026-09-06T09:00:00-04:00,{keeper.email},{keeper.name},"
            f"{keeper.current_desk},false,v1",
        ])
        proc = _run([
            "solve", "--config", str(config_dir), "--responses", str(responses),
            "--out", str(tmp_path / "out"), "--trials", "20", "--keepers", str(log),
        ])
        assert proc.returncode == 4
        assert keeper.email in proc.stdout + proc.stderr

    def test_the_flag_is_optional(self, tmp_path, inputs):
        config_dir, responses = inputs
        proc = _run([
            "solve", "--config", str(config_dir), "--responses", str(responses),
            "--out", str(tmp_path / "out"), "--trials", "20",
        ])
        assert proc.returncode == 0, proc.stderr

    def test_a_malformed_log_is_readable_not_a_traceback(self, tmp_path, inputs):
        config_dir, responses = inputs
        bad = tmp_path / "bad.csv"
        bad.write_text("claim_id,timestamp,email\nx,y,z\n", encoding="utf-8")
        proc = _run([
            "solve", "--config", str(config_dir), "--responses", str(responses),
            "--out", str(tmp_path / "out"), "--trials", "20", "--keepers", str(bad),
        ])
        assert proc.returncode != 0
        assert "Traceback" not in proc.stderr, proc.stderr
        assert "desk_id" in proc.stdout + proc.stderr, "name the missing column"


class TestClaimResolution:
    def test_latest_claim_per_person_wins(self, tmp_path, real_config):
        """Same rule as responses and notes -- and it is literally the same
        function, so the three cannot drift apart."""
        log = _log(tmp_path / "k.csv", [
            "k1,2026-09-05T10:00:00-04:00,a@umich.edu,A,D01,true,v1",
            "k2,2026-09-05T12:00:00-04:00,a@umich.edu,A,D02,true,v1",
        ])
        claims = keepers_mod.load_claims(log)
        assert [c.desk_id for c in claims] == ["D02"]

    def test_a_release_is_not_an_active_claim(self, tmp_path):
        log = _log(tmp_path / "k.csv", [
            "k1,2026-09-05T10:00:00-04:00,a@umich.edu,A,D01,true,v1",
            "k2,2026-09-05T12:00:00-04:00,a@umich.edu,A,D01,false,v1",
        ])
        assert keepers_mod.load_claims(log) == ()

    def test_an_out_of_order_file_still_resolves_by_timestamp(self, tmp_path):
        log = _log(tmp_path / "k.csv", [
            "k2,2026-09-05T12:00:00-04:00,a@umich.edu,A,D02,true,v1",
            "k1,2026-09-05T10:00:00-04:00,a@umich.edu,A,D01,true,v1",
        ])
        assert [c.desk_id for c in keepers_mod.load_claims(log)] == ["D02"]
