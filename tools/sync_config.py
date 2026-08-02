#!/usr/bin/env python3
"""Generate frontend/ConfigData.gs from the files in config/.

Apps Script has no build step and cannot read this repository at runtime, so
the configuration has to be baked into a .gs file and pushed with the rest of
the project. This script is that bake step. It exists so that desk geometry,
the eligibility rule table, the scoring curve and the roster have exactly one
source of truth in git (``config/``) and the Apps Script copy is a derived
artefact that nobody edits by hand.

What it emits (all of it read from the config, none of it written down here):

    var ROOMS_JSON          rooms.json, verbatim
    var ELIGIBILITY_JSON    eligibility.json, verbatim
    var SCORING_JSON        scoring.json, verbatim  (K = len of the primary curve)
    var ROSTER              roster.csv as a list of objects, every column kept
                            for the rooms that declare an `image` — normally
                            none, so normally ``{}``
    var CONFIG_FINGERPRINT  sha256 prefix over every input that ends up in here

Floor-plan images
-----------------
rooms.json is a schematic: the desk rectangles are the map and their spacing
carries the layout, so **no room declares an `image` and nothing is embedded**.
That matters because the images used to be base64'd into this file — one PNG
took the generated output to ~1.1 MB, all of it shipped in the bootstrap
payload to every phone that opened the form.

A room with no `image` key is therefore silent in every respect: no entry in
FLOORPLAN_DATA_URI, no bytes, no warning. It is the intended configuration, not
a gap. Only a room that *declares* an image is looked for on disk, and only
then can it warn:

* declared and readable -> embedded as a data URI, plus a warning saying how
  much weight that just added, because the shipped system does not need one;
* declared and missing/unreadable -> ``null`` and a loud message on stderr.
  That one is an accident: the coordinator asked for a picture and did not get
  it. Still not fatal — a form with no picture beats no form.

Determinism: the output contains no timestamp and no path from the developer's
machine, and key order follows the source files (json.load, csv.DictReader and
Python dicts all preserve order), so identical inputs give a byte-identical
file. That is what makes ``--check`` a pure content comparison, safe for CI.

Usage:
    python3 tools/sync_config.py                     # regenerate
    python3 tools/sync_config.py --check             # CI: fail if out of date
    python3 tools/sync_config.py --config-dir ... --out ...

Standard library only, deliberately: this has to run on whatever Python the
next coordinator happens to have.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import mimetypes
import re
import sys
from pathlib import Path
from typing import NoReturn

# The four config files, in the order they are hashed into the fingerprint.
CONFIG_FILES = ("rooms.json", "eligibility.json", "scoring.json", "roster.csv")

FINGERPRINT_CHARS = 16          # prefix of the sha256 hex digest
#: Past this, an embedded image is not merely heavy, it is a deployment
#: problem: Apps Script serves ConfigData.gs in one bootstrap payload.
LARGE_IMAGE_WARN_BYTES = 1_500_000

# Integers/decimals with no leading zeros and no sign padding. Anything else
# (zip codes, "007", phone numbers, desk ids) stays a string.
_INT_RE = re.compile(r"^-?(?:0|[1-9]\d*)$")
_FLOAT_RE = re.compile(r"^-?(?:0|[1-9]\d*)\.\d+$")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_OUT = REPO_ROOT / "frontend" / "ConfigData.gs"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def warn(message: str) -> None:
    """One-line complaint on stderr. Never fatal on its own."""
    print("sync_config: WARNING: " + message, file=sys.stderr)


def die(message: str) -> NoReturn:
    print("sync_config: ERROR: " + message, file=sys.stderr)
    raise SystemExit(1)


def read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        die(f"cannot read {path}: {exc}")


def load_json(path: Path) -> object:
    raw = read_bytes(path)
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        die(f"{path.name} is not valid UTF-8: {exc}")
    except json.JSONDecodeError as exc:
        die(f"{path.name} is not valid JSON: line {exc.lineno} column {exc.colno}: {exc.msg}")


def coerce_scalar(value: str) -> object:
    """Turn a CSV cell into a number when it unambiguously is one.

    Applied to every column, not to a hard-coded list of them, so that a range
    predicate like ``{"cohort_size": {"min": 3}}`` works on a column this script
    has never heard of. Leading zeros and signs are left alone so ids and phone
    numbers survive intact.
    """
    text = value.strip()
    if _INT_RE.match(text):
        return int(text)
    if _FLOAT_RE.match(text):
        return float(text)
    return text


def load_roster(path: Path) -> list[dict]:
    """roster.csv -> list of dicts, every column preserved verbatim.

    No column is privileged here. Code.gs knows which columns it needs; this
    script just hands over what the file says, so extra columns stay usable in
    eligibility predicates (SPEC §2.3).
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        die(f"cannot read {path}: {exc}")
    except UnicodeDecodeError as exc:
        die(f"{path.name} is not valid UTF-8: {exc}")

    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None:
        die(f"{path.name} is empty; it needs at least a header row.")

    blank = [i for i, name in enumerate(reader.fieldnames) if not (name or "").strip()]
    if blank:
        warn(f"{path.name}: column(s) {blank} have a blank header; they will be dropped.")

    rows: list[dict] = []
    for line_no, raw_row in enumerate(reader, start=2):
        if all((v or "").strip() == "" for v in raw_row.values()):
            continue  # blank line in the middle of the file
        row: dict[str, object] = {}
        for key, value in raw_row.items():
            if key is None or not key.strip():
                continue
            if isinstance(value, list):  # ragged row: csv puts the overflow in a list
                warn(f"{path.name} line {line_no}: more fields than headers; extras ignored.")
                value = value[0] if value else ""
            row[key.strip()] = coerce_scalar("" if value is None else str(value))
        rows.append(row)

    if not rows:
        warn(f"{path.name} has a header but no people in it.")
    return rows


def encode_image(config_dir: Path, rel_path: str) -> tuple[str | None, bytes | None]:
    """Read one declared floor plan and return (data URI, raw bytes).

    Only ever called for a room that actually declares an `image`; a missing
    file yields (None, None) and the caller warns. See the module docstring for
    why that is not fatal, and why no-image-declared never reaches this.
    """
    if not rel_path:
        return None, None
    path = (config_dir / rel_path).resolve()
    if not path.is_file():
        return None, None
    data = read_bytes(path)
    mime, _ = mimetypes.guess_type(path.name)
    if mime is None or not mime.startswith("image/"):
        warn(
            f"{rel_path}: cannot tell what kind of image this is from the file "
            f"extension; assuming image/png. Rename it if the browser refuses it."
        )
        mime = "image/png"
    return "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii"), data


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def js_literal(value: object) -> str:
    """JSON that is also valid JavaScript.

    ``ensure_ascii=True`` matters: it escapes U+2028/U+2029, which are legal in
    JSON but terminate a line in JavaScript, and it keeps the generated file
    pure ASCII so no editor can re-encode it.

    Keys are *not* sorted. json.load and csv.DictReader both preserve source
    order, and Python dicts keep insertion order, so the output is already
    byte-deterministic for identical inputs — and keeping file order means
    ROOMS_JSON really is rooms.json verbatim, so the zones render in the order
    the coordinator wrote them rather than alphabetically.
    """
    return json.dumps(value, indent=2, sort_keys=False, ensure_ascii=True)


def render(
    rooms: object,
    eligibility: object,
    scoring: object,
    roster: list[dict],
    floorplans: dict,
    fingerprint: str,
    config_dir_name: str,
) -> str:
    header = f"""/* ===========================================================================
 * GENERATED FILE — DO NOT EDIT BY HAND.
 *
 * Every edit here will be silently destroyed the next time anyone runs
 *
 *     python3 tools/sync_config.py
 *
 * The source of truth is {config_dir_name}/ in the git repository:
 *     {", ".join(CONFIG_FILES)}
 * No floor-plan bitmap is inlined unless a room asks for one with an "image"
 * key. rooms.json is a schematic -- the desk rectangles are the map -- so the
 * shipped config declares none and this file carries no image bytes.
 *
 * To change a desk, a zone, a rule, the scoring curve or the roster: edit the
 * file in {config_dir_name}/, re-run the command above, and push both the config
 * change and this file in the same commit. CI runs `sync_config.py --check`
 * and fails if they disagree.
 *
 * CONFIG_FINGERPRINT below is a sha256 prefix over all of those inputs. It is
 * recorded in every submitted row (as part of client_version) so a response
 * can be tied back to the exact configuration that produced it.
 *
 * This file intentionally contains no generation timestamp: identical inputs
 * must produce a byte-identical file, or --check would be useless.
 * ======================================================================== */

"""
    parts = [
        header,
        "var ROOMS_JSON = " + js_literal(rooms) + ";\n\n",
        "var ELIGIBILITY_JSON = " + js_literal(eligibility) + ";\n\n",
        "var SCORING_JSON = " + js_literal(scoring) + ";\n\n",
        "var ROSTER = " + js_literal(roster) + ";\n\n",
        "var CONFIG_FINGERPRINT = " + json.dumps(fingerprint) + ";\n",
    ]
    return "".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build(config_dir: Path) -> tuple[str, dict]:
    """Produce the ConfigData.gs text plus a summary dict for the console."""
    for name in CONFIG_FILES:
        if not (config_dir / name).is_file():
            die(f"{config_dir / name} does not exist. Is --config-dir right?")

    rooms = load_json(config_dir / "rooms.json")
    eligibility = load_json(config_dir / "eligibility.json")
    scoring = load_json(config_dir / "scoring.json")
    roster = load_roster(config_dir / "roster.csv")

    if not isinstance(rooms, dict) or not isinstance(rooms.get("rooms"), list):
        die('rooms.json must be an object with a "rooms" list.')

    # ---- floor plans ------------------------------------------------------
    # A room without an `image` key contributes nothing here: no map entry, no
    # bytes and no warning. That is the shipped configuration -- rooms.json is
    # a schematic and neither the form nor the report draws a bitmap -- so
    # complaining about it would be an error message for the intended state.
    # Only a *declared* image is looked for, and only a declared one can be
    # missing.
    floorplans: dict[str, str | None] = {}
    image_digests: list[tuple[str, str]] = []
    declared: list[str] = []
    missing: list[str] = []
    embedded_bytes = 0
    for index, room in enumerate(rooms["rooms"]):
        if not isinstance(room, dict) or "id" not in room:
            die(f'rooms.json: rooms[{index}] has no "id".')
        room_id = str(room["id"])
        rel = str(room.get("image") or "")
        if not rel:
            continue
        declared.append(room_id)
        # The frontend has no bitmap path at all any more: the student map is
        # drawn from the desk rectangles. Embedding the file would add a
        # megabyte to ConfigData.gs that nothing would ever render, and the
        # coordinator would have no way to tell. Say so instead.
        warn(
            f'room "{room_id}": rooms.json declares an image ({rel}), but the web '
            f"form no longer draws floor-plan bitmaps -- the desk rectangles are "
            f"the map. The image is NOT embedded. It still affects the validator "
            f'and the report; remove the "image" key if you did not mean to keep it.'
        )
        uri, data = encode_image(config_dir, rel)
        floorplans[room_id] = uri
        if data is None:
            missing.append(room_id)
            image_digests.append((room_id, "MISSING"))
            warn(
                f'room "{room_id}": rooms.json declares floor-plan image {rel}, but it '
                f"was not found under {config_dir}. The room will render from its desk "
                f"coordinates alone. Drop the file in and re-run this script, or remove "
                f'the "image" key if the schematic is the map.'
            )
        else:
            embedded_bytes += len(data)
            image_digests.append((room_id, hashlib.sha256(data).hexdigest()))
            # Embedding is opt-in now, and worth saying out loud when someone
            # opts in: this is the line item that used to make the generated
            # file ~1.1 MB.
            warn(
                f'room "{room_id}": embedding {rel} ({len(data) / 1_000_000:.2f} MB) as '
                f"a data URI. Base64 inflates that by a third and it all ships in one "
                f'bootstrap payload. The shipped config needs no image at all; drop the '
                f'"image" key to go back to the schematic.'
            )
            if len(data) > LARGE_IMAGE_WARN_BYTES:
                warn(
                    f'room "{room_id}": {rel} is {len(data) / 1_000_000:.1f} MB, which is '
                    f"large enough to be felt on a phone; downscale it before the form "
                    f"opens."
                )

    # ---- fingerprint ------------------------------------------------------
    # Hash the raw bytes of each config file plus each *declared* image, in a
    # fixed order, so a changed floor plan changes the fingerprint too. Rooms
    # that declare no image contribute nothing: the fingerprint covers what
    # actually went into this file, and nothing about them did.
    hasher = hashlib.sha256()
    for name in CONFIG_FILES:
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(hashlib.sha256(read_bytes(config_dir / name)).hexdigest().encode("ascii"))
        hasher.update(b"\n")
    for room_id, digest in image_digests:
        hasher.update(("image:" + room_id).encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(digest.encode("ascii"))
        hasher.update(b"\n")
    fingerprint = hasher.hexdigest()[:FINGERPRINT_CHARS]

    text = render(
        rooms=rooms,
        eligibility=eligibility,
        scoring=scoring,
        roster=roster,
        floorplans=floorplans,
        fingerprint=fingerprint,
        config_dir_name=config_dir.name,
    )

    # ---- summary (all counts derived, none assumed) -----------------------
    n_desks = sum(len(r.get("desks") or []) for r in rooms["rooms"])
    zones = list((rooms.get("zones") or {}).keys())
    curves = scoring.get("curves") if isinstance(scoring, dict) else None
    primary = scoring.get("primary_curve") if isinstance(scoring, dict) else None
    if isinstance(curves, dict) and isinstance(curves.get(primary), list):
        k: int | None = len(curves[primary])
    else:
        k = None
        warn(
            'scoring.json: cannot derive K — primary_curve is '
            f'{primary!r} and curves[{primary!r}] is not a list. The web app will '
            "refuse to load until this is fixed."
        )
    n_rules = len(eligibility.get("rules") or []) if isinstance(eligibility, dict) else 0

    summary = {
        "rooms": len(rooms["rooms"]),
        "desks": n_desks,
        "zones": zones,
        "roster_rows": len(roster),
        "roster_columns": sorted({key for row in roster for key in row}),
        "rules": n_rules,
        "k": k,
        "images_declared": declared,
        "images_embedded": len(declared) - len(missing),
        "images_embedded_bytes": embedded_bytes,
        "images_missing": missing,
        "fingerprint": fingerprint,
        "bytes": len(text.encode("utf-8")),
    }
    return text, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sync_config.py",
        description="Generate frontend/ConfigData.gs from the config directory.",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=DEFAULT_CONFIG_DIR,
        help=f"directory holding {', '.join(CONFIG_FILES)} (default: {DEFAULT_CONFIG_DIR})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"file to write (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if regenerating would change --out (for CI)",
    )
    args = parser.parse_args(argv)

    config_dir = args.config_dir.resolve()
    out_path = args.out.resolve()
    if not config_dir.is_dir():
        die(f"--config-dir {config_dir} is not a directory.")

    text, summary = build(config_dir)

    if args.check:
        if not out_path.is_file():
            print(
                f"sync_config: {out_path} does not exist; run `python3 tools/sync_config.py`.",
                file=sys.stderr,
            )
            return 1
        current = out_path.read_text(encoding="utf-8")
        if current != text:
            print(
                f"sync_config: {out_path} is out of date with {config_dir}.\n"
                f"             Run `python3 tools/sync_config.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"sync_config: {out_path.name} is up to date (fingerprint {summary['fingerprint']}).")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)

    print(f"sync_config: wrote {out_path} ({summary['bytes']:,} bytes)")
    print(
        "  rooms={rooms}  desks={desks}  zones={zones}  roster={roster_rows}"
        "  rules={rules}  K={k}".format(**summary)
    )
    print("  roster columns: " + ", ".join(summary["roster_columns"]))
    if summary["images_declared"]:
        print(
            "  floor plans: {} embedded ({:,} image bytes), {} missing{}".format(
                summary["images_embedded"],
                summary["images_embedded_bytes"],
                len(summary["images_missing"]),
                (" (" + ", ".join(summary["images_missing"]) + ")")
                if summary["images_missing"] else "",
            )
        )
    else:
        print("  floor plans: none declared; no image bytes embedded")
    print("  CONFIG_FINGERPRINT = " + summary["fingerprint"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
