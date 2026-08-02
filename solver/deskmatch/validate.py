"""Schema validation for everything in `config/` (docs/SPEC.md §2).

This module turns "the coordinator typo'd a zone name" into a sentence they can
act on. That goal drives every design decision here:

  * **Nothing raises.** Every check appends to a `ValidationContext` and carries
    on. A coordinator with five typos gets five messages from one run, not five
    sequential runs. Malformed input (a string where an object belongs) is
    reported and then *skipped* — this module has to survive garbage and
    describe it, never traceback on it.
  * **Every message names the file, the location inside it, the offending value,
    and what was expected** (the shape is fixed by SPEC §2.1). Where the valid
    values are a closed set the hint lists them, and `difflib` names the likely
    intended value, because a near-miss is what actually happens in practice.
  * **Cascades are suppressed.** If `rooms.json` failed to yield any zone ids we
    do not then accuse every eligibility rule of referencing an undefined zone.
    One root cause should produce one message.
  * **Determinism (invariant I3).** Mappings and sets are always sorted before
    iteration, so the sequence of problems is a pure function of the documents.
    No `hash()`, no clock, no filesystem order.

Keys beginning with `_` are documentation (`"_comment"`) and are ignored
everywhere. Keys that are *not* recognised and do *not* begin with `_` are a
warning naming the key — that is the typo net for everything the schema does
not otherwise constrain.
"""

from __future__ import annotations

import difflib
import json
import math
import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .errors import Problem

# --------------------------------------------------------------------------
# 1. Constants (schema facts, never problem dimensions — invariant I1)
# --------------------------------------------------------------------------

ROOMS_FILE = "rooms.json"
ELIGIBILITY_FILE = "eligibility.json"
ROSTER_FILE = "roster.csv"
SCORING_FILE = "scoring.json"

#: The files `load_config` reads, in the order it reports on them.
CONFIG_FILES: tuple[str, ...] = (ROOMS_FILE, ELIGIBILITY_FILE, ROSTER_FILE, SCORING_FILE)

SUPPORTED_SCHEMA_VERSIONS: tuple[int, ...] = (1, 2)

#: Feature `kind` values the renderers know how to draw. An unrecognised kind is
#: a warning, not an error: it still draws (as generic structure), and refusing
#: to run because someone invented "bookshelf" would be obstructive.
KNOWN_FEATURE_KINDS: tuple[str, ...] = (
    "outline", "wall", "door", "window", "partition", "furniture", "room", "divider",
)

#: Hinge corner for a `door` feature, so the plan can show which way it opens.
#: "sw" (bottom-left) is the renderers' default when `swing` is absent.
DOOR_SWINGS: tuple[str, ...] = ("nw", "ne", "sw", "se")

COORD_SPACES: tuple[str, ...] = ("normalized", "pixels")

#: Required roster columns (SPEC §2.3). Any *other* column is legal and is
#: preserved verbatim for use in eligibility predicates.
REQUIRED_ROSTER_COLUMNS: tuple[str, ...] = (
    "name",
    "email",
    "year",
    "candidacy",
    "keeps_desk",
    "current_desk",
)

TRUTHY_STRINGS: tuple[str, ...] = ("1", "true", "y", "yes")
FALSY_STRINGS: tuple[str, ...] = ("0", "false", "n", "no")

#: Floating-point slack when checking that a coordinate sits inside its space.
#: Calibrated coordinates are rounded to four decimals; this is far below that
#: and only absorbs binary-representation error on sums like `x + w`.
COORD_TOLERANCE = 1e-9

#: Desks drawn edge-to-edge in tools/calibrate/ routinely share a boundary to
#: within rounding. Only flag a pair that genuinely covers each other: below
#: this fraction of the smaller desk's area it is a calibration artefact, not a
#: mistake worth interrupting the coordinator for.
MIN_OVERLAP_FRACTION = 0.01

#: Curve values are rationalised exactly and then scaled by the LCM of the
#: denominators (SPEC §5.3). A *non-terminating* decimal typed out to float
#: precision — 1/3 written as 0.3333333333333333 — rationalises to a
#: denominator of 10**16, which makes that scale factor overflow the int64
#: points matrix. Any score a human would actually write is orders of magnitude
#: below this bound.
MAX_CURVE_DENOMINATOR = 10 ** 6

#: Headroom check for the scaled integer points (int64 max is 2**63 - 1).
MAX_SCALED_POINT = 2 ** 62

#: Substrings that mark a `tie_break_seed` as still being the shipped
#: placeholder. Solving with the placeholder is not *wrong*, but it means the
#: seed was never announced, which is the one thing SPEC §8 relies on.
PLACEHOLDER_SEED_MARKERS: tuple[str, ...] = (
    "replace",
    "change-me",
    "changeme",
    "publish-me",
    "before-the-form-opens",
    "your-seed",
    "fixme",
    "todo",
    "tbd",
    "xxx",
)

_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_NUMERIC_STRING_RE = re.compile(r"^\s*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?\s*$")


# --------------------------------------------------------------------------
# 2. The accumulator
# --------------------------------------------------------------------------


@dataclass
class ValidationContext:
    """Accumulates `errors.Problem` entries instead of raising them.

    Errors block the run; warnings do not, but every warning is surfaced to the
    coordinator (they are carried on `Config.warnings` and printed by the CLI).
    """

    problems: list[Problem] = field(default_factory=list)
    warnings: list[Problem] = field(default_factory=list)

    def error(self, where: str, what: str, hint: str = "") -> None:
        self.problems.append(Problem(where=where, what=what, hint=hint))

    def warn(self, where: str, what: str, hint: str = "") -> None:
        self.warnings.append(Problem(where=where, what=what, hint=hint))

    def merge(self, other: ValidationContext) -> None:
        self.problems.extend(other.problems)
        self.warnings.extend(other.warnings)

    @property
    def ok(self) -> bool:
        return not self.problems

    def rendered_warnings(self) -> tuple[str, ...]:
        return tuple(w.render() for w in self.warnings)


# --------------------------------------------------------------------------
# 3. Type / formatting helpers
# --------------------------------------------------------------------------


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _is_list(value: Any) -> bool:
    return isinstance(value, (list, tuple))


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_int(value: Any) -> bool:
    # `bool` is a subclass of `int`; `true` in JSON is not an integer here.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    return _is_number(value) and math.isfinite(float(value))


def _typename(value: Any) -> str:
    """JSON type name, so messages speak the language of the file on disk."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return type(value).__name__


def _a_typename(value: Any) -> str:
    """`_typename` with an article, so messages read as English prose."""
    name = _typename(value)
    if name == "null":
        return "null"
    return f"{'an' if name[0] in 'aeiou' else 'a'} {name}"


def _fmt(value: Any, limit: int = 70) -> str:
    """Compact, JSON-flavoured rendering of an offending value."""
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=repr)
    except (TypeError, ValueError):  # pragma: no cover - default=repr covers it
        text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _visible_keys(mapping: Mapping[str, Any]) -> list[str]:
    """Keys the schema applies to: `_`-prefixed keys are documentation."""
    return sorted((k for k in mapping if not str(k).startswith("_")), key=str)


def closed_set_hint(
    value: Any,
    candidates: Iterable[str],
    noun: str,
    *,
    max_listed: int = 15,
) -> str:
    """Hint for a value that had to come from a closed set.

    Lists the alternatives — the coordinator should never have to grep another
    file to find out what was allowed — and, when the offending value is a
    near-miss for a real one, says so outright. A typo'd identifier is the most
    common way this config goes wrong, so naming the intended value is the
    difference between a useful message and a merely correct one.
    """
    cands = sorted({str(c) for c in candidates})
    if not cands:
        return ""
    parts: list[str] = []
    close = difflib.get_close_matches(str(value), cands, n=1, cutoff=0.6)
    if close:
        parts.append(f"Did you mean '{close[0]}'?")
    if len(cands) <= max_listed:
        parts.append(f"{noun} are: {', '.join(cands)}.")
    else:
        near = difflib.get_close_matches(str(value), cands, n=5, cutoff=0.3)
        shown = near or cands[:5]
        parts.append(f"{noun} include: {', '.join(shown)} ({len(cands)} in total).")
    return " ".join(parts)


def _check_unknown_keys(
    ctx: ValidationContext,
    where: str,
    mapping: Mapping[str, Any],
    known: Sequence[str],
) -> None:
    """Warn about keys deskmatch does not read (a silently ignored typo is the
    worst failure mode: the config looks edited but behaves as if it were not)."""
    for key in _visible_keys(mapping):
        if key not in known:
            ctx.warn(
                where,
                f"has an unknown key '{key}', which deskmatch ignores.",
                closed_set_hint(key, known, "Recognised keys")
                + " Keys starting with '_' (e.g. \"_comment\") are documentation"
                " and are ignored on purpose.",
            )


def _check_schema_version(ctx: ValidationContext, filename: str, doc: Mapping[str, Any]) -> None:
    where = f"{filename}: schema_version"
    if "schema_version" not in doc:
        ctx.error(
            where,
            "missing required key 'schema_version'.",
            f'Add "schema_version": {SUPPORTED_SCHEMA_VERSIONS[-1]} at the top of {filename}.',
        )
        return
    value = doc["schema_version"]
    if not _is_int(value):
        ctx.error(
            where,
            f"expected an integer, got {_a_typename(value)} ({_fmt(value)}).",
            f"Supported schema versions: {', '.join(str(v) for v in SUPPORTED_SCHEMA_VERSIONS)}.",
        )
    elif value not in SUPPORTED_SCHEMA_VERSIONS:
        ctx.error(
            where,
            f"is {value}, which this build of deskmatch cannot interpret.",
            f"Supported schema versions: {', '.join(str(v) for v in SUPPORTED_SCHEMA_VERSIONS)}."
            " Either downgrade the file or upgrade deskmatch.",
        )


def _require(
    ctx: ValidationContext,
    where: str,
    mapping: Mapping[str, Any],
    key: str,
    predicate,
    expected: str,
    *,
    hint: str = "",
) -> Any | None:
    """Fetch `mapping[key]`, reporting a missing or wrong-typed value.

    Returns the value only when it is present and passes `predicate`, so every
    caller can `if value is None: continue` and never touch a malformed field.
    """
    if key not in mapping:
        ctx.error(where, f"missing required key '{key}' ({expected}).", hint)
        return None
    value = mapping[key]
    if not predicate(value):
        ctx.error(
            where,
            f"'{key}' is {_a_typename(value)} ({_fmt(value)}); expected {expected}.",
            hint,
        )
        return None
    return value


# --------------------------------------------------------------------------
# 4. Shared parsers — used by both the validator and the loader so the two can
#    never disagree about what a field means.
# --------------------------------------------------------------------------


def normalize_email(raw: str) -> str:
    """The primary key for a human: trimmed and lower-cased (SPEC §2.3)."""
    return raw.strip().lower()


def parse_year(raw: Any) -> int | None:
    """Roster `year`: an integer >= 1. Returns None if it is not one."""
    if _is_int(raw):
        return int(raw) if int(raw) >= 1 else None
    if not _is_str(raw):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value >= 1 else None


def parse_keeps_desk(raw: Any) -> bool | None:
    """Roster `keeps_desk` against the documented vocabulary (SPEC §2.3).

    Returns None for anything outside it — including the empty string, which the
    caller treats as falsy *with a warning* rather than silently guessing.
    """
    if isinstance(raw, bool):
        return raw
    if not _is_str(raw):
        return None
    text = raw.strip().lower()
    if text in TRUTHY_STRINGS:
        return True
    if text in FALSY_STRINGS:
        return False
    return None


def curve_value_to_fraction(value: int | float) -> Fraction:
    """Exact rationalisation of a curve entry — via `str`, never `float`.

    `Fraction(0.1)` is 3602879701896397/36028797018963968: the binary double, not
    the number the coordinator wrote. `Fraction(str(0.1))` is 1/10. SPEC §5.3
    scales points by the LCM of these denominators, so the first form turns a
    harmless decimal into an astronomically large scale factor (and destroys the
    "distinct totals differ by exactly 1" property the jitter bound rests on).
    """
    return Fraction(str(value))


# --------------------------------------------------------------------------
# 5. Tolerant extractors — let a later validator run even when an earlier
#    document is broken, without inventing values.
# --------------------------------------------------------------------------


def zone_ids_of(rooms_doc: Any) -> tuple[str, ...]:
    """Zone ids declared in a rooms document; empty if it is unusable."""
    if not _is_mapping(rooms_doc) or not _is_mapping(rooms_doc.get("zones")):
        return ()
    return tuple(sorted(str(z) for z in rooms_doc["zones"] if not str(z).startswith("_")))


def desk_ids_of(rooms_doc: Any) -> tuple[str, ...]:
    """Desk ids declared across all rooms; empty if the document is unusable."""
    if not _is_mapping(rooms_doc) or not _is_list(rooms_doc.get("rooms")):
        return ()
    ids: list[str] = []
    for room in rooms_doc["rooms"]:
        if not _is_mapping(room) or not _is_list(room.get("desks")):
            continue
        for desk in room["desks"]:
            if _is_mapping(desk) and _is_str(desk.get("id")) and desk["id"].strip():
                ids.append(desk["id"])
    return tuple(sorted(set(ids)))


def predicate_attributes(eligibility_doc: Any) -> tuple[str, ...]:
    """Roster attribute names referenced by any `when` predicate."""
    attrs: set[str] = set()
    for rule in _rules_of(eligibility_doc):
        when = rule.get("when")
        if _is_mapping(when):
            attrs.update(str(k) for k in when if not str(k).startswith("_"))
    return tuple(sorted(attrs))


def predicate_values(eligibility_doc: Any) -> dict[str, frozenset[str]]:
    """attribute -> the scalar values eligibility rules actually compare against.

    Used for the SPEC §2.3 warning about roster values that no rule mentions.
    Values are normalised the way rule evaluation normalises them (trimmed,
    lower-cased) so the comparison is apples to apples.
    """
    found: dict[str, set[str]] = {}
    for rule in _rules_of(eligibility_doc):
        when = rule.get("when")
        if not _is_mapping(when):
            continue
        for attr in sorted((k for k in when if not str(k).startswith("_")), key=str):
            bucket = found.setdefault(str(attr), set())
            bucket.update(_scalars_in_matcher(when[attr]))
    return {attr: frozenset(vals) for attr, vals in sorted(found.items())}


def _rules_of(eligibility_doc: Any) -> list[Mapping[str, Any]]:
    if not _is_mapping(eligibility_doc) or not _is_list(eligibility_doc.get("rules")):
        return []
    return [r for r in eligibility_doc["rules"] if _is_mapping(r)]


def _scalars_in_matcher(matcher: Any) -> set[str]:
    """Every literal a matcher compares equality against, normalised."""
    if _is_mapping(matcher):
        if "not" in matcher:
            return _scalars_in_matcher(matcher["not"])
        return set()  # a {min,max} range names no literal
    if _is_list(matcher):
        out: set[str] = set()
        for item in matcher:
            out |= _scalars_in_matcher(item)
        return out
    if matcher is None:
        return set()
    return {str(matcher).strip().lower()}


# --------------------------------------------------------------------------
# 6. Geometry — desk-shape overlap (SPEC §2.1, warning)
# --------------------------------------------------------------------------

Point = tuple[float, float]


def _rect_to_polygon(rect: Sequence[float]) -> list[Point]:
    x, y, w, h = (float(v) for v in rect[:4])
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]


def _as_polygon(kind: str, shape: Sequence[Any]) -> list[Point] | None:
    """Normalise either shape kind to a vertex list, or None if malformed."""
    try:
        if kind == "rect":
            if len(shape) != 4 or not all(_is_finite_number(v) for v in shape):
                return None
            return _rect_to_polygon(shape)  # type: ignore[arg-type]
        pts: list[Point] = []
        for point in shape:
            if not _is_list(point) or len(point) != 2:
                return None
            if not all(_is_finite_number(c) for c in point):
                return None
            pts.append((float(point[0]), float(point[1])))
        return pts if len(pts) >= 3 else None
    except TypeError:
        return None


def _bbox(pts: Sequence[Point]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _polygon_area(pts: Sequence[Point]) -> float:
    """Shoelace, absolute — orientation is irrelevant here."""
    total = 0.0
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_properly_cross(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    """True only for a *proper* crossing — shared endpoints and T-junctions do
    not count, so desks that merely abut are not reported as overlapping."""
    d1, d2 = _orient(p3, p4, p1), _orient(p3, p4, p2)
    d3, d4 = _orient(p1, p2, p3), _orient(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _point_strictly_inside(pt: Point, poly: Sequence[Point]) -> bool:
    """Ray casting. Points exactly on the boundary are unreliable here and are
    deliberately not treated as inside — abutting desks must stay unreported."""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            t = (y - y1) / (y2 - y1)
            if x < x1 + t * (x2 - x1):
                inside = not inside
    return inside


def shapes_overlap(
    kind_a: str,
    shape_a: Sequence[Any],
    kind_b: str,
    shape_b: Sequence[Any],
) -> tuple[bool, float | None]:
    """Do two desk shapes cover each other? Returns (overlaps, area_fraction).

    Rect/rect is exact: axis-aligned rectangles have a closed-form intersection
    area, so the message can quote how much of the smaller desk is covered.

    Anything involving a polygon uses the general test for two *simple*
    polygons: their interiors intersect iff some edge pair properly crosses, or
    one polygon's vertices lie inside the other (the containment case). That is
    complete for simple polygons — unlike a separating-axis test, it does not
    over-report on concave shapes — but it yields no area, so `None` is returned
    for the fraction and the message omits the percentage.
    """
    pa = _as_polygon(kind_a, shape_a)
    pb = _as_polygon(kind_b, shape_b)
    if pa is None or pb is None:
        return (False, None)  # malformed geometry is reported by the schema checks

    ax1, ay1, ax2, ay2 = _bbox(pa)
    bx1, by1, bx2, by2 = _bbox(pb)
    ox = min(ax2, bx2) - max(ax1, bx1)
    oy = min(ay2, by2) - max(ay1, by1)
    if ox <= COORD_TOLERANCE or oy <= COORD_TOLERANCE:
        return (False, None)  # bounding boxes miss => shapes miss

    if kind_a == "rect" and kind_b == "rect":
        smaller = min(_polygon_area(pa), _polygon_area(pb))
        fraction = (ox * oy) / smaller if smaller > 0 else 0.0
        return (fraction >= MIN_OVERLAP_FRACTION, fraction)

    for i in range(len(pa)):
        p1, p2 = pa[i], pa[(i + 1) % len(pa)]
        for j in range(len(pb)):
            if _segments_properly_cross(p1, p2, pb[j], pb[(j + 1) % len(pb)]):
                return (True, None)
    if any(_point_strictly_inside(p, pb) for p in pa):
        return (True, None)
    if any(_point_strictly_inside(p, pa) for p in pb):
        return (True, None)
    return (False, None)


# --------------------------------------------------------------------------
# 7. rooms.json (SPEC §2.1)
# --------------------------------------------------------------------------


def validate_rooms(doc: Any, config_dir: str | Path | None = None) -> ValidationContext:
    """Validate a parsed `rooms.json`.

    `config_dir` is optional only so the pure-data checks can be exercised
    without a filesystem; when it is None the `image` existence check (a
    warning) is skipped rather than guessed at.
    """
    ctx = ValidationContext()
    top = f"{ROOMS_FILE}: (top level)"
    if not _is_mapping(doc):
        ctx.error(
            top,
            f"expected a JSON object, got {_a_typename(doc)}.",
            "See docs/SPEC.md §2.1 for the required shape.",
        )
        return ctx

    _check_schema_version(ctx, ROOMS_FILE, doc)
    _check_unknown_keys(ctx, top, doc, ("schema_version", "coord_space", "zones", "rooms"))

    coord_space = _validate_coord_space(ctx, doc)
    zone_ids = _validate_zones(ctx, doc)

    rooms = _require(
        ctx, top, doc, "rooms", _is_list, "an array of room objects",
        hint="At least one room must be defined; every desk lives inside a room.",
    )
    if rooms is None:
        return ctx
    if not rooms:
        ctx.error(
            f"{ROOMS_FILE}: rooms",
            "is an empty array; no desks are defined anywhere.",
            "Add at least one room with at least one desk. See docs/SPEC.md §2.1.",
        )
        return ctx

    seen_desks: dict[str, str] = {}   # desk id -> where it was first defined
    seen_rooms: dict[str, str] = {}
    zones_used: set[str] = set()

    for r_index, room in enumerate(rooms):
        room_where = f"{ROOMS_FILE}: rooms[{r_index}]"
        if not _is_mapping(room):
            ctx.error(room_where, f"expected an object, got {_a_typename(room)}.")
            continue

        room_id = room.get("id")
        if _is_str(room_id) and room_id.strip():
            room_where = f'{ROOMS_FILE}: rooms[{r_index}] ("{room_id}")'
            if room_id in seen_rooms:
                ctx.error(
                    room_where,
                    f"duplicate room id '{room_id}'; it was already defined at "
                    f"{seen_rooms[room_id]}.",
                    "Room ids must be unique.",
                )
            else:
                seen_rooms[room_id] = f"rooms[{r_index}]"
        else:
            ctx.error(
                room_where,
                "missing required key 'id' (a non-empty string, unique across rooms)."
                if room_id is None
                else f"'id' is {_a_typename(room_id)} ({_fmt(room_id)}); expected a non-empty string.",
            )

        _check_unknown_keys(
            ctx, room_where, room,
            ("id", "label", "image", "image_size", "desks", "features"),
        )
        if "label" not in room:
            ctx.warn(
                room_where,
                "has no 'label'; reports and the floor-plan legend will show the raw room id.",
            )
        elif not _is_str(room["label"]):
            ctx.error(
                room_where,
                f"'label' is {_a_typename(room['label'])} ({_fmt(room['label'])}); expected a string.",
            )

        image_size = _validate_image_size(ctx, room_where, room)
        _validate_image_path(ctx, room_where, room, config_dir)
        _validate_features(ctx, room_where, room, coord_space, image_size, seen_desks)

        desks = _require(ctx, room_where, room, "desks", _is_list, "an array of desk objects")
        if desks is None:
            continue
        if not desks:
            ctx.error(
                f"{room_where}.desks",
                "is an empty array; a room with no desks cannot appear in the solve.",
                "Remove the room, or add its desks with tools/calibrate/.",
            )
            continue

        placed: list[tuple[str, int, str, Sequence[Any]]] = []  # for overlap checks
        for d_index, desk in enumerate(desks):
            desk_where = f"{ROOMS_FILE}: rooms[{r_index}].desks[{d_index}]"
            if not _is_mapping(desk):
                ctx.error(desk_where, f"expected an object, got {_a_typename(desk)}.")
                continue

            desk_id = desk.get("id")
            if _is_str(desk_id) and desk_id.strip():
                desk_where = f'{desk_where} ("{desk_id}")'
                if desk_id in seen_desks:
                    ctx.error(
                        desk_where,
                        f"duplicate desk id '{desk_id}'; it was already defined at "
                        f"{seen_desks[desk_id]}.",
                        "Desk ids must be unique across ALL rooms — they are the"
                        " identifiers students rank on the form.",
                    )
                else:
                    seen_desks[desk_id] = f"rooms[{r_index}].desks[{d_index}]"
            else:
                ctx.error(
                    desk_where,
                    "missing required key 'id' (a non-empty string, unique across all rooms)."
                    if desk_id is None
                    else f"'id' is {_a_typename(desk_id)} ({_fmt(desk_id)}); expected a non-empty string.",
                )

            _check_unknown_keys(
                ctx, desk_where, desk, ("id", "label", "zone", "shape", "notes", "available")
            )

            if "label" not in desk:
                ctx.warn(
                    desk_where,
                    "has no 'label'; the form and the reports will show the raw desk id.",
                    "The label is what humans call the desk, e.g. \"14\".",
                )
            elif not _is_str(desk["label"]):
                ctx.error(
                    desk_where,
                    f"'label' is {_a_typename(desk['label'])} ({_fmt(desk['label'])});"
                    " expected a string.",
                )

            if "notes" in desk and not _is_str(desk["notes"]):
                ctx.error(
                    desk_where,
                    f"'notes' is {_a_typename(desk['notes'])} ({_fmt(desk['notes'])});"
                    " expected a string.",
                )
            if "available" in desk and not isinstance(desk["available"], bool):
                ctx.error(
                    desk_where,
                    f"'available' is {_a_typename(desk['available'])} ({_fmt(desk['available'])});"
                    " expected true or false.",
                    'Set "available": false to hold a desk out of the pool (SPEC §3.4).',
                )

            zone = desk.get("zone")
            if zone is None:
                ctx.error(
                    desk_where,
                    "missing required key 'zone'.",
                    closed_set_hint("", zone_ids, "Defined zones"),
                )
            elif not _is_str(zone):
                ctx.error(
                    desk_where,
                    f"'zone' is {_a_typename(zone)} ({_fmt(zone)}); expected a zone id string.",
                    closed_set_hint(zone, zone_ids, "Defined zones"),
                )
            elif zone_ids and zone not in zone_ids:
                # The message shape here is fixed by SPEC §2.1.
                ctx.error(
                    desk_where,
                    f"references zone '{zone}', which is not defined in {ROOMS_FILE}:zones.",
                    closed_set_hint(zone, zone_ids, "Defined zones"),
                )
            else:
                zones_used.add(zone)

            kind, shape = _validate_shape(ctx, desk_where, desk, coord_space, image_size)
            if kind is not None and shape is not None and _is_str(desk_id):
                placed.append((desk_id, d_index, kind, shape))

        _report_overlaps(ctx, r_index, placed)

    for zone in zone_ids:
        if zone not in zones_used:
            ctx.warn(
                f"{ROOMS_FILE}: zones.{zone}",
                "is defined but no desk is in it, so it can never be assigned.",
                "An empty zone is legal but is almost always a typo in a desk's"
                " 'zone' field, or a zone left behind after re-drawing the plan.",
            )

    return ctx


def _validate_coord_space(ctx: ValidationContext, doc: Mapping[str, Any]) -> str | None:
    where = f"{ROOMS_FILE}: coord_space"
    if "coord_space" not in doc:
        ctx.error(
            where,
            "missing required key 'coord_space'.",
            f"Must be one of: {', '.join(COORD_SPACES)}. 'normalized' means every"
            " coordinate is a 0-1 fraction of image_size (recommended: it survives"
            " rescaling the floor-plan image).",
        )
        return None
    value = doc["coord_space"]
    if not _is_str(value) or value not in COORD_SPACES:
        ctx.error(
            where,
            f"is {_fmt(value)}, which is not a supported coordinate space.",
            closed_set_hint(value, COORD_SPACES, "Supported coordinate spaces"),
        )
        return None
    return value


def _validate_zones(ctx: ValidationContext, doc: Mapping[str, Any]) -> tuple[str, ...]:
    top = f"{ROOMS_FILE}: (top level)"
    zones = _require(
        ctx, top, doc, "zones", _is_mapping,
        "an object mapping zone id -> {label, color}",
        hint="Zone ids are arbitrary strings; eligibility.json refers to them by name.",
    )
    if zones is None:
        return ()
    ids = tuple(_visible_keys(zones))
    if not ids:
        ctx.error(
            f"{ROOMS_FILE}: zones",
            "defines no zones; every desk must be in a zone and every eligibility"
            " rule allows zones by name.",
            'Define at least one, e.g. {"all": {"label": "Everywhere"}}.',
        )
        return ()

    for zone_id in ids:
        where = f"{ROOMS_FILE}: zones.{zone_id}"
        if not zone_id.strip():
            ctx.error(f"{ROOMS_FILE}: zones", "has a zone whose id is blank.")
        meta = zones[zone_id]
        if not _is_mapping(meta):
            ctx.error(where, f"expected an object, got {_a_typename(meta)} ({_fmt(meta)}).")
            continue
        _check_unknown_keys(ctx, where, meta, ("label", "color", "description"))
        if "label" not in meta:
            ctx.warn(where, "has no 'label'; the map legend will show the raw zone id.")
        elif not _is_str(meta["label"]):
            ctx.error(
                where,
                f"'label' is {_a_typename(meta['label'])} ({_fmt(meta['label'])});"
                " expected a string.",
            )
        if "color" in meta:
            color = meta["color"]
            if not _is_str(color) or not _HEX_COLOR_RE.match(color):
                ctx.error(
                    where,
                    f"'color' is {_fmt(color)}, which is not a hex colour.",
                    "Use '#rgb', '#rrggbb' or '#rrggbbaa', e.g. '#3d6fa8'."
                    " The report renderer cannot interpret anything else.",
                )
        if "description" in meta and not _is_str(meta["description"]):
            ctx.error(
                where,
                f"'description' is {_a_typename(meta['description'])}; expected a string.",
            )
    return ids


def _validate_image_size(
    ctx: ValidationContext, room_where: str, room: Mapping[str, Any]
) -> tuple[int, int] | None:
    where = f"{room_where}.image_size"
    if "image_size" not in room:
        ctx.error(
            where,
            "missing required key 'image_size'.",
            'Give the floor-plan image size in pixels, e.g. "image_size": [1212, 706].'
            " It is required even in normalized coordinate space, because the"
            " renderer needs the aspect ratio.",
        )
        return None
    value = room["image_size"]
    if not _is_list(value) or len(value) != 2:
        ctx.error(
            where,
            f"is {_fmt(value)}; expected an array of exactly two numbers, [width_px, height_px].",
        )
        return None
    width, height = value
    bad = False
    for name, component in (("width", width), ("height", height)):
        if not _is_int(component):
            ctx.error(
                where,
                f"{name} is {_a_typename(component)} ({_fmt(component)});"
                " expected a positive whole number of pixels.",
            )
            bad = True
        elif component <= 0:
            ctx.error(where, f"{name} is {component}; expected a positive number of pixels.")
            bad = True
    return None if bad else (int(width), int(height))


def _validate_image_path(
    ctx: ValidationContext,
    room_where: str,
    room: Mapping[str, Any],
    config_dir: str | Path | None,
) -> None:
    where = f"{room_where}.image"
    if "image" not in room:
        # Silence, deliberately. A room with no `image` is drawing itself from
        # the desk rectangles on purpose -- that is the shipped configuration --
        # and a warning on every single run for an intended state is a warning
        # nobody reads, which then hides the one that matters. A *configured*
        # image that is missing is still reported below, because that one is an
        # accident.
        return
    image = room["image"]
    if not _is_str(image) or not image.strip():
        ctx.error(where, f"is {_fmt(image)}; expected a path string relative to the config directory.")
        return
    if Path(image).is_absolute():
        ctx.warn(
            where,
            f"'{image}' is an absolute path, so this config will not work on anyone"
            " else's machine.",
            "Use a path relative to the config directory, e.g. 'floorplans/main_office.png'.",
        )
    if config_dir is None:
        return  # caller asked for the pure-data checks only
    resolved = Path(config_dir) / image
    if not resolved.is_file():
        ctx.warn(
            where,
            f"floor-plan image '{image}' does not exist (looked for {resolved}).",
            "The solver falls back to a blank canvas, but the web form cannot:"
            " students would be asked to rank desks with no plan to look at.",
        )


def _validate_features(
    ctx: ValidationContext,
    room_where: str,
    room: Mapping[str, Any],
    coord_space: str | None,
    image_size: tuple[int, int] | None,
    seen_desks: Mapping[str, str],
) -> None:
    """Validate a room's decorative `features` (schema v2+).

    Features are walls, doors, rooms, windows, partitions and furniture. They are
    drawn grey and are never selectable, so the rules are looser than for desks:
    a feature may overlap anything, may sit outside the room outline, and needs
    no zone. What it must not do is collide with the desk namespace, because a
    feature id that matches a desk id makes the map ambiguous about what a click
    means.
    """
    if "features" not in room:
        return
    features = room["features"]
    if not _is_list(features):
        ctx.error(
            f"{room_where}.features",
            f"is {_a_typename(features)} ({_fmt(features)}); expected an array of "
            f"feature objects.",
            'Features are optional decoration: [{"id": ..., "kind": "wall", '
            '"label": ..., "shape": {...}}]. See docs/SPEC.md §2.1.',
        )
        return

    seen: dict[str, int] = {}
    for f_index, feature in enumerate(features):
        where = f"{room_where}.features[{f_index}]"
        if not _is_mapping(feature):
            ctx.error(where, f"expected an object, got {_a_typename(feature)}.")
            continue

        fid = feature.get("id")
        if _is_str(fid) and fid.strip():
            where = f'{where} ("{fid}")'
            if fid in seen:
                ctx.error(
                    where,
                    f"duplicate feature id '{fid}'; already used at "
                    f"features[{seen[fid]}] in this room.",
                )
            else:
                seen[fid] = f_index
            if fid in seen_desks:
                ctx.error(
                    where,
                    f"feature id '{fid}' is also a desk id (defined at "
                    f"{seen_desks[fid]}).",
                    "Desks are selectable and features are not, so the two "
                    "namespaces must not overlap.",
                )
        else:
            ctx.error(
                where,
                "missing required key 'id' (a non-empty string, unique within the room)."
                if fid is None
                else f"'id' is {_a_typename(fid)} ({_fmt(fid)}); expected a non-empty string.",
            )

        _check_unknown_keys(
            ctx, where, feature, ("id", "kind", "label", "shape", "note", "swing")
        )

        # `swing` names the hinge corner of a door, so the drawing can show which
        # way it actually opens. Only meaningful on doors.
        if "swing" in feature:
            swing = feature["swing"]
            if not _is_str(swing) or swing.strip().lower() not in DOOR_SWINGS:
                ctx.error(
                    where,
                    f"'swing' is {_fmt(swing)}; expected one of "
                    f"{', '.join(repr(v) for v in DOOR_SWINGS)}.",
                    "It names the corner the door is hinged on: 'sw' is the "
                    "default (bottom-left). Reversing a door usually means "
                    "swapping sw<->se or nw<->ne.",
                )
            elif str(feature.get("kind", "")).strip().lower() != "door":
                ctx.warn(
                    where,
                    f"'swing' is set but 'kind' is "
                    f"{_fmt(feature.get('kind'))}, not 'door', so it is ignored.",
                )

        kind = feature.get("kind")
        if kind is None:
            ctx.error(
                where,
                "missing required key 'kind'.",
                f"One of: {', '.join(KNOWN_FEATURE_KINDS)}.",
            )
        elif not _is_str(kind):
            ctx.error(where, f"'kind' is {_a_typename(kind)} ({_fmt(kind)}); expected a string.")
        elif kind not in KNOWN_FEATURE_KINDS:
            ctx.warn(
                where,
                f"'kind' is '{kind}', which the renderers do not have a specific "
                f"style for; it will be drawn as generic structure.",
                closed_set_hint(kind, KNOWN_FEATURE_KINDS, "kind"),
            )

        if "label" in feature and not _is_str(feature["label"]):
            ctx.error(
                where,
                f"'label' is {_a_typename(feature['label'])} ({_fmt(feature['label'])}); "
                f"expected a string.",
            )
        if "note" in feature and not _is_str(feature["note"]):
            ctx.error(
                where,
                f"'note' is {_a_typename(feature['note'])} ({_fmt(feature['note'])}); "
                f"expected a string.",
            )

        _validate_shape(
            ctx, where, feature, coord_space, image_size, allow_polyline=True
        )


def _validate_shape(
    ctx: ValidationContext,
    desk_where: str,
    desk: Mapping[str, Any],
    coord_space: str | None,
    image_size: tuple[int, int] | None,
    allow_polyline: bool = False,
) -> tuple[str | None, Sequence[Any] | None]:
    """Validate a `shape` block.

    `allow_polyline` is set only for features. A polyline has no interior, so a
    desk drawn as one would be unclickable -- which is exactly the kind of
    failure that is invisible until a student cannot select a desk. Walls, on
    the other hand, are naturally lines.
    """
    allowed = ("rect", "polygon", "polyline") if allow_polyline else ("rect", "polygon")
    options = 'Exactly one of {"rect": [x, y, w, h]} or {"polygon": [[x, y], ...]}'
    if allow_polyline:
        options += ' or {"polyline": [[x, y], ...]}'

    where = f"{desk_where}.shape"
    if "shape" not in desk:
        ctx.error(
            where,
            "missing required key 'shape'.",
            options + ". tools/calibrate/ produces these by clicking on the floor plan.",
        )
        return (None, None)
    shape = desk["shape"]
    if not _is_mapping(shape):
        ctx.error(where, f"is {_a_typename(shape)} ({_fmt(shape)}); expected an object.")
        return (None, None)

    kinds = [k for k in allowed if k in shape]
    if not allow_polyline and "polyline" in shape:
        ctx.error(
            where,
            "uses 'polyline', which is only valid for features, not desks.",
            "A polyline has no interior, so the desk would be impossible to "
            "click. Use 'rect' or 'polygon'.",
        )
        return (None, None)
    _check_unknown_keys(ctx, where, shape, allowed)
    if not kinds:
        # Phrase the 2-option case as "neither X nor Y", which reads better than
        # a degenerate list, and fall back to a list once there are three.
        missing = (
            f"neither {allowed[0]!r} nor {allowed[1]!r}"
            if len(allowed) == 2
            else "none of " + ", ".join(repr(k) for k in allowed[:-1]) + f" or {allowed[-1]!r}"
        )
        ctx.error(where, f"defines {missing}.", options + ".")
        return (None, None)
    if len(kinds) > 1:
        found = (
            f"both {kinds[0]!r} and {kinds[1]!r}"
            if len(kinds) == 2
            else ", ".join(repr(k) for k in kinds)
        )
        ctx.error(
            where,
            f"defines {found}; there is exactly one shape.",
            "Delete whichever one is stale.",
        )
        return (None, None)

    kind = kinds[0]
    value = shape[kind]
    if kind == "rect":
        return ("rect", _validate_rect(ctx, where, value, coord_space, image_size))
    if kind == "polyline":
        return ("polyline", _validate_polygon(
            ctx, where, value, coord_space, image_size, min_points=2, noun="polyline"
        ))
    return ("polygon", _validate_polygon(ctx, where, value, coord_space, image_size))


def _bounds_for(
    coord_space: str | None, image_size: tuple[int, int] | None
) -> tuple[float, float, str] | None:
    """(max_x, max_y, human description) for the active coordinate space."""
    if coord_space == "normalized":
        return (1.0, 1.0, 'coord_space "normalized" means every coordinate is a'
                          " fraction of image_size, so it must lie in [0, 1]")
    if coord_space == "pixels" and image_size is not None:
        w, h = image_size
        return (float(w), float(h), f'coord_space "pixels" means coordinates are'
                                    f" measured on the {w}x{h} floor-plan image")
    return None  # unknown space, or pixel space with an unusable image_size


def _check_coord(
    ctx: ValidationContext,
    where: str,
    name: str,
    value: float,
    axis: int,
    bounds: tuple[float, float, str] | None,
) -> None:
    if bounds is None:
        return
    limit = bounds[axis]
    if value < -COORD_TOLERANCE or value > limit + COORD_TOLERANCE:
        ctx.error(
            where,
            f"{name} = {value:g} lies outside the allowed range [0, {limit:g}].",
            f"{bounds[2]}.",
        )


def _validate_rect(
    ctx: ValidationContext,
    shape_where: str,
    value: Any,
    coord_space: str | None,
    image_size: tuple[int, int] | None,
) -> Sequence[Any] | None:
    where = f"{shape_where}.rect"
    if not _is_list(value) or len(value) != 4:
        ctx.error(
            where,
            f"is {_fmt(value)}; expected exactly four numbers, [x, y, w, h].",
            "x, y is the top-left corner; w, h are width and height in the same"
            " coordinate space.",
        )
        return None
    names = ("x", "y", "w", "h")
    for name, component in zip(names, value):
        if not _is_finite_number(component):
            ctx.error(
                where,
                f"{name} is {_a_typename(component)} ({_fmt(component)});"
                " expected a finite number.",
            )
            return None
    x, y, w, h = (float(v) for v in value)
    ok = True
    for name, extent in (("w", w), ("h", h)):
        if extent <= 0:
            ctx.error(
                where,
                f"{name} = {extent:g}; a desk must have positive width and height.",
                "If the rectangle was drawn from the bottom-right, swap the corners:"
                " [x, y] is the top-left and w, h are positive extents.",
            )
            ok = False
    bounds = _bounds_for(coord_space, image_size)
    _check_coord(ctx, where, "x", x, 0, bounds)
    _check_coord(ctx, where, "y", y, 1, bounds)
    if ok:
        _check_coord(ctx, where, "x + w", x + w, 0, bounds)
        _check_coord(ctx, where, "y + h", y + h, 1, bounds)
    return value if ok else None


def _validate_polygon(
    ctx: ValidationContext,
    shape_where: str,
    value: Any,
    coord_space: str | None,
    image_size: tuple[int, int] | None,
    min_points: int = 3,
    noun: str = "polygon",
) -> Sequence[Any] | None:
    where = f"{shape_where}.{noun}"
    if not _is_list(value):
        ctx.error(where, f"is {_a_typename(value)} ({_fmt(value)}); expected an array of [x, y] points.")
        return None
    if len(value) < min_points:
        ctx.error(
            where,
            f"has {len(value)} point(s); a {noun} needs at least {min_points}.",
            "Use a 'rect' instead if the shape is a plain rectangle."
            if noun == "polygon"
            else "A polyline is a run of connected line segments, e.g. a wall.",
        )
        return None
    bounds = _bounds_for(coord_space, image_size)
    ok = True
    for p_index, point in enumerate(value):
        point_where = f"{where}[{p_index}]"
        if not _is_list(point) or len(point) != 2:
            ctx.error(point_where, f"is {_fmt(point)}; expected a two-element [x, y] pair.")
            ok = False
            continue
        if not all(_is_finite_number(c) for c in point):
            ctx.error(point_where, f"is {_fmt(point)}; both coordinates must be finite numbers.")
            ok = False
            continue
        _check_coord(ctx, point_where, "x", float(point[0]), 0, bounds)
        _check_coord(ctx, point_where, "y", float(point[1]), 1, bounds)
    return value if ok else None


def _report_overlaps(
    ctx: ValidationContext,
    room_index: int,
    placed: Sequence[tuple[str, int, str, Sequence[Any]]],
) -> None:
    """Pairwise overlap warnings, within a room only (desks in different rooms
    are drawn on different images, so their coordinates are unrelated)."""
    for i in range(len(placed)):
        id_a, index_a, kind_a, shape_a = placed[i]
        for j in range(i + 1, len(placed)):
            id_b, index_b, kind_b, shape_b = placed[j]
            hit, fraction = shapes_overlap(kind_a, shape_a, kind_b, shape_b)
            if not hit:
                continue
            how_much = (
                f" by {fraction * 100:.0f}% of the smaller desk's area"
                if fraction is not None
                else ""
            )
            ctx.warn(
                f'{ROOMS_FILE}: rooms[{room_index}].desks[{index_a}] ("{id_a}")',
                f"overlaps desk '{id_b}' (desks[{index_b}]){how_much}.",
                "Two desks drawn on top of each other usually means a coordinate was"
                " pasted twice in tools/calibrate/. Both desks stay in the pool, but"
                " the floor-plan map will be misleading.",
            )


# --------------------------------------------------------------------------
# 8. eligibility.json (SPEC §2.2)
# --------------------------------------------------------------------------


def validate_eligibility(
    doc: Any,
    zone_ids: Sequence[str],
    roster_columns: Sequence[str] | None = None,
) -> ValidationContext:
    """Validate a parsed `eligibility.json` against the zones rooms.json defines.

    `roster_columns` is optional: when it is None the "attribute must be a roster
    column" check is skipped rather than guessed at (roster.csv may itself have
    failed to load).
    """
    ctx = ValidationContext()
    top = f"{ELIGIBILITY_FILE}: (top level)"
    if not _is_mapping(doc):
        ctx.error(
            top,
            f"expected a JSON object, got {_a_typename(doc)}.",
            "See docs/SPEC.md §2.2 for the required shape.",
        )
        return ctx

    _check_schema_version(ctx, ELIGIBILITY_FILE, doc)
    _check_unknown_keys(ctx, top, doc, ("schema_version", "rules"))

    rules = _require(
        ctx, top, doc, "rules", _is_list, "an array of rule objects",
        hint="Rules are evaluated top to bottom, first match wins (SPEC §2.2).",
    )
    if rules is None:
        return ctx
    if not rules:
        ctx.error(
            f"{ELIGIBILITY_FILE}: rules",
            "is empty; every person would have undefined eligibility.",
            'At minimum define a catch-all: {"id": "everyone", "when": {},'
            ' "allow_zones": "*", "reason": "..."}.',
        )
        return ctx

    seen_ids: dict[str, int] = {}
    first_catch_all: tuple[int, str] | None = None

    for index, rule in enumerate(rules):
        where = f"{ELIGIBILITY_FILE}: rules[{index}]"
        if not _is_mapping(rule):
            ctx.error(where, f"expected an object, got {_a_typename(rule)} ({_fmt(rule)}).")
            continue

        rule_id = rule.get("id")
        if _is_str(rule_id) and rule_id.strip():
            where = f'{ELIGIBILITY_FILE}: rules[{index}] ("{rule_id}")'
            if rule_id in seen_ids:
                ctx.error(
                    where,
                    f"duplicate rule id '{rule_id}'; rules[{seen_ids[rule_id]}] already uses it.",
                    "Rule ids must be unique — they are how the report explains why a"
                    " person was allowed a zone.",
                )
            else:
                seen_ids[rule_id] = index
        else:
            ctx.error(
                where,
                "missing required key 'id' (a unique, non-empty string)."
                if rule_id is None
                else f"'id' is {_a_typename(rule_id)} ({_fmt(rule_id)}); expected a non-empty string.",
            )

        _check_unknown_keys(ctx, where, rule, ("id", "when", "allow_zones", "reason"))

        if "reason" not in rule:
            ctx.warn(
                where,
                "has no 'reason'; the coordinator report quotes it to explain each"
                " person's eligibility.",
            )
        elif not _is_str(rule["reason"]):
            ctx.error(
                where,
                f"'reason' is {_a_typename(rule['reason'])} ({_fmt(rule['reason'])});"
                " expected a string.",
            )

        when = rule.get("when")
        if when is None:
            ctx.error(
                where,
                "missing required key 'when'.",
                'Use {} for a catch-all rule that matches everybody.',
            )
        elif not _is_mapping(when):
            ctx.error(
                where,
                f"'when' is {_a_typename(when)} ({_fmt(when)}); expected an object mapping"
                " roster attribute -> matcher.",
                'e.g. {"candidacy": "precandidate"} or {} to match everybody.',
            )
        else:
            _validate_predicate(ctx, f"{where}.when", when, roster_columns)
            # Unreachability is only reported for the one case that is certain:
            # an earlier catch-all matches everybody, so first-match-wins means
            # nothing below it is ever consulted. General subsumption between two
            # non-empty predicates is deliberately NOT attempted — deciding it
            # needs the roster's value domain and would produce confidently wrong
            # warnings about rules that are in fact reachable.
            if first_catch_all is not None:
                ctx.warn(
                    where,
                    f"can never match: rules[{first_catch_all[0]}]"
                    f" (\"{first_catch_all[1]}\") is a catch-all and comes first, so"
                    " evaluation never reaches this rule.",
                    "Rules are first-match-wins. Move this rule above the catch-all"
                    " (the catch-all must be last).",
                )
            elif not _visible_keys(when):
                first_catch_all = (index, str(rule_id))

        _validate_allow_zones(ctx, where, rule, zone_ids)

    _validate_catch_all_last(ctx, rules)
    return ctx


def _validate_catch_all_last(ctx: ValidationContext, rules: Sequence[Any]) -> None:
    last = rules[-1]
    last_index = len(rules) - 1
    if _is_mapping(last) and _is_mapping(last.get("when")) and not _visible_keys(last["when"]):
        return
    last_id = last.get("id") if _is_mapping(last) else None
    where = f"{ELIGIBILITY_FILE}: rules[{last_index}]"
    if _is_str(last_id):
        where = f'{where} ("{last_id}")'
    ctx.error(
        where,
        "is the last rule but is not a catch-all, so a person matching no rule"
        " would have undefined eligibility.",
        'The final rule must have "when": {}. Append e.g.'
        ' {"id": "everyone_else", "when": {}, "allow_zones": "*",'
        ' "reason": "..."} (SPEC §2.2).',
    )


def _validate_allow_zones(
    ctx: ValidationContext,
    rule_where: str,
    rule: Mapping[str, Any],
    zone_ids: Sequence[str],
) -> None:
    where = f"{rule_where}.allow_zones"
    if "allow_zones" not in rule:
        ctx.error(
            where,
            "missing required key 'allow_zones'.",
            '"*" for every zone, or an array of zone ids. '
            + closed_set_hint("", zone_ids, "Defined zones"),
        )
        return
    value = rule["allow_zones"]
    if _is_str(value):
        if value != "*":
            ctx.error(
                where,
                f"is the string {_fmt(value)}; the only string form is \"*\" (all zones).",
                'To allow one zone write it as a list: ["' + value + '"]. '
                + closed_set_hint(value, zone_ids, "Defined zones"),
            )
        return
    if not _is_list(value):
        ctx.error(
            where,
            f"is {_a_typename(value)} ({_fmt(value)}); expected \"*\" or an array of zone ids.",
            closed_set_hint(value, zone_ids, "Defined zones"),
        )
        return
    if not value:
        ctx.error(
            where,
            "is an empty array, so anyone matching this rule would have no eligible"
            " desk anywhere and the solve could never succeed.",
            'Use "*" for every zone, or list at least one zone id. '
            + closed_set_hint("", zone_ids, "Defined zones"),
        )
        return
    seen: dict[str, int] = {}
    for z_index, zone in enumerate(value):
        entry_where = f"{where}[{z_index}]"
        if not _is_str(zone):
            ctx.error(
                entry_where,
                f"is {_a_typename(zone)} ({_fmt(zone)}); expected a zone id string.",
                closed_set_hint(zone, zone_ids, "Defined zones"),
            )
            continue
        if zone in seen:
            ctx.warn(
                entry_where,
                f"lists zone '{zone}' twice (also at index {seen[zone]}); the duplicate"
                " has no effect.",
            )
        else:
            seen[zone] = z_index
        if zone_ids and zone not in zone_ids:
            ctx.error(
                entry_where,
                f"references zone '{zone}', which is not defined in {ROOMS_FILE}:zones.",
                closed_set_hint(zone, zone_ids, "Defined zones"),
            )


def _validate_predicate(
    ctx: ValidationContext,
    when_where: str,
    when: Mapping[str, Any],
    roster_columns: Sequence[str] | None,
) -> None:
    for attr in _visible_keys(when):
        where = f"{when_where}.{attr}"
        if not attr.strip():
            ctx.error(when_where, "has an attribute name that is blank.")
            continue
        if roster_columns and attr not in roster_columns:
            ctx.error(
                where,
                f"tests roster attribute '{attr}', which is not a column in {ROSTER_FILE}.",
                closed_set_hint(attr, roster_columns, "Roster columns"),
            )
        _validate_matcher(ctx, where, when[attr], attr, depth=0)


def _validate_matcher(
    ctx: ValidationContext, where: str, matcher: Any, attr: str, depth: int
) -> None:
    """One matcher from the SPEC §2.2 grammar: scalar | list | {min,max} | {not}."""
    if depth > 8:
        # Only reachable via absurdly nested {"not": {"not": ...}}; bail rather
        # than recurse forever on a hand-written file.
        ctx.error(where, "matcher is nested too deeply to be meaningful.")
        return

    if matcher is None:
        ctx.error(
            where,
            "matcher is null.",
            'Use a value ("candidate"), a list ([1, 2]), a range ({"min": 1, "max": 2})'
            ' or a negation ({"not": ...}). To match an empty cell, compare against "".',
        )
        return

    if _is_list(matcher):
        if not matcher:
            ctx.error(
                where,
                "matcher is an empty list, which matches nobody.",
                "Remove the attribute from 'when' if it should not constrain anything.",
            )
            return
        for i, item in enumerate(matcher):
            if _is_list(item) or _is_mapping(item):
                ctx.error(
                    f"{where}[{i}]",
                    f"list matchers hold plain values; got {_a_typename(item)} ({_fmt(item)}).",
                    'e.g. {"year": [1, 2]}.',
                )
            elif item is None:
                ctx.error(f"{where}[{i}]", "list matchers cannot contain null.")
        return

    if _is_mapping(matcher):
        keys = _visible_keys(matcher)
        if "not" in keys:
            if len(keys) > 1:
                others = ", ".join(k for k in keys if k != "not")
                ctx.error(
                    where,
                    f"combines 'not' with {others}; a negation matcher holds only 'not'.",
                    'Nest instead: {"not": {"min": 1, "max": 2}}.',
                )
            _validate_matcher(ctx, f"{where}.not", matcher["not"], attr, depth + 1)
            return
        if not keys:
            ctx.error(
                where,
                "matcher is an empty object, which constrains nothing.",
                'Expected {"min": ..., "max": ...} (either bound optional) or'
                ' {"not": ...}. Remove the attribute to constrain nothing.',
            )
            return
        unknown = [k for k in keys if k not in ("min", "max")]
        if unknown:
            ctx.error(
                where,
                f"object matcher has key(s) {', '.join(repr(k) for k in unknown)}.",
                closed_set_hint(unknown[0], ("min", "max", "not"), "Valid matcher keys")
                + ' A range is {"min": 1, "max": 2} (either bound optional);'
                ' a negation is {"not": ...}.',
            )
            return
        low = matcher.get("min")
        high = matcher.get("max")
        for name, bound in (("min", low), ("max", high)):
            if name in matcher and not _is_finite_number(bound):
                ctx.error(
                    f"{where}.{name}",
                    f"is {_a_typename(bound)} ({_fmt(bound)}); a range bound must be a"
                    " finite number.",
                    f"Ranges are inclusive and numeric; '{attr}' is compared as a number.",
                )
        if _is_finite_number(low) and _is_finite_number(high) and float(low) > float(high):
            ctx.error(
                where,
                f"range is min={_fmt(low)} > max={_fmt(high)}, which matches nobody.",
                "min and max are inclusive bounds; swap them.",
            )
        return

    if isinstance(matcher, bool):
        ctx.warn(
            where,
            f"matcher is the boolean {_fmt(matcher)}; roster values are text, so this"
            " compares against the string 'true'/'false'.",
            'Quote it if that is what you meant: "true".',
        )
    # Everything else (string / number) is a valid scalar equality matcher.


# --------------------------------------------------------------------------
# 9. roster.csv (SPEC §2.3)
# --------------------------------------------------------------------------


def validate_roster(
    rows: Sequence[Mapping[str, Any]],
    desk_ids: Sequence[str],
    eligibility_attrs: Mapping[str, frozenset[str]] | None = None,
    *,
    columns: Sequence[str] | None = None,
    line_numbers: Sequence[int] | None = None,
) -> ValidationContext:
    """Validate roster rows (already split into column -> raw text).

    `eligibility_attrs` maps a roster attribute to the set of literal values that
    eligibility rules compare it against (see `predicate_values`); it drives the
    SPEC §2.3 warning about values no rule mentions. `desk_ids` may be empty when
    rooms.json failed — the desk-id checks are then skipped so one broken file
    does not produce a second wave of misleading messages.
    """
    ctx = ValidationContext()
    if columns is None:
        found: set[str] = set()
        for row in rows:
            found.update(str(k) for k in row)
        columns = tuple(sorted(found))
    if line_numbers is None:
        # Default assumption: one header line, then one line per row.
        line_numbers = tuple(i + 2 for i in range(len(rows)))

    if not columns:
        ctx.error(
            f"{ROSTER_FILE}: (header)",
            "has no columns; the file appears to be empty.",
            "Expected a header row: " + ",".join(REQUIRED_ROSTER_COLUMNS),
        )
        return ctx

    for required in REQUIRED_ROSTER_COLUMNS:
        if required not in columns:
            ctx.error(
                f"{ROSTER_FILE}: (header)",
                f"missing required column '{required}'.",
                closed_set_hint(required, columns, "Columns present")
                + " Required columns: "
                + ", ".join(REQUIRED_ROSTER_COLUMNS)
                + ". Extra columns are fine and stay usable in eligibility predicates.",
            )

    if not rows:
        ctx.error(
            f"{ROSTER_FILE}: (no data rows)",
            "contains a header but no people.",
            "Export the department roster into this file; the solver matches every"
            " response against it.",
        )
        return ctx

    seen_emails: dict[str, int] = {}      # normalised email -> line number
    desk_keepers: dict[str, tuple[int, str]] = {}   # desk id -> (line, email)
    unreferenced: dict[tuple[str, str], list[str]] = {}

    for index, row in enumerate(rows):
        line = line_numbers[index] if index < len(line_numbers) else index + 2
        raw_email = str(row.get("email", "") or "")
        email = normalize_email(raw_email)
        where = _row_where(line, email)

        # --- email: the primary key -------------------------------------
        if not email:
            ctx.error(
                _row_where(line, None, "email"),
                "is empty; email is the primary key that joins the roster to form"
                " responses.",
            )
        else:
            if "@" not in email:
                ctx.warn(
                    _row_where(line, email, "email"),
                    f"'{raw_email.strip()}' does not look like an email address.",
                    "Responses join to the roster on this exact value, lower-cased.",
                )
            if email in seen_emails:
                ctx.error(
                    _row_where(line, email, "email"),
                    f"duplicate email '{email}'; line {seen_emails[email]} already uses it"
                    " (comparison is case-insensitive and ignores surrounding spaces).",
                    "Email is the primary key: one row per person. Delete or merge the"
                    " duplicate row.",
                )
            else:
                seen_emails[email] = line

        # --- name --------------------------------------------------------
        if "name" in columns and not str(row.get("name", "") or "").strip():
            ctx.warn(
                _row_where(line, email, "name"),
                "is empty; reports and the assignment sheet will fall back to the email.",
            )

        # --- year --------------------------------------------------------
        if "year" in columns:
            raw_year = str(row.get("year", "") or "")
            if parse_year(raw_year) is None:
                ctx.error(
                    _row_where(line, email, "year"),
                    f"is {_fmt(raw_year.strip())}; expected a whole number >= 1.",
                    "Year is the year of the programme (1 = first year). Eligibility"
                    " rules may compare it numerically.",
                )

        # --- candidacy ---------------------------------------------------
        candidacy_raw = str(row.get("candidacy", "") or "").strip()
        if "candidacy" in columns and not candidacy_raw:
            ctx.warn(
                _row_where(line, email, "candidacy"),
                "is empty; this person can only be matched by a rule that tests for"
                " an empty value, so they will fall through to the catch-all rule.",
            )

        # --- keeps_desk / current_desk -----------------------------------
        keeps_raw = str(row.get("keeps_desk", "") or "")
        keeps = parse_keeps_desk(keeps_raw)
        if keeps is None:
            if not keeps_raw.strip():
                # Blank is common in hand-edited exports. Treat it as "no" (the
                # overwhelmingly common case) but say so, because guessing
                # silently would quietly add someone to the pool.
                ctx.warn(
                    _row_where(line, email, "keeps_desk"),
                    "is empty; treating it as 'no' (this person enters the desk pool).",
                    "Accepted values: " + ", ".join(sorted(TRUTHY_STRINGS + FALSY_STRINGS)) + ".",
                )
                keeps = False
            else:
                ctx.error(
                    _row_where(line, email, "keeps_desk"),
                    f"is {_fmt(keeps_raw.strip())}, which is not a recognised yes/no value.",
                    closed_set_hint(
                        keeps_raw.strip().lower(),
                        TRUTHY_STRINGS + FALSY_STRINGS,
                        "Accepted values (case-insensitive)",
                    ),
                )

        current_desk = str(row.get("current_desk", "") or "").strip()
        if keeps is True:
            if not current_desk:
                ctx.error(
                    _row_where(line, email, "current_desk"),
                    "is empty, but keeps_desk is"
                    f" {_fmt(keeps_raw.strip())} — a desk keeper must name the desk"
                    " they are keeping.",
                    "That person and that desk are both removed from the pool before"
                    " solving (SPEC §3.4), so the solver cannot proceed without it."
                    " Either fill in the desk id or set keeps_desk to 'no'.",
                )
            elif desk_ids and current_desk not in desk_ids:
                ctx.error(
                    _row_where(line, email, "current_desk"),
                    f"is '{current_desk}', which is not a desk id defined in {ROOMS_FILE}.",
                    closed_set_hint(current_desk, desk_ids, "Defined desk ids"),
                )
            elif current_desk:
                if current_desk in desk_keepers:
                    other_line, other_email = desk_keepers[current_desk]
                    ctx.error(
                        _row_where(line, email, "current_desk"),
                        f"claims desk '{current_desk}', which line {other_line}"
                        f" ({other_email}) also keeps.",
                        "Two people cannot keep the same desk. One of these rows is"
                        " out of date.",
                    )
                else:
                    desk_keepers[current_desk] = (line, email or f"line {line}")
        elif keeps is False and current_desk:
            # `keeps is False`, not just falsy: when keeps_desk failed to parse we
            # have already reported that, and following it with "so current_desk is
            # ignored" would state a consequence of an error the coordinator has
            # not resolved yet.
            ctx.warn(
                _row_where(line, email, "current_desk"),
                f"names desk '{current_desk}' but keeps_desk is"
                f" {_fmt(keeps_raw.strip())}, so it is ignored and the desk stays in"
                " the pool.",
                "This is normal after a move; set keeps_desk to 'yes' if they are"
                " actually staying put.",
            )
            if desk_ids and current_desk not in desk_ids:
                ctx.warn(
                    _row_where(line, email, "current_desk"),
                    f"'{current_desk}' is not a desk id defined in {ROOMS_FILE}.",
                    closed_set_hint(current_desk, desk_ids, "Defined desk ids"),
                )

        # --- values no eligibility rule mentions (SPEC §2.3) --------------
        if eligibility_attrs:
            for attr in sorted(eligibility_attrs):
                if attr not in columns:
                    continue  # already reported against eligibility.json
                referenced = eligibility_attrs[attr]
                if not referenced:
                    continue  # only range matchers on this attribute; nothing to compare
                if any(_NUMERIC_STRING_RE.match(v) for v in sorted(referenced)):
                    # The attribute is compared numerically somewhere, so the
                    # literals a rule happens to name say nothing about which
                    # values are "handled": {"year": {"min": 3}} names none at all,
                    # and every year but 3 would be flagged by a rule that reads
                    # {"year": 3}. SPEC §2.3 asks for this warning on categorical
                    # columns (candidacy); applying it to numbers is just noise.
                    continue
                value = str(row.get(attr, "") or "").strip()
                if value and value.lower() not in referenced:
                    unreferenced.setdefault((attr, value), []).append(email or f"line {line}")

    for (attr, value), holders in sorted(unreferenced.items()):
        people = sorted(set(holders))  # a duplicated email is its own error
        shown = ", ".join(people[:3])
        more = f" and {len(people) - 3} more" if len(people) > 3 else ""
        who = "1 person" if len(people) == 1 else f"{len(people)} people"
        ctx.warn(
            f"{ROSTER_FILE}: column '{attr}'",
            f"value '{value}' is held by {who} ({shown}{more})"
            f" but is never referenced by any rule in {ELIGIBILITY_FILE}.",
            "They can still be matched by a catch-all rule; this is only a problem if"
            " you meant to write a rule for them (or if the value is a typo of one"
            f" that is referenced: {', '.join(sorted(eligibility_attrs[attr]))}).",
        )

    return ctx


def _row_where(line: int, email: str | None, column: str | None = None) -> str:
    where = f"{ROSTER_FILE}: line {line}"
    if email:
        where += f' ("{email}")'
    if column:
        where += f", column '{column}'"
    return where


# --------------------------------------------------------------------------
# 10. scoring.json (SPEC §2.4)
# --------------------------------------------------------------------------


def validate_scoring(doc: Any) -> ValidationContext:
    """Validate a parsed `scoring.json`. K is `len(curves[primary_curve])`."""
    ctx = ValidationContext()
    top = f"{SCORING_FILE}: (top level)"
    if not _is_mapping(doc):
        ctx.error(
            top,
            f"expected a JSON object, got {_a_typename(doc)}.",
            "See docs/SPEC.md §2.4 for the required shape.",
        )
        return ctx

    _check_schema_version(ctx, SCORING_FILE, doc)
    _check_unknown_keys(
        ctx,
        top,
        doc,
        (
            "schema_version",
            "curves",
            "primary_curve",
            "comparison_curves",
            "tie_break_seed",
            "seed_committed_at",
            "sensitivity_seeds",
            "seed_year",
        ),
    )

    curve_names, curve_lengths = _validate_curves(ctx, doc)
    primary = _validate_primary_curve(ctx, doc, curve_names)

    # K is derived, never declared (invariant I1). The primary curve defines it;
    # if the primary is unusable, fall back to the first curve in sorted order so
    # the length comparison still has a deterministic reference point.
    reference: str | None = None
    if primary is not None and primary in curve_lengths:
        reference = primary
    elif curve_lengths:
        reference = sorted(curve_lengths)[0]
    if reference is not None:
        k = curve_lengths[reference]
        for name in sorted(curve_lengths):
            if curve_lengths[name] != k:
                ctx.error(
                    f"{SCORING_FILE}: curves.{name}",
                    f"has {curve_lengths[name]} values but curves.{reference} has {k}.",
                    "Every curve must have the same length, because that length IS K"
                    " — the number of desks each student ranks (SPEC §2.4). Changing K"
                    " means editing every curve and nothing else.",
                )

    _validate_comparison_curves(ctx, doc, curve_names, primary)
    _validate_seeds(ctx, doc)
    return ctx


def _validate_curves(
    ctx: ValidationContext, doc: Mapping[str, Any]
) -> tuple[tuple[str, ...], dict[str, int]]:
    top = f"{SCORING_FILE}: (top level)"
    curves = _require(
        ctx, top, doc, "curves", _is_mapping,
        "an object mapping curve name -> array of point values",
        hint='e.g. {"linear_borda": [5, 4, 3, 2, 1]}.',
    )
    if curves is None:
        return ((), {})
    names = tuple(_visible_keys(curves))
    if not names:
        ctx.error(
            f"{SCORING_FILE}: curves",
            "defines no curves, so K (= len(curves[primary_curve])) is undefined.",
            'Define at least the primary curve, e.g. "linear_borda": [5, 4, 3, 2, 1].',
        )
        return ((), {})

    lengths: dict[str, int] = {}
    for name in names:  # sorted by _visible_keys -> deterministic message order
        where = f"{SCORING_FILE}: curves.{name}"
        values = curves[name]
        if not _is_list(values):
            ctx.error(
                where,
                f"is {_a_typename(values)} ({_fmt(values)}); expected an array of numbers,"
                " highest first.",
            )
            continue
        if not values:
            ctx.error(
                where,
                "is empty; a curve needs one value per rank, so K would be 0 and nobody"
                " could rank anything.",
            )
            continue
        lengths[name] = len(values)
        _validate_curve_values(ctx, where, name, values)
    return (names, lengths)


def _validate_curve_values(
    ctx: ValidationContext, where: str, name: str, values: Sequence[Any]
) -> None:
    fractions: list[Fraction | None] = []
    for index, value in enumerate(values):
        entry = f"{SCORING_FILE}: curves.{name}[{index}]"
        rank = index + 1
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            if _is_str(value) and _NUMERIC_STRING_RE.match(value):
                ctx.error(
                    entry,
                    f"rank {rank} is the string {_fmt(value)}, not a number.",
                    "Remove the quotes: [5, 4, 3, 2, 1], not [\"5\", \"4\", ...].",
                )
            else:
                ctx.error(
                    entry,
                    f"rank {rank} is {_a_typename(value)} ({_fmt(value)}); expected a"
                    " positive number.",
                )
            fractions.append(None)
            continue
        if not math.isfinite(float(value)):
            ctx.error(
                entry,
                f"rank {rank} is {_fmt(value)}, which is not a finite number.",
                "Point values must be ordinary positive decimals.",
            )
            fractions.append(None)
            continue

        exact = curve_value_to_fraction(value)
        if exact <= 0:
            ctx.error(
                entry,
                f"rank {rank} is worth {_fmt(value)}; every curve value must be"
                " strictly positive.",
                "A zero or negative value makes 'got my last choice' indistinguishable"
                " from 'got no desk at all' in the objective, which destroys the"
                " meaning of the K-floor (SPEC §2.4).",
            )
            fractions.append(None)
            continue
        if exact.denominator > MAX_CURVE_DENOMINATOR:
            ctx.error(
                entry,
                f"rank {rank} is {_fmt(value)}, which is a non-terminating decimal"
                f" written out to floating-point precision: it rationalises exactly to"
                f" {exact.numerator}/{exact.denominator}.",
                "Points are converted to exact fractions and then scaled by the LCM of"
                " all denominators (SPEC §5.3), so this value would produce an"
                " astronomical scale factor and overflow the integer points matrix."
                " Write a terminating decimal (0.333 is fine), or rescale the whole"
                " curve so the value becomes a whole number (multiply every entry by 3).",
            )
            fractions.append(None)
            continue
        fractions.append(exact)

    # Strictly decreasing: compare each adjacent pair whose values both parsed.
    for index in range(len(fractions) - 1):
        high, low = fractions[index], fractions[index + 1]
        if high is None or low is None:
            continue
        if low >= high:
            relation = "the same as" if low == high else "more than"
            ctx.error(
                f"{SCORING_FILE}: curves.{name}[{index + 1}]",
                f"rank {index + 2} is worth {_fmt(values[index + 1])}, which is"
                f" {relation} rank {index + 1} ({_fmt(values[index])}).",
                "Curves must be STRICTLY decreasing: a later choice worth as much as an"
                " earlier one makes the ranking meaningless, and the solver would be"
                " free to hand out last choices instead of first ones (SPEC §2.4).",
            )

    known = [f for f in fractions if f is not None]
    if not known:
        return
    scale = 1
    for exact in known:
        scale = scale * exact.denominator // math.gcd(scale, exact.denominator)
    largest = max(int(exact * scale) for exact in known)
    if largest > MAX_SCALED_POINT:
        ctx.error(
            f"{SCORING_FILE}: curves.{name}",
            f"scales to integer points as large as {largest}, which overflows the"
            " int64 points matrix.",
            "The curve is multiplied by the LCM of its denominators to make points"
            " exact integers (SPEC §5.3). Use smaller values or fewer decimal places.",
        )


def _validate_primary_curve(
    ctx: ValidationContext, doc: Mapping[str, Any], curve_names: Sequence[str]
) -> str | None:
    where = f"{SCORING_FILE}: primary_curve"
    if "primary_curve" not in doc:
        ctx.error(
            where,
            "missing required key 'primary_curve'.",
            "It names the curve the published result uses, and its length is K. "
            + closed_set_hint("", curve_names, "Defined curves"),
        )
        return None
    value = doc["primary_curve"]
    if not _is_str(value):
        ctx.error(
            where,
            f"is {_a_typename(value)} ({_fmt(value)}); expected the name of a curve.",
            closed_set_hint(value, curve_names, "Defined curves"),
        )
        return None
    if curve_names and value not in curve_names:
        ctx.error(
            where,
            f"is '{value}', which is not defined in {SCORING_FILE}:curves.",
            closed_set_hint(value, curve_names, "Defined curves"),
        )
        return None
    return value


def _validate_comparison_curves(
    ctx: ValidationContext,
    doc: Mapping[str, Any],
    curve_names: Sequence[str],
    primary: str | None,
) -> None:
    where = f"{SCORING_FILE}: comparison_curves"
    if "comparison_curves" not in doc:
        ctx.warn(
            where,
            "is not set; the report will show no sensitivity-to-curve comparison.",
            'e.g. "comparison_curves": ["convex", "concave"].',
        )
        return
    value = doc["comparison_curves"]
    if not _is_list(value):
        ctx.error(
            where,
            f"is {_a_typename(value)} ({_fmt(value)}); expected an array of curve names.",
            closed_set_hint(value, curve_names, "Defined curves"),
        )
        return
    seen: dict[str, int] = {}
    for index, name in enumerate(value):
        entry = f"{where}[{index}]"
        if not _is_str(name):
            ctx.error(
                entry,
                f"is {_a_typename(name)} ({_fmt(name)}); expected a curve name.",
                closed_set_hint(name, curve_names, "Defined curves"),
            )
            continue
        if curve_names and name not in curve_names:
            ctx.error(
                entry,
                f"is '{name}', which is not defined in {SCORING_FILE}:curves.",
                closed_set_hint(name, curve_names, "Defined curves"),
            )
            continue
        if name in seen:
            ctx.warn(entry, f"lists '{name}' twice (also at index {seen[name]}).")
        else:
            seen[name] = index
        if primary is not None and name == primary:
            ctx.warn(
                entry,
                f"lists the primary curve '{name}'; comparing the primary curve against"
                " itself adds nothing to the report.",
            )


def _validate_seed_year(ctx: ValidationContext, doc: Mapping[str, Any]) -> bool:
    """Validate `seed_year`. Returns True when it governs (so tie_break_seed is
    then optional and unused).

    `seed_year` may be an integer year, or the string "auto" meaning "the
    calendar year at run time, pinned once at config load". Either way the
    resolved value is recorded in results.json, so a re-run in a later year
    still reproduces the published result.
    """
    where = f"{SCORING_FILE}: seed_year"
    if "seed_year" not in doc:
        return False
    value = doc["seed_year"]
    if value is None:
        return False
    if _is_str(value):
        if value.strip().casefold() == "auto":
            return True
        ctx.error(
            where,
            f"is {_fmt(value)}; the only string accepted is \"auto\".",
            'Use "auto" for "the year this is run", or an integer like 2026 to '
            "pin it. Pinning is what you want when re-running an old cycle.",
        )
        return False
    if not _is_int(value):
        ctx.error(
            where,
            f"is {_a_typename(value)} ({_fmt(value)}); expected an integer year "
            f'or the string "auto".',
        )
        return False
    year = int(value)
    if not 1900 <= year <= 2999:
        ctx.error(
            where, f"is {year}, which is not a plausible calendar year.",
            'Use a four-digit year such as 2026, or "auto".',
        )
        return False
    return True


def _validate_seeds(ctx: ValidationContext, doc: Mapping[str, Any]) -> None:
    year_governs = _validate_seed_year(ctx, doc)

    where = f"{SCORING_FILE}: tie_break_seed"
    seed = doc.get("tie_break_seed")
    if year_governs:
        # The year is the seed. A leftover tie_break_seed is dead config, and
        # dead config that looks live is how someone concludes the wrong value
        # was used. Note this only skips the tie_break_seed checks -- the
        # sensitivity seeds and seed_committed_at below still apply.
        if seed not in (None, ""):
            ctx.warn(
                where,
                f"is set ({_fmt(seed)}) but is IGNORED, because seed_year governs "
                f"the tie-break.",
                "Delete 'tie_break_seed', or remove 'seed_year' if the string seed "
                "is the one you meant to use.",
            )
    elif "tie_break_seed" not in doc:
        ctx.error(
            where,
            "missing required key 'tie_break_seed'.",
            "Tie-breaking is seeded so it is reproducible and publishable. Either"
            ' set "seed_year": "auto" to use the cycle year, or choose a string,'
            " announce it before the form opens, and record when in"
            " 'seed_committed_at' (SPEC §5.4, §8).",
        )
    elif not _is_str(seed):
        ctx.error(
            where,
            f"is {_a_typename(seed)} ({_fmt(seed)}); expected a string.",
            "The seed is hashed with SHA-256 to produce the RNG seed, so it must be text.",
        )
    elif not seed.strip():
        ctx.error(
            where,
            "is empty.",
            "Choose a seed string and announce it before the form opens (SPEC §8).",
        )
    else:
        lowered = seed.lower()
        if any(marker in lowered for marker in PLACEHOLDER_SEED_MARKERS):
            ctx.warn(
                where,
                f"is still the shipped placeholder ({_fmt(seed)}).",
                "*** The whole integrity argument in SPEC §8 rests on this seed being"
                " chosen and ANNOUNCED PUBLICLY BEFORE THE FORM OPENS. Replace it, post"
                " it on Discord, and set 'seed_committed_at' to when you posted it."
                " Running with the placeholder produces a valid, reproducible result"
                " that nobody has any reason to trust. ***",
            )

    committed = doc.get("seed_committed_at")
    committed_where = f"{SCORING_FILE}: seed_committed_at"
    if committed is None:
        # Only worth nagging about when a human chose the seed. With seed_year
        # there is nothing to announce: the seed is the calendar year, which is
        # public knowledge and not the coordinator's to pick.
        if not year_governs:
            ctx.warn(
                committed_where,
                "is not set, so the audit trail has no record of when the tie-break"
                " seed was announced.",
                "Set it to the ISO-8601 timestamp of the announcement, e.g."
                ' "2026-09-01T12:00:00-04:00". It is informational only and never'
                " affects the solve.",
            )
    elif not _is_str(committed):
        ctx.error(
            committed_where,
            f"is {_a_typename(committed)} ({_fmt(committed)}); expected an ISO-8601"
            " timestamp string or null.",
        )
    else:
        from datetime import datetime  # local: only needed to parse, never to read a clock

        try:
            datetime.fromisoformat(committed)
        except ValueError:
            ctx.warn(
                committed_where,
                f"{_fmt(committed)} is not a parseable ISO-8601 timestamp.",
                'Use e.g. "2026-09-01T12:00:00-04:00". It appears verbatim in the audit'
                " trail either way.",
            )

    sens_where = f"{SCORING_FILE}: sensitivity_seeds"
    sens = doc.get("sensitivity_seeds")
    if sens is None:
        return
    if not _is_list(sens):
        ctx.error(
            sens_where,
            f"is {_a_typename(sens)} ({_fmt(sens)}); expected an array of seed strings.",
            "These are alternative seeds used to show that the result is not an"
            " artefact of the chosen one.",
        )
        return
    seen: dict[str, int] = {}
    for index, value in enumerate(sens):
        entry = f"{sens_where}[{index}]"
        if not _is_str(value):
            ctx.error(entry, f"is {_a_typename(value)} ({_fmt(value)}); expected a string.")
            continue
        if not value.strip():
            ctx.error(entry, "is empty; a seed must be a non-empty string.")
            continue
        if value in seen:
            ctx.error(
                entry,
                f"repeats the seed {_fmt(value)} from index {seen[value]}.",
                "Sensitivity seeds must be distinct: two identical seeds produce two"
                " identical runs and measure nothing.",
            )
            continue
        seen[value] = index
        if _is_str(seed) and value == seed:
            ctx.warn(
                entry,
                f"is the same as tie_break_seed ({_fmt(value)}), so this sensitivity run"
                " just reproduces the published one.",
            )
