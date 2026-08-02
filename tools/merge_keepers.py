#!/usr/bin/env python3
"""Fold the pre-lock claim log (the ``Keepers`` sheet tab) into ``config/roster.csv``.

Why this exists
---------------
The form's pre-lock step lets a student claim the desk they already sit at and
keep it, instead of entering the ranking. That decision has to end up in the
roster, because ``keeps_desk`` / ``current_desk`` in ``config/roster.csv`` is
what the solver actually reads (SPEC §2.3, §3.4). Doing that by hand means the
coordinator emailing forty people one by one and typing forty desk ids, which is
the entire thing the pre-lock step was supposed to avoid. So:

    Keepers tab -> File -> Download -> CSV -> one command -> updated roster.

Semantics, matched to ``deskmatch.responses``
---------------------------------------------
The claim log is **append-only**, exactly like the response sheet. Releasing a
desk writes a new row with ``keeping=no`` rather than deleting anything, so the
history stays auditable. Resolution is therefore identical to SPEC §3.2 and to
``responses._resolve_latest``: **the latest row per email wins, ordered by
timestamp, ties broken by later file position.** That rule lives in one place
conceptually and is implemented the same way in both files on purpose — if you
change one, change the other.

What it refuses to do
---------------------
Nothing is written at all if any of these hold, because each one means the
roster you would get is wrong and a half-applied merge is worse than none:

  * two different people are keeping the same desk;
  * somebody claiming a desk is not on the roster;
  * a claimed or released desk id is not in ``rooms.json``.

Run ``--dry-run`` first. It prints exactly what would change and writes nothing.

Usage
-----
    python tools/merge_keepers.py --roster config/roster.csv \\
                                  --keepers keepers.csv --dry-run
    python tools/merge_keepers.py --roster config/roster.csv --keepers keepers.csv

Exit codes follow the rest of the toolchain: 0 success, 1 usage/IO, 4 the input
did not validate.

Standard library plus ``deskmatch`` for config loading and for the timestamp and
roster-vocabulary parsers, so this file cannot drift from how the solver reads
the same values.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, NoReturn, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
# The package lives in solver/, which is not necessarily installed.
sys.path.insert(0, str(REPO_ROOT / "solver"))

from deskmatch import eligibility                                  # noqa: E402
from deskmatch.config import load_config                           # noqa: E402
from deskmatch.responses import parse_timestamp                    # noqa: E402
from deskmatch.validate import (                                   # noqa: E402
    desk_ids_of,
    normalize_email,
    parse_keeps_desk,
    validate_rooms,
)

PROG = "merge_keepers"

#: Columns the claim log must have. The rest of the Keepers header
#: (``claim_id``, ``name``, ``client_version``) is audit trail: read if present,
#: never required, because a hand-built test file should still work.
REQUIRED_KEEPER_COLUMNS: tuple[str, ...] = ("timestamp", "email", "desk_id", "keeping")

#: Roster columns this tool writes. Everything else in the file is passed
#: through untouched, including columns this codebase has never heard of.
ROSTER_KEEPS = "keeps_desk"
ROSTER_DESK = "current_desk"
ROSTER_EMAIL = "email"

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_INVALID = 4


# ---------------------------------------------------------------------------
# Small reporting helpers
# ---------------------------------------------------------------------------


def die(message: str, code: int = EXIT_USAGE) -> NoReturn:
    print(f"{PROG}: error: {message}", file=sys.stderr)
    raise SystemExit(code)


def die_all(headline: str, problems: Sequence[str]) -> NoReturn:
    """Every problem at once. A coordinator merging on deadline night should get
    one list, not one error per run."""
    print(f"{PROG}: error: {headline}", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print(f"\n{PROG}: nothing was written.", file=sys.stderr)
    raise SystemExit(EXIT_INVALID)


def warn(message: str) -> None:
    print(f"{PROG}: warning: {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def read_text(path: Path, what: str) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        die(f"could not read the {what} at {path}: {exc}")
    # Excel and Sheets both like to prepend a BOM.
    return raw.decode("utf-8-sig", errors="replace")


def read_table(path: Path, what: str) -> tuple[list[str], list[dict[str, str]]]:
    """A CSV as (header, rows). Values are strings; missing cells become ''."""
    text = read_text(path, what)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        die(f"the {what} at {path} is empty; it needs at least a header row.")
    header = [(name or "").strip() for name in reader.fieldnames]
    rows: list[dict[str, str]] = []
    for raw_row in reader:
        row: dict[str, str] = {}
        for name in header:
            value = raw_row.get(name)
            row[name] = "" if value is None else str(value)
        rows.append(row)
    return header, rows


def load_desk_zones(rooms_path: Path) -> dict[str, str]:
    """Desk id -> zone id, from ``rooms.json``, validated by the solver's own code."""
    text = read_text(rooms_path, "rooms.json")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        die(f"{rooms_path} is not valid JSON: {exc}", EXIT_INVALID)

    ctx = validate_rooms(doc, rooms_path.parent)
    if ctx.problems:
        die_all(
            f"{rooms_path} does not validate, so desk ids cannot be checked:",
            [str(problem) for problem in ctx.problems],
        )
    ids = desk_ids_of(doc)
    if not ids:
        die(f"{rooms_path} declares no desks at all.", EXIT_INVALID)
    return {
        str(desk["id"]): str(desk.get("zone", ""))
        for room in doc.get("rooms", [])
        for desk in room.get("desks", [])
        if isinstance(desk, dict) and desk.get("id")
    }


# ---------------------------------------------------------------------------
# The claim log
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """One row of the Keepers tab, parsed."""

    line: int                 # 1-based line in the file, as a spreadsheet shows it
    file_row: int             # 0-based position among data rows; the tie-break
    email: str
    name: str
    desk_id: str
    keeping: bool
    when: dt.datetime         # aware, UTC


def parse_claims(path: Path) -> list[Claim]:
    header, rows = read_table(path, "keepers CSV")

    missing = [column for column in REQUIRED_KEEPER_COLUMNS if column not in header]
    if missing:
        die_all(
            f"{path}: the keepers CSV is missing required column(s).",
            [
                f"missing: {', '.join(missing)}",
                f"found: {', '.join(header) if header else '(nothing)'}",
                "Export the Keepers tab with File -> Download -> "
                "Comma Separated Values, header row included.",
            ],
        )

    claims: list[Claim] = []
    problems: list[str] = []

    for index, row in enumerate(rows):
        line = index + 2                       # +1 for the header, +1 for 1-based
        email = normalize_email(row.get(ROSTER_EMAIL, ""))
        if not email and not any(value.strip() for value in row.values()):
            continue                           # blank spacer row
        where = f"{path}: line {line}"
        if not email:
            problems.append(f"{where}: 'email' is empty.")
            continue

        keeping = parse_keeps_desk(row.get("keeping", ""))
        if keeping is None:
            problems.append(
                f"{where} ({email}): 'keeping' is "
                f"{row.get('keeping', '')!r}, which is not yes/no/true/false/1/0."
            )
            continue

        try:
            when, naive = parse_timestamp(row.get("timestamp", ""))
        except ValueError as exc:
            problems.append(f"{where} ({email}): 'timestamp' {exc}.")
            continue
        if naive:
            warn(
                f"{where} ({email}): the timestamp has no UTC offset; assuming UTC. "
                "Ordering between rows is unaffected."
            )

        claims.append(
            Claim(
                line=line,
                file_row=index,
                email=email,
                name=row.get("name", "").strip(),
                desk_id=row.get("desk_id", "").strip(),
                keeping=keeping,
                when=when,
            )
        )

    if problems:
        die_all(f"{path}: the keepers CSV has unreadable rows.", problems)
    return claims


def resolve_latest(claims: Iterable[Claim]) -> dict[str, Claim]:
    """SPEC §3.2, applied to claims: latest row per email wins, ordered by
    timestamp, ties broken by later file position.

    Deliberately the same shape as ``responses._resolve_latest``. Returned in
    sorted-email order so every downstream loop is deterministic without having
    to remember to sort.
    """
    best: dict[str, Claim] = {}
    for claim in claims:
        previous = best.get(claim.email)
        if previous is None or (claim.when, claim.file_row) > (previous.when, previous.file_row):
            best[claim.email] = claim
    return {email: best[email] for email in sorted(best)}


# ---------------------------------------------------------------------------
# The roster
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Vocabulary:
    """How this roster spells yes and no, so the merge does not reformat a file
    that says ``TRUE``/``FALSE`` into one that says ``yes``/``no``."""

    yes: str
    no: str

    @staticmethod
    def of(rows: Sequence[Mapping[str, str]]) -> "Vocabulary":
        yes_counts: dict[str, int] = {}
        no_counts: dict[str, int] = {}
        for row in rows:
            raw = row.get(ROSTER_KEEPS, "").strip()
            if not raw:
                continue
            parsed = parse_keeps_desk(raw)
            if parsed is True:
                yes_counts[raw] = yes_counts.get(raw, 0) + 1
            elif parsed is False:
                no_counts[raw] = no_counts.get(raw, 0) + 1

        def dominant(counts: Mapping[str, int], fallback: str) -> str:
            if not counts:
                return fallback
            # Ties broken alphabetically, so the choice is deterministic.
            return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

        return Vocabulary(yes=dominant(yes_counts, "yes"), no=dominant(no_counts, "no"))


@dataclass(frozen=True)
class Change:
    email: str
    name: str
    field: str
    before: str
    after: str


def index_roster(rows: Sequence[Mapping[str, str]], path: Path) -> dict[str, int]:
    """email -> row index. Duplicate emails are a roster bug the solver would
    also reject, so say so here rather than silently updating one of them."""
    index: dict[str, int] = {}
    duplicates: list[str] = []
    for position, row in enumerate(rows):
        email = normalize_email(row.get(ROSTER_EMAIL, ""))
        if not email:
            continue
        if email in index:
            duplicates.append(
                f"{email} appears on lines {index[email] + 2} and {position + 2}"
            )
            continue
        index[email] = position
    if duplicates:
        die_all(f"{path}: 'email' is the roster's primary key and must be unique.", duplicates)
    return index


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def warn_out_of_zone(
    latest: Mapping[str, Claim],
    roster_rows: Sequence[Mapping[str, str]],
    roster_index: Mapping[str, int],
    desk_zones: Mapping[str, str],
    config_dir: Path,
) -> None:
    """Flag a claim on a desk the claimer could not have been *assigned*.

    This is a warning, never an error. Keeping the desk you already sit at is
    not an assignment, and `keeps_desk` has never been zone-checked -- a
    pre-candidate who is already in the upper-years room is not doing anything
    wrong by staying there.

    But it is worth saying out loud. The zone exists because the cohort is meant
    to sit together, and pre-lock is precisely the change that stops the
    coordinator from hearing about each of these in person. If nobody printed
    it here, nobody would find out.
    """
    try:
        config = load_config(config_dir)
    except Exception:
        return  # config problems are reported elsewhere; do not double up

    for email in sorted(latest):
        claim = latest[email]
        if not claim.keeping or email not in roster_index:
            continue
        zone = desk_zones.get(claim.desk_id, "")
        person = config.roster.by_email(email)
        if person is None or not zone:
            continue
        try:
            allowed = eligibility.allowed_zones(config.eligibility, config.rooms, person)
        except Exception:
            continue
        if zone in allowed:
            continue
        zone_label = getattr(config.rooms.zones.get(zone), "label", zone)
        allowed_labels = ", ".join(
            getattr(config.rooms.zones.get(z), "label", z) for z in allowed
        ) or "(none)"
        who = f"{claim.name} <{email}>" if claim.name else email
        warn(
            f"{who} is keeping {claim.desk_id}, which is in '{zone_label}'. The "
            f"eligibility rules would only ever ASSIGN them to: {allowed_labels}. "
            f"Keeping a desk you already occupy is allowed and this is not an "
            f"error -- but the zone exists for a reason, so check this is what "
            f"you intend."
        )


def validate(
    latest: Mapping[str, Claim],
    roster_rows: Sequence[Mapping[str, str]],
    roster_index: Mapping[str, int],
    desk_ids: frozenset[str],
    keepers_path: Path,
) -> None:
    """Every reason not to write, collected and reported together."""
    problems: list[str] = []

    # --- a claimer must be on the roster ---------------------------------
    for email, claim in latest.items():
        if email in roster_index:
            continue
        who = f"{claim.name} <{email}>" if claim.name else email
        problems.append(
            f"{keepers_path}: line {claim.line}: {who} "
            f"{'claims' if claim.keeping else 'releases'} {claim.desk_id or '(no desk)'}, "
            "but is not on the roster. Add them to config/roster.csv first, or remove "
            "the row if it was a mistake."
        )

    # --- desk ids must exist ---------------------------------------------
    for email, claim in latest.items():
        if not claim.desk_id:
            problems.append(
                f"{keepers_path}: line {claim.line} ({email}): 'desk_id' is empty."
            )
        elif claim.desk_id not in desk_ids:
            problems.append(
                f"{keepers_path}: line {claim.line} ({email}): desk "
                f"{claim.desk_id!r} is not in rooms.json. Known desk ids: "
                f"{', '.join(sorted(desk_ids))}."
            )

    # --- one desk, one keeper --------------------------------------------
    by_desk: dict[str, list[Claim]] = {}
    for email in sorted(latest):
        claim = latest[email]
        if claim.keeping and claim.desk_id:
            by_desk.setdefault(claim.desk_id, []).append(claim)
    for desk_id in sorted(by_desk):
        holders = by_desk[desk_id]
        if len(holders) > 1:
            who = "; ".join(
                f"{c.name or c.email} (line {c.line})" for c in holders
            )
            problems.append(
                f"{desk_id} is claimed by {len(holders)} people: {who}. "
                "Only one of them can keep it — decide which, add a releasing row "
                "(keeping=no) for the other, and re-export."
            )

    # --- and nobody may claim a desk the roster already gives someone else -
    for desk_id in sorted(by_desk):
        claimer = by_desk[desk_id][0]
        for position, row in enumerate(roster_rows):
            email = normalize_email(row.get(ROSTER_EMAIL, ""))
            if email == claimer.email:
                continue
            if not parse_keeps_desk(row.get(ROSTER_KEEPS, "")):
                continue
            if row.get(ROSTER_DESK, "").strip() != desk_id:
                continue
            # ...unless that person is releasing it in this very merge.
            other = latest.get(email)
            if other is not None and not other.keeping:
                continue
            problems.append(
                f"{desk_id} is claimed by {claimer.name or claimer.email} "
                f"(line {claimer.line}), but the roster already has "
                f"{row.get('name', '').strip() or email} keeping it "
                f"(roster line {position + 2}). Fix the roster or the claim."
            )

    if problems:
        die_all("the claims cannot be merged.", problems)


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def apply_claims(
    roster_rows: Sequence[Mapping[str, str]],
    roster_index: Mapping[str, int],
    latest: Mapping[str, Claim],
    vocabulary: Vocabulary,
) -> tuple[list[dict[str, str]], list[Change]]:
    """The new roster and the diff. Row order and every other column survive."""
    updated = [dict(row) for row in roster_rows]
    changes: list[Change] = []

    for email in sorted(latest):
        claim = latest[email]
        row = updated[roster_index[email]]
        name = row.get("name", "").strip() or claim.name or email

        want_keeps = vocabulary.yes if claim.keeping else vocabulary.no
        want_desk = claim.desk_id if claim.keeping else ""

        for field, want in ((ROSTER_KEEPS, want_keeps), (ROSTER_DESK, want_desk)):
            before = row.get(field, "")
            if before == want:
                continue
            row[field] = want
            changes.append(
                Change(email=email, name=name, field=field, before=before, after=want)
            )

    return updated, changes


def write_roster(path: Path, header: Sequence[str], rows: Sequence[Mapping[str, str]]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=list(header), lineterminator="\n", extrasaction="ignore"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in header})
    try:
        path.write_text(buffer.getvalue(), encoding="utf-8", newline="")
    except OSError as exc:
        die(f"could not write {path}: {exc}")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def show(value: str) -> str:
    return repr(value) if value == "" or value != value.strip() else value


def print_diff(changes: Sequence[Change]) -> None:
    by_email: dict[str, list[Change]] = {}
    for change in changes:
        by_email.setdefault(change.email, []).append(change)
    for email in sorted(by_email):
        group = by_email[email]
        print(f"  {group[0].name} <{email}>")
        for change in group:
            print(f"      {change.field}: {show(change.before)} -> {show(change.after)}")


def summarise(latest: Mapping[str, Claim], changes: Sequence[Change]) -> None:
    claiming = sorted(e for e, c in latest.items() if c.keeping)
    releasing = sorted(e for e, c in latest.items() if not c.keeping)
    people = len({change.email for change in changes})
    print(
        f"\n{len(latest)} person(s) acted: {len(claiming)} keeping a desk, "
        f"{len(releasing)} releasing one."
    )
    print(f"{people} roster row(s) would change ({len(changes)} field(s)).")
    if claiming:
        kept = ", ".join(f"{latest[e].desk_id}" for e in claiming)
        print(f"Desks removed from the pool: {kept}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(__doc__ or "").strip().split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Run with --dry-run first. It prints the diff and writes nothing;\n"
            "that is the recommended first step in frontend/DEPLOY.md."
        ),
    )
    parser.add_argument(
        "--roster", required=True, help="config/roster.csv — updated in place"
    )
    parser.add_argument(
        "--keepers", required=True, help="the Keepers tab, exported as CSV"
    )
    parser.add_argument(
        "--rooms",
        default=None,
        help="rooms.json, for checking desk ids (default: alongside the roster)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="write the merged roster here instead of over --roster",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would change and write nothing",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    roster_path = Path(args.roster)
    keepers_path = Path(args.keepers)
    rooms_path = Path(args.rooms) if args.rooms else roster_path.parent / "rooms.json"
    out_path = Path(args.out) if args.out else roster_path

    if not roster_path.exists():
        die(f"no such roster: {roster_path}")
    if not keepers_path.exists():
        die(f"no such keepers CSV: {keepers_path}")
    if not rooms_path.exists():
        die(
            f"no such rooms file: {rooms_path}. Pass --rooms if it does not sit "
            f"next to the roster."
        )

    desk_zones = load_desk_zones(rooms_path)
    desk_ids = frozenset(desk_zones)
    roster_header, roster_rows = read_table(roster_path, "roster")
    for column in (ROSTER_EMAIL, ROSTER_KEEPS, ROSTER_DESK):
        if column not in roster_header:
            die(
                f"{roster_path}: the roster has no {column!r} column, so there is "
                f"nowhere to record who is keeping a desk (SPEC §2.3). "
                f"Found: {', '.join(roster_header)}.",
                EXIT_INVALID,
            )

    roster_index = index_roster(roster_rows, roster_path)
    claims = parse_claims(keepers_path)
    latest = resolve_latest(claims)

    superseded = len(claims) - len(latest)
    print(
        f"{keepers_path}: {len(claims)} claim row(s), "
        f"{len(latest)} after resolving the latest per email"
        + (f" ({superseded} superseded)" if superseded else "")
        + "."
    )

    if not latest:
        print("Nothing to merge.")
        return EXIT_OK

    validate(latest, roster_rows, roster_index, desk_ids, keepers_path)
    warn_out_of_zone(latest, roster_rows, roster_index, desk_zones, rooms_path.parent)

    vocabulary = Vocabulary.of(roster_rows)
    updated, changes = apply_claims(roster_rows, roster_index, latest, vocabulary)

    if not changes:
        summarise(latest, changes)
        print(f"\n{roster_path} already says all of this. Nothing written.")
        return EXIT_OK

    print("\nChanges:")
    print_diff(changes)
    summarise(latest, changes)

    if args.dry_run:
        print("\n--dry-run: nothing was written. Re-run without it to apply.")
        return EXIT_OK

    write_roster(out_path, roster_header, updated)
    print(f"\nWrote {out_path}.")
    print("Commit it, then re-run the solver.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
