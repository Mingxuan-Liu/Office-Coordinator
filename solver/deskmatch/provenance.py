"""Canonical serialisation, hashing, and the provenance block (SPEC §7.1).

This module is the reproducibility primitive for the whole package. Invariant I3
("same inputs ⇒ byte-identical results.json") is *implemented here*: every other
module can be as deterministic as it likes, but if the serialiser is sloppy the
guarantee evaporates.

Determinism rules obeyed here (SPEC §5.5):
  * `sort_keys=True`, fixed separators, `ensure_ascii=False`, one trailing "\\n".
  * Nothing is serialised via `default=str`. Every non-JSON type this package can
    produce (Fraction, Decimal, numpy scalars/arrays, sets, tuples, dates) is
    converted *explicitly*, so a new type shows up as a loud TypeError naming the
    offending JSON path rather than as a silently-stringified value whose text
    representation could drift between releases.
  * Sets are sorted before serialisation; a set is never iterated in output order.
  * No `hash()` of a str anywhere — `hashlib` only.
  * The clock is read only by `now_iso_utc()`, which callers invoke explicitly.
    Nothing in this module reads it implicitly, and the one timestamp field that
    can appear in the provenance block is excluded from the canonical hash.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import io
import json
import math
import os
import platform
from datetime import date, datetime, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .errors import VerificationError

# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------

_DISTRIBUTION_NAME = "deskmatch"

#: Last-resort version, used only if the package metadata is absent AND
#: `deskmatch.__version__` cannot be read. It exists so that this module can be
#: imported standalone in a test; it is not the source of truth.
_FALLBACK_VERSION = "0.0.0+unknown"


def get_version() -> str:
    """The deskmatch version, whether or not the package is pip-installed.

    Three sources, in order of authority:
      1. installed distribution metadata — correct for a `pip install`ed copy;
      2. `deskmatch.__version__` — correct for the git checkout a grad student is
         actually editing, which is the normal case and where
         `importlib.metadata` raises PackageNotFoundError;
      3. a constant, so this never throws.

    Keep `__init__.__version__` equal to the version in pyproject.toml: this
    string goes into results.json and therefore into the canonical hash, so a
    source run and an installed run of the *same* code must agree on it or their
    hashes will differ for no real reason.
    """
    try:
        return importlib.metadata.version(_DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        pass
    try:
        from . import __version__ as package_version

        return str(package_version)
    except Exception:  # pragma: no cover - only if __init__ is unimportable
        return _FALLBACK_VERSION


def _dist_version(distribution: str, module_name: str | None = None) -> str:
    """Version of a third-party dependency, without importing it if avoidable.

    Metadata lookup is preferred because it does not pay the import cost (scipy
    is not cheap). If the package is present but not installed as a distribution
    — vendored, or on PYTHONPATH — fall back to importing and reading
    ``__version__``.
    """
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        pass
    if module_name is not None:
        try:
            import importlib as _importlib

            module = _importlib.import_module(module_name)
        except Exception:  # pragma: no cover - only when the dep is truly absent
            return "unknown"
        return str(getattr(module, "__version__", "unknown"))
    return "unknown"


def environment_versions() -> dict[str, str]:
    """The interpreter/dependency versions recorded in SPEC §7.1.

    These are part of the canonical hash by design: the SPEC lists them inside
    the provenance block and only `canonical_sha256` and `generated_at` are
    excluded from hashing. Consequence, stated plainly because it surprises
    people: upgrading numpy changes the hash even when the assignment is
    identical. That is intentional — the hash certifies "this exact software
    produced this exact answer", and `verify_results()` prints these fields on a
    mismatch so an environment drift is diagnosable in one glance.
    """
    return {
        "deskmatch_version": get_version(),
        "python": platform.python_version(),
        "numpy": str(np.__version__),
        "scipy": _dist_version("scipy", "scipy"),
    }


def now_iso_utc() -> str:
    """The only clock read in this package. Callers pass the result into
    `build_provenance(generated_at=...)`; it never reaches the canonical hash."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Canonical JSON
# --------------------------------------------------------------------------


def _fraction_to_number(value: Fraction, path: str) -> int | float:
    """Exact rational -> JSON number.

    Integral fractions become ints (the common case: SPEC §5.3 rationalises every
    curve to exact integers before solving). A non-integral fraction can only be
    a display copy of a config curve value, and SPEC §2.4 rejects non-terminating
    decimals at validation, so the float below is the decimal the coordinator
    actually typed. Python's float repr is the shortest string that round-trips,
    and that algorithm is specified rather than platform-dependent, so the bytes
    are stable across machines.
    """
    if value.denominator == 1:
        return int(value)
    return _check_float(float(value), path)


def _check_float(value: float, path: str) -> float:
    if not math.isfinite(value):
        raise ValueError(
            f"canonical_json: {path} is {value!r}, which has no JSON representation "
            f"and is not reproducible. Fix the producer of this field."
        )
    # Normalise -0.0 to 0.0: they compare equal but repr differently, which would
    # make two logically identical documents hash differently.
    if value == 0.0:
        return 0.0
    return value


def _canonical_key(key: Any, path: str) -> str:
    """JSON object keys must be strings, and must sort identically everywhere.

    Numeric keys are stringified explicitly (json would do it silently, but then
    `sort_keys=True` would sort them as text without anyone having decided that).
    Float keys are rejected outright — "1.0" vs "1" is exactly the kind of
    ambiguity that produces two hashes for one document.
    """
    if isinstance(key, str):
        return key
    if isinstance(key, bool):
        return "true" if key else "false"
    if isinstance(key, (int, np.integer)):
        return str(int(key))
    raise TypeError(
        f"canonical_json: {path} has a mapping key of type {type(key).__name__} "
        f"({key!r}). Only str/int/bool keys are allowed; stringify it deliberately "
        f"at the call site so the sort order is a decision, not an accident."
    )


def _sorted_for_output(items: Iterable[Any], path: str) -> list[Any]:
    """Deterministic ordering for an unordered collection.

    Try natural sort first (cheap, and gives humans a readable order). Mixed-type
    collections raise TypeError from `sorted`, so fall back to sorting by each
    element's own canonical JSON text — still total, still deterministic.
    """
    values = [_canonicalize(v, f"{path}{{}}") for v in items]
    try:
        return sorted(values)
    except TypeError:
        return sorted(values, key=lambda v: canonical_json(v))


def _canonicalize(obj: Any, path: str) -> Any:
    """Deep-convert `obj` into plain JSON types, explicitly, type by type.

    Returns freshly built dicts/lists, so the result doubles as a defensive deep
    copy: callers can stamp fields into it without mutating the caller's object.
    """
    # bool before int: bool IS an int subclass and must stay a JSON boolean.
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return _check_float(obj, path)
    if isinstance(obj, Fraction):
        return _fraction_to_number(obj, path)
    if isinstance(obj, Decimal):
        if obj != obj or obj.is_infinite():  # NaN compares unequal to itself
            raise ValueError(f"canonical_json: {path} is a non-finite Decimal ({obj!r}).")
        return _fraction_to_number(Fraction(obj), path)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return _check_float(float(obj), path)
    if isinstance(obj, np.str_):
        return str(obj)
    if isinstance(obj, np.ndarray):
        # tolist() already produces Python scalars; recurse anyway so the float
        # finiteness check runs on every element.
        return [_canonicalize(v, f"{path}[{i}]") for i, v in enumerate(obj.tolist())]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for raw_key, value in obj.items():
            key = _canonical_key(raw_key, path)
            if key in out:
                raise ValueError(
                    f"canonical_json: {path} has two mapping keys that both render "
                    f"as {key!r}. One document must not have two hashes."
                )
            out[key] = _canonicalize(value, f"{path}.{key}")
        return out
    if isinstance(obj, (set, frozenset)):
        return _sorted_for_output(obj, path)
    if isinstance(obj, (list, tuple)):
        return [_canonicalize(v, f"{path}[{i}]") for i, v in enumerate(obj)]
    if isinstance(obj, (bytes, bytearray)):
        raise TypeError(
            f"canonical_json: {path} is raw bytes. Encode it at the call site "
            f"(hex or base64) so the choice is explicit and stable."
        )
    raise TypeError(
        f"canonical_json: {path} is of type {type(obj).__name__}, which has no "
        f"declared canonical form. Add an explicit conversion in "
        f"deskmatch.provenance._canonicalize — do NOT reach for default=str, "
        f"which would freeze an arbitrary repr into the reproducibility hash."
    )


def canonical_json(obj: Any) -> bytes:
    """THE reproducibility primitive: one object, exactly one byte string.

    sort_keys=True, separators=(',',':'), ensure_ascii=False, UTF-8, and a single
    trailing newline (so the file is a well-behaved text file and `sha256sum`
    agrees with what a shell pipeline computes).
    """
    plain = _canonicalize(obj, "$")
    text = json.dumps(
        plain,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,   # belt and braces; _check_float already rejected these
    )
    return (text + "\n").encode("utf-8")


# --------------------------------------------------------------------------
# Hashing
# --------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    """Hex sha256 of a byte string."""
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    """Hex sha256 of the UTF-8 encoding of `text`."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | os.PathLike[str], *, chunk_size: int = 1 << 20) -> str:
    """Hex sha256 of a file's RAW bytes — no decoding, no newline translation.

    Hashing the bytes rather than the parsed content is the point: it is what a
    second person with a copy of the export can reproduce with `sha256sum`.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Provenance block (SPEC §7.1)
# --------------------------------------------------------------------------

#: Placeholder in the `reproduce` string. It CANNOT be the real hash: `reproduce`
#: is itself hashed, so substituting the hash into it would require a value whose
#: sha256 contains itself. Use `reproduce_command(doc)` to render a runnable
#: command with the real hash filled in for display.
REPRODUCE_VERIFY_PLACEHOLDER = "<hash>"

#: Key that holds the self-referential hash.
CANONICAL_HASH_KEY = "canonical_sha256"

#: The output directory named in the `reproduce` command. Fixed on purpose: see
#: the comment in build_provenance(). Where results were written is not an input
#: to the solve and must not perturb the hash.
CANONICAL_OUT_DIR = "out/"

#: Keys excluded from the canonical hash, wherever they appear in the provenance
#: block. `canonical_sha256` for the obvious self-reference reason;
#: `generated_at` because SPEC §5.5 requires wall-clock time never to affect the
#: reproducibility target.
HASH_EXCLUDED_KEYS: tuple[str, ...] = (CANONICAL_HASH_KEY, "generated_at")

#: Where the provenance block lives inside results.json.
PROVENANCE_KEY = "provenance"


def _basename_display(path: str | os.PathLike[str], *, is_dir: bool) -> str:
    """Final path component only, with a trailing slash for directories.

    Used for the `reproduce` command, which is hashed and therefore must not
    carry anything that depends on the caller's working directory. See the
    comment at the call site in build_provenance().
    """
    name = PurePosixPath(str(path).replace(os.sep, "/")).name or str(path)
    return f"{name}/" if is_dir else name


def relative_display_path(
    path: str | os.PathLike[str],
    *,
    base: str | os.PathLike[str] | None = None,
    is_dir: bool | None = None,
) -> str:
    """A machine-independent rendering of a path, for the provenance block.

    An absolute path ("/Users/someone/...") would put the operator's home
    directory into the canonical hash and break I3 across machines. So: express
    it relative to `base` (cwd by default) when that stays inside the tree, fall
    back to the bare filename otherwise, and always use forward slashes so
    Windows and macOS produce the same bytes.
    """
    text = os.fspath(path)
    root = os.fspath(base) if base is not None else os.getcwd()
    try:
        rel = os.path.relpath(text, root)
    except ValueError:  # different drives on Windows
        rel = os.path.basename(text.rstrip("/\\"))
    if rel.startswith("..") or os.path.isabs(rel):
        rel = os.path.basename(text.rstrip("/\\")) or text
    rel = rel.replace(os.sep, "/")
    if os.altsep:
        rel = rel.replace(os.altsep, "/")
    want_dir = os.path.isdir(text) if is_dir is None else is_dir
    if want_dir and not rel.endswith("/"):
        rel += "/"
    return rel


def build_provenance(
    *,
    seed_string: str | None = None,
    seed_int: int | None = None,
    curve_name: str | None = None,
    curve_values: Sequence[Fraction | int | float] | None = None,
    k: int | None = None,
    responses_sha256: str | None = None,
    responses_row_count: int | None = None,
    config_sha256: Mapping[str, str] | None = None,
    config_dir: str | os.PathLike[str] | None = None,
    responses_path: str | os.PathLike[str] | None = None,
    backend: str | None = None,
    generated_at: str | None = None,
    extra: Mapping[str, Any] | None = None,
    config: Any = None,
    responses: Any = None,
    solution: Any = None,
    config_path: str | os.PathLike[str] | None = None,
    args_out: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Build the SPEC §7.1 provenance block.

    Two calling forms, because two callers want different things:

      * **Primitive** — pass `seed_string`, `seed_int`, `curve_name`,
        `curve_values`, `k`, `responses_sha256`, `responses_row_count` and
        `config_sha256` directly. This is the form tests and any future tool use;
        it has no dependency on the rest of the package.
      * **Object** — pass `config` (types.Config), `responses` (types.Responses)
        and `solution` (types.Solution) and every field above is derived from
        them. This is what `cli.py` uses, so the CLI cannot drift out of sync
        with the solve it just ran.

    The objects are duck-typed rather than imported so that this module — the one
    everything else depends on for hashing — depends on nothing but the stdlib
    and numpy.

    `canonical_sha256` is deliberately ABSENT from the returned dict. It is a hash
    of the finished results document with that very key removed, so it cannot
    exist until every other part of the document does; `stamp_canonical_sha256()`
    / `write_results_json()` insert it. Emitting a placeholder here would be worse
    than omitting it — a half-written hash that looks real is exactly the failure
    mode this whole module exists to prevent.

    Nothing here is derived from the environment except the version fields and the
    path strings, and every path is normalised to be machine-independent.
    """
    if solution is not None:
        seed_string = seed_string if seed_string is not None else solution.seed_string
        seed_int = seed_int if seed_int is not None else solution.seed_int
        curve_name = curve_name if curve_name is not None else solution.curve_name
        k = k if k is not None else solution.k
        backend = backend if backend is not None else getattr(solution, "backend", None)
    if config is not None:
        if curve_name is None:
            curve_name = config.scoring.primary_curve
        if curve_values is None:
            curve_values = config.scoring.curve(curve_name)
        if config_sha256 is None:
            config_sha256 = config.file_hashes
        if seed_string is None:
            seed_string = config.scoring.resolved_seed()
        if k is None:
            k = config.k
        if config_path is None:
            config_path = config.source_dir
    if responses is not None:
        if responses_sha256 is None:
            responses_sha256 = responses.sha256
        if responses_row_count is None:
            responses_row_count = len(responses.submissions)
        if responses_path is None:
            responses_path = responses.source_path
    if config_dir is None:
        config_dir = config_path if config_path is not None else "config/"
    if responses_path is None:
        responses_path = "responses.csv"

    missing = [
        name
        for name, value in (
            ("seed_string", seed_string),
            ("seed_int", seed_int),
            ("curve_name", curve_name),
            ("curve_values", curve_values),
            ("k", k),
            ("responses_sha256", responses_sha256),
            ("responses_row_count", responses_row_count),
            ("config_sha256", config_sha256),
        )
        if value is None
    ]
    if missing:
        raise ValueError(
            "build_provenance: missing required field(s) "
            + ", ".join(missing)
            + ". Supply them directly, or pass config=/responses=/solution= and "
            "let them be derived."
        )

    doc: dict[str, Any] = {
        "seed_string": seed_string,
        "seed_int": int(seed_int),
        "curve": curve_name,
        # Fractions are canonicalised on serialisation; keep them exact until then.
        "curve_values": list(curve_values),
        "K": int(k),
        "responses_sha256": responses_sha256,
        "responses_row_count": int(responses_row_count),
        # Keys are bare filenames, which is what makes this comparable between
        # two people who checked the repo out to different directories.
        "config_sha256": {str(name): str(digest) for name, digest in config_sha256.items()},
    }
    doc.update(environment_versions())
    # Basenames, not relative paths. `reproduce` is inside the canonical hash, so
    # any part of it that varies with the caller's directory layout makes the hash
    # vary too: the same solve run from the repo root and from inside data/ would
    # produce "data/responses.csv" and "responses.csv" and therefore two different
    # hashes. A verifier who laid their download out differently would then be told
    # the results do not match, which is precisely the false alarm that would
    # destroy confidence in the one step everything else rests on. Basenames are
    # stable everywhere, and they match the flat layout `deskmatch publish` emits.
    command = (
        f"deskmatch solve"
        f" --config {_basename_display(config_dir, is_dir=True)}"
        f" --responses {_basename_display(responses_path, is_dir=False)}"
    )
    # The output directory is deliberately NOT interpolated here, even though the
    # caller passes it. `reproduce` is inside the canonical hash, so embedding the
    # real --out path would make the hash depend on where the coordinator happened
    # to write their results -- and two people re-running into different folders
    # would get different hashes and conclude the results did not match. That is a
    # false alarm in exactly the step the whole audit story depends on. The output
    # directory is not an input to the solve (invariant I2: a pure function of
    # responses, config and seed), so the canonical command names a canonical
    # destination.
    #
    # Nor is the real path recorded elsewhere in the document. Excluding it from
    # the hash would not be enough: SPEC I3 promises results.json is byte-identical,
    # and a verifier diffing two files should see nothing at all, not "nothing that
    # counts". `args_out` stays in the signature because callers pass it.
    command += f" --out {CANONICAL_OUT_DIR}"
    doc["reproduce"] = f"{command} --verify {REPRODUCE_VERIFY_PLACEHOLDER}"
    if backend is not None:
        doc["backend"] = backend
    if generated_at is not None:
        # Excluded from the canonical hash — see HASH_EXCLUDED_KEYS.
        doc["generated_at"] = generated_at
    if extra:
        for key in sorted(extra):
            if key in doc:
                raise ValueError(
                    f"build_provenance: extra key {key!r} would overwrite a "
                    f"SPEC §7.1 field. Pick a different name."
                )
            doc[key] = extra[key]
    return doc


def reproduce_command(doc: Mapping[str, Any]) -> str:
    """The `reproduce` string with the real hash substituted, for printing.

    The stored string keeps the placeholder (it has to — see
    REPRODUCE_VERIFY_PLACEHOLDER); this renders the copy-pasteable version.
    """
    block = _provenance_block(doc) or {}
    template = str(block.get("reproduce", ""))
    digest = block.get(CANONICAL_HASH_KEY)
    if digest:
        return template.replace(REPRODUCE_VERIFY_PLACEHOLDER, str(digest))
    return template


# --------------------------------------------------------------------------
# The self-referential hash
# --------------------------------------------------------------------------


def _provenance_block(doc: Mapping[str, Any]) -> dict[str, Any] | None:
    block = doc.get(PROVENANCE_KEY)
    return block if isinstance(block, Mapping) else None  # type: ignore[return-value]


def _hash_owner(plain: dict[str, Any]) -> dict[str, Any]:
    """The dict that owns `canonical_sha256`.

    SPEC §7.1 shows it inside the provenance block, which is where it goes when
    one exists. A bare document (diagnostics.json, round2_input.json) gets it at
    the top level so the same verify path works for every file we emit.
    """
    block = plain.get(PROVENANCE_KEY)
    if isinstance(block, dict):
        return block
    return plain


def _strip_excluded(plain: dict[str, Any]) -> dict[str, Any]:
    """A copy of the document with the non-hashed fields removed.

    This is THE detail people get wrong. `canonical_sha256` is the hash of the
    results document *with `canonical_sha256` itself deleted* — otherwise the
    value would have to appear inside its own preimage, which no hash function
    can satisfy. `generated_at` is dropped for the same practical reason it is
    excluded elsewhere: SPEC §5.5 forbids wall-clock time from influencing the
    reproducibility target.

    Deletion (not blanking, not zeroing) is the rule: a document written by an
    older version that never emitted the key must hash the same as one written
    today, and blanking would make an absent key differ from an empty one.
    """
    out = dict(plain)
    for key in HASH_EXCLUDED_KEYS:
        out.pop(key, None)
    block = out.get(PROVENANCE_KEY)
    if isinstance(block, dict):
        trimmed = dict(block)
        for key in HASH_EXCLUDED_KEYS:
            trimmed.pop(key, None)
        out[PROVENANCE_KEY] = trimmed
    return out


def compute_canonical_sha256(doc: Mapping[str, Any]) -> str:
    """sha256 of `doc` in canonical form, with the excluded keys removed.

    Idempotent with respect to stamping: hashing a document that already carries
    its `canonical_sha256` yields the same value it carries, which is exactly what
    makes verification possible.
    """
    plain = _canonicalize(doc, "$")
    if not isinstance(plain, dict):
        raise TypeError("compute_canonical_sha256: the results document must be a mapping.")
    return sha256_bytes(canonical_json(_strip_excluded(plain)))


#: Short alias used by cli.py. Same function, shorter at a call site where the
#: surrounding line is already about hashes.
canonical_hash_of = compute_canonical_sha256


def stamp_canonical_sha256(doc: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """Return `(document_with_hash, hash)`.

    The input is never mutated: `_canonicalize` rebuilds the tree in plain JSON
    types, so the returned document is both stamped and already in the exact form
    that will be written.
    """
    plain = _canonicalize(doc, "$")
    if not isinstance(plain, dict):
        raise TypeError("stamp_canonical_sha256: the results document must be a mapping.")
    digest = sha256_bytes(canonical_json(_strip_excluded(plain)))
    _hash_owner(plain)[CANONICAL_HASH_KEY] = digest
    return plain, digest


def write_results_json(path: str | os.PathLike[str], doc: Mapping[str, Any]) -> str:
    """Stamp, serialise canonically, write, and return `canonical_sha256`.

    The returned hash is the hash OF THE DOCUMENT MINUS that field — i.e. the
    value `--verify` takes — not the hash of the bytes on disk. Those two are
    necessarily different, because the bytes on disk contain the hash.
    """
    stamped, digest = stamp_canonical_sha256(doc)
    payload = canonical_json(stamped)
    parent = os.path.dirname(os.fspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)
    return digest


def results_document(
    config: Any,
    build: Any,
    solution: Any,
    provenance_block: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble `results.json` (SPEC §7) from a finished solve.

    Lives here rather than in cli.py because this document *is* the
    reproducibility target: its shape and its hash are one concern, and splitting
    them across two modules is how a field quietly starts or stops being covered
    by the hash.

    `seed_string` and `curve_name` are mirrored at the top level as well as inside
    the provenance block, because `deskmatch verify` re-runs the solve from this
    file and needs them without reaching into provenance.

    Everything is a sorted tuple/list. `solution.assignments` is already sorted by
    email; the mappings below are sorted explicitly so that two runs of the same
    solve serialise to the same bytes (I3).
    """
    problem = build.problem
    assignments = [
        {
            "email": a.email,
            "name": a.name,
            "desk_id": a.desk_id,
            "desk_label": a.desk_label,
            "rank_received": int(a.rank_received),
            "points_scaled": int(a.points),
        }
        for a in sorted(solution.assignments, key=lambda a: a.email)
    ]
    doc: dict[str, Any] = {
        "schema": "deskmatch/results/1",
        "seed_string": solution.seed_string,
        "seed_int": int(solution.seed_int),
        "curve_name": solution.curve_name,
        "curve_values": list(config.scoring.curve(solution.curve_name)),
        "k": int(solution.k),
        "scale": int(solution.scale),
        "backend": solution.backend,
        "n_people": int(problem.n_people),
        "n_desks": int(problem.n_desks),
        "total_points_scaled": int(solution.total_points_scaled),
        "total_points": solution.total_points,     # Fraction; canonicalised on write
        "rank_histogram": list(solution.rank_histogram()),
        "assignments": assignments,
        "free_desks": sorted(solution.free_desks),
        "unassigned_people": sorted(solution.unassigned_people),
        "roster_conflicts": [
            {
                "email": c.email,
                "field": c.field,
                "roster_value": c.roster_value,
                "submitted_value": c.submitted_value,
            }
            for c in sorted(
                getattr(build, "roster_conflicts", ()), key=lambda c: (c.email, c.field)
            )
        ],
        "excluded_people": [
            {"email": e.email, "name": e.name, "reason": e.reason}
            for e in sorted(getattr(build, "excluded_people", ()), key=lambda e: e.email)
        ],
        "locked_desks": [
            [desk, email] for desk, email in sorted(getattr(build, "locked_desks", ()))
        ],
        "unavailable_desks": sorted(getattr(build, "unavailable_desks", ())),
        "dropped_choices": [
            [who, desk, why]
            for who, desk, why in sorted(getattr(build, "dropped_choices", ()))
        ],
        "warnings": list(getattr(build, "warnings", ())),
        "provenance": dict(provenance_block),
    }
    return doc


def write_assignments_csv(path: str | os.PathLike[str], solution: Any) -> str:
    """`assignments.csv` (SPEC §7): name, email, desk, rank received.

    Sorted by email and written with "\\n" line endings for the same reason
    results.json is canonical — two runs of the same solve must produce the same
    file, and the platform default of "\\r\\n" would break that on Windows.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["name", "email", "desk_id", "desk_label", "rank_received"])
    for a in sorted(solution.assignments, key=lambda a: a.email):
        writer.writerow([a.name, a.email, a.desk_id, a.desk_label, str(a.rank_received)])
    payload = buffer.getvalue().encode("utf-8")
    parent = os.path.dirname(os.fspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)
    return sha256_bytes(payload)


def read_results_json(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load a results document, raising VerificationError (never a raw OSError or
    JSONDecodeError) on anything a coordinator could have caused."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise VerificationError(
            f"{os.fspath(path)}: cannot be read ({exc.strerror or exc})."
        ) from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise VerificationError(
            f"{os.fspath(path)}: is not valid UTF-8 (byte {exc.start}). "
            f"Results files are always written as UTF-8; this file has been "
            f"re-saved by something that changed its encoding."
        ) from exc
    except json.JSONDecodeError as exc:
        raise VerificationError(
            f"{os.fspath(path)}: is not valid JSON — {exc.msg} "
            f"at line {exc.lineno}, column {exc.colno}."
        ) from exc
    if not isinstance(parsed, dict):
        raise VerificationError(
            f"{os.fspath(path)}: the top level of a results file must be a JSON "
            f"object; found {type(parsed).__name__}."
        )
    return parsed


def _normalise_expected(expected_hash: str, path: str) -> str:
    text = expected_hash.strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:"):]
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise VerificationError(
            f"--verify was given {expected_hash!r}, which is not a 64-character "
            f"hex sha256 digest. The value to pass is the 'canonical_sha256' "
            f"field printed in the report and stored in {path}."
        )
    return text


def _mismatch_report(
    *,
    path: str,
    expected: str,
    computed: str,
    embedded: Any,
    doc: Mapping[str, Any],
    byte_canonical: bool,
    size: int,
) -> str:
    """A readable, diff-ish explanation of *why* two runs disagree.

    A bare "hash mismatch" is useless to a coordinator. What they can act on is:
    which inputs the file claims to have used, and which software produced it —
    because in practice a mismatch is either a different response export, an
    edited config, or a different numpy.
    """
    block = _provenance_block(doc) or {}
    lines = [
        f"{path}: canonical hash does not match the expected value.",
        f"    expected (--verify) : {expected}",
        f"    computed from file  : {computed}",
    ]
    if isinstance(embedded, str) and embedded:
        consistent = "consistent with the file contents" if embedded == computed else (
            "INCONSISTENT — the file was edited after it was written"
        )
        lines.append(f"    embedded in file    : {embedded}  ({consistent})")
    else:
        lines.append(
            "    embedded in file    : (absent) — this file was not written by "
            "write_results_json()"
        )
    lines.append(
        f"    file                : {size} bytes, "
        f"canonical byte form: {'yes' if byte_canonical else 'no (re-serialised or hand-edited)'}"
    )

    inputs: list[str] = []
    if "responses_sha256" in block:
        inputs.append(f"      responses_sha256          : {block['responses_sha256']}")
    if "responses_row_count" in block:
        inputs.append(f"      responses_row_count       : {block['responses_row_count']}")
    if "seed_string" in block:
        inputs.append(f"      seed_string               : {block['seed_string']!r}")
    if "curve" in block:
        inputs.append(f"      curve                     : {block['curve']}  (K={block.get('K')})")
    config_hashes = block.get("config_sha256")
    if isinstance(config_hashes, Mapping):
        names = sorted(config_hashes)   # sorted, so two reports line up for diffing
        width = max((len(n) for n in names), default=0) + len("config_sha256[]")
        for name in names:
            inputs.append(f"      {f'config_sha256[{name}]':<{width}}: {config_hashes[name]}")
    if inputs:
        lines.append("  Inputs recorded in the file (compare these with your own):")
        lines.extend(inputs)

    env = [
        f"      {key:<26}: {block[key]}"
        for key in ("deskmatch_version", "python", "numpy", "scipy")
        if key in block
    ]
    if env:
        lines.append("  Environment recorded in the file:")
        lines.extend(env)

    lines.append(
        "  The canonical hash covers every field except "
        + " and ".join(HASH_EXCLUDED_KEYS)
        + ", so a difference in the responses, the config, the seed, the computed "
        "assignment OR the recorded software versions all show up here identically."
    )
    return "\n".join(lines)


def verify_results(path: str | os.PathLike[str], expected_hash: str) -> str:
    """Check a results file against a published hash. Returns the hash on success.

    Two distinct failures are reported separately, because they mean different
    things: (a) the file is internally inconsistent — its embedded
    `canonical_sha256` does not match its own contents, so it was edited after it
    was written; (b) the file is internally consistent but is not the run you
    expected.
    """
    text_path = os.fspath(path)
    expected = _normalise_expected(expected_hash, text_path)
    doc = read_results_json(text_path)

    computed = compute_canonical_sha256(doc)
    embedded = _hash_owner(_canonicalize(doc, "$")).get(CANONICAL_HASH_KEY)

    with open(text_path, "rb") as handle:
        raw = handle.read()
    # Compare the file against a canonical re-serialisation of its OWN content —
    # not against a re-stamped version, which would always differ for a file whose
    # embedded hash is stale and would mislabel tampering as a formatting problem.
    byte_canonical = canonical_json(doc) == raw

    if computed != expected:
        raise VerificationError(
            _mismatch_report(
                path=text_path,
                expected=expected,
                computed=computed,
                embedded=embedded,
                doc=doc,
                byte_canonical=byte_canonical,
                size=len(raw),
            )
        )
    if isinstance(embedded, str) and embedded and embedded != computed:
        raise VerificationError(
            f"{text_path}: the file's own 'canonical_sha256' field ({embedded}) "
            f"does not match its contents ({computed}). The hash you passed "
            f"matches the contents, so the results are the expected ones, but the "
            f"stored hash field has been tampered with or hand-edited."
        )
    return computed
