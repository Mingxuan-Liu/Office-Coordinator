"""Exception hierarchy.

Rule: anything a coordinator can cause by editing a config file or exporting a
bad CSV must surface as one of these, with a message that names the file, the
location, and what was expected. A raw KeyError/TypeError escaping to the
console is a bug in this package, not user error.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Problem:
    """One validation complaint. Rendered as a single readable line."""

    where: str          # "rooms.json: rooms[0].desks[13] (\"D14\")"
    what: str           # "references zone 'senior_side', which is not defined"
    hint: str = ""      # "Defined zones are: candidate_side, precandidate_side."

    def render(self) -> str:
        line = f"{self.where}: {self.what}"
        if self.hint:
            line += f"\n    {self.hint}"
        return line


class DeskMatchError(Exception):
    """Base for every error this package raises deliberately."""

    exit_code = 1


class ConfigError(DeskMatchError):
    """One or more config files are invalid. Carries every problem found, not
    just the first, so the coordinator can fix them in one pass."""

    exit_code = 4

    def __init__(self, problems: list[Problem], warnings: list[Problem] | None = None):
        self.problems = list(problems)
        self.warnings = list(warnings or [])
        super().__init__(self.render())

    def render(self) -> str:
        n = len(self.problems)
        head = f"{n} configuration problem{'s' if n != 1 else ''} found:\n"
        body = "\n".join(f"  [{i + 1}] {p.render()}" for i, p in enumerate(self.problems))
        return head + body


class ResponseError(DeskMatchError):
    """The response file does not satisfy the documented schema (docs/SPEC.md §3)."""

    exit_code = 4

    def __init__(self, problems: list[Problem]):
        self.problems = list(problems)
        super().__init__(self.render())

    def render(self) -> str:
        n = len(self.problems)
        head = f"{n} problem{'s' if n != 1 else ''} in the response file:\n"
        return head + "\n".join(f"  [{i + 1}] {p.render()}" for i, p in enumerate(self.problems))


class AccommodationsError(DeskMatchError):
    """The private-notes export does not satisfy docs/SPEC.md §7.3.

    Raised only for a file that cannot be interpreted at all — a missing
    required column, an unreadable file, a header with no rows. Anything
    row-level is a warning instead: a note is not a claim on a desk, and
    refusing to run the department's assignment over one odd row would be the
    wrong trade. See `deskmatch.accommodations`.
    """

    exit_code = 4

    def __init__(self, problems: list[Problem]):
        self.problems = list(problems)
        super().__init__(self.render())

    def render(self) -> str:
        n = len(self.problems)
        head = f"{n} problem{'s' if n != 1 else ''} in the accommodations file:\n"
        return head + "\n".join(f"  [{i + 1}] {p.render()}" for i, p in enumerate(self.problems))


class PrivacyError(DeskMatchError):
    """A public artefact contains data that must never have reached it.

    Deliberately an error and not a warning. A report that leaks is not a
    slightly-worse report; it is the one failure mode of this system that cannot
    be undone once the file has been circulated.

    Lives here rather than in `report.py` because two different modules enforce
    the same rule against two different kinds of leak — attributed preference
    data in the PDF (`report.assert_public_safe`) and private-note text in any
    published file (`accommodations.assert_absent_from`) — and one rule should
    not have two exception types.
    """

    exit_code = 1


class InfeasibleError(DeskMatchError):
    """No assignment exists in which everyone gets a top-K desk.

    Per invariant I7 this is a run failure, never a degraded answer. Carries the
    full diagnostic so the CLI can write it out.
    """

    exit_code = 2

    def __init__(self, diagnosis):  # diagnosis: types.Infeasibility
        self.diagnosis = diagnosis
        super().__init__(diagnosis.summary())


class DeterminismError(DeskMatchError):
    """A runtime invariant that guards reproducibility was violated.

    These should be impossible. If one fires, the output must not be trusted.
    """

    exit_code = 1


class VerificationError(DeskMatchError):
    """`--verify` was given a hash that does not match what we computed."""

    exit_code = 5
