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

Structural features
-------------------
`rooms.json` (schema 2+) carries a `features` array per room: the room outline,
walls, doors, windows, partitions, furniture and named side-rooms. They are
**decoration**, drawn in muted grey *underneath* every desk patch, and they are
data in no sense at all:

* they are never shaded, never counted, and never enter the colour scale — the
  metric is computed in `compute_desk_popularity`, which walks `room.desks` and
  has never heard of a feature;
* they are absent from the contested-desk figure for the same reason;
* they get no legend entry. The legend is a key to *selectable states* (which
  zone, in the pool or not, assigned or not) and a wall is none of those. The
  caption says what the grey shapes are instead.

They earn their place in the report by making the plan legible: a reader can see
that desk 5 is under the windows and which corner the door is in. That matters
because there is no floor-plan image to fall back on — without its features the
senior office page would be a scatter of rectangles on blank paper rather than a
room. Which is why `room` and `outline` features are labelled inside their own
shape, and why a `door` is drawn as a leaf and a swing arc rather than a box: a
grey rectangle in a wall says nothing about which way the door opens, and
`swing` (the corner it is hinged on, default ``"sw"``) says exactly that.

A room may legitimately have **no** features at all — the main office does. Its
desk rectangles are the map and their spacing carries the layout, so there is
nothing structural left to draw and no page furniture is emitted for it.

Floor-plan images: absent by design, broken by accident
-------------------------------------------------------
These are two different states and only one of them is a problem.

* **No `image` key in rooms.json.** The intended configuration. The desks are
  drawn from the coordinates alone, on plain paper, with no placeholder canvas
  and no note — there is nothing missing, so saying so would be wrong.
* **An `image` IS configured but cannot be used** (file absent, unreadable,
  unsupported shape). That is an accident. The desks are drawn on a neutral
  placeholder canvas, a visible banner names the file that was expected, and the
  same text is returned in `FigureResult.notes`. It never raises and never emits
  a blank page.

The declared/actual size mismatch note is likewise only reachable when an image
was loaded.
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
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch, Polygon as MplPolygon, Rectangle  # noqa: E402

from .types import (  # noqa: E402
    Desk,
    DeskId,
    Feature,
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

_BG_NEUTRAL = "#f1efec"        # placeholder canvas when a CONFIGURED image is broken
_BG_ROOM_EDGE = "#c9c4bd"
#: The paper itself. Matches `FIGURE_RC["figure.facecolor"]`; it is what sits
#: behind the desks when no image is configured, which is the normal case.
_BG_PAPER = "#ffffff"
_OUT_OF_POOL_FACE = "#dedbd6"  # desks that are not up for grabs
_OUT_OF_POOL_EDGE = "#9a948c"
_NO_DATA_FACE = "#e7e4e0"      # in the pool, but nobody submitted anything
_DESK_EDGE = "#33302c"
_MUTED = "#5c574f"
_TINT_ALPHA = 0.11        # zone region fill; faint by design
_TEXT_DARK = "#141414"
_TEXT_LIGHT = "#ffffff"

#: Structural features. One warm-grey family, deliberately: features are
#: differentiated by weight, fill lightness and dash pattern, never by hue.
#: Hue is the data channel on this figure and lending any of it to a wall would
#: invite the reader to think the wall means something.
_FEAT_LINE = "#6b665e"         # default stroke
_FEAT_LINE_DARK = "#4f4a43"    # the outline and load-bearing walls
_FEAT_LINE_SOFT = "#948d84"    # furniture, and anything unrecognised
_FEAT_FILL_WALL = "#a49d94"    # solid poché
_FEAT_FILL_BAND = "#d6d2cb"    # windows
_FEAT_FILL_ROOM = "#e8e5e0"    # named side-rooms
_FEAT_FILL_SOFT = "#efece8"    # furniture, doors
_FEAT_TEXT = "#5b564f"
_FEAT_TEXT_BG = "#f7f5f2"      # translucent plate under a feature label

#: Draw order. Everything structural sits below the zone tint (1), the zone
#: halo (2) and the desk patches (3), so a feature can never obscure a desk.
_Z_FEATURE_FILL = 0.30
_Z_FEATURE_LINE = 0.40
_Z_FEATURE_TEXT = 0.50


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


def _feature_points(feature: Feature) -> tuple[list[Point], bool]:
    """A feature's geometry as ``(points, closed)`` in the config's own space.

    Same rect-becomes-a-polygon trick as `_desk_polygon`, plus the third shape
    kind features are allowed and desks are not: a `polyline` has no interior,
    so it comes back open and is stroked rather than filled.
    """
    if feature.shape_kind == "rect":
        x, y, w, h = (float(v) for v in feature.shape)  # type: ignore[misc]
        return ([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], True)
    pts: list[Point] = []
    for p in feature.shape:  # type: ignore[union-attr]
        pts.append((float(p[0]), float(p[1])))  # type: ignore[index]
    return (pts, feature.shape_kind != "polyline")


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
    """A room's backdrop, faded to grey, or the reason there isn't one.

    `configured` and `note` are what separate "this room has no image, as
    intended" from "this room's image is broken". Only the second draws a
    placeholder canvas and a banner; see the module docstring.
    """

    array: np.ndarray | None      # (h, w, 3) float in [0, 1], already faded
    path: str                     # what we looked for, for the note
    missing: bool                 # there is no usable bitmap to draw
    note: str = ""                # non-empty ONLY when something went wrong
    configured: bool = False      # rooms.json named an image for this room

    @property
    def failed(self) -> bool:
        """An image was asked for and could not be drawn. The only error state."""
        return self.configured and self.array is None

    @property
    def backdrop(self) -> str:
        """What is actually behind the desk patches, for the contrast maths.

        The placeholder canvas when one is drawn, the page itself when it is
        not. Getting this wrong flips label colours on the pale end of the
        colormap, which is the whole reason `best_text_color` exists.
        """
        return _BG_NEUTRAL if self.failed else _BG_PAPER


def _source_dir(config: Any) -> str:
    src = getattr(config, "source_dir", None)
    if isinstance(src, (str, os.PathLike)):
        return os.fspath(src)
    return "."


def _load_room_image(config: Any, room: Room, fade: float) -> RoomImage:
    """Load and fade the floor plan. Never raises.

    The plan is line art; converting it to grey and washing it out by `fade`
    stops it competing with the colour overlay, which is the actual data.

    A room with no `image` key comes back with `configured=False` and an empty
    note. That is not a failure and must not be reported as one: the shipped
    config has no images at all, and a banner on every page saying so would be
    an error message for the intended state of the system.
    """
    rel = room.image or ""
    path = os.path.join(_source_dir(config), rel) if rel else ""

    if not rel:
        return RoomImage(None, "", True, "", False)
    if not os.path.isfile(path):
        return RoomImage(
            None, path, True,
            f"Floor-plan image not found: {rel} — desks are drawn from the "
            f"rooms.json coordinates only.",
            True,
        )
    try:
        raw = np.asarray(mimage.imread(path))
    except Exception as exc:                      # pragma: no cover - env specific
        return RoomImage(
            None, path, True,
            f"Floor-plan image {rel} could not be read ({type(exc).__name__}); "
            f"desks are drawn from the rooms.json coordinates only.",
            True,
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
            True,
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
    return RoomImage(out, path, False, note, True)


def _backdrop_at(
    image: RoomImage, box: tuple[float, float, float, float], extent: tuple[float, float]
) -> tuple[float, float, float]:
    """Mean colour of the plan under a desk, for the contrast calculation."""
    if image.array is None:
        return _to_rgb(image.backdrop)
    h, w = image.array.shape[0], image.array.shape[1]
    ex_w, ex_h = extent
    if ex_w <= 0 or ex_h <= 0:
        return _to_rgb(image.backdrop)
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
        return _to_rgb(image.backdrop)
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
#: The same colorbar laid under the plan instead of beside it. Used when the
#: room is a wide strip -- see `_new_page`. `_CB_TEXT_BELOW` is the tick row
#: plus the two-line label, which is set horizontally there and so needs
#: height rather than width.
_CB_GAP_BELOW = 0.30
_CB_H = 0.20
_CB_TEXT_BELOW = 0.60
#: How much of the content width the horizontal bar occupies. A colorbar as
#: wide as a 4:1 room is a stripe, not a scale.
_CB_BELOW_FRACTION = 0.52
_CB_BELOW_MIN_W = 2.60
#: Moving the bar underneath costs a band of page height, so it has to buy the
#: plan at least this much more height to be worth doing. Without the test, a
#: room whose panel is already capped at `_MAX_CELL_H` would pay the band and
#: get nothing back.
_CB_BELOW_MIN_GAIN = 1.05
_T_PAD = 0.20
_TITLE_H = 0.30
_SUB_H = 0.24
#: Leading for a wrapped subtitle. The subtitle grows with the number of rooms
#: (it names the per-page and department-wide desk counts), so it cannot be
#: assumed to fit on one line at any particular page width.
_SUB_LINE_H = 0.16
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
    #: "vertical" (beside the plan) or "horizontal" (beneath it). The caller
    #: hands it straight to `_draw_colorbar`, which has to tick the matching
    #: axis and put the end labels at the matching ends.
    cbar_orientation: str = "vertical"


def _content_block(
    aspects: Sequence[float],
    *,
    rows: int,
    cols: int,
    content_w: float,
    title_band: float,
) -> tuple[list[float], float, float]:
    """Row heights, cell width and total content height for a grid of rooms.

    Split out of `_new_page` because the page has to be able to *cost* a layout
    before committing to it: the colorbar can go beside the plan or beneath it,
    and which one is right depends on how tall the plan turns out to be.
    """
    n = max(1, len(aspects))
    cell_w = max((content_w - (cols - 1) * _CELL_GAP_X) / cols, 0.6)
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
    return row_h, cell_w, content_h


def _vertical_bar_height(content_h: float) -> float:
    return min(content_h * 0.62, 3.2)


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
    cbar_label_run_in: float = 0.0,
    reuse: Figure | None = None,
) -> _Page:
    n = max(1, len(aspects))
    rows, cols = _grid_shape(n)
    title_band = _ROOM_TITLE_H if room_titles else 0.0
    printable_w = max(width - _M_L - _M_R, 1.0)

    # --- where does the colorbar go? --------------------------------------
    # Beside the plan by default. But a vertical bar carries its label rotated
    # through 90°, so the label needs as many inches of *bar* as it is long --
    # and a room like the main office (1334x318, better than 4:1) leaves a
    # content block barely two inches tall. The label then overruns the figure
    # and lands on the subtitle. When that would happen, lay the bar under the
    # plan instead: the label sets horizontally, where there is room for it,
    # and the plan gets back the ~1.6in the right-hand block was holding.
    #
    # `cbar_label_run_in` is measured from the label the caller will actually
    # draw, so nothing here assumes how long that text is.
    content_w = max(printable_w - (_CB_GAP + _CB_W + _CB_TEXT), 1.0) if needs_cbar \
        else printable_w
    row_h, cell_w, content_h = _content_block(
        aspects, rows=rows, cols=cols, content_w=content_w, title_band=title_band
    )
    cbar_below = False
    if needs_cbar and _vertical_bar_height(content_h) < cbar_label_run_in:
        wide_row_h, wide_cell_w, wide_content_h = _content_block(
            aspects, rows=rows, cols=cols, content_w=printable_w,
            title_band=title_band,
        )
        # A panel already pinned at `_MAX_CELL_H` gains nothing from the extra
        # width, so it keeps the side bar and lets the rotated label run a
        # little long -- cheaper than an inch of empty band.
        if wide_content_h >= content_h * _CB_BELOW_MIN_GAIN:
            cbar_below = True
            content_w = printable_w
            row_h, cell_w, content_h = wide_row_h, wide_cell_w, wide_content_h
    cbar_band = (_CB_GAP_BELOW + _CB_H + _CB_TEXT_BELOW) if cbar_below else 0.0

    below = (
        _B_PAD
        + len(footer_lines) * _FOOTER_H
        + (_GAP_BEFORE_FOOTER if footer_lines else 0.0)
        + len(caption_lines) * _CAPTION_LINE_H
        + (_GAP_BEFORE_CAPTION if caption_lines else 0.0)
        + legend_rows * _LEGEND_ROW_H
        + (_GAP_BEFORE_LEGEND if legend_rows else 0.0)
    )
    sub_lines = _wrap(subtitle, width - _M_L - _M_R, 9.5) if subtitle else []
    above = _T_PAD + (_TITLE_H if title else 0.0)
    if sub_lines:
        above += _SUB_H + (len(sub_lines) - 1) * _SUB_LINE_H
    above += _GAP_AFTER_TITLE if (title or sub_lines) else 0.0
    height = below + cbar_band + content_h + above

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

    content_bottom = below + cbar_band
    content_top = content_bottom + content_h

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
    if needs_cbar and cbar_below:
        bar_w = min(content_w, max(_CB_BELOW_MIN_W, content_w * _CB_BELOW_FRACTION))
        bar_x = _M_L + (content_w - bar_w) / 2.0
        bar_y = below + _CB_TEXT_BELOW
        cbar_ax = fig.add_axes((fx(bar_x), fy(bar_y), fx(bar_w), fy(_CB_H)))
    elif needs_cbar:
        bar_h = _vertical_bar_height(content_h)
        bar_y = content_bottom + (content_h - bar_h) / 2.0
        bar_x = _M_L + content_w + _CB_GAP
        cbar_ax = fig.add_axes((fx(bar_x), fy(bar_y), fx(_CB_W), fy(bar_h)))

    y = height - _T_PAD
    if title:
        fig.text(fx(_M_L), fy(y), title, ha="left", va="top",
                 fontsize=14.0, color="#111111", fontweight="semibold")
        y -= _TITLE_H
    for line in sub_lines:
        fig.text(fx(_M_L), fy(y), line, ha="left", va="top",
                 fontsize=9.5, color=_MUTED)
        y -= _SUB_LINE_H

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

    return _Page(
        fig, axes, cbar_ax, legend_anchor, axes_width_in,
        "horizontal" if cbar_below else "vertical",
    )


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


# ==========================================================================
# Structural features (decoration only -- see the module docstring)
# ==========================================================================


@dataclass(frozen=True)
class _FeatureStyle:
    """How one `kind` of feature is drawn. Grey, always."""

    edge: str
    lw: float
    face: str | None = None
    ls: Any = "solid"
    alpha: float = 1.0
    #: Fills are deliberately more transparent than the strokes that bound
    #: them. Where there *is* a floor-plan image, an opaque grey box over the
    #: Huddle Room erases the chairs and the door swing the architect drew --
    #: detail the reader came for. The edge carries the shape; the fill only
    #: needs to say "enclosed area, not open floor".
    face_alpha: float = 0.55
    #: Stroke weight when the shape is drawn as a line rather than an area --
    #: an open polyline, or a thin box collapsed to its axis.
    stroke_lw: float = 1.0
    #: "none"      draw the area as given
    #: "add"       area, plus a line down its long axis (a window mullion)
    #: "instead"   a thin box *is* a line; draw only the axis (partitions,
    #:             the zone divider, which is not a physical wall at all)
    centre_line: str = "none"
    #: Only rooms and the outline are named on the map. Labelling every door
    #: "Door" would bury the plan in nine-point grey text.
    label: bool = False


_FEATURE_STYLES: Mapping[str, _FeatureStyle] = {
    "outline": _FeatureStyle(
        edge=_FEAT_LINE_DARK, lw=1.5, face=None, alpha=0.85, label=True,
    ),
    "wall": _FeatureStyle(
        edge=_FEAT_LINE_DARK, lw=0.9, face=_FEAT_FILL_WALL, alpha=0.80,
        face_alpha=0.70, stroke_lw=1.9,
    ),
    "window": _FeatureStyle(
        edge=_FEAT_LINE, lw=0.9, face=_FEAT_FILL_BAND, alpha=0.90,
        face_alpha=0.62, stroke_lw=0.7, centre_line="add",
    ),
    # A closed door shape is drawn as a leaf and a swing arc, not as a box --
    # see `_door_swing`. `face`/`ls` here only apply to the degenerate case of a
    # door given as an open polyline, which has no corner to hinge on.
    "door": _FeatureStyle(
        edge=_FEAT_LINE, lw=0.9, face=_FEAT_FILL_SOFT, alpha=0.90,
        face_alpha=0.50, ls=(0, (2.4, 1.6)), stroke_lw=0.9,
    ),
    "partition": _FeatureStyle(
        edge=_FEAT_LINE, lw=0.8, face=None, alpha=0.75,
        ls=(0, (3.0, 2.0)), stroke_lw=1.0, centre_line="instead",
    ),
    "furniture": _FeatureStyle(
        edge=_FEAT_LINE_SOFT, lw=0.7, face=_FEAT_FILL_SOFT, alpha=0.85,
        face_alpha=0.45, stroke_lw=0.8,
    ),
    "room": _FeatureStyle(
        edge=_FEAT_LINE, lw=1.0, face=_FEAT_FILL_ROOM, alpha=0.85,
        face_alpha=0.55, stroke_lw=1.0, label=True,
    ),
    "divider": _FeatureStyle(
        edge=_FEAT_LINE, lw=1.1, face=None, alpha=0.75,
        ls=(0, (6.0, 4.0)), stroke_lw=1.2, centre_line="instead",
    ),
}

#: How the caption names each kind, so it can list what is actually drawn
#: rather than what a room *might* contain. Ordered as in `_FEATURE_STYLES`;
#: the caption keeps that order, which is stable and therefore deterministic.
_FEATURE_NOUNS: Mapping[str, str] = {
    "outline": "the room outline",
    "wall": "walls",
    "window": "windows",
    "door": "doors",
    "partition": "partitions",
    "furniture": "furniture",
    "room": "side-rooms",
    "divider": "zone dividers",
}


def _feature_phrase(rooms: Sequence[Room]) -> str:
    """"the room outline, windows and doors" — what is on this page, in order.

    Falls back to the bare word "structure" for a kind with no noun, which is
    the same thing an unrecognised kind is drawn as.
    """
    present: list[str] = []
    for kind in _FEATURE_NOUNS:
        if any(f.kind == kind for room in rooms for f in room.features):
            present.append(_FEATURE_NOUNS[kind])
    if any(
        f.kind not in _FEATURE_NOUNS for room in rooms for f in room.features
    ):
        present.append("other structure")
    if not present:
        return "structure"
    if len(present) == 1:
        return present[0]
    return ", ".join(present[:-1]) + " and " + present[-1]


#: An unrecognised `kind` still draws -- as generic structure, dotted so it is
#: visibly "something the renderer did not recognise" rather than silently
#: impersonating furniture. `validate.KNOWN_FEATURE_KINDS` already warns about
#: it at load time, so this is the second half of a warning, not a new one.
_FEATURE_STYLE_GENERIC = _FeatureStyle(
    edge=_FEAT_LINE_SOFT, lw=0.8, face=_FEAT_FILL_SOFT, alpha=0.80,
    face_alpha=0.45, ls=(0, (1.8, 1.8)), stroke_lw=0.8,
)

#: A box this much longer than it is wide is really a line: a hanging partition
#: or a painted zone boundary, not an area you could stand in.
_THIN_RATIO = 0.34

#: Which corner of a door's box the leaf is hinged on, as (x_sign, y_sign)
#: offsets into the bounding box in *drawing* coordinates -- x grows right and
#: y grows DOWN, because the axes are set up image-style (`set_ylim(h, 0)`).
#: So "s" is y1 (the bottom of the box on the page) and "n" is y0.
#:
#: The values are the direction the arc sweeps *away* from the hinge: the leaf
#: runs vertically into the box, the arc lands on the horizontal jamb.
#: Deliberately the same table, corner for corner, as `DOOR_SWINGS` in
#: frontend/JsMap.html. Keeping the two in step is what makes the report and
#: the form agree about which way a door opens; change one and change both.
_DOOR_HINGES: Mapping[str, tuple[int, int]] = {
    "sw": (+1, -1),   # hinge bottom-left, leaf up, arc to the right
    "se": (-1, -1),   # hinge bottom-right, leaf up, arc to the left
    "nw": (+1, +1),   # hinge top-left, leaf down, arc to the right
    "ne": (-1, +1),   # hinge top-right, leaf down, arc to the left
}

#: The default when `swing` is absent, matching validate.DOOR_SWINGS' documented
#: default and what both renderers used before `swing` existed.
_DOOR_SWING_DEFAULT = "sw"

#: Segments in the quarter-circle. Fixed, so the vertex list -- and therefore
#: the output bytes -- cannot drift (invariant I3).
_DOOR_ARC_SEGMENTS = 24


def _door_swing(
    pts: Sequence[Point], swing: str
) -> tuple[tuple[Point, Point], list[Point]] | None:
    """A door's leaf and swing arc, or None if the shape cannot carry one.

    Returns ``((hinge, leaf_tip), arc_points)``. The leaf runs from the hinge
    corner straight across the box; the arc sweeps a quarter circle of the same
    radius from the leaf tip round to the jamb, which is how a door is drawn on
    a plan and the only thing on the map that says which way it opens.

    Radius is ``min(width, height)`` so the whole swing stays inside the box the
    coordinator drew, whatever its proportions.
    """
    if len(pts) < 3:
        return None
    x0, y0, x1, y1 = _bbox(pts)
    w, h = x1 - x0, y1 - y0
    radius = min(w, h)
    if radius <= 0.0:
        return None

    key = (swing or "").strip().lower()
    sx, sy = _DOOR_HINGES.get(key, _DOOR_HINGES[_DOOR_SWING_DEFAULT])
    hx = x0 if sx > 0 else x1          # "w" hinges on the left edge, "e" on the right
    hy = y1 if sy < 0 else y0          # "s" hinges on the bottom edge, "n" on the top

    tip = (hx, hy + sy * radius)
    arc: list[Point] = []
    for i in range(_DOOR_ARC_SEGMENTS + 1):
        t = (math.pi / 2.0) * (i / _DOOR_ARC_SEGMENTS)
        arc.append((hx + sx * radius * math.sin(t), hy + sy * radius * math.cos(t)))
    return (((hx, hy), tip), arc)


def _thin_axis(pts: Sequence[Point]) -> tuple[Point, Point] | None:
    """The long centre line of a thin box, or None if the shape has bulk."""
    if len(pts) < 2:
        return None
    x0, y0, x1, y1 = _bbox(pts)
    w, h = x1 - x0, y1 - y0
    longest = max(w, h)
    if longest <= 0.0:
        return None
    if min(w, h) > _THIN_RATIO * longest:
        return None
    if w < h:
        mid = (x0 + x1) / 2.0
        return ((mid, y0), (mid, y1))
    mid = (y0 + y1) / 2.0
    return ((x0, mid), (x1, mid))


def _span_at_y(poly: Sequence[Point], y: float, x: float) -> tuple[float, float]:
    """Horizontal extent of `poly` at height `y`, in the span containing `x`.

    A rectangle returns its full width, so nothing changes for the ordinary
    case. It matters for the shapes that do not fill their bounding box: the
    triangular alcove in the senior office is half the width of its bbox where
    its label sits, and sizing the label against the bbox would push the text
    out through the diagonal wall.
    """
    x_lo, _, x_hi, _ = _bbox(poly)
    xs: list[float] = []
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if (y0 > y) != (y1 > y) and y1 != y0:
            xs.append(x0 + (y - y0) / (y1 - y0) * (x1 - x0))
    xs.sort()
    for i in range(0, len(xs) - 1, 2):
        if xs[i] - 1e-9 <= x <= xs[i + 1] + 1e-9:
            return (xs[i], xs[i + 1])
    return (x_lo, x_hi)


def _label_layout(
    label: str, w_pt: float, h_pt: float, lo: float, hi: float
) -> tuple[list[str], float]:
    """Wrap `label` into the line count that lets it be drawn largest.

    Deterministic: a fixed candidate list, `textwrap` on each, and a strict
    improvement test so ties keep the fewest lines. Words are never split --
    "Dept Storage" broken as "Dept S / torage" is worse than no label at all.
    """
    text = " ".join(label.split())
    if not text:
        return ([], lo)
    best_lines = [text]
    best_size = 0.0
    for n_lines in (1, 2, 3):
        wrap_at = max(1, math.ceil(len(text) / n_lines))
        lines = textwrap.wrap(
            text, width=wrap_at, break_long_words=False, break_on_hyphens=False
        ) or [text]
        size = _fit_font_size([(lines, w_pt, h_pt)], lo, hi)
        if size > best_size + 1e-9:
            best_size, best_lines = size, lines
    return (best_lines, best_size)


def _draw_feature_label(
    ax: Axes, feature: Feature, pts: Sequence[Point], ppp: float
) -> None:
    """Name a room (or the room outline) inside its own shape.

    The whole point of the exercise: with the floor-plan image missing, this
    text is the only thing telling the reader that the box behind desk 23 is the
    Quiet Grad Room. So it goes *inside* the shape, small and grey enough to
    stay decoration, on a translucent plate so it survives whatever line art is
    underneath it.
    """
    x0, y0, x1, y1 = _bbox(pts)
    w_px, h_px = x1 - x0, y1 - y0
    if w_px <= 0.0 or h_px <= 0.0:
        return

    if feature.kind == "outline":
        # The centre of an outline is the middle of the desks. Tuck the name
        # into the top-left corner instead, in the strip between the wall and
        # the first row -- the one part of a room plan that is reliably empty.
        lines, size = _label_layout(
            feature.label, 0.45 * w_px * ppp, 0.085 * h_px * ppp, 5.5, 7.5
        )
        inset = 0.012 * max(w_px, h_px)
        x, y = x0 + inset, y0 + inset
        ha, va = "left", "top"
    else:
        x, y = label_anchor(pts)
        span_lo, span_hi = _span_at_y(pts, y, x)
        avail = min(w_px, max(0.0, span_hi - span_lo))
        lines, size = _label_layout(
            feature.label, 0.90 * avail * ppp, 0.60 * h_px * ppp, 4.6, 7.5
        )
        ha, va = "center", "center"
    if not lines:
        return

    ax.text(
        x, y, "\n".join(lines),
        ha=ha, va=va, fontsize=size, color=_FEAT_TEXT,
        linespacing=1.18, zorder=_Z_FEATURE_TEXT,
        bbox=dict(boxstyle="round,pad=0.22", facecolor=_FEAT_TEXT_BG,
                  edgecolor="none", alpha=0.70),
    )


def _draw_door(
    ax: Axes,
    style: _FeatureStyle,
    swing: tuple[tuple[Point, Point], list[Point]],
) -> None:
    """Leaf plus swing arc, in the door style. Two strokes, no fill.

    The leaf is the heavier of the two because it is the door; the arc is a
    hairline because it is the space the door needs, not a thing in the room.
    Neither is filled: a shaded quarter-disc would read as furniture.
    """
    (hinge, tip), arc = swing
    ax.add_line(
        Line2D(
            [p[0] for p in arc], [p[1] for p in arc],
            color=style.edge, linewidth=style.stroke_lw * 0.7,
            linestyle="solid", alpha=style.alpha * 0.75,
            solid_capstyle="butt", zorder=_Z_FEATURE_LINE,
        )
    )
    ax.add_line(
        Line2D(
            [hinge[0], tip[0]], [hinge[1], tip[1]],
            color=style.edge, linewidth=style.lw,
            linestyle="solid", alpha=style.alpha,
            solid_capstyle="butt", zorder=_Z_FEATURE_LINE,
        )
    )


def _draw_features(
    ax: Axes,
    *,
    room: Room,
    sx: float,
    sy: float,
    ppp: float,
    notes: list[str],
) -> None:
    """Draw a room's structure under everything else.

    Iterates `room.features` in rooms.json order — no sorting needed and no set
    anywhere, so the draw order (and therefore the output bytes) is fixed by the
    config file.
    """
    unknown: list[str] = []
    for feature in room.features:
        raw, closed = _feature_points(feature)
        if len(raw) < 2:
            # A one-point polyline has nothing to draw. Not worth a note: the
            # validator rejects it long before the figure is built.
            continue
        pts = _scaled(raw, sx, sy)
        style = _FEATURE_STYLES.get(feature.kind)
        if style is None:
            style = _FEATURE_STYLE_GENERIC
            if feature.kind not in unknown:
                unknown.append(feature.kind)

        # A door is the one kind whose box is not the thing to draw. The box
        # says where the opening is; the leaf and the arc say which way it
        # opens, which is the part a reader can actually use -- and it is why
        # `swing` exists in rooms.json. Falls through to the generic path if the
        # shape has no interior to hinge in (an open polyline).
        if feature.kind == "door" and closed:
            swing = _door_swing(pts, feature.swing)
            if swing is not None:
                _draw_door(ax, style, swing)
                continue

        # Only an area can be collapsed to (or annotated with) a centre line.
        # An open polyline already *is* one; drawing its axis on top of it
        # would just stroke the same dashes twice.
        axis = _thin_axis(pts) if (closed and style.centre_line != "none") else None
        collapse = axis is not None and style.centre_line == "instead"

        if closed and not collapse:
            if style.face is not None:
                ax.add_patch(
                    MplPolygon(
                        pts, closed=True, facecolor=style.face, edgecolor="none",
                        alpha=style.face_alpha, zorder=_Z_FEATURE_FILL,
                    )
                )
            ax.add_patch(
                MplPolygon(
                    pts, closed=True, facecolor="none", edgecolor=style.edge,
                    linewidth=style.lw, linestyle=style.ls, alpha=style.alpha,
                    joinstyle="round", zorder=_Z_FEATURE_LINE,
                )
            )
        elif not closed:
            ax.add_line(
                Line2D(
                    [p[0] for p in pts], [p[1] for p in pts],
                    color=style.edge, linewidth=style.stroke_lw,
                    linestyle=style.ls, alpha=style.alpha,
                    solid_capstyle="round", zorder=_Z_FEATURE_LINE,
                )
            )

        if axis is not None:
            ax.add_line(
                Line2D(
                    [axis[0][0], axis[1][0]], [axis[0][1], axis[1][1]],
                    color=style.edge, linewidth=style.stroke_lw,
                    # A mullion is a real line on a real window; a collapsed
                    # partition or zone boundary keeps the kind's dashes.
                    linestyle=style.ls if collapse else "solid",
                    alpha=style.alpha, solid_capstyle="butt",
                    zorder=_Z_FEATURE_LINE,
                )
            )

        if style.label and feature.label.strip():
            _draw_feature_label(ax, feature, pts, ppp)

    if unknown:
        notes.append(
            f"{room.id}: feature kind(s) {', '.join(sorted(unknown))} are not "
            f"recognised and were drawn as generic structure."
        )


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
    show_features: bool,
    annotate: bool,
    show_occupants: str,
    has_solution: bool,
    notes: list[str],
) -> tuple[list[Patch], list[ZoneId]]:
    """Draw one room.

    Returns the legend handles this room needs and the zone ids it contains,
    both in rooms.json order so a caller assembling a multi-room page can merge
    them deterministically. Structural features get no handle: see the module
    docstring.
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
    # Three states, and only the middle one is a problem worth a banner:
    #   image loaded          -> draw it
    #   image configured but
    #     unusable            -> placeholder canvas + a note saying which file
    #   no image configured   -> nothing. Plain paper, no grey canvas, no note.
    # The last is the shipped configuration: rooms.json is a schematic and the
    # desk spacing is the map, so there is nothing missing to announce.
    if image.array is not None:
        ax.imshow(
            image.array,
            extent=(0.0, width_px, height_px, 0.0),
            zorder=0,
            interpolation="antialiased",
        )
    elif image.failed:
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

    # Points of type per data unit. Needed by anything that has to fit text
    # into a shape, which starts with the feature labels.
    ppp = (axes_width_in * 72.0) / width_px if width_px else 1.0

    # --- structure ------------------------------------------------------
    # Under the zone tint, the zone halos and the desks: decoration first, so
    # nothing structural can sit on top of a datum.
    if show_features:
        _draw_features(ax, room=room, sx=sx, sy=sy, ppp=ppp, notes=notes)

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
    show_features: bool = True,
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

        Either way the scale is computed **once, over every desk in the
        department**, and not per room. `rooms=` narrows what is *drawn*, never
        what the colours mean, so a reader flipping between the main office and
        the senior office is comparing like with like. Two rooms on two
        independently-stretched scales would make the least-wanted desk in a
        popular room look exactly like the least-wanted desk in an ignored one.
        The caption states which regime is in force.
    zone_tint
        ``"auto"`` tints each zone's region when the zones do not interleave,
        and falls back to the per-desk coloured outline when they do.
    show_features
        Draw the structural features from `rooms.json` — walls, doors, windows,
        the room outline, named side-rooms. On by default. They are decoration:
        grey, underneath every desk, and excluded from the metric, the colour
        scale and the legend.
    width
        Figure width in inches. The **height is chosen to fit**, never given:
        the plan is drawn at its true aspect ratio and the page grows around
        it, so a caller placing this on fixed paper should size by width and
        re-home (see `report._fit_map_figure`).

        The colorbar moves to suit the room's shape. A long, narrow room —
        the main office is better than 4:1 — leaves too little height for a
        vertical bar to carry its rotated label, so the bar goes underneath
        the plan instead and the plan takes the full printable width. Nothing
        about that decision is hard-coded to a particular room: it is measured
        from the room's own aspect ratio and the real label text.

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

    # --- colour scale ------------------------------------------------------
    # Deliberately computed from `stats`, which covers every desk in every room
    # in rooms.json, *not* from the subset in `wanted`. So the scale is shared:
    # narrowing the drawing to one room does not restretch the colours, and one
    # page of a multi-room report can be read against the next. The caption
    # says so out loud, because a reader cannot tell by looking.
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
    # The same counts for the rooms actually on this page. They diverge from
    # the department-wide totals whenever `rooms=` narrows the selection, which
    # is every page of the report — it draws one room per page. Saying "40 of
    # 41 desks" on a page showing 31 of them is just wrong.
    drawn_ids = [r.id for r in wanted]
    page_desks = [d for d in stats.desks if d.room_id in drawn_ids]
    n_page_all = len(page_desks)
    n_page_pool = sum(1 for d in page_desks if d.in_pool)
    partial = n_page_all != n_all
    n_rooms_cfg = len(rooms_cfg.rooms)
    # English, not arithmetic: "all 2 rooms" is not a sentence. The count still
    # comes from the config, so five rooms next year reads correctly too.
    rooms_phrase = "both rooms" if n_rooms_cfg == 2 else f"all {n_rooms_cfg} rooms"
    has_features = show_features and any(r.features for r in wanted)
    # Name the structure that is actually on the page. Listing "walls, doors,
    # windows, rooms" under a plan whose only structure is an outline, a window
    # and a door sends the reader hunting for walls that are not there.
    feature_phrase = _feature_phrase(wanted) if has_features else ""

    if title is None:
        if len(wanted) == 1:
            title = f"Desk popularity — {wanted[0].label}"
        else:
            title = "Desk popularity"
    if subtitle is None:
        if partial:
            desks_bit = (
                f"{n_page_pool} of {n_page_all} desks on this page in the pool, "
                f"{n_pool} of {n_all} across {rooms_phrase}"
            )
        else:
            desks_bit = f"{n_pool} of {n_all} desks in the pool"
        subtitle = (
            f"mean rank received from {stats.n_people} "
            f"{'student' if stats.n_people == 1 else 'students'} in the pool  ·  "
            f"K = {k} ranked choices each  ·  {desks_bit}"
        )
        # With one room the span belongs here, next to the other facts about
        # the figure. With several it belongs in the caption, attached to the
        # sentence that says the span is shared — repeating a shared range once
        # per page invites the reader to assume it was computed per page.
        if (
            norm is not None
            and n_rooms_cfg == 1
            and (lo > 1.0 + 1e-9 or hi < k + 1 - 1e-9)
        ):
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
        # Both branches: what the grey shapes are, and what a colour means
        # across pages. Features get no legend swatch — the legend is a key to
        # states a desk can be *in*, and a wall is not one of them — so this
        # sentence is the only thing that names them.
        if has_features:
            caption += (
                f" Grey shapes are structure — {feature_phrase} — drawn "
                f"for orientation only, never shaded."
            )
        if norm is not None and n_rooms_cfg > 1:
            caption += (
                f" One colour scale ({lo:.1f}–{hi:.1f} of a possible 1–{k + 1}) is "
                f"shared by {rooms_phrase}, so the same shade means the same mean "
                f"rank in each."
            )
    if footer is None:
        coord = rooms_cfg.coord_space
        # Only rooms that actually name an image are worth a provenance line.
        # "main_office: (none)" was reporting the normal case as if it were a
        # finding; the geometry line already says where the drawing came from.
        img_bits = [f"{room.id}: {room.image}" for room in wanted if room.image]
        footer = f"geometry from config/rooms.json ({coord} coordinates)"
        if img_bits:
            footer += "  ·  " + "  ·  ".join(img_bits)

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
            cbar_label_run_in=_cbar_label_run(stats),
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
                show_features=show_features,
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
            _draw_colorbar(
                page.fig, page.cbar_ax, cmap_obj, norm, stats,
                page.cbar_orientation,
            )

        figures.append(page.fig)
        slugs.append(group[0].id if group else "empty")

    paths = _emit(figures, target, dpi=dpi, title=title, slugs=slugs)
    return FigureResult(tuple(figures), paths, stats, tuple(notes))


#: Point size of the colorbar's axis label. Named because the layout has to
#: measure the label before the bar exists (see `_cbar_label_run`).
_CB_LABEL_FS = 8.5


def _cbar_label(stats: PopularityStats) -> str:
    """The colorbar's axis label. Two lines, both derived from the stats."""
    return (
        f"mean rank across all {stats.n_people} students\n"
        f"(1 = everyone's first choice, "
        f"{stats.unranked_rank} = nobody ranked it)"
    )


def _cbar_label_run(stats: PopularityStats) -> float:
    """Inches the label occupies *along* the bar when it is set vertically.

    A rotated label is as long as its longest line, and the bar it labels has
    to be at least that tall or the text runs off both ends of it. Measured
    from the real string, at the size it is really drawn, so a bigger cohort
    (a longer "all N students") is accounted for rather than assumed away.
    """
    longest = max((len(line) for line in _cbar_label(stats).split("\n")), default=0)
    return longest * 0.52 * _CB_LABEL_FS / 72.0


def _draw_colorbar(
    fig: Figure,
    cax: Axes,
    cmap: mcolors.Colormap,
    norm: mcolors.Normalize,
    stats: PopularityStats,
    orientation: str = "vertical",
) -> None:
    horizontal = orientation == "horizontal"
    sm = ScalarMappable(norm=norm, cmap=cmap)
    cbar = fig.colorbar(sm, cax=cax, orientation=orientation)
    if horizontal:
        # Low mean rank -- the most wanted -- reads to the left, which is where
        # a left-to-right reader starts. No inversion: vmin is already there.
        axis = cbar.ax.xaxis
    else:
        # Rank 1 at the top: "top of the list" should be at the top of the bar.
        cbar.ax.invert_yaxis()
        axis = cbar.ax.yaxis
    cbar.outline.set_linewidth(0.6)
    cbar.outline.set_edgecolor("#4a4a4a")
    axis.set_major_locator(mticker.MaxNLocator(nbins=6, steps=[1, 2, 2.5, 5, 10]))
    axis.set_major_formatter(mticker.FormatStrFormatter("%.1f"))
    cbar.ax.tick_params(labelsize=8.5, width=0.6, length=3.0, pad=2.0)
    cbar.set_label(
        _cbar_label(stats),
        fontsize=_CB_LABEL_FS,
        labelpad=4,
        linespacing=1.4,
    )
    if horizontal:
        # The ends of a horizontal bar are its left and right, so the two
        # captions move with it rather than staying above and below.
        cax.text(
            -0.015, 0.5, "more wanted", transform=cax.transAxes,
            ha="right", va="center", fontsize=8.0, color=_MUTED, style="italic",
        )
        cax.text(
            1.015, 0.5, "less wanted", transform=cax.transAxes,
            ha="left", va="center", fontsize=8.0, color=_MUTED, style="italic",
        )
        return
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

    Structural features never appear here in any form. Nothing in this function
    reads `room.features`: every bar comes from a `DeskStat`, and `DeskStat`s
    are made from desks. A wall cannot be anybody's first choice.
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
