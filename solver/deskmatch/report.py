"""The results PDFs (SPEC §7): public, coordinator, and diagnostic.

This module owns the *document* — page order, the narrative, the footers, the
provenance table and the privacy guard. The individual figures live in
``figures_stats`` (charts and tables) and ``figures_map`` (floor-plan work);
this file only arranges them, and it is the only place in the package that
turns a figure into bytes on disk. Keeping the arrangement separate from the
drawing is what makes it possible to *state* which figures an audience may see
and then mechanically *check* it.

Nothing here is written in terms of the size of the problem. The number of
rooms, desks, people, zones, rank categories and pages are all read off the
objects at runtime; ``K = solution.k``, full stop (invariant I1).

Determinism (invariant I3)
--------------------------
The PDF is part of the reproducibility target, so:

* The ``PdfPages`` object is opened through :func:`figures_map.open_pdf`, **not**
  ``PdfPages`` directly. matplotlib freezes ``pdf.compression`` and
  ``pdf.fonttype`` from the *caller's* rcParams at construction time, so opening
  it inside the house rc context is the only way the bytes stop depending on
  whatever rcParams the calling process happened to be carrying. Every metadata
  key is set explicitly and the two clock-derived ones are ``None``, so
  matplotlib omits them instead of stamping ``datetime.today()``.
* No wall clock is read anywhere in this module, and the one clock-derived
  provenance field (``generated_at``) is deliberately **not printed** on the
  provenance page. It is excluded from the canonical hash for the same reason;
  printing it would make two identical runs produce different PDFs, destroying
  the one property the hash exists to certify. The page says so.
* No ``set`` is ever iterated. Everything that reaches a page is a list built in
  a stated order or an explicit ``sorted()`` with a total tie-break.
* There is no sampling here, so there is no RNG. If a future page needs one it
  must come from ``deskmatch.scoring.make_rng(seed + "::<purpose>")``.
* ``pyplot`` is never imported: figures are bare ``Figure`` objects, so there is
  no global registry and no interactive-backend state to differ between
  machines.

Privacy (SPEC §7.2) — a correctness property, not an intention
--------------------------------------------------------------
The default output is the **public** report; the coordinator report requires
``full=True``. The rule being enforced is: aggregate figures are public;
per-person *rankings* are never attributed in the public report; final
*assignments* are public, because people need to know where they sit.

Three independent mechanisms enforce it, and :func:`assert_public_safe` runs the
last two against the bytes that were actually written:

1. **Allow-list (structural).** Every page carries a kind, and the public build
   refuses to write if any page carries a coordinator-only kind or a kind that
   has never been classified. The kinds are cross-checked against
   ``figures_stats.FIGURE_AUDIENCE`` at import time so the two statements of the
   same rule cannot drift apart silently. This is the guarantee; the rest is
   verification.
2. **Text-layer audit (empirical).** The finished PDF is re-opened, its text
   layer extracted *with positions*, and every desk id found is attributed to
   the person named nearest it. A desk attributed to somebody it was not
   assigned to is a violation. See :func:`audit_public_pdf` for the four checks
   and their honest limits.
3. **Independent parse (corroboration).** If ``pypdf`` happens to be installed,
   the same question is asked again through a completely different extractor.
   It is optional — it is not in ``requirements.lock`` — and the primary audit
   is self-contained: stdlib ``zlib`` and ``re`` only.
4. **Private-note search (SPEC §7.3).** When the run has private notes, the same
   text layer is searched for them. Those are a different kind of secret from a
   ranking — disclosive on their own, not merely when attributed — so the rule
   is absence, not anonymity. Check G in :func:`audit_public_pdf`.

Floor-plan images
-----------------
There normally are none: ``rooms.json`` is a schematic, no room declares an
``image``, and the map pages draw the desk rectangles on plain paper. That is
the intended state and it produces no note.

If a room *does* declare an image and the file is not there, ``figures_map``
degrades to drawing the desk polygons on a neutral background with a visible
note, and this module surfaces that note in ``ReportResult.notes``. No page is
ever blank and no run ever fails for a missing image.
"""

from __future__ import annotations

import math
import os
import re
import textwrap
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import matplotlib

# Explicit, per the repo's rendering contract. pyplot is never imported.
matplotlib.use("Agg")

import numpy as np  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

from . import accommodations as acc_mod  # noqa: E402
from . import figures_map as fmap  # noqa: E402
from . import figures_stats as fstats  # noqa: E402
from . import provenance as prov_mod  # noqa: E402
from .errors import PrivacyError  # noqa: E402  (re-exported; see __all__)
from .types import Infeasibility, Problem, Solution  # noqa: E402

__all__ = [
    "PAGE_SIZE",
    "AUDIENCE_PUBLIC",
    "AUDIENCE_COORDINATOR",
    "PUBLIC_PAGE_KINDS",
    "COORDINATOR_ONLY_PAGE_KINDS",
    "PrivacyError",
    "PrivacyFinding",
    "PrivacyAudit",
    "ReportResult",
    "TextRun",
    "build_report",
    "build_diagnostic_report",
    "audit_public_pdf",
    "assert_public_safe",
    "extract_text_runs",
]


# ==========================================================================
# House constants
# ==========================================================================

#: Landscape US Letter. Every page in the document is this size, including the
#: floor-plan pages, which are re-homed onto it (see `_rehome`) rather than
#: allowed to set their own MediaBox. A reader flipping through a PDF whose
#: pages change shape assumes something is broken.
PAGE_SIZE: tuple[float, float] = (11.0, 8.5)

MARGIN_L = 0.80
MARGIN_R = 0.80
MARGIN_T = 0.66
MARGIN_B = 0.62
FOOTER_Y_IN = 0.36

AUDIENCE_PUBLIC = "PUBLIC"
AUDIENCE_COORDINATOR = "COORDINATOR — CONTAINS INDIVIDUAL PREFERENCES"

# Mirrors the palette in figures_stats/figures_map so the prose pages and the
# figure pages read as one document.
_INK = "#1b1b1b"
_MUTED = "#6f6f6f"
_FAINT = "#b4b4b4"
_HAIRLINE = "#d8d5d0"
_WARN = "#8a2b06"
_ACCENT = "#2f4f6f"

#: DejaVu Sans averages very close to this fraction of the point size per
#: character. Used only for wrapping, never for anything that reaches a number.
_EM_WIDTH = 0.60

#: Page kinds. The public build asserts that no page it produced carries a kind
#: from the coordinator set — the structural half of the privacy guarantee, and
#: it is checked before a single byte is written.
PUBLIC_PAGE_KINDS: frozenset[str] = frozenset({
    "title",
    "method",
    "outcome",
    "rank-strip",
    "map",
    "baseline",
    "baseline-overlay",
    "contested",
    "curve-sensitivity",
    "seed-sensitivity",
    "assignment-table",
    "provenance",
})

COORDINATOR_ONLY_PAGE_KINDS: frozenset[str] = frozenset({
    "preference-matrix",
    "rank-by-name",
    "ledger",
    "accommodations",
    "diagnostic-summary",
    "diagnostic-blocking",
    "diagnostic-roster",
    "diagnostic-round2",
})

#: Which page kind carries which `figures_stats` figure. Only used to check that
#: this module and `figures_stats.FIGURE_AUDIENCE` agree about who may see what.
_KIND_OF_FIGURE: Mapping[str, str] = {
    "rank_distribution": "outcome",
    "cumulative_satisfaction": "outcome",
    "baseline_comparison": "baseline",
    "curve_sensitivity": "curve-sensitivity",
    "seed_sensitivity": "seed-sensitivity",
    "rank_received_by_person(anonymize=True)": "rank-strip",
    "rank_received_by_person(anonymize=False)": "rank-by-name",
    "assignment_table": "assignment-table",
    "preference_matrix": "preference-matrix",
}


def _check_audience_agreement() -> None:
    """`figures_stats.FIGURE_AUDIENCE` and the page kinds here state one rule.

    Stating a privacy rule twice is one time too many, but the figure module
    cannot import this one without a cycle. So instead the overlap is asserted
    the moment this module is imported: a figure downgraded from coordinator to
    public in one file and not the other fails here, at import, rather than in
    a PDF that has already been mailed round the department.
    """
    for figure, audience in fstats.FIGURE_AUDIENCE.items():
        kind = _KIND_OF_FIGURE.get(figure)
        if kind is None:
            raise AssertionError(
                f"figures_stats.FIGURE_AUDIENCE lists {figure!r}, which report.py "
                f"has never heard of. Add it to _KIND_OF_FIGURE and decide which "
                f"page it belongs on."
            )
        here = "public" if kind in PUBLIC_PAGE_KINDS else "coordinator"
        if here != audience:
            raise AssertionError(
                f"figures_stats says {figure!r} is {audience!r} but report.py "
                f"puts it on a {here!r} page ({kind!r}). Same rule, two files; "
                f"fix both."
            )


_check_audience_agreement()


# `PrivacyError` is imported from `errors` and re-exported here, where it used to
# be defined. It moved because `accommodations.assert_absent_from` enforces the
# same rule against the non-PDF artefacts, and one rule with two exception types
# is how a caller ends up catching only half of it.


# ==========================================================================
# Small text helpers
# ==========================================================================


def _fit_chars(width_in: float, size: float, *, lo: int = 8, hi: int = 400) -> int:
    if size <= 0:
        return hi
    return int(min(max(round(width_in * 72.0 / (size * _EM_WIDTH)), lo), hi))


def _wrap(text: str, width_in: float, size: float) -> str:
    """Wrap to the physical width available. Blank lines are preserved."""
    budget = _fit_chars(width_in, size)
    out: list[str] = []
    for para in str(text).split("\n"):
        if not para.strip():
            out.append("")
            continue
        out.append(textwrap.fill(para, budget))
    return "\n".join(out)


def _n(value: float | int) -> str:
    return f"{int(round(value)):,}"


def _people(n: int) -> str:
    return "1 person" if n == 1 else f"{_n(n)} people"


def _ordinal(n: int) -> str:
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _mean_rank(hist: Sequence[int]) -> float:
    total = int(sum(hist))
    if total == 0:
        return float("nan")
    return sum((i + 1) * c for i, c in enumerate(hist)) / total


def _fmt_number(value: Any) -> str:
    """Render a curve value (Fraction/int/float) the way a human wrote it."""
    from fractions import Fraction

    if isinstance(value, Fraction):
        return str(int(value)) if value.denominator == 1 else f"{float(value):g}"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _short_hash(digest: str | None, n: int = 16) -> str:
    if not digest:
        return "(not supplied)"
    return f"{digest[:n]}…" if len(digest) > n else digest


# ==========================================================================
# One sheet of paper
# ==========================================================================


class _Sheet:
    """A page with a top-down layout cursor, measured in inches.

    Everything is positioned in inches and converted to figure fractions at the
    last moment, so a different `page_size` moves the whole document rather than
    breaking it, and a block of text that grows (more roster conflicts, a longer
    seed string) pushes what follows down instead of overprinting it.
    """

    def __init__(
        self,
        page_size: tuple[float, float] = PAGE_SIZE,
        *,
        margins: tuple[float, float, float, float] | None = None,
    ) -> None:
        self.w, self.h = float(page_size[0]), float(page_size[1])
        self.fig = Figure(figsize=(self.w, self.h))
        FigureCanvasAgg(self.fig)
        self.fig.set_facecolor("white")
        left, right, top, bottom = margins or (MARGIN_L, MARGIN_R, MARGIN_T, MARGIN_B)
        self.left, self.right, self.top, self.bottom = left, right, top, bottom
        self.cursor = self.h - top

    # -- geometry ---------------------------------------------------------

    @property
    def text_width(self) -> float:
        return self.w - self.left - self.right

    @property
    def room(self) -> float:
        """Inches of usable page left below the cursor."""
        return self.cursor - self.bottom

    def fx(self, x_in: float) -> float:
        return x_in / self.w

    def fy(self, y_in: float) -> float:
        return y_in / self.h

    @staticmethod
    def line_h(size: float, spacing: float = 1.42) -> float:
        return size / 72.0 * spacing

    # -- primitives -------------------------------------------------------

    def text(self, x_in: float, y_in: float, s: str, **kw: Any) -> Any:
        return self.fig.text(self.fx(x_in), self.fy(y_in), s, **kw)

    def heading(
        self,
        s: str,
        *,
        size: float = 15.0,
        color: str = _INK,
        weight: str = "semibold",
        gap_before: float = 0.0,
        gap_after: float = 0.13,
    ) -> None:
        self.cursor -= gap_before
        self.text(self.left, self.cursor, s, ha="left", va="top", fontsize=size,
                  color=color, fontweight=weight)
        self.cursor -= self.line_h(size, 1.18) + gap_after

    def kicker(self, s: str, *, color: str = _MUTED) -> None:
        """The small uppercase section label at the top of a figure page."""
        self.text(self.left, self.cursor, s.upper(), ha="left", va="top",
                  fontsize=7.4, color=color, fontweight="bold")
        self.cursor -= self.line_h(7.4, 1.6)

    def block(
        self,
        s: str,
        *,
        size: float = 9.2,
        color: str = _INK,
        indent: float = 0.0,
        width: float | None = None,
        gap_before: float = 0.0,
        gap_after: float = 0.10,
        spacing: float = 1.42,
        style: str | None = None,
        weight: str | None = None,
        family: str | None = None,
    ) -> None:
        self.cursor -= gap_before
        avail = width if width is not None else (self.text_width - indent)
        wrapped = _wrap(s, avail, size)
        kw: dict[str, Any] = {}
        if style:
            kw["style"] = style
        if weight:
            kw["fontweight"] = weight
        if family:
            kw["family"] = family
        self.text(self.left + indent, self.cursor, wrapped, ha="left", va="top",
                  fontsize=size, color=color, linespacing=spacing, **kw)
        self.cursor -= (wrapped.count("\n") + 1) * self.line_h(size, spacing) + gap_after

    def bullets(self, items: Sequence[str], *, size: float = 9.0,
                color: str = _INK, indent: float = 0.0,
                gap_after: float = 0.10) -> None:
        for item in items:
            self.text(self.left + indent, self.cursor, "·", ha="left", va="top",
                      fontsize=size, color=_FAINT)
            self.block(item, size=size, color=color, indent=indent + 0.17,
                       gap_after=0.045)
        self.cursor -= gap_after

    def rule(self, *, gap_before: float = 0.05, gap_after: float = 0.12,
             color: str = _HAIRLINE, lw: float = 0.7,
             width: float | None = None) -> None:
        self.cursor -= gap_before
        span = width if width is not None else self.text_width
        self.fig.add_artist(Line2D(
            [self.fx(self.left), self.fx(self.left + span)],
            [self.fy(self.cursor), self.fy(self.cursor)],
            color=color, linewidth=lw, transform=self.fig.transFigure,
        ))
        self.cursor -= gap_after

    def kv_rows(
        self,
        rows: Sequence[tuple[str, str]],
        *,
        key_w: float = 2.55,
        size: float = 8.6,
        key_color: str = _MUTED,
        value_color: str = _INK,
        value_family: str | None = None,
        gap: float = 0.045,
        gap_after: float = 0.10,
    ) -> None:
        value_w = self.text_width - key_w
        for key, value in rows:
            wrapped = _wrap(str(value), value_w, size)
            self.text(self.left, self.cursor, key, ha="left", va="top",
                      fontsize=size, color=key_color)
            kw: dict[str, Any] = {}
            if value_family:
                kw["family"] = value_family
            self.text(self.left + key_w, self.cursor, wrapped, ha="left", va="top",
                      fontsize=size, color=value_color, linespacing=1.38, **kw)
            self.cursor -= (wrapped.count("\n") + 1) * self.line_h(size, 1.38) + gap
        self.cursor -= gap_after

    def axes(self, *, x_in: float, y_in: float, w_in: float, h_in: float) -> Axes:
        return self.fig.add_axes(
            (self.fx(x_in), self.fy(y_in), self.fx(w_in), self.fy(h_in))
        )


# ==========================================================================
# The deck: pages in order, footers stamped once the total is known
# ==========================================================================


@dataclass
class _PageRecord:
    fig: Figure
    kind: str
    notes: tuple[str, ...] = ()


class _Deck:
    """Every page of one document, in order.

    Pages are built first and written last, because the footer has to say
    "page 3 of 27" and the total is not knowable until the last table has
    paginated itself.
    """

    def __init__(self, page_size: tuple[float, float], audience: str,
                 response_hash: str, subtitle: str = "") -> None:
        self.page_size = page_size
        self.audience = audience
        self.response_hash = response_hash
        self.subtitle = subtitle
        self.pages: list[_PageRecord] = []
        self.notes: list[str] = []

    def sheet(self, kind: str, **kw: Any) -> _Sheet:
        sheet = _Sheet(self.page_size, **kw)
        self.add(sheet.fig, kind)
        return sheet

    def add(self, fig: Figure, kind: str, notes: Sequence[str] = ()) -> Figure:
        self.pages.append(_PageRecord(fig, kind, tuple(notes)))
        self.notes.extend(notes)
        return fig

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(p.kind for p in self.pages)

    # -- the structural half of the privacy guarantee ---------------------

    def assert_audience(self) -> None:
        if self.audience != AUDIENCE_PUBLIC:
            return
        present = sorted(dict.fromkeys(p.kind for p in self.pages))
        bad = [k for k in present if k in COORDINATOR_ONLY_PAGE_KINDS]
        if bad:
            raise PrivacyError(
                "the public report was built with coordinator-only page(s): "
                + ", ".join(bad)
                + ". Per SPEC §7.2 per-person rankings are never attributed in "
                "the public report. This is a bug in report.py, not in the data."
            )
        unknown = [k for k in present if k not in PUBLIC_PAGE_KINDS]
        if unknown:
            raise PrivacyError(
                "the public report contains page kind(s) that have never been "
                "classified: " + ", ".join(unknown) + ". Add them to "
                "PUBLIC_PAGE_KINDS (after deciding they really are public) or to "
                "COORDINATOR_ONLY_PAGE_KINDS."
            )

    def write(self, pdf: Any) -> int:
        self.assert_audience()
        total = len(self.pages)
        for i, page in enumerate(self.pages, start=1):
            _stamp_footer(
                page.fig, page_no=i, total=total, audience=self.audience,
                response_hash=self.response_hash, subtitle=self.subtitle,
            )
            pdf.savefig(page.fig)
            page.fig.clear()
        return total


def _stamp_footer(
    fig: Figure,
    *,
    page_no: int,
    total: int,
    audience: str,
    response_hash: str,
    subtitle: str = "",
) -> None:
    """Page number, response hash prefix, and who this document is for.

    On every page, including the figure pages, because a page photocopied out of
    context is exactly when the audience marking matters.
    """
    w, h = (float(v) for v in fig.get_size_inches())
    y = FOOTER_Y_IN / h
    left = MARGIN_L / w
    right = 1.0 - MARGIN_R / w
    warn = audience != AUDIENCE_PUBLIC

    fig.add_artist(Line2D([left, right], [y + 0.014, y + 0.014], color=_HAIRLINE,
                          linewidth=0.6, transform=fig.transFigure))

    tag = f"responses sha256 {_short_hash(response_hash, 16)}"
    if subtitle:
        tag = f"{subtitle}  ·  {tag}"
    fig.text(left, y, tag, ha="left", va="center", fontsize=6.6, color=_MUTED)
    fig.text(0.5, y, audience, ha="center", va="center", fontsize=6.9,
             color=_WARN if warn else _MUTED,
             fontweight="bold" if warn else "medium")
    fig.text(right, y, f"page {page_no} of {total}", ha="right", va="center",
             fontsize=6.6, color=_MUTED)


# ==========================================================================
# Re-homing a self-sizing figure onto the page
# ==========================================================================


def _rehome(fig: Figure, page_w: float, page_h: float, *, valign: str = "top") -> bool:
    """Place an already-drawn figure on a `page_w` x `page_h` sheet, undistorted.

    `figures_map` sizes its own figures: it picks a height that gives the floor
    plan its true aspect ratio. That is the right thing for a standalone PNG and
    the wrong thing for page 5 of a report, where a MediaBox that changes shape
    halfway through reads as a mistake.

    So rather than rescale — which would stretch the floor plan — this moves
    every artist by a rigid translation in *inches* and grows the paper around
    it. Font sizes are in points and are untouched, so nothing about the drawing
    changes except how much white paper surrounds it.

    Returns False and leaves the figure alone if it does not fit; the caller
    keeps the natural size rather than emitting a clipped page.
    """
    w0, h0 = (float(v) for v in fig.get_size_inches())
    if w0 <= 0.0 or h0 <= 0.0:
        return False
    if w0 > page_w + 1e-6 or h0 > page_h + 1e-6:
        return False
    if abs(w0 - page_w) < 1e-9 and abs(h0 - page_h) < 1e-9:
        return True

    dpi = float(fig.dpi) or 100.0
    dx = (page_w - w0) / 2.0
    dy = (page_h - h0) if valign == "top" else (page_h - h0) / 2.0

    def X(frac: float) -> float:
        return (frac * w0 + dx) / page_w

    def Y(frac: float) -> float:
        return (frac * h0 + dy) / page_h

    sx, sy = w0 / page_w, h0 / page_h

    # Legend anchors read back in *display* pixels, which move the instant the
    # figure is resized -- so snapshot them before touching anything.
    anchors: list[tuple[Any, float, float, float, float]] = []
    for legend in list(fig.legends):
        try:
            bb = legend.get_bbox_to_anchor()
            anchors.append((legend, bb.x0 / dpi, bb.y0 / dpi,
                            bb.width / dpi, bb.height / dpi))
        except Exception:  # pragma: no cover - defensive
            pass

    for ax in list(fig.axes):
        pos = ax.get_position()
        ax.set_position((X(pos.x0), Y(pos.y0), pos.width * sx, pos.height * sy))

    fig_transforms = (fig.transFigure, getattr(fig, "transSubfigure", fig.transFigure))
    for artist in list(fig.texts):
        if artist.get_transform() not in fig_transforms:
            continue
        tx, ty = artist.get_position()
        artist.set_position((X(tx), Y(ty)))

    for legend, ax_in, ay_in, aw_in, ah_in in anchors:
        legend.set_bbox_to_anchor((
            (ax_in + dx) / page_w, (ay_in + dy) / page_h,
            aw_in / page_w, ah_in / page_h,
        ))

    fig.set_size_inches(page_w, page_h)
    return True


# ==========================================================================
# PDF text-layer extraction (stdlib only)
# ==========================================================================


@dataclass(frozen=True)
class TextRun:
    """One text-showing operation, with where it landed on the page.

    Positions are PDF points from the bottom-left of the MediaBox — the same
    coordinates the viewer uses, so "adjacent on the page" means what it says.
    """

    page: int
    x: float
    y: float
    size: float
    text: str


_OBJ_RE = re.compile(rb"(?<![0-9])(\d+)\s+0\s+obj\b")
_XREF_ENTRY_RE = re.compile(rb"(\d{10})\s(\d{5})\s([nf])")


def _xref_offsets(data: bytes) -> dict[int, int] | None:
    """Object offsets from the classic cross-reference table, when there is one.

    Preferred over scanning for `N 0 obj`, because compressed stream data can
    contain those bytes by coincidence and a false object would shadow a real
    one. The scan is kept as a fallback for files without a plain xref.
    """
    tail = data[-2048:]
    last = None
    for last in re.finditer(rb"startxref\s+(\d+)", tail):
        pass
    if last is None:
        return None
    start = int(last.group(1))
    if not 0 <= start < len(data) or data[start:start + 4] != b"xref":
        return None

    out: dict[int, int] = {}
    pos = start + 4
    header = re.compile(rb"\s*(\d+)\s+(\d+)\s*")
    while True:
        head = header.match(data, pos)
        if head is None:
            break
        first, count = int(head.group(1)), int(head.group(2))
        if count < 0 or count > 1_000_000:
            break
        pos = head.end()
        for i in range(count):
            entry = _XREF_ENTRY_RE.match(data, pos)
            if entry is None:
                return out or None
            if entry.group(3) == b"n":
                out[first + i] = int(entry.group(1))
            pos += 20
        if data[pos:pos + 32].lstrip().startswith(b"trailer"):
            break
    return out or None


def _pdf_objects(data: bytes) -> dict[int, tuple[bytes, int | None]]:
    """`object number -> (header bytes, offset of stream data or None)`."""
    out: dict[int, tuple[bytes, int | None]] = {}

    def record(num: int, pos: int) -> None:
        endobj = data.find(b"endobj", pos)
        if endobj == -1:
            endobj = len(data)
        stream = data.find(b"stream", pos)
        if stream != -1 and stream < endobj:
            header = data[pos:stream]
            j = stream + 6
            if data[j:j + 2] == b"\r\n":
                j += 2
            elif data[j:j + 1] in (b"\n", b"\r"):
                j += 1
            out.setdefault(num, (header, j))
        else:
            out.setdefault(num, (data[pos:endobj], None))

    offsets = _xref_offsets(data)
    if offsets:
        for num in sorted(offsets):
            match = _OBJ_RE.match(data, offsets[num])
            if match is None or int(match.group(1)) != num:
                out.clear()
                break
            record(num, match.end())
        if out:
            return out

    for match in _OBJ_RE.finditer(data):
        record(int(match.group(1)), match.end())
    return out


def _stream_bytes(
    data: bytes, objects: Mapping[int, tuple[bytes, int | None]], num: int
) -> bytes | None:
    entry = objects.get(num)
    if entry is None or entry[1] is None:
        return None
    header, start = entry

    length: int | None = None
    indirect = re.search(rb"/Length\s+(\d+)\s+0\s+R", header)
    if indirect is not None:
        target = objects.get(int(indirect.group(1)))
        if target is not None and target[1] is None:
            digits = re.search(rb"(\d+)", target[0])
            if digits is not None:
                length = int(digits.group(1))
    else:
        direct = re.search(rb"/Length\s+(\d+)", header)
        if direct is not None:
            length = int(direct.group(1))

    if length is not None and b"endstream" in data[start + length:start + length + 32]:
        raw = data[start:start + length]
    else:
        end = data.find(b"endstream", start)
        raw = data[start:end].rstrip(b"\r\n") if end != -1 else b""

    if b"/FlateDecode" in header:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            try:
                return zlib.decompressobj().decompress(raw)
            except Exception:
                return None
    return raw


def _utf16be(hex_text: bytes) -> str:
    try:
        raw = bytes.fromhex(hex_text.decode("ascii"))
    except ValueError:
        return ""
    if len(raw) % 2:
        raw += b"\x00"
    return raw.decode("utf-16-be", errors="replace")


def _parse_tounicode(text: bytes) -> dict[int, str]:
    """Parse the `bfchar`/`bfrange` sections of a ToUnicode CMap."""
    out: dict[int, str] = {}
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            out[int(src, 16)] = _utf16be(dst)
    pattern = rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]+>)"
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", text, re.S):
        for lo_h, hi_h, rest in re.findall(pattern, block, re.S):
            lo, hi = int(lo_h, 16), int(hi_h, 16)
            if hi < lo or hi - lo > 65535:
                continue
            if rest.startswith(b"["):
                for offset, item in enumerate(re.findall(rb"<([0-9A-Fa-f]+)>", rest)):
                    out[lo + offset] = _utf16be(item)
            else:
                base = int(rest[1:-1], 16)
                for code in range(lo, hi + 1):
                    value = base + (code - lo)
                    out[code] = chr(value) if 0 <= value < 0x110000 else "�"
    return out


_ESCAPES = {b"n": 0x0A, b"r": 0x0D, b"t": 0x09, b"b": 0x08, b"f": 0x0C,
            b"(": 0x28, b")": 0x29, b"\\": 0x5C}


def _scan_literal_string(data: bytes, i: int) -> tuple[bytes, int]:
    """Read a `( ... )` string starting at `data[i] == '('`. Returns (raw, next)."""
    depth = 0
    out = bytearray()
    while i < len(data):
        ch = data[i]
        if ch == 0x5C:  # backslash
            nxt = data[i + 1:i + 2]
            if not nxt:
                break
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                i += 2
                continue
            if nxt in (b"\n", b"\r"):
                i += 2
                if data[i - 1:i] == b"\r" and data[i:i + 1] == b"\n":
                    i += 1
                continue
            if nxt.isdigit():
                j = i + 1
                digits = b""
                while j < len(data) and len(digits) < 3 and data[j:j + 1].isdigit():
                    digits += data[j:j + 1]
                    j += 1
                out.append(int(digits, 8) & 0xFF)
                i = j
                continue
            out.append(nxt[0])
            i += 2
            continue
        if ch == 0x28:  # (
            depth += 1
            if depth > 1:
                out.append(ch)
            i += 1
            continue
        if ch == 0x29:  # )
            depth -= 1
            if depth == 0:
                return bytes(out), i + 1
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return bytes(out), i


_NUM_RE = re.compile(rb"[+-]?(?:\d+\.\d*|\.\d+|\d+)")
_NAME_RE = re.compile(rb"/[^\s()<>\[\]{}/%]*")
_OP_RE = re.compile(rb"[A-Za-z'\"*][A-Za-z0-9'\"*]*")
_HEX_RE = re.compile(rb"<([0-9A-Fa-f\s]*)>")


def _tokens(data: bytes) -> Iterable[tuple[str, Any]]:
    i, n = 0, len(data)
    while i < n:
        ch = data[i:i + 1]
        if ch.isspace() or ch == b"\x00":
            i += 1
            continue
        if ch == b"%":
            eol = data.find(b"\n", i)
            i = n if eol == -1 else eol + 1
            continue
        if ch == b"(":
            raw, i = _scan_literal_string(data, i)
            yield ("str", raw)
            continue
        if data[i:i + 2] == b"<<":
            yield ("other", None)
            i += 2
            continue
        if data[i:i + 2] == b">>":
            yield ("other", None)
            i += 2
            continue
        if ch == b"<":
            match = _HEX_RE.match(data, i)
            if match is not None:
                digits = re.sub(rb"\s", b"", match.group(1))
                if len(digits) % 2:
                    digits += b"0"
                try:
                    yield ("str", bytes.fromhex(digits.decode("ascii")))
                except ValueError:  # pragma: no cover - non-hex payload
                    yield ("str", b"")
                i = match.end()
                continue
            i += 1
            continue
        if ch == b"[":
            yield ("arropen", None)
            i += 1
            continue
        if ch == b"]":
            yield ("arrclose", None)
            i += 1
            continue
        if ch == b"/":
            match = _NAME_RE.match(data, i)
            yield ("name", match.group(0)[1:].decode("latin-1"))
            i = match.end()
            continue
        match = _NUM_RE.match(data, i)
        if match is not None:
            yield ("num", float(match.group(0)))
            i = match.end()
            continue
        match = _OP_RE.match(data, i)
        if match is not None:
            yield ("op", match.group(0).decode("latin-1"))
            i = match.end()
            continue
        i += 1


_IDENTITY: tuple[float, ...] = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mat_mul(a: Sequence[float], b: Sequence[float]) -> tuple[float, ...]:
    """`a` then `b`, in PDF's row-vector convention."""
    return (
        a[0] * b[0] + a[1] * b[2],
        a[0] * b[1] + a[1] * b[3],
        a[2] * b[0] + a[3] * b[2],
        a[2] * b[1] + a[3] * b[3],
        a[4] * b[0] + a[5] * b[2] + b[4],
        a[4] * b[1] + a[5] * b[3] + b[5],
    )


def _decode_codes(raw: bytes, cmap: Mapping[int, str], two_byte: bool) -> str:
    if two_byte:
        if len(raw) % 2:
            raw = raw + b"\x00"
        codes = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]
    else:
        codes = list(raw)
    out: list[str] = []
    for code in codes:
        mapped = cmap.get(code)
        if mapped is None:
            mapped = chr(code) if 0 <= code < 0x110000 else "�"
        out.append(mapped)
    return "".join(out)


def _content_runs(
    stream: bytes, page: int, cmap: Mapping[int, str], two_byte: bool
) -> list[TextRun]:
    """A minimal content-stream interpreter: enough to know what text is where."""
    runs: list[TextRun] = []
    ctm: tuple[float, ...] = _IDENTITY
    stack: list[tuple[float, ...]] = []
    tm: tuple[float, ...] = _IDENTITY
    tlm: tuple[float, ...] = _IDENTITY
    leading = 0.0
    font_size = 0.0
    operands: list[Any] = []

    def numbers(count: int) -> list[float]:
        vals = [v for v in operands if isinstance(v, float)]
        return vals[-count:] if len(vals) >= count else []

    for kind, value in _tokens(stream):
        if kind == "arropen":
            operands.append("[")
            continue
        if kind == "arrclose":
            items: list[Any] = []
            while operands and operands[-1] != "[":
                items.append(operands.pop())
            if operands:
                operands.pop()
            operands.append(list(reversed(items)))
            continue
        if kind in ("num", "str", "name", "other"):
            operands.append(value)
            continue
        if kind != "op":  # pragma: no cover - the tokenizer yields nothing else
            continue

        op = value
        show: list[bytes] = []
        if op == "q":
            stack.append(ctm)
        elif op == "Q":
            ctm = stack.pop() if stack else _IDENTITY
        elif op == "cm":
            vals = numbers(6)
            if len(vals) == 6:
                ctm = _mat_mul(tuple(vals), ctm)
        elif op == "BT":
            tm = tlm = _IDENTITY
        elif op == "Tf":
            vals = numbers(1)
            if vals:
                font_size = vals[0]
        elif op == "TL":
            vals = numbers(1)
            if vals:
                leading = vals[0]
        elif op in ("Td", "TD"):
            vals = numbers(2)
            if len(vals) == 2:
                if op == "TD":
                    leading = -vals[1]
                tlm = _mat_mul((1.0, 0.0, 0.0, 1.0, vals[0], vals[1]), tlm)
                tm = tlm
        elif op == "Tm":
            vals = numbers(6)
            if len(vals) == 6:
                tm = tlm = tuple(vals)
        elif op == "T*":
            tlm = _mat_mul((1.0, 0.0, 0.0, 1.0, 0.0, -leading), tlm)
            tm = tlm
        elif op == "Tj":
            show = [v for v in operands if isinstance(v, bytes)][-1:]
        elif op == "TJ":
            arrays = [v for v in operands if isinstance(v, list)]
            if arrays:
                show = [v for v in arrays[-1] if isinstance(v, bytes)]
        elif op in ("'", '"'):
            tlm = _mat_mul((1.0, 0.0, 0.0, 1.0, 0.0, -leading), tlm)
            tm = tlm
            show = [v for v in operands if isinstance(v, bytes)][-1:]

        if show:
            text = "".join(_decode_codes(p, cmap, two_byte) for p in show)
            if text.strip():
                combined = _mat_mul(tm, ctm)
                scale = math.hypot(combined[0], combined[1]) or 1.0
                runs.append(TextRun(page, combined[4], combined[5],
                                    font_size * scale, text))
        operands = []
    return runs


def extract_text_runs(
    data: bytes,
) -> tuple[tuple[TextRun, ...], tuple[tuple[float, float], ...], tuple[str, ...]]:
    """Pull the text layer out of a PDF this package wrote.

    Returns `(runs, page_sizes, notes)`. Deliberately a small reader: it
    understands the subset of PDF that matplotlib emits — a classic xref table,
    Flate-compressed content streams, Type0/Identity-H TrueType fonts, no
    XObject forms. That is enough to audit our own output and needs nothing
    outside the standard library, which matters because the audit has to run
    wherever the report is generated.
    """
    notes: list[str] = []
    objects = _pdf_objects(data)

    # ---- ToUnicode maps -------------------------------------------------
    cmap: dict[int, str] = {}
    conflicts = 0
    for num in objects:
        if objects[num][1] is None:
            continue
        stream = _stream_bytes(data, objects, num)
        if not stream or b"begincmap" not in stream:
            continue
        for code, text in _parse_tounicode(stream).items():
            if code in cmap and cmap[code] != text:
                conflicts += 1
                continue
            cmap[code] = text
    if conflicts:
        notes.append(
            f"{conflicts} character code(s) are mapped differently by different "
            f"embedded fonts; the first mapping was kept, so extracted text may "
            f"be wrong for those characters."
        )
    two_byte = b"/Identity-H" in data
    if not two_byte:
        notes.append(
            "no /Identity-H font found, so character codes were read as single "
            "bytes. If the PDF was written with pdf.fonttype 3 the text layer is "
            "a set of glyph procedures and this audit is much weaker."
        )

    # ---- page order -----------------------------------------------------
    kids: list[int] = []
    for num in objects:
        header, start = objects[num]
        if start is not None or b"/Type /Pages" not in header:
            continue
        arr = re.search(rb"/Kids\s*\[(.*?)\]", header, re.S)
        if arr is not None:
            kids = [int(x) for x in re.findall(rb"(\d+)\s+0\s+R", arr.group(1))]
        break

    runs: list[TextRun] = []
    sizes: list[tuple[float, float]] = []

    if kids:
        for index, kid in enumerate(kids):
            entry = objects.get(kid)
            if entry is None:  # pragma: no cover - malformed page tree
                continue
            header = entry[0]
            box = re.search(rb"/MediaBox\s*\[([^\]]*)\]", header)
            nums = ([float(v) for v in re.findall(rb"[-+0-9.]+", box.group(1))]
                    if box is not None else [])
            sizes.append((nums[2] - nums[0], nums[3] - nums[1])
                         if len(nums) == 4 else (0.0, 0.0))
            contents = re.search(rb"/Contents\s+(\d+)\s+0\s+R", header)
            if contents is None:  # pragma: no cover - empty page
                continue
            stream = _stream_bytes(data, objects, int(contents.group(1)))
            if stream:
                runs.extend(_content_runs(stream, index, cmap, two_byte))
    else:  # pragma: no cover - matplotlib always writes a /Pages node
        notes.append(
            "no /Pages node was found; content streams were read in file order, "
            "so page numbers may not match the viewer's."
        )
        index = 0
        for num in objects:
            if objects[num][1] is None:
                continue
            stream = _stream_bytes(data, objects, num)
            if not stream or b"BT" not in stream:
                continue
            runs.extend(_content_runs(stream, index, cmap, two_byte))
            sizes.append((0.0, 0.0))
            index += 1

    return tuple(runs), tuple(sizes), tuple(notes)


# ==========================================================================
# The privacy guard
# ==========================================================================


@dataclass(frozen=True)
class PrivacyFinding:
    check: str
    page: int
    person: str
    desk: str
    detail: str

    def render(self) -> str:
        return f"[{self.check}] page {self.page + 1}: {self.detail}"


@dataclass(frozen=True)
class PrivacyAudit:
    ok: bool
    findings: tuple[PrivacyFinding, ...]
    checks: tuple[str, ...]
    limitations: tuple[str, ...]
    n_pages: int
    n_text_runs: int
    n_names_located: int
    attributions: tuple[tuple[str, str], ...]

    def render(self) -> str:
        lines = [
            ("PASS" if self.ok else "FAIL")
            + f": {self.n_text_runs} text run(s) over {self.n_pages} page(s); "
              f"{self.n_names_located} name occurrence(s) located; "
              f"{len(self.attributions)} desk attribution(s) checked."
        ]
        lines.extend("  check: " + c for c in self.checks)
        lines.extend("  VIOLATION " + f.render() for f in self.findings)
        lines.extend("  limitation: " + lim for lim in self.limitations)
        return "\n".join(lines)


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _public_facts(
    config: Any, problem: Problem | None, solution: Solution | None
) -> tuple[dict[str, str], dict[str, set[str]], tuple[str, ...]]:
    """`(name -> key, key -> desks it may sit beside, every desk id)`.

    The "public facts" are exactly what SPEC §7.2 permits to be attributed: a
    person's own final assignment, or the desk they are keeping. Any other desk
    id next to their name is a leak of their submitted ranking.
    """
    rooms = getattr(config, "rooms", config)
    all_desks = tuple(getattr(rooms, "desk_ids", ()) or ())

    names: dict[str, str] = {}
    allowed: dict[str, set[str]] = {}

    def register(key: str, name: str, desk: str | None) -> None:
        if name and name.strip():
            names.setdefault(_normalise(name), key)
        names.setdefault(_normalise(key), key)
        bucket = allowed.setdefault(key, set())
        if desk:
            bucket.add(desk)

    for person in getattr(getattr(config, "roster", None), "people", ()) or ():
        register(person.email, person.name,
                 person.current_desk if person.keeps_desk else None)

    if problem is not None:
        for email in problem.people:
            register(email, problem.person_names.get(email, ""), None)

    if solution is not None:
        for assignment in solution.assignments:
            register(assignment.email, assignment.name, assignment.desk_id)

    return names, allowed, all_desks


def _desk_pattern(desk_ids: Sequence[str],
                  *, ignore_case: bool = False) -> re.Pattern[str] | None:
    ids = sorted({d for d in desk_ids if d}, key=lambda s: (-len(s), s))
    if not ids:
        return None
    body = "|".join(re.escape(d) for d in ids)
    flags = re.IGNORECASE if ignore_case else 0
    return re.compile(rf"(?<![0-9A-Za-z_])({body})(?![0-9A-Za-z_])", flags)


def _match_people(text: str, names: Mapping[str, str]) -> list[str]:
    """Which people (keys) a text run refers to.

    Table cells truncate long names with an ellipsis, so an exact match is not
    enough: a run names somebody if it is a prefix of their name (at least four
    characters, so "Ann" cannot match three people at once) or if their whole
    name appears inside it.
    """
    core = _normalise(text).rstrip("…").rstrip(". ").strip()
    if len(core) < 4:
        return []
    hits: list[str] = []
    for name, key in names.items():
        if len(name) < 4:
            continue
        if name == core or name.startswith(core) or name in core:
            hits.append(key)
    return sorted(dict.fromkeys(hits))


def _cluster_lines(runs: Sequence[TextRun]) -> list[list[TextRun]]:
    """Group the runs on one page into visual lines by baseline."""
    ordered = sorted(runs, key=lambda r: (-r.y, r.x))
    lines: list[list[TextRun]] = []
    for run in ordered:
        if lines:
            ref = lines[-1][0]
            tol = max(0.35 * max(run.size, ref.size), 1.0)
            if abs(run.y - ref.y) <= tol:
                lines[-1].append(run)
                continue
        lines.append([run])
    return lines


def audit_public_pdf(
    path: str | os.PathLike[str] | bytes,
    config: Any,
    problem: Problem | None,
    solution: Solution | None,
    *,
    expect_names: bool | None = None,
    accommodations: Any = None,
) -> PrivacyAudit:
    """Re-read a finished public PDF and look for attributed preference data.

    Five overlapping checks, run against the bytes that were written rather than
    the objects they were drawn from:

    **A — same line.** Runs are clustered into visual lines by baseline. A desk
    id sharing a line with a person's name must be that person's own desk. This
    covers the assignment table, where "adjacent" means "same row" no matter how
    far apart the columns sit.

    **B — same neighbourhood.** A desk id drawn close to a name, in both axes,
    must again be that person's desk. This catches an annotation or caption
    directly under a name, which check A would miss. The vertical reach is
    capped at 45% of the *name pitch* on that page: in a table that is under
    half a row, so it can never bleed into the row above; on a page with a lone
    name there is no other name to bound it and the full reach applies.

    **C — page inventory.** On any page that names at least one person, *every*
    desk id anywhere on the page must be the assignment of somebody named on
    that page. Blunt, and the strongest of the four: it ignores geometry
    entirely, so a leak drawn anywhere on a page with names is caught.

    **D — audience markings.** No coordinator banner may appear, and every page
    must be footed ``PUBLIC``.

    **G — private-note text.** Given ``accommodations``, no five-word run from
    any note may appear anywhere in the document. Unlike A–C this is not about
    attribution: a note is disclosive on its own ("I need to be away from Ada"
    names somebody whether or not the author is identified), so the text must be
    absent outright rather than merely unattributed. The comparison is over
    normalised words, so line wrapping, hyphenation and page breaks cannot hide
    a leak. See ``accommodations.fingerprints``.

    Honest limits, in order of how much they matter:

    * The check keys on desk **ids** (``D07``), not desk **labels** (``7``).
      Labels in a normal ``rooms.json`` are bare integers, indistinguishable
      from ranks, counts, percentages and page numbers, so testing them would
      fire on every page. Every figure this module puts in front of a name
      prints the id as well as the label, and check C confirms the ids on the
      page are exactly the assigned ones — but a *new* figure printing only
      labels next to names would slip through. If you add one, print the id too.
    * Text drawn as vector outlines has no text layer. ``pdf.fonttype`` is
      pinned to 42 so this cannot happen silently, and the audit fails loudly
      (rather than passing vacuously) if it locates no names at all when it
      expected to.
    * Raster content is not read. The only raster in the document is the floor
      plan, which contains no names.
    * Statistical inference — deducing something about one person from the
      aggregate figures — is out of scope and always will be.
    """
    if isinstance(path, (bytes, bytearray)):
        data = bytes(path)
    else:
        with open(os.fspath(path), "rb") as handle:
            data = handle.read()

    runs, page_sizes, notes = extract_text_runs(data)
    names, allowed, all_desk_ids = _public_facts(config, problem, solution)
    pattern = _desk_pattern(all_desk_ids)

    checks = [
        "A same-line attribution",
        "B same-neighbourhood attribution",
        "C page desk-id inventory",
        "D audience markings",
    ]
    limitations = list(notes)
    limitations.append(
        "keys on desk ids, not desk labels: labels are bare integers in this "
        "config and cannot be told apart from ranks or counts in running text."
    )
    limitations.append("reads the text layer only; raster content is not examined.")
    findings: list[PrivacyFinding] = []

    # ---- G: private-note text, anywhere in the document ------------------
    # Done over the whole document rather than page by page, so a note split
    # across a page break is still caught.
    # Keyed on `is not None`, not on truthiness: an Accommodations whose *latest*
    # view is empty can still hold superseded and withdrawn text, and that text
    # must not appear in a public file either.
    note_marks = (
        acc_mod.all_fingerprints(accommodations) if accommodations is not None else ()
    )
    if note_marks:
        checks.append("G private-note text")
        whole = acc_mod.normalise_words(" ".join(run.text for run in runs))
        for email, phrase in note_marks:
            if phrase in whole:
                findings.append(PrivacyFinding(
                    "G", 0, email, "",
                    f"the public report contains private-note text from {email} "
                    f"(\"{acc_mod._excerpt(phrase)}\"). Per SPEC §7.3 these notes "
                    f"never appear outside the coordinator copy.",
                ))
                break   # one is already a failure; do not print the note twice

    n_pages = len(page_sizes) if page_sizes else 1 + max(
        (r.page for r in runs), default=-1)

    by_page: dict[int, list[TextRun]] = {}
    for run in runs:
        by_page.setdefault(run.page, []).append(run)

    n_located = 0
    attributions: list[tuple[str, str]] = []
    label_of = {key: name for name, key in
                sorted(names.items(), key=lambda kv: -len(kv[0]))}

    def flag(check: str, page: int, person: str, desk: str, detail: str) -> None:
        findings.append(PrivacyFinding(check, page, person, desk, detail))

    def desks_in(text: str) -> list[str]:
        if pattern is None:
            return []
        return sorted(dict.fromkeys(pattern.findall(text)))

    for page in sorted(by_page):
        page_runs = by_page[page]
        named_runs = [(run, hits) for run, hits in
                      ((r, _match_people(r.text, names)) for r in page_runs) if hits]
        n_located += len(named_runs)

        # ---- D: audience markings ---------------------------------------
        joined = " ".join(r.text for r in page_runs)
        if "COORDINATOR COPY" in joined or "CONTAINS INDIVIDUAL PREFERENCES" in joined:
            flag("D", page, "", "",
                 "a coordinator-only banner appears in the public report")
        if AUDIENCE_PUBLIC not in joined:
            flag("D", page, "", "", f"page is not footed '{AUDIENCE_PUBLIC}'")

        if not named_runs:
            continue

        # ---- A: same visual line ----------------------------------------
        for line in _cluster_lines(page_runs):
            line_people: list[str] = []
            for run in line:
                line_people.extend(_match_people(run.text, names))
            line_people = sorted(dict.fromkeys(line_people))
            if not line_people:
                continue
            ok_desks: set[str] = set()
            for key in line_people:
                ok_desks |= allowed.get(key, set())
            who = ", ".join(label_of.get(k, k) for k in line_people)
            for run in line:
                for desk in desks_in(run.text):
                    attributions.append((line_people[0], desk))
                    if desk not in ok_desks:
                        flag("A", page, line_people[0], desk,
                             f"desk {desk} shares a line with {who}, who was "
                             f"not assigned it")

        # ---- B: same tight neighbourhood --------------------------------
        # Bound the vertical reach by how far apart names are on this page, so
        # a dense table cannot make one row's name adopt the next row's desk.
        name_ys = sorted({round(run.y, 2) for run, _ in named_runs})
        pitch = min((b - a for a, b in zip(name_ys, name_ys[1:])), default=1e9)
        for run, hits in named_runs:
            ok_desks = set()
            for key in hits:
                ok_desks |= allowed.get(key, set())
            scale = max(run.size, 4.0)
            reach_y = min(1.6 * scale, 0.45 * pitch)
            reach_x = 2.0 * scale * max(len(run.text), 1) * _EM_WIDTH
            for other in page_runs:
                if other is run:
                    continue
                if abs(other.y - run.y) > reach_y or abs(other.x - run.x) > reach_x:
                    continue
                for desk in desks_in(other.text):
                    if desk not in ok_desks:
                        flag("B", page, hits[0], desk,
                             f"desk {desk} is drawn beside "
                             f"{label_of.get(hits[0], hits[0])}, who was not "
                             f"assigned it")

        # ---- C: page inventory ------------------------------------------
        page_people: list[str] = []
        for _run, hits in named_runs:
            page_people.extend(hits)
        page_people = sorted(dict.fromkeys(page_people))
        page_ok: set[str] = set()
        for key in page_people:
            page_ok |= allowed.get(key, set())
        for run in page_runs:
            for desk in desks_in(run.text):
                if desk not in page_ok:
                    flag("C", page, "", desk,
                         f"desk {desk} appears on a page naming "
                         f"{_people(len(page_people))}, and it is not the "
                         f"assignment of any of them")

    # ---- did the extractor actually work? -------------------------------
    should_find = (bool(solution and solution.assignments)
                   if expect_names is None else expect_names)
    if should_find and n_located == 0:
        checks.append("E extractor sanity")
        findings.append(PrivacyFinding(
            "E", 0, "", "",
            "the text-layer reader located no names at all, but the report "
            "publishes an assignment table. The audit could not run, so this is "
            "reported as a failure rather than a pass.",
        ))

    return PrivacyAudit(
        ok=not findings,
        findings=tuple(findings),
        checks=tuple(checks),
        limitations=tuple(limitations),
        n_pages=n_pages,
        n_text_runs=len(runs),
        n_names_located=n_located,
        attributions=tuple(sorted(dict.fromkeys(attributions))),
    )


def _corroborate_with_pypdf(
    path: str | os.PathLike[str], config: Any, problem: Problem | None,
    solution: Solution | None, *, window: int = 200,
) -> tuple[bool, tuple[str, ...], str]:
    """Ask the same question through a completely different PDF parser.

    Optional: `pypdf` is not a dependency of this package. When it is absent the
    primary audit stands alone; when it is present it is a genuinely independent
    implementation, which is worth having for the one failure mode the primary
    audit cannot see — its own reader being wrong.
    """
    try:
        import pypdf  # type: ignore
    except Exception:
        return (True, (), "not run (pypdf is not installed)")

    names, allowed, all_desk_ids = _public_facts(config, problem, solution)
    pattern = _desk_pattern(all_desk_ids, ignore_case=True)
    if pattern is None:
        return (True, (), "not run (no desk ids configured)")

    sorted_names = sorted((n for n in names if len(n) >= 4),
                          key=lambda s: (-len(s), s))
    problems: list[str] = []
    try:
        reader = pypdf.PdfReader(os.fspath(path))
        n_pages = len(reader.pages)
        for page_no, page in enumerate(reader.pages):
            text = _normalise(page.extract_text() or "")
            if not text:
                continue
            marks: list[tuple[int, str]] = []
            for name in sorted_names:
                start = 0
                while True:
                    at = text.find(name, start)
                    if at == -1:
                        break
                    marks.append((at, names[name]))
                    start = at + len(name)
            if not marks:
                continue
            marks.sort()
            for match in pattern.finditer(text):
                pos = match.start()
                owner_at, owner = -1, None
                for at, key in marks:
                    if at > pos:
                        break
                    owner_at, owner = at, key
                if owner is None or pos - owner_at > window:
                    continue
                if match.group(1).upper() not in allowed.get(owner, set()):
                    problems.append(
                        f"page {page_no + 1}: {match.group(1)} follows {owner} "
                        f"in the extracted text"
                    )
    except Exception as exc:  # pragma: no cover - parser quirks
        return (True, (), f"inconclusive ({type(exc).__name__}: {exc})")
    return (not problems, tuple(problems), f"ran over {n_pages} page(s)")


def assert_public_safe(
    path: str | os.PathLike[str],
    config: Any,
    problem: Problem | None,
    solution: Solution | None,
    *,
    corroborate: bool = True,
    accommodations: Any = None,
) -> PrivacyAudit:
    """Run `audit_public_pdf`, and raise `PrivacyError` if anything is attributed.

    This is what `build_report(full=False)` calls before it returns, so a
    leaking public PDF is never handed back to the caller as a success.
    """
    audit = audit_public_pdf(
        path, config, problem, solution, accommodations=accommodations
    )
    if corroborate:
        ok, extra, status = _corroborate_with_pypdf(path, config, problem, solution)
        audit = PrivacyAudit(
            ok=audit.ok and ok,
            findings=audit.findings + tuple(
                PrivacyFinding("F", 0, "", "", detail) for detail in extra),
            checks=audit.checks + (f"F independent parse — {status}",),
            limitations=audit.limitations,
            n_pages=audit.n_pages,
            n_text_runs=audit.n_text_runs,
            n_names_located=audit.n_names_located,
            attributions=audit.attributions,
        )
    if not audit.ok:
        raise PrivacyError(
            "the public report carries data that SPEC §7.2/§7.3 keeps out of it — "
            "per-person preferences attributed to a name, or private-note text. "
            "The file has NOT been deleted — inspect "
            f"{os.fspath(path)} and fix the figure that produced it.\n"
            + audit.render()
        )
    return audit


# ==========================================================================
# Result type
# ==========================================================================


@dataclass(frozen=True)
class ReportResult:
    path: str
    audience: str
    n_pages: int
    page_kinds: tuple[str, ...]
    notes: tuple[str, ...] = ()
    privacy: PrivacyAudit | None = None

    def render(self) -> str:
        lines = [
            f"{self.path}: {self.n_pages} page(s), {self.audience}",
            "  pages: " + ", ".join(self.page_kinds),
        ]
        lines.extend("  note: " + note for note in self.notes)
        if self.privacy is not None:
            lines.append("  privacy audit "
                         + self.privacy.render().replace("\n", "\n  "))
        return "\n".join(lines)


# ==========================================================================
# Shared page builders
# ==========================================================================


def _baseline_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if hasattr(value, "totals"):
        return [value]
    return [item for item in value if item is not None]


def _room_labels(config: Any) -> str:
    rooms = getattr(config, "rooms", config)
    labels = [r.label for r in getattr(rooms, "rooms", ())]
    return " · ".join(labels) if labels else "no rooms configured"


def _curve_values(config: Any, solution: Solution) -> Sequence[Any]:
    try:
        return config.scoring.curve(solution.curve_name)
    except Exception:  # pragma: no cover - defensive
        return ()


def _superseded_count(responses: Any) -> int | None:
    if responses is None:
        return None
    try:
        return len(responses.superseded)
    except Exception:  # pragma: no cover - duck typing
        return None


def _title_page(
    deck: _Deck, *, config: Any, build: Any, solution: Solution,
    provenance: Mapping[str, Any], responses: Any, full: bool,
) -> None:
    problem = build.problem
    hist = solution.rank_histogram()
    n = int(sum(hist))
    k = int(solution.k)
    sheet = deck.sheet("title")

    if full:
        sheet.text(sheet.w / 2.0, sheet.h - 0.30, AUDIENCE_COORDINATOR,
                   ha="center", va="top", fontsize=8.4, color=_WARN,
                   fontweight="bold")

    sheet.cursor -= 0.50
    sheet.block("DESK ASSIGNMENT", size=8.6, color=_ACCENT, weight="bold",
                gap_after=0.10)
    sheet.heading("Graduate office desk assignment", size=25.0, gap_after=0.08)
    sheet.block(_room_labels(config), size=11.0, color=_MUTED, gap_after=0.24)
    sheet.rule(gap_after=0.24)

    # ---- the headline, in one sentence ----------------------------------
    if n == 0:
        headline = "Nobody was in the pool this year, so no desks were assigned."
    else:
        headline = f"{_n(hist[0])} of {_people(n)} got their first choice."
    sheet.block(headline, size=19.0, weight="medium", gap_after=0.12)

    if n:
        sheet.block(
            f"Every one of the {_people(n)} in the pool received one of the {k} "
            f"desks they themselves ranked — a hard guarantee of this process, "
            f"not a lucky outcome. The mean rank received was "
            f"{_mean_rank(hist):.2f} out of {k}.",
            size=10.2, gap_after=0.20,
        )

    facts: list[tuple[str, str]] = [
        ("people in the pool", _n(problem.n_people)),
        ("desks in the pool", _n(problem.n_desks)),
        ("ranked choices each (K)", _n(k)),
        ("scoring curve",
         f"{solution.curve_name}  ["
         + ", ".join(_fmt_number(v) for v in _curve_values(config, solution)) + "]"),
        ("solver", str(solution.backend)),
    ]
    if build.locked_desks:
        facts.append(("desks held by keepers", _n(len(build.locked_desks))))
    if build.unavailable_desks:
        facts.append(("desks marked unavailable", _n(len(build.unavailable_desks))))
    if solution.free_desks:
        facts.append(("desks left free", _n(len(solution.free_desks))))
    sheet.kv_rows(facts, key_w=2.55, size=9.4, gap_after=0.24)

    # ---- integrity, on page one, where it is worth something ------------
    sheet.rule(gap_after=0.16)
    sheet.block("INTEGRITY", size=8.0, color=_ACCENT, weight="bold", gap_after=0.08)
    sheet.block(
        "These two strings are what make this result checkable. The seed was "
        "published before the form opened; the hash identifies the exact "
        "response file that was solved. Anyone holding both can re-run the "
        "solver and get this document back.",
        size=8.8, color=_MUTED, gap_after=0.12,
    )

    total_rows = provenance.get("responses_row_count")
    if total_rows is None:
        total_rows = len(getattr(responses, "submissions", ()) or ())
    superseded = _superseded_count(responses)
    row_text = f"{_n(total_rows)} submitted row(s)"
    if superseded is not None:
        row_text += f", {_n(superseded)} superseded by a later submission"

    integrity: list[tuple[str, str]] = [
        ("tie-break seed", repr(solution.seed_string)),
        ("seed announced",
         str(getattr(config.scoring, "seed_committed_at", None)
             or "not recorded in scoring.json")),
        ("seed integer", _n(solution.seed_int)),
        ("responses sha256",
         str(provenance.get("responses_sha256")
             or getattr(responses, "sha256", None) or "not supplied to the report")),
        ("response file", row_text),
    ]
    canonical = provenance.get(prov_mod.CANONICAL_HASH_KEY)
    if canonical:
        integrity.append(("results canonical sha256", str(canonical)))
    sheet.kv_rows(integrity, key_w=2.55, size=8.8, value_family="monospace",
                  gap_after=0.0)


def _method_page(
    deck: _Deck, *, build: Any, solution: Solution, full: bool,
    page_index: Sequence[tuple[str, str]],
) -> None:
    problem = build.problem
    k = int(solution.k)
    sheet = deck.sheet("method")
    sheet.heading("How to read this document", size=16.0, gap_after=0.16)

    col_w = (sheet.text_width - 0.55) / 2.0
    right_x = sheet.left + col_w + 0.55
    top = sheet.cursor

    # ---- left column ----------------------------------------------------
    sheet.block("WHAT THE SOLVER DID", size=8.0, color=_ACCENT, weight="bold",
                width=col_w, gap_after=0.08)
    sheet.block(
        f"Everybody in the pool ranked {k} desks. Each rank is worth a fixed "
        f"number of points, published in advance as the "
        f"'{solution.curve_name}' curve. The solver then chose the one "
        f"allocation of {_people(problem.n_people)} to "
        f"{_n(problem.n_desks)} desks that maximises the total points across "
        f"everybody at once — not one person at a time, and not in the order "
        f"the forms arrived.",
        size=9.0, width=col_w, gap_after=0.12,
    )
    sheet.block(
        "Two allocations can be exactly tied for best. When that happens the "
        "choice between them comes from a random draw seeded by a string that "
        "was announced before the form opened, so it cannot have been picked "
        "afterwards to favour anybody. The seed is on page 1.",
        size=9.0, width=col_w, gap_after=0.14,
    )
    sheet.block("WHAT IS GUARANTEED", size=8.0, color=_ACCENT, weight="bold",
                width=col_w, gap_after=0.08)
    for item in (
        f"Every desk assigned is one of that person's own {k} submitted "
        f"choices. If that had been impossible for even one person, the run "
        f"would have failed and produced no assignment at all.",
        "Every desk assigned is in a zone that person is eligible for, under "
        "the published rule table in eligibility.json.",
        "No allocation scores higher than this one under this curve. That is "
        "proved by the algorithm, not sampled.",
        "The result is a pure function of the responses, the config and the "
        "seed. There is no manual override anywhere in the code.",
    ):
        sheet.text(sheet.left, sheet.cursor, "·", ha="left", va="top",
                   fontsize=9.0, color=_FAINT)
        sheet.block(item, size=8.8, indent=0.17, width=col_w - 0.17, gap_after=0.045)
    left_bottom = sheet.cursor

    # ---- right column ---------------------------------------------------
    sheet.cursor = top
    sheet.text(right_x, sheet.cursor, "WHAT IS ON EACH PAGE", ha="left", va="top",
               fontsize=8.0, color=_ACCENT, fontweight="bold")
    sheet.cursor -= sheet.line_h(8.0, 1.7)
    for label, description in page_index:
        sheet.text(right_x, sheet.cursor, label, ha="left", va="top",
                   fontsize=8.8, color=_INK, fontweight="medium")
        wrapped = _wrap(description, col_w - 1.35, 8.6)
        sheet.text(right_x + 1.35, sheet.cursor, wrapped, ha="left", va="top",
                   fontsize=8.6, color=_MUTED, linespacing=1.38)
        sheet.cursor -= max(
            sheet.line_h(8.8, 1.38),
            (wrapped.count("\n") + 1) * sheet.line_h(8.6, 1.38),
        ) + 0.045

    sheet.cursor -= 0.14
    sheet.text(right_x, sheet.cursor, "WHO MAY SEE THIS COPY", ha="left", va="top",
               fontsize=8.0, color=_ACCENT, fontweight="bold")
    sheet.cursor -= sheet.line_h(8.0, 1.7)
    privacy_text = (
        "This is the coordinator copy. It carries the full preference table and "
        "the rank each named person received, so it must not be circulated. The "
        "copy to publish is the other one: it has the aggregate figures and the "
        "final assignment list, and nothing that attributes a ranking to a "
        "named person."
        if full else
        "This is the public copy. Final assignments are published here because "
        "people need to know where they sit. Individual rankings are not: no "
        "page in this document says which desks a named person put on their "
        "list. That is checked mechanically against this file before it is "
        "released, not merely intended."
    )
    wrapped = _wrap(privacy_text, col_w, 8.8)
    sheet.text(right_x, sheet.cursor, wrapped, ha="left", va="top", fontsize=8.8,
               color=_WARN if full else _INK, linespacing=1.42)
    sheet.cursor -= (wrapped.count("\n") + 1) * sheet.line_h(8.8, 1.42)

    sheet.cursor = min(sheet.cursor, left_bottom) - 0.18

    warnings = list(getattr(build, "warnings", ()) or ())
    if warnings and sheet.room > 0.9:
        sheet.rule(gap_after=0.12)
        sheet.block("NOTES FROM BUILDING THE POOL", size=8.0, color=_ACCENT,
                    weight="bold", gap_after=0.08)
        room_for = max(1, int(sheet.room / 0.22))
        for warning in warnings[:room_for]:
            sheet.text(sheet.left, sheet.cursor, "·", ha="left", va="top",
                       fontsize=8.6, color=_FAINT)
            sheet.block(warning, size=8.4, color=_MUTED, indent=0.17, gap_after=0.035)
        if len(warnings) > room_for:
            sheet.block(
                f"… and {_n(len(warnings) - room_for)} more; the full list is in "
                f"results.json under 'warnings'.",
                size=8.4, color=_MUTED, gap_after=0.0,
            )


def _outcome_page(deck: _Deck, solution: Solution) -> None:
    sheet = deck.sheet("outcome")
    sheet.kicker("the outcome")
    ax_h = 4.05
    ax_y = sheet.cursor - ax_h
    ax_w = (sheet.text_width - 0.75) / 2.0
    left = sheet.axes(x_in=sheet.left, y_in=ax_y, w_in=ax_w, h_in=ax_h)
    right = sheet.axes(x_in=sheet.left + ax_w + 0.75, y_in=ax_y, w_in=ax_w, h_in=ax_h)
    fstats.rank_distribution(left, solution)
    fstats.cumulative_satisfaction(right, solution)


def _rank_strip_page(deck: _Deck, solution: Solution) -> None:
    sheet = deck.sheet("rank-strip")
    sheet.kicker("every person, one dot each")
    ax_h = 3.9
    ax = sheet.axes(x_in=sheet.left, y_in=sheet.cursor - ax_h,
                    w_in=sheet.text_width, h_in=ax_h)
    fstats.rank_received_by_person(ax, solution, anonymize=True)


def _fit_map_figure(make: Any, page_size: tuple[float, float]) -> tuple[Any, Figure | None]:
    """Build a self-sizing `figures_map` figure that fits the page.

    Height is close to affine in width, so shrinking by the overshoot converges
    in one or two passes for any room aspect ratio — including a portrait plan,
    which at the full page width would come out taller than the paper.
    """
    page_w, page_h = page_size
    ceiling = page_h - FOOTER_Y_IN - 0.10
    width = page_w
    result = None
    fig: Figure | None = None
    for _attempt in range(3):
        result = make(width)
        figures = getattr(result, "figures", ())
        fig = figures[0] if figures else None
        if fig is None:
            return result, None
        natural_h = float(fig.get_size_inches()[1])
        if natural_h <= ceiling:
            break
        width = max(2.5, width * (ceiling - 0.10) / natural_h)
    if fig is not None:
        _rehome(fig, page_w, page_h, valign="top")
    return result, fig


def _map_pages(
    deck: _Deck, *, config: Any, problem: Problem, solution: Solution | None,
    show_occupants: str, page_size: tuple[float, float],
) -> None:
    """One page per room, at the document's page size."""
    rooms = getattr(config, "rooms", config)
    room_ids = [r.id for r in getattr(rooms, "rooms", ())]
    for room_id in (room_ids or [None]):
        result, fig = _fit_map_figure(
            lambda width, rid=room_id: fmap.desk_popularity_heatmap(
                None, config, problem, solution,
                rooms=[rid] if rid is not None else None,
                layout="grid", width=width, show_occupants=show_occupants,
            ),
            page_size,
        )
        if fig is None:  # pragma: no cover - only if rooms.json is empty
            continue
        deck.add(fig, "map", tuple(getattr(result, "notes", ())))


def _contested_page(
    deck: _Deck, *, config: Any, problem: Problem, solution: Solution | None,
    show_winners: bool, page_size: tuple[float, float],
) -> None:
    result, fig = _fit_map_figure(
        lambda width: fmap.contested_desks_figure(
            None, config, problem, solution, show_winners=show_winners, width=width,
        ),
        page_size,
    )
    if fig is None:  # pragma: no cover
        return
    deck.add(fig, "contested", tuple(getattr(result, "notes", ())))


def _baseline_pages(deck: _Deck, solution: Solution, baselines: Any) -> None:
    results = _baseline_list(baselines)

    # Random serial dictatorship *is* the old process -- people arrive in an
    # effectively random order and take the best free desk -- so it leads. If it
    # is not among the baselines the first one does.
    primary = next(
        (r for r in results
         if "serial dictatorship" in str(getattr(r, "name", "")).casefold()),
        results[0] if results else None,
    )
    sheet = deck.sheet("baseline")
    sheet.kicker("against the old way")
    ax = sheet.axes(x_in=sheet.left, y_in=sheet.cursor - 4.05,
                    w_in=sheet.text_width, h_in=4.05)
    fstats.baseline_comparison(ax, solution, primary)

    others = [r for r in results if r is not primary]
    if not others:
        return
    # On its own page: the uniform lottery scores so much lower that overlaying
    # it squeezes the realistic baseline into the right-hand quarter of the
    # axes, which flatters the result by making the gap look bigger than the
    # comparison that actually matters.
    sheet2 = deck.sheet("baseline-overlay")
    sheet2.kicker("both baselines, for scale")
    sheet2.block(
        "Secondary. The lottery here ignores preferences entirely and brackets "
        "the comparison from underneath; the realistic baseline is the one on "
        "the previous page. They are drawn together only so the whole range is "
        "visible on one axis.",
        size=8.8, color=_MUTED, gap_after=0.12,
    )
    ax2 = sheet2.axes(x_in=sheet2.left, y_in=sheet2.cursor - 3.80,
                      w_in=sheet2.text_width, h_in=3.80)
    fstats.baseline_comparison(ax2, solution, results,
                               title="Every baseline on one scale")


def _sensitivity_pages(
    deck: _Deck, solution: Solution, curve_rows: Sequence[Any],
    seed_rows: Sequence[Any],
) -> None:
    sheet = deck.sheet("curve-sensitivity")
    sheet.kicker("did the scoring choice decide it?")
    ax = sheet.axes(x_in=sheet.left, y_in=sheet.cursor - 3.95,
                    w_in=sheet.text_width, h_in=3.95)
    fstats.curve_sensitivity(ax, list(curve_rows), primary=solution)

    sheet2 = deck.sheet("seed-sensitivity")
    sheet2.kicker("did the random tie-break decide it?")
    ax2 = sheet2.axes(x_in=sheet2.left, y_in=sheet2.cursor - 3.95,
                      w_in=sheet2.text_width, h_in=3.95)
    fstats.seed_sensitivity(ax2, list(seed_rows),
                            published_seed=solution.seed_string)


#: Leaves room under the table for its own footnote and caption *and* for the
#: page footer this module stamps at 0.36in. The figures module derives
#: rows-per-page from rect[3], so a shorter rect paginates rather than overflows.
_TABLE_RECT = (0.062, 0.135, 0.876, 0.775)
_PREF_RECT = (0.135, 0.190, 0.715, 0.700)


def _assignment_table_pages(deck: _Deck, solution: Solution, *, show_email: bool,
                            page_size: tuple[float, float]) -> None:
    probe = _Sheet(page_size)
    n_pages = fstats.assignment_table_page_count(solution, fig=probe.fig,
                                                 rect=_TABLE_RECT)
    probe.fig.clear()
    for page in range(n_pages):
        sheet = deck.sheet("assignment-table")
        fstats.assignment_table(sheet.fig, solution, page=page, rect=_TABLE_RECT,
                                sort_by="name", show_email=show_email)


def _preference_matrix_pages(deck: _Deck, problem: Problem, solution: Solution,
                             *, page_size: tuple[float, float]) -> None:
    probe = _Sheet(page_size)
    n_pages = fstats.preference_matrix_page_count(problem, fig=probe.fig,
                                                  rect=_PREF_RECT)
    probe.fig.clear()
    for page in range(n_pages):
        sheet = deck.sheet("preference-matrix")
        fstats.preference_matrix(sheet.fig, problem, anonymize=False, page=page,
                                 rect=_PREF_RECT, solution=solution)


def _rank_by_name_page(deck: _Deck, solution: Solution) -> None:
    sheet = deck.sheet("rank-by-name")
    sheet.cursor -= 0.16
    sheet.kicker("coordinator: who did worst")
    ax_h = 3.8
    ax = sheet.axes(x_in=sheet.left, y_in=sheet.cursor - ax_h,
                    w_in=sheet.text_width, h_in=ax_h)
    fstats.rank_received_by_person(ax, solution, anonymize=False)


# ---- flowed, paginated text sections ------------------------------------


@dataclass
class _Item:
    """One line of a flowed, paginated text section."""

    text: str
    size: float = 8.6
    color: str = _INK
    indent: float = 0.0
    weight: str | None = None
    family: str | None = None
    gap_before: float = 0.0
    gap_after: float = 0.045


def _flow(deck: _Deck, kind: str, title: str, items: Sequence[_Item],
          *, page_size: tuple[float, float], banner: str | None = None) -> None:
    """Lay `items` out across as many pages as they need.

    Used for the sections whose length is data-dependent (roster conflicts,
    dropped choices, round-2 scope). Nothing is truncated and nothing is assumed
    about how many entries there are.
    """
    index = 0
    part = 0
    while True:
        sheet = deck.sheet(kind)
        if banner:
            sheet.text(sheet.w / 2.0, sheet.h - 0.30, banner, ha="center",
                       va="top", fontsize=7.6, color=_WARN, fontweight="bold")
            sheet.cursor -= 0.18
        sheet.heading(title if part == 0 else f"{title} (continued)",
                      size=15.0, gap_after=0.14)
        drew = 0
        while index < len(items):
            item = items[index]
            wrapped = _wrap(item.text, sheet.text_width - item.indent, item.size)
            need = (item.gap_before
                    + (wrapped.count("\n") + 1) * sheet.line_h(item.size, 1.40)
                    + item.gap_after)
            if drew and need > sheet.room:
                break
            sheet.cursor -= item.gap_before
            kw: dict[str, Any] = {}
            if item.weight:
                kw["fontweight"] = item.weight
            if item.family:
                kw["family"] = item.family
            sheet.text(sheet.left + item.indent, sheet.cursor, wrapped, ha="left",
                       va="top", fontsize=item.size, color=item.color,
                       linespacing=1.40, **kw)
            sheet.cursor -= ((wrapped.count("\n") + 1)
                             * sheet.line_h(item.size, 1.40) + item.gap_after)
            index += 1
            drew += 1
        part += 1
        if index >= len(items):
            return


def _accommodation_items(
    accommodations: Any, config: Any, solution: Solution
) -> list[_Item]:
    """The coordinator-only private-notes section (SPEC §7.3).

    One entry per person: who they are, what desk the solve gave them, and the
    note verbatim. `_flow` paginates it, so a note of any length lays out across
    as many pages as it needs and nothing is truncated — which matters, because
    truncating the one sentence that explains *why* somebody needs a particular
    desk would defeat the point of collecting it.
    """
    latest = dict(getattr(accommodations, "latest", {}) or {})
    items: list[_Item] = [
        _Item(
            "Free-text notes students left on the confirm page, private to the "
            "coordinator. They were written on the understanding that nobody else "
            "reads them, and they contain what that invites: health, "
            "accessibility, caring responsibilities, and conflict with a named "
            "person. They are in this copy and in "
            f"out/{acc_mod.COORDINATOR_TXT_NAME} (owner-readable only), and "
            "nowhere else — not in results.json, not in the anonymised responses, "
            "and not in the public report, which is checked for them before it is "
            "released.",
            size=8.8, color=_MUTED, gap_after=0.10,
        ),
        _Item(
            "None of this entered the solve. The assignment was computed from the "
            "rankings, the config and the seed alone (SPEC I2); there is no code "
            "path by which a note could have moved anybody. To act on one, change "
            "an input — take a desk out of the pool in rooms.json — and re-run, so "
            "that what you did is visible in git.",
            size=8.8, color=_WARN, gap_after=0.16,
        ),
    ]

    if not latest:
        items.append(_Item("No notes were submitted this cycle.", size=8.8,
                           color=_MUTED))
        return items

    desk_of: dict[str, str] = {}
    for assignment in getattr(solution, "assignments", ()) or ():
        desk_of[assignment.email] = (
            f"desk {assignment.desk_id} ({assignment.desk_label}), "
            f"choice #{assignment.rank_received}"
        )

    for index, email in enumerate(sorted(latest), start=1):
        note = latest[email]
        where = desk_of.get(email)
        if where is None:
            person = None
            roster = getattr(config, "roster", None)
            if roster is not None:
                person = roster.by_email(email)
            if person is not None and person.keeps_desk and person.current_desk:
                where = (f"desk {person.current_desk} — keeping their current "
                         f"seat, not in the pool")
            elif person is None:
                where = "not on the roster; no desk assigned"
            else:
                where = "no desk assigned (not in the pool this cycle)"
        items.append(_Item(
            f"{index}. {note.name or email} <{email}> — {where}",
            size=9.0, weight="bold", gap_before=0.18, gap_after=0.04,
        ))
        if note.timestamp:
            items.append(_Item(f"submitted {note.timestamp}", size=7.8,
                               color=_MUTED, indent=0.17, gap_after=0.06))
        items.append(_Item(note.text, size=9.0, indent=0.17, gap_after=0.06))

    warnings = tuple(getattr(accommodations, "warnings", ()) or ())
    if warnings:
        items.append(_Item("NOTES FROM READING THE FILE", size=8.2, color=_ACCENT,
                           weight="bold", gap_before=0.24, gap_after=0.06))
        for warning in warnings:
            items.append(_Item(warning, size=8.2, color=_MUTED, indent=0.17,
                               gap_after=0.035))
    return items


def _ledger_items(build: Any, responses: Any, solution: Solution) -> list[_Item]:
    items: list[_Item] = []

    def section(name: str, blurb: str) -> None:
        items.append(_Item(name.upper(), size=8.2, color=_ACCENT, weight="bold",
                           gap_before=0.20, gap_after=0.05))
        items.append(_Item(blurb, size=8.4, color=_MUTED, gap_after=0.09))

    conflicts = sorted(getattr(build, "roster_conflicts", ()) or (),
                       key=lambda c: (c.email, c.field))
    section(
        f"Roster conflicts ({_n(len(conflicts))})",
        "The student's own answer on the form wins over the roster (SPEC §3.3), "
        "because the roster is stale by design. Each one below changed what the "
        "solver believed about that person, and year/candidacy decide which "
        "zones they may sit in — so none of them is cosmetic. Fix roster.csv "
        "and re-run if the roster was the correct one.",
    )
    if not conflicts:
        items.append(_Item("None. The roster and the submissions agreed about "
                           "every person.", size=8.6, color=_MUTED))
    for conflict in conflicts:
        items.append(_Item(
            f"{conflict.email} — {conflict.field}: roster said "
            f"{conflict.roster_value!r}, submission said "
            f"{conflict.submitted_value!r}; the submission was used.",
            size=8.6, indent=0.17,
        ))

    dropped = sorted(getattr(build, "dropped_choices", ()) or ())
    section(
        f"Dropped choices ({_n(len(dropped))})",
        "A ranked desk that is no longer in the pool is discarded and the person "
        "keeps their remaining choices (SPEC §3.4). These do not appear in any "
        "of the aggregate figures, which is why they are listed here rather "
        "than quietly folded into the counts.",
    )
    if not dropped:
        items.append(_Item("None. Every desk everybody ranked was still in the "
                           "pool.", size=8.6, color=_MUTED))
    for who, desk, why in dropped:
        items.append(_Item(f"{who} ranked {desk} — {why}.", size=8.6, indent=0.17))

    excluded = sorted(getattr(build, "excluded_people", ()) or (),
                      key=lambda e: (e.reason, e.email))
    section(
        f"People not in the pool ({_n(len(excluded))})",
        "Either they are keeping their current desk — in which case both they "
        "and the desk were removed before solving — or they did not submit. A "
        "non-responder is a warning, never an error: they simply did not "
        "participate.",
    )
    if not excluded:
        items.append(_Item("Nobody was excluded.", size=8.6, color=_MUTED))
    for person in excluded:
        items.append(_Item(f"{person.name} <{person.email}> — {person.reason}.",
                           size=8.6, indent=0.17))

    locked = sorted(getattr(build, "locked_desks", ()) or ())
    unavailable = sorted(getattr(build, "unavailable_desks", ()) or ())
    section("Desks held back",
            "Desks removed from the pool before solving, and why.")
    if locked:
        items.append(_Item(
            "kept by their current occupant: "
            + ", ".join(f"{desk} ({email})" for desk, email in locked),
            size=8.6, indent=0.17,
        ))
    if unavailable:
        items.append(_Item(
            "marked unavailable in rooms.json: " + ", ".join(unavailable),
            size=8.6, indent=0.17,
        ))
    if not locked and not unavailable:
        items.append(_Item("None; every desk was in the pool.", size=8.6,
                           color=_MUTED))
    free = sorted(solution.free_desks)
    if free:
        items.append(_Item(
            f"left free after the assignment ({_n(len(free))}): " + ", ".join(free),
            size=8.6, indent=0.17, gap_before=0.05,
        ))

    section(
        "Submission history",
        "One row per submission, not per person: re-submission is allowed and "
        "the latest row per email wins (SPEC §3.2). The superseded rows stay in "
        "the response file and are covered by its hash.",
    )
    superseded = _superseded_count(responses)
    if responses is not None:
        total_rows = len(getattr(responses, "submissions", ()) or ())
        distinct = len(getattr(responses, "latest", {}) or {})
        items.append(_Item(
            f"{_n(total_rows)} row(s) from {_n(distinct)} distinct "
            f"{'person' if distinct == 1 else 'people'}; "
            f"{_n(superseded or 0)} superseded by a later submission.",
            size=8.6, indent=0.17,
        ))
        counts: dict[str, int] = {}
        for sub in getattr(responses, "superseded", ()) or ():
            counts[sub.email] = counts.get(sub.email, 0) + 1
        for email in sorted(counts):
            items.append(_Item(
                f"{email} — {_n(counts[email])} earlier submission(s) superseded.",
                size=8.4, indent=0.34, color=_MUTED, gap_after=0.025,
            ))
    else:
        items.append(_Item(
            "The Responses object was not passed to build_report(), so the "
            "superseded-row count is not available here. It is in the solver's "
            "console output and derivable from the response file.",
            size=8.6, indent=0.17, color=_MUTED,
        ))

    warnings = list(getattr(build, "warnings", ()) or ())
    section(f"Every warning raised while building the pool ({_n(len(warnings))})",
            "Reproduced verbatim, so this page and the console agree.")
    if not warnings:
        items.append(_Item("None.", size=8.6, color=_MUTED))
    for warning in warnings:
        items.append(_Item(warning, size=8.4, indent=0.17, color=_MUTED))

    return items


# ---- provenance ----------------------------------------------------------


_PROV_ORDER = (
    "seed_string", "seed_int", "curve", "curve_values", "K",
    "responses_sha256", "responses_row_count", "backend",
    prov_mod.CANONICAL_HASH_KEY,
    "deskmatch_version", "python", "numpy", "scipy",
)


def _provenance_page(
    deck: _Deck, *, config: Any, solution: Solution,
    provenance: Mapping[str, Any], responses: Any,
) -> None:
    sheet = deck.sheet("provenance")
    sheet.heading("Provenance", size=16.0, gap_after=0.08)
    sheet.block(
        "Everything needed to reproduce this document, exactly as recorded in "
        "results.json (SPEC §7.1). Same responses, same config, same seed, same "
        "answer — on any machine, on any supported Python.",
        size=9.0, color=_MUTED, gap_after=0.18,
    )

    block = dict(provenance)
    if not block:
        block = {
            "seed_string": solution.seed_string,
            "seed_int": solution.seed_int,
            "curve": solution.curve_name,
            "curve_values": list(_curve_values(config, solution)),
            "K": solution.k,
            "backend": solution.backend,
            "responses_sha256": getattr(responses, "sha256", None),
            "responses_row_count": len(getattr(responses, "submissions", ()) or ()),
            "config_sha256": dict(getattr(config, "file_hashes", {}) or {}),
        }
        block.update(prov_mod.environment_versions())

    rows: list[tuple[str, str]] = []
    for key in _PROV_ORDER:
        if key not in block:
            continue
        value = block[key]
        if key == "curve_values":
            text = "[" + ", ".join(_fmt_number(v) for v in value) + "]"
        elif key == "seed_string":
            text = repr(value)
        elif isinstance(value, int) and not isinstance(value, bool):
            text = _n(value)
        else:
            text = str(value)
        rows.append((key, text))

    config_hashes = block.get("config_sha256") or {}
    for name in sorted(config_hashes):
        rows.append((f"config_sha256[{name}]", str(config_hashes[name])))

    handled = set(_PROV_ORDER) | {"config_sha256", "reproduce", "generated_at"}
    for key in sorted(k for k in block if k not in handled):
        rows.append((key, str(block[key])))

    sheet.kv_rows(rows, key_w=2.35, size=8.4, value_family="monospace",
                  gap=0.032, gap_after=0.16)

    sheet.rule(gap_after=0.14)
    sheet.block("TO REPRODUCE THIS RUN", size=8.0, color=_ACCENT, weight="bold",
                gap_after=0.08)
    command = prov_mod.reproduce_command({"provenance": block}) or (
        "deskmatch solve --config config/ --responses responses.csv"
    )
    sheet.block(command, size=9.0, family="monospace", gap_after=0.10)
    if prov_mod.REPRODUCE_VERIFY_PLACEHOLDER in command:
        sheet.block(
            f"Substitute {prov_mod.REPRODUCE_VERIFY_PLACEHOLDER} with the "
            f"'canonical_sha256' field from results.json. It cannot be baked "
            f"into this string, because the string is itself covered by that "
            f"hash.",
            size=8.4, color=_MUTED, gap_after=0.12,
        )

    sheet.block(
        "No timestamp appears anywhere in this document, deliberately. "
        "Wall-clock time is excluded from the canonical hash (SPEC §5.5), and "
        "printing it here would make two byte-identical runs produce two "
        "different PDFs — destroying the property the hash exists to certify. "
        "The run time is recorded in results.json under 'generated_at', outside "
        "the hash.",
        size=8.4, color=_MUTED, gap_after=0.14,
    )
    sheet.block(
        "What this defends against, plainly: tampering with the solve after the "
        "fact, and cherry-picking a favourable seed. What it does not defend "
        "against: the coordinator editing the response export before running. "
        "That is deliberate — the control for it is that the raw export is "
        "committed to git before solving, and the hash above is printed so that "
        "a second person holding a copy can compare it.",
        size=8.4, color=_MUTED, gap_after=0.0,
    )


# ==========================================================================
# build_report
# ==========================================================================


def _page_index(full: bool, has_notes: bool = False) -> list[tuple[str, str]]:
    index = [
        ("1", "the result in one sentence, with the seed and the response hash."),
        ("2", "this page."),
        ("3", "which choice everybody received, and the same thing cumulatively."),
        ("4", "one dot per person, sorted, so the shape of the outcome is visible."),
        ("5", "the floor plan, shaded by how wanted each desk was."),
        ("6", "the result against a simulation of the old first-come process."),
        ("7", "the desks that drew the most first-place votes."),
        ("8", "whether the scoring curve or the random tie-break decided anything."),
        ("9", "the final assignment, by name."),
    ]
    if full:
        index.append(("10", "coordinator only: the full preference table, rank "
                            "received by name, and the data-quality ledger."))
        if has_notes:
            index.append(("11", "coordinator only: the private notes students left "
                                "for you. Not in the public copy."))
    index.append(("last", "provenance and the command to reproduce this run."))
    return index


def _resolved_seed_of(config) -> str:
    """The seed actually used, tolerating a config object that predates
    seed_year (the report is also driven from tests with stub configs)."""
    scoring = getattr(config, "scoring", None)
    if scoring is None:
        return ""
    resolver = getattr(scoring, "resolved_seed", None)
    if callable(resolver):
        return str(resolver())
    return str(getattr(scoring, "tie_break_seed", ""))


def build_report(
    path: str | os.PathLike[str],
    config: Any,
    build: Any,
    solution: Solution,
    *,
    full: bool = False,
    baselines: Any = (),
    curve_rows: Sequence[Any] = (),
    seed_rows: Sequence[Any] = (),
    provenance: Mapping[str, Any] | None = None,
    responses: Any = None,
    accommodations: Any = None,
    page_size: tuple[float, float] = PAGE_SIZE,
    verify_privacy: bool = True,
) -> ReportResult:
    """Write the results PDF: one file, one `PdfPages`, pages in story order.

    Parameters
    ----------
    path
        Where the PDF goes. Parent directories are created.
    config
        A `types.Config`: geometry, zones, roster and scoring curve.
    build
        The `problem.BuildReport` from `build_problem()` — the Problem plus the
        commentary about how the pool was formed (conflicts, exclusions, dropped
        choices). Named `build` rather than `build_report` so that it does not
        shadow this function; `cli.py` passes it positionally.
    solution
        The `types.Solution`. **K comes from `solution.k`**, and the rank
        histogram has that many bars, whatever it is.
    full
        False (the default output) builds the PUBLIC report. True adds the
        coordinator-only pages: the full preference matrix, per-person rank
        received with names, and the roster/data-quality ledger. Per SPEC §7.2
        the coordinator report must not be circulated.
    baselines
        One `baselines.BaselineResult` or a sequence of them.
    curve_rows, seed_rows
        Exactly what `baselines.alternative_curve_outcomes()` and
        `alternative_seed_outcomes()` return.
    provenance
        The SPEC §7.1 block from `provenance.build_provenance()`.
    responses
        Optional `types.Responses`. Only the coordinator ledger needs it, for
        the superseded-submission count; without it that one line says so rather
        than guessing.
    accommodations
        Optional `accommodations.Accommodations` (SPEC §7.3). Under `full=True`
        its contents get a coordinator-only section. Under `full=False` it is
        used the other way round: as the list of strings the finished public PDF
        is searched for and must not contain. Passing it therefore makes the
        public build *stricter*, never different — no page depends on it.
    verify_privacy
        When building the public report, re-open the finished file and audit it
        (see `assert_public_safe`). Leave this on.

    Raises
    ------
    PrivacyError
        If a public report was built with a coordinator-only page, or if the
        audit finds preference data attributed to a named person in the bytes
        that were written.
    """
    problem = build.problem
    audience = AUDIENCE_COORDINATOR if full else AUDIENCE_PUBLIC
    prov = dict(provenance or {})
    response_hash = str(
        prov.get("responses_sha256") or getattr(responses, "sha256", "") or "")

    target = Path(os.fspath(path))
    if str(target.parent) not in ("", "."):
        target.parent.mkdir(parents=True, exist_ok=True)

    deck = _Deck(page_size, audience, response_hash,
                 subtitle=f"curve '{solution.curve_name}' · K={solution.k}")

    with fmap.open_pdf(target,
                       "deskmatch results — coordinator copy" if full
                       else "deskmatch results") as pdf:
        # figures_map.open_pdf holds its house rcParams open for the whole
        # block; layering the stats module's typography on top *inside* that
        # context keeps both figure families consistent and still restores
        # everything the caller had on exit.
        fstats.apply_house_style()

        _title_page(deck, config=config, build=build, solution=solution,
                    provenance=prov, responses=responses, full=full)
        _method_page(deck, build=build, solution=solution, full=full,
                     page_index=_page_index(full, bool(accommodations)))
        _outcome_page(deck, solution)
        _rank_strip_page(deck, solution)
        _map_pages(deck, config=config, problem=problem, solution=solution,
                   show_occupants="surname" if full else "none",
                   page_size=page_size)
        _baseline_pages(deck, solution, baselines)
        _contested_page(deck, config=config, problem=problem, solution=solution,
                        show_winners=full, page_size=page_size)
        _sensitivity_pages(deck, solution, curve_rows, seed_rows)
        _assignment_table_pages(deck, solution, show_email=full,
                                page_size=page_size)
        if full:
            _preference_matrix_pages(deck, problem, solution, page_size=page_size)
            _rank_by_name_page(deck, solution)
            _flow(deck, "ledger", "Pool, conflicts and data quality",
                  _ledger_items(build, responses, solution),
                  page_size=page_size, banner=AUDIENCE_COORDINATOR)
            if accommodations is not None:
                _flow(deck, "accommodations", "Private notes to the coordinator",
                      _accommodation_items(accommodations, config, solution),
                      page_size=page_size, banner=AUDIENCE_COORDINATOR)
        _provenance_page(deck, config=config, solution=solution,
                         provenance=prov, responses=responses)

        n_pages = deck.write(pdf)

    audit: PrivacyAudit | None = None
    if not full and verify_privacy:
        audit = assert_public_safe(target, config, problem, solution,
                                   accommodations=accommodations)

    return ReportResult(
        path=str(target),
        audience=audience,
        n_pages=n_pages,
        page_kinds=deck.kinds,
        notes=tuple(dict.fromkeys(deck.notes)),
        privacy=audit,
    )


# ==========================================================================
# The diagnostic report (infeasible runs)
# ==========================================================================


def _blocking_axes(
    sheet: _Sheet, problem: Problem, people: Sequence[str], desks: Sequence[str],
    *, k: int, x_in: float, y_in: float, w_in: float, h_in: float,
) -> bool:
    """Draw one bipartite blocking set. Returns True if the rows could be named.

    People on the left, the desks they collectively named on the right, one line
    per (person, desk) they ranked. The picture *is* the argument: every line
    lands inside a set of desks smaller than the set of people, so somebody has
    to miss out however the lines are followed.
    """
    ax = sheet.axes(x_in=x_in, y_in=y_in, w_in=w_in, h_in=h_in)
    ax.set_axis_off()
    ax.set_xlim(0.0, 1.0)
    ax.set_ylim(0.0, 1.0)

    n_left, n_right = len(people), len(desks)
    rows = max(n_left, n_right, 1)

    # Font size from the geometry, so any group size renders. Below the floor
    # the labels stop being legible and a companion list page carries the names
    # instead -- nobody is dropped from the drawing either way.
    size = float(np.clip((h_in * 72.0) / rows * 0.58, 3.6, 9.0))
    labelled = size >= 5.0

    def ys(count: int) -> list[float]:
        if count <= 0:
            return []
        if count == 1:
            return [0.5]
        top, bottom = 0.94, 0.06
        return [top - (top - bottom) * i / (count - 1) for i in range(count)]

    y_left, y_right = ys(n_left), ys(n_right)
    x_left, x_right = 0.30, 0.70
    person_row = {email: i for i, email in enumerate(people)}
    desk_row = {desk: j for j, desk in enumerate(desks)}
    p_index = {email: i for i, email in enumerate(problem.people)}
    d_index = {desk: j for j, desk in enumerate(problem.desks)}
    colors = fstats.rank_colors(k)

    edges: list[tuple[int, str, str]] = []
    for email in people:
        i = p_index.get(email)
        if i is None:
            continue
        for desk in desks:
            j = d_index.get(desk)
            if j is None:
                continue
            rank = int(problem.rank[i, j])
            if rank >= 1:
                edges.append((rank, email, desk))
    # Worst ranks first, so first choices end up on top and read as the
    # strongest lines. Also a total order, so the drawing is reproducible.
    edges.sort(key=lambda e: (-e[0], e[1], e[2]))

    for rank, email, desk in edges:
        color = colors[rank - 1] if 1 <= rank <= len(colors) else _MUTED
        ax.plot([x_left, x_right],
                [y_left[person_row[email]], y_right[desk_row[desk]]],
                color=color, linewidth=1.9 if rank == 1 else 1.0,
                alpha=0.85 if rank == 1 else 0.55, solid_capstyle="round",
                zorder=2)

    for i, email in enumerate(people):
        ax.plot([x_left], [y_left[i]], marker="o",
                markersize=max(size * 0.42, 2.4), color=_INK, zorder=4)
        if labelled:
            ax.text(x_left - 0.018, y_left[i],
                    problem.person_names.get(email, email), ha="right",
                    va="center", fontsize=size, color=_INK, zorder=5)

    for j, desk in enumerate(desks):
        ax.plot([x_right], [y_right[j]], marker="s",
                markersize=max(size * 0.42, 2.4), color=_ACCENT, zorder=4)
        if labelled:
            ax.text(x_right + 0.018, y_right[j],
                    f"desk {problem.desk_labels.get(desk, desk)}  ({desk})",
                    ha="left", va="center", fontsize=size, color=_INK, zorder=5)

    ax.text(x_left, 1.0, _people(n_left), ha="center", va="bottom",
            fontsize=9.0, color=_INK, fontweight="medium")
    ax.text(x_right, 1.0,
            f"{_n(n_right)} desk{'' if n_right == 1 else 's'} between them",
            ha="center", va="bottom", fontsize=9.0, color=_INK,
            fontweight="medium")

    ax.legend(
        handles=[
            Line2D([0], [0],
                   color=colors[r - 1] if r <= len(colors) else _MUTED,
                   linewidth=1.9 if r == 1 else 1.0,
                   label=f"{_ordinal(r)} choice")
            for r in range(1, k + 1)
        ],
        loc="lower center", bbox_to_anchor=(0.5, -0.085), ncols=min(max(k, 1), 8),
        frameon=False, fontsize=7.6, handlelength=1.6, columnspacing=1.4,
        handletextpad=0.5,
    )
    return labelled


def _diagnostic_summary_page(
    deck: _Deck, *, config: Any, diagnosis: Infeasibility
) -> None:
    sheet = deck.sheet("diagnostic-summary")
    sheet.text(sheet.w / 2.0, sheet.h - 0.30, AUDIENCE_COORDINATOR, ha="center",
               va="top", fontsize=8.4, color=_WARN, fontweight="bold")
    sheet.cursor -= 0.40

    k = int(diagnosis.k)
    sheet.block("DESK ASSIGNMENT — NO RESULT", size=8.6, color=_WARN,
                weight="bold", gap_after=0.08)
    sheet.heading("This year's choices cannot all be honoured at once",
                  size=21.0, gap_after=0.08)
    sheet.block(_room_labels(config), size=10.5, color=_MUTED, gap_after=0.20)
    sheet.rule(gap_after=0.20)

    sheet.block(
        f"At most {_n(diagnosis.max_satisfiable)} of "
        f"{_people(diagnosis.n_people)} can be given one of their own {k} "
        f"choices at the same time. That leaves {_n(diagnosis.deficiency)} short.",
        size=16.0, weight="medium", gap_after=0.14,
    )
    sheet.block(
        "No assignment has been produced, and none will be until the affected "
        "students have widened their lists. The alternative would be seating "
        "somebody at a desk they did not choose, which this process does not do "
        "— that guarantee is the reason people were willing to submit honest "
        "rankings in the first place.",
        size=9.8, gap_after=0.16,
    )
    sheet.block(
        "This is nobody's fault. Every list here is a perfectly reasonable set "
        "of preferences. What has happened is that a group of people named "
        "overlapping desks, and there are fewer desks in that overlap than "
        "there are people in the group. It is a property of the combination, "
        "not of anybody's individual choices, and it is invisible to everyone "
        "filling in the form.",
        size=9.8, color=_ACCENT, gap_after=0.20,
    )

    sheet.rule(gap_after=0.16)
    rows: list[tuple[str, str]] = [
        ("people in the pool", _n(diagnosis.n_people)),
        ("desks in the pool", _n(diagnosis.n_desks)),
        ("ranked choices each (K)", _n(k)),
        ("most that can be seated", _n(diagnosis.max_satisfiable)),
        ("short by", _n(diagnosis.deficiency)),
        ("over-subscribed groups found", _n(len(diagnosis.blocking_sets))),
    ]
    if diagnosis.k_min_submitted is not None:
        rows.append(("would have worked at K =", _n(diagnosis.k_min_submitted)))
    else:
        rows.append(("would have worked at K =",
                     f"nowhere at or below {_n(k)}: the submitted lists cannot "
                     f"be made to fit by tightening K"))
    if diagnosis.k_min_extended is not None:
        rows.append(("hypothetically feasible at K =",
                     f"{_n(diagnosis.k_min_extended)}  (diagnostic only — see "
                     f"below)"))
    else:
        rows.append(("hypothetically feasible at K =",
                     "no value of K works: there are structurally too few "
                     "eligible desks for this group"))
    if diagnosis.always_unmatched:
        rows.append(("cannot be seated in any solution",
                     _n(len(diagnosis.always_unmatched))))
    if diagnosis.sometimes_unmatched:
        rows.append(("at risk in some solutions",
                     _n(len(diagnosis.sometimes_unmatched))))
    sheet.kv_rows(rows, key_w=2.9, size=9.2, gap_after=0.14)

    sheet.block(
        "The 'hypothetically feasible' number is the solver filling in ranks "
        "past K on people's behalf, in a seeded order over the desks they are "
        "eligible for. It answers 'how close were we' and nothing else. No "
        "assignment is ever produced from invented ranks, because they were not "
        "chosen by the student.",
        size=8.6, color=_MUTED, gap_after=0.0,
    )


def _diagnostic_blocking_pages(
    deck: _Deck, *, build: Any, diagnosis: Infeasibility,
    page_size: tuple[float, float],
) -> None:
    problem = build.problem
    k = int(diagnosis.k)
    sets = list(diagnosis.blocking_sets)

    if not sets:
        sheet = deck.sheet("diagnostic-blocking")
        sheet.text(sheet.w / 2.0, sheet.h - 0.30, AUDIENCE_COORDINATOR,
                   ha="center", va="top", fontsize=8.4, color=_WARN,
                   fontweight="bold")
        sheet.cursor -= 0.28
        sheet.heading("Where the shortage is", size=16.0)
        sheet.block(
            "No single over-subscribed group could be isolated. That happens "
            "when the shortage is spread across the whole pool rather than "
            "concentrated in one clique — most often because there are simply "
            f"fewer desks ({_n(diagnosis.n_desks)}) than people "
            f"({_n(diagnosis.n_people)}). Widening any individual list will not "
            "help in that case; the pool itself has to change.",
            size=9.8, gap_after=0.0,
        )
        return

    for index, bset in enumerate(sets, start=1):
        sheet = deck.sheet("diagnostic-blocking")
        sheet.text(sheet.w / 2.0, sheet.h - 0.30, AUDIENCE_COORDINATOR,
                   ha="center", va="top", fontsize=8.4, color=_WARN,
                   fontweight="bold")
        sheet.cursor -= 0.28
        title = "Where the shortage is"
        if len(sets) > 1:
            title += f" — group {index} of {len(sets)}"
        sheet.heading(title, size=16.0, gap_after=0.08)
        sheet.block(
            f"{_people(len(bset.people))} on the left named only the "
            f"{_n(len(bset.desks))} desk"
            f"{'' if len(bset.desks) == 1 else 's'} on the right between them, "
            f"so at least {_n(bset.shortfall)} of them cannot be seated within "
            f"their top {k} however the lines are followed.",
            size=10.0, gap_after=0.08,
        )

        ax_h = max(2.4, sheet.room - 1.05)
        ax_y = sheet.cursor - ax_h
        labelled = _blocking_axes(
            sheet, problem, list(bset.people), list(bset.desks), k=k,
            x_in=sheet.left + 0.10, y_in=ax_y, w_in=sheet.text_width - 0.20,
            h_in=ax_h,
        )
        sheet.cursor = ax_y - 0.40

        caption = (
            "How to read this: each line joins a student to one desk they "
            "ranked, coloured by where it sat on their list. Every line from "
            "the left ends somewhere on the right — that is the whole problem. "
            "A desk seats one person, so with more people than desks on this "
            "page some of these lines have to go unused. Widening any of these "
            "lists adds a line to a desk that is not on the right-hand side, "
            "and that is what breaks the deadlock."
        )
        if not labelled:
            caption += (" There are too many rows to name legibly here; "
                        "everybody in this group is listed on the next page.")
        sheet.block(caption, size=8.8, color=_MUTED, gap_after=0.0)

        if not labelled:
            items = [
                _Item(f"Group {index}: {_people(len(bset.people))} competing for "
                      f"{_n(len(bset.desks))} desks.", size=9.4, gap_after=0.14),
                _Item("STUDENTS", size=8.2, color=_ACCENT, weight="bold",
                      gap_before=0.08, gap_after=0.05),
            ]
            for email, name in sorted(zip(bset.people, bset.names),
                                      key=lambda pair: (pair[1].casefold(), pair[0])):
                items.append(_Item(f"{name} <{email}>", size=8.6, indent=0.17))
            items.append(_Item("DESKS THEY NAMED BETWEEN THEM", size=8.2,
                               color=_ACCENT, weight="bold", gap_before=0.12,
                               gap_after=0.05))
            for desk, label in sorted(zip(bset.desks, bset.desk_labels)):
                items.append(_Item(f"desk {label}  ({desk})", size=8.6, indent=0.17))
            _flow(deck, "diagnostic-roster",
                  f"Group {index}: who, and which desks", items,
                  page_size=page_size, banner=AUDIENCE_COORDINATOR)


def _diagnostic_round2_pages(
    deck: _Deck, *, build: Any, diagnosis: Infeasibility,
    page_size: tuple[float, float],
) -> None:
    from . import diagnostics as diag_mod

    try:
        entries = diag_mod.build_round2(build.problem, diagnosis)
    except Exception as exc:  # pragma: no cover - defensive
        entries = ()
        deck.notes.append(f"round-2 scope could not be computed: {exc}")

    suggested = (diagnosis.k_min_extended
                 if diagnosis.k_min_extended is not None else diagnosis.k + 1)

    items: list[_Item] = [
        _Item(f"{_people(len(entries))} need to re-rank. Everybody else's "
              f"submission stands and does not need to be touched.",
              size=11.0, gap_after=0.12),
        _Item("Their assignments are deliberately not finalised yet. Publishing "
              "part of the result now would leak information into the second "
              "round and would give the people already seated a reason to think "
              "the outcome was settled before everybody had submitted.",
              size=9.0, color=_MUTED, gap_after=0.12),
        _Item(f"Ask them for at least {_n(suggested)} ranked desks. They do not "
              f"have to change their top pick — only to keep going further down "
              f"the list. The full machine-readable scope, including which desks "
              f"are genuinely still open to each of them, is in "
              f"round2_input.json and round2_roster.csv.",
              size=9.0, gap_after=0.18),
    ]

    for entry in entries:
        items.append(_Item(f"{entry.name} <{entry.email}>", size=9.2,
                           weight="medium", gap_before=0.09, gap_after=0.025))
        items.append(_Item(entry.reason, size=8.4, color=_MUTED, indent=0.17,
                           gap_after=0.025))
        items.append(_Item(
            "currently ranked: " + (", ".join(entry.current_choices) or "nothing"),
            size=8.4, indent=0.17, gap_after=0.025))
        items.append(_Item(
            f"still open to them ({_n(len(entry.available_desks))}): "
            + (", ".join(entry.available_desks) or "none"),
            size=8.4, indent=0.17, color=_MUTED, gap_after=0.025))
        items.append(_Item(
            f"ask for at least {_n(entry.suggested_min_ranks)} ranks",
            size=8.4, indent=0.17, color=_ACCENT))

    if not entries:
        items.append(_Item(
            "No individual could be identified as needing to act, which means "
            "the shortage is structural rather than a clique of overlapping "
            "lists. Adding desks to the pool, or reducing the number of people "
            "competing for them, is the only thing that will change the answer.",
            size=9.0, color=_MUTED))

    _flow(deck, "diagnostic-round2", "Who needs to act, and what to ask them",
          items, page_size=page_size, banner=AUDIENCE_COORDINATOR)


def _diagnostic_provenance_page(
    deck: _Deck, *, config: Any, diagnosis: Infeasibility,
    provenance: Mapping[str, Any], responses: Any,
) -> None:
    sheet = deck.sheet("provenance")
    sheet.text(sheet.w / 2.0, sheet.h - 0.30, AUDIENCE_COORDINATOR, ha="center",
               va="top", fontsize=8.4, color=_WARN, fontweight="bold")
    sheet.cursor -= 0.28
    sheet.heading("Provenance", size=16.0, gap_after=0.08)
    sheet.block(
        "This diagnosis is reproducible in exactly the way a successful run is: "
        "it is a pure function of the same inputs and the same seed.",
        size=9.0, color=_MUTED, gap_after=0.18,
    )

    rows: list[tuple[str, str]] = [
        ("outcome", f"INFEASIBLE at K={diagnosis.k}; no assignment produced "
                    f"(exit code 2)"),
        ("tie-break seed",
         repr(diagnosis.seed_string
              or _resolved_seed_of(config))),
        ("responses sha256",
         str(provenance.get("responses_sha256")
             or getattr(responses, "sha256", None) or "not supplied to the report")),
    ]
    for name in sorted(getattr(config, "file_hashes", {}) or {}):
        rows.append((f"config_sha256[{name}]", str(config.file_hashes[name])))
    for key, value in sorted(prov_mod.environment_versions().items()):
        rows.append((key, str(value)))
    sheet.kv_rows(rows, key_w=2.45, size=8.5, value_family="monospace",
                  gap=0.038, gap_after=0.16)

    sheet.rule(gap_after=0.14)
    sheet.block("WHAT TO DO NEXT", size=8.0, color=_ACCENT, weight="bold",
                gap_after=0.08)
    sheet.bullets([
        "Contact the students listed earlier. Send them the group diagram — it "
        "explains the situation better than a paragraph does.",
        "Re-open the form for them using round2_roster.csv. They keep their "
        "existing ranking; they only add to it.",
        "Re-export the responses, commit the raw export, and re-run. Do not "
        "hand-edit an assignment: there is no code path for it, and adding one "
        "would end the guarantee that makes this process worth running.",
    ], size=9.2)

    sheet.block(
        "No timestamp appears in this document. Wall-clock time is excluded "
        "from the reproducibility target (SPEC §5.5), so printing it would make "
        "two identical runs produce two different files.",
        size=8.4, color=_MUTED, gap_before=0.08, gap_after=0.0,
    )


def build_diagnostic_report(
    path: str | os.PathLike[str],
    config: Any,
    build: Any,
    diagnosis: Infeasibility,
    *,
    provenance: Mapping[str, Any] | None = None,
    responses: Any = None,
    page_size: tuple[float, float] = PAGE_SIZE,
) -> ReportResult:
    """The PDF for a run that failed the K-floor (SPEC §6).

    The coordinator shows this to the students who are affected, so it is
    written to be read cold, with nobody standing next to it, and written not to
    read as an accusation. The shortage is a property of a *set* of lists, not
    of a person; the document says so on the first page and again under the
    diagram.

    It carries the coordinator footer, because naming who wanted which desks is
    exactly the per-person preference data SPEC §7.2 keeps out of the public
    report. That is unavoidable here — the diagram *is* the explanation — so the
    marking is made honest rather than the content sanitised.
    """
    problem = build.problem
    prov = dict(provenance or {})
    response_hash = str(
        prov.get("responses_sha256") or getattr(responses, "sha256", "") or "")

    target = Path(os.fspath(path))
    if str(target.parent) not in ("", "."):
        target.parent.mkdir(parents=True, exist_ok=True)

    deck = _Deck(page_size, AUDIENCE_COORDINATOR, response_hash,
                 subtitle=f"INFEASIBLE at K={diagnosis.k}")

    with fmap.open_pdf(target,
                       "deskmatch diagnostic — no assignment produced") as pdf:
        fstats.apply_house_style()

        _diagnostic_summary_page(deck, config=config, diagnosis=diagnosis)
        _diagnostic_blocking_pages(deck, build=build, diagnosis=diagnosis,
                                   page_size=page_size)
        _diagnostic_round2_pages(deck, build=build, diagnosis=diagnosis,
                                 page_size=page_size)

        # Where the crunch is, on the actual floor plan. Demand only: there is
        # no assignment to draw, and inventing one would be the whole point
        # missed.
        _map_pages(deck, config=config, problem=problem, solution=None,
                   show_occupants="none", page_size=page_size)
        _contested_page(deck, config=config, problem=problem, solution=None,
                        show_winners=False, page_size=page_size)

        _diagnostic_provenance_page(deck, config=config, diagnosis=diagnosis,
                                    provenance=prov, responses=responses)

        n_pages = deck.write(pdf)

    return ReportResult(
        path=str(target),
        audience=AUDIENCE_COORDINATOR,
        n_pages=n_pages,
        page_kinds=deck.kinds,
        notes=tuple(dict.fromkeys(deck.notes)),
        privacy=None,
    )
