"""Shared fixtures for the deskmatch test suite.

Two rules govern everything in here, and they come straight from docs/SPEC.md:

* **Nothing knows how big the problem is (invariant I1).** Every fixture is a
  factory parameterised by ``(n_people, n_desks, K)``, and every helper derives
  its indices from the document it is handed -- ``rooms["rooms"][-1]["desks"][0]``
  rather than ``desks[13]``. A test that only passes at one size is not testing
  the invariant.

* **Nothing depends on the clock, the environment, or file order (I3).** The
  synthetic worlds are seeded from a fixed string, the roster/response rows are
  written in an order the fixture chose deliberately, and no fixture reads the
  wall clock.

The baseline configs are built by ``deskmatch.synth`` rather than hand-written
JSON, so a test that breaks one rule is breaking it against a document the real
loaders otherwise accept clean -- which is what makes "the specific error fires"
a meaningful assertion rather than an artefact of a fixture that was broken in
five other ways to begin with.
"""

from __future__ import annotations

import copy
import csv
import io
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
SOLVER_DIR = REPO_ROOT / "solver"

# The documented invocation is `PYTHONPATH=solver python3 -m pytest tests/`, but
# a bare `pytest tests/` from the repo root must not silently collect nothing.
if str(SOLVER_DIR) not in sys.path:
    sys.path.insert(0, str(SOLVER_DIR))

from deskmatch import responses as responses_mod  # noqa: E402
from deskmatch import synth  # noqa: E402
from deskmatch.config import load_config  # noqa: E402
from deskmatch.errors import ConfigError, Problem, ResponseError  # noqa: E402
from deskmatch.types import Config  # noqa: E402

#: Small floor-plan images: the validator only checks that the file exists and
#: that image_size is a pair of positive ints, so there is no reason to pay for
#: a megapixel PNG in every fixture.
FIXTURE_IMAGE_SIZE = (240, 160)


# --------------------------------------------------------------------------
# Serialisation helpers (mirror what deskmatch.synth writes)
# --------------------------------------------------------------------------


def dump_json_text(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def dump_csv_text(fieldnames: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> str:
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(
        buf, fieldnames=list(fieldnames), lineterminator="\n", extrasaction="ignore"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in fieldnames})
    return buf.getvalue()


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" so nothing is translated: the same bytes on every platform.
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


# --------------------------------------------------------------------------
# Problem-list assertions
# --------------------------------------------------------------------------


def render_all(problems: Iterable[Problem]) -> str:
    return "\n".join(f"  [{i + 1}] {p.render()}" for i, p in enumerate(problems))


def find_problem(
    problems: Sequence[Problem],
    where: str = "",
    what: str = "",
    hint: str = "",
) -> Problem:
    """The single problem matching all given substrings. Fails loudly otherwise.

    Failure prints every problem that *was* reported: when a validator message
    changes, the diff is what tells you whether the rule stopped firing or just
    got reworded.
    """
    hits = [
        p
        for p in problems
        if where in p.where and what in p.what and hint in p.hint
    ]
    if len(hits) == 1:
        return hits[0]
    wanted = ", ".join(
        f"{name}={value!r}"
        for name, value in (("where", where), ("what", what), ("hint", hint))
        if value
    )
    raise AssertionError(
        f"expected exactly one problem matching {wanted}, found {len(hits)}.\n"
        f"All {len(problems)} problem(s) reported:\n{render_all(problems)}"
    )


def find_warning(warnings: Sequence[str], *fragments: str) -> str:
    """The single rendered warning containing every fragment."""
    hits = [w for w in warnings if all(f in w for f in fragments)]
    if len(hits) == 1:
        return hits[0]
    listing = "\n".join(f"  [{i + 1}] {w}" for i, w in enumerate(warnings))
    raise AssertionError(
        f"expected exactly one warning containing {fragments!r}, found {len(hits)}.\n"
        f"All {len(warnings)} warning(s):\n{listing}"
    )


def assert_no_problem(problems: Sequence[Problem], where: str = "", what: str = "") -> None:
    hits = [p for p in problems if where in p.where and what in p.what]
    assert not hits, (
        f"expected no problem matching where={where!r} what={what!r}, got:\n"
        + render_all(hits)
    )


# --------------------------------------------------------------------------
# A mutable config directory
# --------------------------------------------------------------------------


@dataclass
class ConfigCase:
    """A valid config on disk, plus the parsed documents, ready to be broken.

    The four documents are plain Python objects. A test mutates whichever one it
    is about and calls :meth:`errors` (expects rejection) or :meth:`load`
    (expects acceptance); both re-serialise everything first, so the files on
    disk and the objects in memory can never drift.
    """

    path: Path
    responses_path: Path
    rooms: dict[str, Any]
    eligibility: dict[str, Any]
    scoring: dict[str, Any]
    roster_fields: list[str]
    roster_rows: list[dict[str, str]]
    world: Any = None
    raw_overrides: dict[str, bytes] = field(default_factory=dict)
    deleted: set[str] = field(default_factory=set)

    # -- derived sizes: counted, never stored (invariant I1) --------------

    @property
    def k(self) -> int:
        return len(self.scoring["curves"][self.scoring["primary_curve"]])

    @property
    def zone_ids(self) -> list[str]:
        return [z for z in self.rooms["zones"] if not str(z).startswith("_")]

    @property
    def desk_ids(self) -> list[str]:
        return [d["id"] for r in self.rooms["rooms"] for d in r["desks"]]

    def desk_at(self, room_index: int = 0, desk_index: int = 0) -> dict[str, Any]:
        return self.rooms["rooms"][room_index]["desks"][desk_index]

    def desk_locator(self, room_index: int = 0, desk_index: int = 0) -> str:
        """The `rooms[i].desks[j] ("Dnn")` locator the validator will print."""
        desk = self.desk_at(room_index, desk_index)
        return f'rooms[{room_index}].desks[{desk_index}] ("{desk["id"]}")'

    # -- serialise / load --------------------------------------------------

    def dump(self) -> Path:
        texts = {
            "rooms.json": dump_json_text(self.rooms),
            "eligibility.json": dump_json_text(self.eligibility),
            "scoring.json": dump_json_text(self.scoring),
            "roster.csv": dump_csv_text(self.roster_fields, self.roster_rows),
        }
        for name, text in texts.items():
            if name in self.raw_overrides:
                (self.path / name).write_bytes(self.raw_overrides[name])
            else:
                write_text(self.path / name, text)
        for name, payload in self.raw_overrides.items():
            if name not in texts:
                (self.path / name).write_bytes(payload)
        for name in self.deleted:
            (self.path / name).unlink(missing_ok=True)
        return self.path

    def set_raw(self, name: str, payload: bytes | str) -> None:
        """Replace one config file with literal bytes (bad UTF-8, bad JSON, ...)."""
        self.raw_overrides[name] = (
            payload if isinstance(payload, bytes) else payload.encode("utf-8")
        )

    def delete(self, name: str) -> None:
        """Remove a config file. Sticky: later `dump()`s do not resurrect it."""
        self.deleted.add(name)
        self.dump()

    def load(self) -> Config:
        """Load, asserting success. Returns the Config (carrying its warnings)."""
        self.dump()
        try:
            return load_config(self.path)
        except ConfigError as exc:  # pragma: no cover - only on a genuine failure
            raise AssertionError(
                "expected this config to load cleanly, but load_config raised:\n"
                + exc.render()
            ) from None

    def errors(self) -> list[Problem]:
        """Load, asserting rejection. Returns every collected problem."""
        self.dump()
        try:
            config = load_config(self.path)
        except ConfigError as exc:
            return list(exc.problems)
        raise AssertionError(
            f"load_config({self.path}) accepted this config; the test expected it to "
            f"be rejected. It produced {len(config.warnings)} warning(s):\n  "
            + "\n  ".join(config.warnings)
        )

    def warnings(self) -> list[str]:
        """Load, asserting success, and return the rendered warnings."""
        return list(self.load().warnings)

    def error_warnings(self) -> list[str]:
        """Warnings carried alongside a *failed* load."""
        self.dump()
        try:
            load_config(self.path)
        except ConfigError as exc:
            return [w.render() for w in exc.warnings]
        raise AssertionError("expected load_config to reject this config")


# --------------------------------------------------------------------------
# Synthetic worlds
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def synth_world_factory():
    """Cached `synth.generate` -- the same shape is built once per session.

    Generation is deterministic, so caching cannot leak state between tests as
    long as callers treat the world as read-only; `config_case` deep-copies the
    documents before handing them over.
    """
    cache: dict[tuple, Any] = {}

    def build(n_people: int = 8, k: int = 5, **kwargs: Any):
        key = (n_people, k) + tuple(sorted(kwargs.items()))
        if key not in cache:
            kwargs.setdefault("image_size", FIXTURE_IMAGE_SIZE)
            kwargs.setdefault("seed", f"fixture-n{n_people}-k{k}")
            cache[key] = synth.generate(n_people, k=k, **kwargs)
        return cache[key]

    return build


@pytest.fixture
def config_case(tmp_path, synth_world_factory):
    """Factory for a fresh, valid `ConfigCase` on disk.

    ``config_case(n_people=..., k=..., n_desks=...)``; any other keyword goes
    straight through to ``synth.generate``.
    """
    counter = {"n": 0}

    def build(n_people: int = 8, k: int = 5, **kwargs: Any) -> ConfigCase:
        world = synth_world_factory(n_people, k, **kwargs)
        counter["n"] += 1
        root = tmp_path / f"case{counter['n']:02d}"
        config_dir, responses_path = world.write(root)
        return ConfigCase(
            path=config_dir,
            responses_path=responses_path,
            rooms=copy.deepcopy(dict(world.rooms)),
            eligibility=copy.deepcopy(dict(world.eligibility)),
            scoring=copy.deepcopy(dict(world.scoring)),
            roster_fields=list(world.roster_fields),
            roster_rows=[dict(row) for row in world.roster_rows],
            world=world,
        )

    return build


# --------------------------------------------------------------------------
# Response files
# --------------------------------------------------------------------------


def submission_row(
    *,
    submission_id: str,
    timestamp: str,
    email: str,
    choices: Sequence[str],
    name: str | None = None,
    year: Any = 3,
    candidacy: str = "candidate",
    client_version: str = "test-1",
    auth_method: str = "google",
    **extra: Any,
) -> dict[str, str]:
    """One response row as the loader will see it (all values stringified)."""
    row: dict[str, str] = {
        "submission_id": submission_id,
        "timestamp": timestamp,
        "email": email,
        "name": name if name is not None else email.split("@")[0],
        "year": str(year),
        "candidacy": candidacy,
        "client_version": client_version,
        "auth_method": auth_method,
    }
    for rank, desk in enumerate(choices, start=1):
        row[f"choice_{rank}"] = desk
    row.update({key: str(value) for key, value in extra.items()})
    return row


def response_header(k: int, extra: Sequence[str] = ()) -> tuple[str, ...]:
    return (*responses_mod.canonical_header(k), *extra)


@pytest.fixture
def response_file(tmp_path):
    """Factory writing a response file and returning its path.

    ``response_file(rows, k=K)`` writes CSV; ``fmt="json"`` writes the JSON form
    of the same rows, which is what the CSV<->JSON parity test compares.
    """
    counter = {"n": 0}

    def build(
        rows: Sequence[Mapping[str, Any]],
        *,
        k: int | None = None,
        header: Sequence[str] | None = None,
        fmt: str = "csv",
        name: str | None = None,
    ) -> Path:
        counter["n"] += 1
        if header is None:
            if k is None:
                raise ValueError("pass k= or header=")
            header = response_header(k)
        path = tmp_path / (name or f"responses{counter['n']:02d}.{fmt}")
        if fmt == "json":
            payload = [
                {column: str(row.get(column, "")) for column in header} for row in rows
            ]
            write_text(path, json.dumps(payload, indent=2) + "\n")
        else:
            write_text(path, dump_csv_text(header, rows))
        return path

    return build


def load_response_problems(path: Path, k: int | None = None) -> list[Problem]:
    """Load, asserting rejection. Returns every collected problem."""
    try:
        loaded = responses_mod.load_responses(str(path), k)
    except ResponseError as exc:
        return list(exc.problems)
    raise AssertionError(
        f"load_responses({path}) accepted this file; the test expected rejection. "
        f"It produced {len(loaded.warnings)} warning(s):\n  "
        + "\n  ".join(loaded.warnings)
    )


# --------------------------------------------------------------------------
# The real, shipped configuration
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def real_config_dir() -> Path:
    return REPO_ROOT / "config"


#: Names the shipped response export is known to have used. The runbook (README
#: step 6) tells the coordinator to commit it as `data/responses_<cycle>.csv`, so
#: the filename is *expected* to change from year to year and pinning one here
#: would make the determinism tests fail for a reason that has nothing to do with
#: determinism.
REAL_RESPONSES_PREFERRED: tuple[str, ...] = (
    "responses_demo.csv",
    "test_responses.csv",
)


def find_real_responses_csv(data_dir: Path) -> Path:
    """Locate the shipped response export in `data/`, deterministically.

    Preferred names first, then the sole `*.csv` if there is exactly one. Sorted
    at every step, so the answer never depends on directory order (SPEC §5.5).
    Returns a non-existent path rather than raising: `real_build` skips on a
    missing export, and a fixture that raised here would turn "the demo data is
    not checked out" into a hard error in every test that touches it.
    """
    for name in REAL_RESPONSES_PREFERRED:
        candidate = data_dir / name
        if candidate.is_file():
            return candidate
    found = sorted(p for p in data_dir.glob("*.csv") if p.is_file())
    if len(found) == 1:
        return found[0]
    return data_dir / REAL_RESPONSES_PREFERRED[0]


@pytest.fixture(scope="session")
def real_responses_csv() -> Path:
    return find_real_responses_csv(REPO_ROOT / "data")


@pytest.fixture(scope="session")
def golden_dir() -> Path:
    return TESTS_DIR / "golden"


# ==========================================================================
# Solver-core fixtures: declarative Problem building and the post-solve
# invariant assertions (I4 / I5 / I7).
# ==========================================================================
#
# `deskmatch.errors.Problem` (imported above) is a *validation complaint*;
# `deskmatch.types.Problem` is the solve matrix. They are different things with
# the same name, so the solve one is aliased for the rest of this file.

import hashlib  # noqa: E402
import math  # noqa: E402
from fractions import Fraction  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import numpy as np  # noqa: E402

from deskmatch import matching, scoring  # noqa: E402
from deskmatch.types import Problem as SolveProblem  # noqa: E402
from deskmatch.types import Solution  # noqa: E402

#: What the shipped config + shipped response export must produce. Written down
#: so that a change which silently moves the department's real answer cannot
#: pass; every number here is *derived* by the assertions, never assumed.
REAL_CONFIG_EXPECTED = SimpleNamespace(
    n_people=9,
    n_desks=30,
    k=5,
    rank_histogram=(7, 1, 1, 0, 0),
    total_points_scaled=42,
    scale=1,
)


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------


def stable_seed(*parts: Any) -> int:
    """A 64-bit seed derived from the arguments, stably.

    `hashlib`, never the builtin `hash()`: the latter is salted per interpreter
    start (PYTHONHASHSEED), which would make the "random" instances in a
    property test differ between runs -- the opposite of a regression test.
    """
    text = "\x00".join(repr(part) for part in parts)
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def stable_rng(*parts: Any) -> "np.random.Generator":
    return np.random.default_rng(stable_seed(*parts))


# --------------------------------------------------------------------------
# Scoring curves
# --------------------------------------------------------------------------

#: Curve families used across the property tests. "halves" and "thirds" exist
#: to push `scoring.integerise` off scale 1: the jitter bound's proof rests on
#: the *scaled* matrix being integral, not on the config file being.
CURVE_KINDS: tuple[str, ...] = ("linear", "convex", "halves", "thirds")


def make_curve(k: int, kind: str = "linear") -> tuple[Fraction, ...]:
    """A strictly decreasing, strictly positive curve of length K (SPEC §2.4)."""
    if k < 1:
        raise ValueError(f"K must be at least 1, got {k}")
    if kind == "linear":
        values = [Fraction(k - i) for i in range(k)]
    elif kind == "convex":
        values = [Fraction(2) ** (k - 1 - i) for i in range(k)]
    elif kind == "halves":
        values = [Fraction(2 * k - i, 2) for i in range(k)]
    elif kind == "thirds":
        values = [Fraction(3 * k - i, 3) for i in range(k)]
    else:
        raise ValueError(f"unknown curve kind {kind!r}; expected one of {CURVE_KINDS}")
    assert all(a > b for a, b in zip(values, values[1:])), (kind, values)
    assert all(v > 0 for v in values), (kind, values)
    return tuple(values)


def expected_scale(k: int, kind: str) -> int:
    """The integerisation factor `make_curve(k, kind)` must produce.

    Computed from the denominators rather than asserted as a constant, so this
    stays true when K changes -- e.g. "halves" needs no scaling at K=1.
    """
    return math.lcm(*(v.denominator for v in make_curve(k, kind)))


# --------------------------------------------------------------------------
# Declarative Problem builder
# --------------------------------------------------------------------------

ALL_ZONES = "*"


def _generated_desk_ids(count: int) -> tuple[str, ...]:
    width = max(2, len(str(max(count, 1))))
    return tuple(f"D{j + 1:0{width}d}" for j in range(count))


def _generated_person_ids(count: int) -> tuple[str, ...]:
    width = max(2, len(str(max(count, 1))))
    return tuple(f"p{i + 1:0{width}d}@example.test" for i in range(count))


def build_problem_from_prefs(
    prefs: Sequence[Sequence[Any]],
    *,
    desks: int | Sequence[str] | None = None,
    k: int | None = None,
    curve: Sequence[Any] | None = None,
    curve_kind: str = "linear",
    curve_name: str | None = None,
    zone_of_desk: Mapping[str, str] | Sequence[str] | None = None,
    allowed_zones: Mapping[str, Any] | Sequence[Any] | None = None,
    people: Sequence[str] | None = None,
) -> SolveProblem:
    """A `types.Problem` from a list of per-person ranked desk lists.

    ``prefs[i]`` is person i's submitted ranking, best first, given either as
    column indices (``[0, 2, 1]``) or as desk ids (``["D01", "D03"]``). A list
    may be shorter than K -- that is what a person with a small eligible pool
    looks like -- but never longer, and never with a repeat.

    Zones mirror SPEC §5.1 exactly: ``allowed[i, j]`` is set only where the desk
    is in person i's top-K *and* its zone is permitted for them, so a ranked
    desk in a forbidden zone is dropped from the matrix (which is what
    ``problem.build_problem`` does with it), while ``eligible[i, j]`` records
    the zone decision on its own -- which is what makes I5 checkable
    independently of I4.

    Every dimension is derived, none assumed: N = len(prefs); M and K default to
    the smallest values consistent with `prefs` and can be widened by argument.
    """
    rows = [list(row) for row in prefs]
    n_people = len(rows)

    # ---- desk ids --------------------------------------------------------
    if isinstance(desks, int):
        desk_ids = _generated_desk_ids(desks)
    elif desks is not None:
        desk_ids = tuple(desks)
    else:
        named = sorted({d for row in rows for d in row if isinstance(d, str)})
        highest = max((d for row in rows for d in row if isinstance(d, int)), default=-1)
        if named and highest >= 0:
            raise ValueError("a mix of desk ids and desk indices needs an explicit desks=")
        desk_ids = tuple(named) if named else _generated_desk_ids(highest + 1)
    n_desks = len(desk_ids)
    index_of_desk = {d: j for j, d in enumerate(desk_ids)}
    if len(index_of_desk) != n_desks:
        raise ValueError(f"duplicate desk id in {desk_ids}")

    # ---- K and the curve -------------------------------------------------
    longest = max((len(row) for row in rows), default=0)
    k_eff = int(k) if k is not None else max(longest, 1)
    if longest > k_eff:
        raise ValueError(f"a preference list has {longest} entries but K={k_eff}")
    if k_eff > 127:
        raise ValueError("types.Problem.rank is int8; this builder tops out at K=127")

    raw_curve = curve if curve is not None else make_curve(k_eff, curve_kind)
    if len(raw_curve) != k_eff:
        raise ValueError(f"the curve has {len(raw_curve)} entries but K={k_eff}")
    fractions = tuple(
        value if isinstance(value, Fraction) else Fraction(str(value))
        for value in raw_curve
    )
    int_curve, scale = scoring.integerise(fractions)

    # ---- zones -----------------------------------------------------------
    if zone_of_desk is None:
        zone_map = {d: "zone_all" for d in desk_ids}
    elif isinstance(zone_of_desk, Mapping):
        zone_map = {d: zone_of_desk[d] for d in desk_ids}
    else:
        sequence = list(zone_of_desk)
        if len(sequence) != n_desks:
            raise ValueError(f"zone_of_desk has {len(sequence)} entries for {n_desks} desks")
        zone_map = dict(zip(desk_ids, sequence))
    all_zones = tuple(sorted(set(zone_map.values())))

    person_ids = tuple(people) if people is not None else _generated_person_ids(n_people)
    if len(person_ids) != n_people:
        raise ValueError(f"{len(person_ids)} person ids for {n_people} preference lists")

    if allowed_zones is None:
        zones_for = {p: all_zones for p in person_ids}
    elif isinstance(allowed_zones, Mapping):
        zones_for = {
            p: (
                all_zones
                if allowed_zones.get(p, ALL_ZONES) == ALL_ZONES
                else tuple(sorted(allowed_zones[p]))
            )
            for p in person_ids
        }
    else:
        per_person = list(allowed_zones)
        if len(per_person) != n_people:
            raise ValueError(
                f"allowed_zones has {len(per_person)} entries for {n_people} people"
            )
        zones_for = {
            p: all_zones if z == ALL_ZONES else tuple(sorted(z))
            for p, z in zip(person_ids, per_person)
        }

    # ---- matrices --------------------------------------------------------
    allowed = np.zeros((n_people, n_desks), dtype=bool)
    points = np.zeros((n_people, n_desks), dtype=np.int64)
    rank = np.full((n_people, n_desks), -1, dtype=np.int8)
    eligible = np.zeros((n_people, n_desks), dtype=bool)

    for i, email in enumerate(person_ids):
        permitted = frozenset(zones_for[email])
        for j, desk_id in enumerate(desk_ids):
            eligible[i, j] = zone_map[desk_id] in permitted

        seen: set[int] = set()
        for r, entry in enumerate(rows[i], start=1):
            j = entry if isinstance(entry, int) else index_of_desk[entry]
            if not 0 <= j < n_desks:
                raise ValueError(f"person {i} ranked desk index {j}, which is out of range")
            if j in seen:
                raise ValueError(f"person {i} ranked desk {desk_ids[j]} twice")
            seen.add(j)
            if not eligible[i, j]:
                continue  # SPEC §3.4: a choice in a forbidden zone is dropped
            allowed[i, j] = True
            rank[i, j] = r
            points[i, j] = int_curve[r - 1]

    return SolveProblem(
        people=person_ids,
        desks=desk_ids,
        allowed=allowed,
        points=points,
        rank=rank,
        eligible=eligible,
        scale=scale,
        curve_name=curve_name or f"test_{curve_kind}",
        k=k_eff,
        person_names={p: f"Person {i + 1}" for i, p in enumerate(person_ids)},
        desk_labels={d: str(j + 1) for j, d in enumerate(desk_ids)},
    )


def random_problem(
    *seed_parts: Any,
    n_people: int,
    n_desks: int,
    k: int,
    n_zones: int = 1,
    restricted_frac: float = 0.0,
    curve_kind: str = "linear",
) -> SolveProblem:
    """A random small instance. Every dimension is an argument (invariant I1).

    Preferences are drawn from the *whole* desk set rather than from each
    person's eligible subset, so a zone-restricted person routinely ranks desks
    they may not have. That is exactly the configuration in which a solver that
    ignored the mask would be tempted to cheat.
    """
    rng = np.random.default_rng(
        stable_seed(*seed_parts, n_people, n_desks, k, n_zones, restricted_frac, curve_kind)
    )
    zones = [f"z{j % n_zones}" for j in range(n_desks)]

    zones_per_person: list[Any] = []
    for _ in range(n_people):
        if n_zones > 1 and rng.random() < restricted_frac:
            size = int(rng.integers(1, n_zones))          # a non-empty strict subset
            picked = sorted(rng.choice(n_zones, size=size, replace=False).tolist())
            zones_per_person.append(tuple(f"z{z}" for z in picked))
        else:
            zones_per_person.append(ALL_ZONES)

    prefs: list[list[int]] = []
    for _ in range(n_people):
        size = min(k, n_desks)
        prefs.append(rng.choice(n_desks, size=size, replace=False).tolist())

    return build_problem_from_prefs(
        prefs,
        desks=n_desks,
        k=k,
        curve_kind=curve_kind,
        zone_of_desk=zones,
        allowed_zones=zones_per_person,
    )


def is_feasible(problem: SolveProblem) -> bool:
    """Can every person get one of their top-K desks? (SPEC §5.2)"""
    if problem.n_people == 0:
        return True
    return matching.has_perfect_left_matching(problem.allowed)


# --------------------------------------------------------------------------
# Invariant assertions -- call these from every test that produces a Solution
# --------------------------------------------------------------------------


def assert_k_floor(problem: SolveProblem, solution: Solution) -> None:
    """Invariant I4, re-derived from the matrix the solver was handed.

    Also checks what makes the claim meaningful: that *everyone* was seated
    (I7 -- a partial answer is never acceptable), that no desk went out twice,
    and that the reported total, points and histogram agree with the matrix
    rather than merely with each other.
    """
    row_of = {email: i for i, email in enumerate(problem.people)}
    col_of = {desk: j for j, desk in enumerate(problem.desks)}

    assert solution.k == problem.k, "Solution.k disagrees with the Problem's K"
    assert solution.scale == problem.scale
    assert solution.curve_name == problem.curve_name
    assert solution.unassigned_people == (), (
        f"I7 violated: solve() returned a partial assignment, leaving "
        f"{solution.unassigned_people} unseated"
    )

    emails = [a.email for a in solution.assignments]
    assert len(emails) == problem.n_people, (
        f"expected {problem.n_people} assignments, got {len(emails)}"
    )
    assert len(set(emails)) == len(emails), "the same person appears twice"
    assert set(emails) == set(problem.people), "the assignments do not cover the pool exactly"
    assert emails == sorted(emails), "Solution.assignments must be sorted by email"

    desks_used = [a.desk_id for a in solution.assignments]
    assert len(set(desks_used)) == len(desks_used), "a desk was assigned to two people"

    running_total = 0
    for a in solution.assignments:
        i, j = row_of[a.email], col_of[a.desk_id]
        assert problem.allowed[i, j], (
            f"forbidden pairing in the output: {a.email} -> {a.desk_id}"
        )
        r = int(problem.rank[i, j])
        assert 1 <= r <= problem.k, (
            f"I4 violated: {a.email} got {a.desk_id} at rank {r} (K={problem.k})"
        )
        assert a.rank_received == r, "Assignment.rank_received disagrees with Problem.rank"
        assert a.points == int(problem.points[i, j]), (
            "Assignment.points disagrees with Problem.points"
        )
        assert a.name == problem.person_names[a.email]
        assert a.desk_label == problem.desk_labels[a.desk_id]
        running_total += a.points

    assert solution.total_points_scaled == running_total, (
        f"reported total {solution.total_points_scaled} != the sum of the assigned "
        f"cells ({running_total})"
    )
    assert solution.total_points == Fraction(running_total, problem.scale)

    histogram = solution.rank_histogram()
    assert len(histogram) == problem.k
    assert sum(histogram) == problem.n_people
    for rank_index, count in enumerate(histogram, start=1):
        assert count == sum(1 for a in solution.assignments if a.rank_received == rank_index)

    free = set(solution.free_desks)
    assert free == set(problem.desks) - set(desks_used), "free_desks is wrong"
    assert len(free) == problem.n_desks - problem.n_people

    assert solution.seed_int == scoring.seed_int(solution.seed_string), (
        "seed_int is not derived from sha256(seed_string) -- SPEC §5.4"
    )


def assert_zone_constraint(problem: SolveProblem, solution: Solution) -> None:
    """Invariant I5: every assigned desk is in a zone permitted for that person.

    `Problem.eligible` is zone eligibility on its own, ignoring the top-K
    restriction, so it is the right thing to check against -- asserting on
    `allowed` would only re-check I4 with extra steps.
    """
    assert problem.eligible is not None, (
        "Problem.eligible is None, so I5 cannot be checked independently of I4"
    )
    assert np.all(problem.allowed <= problem.eligible), (
        "the matrix itself violates I5: an allowed cell lies outside the person's zones"
    )

    row_of = {email: i for i, email in enumerate(problem.people)}
    col_of = {desk: j for j, desk in enumerate(problem.desks)}
    for a in solution.assignments:
        i, j = row_of[a.email], col_of[a.desk_id]
        assert problem.eligible[i, j], (
            f"I5 violated: {a.email} was assigned {a.desk_id}, whose zone they are "
            f"not permitted to sit in"
        )


def assert_solution_invariants(problem: SolveProblem, solution: Solution) -> None:
    """The whole post-solve contract: I4, I5, I7 and structural consistency."""
    assert_k_floor(problem, solution)
    assert_zone_constraint(problem, solution)


def solution_signature(solution: Solution) -> tuple:
    """A hashable rendering of everything a Solution decides.

    Used to count distinct outcomes across seeds; deliberately excludes the seed
    itself, which differs by construction.
    """
    return (
        solution.total_points_scaled,
        solution.rank_histogram(),
        tuple((a.email, a.desk_id, a.rank_received, a.points) for a in solution.assignments),
        tuple(solution.free_desks),
    )


SOLUTION_FIELDS: tuple[str, ...] = (
    "assignments",
    "total_points_scaled",
    "scale",
    "curve_name",
    "seed_string",
    "seed_int",
    "k",
    "backend",
    "unassigned_people",
    "free_desks",
)


def assert_solutions_identical(first: Solution, second: Solution, context: str = "") -> None:
    """Field-by-field equality, not just `==`.

    `==` on the dataclass would do this in one line, but it reports only "these
    objects differ". Naming the field is the difference between a five-minute
    and a fifty-minute debugging session.
    """
    where = f" ({context})" if context else ""
    for name in SOLUTION_FIELDS:
        a, b = getattr(first, name), getattr(second, name)
        assert a == b, f"Solution.{name} differs between runs{where}: {a!r} != {b!r}"
    assert first == second, f"the Solutions are unequal{where} despite matching fields"
    assert first.rank_histogram() == second.rank_histogram()
    assert first.total_points == second.total_points


# --------------------------------------------------------------------------
# Solver-core fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def make_problem():
    """Factory: a `types.Problem` from a list of preference lists."""
    return build_problem_from_prefs


@pytest.fixture
def make_random_problem():
    """Factory: a seeded random small instance at any (N, M, K, zones)."""
    return random_problem


@pytest.fixture
def invariants() -> SimpleNamespace:
    """The invariant assertions, for tests that prefer a fixture to an import."""
    return SimpleNamespace(
        k_floor=assert_k_floor,
        zone_constraint=assert_zone_constraint,
        all=assert_solution_invariants,
        identical=assert_solutions_identical,
        signature=solution_signature,
        feasible=is_feasible,
    )


@pytest.fixture(scope="session")
def real_config(real_config_dir: Path) -> Config:
    """The department's shipped `config/`, loaded and validated."""
    if not real_config_dir.is_dir():
        pytest.skip(f"the real config directory is missing: {real_config_dir}")
    return load_config(real_config_dir)


@pytest.fixture(scope="session")
def real_build(real_config: Config, real_responses_csv: Path):
    """`BuildReport` for the real config plus the shipped response export."""
    from deskmatch import problem as problem_mod

    if not real_responses_csv.is_file():
        pytest.skip(f"the shipped response export is missing: {real_responses_csv}")
    loaded = responses_mod.load_responses(str(real_responses_csv), real_config.k)
    return problem_mod.build_problem(real_config, loaded)


@pytest.fixture(scope="session")
def real_problem(real_build) -> SolveProblem:
    return real_build.problem


@pytest.fixture
def world_config_dir(tmp_path_factory):
    """Factory: write a `synth.SynthWorld` out; return (config_dir, responses).

    Floor-plan PNGs are off by default -- a solver test pays nothing for them
    and the validator only *warns* when the image is missing.
    """
    counter = {"n": 0}

    def build(world, *, write_images: bool = False) -> tuple[Path, Path]:
        counter["n"] += 1
        destination = tmp_path_factory.mktemp(f"world{counter['n']:02d}")
        return world.write(destination, write_images=write_images)

    return build


@pytest.fixture
def build_from_world(world_config_dir):
    """Factory: `synth.SynthWorld` -> `(Config, BuildReport)` via the real loaders.

    Going through disk rather than `world.to_config()` is deliberate: it
    exercises validation, response parsing and pool formation, so a scenario
    fixture cannot drift away from what the CLI would actually see.
    """
    from deskmatch import problem as problem_mod

    def build(world, *, curve_name: str | None = None, write_images: bool = False):
        config_dir, responses_path = world_config_dir(world, write_images=write_images)
        config = load_config(config_dir)
        loaded = responses_mod.load_responses(str(responses_path), config.k)
        return config, problem_mod.build_problem(config, loaded, curve_name)

    return build
