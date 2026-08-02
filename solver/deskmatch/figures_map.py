"""Floor-plan figures: the desk-popularity heatmap and the contested-desk chart.

This is the figure people actually look at, so it is drawn on the real floor
plan rather than as a generic matrix heatmap. Everything it needs comes from
`config/rooms.json` (geometry, zones, zone colours) and the solve-ready
`Problem` (who ranked what). Nothing about the size of the problem is written
down here: the number of rooms, desks, zones and the value of K are all read
off the objects at runtime (invariant I1).

The metric
----------
A desk's colour is the **mean rank it received across every student in the
pool**, counting "this student did not rank this desk" as rank ``K+1``. So the
metric lives in ``[1, K+1]``: 1 means every single student put it first, and
K+1 means nobody named it at all. Lower is more wanted, and the colormap is
oriented so that more-wanted desks are the dark, salient ones.

Note that the metric is deliberately *not* normalised by "how many people
ranked it". A desk that one enthusiast ranked first and everyone else ignored
is not a popular desk, and averaging over only the people who ranked it would
say that it was.

Determinism (invariant I3)
--------------------------
These figures are part of the reproducibility target, so the same inputs must
produce byte-identical files:

* PDFs are written with a fully-specified info dictionary and
  ``CreationDate=None`` — matplotlib otherwise stamps ``datetime.today()``.
  SVG's ``<dc:date>`` and PNG's ``Software`` tag are pinned the same way, and
  ``rcParams['svg.hashsalt']`` is fixed so SVG element ids are stable.
* Nothing in a figure reads the clock. Provenance strings (seed, hashes) are
  the caller's business; this module never invents one.
* No set is ever iterated. Desks come out in `rooms.json` order, zones in
  `rooms.json` order (a Mapping, whose insertion order is guaranteed), and
  every derived ordering is an explicit ``sorted()`` with a total tie-break.
* There is no sampling anywhere in this module, so there is no RNG to seed. If
  that ever changes, the draw must come from `deskmatch.scoring.make_rng`.
* `pyplot` is never imported: figures are built as bare `Figure` objects, so
  there is no global figure registry and no interactive-backend state that
  could differ between machines.

Missing floor-plan image
------------------------
`config/floorplans/*.png` is dropped in by the coordinator and may simply not
be there. Every image-using path degrades: the desk polygons are drawn on a
neutral background, a visible note says which file was expected, and the run
carries on. It never raises and never emits a blank page.
"""

from __future__ import annotations

import contextlib
import functools
import math
import os
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import matplotlib

# Explicit per the repo's rendering contract. We also avoid pyplot entirely
# (see the module docstring), so this cannot leak into a caller's session in
# any way that matters -- but it is stated rather than assumed.
matplotlib.use("Agg")

import matplotlib as mpl  # noqa: E402  (must follow matplotlib.use)
import numpy as np  # noqa: E402
from matplotlib import colors as mcolors  # noqa: E402
from matplotlib import image as mimage  # noqa: E402
from matplotlib import ticker as mticker  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
from matplotlib.cm import ScalarMappable  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.patches import Patch, Polygon as MplPolygon, Rectangle  # noqa: E402

from .types import (  # noqa: E402
    Desk,
    DeskId,
    PersonId,
    Problem,
    Room,
    RoomId,
    Rooms,
    Solution,
    ZoneId,
)

__all__ = [
    "DeskStat",
    "PopularityStats",
    "FigureResult",
    "compute_desk_popularity",
    "desk_popularity_heatmap",
    "contested_desks_figure",
    # Shared with any other figure module, so the whole report stays visually
    # and reproducibly consistent.
    "figure_metadata",
    "open_pdf",
    "house_rc",
    "relative_luminance",
    "contrast_ratio",
    "best_text_color",
    "label_anchor",
    "FIGURE_RC",
    "SVG_HASHSALT",
    "PRODUCER",
]


# ==========================================================================
# Determinism knobs
# ==========================================================================

#: Fixed salt for SVG element ids. Without this matplotlib derives ids from
#: object identity, which varies run to run. Deliberately the same string as
#: `figures_stats._HOUSE_RCPARAMS`, so a report that mixes figures from both
#: modules has one consistent id namespace.
SVG_HASHSALT = "deskmatch"

#: Written into PDF /Producer and the PNG Software tag. A constant rather than
#: the matplotlib version, so the bytes do not move under a patch upgrade.
#: Matches `figures_stats.PDF_METADATA`.
PRODUCER = "deskmatch (matplotlib)"

#: Type 42 embeds a TrueType subset, which keeps the text selectable and
#: searchable in the published PDF. It routes through fontTools, so it is a
#: byte-determinism risk in principle -- the test harness diffs the bytes of two
#: runs in separate processes to confirm it is not one in practice.
_PDF_FONTTYPE = 42

#: rcParams applied around every draw *and* every save. Kept as a module
#: constant so a caller can see exactly what is being pinned.
FIGURE_RC: Mapping[str, Any] = {
    "svg.hashsalt": SVG_HASHSALT,
    "font.size": 9.0,
    "svg.fonttype": "path",
    "pdf.fonttype": _PDF_FONTTYPE,
    "ps.fonttype": _PDF_FONTTYPE,
    "pdf.compression": 6,
    "path.simplify": True,
    "path.simplify_threshold": 0.111111,
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans"],
    "mathtext.fontset": "dejavusans",
    "text.usetex": False,
    "axes.unicode_minus": True,
    "axes.linewidth": 0.6,
    "axes.edgecolor": "#4a4a4a",
    "axes.labelcolor": "#1a1a1a",
    "text.color": "#1a1a1a",
    "xtick.color": "#4a4a4a",
    "ytick.color": "#4a4a4a",
    "xtick.labelcolor": "#1a1a1a",
    "ytick.labelcolor": "#1a1a1a",
    "xtick.direction": "out",
    "ytick.direction": "out",
    "xtick.major.width": 0.6,
    "ytick.major.width": 0.6,
    "xtick.major.size": 3.0,
    "ytick.major.size": 3.0,
    "hatch.linewidth": 0.5,
    "hatch.color": "#8a8a8a",
    "legend.frameon": False,
    "legend.handlelength": 1.4,
    "legend.handleheight": 0.9,
    "legend.borderpad": 0.0,
    "legend.columnspacing": 1.4,
    "legend.handletextpad": 0.5,
    "figure.facecolor": "white",
    "savefig.facecolor": "white",
    "image.interpolation": "antialiased",
}

_PDF_METADATA_KEYS = (
    "Title",
    "Author",
    "Subject",
    "Keywords",
    "Creator",
    "Producer",
    "CreationDate",
    "ModDate",
    "Trapped",
)


def figure_metadata(
    title: str = "",
    *,
    subject: str = "",
    author: str = "",
    keywords: str = "",
    creator: str = "deskmatch",
) -> dict[str, Any]:
    """A fully-specified, clock-free PDF info dictionary.

    Every key matplotlib knows about is set explicitly. ``CreationDate`` and
    ``ModDate`` are ``None``, which is matplotlib's documented way of *removing*
    a key rather than defaulting it to `datetime.today()`. Leaving either one to
    the default is the single easiest way to break invariant I3, because the
    file then differs on every run by construction.
    """
    return {
        "Title": title,
        "Author": author,
        "Subject": subject,
        "Keywords": keywords,
        "Creator": creator,
        "Producer": PRODUCER,
        "CreationDate": None,
        "ModDate": None,
        "Trapped": "Unknown",
    }


def _png_metadata(title: str) -> dict[str, Any]:
    # Pillow writes tEXt chunks for these. 'Software' defaults to a string
    # carrying the matplotlib version; pin it so a version bump does not move
    # the bytes.
    return {"Software": PRODUCER, "Title": title}


def _svg_metadata(title: str) -> dict[str, Any]:
    # 'Date': None suppresses <dc:date>, which otherwise holds the wall clock.
    return {"Creator": PRODUCER, "Date": None, "Title": title}


# ==========================================================================
# Palette
# ==========================================================================

DEFAULT_CMAP = "magma"

#: Trim the extremes off the sequential map. Pure black swallows the floor plan
#: underneath and the top of magma is so near white that a desk nobody wanted
#: looks like a rendering failure rather than a result. A linear sub-range of a
#: perceptually uniform map is still perceptually uniform.
_CMAP_CLIP = (0.06, 0.90)

_BG_NEUTRAL = "#f1efec"        # canvas when the floor-plan image is missing
_BG_ROOM_EDGE = "#c9c4bd"
_OUT_OF_POOL_FACE = "#dedbd6"  # desks that are not up for grabs
_OUT_OF_POOL_EDGE = "#9a948c"
_NO_DATA_FACE = "#e7e4e0"      # in the pool, but nobody submitted anything
_DESK_EDGE = "#33302c"
_MUTED = "#5c574f"
_TINT_ALPHA = 0.11        # zone region fill; faint by design
_TEXT_DARK = "#141414"
_TEXT_LIGHT = "#ffffff"


def _build_cmap(name: str) -> mcolors.Colormap:
    """Clip a named colormap to `_CMAP_CLIP` and return it as a fixed map."""
    base = mpl.colormaps[name]
    lo, hi = _CMAP_CLIP
    samples = base(np.linspace(lo, hi, 256))
    return mcolors.ListedColormap(samples, name=f"{name}-clipped")


# ==========================================================================
# Colour arithmetic (contrast is computed, never guessed)
# ==========================================================================


def _to_rgb(color: Any) -> tuple[float, float, float]:
    r, g, b = mcolors.to_rgb(color)
    return (float(r), float(g), float(b))


def _srgb_to_linear(channel: float) -> float:
    if channel <= 0.04045:
        return channel / 12.92
    return float(((channel + 0.055) / 1.055) ** 2.4)


def relative_luminance(color: Any) -> float:
    """WCAG relative luminance of an sRGB colour, in [0, 1]."""
    r, g, b = (_srgb_to_linear(c) for c in _to_rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: Any, b: Any) -> float:
    """WCAG 2.x contrast ratio between two opaque colours, in [1, 21]."""
    la, lb = relative_luminance(a), relative_luminance(b)
    lighter, darker = (la, lb) if la >= lb else (lb, la)
    return (lighter + 0.05) / (darker + 0.05)


def _composite(fore: Any, alpha: float, back: Any) -> tuple[float, float, float]:
    """Alpha-composite `fore` at `alpha` over opaque `back`."""
    f = _to_rgb(fore)
    b = _to_rgb(back)
    a = min(max(float(alpha), 0.0), 1.0)
    return tuple(a * f[i] + (1.0 - a) * b[i] for i in range(3))  # type: ignore[return-value]


def best_text_color(background: Any) -> str:
    """Pick black or white for text on `background` by contrast ratio.

    This is the whole reason the effective background of every desk patch is
    reconstructed (patch colour composited over the actual pixels of the floor
    plan underneath): on a dark end of the colormap white wins by a mile, on the
    pale end black does, and the crossover is not where eyeballing puts it.
    """
    return _TEXT_LIGHT if contrast_ratio(_TEXT_LIGHT, background) >= contrast_ratio(
        _TEXT_DARK, background
    ) else _TEXT_DARK


# ==========================================================================
# Geometry
# ==========================================================================

Point = tuple[float, float]


def _desk_polygon(desk: Desk) -> list[Point]:
    """Desk outline in the config's own coordinate space, as a polygon.

    Rects become 4-point polygons so everything downstream has one code path.
    """
    if desk.shape_kind == "rect":
        x, y, w, h = (float(v) for v in desk.shape)  # type: ignore[misc]
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    pts: list[Point] = []
    for p in desk.shape:  # type: ignore[union-attr]
        pts.append((float(p[0]), float(p[1])))  # type: ignore[index]
    return pts


def _scaled(pts: Sequence[Point], sx: float, sy: float) -> list[Point]:
    return [(p[0] * sx, p[1] * sy) for p in pts]


def _bbox(pts: Sequence[Point]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _signed_area(pts: Sequence[Point]) -> float:
    total = 0.0
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        total += x0 * y1 - x1 * y0
    return total / 2.0


def _point_in_polygon(pt: Point, poly: Sequence[Point]) -> bool:
    """Ray casting. Boundary cases are not important here (label placement)."""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            t = (y - y0) / (y1 - y0) if y1 != y0 else 0.0
            if x < x0 + t * (x1 - x0):
                inside = not inside
    return inside


def label_anchor(poly: Sequence[Point]) -> Point:
    """Where to put a desk's label.

    Area centroid where that lands inside the shape, bbox centre where it does
    not (an L-shaped desk), vertex mean as the last resort. Vertex mean alone --
    what `Desk.centroid` does -- is fine for rectangles and wrong for anything
    with unevenly spaced vertices.
    """
    area = _signed_area(poly)
    if abs(area) > 1e-12:
        cx = cy = 0.0
        n = len(poly)
        for i in range(n):
            x0, y0 = poly[i]
            x1, y1 = poly[(i + 1) % n]
            cross = x0 * y1 - x1 * y0
            cx += (x0 + x1) * cross
            cy += (y0 + y1) * cross
        centroid = (cx / (6.0 * area), cy / (6.0 * area))
        if _point_in_polygon(centroid, poly):
            return centroid
    x0, y0, x1, y1 = _bbox(poly)
    mid = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    if _point_in_polygon(mid, poly):
        return mid
    return (sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly))


def _convex_hull(points: Sequence[Point]) -> list[Point]:
    """Monotone chain. Written out rather than pulled from scipy so the vertex
    order is ours and therefore trivially deterministic."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return list(pts)

    def cross(o: Point, a: Point, b: Point) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[Point] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[Point] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _grow_hull(poly: Sequence[Point], pad: float) -> list[Point]:
    """Push every vertex `pad` outward from the shape's centre, then re-hull.

    An approximate outward offset. Good enough for a soft zone tint and it
    cannot produce a self-intersecting result, because of the re-hull.
    """
    if not poly:
        return []
    cx = sum(p[0] for p in poly) / len(poly)
    cy = sum(p[1] for p in poly) / len(poly)
    moved: list[Point] = []
    for x, y in poly:
        dx, dy = x - cx, y - cy
        norm = math.hypot(dx, dy)
        if norm < 1e-9:
            moved.append((x, y))
        else:
            moved.append((x + pad * dx / norm, y + pad * dy / norm))
    return _convex_hull(moved) or list(moved)


def _convex_overlap(a: Sequence[Point], b: Sequence[Point]) -> bool:
    """Separating-axis test for two convex polygons. True when they overlap."""
    if len(a) < 3 or len(b) < 3:
        return False
    for poly in (a, b):
        n = len(poly)
        for i in range(n):
            x0, y0 = poly[i]
            x1, y1 = poly[(i + 1) % n]
            axis = (-(y1 - y0), x1 - x0)
            length = math.hypot(*axis)
            if length < 1e-12:
                continue
            axis = (axis[0] / length, axis[1] / length)
            pa = [axis[0] * p[0] + axis[1] * p[1] for p in a]
            pb = [axis[0] * p[0] + axis[1] * p[1] for p in b]
            if max(pa) <= min(pb) + 1e-9 or max(pb) <= min(pa) + 1e-9:
                return False
    return True


# ==========================================================================
# The metric
# ==========================================================================


@dataclass(frozen=True)
class DeskStat:
    """Everything the figures need to know about one desk."""

    desk_id: DeskId
    label: str
    room_id: RoomId
    zone: ZoneId
    in_pool: bool
    exclusion_reason: str            # "" when in_pool
    keeper_name: str = ""            # set only when a roster keeper holds it
    mean_rank: float | None = None   # None when nobody is in the pool
    first_choice_votes: int = 0
    n_ranked: int = 0                # students who ranked it at all
    rank_counts: tuple[int, ...] = ()  # length K; counts[i] = ranked (i+1)-th
    assigned_to: PersonId | None = None
    assigned_name: str = ""
    rank_received: int | None = None

    @property
    def assigned(self) -> bool:
        return self.assigned_to is not None


@dataclass(frozen=True)
class PopularityStats:
    k: int
    n_people: int
    unranked_rank: int                   # K + 1
    desks: tuple[DeskStat, ...]          # rooms.json order
    notes: tuple[str, ...] = ()

    @property
    def by_id(self) -> Mapping[DeskId, DeskStat]:
        return {d.desk_id: d for d in self.desks}

    @property
    def pool_desks(self) -> tuple[DeskStat, ...]:
        return tuple(d for d in self.desks if d.in_pool)

    def value_range(self) -> tuple[float, float] | None:
        vals = [d.mean_rank for d in self.desks if d.in_pool and d.mean_rank is not None]
        if not vals:
            return None
        return (min(vals), max(vals))


def _rooms_of(config: Any) -> Rooms:
    """Accept a `Config`, or a bare `Rooms`, or anything exposing `.rooms`.

    The report code holds a `Config`; the calibration tooling and the tests
    often only have the rooms document. Both are useful callers and the
    distinction costs three lines to erase.
    """
    rooms = getattr(config, "rooms", None)
    if isinstance(rooms, Rooms):
        return rooms
    if isinstance(config, Rooms):
        return config
    if rooms is not None and hasattr(rooms, "all_desks"):
        return rooms  # type: ignore[return-value]
    raise TypeError(
        "config must be a deskmatch.types.Config (or a Rooms); got "
        f"{type(config).__name__}, which exposes no rooms geometry"
    )


def _keepers(config: Any) -> Mapping[DeskId, str]:
    """desk id -> name of the person keeping it, from the roster."""
    roster = getattr(config, "roster", None)
    people = getattr(roster, "people", ()) if roster is not None else ()
    out: dict[DeskId, str] = {}
    for person in people:
        if getattr(person, "keeps_desk", False) and getattr(person, "current_desk", None):
            out[person.current_desk] = person.name
    return out


def compute_desk_popularity(
    config: Any,
    problem: Problem,
    solution: Solution | None = None,
    *,
    name_keepers: bool = False,
) -> PopularityStats:
    """Mean rank received per desk, plus the vote counts the bar chart needs.

    A desk a student did not rank counts as rank ``K+1`` for that student, so
    the average is over *everyone in the pool*, not over the subset who happened
    to name the desk. Desks that are not in the pool at all (kept by their
    current occupant, or `available: false`) get `mean_rank=None` and are
    reported with the reason, so the figures can show them as a distinct thing
    rather than as an unpopular desk.
    """
    rooms = _rooms_of(config)
    k = int(problem.k)
    unranked = k + 1
    n_people = int(problem.n_people)

    col_of: dict[DeskId, int] = {d: j for j, d in enumerate(problem.desks)}
    keepers = _keepers(config)

    assigned_by_desk: dict[DeskId, Any] = {}
    if solution is not None:
        for a in solution.assignments:
            assigned_by_desk[a.desk_id] = a

    notes: list[str] = []
    stats: list[DeskStat] = []

    for desk in rooms.all_desks:
        j = col_of.get(desk.id)
        if j is None:
            keeper = keepers.get(desk.id, "")
            if desk.id in keepers:
                who = keeper if (name_keepers and keeper) else "its current occupant"
                reason = f"kept by {who}"
            elif not desk.available:
                reason = "marked unavailable in rooms.json"
            else:
                reason = "not in this year's desk pool"
            stats.append(
                DeskStat(
                    desk_id=desk.id,
                    label=desk.label,
                    room_id=desk.room_id,
                    zone=desk.zone,
                    in_pool=False,
                    exclusion_reason=reason,
                    keeper_name=keeper,
                    mean_rank=None,
                    first_choice_votes=0,
                    n_ranked=0,
                    rank_counts=(0,) * k,
                )
            )
            continue

        ranks = np.asarray(problem.rank[:, j], dtype=np.int64)
        ranked_mask = ranks >= 1
        if n_people:
            effective = np.where(ranked_mask, ranks, unranked).astype(np.float64)
            mean_rank: float | None = float(effective.mean())
        else:
            mean_rank = None
        counts = tuple(int(np.count_nonzero(ranks == r)) for r in range(1, k + 1))

        a = assigned_by_desk.get(desk.id)
        stats.append(
            DeskStat(
                desk_id=desk.id,
                label=desk.label,
                room_id=desk.room_id,
                zone=desk.zone,
                in_pool=True,
                exclusion_reason="",
                mean_rank=mean_rank,
                first_choice_votes=counts[0] if counts else 0,
                n_ranked=int(np.count_nonzero(ranked_mask)),
                rank_counts=counts,
                assigned_to=None if a is None else a.email,
                assigned_name="" if a is None else a.name,
                rank_received=None if a is None else int(a.rank_received),
            )
        )

    known = {d.id for d in rooms.all_desks}
    orphans = sorted(d for d in problem.desks if d not in known)
    if orphans:
        notes.append(
            f"{len(orphans)} desk(s) in the solve are not in rooms.json and cannot "
            f"be drawn: {', '.join(orphans)}. The config and the response data have "
            f"drifted apart."
        )
    if n_people == 0:
        notes.append("No students are in the pool, so no desk has a mean rank.")

    return PopularityStats(
        k=k,
        n_people=n_people,
        unranked_rank=unranked,
        desks=tuple(stats),
        notes=tuple(notes),
    )


# ==========================================================================
# The floor-plan image (which may not be there)
# ==========================================================================


@dataclass(frozen=True)
class RoomImage:
    """A room's backdrop, faded to grey, or the reason there isn't one."""

    array: np.ndarray | None      # (h, w, 3) float in [0, 1], already faded
    path: str                     # what we looked for, for the note
    missing: bool
    note: str = ""


def _source_dir(config: Any) -> str:
    src = getattr(config, "source_dir", None)
    if isinstance(src, (str, os.PathLike)):
        return os.fspath(src)
    return "."


def _load_room_image(config: Any, room: Room, fade: float) -> RoomImage:
    """Load and fade the floor plan. Never raises.

    The plan is line art; converting it to grey and washing it out by `fade`
    stops it competing with the colour overlay, which is the actual data.
    """
    rel = room.image or ""
    path = os.path.join(_source_dir(config), rel) if rel else ""

    if not rel:
        return RoomImage(
            None, "", True,
            f"No floor-plan image is configured for {room.id}; desks are drawn "
            f"from rooms.json coordinates only.",
        )
    if not os.path.isfile(path):
        return RoomImage(
            None, path, True,
            f"Floor-plan image not found: {rel} — desks are drawn from the "
            f"rooms.json coordinates only.",
        )
    try:
        raw = np.asarray(mimage.imread(path))
    except Exception as exc:                      # pragma: no cover - env specific
        return RoomImage(
            None, path, True,
            f"Floor-plan image {rel} could not be read ({type(exc).__name__}); "
            f"desks are drawn from the rooms.json coordinates only.",
        )

    arr = raw.astype(np.float64)
    if raw.dtype == np.uint8:
        arr = arr / 255.0
    if arr.ndim == 2:
        rgb = np.repeat(arr[:, :, None], 3, axis=2)
    elif arr.ndim == 3 and arr.shape[2] == 4:
        alpha = arr[:, :, 3:4]
        rgb = arr[:, :, :3] * alpha + (1.0 - alpha)      # over white
    elif arr.ndim == 3 and arr.shape[2] == 3:
        rgb = arr[:, :, :3]
    else:
        return RoomImage(
            None, path, True,
            f"Floor-plan image {rel} has an unsupported shape {raw.shape}; desks "
            f"are drawn from the rooms.json coordinates only.",
        )

    rgb = np.clip(rgb, 0.0, 1.0)
    grey = 0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]
    f = min(max(float(fade), 0.0), 1.0)
    faded = 1.0 - (1.0 - grey) * (1.0 - f)
    out = np.repeat(faded[:, :, None], 3, axis=2)

    note = ""
    img_h, img_w = out.shape[0], out.shape[1]
    cfg_w, cfg_h = int(room.image_size[0]), int(room.image_size[1])
    if cfg_w > 0 and cfg_h > 0:
        want = cfg_w / cfg_h
        have = img_w / img_h if img_h else want
        if abs(want - have) / want > 0.02:
            note = (
                f"{rel} is {img_w}×{img_h}px but rooms.json declares "
                f"{cfg_w}×{cfg_h}px; the image has been stretched to the declared "
                f"size so the desk coordinates still land correctly."
            )
    return RoomImage(out, path, False, note)


def _backdrop_at(
    image: RoomImage, box: tuple[float, float, float, float], extent: tuple[float, float]
) -> tuple[float, float, float]:
    """Mean colour of the plan under a desk, for the contrast calculation."""
    if image.array is None:
        return _to_rgb(_BG_NEUTRAL)
    h, w = image.array.shape[0], image.array.shape[1]
    ex_w, ex_h = extent
    if ex_w <= 0 or ex_h <= 0:
        return _to_rgb(_BG_NEUTRAL)
    x0, y0, x1, y1 = box
    c0 = int(math.floor(x0 / ex_w * w))
    c1 = int(math.ceil(x1 / ex_w * w))
    r0 = int(math.floor(y0 / ex_h * h))
    r1 = int(math.ceil(y1 / ex_h * h))
    c0, c1 = max(0, min(c0, w - 1)), max(1, min(c1, w))
    r0, r1 = max(0, min(r0, h - 1)), max(1, min(r1, h))
    if c1 <= c0:
        c1 = c0 + 1
    if r1 <= r0:
        r1 = r0 + 1
    patch = image.array[r0:r1, c0:c1, :]
    if patch.size == 0:
        return _to_rgb(_BG_NEUTRAL)
    mean = patch.reshape(-1, 3).mean(axis=0)
    return (float(mean[0]), float(mean[1]), float(mean[2]))


# ==========================================================================
# Layout (everything in inches; no constrained_layout, so sizes are knowable)
# ==========================================================================

_M_L = 0.42
_M_R = 0.42
_CB_GAP = 0.34
_CB_W = 0.20
_CB_TEXT = 1.05      # tick labels + the two-line rotated label
_T_PAD = 0.20
_TITLE_H = 0.30
_SUB_H = 0.24
_GAP_AFTER_TITLE = 0.24
_ROOM_TITLE_H = 0.26
_CELL_GAP_X = 0.34
_CELL_GAP_Y = 0.46
_GAP_BEFORE_LEGEND = 0.24
_LEGEND_ROW_H = 0.25
_GAP_BEFORE_CAPTION = 0.20
_CAPTION_LINE_H = 0.175
_GAP_BEFORE_FOOTER = 0.14
_FOOTER_H = 0.16
_B_PAD = 0.20


def _fit_aspect(
    x: float, y: float, w: float, h: float, aspect: float
) -> tuple[float, float, float, float]:
    """Largest box of the given width/height ratio, centred in (x, y, w, h)."""
    if aspect <= 0 or w <= 0 or h <= 0:
        return (x, y, w, h)
    if w / h > aspect:
        nh = h
        nw = h * aspect
    else:
        nw = w
        nh = w / aspect
    return (x + (w - nw) / 2.0, y + (h - nh) / 2.0, nw, nh)


#: Ceiling on how tall one room's panel may get, in inches. Without it a
#: portrait room drags the whole page to an unprintable length; with it the
#: room is fitted by height instead and centred in its cell.
_MAX_CELL_H = 5.0


def _grid_shape(n: int) -> tuple[int, int]:
    """Rows and columns for n rooms.

    Floor plans are wide, so one or two of them stack in a single column and
    stay large; beyond that a near-square grid wastes less page.
    """
    if n <= 0:
        return (1, 1)
    if n <= 2:
        return (n, 1)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    return (rows, cols)


def _wrap(text: str, width_in: float, fontsize: float) -> list[str]:
    """Wrap to the printable width. DejaVu Sans averages ~0.52em per character."""
    if not text:
        return []
    chars = max(24, int(width_in * 72.0 / (0.52 * fontsize)))
    lines: list[str] = []
    for para in text.split("\n"):
        lines.extend(textwrap.wrap(para, width=chars) or [""])
    return lines


@dataclass
class _Page:
    fig: Figure
    axes: list[Axes]
    cbar_ax: Axes | None
    legend_anchor: tuple[float, float]
    axes_width_in: list[float]


def _new_page(
    *,
    aspects: Sequence[float],
    room_titles: Sequence[str] | None,
    width: float,
    title: str,
    subtitle: str,
    caption_lines: Sequence[str],
    footer_lines: Sequence[str],
    needs_cbar: bool,
    legend_rows: int,
    reuse: Figure | None = None,
) -> _Page:
    n = max(1, len(aspects))
    rows, cols = _grid_shape(n)

    cbar_block = (_CB_GAP + _CB_W + _CB_TEXT) if needs_cbar else 0.0
    content_w = width - _M_L - _M_R - cbar_block
    content_w = max(content_w, 1.0)
    cell_w = (content_w - (cols - 1) * _CELL_GAP_X) / cols
    cell_w = max(cell_w, 0.6)
    title_band = _ROOM_TITLE_H if room_titles else 0.0
    # Each row is only as tall as the tallest room in *that* row. Sharing one
    # height across the whole grid means a wide room stacked under a portrait
    # one sits in an acre of blank paper.
    row_h: list[float] = []
    for r in range(rows):
        in_row = [aspects[i] for i in range(r * cols, min((r + 1) * cols, n))]
        if not in_row:
            in_row = [1.0]
        row_h.append(
            min(_MAX_CELL_H, max((cell_w / a) if a > 0 else cell_w for a in in_row))
        )
    content_h = sum(row_h) + rows * title_band + (rows - 1) * _CELL_GAP_Y

    below = (
        _B_PAD
        + len(footer_lines) * _FOOTER_H
        + (_GAP_BEFORE_FOOTER if footer_lines else 0.0)
        + len(caption_lines) * _CAPTION_LINE_H
        + (_GAP_BEFORE_CAPTION if caption_lines else 0.0)
        + legend_rows * _LEGEND_ROW_H
        + (_GAP_BEFORE_LEGEND if legend_rows else 0.0)
    )
    above = _T_PAD + (_TITLE_H if title else 0.0) + (_SUB_H if subtitle else 0.0)
    above += _GAP_AFTER_TITLE if (title or subtitle) else 0.0
    height = below + content_h + above

    if reuse is not None:
        fig = reuse
        fig.clear()
        fig.set_size_inches(width, height)
    else:
        fig = Figure(figsize=(width, height))
    FigureCanvasAgg(fig)
    fig.set_facecolor("white")

    def fx(v: float) -> float:
        return v / width

    def fy(v: float) -> float:
        return v / height

    content_bottom = below
    content_top = below + content_h

    axes: list[Axes] = []
    axes_width_in: list[float] = []
    row_top = [content_top]
    for r in range(1, rows):
        row_top.append(row_top[r - 1] - row_h[r - 1] - title_band - _CELL_GAP_Y)

    for idx in range(n):
        r, c = divmod(idx, cols)
        cell_x = _M_L + c * (cell_w + _CELL_GAP_X)
        cell_top = row_top[r]
        cell_y = cell_top - row_h[r] - title_band
        ax_x, ax_y, ax_w, ax_h = _fit_aspect(
            cell_x, cell_y, cell_w, row_h[r],
            aspects[idx] if idx < len(aspects) else 1.0,
        )
        ax = fig.add_axes((fx(ax_x), fy(ax_y), fx(ax_w), fy(ax_h)))
        axes.append(ax)
        axes_width_in.append(ax_w)
        if room_titles:
            fig.text(
                fx(cell_x + cell_w / 2.0),
                fy(cell_top - 0.03),
                room_titles[idx],
                ha="center",
                va="top",
                fontsize=10.0,
                color="#232323",
                fontweight="medium",
            )

    cbar_ax: Axes | None = None
    if needs_cbar:
        bar_h = min(content_h * 0.62, 3.2)
        bar_y = content_bottom + (content_h - bar_h) / 2.0
        bar_x = _M_L + content_w + _CB_GAP
        cbar_ax = fig.add_axes((fx(bar_x), fy(bar_y), fx(_CB_W), fy(bar_h)))

    y = height - _T_PAD
    if title:
        fig.text(fx(_M_L), fy(y), title, ha="left", va="top",
                 fontsize=14.0, color="#111111", fontweight="semibold")
        y -= _TITLE_H
    if subtitle:
        fig.text(fx(_M_L), fy(y), subtitle, ha="left", va="top",
                 fontsize=9.5, color=_MUTED)

    y = _B_PAD
    for line in reversed(list(footer_lines)):
        fig.text(fx(_M_L), fy(y), line, ha="left", va="bottom",
                 fontsize=7.0, color="#8a857d")
        y += _FOOTER_H
    if footer_lines:
        y += _GAP_BEFORE_FOOTER
    for line in reversed(list(caption_lines)):
        fig.text(fx(_M_L), fy(y), line, ha="left", va="bottom",
                 fontsize=8.5, color="#333333")
        y += _CAPTION_LINE_H
    if caption_lines:
        y += _GAP_BEFORE_CAPTION
    legend_anchor = (fx(_M_L), fy(y))

    return _Page(fig, axes, cbar_ax, legend_anchor, axes_width_in)


# ==========================================================================
# Drawing one room
# ==========================================================================


def _occupant_text(name: str, mode: str) -> str:
    parts = [p for p in name.split() if p]
    if not parts:
        return ""
    if mode == "initials":
        return "".join(p[0].upper() for p in parts)
    if mode == "surname":
        return parts[-1]
    return name


_OCCUPANT_FALLBACK = {"full": "surname", "surname": "initials", "initials": "initials"}


def _fit_font_size(
    entries: Sequence[tuple[list[str], float, float]],
    lo: float,
    hi: float,
) -> float:
    """One font size for every desk in the room, the largest that fits them all.

    Uniform label size is better typography than per-desk scaling, and it means
    a big desk does not read as more important than a small one.
    """
    best = hi
    for lines, w_pt, h_pt in entries:
        if not lines:
            continue
        longest = max(len(s) for s in lines) or 1
        by_width = (w_pt * 0.90) / (0.60 * longest)
        by_height = (h_pt * 0.86) / (1.22 * len(lines))
        best = min(best, by_width, by_height)
    return float(min(max(best, lo), hi))


def _rect_gap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """Closest distance between two axis-aligned boxes; 0 if they touch."""
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _cluster_desks(ids: Sequence[DeskId], boxes: Mapping[DeskId, tuple[float, float, float, float]],
                   threshold: float) -> list[list[DeskId]]:
    """Single-linkage clustering of desks by gap. Deterministic (union-find over
    a fixed order, then sorted output).

    Wanted because a zone is often not one blob: in the real plan the upper-years
    side is two desk rows *plus* three desks against the far wall, and one convex
    hull over the lot draws a large wedge through empty floor that looks like it
    means something. Per-cluster regions say what is actually true.
    """
    parent = {d: d for d in ids}

    def find(x: DeskId) -> DeskId:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if _rect_gap(boxes[ids[i]], boxes[ids[j]]) <= threshold:
                ri, rj = find(ids[i]), find(ids[j])
                if ri != rj:
                    parent[rj] = ri

    groups: dict[DeskId, list[DeskId]] = {}
    for d in ids:
        groups.setdefault(find(d), []).append(d)
    return [groups[key] for key in sorted(groups)]


def _zone_regions(
    room: Room,
    polys: Mapping[DeskId, list[Point]],
    zone_order: Sequence[ZoneId],
    pad: float,
    cluster_gap: float,
) -> dict[ZoneId, list[list[Point]]]:
    """One padded convex region per contiguous cluster of a zone's desks."""
    boxes = {did: _bbox(p) for did, p in polys.items()}
    out: dict[ZoneId, list[list[Point]]] = {}
    for zone_id in zone_order:
        ids = [d.id for d in room.desks if d.zone == zone_id and d.id in polys]
        if not ids:
            continue
        regions: list[list[Point]] = []
        for cluster in _cluster_desks(ids, boxes, cluster_gap):
            pts: list[Point] = []
            for did in cluster:
                pts.extend(polys[did])
            hull = _convex_hull(pts)
            if len(hull) < 3:
                x0, y0, x1, y1 = _bbox(pts)
                hull = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            regions.append(_grow_hull(hull, pad))
        out[zone_id] = regions
    return out


def _draw_room(
    ax: Axes,
    *,
    room: Room,
    rooms_cfg: Rooms,
    stats: PopularityStats,
    image: RoomImage,
    cmap: mcolors.Colormap,
    norm: mcolors.Normalize | None,
    axes_width_in: float,
    patch_alpha: float,
    zone_tint: bool,
    annotate: bool,
    show_occupants: str,
    has_solution: bool,
    notes: list[str],
) -> tuple[list[Patch], list[ZoneId]]:
    """Draw one room.

    Returns the legend handles this room needs and the zone ids it contains,
    both in rooms.json order so a caller assembling a multi-room page can merge
    them deterministically.
    """
    width_px = float(room.image_size[0]) or 1.0
    height_px = float(room.image_size[1]) or 1.0
    if rooms_cfg.coord_space == "normalized":
        sx, sy = width_px, height_px
    else:
        sx = sy = 1.0

    ax.set_xlim(0.0, width_px)
    ax.set_ylim(height_px, 0.0)          # image convention: y grows downward
    ax.set_aspect("equal", adjustable="box")
    ax.set_axis_off()

    # --- backdrop -------------------------------------------------------
    if image.array is not None:
        ax.imshow(
            image.array,
            extent=(0.0, width_px, height_px, 0.0),
            zorder=0,
            interpolation="antialiased",
        )
    else:
        ax.add_patch(
            Rectangle(
                (0.0, 0.0), width_px, height_px,
                facecolor=_BG_NEUTRAL, edgecolor=_BG_ROOM_EDGE,
                linewidth=0.8, zorder=0,
            )
        )
        note_fs = min(8.0, max(5.5, axes_width_in * 0.95))
        ax.text(
            width_px / 2.0, height_px * 0.045,
            "\n".join(_wrap(image.note, axes_width_in * 0.86, note_fs)),
            ha="center", va="top", fontsize=note_fs, color="#8a5a2a", zorder=6,
            linespacing=1.25,
            bbox=dict(boxstyle="round,pad=0.35", facecolor="#fdf3e3",
                      edgecolor="#d9b47a", linewidth=0.6),
        )
    if image.note:
        notes.append(image.note)

    # --- geometry -------------------------------------------------------
    polys: dict[DeskId, list[Point]] = {}
    for desk in room.desks:
        polys[desk.id] = _scaled(_desk_polygon(desk), sx, sy)

    zone_order = [z for z in rooms_cfg.zones if any(d.zone == z for d in room.desks)]
    # A zone referenced by a desk but absent from rooms.json:zones would be a
    # validation error, but the figure must not crash on one.
    for desk in room.desks:
        if desk.zone not in zone_order and desk.zone not in rooms_cfg.zones:
            zone_order.append(desk.zone)

    def zone_color(zone_id: ZoneId) -> str:
        z = rooms_cfg.zones.get(zone_id)
        return getattr(z, "color", None) or "#666666"

    def zone_label(zone_id: ZoneId) -> str:
        z = rooms_cfg.zones.get(zone_id)
        return getattr(z, "label", None) or zone_id

    # --- zone tint ------------------------------------------------------
    tinted: set[ZoneId] = set()
    if zone_tint and zone_order and polys:
        # A tint reads best with a little breathing room around the desks, but
        # zones can sit close together (in the real plan the two sides of the
        # room are ~11px apart). Take the largest padding that still leaves the
        # regions of *different* zones disjoint, and give up only if even zero
        # padding overlaps -- which means the zones genuinely interleave and no
        # region can honestly be drawn.
        span = max(width_px, height_px)
        sizes = sorted(
            min(b[2] - b[0], b[3] - b[1]) for b in (_bbox(p) for p in polys.values())
        )
        cluster_gap = 0.5 * (sizes[len(sizes) // 2] if sizes else span * 0.02)
        regions: dict[ZoneId, list[list[Point]]] = {}
        clash = True
        for factor in (0.012, 0.008, 0.005, 0.0025, 0.0):
            regions = _zone_regions(room, polys, zone_order, factor * span, cluster_gap)
            ids = [z for z in zone_order if regions.get(z)]
            clash = any(
                _convex_overlap(pa, pb)
                for i in range(len(ids))
                for j in range(i + 1, len(ids))
                for pa in regions[ids[i]]
                for pb in regions[ids[j]]
            )
            if not clash:
                break
        if clash:
            notes.append(
                f"{room.id}: zone regions interleave, so no zone tint is drawn; "
                f"the coloured outline on each desk carries the zone instead."
            )
        else:
            for zone_id in zone_order:
                for region in regions.get(zone_id, ()):
                    tinted.add(zone_id)
                    # Fill and boundary as separate patches: the fill has to be
                    # faint enough not to compete with the data, but at that
                    # alpha the boundary disappears, and the boundary is the
                    # part that says "this is where the zone ends".
                    ax.add_patch(
                        MplPolygon(
                            region, closed=True,
                            facecolor=zone_color(zone_id), alpha=_TINT_ALPHA,
                            edgecolor="none", zorder=1,
                        )
                    )
                    ax.add_patch(
                        MplPolygon(
                            region, closed=True, facecolor="none",
                            edgecolor=zone_color(zone_id), alpha=0.40,
                            linewidth=0.9, linestyle=(0, (5, 3)), zorder=1,
                        )
                    )

    # --- desks ----------------------------------------------------------
    by_id = stats.by_id
    text_entries: list[tuple[list[str], float, float]] = []
    ppp = (axes_width_in * 72.0) / width_px if width_px else 1.0
    occ_mode = show_occupants if show_occupants in _OCCUPANT_FALLBACK else "none"

    def build_lines(stat: DeskStat | None, desk: Desk, mode: str) -> list[str]:
        lines = [desk.label]
        if stat is None:
            return lines
        if stat.in_pool and stat.mean_rank is not None:
            lines.append(f"{stat.mean_rank:.1f}")
        else:
            lines.append("–")               # en dash: no value by design
        if mode != "none":
            if stat.assigned_name:
                who = _occupant_text(stat.assigned_name, mode)
                if stat.rank_received is not None:
                    who = f"{who} #{stat.rank_received}"
                lines.append(who)
            elif stat.keeper_name:
                lines.append(_occupant_text(stat.keeper_name, mode))
            elif stat.in_pool:
                lines.append("free")
        return lines

    if annotate:
        mode = occ_mode
        while True:
            text_entries = []
            for desk in room.desks:
                poly = polys[desk.id]
                x0, y0, x1, y1 = _bbox(poly)
                text_entries.append(
                    (
                        build_lines(by_id.get(desk.id), desk, mode),
                        (x1 - x0) * ppp,
                        (y1 - y0) * ppp,
                    )
                )
            size = _fit_font_size(text_entries, 3.6, 10.0)
            if size >= 4.6 or mode in ("none", "initials"):
                break
            nxt = _OCCUPANT_FALLBACK[mode]
            notes.append(
                f"{room.id}: occupant names do not fit at a readable size; showing "
                f"'{nxt}' instead of '{mode}'."
            )
            mode = nxt
        occ_mode = mode
        font_size = size
    else:
        font_size = 8.0

    has_out_of_pool = False
    has_no_data = False
    has_free = False

    for desk in room.desks:
        poly = polys[desk.id]
        stat = by_id.get(desk.id)
        zcol = zone_color(desk.zone)

        # Zone halo: a wider stroke under the desk, so the zone reads even when
        # the tint is suppressed and regardless of the fill colour.
        ax.add_patch(
            MplPolygon(
                poly, closed=True, facecolor="none", edgecolor=zcol,
                linewidth=2.6, alpha=0.85, zorder=2, joinstyle="round",
            )
        )

        if stat is None or not stat.in_pool:
            has_out_of_pool = True
            face: Any = _OUT_OF_POOL_FACE
            alpha = 1.0
            ax.add_patch(
                MplPolygon(
                    poly, closed=True, facecolor=face, edgecolor=_OUT_OF_POOL_EDGE,
                    linewidth=0.8, hatch="////", zorder=3,
                )
            )
        elif stat.mean_rank is None or norm is None:
            has_no_data = True
            face = _NO_DATA_FACE
            alpha = 1.0
            ax.add_patch(
                MplPolygon(
                    poly, closed=True, facecolor=face, edgecolor=_DESK_EDGE,
                    linewidth=0.7, zorder=3,
                )
            )
        else:
            face = cmap(norm(stat.mean_rank))
            alpha = patch_alpha
            free = has_solution and show_occupants != "none" and not stat.assigned
            has_free = has_free or free
            # A free desk is marked by breaking its outline, not by recolouring
            # it: in a normal year most desks are free at this point in the
            # figure's life and a coloured ring around nearly every one of them
            # would drown out the zone halo underneath, which carries real
            # information. The dashes let the halo show through instead.
            ax.add_patch(
                MplPolygon(
                    poly, closed=True, facecolor=face, alpha=alpha,
                    edgecolor=_DESK_EDGE,
                    linewidth=1.0 if free else 0.7,
                    linestyle=(0, (2.2, 1.6)) if free else "solid",
                    zorder=3,
                )
            )

        if not annotate:
            continue

        # Contrast: reconstruct what is actually behind the glyphs -- the desk
        # colour at its alpha, over the zone tint if there is one, over the
        # mean pixel value of the plan under this desk.
        box = _bbox(poly)
        backdrop = _backdrop_at(image, box, (width_px, height_px))
        if desk.zone in tinted:
            backdrop = _composite(zone_color(desk.zone), _TINT_ALPHA, backdrop)
        effective = _composite(face, alpha, backdrop)
        colour = best_text_color(effective)

        lines = build_lines(stat, desk, occ_mode)
        anchor = label_anchor(poly)
        ax.text(
            anchor[0], anchor[1], "\n".join(lines),
            ha="center", va="center", fontsize=font_size, color=colour,
            linespacing=1.15, zorder=5,
            fontweight="medium" if len(lines) <= 2 else "normal",
        )

    handles: list[Patch] = []
    for zone_id in zone_order:
        handles.append(
            Patch(
                facecolor=zone_color(zone_id), alpha=0.30,
                edgecolor=zone_color(zone_id), linewidth=1.6,
                label=f"{zone_label(zone_id)}",
            )
        )
    if has_out_of_pool:
        handles.append(
            Patch(
                facecolor=_OUT_OF_POOL_FACE, edgecolor=_OUT_OF_POOL_EDGE,
                hatch="////", linewidth=0.8,
                label="not in the pool (kept or unavailable) — no value shown",
            )
        )
    if has_no_data:
        handles.append(
            Patch(facecolor=_NO_DATA_FACE, edgecolor=_DESK_EDGE, linewidth=0.7,
                  label="in the pool, but no rankings to average")
        )
    if has_free:
        handles.append(
            Patch(facecolor="white", edgecolor=_DESK_EDGE, linewidth=1.0,
                  linestyle=(0, (2.2, 1.6)),
                  label="dashed outline: in the pool, nobody assigned")
        )
    return handles, zone_order


# ==========================================================================
# Output
# ==========================================================================


@dataclass(frozen=True)
class FigureResult:
    """What was drawn, where it went, and anything the caller should be told."""

    figures: tuple[Figure, ...]
    paths: tuple[str, ...] = ()
    stats: PopularityStats | None = None
    notes: tuple[str, ...] = ()

    def render_notes(self) -> str:
        return "\n".join(f"  {n}" for n in self.notes)


def _save_one(fig: Figure, path: Path, *, dpi: float, title: str) -> None:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        with PdfPages(path, metadata=figure_metadata(title)) as pdf:
            pdf.savefig(fig)
    elif suffix == ".png":
        fig.savefig(path, dpi=dpi, metadata=_png_metadata(title))
    elif suffix in (".svg", ".svgz"):
        fig.savefig(path, metadata=_svg_metadata(title))
    else:
        fig.savefig(path, dpi=dpi)


def _emit(
    figures: Sequence[Figure],
    target: Any,
    *,
    dpi: float,
    title: str,
    slugs: Sequence[str],
) -> tuple[str, ...]:
    """Write the figures wherever `target` says. Returns the paths written."""
    if target is None or isinstance(target, Figure):
        return ()
    if isinstance(target, PdfPages):
        for fig in figures:
            target.savefig(fig)
        return ()

    path = Path(os.fspath(target))
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)

    if path.suffix.lower() == ".pdf":
        with PdfPages(path, metadata=figure_metadata(title)) as pdf:
            for fig in figures:
                pdf.savefig(fig)
        return (str(path),)

    if len(figures) == 1:
        _save_one(figures[0], path, dpi=dpi, title=title)
        return (str(path),)

    out: list[str] = []
    for fig, slug in zip(figures, slugs):
        p = path.with_name(f"{path.stem}-{slug}{path.suffix}")
        _save_one(fig, p, dpi=dpi, title=f"{title} — {slug}")
        out.append(str(p))
    return tuple(out)


# ==========================================================================
# The heatmap
# ==========================================================================


def desk_popularity_heatmap(
    target: Any,
    config: Any,
    problem: Problem,
    solution: Solution | None = None,
    *,
    rooms: Sequence[RoomId] | RoomId | None = None,
    layout: str = "auto",
    show_occupants: str = "none",
    color_scale: str = "data",
    vmin: float | None = None,
    vmax: float | None = None,
    cmap: str | mcolors.Colormap = DEFAULT_CMAP,
    zone_tint: bool | str = "auto",
    annotate: bool = True,
    patch_alpha: float = 0.86,
    image_fade: float = 0.55,
    width: float = 11.0,
    dpi: float = 200.0,
    title: str | None = None,
    subtitle: str | None = None,
    caption: str | None = None,
    footer: str | None = None,
    stats: PopularityStats | None = None,
) -> FigureResult:
    """Shade every desk on the floor plan by how wanted it was.

    Parameters
    ----------
    target
        Where the figures go. ``None`` builds them and returns them without
        writing; a path writes (``.pdf`` gets one page per room, other formats
        get one file per figure, suffixed with the room id when there is more
        than one); an open `PdfPages` gets pages appended; a `Figure` is reused
        in place, which forces a single-page grid layout.
    config
        A `deskmatch.types.Config` (or a bare `Rooms`). Supplies the geometry,
        the zones and their colours, the coordinate space, and the roster —
        which is what lets a locked desk be labelled as *kept* rather than just
        *absent*.
    problem
        The solve-ready `Problem`. Its `rank` matrix is the whole data source
        for the metric, and `problem.k` is K. Nothing is assumed about the size
        of any of it.
    solution
        Optional. Only needed for `show_occupants`.
    layout
        ``"auto"`` (pages into a PDF, one grid otherwise), ``"pages"``, or
        ``"grid"``.
    show_occupants
        ``"none"`` (default), ``"initials"``, ``"surname"`` or ``"full"``. This
        is the who-got-what overlay. Final assignments are public per SPEC
        §7.2, so this is not a privacy leak in itself — but it is clutter on a
        figure whose subject is *demand*, so the caller decides and the public
        report should leave it off.
    color_scale
        ``"data"`` (default) stretches the colormap over the range the desks
        actually occupy; ``"full"`` fixes it to the theoretical ``[1, K+1]``.
        Data is the default because with a normal-sized department the
        achievable range is a small slice of the theoretical one — every desk
        would come out the same colour on a full scale — and the colorbar
        states the full range either way. `vmin`/`vmax` override both.
    zone_tint
        ``"auto"`` tints each zone's region when the zones do not interleave,
        and falls back to the per-desk coloured outline when they do.

    Never raises on a missing floor-plan image; see the module docstring.
    """
    rooms_cfg = _rooms_of(config)
    if show_occupants not in ("none", "initials", "surname", "full"):
        raise ValueError(
            f"show_occupants must be one of none/initials/surname/full, "
            f"got {show_occupants!r}"
        )
    if color_scale not in ("data", "full"):
        raise ValueError(f"color_scale must be 'data' or 'full', got {color_scale!r}")
    if layout not in ("auto", "pages", "grid"):
        raise ValueError(f"layout must be auto/pages/grid, got {layout!r}")

    if stats is None:
        stats = compute_desk_popularity(
            config, problem, solution, name_keepers=show_occupants != "none"
        )
    notes: list[str] = list(stats.notes)

    wanted = list(rooms_cfg.rooms)
    if rooms is not None:
        keep = [rooms] if isinstance(rooms, str) else list(rooms)
        wanted = [r for r in wanted if r.id in keep]
        missing = [r for r in keep if all(x.id != r for x in rooms_cfg.rooms)]
        if missing:
            notes.append(
                f"requested room(s) not in rooms.json, skipped: {', '.join(sorted(missing))}"
            )
    if not wanted:
        notes.append("No rooms to draw.")
        wanted = []

    # --- colour scale, computed once across every room so pages compare ---
    k = stats.k
    cmap_obj = _build_cmap(cmap) if isinstance(cmap, str) else cmap
    rng = stats.value_range()
    if vmin is None or vmax is None:
        if color_scale == "full" or rng is None:
            lo, hi = 1.0, float(k + 1)
        else:
            lo, hi = rng
        lo = float(vmin) if vmin is not None else lo
        hi = float(vmax) if vmax is not None else hi
    else:
        lo, hi = float(vmin), float(vmax)

    flat = hi - lo < 1e-9
    if flat:
        notes.append(
            f"Every desk in the pool has the same mean rank ({lo:.2f}); the colour "
            f"scale has been widened artificially so the map still renders."
        )
        lo, hi = lo - 0.05, hi + 0.05
    norm: mcolors.Normalize | None = (
        mcolors.Normalize(vmin=lo, vmax=hi) if rng is not None else None
    )

    # --- text -------------------------------------------------------------
    n_pool = len(stats.pool_desks)
    n_all = len(stats.desks)
    if title is None:
        if len(wanted) == 1:
            title = f"Desk popularity — {wanted[0].label}"
        else:
            title = "Desk popularity"
    if subtitle is None:
        subtitle = (
            f"mean rank received from {stats.n_people} "
            f"{'student' if stats.n_people == 1 else 'students'} in the pool  ·  "
            f"K = {k} ranked choices each  ·  {n_pool} of {n_all} desks in the pool"
        )
        if norm is not None and (lo > 1.0 + 1e-9 or hi < k + 1 - 1e-9):
            subtitle += (
                f"  ·  colour scale spans {lo:.1f}–{hi:.1f} of a possible 1–{k + 1}"
            )
    if caption is None:
        if stats.n_people == 0:
            caption = (
                "How to read this: nobody is in the pool, so no desk has a mean "
                "rank and nothing is shaded. The plan still shows where every desk "
                "is and which seating zone it belongs to."
            )
        else:
            caption = (
                f"How to read this: every desk is shaded by the average rank it was "
                f"given across all {stats.n_people} students in the pool, where a "
                f"desk a student did not rank counts as rank {stats.unranked_rank} "
                f"for that student — so a lower number, drawn darker, means more "
                f"people put it near the top of their list. Coloured outlines and "
                f"tints mark the seating zones."
            )
            if n_pool < n_all:
                caption += (
                    " Hatched desks are outside this year's pool and are excluded "
                    "from the colour scale."
                )
            if show_occupants != "none":
                caption += (
                    " The third line in each desk is who was assigned there and the "
                    "rank they received."
                )
    if footer is None:
        coord = rooms_cfg.coord_space
        img_bits = []
        for room in wanted:
            img = room.image or "(none)"
            img_bits.append(f"{room.id}: {img}")
        footer = (
            f"geometry from config/rooms.json ({coord} coordinates)  ·  "
            + "  ·  ".join(img_bits)
        )

    caption_lines = _wrap(caption, width - _M_L - _M_R, 8.5)
    footer_lines = _wrap(footer, width - _M_L - _M_R, 7.0)

    as_pages = layout == "pages" or (
        layout == "auto"
        and (
            isinstance(target, PdfPages)
            or (
                target is not None
                and not isinstance(target, Figure)
                and Path(os.fspath(target)).suffix.lower() == ".pdf"
            )
        )
    )
    if isinstance(target, Figure) and as_pages and len(wanted) > 1:
        notes.append(
            "a Figure target can only hold one page; falling back to a grid layout."
        )
        as_pages = False

    images = {r.id: _load_room_image(config, r, image_fade) for r in wanted}
    tint_flag = True if zone_tint == "auto" else bool(zone_tint)

    figures: list[Figure] = []
    slugs: list[str] = []

    groups: list[list[Room]] = [[r] for r in wanted] if as_pages else ([wanted] if wanted else [[]])

    for group in groups:
        aspects = []
        for room in group:
            w, h = float(room.image_size[0]), float(room.image_size[1])
            aspects.append((w / h) if h > 0 else 1.0)
        if not aspects:
            aspects = [1.4]

        page_title = title
        if as_pages and len(wanted) > 1:
            page_title = f"{title} — {group[0].label}"
        room_titles = [r.label for r in group] if len(group) > 1 else None

        page = _new_page(
            aspects=aspects,
            room_titles=room_titles,
            width=width,
            title=page_title,
            subtitle=subtitle,
            caption_lines=caption_lines,
            footer_lines=footer_lines,
            needs_cbar=norm is not None,
            legend_rows=1,
            reuse=target if isinstance(target, Figure) else None,
        )

        handles: list[Patch] = []
        seen_labels: list[str] = []
        for idx, (ax, room) in enumerate(zip(page.axes, group)):
            room_handles, _ = _draw_room(
                ax,
                room=room,
                rooms_cfg=rooms_cfg,
                stats=stats,
                image=images[room.id],
                cmap=cmap_obj,
                norm=norm,
                axes_width_in=page.axes_width_in[idx],
                patch_alpha=patch_alpha,
                zone_tint=tint_flag,
                annotate=annotate,
                show_occupants=show_occupants,
                has_solution=solution is not None,
                notes=notes,
            )
            for h in room_handles:
                lab = h.get_label()
                if lab not in seen_labels:
                    seen_labels.append(lab)
                    handles.append(h)

        if not group:
            page.axes[0].set_axis_off()
            page.axes[0].text(
                0.5, 0.5, "No rooms are configured in rooms.json.",
                ha="center", va="center", fontsize=11, color=_MUTED,
                transform=page.axes[0].transAxes,
            )

        if handles:
            page.fig.legend(
                handles=handles,
                loc="lower left",
                bbox_to_anchor=page.legend_anchor,
                ncols=min(len(handles), 4),
                fontsize=8.5,
                frameon=False,
                borderaxespad=0.0,
            )

        if page.cbar_ax is not None and norm is not None:
            _draw_colorbar(page.fig, page.cbar_ax, cmap_obj, norm, stats)

        figures.append(page.fig)
        slugs.append(group[0].id if group else "empty")

    paths = _emit(figures, target, dpi=dpi, title=title, slugs=slugs)
    return FigureResult(tuple(figures), paths, stats, tuple(notes))


def _draw_colorbar(
    fig: Figure,
    cax: Axes,
    cmap: mcolors.Colormap,
    norm: mcolors.Normalize,
    stats: PopularityStats,
) -> None:
    sm = ScalarMappable(norm=norm, cmap=cmap)
    cbar = fig.colorbar(sm, cax=cax, orientation="vertical")
    # Rank 1 at the top: "top of the list" should be at the top of the bar.
    cbar.ax.invert_yaxis()
    cbar.outline.set_linewidth(0.6)
    cbar.outline.set_edgecolor("#4a4a4a")
    cbar.ax.yaxis.set_major_locator(mticker.MaxNLocator(nbins=6, steps=[1, 2, 2.5, 5, 10]))
    cbar.ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.1f"))
    cbar.ax.tick_params(labelsize=8.5, width=0.6, length=3.0, pad=2.0)
    cbar.set_label(
        f"mean rank across all {stats.n_people} students\n"
        f"(1 = everyone's first choice, "
        f"{stats.unranked_rank} = nobody ranked it)",
        fontsize=8.5,
        labelpad=4,
        linespacing=1.4,
    )
    cax.text(
        0.5, 1.02, "more wanted", transform=cax.transAxes,
        ha="center", va="bottom", fontsize=8.0, color=_MUTED, style="italic",
    )
    cax.text(
        0.5, -0.02, "less wanted", transform=cax.transAxes,
        ha="center", va="top", fontsize=8.0, color=_MUTED, style="italic",
    )


# ==========================================================================
# Contested desks
# ==========================================================================


def contested_desks_figure(
    target: Any,
    config: Any,
    problem: Problem,
    solution: Solution | None = None,
    *,
    show_winners: bool = False,
    top_n: int | None = None,
    min_votes: int = 1,
    width: float = 9.0,
    dpi: float = 200.0,
    title: str | None = None,
    subtitle: str | None = None,
    caption: str | None = None,
    footer: str | None = None,
    stats: PopularityStats | None = None,
) -> FigureResult:
    """Which desks drew the most first-place votes, as a sorted bar chart.

    Bars are coloured by zone, so the shape of the competition (one side of the
    room fought over, the other not) reads immediately. The dashed line at one
    is the point of the figure: a desk can only seat one person, so every unit
    of bar past that line is a student who had to be given something else.

    `show_winners` adds who ended up with each desk, and the rank *they* gave
    it. That is coordinator-report material — not because the assignment is
    secret (SPEC §7.2 makes assignments public) but because putting a name next
    to "seven people wanted this" is a different, more pointed statement than
    the aggregate figure alone.

    Counts come from `problem.rank`, i.e. after choices for out-of-pool or
    ineligible desks have been dropped. Any such drops are stated in a footnote
    rather than quietly changing the totals.
    """
    rooms_cfg = _rooms_of(config)
    if stats is None:
        stats = compute_desk_popularity(config, problem, solution, name_keepers=show_winners)
    notes: list[str] = list(stats.notes)

    def zone_color(zone_id: ZoneId) -> str:
        z = rooms_cfg.zones.get(zone_id)
        return getattr(z, "color", None) or "#666666"

    def zone_label(zone_id: ZoneId) -> str:
        z = rooms_cfg.zones.get(zone_id)
        return getattr(z, "label", None) or zone_id

    room_label = {r.id: r.label for r in rooms_cfg.rooms}
    multi_room = len(rooms_cfg.rooms) > 1

    # Deterministic order: votes descending, then desk id ascending.
    contested = [d for d in stats.desks if d.first_choice_votes >= max(0, min_votes)]
    contested.sort(key=lambda d: (-d.first_choice_votes, d.desk_id))
    if top_n is not None and top_n > 0:
        hidden = max(0, len(contested) - top_n)
        contested = contested[:top_n]
    else:
        hidden = 0

    n_bars = len(contested)
    if title is None:
        title = "Most contested desks"
    if subtitle is None:
        total_first = sum(d.first_choice_votes for d in stats.desks)
        distinct = sum(1 for d in stats.desks if d.first_choice_votes > 0)
        subtitle = (
            f"{total_first} first-place "
            f"{'vote' if total_first == 1 else 'votes'} spread over {distinct} "
            f"{'desk' if distinct == 1 else 'desks'}"
        )
    if caption is None:
        caption = (
            "How to read this: each bar counts the students whose FIRST choice was "
            "that desk; only one of them can have it, so every bar past the dashed "
            "line at one is a desk that was always going to disappoint someone. "
            "Bars are coloured by seating zone."
        )
        if show_winners:
            caption += (
                " The name on the right is who was assigned the desk, with the rank "
                "they themselves gave it."
            )
    footnotes: list[str] = []
    if hidden:
        footnotes.append(f"{hidden} further desk(s) with fewer votes are not shown.")
    if problem.dropped_choices:
        footnotes.append(
            f"{len(problem.dropped_choices)} submitted choice(s) were dropped before "
            f"this count (desk out of the pool, or a zone the student may not sit in)."
        )
    if footer is None:
        footer = "  ·  ".join(footnotes)

    # --- layout ---------------------------------------------------------
    caption_lines = _wrap(caption, width - _M_L - _M_R, 8.5)
    footer_lines = _wrap(footer, width - _M_L - _M_R, 7.0) if footer else []

    def _text_width(chars: int, fontsize: float) -> float:
        return chars * 0.52 * fontsize / 72.0

    bar_labels: list[str] = []
    for d in contested:
        base = f"Desk {d.label}"
        if multi_room:
            base = f"{base} · {room_label.get(d.room_id, d.room_id)}"
        bar_labels.append(base)

    winner_texts: list[str] = []
    if show_winners and solution is not None:
        for d in contested:
            winner_texts.append(
                f"{d.assigned_name} (their rank {d.rank_received})"
                if d.assigned_name
                else "unassigned"
            )

    bar_h = 0.26
    plot_h = max(1.6, n_bars * bar_h + 0.55)

    # Reserve exactly as much gutter as the longest label needs, then truncate
    # anything that would still overrun. Names like "Subrahmanyan
    # Chandrasekhar" are not hypothetical -- one is on the roster.
    left_cap = 0.30 * width
    left_labels = min(
        left_cap,
        max(0.85, _text_width(max((len(s) for s in bar_labels), default=8), 9.0) + 0.22),
    )
    winner_cap = 0.34 * width
    if winner_texts:
        winner_pad = min(
            winner_cap,
            _text_width(max(len(s) for s in winner_texts), 8.0) + 0.28,
        )
        budget = max(8, int((winner_pad - 0.28) * 72.0 / (0.52 * 8.0)))
        winner_texts = [
            s if len(s) <= budget else s[: budget - 1].rstrip() + "…" for s in winner_texts
        ]
    else:
        winner_pad = 0.0
    budget_left = max(6, int((left_labels - 0.22) * 72.0 / (0.52 * 9.0)))
    bar_labels = [
        s if len(s) <= budget_left else s[: budget_left - 1].rstrip() + "…"
        for s in bar_labels
    ]

    plot_w = max(2.0, width - _M_L - _M_R - left_labels - winner_pad)

    zones_present: list[ZoneId] = [
        z for z in rooms_cfg.zones if any(d.zone == z for d in contested)
    ]
    for d in contested:
        if d.zone not in zones_present:
            zones_present.append(d.zone)
    legend_rows = 1 if zones_present else 0

    below = (
        _B_PAD
        + len(footer_lines) * _FOOTER_H
        + (_GAP_BEFORE_FOOTER if footer_lines else 0.0)
        + len(caption_lines) * _CAPTION_LINE_H
        + _GAP_BEFORE_CAPTION
        + legend_rows * _LEGEND_ROW_H
        + (_GAP_BEFORE_LEGEND if legend_rows else 0.0)
        + 0.42                                    # x-axis label + ticks
    )
    # Headroom for the "one desk, one person" call-out above the top bar.
    above = _T_PAD + _TITLE_H + _SUB_H + _GAP_AFTER_TITLE + (0.20 if n_bars else 0.0)
    height = below + plot_h + above

    fig = Figure(figsize=(width, height))
    FigureCanvasAgg(fig)
    fig.set_facecolor("white")

    def fx(v: float) -> float:
        return v / width

    def fy(v: float) -> float:
        return v / height

    ax = fig.add_axes(
        (fx(_M_L + left_labels), fy(below), fx(plot_w), fy(plot_h))
    )

    y = height - _T_PAD
    fig.text(fx(_M_L), fy(y), title, ha="left", va="top",
             fontsize=14.0, color="#111111", fontweight="semibold")
    y -= _TITLE_H
    fig.text(fx(_M_L), fy(y), subtitle, ha="left", va="top",
             fontsize=9.5, color=_MUTED)

    y = _B_PAD
    for line in reversed(footer_lines):
        fig.text(fx(_M_L), fy(y), line, ha="left", va="bottom",
                 fontsize=7.0, color="#8a857d")
        y += _FOOTER_H
    if footer_lines:
        y += _GAP_BEFORE_FOOTER
    for line in reversed(caption_lines):
        fig.text(fx(_M_L), fy(y), line, ha="left", va="bottom",
                 fontsize=8.5, color="#333333")
        y += _CAPTION_LINE_H
    y += _GAP_BEFORE_CAPTION
    legend_anchor = (fx(_M_L), fy(y))

    # --- bars -----------------------------------------------------------
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color("#4a4a4a")
    ax.tick_params(axis="y", length=0, labelsize=9.0)
    ax.tick_params(axis="x", labelsize=9.0)
    ax.set_axisbelow(True)
    ax.xaxis.grid(True, color="#e2ded8", linewidth=0.6)
    ax.yaxis.grid(False)

    if n_bars == 0:
        ax.set_yticks([])
        ax.set_xticks([])
        ax.spines["bottom"].set_visible(False)
        ax.xaxis.grid(False)
        msg = (
            "No desk received a first-place vote."
            if stats.n_people
            else "No submissions, so there are no first-place votes to count."
        )
        ax.text(0.5, 0.5, msg, transform=ax.transAxes, ha="center", va="center",
                fontsize=10.5, color=_MUTED)
    else:
        positions = np.arange(n_bars, dtype=float)
        values = np.array([d.first_choice_votes for d in contested], dtype=float)
        colors = [zone_color(d.zone) for d in contested]
        ax.barh(positions, values, height=0.68, color=colors, edgecolor="none", zorder=3)
        ax.set_ylim(n_bars - 0.5, -0.5)          # most contested at the top
        vmax_bar = float(values.max())
        ax.set_xlim(0.0, vmax_bar * 1.14 + 0.15)

        ax.set_yticks(positions)
        ax.set_yticklabels(bar_labels)

        ax.xaxis.set_major_locator(mticker.MaxNLocator(integer=True, nbins=8))
        ax.set_xlabel("students whose first choice this was  (people)", fontsize=9.0)

        ax.axvline(1.0, color="#8a2f2f", linewidth=1.0, linestyle=(0, (4, 2)), zorder=4)
        ax.annotate(
            "one desk, one person",
            xy=(1.0, -0.5), xytext=(4, 5), textcoords="offset points",
            ha="left", va="bottom", fontsize=8.0, color="#8a2f2f",
            annotation_clip=False,
        )

        for idx, (pos, val, d) in enumerate(zip(positions, values, contested)):
            # Label inside the bar only when there is comfortably room, and
            # never right on top of the reference line at 1.
            inside = val >= vmax_bar * 0.45 and abs(val - 1.0) > 0.02 * vmax_bar
            ax.annotate(
                f"{int(val)}",
                xy=(val, pos),
                xytext=(-5 if inside else 4, 0),
                textcoords="offset points",
                ha="right" if inside else "left",
                va="center",
                fontsize=8.5,
                fontweight="medium",
                color=best_text_color(zone_color(d.zone)) if inside else "#333333",
            )
            if winner_texts:
                ax.annotate(
                    winner_texts[idx],
                    xy=(1.0, pos), xycoords=("axes fraction", "data"),
                    xytext=(8, 0), textcoords="offset points",
                    ha="left", va="center", fontsize=8.0, color="#3d3a35",
                    annotation_clip=False,
                )

    if zones_present:
        handles = [
            Patch(facecolor=zone_color(z), edgecolor="none", label=zone_label(z))
            for z in zones_present
        ]
        fig.legend(
            handles=handles,
            loc="lower left",
            bbox_to_anchor=legend_anchor,
            ncols=min(len(handles), 4),
            fontsize=8.5,
            frameon=False,
            borderaxespad=0.0,
        )

    paths = _emit([fig], target, dpi=dpi, title=title, slugs=["contested"])
    return FigureResult((fig,), paths, stats, tuple(notes))


# ==========================================================================
# rc scoping
# ==========================================================================
#
# Every public entry point is wrapped so that the rcParams above (crucially
# svg.hashsalt) are in force for both the draw and the save, and so that the
# caller's global rcParams are left exactly as they were found.


@contextlib.contextmanager
def house_rc() -> Any:
    """The rcParams these figures are drawn and saved under.

    rc_context() snapshots and restores, so mutating inside it is safe. We
    reset to matplotlib's *library* defaults first -- which a user's
    matplotlibrc does not touch -- and only then apply the house style, so the
    output cannot depend on whatever rcParams the calling process happened to
    be carrying. Without that reset, I3 would hold only for callers whose
    ambient state matched ours.
    """
    with mpl.rc_context():
        mpl.rcParams.update(mpl.rcParamsDefault)
        mpl.rcParams["backend"] = "Agg"
        mpl.rcParams.update(FIGURE_RC)
        yield


@contextlib.contextmanager
def open_pdf(path: str | os.PathLike[str], title: str = "", **meta: Any) -> Any:
    """`PdfPages` opened under the house rcParams. Use this, not `PdfPages`.

    matplotlib fixes two *file-level* settings when the `PdfPages` object is
    constructed, not when a figure is drawn into it: ``pdf.compression`` and
    ``pdf.fonttype``. Those are read from whatever rcParams the **caller** holds
    at construction time, and no rc scoping inside `desk_popularity_heatmap`
    can reach back and change them. A caller with unusual ambient rcParams
    therefore gets a byte-different (though visually identical, and still
    perfectly reproducible *for that caller*) file.

    This helper closes that gap: it builds the `PdfPages` inside `house_rc()`
    and holds that context for the whole ``with`` block, so a multi-figure
    report is byte-identical no matter what the calling process was carrying.

        with figures_map.open_pdf("results.pdf", "deskmatch results") as pdf:
            figures_map.desk_popularity_heatmap(pdf, config, problem)
            figures_map.contested_desks_figure(pdf, config, problem)
    """
    with house_rc():
        pdf = PdfPages(os.fspath(path), metadata=figure_metadata(title, **meta))
        try:
            yield pdf
        finally:
            pdf.close()


def _rc_wrapped(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with house_rc():
            return func(*args, **kwargs)

    return wrapper


desk_popularity_heatmap = _rc_wrapped(desk_popularity_heatmap)
contested_desks_figure = _rc_wrapped(contested_desks_figure)
