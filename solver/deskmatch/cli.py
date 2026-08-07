"""Command-line interface.

Deliberate omission: `solve` has no `--seed` flag.

The seed is read from config/scoring.json and nowhere else. A command-line
override would be the single easiest way for a coordinator -- who is also a
participant -- to shop for a favourable tie-break, and no amount of logging
would make that as safe as simply not building the door. Changing the seed means
editing a tracked file, which shows up in git. Sensitivity analysis over other
seeds is driven by `sensitivity_seeds` in the same file and is reported
alongside the result, so the question "would another seed have changed this?"
is answered in the PDF rather than by re-running with a different flag.

`solve --accommodations` is not a counter-example to any of that. It feeds the
private notes (SPEC §7.3) to the coordinator's own outputs and to nothing else:
`results.json`, `assignments.csv`, `responses_anonymized.csv` and
`results_public.pdf` are byte-identical whether or not the flag is passed, which
is asserted by a test that runs both ways and diffs them. A note is advice to a
human. Acting on one means changing an input -- a desk marked unavailable in
`rooms.json` -- which is visible in git, exactly like changing the seed.

Exit codes are documented in docs/SPEC.md §9.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .errors import DeskMatchError, InfeasibleError


def _eprint(*args) -> None:
    print(*args, file=sys.stderr)


def _rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    from .config import load_config

    cfg = load_config(Path(args.config))
    print(f"Config OK: {args.config}")
    print(f"  rooms      : {len(cfg.rooms.rooms)} room(s), "
          f"{len(cfg.rooms.all_desks)} desk(s), {len(cfg.rooms.zones)} zone(s)")
    for zone_id in sorted(cfg.rooms.zones):
        n = sum(1 for d in cfg.rooms.all_desks if d.zone == zone_id)
        print(f"      {zone_id}: {n} desk(s) -- {cfg.rooms.zones[zone_id].label}")
    print(f"  eligibility: {len(cfg.eligibility.rules)} rule(s)")
    if cfg.eligibility.candidacy_options:
        print(f"      form offers: {', '.join(cfg.eligibility.candidacy_options)}")
    print(f"  roster     : {len(cfg.roster.people)} person/people, "
          f"{sum(1 for p in cfg.roster.people if p.keeps_desk)} keeping their desk")
    print(f"  scoring    : K={cfg.k}, primary curve '{cfg.scoring.primary_curve}' = "
          f"{[str(v) for v in cfg.scoring.curve()]}")
    seed = cfg.scoring.resolved_seed()
    if cfg.scoring.seed_year is not None:
        origin = "cycle year, taken from the clock" if cfg.scoring.seed_year_from_clock \
            else "cycle year, pinned in scoring.json"
        print(f"  seed       : {seed!r}  ({origin})")
    else:
        print(f"  seed       : {seed!r}")
    if cfg.warnings:
        print(f"\n{len(cfg.warnings)} warning(s):")
        for w in cfg.warnings:
            print(f"  ! {w}")
    return 0


# --------------------------------------------------------------------------
# solve
# --------------------------------------------------------------------------


def cmd_solve(args: argparse.Namespace) -> int:
    from . import accommodations as acc_mod
    from . import baselines, diagnostics, problem as problem_mod, provenance, report
    from . import responses as responses_mod
    from . import solve as solve_mod
    from .config import load_config

    cfg = load_config(Path(args.config))
    resp = responses_mod.load_responses(Path(args.responses), cfg.k)
    # Loaded here, before anything is written, so a broken notes export fails the
    # run early rather than after the department's PDF is on disk. It is read
    # only by the coordinator outputs below: no part of the solve can see it
    # (SPEC I2, §7.3).
    notes = None
    if args.accommodations:
        notes = acc_mod.load(
            Path(args.accommodations),
            roster_emails=[p.email for p in cfg.roster.people],
        )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    seed = cfg.scoring.resolved_seed()

    _rule("INPUTS")
    print(f"  config      : {args.config}")
    print(f"  responses   : {args.responses}")
    print(f"  sha256      : {resp.sha256}")
    print(f"  submissions : {len(resp.submissions)} row(s), "
          f"{len(resp.latest)} person/people after de-duplication")
    if notes is not None:
        # The COUNT is a normal run-summary line. The CONTENT goes only to the
        # coordinator report and the 0600 text file -- never to stdout, which
        # ends up in scrollback, terminal logs and screen shares.
        print(f"  notes       : {len(notes)} private note(s) recorded "
              f"(content is written only to the coordinator files)")
    print(f"  seed        : {seed!r}")
    if cfg.scoring.seed_year is not None and cfg.scoring.seed_year_from_clock:
        print(f"                (cycle year {cfg.scoring.seed_year}, resolved now and "
              f"recorded in results.json;\n"
              f"                 pin it in scoring.json to re-run this cycle later)")
    if cfg.scoring.seed_committed_at:
        print(f"  committed   : {cfg.scoring.seed_committed_at}")
    elif cfg.scoring.seed_year is None:
        # Only meaningful when a human picked the seed. Under seed_year there is
        # nothing to announce -- the seed is the calendar year, which nobody
        # chooses and everybody can predict.
        _eprint("  ! scoring.json has no seed_committed_at. Record when you "
                "announced the seed -- it is part of the audit trail.")

    # Cross-check the pre-lock claim log against the roster, when given one.
    # See deskmatch/keepers.py for why: the solver reads the roster, the form
    # reads the claim log, and if the merge was skipped only the solver is wrong.
    claims: tuple = ()
    if args.keepers:
        from . import keepers as keepers_mod

        claims = keepers_mod.load_claims(Path(args.keepers))
        if cfg.roster.people:
            # There is a roster, so it is supposed to already record these.
            # Disagreement means the merge was skipped, and the solver would
            # otherwise give a kept desk away.
            mismatches = keepers_mod.verify_against_roster(claims, cfg.roster)
            if mismatches:
                raise keepers_mod.KeepersError(mismatches)
            print(f"  keepers     : {len(claims)} active claim(s), all reflected "
                  f"in the roster")
        else:
            # No roster to reconcile against: the claim log IS the record, and
            # build_problem takes it directly. This is the normal path now --
            # it removes merge_keepers.py from the critical route entirely.
            print(f"  keepers     : {len(claims)} active claim(s), taken from the "
                  f"claim log (no roster to reconcile against)")

    build = problem_mod.build_problem(cfg, resp, args.curve, claims=claims)
    prob = build.problem

    _rule("POOL")
    print(f"  {prob.n_people} people competing for {prob.n_desks} desks (K={prob.k})")
    # Printed even when zero, deliberately. After a pre-lock phase, "0 desk(s)
    # held" is the visible symptom of the claims never having been merged into
    # the roster -- and that mistake is otherwise silent all the way to
    # publication. Staying quiet on zero hides exactly the case worth seeing.
    print(f"  {len(build.locked_desks)} desk(s) held by people keeping their seat")
    if not build.locked_desks:
        print("      (if you have just run a pre-lock phase, this should not be "
              "zero --\n       check the claims were merged with "
              "tools/merge_keepers.py)")
    pool_notes = build.render_warnings()
    if pool_notes:
        print("\nNotes:")
        print(pool_notes)
    if notes is not None and notes.warnings:
        print(f"\nPrivate notes file ({len(notes.warnings)} warning(s)):")
        for warning in notes.warnings:
            print(f"  ! {warning}")

    try:
        solution = solve_mod.solve(prob, seed, args.backend)
    except InfeasibleError as exc:
        return _handle_infeasible(exc, cfg, build, out, seed, notes)

    _rule("RESULT")
    hist = solution.rank_histogram()
    for r, count in enumerate(hist, start=1):
        pct = 100.0 * count / max(prob.n_people, 1)
        print(f"  choice {r}: {count:4d}  ({pct:5.1f}%)")
    mean_rank = (
        sum(a.rank_received for a in solution.assignments) / len(solution.assignments)
        if solution.assignments else float("nan")
    )
    print(f"  mean rank received: {mean_rank:.3f}")
    print(f"  total points      : {solution.total_points}")

    # ---- outputs --------------------------------------------------------
    prov = provenance.build_provenance(
        config=cfg, responses=resp, solution=solution, args_out=str(out),
        config_path=str(args.config), responses_path=str(args.responses),
    )

    results_doc = provenance.results_document(cfg, build, solution, prov)
    results_path = out / "results.json"
    digest = provenance.write_results_json(results_path, results_doc)
    print(f"\n  wrote {results_path}  (sha256 {digest[:16]}...)")

    provenance.write_assignments_csv(out / "assignments.csv", solution)
    print(f"  wrote {out / 'assignments.csv'}")

    anon_path = out / "responses_anonymized.csv"
    responses_mod.write_anonymized(anon_path, resp, seed)
    print(f"  wrote {anon_path}")

    if notes is not None:
        # SPEC §7.3: these three files go to the whole department, so the notes
        # must not be in them. Checked against the bytes just written rather than
        # trusted, for the same reason the public PDF is re-opened and audited:
        # a leak here cannot be undone once the folder has been posted.
        acc_mod.assert_absent_from(notes, {
            str(results_path): results_path.read_bytes(),
            str(out / "assignments.csv"): (out / "assignments.csv").read_bytes(),
            str(anon_path): anon_path.read_bytes(),
        })

    # ---- analysis for the report ---------------------------------------
    rsd = baselines.random_serial_dictatorship(prob, args.trials, seed)
    unif = baselines.uniform_random_assignment(prob, args.trials, seed)
    curve_rows = baselines.alternative_curve_outcomes(
        cfg, resp, seed, list(cfg.scoring.comparison_curves), args.backend
    )
    seed_rows = baselines.alternative_seed_outcomes(
        prob, [seed, *cfg.scoring.sensitivity_seeds], args.backend
    )

    pct = rsd.percentile_of(solution.total_points_scaled)
    print(f"\n  optimal total beats {pct:.1f}% of {args.trials} random "
          f"serial-dictatorship runs")
    print(f"  RSD seated everyone within top-{prob.k} in "
          f"{100 * rsd.complete_fraction:.1f}% of runs")
    moved = [row for row in seed_rows if row[3] > 0]
    if not moved:
        print("  tie-break: no alternative seed changed anyone's desk this year")
    else:
        worst = max(row[3] for row in seed_rows)
        print(f"  tie-break: up to {worst} person/people move under other seeds")

    public_pdf = out / "results_public.pdf"
    report.build_report(
        public_pdf, cfg, build, solution, full=False,
        baselines=[rsd, unif], curve_rows=curve_rows, seed_rows=seed_rows,
        provenance=prov,
        # Not drawn: searched for. Passing the notes here makes the finished
        # public PDF get audited for their text as well as for attributed
        # rankings. It cannot change a byte of the file (SPEC §7.3).
        accommodations=notes,
    )
    print(f"  wrote {public_pdf}")

    if notes is not None:
        notes_path = out / acc_mod.COORDINATOR_TXT_NAME
        acc_mod.write_coordinator_text(notes_path, notes, solution=solution,
                                       config=cfg)
        print(f"  wrote {notes_path}   *** PRIVATE NOTES -- 0600, DO NOT SHARE ***")

    if args.full:
        coord_pdf = out / "results_coordinator.pdf"
        report.build_report(
            coord_pdf, cfg, build, solution, full=True,
            baselines=[rsd, unif], curve_rows=curve_rows, seed_rows=seed_rows,
            provenance=prov, accommodations=notes,
        )
        print(f"  wrote {coord_pdf}   *** CONTAINS INDIVIDUAL PREFERENCES ***")

    if args.verify:
        if digest != args.verify:
            _eprint(f"\nVERIFICATION FAILED\n  expected {args.verify}\n  got      {digest}")
            return 5
        print(f"\n  verified: results hash matches {args.verify}")

    _rule("DONE")
    print(f"  Publish: {results_path.name}, {public_pdf.name}, "
          f"{anon_path.name}, and the response file whose sha256 is\n"
          f"  {resp.sha256}")
    if notes is not None:
        print(f"  Do NOT publish: {acc_mod.COORDINATOR_TXT_NAME}"
              + (f", {(out / 'results_coordinator.pdf').name}" if args.full else ""))
    return 0


def _handle_infeasible(exc, cfg, build, out: Path, seed: str, notes=None) -> int:
    """The K-floor could not be met. Write diagnostics and refuse to assign."""
    from . import accommodations as acc_mod
    from . import diagnostics, report

    diagnosis = exc.diagnosis
    prob = build.problem

    _rule("INFEASIBLE -- NO ASSIGNMENT PRODUCED")
    print(diagnosis.summary())

    import json
    diag_path = out / "diagnostics.json"
    diag_path.write_text(
        json.dumps(diagnostics.diagnosis_to_dict(diagnosis), indent=2,
                   sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\n  wrote {diag_path}")

    entries = diagnostics.write_round2(
        out / "round2_input.json", out / "round2_roster.csv", prob, diagnosis
    )
    print(f"  wrote {out / 'round2_input.json'}")
    print(f"  wrote {out / 'round2_roster.csv'}  ({len(entries)} student(s) to contact)")

    try:
        pdf = out / "diagnostic_report.pdf"
        report.build_diagnostic_report(pdf, cfg, build, diagnosis)
        print(f"  wrote {pdf}")
    except Exception as err:  # a missing figure must not hide the diagnosis
        _eprint(f"  ! could not render the diagnostic PDF: {err}")

    if notes is not None:
        # Written on a failed run too. This is exactly when the coordinator has
        # to go and talk to people, and the notes are what tell them who they are
        # talking to. There is no assignment to report alongside them.
        notes_path = out / acc_mod.COORDINATOR_TXT_NAME
        acc_mod.write_coordinator_text(notes_path, notes, solution=None, config=cfg)
        print(f"  wrote {notes_path}   *** PRIVATE NOTES -- 0600, DO NOT SHARE ***")

    print(
        "\nWhat to do next:\n"
        "  1. Contact the students named above. They do not have to change their\n"
        "     top pick -- they only need to rank further down the list.\n"
        f"  2. Re-open the form for them using {out / 'round2_roster.csv'}.\n"
        "  3. Re-export and re-run. Do not hand-edit the assignment; there is no\n"
        "     path to do that and adding one would break the guarantee."
    )
    return 2


# --------------------------------------------------------------------------
# check (pre-deadline)
# --------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    from . import diagnostics, problem as problem_mod
    from . import responses as responses_mod
    from .config import load_config

    cfg = load_config(Path(args.config))
    resp = responses_mod.load_responses(Path(args.responses), cfg.k)

    eligible_roster = [p for p in cfg.roster.people if not p.keeps_desk]
    outstanding = [p for p in eligible_roster if p.email not in resp.latest]

    build = problem_mod.build_problem(cfg, resp, args.curve)
    result = diagnostics.preflight(
        build.problem, len(outstanding), cfg.scoring.resolved_seed()
    )

    _rule("PRE-DEADLINE FEASIBILITY CHECK")
    print(result.render())

    if outstanding and args.list_outstanding:
        print(f"\nStill to submit ({len(outstanding)}):")
        for p in outstanding:
            print(f"  {p.name} <{p.email}>")

    return 0 if result.would_succeed else 3


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


def cmd_verify(args: argparse.Namespace) -> int:
    """Re-run from published inputs and confirm the published result.

    This is the command a student runs to check the coordinator's work.
    """
    from . import problem as problem_mod, provenance
    from . import responses as responses_mod
    from . import solve as solve_mod
    from .config import load_config

    cfg = load_config(Path(args.config))
    resp = responses_mod.load_responses(Path(args.responses), cfg.k)

    published = provenance.read_results_json(Path(args.results))
    claimed_hash = published.get("provenance", {}).get("canonical_sha256")
    recomputed_self = provenance.canonical_hash_of(published)

    _rule("VERIFY")
    print(f"  published results : {args.results}")
    print(f"  claimed hash      : {claimed_hash}")
    print(f"  recomputed hash   : {recomputed_self}")
    if claimed_hash != recomputed_self:
        _eprint("  MISMATCH: the results file's own hash does not match its contents.")
        return 5
    print("  the results file is internally consistent")

    claimed_resp = published.get("provenance", {}).get("responses_sha256")
    print(f"  responses sha256  : {resp.sha256}")
    if claimed_resp and claimed_resp != resp.sha256:
        _eprint(f"  MISMATCH: the results were produced from a DIFFERENT response "
                f"file (claimed {claimed_resp}).")
        return 5
    print("  the response file matches the one that was used")

    build = problem_mod.build_problem(cfg, resp, published.get("curve_name"))
    solution = solve_mod.solve(
        build.problem, published["seed_string"], args.backend
    )
    ours = {a.email: a.desk_id for a in solution.assignments}
    theirs = {row["email"]: row["desk_id"] for row in published["assignments"]}

    if ours != theirs:
        diffs = sorted(
            set(ours) | set(theirs),
            key=lambda e: e,
        )
        _eprint("  MISMATCH: re-running produced a different assignment:")
        for email in diffs:
            if ours.get(email) != theirs.get(email):
                _eprint(f"    {email}: published {theirs.get(email)} vs "
                        f"recomputed {ours.get(email)}")
        return 5

    print(f"  re-ran the solver: all {len(ours)} assignments identical")
    print("\n  VERIFIED.")
    return 0


# --------------------------------------------------------------------------
# publish helper
# --------------------------------------------------------------------------


def cmd_publish(args: argparse.Namespace) -> int:
    """Assemble the folder of artefacts to hand to the department.

    Exists so "publish the anonymized responses plus the solver" is one command
    rather than a checklist someone can do 80% of.
    """
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = Path(args.results_dir)
    repo = Path(__file__).resolve().parents[2]

    # An ALLOW-list, deliberately, not "everything in results_dir minus a few".
    # `out/` also holds the coordinator PDF and accommodations_coordinator.txt;
    # a deny-list would publish the next coordinator-only file somebody adds.
    copied: list[str] = []
    for name in ("results.json", "results_public.pdf", "assignments.csv",
                 "responses_anonymized.csv", "diagnostics.json"):
        src = results / name
        if src.exists():
            shutil.copy2(src, out / name)
            copied.append(name)

    cfg_dst = out / "config"
    if cfg_dst.exists():
        shutil.rmtree(cfg_dst)
    shutil.copytree(Path(args.config), cfg_dst)
    copied.append("config/")

    solver_dst = out / "solver"
    if solver_dst.exists():
        shutil.rmtree(solver_dst)
    shutil.copytree(
        repo / "solver", solver_dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )
    copied.append("solver/")

    for doc in ("README.md", "docs/SPEC.md"):
        src = repo / doc
        if src.exists():
            dst = out / Path(doc).name
            shutil.copy2(src, dst)
            copied.append(Path(doc).name)

    print(f"Published to {out}:")
    for name in copied:
        print(f"  {name}")
    print(
        "\nThis folder is self-contained: anyone can run\n"
        "  python -m deskmatch solve --config config/ "
        "--responses responses_anonymized.csv --out check/\n"
        "and compare their results.json hash against the published one."
    )
    return 0


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deskmatch",
        description="Preference-matching desk assignment. See docs/SPEC.md and "
                    "the runbook in README.md.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("--config", default="config", help="config directory")
        sp.add_argument("--backend", default=None,
                        help="solver backend (default scipy-jv)")

    v = sub.add_parser("validate", help="check the config files and stop")
    v.add_argument("--config", default="config")
    v.set_defaults(func=cmd_validate)

    s = sub.add_parser("solve", help="run the assignment and write the report")
    add_common(s)
    s.add_argument("--responses", required=True)
    s.add_argument("--out", default="out")
    s.add_argument("--curve", default=None,
                   help="override which scoring curve is primary (recorded in "
                        "the output; the default comes from scoring.json)")
    s.add_argument("--accommodations", default=None, metavar="FILE.csv",
                   help="the exported private-notes sheet (SPEC §7.3). Optional. "
                        "The notes never enter the solve and never reach a "
                        "published file; they are written to "
                        "out/accommodations_coordinator.txt (mode 0600) and, with "
                        "--full, to the coordinator PDF")
    s.add_argument("--full", action="store_true",
                   help="also write the coordinator report, which contains "
                        "individual preferences")
    s.add_argument("--trials", type=int, default=5000,
                   help="Monte-Carlo trials for the baseline comparison")
    s.add_argument("--keepers", default=None,
                   help="the Keepers tab exported as CSV. Optional, and worth "
                        "passing after a pre-lock phase: it refuses to run if a "
                        "claimed desk is not yet recorded in roster.csv")
    s.add_argument("--verify", default=None,
                   help="assert the results hash equals this value")
    s.set_defaults(func=cmd_solve)

    c = sub.add_parser("check", help="pre-deadline feasibility check on partial "
                                     "responses")
    add_common(c)
    c.add_argument("--responses", required=True)
    c.add_argument("--curve", default=None)
    c.add_argument("--list-outstanding", action="store_true")
    c.set_defaults(func=cmd_check)

    vf = sub.add_parser("verify", help="re-run from published inputs and compare")
    add_common(vf)
    vf.add_argument("--responses", required=True)
    vf.add_argument("--results", required=True)
    vf.set_defaults(func=cmd_verify)

    pb = sub.add_parser("publish", help="assemble the public artefact folder")
    pb.add_argument("--config", default="config")
    pb.add_argument("--results-dir", default="out")
    pb.add_argument("--out", default="publish")
    pb.set_defaults(func=cmd_publish)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except DeskMatchError as exc:
        _eprint(f"\n{exc}")
        return exc.exit_code
    except KeyboardInterrupt:
        _eprint("\ninterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
