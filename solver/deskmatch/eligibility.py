"""Rule-table interpreter for zone eligibility.

There is deliberately no `if year <= 2` anywhere in this package. The current
policy (pre-candidates sit together) is data in `config/eligibility.json`, and a
future coordinator who wants a third room or a different cohort rule edits JSON,
not Python.

Predicate grammar (docs/SPEC.md §2.2), all keys in a `when` ANDed together:

    scalar    {"candidacy": "precandidate"}         equality, case-insensitive
    list      {"year": [1, 2]}                      membership
    range     {"year": {"min": 1, "max": 2}}        inclusive, both optional
    negation  {"candidacy": {"not": "candidate"}}   inverts any of the above

Rules are evaluated top-to-bottom and the FIRST match wins, which is why the
validator insists the final rule is a catch-all: without one, a person with an
unanticipated attribute combination would have undefined eligibility, and the
failure would show up as an empty desk list rather than as an error.
"""

from __future__ import annotations

from typing import Any, Mapping

from .types import Eligibility, EligibilityRule, Person, Rooms, ZoneId


class NoMatchingRule(Exception):
    """Raised only if the catch-all invariant was somehow bypassed.

    The validator makes this unreachable via the config path. It exists so that
    a programmatically-constructed Eligibility (in a test, say) fails loudly
    rather than silently granting or denying access.
    """


# --------------------------------------------------------------------------
# Value coercion
# --------------------------------------------------------------------------


def _norm_scalar(value: Any) -> Any:
    """Normalise one side of a comparison.

    Strings are trimmed and case-folded; the coordinator typing "Precandidate"
    in the roster and "precandidate" in the rules must not silently mean two
    different cohorts. Numbers pass through so ranges keep working. bools are
    handled before ints because bool is a subclass of int in Python and
    `True == 1` would otherwise make a truthy flag match a year of 1.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip()
        # A roster CSV has no types: "2" and 2 must compare equal.
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return float(s)
        except ValueError:
            pass
        return s.casefold()
    return value


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------
# Matcher evaluation
# --------------------------------------------------------------------------


def match_value(matcher: Any, actual: Any) -> bool:
    """Evaluate a single matcher against a single roster attribute value."""
    # Negation and range both arrive as dicts; disambiguate on the keys.
    if isinstance(matcher, Mapping):
        if "not" in matcher:
            if len(matcher) != 1:
                raise ValueError(
                    f"a 'not' matcher must be the only key, got {sorted(matcher)}"
                )
            return not match_value(matcher["not"], actual)

        unknown = set(matcher) - {"min", "max"}
        if unknown:
            raise ValueError(
                f"unknown matcher keys {sorted(unknown)}; expected 'min', 'max' or 'not'"
            )
        number = _as_number(actual)
        if number is None:
            # A range against a non-numeric attribute is never satisfied. This
            # is not an error: {"year": {"min": 3}} applied to a roster row with
            # a blank year should simply not match, and fall through to the
            # next rule.
            return False
        lo = _as_number(matcher.get("min")) if "min" in matcher else None
        hi = _as_number(matcher.get("max")) if "max" in matcher else None
        if lo is not None and number < lo:
            return False
        if hi is not None and number > hi:
            return False
        return True

    if isinstance(matcher, (list, tuple)):
        return any(match_value(m, actual) for m in matcher)

    return _norm_scalar(matcher) == _norm_scalar(actual)


def rule_matches(rule: EligibilityRule, attributes: Mapping[str, Any]) -> bool:
    """True if every key in the rule's `when` matches (empty `when` = catch-all)."""
    for attr, matcher in rule.when.items():
        if attr not in attributes:
            # The validator rejects rules naming a column the roster lacks, so
            # reaching here means a hand-built object. Treat a missing attribute
            # as "does not match" rather than crashing.
            return False
        if not match_value(matcher, attributes[attr]):
            return False
    return True


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def matching_rule(eligibility: Eligibility, person: Person) -> EligibilityRule:
    """The first rule that matches this person. Raises if none does."""
    attrs = dict(person.attributes)
    # The typed fields win over the raw CSV strings, since submissions may have
    # corrected a stale roster year/candidacy before we got here.
    attrs.setdefault("name", person.name)
    attrs["email"] = person.email
    attrs["year"] = person.year
    attrs["candidacy"] = person.candidacy
    attrs["keeps_desk"] = person.keeps_desk

    for rule in eligibility.rules:
        if rule_matches(rule, attrs):
            return rule
    raise NoMatchingRule(
        f"no eligibility rule matched {person.email!r}. The last rule in "
        f"eligibility.json must be a catch-all ({{\"when\": {{}}}})."
    )


def allowed_zones(eligibility: Eligibility, rooms: Rooms, person: Person) -> tuple[ZoneId, ...]:
    """Zones this person may be assigned to, sorted for determinism."""
    rule = matching_rule(eligibility, person)
    if rule.allow_zones == "*":
        return tuple(sorted(rooms.zones))
    return tuple(sorted(rule.allow_zones))


def eligibility_reason(eligibility: Eligibility, person: Person) -> str:
    """Human-readable justification, for tooltips and the report."""
    return matching_rule(eligibility, person).reason


def allowed_desks(
    eligibility: Eligibility, rooms: Rooms, person: Person
) -> tuple[str, ...]:
    """Desk ids in zones this person may occupy. Ignores availability/pool."""
    zones = set(allowed_zones(eligibility, rooms, person))
    return tuple(sorted(d.id for d in rooms.all_desks if d.zone in zones))


def zone_capacity(rooms: Rooms) -> Mapping[ZoneId, int]:
    """Desk count per zone. Used by the pre-deadline check to spot a cohort
    whose zone simply cannot hold it, which is a config problem rather than a
    preference problem and needs a different conversation."""
    counts: dict[ZoneId, int] = {z: 0 for z in sorted(rooms.zones)}
    for desk in rooms.all_desks:
        counts[desk.zone] = counts.get(desk.zone, 0) + 1
    return counts
