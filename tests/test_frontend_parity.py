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


# ==========================================================================
# Pre-lock claim semantics
# ==========================================================================


@requires_js
def test_existing_claims_are_honoured_even_when_the_step_is_switched_off(tmp_path):
    """PRELOCK_ENABLED means "students may still claim", not "claims exist".

    Both `getBootstrap` and `submitRanking` must read the claim log
    unconditionally. If either gated on the switch, turning the step off after
    people had claimed would silently release their desks: the form would start
    offering them to everyone, and a student who was told "there is nothing else
    for you to do" would lose their seat without being asked. And if only *one*
    of the two gated, the form and the server would disagree -- the form would
    invite a ranking the server then refuses.

    Asserted against the source because the behaviour lives in Apps Script,
    which the Python suite cannot execute. Crude, but it pins the decision, and
    the comment above each site explains it.
    """
    code = (FRONTEND / "Code.gs").read_text(encoding="utf-8")

    # Claim *creation* must still be gated: no new claims once the step is off.
    claim_desk = code[code.index("function claimDesk"):]
    claim_desk = claim_desk[: claim_desk.index("\nfunction ")]
    assert "prelock.enabled" in claim_desk or "PRELOCK" in claim_desk, (
        "claimDesk must refuse new claims when the step is switched off"
    )

    # Claim *enforcement* must not be gated.
    submit = code[code.index("function submitRanking"):]
    submit = submit[: submit.index("\nfunction ")]
    read_at = submit.find("readKeepers_(")
    assert read_at != -1, "submitRanking must read the claim log"
    preceding = submit[:read_at]
    guard = preceding.rfind("if (person)")
    gated = preceding.rfind("prelockSettings_().enabled")
    assert gated == -1 or gated < guard, (
        "submitRanking must NOT gate reading the claim log on PRELOCK_ENABLED; "
        "switching the step off would silently free desks people are keeping"
    )


@requires_js
def test_the_mock_server_matches_that_rule(tmp_path):
    """A preview that disagrees with the server teaches the wrong thing."""
    mock = (FRONTEND / "mock_server.html").read_text(encoding="utf-8")
    body = mock[mock.index("function activeClaims()"):]
    body = body[: body.index("\n  function ")]
    assert "if (!PRELOCK_ENABLED) { return []; }" not in body, (
        "mock activeClaims() must not gate on PRELOCK_ENABLED -- the real server "
        "does not, so the preview would show a desk as free that the server "
        "would refuse"
    )


# ==========================================================================
# Phases: pre-lock and ranking are mutually exclusive
# ==========================================================================

# Runs the real Code.gs entry points under jsc with the script properties the
# coordinator would set. Every Sheet access in Code.gs is inside its own
# try/catch, so a harness with no spreadsheet exercises the genuine degraded
# paths -- and a submission that gets as far as "could not be saved" has, by
# construction, passed every validation before the write.
_PHASE_HARNESS = r"""
var R = %(repo)s;
var PROPS = JSON.parse(readFile(%(props)s));

var Session = {
  getActiveUser: function () { return { getEmail: function () { return ''; } }; },
  getScriptTimeZone: function () { return 'America/Detroit'; }
};
var SpreadsheetApp = { getActiveSpreadsheet: function () { return null; }, flush: function () {} };
var PropertiesService = {
  getScriptProperties: function () {
    return {
      getProperty: function (key) {
        return Object.prototype.hasOwnProperty.call(PROPS, key) ? PROPS[key] : null;
      }
    };
  }
};
var Utilities = {
  getUuid: function () { return 'uuid-0'; },
  formatDate: function () { return '2026-09-15T14:03:22-04:00'; }
};
var HtmlService = {};
var LockService = {
  getScriptLock: function () { return { waitLock: function () {}, releaseLock: function () {} }; }
};
var Logger = { log: function () {} };

(0, eval)(readFile(R + 'frontend/ConfigData.gs'));
(0, eval)(readFile(R + 'frontend/Code.gs'));

var payload = JSON.parse(readFile(%(payload)s));
var out = {};
try { out.phase = getBootstrap().phase; }
catch (e) { out.bootstrapError = String(e && e.message || e); }
try { out.submit = submitRanking(payload); }
catch (e) { out.submitError = String(e && e.message || e); }
try {
  out.claim = claimDesk({
    email: payload.email, name: payload.name,
    deskId: payload.choices[0], keeping: true, clientVersion: 'test'
  });
} catch (e) { out.claimError = String(e && e.message || e); }
print(JSON.stringify(out));
"""


def _valid_payload() -> dict:
    """One ranking that passes every check in submitRanking() except the write.

    Derived from config/, never written down: K comes from the scoring curve and
    the desks come from whichever zones the rule table opens to this person.
    """
    config = load_config(CONFIG_DIR)
    k = config.scoring.k

    held = {p.current_desk for p in config.roster.people if p.keeps_desk}
    # Synthesised, not taken from the roster. config/roster.csv ships EMPTY now
    # -- the domain-restricted link is the membership check -- so pulling a
    # person out of it raised StopIteration. Building one here is also the more
    # faithful fixture: somebody the roster does not list is the normal case,
    # not the exceptional one.
    candidacy = _some_candidacy(config)
    person = Person(
        email="parity.fixture@umich.edu", name="Parity Fixture", year=0,
        candidacy=candidacy, keeps_desk=False, current_desk=None,
        attributes={"email": "parity.fixture@umich.edu", "name": "Parity Fixture",
                    "year": 0, "candidacy": candidacy, "keeps_desk": "no",
                    "current_desk": ""},
    )
    zones = set(elig.allowed_zones(config.eligibility, config.rooms, person))
    desks = [
        d.id
        for d in config.rooms.all_desks
        if d.available and d.zone in zones and d.id not in held
    ]
    assert len(desks) >= k, (
        f"config/ leaves {person.email} only {len(desks)} desk(s) for K={k}; "
        "this fixture needs a person who can actually submit"
    )
    return {
        "email": person.email,
        "name": person.name,
        "candidacy": person.candidacy,
        "choices": desks[:k],
        "authMethod": "google",
        "clientVersion": "test",
    }


def _some_candidacy(config) -> str:
    """A candidacy the rule table actually opens desks to.

    Read off eligibility.json rather than hard-coded, so renaming a cohort does
    not quietly turn this fixture into a person with nowhere to sit.
    """
    best, best_n = "", -1
    seen = [
        str(rule.when["candidacy"])
        for rule in config.eligibility.rules
        if isinstance(rule.when.get("candidacy"), str)
    ] + ["candidate"]
    for value in seen:
        probe = Person(
            email="probe@umich.edu", name="Probe", year=0, candidacy=value,
            keeps_desk=False, current_desk=None,
            attributes={"email": "probe@umich.edu", "name": "Probe", "year": 0,
                        "candidacy": value, "keeps_desk": "no", "current_desk": ""},
        )
        zones = set(elig.allowed_zones(config.eligibility, config.rooms, probe))
        n = sum(1 for d in config.rooms.all_desks if d.available and d.zone in zones)
        if n > best_n:
            best, best_n = value, n
    return best


def _run_phase(tmp_path: Path, props: dict, tag: str) -> dict:
    payload_path = tmp_path / f"payload-{tag}.json"
    payload_path.write_text(json.dumps(_valid_payload()), encoding="utf-8")
    props_path = tmp_path / f"props-{tag}.json"
    props_path.write_text(json.dumps(props), encoding="utf-8")

    harness = tmp_path / f"phase-{tag}.js"
    harness.write_text(
        _PHASE_HARNESS % {
            "repo": json.dumps(str(REPO) + os.sep),
            "props": json.dumps(str(props_path)),
            "payload": json.dumps(str(payload_path)),
        },
        encoding="utf-8",
    )
    proc = subprocess.run(
        [_jsc(), str(harness)], capture_output=True, text=True, timeout=180, cwd=tmp_path
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _errors(result: dict | None) -> str:
    return " ".join((result or {}).get("errors") or [])


@requires_js
def test_submit_ranking_refuses_while_the_prelock_phase_is_on(tmp_path):
    """PRELOCK_ENABLED says which PHASE the cycle is in, and phase 1 shuts ranking.

    The client greys Choose and Confirm out, but the client is not trusted, and
    the reason this has to be enforced is not tidiness: during phase 1 the
    roster has not been updated with the keepers yet, so a ranking taken then is
    computed against a stale pool. The desks that are about to leave it still
    look available, and the student ends up having ranked desks nobody can be
    given.

    Run against the real Code.gs, not grepped, so the check cannot be satisfied
    by a comment.
    """
    on = _run_phase(tmp_path, {"RESPONSE_SHEET_NAME": "Responses",
                               "PRELOCK_ENABLED": "true"}, "on")

    assert on["phase"] == "prelock", (
        "getBootstrap() must report the phase outright so the client does not "
        f"have to infer it from prelock.enabled; got {on.get('phase')!r}"
    )
    assert on["submit"]["ok"] is False, (
        "submitRanking() accepted a ranking during the pre-lock phase"
    )
    assert "not opened yet" in _errors(on["submit"]), (
        "the refusal must say ranking has not opened yet, not blame something "
        f"else: {_errors(on['submit'])!r}"
    )
    # Claiming is the thing phase 1 is FOR, so it must not be refused as a phase
    # error. With no spreadsheet in the harness it fails at the write instead.
    assert "not open" not in _errors(on["claim"]), (
        "claimDesk() must work during the pre-lock phase: " + _errors(on["claim"])
    )


@requires_js
def test_the_ranking_phase_opens_ranking_and_shuts_claims(tmp_path):
    """The mirror image, and the half that proves the refusal is phase-specific.

    With PRELOCK_ENABLED off, the same payload must get all the way to the write
    -- i.e. past the deadline check, the roster lookup, the claim check, the
    eligibility check and all K choices -- and fail only because this harness
    has no spreadsheet. If it failed earlier, the phase gate would be rejecting
    rankings it has no business rejecting.
    """
    for tag, props in (
        ("off", {"RESPONSE_SHEET_NAME": "Responses", "PRELOCK_ENABLED": "false"}),
        ("unset", {"RESPONSE_SHEET_NAME": "Responses"}),
    ):
        out = _run_phase(tmp_path, props, tag)

        assert out["phase"] == "ranking", (
            f"PRELOCK_ENABLED={props.get('PRELOCK_ENABLED')!r} is the ranking "
            f"phase; got {out.get('phase')!r}"
        )
        assert "not opened yet" not in _errors(out["submit"]), (
            "submitRanking() refused a ranking during the ranking phase: "
            + _errors(out["submit"])
        )
        assert "could not be saved" in _errors(out["submit"]), (
            "the ranking should have passed every check and failed only at the "
            "write; it was rejected earlier, so something else refused it: "
            + _errors(out["submit"])
        )
        assert out["claim"]["ok"] is False and "not open" in _errors(out["claim"]), (
            "claimDesk() must refuse a NEW claim outside the pre-lock phase: "
            + _errors(out["claim"])
        )


@requires_js
def test_the_mock_server_mirrors_the_phase(tmp_path):
    """?mockPrelock drives the same two phases, or the preview teaches a lie."""
    mock = (FRONTEND / "mock_server.html").read_text(encoding="utf-8")

    assert "?mockPrelock=on|off" in mock, "the preview switch must keep its name"
    assert "phase: PHASE," in mock, (
        "the mock bootstrap must send bootstrap.phase, as Code.gs does"
    )

    submit = mock[mock.index("function submitRanking(payload)"):]
    submit = submit[: submit.index("\n  function ")]
    assert "PHASE === 'prelock'" in submit, (
        "the mock submitRanking() must refuse during the pre-lock phase; the "
        "real server does, so a preview that accepts would teach the coordinator "
        "that the greyed-out chips are only decoration"
    )


@requires_js
def test_the_client_shuts_the_other_half_of_the_flow(tmp_path):
    """Both halves are greyed by the SAME mechanism, in one place.

    setStepEnabled() is what already greyed Pre-lock out -- disabled plus
    aria-disabled plus a tooltip, struck through by Style.html. Choose and
    Confirm go through it too rather than growing a second way to be shut, and
    the guards refuse entry so a stale link or a restored session cannot walk
    round the greying.
    """
    core = (FRONTEND / "JsCore.html").read_text(encoding="utf-8")

    chrome = core[core.index("function applyPhaseChrome()"):]
    chrome = chrome[: chrome.index("\n  /*")]
    for step in ("'select'", "'confirm'", "'prelock'"):
        assert step in chrome, f"applyPhaseChrome() must switch {step} for the phase"
    assert "setStepEnabled(" in chrome, (
        "reuse setStepEnabled() rather than inventing a second greying mechanism"
    )

    for view in ("select", "confirm"):
        guard = core[core.index("    %s: function () {" % view):]
        guard = guard[: guard.index("\n    },")]
        assert "requireRankingPhase()" in guard, (
            f"the {view} guard must refuse entry outside the ranking phase, so "
            "APP.go() into a closed view lands somewhere sensible"
        )


@requires_js
def test_calibration_tool_round_trips_the_real_config(tmp_path):
    """Import config/rooms.json into the calibration tool and export it back:
    the bytes must be unchanged.

    The tool is the coordinator's only way to check the map by eye, so merely
    *opening* it must never alter the config. Two ways that has already gone
    wrong and would go wrong silently:

      * it re-emitted `"image": ""` for rooms that had no image, and the loader
        rejects an empty string outright -- so a look-but-don't-touch visit
        produced a config that would not load;
      * a new feature key (`swing`) could be dropped, quietly reverting a door
        the coordinator had deliberately reversed.

    Byte-equality catches both, and it also means a git diff after opening the
    tool shows only what was actually changed.
    """
    harness = tmp_path / "roundtrip.js"
    harness.write_text(
        _CALIBRATE_HARNESS % {
            "script": json.dumps(str(tmp_path / "calib.js")),
            "rooms": json.dumps(str(CONFIG_DIR / "rooms.json")),
        },
        encoding="utf-8",
    )
    import re as _re

    html = (REPO / "tools" / "calibrate" / "index.html").read_text(encoding="utf-8")
    (tmp_path / "calib.js").write_text(
        "\n".join(_re.findall(r"<script[^>]*>(.*?)</script>", html, _re.S)),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [_jsc(), str(harness)], capture_output=True, text=True, timeout=180, cwd=tmp_path
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout}\n{proc.stderr}"
    out = proc.stdout.strip()
    assert "ROUNDTRIP OK" in out, (
        "the calibration tool changed config/rooms.json just by importing and "
        f"re-exporting it:\n{out}"
    )


_CALIBRATE_HARNESS = r"""
var _n = function () {};
function mkEl() {
  return { style:{setProperty:_n,removeProperty:_n},
    classList:{add:_n,remove:_n,toggle:_n,contains:function(){return false}},
    dataset:{}, children:[], value:'', textContent:'', innerHTML:'', checked:false, files:[],
    appendChild:function(c){return c}, removeChild:_n, insertBefore:_n,
    addEventListener:_n, removeEventListener:_n, setAttribute:_n,
    getAttribute:function(){return null}, removeAttribute:_n,
    querySelector:function(){return mkEl()}, querySelectorAll:function(){return[]},
    getContext:function(){return new Proxy({},{get:function(){return _n}})},
    getBoundingClientRect:function(){return{left:0,top:0,width:800,height:600}},
    focus:_n, blur:_n, click:_n, closest:function(){return null}, remove:_n,
    scrollIntoView:_n, replaceChildren:_n, cloneNode:function(){return mkEl()} };
}
var document = { getElementById:function(){return mkEl()}, createElement:function(){return mkEl()},
  createElementNS:function(){return mkEl()}, querySelector:function(){return mkEl()},
  querySelectorAll:function(){return[]}, addEventListener:_n, removeEventListener:_n,
  body:mkEl(), documentElement:mkEl(), readyState:'loading',
  createTextNode:function(){return mkEl()} };
var window = { addEventListener:_n, removeEventListener:_n, localStorage:null,
  devicePixelRatio:1, innerWidth:1200, innerHeight:800,
  matchMedia:function(){return{matches:false,addEventListener:_n}},
  requestAnimationFrame:_n, setTimeout:_n, clearTimeout:_n, location:{href:'file:///x'} };
var navigator = { userAgent:'jsc' }, localStorage = null;
var requestAnimationFrame = _n, cancelAnimationFrame = _n;
var setTimeout = function(){return 0}, clearTimeout = _n;
var setInterval = function(){return 0}, clearInterval = _n;
var Image = function(){return mkEl()};
var FileReader = function(){return {readAsDataURL:_n, addEventListener:_n}};
var URL = { createObjectURL:function(){return 'blob:x'}, revokeObjectURL:_n };
var Blob = function(){return {}};
var alert = _n, confirm = function(){return true}, prompt = function(){return ''};

try { (0, eval)(readFile(%(script)s)); } catch (e) { print('BOOT ' + e.message); }
if (!window.CAL) { print('NO CAL HOOK'); }
else {
  var raw = readFile(%(rooms)s);
  var res = window.CAL.importRooms(JSON.parse(raw));
  var out = JSON.stringify(window.CAL.buildExport(res.doc, 'normalized', {decimals:4}), null, 2) + "\n";
  if (out === raw) { print('ROUNDTRIP OK'); }
  else {
    var a = raw.split('\n'), b = out.split('\n'), shown = 0;
    for (var i = 0; i < Math.max(a.length, b.length) && shown < 6; i++) {
      if (a[i] !== b[i]) {
        print('line ' + (i+1) + '\n  was: ' + a[i] + '\n  now: ' + b[i]);
        shown++;
      }
    }
  }
}
"""


@requires_js
def test_correcting_your_candidacy_never_leaves_you_with_no_zone(tmp_path):
    """The submitted candidacy governs, on its own.

    `getEligibleZonesForClaim` used to return the INTERSECTION of the roster's
    zones and the submitted ones. That was invisible while candidates were
    allowed everywhere, because intersecting with "*" is a no-op. The moment
    candidates were restricted, a student whose roster row said "candidate"
    correcting themselves to "precandidate" got
    {candidate_side, senior_office} ∩ {precandidate_side} = {} and was told they
    had "no zone at all" -- a dead end reached by using the field exactly as
    intended, on the one screen where a student cannot route around it.

    So: for every roster member crossed with every candidacy the rules mention,
    the answer must be non-empty AND must equal what the Python rule table gives
    for that candidacy alone, ignoring the roster.
    """
    config = load_config(CONFIG_DIR)

    candidacies = sorted(
        {
            str(v).strip()
            for rule in config.eligibility.rules
            for v in (
                [rule.when.get("candidacy")]
                if not isinstance(rule.when.get("candidacy"), (list, tuple))
                else list(rule.when["candidacy"])
            )
            if v
        }
        | {p.candidacy for p in config.roster.people if p.candidacy}
    )
    assert candidacies, "no candidacy values to test"

    cases = [
        {"email": p.email, "candidacy": c}
        for p in config.roster.people
        for c in candidacies
    ]
    cases_path = tmp_path / "claims.json"
    cases_path.write_text(json.dumps(cases), encoding="utf-8")

    harness = tmp_path / "claims.js"
    harness.write_text(
        _CLAIM_HARNESS % {
            "repo": json.dumps(str(REPO) + os.sep),
            "cases": json.dumps(str(cases_path)),
        },
        encoding="utf-8",
    )
    proc = subprocess.run(
        [_jsc(), str(harness)], capture_output=True, text=True, timeout=180, cwd=tmp_path
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout}\n{proc.stderr}"
    got = {
        (row["email"], row["candidacy"]): sorted(row["zones"] or [])
        for row in json.loads(proc.stdout.strip().splitlines()[-1])
    }

    empties, mismatches = [], []
    for case in cases:
        person = config.roster.by_email(case["email"])
        corrected = Person(
            email=person.email, name=person.name, year=person.year,
            candidacy=case["candidacy"], keeps_desk=person.keeps_desk,
            current_desk=person.current_desk,
            attributes={**dict(person.attributes), "candidacy": case["candidacy"]},
        )
        expected = sorted(
            elig.allowed_zones(config.eligibility, config.rooms, corrected)
        )
        actual = got.get((case["email"], case["candidacy"]))
        if not actual:
            empties.append(f"  {case['email']} as {case['candidacy']!r} -> no zones")
        elif actual != expected:
            mismatches.append(
                f"  {case['email']} as {case['candidacy']!r}: "
                f"apps_script={actual} python={expected}"
            )

    assert not empties, (
        "correcting your candidacy must never leave you with nowhere to sit:\n"
        + "\n".join(empties)
    )
    assert not mismatches, (
        "the form and the solver disagree about a corrected candidacy:\n"
        + "\n".join(mismatches)
    )


_CLAIM_HARNESS = r"""
var R = %(repo)s;
var Session = { getActiveUser: function () { return { getEmail: function () { return ''; } }; } };
var SpreadsheetApp = {};
var PropertiesService = { getScriptProperties: function () { return { getProperty: function () { return null; } }; } };
var Utilities = { getUuid: function () { return 'u'; }, formatDate: function () { return ''; } };
var HtmlService = {}, LockService = {}, Logger = { log: function () {} };

(0, eval)(readFile(R + 'frontend/ConfigData.gs'));
(0, eval)(readFile(R + 'frontend/Code.gs'));

var cases = JSON.parse(readFile(%(cases)s));
var out = [];
cases.forEach(function (c) {
  var res;
  try { res = getEligibleZonesForClaim(c); }
  catch (e) { res = { zones: [], error: String(e && e.message || e) }; }
  out.push({ email: c.email, candidacy: c.candidacy, zones: res.zones || [] });
});
print(JSON.stringify(out));
"""


# ==========================================================================
# The year: collected and recorded by Code.gs, and an input to nothing
# ==========================================================================

_YEAR_HARNESS = r"""
var R = %(repo)s;
var Session = { getActiveUser: function () { return { getEmail: function () { return ''; } }; } };
var SpreadsheetApp = {};
var PropertiesService = { getScriptProperties: function () { return { getProperty: function () { return null; } }; } };
var Utilities = { getUuid: function () { return 'u'; }, formatDate: function () { return ''; } };
var HtmlService = {}, LockService = {}, Logger = { log: function () {} };

(0, eval)(readFile(R + 'frontend/ConfigData.gs'));
(0, eval)(readFile(R + 'frontend/Code.gs'));

var input = JSON.parse(readFile(%(cases)s));
var out = {
  header: responseHeader_(input.k),
  confirmed: CONFIRMED_FIELDS.slice(),
  claims: []
};
input.claims.forEach(function (c) {
  var res;
  try { res = getEligibleZonesForClaim(c); }
  catch (e) { res = { zones: [], error: String(e && e.message || e) }; }
  out.claims.push({
    email: c.email,
    candidacy: c.candidacy,
    year: c.year === undefined ? null : c.year,
    zones: (res.zones || []).slice().sort(),
    error: res.error || null
  });
});
print(JSON.stringify(out));
"""


def _run_year_harness(tmp_path: Path, payload: dict) -> dict:
    cases_path = tmp_path / "yearcases.json"
    cases_path.write_text(json.dumps(payload), encoding="utf-8")
    harness = tmp_path / "year.js"
    harness.write_text(
        _YEAR_HARNESS % {
            "repo": json.dumps(str(REPO) + os.sep),
            "cases": json.dumps(str(cases_path)),
        },
        encoding="utf-8",
    )
    proc = subprocess.run(
        [_jsc(), str(harness)], capture_output=True, text=True, timeout=180, cwd=tmp_path
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@requires_js
def test_the_sheet_header_is_the_one_the_solver_reads(tmp_path):
    """Code.gs writes the column order `responses.py` expects, `year` included.

    The response CSV is the entire contract between the two halves (SPEC §3.1),
    and the loader is positional in exactly one place that matters: a header
    whose `year` and `candidacy` are swapped produces a file that still parses,
    still has the right number of columns, and has every student's candidacy in
    the year column. That survives to a finished assignment.

    The header has changed twice now -- `year` dropped, then restored after
    `candidacy` rather than before it -- which is precisely how the two sides
    drift, so compare them rather than trusting two documents to have been
    updated together.
    """
    from deskmatch import responses as responses_mod

    config = load_config(CONFIG_DIR)
    k = len(config.scoring.curve(config.scoring.primary_curve))
    result = _run_year_harness(tmp_path, {"k": k, "claims": []})

    expected = list(responses_mod.canonical_header(k))
    assert result["header"] == expected, (
        "Code.gs and deskmatch.responses disagree about the response header.\n"
        f"  Code.gs:    {result['header']}\n"
        f"  responses:  {expected}"
    )
    assert "year" in expected, "the form collects a year again; it must be a column"
    assert expected.index("year") == expected.index("candidacy") + 1, (
        "SPEC §3.1 puts `year` immediately after `candidacy`"
    )


@requires_js
def test_a_year_in_the_claim_cannot_change_the_zones_code_gs_offers(tmp_path):
    """The client is not trusted, and the year is not an input.

    `getEligibleZonesForClaim` is what the identity screen calls after every
    edit, and its answer is what the student is shown as "you may choose from".
    The browser does not send a year -- but "the client does not send it" is not
    a guarantee, it is a habit. `CONFIRMED_FIELDS` is the guarantee: it lists
    the fields a claim may override, and `year` is not in it, so a claim that
    carries one gets the roster's value regardless.

    Checked here rather than trusted, because the day somebody writes a
    `{"year": [1, 2]}` rule (SPEC §2.2 allows it) this stops being theoretical
    and starts deciding where people sit.
    """
    config = load_config(CONFIG_DIR)
    k = len(config.scoring.curve(config.scoring.primary_curve))

    candidacies = sorted({p.candidacy for p in config.roster.people if p.candidacy})
    years = (1, 2, 9, 0, -3, "third", "", None)
    claims = []
    for person in config.roster.people:
        for candidacy in candidacies:
            for year in years:
                claim = {"email": person.email, "candidacy": candidacy}
                if year is not None:
                    claim["year"] = year
                claims.append(claim)

    result = _run_year_harness(tmp_path, {"k": k, "claims": claims})

    assert result["confirmed"] == ["candidacy"], (
        "Code.gs CONFIRMED_FIELDS is the list of roster attributes a student's "
        "own answer may override in the eligibility inputs. Adding `year` to it "
        "would let a field the form describes as recorded-only decide which "
        f"desks somebody may rank. Got: {result['confirmed']}"
    )

    baseline: dict[tuple[str, str], list[str]] = {}
    drift = []
    for row in result["claims"]:
        assert row["error"] is None, f"{row['email']}: {row['error']}"
        key = (row["email"], row["candidacy"])
        if row["year"] is None:
            baseline[key] = row["zones"]
    for row in result["claims"]:
        key = (row["email"], row["candidacy"])
        if row["zones"] != baseline[key]:
            drift.append(
                f"  {row['email']} as {row['candidacy']!r} with year="
                f"{row['year']!r}: {row['zones']} instead of {baseline[key]}"
            )

    assert not drift, (
        "a year sent with the claim changed which zones Code.gs offered. The "
        "year is collected and recorded; candidacy alone decides zones "
        "(SPEC §3.1, §3.3):\n" + "\n".join(drift)
    )
