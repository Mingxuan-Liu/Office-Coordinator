"""Private notes to the coordinator (SPEC §7.3).

Two things are being asserted here, and they pull in opposite directions.

**The notes must reach the coordinator.** Whole, un-truncated, attributed to a
person and to the desk that person ended up with — otherwise there is no point
collecting them.

**The notes must reach nobody else.** Not `results.json`, not
`assignments.csv`, not `responses_anonymized.csv`, not `results_public.pdf`.
Pseudonymising the author does not help, because "I need distance from Ada"
identifies somebody in the body text, so the requirement is *absence*, not
anonymity. The load-bearing test is
:func:`test_the_public_artefacts_are_byte_identical_with_and_without_notes`:
the same solve is run twice, once with the flag and once without, and every
published file is compared byte for byte. If that passes, no leak into a
published artefact is possible, because the artefact did not change at all.

Nothing here hard-codes a problem size (invariant I1): counts come off the
config and the solution.
"""

from __future__ import annotations

import csv
import io
import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from conftest import (  # noqa: E402
    RESPONSES_SEARCH_DIRS,
    find_real_responses_csv,
    response_header,
    submission_row,
    write_text,
)

from deskmatch import accommodations as acc  # noqa: E402
from deskmatch import cli, report  # noqa: E402
from deskmatch import responses as responses_mod  # noqa: E402
from deskmatch.errors import AccommodationsError, PrivacyError  # noqa: E402

#: A string that occurs in no template, no config, no name and no desk id, so
#: finding it anywhere is proof that note text got there.
SENTINEL = "XYZZYPLUGHSENTINEL"

NOTE_HEADER: tuple[str, ...] = (
    "note_id", "timestamp", "email", "name", "note", "client_version",
)

#: Every artefact `deskmatch publish` hands to the department. The notes must be
#: in none of them, and none of them may differ when the flag is passed.
PUBLIC_ARTEFACTS: tuple[str, ...] = (
    "results.json",
    "assignments.csv",
    "responses_anonymized.csv",
    "results_public.pdf",
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def note_row(
    *,
    note_id: str,
    timestamp: str,
    email: str,
    note: str,
    name: str | None = None,
    client_version: str = "test-1",
    **extra: Any,
) -> dict[str, str]:
    row = {
        "note_id": note_id,
        "timestamp": timestamp,
        "email": email,
        "name": name if name is not None else email.split("@")[0],
        "note": note,
        "client_version": client_version,
    }
    row.update({key: str(value) for key, value in extra.items()})
    return row


def write_notes(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
    header: Sequence[str] = NOTE_HEADER,
) -> Path:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=list(header), lineterminator="\n", extrasaction="ignore"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in header})
    return write_text(path, buffer.getvalue())


def pdf_text(path: Path) -> str:
    """The PDF's text layer, whitespace removed.

    Whitespace removed rather than normalised because the question these tests
    ask is "is this string in the document at all", and a report lays text out
    with line breaks wherever the wrap falls. Searching the object stream would
    be weaker: a Flate-compressed stream contains none of the words.
    """
    runs, _sizes, _notes = report.extract_text_runs(Path(path).read_bytes())
    return "".join("".join(run.text.split()) for run in runs)


def pdf_text_pypdf(path: Path) -> str | None:
    """The same, through an independent parser. None when pypdf is absent."""
    try:
        import pypdf  # type: ignore
    except Exception:  # pragma: no cover - pypdf is optional
        return None
    reader = pypdf.PdfReader(os.fspath(path))
    return "".join(
        "".join((page.extract_text() or "").split()) for page in reader.pages
    )


LONG_NOTE = (
    "I am setting this out at length because the short version keeps being "
    "misread. " * 45
) + f"{SENTINEL} is the last thing in this very long note."


def synthetic_notes(roster_emails: Sequence[str]) -> list[dict[str, str]]:
    """The awkward file from the task: a duplicate email, a blank note, an
    author who is not on the roster, a withdrawal, and a very long note."""
    a, b, c, d = roster_emails[0], roster_emails[1], roster_emails[2], roster_emails[3]
    return [
        note_row(note_id="n01", timestamp="2026-09-10T09:00:00-04:00", email=a,
                 note="SUPERSEDEDDRAFT — an early version I later replaced."),
        note_row(note_id="n02", timestamp="2026-09-11T09:00:00-04:00", email=a,
                 note=(f"Not within earshot of desk 12. {SENTINEL} We have an "
                       f"unresolved conflict and being close makes it "
                       f"impossible for me to concentrate.\n\nThe far side of "
                       f"the room would be enough.")),
        note_row(note_id="n03", timestamp="2026-09-11T10:00:00-04:00", email=b,
                 note="   \n \t "),
        note_row(note_id="n04", timestamp="2026-09-11T11:00:00-04:00",
                 email="stranger@gmail.example", name="Someone Else",
                 note="I graduated but the form was still open. Please ignore."),
        note_row(note_id="n05", timestamp="2026-09-11T12:00:00-04:00", email=c,
                 note=LONG_NOTE),
        note_row(note_id="n06", timestamp="2026-09-11T13:00:00-04:00", email=d,
                 note="WITHDRAWNTEXT — something I changed my mind about."),
        note_row(note_id="n07", timestamp="2026-09-11T14:00:00-04:00", email=d,
                 note=""),
    ]


# ==========================================================================
# Ingest (SPEC §7.3)
# ==========================================================================


def test_the_latest_row_per_email_wins_exactly_as_it_does_for_responses(tmp_path):
    """SPEC §3.2, one rule, one implementation.

    The notes loader and the response loader are handed the same pattern of
    (email, timestamp, file position) and must pick the same row — including the
    tie, where the later file position breaks it. Asserted by comparison rather
    than by restating the rule, because a restatement is what drifts.
    """
    pattern = [
        # (id, timestamp, email) in file order; two of them tie on the clock.
        ("r1", "2026-09-10T10:00:00-04:00", "ada@example.test"),
        ("r2", "2026-09-12T10:00:00-04:00", "ada@example.test"),
        ("r3", "2026-09-11T10:00:00-04:00", "ada@example.test"),
        ("r4", "2026-09-11T10:00:00-04:00", "vera@example.test"),
        ("r5", "2026-09-11T10:00:00-04:00", "vera@example.test"),   # ties r4, later
    ]
    k = 2
    response_rows = [
        submission_row(submission_id=rid, timestamp=ts, email=email,
                       choices=[f"D{i:02d}" for i in range(1, k + 1)])
        for rid, ts, email in pattern
    ]
    responses_path = write_text(
        tmp_path / "responses.csv",
        _csv_text(response_header(k), response_rows),
    )
    notes_path = write_notes(
        tmp_path / "notes.csv",
        [note_row(note_id=rid, timestamp=ts, email=email, note=f"note {rid}")
         for rid, ts, email in pattern],
    )

    loaded = responses_mod.load_responses(str(responses_path), k)
    notes = acc.load(notes_path)

    assert {e: s.submission_id for e, s in loaded.latest.items()} == {
        e: n.note_id for e, n in notes.latest.items()
    }
    # And it really is the tie-break being exercised, not just the timestamps.
    assert notes.latest["vera@example.test"].note_id == "r5"
    assert notes.latest["ada@example.test"].note_id == "r2"
    # Sorted-email iteration order, so anything downstream is deterministic.
    assert list(notes.latest) == sorted(notes.latest)


def _csv_text(header, rows):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(header), lineterminator="\n",
                            extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in header})
    return buffer.getvalue()


def test_blank_notes_are_dropped_rather_than_stored_empty(tmp_path):
    path = write_notes(tmp_path / "notes.csv", [
        note_row(note_id="n1", timestamp="2026-09-10T10:00:00-04:00",
                 email="a@example.test", note="   \n\t  \n "),
        note_row(note_id="n2", timestamp="2026-09-10T10:01:00-04:00",
                 email="b@example.test", note="Something real."),
    ])
    notes = acc.load(path)

    assert list(notes.latest) == ["b@example.test"]
    assert all(note.text for note in notes.notes), (
        "an empty note must never be stored; downstream should not have to "
        "decide what one means"
    )
    assert len(notes) == 1


def test_clearing_the_box_withdraws_the_earlier_note(tmp_path):
    """The later, empty row wins — and says so.

    Resolving over the non-blank rows alone would resurrect text the student
    deliberately deleted, which is the worst possible direction for this feature
    to fail in.
    """
    path = write_notes(tmp_path / "notes.csv", [
        note_row(note_id="n1", timestamp="2026-09-10T10:00:00-04:00",
                 email="a@example.test", note="WITHDRAWNTEXT, please forget it."),
        note_row(note_id="n2", timestamp="2026-09-11T10:00:00-04:00",
                 email="a@example.test", note=""),
    ])
    notes = acc.load(path)

    assert notes.latest == {}
    assert not any("WITHDRAWNTEXT" in note.text for note in notes.latest.values())
    assert any("cleared it" in w for w in notes.warnings), notes.warnings
    # It is still in the history, so the coordinator's count is honest -- but
    # `latest`, which is all that gets rendered, does not have it.
    assert len(notes.superseded) == 1


def test_a_blank_row_from_someone_with_no_history_says_nothing(tmp_path):
    path = write_notes(tmp_path / "notes.csv", [
        note_row(note_id="n1", timestamp="2026-09-10T10:00:00-04:00",
                 email="a@example.test", note=""),
    ])
    notes = acc.load(path)
    assert notes.latest == {}
    assert not any("cleared it" in w for w in notes.warnings), (
        "an empty row from somebody who never wrote anything is a no-op, and "
        "printing a warning for it trains the coordinator to skip the warnings"
    )


def test_an_author_who_is_not_on_the_roster_is_a_warning_naming_them(tmp_path):
    """A note is not a claim on a desk (SPEC §7.3).

    Refusing to run the department's assignment because somebody who left last
    year still had the form open would be the wrong trade, so this is a warning
    — and the note is still reported, because it might be the one that matters.
    """
    path = write_notes(tmp_path / "notes.csv", [
        note_row(note_id="n1", timestamp="2026-09-10T10:00:00-04:00",
                 email="ghost@example.test", note="I still have a key."),
        note_row(note_id="n2", timestamp="2026-09-10T10:01:00-04:00",
                 email="ada@example.test", note="I use a wheelchair."),
    ])
    notes = acc.load(path, roster_emails=["ada@example.test"])

    named = [w for w in notes.warnings if "ghost@example.test" in w]
    assert len(named) == 1, notes.warnings
    assert "not on config/roster.csv" in named[0]
    assert "ghost@example.test" in notes.latest, (
        "the note must still be reported; losing it is the failure this warning "
        "exists to avoid"
    )


@pytest.mark.parametrize(
    "mutate, expect_fragment",
    (
        (lambda rows: rows + [dict(rows[0], note_id="", email="x@example.test")],
         "note_id"),
        (lambda rows: rows + [dict(rows[0], note_id=rows[0]["note_id"],
                                   email="y@example.test")],
         "already used"),
        (lambda rows: rows + [dict(rows[0], note_id="n9", email="z@example.test",
                                   timestamp="not a timestamp")],
         "timestamp"),
        (lambda rows: rows + [dict(rows[0], note_id="n9", email="",
                                   note="whose note is this?")],
         "no email address"),
        (lambda rows: rows + [dict(rows[0], note_id="n9", email="w@example.test",
                                   timestamp="2026-09-10 10:00:00")],
         "no UTC offset"),
    ),
)
def test_an_odd_row_is_a_warning_and_never_stops_the_run(
    tmp_path, mutate, expect_fragment
):
    base = [note_row(note_id="n1", timestamp="2026-09-10T10:00:00-04:00",
                     email="a@example.test", note="A perfectly ordinary note.")]
    path = write_notes(tmp_path / "notes.csv", mutate(base))

    notes = acc.load(path)      # must not raise

    assert any(expect_fragment in w for w in notes.warnings), notes.warnings
    assert "a@example.test" in notes.latest, "the good row is still read"


def test_extra_and_missing_optional_columns_are_warnings(tmp_path):
    path = write_notes(
        tmp_path / "notes.csv",
        [note_row(note_id="n1", timestamp="2026-09-10T10:00:00-04:00",
                  email="a@example.test", note="Hello.", triage="urgent")],
        header=("note_id", "timestamp", "email", "name", "note", "triage"),
    )
    notes = acc.load(path)
    assert len(notes) == 1
    assert any("client_version" in w for w in notes.warnings)
    assert any("triage" in w for w in notes.warnings)


@pytest.mark.parametrize("missing", acc.REQUIRED_COLUMNS)
def test_a_missing_required_column_is_an_error_that_names_every_problem(
    tmp_path, missing
):
    """The one thing that *does* stop the run: a file the notes cannot be found
    in. Silently reading zero notes out of it would be worse — the coordinator
    would believe nobody wrote anything."""
    header = tuple(c for c in NOTE_HEADER if c != missing)
    path = write_notes(
        tmp_path / "notes.csv",
        [note_row(note_id="n1", timestamp="2026-09-10T10:00:00-04:00",
                  email="a@example.test", note="Hello.")],
        header=header,
    )
    with pytest.raises(AccommodationsError) as excinfo:
        acc.load(path)
    rendered = excinfo.value.render()
    assert missing in rendered
    assert "SPEC §7.3" in rendered


def test_an_unreadable_file_is_an_error_not_a_traceback(tmp_path):
    with pytest.raises(AccommodationsError) as excinfo:
        acc.load(tmp_path / "does_not_exist.csv")
    assert "cannot be read" in excinfo.value.render()

    with pytest.raises(AccommodationsError) as excinfo:
        acc.load(write_text(tmp_path / "notes.txt", "note_id\n"))
    assert "not a supported notes format" in excinfo.value.render()


def test_paragraphs_and_line_endings_survive_but_surrounding_space_does_not(
    tmp_path
):
    path = write_notes(tmp_path / "notes.csv", [
        note_row(note_id="n1", timestamp="2026-09-10T10:00:00-04:00",
                 email="a@example.test",
                 note="  First paragraph.  \r\n\r\nSecond paragraph.\r\n  "),
    ])
    note = acc.load(path).latest["a@example.test"]
    assert note.text == "First paragraph.\n\nSecond paragraph."


# ==========================================================================
# The coordinator-only text file
# ==========================================================================


def test_the_text_dump_is_0600_and_says_it_is_not_for_distribution(tmp_path):
    path = write_notes(tmp_path / "notes.csv", [
        note_row(note_id="n1", timestamp="2026-09-10T10:00:00-04:00",
                 email="a@example.test", note=f"{SENTINEL} keep this private."),
    ])
    notes = acc.load(path)
    target = tmp_path / "out" / acc.COORDINATOR_TXT_NAME
    acc.write_coordinator_text(target, notes)

    text = target.read_text(encoding="utf-8")
    assert SENTINEL in text
    assert "NOT FOR DISTRIBUTION" in text
    if os.name == "posix":
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == acc.COORDINATOR_FILE_MODE, oct(mode)


def test_the_text_dump_forces_0600_over_a_pre_existing_loose_file(tmp_path):
    """`O_CREAT`'s mode is ignored for a file that already exists, and the umask
    eats it for one that does not. Neither is allowed to leave this readable."""
    if os.name != "posix":
        pytest.skip("POSIX permission bits")
    target = tmp_path / acc.COORDINATOR_TXT_NAME
    target.write_text("stale", encoding="utf-8")
    target.chmod(0o644)

    path = write_notes(tmp_path / "notes.csv", [
        note_row(note_id="n1", timestamp="2026-09-10T10:00:00-04:00",
                 email="a@example.test", note="Private."),
    ])
    acc.write_coordinator_text(target, acc.load(path))
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert "stale" not in target.read_text(encoding="utf-8")


def test_the_text_dump_is_deterministic_and_reproduces_a_long_note_whole(tmp_path):
    path = write_notes(tmp_path / "notes.csv", [
        note_row(note_id="n1", timestamp="2026-09-10T10:00:00-04:00",
                 email="b@example.test", note=LONG_NOTE),
        note_row(note_id="n2", timestamp="2026-09-10T10:01:00-04:00",
                 email="a@example.test", note="Short one."),
    ])
    notes = acc.load(path)
    first = acc.render_coordinator_text(notes)
    second = acc.render_coordinator_text(notes)
    assert first == second

    # Every word of the long note survives the wrapping, in order.
    assert acc.normalise_words(LONG_NOTE) in acc.normalise_words(first)
    # Sorted by email, so the file does not depend on row order.
    assert first.index("a@example.test") < first.index("b@example.test")


# ==========================================================================
# The leak detector
# ==========================================================================


def test_fingerprints_shingle_long_notes_and_keep_short_ones_whole():
    long_note = "one two three four five six seven"
    marks = acc.fingerprints(long_note)
    assert marks[0] == "one two three four five"
    assert marks[-1] == "three four five six seven"
    assert acc.fingerprints("distance from Ada") == ("distance from ada",)
    # Too short to be distinctive; matching it would fire on ordinary prose.
    assert acc.fingerprints("no") == ()
    assert acc.fingerprints("   ") == ()


def test_the_leak_detector_ignores_layout_and_catches_a_quoted_fragment(tmp_path):
    path = write_notes(tmp_path / "notes.csv", [
        note_row(note_id="n1", timestamp="2026-09-10T10:00:00-04:00",
                 email="a@example.test",
                 note="I need to be at least two desks from the printer."),
    ])
    notes = acc.load(path)

    clean = {"results.json": json.dumps({"assignments": [{"desk_id": "D01"}]})}
    assert acc.find_leaks(notes, clean) == ()

    # Re-wrapped, re-punctuated, re-cased -- still the same words.
    leaked = {"summary.txt": "…be at LEAST two\n   desks, from the printer…"}
    findings = acc.find_leaks(notes, leaked)
    assert len(findings) == 1
    assert "a@example.test" in findings[0]
    with pytest.raises(PrivacyError) as excinfo:
        acc.assert_absent_from(notes, leaked)
    assert "SPEC §7.3" in str(excinfo.value)


def test_a_superseded_note_must_not_leak_either(tmp_path):
    path = write_notes(tmp_path / "notes.csv", [
        note_row(note_id="n1", timestamp="2026-09-10T10:00:00-04:00",
                 email="a@example.test",
                 note="The first draft mentioned a diagnosis by name."),
        note_row(note_id="n2", timestamp="2026-09-11T10:00:00-04:00",
                 email="a@example.test", note="A calmer second version."),
    ])
    notes = acc.load(path)
    assert acc.find_leaks(
        notes, {"leak.csv": "the first draft mentioned a diagnosis by name"}
    )


# ==========================================================================
# End to end: the whole point
# ==========================================================================


@pytest.fixture(scope="module")
def two_runs(tmp_path_factory, request):
    """The same solve twice: without `--accommodations`, then with it.

    Module-scoped because it runs the real CLI end to end (PDFs included) and
    every assertion below is a different question about the same pair of output
    directories.
    """
    repo_root = Path(__file__).resolve().parent.parent
    config_dir = repo_root / "config"
    responses_csv = find_real_responses_csv(
        *(repo_root / name for name in RESPONSES_SEARCH_DIRS)
    )
    if not config_dir.is_dir() or not responses_csv.is_file():
        pytest.skip("the shipped config or response export is missing")

    from deskmatch.config import load_config

    # People come from the RESPONSES, not the roster. config/roster.csv ships
    # empty now -- the domain-restricted link is the membership check -- so the
    # pool is whoever submitted, and a note is only interesting when it belongs
    # to somebody who is actually in that pool.
    from deskmatch import responses as _responses_mod

    _config = load_config(config_dir)
    roster = sorted(
        _responses_mod.load_responses(str(responses_csv), _config.k).latest
    )
    if not roster:
        pytest.skip("the shipped response export has no submissions")
    base = tmp_path_factory.mktemp("accommodations_e2e")
    notes_csv = write_notes(base / "notes.csv", synthetic_notes(roster))

    without, with_notes = base / "without", base / "with"
    common = [
        "solve", "--config", str(config_dir), "--responses", str(responses_csv),
        "--trials", "50", "--full",
    ]
    assert cli.main(common + ["--out", str(without)]) == 0
    assert cli.main(
        common + ["--out", str(with_notes), "--accommodations", str(notes_csv)]
    ) == 0
    return without, with_notes, notes_csv, roster


def test_the_public_artefacts_are_byte_identical_with_and_without_notes(two_runs):
    """The property everything else rests on.

    If not one byte of any published file changes, then no note can have reached
    one, and no note can have influenced the assignment either — a solve that
    read the notes would have to produce a different `results.json` to be worth
    anything. This is invariant I2 and SPEC §7.3 in the same assertion.
    """
    without, with_notes, _notes_csv, _roster = two_runs
    for name in PUBLIC_ARTEFACTS:
        a, b = (without / name).read_bytes(), (with_notes / name).read_bytes()
        assert a == b, (
            f"{name} differs when --accommodations is passed. The private notes "
            f"are advisory (SPEC §7.3, I2); they must not touch a published file."
        )


def test_the_canonical_hash_is_unchanged_by_the_notes(two_runs):
    """Determinism, stated the way `verify` states it."""
    without, with_notes, _notes_csv, _roster = two_runs
    hashes = [
        json.loads((d / "results.json").read_text(encoding="utf-8"))
        ["provenance"]["canonical_sha256"]
        for d in (without, with_notes)
    ]
    assert hashes[0] == hashes[1]
    # And nothing about the notes was recorded in the document at all.
    document = (with_notes / "results.json").read_text(encoding="utf-8")
    assert "accommodation" not in document.casefold()
    assert "note_id" not in document


def test_the_sentinel_reaches_the_coordinator_files_and_nothing_else(two_runs):
    """The direct statement of SPEC §7.3, checked against the bytes on disk.

    The PDFs are searched through their *text layer*, not their object stream: a
    Flate-compressed stream contains none of the words, so grepping the raw file
    would pass vacuously.
    """
    _without, out, _notes_csv, _roster = two_runs

    coordinator_txt = out / acc.COORDINATOR_TXT_NAME
    assert coordinator_txt.is_file()
    assert SENTINEL in coordinator_txt.read_text(encoding="utf-8")

    coordinator_pdf = out / "results_coordinator.pdf"
    assert SENTINEL in pdf_text(coordinator_pdf)
    independent = pdf_text_pypdf(coordinator_pdf)
    if independent is not None:
        assert SENTINEL in independent, (
            "our own extractor found the note but an independent parser did not; "
            "trust the independent one"
        )

    for name in PUBLIC_ARTEFACTS:
        path = out / name
        raw = path.read_bytes()
        assert SENTINEL.encode() not in raw, f"{name} contains the sentinel"
        if name.endswith(".pdf"):
            assert SENTINEL not in pdf_text(path), f"{name} text layer leaks"
            other = pdf_text_pypdf(path)
            if other is not None:
                assert SENTINEL not in other, f"{name} leaks per pypdf"

    # The whole note, not merely the sentinel: no published file may carry any
    # distinctive run of words from any note in the file.
    notes = acc.load(_notes_csv)
    acc.assert_absent_from(
        notes, {name: (out / name).read_bytes() for name in PUBLIC_ARTEFACTS}
    )
    assert acc.find_leaks(
        notes, {"public.pdf": pdf_text(out / "results_public.pdf")}
    ) == ()


def test_the_coordinator_report_carries_every_note_whole(two_runs):
    _without, out, notes_csv, _roster = two_runs
    notes = acc.load(notes_csv)
    assert len(notes) >= 3, "the fixture is meant to have several notes in it"

    text = pdf_text(out / "results_coordinator.pdf")
    squashed_txt = "".join(
        (out / acc.COORDINATOR_TXT_NAME).read_text(encoding="utf-8").split()
    )
    for email, note in notes.latest.items():
        squashed = "".join(note.text.split())
        assert squashed in text, f"{email}'s note is truncated in the PDF"
        assert squashed in squashed_txt, f"{email}'s note is truncated in the txt"
        assert email in squashed_txt

    # The withdrawn and superseded drafts are NOT reproduced.
    assert "WITHDRAWNTEXT" not in text
    assert "SUPERSEDEDDRAFT" not in text
    assert "WITHDRAWNTEXT" not in squashed_txt


def test_the_public_report_page_kinds_never_include_the_notes_section(two_runs):
    """The structural half of the guarantee (`_Deck.assert_audience`)."""
    assert "accommodations" in report.COORDINATOR_ONLY_PAGE_KINDS
    assert "accommodations" not in report.PUBLIC_PAGE_KINDS


def test_the_notes_stay_out_of_the_publish_folder(two_runs, tmp_path):
    _without, out, _notes_csv, _roster = two_runs
    repo_root = Path(__file__).resolve().parent.parent
    destination = tmp_path / "publish"
    assert cli.main([
        "publish", "--config", str(repo_root / "config"),
        "--results-dir", str(out), "--out", str(destination),
    ]) == 0

    assert not (destination / acc.COORDINATOR_TXT_NAME).exists()
    assert not (destination / "results_coordinator.pdf").exists()
    for path in destination.rglob("*"):
        if path.is_file():
            assert SENTINEL.encode() not in path.read_bytes(), path


# ==========================================================================
# Does the guard actually have teeth?
# ==========================================================================


def test_the_public_audit_fails_when_note_text_really_is_in_the_pdf(two_runs):
    """A privacy check that cannot fail is not a check.

    Rather than manufacturing a leak — which would mean writing the very code
    this repository must not contain — the audit is handed a "note" whose text is
    a sentence the public report genuinely prints. If check G is wired up, it
    fires; if somebody removes it, this test goes green-to-red immediately.
    """
    _without, out, _notes_csv, _roster = two_runs
    public = out / "results_public.pdf"

    planted = acc.Accommodations(
        notes=(acc.Note(
            note_id="planted", timestamp="", email="mole@example.test", name="",
            text="There is no manual override anywhere in the code.",
        ),),
        latest={}, source_path="(planted)", sha256="",
    )
    audit = report.audit_public_pdf(public, None, None, None,
                                    accommodations=planted, expect_names=False)
    assert not audit.ok
    assert any(f.check == "G" for f in audit.findings), audit.render()
    assert "mole@example.test" in audit.render()
    assert "G private-note text" in audit.checks

    with pytest.raises(PrivacyError):
        report.assert_public_safe(public, None, None, None,
                                  accommodations=planted, corroborate=False)

    # ... and the same audit passes when the "note" says something the report
    # does not, so the check is discriminating rather than merely loud.
    innocent = acc.Accommodations(
        notes=(acc.Note(note_id="ok", timestamp="", email="a@example.test",
                        name="", text=f"{SENTINEL} never appears in the report."),),
        latest={}, source_path="", sha256="",
    )
    clean = report.audit_public_pdf(public, None, None, None,
                                    accommodations=innocent, expect_names=False)
    assert not [f for f in clean.findings if f.check == "G"], clean.render()


def test_a_note_alone_cannot_change_the_assignment(two_runs):
    """SPEC I2 restated as an experiment rather than as a promise."""
    without, with_notes, _notes_csv, _roster = two_runs
    a = json.loads((without / "results.json").read_text(encoding="utf-8"))
    b = json.loads((with_notes / "results.json").read_text(encoding="utf-8"))
    assert a["assignments"] == b["assignments"]
    assert a == b
