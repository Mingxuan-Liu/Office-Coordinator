"""Bipartite matching primitives, and the structural diagnostics built on them.

The solver never guesses at feasibility. Before any cost minimisation happens we
answer a purely combinatorial question -- *can* everyone get a top-K desk? -- with
Hopcroft-Karp. If the answer is no, everything else in this module exists to turn
that "no" into a sentence the coordinator can act on: which people, which desks,
how many short.

Hopcroft-Karp is implemented here rather than taken from scipy for three reasons:
we need the internal alternating-BFS layer for Konig's theorem, we need control
over iteration order so results are reproducible, and the tests cross-check this
implementation against scipy's on random instances.

Everything here is deterministic: adjacency lists are sorted, vertices are visited
in index order, and no set is ever iterated without sorting first.
"""

from __future__ import annotations

from collections import deque
from typing import Iterable, Sequence

import numpy as np

INF = float("inf")


# --------------------------------------------------------------------------
# Graph representation
# --------------------------------------------------------------------------


def adjacency(allowed: np.ndarray) -> list[list[int]]:
    """Boolean (n_left, n_right) matrix -> sorted adjacency lists."""
    if allowed.ndim != 2:
        raise ValueError(f"allowed must be 2-D, got shape {allowed.shape}")
    return [sorted(np.flatnonzero(row).tolist()) for row in allowed]


# --------------------------------------------------------------------------
# Hopcroft-Karp
# --------------------------------------------------------------------------


def hopcroft_karp(
    adj: Sequence[Sequence[int]], n_right: int
) -> tuple[np.ndarray, np.ndarray]:
    """Maximum-cardinality bipartite matching.

    Returns (match_left, match_right); each entry is the index of the partner or
    -1 for unmatched. O(E * sqrt(V)).
    """
    n_left = len(adj)
    match_left = np.full(n_left, -1, dtype=np.int64)
    match_right = np.full(n_right, -1, dtype=np.int64)
    dist = np.zeros(n_left, dtype=np.float64)

    def bfs() -> bool:
        queue: deque[int] = deque()
        for u in range(n_left):
            if match_left[u] == -1:
                dist[u] = 0.0
                queue.append(u)
            else:
                dist[u] = INF
        found = False
        while queue:
            u = queue.popleft()
            for v in adj[u]:
                w = match_right[v]
                if w == -1:
                    found = True
                elif dist[w] == INF:
                    dist[w] = dist[u] + 1.0
                    queue.append(w)
        return found

    def dfs(u: int) -> bool:
        for v in adj[u]:
            w = match_right[v]
            if w == -1 or (dist[w] == dist[u] + 1.0 and dfs(int(w))):
                match_left[u] = v
                match_right[v] = u
                return True
        # No augmenting path through u in this phase; do not revisit it.
        dist[u] = INF
        return False

    while bfs():
        for u in range(n_left):
            if match_left[u] == -1:
                dfs(u)

    return match_left, match_right


def matching_size(adj: Sequence[Sequence[int]], n_right: int) -> int:
    match_left, _ = hopcroft_karp(adj, n_right)
    return int((match_left >= 0).sum())


def has_perfect_left_matching(allowed: np.ndarray) -> bool:
    """True iff every LEFT vertex (person) can be matched. Note this is about
    saturating the people, not the desks -- n_people != n_desks is normal."""
    adj = adjacency(allowed)
    return matching_size(adj, allowed.shape[1]) == allowed.shape[0]


# --------------------------------------------------------------------------
# Alternating reachability -> Konig / Hall
# --------------------------------------------------------------------------


def alternating_reachable(
    adj: Sequence[Sequence[int]], match_left: np.ndarray, match_right: np.ndarray
) -> tuple[list[int], list[int]]:
    """Vertices reachable by M-alternating paths from unmatched left vertices.

    Walk left->right along ANY edge and right->left along MATCHED edges only.
    Returns (left_reachable, right_reachable), both sorted.

    With Z the reachable set, S = left ∩ Z satisfies |N(S)| < |S| whenever the
    matching is not left-perfect: every vertex of N(S) is matched (otherwise we
    would have found an augmenting path and the matching would not be maximum),
    and each is matched back into S, so |N(S)| = |S| - (number of unmatched left
    vertices). That is Konig's theorem doing the work, and it is what lets us
    name the blocking group instead of just reporting a count.
    """
    n_left = len(adj)
    seen_left = np.zeros(n_left, dtype=bool)
    seen_right = np.zeros(len(match_right), dtype=bool)

    queue: deque[int] = deque()
    for u in range(n_left):
        if match_left[u] == -1:
            seen_left[u] = True
            queue.append(u)

    while queue:
        u = queue.popleft()
        for v in adj[u]:
            if seen_right[v]:
                continue
            seen_right[v] = True
            w = match_right[v]
            if w != -1 and not seen_left[w]:
                seen_left[w] = True
                queue.append(int(w))

    return sorted(np.flatnonzero(seen_left).tolist()), sorted(
        np.flatnonzero(seen_right).tolist()
    )


def neighbourhood(adj: Sequence[Sequence[int]], lefts: Iterable[int]) -> list[int]:
    """N(S), sorted."""
    out: set[int] = set()
    for u in lefts:
        out.update(adj[u])
    return sorted(out)


def is_violator(adj: Sequence[Sequence[int]], lefts: Sequence[int]) -> bool:
    return len(neighbourhood(adj, lefts)) < len(lefts)


# --------------------------------------------------------------------------
# Blocking-set extraction
# --------------------------------------------------------------------------


def _components(
    adj: Sequence[Sequence[int]], lefts: Sequence[int]
) -> list[list[int]]:
    """Split a left-vertex set into connected components of the induced
    bipartite subgraph, so the coordinator gets separate, independently true
    statements instead of one giant union."""
    left_set = set(lefts)
    right_owner: dict[int, list[int]] = {}
    for u in lefts:
        for v in adj[u]:
            right_owner.setdefault(v, []).append(u)

    unvisited = set(lefts)
    comps: list[list[int]] = []
    while unvisited:
        start = min(unvisited)
        comp: list[int] = []
        stack = [start]
        unvisited.discard(start)
        while stack:
            u = stack.pop()
            comp.append(u)
            for v in adj[u]:
                for w in right_owner.get(v, ()):
                    if w in unvisited and w in left_set:
                        unvisited.discard(w)
                        stack.append(w)
        comps.append(sorted(comp))
    return sorted(comps, key=lambda c: (-len(c), c))


def minimal_violator(
    adj: Sequence[Sequence[int]],
    lefts: Sequence[int],
    rng: np.random.Generator | None = None,
) -> list[int]:
    """Shrink a Hall violator until no member can be dropped.

    Greedy: try removing each member; keep the removal if the set is still a
    violator. The order of attempts changes which minimal violator you land on
    when several exist, so it is driven by the published seed rather than by
    index order -- otherwise the same low-index people would be named every year.

    The result is minimal -- NO single member can be dropped -- but not minimUM
    (smallest possible). Finding the minimum one is NP-hard in general; minimal
    is what the coordinator actually needs: a tight, true statement about a
    specific group.

    Repeated passes are required, not one. Dropping member A can be invalid
    while B is still present and become valid once B is gone, so a single pass
    that happens to try A before B leaves A in the set forever. Concretely, with
    people {0,1,2,3,4} reaching desks {0,2},{1},{1,2},{0},{0}: a single pass in
    index order stalls at {1,3,4}, even though {3,4} is still a violator. We
    therefore keep sweeping until a full pass removes nothing, which is exactly
    the condition "no single member can be dropped". Each successful removal
    shrinks the set by one, so this terminates in at most |lefts| passes.
    """
    current = list(lefts)
    if not is_violator(adj, current):
        return []

    changed = True
    while changed and len(current) > 1:
        changed = False
        order = list(range(len(current)))
        if rng is not None:
            order = rng.permutation(len(current)).tolist()
        for u in [current[i] for i in order]:
            if len(current) <= 1:
                break
            if u not in current:
                continue  # already removed earlier in this pass
            trial = [x for x in current if x != u]
            if is_violator(adj, trial):
                current = trial
                changed = True
    return sorted(current)


def deficiency_set(allowed: np.ndarray) -> tuple[list[int], list[int]]:
    """The WIDEST Hall violator: every person reachable from an unmatched one.

    Returns (lefts, N(lefts)), or ([], []) if the instance is feasible.

    This is the complement to `minimal_violator`, and both are needed for the
    coordinator to act correctly. A minimal set is the tightest true statement
    ("these six span only five desks"), but it can badly understate the problem:
    when nine people all rank the same five desks, *some* six of them form a
    minimal violator short by one, while the real situation is nine people short
    by four. Acting on the minimal set alone, you would ask one person to
    re-rank when four have to.

    By Konig's theorem this set's shortfall equals the deficiency exactly, so it
    is the honest headline number.
    """
    adj = adjacency(allowed)
    match_left, match_right = hopcroft_karp(adj, allowed.shape[1])
    if int((match_left >= 0).sum()) == allowed.shape[0]:
        return [], []
    lefts, _ = alternating_reachable(adj, match_left, match_right)
    return lefts, neighbourhood(adj, lefts)


def blocking_groups(
    allowed: np.ndarray, rng: np.random.Generator | None = None
) -> list[tuple[list[int], list[int], list[int], list[int]]]:
    """Per-component blocking analysis: (full, N(full), minimal, N(minimal)).

    One entry per connected deficiency component. Each entry carries BOTH the
    whole over-subscribed group and a minimal violator inside it, because the
    coordinator needs both numbers and they answer different questions:

      * the full group is how many people are competing and, via its shortfall,
        how many of them must re-rank -- the number to act on;
      * the minimal group is the tightest true statement, useful for showing a
        specific set of people that they alone are over-subscribed.

    Reporting only the minimal one understates the work (nine people on five
    desks yields "some six of you are short by one" when four must move).
    Reporting a single global union instead overstates it: two *independent*
    over-subscribed groups would be merged into one statement, sending twice as
    many students to re-rank as necessary. Hence: per component, both numbers.

    `minimal` is omitted (returned equal to `full`) when the component is
    already minimal.
    """
    adj = adjacency(allowed)
    match_left, match_right = hopcroft_karp(adj, allowed.shape[1])
    if int((match_left >= 0).sum()) == allowed.shape[0]:
        return []

    reachable_left, _ = alternating_reachable(adj, match_left, match_right)
    out: list[tuple[list[int], list[int], list[int], list[int]]] = []
    for comp in _components(adj, reachable_left):
        if not is_violator(adj, comp):
            continue
        minimal = minimal_violator(adj, comp, rng)
        out.append((comp, neighbourhood(adj, comp), minimal, neighbourhood(adj, minimal)))

    if not out:  # pragma: no cover - the reachable set is always a violator here
        minimal = minimal_violator(adj, reachable_left, rng)
        out.append((reachable_left, neighbourhood(adj, reachable_left),
                    minimal, neighbourhood(adj, minimal)))

    out.sort(key=lambda g: (-(len(g[0]) - len(g[1])), g[0]))
    return out


def hall_violators(
    allowed: np.ndarray, rng: np.random.Generator | None = None
) -> list[tuple[list[int], list[int]]]:
    """All minimal blocking groups, as (left_indices, their_neighbourhood).

    Empty list means the instance is feasible.
    """
    adj = adjacency(allowed)
    match_left, match_right = hopcroft_karp(adj, allowed.shape[1])
    if int((match_left >= 0).sum()) == allowed.shape[0]:
        return []

    reachable_left, _ = alternating_reachable(adj, match_left, match_right)
    out: list[tuple[list[int], list[int]]] = []
    for comp in _components(adj, reachable_left):
        if not is_violator(adj, comp):
            # A component can be tight (|N| == |S|) while the union is deficient;
            # only report the ones that are genuinely over-subscribed.
            continue
        minimal = minimal_violator(adj, comp, rng)
        if minimal:
            out.append((minimal, neighbourhood(adj, minimal)))

    if not out:
        # Fall back to the whole reachable set, which is always a violator when
        # the matching is not left-perfect. Reaching here would mean the
        # component split lost the deficiency, which should be impossible.
        minimal = minimal_violator(adj, reachable_left, rng)
        if minimal:
            out.append((minimal, neighbourhood(adj, minimal)))

    out.sort(key=lambda pair: (-(len(pair[0]) - len(pair[1])), pair[0]))
    return out


# --------------------------------------------------------------------------
# Who is actually at risk
# --------------------------------------------------------------------------


def _size_without(adj: Sequence[Sequence[int]], n_right: int, drop_left: int) -> int:
    sub = [list(a) if i != drop_left else [] for i, a in enumerate(adj)]
    return matching_size(sub, n_right)


def _size_using_edge(
    adj: Sequence[Sequence[int]], n_right: int, u: int, v: int
) -> int:
    """Size of a maximum matching that is forced to use edge (u, v)."""
    sub: list[list[int]] = []
    for i, a in enumerate(adj):
        if i == u:
            sub.append([])
        else:
            sub.append([x for x in a if x != v])
    return 1 + matching_size(sub, n_right)


def unmatched_analysis(
    allowed: np.ndarray,
) -> tuple[list[int], list[int]]:
    """Distinguish 'at risk' from 'cannot be seated at all'.

    Returns (always_unmatched, sometimes_unmatched), both sorted left indices.

      * sometimes_unmatched -- some maximum matching leaves this person out.
        They are at the mercy of the tie-break, which is exactly the population
        the coordinator needs to talk to.
      * always_unmatched -- NO maximum matching covers them. Their top-K list is
        structurally unsatisfiable no matter what; they must re-rank.

    always_unmatched ⊆ sometimes_unmatched.

    Both are computed exactly rather than heuristically. Cost is O(n*K) matchings,
    which is nothing at department scale and only runs on the failure path.
    """
    adj = adjacency(allowed)
    n_left, n_right = allowed.shape
    nu = matching_size(adj, n_right)

    sometimes: list[int] = []
    always: list[int] = []
    for u in range(n_left):
        # Some maximum matching misses u iff dropping u does not shrink nu.
        if _size_without(adj, n_right, u) == nu:
            sometimes.append(u)
            # u is coverable iff some edge at u lies in a maximum matching.
            coverable = any(_size_using_edge(adj, n_right, u, v) == nu for v in adj[u])
            if not coverable:
                always.append(u)
    return always, sometimes


# --------------------------------------------------------------------------
# K sweeps
# --------------------------------------------------------------------------


def min_feasible_k(rank: np.ndarray, eligible: np.ndarray, k_max: int) -> int | None:
    """Smallest K' in 1..k_max for which everyone can get a top-K' desk.

    `rank` is 1-based with -1 for "not ranked". Feasibility is monotone in K'
    (a larger K' only adds edges), so a linear sweep upward returns the minimum
    and we can stop at the first success.
    """
    for k_try in range(1, k_max + 1):
        allowed = eligible & (rank >= 1) & (rank <= k_try)
        if has_perfect_left_matching(allowed):
            return k_try
    return None
