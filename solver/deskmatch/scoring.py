"""Rank-to-points conversion, seeded RNG, and the tie-break jitter bound.

Everything in here exists to support one claim, which is the claim the whole
process rests on:

    The published seed can only ever choose *among assignments that are exactly
    tied for optimal*. It can never change which total is best.

That is a theorem, not a hope, and §"The bound" below is its proof. The bound is
also re-derived at runtime against the actual matrix (`assert_jitter_bound`), so
a future change to the curve cannot quietly invalidate it.
"""

from __future__ import annotations

import hashlib
from fractions import Fraction
from math import gcd
from typing import Sequence

import numpy as np

from .errors import DeterminismError

# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------


def seed_int(seed_string: str) -> int:
    """Map the published seed string to a 64-bit integer, stably.

    Python's built-in hash() is salted per-process (PYTHONHASHSEED) and would
    make results irreproducible; SHA-256 is specified byte-for-byte forever.
    NumPy's PCG64 stream is covered by NumPy's stream-compatibility policy, so
    default_rng(seed_int(s)) yields the same draws on any platform and any
    supported NumPy version.
    """
    digest = hashlib.sha256(seed_string.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def make_rng(seed_string: str) -> np.random.Generator:
    return np.random.default_rng(seed_int(seed_string))


# --------------------------------------------------------------------------
# Exact integerisation of the curve
# --------------------------------------------------------------------------


def _lcm(a: int, b: int) -> int:
    return a * b // gcd(a, b)


def integerise(curve: Sequence[Fraction]) -> tuple[tuple[int, ...], int]:
    """Scale a rational curve to integers.

    Returns (integer_points, scale) with integer_points[i] == curve[i] * scale
    exactly. The scale is the LCM of the denominators, i.e. the smallest factor
    that clears them all.

    Why this matters: with an all-integer score matrix, two assignments with
    *different* totals differ by at least 1. That "at least 1" is the entire
    basis for the jitter bound below. If we left 4.5 as a binary float we would
    have no exact statement about the minimum gap, and the tie-break could in
    principle flip a genuine (if tiny) preference difference.
    """
    if not curve:
        raise ValueError("empty scoring curve")
    scale = 1
    for value in curve:
        scale = _lcm(scale, value.denominator)
    points = []
    for value in curve:
        scaled = value * scale
        if scaled.denominator != 1:
            raise DeterminismError(
                f"curve value {value} did not integerise at scale {scale}; "
                "this should be impossible and indicates a bug in integerise()"
            )
        points.append(int(scaled))
    return tuple(points), scale


# --------------------------------------------------------------------------
# The jitter bound
# --------------------------------------------------------------------------

#: Safety factor. The bound needs n*eps < 1; we target n*eps < 1/MARGIN.
_MARGIN = 2


def jitter_epsilon(n_assigned: int) -> float:
    """Per-cell jitter magnitude.

    The bound
    ---------
    Let the score matrix be integral (guaranteed by `integerise`). An assignment
    picks exactly n = n_assigned cells, so any achievable total is an integer,
    and two *distinct* totals differ by at least 1.

    Add independent jitter J[p,d] ~ Uniform[0, eps) to every cell. The total
    jitter accumulated by any assignment lies in [0, n*eps).

    Take assignments A and B with true totals T(A) > T(B), hence T(A) >= T(B)+1.
    If n*eps < 1 then

        T(A) + jitter(A)  >=  T(A)  =  T(B) + 1  >  T(B) + n*eps  >  T(B) + jitter(B)

    so the jittered comparison agrees with the true one. Jitter therefore
    reorders only cells whose true totals are *equal* -- exactly the ties we
    want the published seed to resolve. QED.

    We use eps = 1 / (MARGIN * (n + 1)), giving n*eps = n/(2n+2) < 1/2, a factor
    of two inside the requirement even before floating-point slop.
    """
    if n_assigned < 0:
        raise ValueError("n_assigned must be non-negative")
    return 1.0 / (_MARGIN * (n_assigned + 1))


def assert_jitter_bound(points: np.ndarray, n_assigned: int, epsilon: float) -> None:
    """Re-derive the bound against the real matrix at runtime (invariant I6).

    Guards against three ways the proof above could stop applying: a non-integer
    matrix, an epsilon someone tuned by hand, and float64 running out of
    precision on a large total.
    """
    if not np.issubdtype(points.dtype, np.integer):
        raise DeterminismError(
            f"score matrix must be integral for the jitter bound to hold, "
            f"got dtype {points.dtype}. See scoring.integerise()."
        )
    total_jitter = n_assigned * epsilon
    if not total_jitter < 1.0:
        raise DeterminismError(
            f"jitter bound violated: n_assigned({n_assigned}) * epsilon({epsilon}) "
            f"= {total_jitter}, which is not < 1. Tie-break jitter could change "
            f"the optimum. Refusing to continue."
        )

    # float64 carries 53 bits of mantissa. If the largest achievable total is so
    # big that 1 ulp exceeds our jitter, the additions below would round the
    # jitter away (harmless) or, worse, round the integer part (not harmless).
    max_total = float(np.abs(points).max(initial=0)) * max(n_assigned, 1)
    if max_total > 0:
        ulp = np.spacing(max_total)
        if ulp >= epsilon:
            raise DeterminismError(
                f"score magnitudes are too large for float64 tie-breaking: "
                f"1 ulp at {max_total:g} is {ulp:g}, which is >= epsilon {epsilon:g}. "
                f"Reduce the scoring curve's dynamic range."
            )


def jitter_matrix(
    rng: np.random.Generator, shape: tuple[int, int], epsilon: float
) -> np.ndarray:
    """Deterministic jitter drawn from the seeded generator.

    Drawn for the full matrix in one call so the draw depends only on the shape
    and the seed -- not on which cells happen to be allowed, which would make
    the result depend on preference data in a way that is harder to reason about.
    """
    return rng.random(shape) * epsilon


# --------------------------------------------------------------------------
# Curve application
# --------------------------------------------------------------------------


def points_for_rank(int_curve: Sequence[int], rank: int) -> int:
    """Points for a 1-based rank. Ranks outside 1..K score nothing; such cells
    are masked out entirely, so this is a defensive default rather than a path
    the solver takes."""
    if 1 <= rank <= len(int_curve):
        return int_curve[rank - 1]
    return 0


def describe_curve(name: str, curve: Sequence[Fraction]) -> str:
    body = ", ".join(str(c) for c in curve)
    return f"{name}: [{body}]  (K={len(curve)})"
