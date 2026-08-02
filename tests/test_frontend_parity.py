"""Cross-language parity: the Apps Script rule-table interpreter must agree with
the Python one.

Why this test exists
--------------------
Zone eligibility is implemented twice, on purpose. The frontend has to grey out
ineligible desks while the student is choosing (Apps Script, `Code.gs`), and the
solver has to enforce it again on the server side (Python, `eligibility.py`) --
"the client is not trusted, even in a friendly department".

Two implementations of one rule means they can drift. If they drift, the failure
is nasty and quiet: the form offers a desk the solver will later refuse, and the
student's choice is silently dropped, costing them a rank for no visible reason.
Nothing else in the test suite would catch that, because each side is
individually correct.

So: run both over the same sweep of roster attributes and demand identical
answers.

Requires JavaScriptCore (`jsc`), which ships with macOS. There is no node on the
target machine. Skipped cleanly where jsc is unavailable, so CI on Linux does not
go red -- but do not let it stay skipped forever on the machine that matters.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from deskmatch import eligibility as elig
from deskmatch.config import load_config
from deskmatch.types import Person

REPO = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO / "config"
FRONTEND = REPO / "frontend"

JSC_CANDIDATES = [
    "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc",
    shutil.which("jsc") or "",
    shutil.which("node") or "",
]


def _jsc() -> str | None:
    for path in JSC_CANDIDATES:
        if path and Path(path).exists():
            return path
    return None


requires_js = pytest.mark.skipif(
    _jsc() is None, reason="no JavaScriptCore/node available to run the Apps Script side"
)


# The sweep. Deliberately includes the messy values a real roster produces:
# mixed case, stray whitespace, a candidacy nobody wrote a rule for, an empty
# string, and years arriving as strings because they came from a CSV.
YEARS: tuple[object, ...] = (1, 2, 3, 4, 5, 8, "1", "2", "4")
CANDIDACIES: tuple[str, ...] = (
    "precandidate",
    "candidate",
    "Precandidate",
    "  PRECANDIDATE  ",
    "postcandidate",
    "",
)


def _cases() -> list[dict]:
    out = []
    for year in YEARS:
        for candidacy in CANDIDACIES:
            out.append(
                {
                    "email": f"case{len(out)}@umich.edu",
                    "name": f"Case {len(out)}",
                    "year": year,
                    "candidacy": candidacy,
                    "keeps_desk": False,
                    "current_desk": "",
                }
            )
    return out


_HARNESS = r"""
var R = %(repo)s;
/* Minimal Apps Script global stubs: enough for Code.gs to parse and for the
   pure rule-table functions to run. None of these are exercised by
   getEligibleZones itself. */
var Session = { getActiveUser: function () { return { getEmail: function () { return ''; } }; } };
var SpreadsheetApp = {};
var PropertiesService = { getScriptProperties: function () { return { getProperty: function () { return null; } }; } };
var Utilities = { getUuid: function () { return 'u'; }, formatDate: function () { return ''; } };
var HtmlService = {};
var LockService = {};
var Logger = { log: function () {} };

(0, eval)(readFile(R + 'frontend/ConfigData.gs'));
(0, eval)(readFile(R + 'frontend/Code.gs'));

if (typeof getEligibleZones !== 'function') {
  print(JSON.stringify({ error: 'getEligibleZones is not defined in Code.gs' }));
} else {
  var cases = JSON.parse(readFile(%(cases)s));
  var out = [];
  cases.forEach(function (p) {
    var res, zones = null, err = null;
    try {
      res = getEligibleZones(p);
      zones = Array.isArray(res) ? res : (res && res.zones ? res.zones : res);
      if (zones && zones.slice) zones = zones.slice().sort();
    } catch (e) {
      err = String(e && e.message || e);
    }
    out.push({ email: p.email, zones: zones, error: err });
  });
  print(JSON.stringify(out));
}
"""


def _run_js(tmp_path: Path, cases: list[dict]) -> dict[str, list[str]]:
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps(cases), encoding="utf-8")

    harness = _HARNESS % {
        "repo": json.dumps(str(REPO) + os.sep),
        "cases": json.dumps(str(cases_path)),
    }
    harness_path = tmp_path / "harness.js"
    harness_path.write_text(harness, encoding="utf-8")

    proc = subprocess.run(
        [_jsc(), str(harness_path)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, (
        f"the Apps Script harness failed (exit {proc.returncode}):\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert not isinstance(payload, dict), f"harness error: {payload}"

    result: dict[str, list[str]] = {}
    for row in payload:
        assert row["error"] is None, (
            f"Code.gs getEligibleZones threw for {row['email']}: {row['error']}"
        )
        result[row["email"]] = sorted(row["zones"] or [])
    return result


def _python_zones(config, case: dict) -> list[str]:
    person = Person(
        email=case["email"],
        name=case["name"],
        year=case["year"],
        candidacy=case["candidacy"],
        keeps_desk=False,
        current_desk=None,
        attributes={
            "email": case["email"],
            "name": case["name"],
            "year": case["year"],
            "candidacy": case["candidacy"],
            "keeps_desk": "no",
        },
    )
    return sorted(elig.allowed_zones(config.eligibility, config.rooms, person))


@requires_js
def test_eligibility_matches_between_python_and_apps_script(tmp_path):
    """The two rule-table interpreters must return identical zone sets."""
    config = load_config(CONFIG_DIR)
    cases = _cases()
    js_result = _run_js(tmp_path, cases)

    mismatches = []
    for case in cases:
        expected = _python_zones(config, case)
        actual = js_result.get(case["email"])
        if actual != expected:
            mismatches.append(
                f"  year={case['year']!r} candidacy={case['candidacy']!r}: "
                f"python={expected} apps_script={actual}"
            )

    assert not mismatches, (
        "the Apps Script and Python eligibility interpreters disagree. The form "
        "would offer desks the solver later refuses.\n" + "\n".join(mismatches)
    )
    # Guard against the test passing vacuously if the sweep ever empties out.
    assert len(js_result) == len(cases) >= len(YEARS)


@requires_js
def test_apps_script_sources_parse(tmp_path):
    """Every .gs file must at least parse. Apps Script gives no build step, so a
    syntax error is discovered by a student staring at a blank page."""
    sources = sorted(FRONTEND.glob("*.gs"))
    assert sources, "no .gs files found in frontend/"

    checks = "\n".join(
        "try { new Function(readFile(%s)); print('OK %s'); } "
        "catch (e) { print('ERR %s: ' + e.message); }"
        % (json.dumps(str(path)), path.name, path.name)
        for path in sources
    )
    script = tmp_path / "parse.js"
    script.write_text(checks, encoding="utf-8")

    proc = subprocess.run(
        [_jsc(), str(script)], capture_output=True, text=True, timeout=120
    )
    failures = [ln for ln in proc.stdout.splitlines() if ln.startswith("ERR")]
    assert not failures, "Apps Script source(s) failed to parse:\n" + "\n".join(failures)


@requires_js
def test_config_data_gs_is_in_sync_with_config_dir(tmp_path):
    """ConfigData.gs is generated from config/. If someone edits rooms.json and
    forgets to re-run tools/sync_config.py, the form and the solver disagree
    about which desks exist -- so make that a test failure rather than a
    deployment surprise."""
    sync = REPO / "tools" / "sync_config.py"
    if not sync.exists():
        pytest.skip("tools/sync_config.py not present")

    proc = subprocess.run(
        ["python3", str(sync), "--config-dir", str(CONFIG_DIR),
         "--out", str(FRONTEND / "ConfigData.gs"), "--check"],
        capture_output=True, text=True, timeout=120, cwd=REPO,
    )
    assert proc.returncode == 0, (
        "frontend/ConfigData.gs is stale relative to config/. Re-run:\n"
        "  python tools/sync_config.py --config-dir config/ "
        "--out frontend/ConfigData.gs\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
