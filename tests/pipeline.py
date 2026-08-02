"""The end-to-end `results.json` pipeline, shared by tests and by subprocesses.

This is deliberately **not** a test module. ``tests/test_determinism.py`` both
imports it and executes it as a script under two different ``PYTHONHASHSEED``
values, so it must import cleanly with no pytest dependency and must not read
anything from the environment beyond what it is told.

It reproduces the `results.json` half of ``deskmatch.cli.cmd_solve`` exactly --
load config, load responses, build the problem, solve, build provenance, write
`results.json` -- and nothing else. SPEC §7 names `results.json` "the canonical;
the reproducibility target"; the PDFs are explicitly *not* byte-reproducible
across matplotlib versions (README, "Reproducibility, precisely"), so they have
no place in a determinism test.

Why not just call ``cli.cmd_solve``? Because it also imports ``deskmatch.report``
and runs the Monte-Carlo baselines, neither of which is part of the
reproducibility target, and ``deskmatch.report`` does not currently exist in the
package -- ``from . import ... report`` inside ``cmd_solve`` raises ImportError
before a single byte is written. Driving the documented module API directly is
both narrower and honest about what is being asserted.

Environment pinning
-------------------
SPEC §7.1 puts ``python`` / ``numpy`` / ``scipy`` versions inside the provenance
block and therefore inside the canonical hash: "upgrading numpy changes the hash
even when the assignment is identical. That is intentional." True, and useful
for an audit -- but it makes a committed golden file expire whenever anybody
runs ``pip install -U``. ``pin_environment=True`` replaces exactly those three
third-party version strings with a constant, so the golden file pins everything
this repository controls and nothing it does not. ``deskmatch_version`` is *not*
pinned: it lives in a tracked file, so changing it is a repository change and the
golden should be regenerated deliberately.

``tests/test_determinism.py`` additionally asserts that a *pinned* and an
*unpinned* run of the same inputs differ in no field other than the pinned ones,
so the pinning cannot mask a regression anywhere else in the document.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
SOLVER_DIR = REPO_ROOT / "solver"
if str(SOLVER_DIR) not in sys.path:
    sys.path.insert(0, str(SOLVER_DIR))

#: Substituted for the three third-party version fields when pinning. A literal
#: rather than a plausible-looking version, so a pinned file can never be
#: mistaken for a real audit record.
PINNED_ENVIRONMENT: dict[str, str] = {
    "python": "PINNED-FOR-GOLDEN-FILE",
    "numpy": "PINNED-FOR-GOLDEN-FILE",
    "scipy": "PINNED-FOR-GOLDEN-FILE",
}

#: Provenance fields `pin_environment=True` is allowed to change, plus the two
#: that necessarily follow from changing them.
PINNABLE_PROVENANCE_KEYS: frozenset[str] = frozenset(PINNED_ENVIRONMENT) | {
    "reproduce",
    "canonical_sha256",
}

#: Display paths used when pinning, so `reproduce` does not embed the working
#: directory the test happened to run from. A *relative* path is what makes this
#: stable: `relative_display_path` renders it unchanged whatever the cwd is.
PINNED_CONFIG_DISPLAY = "config"
PINNED_RESPONSES_DISPLAY = "responses.csv"


def run_pipeline(
    config_dir: str | os.PathLike[str],
    responses_path: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    *,
    curve: str | None = None,
    backend: str | None = None,
    pin_environment: bool = False,
    display_config: str | None = None,
    display_responses: str | None = None,
) -> dict[str, Any]:
    """Run config -> responses -> problem -> solve -> results.json.

    Returns a plain-JSON summary (so the subprocess entry point can print it):
    the results path, the canonical hash (the value `--verify` takes), the
    sha256 of the bytes actually written, and the counts a caller may want to
    sanity-check.
    """
    from deskmatch import problem as problem_mod
    from deskmatch import provenance
    from deskmatch import responses as responses_mod
    from deskmatch import solve as solve_mod
    from deskmatch.config import load_config

    if pin_environment:
        display_config = display_config or PINNED_CONFIG_DISPLAY
        display_responses = display_responses or PINNED_RESPONSES_DISPLAY

    config = load_config(config_dir)
    responses = responses_mod.load_responses(os.fspath(responses_path), config.k)
    build = problem_mod.build_problem(config, responses, curve)
    solution = solve_mod.solve(
        build.problem, config.scoring.tie_break_seed, backend
    )

    prov = provenance.build_provenance(
        config=config,
        responses=responses,
        solution=solution,
        config_path=(
            display_config if display_config is not None else os.fspath(config_dir)
        ),
        responses_path=(
            display_responses
            if display_responses is not None
            else os.fspath(responses_path)
        ),
    )
    if pin_environment:
        prov.update(PINNED_ENVIRONMENT)

    document = provenance.results_document(config, build, solution, prov)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "results.json"
    canonical = provenance.write_results_json(results_path, document)
    payload = results_path.read_bytes()

    return {
        "results_path": str(results_path),
        "canonical_sha256": canonical,
        "results_bytes_sha256": hashlib.sha256(payload).hexdigest(),
        "results_size": len(payload),
        "n_people": int(build.problem.n_people),
        "n_desks": int(build.problem.n_desks),
        "k": int(build.problem.k),
        "total_points_scaled": int(solution.total_points_scaled),
        "rank_histogram": list(solution.rank_histogram()),
        "responses_sha256": responses.sha256,
    }


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tests/pipeline.py",
        description="Run the deskmatch results.json pipeline and print a JSON summary.",
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--responses", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--curve", default=None)
    parser.add_argument("--backend", default=None)
    parser.add_argument("--pin-environment", action="store_true")
    args = parser.parse_args(argv)

    summary = run_pipeline(
        args.config,
        args.responses,
        args.out,
        curve=args.curve,
        backend=args.backend,
        pin_environment=args.pin_environment,
    )
    # Probes proving the interpreter really was started with the PYTHONHASHSEED
    # the caller asked for. Without these, "both runs agree" could just mean
    # "the environment variable never took effect".
    summary["pythonhashseed"] = os.environ.get("PYTHONHASHSEED")
    summary["str_hash_probe"] = hash("deskmatch")
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
