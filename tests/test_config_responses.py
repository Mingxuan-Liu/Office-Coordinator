"""Config validation (SPEC §2) and response ingest (SPEC §3).

What this file is for
---------------------
The messages asserted here are the entire user interface of this system for the
one person who matters most: next year's coordinator, at 4pm on the deadline,
holding a CSV that does not load. SPEC §2 promises that every validation failure
"names the file, the JSON path, the offending value, and what was expected", and
that failures are *collected* rather than reported one per run. Those promises
are only real if something checks them, so every rule in SPEC §2.1-§2.4 gets a
test that breaks exactly that rule and asserts the specific message.

The tables below are deliberately written as data — one row per SPEC rule, each
carrying the clause it comes from — so that reading the table is a way of
reading the spec. A rule with no row is a rule with no test.

Sizing (invariant I1)
---------------------
Every baseline config is generated at a size the test is handed, and every
mutation addresses its target relatively (`rooms[-1].desks[0]`, `curves[primary]`)
rather than by a literal index. The whole table runs at two different (N, K)
shapes; anything that only worked at one size would fail at the other.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable

import pytest

from conftest import (
    ConfigCase,
    assert_no_problem,
    dump_csv_text,
    find_problem,
    find_warning,
    load_response_problems,
    render_all,
    response_header,
    submission_row,
    write_text,
)
from deskmatch import responses as responses_mod
from deskmatch import validate
from deskmatch.errors import ResponseError
from deskmatch.types import Responses

# --------------------------------------------------------------------------
# Shapes. Two sizes and two Ks, so nothing can quietly depend on either.
# --------------------------------------------------------------------------

SHAPES = [
    pytest.param({"n_people": 6, "k": 3}, id="N6-K3"),
    pytest.param({"n_people": 13, "k": 5}, id="N13-K5"),
]


@pytest.fixture(params=SHAPES)
def shape(request) -> dict[str, int]:
    return dict(request.param)


@pytest.fixture
def case(config_case, shape) -> ConfigCase:
    """A valid config at the current shape, ready to be broken."""
    return config_case(**shape)


# --------------------------------------------------------------------------
# The rule table
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BrokenRule:
    """One SPEC rule, the smallest edit that breaks it, and the message wanted.

    `where` / `what` / `hint` are substrings of `errors.Problem`'s three fields;
    `where` may be a callable so an expectation can be derived from the document
    (a desk locator depends on how many desks there are).
    """

    id: str
    spec: str
    mutate: Callable[[ConfigCase], None]
    where: str | Callable[[ConfigCase], str]
    what: str
    hint: str = ""

    def expected_where(self, case: ConfigCase) -> str:
        return self.where(case) if callable(self.where) else self.where


def check(case: ConfigCase, rule: BrokenRule):
    """Break the rule, load, and return the single matching Problem."""
    rule.mutate(case)
    problems = case.errors()
    return find_problem(
        problems, rule.expected_where(case), rule.what, rule.hint
    )


def as_params(rules: list[BrokenRule]):
    return [pytest.param(rule, id=rule.id) for rule in rules]


# --------------------------------------------------------------------------
# SPEC §2.1 — rooms.json
# --------------------------------------------------------------------------


def _last_desk(case: ConfigCase) -> dict[str, Any]:
    return case.rooms["rooms"][-1]["desks"][-1]


def _last_desk_locator(case: ConfigCase) -> str:
    room_index = len(case.rooms["rooms"]) - 1
    desk_index = len(case.rooms["rooms"][room_index]["desks"]) - 1
    return case.desk_locator(room_index, desk_index)


def _first_zone(case: ConfigCase) -> str:
    return case.zone_ids[0]


ROOMS_RULES: list[BrokenRule] = [
    BrokenRule(
        "top-level-not-object", "§2.1 document shape",
        lambda c: c.set_raw("rooms.json", '["not", "an", "object"]'),
        "rooms.json: (top level)", "expected a JSON object, got an array",
        "docs/SPEC.md §2.1",
    ),
    BrokenRule(
        "invalid-json", "§2 files are strict JSON",
        lambda c: c.set_raw("rooms.json", '{"schema_version": 1,}'),
        "rooms.json: line 1", "is not valid JSON",
        "trailing comma",
    ),
    BrokenRule(
        "empty-file", "§2 files must have contents",
        lambda c: c.set_raw("rooms.json", "   \n"),
        "rooms.json", "is empty.",
    ),
    BrokenRule(
        "bad-utf8", "§2 files are UTF-8",
        lambda c: c.set_raw("rooms.json", b'{"schema_version": 1, "x": "\xff\xfe"}'),
        "rooms.json: byte", "is not valid UTF-8",
        "Re-save the file as UTF-8.",
    ),
    BrokenRule(
        "schema-version-missing", "§2.1 schema_version",
        lambda c: c.rooms.pop("schema_version"),
        "rooms.json: schema_version", "missing required key 'schema_version'",
    ),
    BrokenRule(
        "schema-version-not-int", "§2.1 schema_version",
        lambda c: c.rooms.__setitem__("schema_version", "1"),
        "rooms.json: schema_version", "expected an integer, got a string",
    ),
    BrokenRule(
        "schema-version-unsupported", "§2.1 schema_version",
        lambda c: c.rooms.__setitem__("schema_version", 99),
        "rooms.json: schema_version", "is 99, which this build of deskmatch cannot interpret",
    ),
    BrokenRule(
        "coord-space-missing", "§2.1 coord_space",
        lambda c: c.rooms.pop("coord_space"),
        "rooms.json: coord_space", "missing required key 'coord_space'",
        "normalized, pixels",
    ),
    BrokenRule(
        "coord-space-unknown", "§2.1 coord_space ∈ {normalized, pixels}",
        lambda c: c.rooms.__setitem__("coord_space", "normalised"),
        "rooms.json: coord_space", "not a supported coordinate space",
        "Did you mean 'normalized'?",
    ),
    BrokenRule(
        "zones-missing", "§2.1 zones",
        lambda c: c.rooms.pop("zones"),
        "rooms.json: (top level)", "missing required key 'zones'",
    ),
    BrokenRule(
        "zones-not-object", "§2.1 zones",
        lambda c: c.rooms.__setitem__("zones", ["a", "b"]),
        "rooms.json: (top level)", "'zones' is an array",
    ),
    BrokenRule(
        "zones-empty", "§2.1 at least one zone",
        lambda c: c.rooms.__setitem__("zones", {}),
        "rooms.json: zones", "defines no zones",
    ),
    BrokenRule(
        "zone-meta-not-object", "§2.1 zone metadata",
        lambda c: c.rooms["zones"].__setitem__(_first_zone(c), "Upper years"),
        lambda c: f"rooms.json: zones.{_first_zone(c)}", "expected an object, got a string",
    ),
    BrokenRule(
        "zone-label-not-string", "§2.1 zone label",
        lambda c: c.rooms["zones"][_first_zone(c)].__setitem__("label", 7),
        lambda c: f"rooms.json: zones.{_first_zone(c)}", "'label' is a number",
    ),
    BrokenRule(
        "zone-colour-not-hex", "§2.1 zone colour",
        lambda c: c.rooms["zones"][_first_zone(c)].__setitem__("color", "cornflower"),
        lambda c: f"rooms.json: zones.{_first_zone(c)}", "which is not a hex colour",
        "'#rgb', '#rrggbb' or '#rrggbbaa'",
    ),
    BrokenRule(
        "zone-description-not-string", "§2.1 zone description",
        lambda c: c.rooms["zones"][_first_zone(c)].__setitem__("description", {"x": 1}),
        lambda c: f"rooms.json: zones.{_first_zone(c)}", "'description' is an object",
    ),
    BrokenRule(
        "rooms-missing", "§2.1 rooms",
        lambda c: c.rooms.pop("rooms"),
        "rooms.json: (top level)", "missing required key 'rooms'",
    ),
    BrokenRule(
        "rooms-not-array", "§2.1 rooms",
        lambda c: c.rooms.__setitem__("rooms", {"main": {}}),
        "rooms.json: (top level)", "'rooms' is an object",
    ),
    BrokenRule(
        "rooms-empty", "§2.1 at least one room",
        lambda c: c.rooms.__setitem__("rooms", []),
        "rooms.json: rooms", "is an empty array; no desks are defined anywhere",
    ),
    BrokenRule(
        "room-not-object", "§2.1 room shape",
        lambda c: c.rooms["rooms"].append("room_two"),
        lambda c: f"rooms.json: rooms[{len(c.rooms['rooms']) - 1}]",
        "expected an object, got a string",
    ),
    BrokenRule(
        "room-id-missing", "§2.1 room.id",
        lambda c: c.rooms["rooms"][-1].pop("id"),
        lambda c: f"rooms.json: rooms[{len(c.rooms['rooms']) - 1}]",
        "missing required key 'id'",
    ),
    BrokenRule(
        "room-id-duplicate", "§2.1 room.id unique",
        lambda c: c.rooms["rooms"].append(dict(c.rooms["rooms"][-1])),
        lambda c: f'rooms.json: rooms[{len(c.rooms["rooms"]) - 1}] ("{c.rooms["rooms"][-1]["id"]}")',
        "duplicate room id", "Room ids must be unique.",
    ),
    BrokenRule(
        "room-label-not-string", "§2.1 room.label",
        lambda c: c.rooms["rooms"][-1].__setitem__("label", ["Main", "Office"]),
        "rooms.json: rooms[0]", "'label' is an array",
    ),
    BrokenRule(
        "image-size-missing", "§2.1 image_size required",
        lambda c: c.rooms["rooms"][-1].pop("image_size"),
        "rooms.json: rooms[0]", "missing required key 'image_size'",
        "required even in normalized coordinate space",
    ),
    BrokenRule(
        "image-size-wrong-length", "§2.1 image_size is [w, h]",
        lambda c: c.rooms["rooms"][-1].__setitem__("image_size", [1212, 706, 3]),
        "rooms.json: rooms[0]", "expected an array of exactly two numbers",
    ),
    BrokenRule(
        "image-size-not-int", "§2.1 image_size pixels",
        lambda c: c.rooms["rooms"][-1].__setitem__("image_size", [1212.5, 706]),
        "rooms.json: rooms[0]", "width is a number",
    ),
    BrokenRule(
        "image-size-non-positive", "§2.1 image_size positive",
        lambda c: c.rooms["rooms"][-1].__setitem__("image_size", [1212, 0]),
        "rooms.json: rooms[0]", "height is 0; expected a positive number of pixels",
    ),
    BrokenRule(
        "image-not-a-path", "§2.1 image path",
        lambda c: c.rooms["rooms"][-1].__setitem__("image", 3),
        "rooms.json: rooms[0]", "expected a path string relative to the config directory",
    ),
    BrokenRule(
        "desks-missing", "§2.1 room.desks",
        lambda c: c.rooms["rooms"][-1].pop("desks"),
        "rooms.json: rooms[0]", "missing required key 'desks'",
    ),
    BrokenRule(
        "desks-empty", "§2.1 a room needs desks",
        lambda c: c.rooms["rooms"][-1].__setitem__("desks", []),
        "rooms.json: rooms[0]", "is an empty array; a room with no desks",
    ),
    BrokenRule(
        "desk-not-object", "§2.1 desk shape",
        lambda c: c.rooms["rooms"][-1]["desks"].append("D99"),
        lambda c: f"rooms.json: rooms[0].desks[{len(c.rooms['rooms'][0]['desks']) - 1}]",
        "expected an object, got a string",
    ),
    BrokenRule(
        "desk-id-missing", "§2.1 desk.id",
        lambda c: _last_desk(c).pop("id"),
        lambda c: f"rooms.json: rooms[0].desks[{len(c.rooms['rooms'][0]['desks']) - 1}]",
        "missing required key 'id'",
    ),
    BrokenRule(
        "desk-id-blank", "§2.1 desk.id non-empty",
        lambda c: _last_desk(c).__setitem__("id", "   "),
        "rooms.json: rooms[0].desks[", "expected a non-empty string",
    ),
    BrokenRule(
        "desk-id-duplicate", "§2.1 desk.id unique across ALL rooms",
        lambda c: c.rooms["rooms"][-1]["desks"].append(
            dict(c.rooms["rooms"][-1]["desks"][0])
        ),
        lambda c: (
            f'rooms.json: rooms[0].desks[{len(c.rooms["rooms"][0]["desks"]) - 1}] '
            f'("{c.rooms["rooms"][0]["desks"][0]["id"]}")'
        ),
        "duplicate desk id", "unique across ALL rooms",
    ),
    BrokenRule(
        "desk-zone-missing", "§2.1 desk.zone",
        lambda c: _last_desk(c).pop("zone"),
        _last_desk_locator, "missing required key 'zone'", "Defined zones are:",
    ),
    BrokenRule(
        "desk-zone-not-string", "§2.1 desk.zone",
        lambda c: _last_desk(c).__setitem__("zone", 3),
        _last_desk_locator, "'zone' is a number",
    ),
    BrokenRule(
        # The one message whose exact shape SPEC §2.1 fixes verbatim.
        "desk-zone-undefined", "§2.1 desk.zone ∈ zones (message shape is normative)",
        lambda c: _last_desk(c).__setitem__("zone", "senior_side"),
        _last_desk_locator,
        "references zone 'senior_side', which is not defined in rooms.json:zones.",
        "Defined zones are:",
    ),
    BrokenRule(
        "desk-label-not-string", "§2.1 desk.label",
        lambda c: _last_desk(c).__setitem__("label", 14),
        _last_desk_locator, "'label' is a number",
    ),
    BrokenRule(
        "desk-notes-not-string", "§2.1 desk.notes",
        lambda c: _last_desk(c).__setitem__("notes", ["by the window"]),
        _last_desk_locator, "'notes' is an array",
    ),
    BrokenRule(
        "desk-available-not-bool", "§3.4 desk.available",
        lambda c: _last_desk(c).__setitem__("available", "no"),
        _last_desk_locator, "'available' is a string",
        '"available": false',
    ),
    BrokenRule(
        "shape-missing", "§2.1 desk.shape",
        lambda c: _last_desk(c).pop("shape"),
        _last_desk_locator, "missing required key 'shape'",
        "tools/calibrate/",
    ),
    BrokenRule(
        "shape-not-object", "§2.1 desk.shape",
        lambda c: _last_desk(c).__setitem__("shape", [0.1, 0.1, 0.1, 0.1]),
        _last_desk_locator, "expected an object",
    ),
    BrokenRule(
        "shape-neither", "§2.1 exactly one of rect | polygon",
        lambda c: _last_desk(c).__setitem__("shape", {"circle": [0.5, 0.5, 0.1]}),
        _last_desk_locator, "defines neither 'rect' nor 'polygon'",
    ),
    BrokenRule(
        "shape-both", "§2.1 exactly one of rect | polygon",
        lambda c: _last_desk(c)["shape"].__setitem__(
            "polygon", [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]]
        ),
        _last_desk_locator, "defines both 'rect' and 'polygon'",
    ),
    BrokenRule(
        "rect-wrong-length", "§2.1 rect = [x, y, w, h]",
        lambda c: _last_desk(c)["shape"].__setitem__("rect", [0.1, 0.1, 0.1]),
        _last_desk_locator, "expected exactly four numbers, [x, y, w, h]",
    ),
    BrokenRule(
        "rect-not-a-number", "§2.1 rect components are numbers",
        lambda c: _last_desk(c)["shape"].__setitem__("rect", [0.1, 0.1, "0.05", 0.1]),
        _last_desk_locator, "w is a string",
    ),
    BrokenRule(
        "rect-non-positive-extent", "§2.1 a desk has positive extent",
        lambda c: _last_desk(c)["shape"].__setitem__("rect", [0.1, 0.1, 0.0, 0.1]),
        _last_desk_locator, "a desk must have positive width and height",
    ),
    BrokenRule(
        "rect-outside-normalized", "§2.1 normalized coords ∈ [0, 1]",
        lambda c: _last_desk(c)["shape"].__setitem__("rect", [0.9, 0.1, 0.4, 0.1]),
        _last_desk_locator, "x + w = 1.3 lies outside the allowed range [0, 1]",
        'coord_space "normalized"',
    ),
    BrokenRule(
        "rect-negative-coord", "§2.1 normalized coords ∈ [0, 1]",
        lambda c: _last_desk(c)["shape"].__setitem__("rect", [-0.2, 0.1, 0.1, 0.1]),
        _last_desk_locator, "x = -0.2 lies outside the allowed range [0, 1]",
    ),
    BrokenRule(
        "polygon-not-array", "§2.1 polygon = [[x, y], ...]",
        lambda c: _last_desk(c).__setitem__("shape", {"polygon": "0,0 1,1"}),
        _last_desk_locator, "expected an array of [x, y] points",
    ),
    BrokenRule(
        "polygon-too-few-points", "§2.1 polygon needs ≥ 3 points",
        lambda c: _last_desk(c).__setitem__(
            "shape", {"polygon": [[0.1, 0.1], [0.2, 0.2]]}
        ),
        _last_desk_locator, "has 2 point(s); a polygon needs at least 3",
    ),
    BrokenRule(
        "polygon-bad-point", "§2.1 polygon points are [x, y] pairs",
        lambda c: _last_desk(c).__setitem__(
            "shape", {"polygon": [[0.1, 0.1], [0.2, 0.2], [0.3]]}
        ),
        _last_desk_locator, "expected a two-element [x, y] pair",
    ),
    BrokenRule(
        "polygon-outside-space", "§2.1 normalized coords ∈ [0, 1]",
        lambda c: _last_desk(c).__setitem__(
            "shape", {"polygon": [[0.1, 0.1], [0.2, 0.2], [1.4, 0.3]]}
        ),
        _last_desk_locator, "x = 1.4 lies outside the allowed range [0, 1]",
    ),
]


@pytest.mark.parametrize("rule", as_params(ROOMS_RULES))
def test_rooms_rule_is_enforced(case: ConfigCase, rule: BrokenRule):
    problem = check(case, rule)
    assert problem.render(), rule.spec


def test_pixel_space_bounds_are_the_image(case: ConfigCase):
    """SPEC §2.1: in `pixels` space, coordinates must lie inside image bounds."""
    width, height = case.rooms["rooms"][0]["image_size"]
    case.rooms["coord_space"] = "pixels"
    for room in case.rooms["rooms"]:
        for desk in room["desks"]:
            x, y, w, h = desk["shape"]["rect"]
            desk["shape"]["rect"] = [x * width, y * height, w * width, h * height]
    case.load()  # rescaled config is valid in pixel space

    _last_desk(case)["shape"]["rect"] = [width - 1, 10, 50, 20]
    problem = find_problem(
        case.errors(),
        _last_desk_locator(case),
        f"x + w = {width + 49:g} lies outside the allowed range [0, {width:g}]",
    )
    assert f"{width}x{height} floor-plan image" in problem.hint


def test_rooms_zone_error_message_matches_the_spec_example(config_case):
    """The literal message shape SPEC §2.1 prints as the required example."""
    case = config_case(n_people=4, k=3, n_desks=6, n_zones=2)
    desk = case.rooms["rooms"][0]["desks"][0]
    desk["zone"] = "senior_side"
    problem = find_problem(case.errors(), what="references zone 'senior_side'")

    assert problem.where == f'rooms.json: rooms[0].desks[0] ("{desk["id"]}")'
    assert problem.what == (
        "references zone 'senior_side', which is not defined in rooms.json:zones."
    )
    assert problem.hint.endswith(
        "Defined zones are: " + ", ".join(sorted(case.zone_ids)) + "."
    )


# --- rooms.json warnings ---------------------------------------------------


def test_unreferenced_zone_warns_but_loads(case: ConfigCase):
    """SPEC §2.1: an empty zone is legal but almost always a typo."""
    case.rooms["zones"]["zone_unused"] = {"label": "Nobody", "color": "#123456"}
    warning = find_warning(case.warnings(), "rooms.json: zones.zone_unused", "no desk is in it")
    assert "almost always a typo" in warning


def test_overlapping_desks_warn_and_name_both(case: ConfigCase):
    """SPEC §2.1: overlapping shapes produce a warning naming both desks."""
    desks = case.rooms["rooms"][0]["desks"]
    assert len(desks) >= 2, "fixture must have at least two desks to overlap"
    desks[1]["shape"] = {"rect": list(desks[0]["shape"]["rect"])}
    warning = find_warning(
        case.warnings(),
        f'rooms.json: rooms[0].desks[0] ("{desks[0]["id"]}")',
        f"overlaps desk '{desks[1]['id']}'",
    )
    assert "100% of the smaller desk's area" in warning


def test_abutting_desks_do_not_warn(case: ConfigCase):
    """Desks that merely share an edge are a calibration artefact, not a mistake."""
    desks = case.rooms["rooms"][0]["desks"]
    x, y, w, h = desks[0]["shape"]["rect"]
    desks[1]["shape"] = {"rect": [x + w, y, w, h]}
    assert not [w for w in case.warnings() if "overlaps desk" in w]


def test_missing_floorplan_image_warns(case: ConfigCase):
    (case.path / case.rooms["rooms"][0]["image"]).unlink()
    warning = find_warning(case.warnings(), ".image:", "does not exist")
    assert "the web form cannot" in warning


def test_absent_image_key_is_silent(case: ConfigCase):
    """A room with no `image` draws itself from the desk rectangles, on purpose.

    That is the shipped configuration -- the department decided the schematic
    is the map -- so warning about it on every run would be a warning nobody
    reads, which is how the one that matters gets missed. The *configured but
    missing* case still warns; see test_missing_image_file_warns above.
    """
    case.rooms["rooms"][0].pop("image")
    assert not [w for w in case.warnings() if ".image:" in w], (
        "an intentionally image-less room must not warn: " + repr(case.warnings())
    )
    case.load()  # and it must still load


def test_absolute_image_path_warns(case: ConfigCase):
    case.rooms["rooms"][0]["image"] = "/Users/somebody/plan.png"
    find_warning(case.warnings(), ".image:", "is an absolute path")


def test_missing_labels_warn(case: ConfigCase):
    room = case.rooms["rooms"][0]
    desk = room["desks"][0]
    room.pop("label")
    desk.pop("label")
    case.rooms["zones"][case.zone_ids[0]].pop("label")
    warnings = case.warnings()
    find_warning(warnings, f'rooms.json: rooms[0] ("{room["id"]}"):', "has no 'label'")
    find_warning(warnings, f'rooms.json: rooms[0].desks[0] ("{desk["id"]}")', "has no 'label'")
    find_warning(warnings, f"rooms.json: zones.{case.zone_ids[0]}", "has no 'label'")


# --------------------------------------------------------------------------
# SPEC §2.2 — eligibility.json
# --------------------------------------------------------------------------


def _first_rule(case: ConfigCase) -> dict[str, Any]:
    return case.eligibility["rules"][0]


def _catch_all(case: ConfigCase) -> dict[str, Any]:
    return case.eligibility["rules"][-1]


def _rule_locator(index: int) -> Callable[[ConfigCase], str]:
    def locate(case: ConfigCase) -> str:
        rule = case.eligibility["rules"][index]
        real = index if index >= 0 else len(case.eligibility["rules"]) + index
        rule_id = rule.get("id") if isinstance(rule, dict) else None
        if isinstance(rule_id, str):
            return f'eligibility.json: rules[{real}] ("{rule_id}")'
        return f"eligibility.json: rules[{real}]"

    return locate


def _set_when(case: ConfigCase, when: Any) -> None:
    """Give the FIRST rule a predicate. The catch-all must stay last."""
    _first_rule(case)["when"] = when


def _nested_not(depth: int) -> Any:
    """`{"not": {"not": ...}}` nested `depth` deep — past the recursion guard."""
    matcher: Any = 1
    for _ in range(depth):
        matcher = {"not": matcher}
    return matcher


ELIGIBILITY_RULES: list[BrokenRule] = [
    BrokenRule(
        "top-level-not-object", "§2.2 document shape",
        lambda c: c.set_raw("eligibility.json", "42"),
        "eligibility.json: (top level)", "expected a JSON object, got a number",
    ),
    BrokenRule(
        "schema-version-missing", "§2.2 schema_version",
        lambda c: c.eligibility.pop("schema_version"),
        "eligibility.json: schema_version", "missing required key 'schema_version'",
    ),
    BrokenRule(
        "rules-missing", "§2.2 rules",
        lambda c: c.eligibility.pop("rules"),
        "eligibility.json: (top level)", "missing required key 'rules'",
        "first match wins",
    ),
    BrokenRule(
        "rules-not-array", "§2.2 rules is a list",
        lambda c: c.eligibility.__setitem__("rules", {"everyone": "*"}),
        "eligibility.json: (top level)", "'rules' is an object",
    ),
    BrokenRule(
        "rules-empty", "§2.2 at least a catch-all",
        lambda c: c.eligibility.__setitem__("rules", []),
        "eligibility.json: rules", "is empty; every person would have undefined eligibility",
    ),
    BrokenRule(
        "rule-not-object", "§2.2 rule shape",
        lambda c: c.eligibility["rules"].insert(0, "precandidates"),
        "eligibility.json: rules[0]", "expected an object, got a string",
    ),
    BrokenRule(
        "rule-id-missing", "§2.2 rule.id",
        lambda c: _first_rule(c).pop("id"),
        "eligibility.json: rules[0]", "missing required key 'id'",
    ),
    BrokenRule(
        "rule-id-duplicate", "§2.2 rule.id unique",
        lambda c: _catch_all(c).__setitem__("id", _first_rule(c)["id"]),
        _rule_locator(-1), "duplicate rule id", "Rule ids must be unique",
    ),
    BrokenRule(
        "rule-reason-not-string", "§2.2 rule.reason",
        lambda c: _first_rule(c).__setitem__("reason", 12),
        _rule_locator(0), "'reason' is a number",
    ),
    BrokenRule(
        "when-missing", "§2.2 rule.when",
        lambda c: _first_rule(c).pop("when"),
        _rule_locator(0), "missing required key 'when'",
        "Use {} for a catch-all",
    ),
    BrokenRule(
        "when-not-object", "§2.2 rule.when is a predicate object",
        lambda c: _set_when(c, ["candidacy", "precandidate"]),
        _rule_locator(0), "'when' is an array",
    ),
    BrokenRule(
        "when-attribute-not-a-roster-column", "§2.2 attributes must be roster columns",
        lambda c: _set_when(c, {"cohort": "precandidate"}),
        _rule_locator(0), "tests roster attribute 'cohort', which is not a column in roster.csv",
        "Roster columns are:",
    ),
    BrokenRule(
        "matcher-null", "§2.2 matcher grammar",
        lambda c: _set_when(c, {"candidacy": None}),
        _rule_locator(0), "matcher is null",
    ),
    BrokenRule(
        "matcher-empty-list", "§2.2 list matcher",
        lambda c: _set_when(c, {"year": []}),
        _rule_locator(0), "matcher is an empty list, which matches nobody",
    ),
    BrokenRule(
        "matcher-nested-list", "§2.2 list matchers hold plain values",
        lambda c: _set_when(c, {"year": [1, [2, 3]]}),
        _rule_locator(0), "list matchers hold plain values; got an array",
    ),
    BrokenRule(
        "matcher-list-with-null", "§2.2 list matchers hold plain values",
        lambda c: _set_when(c, {"year": [1, None]}),
        _rule_locator(0), "list matchers cannot contain null",
    ),
    BrokenRule(
        "matcher-empty-object", "§2.2 range matcher",
        lambda c: _set_when(c, {"year": {}}),
        _rule_locator(0), "matcher is an empty object, which constrains nothing",
    ),
    BrokenRule(
        "matcher-unknown-key", "§2.2 range matcher keys are min/max",
        lambda c: _set_when(c, {"year": {"lt": 3}}),
        _rule_locator(0), "object matcher has key(s) 'lt'",
        "Valid matcher keys",
    ),
    BrokenRule(
        "matcher-not-plus-others", "§2.2 negation holds only 'not'",
        lambda c: _set_when(c, {"year": {"not": 1, "min": 2}}),
        _rule_locator(0), "combines 'not' with min",
        'Nest instead: {"not": {"min": 1, "max": 2}}',
    ),
    BrokenRule(
        "matcher-bound-not-numeric", "§2.2 range bounds are numbers",
        lambda c: _set_when(c, {"year": {"min": "one"}}),
        _rule_locator(0), "a range bound must be a finite number",
    ),
    BrokenRule(
        "matcher-min-above-max", "§2.2 inclusive range",
        lambda c: _set_when(c, {"year": {"min": 5, "max": 2}}),
        _rule_locator(0), "range is min=5 > max=2, which matches nobody",
    ),
    BrokenRule(
        "matcher-too-deep", "§2.2 negation nesting",
        lambda c: _set_when(c, {"year": _nested_not(12)}),
        _rule_locator(0), "matcher is nested too deeply",
    ),
    BrokenRule(
        "allow-zones-missing", "§2.2 allow_zones",
        lambda c: _first_rule(c).pop("allow_zones"),
        _rule_locator(0), "missing required key 'allow_zones'",
        "Defined zones are:",
    ),
    BrokenRule(
        "allow-zones-bare-string", "§2.2 allow_zones is \"*\" or a list",
        lambda c: _first_rule(c).__setitem__("allow_zones", c.zone_ids[0]),
        _rule_locator(0), 'the only string form is "*"',
        "To allow one zone write it as a list",
    ),
    BrokenRule(
        "allow-zones-wrong-type", "§2.2 allow_zones is \"*\" or a list",
        lambda c: _first_rule(c).__setitem__("allow_zones", 3),
        _rule_locator(0), 'expected "*" or an array of zone ids',
    ),
    BrokenRule(
        "allow-zones-empty", "§2.2 allow_zones must permit something",
        lambda c: _first_rule(c).__setitem__("allow_zones", []),
        _rule_locator(0), "is an empty array, so anyone matching this rule would have no eligible",
    ),
    BrokenRule(
        "allow-zones-entry-not-string", "§2.2 allow_zones entries are zone ids",
        lambda c: _first_rule(c).__setitem__("allow_zones", [1]),
        _rule_locator(0), "expected a zone id string",
    ),
    BrokenRule(
        "allow-zones-undefined", "§2.2 allow_zones validated against rooms.json",
        lambda c: _first_rule(c).__setitem__("allow_zones", ["basement"]),
        _rule_locator(0), "references zone 'basement', which is not defined in rooms.json:zones",
        "Defined zones are:",
    ),
    BrokenRule(
        "catch-all-not-last", "§2.2 the last rule MUST be a catch-all",
        lambda c: c.eligibility["rules"].append(
            {
                "id": "stragglers",
                "when": {"year": {"min": 9}},
                "allow_zones": "*",
                "reason": "Not a catch-all.",
            }
        ),
        _rule_locator(-1), "is the last rule but is not a catch-all",
        '"when": {}',
    ),
]


@pytest.mark.parametrize("rule", as_params(ELIGIBILITY_RULES))
def test_eligibility_rule_is_enforced(case: ConfigCase, rule: BrokenRule):
    problem = check(case, rule)
    assert problem.render(), rule.spec


def test_rule_below_a_catch_all_warns_as_unreachable(case: ConfigCase):
    """§2.2 is first-match-wins, so anything under a catch-all is dead code."""
    rules = case.eligibility["rules"]
    rules.insert(
        len(rules) - 1 if len(rules) > 1 else 0,
        {"id": "everyone_early", "when": {}, "allow_zones": "*", "reason": "Catch-all."},
    )
    warning = find_warning(case.warnings(), "can never match", "is a catch-all and comes first")
    assert "the catch-all must be last" in warning


def test_missing_reason_warns(case: ConfigCase):
    _first_rule(case).pop("reason")
    find_warning(case.warnings(), "eligibility.json: rules[0]", "has no 'reason'")


def test_duplicate_zone_in_allow_zones_warns(case: ConfigCase):
    zone = case.zone_ids[0]
    _first_rule(case)["allow_zones"] = [zone, zone]
    find_warning(case.warnings(), ".allow_zones[1]", f"lists zone '{zone}' twice")


def test_boolean_matcher_warns(case: ConfigCase):
    _set_when(case, {"keeps_desk": True})
    find_warning(case.warnings(), ".when.keeps_desk", "matcher is the boolean true")


#: The SPEC §2.2 matcher table, read as an executable specification:
#: (predicate, roster attributes, does the rule fire?).
PREDICATE_SEMANTICS = [
    ("scalar", {"candidacy": "precandidate"}, {"candidacy": "precandidate"}, True),
    ("scalar-trimmed-and-case-folded",
     {"candidacy": "PreCandidate"}, {"candidacy": "  precandidate "}, True),
    ("scalar-miss", {"candidacy": "precandidate"}, {"candidacy": "candidate"}, False),
    ("scalar-numeric-across-types", {"year": 2}, {"year": "2"}, True),
    ("list-member", {"year": [1, 2]}, {"year": "2"}, True),
    ("list-non-member", {"year": [1, 2]}, {"year": "4"}, False),
    ("range-inside", {"year": {"min": 1, "max": 2}}, {"year": "1"}, True),
    ("range-at-the-upper-bound", {"year": {"min": 1, "max": 2}}, {"year": "2"}, True),
    ("range-outside", {"year": {"min": 1, "max": 2}}, {"year": "3"}, False),
    ("range-open-min", {"year": {"min": 3}}, {"year": "6"}, True),
    ("range-open-max", {"year": {"max": 3}}, {"year": "6"}, False),
    ("negation-hits", {"candidacy": {"not": "candidate"}}, {"candidacy": "precandidate"}, True),
    ("negation-misses", {"candidacy": {"not": "candidate"}}, {"candidacy": "candidate"}, False),
    ("negated-range", {"year": {"not": {"min": 3}}}, {"year": "1"}, True),
    ("anded-both-true", {"candidacy": "precandidate", "year": {"max": 2}},
     {"candidacy": "precandidate", "year": "2"}, True),
    ("anded-one-false", {"candidacy": "precandidate", "year": {"max": 2}},
     {"candidacy": "precandidate", "year": "5"}, False),
    ("extra-column", {"advisor": "Rubin"}, {"advisor": "Rubin"}, True),
]


@pytest.mark.parametrize(
    "matcher,attributes,should_match",
    [pytest.param(m, a, s, id=i) for i, m, a, s in PREDICATE_SEMANTICS],
)
def test_predicate_grammar_evaluates_as_documented(
    config_case, matcher, attributes, should_match
):
    """§2.2's matcher table, evaluated through the real rule-table interpreter.

    Validation checks that a predicate is *well formed*; this checks that a
    well-formed one *means* what the table says. Getting the second one wrong is
    how somebody quietly ends up eligible for the wrong side of the room.
    """
    from deskmatch import eligibility as elig

    case = config_case(n_people=6, k=3, n_zones=3)
    restricted = case.zone_ids[0]
    case.eligibility["rules"] = [
        {
            "id": "under_test",
            "when": matcher,
            "allow_zones": [restricted],
            "reason": "The rule being tested.",
        },
        {"id": "everyone_else", "when": {}, "allow_zones": "*", "reason": "Catch-all."},
    ]
    row = case.roster_rows[0]
    row.update({key: str(value) for key, value in attributes.items()})
    row["keeps_desk"] = "no"
    row["current_desk"] = ""

    config = case.load()
    person = config.roster.by_email(row["email"].strip().lower())
    assert person is not None
    zones = elig.allowed_zones(config.eligibility, config.rooms, person)

    expected = (restricted,) if should_match else tuple(sorted(case.zone_ids))
    assert zones == expected
    assert len(case.zone_ids) > 1, "the two outcomes must be distinguishable"
    reason = elig.eligibility_reason(config.eligibility, person)
    assert reason == ("The rule being tested." if should_match else "Catch-all.")


def test_all_four_predicate_forms_are_accepted(config_case):
    """§2.2 grammar: scalar, list, range, negation — all four must load."""
    forms = [
        {"candidacy": "precandidate"},
        {"year": [1, 2]},
        {"year": {"min": 1, "max": 2}},
        {"candidacy": {"not": "candidate"}},
        {"year": {"not": {"min": 3}}},
        {"candidacy": "precandidate", "year": {"max": 2}},  # ANDed
    ]
    for index, when in enumerate(forms):
        case = config_case(n_people=6, k=3)
        zone = case.zone_ids[0]
        case.eligibility["rules"] = [
            {"id": f"form_{index}", "when": when, "allow_zones": [zone], "reason": "x"},
            {"id": "everyone", "when": {}, "allow_zones": "*", "reason": "y"},
        ]
        config = case.load()
        assert config.eligibility.rules[0].when == when


# --------------------------------------------------------------------------
# SPEC §2.3 — roster.csv
# --------------------------------------------------------------------------


def _row(case: ConfigCase, index: int = 0) -> dict[str, str]:
    return case.roster_rows[index]


def _non_keeper(case: ConfigCase) -> dict[str, str]:
    for row in case.roster_rows:
        if not row.get("current_desk"):
            return row
    raise AssertionError("fixture has no non-keeper row")


ROSTER_RULES: list[BrokenRule] = [
    BrokenRule(
        "header-missing-required-column", "§2.3 required columns",
        lambda c: (
            c.roster_fields.remove("candidacy"),
            [row.pop("candidacy", None) for row in c.roster_rows],
        ),
        "roster.csv: (header)", "missing required column 'candidacy'",
        "Extra columns are fine",
    ),
    BrokenRule(
        "no-data-rows", "§2.3 the roster lists people",
        lambda c: c.roster_rows.clear(),
        "roster.csv: (no data rows)", "contains a header but no people",
    ),
    BrokenRule(
        "completely-empty", "§2.3 the roster is a CSV",
        lambda c: c.set_raw("roster.csv", ""),
        "roster.csv", "is empty; there is not even a header row",
    ),
    BrokenRule(
        "bad-utf8", "§2.3 roster is UTF-8",
        lambda c: c.set_raw("roster.csv", b"name,email\nAda,\xffada@umich.edu\n"),
        "roster.csv: byte", "is not valid UTF-8",
        "CSV UTF-8",
    ),
    BrokenRule(
        "duplicate-column", "§2.3 columns are attribute names",
        lambda c: c.set_raw(
            "roster.csv",
            "name,email,year,candidacy,keeps_desk,current_desk,year\n"
            "Ada,a@umich.edu,3,candidate,no,,3\n",
        ),
        "roster.csv: (header)", "column 'year' appears twice",
    ),
    BrokenRule(
        "empty-column-name", "§2.3 every column needs a name",
        lambda c: c.set_raw(
            "roster.csv",
            "name,email,year,candidacy,keeps_desk,current_desk,\n"
            "Ada,a@umich.edu,3,candidate,no,,\n",
        ),
        "roster.csv: (header), column 7", "has an empty name",
    ),
    BrokenRule(
        "row-field-count", "§2.3 a comma in a value must be quoted",
        lambda c: c.set_raw(
            "roster.csv",
            "name,email,year,candidacy,keeps_desk,current_desk\n"
            "Chandrasekhar, Subrahmanyan,s@umich.edu,4,candidate,no,\n",
        ),
        "roster.csv: line 2", "has 7 field(s) but the header declares 6",
        "must be quoted",
    ),
    BrokenRule(
        "email-empty", "§2.3 email is the primary key",
        lambda c: _row(c).__setitem__("email", "  "),
        "roster.csv: line 2, column 'email'", "is empty; email is the primary key",
    ),
    BrokenRule(
        "email-duplicate", "§2.3 email unique (case-insensitive, trimmed)",
        lambda c: c.roster_rows.append(
            dict(_row(c), email=f"  {_row(c)['email'].upper()} ", current_desk="")
        ),
        "roster.csv: line", "duplicate email",
        "Email is the primary key",
    ),
    BrokenRule(
        "year-not-a-number", "§2.3 year is an integer ≥ 1",
        lambda c: _row(c).__setitem__("year", "second"),
        "roster.csv: line 2", "expected a whole number >= 1",
    ),
    BrokenRule(
        "year-zero", "§2.3 year ≥ 1",
        lambda c: _row(c).__setitem__("year", "0"),
        "roster.csv: line 2", 'is "0"; expected a whole number >= 1',
    ),
    BrokenRule(
        "keeps-desk-unrecognised", "§2.3 keeps_desk vocabulary",
        lambda c: _row(c).__setitem__("keeps_desk", "maybe"),
        "column 'keeps_desk'", "which is not a recognised yes/no value",
        "Accepted values (case-insensitive)",
    ),
    BrokenRule(
        "keeper-without-current-desk", "§2.3 keeps_desk truthy ⇒ current_desk required",
        lambda c: _non_keeper(c).update(keeps_desk="yes", current_desk=""),
        "roster.csv: line", "a desk keeper must name the desk",
        "removed from the pool before solving",
    ),
    BrokenRule(
        "current-desk-unknown", "§2.3 current_desk must be a valid desk id",
        lambda c: _non_keeper(c).update(keeps_desk="yes", current_desk="D999"),
        "roster.csv: line", "is 'D999', which is not a desk id defined in rooms.json",
        "Defined desk ids",
    ),
    BrokenRule(
        "two-people-keep-one-desk", "§2.3 two people keeping the same desk is an error",
        lambda c: [
            row.update(keeps_desk="yes", current_desk=c.desk_ids[0])
            for row in c.roster_rows[:2]
        ],
        "roster.csv: line", "also keeps",
        "Two people cannot keep the same desk",
    ),
]


@pytest.mark.parametrize("rule", as_params(ROSTER_RULES))
def test_roster_rule_is_enforced(case: ConfigCase, rule: BrokenRule):
    problem = check(case, rule)
    assert problem.render(), rule.spec


def test_roster_warnings(case: ConfigCase):
    """The §2.3 conditions that are survivable, and therefore warnings."""
    rows = case.roster_rows
    assert len(rows) >= 4, "fixture must be big enough for four independent warnings"
    non_keepers = [row for row in rows if not row.get("current_desk")]
    assert len(non_keepers) >= 4

    non_keepers[0]["email"] = "ada-at-umich"
    non_keepers[1]["name"] = ""
    non_keepers[2]["candidacy"] = ""
    non_keepers[3]["keeps_desk"] = ""

    warnings = case.warnings()
    find_warning(warnings, "column 'email'", "does not look like an email address")
    find_warning(warnings, "column 'name'", "is empty; reports and the assignment sheet")
    find_warning(warnings, "column 'candidacy'", "is empty; this person can only be matched")
    find_warning(warnings, "column 'keeps_desk'", "treating it as 'no'")


def test_current_desk_ignored_when_not_keeping(case: ConfigCase):
    row = _non_keeper(case)
    row["keeps_desk"] = "no"
    row["current_desk"] = case.desk_ids[-1]
    warning = find_warning(case.warnings(), "column 'current_desk'", "so it is ignored")
    assert "the desk stays in" in warning


def test_unknown_current_desk_is_only_a_warning_when_not_keeping(case: ConfigCase):
    row = _non_keeper(case)
    row["keeps_desk"] = "no"
    row["current_desk"] = "D999"
    find_warning(case.warnings(), "column 'current_desk'", "'D999' is not a desk id")


def test_header_whitespace_is_trimmed_with_a_warning(case: ConfigCase):
    case.dump()
    text = (case.path / "roster.csv").read_text(encoding="utf-8")
    header, _, body = text.partition("\n")
    case.set_raw("roster.csv", " " + header.replace(",", " , ") + "\n" + body)
    find_warning(case.warnings(), "roster.csv: (header)", "leading or trailing spaces")


def test_unreferenced_candidacy_value_warns(case: ConfigCase):
    """§2.3: a roster value no eligibility rule mentions is probably a typo."""
    _non_keeper(case)["candidacy"] = "postcandidate"
    warning = find_warning(
        case.warnings(),
        "roster.csv: column 'candidacy'",
        "value 'postcandidate' is held by 1 person",
    )
    assert "never referenced by any rule in eligibility.json" in warning


def test_numeric_columns_do_not_produce_unreferenced_value_noise(case: ConfigCase):
    """A range matcher names no literal, so `year` must not be flagged per value."""
    _set_when(case, {"year": {"min": 1, "max": 2}})
    assert not [w for w in case.warnings() if "column 'year'" in w and "never referenced" in w]


def test_extra_columns_are_preserved_and_usable_in_predicates(case: ConfigCase):
    """§2.3: extra columns are preserved and usable in eligibility predicates."""
    advisors = sorted({row["advisor"] for row in case.roster_rows})
    assert advisors, "the synth fixture is expected to carry an 'advisor' column"

    zone = case.zone_ids[0]
    case.eligibility["rules"] = [
        {
            "id": "advisor_rule",
            "when": {"advisor": advisors[0]},
            "allow_zones": [zone],
            "reason": "Grouped with their advisor's students.",
        },
        {"id": "everyone", "when": {}, "allow_zones": "*", "reason": "Anywhere."},
    ]
    config = case.load()
    for person in config.roster.people:
        assert person.attributes["advisor"] in advisors


def test_email_is_lowercased_and_trimmed_and_people_are_sorted(case: ConfigCase):
    row = _non_keeper(case)
    row["email"] = f"  {row['email'].upper()}  "
    config = case.load()
    emails = [p.email for p in config.roster.people]
    assert emails == sorted(emails), "roster must be sorted by email (I3)"
    assert all(e == e.strip().lower() for e in emails)


def test_keeper_and_desk_leave_the_pool(case: ConfigCase):
    """§2.3/§3.4: a keeper and their desk are both removed before solving."""
    row = _non_keeper(case)
    desk = case.desk_ids[-1]
    row["keeps_desk"] = "TRUE"
    row["current_desk"] = desk
    config = case.load()
    person = config.roster.by_email(row["email"].strip().lower())
    assert person is not None and person.keeps_desk and person.current_desk == desk


@pytest.mark.parametrize("token", list(validate.TRUTHY_STRINGS))
def test_every_documented_truthy_token_is_accepted(case: ConfigCase, token: str):
    row = _non_keeper(case)
    row["keeps_desk"] = token.upper() if token.isalpha() else token
    row["current_desk"] = case.desk_ids[-1]
    config = case.load()
    person = config.roster.by_email(row["email"])
    assert person is not None and person.keeps_desk is True


@pytest.mark.parametrize("token", list(validate.FALSY_STRINGS))
def test_every_documented_falsy_token_is_accepted(case: ConfigCase, token: str):
    row = _non_keeper(case)
    row["keeps_desk"] = token.title() if token.isalpha() else token
    config = case.load()
    person = config.roster.by_email(row["email"])
    assert person is not None and person.keeps_desk is False
    assert person.current_desk is None


# --------------------------------------------------------------------------
# SPEC §2.4 — scoring.json
# --------------------------------------------------------------------------


def _primary(case: ConfigCase) -> str:
    return case.scoring["primary_curve"]


def _set_primary_curve(case: ConfigCase, values: list[Any]) -> None:
    case.scoring["curves"][_primary(case)] = values


def _other_curve(case: ConfigCase) -> str:
    for name in sorted(case.scoring["curves"]):
        if name != _primary(case) and not name.startswith("_"):
            return name
    raise AssertionError("fixture must define more than one curve")


def _flat_curve(case: ConfigCase) -> list[Any]:
    """A K-length curve whose last two ranks are worth the same."""
    k = case.k
    return [k - i for i in range(k - 1)] + [2]


SCORING_RULES: list[BrokenRule] = [
    BrokenRule(
        "top-level-not-object", "§2.4 document shape",
        lambda c: c.set_raw("scoring.json", '"linear_borda"'),
        "scoring.json: (top level)", "expected a JSON object, got a string",
    ),
    BrokenRule(
        "schema-version-missing", "§2.4 schema_version",
        lambda c: c.scoring.pop("schema_version"),
        "scoring.json: schema_version", "missing required key 'schema_version'",
    ),
    BrokenRule(
        "curves-missing", "§2.4 curves",
        lambda c: c.scoring.pop("curves"),
        "scoring.json: (top level)", "missing required key 'curves'",
    ),
    BrokenRule(
        "curves-not-object", "§2.4 curves",
        lambda c: c.scoring.__setitem__("curves", [[5, 4, 3, 2, 1]]),
        "scoring.json: (top level)", "'curves' is an array",
    ),
    BrokenRule(
        "curves-empty", "§2.4 K = len(curves[primary])",
        lambda c: c.scoring.__setitem__("curves", {}),
        "scoring.json: curves", "defines no curves, so K",
    ),
    BrokenRule(
        "curve-not-array", "§2.4 a curve is an array of numbers",
        lambda c: _set_primary_curve(c, "5,4,3,2,1"),
        lambda c: f"scoring.json: curves.{_primary(c)}", "expected an array of numbers",
    ),
    BrokenRule(
        "curve-empty", "§2.4 a curve has one value per rank",
        lambda c: _set_primary_curve(c, []),
        lambda c: f"scoring.json: curves.{_primary(c)}", "is empty; a curve needs one value per rank",
    ),
    BrokenRule(
        "curve-value-quoted", "§2.4 values are numbers",
        lambda c: _set_primary_curve(c, [str(c.k - i) for i in range(c.k)]),
        lambda c: f"scoring.json: curves.{_primary(c)}[0]", "is the string",
        "Remove the quotes",
    ),
    BrokenRule(
        "curve-value-null", "§2.4 values are numbers",
        lambda c: _set_primary_curve(c, [None] + [c.k - i for i in range(1, c.k)]),
        lambda c: f"scoring.json: curves.{_primary(c)}[0]", "rank 1 is null",
    ),
    BrokenRule(
        "curve-value-boolean", "§2.4 values are numbers",
        lambda c: _set_primary_curve(c, [True] + [c.k - i for i in range(1, c.k)]),
        lambda c: f"scoring.json: curves.{_primary(c)}[0]", "rank 1 is a boolean",
    ),
    BrokenRule(
        "curve-value-not-finite", "§2.4 values are ordinary decimals",
        lambda c: _set_primary_curve(
            c, [float("inf")] + [c.k - i for i in range(1, c.k)]
        ),
        lambda c: f"scoring.json: curves.{_primary(c)}[0]", "which is not a finite number",
    ),
    BrokenRule(
        "curve-value-zero", "§2.4 all values > 0",
        lambda c: _set_primary_curve(c, [c.k - i for i in range(c.k - 1)] + [0]),
        lambda c: f"scoring.json: curves.{_primary(c)}[{c.k - 1}]",
        "every curve value must be strictly positive",
        "indistinguishable",
    ),
    BrokenRule(
        "curve-value-negative", "§2.4 all values > 0",
        lambda c: _set_primary_curve(c, [c.k - i for i in range(c.k - 1)] + [-1]),
        lambda c: f"scoring.json: curves.{_primary(c)}[{c.k - 1}]",
        "every curve value must be strictly positive",
    ),
    BrokenRule(
        "curve-value-non-terminating", "§2.4 non-terminating decimals are rejected",
        lambda c: _set_primary_curve(
            c, [c.k - i for i in range(c.k - 1)] + [1 / 3]
        ),
        lambda c: f"scoring.json: curves.{_primary(c)}[{c.k - 1}]",
        "which is a non-terminating decimal",
        "astronomical scale factor",
    ),
    BrokenRule(
        "curve-not-strictly-decreasing-equal", "§2.4 strictly decreasing",
        lambda c: _set_primary_curve(c, [2] * c.k),
        lambda c: f"scoring.json: curves.{_primary(c)}[1]", "which is the same as rank 1",
        "STRICTLY decreasing",
    ),
    BrokenRule(
        "curve-not-strictly-decreasing-increasing", "§2.4 strictly decreasing",
        lambda c: _set_primary_curve(c, [i + 1 for i in range(c.k)]),
        lambda c: f"scoring.json: curves.{_primary(c)}[1]", "which is more than rank 1",
    ),
    BrokenRule(
        "curve-flat-tail", "§2.4 strictly decreasing at every adjacent pair",
        lambda c: _set_primary_curve(c, _flat_curve(c)),
        lambda c: f"scoring.json: curves.{_primary(c)}[{c.k - 1}]", "which is the same as rank",
    ),
    BrokenRule(
        "curves-differ-in-length", "§2.4 all curves have length K",
        lambda c: c.scoring["curves"].__setitem__(
            _other_curve(c), c.scoring["curves"][_other_curve(c)][:-1]
        ),
        lambda c: f"scoring.json: curves.{_other_curve(c)}", "values but curves.",
        "that length IS K",
    ),
    BrokenRule(
        "curve-overflows-int64", "§2.4/§5.3 exact integer points",
        lambda c: _set_primary_curve(c, [2 ** 62 + c.k - i for i in range(c.k)]),
        lambda c: f"scoring.json: curves.{_primary(c)}", "which overflows the",
    ),
    BrokenRule(
        "primary-curve-missing", "§2.4 primary_curve",
        lambda c: c.scoring.pop("primary_curve"),
        "scoring.json: primary_curve", "missing required key 'primary_curve'",
        "its length is K",
    ),
    BrokenRule(
        "primary-curve-not-string", "§2.4 primary_curve names a curve",
        lambda c: c.scoring.__setitem__("primary_curve", [1, 2]),
        "scoring.json: primary_curve", "expected the name of a curve",
    ),
    BrokenRule(
        "primary-curve-undefined", "§2.4 primary_curve must exist",
        lambda c: c.scoring.__setitem__("primary_curve", "linear-borda"),
        "scoring.json: primary_curve", "which is not defined in scoring.json:curves",
        "Did you mean 'linear_borda'?",
    ),
    BrokenRule(
        "comparison-curves-not-array", "§2.4 comparison_curves",
        lambda c: c.scoring.__setitem__("comparison_curves", "convex"),
        "scoring.json: comparison_curves", "expected an array of curve names",
    ),
    BrokenRule(
        "comparison-curve-not-string", "§2.4 comparison_curves entries",
        lambda c: c.scoring.__setitem__("comparison_curves", [7]),
        "scoring.json: comparison_curves[0]", "expected a curve name",
    ),
    BrokenRule(
        "comparison-curve-undefined", "§2.4 comparison_curves must exist",
        lambda c: c.scoring.__setitem__("comparison_curves", ["parabolic"]),
        "scoring.json: comparison_curves[0]", "which is not defined in scoring.json:curves",
    ),
    BrokenRule(
        "tie-break-seed-missing", "§2.4/§5.4 tie_break_seed",
        lambda c: c.scoring.pop("tie_break_seed"),
        "scoring.json: tie_break_seed", "missing required key 'tie_break_seed'",
        "announce it before the form opens",
    ),
    BrokenRule(
        "tie-break-seed-not-string", "§2.4 tie_break_seed is text",
        lambda c: c.scoring.__setitem__("tie_break_seed", 2026),
        "scoring.json: tie_break_seed", "expected a string",
        "hashed with SHA-256",
    ),
    BrokenRule(
        "tie-break-seed-empty", "§2.4 tie_break_seed non-empty",
        lambda c: c.scoring.__setitem__("tie_break_seed", "   "),
        "scoring.json: tie_break_seed", "is empty.",
    ),
    BrokenRule(
        "seed-committed-at-not-string", "§2.4 seed_committed_at",
        lambda c: c.scoring.__setitem__("seed_committed_at", 20260901),
        "scoring.json: seed_committed_at", "expected an ISO-8601",
    ),
    BrokenRule(
        "sensitivity-seeds-not-array", "§2.4 sensitivity_seeds",
        lambda c: c.scoring.__setitem__("sensitivity_seeds", "alt-a"),
        "scoring.json: sensitivity_seeds", "expected an array of seed strings",
    ),
    BrokenRule(
        "sensitivity-seed-not-string", "§2.4 sensitivity_seeds entries",
        lambda c: c.scoring.__setitem__("sensitivity_seeds", [1]),
        "scoring.json: sensitivity_seeds[0]", "expected a string",
    ),
    BrokenRule(
        "sensitivity-seed-empty", "§2.4 sensitivity_seeds entries",
        lambda c: c.scoring.__setitem__("sensitivity_seeds", [""]),
        "scoring.json: sensitivity_seeds[0]", "is empty; a seed must be a non-empty string",
    ),
    BrokenRule(
        "sensitivity-seed-duplicate", "§2.4 sensitivity seeds must be distinct",
        lambda c: c.scoring.__setitem__("sensitivity_seeds", ["alt-a", "alt-a"]),
        "scoring.json: sensitivity_seeds[1]", "repeats the seed",
        "measure nothing",
    ),
]


@pytest.mark.parametrize("rule", as_params(SCORING_RULES))
def test_scoring_rule_is_enforced(case: ConfigCase, rule: BrokenRule):
    problem = check(case, rule)
    assert problem.render(), rule.spec


def test_placeholder_seed_warns_loudly(case: ConfigCase):
    """§8 hangs on the seed being announced first; the placeholder must shout."""
    case.scoring["tie_break_seed"] = "PUBLISH-ME-BEFORE-THE-FORM-OPENS"
    warning = find_warning(case.warnings(), "tie_break_seed", "is still the shipped placeholder")
    assert "ANNOUNCED PUBLICLY BEFORE THE FORM OPENS" in warning


@pytest.mark.parametrize("marker", list(validate.PLACEHOLDER_SEED_MARKERS))
def test_every_placeholder_marker_is_detected(case: ConfigCase, marker: str):
    case.scoring["tie_break_seed"] = f"astro-desks-{marker.upper()}-2026"
    find_warning(case.warnings(), "tie_break_seed", "is still the shipped placeholder")


def test_scoring_warnings(case: ConfigCase):
    case.scoring.pop("comparison_curves")
    case.scoring["seed_committed_at"] = "last Tuesday"
    case.scoring["sensitivity_seeds"] = [case.scoring["tie_break_seed"]]
    warnings = case.warnings()
    find_warning(warnings, "comparison_curves", "no sensitivity-to-curve comparison")
    find_warning(warnings, "seed_committed_at", "is not a parseable ISO-8601 timestamp")
    find_warning(warnings, "sensitivity_seeds[0]", "is the same as tie_break_seed")


def test_missing_seed_committed_at_warns(case: ConfigCase):
    case.scoring["seed_committed_at"] = None
    find_warning(case.warnings(), "seed_committed_at", "is not set, so the audit trail")


def test_comparison_curve_duplicated_or_primary_warns(case: ConfigCase):
    other = _other_curve(case)
    case.scoring["comparison_curves"] = [other, other, _primary(case)]
    warnings = case.warnings()
    find_warning(warnings, "comparison_curves[1]", f"lists '{other}' twice")
    find_warning(warnings, "comparison_curves[2]", "lists the primary curve")


# --------------------------------------------------------------------------
# K is derived, never declared (invariant I1)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("k", [1, 2, 3, 5, 8])
def test_k_comes_from_the_primary_curve_length(config_case, k: int):
    case = config_case(n_people=4, k=k)
    config = case.load()
    assert config.k == k == len(config.scoring.curve())
    assert "K" not in case.scoring and "k" not in case.scoring


# --------------------------------------------------------------------------
# Curve values are exact rationals, via str() (SPEC §5.3)
# --------------------------------------------------------------------------


class TestExactCurveRationalisation:
    """`Fraction(str(v))`, never `Fraction(v)`.

    `Fraction(0.1)` is 3602879701896397/36028797018963968 -- the binary double,
    not the number the coordinator typed. SPEC §5.3 scales points by the LCM of
    the denominators, so reading the float would turn a harmless decimal into an
    astronomical scale factor and destroy the "distinct totals differ by exactly
    1" property the whole jitter bound rests on.
    """

    def test_half_is_exact(self):
        assert validate.curve_value_to_fraction(4.5) == Fraction(9, 2)

    def test_one_tenth_is_a_tenth_not_the_binary_double(self):
        exact = validate.curve_value_to_fraction(0.1)
        assert exact == Fraction(1, 10)
        assert exact.denominator == 10
        assert exact != Fraction(0.1)
        assert Fraction(0.1).denominator == 36028797018963968

    def test_integers_stay_integral(self):
        assert validate.curve_value_to_fraction(5) == Fraction(5, 1)
        assert validate.curve_value_to_fraction(5).denominator == 1

    @pytest.mark.parametrize(
        "value,expected",
        [
            (4.5, Fraction(9, 2)),
            (0.1, Fraction(1, 10)),
            (0.25, Fraction(1, 4)),
            (3.75, Fraction(15, 4)),
            (0.001, Fraction(1, 1000)),
        ],
    )
    def test_loaded_config_holds_exact_fractions(self, config_case, value, expected):
        case = config_case(n_people=4, k=3)
        primary = case.scoring["primary_curve"]
        # Strictly decreasing, ending on the decimal under test.
        case.scoring["curves"] = {
            primary: [value + 2, value + 1, value],
            "alt": [30, 20, 10],
        }
        case.scoring["comparison_curves"] = ["alt"]
        config = case.load()

        curve = config.scoring.curve()
        assert all(isinstance(v, Fraction) for v in curve)
        assert curve[-1] == expected
        assert curve[-1].denominator == expected.denominator, (
            "the loader must rationalise via str(); a float-based Fraction would "
            f"give denominator {Fraction(value).denominator}"
        )

    def test_scale_stays_small_for_a_decimal_curve(self, config_case):
        from deskmatch import scoring as scoring_mod

        case = config_case(n_people=4, k=3)
        primary = case.scoring["primary_curve"]
        case.scoring["curves"] = {primary: [5, 4.5, 4], "alt": [3, 2, 1]}
        case.scoring["comparison_curves"] = ["alt"]
        config = case.load()
        points, scale = scoring_mod.integerise(config.scoring.curve())
        assert scale == 2 and points == (10, 9, 8)


# --------------------------------------------------------------------------
# Underscore keys, unknown keys, and multi-problem collection
# --------------------------------------------------------------------------


def test_underscore_keys_are_ignored_everywhere(case: ConfigCase):
    """`"_comment"` is documentation. SPEC §2: it is ignored, never warned about."""
    case.rooms["_note"] = "top level"
    case.rooms["zones"]["_note"] = "not a zone"
    case.rooms["zones"][case.zone_ids[0]]["_note"] = "zone meta"
    case.rooms["rooms"][0]["_note"] = "room"
    case.rooms["rooms"][0]["desks"][0]["_note"] = "desk"
    case.rooms["rooms"][0]["desks"][0]["shape"]["_note"] = "shape"
    case.eligibility["_note"] = "top level"
    _first_rule(case)["_note"] = "rule"
    _first_rule(case)["when"]["_note"] = "predicate"
    case.scoring["_note"] = "top level"
    case.scoring["curves"]["_draft"] = [99, 98]  # wrong length on purpose

    config = case.load()
    assert not [w for w in config.warnings if "_note" in w or "_draft" in w]
    # The underscore-prefixed curve must not participate in the K calculation.
    assert config.k == case.k
    assert "_draft" not in config.scoring.curves
    assert "_note" not in config.rooms.zones


UNKNOWN_KEY_SITES: list[tuple[str, Callable[[ConfigCase], None], str]] = [
    ("rooms-top-level", lambda c: c.rooms.__setitem__("coord_spaces", "normalized"),
     "rooms.json: (top level)"),
    ("zone-meta", lambda c: c.rooms["zones"][c.zone_ids[0]].__setitem__("colour", "#fff"),
     "rooms.json: zones."),
    ("room", lambda c: c.rooms["rooms"][0].__setitem__("images", "x.png"),
     "rooms.json: rooms[0]"),
    ("desk", lambda c: c.rooms["rooms"][0]["desks"][0].__setitem__("zones", "x"),
     "rooms.json: rooms[0].desks[0]"),
    ("shape", lambda c: c.rooms["rooms"][0]["desks"][0]["shape"].__setitem__("rects", []),
     ".shape"),
    ("eligibility-top-level", lambda c: c.eligibility.__setitem__("rule", []),
     "eligibility.json: (top level)"),
    ("eligibility-rule", lambda c: _first_rule(c).__setitem__("allow_zone", "*"),
     "eligibility.json: rules[0]"),
    ("scoring-top-level", lambda c: c.scoring.__setitem__("primary_curves", "x"),
     "scoring.json: (top level)"),
]


@pytest.mark.parametrize(
    "mutate,where",
    [pytest.param(m, w, id=i) for i, m, w in UNKNOWN_KEY_SITES],
)
def test_unknown_non_underscore_keys_warn(case: ConfigCase, mutate, where):
    mutate(case)
    warning = find_warning(case.warnings(), where, "which deskmatch ignores")
    assert "Keys starting with '_'" in warning


def test_config_error_collects_every_problem_across_all_four_files(case: ConfigCase):
    """SPEC §2: "Validation errors are collected and reported together."

    A coordinator with five typos gets five messages from one run, not five
    sequential edit-run cycles. Break one rule in each file and demand all four.
    """
    _last_desk(case)["zone"] = "no_such_zone"
    _first_rule(case)["allow_zones"] = ["also_no_such_zone"]
    _row(case)["year"] = "zero"
    case.scoring["primary_curve"] = "no_such_curve"

    problems = case.errors()
    find_problem(problems, "rooms.json:", "references zone 'no_such_zone'")
    find_problem(problems, "eligibility.json:", "references zone 'also_no_such_zone'")
    find_problem(problems, "roster.csv:", "expected a whole number >= 1")
    find_problem(problems, "scoring.json: primary_curve", "is not defined")
    assert len(problems) >= 4, render_all(problems)


def test_problems_are_grouped_in_spec_file_order(case: ConfigCase):
    """§1 order: rooms, eligibility, roster, scoring — so fixing them is one pass."""
    case.scoring["primary_curve"] = "no_such_curve"
    _row(case)["year"] = "zero"
    _first_rule(case)["allow_zones"] = ["nope"]
    _last_desk(case)["zone"] = "nope"

    files = [p.where.split(":", 1)[0].strip() for p in case.errors()]
    order = {name: i for i, name in enumerate(validate.CONFIG_FILES)}
    ranks = [order[name] for name in files if name in order]
    assert ranks == sorted(ranks), files


def test_multiple_problems_in_one_file_are_all_reported(case: ConfigCase):
    for desk in case.rooms["rooms"][0]["desks"][:3]:
        desk["zone"] = f"ghost_{desk['id']}"
    problems = case.errors()
    reported = [p for p in problems if "references zone 'ghost_" in p.what]
    assert len(reported) == 3, render_all(problems)


class TestCascadeSuppression:
    """One root cause should produce one message.

    `validate.py` promises this explicitly: if `rooms.json` failed to yield any
    zone ids, every eligibility rule must NOT then be accused of referencing an
    undefined zone. A coordinator staring at forty consequences of one typo will
    not find the typo.
    """

    def test_a_broken_rooms_file_does_not_cascade_into_eligibility(self, case: ConfigCase):
        case.rooms.pop("zones")
        problems = case.errors()
        find_problem(problems, "rooms.json: (top level)", "missing required key 'zones'")
        assert_no_problem(problems, "eligibility.json", "not defined in rooms.json:zones")
        assert_no_problem(problems, "rooms.json", "references zone")

    def test_a_broken_rooms_file_does_not_cascade_into_current_desk_checks(
        self, case: ConfigCase
    ):
        case.rooms.pop("rooms")
        row = _non_keeper(case)
        row["keeps_desk"] = "yes"
        row["current_desk"] = "D999"
        problems = case.errors()
        find_problem(problems, "rooms.json: (top level)", "missing required key 'rooms'")
        assert_no_problem(problems, "roster.csv", "is not a desk id defined in rooms.json")

    def test_an_unreadable_roster_does_not_cascade_into_predicate_checks(
        self, case: ConfigCase
    ):
        case.set_raw("roster.csv", b"name,email\n\xff\n")
        problems = case.errors()
        find_problem(problems, "roster.csv: byte", "is not valid UTF-8")
        assert_no_problem(problems, "eligibility.json", "is not a column in roster.csv")

    def test_unparseable_json_reports_once_and_stops(self, case: ConfigCase):
        case.set_raw("rooms.json", "{oh no")
        problems = case.errors()
        find_problem(problems, "rooms.json: line 1", "is not valid JSON")
        assert_no_problem(problems, "rooms.json: rooms")
        assert_no_problem(problems, "rooms.json: zones")


def test_missing_config_file_is_a_config_error_not_a_traceback(case: ConfigCase):
    case.delete("scoring.json")
    problem = find_problem(case.errors(), "scoring.json", "is missing from the config directory")
    assert "are required (SPEC §2)" in problem.hint


def test_missing_config_directory_is_reported(tmp_path):
    from deskmatch.config import load_config
    from deskmatch.errors import ConfigError

    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "nope")
    assert "config directory does not exist" in excinfo.value.render()
    assert excinfo.value.exit_code == 4


def test_warnings_survive_a_failed_load(case: ConfigCase):
    """A run that fails still shows the warnings it collected on the way."""
    case.scoring["tie_break_seed"] = "PUBLISH-ME-BEFORE-THE-FORM-OPENS"
    case.scoring["primary_curve"] = "no_such_curve"
    assert any("still the shipped placeholder" in w for w in case.error_warnings())


def test_the_shipped_config_loads(real_config_dir):
    """The config in the repository must itself satisfy SPEC §2."""
    from deskmatch.config import load_config

    config = load_config(real_config_dir)
    assert config.k == len(config.scoring.curve())
    assert config.rooms.desk_ids, "the shipped rooms.json defines desks"


# ==========================================================================
# SPEC §3 — response ingest
# ==========================================================================

BASE_TS = "2026-09-15T14:{:02d}:00-04:00"


def desk_ids(n: int, offset: int = 0) -> list[str]:
    return [f"D{i + 1 + offset:02d}" for i in range(n)]


def people_rows(n_people: int, k: int, *, start_minute: int = 0) -> list[dict[str, str]]:
    """`n_people` single-submission rows, each ranking a distinct block of K desks."""
    return [
        submission_row(
            submission_id=f"s{i:03d}",
            timestamp=BASE_TS.format(start_minute + i),
            email=f"person{i:03d}@umich.edu",
            choices=desk_ids(k, offset=i * k),
        )
        for i in range(n_people)
    ]


K_VALUES = [1, 3, 5, 8]


class TestKDiscovery:
    """SPEC §3.1: "The number of `choice_*` columns is discovered from the header"."""

    @pytest.mark.parametrize("k", K_VALUES)
    def test_k_is_read_from_the_header(self, response_file, k: int):
        path = response_file(people_rows(3, k), k=k)
        loaded = responses_mod.load_responses(str(path), None)
        assert loaded.k == k
        assert all(len(s.choices) == k for s in loaded.submissions)

    @pytest.mark.parametrize("k", K_VALUES)
    def test_header_k_is_cross_checked_against_the_config(self, response_file, k: int):
        path = response_file(people_rows(2, k), k=k)
        wanted = k + 2
        problem = find_problem(
            load_response_problems(path, wanted),
            "header",
            f"declares K={k} (it has choice_1..choice_{k}), but K={wanted} is required",
        )
        assert "scoring.json" in problem.hint and "re-export the form" in problem.hint

    def test_no_choice_columns_at_all(self, response_file):
        rows = [submission_row(
            submission_id="s1", timestamp=BASE_TS.format(0),
            email="a@umich.edu", choices=[],
        )]
        path = response_file(rows, header=response_header(0))
        find_problem(load_response_problems(path, None), "header", "no 'choice_N' columns found")

    def test_choice_columns_must_start_at_one(self, response_file):
        header = (
            "submission_id", "timestamp", "email", "name", "year", "candidacy",
            "choice_2", "choice_3", "client_version", "auth_method",
        )
        rows = [{"submission_id": "s1", "timestamp": BASE_TS.format(0),
                 "email": "a@umich.edu", "name": "A", "year": "3",
                 "candidacy": "candidate", "choice_2": "D01", "choice_3": "D02",
                 "client_version": "v", "auth_method": "google"}]
        path = response_file(rows, header=header)
        find_problem(
            load_response_problems(path, None),
            "header", "choice columns start at 'choice_2'",
        )

    def test_choice_columns_must_be_contiguous(self, response_file):
        header = (
            "submission_id", "timestamp", "email", "name", "year", "candidacy",
            "choice_1", "choice_2", "choice_4", "client_version", "auth_method",
        )
        rows = [{"submission_id": "s1", "timestamp": BASE_TS.format(0),
                 "email": "a@umich.edu", "name": "A", "year": "3",
                 "candidacy": "candidate", "choice_1": "D01", "choice_2": "D02",
                 "choice_4": "D03", "client_version": "v", "auth_method": "google"}]
        path = response_file(rows, header=header)
        problem = find_problem(
            load_response_problems(path, None),
            "header", "'choice_3' is missing but 'choice_4' is present",
        )
        assert "choice_1..choice_2" in problem.hint


class TestResponseHeaderAndRows:
    @pytest.mark.parametrize("column", list(responses_mod.REQUIRED_COLUMNS))
    def test_every_required_column_is_required(self, response_file, column: str):
        k = 3
        header = [c for c in response_header(k) if c != column]
        path = response_file(people_rows(2, k), header=header)
        problem = find_problem(
            load_response_problems(path, k), "header", f"required column '{column}' is missing"
        )
        assert "SPEC §3.1" in problem.hint

    @pytest.mark.parametrize("column", list(responses_mod.AUDIT_COLUMNS))
    def test_audit_columns_are_only_a_warning(self, response_file, column: str):
        """§3.1: `auth_method` is "audit only, never affects the solve"."""
        k = 3
        header = [c for c in response_header(k) if c != column]
        path = response_file(people_rows(2, k), header=header)
        loaded = responses_mod.load_responses(str(path), k)
        find_warning(list(loaded.warnings), f"audit column '{column}' is missing")

    def test_duplicate_column_is_rejected(self, response_file):
        k = 3
        header = list(response_header(k)) + ["email"]
        path = response_file(people_rows(2, k), header=header)
        find_problem(load_response_problems(path, k), "header", "column 'email' appears 2 times")

    def test_choices_must_be_k_distinct_non_empty_desk_ids(self, response_file):
        k = 4
        rows = [
            submission_row(
                submission_id="s1", timestamp=BASE_TS.format(1),
                email="dup@umich.edu", choices=["D01", "D02", "D01", "D04"],
            ),
            submission_row(
                submission_id="s2", timestamp=BASE_TS.format(2),
                email="blank@umich.edu", choices=["D01", "", "D03", "D04"],
            ),
        ]
        path = response_file(rows, k=k)
        problems = load_response_problems(path, k)
        find_problem(problems, "dup@umich.edu", "desk 'D01' is ranked twice, at choice_1 and choice_3")
        find_problem(problems, "blank@umich.edu", "'choice_2' is empty")

    def test_duplicate_submission_id_is_rejected(self, response_file):
        k = 3
        rows = people_rows(2, k)
        rows[1]["submission_id"] = rows[0]["submission_id"]
        path = response_file(rows, k=k)
        find_problem(load_response_problems(path, k), "line", "was already used at line")

    def test_bad_candidacy_and_bad_timestamp_are_reported_per_row(self, response_file):
        """A bad timestamp stops the run; a bad year no longer does.

        The timestamp decides which re-submission wins, so it cannot be guessed.
        `year` is informational since candidacy alone drives eligibility, so it
        degrades to a warning that still names the row and quotes the value.
        """
        k = 3
        rows = people_rows(3, k)
        rows[0]["year"] = "third"
        rows[1]["timestamp"] = "whenever"
        path = response_file(rows, k=k)
        problems = load_response_problems(path, k)
        find_problem(problems, rows[1]["email"], "is not a recognisable timestamp")
        assert not [p for p in problems if "year" in p.what], (
            "an unparseable year must not be a hard error any more"
        )

        # And with only the year broken, the file must load, warning about it.
        ok_rows = people_rows(3, k)
        ok_rows[0]["year"] = "third"
        loaded = responses_mod.load_responses(response_file(ok_rows, k=k), k)
        assert len(loaded.latest) == 3
        blob = " ".join(loaded.warnings)
        assert "'third'" in blob and "year" in blob, loaded.warnings

    def test_every_bad_row_is_reported_in_one_pass(self, response_file):
        """§3: eleven bad rows must produce eleven messages, not eleven runs."""
        k = 3
        rows = people_rows(6, k)
        for row in rows:
            row["choice_2"] = ""
        path = response_file(rows, k=k)
        problems = load_response_problems(path, k)
        assert len([p for p in problems if "'choice_2' is empty" in p.what]) == 6

    def test_row_with_wrong_field_count_is_reported(self, tmp_path):
        k = 3
        header = ",".join(response_header(k))
        path = write_text(tmp_path / "short.csv", header + "\ns1,x,y\n")
        find_problem(load_response_problems(path, k), "line 2", "fields but the header has")

    def test_blank_rows_are_skipped_with_a_warning(self, tmp_path):
        k = 3
        header = ",".join(response_header(k))
        rows = people_rows(2, k)
        body = "\n".join(",".join(row.get(c, "") for c in response_header(k)) for row in rows)
        path = write_text(tmp_path / "blank.csv", f"{header}\n{body}\n" + "," * (len(response_header(k)) - 1) + "\n")
        loaded = responses_mod.load_responses(str(path), k)
        assert len(loaded.submissions) == 2
        find_warning(list(loaded.warnings), "the row is entirely blank and has been skipped")

    def test_unsupported_extension_is_rejected(self, tmp_path):
        path = write_text(tmp_path / "responses.txt", "nothing")
        with pytest.raises(ResponseError) as excinfo:
            responses_mod.load_responses(str(path), 5)
        assert "not a supported response format" in excinfo.value.render()

    def test_missing_file_is_a_response_error_not_an_oserror(self, tmp_path):
        with pytest.raises(ResponseError) as excinfo:
            responses_mod.load_responses(str(tmp_path / "gone.csv"), 5)
        assert "cannot be read" in excinfo.value.render()

    def test_extra_columns_are_preserved(self, response_file):
        k = 3
        rows = people_rows(2, k)
        for index, row in enumerate(rows):
            row["office_phone"] = f"555-000{index}"
        header = response_header(k, extra=("office_phone",))
        path = response_file(rows, header=header)
        loaded = responses_mod.load_responses_ex(str(path), k)
        assert loaded.extra_columns == ("office_phone",)
        assert loaded.extra_values[rows[0]["submission_id"]] == {"office_phone": "555-0000"}
        find_warning(list(loaded.responses.warnings), "office_phone")

    def test_email_is_lowercased_on_ingest(self, response_file):
        k = 3
        rows = people_rows(1, k)
        rows[0]["email"] = "  ADA@UMICH.EDU "
        path = response_file(rows, k=k)
        loaded = responses_mod.load_responses(str(path), k)
        assert loaded.submissions[0].email == "ada@umich.edu"
        assert set(loaded.latest) == {"ada@umich.edu"}

    def test_utf8_bom_is_stripped(self, tmp_path):
        k = 3
        rows = people_rows(1, k)
        header = response_header(k)
        text = dump_csv_text(header, rows)
        path = tmp_path / "bom.csv"
        path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
        loaded = responses_mod.load_responses(str(path), k)
        assert len(loaded.submissions) == 1
        # The hash is over the RAW bytes, BOM included.
        assert loaded.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


class TestLatestPerEmail:
    """SPEC §3.2: latest row per email wins, by timestamp, ties by file position."""

    def _rows(self) -> list[dict[str, str]]:
        # Deliberately out of chronological order in the file.
        return [
            submission_row(submission_id="c", timestamp=BASE_TS.format(30),
                           email="ada@umich.edu", choices=["D05", "D06", "D07"]),
            submission_row(submission_id="a", timestamp=BASE_TS.format(10),
                           email="ada@umich.edu", choices=["D01", "D02", "D03"]),
            submission_row(submission_id="b", timestamp=BASE_TS.format(20),
                           email="ada@umich.edu", choices=["D03", "D04", "D05"]),
            submission_row(submission_id="v", timestamp=BASE_TS.format(15),
                           email="vera@umich.edu", choices=["D11", "D12", "D13"]),
        ]

    def test_latest_timestamp_wins_regardless_of_file_order(self, response_file):
        path = response_file(self._rows(), k=3)
        loaded = responses_mod.load_responses(str(path), 3)
        assert loaded.latest["ada@umich.edu"].submission_id == "c"
        assert loaded.latest["vera@umich.edu"].submission_id == "v"

    def test_superseded_rows_are_retained(self, response_file):
        path = response_file(self._rows(), k=3)
        loaded = responses_mod.load_responses(str(path), 3)
        assert len(loaded.submissions) == 4
        assert sorted(s.submission_id for s in loaded.superseded) == ["a", "b"]

    def test_resubmission_warns_and_names_the_winner(self, response_file):
        path = response_file(self._rows(), k=3)
        loaded = responses_mod.load_responses(str(path), 3)
        warning = find_warning(list(loaded.warnings), "ada@umich.edu", "submitted 3 times")
        assert "retained as superseded" in warning

    def test_timestamp_tie_is_broken_by_later_file_position(self, response_file):
        same = BASE_TS.format(42)
        rows = [
            submission_row(submission_id="first", timestamp=same,
                           email="ada@umich.edu", choices=["D01", "D02", "D03"]),
            submission_row(submission_id="second", timestamp=same,
                           email="ada@umich.edu", choices=["D04", "D05", "D06"]),
        ]
        path = response_file(rows, k=3)
        loaded = responses_mod.load_responses(str(path), 3)
        assert loaded.latest["ada@umich.edu"].submission_id == "second"

        reversed_path = response_file(list(reversed(rows)), k=3)
        reloaded = responses_mod.load_responses(str(reversed_path), 3)
        assert reloaded.latest["ada@umich.edu"].submission_id == "first", (
            "the tie-break must follow file position, not submission_id"
        )

    def test_equal_timestamps_across_offsets_still_order_correctly(self, response_file):
        """Two offsets naming the same instant must not be ordered by their text."""
        rows = [
            submission_row(submission_id="utc", timestamp="2026-09-15T18:00:00+00:00",
                           email="ada@umich.edu", choices=["D01", "D02", "D03"]),
            submission_row(submission_id="edt", timestamp="2026-09-15T15:00:00-04:00",
                           email="ada@umich.edu", choices=["D04", "D05", "D06"]),
        ]
        path = response_file(rows, k=3)
        loaded = responses_mod.load_responses(str(path), 3)
        # 15:00-04:00 is 19:00 UTC, an hour later than 18:00Z.
        assert loaded.latest["ada@umich.edu"].submission_id == "edt"

    def test_latest_is_built_in_sorted_email_order(self, response_file):
        rows = [
            submission_row(submission_id=f"s{i}", timestamp=BASE_TS.format(i),
                           email=email, choices=["D01", "D02", "D03"])
            for i, email in enumerate(["zoe@umich.edu", "ada@umich.edu", "mia@umich.edu"])
        ]
        path = response_file(rows, k=3)
        loaded = responses_mod.load_responses(str(path), 3)
        assert list(loaded.latest) == sorted(loaded.latest)


class TestTimestamps:
    def test_offsetless_timestamps_are_accepted_loudly(self, response_file):
        rows = people_rows(2, 3)
        for row in rows:
            row["timestamp"] = row["timestamp"][:-6]
        path = response_file(rows, k=3)
        loaded = responses_mod.load_responses(str(path), 3)
        warning = find_warning(list(loaded.warnings), "carry no UTC offset")
        assert "UTC" in warning

    def test_mixing_offsets_and_none_warns_separately(self, response_file):
        rows = people_rows(2, 3)
        rows[0]["timestamp"] = rows[0]["timestamp"][:-6]
        path = response_file(rows, k=3)
        loaded = responses_mod.load_responses(str(path), 3)
        find_warning(list(loaded.warnings), "MIXES")

    def test_timestamp_is_stored_exactly_as_submitted(self, response_file):
        rows = people_rows(1, 3)
        rows[0]["timestamp"] = "2026-09-15T14:03:22-04:00"
        path = response_file(rows, k=3)
        loaded = responses_mod.load_responses(str(path), 3)
        assert loaded.submissions[0].timestamp == "2026-09-15T14:03:22-04:00"

    @pytest.mark.parametrize(
        "text", ["2026-09-15T14:03:22Z", "2026-09-15 14:03:22", "09/15/2026 14:03:22"]
    )
    def test_accepted_timestamp_spellings(self, text: str):
        parsed, _naive = responses_mod.parse_timestamp(text)
        assert parsed.year == 2026 and parsed.month == 9 and parsed.day == 15


class TestHashingAndRoundTrip:
    def test_sha256_is_over_the_raw_bytes(self, response_file):
        path = response_file(people_rows(3, 4), k=4)
        loaded = responses_mod.load_responses(str(path), 4)
        assert loaded.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()

    def test_sha256_changes_with_a_single_byte(self, response_file, tmp_path):
        path = response_file(people_rows(3, 4), k=4)
        first = responses_mod.load_responses(str(path), 4).sha256
        edited = tmp_path / "edited.csv"
        edited.write_bytes(path.read_bytes().replace(b"person000", b"person00X"))
        second = responses_mod.load_responses(str(edited), 4).sha256
        assert first != second

    @pytest.mark.parametrize("k", K_VALUES)
    def test_csv_and_json_forms_are_equivalent(self, response_file, k: int):
        rows = people_rows(4, k)
        rows.append(
            submission_row(
                submission_id="resub", timestamp=BASE_TS.format(59),
                email=rows[0]["email"], choices=desk_ids(k, offset=100),
            )
        )
        csv_path = response_file(rows, k=k, fmt="csv")
        json_path = response_file(rows, k=k, fmt="json")

        from_csv = responses_mod.load_responses(str(csv_path), k)
        from_json = responses_mod.load_responses(str(json_path), k)

        assert from_csv.k == from_json.k == k
        assert from_csv.submissions == from_json.submissions
        assert {e: s.submission_id for e, s in from_csv.latest.items()} == {
            e: s.submission_id for e, s in from_json.latest.items()
        }
        # The hash is over the bytes, so the two files must NOT agree on it.
        assert from_csv.sha256 != from_json.sha256

    def test_json_rows_must_share_one_key_set(self, tmp_path):
        k = 3
        rows = [
            {c: r.get(c, "") for c in response_header(k)} for r in people_rows(2, k)
        ]
        rows[1].pop("auth_method")
        path = write_text(tmp_path / "ragged.json", json.dumps(rows))
        find_problem(
            load_response_problems(path, k), "[1]", "has different keys from the first object"
        )

    @pytest.mark.parametrize("k", K_VALUES)
    def test_round_trip_load_write_load(self, response_file, tmp_path, k: int):
        """`load(write(x)) == x` for everything the solve depends on."""
        rows = people_rows(3, k)
        rows.append(
            submission_row(
                submission_id="again", timestamp=BASE_TS.format(58),
                email=rows[1]["email"], choices=desk_ids(k, offset=200),
            )
        )
        path = response_file(rows, k=k)
        original = responses_mod.load_responses(str(path), k)

        out = tmp_path / f"round_trip_{k}.csv"
        responses_mod.write_responses(original, out)
        reloaded = responses_mod.load_responses(str(out), k)

        assert reloaded.submissions == original.submissions
        assert reloaded.latest == original.latest
        assert reloaded.k == original.k

    def test_round_trip_is_byte_stable(self, response_file, tmp_path):
        k = 5
        path = response_file(people_rows(3, k), k=k)
        original = responses_mod.load_responses(str(path), k)
        first = responses_mod.write_responses(original, tmp_path / "a.csv")
        second = responses_mod.write_responses(
            responses_mod.load_responses(str(tmp_path / "a.csv"), k), tmp_path / "b.csv"
        )
        assert first == second
        assert (tmp_path / "a.csv").read_bytes() == (tmp_path / "b.csv").read_bytes()

    def test_round_trip_preserves_extra_columns(self, response_file, tmp_path):
        k = 3
        rows = people_rows(2, k)
        for index, row in enumerate(rows):
            row["cohort_note"] = f"note-{index}"
        path = response_file(rows, header=response_header(k, extra=("cohort_note",)))
        loaded = responses_mod.load_responses_ex(str(path), k)
        out = tmp_path / "extras.csv"
        responses_mod.write_responses(
            loaded.responses, out,
            extras=loaded.extra_values, extra_columns=loaded.extra_columns,
        )
        again = responses_mod.load_responses_ex(str(out), k)
        assert again.extra_values == loaded.extra_values

    def test_canonical_row_order_is_file_order(self, response_file):
        """§3.2 breaks ties by file position, so the writer must not re-sort."""
        rows = [
            submission_row(submission_id="z", timestamp=BASE_TS.format(9),
                           email="zoe@umich.edu", choices=["D01", "D02", "D03"]),
            submission_row(submission_id="a", timestamp=BASE_TS.format(8),
                           email="ada@umich.edu", choices=["D04", "D05", "D06"]),
        ]
        path = response_file(rows, k=3)
        loaded = responses_mod.load_responses(str(path), 3)
        emitted = responses_mod.canonical_rows(loaded)
        assert [row[0] for row in emitted[1:]] == ["z", "a"]


class TestAnonymisation:
    """SPEC §7.2: `sha256(email + seed)[:8]`, and an order that leaks nothing."""

    def _loaded(self, response_file, order: str = "forward") -> Responses:
        k = 4
        rows = [
            submission_row(
                submission_id=f"s{i:02d}",
                timestamp=BASE_TS.format(i),
                email=f"student{i:02d}@umich.edu",
                name=f"Student {i:02d}",
                choices=desk_ids(k, offset=i * k),
            )
            for i in range(7)
        ]
        if order == "reversed":
            rows = list(reversed(rows))
        elif order == "rotated":
            rows = rows[3:] + rows[:3]
        path = response_file(rows, k=k, name=f"anon_{order}.csv")
        return responses_mod.load_responses(str(path), k)

    def test_pseudonym_is_the_spec_expression(self):
        email, seed = "ada@umich.edu", "astro-desks-2026"
        assert responses_mod.pseudonym(email, seed) == hashlib.sha256(
            (email + seed).encode("utf-8")
        ).hexdigest()[:8]

    def test_pseudonym_normalises_the_email(self):
        assert responses_mod.pseudonym("  ADA@UMICH.EDU ", "s") == responses_mod.pseudonym(
            "ada@umich.edu", "s"
        )

    def test_pseudonym_depends_on_the_seed_and_the_salt(self):
        base = responses_mod.pseudonym("ada@umich.edu", "seed-a")
        assert base != responses_mod.pseudonym("ada@umich.edu", "seed-b")
        assert base != responses_mod.pseudonym("ada@umich.edu", "seed-a", salt="pepper")

    def test_pseudonyms_are_stable_across_runs(self, response_file):
        loaded = self._loaded(response_file)
        first = responses_mod.anonymize(loaded, "seed-2026")
        second = responses_mod.anonymize(loaded, "seed-2026")
        assert first == second

    def test_identity_is_removed_from_every_row(self, response_file):
        loaded = self._loaded(response_file)
        text = responses_mod.anonymize(loaded, "seed-2026")
        for submission in loaded.submissions:
            assert submission.email not in text
            assert submission.name not in text
            pseudo = responses_mod.pseudonym(submission.email, "seed-2026")
            assert pseudo in text

    def test_row_order_does_not_leak_the_original_order(self, response_file):
        """The whole point of §7.2: submission order is itself identifying.

        Anyone who watched the live sheet -- or who merely knows they submitted
        first -- could walk a file published in arrival order back to names. So
        the same rows fed in a different order must produce the same file.
        """
        forward = responses_mod.anonymize(self._loaded(response_file, "forward"), "seed-2026")
        backward = responses_mod.anonymize(self._loaded(response_file, "reversed"), "seed-2026")
        rotated = responses_mod.anonymize(self._loaded(response_file, "rotated"), "seed-2026")
        assert forward == backward == rotated

    def test_rows_are_sorted_by_pseudonym(self, response_file):
        loaded = self._loaded(response_file)
        text = responses_mod.anonymize(loaded, "seed-2026")
        emails = [line.split(",")[2] for line in text.strip().splitlines()[1:]]
        assert emails == sorted(emails)
        # ...and that order is NOT the order the rows arrived in, or the test
        # above would be vacuous.
        arrival = [
            responses_mod.pseudonym(s.email, "seed-2026") for s in loaded.submissions
        ]
        assert emails != arrival

    def test_one_persons_rows_keep_their_relative_order(self, response_file):
        """§3.2's file-position tie-break must survive anonymisation."""
        k = 3
        same = BASE_TS.format(7)
        rows = [
            submission_row(submission_id="early", timestamp=same,
                           email="ada@umich.edu", choices=["D01", "D02", "D03"]),
            submission_row(submission_id="late", timestamp=same,
                           email="ada@umich.edu", choices=["D04", "D05", "D06"]),
        ]
        path = response_file(rows, k=k, name="anon_tie.csv")
        loaded = responses_mod.load_responses(str(path), k)
        text = responses_mod.anonymize(loaded, "seed-2026")
        ids = [line.split(",")[0] for line in text.strip().splitlines()[1:]]
        assert ids == ["early", "late"]

        reloaded_path = write_text(path.parent / "anon_tie_rt.csv", text)
        reloaded = responses_mod.load_responses(str(reloaded_path), k)
        assert reloaded.latest[
            responses_mod.pseudonym("ada@umich.edu", "seed-2026")
        ].submission_id == "late"

    def test_the_anonymised_file_reloads(self, response_file, tmp_path):
        loaded = self._loaded(response_file)
        out = tmp_path / "responses_anonymized.csv"
        digest = responses_mod.write_anonymized(out, loaded, "seed-2026")
        assert digest == hashlib.sha256(out.read_bytes()).hexdigest()
        again = responses_mod.load_responses(str(out), loaded.k)
        assert len(again.submissions) == len(loaded.submissions)
        assert set(again.latest) == {
            responses_mod.pseudonym(e, "seed-2026") for e in loaded.latest
        }

    def test_the_shape_of_the_solve_is_preserved(self, response_file):
        """§7.2: the anonymised file reproduces the *structure* of the solve."""
        loaded = self._loaded(response_file)
        out_text = responses_mod.anonymize(loaded, "seed-2026")
        by_pseudo = {
            responses_mod.pseudonym(s.email, "seed-2026"): s.choices
            for s in loaded.submissions
        }
        for line in out_text.strip().splitlines()[1:]:
            cells = line.split(",")
            pseudo = cells[2]
            choices = tuple(cells[6:6 + loaded.k])
            assert choices == by_pseudo[pseudo]
