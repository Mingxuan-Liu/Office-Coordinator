"""Load `config/` into a validated `types.Config`.

The contract this module upholds: **no downstream module ever sees a malformed
config object.** Every file is read, hashed, parsed and validated *first*; the
dataclasses are only built once validation has passed clean. A coordinator's
mistake therefore surfaces as one `ConfigError` listing every problem found
across all four files, never as a `KeyError` two modules later.

Consequences worth knowing:

  * The `_build_*` functions below assume the schema has already been enforced.
    They are unreachable while `ctx.problems` is non-empty. Where they still
    index a required key, that is deliberate: if it ever fails, the validator
    and the loader have drifted apart, and `_impossible()` says so in as many
    words rather than emitting a plausible-looking wrong object.
  * Keys starting with `_` are documentation (`"_comment"`) and are ignored
    everywhere. Unrecognised keys that do not start with `_` are a warning, not
    an error — see `validate._check_unknown_keys`.
  * `Person.attributes` holds every roster column verbatim, including columns
    this codebase has never heard of, so eligibility predicates can reference
    them. The known columns are additionally parsed into typed fields; a
    predicate evaluator should prefer the typed attribute where one exists and
    fall back to the raw string otherwise.
  * Determinism (invariant I3): `roster.people` is sorted by email, hashes are
    computed over raw bytes, and nothing here reads the clock.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO

from .errors import ConfigError, DeskMatchError, Problem
from .types import (
    Feature,
    Config,
    Desk,
    Eligibility,
    EligibilityRule,
    Person,
    Room,
    Rooms,
    Roster,
    Scoring,
    Zone,
)
from .validate import (
    CONFIG_FILES,
    ELIGIBILITY_FILE,
    ROOMS_FILE,
    ROSTER_FILE,
    SCORING_FILE,
    ValidationContext,
    closed_set_hint,
    curve_value_to_fraction,
    desk_ids_of,
    normalize_email,
    parse_keeps_desk,
    parse_year,
    predicate_values,
    validate_eligibility,
    validate_rooms,
    validate_roster,
    validate_scoring,
    zone_ids_of,
)

__all__ = ["load_config", "load_config_or_exit"]


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------


def load_config(config_dir: str | Path) -> Config:
    """Read, validate and build the configuration in `config_dir`.

    Raises `ConfigError` carrying **every** problem found (never just the first).
    Warnings do not block the load; they are carried on `Config.warnings`.
    """
    base = Path(config_dir)
    ctx = ValidationContext()

    if not base.exists():
        ctx.error(
            f"{base}",
            "config directory does not exist.",
            "Point --config at the directory holding "
            + ", ".join(CONFIG_FILES)
            + " (the repository ships one at config/).",
        )
        raise ConfigError(ctx.problems, ctx.warnings)
    if not base.is_dir():
        ctx.error(f"{base}", "is a file, not a directory.", "--config takes a directory.")
        raise ConfigError(ctx.problems, ctx.warnings)

    raw = _read_all(base, ctx)
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in raw.items()}

    rooms_doc = _parse_json(ctx, ROOMS_FILE, raw.get(ROOMS_FILE))
    elig_doc = _parse_json(ctx, ELIGIBILITY_FILE, raw.get(ELIGIBILITY_FILE))
    scoring_doc = _parse_json(ctx, SCORING_FILE, raw.get(SCORING_FILE))
    roster_table = _parse_roster_csv(ctx, raw.get(ROSTER_FILE))

    # Derived cross-file inputs, extracted tolerantly: if rooms.json is broken we
    # get an empty tuple and the dependent checks are skipped, so one root cause
    # produces one message instead of a second wave of misleading ones.
    zone_ids = zone_ids_of(rooms_doc)
    desk_ids = desk_ids_of(rooms_doc)
    roster_columns = roster_table[0] if roster_table is not None else None

    if rooms_doc is not None:
        ctx.merge(validate_rooms(rooms_doc, base))
    if elig_doc is not None:
        ctx.merge(validate_eligibility(elig_doc, zone_ids, roster_columns))
    if roster_table is not None:
        columns, rows, line_numbers = roster_table
        ctx.merge(
            validate_roster(
                rows,
                desk_ids,
                predicate_values(elig_doc) if elig_doc is not None else None,
                columns=columns,
                line_numbers=line_numbers,
            )
        )
    if scoring_doc is not None:
        ctx.merge(validate_scoring(scoring_doc))

    problems = _grouped_by_file(ctx.problems)
    warnings = _grouped_by_file(ctx.warnings)
    if problems:
        raise ConfigError(problems, warnings)

    # Past this point every document is known to satisfy docs/SPEC.md §2.
    assert roster_table is not None  # guaranteed: a parse failure is a problem
    return Config(
        rooms=_build_rooms(rooms_doc),
        eligibility=_build_eligibility(elig_doc),
        roster=_build_roster(roster_table[1]),
        scoring=_build_scoring(scoring_doc),
        source_dir=str(base),
        file_hashes={name: hashes[name] for name in CONFIG_FILES if name in hashes},
        warnings=tuple(w.render() for w in warnings),
    )


def _grouped_by_file(items: Sequence[Problem]) -> list[Problem]:
    """Group the report by config file, in the SPEC §1 order.

    A stable sort, so the order *within* a file (which follows the document) is
    untouched. Without this, a JSON parse failure in one file would be listed
    ahead of schema problems in another simply because parsing happens first,
    and the coordinator would be bounced between files while fixing them.
    """
    order = {name: index for index, name in enumerate(CONFIG_FILES)}
    return sorted(
        items,
        key=lambda p: order.get(p.where.split(":", 1)[0].strip(), len(order)),
    )


def load_config_or_exit(
    config_dir: str | Path,
    *,
    stream: TextIO | None = None,
    show_warnings: bool = True,
) -> Config:
    """`load_config`, but for a CLI: render the failure and exit.

    Prints the rendered `ConfigError` (and any warnings collected alongside it)
    to stderr and exits with the exception's own exit code, so the shell sees the
    documented code from SPEC §9 rather than a traceback.
    """
    out = stream if stream is not None else sys.stderr
    try:
        config = load_config(config_dir)
    except ConfigError as exc:
        print(exc.render(), file=out)
        _print_warnings(exc.warnings, out)
        print(
            f"\nNothing was run. Fix the problems above in {config_dir} and try again.",
            file=out,
        )
        raise SystemExit(exc.exit_code)
    if show_warnings and config.warnings:
        count = len(config.warnings)
        print(
            f"{count} configuration warning{'s' if count != 1 else ''}"
            " (the run continues):",
            file=out,
        )
        for index, warning in enumerate(config.warnings):
            print(f"  ({index + 1}) {warning}", file=out)
    return config


def _print_warnings(warnings: Sequence[Problem], out: TextIO) -> None:
    if not warnings:
        return
    count = len(warnings)
    print(f"\n...and {count} warning{'s' if count != 1 else ''}:", file=out)
    for index, warning in enumerate(warnings):
        print(f"  ({index + 1}) {warning.render()}", file=out)


# --------------------------------------------------------------------------
# Reading and parsing
# --------------------------------------------------------------------------


def _read_all(base: Path, ctx: ValidationContext) -> dict[str, bytes]:
    """Read every config file as RAW BYTES (what the sha256 in the provenance
    block commits to — decoding first would make the hash depend on us)."""
    try:
        present = sorted(p.name for p in base.iterdir() if p.is_file())
    except OSError:
        present = []  # unlistable directory: the per-file errors below still fire
    raw: dict[str, bytes] = {}
    for name in CONFIG_FILES:
        path = base / name
        try:
            raw[name] = path.read_bytes()
        except FileNotFoundError:
            ctx.error(
                f"{name}",
                f"is missing from the config directory ({base}).",
                closed_set_hint(name, present, "Files present")
                + f" All of {', '.join(CONFIG_FILES)} are required (SPEC §2).",
            )
        except OSError as exc:
            ctx.error(f"{name}", f"could not be read: {exc.strerror or exc}.")
    return raw


def _parse_json(ctx: ValidationContext, name: str, data: bytes | None) -> Any | None:
    if data is None:
        return None  # the missing/unreadable file was already reported
    try:
        text = data.decode("utf-8-sig")  # -sig: strip a BOM if an editor added one
    except UnicodeDecodeError as exc:
        ctx.error(
            f"{name}: byte {exc.start}",
            f"is not valid UTF-8 ({exc.reason}).",
            "Re-save the file as UTF-8.",
        )
        return None
    if not text.strip():
        ctx.error(f"{name}", "is empty.", "See docs/SPEC.md §2 for the expected contents.")
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        ctx.error(
            f"{name}: line {exc.lineno}, column {exc.colno}",
            f"is not valid JSON: {exc.msg}.",
            "deskmatch reads strict JSON. The usual causes are a trailing comma"
            " before a closing ] or }, a missing comma between entries, single"
            " quotes instead of double quotes, or a // comment — write documentation"
            ' as a "_comment" key instead (keys starting with _ are ignored).',
        )
        return None


def _parse_roster_csv(
    ctx: ValidationContext, data: bytes | None
) -> tuple[tuple[str, ...], tuple[dict[str, str], ...], tuple[int, ...]] | None:
    """Split roster.csv into (columns, rows, line numbers).

    Structural problems (bad encoding, a row with the wrong number of fields) are
    reported here; everything semantic is `validate.validate_roster`'s job.
    """
    if data is None:
        return None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        ctx.error(
            f"{ROSTER_FILE}: byte {exc.start}",
            f"is not valid UTF-8 ({exc.reason}).",
            "Export the roster as UTF-8 CSV (in Excel: 'CSV UTF-8').",
        )
        return None

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header_row = next(reader)
    except StopIteration:
        ctx.error(
            f"{ROSTER_FILE}",
            "is empty; there is not even a header row.",
            "See docs/SPEC.md §2.3 for the expected columns.",
        )
        return None

    header = tuple(h.strip() for h in header_row)
    if header != tuple(header_row):
        ctx.warn(
            f"{ROSTER_FILE}: (header)",
            "has leading or trailing spaces in one or more column names; they have"
            " been trimmed.",
            "Spreadsheet exports do this. Eligibility predicates match the trimmed name.",
        )
    seen_columns: dict[str, int] = {}
    for position, column in enumerate(header):
        if not column:
            ctx.error(
                f"{ROSTER_FILE}: (header), column {position + 1}",
                "has an empty name.",
                "Every column needs a name; extra columns are preserved and can be"
                " used in eligibility predicates.",
            )
        elif column in seen_columns:
            ctx.error(
                f"{ROSTER_FILE}: (header)",
                f"column '{column}' appears twice (positions"
                f" {seen_columns[column] + 1} and {position + 1}).",
                "Column names are the attribute names eligibility rules reference, so"
                " they must be unique.",
            )
        else:
            seen_columns[column] = position

    rows: list[dict[str, str]] = []
    line_numbers: list[int] = []
    for record in reader:
        line = reader.line_num  # counts physical lines, so quoted newlines are handled
        if not record or all(not cell.strip() for cell in record):
            continue  # blank separator lines are common in hand-edited exports
        if len(record) != len(header):
            ctx.error(
                f"{ROSTER_FILE}: line {line}",
                f"has {len(record)} field(s) but the header declares {len(header)}.",
                "A value containing a comma must be quoted, e.g."
                ' "Chandrasekhar, Subrahmanyan". Header: ' + ",".join(header),
            )
            continue
        rows.append(dict(zip(header, record)))
        line_numbers.append(line)

    return (header, tuple(rows), tuple(line_numbers))


# --------------------------------------------------------------------------
# Building the dataclasses (validation has already passed)
# --------------------------------------------------------------------------


def _impossible(what: str) -> Any:
    """The validator was supposed to make this unreachable.

    Reached only if `validate.py` and this module have drifted apart. Failing
    loudly beats building a Config that looks fine and is not.
    """
    raise DeskMatchError(
        f"internal error: {what}. Validation should have rejected this input;"
        " deskmatch.validate and deskmatch.config have drifted apart."
    )


def _build_rooms(doc: Mapping[str, Any]) -> Rooms:
    zones: dict[str, Zone] = {}
    for zone_id, meta in doc["zones"].items():
        if str(zone_id).startswith("_"):
            continue
        color = meta.get("color")
        zones[zone_id] = Zone(
            id=zone_id,
            label=str(meta.get("label", zone_id)),
            **({"color": str(color)} if color is not None else {}),
        )

    rooms: list[Room] = []
    for room in doc["rooms"]:
        room_id = room["id"]
        desks = tuple(
            Desk(
                id=desk["id"],
                label=str(desk.get("label", desk["id"])),
                zone=desk["zone"],
                room_id=room_id,
                shape_kind=_shape_kind(desk["shape"]),
                shape=_shape_value(desk["shape"]),
                available=bool(desk.get("available", True)),
                notes=str(desk.get("notes", "")),
            )
            for desk in room["desks"]
        )
        # Features are decoration (walls, doors, rooms). Absent in schema v1,
        # so default to none rather than requiring the key.
        features = tuple(
            Feature(
                id=str(f["id"]),
                kind=str(f.get("kind", "")),
                label=str(f.get("label", "")),
                shape_kind=_shape_kind(f["shape"]),
                shape=_shape_value(f["shape"]),
                note=str(f.get("note", "")),
            )
            for f in room.get("features", ())
        )
        width, height = room["image_size"]
        rooms.append(
            Room(
                id=room_id,
                label=str(room.get("label", room_id)),
                image=str(room.get("image", "")),
                image_size=(int(width), int(height)),
                desks=desks,
                features=features,
            )
        )

    return Rooms(
        schema_version=int(doc["schema_version"]),
        coord_space=str(doc["coord_space"]),
        zones=zones,
        rooms=tuple(rooms),
    )


def _shape_kind(shape: Mapping[str, Any]) -> str:
    for kind in ("rect", "polygon", "polyline"):
        if kind in shape:
            return kind
    return _impossible("shape has none of 'rect', 'polygon', 'polyline'")


def _shape_value(
    shape: Mapping[str, Any],
) -> tuple[float, ...] | tuple[tuple[float, float], ...]:
    if "rect" in shape:
        return tuple(float(v) for v in shape["rect"])
    points = shape.get("polygon", shape.get("polyline"))
    return tuple((float(p[0]), float(p[1])) for p in points)


def _build_eligibility(doc: Mapping[str, Any]) -> Eligibility:
    rules = tuple(
        EligibilityRule(
            id=rule["id"],
            # `when` is kept verbatim (minus documentation keys): the predicate
            # grammar is open enough that normalising it here would lose meaning.
            when={k: v for k, v in rule["when"].items() if not str(k).startswith("_")},
            allow_zones=(
                "*"
                if isinstance(rule["allow_zones"], str)
                else tuple(rule["allow_zones"])
            ),
            reason=str(rule.get("reason", "")),
        )
        for rule in doc["rules"]
    )
    return Eligibility(schema_version=int(doc["schema_version"]), rules=rules)


def _build_roster(rows: Sequence[Mapping[str, str]]) -> Roster:
    people: list[Person] = []
    for row in rows:
        email = normalize_email(row.get("email", ""))
        year = parse_year(row.get("year", ""))
        if year is None:
            _impossible(f"roster year for {email!r} is not a positive integer")
        # A blank keeps_desk is warned about and treated as "no" by the validator;
        # keep the two in step here.
        keeps = parse_keeps_desk(row.get("keeps_desk", "")) or False
        current_desk = row.get("current_desk", "").strip()
        people.append(
            Person(
                email=email,
                name=row.get("name", "").strip() or email,
                year=int(year),  # type: ignore[arg-type]
                candidacy=row.get("candidacy", "").strip(),
                keeps_desk=keeps,
                # SPEC §2.3: current_desk only means anything when keeps_desk is
                # truthy. Dropping it otherwise makes the object say exactly what
                # the warning told the coordinator: the value is ignored.
                current_desk=current_desk if (keeps and current_desk) else None,
                attributes=dict(row),  # every column, verbatim, including extras
            )
        )
    # Sorted by email: the row order of a spreadsheet export must not be able to
    # influence any output (invariant I3).
    people.sort(key=lambda p: p.email)
    return Roster(people=tuple(people))


def _build_scoring(doc: Mapping[str, Any]) -> Scoring:
    curves = {
        name: tuple(curve_value_to_fraction(v) for v in values)
        for name, values in sorted(doc["curves"].items())
        if not str(name).startswith("_")
    }
    committed = doc.get("seed_committed_at")

    # seed_year resolution. "auto" means "the calendar year of this run", and it
    # is pinned HERE, once, at load time -- never read again deeper in the
    # pipeline. Everything downstream sees a fixed integer, so the solve stays a
    # pure function of its inputs (invariant I2) and re-running next January
    # still reproduces this year's published hash (I3).
    raw_year = doc.get("seed_year")
    seed_year: int | None = None
    from_clock = False
    if isinstance(raw_year, str) and raw_year.strip().casefold() == "auto":
        from datetime import datetime
        seed_year = datetime.now().year
        from_clock = True
    elif raw_year is not None:
        seed_year = int(raw_year)

    return Scoring(
        schema_version=int(doc["schema_version"]),
        curves=curves,
        primary_curve=str(doc["primary_curve"]),
        comparison_curves=tuple(doc.get("comparison_curves", ()) or ()),
        tie_break_seed=str(doc.get("tie_break_seed", "")),
        seed_committed_at=str(committed) if committed is not None else None,
        sensitivity_seeds=tuple(doc.get("sensitivity_seeds", ()) or ()),
        seed_year=seed_year,
        seed_year_from_clock=from_clock,
    )
