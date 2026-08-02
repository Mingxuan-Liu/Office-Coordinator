"""Statistical figures for the results PDFs.

Every function here draws exactly one figure onto an ``Axes`` (or, where the
content has to paginate, onto a ``Figure``) that the caller owns. **Nothing in
this module touches the filesystem.** ``report.py`` creates the figures, calls
these, and saves them; that split keeps the drawing code testable and keeps the
one place that writes bytes to disk small enough to audit.

House style
-----------
Restrained and print-first: no 3-D, no chartjunk, no rainbow colormaps.
Rank is an ordered quantity, so it gets a *sequential* map (viridis, truncated
away from its pale extreme). Nothing in here uses a diverging map, because
nothing in here is a genuinely diverging quantity. Series are direct-labelled
wherever direct labelling is clearer than a legend. Every figure carries a
one-sentence caption *inside* the figure explaining how to read it, because the
PDF is read by people with no one standing next to them to explain it.

Determinism (SPEC I3)
---------------------
The PDF has to be byte-identical across runs, so:

* ``apply_house_style()`` pins ``pdf.fonttype`` and ``svg.hashsalt``. It should
  be called once by ``report.py`` before any figure is drawn.
* ``PDF_METADATA`` is the metadata dict to hand to ``PdfPages``/``savefig``.
  It sets **every** key matplotlib understands explicitly and sets the two
  clock-derived ones to ``None`` so matplotlib omits them rather than stamping
  the current time.
* No wall-clock value is read anywhere in this module. Nothing here formats a
  date. The provenance page (``provenance.py``) is the only place a timestamp
  is allowed, and it comes from the inputs, not from ``datetime.now()``.
* No ``set`` is ever iterated. Where set semantics are wanted, the code builds
  a ``dict``/list and sorts before iterating.
* **No sampling happens in this module at all.** If a future figure needs a
  random draw (jittered strip plots being the obvious temptation), it must use
  ``deskmatch.scoring.make_rng(seed_string + "::<purpose>")`` and nothing else.
  A bare ``np.random`` call here would silently break I3.
* Text is only ever laid out from data, never from iteration order of a hash
  container, and font sizes are computed from figure geometry rather than from
  a renderer measurement, so no rasterisation state leaks into the layout.

Privacy (SPEC §7.2)
-------------------
``FIGURE_AUDIENCE`` records which figures may go in the public PDF. The two
sensitive ones are called out at the top of the figure itself as well, so a
page that leaks cannot be mistaken for a public one at a glance:

* ``rank_received_by_person(..., anonymize=False)`` — names against ranks.
* ``preference_matrix`` — the whole preference table. Coordinator only, always.

``rank_received_by_person(..., anonymize=True)`` is a pure function of
``solution.rank_histogram()``. It never reads a name, an email, or the row
order of the solution, so it cannot leak anything the aggregate bar chart has
not already published. That is asserted in the code, not just claimed here.
"""

from __future__ import annotations

import math
import textwrap
from typing import Any, Mapping, Sequence

import matplotlib

matplotlib.use("Agg")  # explicit; report.py renders headless and must stay headless

import matplotlib as mpl  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402
from matplotlib.colors import BoundaryNorm, ListedColormap, to_rgb  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch, Rectangle  # noqa: E402
from matplotlib.ticker import FuncFormatter, MaxNLocator  # noqa: E402

from .types import Problem, Solution  # noqa: E402

__all__ = [
    "PDF_METADATA",
    "FIGURE_AUDIENCE",
    "apply_house_style",
    "rank_colors",
    "rank_distribution",
    "cumulative_satisfaction",
    "baseline_comparison",
    "curve_sensitivity",
    "seed_sensitivity",
    "rank_received_by_person",
    "assignment_table",
    "assignment_table_page_count",
    "preference_matrix",
    "preference_matrix_page_count",
]


# --------------------------------------------------------------------------
# House style
# --------------------------------------------------------------------------

_INK = "#1b1b1b"
_MUTED = "#6f6f6f"
_FAINT = "#b4b4b4"
_GRID = "#e5e5e5"
_ZEBRA = "#f4f4f4"
_RULE = "#111111"
_WARN = "#8a2b06"
_NEUTRAL_FILL = "#dedede"

#: Metadata for ``PdfPages(..., metadata=PDF_METADATA)`` or ``savefig``.
#:
#: All nine keys matplotlib's PDF backend understands are set explicitly. Left
#: to itself matplotlib stamps ``CreationDate`` with ``datetime.today()`` and
#: fills ``Creator``/``Producer`` with its own version string; the first of
#: those alone makes every run differ and I3 unverifiable. Setting a key to
#: ``None`` makes matplotlib drop it, which is how the two date fields are
#: removed rather than frozen at some arbitrary instant.
#:
#: (Matplotlib also honours ``SOURCE_DATE_EPOCH`` for ``CreationDate``. We do
#: not rely on it: an environment variable that has to be set correctly is a
#: reproducibility trap, and an explicit ``None`` needs no cooperation from
#: whoever runs the solve.)
PDF_METADATA: dict[str, Any] = {
    "Title": "deskmatch results",
    "Author": "deskmatch",
    "Subject": "Graduate office desk assignment",
    "Keywords": "deskmatch desk assignment reproducible",
    "Creator": "deskmatch",
    "Producer": "deskmatch (matplotlib)",
    "CreationDate": None,
    "ModDate": None,
    "Trapped": "False",
}

#: Which report each figure may appear in. Consulted by report.py; also the
#: single place to look when adding a figure.
FIGURE_AUDIENCE: Mapping[str, str] = {
    "rank_distribution": "public",
    "cumulative_satisfaction": "public",
    "baseline_comparison": "public",
    "curve_sensitivity": "public",
    "seed_sensitivity": "public",
    "rank_received_by_person(anonymize=True)": "public",
    "rank_received_by_person(anonymize=False)": "coordinator",
    "assignment_table": "public",
    "preference_matrix": "coordinator",
}

_HOUSE_RCPARAMS: dict[str, Any] = {
    # DejaVu Sans ships with matplotlib, so it resolves to the same font file on
    # every machine. Naming a system font here (Helvetica, Arial) would make the
    # embedded glyphs machine-dependent and break I3.
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans"],
    "mathtext.fontset": "dejavusans",
    "font.size": 9.0,
    "axes.titlesize": 11.0,
    "axes.titleweight": "regular",
    "axes.titlelocation": "left",
    "axes.titlepad": 9.0,
    "axes.labelsize": 9.0,
    "axes.labelcolor": _INK,
    "axes.edgecolor": "#4a4a4a",
    "axes.linewidth": 0.7,
    "axes.facecolor": "white",
    "axes.grid": False,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "xtick.color": _INK,
    "ytick.color": _INK,
    "xtick.labelsize": 8.0,
    "ytick.labelsize": 8.0,
    "xtick.major.width": 0.7,
    "ytick.major.width": 0.7,
    "xtick.major.size": 3.0,
    "ytick.major.size": 3.0,
    "grid.color": _GRID,
    "grid.linewidth": 0.6,
    "legend.frameon": False,
    "legend.fontsize": 8.0,
    "legend.handlelength": 1.4,
    "legend.handletextpad": 0.6,
    "legend.labelspacing": 0.45,
    "figure.facecolor": "white",
    "savefig.facecolor": "white",
    "figure.dpi": 150.0,
    "savefig.dpi": 150.0,
    "figure.max_open_warning": 0,
    # Determinism-relevant:
    "pdf.fonttype": 42,      # embed a TrueType subset; keeps text selectable
    "ps.fonttype": 42,
    "pdf.compression": 6,
    "svg.hashsalt": "deskmatch",
    "path.simplify": True,
    "path.simplify_threshold": 1.0 / 9.0,
}


def apply_house_style() -> None:
    """Pin the global rcParams the figures assume. Idempotent.

    ``report.py`` should call this once, before creating any figure. The two
    entries that genuinely have to be global are ``pdf.fonttype`` and
    ``svg.hashsalt``; the rest is typography that the drawing functions also
    set per-artist, so a caller who forgets this gets figures that are plainer
    but still correct and still deterministic.
    """
    mpl.rcParams.update(_HOUSE_RCPARAMS)


def rank_colors(k: int) -> list[tuple[float, float, float, float]]:
    """One colour per rank, 1..K, from viridis.

    Rank is ordered, so the map is sequential. The ends are trimmed off:
    viridis' extremes are a very dark navy and a pale yellow, and the pale
    yellow has too little contrast against white paper to carry a bar.
    """
    if k <= 0:
        return []
    cmap = mpl.colormaps["viridis"]
    if k == 1:
        return [cmap(0.30)]
    return [cmap(0.12 + 0.66 * i / (k - 1)) for i in range(k)]


def _series_colors(n: int) -> list[tuple[float, float, float]]:
    """Colours for *scenario* series (curves, seeds).

    Scenarios are not ordered, but "primary" versus "alternative" is a real
    distinction, so the primary gets an ink-dark slate and the alternatives sit
    in the middle of viridis where they stay distinguishable in greyscale.
    """
    if n <= 0:
        return []
    out = [to_rgb("#20364a")]
    if n == 1:
        return out
    cmap = mpl.colormaps["viridis"]
    for i in range(n - 1):
        t = 0.32 + 0.36 * (i / max(n - 2, 1)) if n > 2 else 0.50
        out.append(tuple(cmap(t)[:3]))
    return out


def _darken(color: Any, factor: float = 0.62) -> tuple[float, float, float]:
    r, g, b = to_rgb(color)
    return (r * factor, g * factor, b * factor)


# --------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------


def _style_axes(ax: Axes, *, grid_axis: str | None = "y") -> None:
    """Apply the house look to one Axes without mutating global state."""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_linewidth(0.7)
        ax.spines[side].set_color("#4a4a4a")
    ax.tick_params(labelsize=8.0, width=0.7, length=3.0, color="#4a4a4a",
                   labelcolor=_INK)
    if grid_axis:
        ax.grid(True, axis=grid_axis, color=_GRID, linewidth=0.6, zorder=0)
        ax.set_axisbelow(True)
    else:
        ax.grid(False)


def _axes_width_in(ax: Axes) -> float:
    fig_w = float(ax.figure.get_size_inches()[0])
    return max(fig_w * float(ax.get_position().width), 1.0)


#: Mean glyph advance for DejaVu Sans as a fraction of the point size. Measured
#: rather than guessed: too small and every wrapped block overruns its column.
_EM_WIDTH = 0.60


def _fit_chars(width_in: float, size: float, *, lo: int = 8, hi: int = 190) -> int:
    """How many characters of `size`-point text fit across `width_in` inches."""
    chars = int(width_in * 72.0 / (_EM_WIDTH * size))
    return int(min(max(chars, lo), hi))


def _wrap_for(text: str, width_in: float, size: float, *, lo: int = 46,
              hi: int = 190) -> str:
    """Wrap `text` to the character count that fits `width_in` inches at `size`.

    Computed from figure geometry rather than measured with a renderer: a
    renderer measurement would drag rasterisation state into the layout, and
    the layout has to be identical on every machine.
    """
    return textwrap.fill(text, _fit_chars(width_in, size, lo=lo, hi=hi))


def _people(n: int) -> str:
    """'1 person' / '35 people'. N=1 is a real case, not a rounding error."""
    return "1 person" if n == 1 else f"{_n(n)} people"


def _caption(ax: Axes, text: str, *, dy: float | None = None,
             size: float = 7.2) -> None:
    """The one-sentence 'how to read this' line, pinned under the axes."""
    if dy is None:
        dy = -46.0 if ax.get_xlabel() else -30.0
    ax.annotate(
        _wrap_for(text, _axes_width_in(ax), size),
        xy=(0.0, 0.0), xycoords="axes fraction",
        xytext=(0.0, dy), textcoords="offset points",
        ha="left", va="top", fontsize=size, color=_MUTED, style="italic",
        linespacing=1.35, annotation_clip=False, zorder=6,
    )


def _note_block(ax: Axes, lines: Sequence[str], *, dy: float = -46.0,
                size: float = 7.8) -> float:
    """A short block of supporting numbers between the xlabel and the caption.

    Returns the offset (in points) at which the caption should start, so the
    two never overlap regardless of how many lines the block has.
    """
    if not lines:
        return dy
    width_in = _axes_width_in(ax)
    wrapped = [_wrap_for(line, width_in, size) for line in lines]
    body = "\n".join(wrapped)
    ax.annotate(
        body,
        xy=(0.0, 0.0), xycoords="axes fraction",
        xytext=(0.0, dy), textcoords="offset points",
        ha="left", va="top", fontsize=size, color=_INK,
        linespacing=1.45, annotation_clip=False, zorder=6,
    )
    n_lines = sum(1 + w.count("\n") for w in wrapped)
    return dy - n_lines * size * 1.45 - 6.0


def _coordinator_banner(fig: Figure, y: float = 0.988) -> None:
    fig.text(
        0.5, y,
        "COORDINATOR COPY — contains individual preferences. Not for circulation.",
        ha="center", va="top", fontsize=7.6, color=_WARN, weight="bold",
        zorder=10,
    )


def _empty_state(ax: Axes, headline: str, body: str = "",
                 caption: str | None = None) -> None:
    """What every figure does instead of emitting a blank page."""
    ax.set_axis_off()
    ax.text(0.5, 0.60, headline, ha="center", va="center", fontsize=11.0,
            color=_INK, transform=ax.transAxes)
    if body:
        ax.text(0.5, 0.44, _wrap_for(body, _axes_width_in(ax), 8.4),
                ha="center", va="top", fontsize=8.4, color=_MUTED,
                linespacing=1.5, transform=ax.transAxes)
    if caption:
        _caption(ax, caption, dy=-14.0)


def _ordinal(n: int) -> str:
    """'1st', '2nd', '13th', '21st'. Works for any K, which is the point."""
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _n(value: float | int) -> str:
    """Thousands-separated integer. ``format`` is locale-independent here."""
    return f"{int(round(value)):,}"


def _pct(x: float, digits: int = 1) -> str:
    return f"{100.0 * x:.{digits}f}%"


def _pct_formatter(digits: int = 0) -> FuncFormatter:
    return FuncFormatter(lambda v, _pos: f"{100.0 * v:.{digits}f}%")


def _truncate(text: str, budget: int) -> str:
    if budget <= 1 or len(text) <= budget:
        return text
    return text[: max(budget - 1, 1)].rstrip() + "…"


def _hist_and_n(solution: Solution) -> tuple[tuple[int, ...], int]:
    hist = solution.rank_histogram()
    return hist, int(sum(hist))


def _mean_rank(hist: Sequence[int]) -> float:
    total = int(sum(hist))
    if total == 0:
        return float("nan")
    return sum((i + 1) * c for i, c in enumerate(hist)) / total


def _rank_points(solution: Solution) -> dict[int, int]:
    """rank -> points, recovered from the assignments.

    Lets figures quote the curve without being handed it separately. Only ranks
    that someone actually received appear, which is exactly the set we can
    honestly quote.
    """
    out: dict[int, int] = {}
    for a in solution.assignments:
        out.setdefault(int(a.rank_received), int(a.points))
    return dict(sorted(out.items()))


def _page_count(n_items: int, per_page: int) -> int:
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    return max(1, math.ceil(n_items / per_page))


def _check_page(page: int, n_pages: int, what: str) -> None:
    if not 0 <= page < n_pages:
        raise ValueError(
            f"{what}: page={page} is out of range; this content needs "
            f"{n_pages} page(s), so valid pages are 0..{n_pages - 1}."
        )


def _rows_per_page(fig: Figure, rect: tuple[float, float, float, float],
                   row_height_in: float, header_rows: float) -> int:
    """How many data rows fit, derived from the figure's own geometry.

    Nothing here is hard-coded to a page size or to N: give it a taller figure
    and it uses the space; give it 200 people and it paginates.
    """
    usable_in = float(fig.get_size_inches()[1]) * rect[3]
    rows = int(usable_in / row_height_in - header_rows)
    return max(rows, 1)


# --------------------------------------------------------------------------
# 1. Rank distribution
# --------------------------------------------------------------------------


def rank_distribution(
    ax: Axes,
    solution: Solution,
    *,
    title: str | None = None,
    caption: bool = True,
) -> None:
    """Share of people who received their 1st .. Kth choice.

    K bars, K taken from ``solution.k`` — nothing here knows or cares what K
    is. Both the count and the percentage are printed on every bar (the
    coordinator asked for both: the percentage is what people argue about, the
    count is what they check). The mean rank received is marked with a rule.

    A rank with nobody in it still gets a bar position and an explicit "0" so
    the reader can see the gap rather than having to infer it from a missing
    tick.
    """
    hist, n = _hist_and_n(solution)
    k = int(solution.k)
    _style_axes(ax, grid_axis="y")

    if title is None:
        title = "Which choice each person received"
    if title:
        ax.set_title(title, loc="left", fontsize=11.0, color=_INK, pad=9.0)

    if n == 0 or k == 0:
        _empty_state(
            ax,
            "No desks were assigned.",
            "The solve produced an empty assignment, so there is no rank "
            "distribution to show. This normally means nobody was left in the "
            "pool after keepers and non-responders were removed.",
            caption="This panel is intentionally empty; see the pool summary "
                    "for why nobody was assigned." if caption else None,
        )
        return

    x = np.arange(1, k + 1, dtype=float)
    fracs = np.array([c / n for c in hist], dtype=float)
    colors = rank_colors(k)

    bars = ax.bar(
        x, fracs, width=0.62, color=colors, zorder=3,
        edgecolor=[_darken(c, 0.55) for c in colors], linewidth=0.6,
    )

    # Headroom for the two-line labels above every bar, plus the mean rule
    # annotation in the top band.
    top = max(float(fracs.max()), 0.02) * 1.46
    ax.set_ylim(0.0, min(top, 1.55))

    # White halo behind the value labels so the mean rule, which can pass
    # straight through the tallest bar's labels, does not run over the glyphs.
    halo = dict(facecolor="white", edgecolor="none", pad=0.9)
    for bar, count, frac in zip(bars, hist, fracs):
        cx = bar.get_x() + bar.get_width() / 2.0
        ax.annotate(
            _pct(frac),
            xy=(cx, frac), xytext=(0.0, 3.5), textcoords="offset points",
            ha="center", va="bottom", fontsize=8.4, color=_INK, zorder=5,
            bbox=halo,
        )
        ax.annotate(
            f"n = {_n(count)}",
            xy=(cx, frac), xytext=(0.0, 14.5), textcoords="offset points",
            ha="center", va="bottom", fontsize=7.2, color=_MUTED, zorder=5,
            bbox=halo,
        )

    mean_rank = _mean_rank(hist)
    ax.axvline(mean_rank, color=_RULE, linewidth=1.0, linestyle=(0, (4, 2.5)),
               zorder=4)
    # Put the label on whichever side of the rule has room; flip the offset as
    # well as the alignment, or the text sits on top of the rule.
    on_left = mean_rank < (k + 1) / 2.0
    ax.annotate(
        f"mean rank received  {mean_rank:.2f}",
        xy=(mean_rank, ax.get_ylim()[1]),
        xytext=(5.0 if on_left else -5.0, -4.0), textcoords="offset points",
        ha="left" if on_left else "right", va="top", fontsize=8.0,
        color=_RULE, zorder=6,
    )

    ax.set_xticks(x)
    ax.set_xticklabels([_ordinal(r) for r in range(1, k + 1)], fontsize=8.0)
    ax.set_xlim(0.4, k + 0.6)
    ax.set_xlabel("choice received (position in that person's own ranked list)")
    ax.set_ylabel(f"share of the {_people(n)} assigned")
    ax.yaxis.set_major_formatter(_pct_formatter(0))

    if caption:
        _caption(
            ax,
            "Each bar is the share of people who were given the desk sitting "
            f"at that position in their own ranked list of {k}; the percentage "
            "and the headcount are printed above every bar, and the dashed "
            "rule marks the mean rank received.",
        )


# --------------------------------------------------------------------------
# 2. Cumulative satisfaction
# --------------------------------------------------------------------------


def cumulative_satisfaction(
    ax: Axes,
    solution: Solution,
    *,
    title: str | None = None,
    caption: bool = True,
) -> None:
    """Cumulative share receiving their Nth choice or better, N = 1..K.

    The last point is 100% by construction — every assigned desk is inside the
    person's submitted top-K (invariant I4) — and the figure says so rather
    than letting the reader think the solver got lucky.
    """
    hist, n = _hist_and_n(solution)
    k = int(solution.k)
    _style_axes(ax, grid_axis="y")

    if title is None:
        title = "Cumulative satisfaction"
    if title:
        ax.set_title(title, loc="left", fontsize=11.0, color=_INK, pad=9.0)

    if n == 0 or k == 0:
        _empty_state(
            ax,
            "No desks were assigned.",
            "With an empty assignment there is no cumulative curve to draw.",
            caption="This panel is intentionally empty." if caption else None,
        )
        return

    cum_counts = np.cumsum(np.asarray(hist, dtype=np.int64))
    cum = cum_counts / float(n)
    x = np.arange(1, k + 1, dtype=float)
    colors = rank_colors(k)

    # 100% reference. Deliberately unlabelled: the y axis already says 100%,
    # and an "everyone" tag here collides with the last point's own label.
    ax.axhline(1.0, color=_FAINT, linewidth=0.8, linestyle=(0, (3, 3)), zorder=2)

    ax.plot(x, cum, color="#20364a", linewidth=1.3, zorder=4, solid_capstyle="round")
    ax.scatter(x, cum, s=42, c=colors, zorder=5, edgecolors=[_darken(c, 0.55) for c in colors],
               linewidths=0.7)

    for xi, frac, count in zip(x, cum, cum_counts):
        ax.annotate(
            _pct(frac),
            xy=(xi, frac), xytext=(0.0, 9.0), textcoords="offset points",
            ha="center", va="bottom", fontsize=8.2, color=_INK, zorder=6,
        )
        ax.annotate(
            f"{_n(count)} of {_n(n)}",
            xy=(xi, frac), xytext=(0.0, -12.0), textcoords="offset points",
            ha="center", va="top", fontsize=7.0, color=_MUTED, zorder=6,
        )

    lo = float(cum.min())
    pad = max(0.08, (1.0 - lo) * 0.28)
    ax.set_ylim(max(0.0, lo - pad), 1.0 + max(0.10, pad * 0.9))
    ax.set_xlim(0.5, k + 0.5)
    ax.set_xticks(x)
    ax.set_xticklabels([_ordinal(r) for r in range(1, k + 1)], fontsize=8.0)
    ax.set_xlabel("received their Nth choice or better")
    ax.set_ylabel(f"share of the {_people(n)} assigned")
    ax.yaxis.set_major_formatter(_pct_formatter(0))

    if caption:
        _caption(
            ax,
            "Read each point as 'this share of people got a desk at least this "
            "high on their own list'; the final point is 100% by construction, "
            f"because no one can be assigned a desk outside their submitted top "
            f"{k}.",
        )


# --------------------------------------------------------------------------
# 3. Baseline comparison  — the persuasive one
# --------------------------------------------------------------------------


def _as_baseline_list(baseline_result: Any) -> list[Any]:
    if baseline_result is None:
        return []
    if hasattr(baseline_result, "totals"):
        return [baseline_result]
    return list(baseline_result)


def _bin_edges(values: np.ndarray, opt: float, scale: int,
               n_bins_hint: int | None) -> np.ndarray:
    """Deterministic bins covering every baseline draw and the optimum.

    Totals are integers divided by ``scale``, so when the whole range spans
    only a few achievable totals, linspace bins produce a comb of empty gaps.
    In that case snap to one bin per achievable total instead.
    """
    lo = float(min(values.min(), opt))
    hi = float(max(values.max(), opt))
    if not math.isfinite(lo) or not math.isfinite(hi):
        lo, hi = 0.0, 1.0
    span_units = (hi - lo) * scale
    target = n_bins_hint or int(np.clip(round(math.sqrt(max(values.size, 1))), 12, 44))
    if span_units < 1.0:
        return np.linspace(lo - 0.5 / scale, hi + 0.5 / scale, 5)
    if span_units <= target * 1.6:
        lo_u = math.floor(lo * scale)
        hi_u = math.ceil(hi * scale)
        return (np.arange(lo_u - 0.5, hi_u + 1.5, 1.0)) / float(scale)
    pad = (hi - lo) * 0.03
    return np.linspace(lo - pad, hi + pad, target + 1)


def baseline_comparison(
    ax: Axes,
    solution: Solution,
    baseline_result: Any,
    *,
    bins: int | None = None,
    title: str | None = None,
    caption: bool = True,
) -> None:
    """Monte-Carlo baseline totals, with the published optimum as a rule.

    ``baseline_result`` is one ``baselines.BaselineResult`` or any sequence of
    them, so RSD and the uniform lottery can be overlaid on the same axes.

    This is the figure the whole report is arguing with, so it is deliberately
    built to survive a hostile read:

    * The percentile is stated as an exact count ("beats 19 993 of 20 000"),
      never as a rounded "100%" that is really 99.97%.
    * Trials in which the random process left somebody with *no* desk from
      their list are **included** in the totals — they score lower, and
      excluding them would flatter the optimum. The share of such trials is
      reported separately, because "the old way strands people" is the other
      half of the argument and is arguably the more important half.
    * The ceiling (everyone gets their first choice) is drawn when it can be
      derived, so the reader can see how much of the gap is actually available
      rather than only how far above the lottery we landed.
    """
    results = _as_baseline_list(baseline_result)
    scale = max(int(solution.scale), 1)
    opt = solution.total_points_scaled / float(scale)
    _, n_people = _hist_and_n(solution)

    _style_axes(ax, grid_axis="y")
    if title is None:
        title = "The published assignment against simulated random allocation"
    if title:
        ax.set_title(title, loc="left", fontsize=11.0, color=_INK, pad=9.0)

    usable = [r for r in results
              if getattr(r, "totals", None) is not None and np.size(r.totals) > 0]
    if not usable:
        _empty_state(
            ax,
            "No baseline trials were run.",
            "The Monte-Carlo comparison needs at least one trial. Re-run with "
            "--trials set above zero to produce this figure.",
            caption="This panel is intentionally empty; the comparison was not "
                    "run." if caption else None,
        )
        return

    all_totals = np.concatenate(
        [np.asarray(r.totals, dtype=np.float64) / scale for r in usable]
    )
    edges = _bin_edges(all_totals, opt, scale, bins)
    colors = _series_colors(len(usable) + 1)[1:]  # keep the ink slate for the rule

    peak = 0.0
    summary_lines: list[str] = []
    headline: str | None = None

    for idx, res in enumerate(usable):
        totals = np.asarray(res.totals, dtype=np.float64) / scale
        n_trials = int(totals.size)
        weights = np.full(n_trials, 1.0 / n_trials)
        color = colors[idx % len(colors)]
        counts, _ = np.histogram(totals, bins=edges, weights=weights)
        peak = max(peak, float(counts.max(initial=0.0)))
        ax.hist(totals, bins=edges, weights=weights, color=color, alpha=0.30,
                histtype="stepfilled", zorder=3)
        ax.hist(totals, bins=edges, weights=weights, color=_darken(color, 0.8),
                histtype="step", linewidth=1.0, zorder=4)

        # Direct label at the distribution's mode, so no legend is needed.
        mode_i = int(np.argmax(counts))
        mode_x = float((edges[mode_i] + edges[mode_i + 1]) / 2.0)
        ax.annotate(
            str(getattr(res, "name", f"baseline {idx + 1}")),
            xy=(mode_x, float(counts[mode_i])), xytext=(0.0, 6.0),
            textcoords="offset points", ha="center", va="bottom",
            fontsize=8.2, color=_darken(color, 0.7), zorder=6,
        )

        n_at_least = int((totals >= opt).sum())
        median = float(np.median(totals))
        # A short unlabelled tick on the baseline for the median; the note block
        # below the axes gives the number, so a label here would just be noise.
        ax.plot([median], [0.0], marker="|", markersize=9.0, color=_darken(color, 0.7),
                markeredgewidth=1.2, clip_on=False, zorder=6)

        name = str(getattr(res, "name", f"baseline {idx + 1}"))
        if n_at_least == 0:
            verdict = f"beats all {_n(n_trials)} {name} trials"
        else:
            verdict = (
                f"beats {_n(n_trials - n_at_least)} of {_n(n_trials)} {name} "
                f"trials ({_pct((n_trials - n_at_least) / n_trials, 2)}); "
                f"{_n(n_at_least)} matched or exceeded it"
            )
        gap = opt - median
        gap_pct = (gap / median) if median > 0 else float("nan")
        gap_txt = (f"{gap:+.6g} points above the {name} median"
                   + (f" ({gap_pct:+.1%})" if math.isfinite(gap_pct) else ""))
        if idx == 0:
            headline = (f"The published assignment scores {opt:.6g} points and "
                        f"{verdict}.")
        summary_lines.append(f"{name}: {verdict}; {gap_txt}.")

        unassigned = np.asarray(getattr(res, "unassigned", np.zeros(n_trials)),
                                dtype=np.int64)
        failed = int((unassigned > 0).sum())
        if failed:
            stranded = unassigned[unassigned > 0]
            summary_lines.append(
                f"{name} left at least one person with no desk from their own "
                f"list in {_n(failed)} of {_n(n_trials)} trials "
                f"({_pct(failed / n_trials, 1)}); a median of "
                f"{float(np.median(stranded)):.3g} stranded in those trials. The "
                f"published assignment seats all {_people(n_people)} inside "
                f"their submitted choices, by construction."
            )
        else:
            summary_lines.append(
                f"{name} seated everyone inside their own list in all "
                f"{_n(n_trials)} trials, so the argument here is only about "
                f"quality, not about people being stranded."
            )

    # Ceiling: everyone's first choice, when we can derive the rank-1 value
    # honestly from the assignments themselves.
    rank_points = _rank_points(solution)
    if 1 in rank_points and n_people:
        ceiling = n_people * rank_points[1] / float(scale)
        if ceiling > opt:
            ax.axvline(ceiling, color=_FAINT, linewidth=1.0,
                       linestyle=(0, (2, 2.5)), zorder=3)
            # rotation_mode="anchor" applies the alignment *before* rotating, so
            # va="bottom" reliably puts the text beside the rule rather than
            # straddling it. Without it, rotated labels sit on the line.
            ax.annotate(
                "ceiling: everyone's 1st choice",
                xy=(ceiling, 0.0), xytext=(-3.0, 4.0), textcoords="offset points",
                rotation=90.0, rotation_mode="anchor", ha="left", va="bottom",
                fontsize=7.0, color=_MUTED, zorder=6,
            )

    ax.axvline(opt, color=_RULE, linewidth=1.6, zorder=7)
    ax.set_ylim(0.0, max(peak, 0.01) * 1.55)
    ylim_top = ax.get_ylim()[1]

    xlo, xhi = ax.get_xlim()
    on_right = opt > (xlo + xhi) / 2.0
    # Kept short on purpose: a long rotated label runs up through whatever
    # distribution happens to sit beside the rule. The value lives in the
    # headline sentence instead.
    ax.annotate(
        "published assignment",
        xy=(opt, ylim_top * 0.06),
        xytext=(-3.0 if on_right else 3.0, 0.0), textcoords="offset points",
        rotation=90.0, rotation_mode="anchor", ha="left",
        va="bottom" if on_right else "top",
        fontsize=8.0, color=_RULE, zorder=8,
    )

    if headline:
        ax.text(0.005, 0.985, _wrap_for(headline, _axes_width_in(ax), 9.6),
                transform=ax.transAxes, ha="left", va="top", fontsize=9.6,
                color=_INK, zorder=8, linespacing=1.35)

    ax.set_xlabel(
        f"total satisfaction score (points on the '{solution.curve_name}' curve; "
        f"higher is better)"
    )
    ax.set_ylabel("share of simulated trials")
    ax.yaxis.set_major_formatter(_pct_formatter(0))
    ax.xaxis.set_major_locator(MaxNLocator(nbins=8, steps=[1, 2, 2.5, 5, 10]))

    next_dy = _note_block(ax, summary_lines, dy=-46.0, size=7.6)
    if caption:
        _caption(
            ax,
            "The shaded distributions are what the simulated random processes "
            "achieved over many trials on exactly the same preferences and "
            "curve; the heavy vertical rule is the assignment this report "
            "publishes, the short tick under each distribution is its median, "
            "and trials that stranded someone are included in the totals rather "
            "than dropped.",
            dy=next_dy,
        )


# --------------------------------------------------------------------------
# 4 & 5. Sensitivity panels
# --------------------------------------------------------------------------

#: (name, rank_histogram, total_scaled, n_moved) — what baselines.py returns.
SensitivityRow = tuple[str, Sequence[int], int, int]


def _normalise_rows(rows: Sequence[SensitivityRow], what: str
                    ) -> list[tuple[str, tuple[int, ...], int, int]]:
    out: list[tuple[str, tuple[int, ...], int, int]] = []
    k: int | None = None
    for row in rows:
        name, hist, total, moved = row
        hist_t = tuple(int(v) for v in hist)
        if k is None:
            k = len(hist_t)
        elif len(hist_t) != k:
            raise ValueError(
                f"{what}: rank histograms have inconsistent lengths "
                f"({k} vs {len(hist_t)}). Every curve must have the same K "
                f"(SPEC §2.4), so this is a bug upstream, not a plotting choice."
            )
        out.append((str(name), hist_t, int(total), int(moved)))
    return out


def _solution_row(solution: Solution) -> tuple[str, tuple[int, ...], int, int]:
    return (str(solution.curve_name), solution.rank_histogram(),
            int(solution.total_points_scaled), 0)


def _grouped_rank_bars(
    ax: Axes,
    rows: Sequence[tuple[str, tuple[int, ...], int, int]],
    *,
    reference_index: int = 0,
    moved_denominator: int,
    unit_noun: str,
) -> None:
    """Grouped bars: one group per rank, one bar per scenario."""
    k = len(rows[0][1])
    n_series = len(rows)
    colors = _series_colors(n_series)
    x = np.arange(1, k + 1, dtype=float)
    slot = 0.82 / n_series

    peak = 0
    for s, (name, hist, _total, moved) in enumerate(rows):
        offset = (s - (n_series - 1) / 2.0) * slot
        if s == reference_index:
            label = f"{name}  (reference)"
        elif moved_denominator > 0:
            label = (f"{name}  — {_n(moved)} of {_n(moved_denominator)} "
                     f"{unit_noun} moved ({_pct(moved / moved_denominator, 1)})")
        else:
            label = f"{name}  — {_n(moved)} {unit_noun} moved"
        ax.bar(
            x + offset, hist, width=slot * 0.9, label=label,
            color=colors[s], edgecolor=_darken(colors[s], 0.55), linewidth=0.6,
            zorder=3,
        )
        peak = max(peak, max(hist) if hist else 0)

    # Value labels only while they still fit; past that the y axis carries it.
    if n_series * k <= 18:
        for s, (_name, hist, _total, _moved) in enumerate(rows):
            offset = (s - (n_series - 1) / 2.0) * slot
            for xi, value in zip(x, hist):
                ax.annotate(
                    _n(value), xy=(xi + offset, value), xytext=(0.0, 2.5),
                    textcoords="offset points", ha="center", va="bottom",
                    fontsize=6.6, color=_MUTED, zorder=5,
                )

    ax.set_ylim(0.0, max(peak, 1) * (1.24 + 0.055 * n_series))
    ax.set_xticks(x)
    ax.set_xticklabels([_ordinal(r) for r in range(1, k + 1)], fontsize=8.0)
    ax.set_xlim(0.4, k + 0.6)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True, nbins=6))
    ax.legend(loc="upper right", frameon=False, fontsize=7.8, ncols=1,
              borderaxespad=0.3, handlelength=1.2, labelspacing=0.4)


def curve_sensitivity(
    ax: Axes,
    rows: Sequence[SensitivityRow],
    *,
    primary: Solution | SensitivityRow | None = None,
    title: str | None = None,
    caption: bool = True,
) -> None:
    """Rank histogram under the primary curve versus each alternative curve.

    ``rows`` is exactly what ``baselines.alternative_curve_outcomes`` returns:
    ``(curve_name, rank_histogram, total_scaled, n_moved_vs_primary)``. That
    function does *not* include the primary curve in its output, so pass the
    primary ``Solution`` (or its own row) as ``primary`` and it is drawn first
    as the reference.

    Totals are deliberately not plotted. Each curve integerises to its own
    scale — ``[5,4,3,2,1]`` and ``[5,4.5,4,3.5,3]`` do not live in the same
    units — so a bar chart of totals across curves would be meaningless. Mean
    rank received *is* comparable, so that is what the footnote quotes.
    """
    _style_axes(ax, grid_axis="y")
    if title is None:
        title = "Sensitivity to the scoring curve"
    if title:
        ax.set_title(title, loc="left", fontsize=11.0, color=_INK, pad=9.0)

    body = _normalise_rows(rows, "curve_sensitivity")

    ref_row: tuple[str, tuple[int, ...], int, int] | None = None
    if isinstance(primary, Solution):
        ref_row = _solution_row(primary)
    elif primary is not None:
        ref_row = _normalise_rows([primary], "curve_sensitivity")[0]

    if ref_row is not None:
        body = [r for r in body if r[0] != ref_row[0]]
        all_rows = [ref_row] + body
    else:
        all_rows = body

    if not all_rows:
        _empty_state(
            ax,
            "No comparison curves were configured.",
            "scoring.json lists no comparison_curves, so there is nothing to "
            "compare the primary curve against. Add one or more curve names to "
            "comparison_curves and re-run to populate this figure.",
            caption="This panel is intentionally empty; no alternative curves "
                    "were configured." if caption else None,
        )
        return

    if len(all_rows) == 1:
        name, hist, _total, _moved = all_rows[0]
        _empty_state(
            ax,
            "Only one scoring curve was evaluated.",
            f"'{name}' is the only curve in this run, so there is no "
            f"alternative to compare it against. Add entries to "
            f"comparison_curves in scoring.json to make this figure "
            f"informative.",
            caption="This panel is intentionally empty; only the primary curve "
                    "was run." if caption else None,
        )
        return

    n_people = int(sum(all_rows[0][1]))
    _grouped_rank_bars(ax, all_rows, reference_index=0,
                       moved_denominator=n_people, unit_noun="people")

    ax.set_xlabel("choice received (position in that person's own ranked list)")
    ax.set_ylabel("people")

    moved_bits = [f"{name} {_n(moved)}" for name, _h, _t, moved in all_rows[1:]]
    mean_bits = [f"{name} {_mean_rank(hist):.2f}" for name, hist, _t, _m in all_rows]
    lines = [
        "People whose desk changed relative to the primary curve:  "
        + "  ·  ".join(moved_bits)
        + f"   (out of {_n(n_people)})",
        "Mean rank received:  " + "  ·  ".join(mean_bits)
        + ".  Totals are not shown because each curve integerises to its own "
          "point scale and they are not comparable across curves; the rank "
          "histogram and the mean rank are.",
    ]
    next_dy = _note_block(ax, lines, dy=-46.0, size=7.6)

    if caption:
        _caption(
            ax,
            "Bars are grouped by which choice people received, with one bar per "
            "scoring curve, so groups that look the same mean the choice of "
            "curve did not change the shape of the outcome; the legend gives "
            "how many individual people's desks actually moved, which can be "
            "non-zero even when the bars are identical.",
            dy=next_dy,
        )


def seed_sensitivity(
    ax: Axes,
    rows: Sequence[SensitivityRow],
    *,
    published_seed: str | None = None,
    title: str | None = None,
    caption: bool = True,
) -> None:
    """The same comparison, for alternative tie-break seeds.

    ``rows`` is what ``baselines.alternative_seed_outcomes`` returns, whose
    first entry is the reference the ``n_moved`` counts are measured against.

    Three outcomes, three different figures, because drawing identical bars
    would be actively misleading:

    * **Nobody moved anywhere.** Plain words. Five identical bar groups say
      "look how stable" when what they actually say is "there was nothing to
      show"; the sentence is the honest version, and it is the informative
      result.
    * **People moved but the rank histogram is unchanged.** This is the normal
      case and the bars would again be identical: the seed only ever chooses
      among assignments that are *already exactly tied* for optimal, so the
      shape cannot move even when individuals do. Draw the thing that did
      change — how many people swapped desks under each seed.
    * **The histogram genuinely differs.** Grouped bars, same layout as
      ``curve_sensitivity``. Also flag it, because under the epsilon bound
      (SPEC §5.4) the totals must be identical across seeds; a differing total
      means the bound has failed and the run should not be published.
    """
    _style_axes(ax, grid_axis="y")
    if title is None:
        title = "Sensitivity to the tie-break seed"
    if title:
        ax.set_title(title, loc="left", fontsize=11.0, color=_INK, pad=9.0)

    all_rows = _normalise_rows(rows, "seed_sensitivity")

    if not all_rows:
        _empty_state(
            ax,
            "No alternative seeds were evaluated.",
            "scoring.json lists no sensitivity_seeds, so the effect of the "
            "tie-break seed was not measured. Add a few seed strings to "
            "sensitivity_seeds and re-run to populate this figure.",
            caption="This panel is intentionally empty; no alternative seeds "
                    "were configured." if caption else None,
        )
        return

    reference = all_rows[0]
    alternatives = all_rows[1:]
    n_people = int(sum(reference[1]))
    ref_label = published_seed if published_seed is not None else reference[0]

    if not alternatives:
        _empty_state(
            ax,
            "Only the published seed was evaluated.",
            f"'{ref_label}' is the only seed in this run, so there is nothing "
            f"to compare it against. Add entries to sensitivity_seeds in "
            f"scoring.json to make this figure informative.",
            caption="This panel is intentionally empty; no alternative seeds "
                    "were configured." if caption else None,
        )
        return

    # Comparisons only -- no set is built, let alone iterated (I3).
    totals_differ = any(t != reference[2] for _n_, _h, t, _m in alternatives)
    hists_differ = any(h != reference[1] for _n_, h, _t, _m in alternatives)
    nobody_moved = all(moved == 0 for _n_, _h, _t, moved in alternatives)

    # ---- outcome 1: the seed changed nothing at all ----------------------
    if nobody_moved and not hists_differ and not totals_differ:
        ax.set_axis_off()
        ax.text(
            0.0, 0.90, "The tie-break seed changed nothing this year.",
            transform=ax.transAxes, ha="left", va="top", fontsize=12.5,
            color=_INK,
        )
        body = (
            f"All {_n(len(alternatives))} alternative seeds produced exactly the "
            f"same assignment as the published seed '{ref_label}': every one of "
            f"the {_n(n_people)} people got the same desk under every seed, and "
            f"the total was identical throughout. The seed can only ever choose "
            f"among assignments that are already exactly tied for optimal "
            f"(SPEC §5.4), and under these preferences no alternative seed "
            f"found a different tied optimum to choose. Drawing "
            f"{_n(len(all_rows))} identical bar charts here would imply a "
            f"comparison was made and came out close; in fact there was nothing "
            f"to compare, which is the stronger result."
        )
        ax.text(
            0.0, 0.735, _wrap_for(body, _axes_width_in(ax), 9.2),
            transform=ax.transAxes, ha="left", va="top", fontsize=9.2,
            color=_INK, linespacing=1.65,
        )
        listed = "   ".join(f"· {name}" for name, _h, _t, _m in alternatives)
        ax.text(
            0.0, 0.20,
            _wrap_for("Seeds tested, in the order configured:  "
                      + f"{ref_label} (published)   " + listed,
                      _axes_width_in(ax), 7.8),
            transform=ax.transAxes, ha="left", va="top", fontsize=7.8,
            color=_MUTED, linespacing=1.5,
        )
        if caption:
            _caption(
                ax,
                "This panel is a sentence rather than a chart on purpose: the "
                "measured quantity — how many people's desks the seed moved — "
                "was zero for every alternative seed.",
                dy=-6.0,
            )
        return

    # ---- outcome 2: people moved, the distribution did not ---------------
    if not hists_differ and not totals_differ:
        names = [name for name, _h, _t, _m in alternatives]
        moved = [m for _n_, _h, _t, m in alternatives]
        y = np.arange(len(alternatives), dtype=float)
        colors = _series_colors(len(alternatives) + 1)[1:]
        ax.barh(y, moved, height=0.6, color=colors,
                edgecolor=[_darken(c, 0.55) for c in colors], linewidth=0.6,
                zorder=3)
        for yi, value in zip(y, moved):
            ax.annotate(
                f"{_n(value)} of {_n(n_people)}"
                + (f"  ({_pct(value / n_people, 1)})" if n_people else ""),
                xy=(value, yi), xytext=(4.0, 0.0), textcoords="offset points",
                ha="left", va="center", fontsize=8.0, color=_INK, zorder=5,
            )
        ax.set_yticks(y)
        ax.set_yticklabels(names, fontsize=8.0)
        ax.invert_yaxis()
        ax.set_xlim(0.0, max(max(moved), 1) * 1.42)
        ax.set_xlabel("people given a different desk than under the published seed")
        ax.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=7))
        ax.grid(True, axis="x", color=_GRID, linewidth=0.6)
        ax.grid(False, axis="y")
        ax.set_axisbelow(True)

        lines = [
            f"Every alternative seed produced the identical rank distribution "
            f"{list(reference[1])} and the identical total, so the bar chart of "
            f"rank counts would be {_n(len(all_rows))} copies of one shape. What "
            f"actually varies is which of the exactly-tied optimal assignments "
            f"gets chosen, shown above.",
        ]
        next_dy = _note_block(ax, lines, dy=-40.0, size=7.6)
        if caption:
            _caption(
                ax,
                "Each bar is the number of people who would sit somewhere else "
                "under that alternative seed; the outcome as a whole is "
                "unchanged, because the seed can only pick between assignments "
                "that already score exactly the same.",
                dy=next_dy,
            )
        return

    # ---- outcome 3: the distribution itself moved ------------------------
    _grouped_rank_bars(ax, all_rows, reference_index=0,
                       moved_denominator=n_people, unit_noun="people")
    ax.set_xlabel("choice received (position in that person's own ranked list)")
    ax.set_ylabel("people")

    lines = [
        "People whose desk changed relative to the published seed:  "
        + "  ·  ".join(f"{name} {_n(m)}" for name, _h, _t, m in alternatives)
        + f"   (out of {_n(n_people)})",
    ]
    if totals_differ:
        lines.append(
            "WARNING: the total score is not identical across seeds. Under the "
            "epsilon bound (SPEC §5.4, invariant I6) the tie-break can only "
            "select among assignments that are exactly tied, so the totals must "
            "match. They do not. Treat this run as void and investigate before "
            "publishing anything."
        )
    next_dy = _note_block(ax, lines, dy=-46.0, size=7.6)
    if caption:
        _caption(
            ax,
            "Bars are grouped by which choice people received, one bar per "
            "seed; the seed is supposed to be able to move individuals without "
            "moving this distribution, so visible differences between groups "
            "are worth investigating.",
            dy=next_dy,
        )


# --------------------------------------------------------------------------
# 6. Rank received, per person
# --------------------------------------------------------------------------


def rank_received_by_person(
    ax: Axes,
    solution: Solution,
    anonymize: bool = True,
    *,
    name_limit: int | None = None,
    title: str | None = None,
    caption: bool = True,
) -> None:
    """One dot per person, showing the rank they received, sorted.

    ``anonymize=True`` (public) plots positional indices only. It is built
    **entirely from** ``solution.rank_histogram()`` — no name, no email, and no
    per-person record is read at all — so it is provably a function of the
    aggregate that ``rank_distribution`` already publishes. In particular the
    left-to-right order carries no information about the input order, because
    there is no input order in the data it was drawn from. The code asserts
    this rather than relying on the reader believing the docstring.

    ``anonymize=False`` (coordinator) labels people, worst rank first, and is
    banner-marked as a coordinator page. Where there are more people than can
    be labelled legibly on one axes it keeps every dot and thins the *labels*,
    naming the worst-off people and pointing at the assignment table for the
    rest — it never silently drops anyone.
    """
    hist, n = _hist_and_n(solution)
    k = int(solution.k)
    _style_axes(ax, grid_axis="y")

    if title is None:
        title = ("Rank received, person by person" if anonymize
                 else "Rank received, by name")
    if title:
        ax.set_title(title, loc="left", fontsize=11.0, color=_INK, pad=9.0)

    if n == 0 or k == 0:
        _empty_state(
            ax,
            "No desks were assigned.",
            "There is nobody to plot. See the pool summary for why the "
            "assignment came out empty.",
            caption="This panel is intentionally empty." if caption else None,
        )
        return

    if not anonymize:
        _coordinator_banner(ax.figure)

    colors = rank_colors(k)

    extra: list[str] = []

    if anonymize:
        # Built from the histogram alone. Nothing identifying is touched: no
        # name, no email, no per-assignment record. This is the privacy claim
        # in the module docstring, enforced here rather than promised.
        ranks = [r for r, count in enumerate(hist, start=1) for _ in range(count)]
        if len(ranks) != n:
            raise ValueError(
                f"rank histogram sums to {len(ranks)} but the solution has {n} "
                f"assignments; the two disagree and the anonymised strip would "
                f"misrepresent the outcome."
            )
        labels: list[str] | None = None
    else:
        ordered = sorted(
            solution.assignments,
            key=lambda a: (-int(a.rank_received), a.name.casefold(), a.email),
        )
        ranks = [int(a.rank_received) for a in ordered]
        labels = [f"{a.name}  —  desk {a.desk_label}" for a in ordered]

    x = np.arange(1, n + 1, dtype=float)

    if anonymize:
        # Best at the left; y inverted so "better" is up.
        marker_size = float(np.clip(2600.0 / max(n, 1), 4.0, 46.0))
        point_colors = [colors[r - 1] for r in ranks]
        ax.scatter(x, ranks, s=marker_size, c=point_colors, zorder=4,
                   edgecolors="none")

        # Group boundaries and per-group counts tie this back to figure 1.
        start = 0
        for rank, count in enumerate(hist, start=1):
            if count == 0:
                continue
            end = start + count
            mid = (start + end + 1) / 2.0
            if start > 0:
                ax.axvline(start + 0.5, color=_GRID, linewidth=0.7, zorder=2)
            # Labels sit below their run, except for the last rank, where
            # "below" is off the bottom of the axes.
            below = rank < k
            ax.annotate(
                f"{_n(count)}\n{_pct(count / n, 0)}",
                xy=(mid, rank), xytext=(0.0, -13.0 if below else 13.0),
                textcoords="offset points", ha="center",
                va="top" if below else "bottom", fontsize=7.2, color=_MUTED,
                zorder=5, linespacing=1.3,
            )
            start = end

        ax.set_xlim(0.5, n + 0.5)
        ax.set_xlabel(f"the {_people(n)}, sorted by the rank they received "
                      f"(no names, no input order)")
        ax.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=8))
        ax.set_ylim(k + 0.55, 0.45)
        ax.set_yticks(np.arange(1, k + 1))
        ax.set_yticklabels([_ordinal(r) for r in range(1, k + 1)], fontsize=8.0)
        ax.set_ylabel("choice received")
        ax.grid(True, axis="y", color=_GRID, linewidth=0.6)
        ax.set_axisbelow(True)

        if caption:
            _caption(
                ax,
                "One dot per person, sorted from best to worst rank received, "
                "so the length of each flat run is the number of people at that "
                "rank; this panel is drawn from the aggregate counts alone and "
                "carries no information about who is who.",
            )
        return

    # ---- named (coordinator) --------------------------------------------
    assert labels is not None
    fig_h_in = float(ax.figure.get_size_inches()[1]) * float(ax.get_position().height)
    if name_limit is None:
        # ~7.5pt of vertical room per legible label.
        name_limit = max(int(fig_h_in * 72.0 / 7.5), 4)

    y = np.arange(n, dtype=float)
    point_colors = [colors[r - 1] for r in ranks]
    marker_size = float(np.clip(2600.0 / max(n, 1), 4.0, 44.0))
    ax.scatter(ranks, y, s=marker_size, c=point_colors, zorder=4, edgecolors="none")
    ax.hlines(y, 0.5, ranks, colors=_GRID, linewidth=0.6, zorder=2)

    if n <= name_limit:
        label_size = float(np.clip(fig_h_in * 72.0 / max(n, 1) * 0.62, 4.6, 8.4))
        margin_in = float(ax.get_position().x0) * float(ax.figure.get_size_inches()[0])
        budget = max(_fit_chars(margin_in, label_size) - 1, 8)
        ax.set_yticks(y)
        ax.set_yticklabels([_truncate(t, budget) for t in labels],
                           fontsize=label_size)
        ax.tick_params(axis="y", length=0.0, pad=3.0)
    else:
        # A label per row is geometrically impossible here, and thinning them
        # does not help: the worst-ranked people occupy the top `shown/n` of
        # the axes, so labelling any subset at its own row still needs one row
        # of type per plotted row. Keep every dot, drop the row labels, and put
        # the information the coordinator actually wants -- who did badly -- in
        # a block in the empty corner, worst rank first.
        ax.set_yticks([])
        label_size = 7.4
        block_lines = max(int(fig_h_in * 72.0 * 0.30 / (label_size * 1.5)), 3)
        listed: list[str] = []
        i = 0
        while i < n and len(listed) < block_lines - 1:
            if ranks[i] <= 1:
                break
            listed.append(f"{_ordinal(ranks[i])}   {labels[i]}")
            i += 1
        worse_than_first = sum(1 for r in ranks if r > 1)
        if not listed:
            listed.append("Everybody received their 1st choice.")
            trailer = ""
        elif i < worse_than_first:
            trailer = (f"… and {_n(worse_than_first - i)} more below their 1st "
                       f"choice; the full list is in the assignment table.")
        else:
            trailer = (f"Everyone else — {_people(n - worse_than_first)} — "
                       f"received their 1st choice.")
        block = "Did not get a 1st choice, worst first:\n" + "\n".join(listed)
        if trailer:
            block += "\n" + trailer
        # Top-left: the y axis is inverted, so the worst-ranked dots are at the
        # top *right* and this corner is the empty one.
        ax.text(
            0.015, 0.985, block, transform=ax.transAxes, ha="left", va="top",
            fontsize=label_size, color=_INK, linespacing=1.5, zorder=6,
            # The block can reach down far enough to meet the dot column for
            # the middle ranks; a plain white ground keeps it readable.
            bbox=dict(facecolor="white", edgecolor="none", pad=2.5),
        )
        extra.append(
            f"All {_people(n)} are plotted, one dot each, but there is not "
            f"enough vertical room on a page this size to name {_n(n)} rows "
            f"legibly, so the row labels are dropped rather than overprinted. "
            f"Names against desks for everyone are in the assignment table."
        )

    ax.set_ylim(n - 0.5, -0.5)
    ax.set_xlim(0.5, k + 0.5)
    ax.set_xticks(np.arange(1, k + 1))
    ax.set_xticklabels([_ordinal(r) for r in range(1, k + 1)], fontsize=8.0)
    ax.set_xlabel("choice received")
    ax.grid(True, axis="x", color=_GRID, linewidth=0.6)
    ax.grid(False, axis="y")
    ax.set_axisbelow(True)
    for side in ("left",):
        ax.spines[side].set_visible(False)

    next_dy = _note_block(ax, extra, dy=-48.0, size=7.4)
    if caption:
        _caption(
            ax,
            "One row per person, worst rank at the top, so the people who did "
            "least well are the ones you read first; this page names "
            "individuals and belongs only in the coordinator's copy.",
            dy=next_dy,
        )


# --------------------------------------------------------------------------
# 7. Assignment table
# --------------------------------------------------------------------------

_TABLE_RECT = (0.055, 0.065, 0.90, 0.845)
_TABLE_ROW_IN = 0.185
_TABLE_HEADER_ROWS = 2.6


def assignment_table_page_count(
    solution: Solution,
    *,
    fig: Figure | None = None,
    rows_per_page: int | None = None,
    rect: tuple[float, float, float, float] = _TABLE_RECT,
    row_height_in: float = _TABLE_ROW_IN,
) -> int:
    """How many pages ``assignment_table`` will need. Cheap; no drawing."""
    n = len(solution.assignments)
    if rows_per_page is None:
        if fig is None:
            raise ValueError("pass either rows_per_page or the fig it will be drawn on")
        rows_per_page = _rows_per_page(fig, rect, row_height_in, _TABLE_HEADER_ROWS)
    return _page_count(n, rows_per_page)


def assignment_table(
    fig: Figure,
    solution: Solution,
    *,
    page: int = 0,
    rows_per_page: int | None = None,
    sort_by: str = "name",
    show_email: bool = False,
    rect: tuple[float, float, float, float] = _TABLE_RECT,
    row_height_in: float = _TABLE_ROW_IN,
    title: str | None = None,
    caption: bool = True,
) -> int:
    """Render one page of the final assignment table. Returns the TOTAL pages.

    Rows per page are derived from the figure's own height, so the table adapts
    to whatever page size ``report.py`` chose and paginates for any N. Nothing
    is truncated: at N=200 on a letter page this is five pages, and the caller
    gets told so by the return value.

    Usage::

        n_pages = figures_stats.assignment_table_page_count(sol, fig=fig)
        for p in range(n_pages):
            fig = plt.figure(figsize=(8.5, 11))
            figures_stats.assignment_table(fig, sol, page=p)
            pdf.savefig(fig); plt.close(fig)

    Calling it once with the default ``page=0`` and ignoring the return value
    silently shows only the first page, so the return value is the contract:
    check it.
    """
    if rows_per_page is None:
        rows_per_page = _rows_per_page(fig, rect, row_height_in, _TABLE_HEADER_ROWS)

    rows = list(solution.assignments)
    keys = {
        "name": lambda a: (a.name.casefold(), a.email),
        "email": lambda a: (a.email,),
        "desk": lambda a: (a.desk_label.rjust(12), a.desk_id, a.email),
        "rank": lambda a: (int(a.rank_received), a.name.casefold(), a.email),
    }
    if sort_by not in keys:
        raise ValueError(
            f"sort_by={sort_by!r} is not one of {sorted(keys)}"
        )
    rows.sort(key=keys[sort_by])

    n = len(rows)
    k = int(solution.k)
    n_pages = _page_count(n, rows_per_page)
    _check_page(page, n_pages, "assignment_table")

    ax = fig.add_axes(rect)
    ax.set_axis_off()
    ax.set_xlim(0.0, 1.0)
    ax.set_ylim(0.0, 1.0)

    if title is None:
        title = "Final desk assignment"
    heading = title if n_pages == 1 else f"{title}  ({page + 1} of {n_pages})"
    ax.text(0.0, 1.035, heading, transform=ax.transAxes, ha="left",
            va="bottom", fontsize=12.0, color=_INK)

    if n == 0:
        ax.text(0.5, 0.6, "No desks were assigned.", transform=ax.transAxes,
                ha="center", va="center", fontsize=11.0, color=_INK)
        ax.text(0.5, 0.52,
                "Nobody remained in the pool once keepers and non-responders "
                "were removed.",
                transform=ax.transAxes, ha="center", va="top", fontsize=8.4,
                color=_MUTED)
        if caption:
            fig.text(rect[0], rect[1] - 0.028,
                     "This table is intentionally empty; see the pool summary.",
                     ha="left", va="top", fontsize=7.2, color=_MUTED,
                     style="italic")
        return n_pages

    start = page * rows_per_page
    chunk = rows[start:start + rows_per_page]

    row_h = 1.0 / (rows_per_page + _TABLE_HEADER_ROWS)
    table_w_in = float(fig.get_size_inches()[0]) * rect[2]
    row_pts = row_h * float(fig.get_size_inches()[1]) * rect[3] * 72.0
    body_size = float(np.clip(row_pts * 0.60, 5.0, 9.5))
    head_size = float(np.clip(body_size * 0.92, 5.0, 8.6))

    # Column geometry in axes fractions.
    if show_email:
        col_name_x, col_mail_x, col_desk_x, col_id_x, col_rank_x = (
            0.0, 0.34, 0.615, 0.695, 0.775)
        name_frac = 0.33
    else:
        col_name_x, col_mail_x, col_desk_x, col_id_x, col_rank_x = (
            0.0, None, 0.545, 0.655, 0.755)
        name_frac = 0.53

    top = 1.0 - (_TABLE_HEADER_ROWS - 1.0) * row_h

    # Header
    head_y = top + row_h * 0.35
    ax.text(col_name_x, head_y, "NAME", transform=ax.transAxes, ha="left",
            va="bottom", fontsize=head_size, color=_MUTED, weight="bold")
    if col_mail_x is not None:
        ax.text(col_mail_x, head_y, "EMAIL", transform=ax.transAxes, ha="left",
                va="bottom", fontsize=head_size, color=_MUTED, weight="bold")
    ax.text(col_desk_x, head_y, "DESK", transform=ax.transAxes, ha="left",
            va="bottom", fontsize=head_size, color=_MUTED, weight="bold")
    ax.text(col_id_x, head_y, "ID", transform=ax.transAxes, ha="left",
            va="bottom", fontsize=head_size, color=_MUTED, weight="bold")
    ax.text(col_rank_x, head_y, "RECEIVED", transform=ax.transAxes, ha="left",
            va="bottom", fontsize=head_size, color=_MUTED, weight="bold")
    ax.plot([0.0, 1.0], [top + row_h * 0.18] * 2, transform=ax.transAxes,
            color="#5a5a5a", linewidth=0.8, clip_on=False, zorder=3)

    colors = rank_colors(k)
    name_budget = _fit_chars(name_frac * table_w_in, body_size)
    mail_budget = _fit_chars(0.26 * table_w_in, body_size)

    for i, a in enumerate(chunk):
        y_top = top - i * row_h
        y_mid = y_top - row_h * 0.5
        if i % 2 == 1:
            ax.add_patch(Rectangle(
                (0.0, y_top - row_h), 1.0, row_h, transform=ax.transAxes,
                facecolor=_ZEBRA, edgecolor="none", zorder=1,
            ))
        ax.text(col_name_x, y_mid, _truncate(a.name, name_budget),
                transform=ax.transAxes, ha="left", va="center",
                fontsize=body_size, color=_INK, zorder=2)
        if col_mail_x is not None:
            ax.text(col_mail_x, y_mid, _truncate(a.email, mail_budget),
                    transform=ax.transAxes, ha="left", va="center",
                    fontsize=body_size * 0.92, color=_MUTED, zorder=2)
        ax.text(col_desk_x, y_mid, a.desk_label, transform=ax.transAxes,
                ha="left", va="center", fontsize=body_size, color=_INK, zorder=2)
        ax.text(col_id_x, y_mid, a.desk_id, transform=ax.transAxes,
                ha="left", va="center", fontsize=body_size * 0.92,
                color=_MUTED, zorder=2)
        rank = int(a.rank_received)
        ax.scatter([col_rank_x + 0.008], [y_mid], s=max(body_size * 1.6, 6.0),
                   c=[colors[rank - 1]], transform=ax.transAxes, zorder=3,
                   edgecolors="none", clip_on=False)
        ax.text(col_rank_x + 0.028, y_mid, f"{_ordinal(rank)} choice",
                transform=ax.transAxes, ha="left", va="center",
                fontsize=body_size, color=_INK, zorder=2)

    last = start + len(chunk)
    footer = (f"people {_n(start + 1)}–{_n(last)} of {_n(n)}"
              f"    ·    sorted by {sort_by}"
              f"    ·    curve '{solution.curve_name}', K = {k}"
              f"    ·    seed '{solution.seed_string}'")
    ax.text(0.0, -row_h * 0.9, footer, transform=ax.transAxes, ha="left",
            va="top", fontsize=max(body_size * 0.78, 5.6), color=_MUTED)

    if caption:
        fig.text(
            rect[0], rect[1] - 0.030,
            _wrap_for(
                "The published desk assignment; 'received' says which of that "
                f"person's own {k} submitted choices they were given, and the "
                "dot repeats that rank in the same colour used by the "
                "distribution figures.",
                table_w_in, 7.2),
            ha="left", va="top", fontsize=7.2, color=_MUTED, style="italic",
            linespacing=1.35,
        )
    return n_pages


# --------------------------------------------------------------------------
# 8. Preference matrix  — coordinator only
# --------------------------------------------------------------------------

_PREF_RECT = (0.135, 0.115, 0.72, 0.775)
_PREF_ROW_IN = 0.155
_PREF_HEADER_ROWS = 0.0


def preference_matrix_page_count(
    problem: Problem,
    *,
    fig: Figure | None = None,
    rows_per_page: int | None = None,
    rect: tuple[float, float, float, float] = _PREF_RECT,
    row_height_in: float = _PREF_ROW_IN,
) -> int:
    """How many pages ``preference_matrix`` will need."""
    if rows_per_page is None:
        if fig is None:
            raise ValueError("pass either rows_per_page or the fig it will be drawn on")
        rows_per_page = _rows_per_page(fig, rect, row_height_in, _PREF_HEADER_ROWS)
    return _page_count(problem.n_people, rows_per_page)


def preference_matrix(
    fig: Figure,
    problem: Problem,
    anonymize: bool = False,
    *,
    page: int = 0,
    rows_per_page: int | None = None,
    solution: Solution | None = None,
    rect: tuple[float, float, float, float] = _PREF_RECT,
    row_height_in: float = _PREF_ROW_IN,
    title: str | None = None,
    caption: bool = True,
) -> int:
    """The full person x desk rank matrix as a heatmap. Returns TOTAL pages.

    **Coordinator only, in both modes.** Anonymising the row labels does not
    make this page publishable: the matrix itself is the preference data, and
    final assignments are public, so a reader who knows who sits where can walk
    a pseudonymous row straight back to a person. The page is banner-marked and
    ``FIGURE_AUDIENCE`` records it as coordinator-only.

    Three distinct cell states are drawn, because the coordinator's real
    question is "could this person have been seated somewhere else?":

    * coloured — the desk is in that person's top-K, colour gives the rank;
    * pale grey — eligible for that person's zone but not ranked by them;
    * darker grey — outside the zones that person is eligible for.

    When ``solution`` is given and ``anonymize`` is False, the assigned cell in
    each row is outlined. With ``anonymize=True`` the outlines are suppressed:
    assignments are public, so an outlined cell in a pseudonymous row would
    re-identify the row outright.
    """
    if rows_per_page is None:
        rows_per_page = _rows_per_page(fig, rect, row_height_in, _PREF_HEADER_ROWS)

    n, m, k = problem.n_people, problem.n_desks, int(problem.k)
    n_pages = _page_count(n, rows_per_page)
    _check_page(page, n_pages, "preference_matrix")

    _coordinator_banner(fig)
    ax = fig.add_axes(rect)

    if title is None:
        title = "Submitted preferences, person by desk"
    heading = title if n_pages == 1 else f"{title}  ({page + 1} of {n_pages})"
    # Generous pad: the cell-state legend is anchored just above the axes and
    # a default pad puts the title straight through it.
    ax.set_title(heading, loc="left", fontsize=11.0, color=_INK, pad=30.0)

    if n == 0 or m == 0:
        ax.set_axis_off()
        ax.text(0.5, 0.6, "There are no preferences to show.",
                transform=ax.transAxes, ha="center", va="center",
                fontsize=11.0, color=_INK)
        ax.text(0.5, 0.5,
                f"The problem has {_n(n)} people and {_n(m)} desks in the pool.",
                transform=ax.transAxes, ha="center", va="top", fontsize=8.4,
                color=_MUTED)
        return n_pages

    rank = np.asarray(problem.rank)
    eligible = (np.asarray(problem.eligible) if problem.eligible is not None
                else np.ones((n, m), dtype=bool))

    # Row order. Named: by name. Anonymous: by the preference vector itself, so
    # the order is derived from the data on the page and not from the email
    # sort that produced problem.people.
    if anonymize:
        order = sorted(range(n), key=lambda i: (tuple(int(v) for v in rank[i]), i))
    else:
        order = sorted(
            range(n),
            key=lambda i: (problem.person_names[problem.people[i]].casefold(),
                           problem.people[i]),
        )

    start = page * rows_per_page
    idx = order[start:start + rows_per_page]
    rows_here = len(idx)

    sub_rank = rank[idx, :].astype(float)
    sub_elig = eligible[idx, :]
    masked = np.ma.masked_where(sub_rank < 1, sub_rank)

    base = np.where(sub_elig, 1.0, 0.0)
    ax.imshow(base, cmap=ListedColormap(["#c9c9c9", "#f2f2f2"]), vmin=0.0,
              vmax=1.0, aspect="auto", interpolation="nearest", zorder=1,
              extent=(-0.5, m - 0.5, rows_here - 0.5, -0.5))

    cmap = ListedColormap(rank_colors(k))
    cmap.set_bad(color=(0.0, 0.0, 0.0, 0.0))
    norm = BoundaryNorm(np.arange(0.5, k + 1.5, 1.0), k)
    im = ax.imshow(masked, cmap=cmap, norm=norm, aspect="auto",
                   interpolation="nearest", zorder=2,
                   extent=(-0.5, m - 0.5, rows_here - 0.5, -0.5))

    # Cell separators, while they are still thinner than the cells.
    if m <= 90 and rows_here <= 90:
        ax.set_xticks(np.arange(-0.5, m, 1.0), minor=True)
        ax.set_yticks(np.arange(-0.5, rows_here, 1.0), minor=True)
        ax.grid(which="minor", color="white", linewidth=0.5, zorder=3)
        ax.tick_params(which="minor", length=0.0)

    if solution is not None and not anonymize:
        desk_col = {d: j for j, d in enumerate(problem.desks)}
        assigned = {a.email: a.desk_id for a in solution.assignments}
        for r, i in enumerate(idx):
            desk = assigned.get(problem.people[i])
            if desk is None or desk not in desk_col:
                continue
            ax.add_patch(Rectangle(
                (desk_col[desk] - 0.5, r - 0.5), 1.0, 1.0, facecolor="none",
                edgecolor=_RULE, linewidth=1.25, zorder=5,
            ))

    # Row labels.
    row_pts = (float(fig.get_size_inches()[1]) * rect[3] * 72.0) / max(rows_here, 1)
    row_size = float(np.clip(row_pts * 0.62, 4.2, 8.4))
    if anonymize:
        width = max(len(str(n)), 3)
        row_labels = [f"P{start + r + 1:0{width}d}" for r in range(rows_here)]
    else:
        margin_in = rect[0] * float(fig.get_size_inches()[0])
        budget = max(_fit_chars(margin_in, row_size) - 2, 6)
        row_labels = [_truncate(problem.person_names[problem.people[i]], budget)
                      for i in idx]
    ax.set_yticks(np.arange(rows_here))
    ax.set_yticklabels(row_labels, fontsize=row_size)
    ax.tick_params(axis="y", length=0.0, pad=3.0)

    # Column labels, thinned if there are more desks than labels can fit.
    col_pts = (float(fig.get_size_inches()[0]) * rect[2] * 72.0) / max(m, 1)
    col_size = float(np.clip(col_pts * 0.80, 4.2, 8.0))
    step = max(1, math.ceil(m / max(int(_axes_width_in(ax) * 72.0 / 6.0), 1)))
    ticks = list(range(0, m, step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([problem.desk_labels[problem.desks[j]] for j in ticks],
                       fontsize=col_size, rotation=90.0)
    ax.tick_params(axis="x", length=0.0, pad=3.0)
    ax.set_xlim(-0.5, m - 0.5)
    ax.set_ylim(rows_here - 0.5, -0.5)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)

    # An explicit cax, not `ax=ax`: colorbar(ax=...) steals space by *moving*
    # the axes, which would invalidate the geometry every label size above was
    # computed from.
    cax = fig.add_axes([
        min(rect[0] + rect[2] + 0.022, 0.955),
        rect[1] + rect[3] * 0.28,
        0.015,
        rect[3] * 0.44,
    ])
    cbar = fig.colorbar(im, cax=cax, ticks=np.arange(1, k + 1))
    cbar.ax.set_yticklabels([_ordinal(r) for r in range(1, k + 1)], fontsize=7.2)
    cbar.set_label("position in that person's ranked list", fontsize=7.6)
    cbar.outline.set_visible(False)
    cbar.ax.tick_params(length=0.0)

    ax.legend(
        handles=[
            Patch(facecolor="#f2f2f2", edgecolor="#d0d0d0", linewidth=0.5,
                  label="eligible, not ranked"),
            Patch(facecolor="#c9c9c9", edgecolor="#b0b0b0", linewidth=0.5,
                  label="not eligible for this zone"),
        ] + ([Line2D([0], [0], marker="s", markerfacecolor="none",
                     markeredgecolor=_RULE, markersize=7.0, linestyle="none",
                     label="desk actually assigned")]
             if (solution is not None and not anonymize) else []),
        loc="lower left", bbox_to_anchor=(0.0, 1.012), ncols=3, frameon=False,
        fontsize=7.4, handlelength=1.1, handletextpad=0.5, columnspacing=1.4,
    )

    footer = (f"people {_n(start + 1)}–{_n(start + rows_here)} of {_n(n)}"
              f"    ·    {_n(m)} desks in the pool    ·    K = {k}"
              f"    ·    curve '{problem.curve_name}'")
    if anonymize and solution is not None:
        footer += ("    ·    assignment outlines suppressed: with public "
                   "assignments they would re-identify pseudonymous rows")
    fig.text(rect[0], rect[1] - 0.048, _wrap_for(footer, _axes_width_in(ax), 7.2),
             ha="left", va="top", fontsize=7.2, color=_MUTED, linespacing=1.35)

    if caption:
        fig.text(
            rect[0], rect[1] - 0.048 - 0.030,
            _wrap_for(
                "One row per person and one column per desk: colour is where "
                "that desk sat in that person's ranked list, pale grey means "
                "the person could have sat there but did not rank it, and "
                "darker grey means the desk is outside the zones they are "
                "eligible for.",
                _axes_width_in(ax), 7.2),
            ha="left", va="top", fontsize=7.2, color=_MUTED, style="italic",
            linespacing=1.35,
        )
    return n_pages
