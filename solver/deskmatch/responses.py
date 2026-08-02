"""Response ingest: CSV/JSON -> types.Responses (SPEC §3).

This is the Component A ↔ Component B seam. Everything downstream runs from the
object this module produces, with no Google dependency, so the parsing rules here
*are* the contract.

Three things in here are load-bearing and easy to get subtly wrong:

  1. **K is discovered, never assumed** (invariant I1). The header is scanned for
     a contiguous `choice_1..choice_N` run; N is what the file says. If that
     disagrees with `len(scoring.curves[primary])`, the run stops and says which
     number came from which file. "5" appears nowhere in this module.

  2. **Every problem is collected, then reported once.** A coordinator exporting
     a 60-row sheet at 4pm on the deadline should get all 11 bad rows in one
     message, not eleven edit-run cycles. A malformed row is recorded and parsing
     continues; nothing here raises on the first fault, and nothing lets a raw
     KeyError/ValueError reach the console.

  3. **Determinism** (I2/I3). No set is iterated; `latest` is built in sorted
     email order; the clock is never read; timestamps are compared as aware UTC
     but stored exactly as submitted so the round-trip is byte-stable.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from . import types
from .errors import Problem as Complaint  # errors.Problem is a *validation complaint*;
from .errors import ResponseError        # types.Problem is the solve matrix. Alias to keep them apart.
from .provenance import sha256_bytes

# --------------------------------------------------------------------------
# Schema (SPEC §3.1)
# --------------------------------------------------------------------------

#: Columns without which the file cannot be interpreted at all.
REQUIRED_COLUMNS: tuple[str, ...] = (
    "submission_id",
    "timestamp",
    "email",
    "name",
    "candidacy",
)

#: `year` used to be required. It is now OPTIONAL, because candidacy alone
#: decides which zones a person may sit in and the form no longer asks for the
#: year. Files that still carry the column are read (older exports, and the
#: coordinator may find it handy in the coordinator report), but the value is
#: recorded and never used for eligibility. A missing year becomes 0.
OPTIONAL_COLUMNS: tuple[str, ...] = ("year",)

#: Columns SPEC §3.1 lists but which are audit-only and "never affect the solve".
#: `types.Submission` gives both of them a default of "", which is the data model
#: saying out loud that a file without them is still usable — so their absence is
#: a warning, not an error. A hand-built test fixture should not be rejected for
#: lacking a frontend build id.
AUDIT_COLUMNS: tuple[str, ...] = ("client_version", "auth_method")

_CHOICE_RE = re.compile(r"^choice_(\d+)$")

#: Timezone assumed for timestamps that carry no UTC offset.
#:
#: SPEC §3.1 requires an offset. Google Sheets' CSV export drops it, and telling a
#: coordinator "re-export your sheet" at deadline is not a plan, so offset-less
#: timestamps are accepted — loudly. UTC is the assumption because it is the one
#: choice that cannot be silently wrong in an interesting way: the *relative*
#: order of offset-less rows (which is all that latest-per-email needs) is
#: identical under any single fixed offset, so this assumption can only matter in
#: a file that MIXES offset-bearing and offset-less rows, and that case gets its
#: own warning. Pass `assume_timezone=` to override.
DEFAULT_NAIVE_TIMEZONE: dt.tzinfo = dt.timezone.utc

#: Offset-less formats accepted in addition to whatever `datetime.fromisoformat`
#: handles (which already covers "YYYY-MM-DD HH:MM:SS" and the "T" variants).
#: These are the US-locale shapes a Google Sheet produces when the timestamp
#: column has been reformatted by hand.
_NAIVE_FALLBACK_FORMATS: tuple[str, ...] = (
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%y %H:%M:%S",
    "%m/%d/%y %H:%M",
    "%m/%d/%Y",
)


def canonical_header(k: int) -> tuple[str, ...]:
    """The canonical column order for a response file with K choices (SPEC §3.1)."""
    return (
        *REQUIRED_COLUMNS,
        *OPTIONAL_COLUMNS,
        *(f"choice_{i}" for i in range(1, k + 1)),
        *AUDIT_COLUMNS,
    )


@dataclass(frozen=True)
class LoadedResponses:
    """`load_responses()` plus the bits that do not fit in `types.Responses`.

    `types.Responses`/`types.Submission` are a fixed, frozen contract with no
    room for arbitrary columns, and adding one would change the data model for
    every other module. SPEC §2.3 says extra columns are *preserved*, so they are
    preserved here instead, keyed by `submission_id`, and `write_responses()` can
    re-emit them.
    """

    responses: types.Responses
    extra_columns: tuple[str, ...]                          # sorted, deterministic
    extra_values: Mapping[str, Mapping[str, str]]           # submission_id -> col -> value
    header: tuple[str, ...]                                 # the header exactly as read


# --------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------


def _tz_name(tz: dt.tzinfo) -> str:
    return getattr(tz, "key", None) or str(tz)


def parse_timestamp(
    raw: str,
    *,
    assume_timezone: dt.tzinfo = DEFAULT_NAIVE_TIMEZONE,
) -> tuple[dt.datetime, bool]:
    """Parse a submission timestamp. Returns `(aware_utc_datetime, was_naive)`.

    Raises ValueError with a human-readable message; callers turn that into a
    collected complaint rather than letting it escape.
    """
    text = raw.strip()
    if not text:
        raise ValueError("is empty; an ISO-8601 timestamp with a UTC offset is required")

    # 'Z' is legal ISO-8601 and is what most exporters emit; fromisoformat has
    # accepted it since 3.11 but normalising first keeps the intent explicit.
    candidate = text[:-1] + "+00:00" if text[-1] in "Zz" else text

    parsed: dt.datetime | None = None
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in _NAIVE_FALLBACK_FORMATS:
            try:
                parsed = dt.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        raise ValueError(
            f"{text!r} is not a recognisable timestamp; expected ISO-8601 with a "
            f"UTC offset such as '2026-09-15T14:03:22-04:00'"
        )

    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return parsed.replace(tzinfo=assume_timezone).astimezone(dt.timezone.utc), True
    return parsed.astimezone(dt.timezone.utc), False


# --------------------------------------------------------------------------
# Reading + format detection
# --------------------------------------------------------------------------


def _read_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        # Even "file not found" goes through ResponseError: a raw traceback for
        # something the coordinator can fix by checking a filename is a bug here.
        raise ResponseError(
            [
                Complaint(
                    where=path,
                    what=f"cannot be read ({exc.strerror or exc})",
                    hint="Check the path and that the response export actually exists.",
                )
            ]
        ) from exc


def _decode(raw: bytes, path: str) -> str:
    try:
        # utf-8-sig, not utf-8: Google Sheets prefixes its CSV export with a BOM,
        # which would otherwise turn the first header into '﻿submission_id'
        # and produce a baffling "missing column 'submission_id'".
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ResponseError(
            [
                Complaint(
                    where=f"{path}: byte {exc.start}",
                    what="the file is not valid UTF-8",
                    hint="Re-export as UTF-8 CSV; Excel's default 'CSV' on Windows "
                         "is often cp1252 and mangles non-ASCII names.",
                )
            ]
        ) from exc


def _json_cell(value: Any) -> str | None:
    """Coerce one JSON value to the string form the CSV path validates.

    Returns None for a structure (list/dict) that has no sensible cell form; the
    caller turns that into a complaint.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # Spreadsheet exports love turning 3 into 3.0; keep it as an int-looking
        # string so `year` validation does not have to special-case it twice.
        return str(int(value)) if value.is_integer() else repr(value)
    return None


def _parse_csv(
    text: str, path: str, problems: list[Complaint], warnings: list[str]
) -> tuple[tuple[str, ...], list[tuple[int, dict[str, str]]]]:
    """-> (header, [(line_number, {column: value}), ...]) in file order."""
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header_row = next(reader)
    except StopIteration:
        problems.append(
            Complaint(
                where=path,
                what="the file is empty",
                hint="A response file needs at least a header row.",
            )
        )
        return (), []

    header = tuple(cell.strip() for cell in header_row)
    rows: list[tuple[int, dict[str, str]]] = []
    for cells in reader:
        line_no = reader.line_num
        if not cells or all(not cell.strip() for cell in cells):
            # A trailing newline yields [] and is normal; a row of nothing but
            # commas is a stray blank line in the sheet. Neither is an error —
            # refusing to run over a blank line in a spreadsheet would be absurd —
            # but the second one is worth saying out loud.
            if cells:
                warnings.append(
                    f"{path}: line {line_no}: the row is entirely blank and has "
                    f"been skipped. Delete the empty row in the sheet to silence this."
                )
            continue
        if len(cells) != len(header):
            problems.append(
                Complaint(
                    where=f"{path}: line {line_no}",
                    what=f"has {len(cells)} fields but the header has {len(header)}",
                    hint="A quoted field probably contains an unescaped newline or "
                         "comma. Re-export rather than hand-editing.",
                )
            )
        padded = list(cells) + [""] * max(0, len(header) - len(cells))
        rows.append((line_no, {col: padded[i] for i, col in enumerate(header)}))
    return header, rows


def _parse_json(
    text: str, path: str, problems: list[Complaint], warnings: list[str]
) -> tuple[tuple[str, ...], list[tuple[int, dict[str, str]]]]:
    """JSON form: a list of row objects carrying the same keys as the CSV header."""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        problems.append(
            Complaint(
                where=f"{path}: line {exc.lineno}, column {exc.colno}",
                what=f"is not valid JSON — {exc.msg}",
            )
        )
        return (), []

    if not isinstance(doc, list):
        problems.append(
            Complaint(
                where=path,
                what=f"the top level is a {type(doc).__name__}, not a list",
                hint="The JSON form of a response file is a list of row objects, "
                     "one per submission, with the same keys as the CSV columns.",
            )
        )
        return (), []
    if not doc:
        problems.append(
            Complaint(where=path, what="contains no submissions (the list is empty)")
        )
        return (), []

    first = doc[0]
    if not isinstance(first, dict):
        problems.append(
            Complaint(
                where=f"{path}: [0]",
                what=f"is a {type(first).__name__}, not an object",
                hint="Every element must be an object mapping column name -> value.",
            )
        )
        return (), []

    # Key order in the first object defines the header, mirroring how a CSV's
    # first line does. json preserves object order, so this is deterministic.
    header = tuple(str(key).strip() for key in first)
    header_set = set(header)

    rows: list[tuple[int, dict[str, str]]] = []
    for index, item in enumerate(doc):
        if not isinstance(item, dict):
            problems.append(
                Complaint(
                    where=f"{path}: [{index}]",
                    what=f"is a {type(item).__name__}, not an object",
                )
            )
            continue
        keys = {str(key).strip() for key in item}
        missing = sorted(header_set - keys)
        surplus = sorted(keys - header_set)
        if missing or surplus:
            detail = []
            if missing:
                detail.append("missing " + ", ".join(repr(k) for k in missing))
            if surplus:
                detail.append("has unexpected " + ", ".join(repr(k) for k in surplus))
            problems.append(
                Complaint(
                    where=f"{path}: [{index}]",
                    what="has different keys from the first object: " + "; ".join(detail),
                    hint="Every row object must carry the same keys, exactly as a "
                         "CSV has one fixed header.",
                )
            )
        cells: dict[str, str] = {}
        for key, value in item.items():
            cell = _json_cell(value)
            if cell is None:
                problems.append(
                    Complaint(
                        where=f"{path}: [{index}], key '{key}'",
                        what=f"holds a {type(value).__name__}, which is not a cell value",
                        hint="Values must be strings, numbers, booleans or null.",
                    )
                )
                cell = ""
            cells[str(key).strip()] = cell
        rows.append((index, cells))
    return header, rows


# --------------------------------------------------------------------------
# Header validation and K discovery
# --------------------------------------------------------------------------


def _discover_k(header: Sequence[str], path: str, problems: list[Complaint]) -> int:
    """Count the contiguous `choice_1..choice_N` run in the header. Never 5 by fiat."""
    ranks: dict[int, int] = {}
    for column in header:
        match = _CHOICE_RE.match(column)
        if match:
            ranks[int(match.group(1))] = ranks.get(int(match.group(1)), 0) + 1

    nonpositive = sorted(r for r in ranks if r < 1)
    if nonpositive:
        problems.append(
            Complaint(
                where=f"{path}: header",
                what="choice columns are numbered from 1; found "
                     + ", ".join(f"'choice_{r}'" for r in nonpositive),
            )
        )

    positive = {r: n for r, n in ranks.items() if r >= 1}
    if not positive:
        problems.append(
            Complaint(
                where=f"{path}: header",
                what="no 'choice_N' columns found",
                hint="A response file must have choice_1, choice_2, ... one per "
                     "rank. The number of them is K; it is read from this header, "
                     "not assumed.",
            )
        )
        return 0

    if 1 not in positive:
        problems.append(
            Complaint(
                where=f"{path}: header",
                what=f"choice columns start at 'choice_{min(positive)}'; they must "
                     f"be contiguous starting at 'choice_1'",
            )
        )
        return 0

    k = 0
    while (k + 1) in positive:
        k += 1

    stray = sorted(r for r in positive if r > k)
    if stray:
        problems.append(
            Complaint(
                where=f"{path}: header",
                what=f"choice columns are not contiguous: 'choice_{k + 1}' is missing "
                     f"but 'choice_{stray[0]}' is present",
                hint=f"Columns run choice_1..choice_{k}, then jump to "
                     + ", ".join(f"choice_{r}" for r in stray)
                     + ". Rename or add the missing rank.",
            )
        )
    return k


def _validate_header(
    header: Sequence[str],
    path: str,
    problems: list[Complaint],
    warnings: list[str],
) -> tuple[str, ...]:
    """Check required/duplicate/extra columns. Returns the sorted extra columns."""
    seen: dict[str, int] = {}
    for column in header:
        seen[column] = seen.get(column, 0) + 1
    duplicates = sorted(col for col, n in seen.items() if n > 1)
    for column in duplicates:
        problems.append(
            Complaint(
                where=f"{path}: header",
                what=f"column '{column}' appears {seen[column]} times",
                hint="Column names must be unique; otherwise which one holds the "
                     "value is undefined.",
            )
        )

    for column in REQUIRED_COLUMNS:
        if column not in seen:
            problems.append(
                Complaint(
                    where=f"{path}: header",
                    what=f"required column '{column}' is missing",
                    hint="Required columns (SPEC §3.1): "
                         + ", ".join(REQUIRED_COLUMNS)
                         + ", then choice_1..choice_K.",
                )
            )

    for column in AUDIT_COLUMNS:
        if column not in seen:
            warnings.append(
                f"{path}: header: audit column '{column}' is missing. It never "
                f"affects the solve, so the run continues with an empty value, but "
                f"the audit trail is thinner than SPEC §3.1 expects."
            )

    known = set(REQUIRED_COLUMNS) | set(OPTIONAL_COLUMNS) | set(AUDIT_COLUMNS)
    extras = tuple(
        sorted(
            column
            for column in seen
            if column and column not in known and not _CHOICE_RE.match(column)
        )
    )
    if extras:
        warnings.append(
            f"{path}: header: {len(extras)} column(s) are not part of SPEC §3.1 and "
            f"are preserved but unused: " + ", ".join(extras) + "."
        )
    if "" in seen:
        warnings.append(
            f"{path}: header: {seen['']} column(s) have an empty name and are ignored."
        )
    return extras


# --------------------------------------------------------------------------
# Row validation
# --------------------------------------------------------------------------


def _parse_year(text: str) -> int:
    """`year` as an int. Accepts the '4.0' a spreadsheet produces for an integer."""
    stripped = text.strip()
    try:
        return int(stripped)
    except ValueError:
        pass
    value = float(stripped)   # raises ValueError, caught by the caller
    if not value.is_integer():
        raise ValueError(f"{stripped!r} is not a whole number")
    return int(value)


def _row_where(path: str, is_json: bool, locator: int, email: str) -> str:
    """Location string for a complaint. Line numbers for CSV (so it matches what
    the sheet shows), list indices for JSON."""
    base = f"{path}: [{locator}]" if is_json else f"{path}: line {locator}"
    return f"{base} ({email})" if email else base


def _build_submission(
    cells: Mapping[str, str],
    *,
    k: int,
    file_row: int,
    locator: int,
    path: str,
    is_json: bool,
    assume_timezone: dt.tzinfo,
    absent_columns: frozenset[str],
    problems: list[Complaint],
    warnings: list[str],
) -> tuple[types.Submission | None, dt.datetime | None, bool]:
    """Validate one row. Returns `(submission | None, utc_timestamp | None, was_naive)`.

    Never raises: every fault becomes a complaint so the remaining rows still get
    parsed and the coordinator sees the whole picture at once.

    `absent_columns` are required columns the header does not have at all. Their
    per-row checks are skipped: the header complaint already says everything
    useful, and repeating "'year' is empty" once per row for 60 rows buries the
    one line that tells the coordinator they named the column "yr".
    """
    email = cells.get("email", "").strip().lower()   # SPEC §3.1: lower-cased on ingest
    where = _row_where(path, is_json, locator, email)
    ok = True

    if not email:
        if "email" not in absent_columns:
            problems.append(
                Complaint(
                    where=where,
                    what="'email' is empty",
                    hint="email is the primary key that joins a submission to the "
                         "roster.",
                )
            )
        ok = False

    submission_id = cells.get("submission_id", "").strip()
    if not submission_id:
        if "submission_id" not in absent_columns:
            problems.append(
                Complaint(
                    where=where,
                    what="'submission_id' is empty",
                    hint="Each row needs a unique id; the frontend uses "
                         "Utilities.getUuid().",
                )
            )
        ok = False

    timestamp_raw = cells.get("timestamp", "")
    utc: dt.datetime | None = None
    was_naive = False
    try:
        utc, was_naive = parse_timestamp(timestamp_raw, assume_timezone=assume_timezone)
    except ValueError as exc:
        if "timestamp" not in absent_columns:
            problems.append(
                Complaint(
                    where=where,
                    what=f"'timestamp' {exc}",
                    hint="Re-submission ordering depends on this field; it cannot be "
                         "guessed.",
                )
            )
        ok = False

    # `year` is optional and informational only (see OPTIONAL_COLUMNS). An
    # unparseable value is a warning rather than an error: refusing to run the
    # whole department's assignment over a field that cannot affect the outcome
    # would be the wrong trade.
    year = 0
    raw_year = cells.get("year", "").strip()
    if raw_year:
        try:
            year = _parse_year(raw_year)
        except ValueError:
            warnings.append(
                f"{where}: 'year' is {raw_year!r}, which is not an integer. It is "
                f"recorded as 0 and ignored -- eligibility is decided by candidacy."
            )

    candidacy = cells.get("candidacy", "").strip()
    if not candidacy and "candidacy" not in absent_columns:
        warnings.append(
            f"{where}: 'candidacy' is empty. Per SPEC §3.3 the submission "
            f"overrides the roster, so this would blank out the roster value and "
            f"change which eligibility rule matches. Fix the export or the form."
        )

    choices: list[str] = []
    seen_choice: dict[str, int] = {}
    for rank in range(1, k + 1):
        column = f"choice_{rank}"
        value = cells.get(column, "").strip()
        if not value:
            problems.append(
                Complaint(
                    where=where,
                    what=f"'{column}' is empty",
                    hint=f"Exactly {k} distinct desk ids are required "
                         f"(K={k} comes from this file's header).",
                )
            )
            ok = False
        elif value in seen_choice:
            problems.append(
                Complaint(
                    where=where,
                    what=f"desk '{value}' is ranked twice, at choice_"
                         f"{seen_choice[value]} and {column}",
                    hint="The K choices must be distinct.",
                )
            )
            ok = False
        else:
            seen_choice[value] = rank
        choices.append(value)

    if not ok:
        return None, utc, was_naive

    assert utc is not None   # ok implies the timestamp parsed
    return (
        types.Submission(
            submission_id=submission_id,
            timestamp=cells.get("timestamp", "").strip(),   # kept exactly as submitted
            email=email,
            name=cells.get("name", "").strip(),
            year=year,
            candidacy=candidacy,
            choices=tuple(choices),
            client_version=cells.get("client_version", "").strip(),
            auth_method=cells.get("auth_method", "").strip(),
            file_row=file_row,
        ),
        utc,
        was_naive,
    )


# --------------------------------------------------------------------------
# Public loader
# --------------------------------------------------------------------------


def load_responses(
    path: str | os.PathLike[str],
    k: int | None,
    *,
    assume_timezone: dt.tzinfo = DEFAULT_NAIVE_TIMEZONE,
    k_source: str = "scoring.json",
) -> types.Responses:
    """Load a `.csv` or `.json` response export (SPEC §3).

    `k` is K as derived from the config (`len(scoring.curves[primary_curve])`).
    Pass None to adopt whatever the file's header declares — useful for tooling
    that has no config to hand; the solve path always passes the real K so the
    two are cross-checked.

    Raises `ResponseError` carrying every problem found, never just the first.
    """
    return load_responses_ex(
        path, k, assume_timezone=assume_timezone, k_source=k_source
    ).responses


def load_responses_ex(
    path: str | os.PathLike[str],
    k: int | None,
    *,
    assume_timezone: dt.tzinfo = DEFAULT_NAIVE_TIMEZONE,
    k_source: str = "scoring.json",
) -> LoadedResponses:
    """`load_responses()` plus the preserved extra columns. See `LoadedResponses`."""
    text_path = os.fspath(path)
    suffix = os.path.splitext(text_path)[1].lower()
    if suffix not in (".csv", ".json"):
        raise ResponseError(
            [
                Complaint(
                    where=text_path,
                    what=f"has extension {suffix or '(none)'}, which is not a "
                         f"supported response format",
                    hint="Use '.csv' (the Google Sheets export) or '.json' (a list "
                         "of row objects with the same keys).",
                )
            ]
        )

    raw = _read_bytes(text_path)
    # Hash the RAW bytes, before any decoding or BOM stripping: this is the value
    # a second person reproduces with `sha256sum` on the committed export.
    digest = sha256_bytes(raw)
    text = _decode(raw, text_path)

    problems: list[Complaint] = []
    warnings: list[str] = []
    is_json = suffix == ".json"

    # Structural (row-shape) faults are collected separately so the final report
    # reads top-down: header problems, then file-structure problems, then rows in
    # file order. Whoever is fixing the export reads it in that order too.
    structural: list[Complaint] = []
    if is_json:
        header, rows = _parse_json(text, text_path, structural, warnings)
    else:
        header, rows = _parse_csv(text, text_path, structural, warnings)

    if not header:
        raise ResponseError(structural)

    extras = _validate_header(header, text_path, problems, warnings)
    discovered_k = _discover_k(header, text_path, problems)
    absent_columns = frozenset(c for c in REQUIRED_COLUMNS if c not in set(header))

    if discovered_k > 0 and k is not None and discovered_k != k:
        problems.append(
            Complaint(
                where=f"{text_path}: header",
                what=f"declares K={discovered_k} (it has choice_1..choice_"
                     f"{discovered_k}), but K={k} is required",
                hint=f"K={k} comes from {k_source}: it is "
                     f"len(curves[primary_curve]). K={discovered_k} comes from the "
                     f"header of {text_path}. Change the curve lengths in "
                     f"{k_source} or re-export the form with "
                     f"{k} choice columns — the two must agree.",
            )
        )

    problems.extend(structural)

    if discovered_k <= 0:
        # Without choice columns there is nothing row-level worth checking; the
        # header complaints already say everything useful.
        raise ResponseError(problems)

    submissions: list[types.Submission] = []
    utc_by_id: dict[str, dt.datetime] = {}
    naive_locators: list[int] = []
    aware_count = 0
    ids_seen: dict[str, int] = {}

    for file_row, (locator, cells) in enumerate(rows):
        try:
            submission, utc, was_naive = _build_submission(
                cells,
                k=discovered_k,
                file_row=file_row,
                locator=locator,
                path=text_path,
                is_json=is_json,
                assume_timezone=assume_timezone,
                absent_columns=absent_columns,
                problems=problems,
                warnings=warnings,
            )
        except Exception as exc:  # pragma: no cover - defensive
            # A row must never be able to abort the parse of later rows. Anything
            # _build_submission failed to anticipate lands here as a complaint.
            problems.append(
                Complaint(
                    where=_row_where(text_path, is_json, locator, ""),
                    what=f"could not be parsed: {type(exc).__name__}: {exc}",
                    hint="This is a bug in deskmatch.responses; please report the "
                         "row that triggered it.",
                )
            )
            continue

        if utc is not None:
            if was_naive:
                naive_locators.append(locator)
            else:
                aware_count += 1
        if submission is None:
            continue

        previous = ids_seen.get(submission.submission_id)
        if previous is not None:
            problems.append(
                Complaint(
                    where=_row_where(
                        text_path, is_json, locator, submission.email
                    ),
                    what=f"'submission_id' {submission.submission_id!r} was already "
                         f"used at {'[' + str(previous) + ']' if is_json else 'line ' + str(previous)}",
                    hint="submission_id must be unique per row; superseded-row "
                         "tracking keys off it.",
                )
            )
            continue
        ids_seen[submission.submission_id] = locator

        submissions.append(submission)
        utc_by_id[submission.submission_id] = utc   # type: ignore[assignment]

    if naive_locators:
        label = "items" if is_json else "lines"
        warnings.append(
            f"{text_path}: {len(naive_locators)} timestamp(s) carry no UTC offset "
            f"(the Google Sheets export format); they are being interpreted as "
            f"{_tz_name(assume_timezone)}. Affected {label}: "
            + ", ".join(str(n) for n in naive_locators)
            + ". Relative ordering among these rows is unaffected by the "
            "assumption, so latest-per-email is still correct as long as every "
            "offset-less row came from the same clock."
        )
        if aware_count:
            warnings.append(
                f"{text_path}: the file MIXES {aware_count} timestamp(s) with an "
                f"explicit UTC offset and {len(naive_locators)} without one. The "
                f"two groups are being compared across an assumed "
                f"{_tz_name(assume_timezone)} boundary, so a re-submission could "
                f"be ordered wrongly by up to the size of that offset. Re-export "
                f"with offsets, or pass assume_timezone= explicitly."
            )

    if not submissions and not problems:
        problems.append(
            Complaint(
                where=text_path,
                what="contains a header but no submission rows",
            )
        )

    if problems:
        raise ResponseError(problems)

    latest = _resolve_latest(submissions, utc_by_id)

    repeats = sorted(
        (email, n)
        for email, n in _count_by_email(submissions).items()
        if n > 1
    )
    for email, count in repeats:
        warnings.append(
            f"{text_path}: {email} submitted {count} times; the latest "
            f"(timestamp {latest[email].timestamp}, ties broken by later file "
            f"position) wins and the other {count - 1} are retained as superseded."
        )

    extra_values = _collect_extras(rows, submissions, extras, is_json)

    responses = types.Responses(
        submissions=tuple(submissions),
        latest=latest,
        k=discovered_k,
        source_path=text_path,
        sha256=digest,
        warnings=tuple(warnings),
    )
    return LoadedResponses(
        responses=responses,
        extra_columns=extras,
        extra_values=extra_values,
        header=tuple(header),
    )


def _count_by_email(submissions: Iterable[types.Submission]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for submission in submissions:
        counts[submission.email] = counts.get(submission.email, 0) + 1
    return counts


def _resolve_latest(
    submissions: Sequence[types.Submission],
    utc_by_id: Mapping[str, dt.datetime],
) -> dict[types.PersonId, types.Submission]:
    """SPEC §3.2: latest row per email wins, ordered by timestamp, ties broken by
    later file position.

    The returned dict is built in sorted-email order so that anything iterating it
    (a report, a JSON dump) is deterministic without having to remember to sort.
    """
    best: dict[str, types.Submission] = {}
    best_key: dict[str, tuple[dt.datetime, int]] = {}
    for submission in submissions:
        key = (utc_by_id[submission.submission_id], submission.file_row)
        if submission.email not in best_key or key > best_key[submission.email]:
            best[submission.email] = submission
            best_key[submission.email] = key
    return {email: best[email] for email in sorted(best)}


def _collect_extras(
    rows: Sequence[tuple[int, Mapping[str, str]]],
    submissions: Sequence[types.Submission],
    extras: Sequence[str],
    is_json: bool,
) -> dict[str, dict[str, str]]:
    if not extras:
        return {}
    by_file_row = {s.file_row: s.submission_id for s in submissions}
    out: dict[str, dict[str, str]] = {}
    for file_row, (_locator, cells) in enumerate(rows):
        submission_id = by_file_row.get(file_row)
        if submission_id is None:
            continue
        out[submission_id] = {column: cells.get(column, "") for column in extras}
    return out


# --------------------------------------------------------------------------
# Canonical writer (round-trip)
# --------------------------------------------------------------------------


def _csv_text(header: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    buffer = io.StringIO(newline="")
    # lineterminator="\n": the platform default is "\r\n", which would make the
    # same Responses object serialise to different bytes on Windows.
    writer = csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(list(header))
    for row in rows:
        writer.writerow(list(row))
    return buffer.getvalue()


def canonical_rows(
    responses: types.Responses,
    *,
    extras: Mapping[str, Mapping[str, str]] | None = None,
    extra_columns: Sequence[str] = (),
) -> tuple[tuple[str, ...], ...]:
    """Header row followed by one row per submission, in canonical column order.

    Row order is FILE order, not sorted: `Submission.file_row` is the SPEC §3.2
    tie-break, so re-ordering the rows here would silently change which
    re-submission wins on a timestamp tie.
    """
    header = (*canonical_header(responses.k), *extra_columns)
    out: list[tuple[str, ...]] = [header]
    for submission in responses.submissions:
        # Keyed by column name, then emitted in header order. This used to be a
        # positional list, which silently wrote every value one column out of
        # place the moment the header order changed -- the round-trip came back
        # with the year in the candidacy field. Deriving the order from the
        # header makes that failure impossible rather than merely fixed.
        cells: dict[str, str] = {
            "submission_id": submission.submission_id,
            "timestamp": submission.timestamp,
            "email": submission.email,
            "name": submission.name,
            "year": str(submission.year),
            "candidacy": submission.candidacy,
            "client_version": submission.client_version,
            "auth_method": submission.auth_method,
        }
        for rank, desk in enumerate(submission.choices, start=1):
            cells[f"choice_{rank}"] = desk
        values = (extras or {}).get(submission.submission_id, {})

        row: list[str] = []
        for column in header:
            if column in cells:
                row.append(cells[column])
            else:
                row.append(values.get(column, ""))
        out.append(tuple(row))
    return tuple(out)


def dumps_responses(
    responses: types.Responses,
    *,
    extras: Mapping[str, Mapping[str, str]] | None = None,
    extra_columns: Sequence[str] = (),
) -> str:
    """Serialise back to the canonical CSV. `load(write(x))` reproduces x's
    `submissions` and `latest` exactly."""
    rows = canonical_rows(responses, extras=extras, extra_columns=extra_columns)
    return _csv_text(rows[0], rows[1:])


def write_responses(
    responses: types.Responses,
    path: str | os.PathLike[str],
    *,
    extras: Mapping[str, Mapping[str, str]] | None = None,
    extra_columns: Sequence[str] = (),
) -> str:
    """Write the canonical CSV and return the sha256 of the bytes written."""
    payload = dumps_responses(
        responses, extras=extras, extra_columns=extra_columns
    ).encode("utf-8")
    parent = os.path.dirname(os.fspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)
    return sha256_bytes(payload)


# --------------------------------------------------------------------------
# Anonymisation (SPEC §7.2)
# --------------------------------------------------------------------------


def pseudonym(email: str, seed_string: str, salt: str = "") -> str:
    """`sha256(email + seed)[:8]`, per SPEC §7.2.

    With the default empty salt this is exactly the SPEC expression. `salt` is
    prefixed so that default is preserved byte-for-byte while still allowing a
    coordinator to re-anonymise a published file under a fresh salt (e.g. after
    the seed itself has been published, which it always is).

    hashlib, never `hash()`: the builtin is salted per-process by PYTHONHASHSEED
    and would produce a different file on every run.
    """
    return sha256_bytes(f"{salt}{email.strip().lower()}{seed_string}".encode("utf-8"))[:8]


def anonymize(
    responses: types.Responses,
    seed_string: str,
    salt: str = "",
    path: str | os.PathLike[str] | None = None,
    *,
    assume_timezone: dt.tzinfo = DEFAULT_NAIVE_TIMEZONE,
) -> str:
    """Return (and optionally write) `responses_anonymized.csv` per SPEC §7.2.

    `email` and `name` are both replaced by the pseudonym; everything needed to
    reproduce the *shape* of the solve is kept.

    **Row order is sorted by pseudonym, not by original order.** This is not
    cosmetic. Publishing the rows in their original order publishes the order in
    which people submitted, and the coordinator (and anyone who saw the live
    sheet, or who knows they themselves submitted first) can walk that ordering
    back to names — which un-anonymises a file whose entire purpose is to be
    un-attributable. Sorting by an unpredictable pseudonym destroys that channel.

    Within one pseudonym the rows stay in their original relative order
    (timestamp, then original file position). That is deliberate too: SPEC §3.2
    breaks timestamp ties by later file position, so scrambling a single person's
    own rows could make a re-run of the anonymised file pick a *different* latest
    submission than the real run did. One person's rows are already ordered by
    their published timestamps, so preserving that order leaks nothing further.
    """
    order: list[tuple[str, dt.datetime, int, types.Submission]] = []
    for submission in responses.submissions:
        try:
            utc, _naive = parse_timestamp(
                submission.timestamp, assume_timezone=assume_timezone
            )
        except ValueError:
            # anonymize() may be handed a hand-built Responses that never went
            # through the loader. Fall back to file order rather than raising:
            # producing the file is more useful than refusing over a sort key.
            utc = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        order.append(
            (pseudonym(submission.email, seed_string, salt), utc, submission.file_row, submission)
        )
    order.sort(key=lambda item: (item[0], item[1], item[2]))

    header = canonical_header(responses.k)
    rows = [
        (
            submission.submission_id,
            submission.timestamp,
            pseudo,          # email -> pseudonym
            pseudo,          # name  -> the same pseudonym; no free-text identity
            str(submission.year),
            submission.candidacy,
            *submission.choices,
            submission.client_version,
            submission.auth_method,
        )
        for pseudo, _utc, _row, submission in order
    ]
    text = _csv_text(header, rows)

    if path is not None:
        parent = os.path.dirname(os.fspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(text.encode("utf-8"))
    return text


def write_anonymized(
    path: str | os.PathLike[str],
    responses: types.Responses,
    seed_string: str,
    salt: str = "",
) -> str:
    """`anonymize()` with the argument order cli.py writes at its call sites
    (destination first, matching the other `write_*` functions). Returns the
    sha256 of the bytes written, so the coordinator can publish it."""
    text = anonymize(responses, seed_string, salt, path)
    return sha256_bytes(text.encode("utf-8"))
