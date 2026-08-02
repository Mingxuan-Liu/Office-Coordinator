"""Synthetic roster / preference generator.

Everything else in this package is tested against what this module emits, so it
is written to be *adversarial*, not convenient: it produces files that the real
loaders must accept verbatim, and named scenario builders that produce the exact
pathologies the pipeline is required to survive.

Nothing here knows how big the problem is. `n_people`, `n_desks`, `n_zones`,
`n_rooms` and `K` are all arguments or derived from arguments (invariant I1).
K is always `len(scoring["curves"][scoring["primary_curve"]])`.

--------------------------------------------------------------------------
The preference model (the `concentration` knob)
--------------------------------------------------------------------------

`concentration` (written `c` below, `c ∈ [0, 1]`) is a single knob controlling
how much people's preferences overlap. The model is a random-utility ranking
model with one shared component and one idiosyncratic component:

    θ_d      ~ N(0, 1)                  ONE draw per desk, shared by everyone
                                        ("how nice is this desk, objectively")
    ξ_{p,d}  ~ iid, mean 0, variance 1  per person-desk idiosyncratic taste

    u_{p,d}  = sqrt(c) · θ_d  +  sqrt(1 - c) · ξ_{p,d}          (†)

    ranking of person p = desks sorted by u_{p,·}, descending

The coefficients are square roots so that the *variance* of `u` is
`c + (1 - c) = 1` for every c, and therefore for two distinct people p ≠ q

    Corr(u_{p,·}, u_{q,·}) = c              exactly.

So the knob is not a vibe: it is literally the between-person correlation of
latent desk utility. The endpoints are exact, not asymptotic:

  * c = 0 → u_{p,d} = ξ_{p,d}: independent, exchangeable, so every person's
    ranking is an independent uniform random permutation of the desks.
  * c = 1 → u_{p,d} = θ_d for every p: everyone ranks the identical desks in
    the identical order.

Two noise families are supported, both of which satisfy (†):

`noise="thurstone"` (default) — ξ ~ N(0,1). This is the Gaussian (Thurstone–
Mosteller) ranking model. Because (u_p, u_q) is bivariate normal with
correlation c, the expected pairwise Kendall tau has a closed form:

    E[tau(p, q)] = (2 / pi) · arcsin(c)

which is what `kendall_tau_table()` checks the empirical draw against. That
closed form is the reason this is the default: the knob is *falsifiable*.

`noise="plackett_luce"` — ξ ~ standardised Gumbel, i.e. (G - γ)/(pi/sqrt(6))
with G = -log(-log(U)). Sorting (†) is invariant to dividing by the positive
constant sqrt(1 - c), so for c < 1 the induced ranking is *exactly* a
Plackett-Luce draw with per-desk weights

    w_d = exp(λ(c) · θ_d),     λ(c) = (pi / sqrt(6)) · sqrt(c / (1 - c))

identical for every person — i.e. sequential sampling without replacement
proportional to w. λ(0) = 0 gives the uniform random permutation; λ → ∞ as
c → 1 gives the shared order. (At c = 1 exactly the formula degenerates to
u = θ, which is the same limit.)

Both families are monotone in c; `kendall_tau_table()` measures it.

--------------------------------------------------------------------------
Determinism
--------------------------------------------------------------------------

Same arguments ⇒ byte-identical `roster.csv`, `responses.csv`, `rooms.json`,
`eligibility.json`, `scoring.json`. Guarded the same way the solver is: the RNG
is seeded from `hashlib` (never `hash()`), no set is ever iterated, no clock is
read anywhere (submission timestamps are a fixed base date plus fixed offsets),
and JSON is written with `sort_keys=True` and floats rounded to a fixed number
of decimals.

The one honest caveat: the generated floor-plan PNGs are DEFLATE streams, so
their bytes depend on the local zlib build. The JSON/CSV — everything that
feeds the solve — does not.
"""

from __future__ import annotations

import argparse
import colorsys
import csv
import hashlib
import io
import json
import math
import struct
import sys
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from .types import (
    Config,
    Desk,
    DeskId,
    Eligibility,
    EligibilityRule,
    Person,
    PersonId,
    Room,
    Rooms,
    Roster,
    Scoring,
    ZoneId,
    Zone,
)

__all__ = [
    "SynthWorld",
    "SynthCase",
    "generate",
    "make_rooms",
    "make_eligibility",
    "make_scoring",
    "kendall_tau_table",
    "reference_problem",
    "count_optimal_assignments",
    "SCENARIOS",
    "scenario_names",
    "everyone_ranks_same_k_desks",
    "more_people_than_desks",
    "cohort_zone_starved",
    "empty_roster",
    "single_person",
    "duplicate_submissions",
    "stale_desk_reference",
    "all_keepers",
    "exact_fit",
    "tie_heavy",
    "main",
]

# --------------------------------------------------------------------------
# Fixed constants. None of these is a problem dimension; they are the
# generator's own defaults and every one of them is overridable.
# --------------------------------------------------------------------------

# A fixed offset-aware base date. The clock is NEVER read: reading it would make
# `responses.csv` differ between runs, which would break the byte-identity
# guarantee this module exists to provide.
_TZ = timezone(timedelta(hours=-4))
_BASE_SUBMIT = datetime(2026, 9, 15, 9, 0, 0, tzinfo=_TZ)
_BASE_COMMIT = datetime(2026, 9, 1, 12, 0, 0, tzinfo=_TZ)
_SUBMIT_STEP = timedelta(minutes=7)

_CLIENT_VERSION = "synth-1"
_EMAIL_DOMAIN = "umich.edu"

# Coordinates are rounded before serialisation so the JSON text is stable and
# readable; 6 decimals is ~0.001 px on a 1000 px image.
_COORD_DECIMALS = 6

_GIVEN_NAMES: tuple[str, ...] = (
    "Ada", "Vera", "Cecilia", "Jocelyn", "Subrahmanyan", "Annie", "Henrietta",
    "Fritz", "Nancy", "Edwin", "Beatrice", "Antonia", "Williamina", "Margaret",
    "Sandra", "Carl", "Arthur", "Karl", "Bernhard", "Georges", "Milton",
    "Yakov", "Chushiro", "Donald", "Jan", "Bertil", "Walter", "Rudolph",
    "Priyamvada", "Wendy", "Andrea", "Nergis", "Chanda", "Sara", "Bohdan",
    "Riccardo", "Martin", "Eugene", "Rashid", "Roger",
)

# Deliberately punctuated: apostrophes, hyphens and internal spaces are all
# legal in a name and all of them have broken a naive CSV/name parser before.
_SURNAMES: tuple[str, ...] = (
    "Lovelace", "Rubin", "Payne-Gaposchkin", "Bell Burnell", "Chandrasekhar",
    "Cannon", "Leavitt", "Zwicky", "Roman", "Hubble", "Tinsley", "Maury",
    "Fleming", "Burbidge", "Faber", "Sagan", "Eddington", "Schwarzschild",
    "Riemann", "Lemaitre", "Humason", "Zeldovich", "Hayashi", "Lynden-Bell",
    "Oort", "Lindblad", "Baade", "Minkowski", "Natarajan", "Freedman",
    "Ghez", "Mavalvala", "Prescod-Weinstein", "Seager", "Paczynski",
    "Giacconi", "Schwarz", "Parker", "Sunyaev", "Penrose", "O'Dell",
    "de Sitter", "van Maanen", "d'Arrest", "St John",
)

_GREEK: tuple[str, ...] = (
    "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
    "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron", "pi", "rho",
    "sigma", "tau", "upsilon", "phi", "chi", "psi", "omega",
)

_TRUTHY_TOKENS: tuple[str, ...] = ("yes", "true", "1", "y", "Yes", "TRUE")
_FALSY_TOKENS: tuple[str, ...] = ("no", "false", "0", "n", "No", "FALSE")

_AUTH_METHODS: tuple[str, ...] = ("google", "self_select")

ROSTER_BASE_FIELDS: tuple[str, ...] = (
    "name", "email", "year", "candidacy", "keeps_desk", "current_desk",
)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    """Generator-misuse guard.

    These are ValueErrors, not ConfigError/ResponseError: a bad argument to
    `generate()` is a programming mistake, not something a coordinator can
    cause by editing a config file. The CLI catches them and exits 1 (usage).
    """
    if not condition:
        raise ValueError(message)


def seed_int(seed: int | str) -> int:
    """Derive a 64-bit RNG seed. Mirrors SPEC §5.4 for string seeds.

    `hashlib`, never `hash()`: the builtin is salted per interpreter start
    (PYTHONHASHSEED) and would silently destroy reproducibility.
    """
    if isinstance(seed, bool):  # bool is an int subclass; almost certainly a bug
        raise TypeError("seed must be an int or a str, not a bool")
    if isinstance(seed, int):
        return int(seed) & 0xFFFFFFFFFFFFFFFF
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _rng(seed: int | str) -> np.random.Generator:
    # PCG64 via default_rng: NumPy guarantees stream stability for it, so the
    # same seed gives the same numbers on any platform and NumPy version.
    return np.random.default_rng(seed_int(seed))


def _uuid_for(seed_string: str, tag: str) -> str:
    """A deterministic RFC-4122-shaped uuid, standing in for Utilities.getUuid()."""
    digest = hashlib.sha256(f"{seed_string}\x00{tag}".encode("utf-8")).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def _dump_json(obj: Any) -> str:
    # sort_keys for byte-identity; ensure_ascii so the file is pure ASCII and
    # cannot pick up an encoding difference between platforms.
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def _dump_csv(fieldnames: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> str:
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(
        buf, fieldnames=list(fieldnames), lineterminator="\n", extrasaction="raise"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in fieldnames})
    return buf.getvalue()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _round_coord(value: float) -> float:
    return round(float(value), _COORD_DECIMALS)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    raw = value.lstrip("#")
    if len(raw) != 6:
        return (102, 102, 102)
    return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))


def _zone_color(index: int, total: int) -> str:
    """Evenly spaced hues so an arbitrary number of zones stays legible."""
    hue = (index / max(1, total)) % 1.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.55, 0.68)
    return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))


def _split_evenly(total: int, parts: int) -> tuple[int, ...]:
    """Split `total` into `parts` sizes differing by at most 1, largest first."""
    _require(parts >= 1, "cannot split into fewer than one part")
    base, remainder = divmod(total, parts)
    return tuple(base + (1 if i < remainder else 0) for i in range(parts))


# --------------------------------------------------------------------------
# Floor-plan PNG rendering (stdlib only)
# --------------------------------------------------------------------------


def _png_bytes(image: np.ndarray) -> bytes:
    """Encode an (h, w, 3) uint8 array as a PNG.

    Hand-rolled because Pillow is not in the dependency set and the point of
    these images is only that `rooms.json:image` resolves to a real file of the
    declared `image_size`, so the validator does not warn and the report's
    floor-plan heatmap has something to draw on.
    """
    height, width, _ = image.shape
    # PNG scanlines are each prefixed with a filter-type byte; 0 = None.
    raw = np.zeros((height, width * 3 + 1), dtype=np.uint8)
    raw[:, 1:] = image.reshape(height, width * 3)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6))
        + chunk(b"IEND", b"")
    )


def _desk_bbox(
    desk: Mapping[str, Any], width: int, height: int, coord_space: str
) -> tuple[int, int, int, int]:
    """Pixel bounding box of a desk shape, for both rect and polygon."""
    shape = desk["shape"]
    if "rect" in shape:
        x, y, w, h = (float(v) for v in shape["rect"])
        xs, ys = (x, x + w), (y, y + h)
    else:
        points = shape["polygon"]
        xs = tuple(float(p[0]) for p in points)
        ys = tuple(float(p[1]) for p in points)
    sx, sy = (width, height) if coord_space == "normalized" else (1.0, 1.0)
    x0 = int(round(min(xs) * sx))
    x1 = int(round(max(xs) * sx))
    y0 = int(round(min(ys) * sy))
    y1 = int(round(max(ys) * sy))
    # Clamp and keep at least one pixel so tiny desks still render.
    x0 = max(0, min(width - 1, x0))
    y0 = max(0, min(height - 1, y0))
    x1 = max(x0 + 1, min(width, x1))
    y1 = max(y0 + 1, min(height, y1))
    return x0, y0, x1, y1


def render_room_png(rooms: Mapping[str, Any], room: Mapping[str, Any]) -> bytes:
    """A schematic floor plan: zone-tinted desk rectangles on a light ground."""
    width, height = (int(v) for v in room["image_size"])
    image = np.full((height, width, 3), 247, dtype=np.uint8)
    image[:6, :, :] = 210
    image[-6:, :, :] = 210
    image[:, :6, :] = 210
    image[:, -6:, :] = 210

    zones = rooms["zones"]
    coord_space = rooms["coord_space"]
    for desk in room["desks"]:
        x0, y0, x1, y1 = _desk_bbox(desk, width, height, coord_space)
        color = np.array(
            _hex_to_rgb(zones.get(desk["zone"], {}).get("color", "#666666")),
            dtype=np.float64,
        )
        fill = np.clip(color * 0.35 + 255.0 * 0.65, 0, 255).astype(np.uint8)
        edge = color.astype(np.uint8)
        image[y0:y1, x0:x1] = fill
        border = max(1, min(3, (x1 - x0) // 12, (y1 - y0) // 12))
        image[y0 : y0 + border, x0:x1] = edge
        image[y1 - border : y1, x0:x1] = edge
        image[y0:y1, x0 : x0 + border] = edge
        image[y0:y1, x1 - border : x1] = edge
    return _png_bytes(image)


# --------------------------------------------------------------------------
# rooms.json
# --------------------------------------------------------------------------


def make_rooms(
    n_desks: int,
    *,
    n_rooms: int | None = None,
    n_zones: int = 2,
    zone_sizes: Sequence[int] | None = None,
    coord_space: str = "normalized",
    image_size: tuple[int, int] = (1000, 700),
    desk_id_prefix: str = "D",
    unavailable: Sequence[DeskId] = (),
    label_prefix: str = "Synthetic Room",
) -> dict[str, Any]:
    """Build a `rooms.json` payload of arbitrary size.

    `n_desks` desks are laid out on a real grid across `n_rooms` rooms with
    genuine, non-overlapping rectangles inside the image bounds, so the map
    renderer and the report heatmap can be exercised without the real floor
    plan. Zones are contiguous chunks of the global desk sequence (a zone may
    therefore span rooms, which is legal and worth testing); pass `zone_sizes`
    to control the split exactly — that is how the zone-starvation scenario is
    built.
    """
    _require(n_desks >= 0, f"n_desks must be >= 0, got {n_desks}")
    _require(
        coord_space in ("normalized", "pixels"),
        f"coord_space must be 'normalized' or 'pixels', got {coord_space!r}",
    )
    width, height = (int(image_size[0]), int(image_size[1]))
    _require(width > 0 and height > 0, f"image_size must be positive, got {image_size}")

    if n_desks == 0:
        n_rooms_eff, n_zones_eff = 0, 0
    else:
        n_rooms_eff = n_rooms if n_rooms is not None else max(1, math.ceil(n_desks / 40))
        _require(
            1 <= n_rooms_eff <= n_desks,
            f"n_rooms must be between 1 and n_desks ({n_desks}), got {n_rooms_eff}",
        )
        n_zones_eff = len(zone_sizes) if zone_sizes is not None else n_zones
        _require(
            1 <= n_zones_eff <= n_desks,
            f"n_zones must be between 1 and n_desks ({n_desks}), got {n_zones_eff}",
        )

    # Zone ids. Arbitrary strings as far as the rest of the package is
    # concerned; greek letters just keep them readable past two zones.
    zone_ids: tuple[ZoneId, ...] = tuple(
        f"zone_{_GREEK[i]}" if i < len(_GREEK) else f"zone_{i:03d}"
        for i in range(n_zones_eff)
    )
    zones: dict[str, Any] = {
        zid: {
            "label": f"Zone {zid.split('_', 1)[1].title()}",
            "color": _zone_color(i, max(1, n_zones_eff)),
        }
        for i, zid in enumerate(zone_ids)
    }

    if zone_sizes is None:
        sizes = _split_evenly(n_desks, n_zones_eff) if n_zones_eff else ()
    else:
        sizes = tuple(int(s) for s in zone_sizes)
        _require(
            sum(sizes) == n_desks,
            f"zone_sizes must sum to n_desks ({n_desks}), got {sum(sizes)}",
        )
        _require(all(s >= 1 for s in sizes), "every zone must contain at least one desk")

    # Global desk index -> zone id. Contiguous blocks.
    desk_zone: list[ZoneId] = []
    for zid, size in zip(zone_ids, sizes):
        desk_zone.extend([zid] * size)

    id_width = max(2, len(str(max(1, n_desks))))
    unavailable_set = frozenset(unavailable)

    rooms_out: list[dict[str, Any]] = []
    per_room = _split_evenly(n_desks, n_rooms_eff) if n_rooms_eff else ()
    cursor = 0
    for room_index, count in enumerate(per_room):
        # Grid shaped to the image aspect so the plan looks like a room, not a
        # column of desks.
        cols = max(1, math.ceil(math.sqrt(count * (width / height))))
        rows = max(1, math.ceil(count / cols))
        margin = 0.06
        cell_w = (1.0 - 2 * margin) / cols
        cell_h = (1.0 - 2 * margin) / rows
        desk_w, desk_h = cell_w * 0.72, cell_h * 0.62  # < cell ⇒ never overlaps

        desks: list[dict[str, Any]] = []
        for j in range(count):
            gi = cursor + j
            col, row = j % cols, j // cols
            x = margin + col * cell_w + (cell_w - desk_w) / 2.0
            y = margin + row * cell_h + (cell_h - desk_h) / 2.0
            if coord_space == "normalized":
                rect = [x, y, desk_w, desk_h]
            else:
                rect = [x * width, y * height, desk_w * width, desk_h * height]
            desk_id = f"{desk_id_prefix}{gi + 1:0{id_width}d}"
            desk: dict[str, Any] = {
                "id": desk_id,
                "label": str(gi + 1),
                "zone": desk_zone[gi],
                "shape": {"rect": [_round_coord(v) for v in rect]},
            }
            if j == count - 1 and count > 1:
                desk["notes"] = "Last desk in the row block; nearest the door."
            if desk_id in unavailable_set:
                desk["available"] = False
            desks.append(desk)
        cursor += count

        rooms_out.append(
            {
                "id": f"room_{room_index + 1:02d}",
                "label": f"{label_prefix} {room_index + 1}",
                "image": f"floorplans/room_{room_index + 1:02d}.png",
                "image_size": [width, height],
                "desks": desks,
            }
        )

    payload: dict[str, Any] = {
        "schema_version": 1,
        "_comment": (
            "Generated by deskmatch.synth. Desks are laid out on a grid; "
            "coordinates are real and non-overlapping. Not a real floor plan."
        ),
        "coord_space": coord_space,
        "zones": zones,
        "rooms": rooms_out,
    }

    # Self-check: every coordinate inside the declared space (SPEC §2.1).
    hi_x, hi_y = (1.0, 1.0) if coord_space == "normalized" else (float(width), float(height))
    for room in rooms_out:
        for desk in room["desks"]:
            x, y, w, h = desk["shape"]["rect"]
            _require(
                0.0 <= x and 0.0 <= y and x + w <= hi_x + 1e-9 and y + h <= hi_y + 1e-9,
                f"generated desk {desk['id']} escapes the {coord_space} coordinate space",
            )
    return payload


def rooms_desk_index(rooms: Mapping[str, Any]) -> tuple[tuple[DeskId, ...], dict[DeskId, ZoneId]]:
    """(desk ids in file order, desk id -> zone). File order, never set order."""
    ids: list[DeskId] = []
    zone_of: dict[DeskId, ZoneId] = {}
    for room in rooms["rooms"]:
        for desk in room["desks"]:
            ids.append(desk["id"])
            zone_of[desk["id"]] = desk["zone"]
    return tuple(ids), zone_of


def rooms_unavailable(rooms: Mapping[str, Any]) -> tuple[DeskId, ...]:
    """Desks administratively out of the pool (SPEC §3.4), in file order."""
    return tuple(
        desk["id"]
        for room in rooms["rooms"]
        for desk in room["desks"]
        if desk.get("available", True) is False
    )


# --------------------------------------------------------------------------
# eligibility.json
# --------------------------------------------------------------------------


def make_eligibility(
    zone_ids: Sequence[ZoneId],
    *,
    style: str = "cohort",
    precandidate_zones: Sequence[ZoneId] | None = None,
    precandidate_value: str = "precandidate",
    candidate_value: str = "candidate",
) -> dict[str, Any]:
    """Build an `eligibility.json` rule table over the given zones.

    style="flat"   : a single catch-all rule; everyone may sit anywhere. Used
                     wherever a scenario needs a common ground set (the
                     Kendall-tau measurement, exact_fit, tie_heavy).
    style="cohort" : pre-candidates are confined to `precandidate_zones`
                     (default: the first half of the zones, at least one),
                     everyone else is unconstrained. Mirrors the real config.

    The catch-all is always last, so nobody can fall through with undefined
    eligibility (SPEC §2.2).

    The cohort table names `candidate_value` explicitly even though the
    catch-all would already cover it. Leaving it implicit is legal but earns a
    validator warning ("this candidacy value is never referenced by any rule"),
    and the default output of this generator should be warning-free so that a
    warning in a dry run means something.
    """
    _require(style in ("flat", "cohort"), f"unknown eligibility style {style!r}")
    catch_all = {
        "id": "everyone_else_anywhere",
        "when": {},
        "allow_zones": "*",
        "reason": "No cohort restriction applies; may sit anywhere.",
    }
    rules: list[dict[str, Any]] = []
    if style == "cohort" and zone_ids:
        if precandidate_zones is None:
            cut = max(1, len(zone_ids) // 2)
            chosen = tuple(zone_ids[:cut])
        else:
            chosen = tuple(precandidate_zones)
            unknown = sorted(set(chosen) - set(zone_ids))
            _require(not unknown, f"precandidate_zones not defined in rooms.json: {unknown}")
        rules.append(
            {
                "id": "precandidates_sit_together",
                "when": {"candidacy": precandidate_value},
                "allow_zones": list(chosen),
                "reason": "Pre-candidates are seated together as a cohort.",
            }
        )
        rules.append(
            {
                "id": "candidates_anywhere",
                "when": {"candidacy": candidate_value},
                "allow_zones": "*",
                "reason": "Post-candidacy students may sit anywhere.",
            }
        )
    rules.append(catch_all)
    return {
        "schema_version": 1,
        "_comment": (
            "Generated by deskmatch.synth. Evaluated top to bottom, first match "
            "wins; the last rule is the required catch-all."
        ),
        "rules": rules,
    }


# --------------------------------------------------------------------------
# scoring.json
# --------------------------------------------------------------------------


def make_scoring(
    k: int,
    *,
    seed_string: str,
    primary_curve: str = "linear_borda",
    n_sensitivity_seeds: int = 3,
) -> dict[str, Any]:
    """Build a `scoring.json` with curves of length K.

    K is whatever `k` says; the curves are generated from it. Every curve is
    strictly decreasing and strictly positive (SPEC §2.4). `concave` uses halves
    so it exercises the decimal→exact-integer rationalisation path (§5.3) while
    still terminating.
    """
    _require(k >= 1, f"k must be >= 1, got {k}")
    linear = [k - i for i in range(k)]
    convex = [2 ** (k - 1 - i) for i in range(k)]
    concave = [k - 0.5 * i for i in range(k)]  # last term (k+1)/2 > 0
    curves = {"linear_borda": linear, "convex": convex, "concave": concave}
    _require(
        primary_curve in curves,
        f"primary_curve {primary_curve!r} is not one of {sorted(curves)}",
    )
    comparison = tuple(name for name in sorted(curves) if name != primary_curve)
    return {
        "schema_version": 1,
        "_comment": (
            "Generated by deskmatch.synth. K is len(curves[primary_curve]); "
            "nothing declares it separately."
        ),
        "curves": curves,
        "primary_curve": primary_curve,
        "comparison_curves": list(comparison),
        "tie_break_seed": seed_string,
        "seed_committed_at": _BASE_COMMIT.isoformat(),
        "sensitivity_seeds": [
            f"{seed_string}-sensitivity-{_GREEK[i]}" if i < len(_GREEK)
            else f"{seed_string}-sensitivity-{i:03d}"
            for i in range(max(0, n_sensitivity_seeds))
        ],
    }


def scoring_k(scoring: Mapping[str, Any]) -> int:
    """K, derived the only legal way (invariant I1)."""
    return len(scoring["curves"][scoring["primary_curve"]])


# --------------------------------------------------------------------------
# The preference model
# --------------------------------------------------------------------------

_GUMBEL_MEAN = 0.5772156649015329          # Euler-Mascheroni γ
_GUMBEL_SCALE = math.pi / math.sqrt(6.0)   # sd of a standard Gumbel

NOISE_FAMILIES: tuple[str, ...] = ("thurstone", "plackett_luce")


def latent_utilities(
    rng: np.random.Generator,
    n_people: int,
    n_desks: int,
    concentration: float,
    noise: str = "thurstone",
) -> np.ndarray:
    """Draw the (n_people, n_desks) latent utility matrix of the module docstring.

        u[p, d] = sqrt(c) * theta[d] + sqrt(1 - c) * xi[p, d]

    theta is drawn once and shared; xi is iid with mean 0 and variance 1, so
    Corr(u_p, u_q) = c exactly for p != q. See the module docstring for why the
    two noise families are the Thurstone and Plackett-Luce models respectively.
    """
    _require(
        0.0 <= concentration <= 1.0,
        f"concentration must be in [0, 1], got {concentration}",
    )
    _require(noise in NOISE_FAMILIES, f"noise must be one of {NOISE_FAMILIES}, got {noise!r}")
    c = float(concentration)
    theta = rng.standard_normal(n_desks)
    if noise == "thurstone":
        xi = rng.standard_normal((n_people, n_desks))
    else:
        # Standardising the Gumbel is an affine map with positive scale, so it
        # does not touch the induced ranking — it only puts the two families on
        # the same variance footing so `c` means the same thing in both.
        xi = (rng.gumbel(0.0, 1.0, size=(n_people, n_desks)) - _GUMBEL_MEAN) / _GUMBEL_SCALE
    # The draws above happen unconditionally, including at c = 0 and c = 1, so
    # the RNG stream position does not depend on the knob.
    return math.sqrt(c) * theta[None, :] + math.sqrt(1.0 - c) * xi


def preference_orders(
    utilities: np.ndarray, desk_ids: Sequence[DeskId]
) -> tuple[tuple[DeskId, ...], ...]:
    """Rank desks best-first for each row of `utilities`.

    A stable argsort on the negated utilities: descending, with ties broken by
    desk index rather than by whatever the sort happened to do. Ties have
    probability zero with continuous noise, but "probability zero" is not
    "impossible" and determinism is a correctness property (I3).
    """
    if utilities.size == 0:
        return tuple(() for _ in range(utilities.shape[0]))
    order = np.argsort(-utilities, axis=1, kind="stable")
    return tuple(tuple(desk_ids[j] for j in row) for row in order.tolist())


# --------------------------------------------------------------------------
# Eligibility evaluation
# --------------------------------------------------------------------------
#
# `eligibility.py` is authoritative at solve time. This is the generator's own
# mirror of SPEC §2.2, needed because the generator has to know which desks a
# person may legally rank *before* the loaders exist. It is deliberately a
# complete implementation of the documented grammar rather than a shortcut, so
# that a disagreement between the two is a real, findable bug rather than a
# limitation of the generator.


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


def _scalar_matches(value: Any, expected: Any) -> bool:
    lhs, rhs = _as_number(value), _as_number(expected)
    if lhs is not None and rhs is not None:
        return lhs == rhs
    return str(value).strip().casefold() == str(expected).strip().casefold()


def _matches(value: Any, matcher: Any) -> bool:
    if isinstance(matcher, Mapping):
        if "not" in matcher:
            return not _matches(value, matcher["not"])
        if "min" in matcher or "max" in matcher:
            number = _as_number(value)
            if number is None:
                return False
            if "min" in matcher and number < float(matcher["min"]):
                return False
            if "max" in matcher and number > float(matcher["max"]):
                return False
            return True
        return False
    if isinstance(matcher, (list, tuple)):
        return any(_matches(value, option) for option in matcher)
    return _scalar_matches(value, matcher)


def allowed_zones_for(
    person: Mapping[str, Any], eligibility: Mapping[str, Any], zone_ids: Sequence[ZoneId]
) -> tuple[ZoneId, ...]:
    """Evaluate the rule table top-to-bottom, first match wins (SPEC §2.2)."""
    for rule in eligibility["rules"]:
        when = rule.get("when", {})
        if all(_matches(person.get(attr), matcher) for attr, matcher in when.items()):
            allow = rule.get("allow_zones", "*")
            if allow == "*":
                return tuple(zone_ids)
            # Sorted, because this tuple reaches the choice list and therefore
            # the output file. Never let a set's iteration order decide bytes.
            return tuple(sorted(set(allow) & set(zone_ids)))
    # Unreachable with a validated table (the catch-all is mandatory), but a
    # generator that silently produced "eligible for nothing" would be worse.
    return ()


# --------------------------------------------------------------------------
# Roster construction
# --------------------------------------------------------------------------


def _person_name(index: int) -> str:
    # Both components advance every step, so the pair repeats only after
    # lcm(len(given), len(surnames)) people rather than after len(given).
    given = _GIVEN_NAMES[index % len(_GIVEN_NAMES)]
    surname = _SURNAMES[index % len(_SURNAMES)]
    cycle = index // math.lcm(len(_GIVEN_NAMES), len(_SURNAMES))
    name = f"{given} {surname}"
    if cycle:
        name = f"{name} {chr(ord('A') + cycle % 26)}"
    # One deliberately comma-bearing name, so every run of the loader has to
    # survive a quoted CSV field. Index 4 rather than 0 so tiny scenarios
    # (single_person) stay boring.
    if index == 4:
        name = f"{surname}, {given}"
    return name


def _email_for(name: str, taken: Mapping[str, int], index: int) -> str:
    cleaned = "".join(ch for ch in name.replace(",", " ") if ch.isalnum() or ch.isspace())
    parts = [p for p in cleaned.split() if p]
    if not parts:
        stem = f"person{index}"
    elif len(parts) == 1:
        stem = parts[0].lower()
    else:
        stem = (parts[0][0] + "".join(parts[1:])).lower()
    candidate = f"{stem}@{_EMAIL_DOMAIN}"
    if candidate in taken:
        candidate = f"{stem}{index}@{_EMAIL_DOMAIN}"
    return candidate


def _bool_token(value: bool, index: int, vary: bool) -> str:
    """Emit one of the accepted truthy/falsy spellings (SPEC §2.3).

    Varying them is the only way the loader's token table actually gets tested.
    """
    table = _TRUTHY_TOKENS if value else _FALSY_TOKENS
    return table[index % len(table)] if vary else ("yes" if value else "no")


# --------------------------------------------------------------------------
# The generated world
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SynthCase:
    """A scenario, ready to hand to the CLI or to a test.

    `config_dir` / `responses_path` are None when the caller asked for the
    in-memory form only; `world` is always present.
    """

    name: str
    description: str
    expectation: str            # what the pipeline is supposed to DO with this
    world: SynthWorld
    config_dir: Path | None = None
    responses_path: Path | None = None

    def as_paths(self) -> tuple[Path, Path]:
        """(config_dir, responses_path); raises if this case was never written."""
        if self.config_dir is None or self.responses_path is None:
            raise ValueError(
                f"scenario {self.name!r} was built in memory only; "
                f"call the builder with out_dir=... to get files on disk"
            )
        return self.config_dir, self.responses_path


@dataclass(frozen=True)
class SynthWorld:
    """Everything the generator produced, in memory.

    All collections are tuples in a deterministic order. `roster_rows` and
    `response_rows` are the literal CSV rows (values already stringified), so
    what a test asserts on is exactly what the loader will read.
    """

    rooms: Mapping[str, Any]
    eligibility: Mapping[str, Any]
    scoring: Mapping[str, Any]
    roster_fields: tuple[str, ...]
    roster_rows: tuple[Mapping[str, str], ...]
    response_fields: tuple[str, ...]
    response_rows: tuple[Mapping[str, str], ...]
    k: int
    seed_string: str
    concentration: float
    noise: str
    desk_ids: tuple[DeskId, ...]
    zone_of_desk: Mapping[DeskId, ZoneId]
    allowed_zones: Mapping[PersonId, tuple[ZoneId, ...]]
    full_orders: Mapping[PersonId, tuple[DeskId, ...]]   # latent order, ALL desks
    pool_desks: tuple[DeskId, ...]
    pool_people: tuple[PersonId, ...]
    keeper_desks: Mapping[PersonId, DeskId]
    expected_latest: Mapping[PersonId, str]              # email -> submission_id
    padded_people: tuple[PersonId, ...]
    notes: tuple[str, ...] = ()

    # -- derived sizes: never stored, always counted ----------------------

    @property
    def n_people(self) -> int:
        return len(self.roster_rows)

    @property
    def n_desks(self) -> int:
        return len(self.desk_ids)

    @property
    def zone_ids(self) -> tuple[ZoneId, ...]:
        return tuple(self.rooms["zones"].keys())

    @property
    def emails(self) -> tuple[PersonId, ...]:
        return tuple(row["email"] for row in self.roster_rows)

    def latest_choices(self) -> Mapping[PersonId, tuple[DeskId, ...]]:
        """email -> the winning submission's choices, per SPEC §3.2."""
        by_id = {row["submission_id"]: row for row in self.response_rows}
        out: dict[PersonId, tuple[DeskId, ...]] = {}
        for email in sorted(self.expected_latest):
            row = by_id[self.expected_latest[email]]
            out[email] = tuple(row[f"choice_{i + 1}"] for i in range(self.k))
        return out

    # -- serialisation ----------------------------------------------------

    def file_texts(self) -> Mapping[str, str]:
        """Canonical text of every text file, keyed by name. The bytes tests hash."""
        return {
            "rooms.json": _dump_json(self.rooms),
            "eligibility.json": _dump_json(self.eligibility),
            "scoring.json": _dump_json(self.scoring),
            "roster.csv": _dump_csv(self.roster_fields, self.roster_rows),
            "responses.csv": _dump_csv(self.response_fields, self.response_rows),
        }

    def write(
        self,
        out_dir: str | Path,
        *,
        config_subdir: str = "config",
        responses_name: str = "responses.csv",
        write_images: bool = True,
    ) -> tuple[Path, Path]:
        """Write config/ + responses.csv under `out_dir`. Returns both paths."""
        root = Path(out_dir)
        config_dir = root / config_subdir
        config_dir.mkdir(parents=True, exist_ok=True)
        texts = self.file_texts()
        for name in ("rooms.json", "eligibility.json", "scoring.json", "roster.csv"):
            _write_text(config_dir / name, texts[name])
        responses_path = root / responses_name
        _write_text(responses_path, texts["responses.csv"])
        if write_images:
            plans = config_dir / "floorplans"
            plans.mkdir(parents=True, exist_ok=True)
            for room in self.rooms["rooms"]:
                (plans / Path(room["image"]).name).write_bytes(
                    render_room_png(self.rooms, room)
                )
        return config_dir, responses_path

    # -- in-memory typed views (unit tests that must not touch disk) ------

    def to_rooms(self) -> Rooms:
        zones = {
            zid: Zone(id=zid, label=meta.get("label", zid), color=meta.get("color", "#666666"))
            for zid, meta in self.rooms["zones"].items()
        }
        rooms = tuple(
            Room(
                id=room["id"],
                label=room["label"],
                image=room["image"],
                image_size=(int(room["image_size"][0]), int(room["image_size"][1])),
                desks=tuple(
                    Desk(
                        id=desk["id"],
                        label=desk["label"],
                        zone=desk["zone"],
                        room_id=room["id"],
                        shape_kind="rect" if "rect" in desk["shape"] else "polygon",
                        shape=(
                            tuple(float(v) for v in desk["shape"]["rect"])
                            if "rect" in desk["shape"]
                            else tuple(
                                (float(p[0]), float(p[1])) for p in desk["shape"]["polygon"]
                            )
                        ),
                        available=bool(desk.get("available", True)),
                        notes=desk.get("notes", ""),
                    )
                    for desk in room["desks"]
                ),
            )
            for room in self.rooms["rooms"]
        )
        return Rooms(
            schema_version=int(self.rooms["schema_version"]),
            coord_space=self.rooms["coord_space"],
            zones=zones,
            rooms=rooms,
        )

    def to_eligibility(self) -> Eligibility:
        return Eligibility(
            schema_version=int(self.eligibility["schema_version"]),
            rules=tuple(
                EligibilityRule(
                    id=rule["id"],
                    when=dict(rule.get("when", {})),
                    allow_zones=(
                        "*" if rule.get("allow_zones", "*") == "*"
                        else tuple(rule["allow_zones"])
                    ),
                    reason=rule.get("reason", ""),
                )
                for rule in self.eligibility["rules"]
            ),
        )

    def to_roster(self) -> Roster:
        return Roster(
            people=tuple(
                Person(
                    email=row["email"],
                    name=row["name"],
                    year=int(row["year"]),
                    candidacy=row["candidacy"],
                    keeps_desk=row["keeps_desk"].strip().casefold() in
                    {t.casefold() for t in _TRUTHY_TOKENS},
                    current_desk=row["current_desk"] or None,
                    attributes=dict(row),
                )
                for row in self.roster_rows
            )
        )

    def to_scoring(self) -> Scoring:
        return Scoring(
            schema_version=int(self.scoring["schema_version"]),
            curves={
                name: tuple(Fraction(str(v)) for v in values)
                for name, values in self.scoring["curves"].items()
            },
            primary_curve=self.scoring["primary_curve"],
            comparison_curves=tuple(self.scoring["comparison_curves"]),
            tie_break_seed=self.scoring["tie_break_seed"],
            seed_committed_at=self.scoring.get("seed_committed_at"),
            sensitivity_seeds=tuple(self.scoring.get("sensitivity_seeds", ())),
        )

    def to_config(self, source_dir: str = "<synth>") -> Config:
        """Assemble a `types.Config` without touching the filesystem.

        Convenience for unit tests only. `config.load_config()` is the
        authoritative path and additionally produces validation warnings; this
        does no validation at all.
        """
        texts = self.file_texts()
        return Config(
            rooms=self.to_rooms(),
            eligibility=self.to_eligibility(),
            roster=self.to_roster(),
            scoring=self.to_scoring(),
            source_dir=source_dir,
            file_hashes={
                name: _sha256_text(texts[name])
                for name in ("rooms.json", "eligibility.json", "roster.csv", "scoring.json")
            },
            warnings=self.notes,
        )


def _write_text(path: Path, text: str) -> None:
    # newline="" so nothing is translated on Windows: the same bytes everywhere.
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


# --------------------------------------------------------------------------
# generate()
# --------------------------------------------------------------------------

# Signature of the per-person choice hook. Returning None means "use the
# model's own top-K". Scenario builders use it to force exact structure
# (identical blocks, unique optima, stale ids) without post-editing rows.
ChoicesFn = Callable[
    [int, Mapping[str, str], "tuple[DeskId, ...]", "tuple[DeskId, ...]"],
    "Sequence[DeskId] | None",
]


def generate(
    n_people: int | None = None,
    *,
    # -- problem shape ---------------------------------------------------
    n_desks: int | None = None,
    rooms: Mapping[str, Any] | None = None,
    eligibility: Mapping[str, Any] | None = None,
    scoring: Mapping[str, Any] | None = None,
    roster_rows: Sequence[Mapping[str, Any]] | None = None,
    k: int = 5,
    seed: int | str = 0,
    # -- preference model ------------------------------------------------
    concentration: float = 0.35,
    noise: str = "thurstone",
    # -- rooms.json ------------------------------------------------------
    n_rooms: int | None = None,
    n_zones: int = 2,
    zone_sizes: Sequence[int] | None = None,
    coord_space: str = "normalized",
    image_size: tuple[int, int] = (1000, 700),
    desk_id_prefix: str = "D",
    unavailable_frac: float = 0.0,
    # -- eligibility.json ------------------------------------------------
    eligibility_style: str = "cohort",
    precandidate_zones: Sequence[ZoneId] | None = None,
    # -- roster.csv ------------------------------------------------------
    max_year: int = 6,
    precandidate_max_year: int = 2,
    n_precandidates: int | None = None,
    keeper_frac: float = 0.1,
    n_keepers: int | None = None,
    keepers_submit: bool = False,
    extra_columns: bool = True,
    vary_bool_tokens: bool = False,
    # -- responses.csv ---------------------------------------------------
    response_rate: float = 1.0,
    resubmit_frac: float = 0.0,
    extra_submissions: int = 1,
    conflict_frac: float = 0.0,
    stale_frac: float = 0.0,
    shuffle_rows: bool = False,
    allow_zero_valid_choices: bool = False,
    choices_fn: ChoicesFn | None = None,
    seed_string: str | None = None,
    notes: Sequence[str] = (),
) -> SynthWorld:
    """Generate a roster and a response set the real loaders accept verbatim.

    Sizes
    -----
    Pass `n_people` and either `n_desks` or a prebuilt `rooms` mapping. With
    neither, `n_desks` defaults to `max(k * n_zones, n_people + n_people//8)`:
    enough that every zone holds at least K desks, so a cohort-restricted
    person can always name K eligible ones without padding.

    Reusing the real config
    -----------------------
    `rooms`, `eligibility`, `scoring` and `roster_rows` each accept the parsed
    contents of a real file, so `generate(rooms=real_rooms, scoring=real_scoring,
    roster_rows=real_roster)` produces a response set for the department's
    actual config. When `scoring` is given, K comes from it, never from `k`.

    Defaults are clean
    ------------------
    Out of the box this produces a warning-free instance suitable for the
    runbook's dry run: everyone responds once, nothing is stale, no roster
    conflicts. Every pathology is an explicit opt-in knob (`response_rate`,
    `resubmit_frac`, `conflict_frac`, `stale_frac`, `unavailable_frac`), which
    is what the scenario builders below turn on.

    Feasibility is *not* promised, and cannot be: whether everyone can get a
    top-K desk depends on how much the preferences collide, which is the whole
    point of `concentration`. Measured deficiency at the default desk count
    (K=5, two zones, `-n` = shortfall, seeded per row):

        N \\ c    0.0   0.1   0.2   0.35   0.5    0.7
          5      OK    OK    OK    OK     OK     OK
         35      OK    OK    OK    OK     -1     -7
        100      OK    -5    -12   -27    -39    -55
        200      OK    -4    -25   -58    -90    -128

    That is the model telling the truth: 200 people who mostly agree which
    desks are good, naming only five each, genuinely cannot all be seated. The
    binding constraint is `concentration`, not the desk count — at c=0 even
    N=200 solves with the default 12.5% slack. For a large instance that always
    solves, pass `concentration=0.0`; for one with a known answer, use
    `exact_fit`; for a guaranteed failure, use the infeasible scenarios.

    RNG draw order is fixed and documented in the body; inserting a draw in the
    middle changes every downstream file, so don't.
    """
    seed_text = seed_string if seed_string is not None else (
        seed if isinstance(seed, str) else f"synth-seed-{seed}"
    )
    rng = _rng(seed)

    # ---- 1. scoring / K ------------------------------------------------
    scoring_payload = dict(scoring) if scoring is not None else make_scoring(k, seed_string=seed_text)
    k_eff = scoring_k(scoring_payload)
    _require(k_eff >= 1, "K (len of the primary curve) must be at least 1")

    # ---- 2. roster size ------------------------------------------------
    if roster_rows is not None:
        n_people_eff = len(roster_rows)
    else:
        _require(n_people is not None, "pass n_people, or roster_rows to reuse a real roster")
        n_people_eff = int(n_people)  # type: ignore[arg-type]
    _require(n_people_eff >= 0, f"n_people must be >= 0, got {n_people_eff}")

    # ---- 3. rooms ------------------------------------------------------
    if rooms is not None:
        rooms_payload: dict[str, Any] = json.loads(json.dumps(rooms))  # deep copy, no aliasing
    else:
        if n_desks is None:
            desks_needed = max(k_eff * max(1, n_zones), n_people_eff + max(1, n_people_eff // 8))
        else:
            desks_needed = int(n_desks)
        rooms_payload = make_rooms(
            desks_needed,
            n_rooms=n_rooms,
            n_zones=n_zones,
            zone_sizes=zone_sizes,
            coord_space=coord_space,
            image_size=image_size,
            desk_id_prefix=desk_id_prefix,
        )
    all_desks, zone_of_desk = rooms_desk_index(rooms_payload)
    zone_ids = tuple(rooms_payload["zones"].keys())

    # ---- 4. eligibility -------------------------------------------------
    eligibility_payload = (
        json.loads(json.dumps(eligibility))
        if eligibility is not None
        else make_eligibility(
            zone_ids, style=eligibility_style, precandidate_zones=precandidate_zones
        )
    )

    # ---- 5. roster rows (RNG: years) -------------------------------------
    roster_fields: tuple[str, ...] = ROSTER_BASE_FIELDS + (("advisor",) if extra_columns else ())
    if roster_rows is not None:
        supplied_fields = []
        for row in roster_rows:
            for key in row:
                if key not in supplied_fields:
                    supplied_fields.append(key)
        roster_fields = tuple(supplied_fields) or ROSTER_BASE_FIELDS
        people: list[dict[str, str]] = [
            {key: ("" if row.get(key) is None else str(row.get(key, ""))) for key in roster_fields}
            for row in roster_rows
        ]
        for row in people:
            row["email"] = row["email"].strip().lower()
    else:
        _require(max_year >= 1, f"max_year must be >= 1, got {max_year}")
        if n_precandidates is None:
            years = rng.integers(1, max_year + 1, size=n_people_eff).tolist()
        else:
            _require(
                0 <= n_precandidates <= n_people_eff,
                f"n_precandidates must be within [0, {n_people_eff}], got {n_precandidates}",
            )
            _require(
                precandidate_max_year < max_year or n_precandidates == n_people_eff,
                "precandidate_max_year must leave room for at least one non-precandidate year",
            )
            low = rng.integers(1, precandidate_max_year + 1, size=n_precandidates).tolist()
            high = rng.integers(
                precandidate_max_year + 1, max_year + 1, size=n_people_eff - n_precandidates
            ).tolist() if n_people_eff > n_precandidates else []
            years = low + high
        people = []
        seen_emails: dict[str, int] = {}
        for i in range(n_people_eff):
            name = _person_name(i)
            email = _email_for(name, seen_emails, i)
            seen_emails[email] = i
            year = int(years[i])
            row = {
                "name": name,
                "email": email,
                "year": str(year),
                "candidacy": "precandidate" if year <= precandidate_max_year else "candidate",
                "keeps_desk": _bool_token(False, i, vary_bool_tokens),
                "current_desk": "",
            }
            if extra_columns:
                # An extra column is legal and must survive the round trip; it
                # is also usable in an eligibility predicate (SPEC §2.3).
                row["advisor"] = _SURNAMES[(i * 7 + 3) % len(_SURNAMES)]
            people.append(row)

    seen: dict[str, int] = {}
    duplicates: list[str] = []
    for row in people:
        if row["email"] in seen:
            duplicates.append(row["email"])
        seen[row["email"]] = 1
    _require(
        not duplicates,
        f"roster emails must be unique; these repeat: {sorted(set(duplicates))}",
    )

    # ---- 6. eligibility as the ROSTER sees it ------------------------------
    # Only used to place keepers on a plausible desk. The eligibility that
    # governs the *submitted* choices is recomputed at step 10b, after the
    # roster/submission conflicts are known, because SPEC §3.3 says the
    # submission's candidacy wins and eligibility keys off candidacy.
    roster_zones: dict[PersonId, tuple[ZoneId, ...]] = {
        row["email"]: allowed_zones_for(row, eligibility_payload, zone_ids) for row in people
    }

    # ---- 7. keepers (RNG: which people, which desks) ----------------------
    taken_desks: set[DeskId] = set()
    keeper_desks: dict[PersonId, DeskId] = {}
    truthy = {token.casefold() for token in _TRUTHY_TOKENS}
    if roster_rows is not None:
        # A supplied roster already says who keeps what. Overriding it would
        # silently leave that person in the pool AND their desk in the pool —
        # the exact double-booking SPEC §3.4 exists to prevent.
        for row in people:
            if row.get("keeps_desk", "").strip().casefold() in truthy:
                desk_id = row.get("current_desk", "").strip()
                _require(
                    desk_id in zone_of_desk,
                    f"roster row {row['email']} keeps_desk is truthy but current_desk "
                    f"{desk_id!r} is not a desk in rooms.json",
                )
                _require(
                    desk_id not in taken_desks,
                    f"two people keep the same desk {desk_id!r}",
                )
                taken_desks.add(desk_id)
                keeper_desks[row["email"]] = desk_id
        keepers_wanted = len(keeper_desks)
    elif n_keepers is None:
        keepers_wanted = min(n_people_eff, int(keeper_frac * n_people_eff + 0.5))
    else:
        keepers_wanted = min(n_people_eff, max(0, int(n_keepers)))
    keeper_order = rng.permutation(n_people_eff).tolist() if n_people_eff else []
    desk_order = rng.permutation(len(all_desks)).tolist() if all_desks else []
    for person_index in keeper_order:
        if len(keeper_desks) >= keepers_wanted:
            break
        row = people[person_index]
        if row["email"] in keeper_desks:
            continue
        zones_ok = frozenset(roster_zones[row["email"]])
        for desk_index in desk_order:
            desk_id = all_desks[desk_index]
            if desk_id in taken_desks or zone_of_desk[desk_id] not in zones_ok:
                continue
            taken_desks.add(desk_id)
            keeper_desks[row["email"]] = desk_id
            row["keeps_desk"] = _bool_token(True, person_index, vary_bool_tokens)
            row["current_desk"] = desk_id
            break

    # ---- 8. administratively unavailable desks (RNG) ----------------------
    free_desks = tuple(d for d in all_desks if d not in taken_desks)
    n_unavailable = min(len(free_desks), int(unavailable_frac * len(all_desks) + 0.5))
    unavailable: tuple[DeskId, ...] = ()
    if n_unavailable:
        picks = rng.choice(len(free_desks), size=n_unavailable, replace=False).tolist()
        unavailable = tuple(sorted(free_desks[i] for i in picks))
        blocked = frozenset(unavailable)
        for room in rooms_payload["rooms"]:
            for desk in room["desks"]:
                if desk["id"] in blocked:
                    desk["available"] = False
    # Desks already marked unavailable in a supplied rooms payload count too.
    out_of_pool = frozenset(taken_desks) | frozenset(rooms_unavailable(rooms_payload))
    pool_desks = tuple(d for d in all_desks if d not in out_of_pool)

    # ---- 9. latent utilities (RNG) ----------------------------------------
    utilities = latent_utilities(rng, n_people_eff, len(all_desks), concentration, noise)
    orders = preference_orders(utilities, all_desks)
    full_orders: dict[PersonId, tuple[DeskId, ...]] = {
        row["email"]: orders[i] for i, row in enumerate(people)
    }

    # ---- 10. who responds, who resubmits, who conflicts, who goes stale ----
    # Keepers normally do not submit — they are not participating. `keepers_submit`
    # models the coordinator marking someone a keeper *after* they filled the form,
    # which is common and must still exclude them from the pool (SPEC §3.4).
    candidates = tuple(
        i for i, row in enumerate(people)
        if keepers_submit or row["email"] not in keeper_desks
    )
    n_responders = min(len(candidates), int(round(response_rate * len(candidates))))
    responder_pick = (
        sorted(rng.choice(len(candidates), size=n_responders, replace=False).tolist())
        if n_responders and n_responders < len(candidates)
        else list(range(n_responders))
    )
    responders = tuple(candidates[i] for i in responder_pick)

    def _subset(fraction: float) -> frozenset[int]:
        count = min(len(responders), int(fraction * len(responders) + 0.5))
        if count <= 0:
            return frozenset()
        picks = rng.choice(len(responders), size=count, replace=False).tolist()
        return frozenset(responders[i] for i in picks)

    resubmitters = _subset(resubmit_frac)
    conflicted = _subset(conflict_frac)
    stale_pick = _subset(stale_frac)

    # ---- 10b. eligibility as the SOLVER will see it ------------------------
    # SPEC §3.3: year and candidacy from the submission override the roster,
    # and eligibility predicates key off exactly those attributes. So a person
    # who reports a different candidacy than the roster holds is eligible for
    # different zones than the roster implies. Picking their choices from the
    # roster's zone set would hand them a ranking the solver then filters to
    # nothing — a self-inconsistent fixture rather than an interesting one.
    def _conflicted_view(row: Mapping[str, str]) -> dict[str, str]:
        view = dict(row)
        view["year"] = str(int(row["year"]) + 1)
        view["candidacy"] = (
            "candidate" if row["candidacy"] == "precandidate" else "precandidate"
        )
        return view

    effective: dict[PersonId, dict[str, str]] = {}
    for person_index, row in enumerate(people):
        effective[row["email"]] = (
            _conflicted_view(row) if person_index in conflicted else dict(row)
        )
    allowed_zones: dict[PersonId, tuple[ZoneId, ...]] = {
        email: allowed_zones_for(view, eligibility_payload, zone_ids)
        for email, view in effective.items()
    }

    # ---- 11. build the submissions ----------------------------------------
    pool_set = frozenset(pool_desks)
    stale_candidates = tuple(sorted(out_of_pool)) if out_of_pool else ()
    response_fields: tuple[str, ...] = (
        ("submission_id", "timestamp", "email", "name", "year", "candidacy")
        + tuple(f"choice_{i + 1}" for i in range(k_eff))
        + ("client_version", "auth_method")
    )

    rows: list[dict[str, str]] = []
    padded: list[PersonId] = []
    clock = 0            # units of _SUBMIT_STEP; monotone, never the wall clock
    seen_timestamp_twin = False

    for person_index in responders:
        row = people[person_index]
        email = row["email"]
        zones_ok = frozenset(allowed_zones[email])
        eligible = tuple(
            d for d in full_orders[email] if d in pool_set and zone_of_desk[d] in zones_ok
        )
        chosen = _pick_choices(
            person_index, row, eligible, full_orders[email], pool_set, k_eff, choices_fn
        )
        if len(eligible) < k_eff and choices_fn is None:
            padded.append(email)

        n_rows = 1 + (extra_submissions if person_index in resubmitters else 0)
        for attempt in range(n_rows):
            clock += 1
            # One deliberately duplicated timestamp: SPEC §3.2 says ties are
            # broken by file position, and that branch is otherwise untested.
            if attempt == 1 and not seen_timestamp_twin:
                clock -= 1
                seen_timestamp_twin = True
            variant = chosen
            if attempt:
                # A resubmission is a real change of mind: rotate the tail so
                # the winning row is distinguishable from the superseded ones.
                variant = _rotate_tail(chosen, attempt)
            # SPEC §3.3: the submission wins on year/candidacy and the conflict
            # is reported. `effective` already holds the flipped values, and the
            # choices above were drawn against them, so the row is consistent.
            view = effective[email]
            year_text, candidacy_text = view["year"], view["candidacy"]
            if person_index in stale_pick and stale_candidates and k_eff >= 2:
                replaced = variant[: k_eff - 1] + (
                    stale_candidates[person_index % len(stale_candidates)],
                )
                # Never emit a duplicated choice, and never make the stale
                # injection the thing that empties someone's ranking — that is
                # a different failure and `starve_one` is how you ask for it.
                still_valid = any(
                    d in pool_set and zone_of_desk.get(d) in zones_ok
                    for d in replaced[: k_eff - 1]
                )
                if len(set(replaced)) == k_eff and still_valid:
                    variant = replaced
            entry = {
                "submission_id": _uuid_for(seed_text, f"{email}#{attempt}"),
                "timestamp": (_BASE_SUBMIT + clock * _SUBMIT_STEP).isoformat(),
                "email": email,
                "name": row["name"],
                "year": year_text,
                "candidacy": candidacy_text,
                "client_version": _CLIENT_VERSION,
                "auth_method": _AUTH_METHODS[person_index % len(_AUTH_METHODS)],
            }
            for rank, desk_id in enumerate(variant):
                entry[f"choice_{rank + 1}"] = desk_id
            rows.append(entry)

    if shuffle_rows and rows:
        rows = [rows[i] for i in rng.permutation(len(rows)).tolist()]

    # Self-check before anything is written: K distinct non-empty choices.
    for position, entry in enumerate(rows):
        picks = [entry[f"choice_{i + 1}"] for i in range(k_eff)]
        _require(all(picks), f"row {position} ({entry['email']}) has an empty choice")
        _require(
            len(set(picks)) == k_eff,
            f"row {position} ({entry['email']}) repeats a desk: {picks}",
        )

    expected_latest = _resolve_latest(rows)
    # SPEC §3.4: keeps_desk falsy AND a valid submission. A keeper who happened
    # to submit is still out of the pool.
    pool_people = tuple(email for email in sorted(expected_latest) if email not in keeper_desks)

    # Self-check: nobody in the pool may reach zero allowed cells. That is an
    # error per SPEC §3.4, so producing one by accident would mean the fixture
    # fails validation for a reason the caller never asked for. Scenarios that
    # want it say so.
    if not allow_zero_valid_choices:
        by_id = {entry["submission_id"]: entry for entry in rows}
        starved: list[str] = []
        for email in pool_people:
            entry = by_id[expected_latest[email]]
            zones_ok = frozenset(allowed_zones[email])
            if not any(
                entry[f"choice_{i + 1}"] in pool_set
                and zone_of_desk.get(entry[f"choice_{i + 1}"]) in zones_ok
                for i in range(k_eff)
            ):
                starved.append(email)
        _require(
            not starved,
            f"{len(starved)} people would have zero valid choices after filtering "
            f"({', '.join(starved[:5])}{'...' if len(starved) > 5 else ''}). Either widen "
            f"the desk pool / eligible zones, or pass allow_zero_valid_choices=True if "
            f"that is the case you meant to build.",
        )

    return SynthWorld(
        rooms=rooms_payload,
        eligibility=eligibility_payload,
        scoring=scoring_payload,
        roster_fields=roster_fields,
        roster_rows=tuple(dict(row) for row in people),
        response_fields=response_fields,
        response_rows=tuple(rows),
        k=k_eff,
        seed_string=seed_text,
        concentration=float(concentration),
        noise=noise,
        desk_ids=all_desks,
        zone_of_desk=zone_of_desk,
        allowed_zones=allowed_zones,
        full_orders=full_orders,
        pool_desks=pool_desks,
        pool_people=pool_people,
        keeper_desks=keeper_desks,
        expected_latest=expected_latest,
        padded_people=tuple(sorted(set(padded))),
        notes=tuple(notes),
    )


def _pick_choices(
    person_index: int,
    row: Mapping[str, str],
    eligible: tuple[DeskId, ...],
    full_order: tuple[DeskId, ...],
    pool_set: frozenset[DeskId],
    k: int,
    choices_fn: ChoicesFn | None,
) -> tuple[DeskId, ...]:
    """The person's top-K, padded if their eligible pool is smaller than K.

    The schema demands exactly K distinct desk ids (SPEC §3.1) even when the
    person cannot legally reach K desks — a real form would not offer them, but
    a real CSV can still contain them and the loader must cope. Padding order:
    eligible pool desks, then other pool desks (which become disallowed cells),
    then anything at all (which becomes a dropped choice with a warning).
    """
    if choices_fn is not None:
        forced = choices_fn(person_index, row, eligible, full_order)
        if forced is not None:
            picks = tuple(forced)
            _require(
                len(picks) == k and len(set(picks)) == k,
                f"choices_fn returned {len(picks)} choices ({len(set(picks))} distinct) "
                f"for {row['email']}; exactly {k} distinct are required",
            )
            return picks
    chosen: list[DeskId] = list(eligible[:k])
    if len(chosen) < k:
        taken = set(chosen)
        for tier in (
            tuple(d for d in full_order if d in pool_set),
            full_order,
        ):
            for desk_id in tier:
                if len(chosen) >= k:
                    break
                if desk_id not in taken:
                    chosen.append(desk_id)
                    taken.add(desk_id)
    _require(
        len(chosen) == k,
        f"cannot build {k} distinct choices for {row['email']}: only "
        f"{len(chosen)} desk ids exist in total",
    )
    return tuple(chosen)


def _rotate_tail(choices: tuple[DeskId, ...], amount: int) -> tuple[DeskId, ...]:
    """Deterministically reorder a choice list; keeps it K distinct."""
    if len(choices) < 2:
        return choices
    shift = 1 + (amount - 1) % (len(choices) - 1)
    return choices[shift:] + choices[:shift]


def _resolve_latest(rows: Sequence[Mapping[str, str]]) -> dict[PersonId, str]:
    """Latest submission per email: max by (timestamp, file position) — SPEC §3.2.

    Duplicated here so tests can assert the real loader agrees; the tie-break on
    file position is exactly the part that a loader is likely to get wrong.
    """
    best: dict[PersonId, tuple[datetime, int, str]] = {}
    for position, row in enumerate(rows):
        stamp = datetime.fromisoformat(row["timestamp"])
        key = (stamp, position, row["submission_id"])
        current = best.get(row["email"])
        if current is None or key[:2] > current[:2]:
            best[row["email"]] = key
    return {email: best[email][2] for email in sorted(best)}


# --------------------------------------------------------------------------
# Sanity-checking the concentration knob
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TauRow:
    """One row of the concentration sanity check."""

    concentration: float
    mean_tau: float          # mean pairwise Kendall tau-b over full rankings
    sd_tau: float
    predicted_tau: float | None   # (2/pi)*arcsin(c); Gaussian noise only
    mean_topk_overlap: float      # mean |A ∩ B| / K over submitted top-K lists
    n_pairs: int


def kendall_tau_table(
    *,
    concentrations: Sequence[float] = (0.0, 0.25, 0.5, 0.75, 1.0),
    n_people: int = 40,
    n_desks: int = 30,
    k: int = 5,
    seed: int | str = "tau-check",
    noise: str = "thurstone",
    max_pairs: int = 2000,
    replicates: int = 1,
) -> tuple[TauRow, ...]:
    """Measure how much preference profiles overlap as `concentration` varies.

    Two independent measurements, because they answer different questions:

    * `mean_tau` — mean pairwise Kendall tau-b between the full latent
      rankings. For `noise="thurstone"` theory says this should land on
      (2/pi)*arcsin(c), reported alongside as `predicted_tau`.
    * `mean_topk_overlap` — mean |A ∩ B| / K between the submitted top-K lists.
      This is the operationally meaningful one: collisions in the top-K are
      what make a run infeasible.

    Both must increase monotonically in `concentration` for the knob to mean
    anything. Rankings are taken over the full desk set (no eligibility
    filtering) so every pair is compared on a common ground set.

    `replicates > 1` averages over that many independent draws of the *shared*
    desirability vector theta. This matters for comparing against the closed
    form: within one world theta is a single draw, so the pairwise mean is
    conditioned on it and sits a few hundredths off the marginal expectation.
    Averaging over thetas removes that, and the empirical column converges on
    (2/pi)*arcsin(c).
    """
    from scipy.stats import kendalltau  # local: keeps `emit` from paying for scipy

    _require(n_people >= 2, "need at least two people to have a pairwise correlation")
    _require(1 <= k <= n_desks, f"k must be in [1, n_desks={n_desks}], got {k}")
    desk_ids = tuple(f"D{i + 1:04d}" for i in range(n_desks))

    # A fixed pair sample, drawn once, so every concentration is measured on
    # exactly the same pairs — otherwise the comparison is confounded.
    pair_rng = _rng(f"{seed}/pairs")
    all_pairs = [(i, j) for i in range(n_people) for j in range(i + 1, n_people)]
    if len(all_pairs) > max_pairs:
        picks = sorted(pair_rng.choice(len(all_pairs), size=max_pairs, replace=False).tolist())
        pairs = [all_pairs[i] for i in picks]
    else:
        pairs = all_pairs

    _require(replicates >= 1, f"replicates must be >= 1, got {replicates}")
    rows: list[TauRow] = []
    for c in concentrations:
        taus: list[float] = []
        overlaps: list[float] = []
        for replicate in range(replicates):
            # A per-(concentration, replicate) seed keeps draws independent
            # across the table while staying reproducible.
            rng = _rng(f"{seed}/c={c!r}/r={replicate}")
            utilities = latent_utilities(rng, n_people, n_desks, float(c), noise)
            ranks = np.argsort(np.argsort(-utilities, axis=1, kind="stable"), axis=1)
            orders = preference_orders(utilities, desk_ids)
            topk = [frozenset(order[:k]) for order in orders]
            for i, j in pairs:
                # tau is nan only if a vector is constant, impossible here.
                taus.append(float(kendalltau(ranks[i], ranks[j]).statistic))
                overlaps.append(len(topk[i] & topk[j]) / k)
        tau_arr = np.asarray(taus, dtype=np.float64)
        rows.append(
            TauRow(
                concentration=float(c),
                mean_tau=float(tau_arr.mean()),
                sd_tau=float(tau_arr.std(ddof=1)) if len(tau_arr) > 1 else 0.0,
                predicted_tau=(
                    (2.0 / math.pi) * math.asin(min(1.0, max(0.0, float(c))))
                    if noise == "thurstone" else None
                ),
                mean_topk_overlap=float(np.mean(overlaps)),
                n_pairs=len(pairs) * replicates,
            )
        )
    return tuple(rows)


def render_tau_table(rows: Sequence[TauRow]) -> str:
    header = (
        f"{'concentration':>13} | {'mean Kendall tau':>16} | {'sd':>6} | "
        f"{'(2/pi)arcsin(c)':>15} | {'mean top-K overlap':>18} | {'pairs':>6}"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        predicted = f"{'n/a':>15}" if row.predicted_tau is None else f"{row.predicted_tau:15.4f}"
        lines.append(
            f"{row.concentration:13.2f} | {row.mean_tau:16.4f} | {row.sd_tau:6.3f} | "
            f"{predicted} | {row.mean_topk_overlap:18.4f} | {row.n_pairs:6d}"
        )
    monotone_tau = all(b.mean_tau > a.mean_tau for a, b in zip(rows, rows[1:]))
    monotone_top = all(
        b.mean_topk_overlap > a.mean_topk_overlap for a, b in zip(rows, rows[1:])
    )
    lines.append("")
    lines.append(f"strictly increasing in tau:            {monotone_tau}")
    lines.append(f"strictly increasing in top-K overlap:  {monotone_top}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# A reference Problem, for tests that must run before problem.py exists
# --------------------------------------------------------------------------


def curve_to_integers(values: Sequence[Any]) -> tuple[tuple[int, ...], int]:
    """Exact rationalisation of a curve to integers (SPEC §5.3).

    `Fraction(str(v))` rather than `Fraction(v)`: the former reads the decimal
    literal the coordinator typed, the latter reads the binary float and
    produces a denominator of 2**52.
    """
    fractions = tuple(Fraction(str(v)) for v in values)
    scale = math.lcm(*(f.denominator for f in fractions)) if fractions else 1
    scaled = tuple((f * scale) for f in fractions)
    for original, s in zip(fractions, scaled):
        if s.denominator != 1:
            raise ValueError(f"curve value {original} is not exactly integral at scale {scale}")
    return tuple(int(s) for s in scaled), scale


def reference_problem(
    world: SynthWorld, *, curve_name: str | None = None
) -> tuple[tuple[PersonId, ...], tuple[DeskId, ...], np.ndarray, np.ndarray]:
    """(people, desks, allowed, points) built straight from a SynthWorld.

    A deliberately small reference implementation of SPEC §5.1 so a scenario
    can prove its own claim (how many optima tie_heavy really has) without
    depending on `problem.py`. Tests that have `problem.py` available should
    use that instead — this exists to check it, not to replace it.
    """
    name = curve_name or world.scoring["primary_curve"]
    points_curve, _scale = curve_to_integers(world.scoring["curves"][name])
    people = world.pool_people
    desks = world.pool_desks
    index_of_desk = {desk_id: j for j, desk_id in enumerate(desks)}
    allowed = np.zeros((len(people), len(desks)), dtype=bool)
    points = np.zeros((len(people), len(desks)), dtype=np.int64)
    latest = world.latest_choices()
    for i, email in enumerate(people):
        zones_ok = frozenset(world.allowed_zones[email])
        for rank, desk_id in enumerate(latest[email]):
            j = index_of_desk.get(desk_id)
            if j is None:                                   # dropped: out of pool
                continue
            if world.zone_of_desk[desk_id] not in zones_ok:  # dropped: wrong zone
                continue
            allowed[i, j] = True
            points[i, j] = points_curve[rank]
    return people, desks, allowed, points


def count_optimal_assignments(
    points: np.ndarray, allowed: np.ndarray, *, max_people: int = 10
) -> tuple[int | None, int]:
    """Brute-force (best_total, number_of_assignments_achieving_it).

    Exhaustive depth-first search over injective person→desk maps. Exponential
    by construction — it exists to certify small cases, so it refuses to run on
    large ones rather than appearing to hang.
    """
    n_people, n_desks = points.shape
    _require(
        n_people <= max_people,
        f"brute force refuses {n_people} people (limit {max_people}); it is O(n!)",
    )
    if n_people == 0:
        return 0, 1
    options = [np.flatnonzero(allowed[i]).tolist() for i in range(n_people)]
    best: int | None = None
    count = 0
    used = [False] * n_desks

    def walk(row: int, total: int) -> None:
        nonlocal best, count
        if row == n_people:
            if best is None or total > best:
                best, count = total, 1
            elif total == best:
                count += 1
            return
        for column in options[row]:
            if used[column]:
                continue
            used[column] = True
            walk(row + 1, total + int(points[row, column]))
            used[column] = False

    walk(0, 0)
    return best, count


# --------------------------------------------------------------------------
# Adversarial scenario builders
# --------------------------------------------------------------------------
#
# Each returns a SynthCase. Pass `out_dir` to get real files on disk
# (`case.as_paths()` gives you (config_dir, responses_path) for the CLI);
# omit it for the in-memory world only.


def _case(
    name: str,
    description: str,
    expectation: str,
    world: SynthWorld,
    out_dir: str | Path | None,
    *,
    write_images: bool = True,
) -> SynthCase:
    config_dir = responses_path = None
    if out_dir is not None:
        config_dir, responses_path = world.write(out_dir, write_images=write_images)
    return SynthCase(
        name=name,
        description=description,
        expectation=expectation,
        world=world,
        config_dir=config_dir,
        responses_path=responses_path,
    )


def everyone_ranks_same_k_desks(
    out_dir: str | Path | None = None,
    *,
    n_people: int = 9,
    k: int = 5,
    n_desks: int | None = None,
    seed: int | str = "same-k",
) -> SynthCase:
    """Every single person submits the identical K desks in the identical order.

    Infeasible whenever `n_people > k`: those people between them reach exactly
    K desks, so Hall's condition fails by `n_people - k` and the whole roster is
    one blocking set. The desk pool is deliberately larger than K, so the
    failure is a preference collision, not a shortage of furniture — a solver
    that reported "not enough desks" here would be wrong.
    """
    desks_total = n_desks if n_desks is not None else max(k + 3, n_people + 2)
    _require(desks_total >= k, f"need at least k={k} desks, got {desks_total}")
    target = tuple(f"D{i + 1:0{max(2, len(str(desks_total)))}d}" for i in range(k))

    def forced(index, row, eligible, full_order):
        return target

    world = generate(
        n_people,
        n_desks=desks_total,
        k=k,
        seed=seed,
        concentration=1.0,
        n_zones=1,
        eligibility_style="flat",
        keeper_frac=0.0,
        choices_fn=forced,
        notes=(f"all {n_people} people rank exactly {target}",),
    )
    return _case(
        "everyone_ranks_same_k_desks",
        f"{n_people} people, {desks_total} desks, all ranking the same {k}.",
        (
            f"INFEASIBLE (exit 2) when n_people > K: max matching = {min(n_people, k)}, "
            f"deficiency = {max(0, n_people - k)}; one blocking set covering everyone."
            if n_people > k else
            f"Feasible: n_people ({n_people}) <= K ({k}), and every assignment ties."
        ),
        world,
        out_dir,
    )


def more_people_than_desks(
    out_dir: str | Path | None = None,
    *,
    n_people: int = 14,
    n_desks: int = 8,
    k: int = 5,
    seed: int | str = "overflow",
) -> SynthCase:
    """Strictly more people than desks. Infeasible by counting alone.

    Invariant I8 says n_people != n_desks is normal *in either direction*, so
    this must fail cleanly with diagnostics rather than crash or truncate.
    """
    _require(n_desks >= k, f"n_desks ({n_desks}) must be at least K ({k})")
    world = generate(
        n_people,
        n_desks=n_desks,
        k=k,
        seed=seed,
        concentration=0.3,
        n_zones=1,
        eligibility_style="flat",
        keeper_frac=0.0,
        notes=(f"{n_people} people chasing {n_desks} desks",),
    )
    return _case(
        "more_people_than_desks",
        f"{n_people} people, only {n_desks} desks.",
        f"INFEASIBLE (exit 2): deficiency >= {n_people - n_desks} on pigeonhole alone.",
        world,
        out_dir,
    )


def cohort_zone_starved(
    out_dir: str | Path | None = None,
    *,
    n_people: int = 20,
    n_precandidates: int = 12,
    precandidate_desks: int = 6,
    k: int = 5,
    seed: int | str = "starved",
) -> SynthCase:
    """A zone holding fewer desks than the cohort confined to it.

    Everything else is comfortable: there are plenty of desks overall, and the
    candidates have room to spare. Only the eligibility rule makes it fail, so
    the diagnostic has to name the cohort and the zone rather than blaming the
    global desk count.
    """
    _require(
        precandidate_desks < n_precandidates,
        "the point of this scenario is precandidate_desks < n_precandidates",
    )
    _require(0 < n_precandidates <= n_people, "n_precandidates must be within the roster")
    other_desks = max(k, n_people - n_precandidates + 4)
    world = generate(
        n_people,
        n_desks=precandidate_desks + other_desks,
        k=k,
        seed=seed,
        concentration=0.3,
        n_zones=2,
        zone_sizes=(precandidate_desks, other_desks),
        eligibility_style="cohort",
        n_precandidates=n_precandidates,
        keeper_frac=0.0,
        notes=(
            f"zone_alpha holds {precandidate_desks} desks for {n_precandidates} precandidates",
        ),
    )
    return _case(
        "cohort_zone_starved",
        f"{n_precandidates} precandidates confined to a zone with {precandidate_desks} desks.",
        (
            f"INFEASIBLE (exit 2): deficiency {n_precandidates - precandidate_desks}. "
            f"The blocking set must be precandidates only, and N(S) must be zone_alpha."
        ),
        world,
        out_dir,
    )


def empty_roster(
    out_dir: str | Path | None = None, *, n_desks: int = 8, k: int = 5,
    seed: int | str = "empty",
) -> SynthCase:
    """Zero people. Header-only roster.csv and responses.csv.

    The degenerate case that eats naive code: empty matrices, empty argmax,
    division by n_people.

    `config.py` currently treats an empty roster as an export mistake and
    rejects it, which is a defensible call — an empty roster is far more often
    a broken export than a real department. What this fixture pins down is that
    the rejection is a *clean* ConfigError naming roster.csv, never a traceback
    out of numpy. If that policy is ever relaxed, this same fixture must then
    produce exit 0 with zero assignments and still not divide by n_people.
    """
    _require(n_desks >= k, f"n_desks ({n_desks}) must be at least K ({k})")
    world = generate(
        0, n_desks=n_desks, k=k, seed=seed, n_zones=1, eligibility_style="flat",
        notes=("header-only roster and responses",),
    )
    return _case(
        "empty_roster",
        f"No people at all; {n_desks} desks sit empty.",
        "ConfigError (exit 4) naming roster.csv and saying it has a header but no people. "
        "Any ZeroDivisionError, empty-argmax or raw StopIteration here is a bug.",
        world,
        out_dir,
    )


def single_person(
    out_dir: str | Path | None = None, *, k: int = 5, n_desks: int | None = None,
    seed: int | str = "single",
) -> SynthCase:
    """Exactly one person. n=1 breaks anything that assumes a population."""
    desks_total = n_desks if n_desks is not None else k + 1
    _require(desks_total >= k, f"n_desks ({desks_total}) must be at least K ({k})")
    world = generate(
        1, n_desks=desks_total, k=k, seed=seed, n_zones=1, eligibility_style="flat",
        keeper_frac=0.0, notes=("population of one",),
    )
    return _case(
        "single_person",
        f"One person, {desks_total} desks.",
        "SUCCESS (exit 0): that person gets their first choice; the rank histogram "
        "is (1, 0, ..., 0). The jitter bound epsilon = 1/(2*(1+1)) must still hold.",
        world,
        out_dir,
    )


def duplicate_submissions(
    out_dir: str | Path | None = None,
    *,
    n_people: int = 10,
    k: int = 5,
    extra: int = 3,
    seed: int | str = "duplicates",
) -> SynthCase:
    """Everyone submits several times, and one pair shares a timestamp exactly.

    SPEC §3.2: latest row per email wins, ordered by timestamp, ties broken by
    later file position. Rows are shuffled so file order is not timestamp order
    — a loader that just takes the last row it sees will get this wrong.
    `world.expected_latest` is the answer key.
    """
    world = generate(
        n_people,
        k=k,
        seed=seed,
        concentration=0.3,
        n_zones=1,
        eligibility_style="flat",
        keeper_frac=0.0,
        resubmit_frac=1.0,
        extra_submissions=extra,
        shuffle_rows=True,
        vary_bool_tokens=True,
        notes=(f"{extra} resubmissions each; one timestamp tie broken by file order",),
    )
    return _case(
        "duplicate_submissions",
        f"{n_people} people, {1 + extra} submissions each, rows out of chronological order.",
        f"SUCCESS (exit 0) with {n_people} people in the pool and "
        f"{len(world.response_rows) - n_people} rows retained as superseded. The resolved "
        f"latest submission per email must equal world.expected_latest.",
        world,
        out_dir,
    )


def stale_desk_reference(
    out_dir: str | Path | None = None,
    *,
    n_people: int = 12,
    k: int = 5,
    n_desks: int | None = None,
    seed: int | str = "stale",
    starve_one: bool = False,
) -> SynthCase:
    """Submissions naming desks that are no longer in the pool.

    Three flavours at once, because they take different code paths: a desk held
    by a keeper, a desk marked `"available": false`, and a desk id that appears
    nowhere in rooms.json at all. Each is a warning and a dropped choice
    (SPEC §3.4), never a crash.

    `starve_one=True` additionally drops one person's *entire* ranking into
    nonexistent ids, which per §3.4 is an error naming that person.

    The desk pool is deliberately kept comfortably larger than the people pool
    even after the keepers and the unavailable desks are subtracted, so that
    the run stays feasible. Otherwise this fixture would fail on pigeonhole
    arithmetic and stop testing the thing it is named after.
    """
    ghost_ids = tuple(f"D9{i:02d}" for i in range(1, k + 1))   # in no rooms.json

    def forced(index, row, eligible, full_order):
        if starve_one and index == 0:
            return ghost_ids
        if index % 4 == 1 and len(eligible) >= k:
            return eligible[: k - 1] + (ghost_ids[0],)
        return None

    keeper_frac, unavailable_frac = 0.25, 0.15
    desks_total = n_desks if n_desks is not None else max(
        k, math.ceil((n_people + 4) / (1.0 - unavailable_frac)) + 1
    )
    world = generate(
        n_people,
        n_desks=desks_total,
        k=k,
        seed=seed,
        concentration=0.3,
        keeper_frac=keeper_frac,
        unavailable_frac=unavailable_frac,
        stale_frac=0.4,
        choices_fn=forced,
        allow_zero_valid_choices=starve_one,
        notes=("stale refs: keeper-held, available:false, and nonexistent ids",),
    )
    if not starve_one:
        _require(
            len(world.pool_desks) > len(world.pool_people),
            f"stale_desk_reference must stay feasible on counting: "
            f"{len(world.pool_people)} people vs {len(world.pool_desks)} pool desks",
        )
    return _case(
        "stale_desk_reference",
        f"{n_people} people; some rank keeper-held, unavailable, or nonexistent desks.",
        (
            "ERROR (exit 4) naming the person left with zero valid choices."
            if starve_one else
            "SUCCESS (exit 0) with one warning per dropped choice, each naming the person "
            "and the desk. No KeyError on the unknown desk ids."
        ),
        world,
        out_dir,
    )


def all_keepers(
    out_dir: str | Path | None = None, *, n_people: int = 8, k: int = 5,
    seed: int | str = "keepers", keepers_submit: bool = True,
) -> SynthCase:
    """Everybody keeps their current desk. Nobody is left in the pool.

    Both sides of the problem go to zero at once: pool_people is empty *and*
    every desk they hold leaves pool_desks (SPEC §3.4).

    By default the keepers *did* submit before the coordinator marked them as
    keeping — which is what actually happens, and is the harder test: both
    files are full, every row parses, and the pool is still empty. The pool
    must be emptied by `keeps_desk`, not by the response file being short.
    Pass `keepers_submit=False` for the header-only variant.
    """
    world = generate(
        n_people,
        n_desks=max(k, n_people + 2),
        k=k,
        seed=seed,
        n_zones=1,
        eligibility_style="flat",
        n_keepers=n_people,
        keepers_submit=keepers_submit,
        vary_bool_tokens=True,
        notes=("every roster member has keeps_desk truthy",),
    )
    _require(
        len(world.keeper_desks) == n_people,
        "all_keepers failed to place every keeper on a distinct desk",
    )
    _require(not world.pool_people, "all_keepers left somebody in the pool")
    return _case(
        "all_keepers",
        f"All {n_people} roster members keep their desks"
        + (f"; all {n_people} submitted anyway." if keepers_submit
           else "; the response file is header-only."),
        "SUCCESS (exit 0) with zero assignments and zero pool desks. Must not warn that "
        "keepers 'did not participate' — they are not supposed to. Nobody may be "
        "assigned a desk another keeper holds.",
        world,
        out_dir,
    )


def exact_fit(
    out_dir: str | Path | None = None, *, n_people: int = 8, k: int = 5,
    seed: int | str = "exact",
) -> SynthCase:
    """n_people == n_desks with a provably unique optimum.

    Person i's first choice is desk i, and those first choices are all
    distinct. The assignment giving everyone rank 1 therefore exists, scores
    n * curve[0], and is the *only* assignment that scores it — every other
    injective map hands someone a rank > 1, and the curve is strictly
    decreasing. So the seed must NOT change the answer here, which is the exact
    complement of `tie_heavy`.
    """
    _require(n_people >= k, f"n_people ({n_people}) must be at least K ({k}) for distinct choices")
    width = max(2, len(str(n_people)))
    desk_ids = tuple(f"D{i + 1:0{width}d}" for i in range(n_people))

    def forced(index, row, eligible, full_order):
        # Rotate so choice_1 is desk `index` and the rest are a fixed rotation:
        # unique optimum, but non-trivial competition below rank 1.
        return tuple(desk_ids[(index + offset) % n_people] for offset in range(k))

    world = generate(
        n_people,
        n_desks=n_people,
        k=k,
        seed=seed,
        n_zones=1,
        eligibility_style="flat",
        keeper_frac=0.0,
        choices_fn=forced,
        notes=("person i ranks desk i first; unique optimum",),
    )
    return _case(
        "exact_fit",
        f"{n_people} people, {n_people} desks, one optimal assignment.",
        f"SUCCESS (exit 0), total = {n_people} * curve[0], rank histogram "
        f"({n_people}, 0, ...). Identical output under every seed.",
        world,
        out_dir,
    )


def tie_heavy(
    out_dir: str | Path | None = None,
    *,
    n_blocks: int = 2,
    k: int = 3,
    seed: int | str = "ties",
    spare_desks: int = 2,
) -> SynthCase:
    """Built so that a large number of assignments are exactly tied for optimal.

    Construction: `n_blocks` disjoint blocks of exactly K people and K desks.
    Everyone inside a block submits the same K desks in the same order, and no
    desk is shared between blocks.

    Why every block bijection ties: within a block the K people must be matched
    to the K block desks (nothing else is in their top-K), so any feasible
    assignment gives out each rank 1..K exactly once and scores sum(curve). The
    total is therefore `n_blocks * sum(curve)` for *every* feasible assignment,
    and the number of them is exactly

        (K!) ** n_blocks

    which is 36 at the default K=3, n_blocks=2. `count_optimal_assignments()`
    on `reference_problem()` confirms the count by exhaustive search rather
    than by assertion; `verify_tie_heavy()` does exactly that.

    Because everything ties, the seeded tie-break is the *only* thing choosing
    the output — which is what makes this the right fixture for testing that
    the seed actually varies the result (SPEC §5.4).
    """
    _require(n_blocks >= 1, f"n_blocks must be >= 1, got {n_blocks}")
    n_people = n_blocks * k
    total_desks = n_people + max(0, spare_desks)
    width = max(2, len(str(total_desks)))
    desk_ids = tuple(f"D{i + 1:0{width}d}" for i in range(total_desks))

    def forced(index, row, eligible, full_order):
        block = index // k
        return tuple(desk_ids[block * k + offset] for offset in range(k))

    world = generate(
        n_people,
        n_desks=total_desks,
        k=k,
        seed=seed,
        n_zones=1,
        eligibility_style="flat",
        keeper_frac=0.0,
        choices_fn=forced,
        notes=(f"{n_blocks} blocks of {k}; exactly {math.factorial(k) ** n_blocks} optima",),
    )
    return _case(
        "tie_heavy",
        f"{n_blocks} independent blocks of {k} people over {k} desks; "
        f"{math.factorial(k) ** n_blocks} tied optima.",
        f"SUCCESS (exit 0), total = {n_blocks} * sum(curve), always. Different "
        f"tie_break_seed values must produce different assignments with identical totals.",
        world,
        out_dir,
    )


def verify_tie_heavy(case: SynthCase) -> tuple[int, int, int]:
    """Brute-force the tie claim: (best_total, n_optima, n_feasible_assignments).

    Exhaustive, so keep the case small. Returns the counts rather than
    asserting, so a caller can print them.
    """
    _people, _desks, allowed, points = reference_problem(case.world)
    best, count = count_optimal_assignments(points, allowed)
    # Every feasible assignment is optimal here, so counting them again with a
    # flat objective is the check that "many optima" is not "one optimum and a
    # lot of infeasible noise".
    _flat_best, feasible = count_optimal_assignments(np.zeros_like(points), allowed)
    return (0 if best is None else best), count, feasible


SCENARIOS: Mapping[str, Callable[..., SynthCase]] = {
    "everyone_ranks_same_k_desks": everyone_ranks_same_k_desks,
    "more_people_than_desks": more_people_than_desks,
    "cohort_zone_starved": cohort_zone_starved,
    "empty_roster": empty_roster,
    "single_person": single_person,
    "duplicate_submissions": duplicate_submissions,
    "stale_desk_reference": stale_desk_reference,
    "all_keepers": all_keepers,
    "exact_fit": exact_fit,
    "tie_heavy": tie_heavy,
}


def scenario_names() -> tuple[str, ...]:
    """Sorted, because this reaches `--help` output and therefore the docs."""
    return tuple(sorted(SCENARIOS))


# --------------------------------------------------------------------------
# CLI:  python -m deskmatch.synth
# --------------------------------------------------------------------------


def _add_shape_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out", required=True, help="directory to write config/ and responses.csv into")
    parser.add_argument("-n", "--n-people", type=int, default=24, help="roster size (default: %(default)s)")
    parser.add_argument("--n-desks", type=int, default=None,
                        help="total desks (default: max(K*zones, n_people + n_people//8))")
    parser.add_argument("--rooms", type=int, default=None, dest="n_rooms",
                        help="number of rooms (default: one per 40 desks)")
    parser.add_argument("--zones", type=int, default=2, dest="n_zones",
                        help="number of zones (default: %(default)s)")
    parser.add_argument("-k", type=int, default=5, dest="k",
                        help="K, the number of ranked choices (default: %(default)s)")
    parser.add_argument("--seed", default="synth", help="seed string or int (default: %(default)s)")
    parser.add_argument("--concentration", type=float, default=0.35,
                        help="preference overlap in [0,1] (default: %(default)s)")
    parser.add_argument("--noise", choices=NOISE_FAMILIES, default="thurstone",
                        help="idiosyncratic noise family (default: %(default)s)")
    parser.add_argument("--coord-space", choices=("normalized", "pixels"), default="normalized")
    parser.add_argument("--eligibility", choices=("cohort", "flat"), default="cohort",
                        dest="eligibility_style")
    parser.add_argument("--keeper-frac", type=float, default=0.1)
    parser.add_argument("--response-rate", type=float, default=1.0)
    parser.add_argument("--resubmit-frac", type=float, default=0.0)
    parser.add_argument("--conflict-frac", type=float, default=0.0)
    parser.add_argument("--stale-frac", type=float, default=0.0)
    parser.add_argument("--unavailable-frac", type=float, default=0.0)
    parser.add_argument("--no-extra-columns", action="store_false", dest="extra_columns")
    parser.add_argument("--no-images", action="store_false", dest="write_images",
                        help="skip the floor-plan PNGs (the validator will then warn)")
    parser.add_argument("--messy", action="store_true",
                        help="turn on realistic mess: non-responders, resubmissions, "
                             "roster conflicts, stale desk references, unavailable desks")


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error; SPEC §9 reserves 2 for 'infeasible'.

    A synth CLI can never be infeasible, so leaving the default would emit an
    exit code that means something entirely different to whatever is reading it.
    """

    def error(self, message: str) -> Any:      # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(1)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m deskmatch.synth",
        description="Generate synthetic deskmatch config and responses.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m deskmatch.synth emit --out examples/dryrun -n 24 --seed demo\n"
            "  python -m deskmatch.synth emit --out /tmp/messy -n 40 --messy\n"
            "  python -m deskmatch.synth scenario cohort_zone_starved --out /tmp/starved\n"
            "  python -m deskmatch.synth tau-table --replicates 8\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    emit = sub.add_parser("emit", help="write a full example directory")
    _add_shape_args(emit)

    scenario = sub.add_parser("scenario", help="write one named adversarial scenario")
    scenario.add_argument("name", choices=scenario_names(), help="scenario to build")
    scenario.add_argument("--out", required=True)
    scenario.add_argument("--seed", default=None, help="override the scenario's seed")
    scenario.add_argument("--no-images", action="store_false", dest="write_images")

    sub.add_parser("list-scenarios", help="describe every scenario and exit")

    tau = sub.add_parser("tau-table", help="sanity-check the concentration knob")
    tau.add_argument("--n-people", type=int, default=30)
    tau.add_argument("--n-desks", type=int, default=30)
    tau.add_argument("-k", type=int, default=5, dest="k")
    tau.add_argument("--seed", default="tau-check")
    tau.add_argument("--noise", choices=NOISE_FAMILIES, default="thurstone")
    tau.add_argument("--replicates", type=int, default=8)
    tau.add_argument(
        "--concentrations", default="0,0.25,0.5,0.75,1",
        help="comma-separated values in [0,1] (default: %(default)s)",
    )
    return parser


def _emit(args: argparse.Namespace) -> int:
    mess = dict(
        response_rate=0.9, resubmit_frac=0.2, conflict_frac=0.12,
        stale_frac=0.1, unavailable_frac=0.06, vary_bool_tokens=True, shuffle_rows=True,
    ) if args.messy else {}
    world = generate(
        args.n_people,
        n_desks=args.n_desks,
        k=args.k,
        seed=args.seed,
        concentration=args.concentration,
        noise=args.noise,
        n_rooms=args.n_rooms,
        n_zones=args.n_zones,
        coord_space=args.coord_space,
        eligibility_style=args.eligibility_style,
        keeper_frac=args.keeper_frac,
        extra_columns=args.extra_columns,
        response_rate=mess.pop("response_rate", args.response_rate),
        resubmit_frac=mess.pop("resubmit_frac", args.resubmit_frac),
        conflict_frac=mess.pop("conflict_frac", args.conflict_frac),
        stale_frac=mess.pop("stale_frac", args.stale_frac),
        unavailable_frac=mess.pop("unavailable_frac", args.unavailable_frac),
        **mess,
    )
    config_dir, responses_path = world.write(args.out, write_images=args.write_images)
    print(f"wrote {config_dir}/  and  {responses_path}")
    print(
        f"  {world.n_people} people ({len(world.keeper_desks)} keepers), "
        f"{world.n_desks} desks in {len(world.rooms['rooms'])} room(s), "
        f"{len(world.zone_ids)} zone(s), K={world.k}"
    )
    print(
        f"  pool: {len(world.pool_people)} people / {len(world.pool_desks)} desks; "
        f"{len(world.response_rows)} response rows"
    )
    print(f"  concentration={world.concentration} noise={world.noise} seed={world.seed_string!r}")
    print(f"\nnext:\n  deskmatch solve --config {config_dir} --responses {responses_path}")
    return 0


def _scenario(args: argparse.Namespace) -> int:
    builder = SCENARIOS[args.name]
    # Built in memory first, then written here, so --no-images does not have to
    # be threaded through ten builder signatures.
    case = builder(**({"seed": args.seed} if args.seed is not None else {}))
    config_dir, responses_path = case.world.write(args.out, write_images=args.write_images)
    print(f"scenario: {case.name}")
    print(f"  {case.description}")
    print(f"  expected: {case.expectation}")
    print(f"  wrote {config_dir}/  and  {responses_path}")
    return 0


def _list_scenarios() -> int:
    for name in scenario_names():
        case = SCENARIOS[name]()
        print(f"{name}\n    {case.description}\n    expected: {case.expectation}\n")
    return 0


def _tau_table(args: argparse.Namespace) -> int:
    try:
        values = tuple(float(part) for part in args.concentrations.split(",") if part.strip())
    except ValueError:
        print(f"--concentrations must be comma-separated numbers, got {args.concentrations!r}",
              file=sys.stderr)
        return 1
    rows = kendall_tau_table(
        concentrations=values,
        n_people=args.n_people,
        n_desks=args.n_desks,
        k=args.k,
        seed=args.seed,
        noise=args.noise,
        replicates=args.replicates,
    )
    print(f"noise={args.noise}  n_people={args.n_people}  n_desks={args.n_desks}  "
          f"K={args.k}  replicates={args.replicates}  seed={args.seed!r}")
    print(render_tau_table(rows))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "emit":
            return _emit(args)
        if args.command == "scenario":
            return _scenario(args)
        if args.command == "list-scenarios":
            return _list_scenarios()
        if args.command == "tau-table":
            return _tau_table(args)
    except (ValueError, TypeError) as exc:
        # Bad arguments are a usage error (SPEC §9 exit code 1), not a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: could not write output: {exc}", file=sys.stderr)
        return 1
    print(f"error: unknown command {args.command!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
