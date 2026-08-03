"""Cross-check the pre-lock claim log against the roster.

Why this exists
---------------
The two-phase process invites one specific mistake, and it is silent:

  phase 1  people claim the desk they already sit at (the Keepers sheet tab)
  ...      the coordinator exports that tab and runs tools/merge_keepers.py,
           which sets keeps_desk / current_desk in config/roster.csv
  phase 2  everybody else ranks, and the solver reads the ROSTER

If the merge is skipped, nothing complains. The form still hides claimed desks,
because Code.gs reads the claim log directly — so phase 2 looks correct. But the
solver never sees the claim log; it only sees the roster. It would treat those
desks as free and hand one to somebody else, and the person who was told "you
are keeping this desk, there is nothing more for you to do" would find out when
the results were published.

So: given the exported claim log, verify the roster already reflects it, and
refuse to continue if it does not. Parsing is shared with tools/merge_keepers.py
rather than written twice, because two readers of the same file that disagree
about which claim is current would be its own version of this bug.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

from . import responses as responses_mod
from .errors import DeskMatchError
from .errors import Problem as Complaint
from .types import Roster

#: Columns the Keepers tab is written with (frontend/Code.gs KEEPERS_HEADER).
REQUIRED_COLUMNS: tuple[str, ...] = (
    "claim_id",
    "timestamp",
    "email",
    "name",
    "desk_id",
    "keeping",
)

_TRUE = {"yes", "true", "1", "y", "t"}
_FALSE = {"no", "false", "0", "n", "f", ""}


class KeepersError(DeskMatchError):
    """The roster does not match the claim log, or the log cannot be read."""

    exit_code = 4

    def __init__(self, problems: list[Complaint]):
        self.problems = list(problems)
        n = len(self.problems)
        head = (
            f"{n} problem{'s' if n != 1 else ''} reconciling the pre-lock claims "
            f"with the roster:\n"
        )
        super().__init__(
            head + "\n".join(f"  [{i + 1}] {p.render()}" for i, p in enumerate(self.problems))
        )


@dataclass(frozen=True)
class Claim:
    email: str
    name: str
    desk_id: str
    keeping: bool
    timestamp: str
    file_row: int


def load_claims(path: Path) -> tuple[Claim, ...]:
    """Active claims, latest row per person.

    Resolution is `responses.resolve_latest_by_email`, the same function the
    response loader uses, so "which row is current" cannot mean two things in
    one codebase. A latest row with keeping=false is a release and yields no
    claim.
    """
    problems: list[Complaint] = []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise KeepersError([
            Complaint(where=str(path), what=f"cannot be read ({exc.strerror or exc})")
        ]) from None

    rows = list(csv.DictReader(io.StringIO(text)))
    header = set(rows[0].keys()) if rows else set()
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise KeepersError([
            Complaint(
                where=str(path),
                what=f"is missing required column(s): {', '.join(missing)}",
                hint="This should be the Keepers tab exported as CSV. Expected: "
                     + ", ".join(REQUIRED_COLUMNS),
            )
        ])

    parsed: list[Claim] = []
    when = {}
    for index, row in enumerate(rows):
        email = (row.get("email") or "").strip().lower()
        if not email:
            continue
        raw_keeping = (row.get("keeping") or "").strip().lower()
        if raw_keeping in _TRUE:
            keeping = True
        elif raw_keeping in _FALSE:
            keeping = False
        else:
            problems.append(Complaint(
                where=f"{path}: line {index + 2} ({email})",
                what=f"'keeping' is {row.get('keeping')!r}, which is neither true nor false",
            ))
            continue
        claim = Claim(
            email=email,
            name=(row.get("name") or "").strip(),
            desk_id=(row.get("desk_id") or "").strip(),
            keeping=keeping,
            timestamp=(row.get("timestamp") or "").strip(),
            file_row=index,
        )
        try:
            stamp, _naive = responses_mod.parse_timestamp(claim.timestamp)
        except ValueError:
            # Keep it, ordered before anything with a usable timestamp. Dropping
            # somebody's claim over a reformatted cell is the worse failure.
            import datetime as dt

            stamp = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        parsed.append(claim)
        # Keyed by file_row, which is what key_of returns below. The shared
        # resolver looks the timestamp up via key_of(record), so the two must
        # agree; keying on anything else is a KeyError at run time.
        when[claim.file_row] = stamp

    if problems:
        raise KeepersError(problems)

    latest = responses_mod.resolve_latest_by_email(
        parsed, when, lambda claim: claim.file_row
    )
    return tuple(
        sorted((c for c in latest.values() if c.keeping), key=lambda c: c.email)
    )


def verify_against_roster(claims: tuple[Claim, ...], roster: Roster) -> list[Complaint]:
    """Every active claim must already be recorded in the roster."""
    problems: list[Complaint] = []
    by_email = {p.email: p for p in roster.people}

    for claim in claims:
        person = by_email.get(claim.email)
        if person is None:
            problems.append(Complaint(
                where=f"{claim.name or claim.email} <{claim.email}>",
                what=f"is keeping {claim.desk_id} but is not on config/roster.csv",
                hint="Add them to the roster, then re-run tools/merge_keepers.py.",
            ))
            continue
        if not person.keeps_desk:
            problems.append(Complaint(
                where=f"{person.name} <{person.email}>",
                what=f"claimed {claim.desk_id} in the pre-lock phase, but "
                     f"config/roster.csv still says keeps_desk=no",
                hint="The claims have not been merged into the roster. Run:\n"
                     "        python tools/merge_keepers.py --roster config/roster.csv "
                     "--keepers <the export> --dry-run\n"
                     "    then again without --dry-run, and commit the result. Without "
                     "it the solver treats that desk as free and will give it to "
                     "somebody else.",
            ))
        elif (person.current_desk or "").strip() != claim.desk_id:
            problems.append(Complaint(
                where=f"{person.name} <{person.email}>",
                what=f"claimed {claim.desk_id} but config/roster.csv records "
                     f"current_desk={person.current_desk!r}",
                hint="The roster is out of date, or was edited by hand after the "
                     "merge. Re-run tools/merge_keepers.py.",
            ))

    # The other direction: a roster keeper who released their desk in the log.
    claimed = {c.email for c in claims}
    for person in roster.people:
        if person.keeps_desk and person.email not in claimed:
            problems.append(Complaint(
                where=f"{person.name} <{person.email}>",
                what=f"is recorded in config/roster.csv as keeping "
                     f"{person.current_desk}, but holds no active claim in the "
                     f"pre-lock log",
                hint="They may have released it after the merge. Re-run "
                     "tools/merge_keepers.py so the roster and the log agree — "
                     "otherwise a desk nobody is keeping stays out of the pool.",
            ))
    return problems
