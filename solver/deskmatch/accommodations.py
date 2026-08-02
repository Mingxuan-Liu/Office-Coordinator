"""Private notes to the coordinator (SPEC §7.3).

The form has an optional free-text box on the confirm page: "Private note to the
coordinator". A student uses it when *where* they sit matters for a reason a
ranked list cannot express — an accessibility or health need, needing distance
from a specific person in order to work, equipment or schedules that constrain
where they can be. The notes are exported as their own sheet and passed to
`deskmatch solve --accommodations`.

Three properties govern every line of this module.

**1. A note is never an input to the solve.** SPEC I2 says the assignment is a
pure function of `(responses, config, seed)` with no override path, and that is
the basis of the whole integrity argument in §8. Nothing here is imported by
`problem`, `solve`, `scoring` or `provenance`, nothing here reaches
`results.json`, and passing `--accommodations` cannot change a single byte of any
published artefact. If the coordinator decides to act on a note they change an
*input* — mark a desk unavailable in `rooms.json`, say — which is visible in git.
The note is advice to a human.

**2. The content is coordinator-only, and that is a correctness property.** These
notes contain interpersonal conflict, health, and caring responsibilities.
Pseudonymising the author does not help: "I need to be away from Ada" identifies
someone in the body text. So the text goes to exactly two places — the
coordinator PDF and `out/accommodations_coordinator.txt`, written 0600 — and
`assert_absent_from()` / `report.audit_public_pdf()` check the published files
for it rather than trusting that it stayed put.

**3. An odd file must not stop the run.** A note is not a claim on a desk.
Refusing to run the department's assignment because one row has a bad timestamp,
or because somebody who left last year still had the form open, would be the
wrong trade. Row-level faults are collected as warnings; only a file that cannot
be interpreted at all raises `AccommodationsError`, and then it carries every
problem found rather than the first.

Row semantics are SPEC §3.2's, unchanged and *shared*: the latest row per email
wins, ordered by timestamp with ties broken by later file position, via
`responses.resolve_latest_by_email` — the same function the response loader and
the keeper log use, not a third copy of the rule.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import textwrap
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from . import responses as responses_mod
from . import types
from .errors import AccommodationsError, PrivacyError
from .errors import Problem as Complaint
from .provenance import sha256_bytes

__all__ = [
    "REQUIRED_COLUMNS",
    "AUDIT_COLUMNS",
    "COORDINATOR_TXT_NAME",
    "COORDINATOR_FILE_MODE",
    "Note",
    "Accommodations",
    "load",
    "fingerprints",
    "normalise_words",
    "find_leaks",
    "assert_absent_from",
    "render_coordinator_text",
    "write_coordinator_text",
]


# --------------------------------------------------------------------------
# Schema (SPEC §7.3)
# --------------------------------------------------------------------------

#: Columns without which the file cannot be interpreted at all.
REQUIRED_COLUMNS: tuple[str, ...] = ("note_id", "timestamp", "email", "name", "note")

#: Audit-only, exactly as SPEC §3.1 treats `client_version` on the response file:
#: absence is a warning, never an error, so a hand-built fixture is still usable.
AUDIT_COLUMNS: tuple[str, ...] = ("client_version",)

#: Deterministic name for the coordinator-only dump, under the run's `--out`.
COORDINATOR_TXT_NAME = "accommodations_coordinator.txt"

#: The dump is written 0600. It is the only file this package writes that a
#: second person on a shared machine must not be able to read.
COORDINATOR_FILE_MODE = 0o600

#: Wrap width for the text dump. Fixed columns, not the terminal width: the file
#: is committed nowhere and read anywhere, and reading the clock or the
#: environment to lay it out would make two runs produce two different files.
TEXT_WIDTH = 78

_NOT_FOR_DISTRIBUTION = "NOT FOR DISTRIBUTION — COORDINATOR ONLY"


@dataclass(frozen=True)
class Note:
    """One row of the notes export.

    `text` is the note with leading/trailing whitespace stripped and line endings
    normalised to "\\n"; internal blank lines survive, because a student who
    wrote two paragraphs meant two paragraphs.
    """

    note_id: str
    timestamp: str                 # ISO-8601 with offset, exactly as submitted
    email: types.PersonId
    name: str
    text: str
    client_version: str = ""
    file_row: int = -1             # 0-based data-row index; the §3.2 tie-break


@dataclass(frozen=True)
class Accommodations:
    """Every non-blank note, plus the resolved latest-per-person view.

    `latest` is what the coordinator reads: one note per person, in sorted-email
    order. `notes` keeps the superseded rows too, so the count on the report page
    is honest about the history without reprinting withdrawn text.
    """

    notes: tuple[Note, ...]                        # non-blank rows, file order
    latest: Mapping[types.PersonId, Note]          # SPEC §3.2, blanks removed
    source_path: str
    sha256: str
    warnings: tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.latest)

    def __bool__(self) -> bool:
        return bool(self.latest)

    @property
    def emails(self) -> tuple[types.PersonId, ...]:
        return tuple(self.latest)

    @property
    def superseded(self) -> tuple[Note, ...]:
        # Keyed on file_row, which is unique per row and is the SPEC §3.2
        # tie-break, so this is exact even when two people write the same words.
        keep = {note.file_row for note in self.latest.values()}
        return tuple(note for note in self.notes if note.file_row not in keep)

    def for_email(self, email: str) -> Note | None:
        return self.latest.get(email.strip().lower())


#: An empty result, for the code paths where no file was supplied. Callers can
#: treat "no --accommodations flag" and "a file with no notes in it" alike.
EMPTY = Accommodations(notes=(), latest={}, source_path="", sha256="")


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _read_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise AccommodationsError(
            [
                Complaint(
                    where=path,
                    what=f"cannot be read ({exc.strerror or exc})",
                    hint="Check the path, and that the notes tab was exported. Omit "
                         "--accommodations entirely if there are no notes this cycle.",
                )
            ]
        ) from exc


def _decode(raw: bytes, path: str) -> str:
    try:
        # utf-8-sig for the same reason responses.py uses it: the Google Sheets
        # CSV export is BOM-prefixed and would otherwise hide 'note_id'.
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AccommodationsError(
            [
                Complaint(
                    where=f"{path}: byte {exc.start}",
                    what="the file is not valid UTF-8",
                    hint="Re-export as UTF-8 CSV.",
                )
            ]
        ) from exc


def _clean_text(raw: str) -> str:
    """Normalise a submitted note. Empty string means "there is no note here"."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def load(
    path: str | os.PathLike[str],
    *,
    roster_emails: Iterable[str] | None = None,
    assume_timezone: dt.tzinfo = responses_mod.DEFAULT_NAIVE_TIMEZONE,
) -> Accommodations:
    """Load the exported Accommodations CSV (or its JSON form).

    `roster_emails`, when given, turns an unrecognised author into a warning
    naming them — not an error. A note is not a claim on a desk; the person may
    have graduated, or typed their gmail address. Losing the note is bad, and
    refusing to run the assignment over it would be worse.

    Raises `AccommodationsError` only for a file that cannot be interpreted at
    all, and then with every problem found at once.
    """
    text_path = os.fspath(path)
    suffix = os.path.splitext(text_path)[1].lower()
    if suffix not in (".csv", ".json"):
        raise AccommodationsError(
            [
                Complaint(
                    where=text_path,
                    what=f"has extension {suffix or '(none)'}, which is not a "
                         f"supported notes format",
                    hint="Use '.csv' (the Google Sheets export) or '.json' (a list "
                         "of row objects with the same keys).",
                )
            ]
        )

    raw = _read_bytes(text_path)
    digest = sha256_bytes(raw)
    text = _decode(raw, text_path)

    problems: list[Complaint] = []
    warnings: list[str] = []
    is_json = suffix == ".json"

    if is_json:
        header, rows = responses_mod._parse_json(text, text_path, problems, warnings)
    else:
        header, rows = responses_mod._parse_csv(text, text_path, problems, warnings)
    if not header:
        raise AccommodationsError(problems)

    seen: dict[str, int] = {}
    for column in header:
        seen[column] = seen.get(column, 0) + 1
    for column in sorted(col for col, n in seen.items() if n > 1):
        problems.append(
            Complaint(
                where=f"{text_path}: header",
                what=f"column '{column}' appears {seen[column]} times",
                hint="Column names must be unique; otherwise which one holds the "
                     "note is undefined.",
            )
        )
    for column in REQUIRED_COLUMNS:
        if column not in seen:
            problems.append(
                Complaint(
                    where=f"{text_path}: header",
                    what=f"required column '{column}' is missing",
                    hint="Required columns (SPEC §7.3): " + ", ".join(REQUIRED_COLUMNS)
                         + ". Export the notes tab rather than hand-building the file.",
                )
            )
    for column in AUDIT_COLUMNS:
        if column not in seen:
            warnings.append(
                f"{text_path}: header: audit column '{column}' is missing. Nothing "
                f"depends on it, so the notes are still read."
            )
    known = set(REQUIRED_COLUMNS) | set(AUDIT_COLUMNS)
    extras = sorted(col for col in seen if col and col not in known)
    if extras:
        warnings.append(
            f"{text_path}: header: {len(extras)} column(s) are not part of "
            f"SPEC §7.3 and are ignored: " + ", ".join(extras) + "."
        )

    if problems:
        # Only ever a structural fault: an unreadable header, or a column the
        # notes cannot be found in. Everything below is a warning by design.
        raise AccommodationsError(problems)

    parsed: list[Note] = []
    when: dict[int, dt.datetime] = {}
    ids_seen: dict[str, int] = {}
    unparseable_times = 0

    for file_row, (locator, cells) in enumerate(rows):
        where = (
            f"{text_path}: [{locator}]" if is_json else f"{text_path}: line {locator}"
        )
        email = cells.get("email", "").strip().lower()
        body = _clean_text(cells.get("note", ""))

        if not email:
            if body:
                warnings.append(
                    f"{where}: a note was submitted with no email address, so there "
                    f"is no way to tell whose it is. It has been SKIPPED -- read it "
                    f"in the sheet yourself."
                )
            continue

        try:
            utc, was_naive = responses_mod.parse_timestamp(
                cells.get("timestamp", ""), assume_timezone=assume_timezone
            )
        except ValueError as exc:
            # Ordering, not eligibility, is all this feeds. Sort such a row to the
            # very beginning so that any row with a real timestamp supersedes it,
            # and let file position order it against other broken rows: strictly
            # better than discarding somebody's accessibility request over a cell
            # a spreadsheet reformatted.
            unparseable_times += 1
            warnings.append(
                f"{where} ({email}): 'timestamp' {exc}. The note is kept; it is "
                f"ordered before every row that has a usable timestamp, so if this "
                f"person submitted twice, check which one you are reading."
            )
            utc, was_naive = dt.datetime.min.replace(tzinfo=dt.timezone.utc), False
        if was_naive:
            warnings.append(
                f"{where} ({email}): the timestamp carries no UTC offset and is "
                f"read as {responses_mod._tz_name(assume_timezone)}."
            )

        note_id = cells.get("note_id", "").strip()
        if not note_id:
            warnings.append(
                f"{where} ({email}): 'note_id' is empty. The note is kept -- the id "
                f"is only an audit handle -- but the export is not what the form "
                f"writes."
            )
        elif note_id in ids_seen:
            warnings.append(
                f"{where} ({email}): 'note_id' {note_id!r} was already used at "
                f"{'[' + str(ids_seen[note_id]) + ']' if is_json else 'line ' + str(ids_seen[note_id])}. "
                f"Both rows are kept; latest-per-email still decides which one you "
                f"read."
            )
        else:
            ids_seen[note_id] = locator

        note = Note(
            note_id=note_id,
            timestamp=cells.get("timestamp", "").strip(),
            email=email,
            name=cells.get("name", "").strip(),
            text=body,
            client_version=cells.get("client_version", "").strip(),
            file_row=file_row,
        )
        parsed.append(note)
        when[file_row] = utc

    # Resolve over EVERY row, blank ones included, and only then drop the blanks.
    # This is the withdrawal path and it has to work in this order: a student who
    # clears the box and re-submits is telling the coordinator to forget what they
    # wrote, and resolving over the non-blank rows alone would resurrect it.
    latest_all = responses_mod.resolve_latest_by_email(
        parsed, when, lambda note: note.file_row
    )
    latest = {
        email: note for email, note in latest_all.items() if note.text
    }
    # An empty row from somebody who never wrote anything is a no-op and says
    # nothing worth printing. An empty row from somebody who DID is a withdrawal,
    # and the coordinator has to be told, because otherwise "there is no note
    # from X" is indistinguishable from "the export dropped X's note".
    ever_wrote = {note.email for note in parsed if note.text}
    for email in sorted(
        e for e, note in latest_all.items() if not note.text and e in ever_wrote
    ):
        warnings.append(
            f"{text_path}: {email} wrote a note and then cleared it. The later, "
            f"empty row wins (SPEC §3.2), so their earlier note is treated as "
            f"withdrawn and is not reported."
        )

    counts: dict[str, int] = {}
    for note in parsed:
        counts[note.email] = counts.get(note.email, 0) + 1
    for email in sorted(e for e, n in counts.items() if n > 1):
        warnings.append(
            f"{text_path}: {email} submitted {counts[email]} notes; the latest "
            f"(timestamp, ties broken by later file position) is the one reported."
        )

    if roster_emails is not None:
        known_emails = {str(e).strip().lower() for e in roster_emails}
        strangers = sorted(set(latest) - known_emails)
        for email in strangers:
            warnings.append(
                f"{text_path}: {email} left a private note but is not on "
                f"config/roster.csv, so they are not in the pool and no desk will "
                f"be assigned to them. The note is still reported to you -- a note "
                f"is not a claim on a desk, and refusing to run the assignment over "
                f"a stray note would be the wrong trade. Fix the roster if they "
                f"should have been in it."
            )

    if not parsed and not warnings:
        warnings.append(f"{text_path}: the file has a header but no note rows.")

    # `notes` keeps only rows that actually say something: an empty note is never
    # stored, so nothing downstream has to decide what an empty one means.
    return Accommodations(
        notes=tuple(note for note in parsed if note.text),
        latest=latest,
        source_path=text_path,
        sha256=digest,
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------
# Leak detection
# --------------------------------------------------------------------------
#
# Used twice, against two different kinds of artefact: `report.audit_public_pdf`
# runs it over the text layer of the public PDF, and `assert_absent_from` runs it
# over the bytes of results.json / assignments.csv / responses_anonymized.csv.
# Both ask the same question, so both ask it with the same code.

_WORD_RE = re.compile(r"[0-9a-z]+")

#: Words per shingle. Long enough that ordinary English cannot collide with it by
#: accident, short enough that a leak of even one sentence is caught.
SHINGLE_WORDS = 5

#: A note shorter than a shingle is matched whole, provided it is at least this
#: many characters. Below that a "note" carries no private content and matching
#: it would fire on the report's own vocabulary.
MIN_PHRASE_CHARS = 8


def normalise_words(text: str) -> str:
    """Lower-case alphanumeric words, single-spaced.

    Everything else — punctuation, line breaks, the wrapping a PDF imposes, the
    quoting a CSV imposes — is discarded, so the comparison is about the words a
    person wrote rather than how a file happened to lay them out.
    """
    return " ".join(_WORD_RE.findall(str(text).casefold()))


def fingerprints(text: str, *, n: int = SHINGLE_WORDS) -> tuple[str, ...]:
    """Distinctive word sequences from one note, for searching other files.

    Overlapping n-grams, so a leak is caught wherever it starts and whatever
    surrounds it. A note too short to shingle is returned whole.

    Honest trade-off: this errs towards the false alarm. A one-word note that
    happens to be a word the report already prints ("eligibility") would be
    flagged. That is the right direction for a privacy guard — the failure is
    loud, names the phrase, and costs a conversation; the opposite failure costs
    somebody's medical history.
    """
    words = _WORD_RE.findall(str(text).casefold())
    if not words:
        return ()
    if len(words) <= n:
        phrase = " ".join(words)
        return (phrase,) if len(phrase) >= MIN_PHRASE_CHARS else ()
    return tuple(
        dict.fromkeys(" ".join(words[i:i + n]) for i in range(len(words) - n + 1))
    )


def all_fingerprints(accommodations: Any) -> tuple[tuple[str, str], ...]:
    """`(email, phrase)` for every note in `accommodations`.

    Every stored note, not only the latest one: a withdrawn or superseded note
    must not appear in a published file either.
    """
    notes = getattr(accommodations, "notes", None)
    if notes is None:
        notes = tuple(accommodations or ())
    out: list[tuple[str, str]] = []
    for note in notes:
        email = getattr(note, "email", "")
        text = getattr(note, "text", note if isinstance(note, str) else "")
        for phrase in fingerprints(text):
            out.append((str(email), phrase))
    return tuple(dict.fromkeys(out))


def _excerpt(phrase: str, limit: int = 40) -> str:
    return phrase if len(phrase) <= limit else phrase[:limit].rstrip() + "…"


def find_leaks(
    accommodations: Any, haystacks: Mapping[str, str | bytes]
) -> tuple[str, ...]:
    """Which of `haystacks` contain private-note text. `{label: text or bytes}`.

    Returns one rendered line per hit. Empty means clean.
    """
    marks = all_fingerprints(accommodations)
    if not marks:
        return ()
    findings: list[str] = []
    for label in sorted(haystacks):
        payload = haystacks[label]
        if isinstance(payload, (bytes, bytearray)):
            payload = bytes(payload).decode("utf-8", errors="replace")
        hay = normalise_words(payload)
        if not hay:
            continue
        for email, phrase in marks:
            if phrase in hay:
                findings.append(
                    f"{label}: contains private-note text from {email} "
                    f"(\"{_excerpt(phrase)}\")"
                )
                break   # one finding per file is enough to stop the run
    return tuple(findings)


def assert_absent_from(
    accommodations: Any, haystacks: Mapping[str, str | bytes]
) -> None:
    """Raise `PrivacyError` if any published artefact carries note text.

    Called by the CLI over the artefacts that are handed to the department. The
    PDF has its own, richer audit in `report.audit_public_pdf`; this is the same
    rule applied to the files that are not PDFs.
    """
    findings = find_leaks(accommodations, haystacks)
    if not findings:
        return
    raise PrivacyError(
        "private notes to the coordinator reached a file that gets published, "
        "which SPEC §7.3 forbids. Nothing has been deleted -- inspect the "
        "file(s) below and fix the code that wrote them. Do NOT circulate them.\n"
        + "\n".join("  " + line for line in findings)
    )


# --------------------------------------------------------------------------
# The coordinator-only text dump
# --------------------------------------------------------------------------


def _desk_line(email: str, solution: Any, config: Any) -> str:
    """What happened to this person, in one phrase."""
    if solution is not None:
        for assignment in getattr(solution, "assignments", ()) or ():
            if assignment.email == email:
                return (
                    f"desk {assignment.desk_id} ({assignment.desk_label}), "
                    f"their choice #{assignment.rank_received}"
                )
    person = None
    roster = getattr(config, "roster", None)
    if roster is not None:
        person = roster.by_email(email)
    if person is not None and person.keeps_desk and person.current_desk:
        return f"desk {person.current_desk} — keeping their current seat, not in the pool"
    if person is None:
        return "not on the roster; no desk assigned"
    return "no desk assigned (not in the pool this cycle)"


def _wrap_body(
    text: str, width: int, indent: str, first_indent: str | None = None
) -> list[str]:
    """Wrap to `width` columns, preserving blank lines between paragraphs.

    Only the first line of the first paragraph gets `first_indent`; everything
    else gets `indent`. A note of arbitrary length lays out; nothing truncates.
    """
    out: list[str] = []
    lead = indent if first_indent is None else first_indent
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            out.append("")
            continue
        out.extend(
            textwrap.wrap(
                paragraph,
                width=max(width, len(indent) + 20),
                initial_indent=lead,
                subsequent_indent=indent,
                break_long_words=True,
                break_on_hyphens=False,
            )
            or [lead.rstrip()]
        )
        lead = indent
    return out


def render_coordinator_text(
    accommodations: Any,
    *,
    solution: Any = None,
    config: Any = None,
    width: int = TEXT_WIDTH,
) -> str:
    """The body of `accommodations_coordinator.txt`.

    Deterministic: sorted by email, no clock, no environment. The header says
    what the file is, because a file like this one will eventually be found on a
    laptop by somebody who did not create it.
    """
    latest = dict(getattr(accommodations, "latest", {}) or {})
    rule = "=" * width
    lines: list[str] = [
        rule,
        "PRIVATE NOTES TO THE COORDINATOR",
        _NOT_FOR_DISTRIBUTION,
        rule,
        "",
    ]
    lines.extend(
        _wrap_body(
            "These are the free-text notes students left on the confirm page of the "
            "form. They were written on the understanding that nobody but the "
            "coordinator would read them, and they contain the kind of thing that "
            "understanding invites: health, accessibility, caring responsibilities, "
            "and conflict with a named person.",
            width, "",
        )
    )
    lines.append("")
    lines.extend(
        _wrap_body(
            "They did not enter the solve. The assignment above them was computed "
            "from the rankings, the config and the seed alone (SPEC I2), and there "
            "is no code path by which a note could have changed it. If you want to "
            "act on one, change an INPUT -- take a desk out of the pool in "
            "rooms.json, say -- and re-run, so that what you did is visible in git.",
            width, "",
        )
    )
    lines.append("")
    lines.extend(
        _wrap_body(
            "Do not attach this file to an email, do not put it in the publish "
            "folder, and delete it when the cycle is over. It is written with "
            "owner-only permissions (0600) for that reason.",
            width, "",
        )
    )
    lines.append("")
    lines.append(rule)

    source = getattr(accommodations, "source_path", "")
    digest = getattr(accommodations, "sha256", "")
    if source:
        lines.append(f"source     : {source}")
    if digest:
        lines.append(f"sha256     : {digest}")
    lines.append(f"notes      : {len(latest)}")
    superseded = getattr(accommodations, "superseded", ())
    if superseded:
        lines.append(
            f"superseded : {len(superseded)} earlier note(s), not reproduced here"
        )
    lines.append(rule)
    lines.append("")

    if not latest:
        lines.append("No notes were submitted this cycle.")
        lines.append("")
        return "\n".join(lines)

    for index, email in enumerate(sorted(latest), start=1):
        note = latest[email]
        name = note.name or email
        lines.append(f"[{index}] {name} <{email}>")
        lines.append(f"    assigned   : {_desk_line(email, solution, config)}")
        if note.timestamp:
            lines.append(f"    submitted  : {note.timestamp}")
        if note.note_id:
            lines.append(f"    note_id    : {note.note_id}")
        if note.client_version:
            lines.append(f"    client     : {note.client_version}")
        lines.append("")
        lines.extend(_wrap_body(note.text, width, "    "))
        lines.append("")
        lines.append("-" * width)
        lines.append("")

    warnings = tuple(getattr(accommodations, "warnings", ()) or ())
    if warnings:
        lines.append("NOTES FROM READING THE FILE")
        lines.append("")
        for warning in warnings:
            lines.extend(_wrap_body(warning, width, "    ", first_indent="  - "))
        lines.append("")

    return "\n".join(lines)


def write_coordinator_text(
    path: str | os.PathLike[str],
    accommodations: Any,
    *,
    solution: Any = None,
    config: Any = None,
    width: int = TEXT_WIDTH,
) -> str:
    """Write the dump with owner-only permissions. Returns its sha256.

    The mode is applied twice on purpose: `os.open` masks it with the process
    umask, and the file may already exist from a previous run with looser
    permissions, in which case `O_CREAT`'s mode is ignored entirely. `chmod`
    afterwards is the one that actually guarantees 0600.
    """
    payload = render_coordinator_text(
        accommodations, solution=solution, config=config, width=width
    ).encode("utf-8")
    target = os.fspath(path)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, COORDINATOR_FILE_MODE)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    try:
        os.chmod(target, COORDINATOR_FILE_MODE)
    except OSError:  # pragma: no cover - filesystems without POSIX modes
        pass
    return sha256_bytes(payload)
