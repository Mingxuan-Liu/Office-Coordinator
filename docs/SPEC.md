# deskmatch — Internal Specification (authoritative contract)

This document is the contract that every module in this repository is written
against. If code and this document disagree, that is a bug in one of them.
Read this before changing anything.

Audience: a developer (probably a grad student) modifying the system.
For *operating* the system, read `README.md` instead.

---

## 0. Non-negotiable invariants

These are correctness properties, asserted at runtime, not aspirations.

| ID | Invariant |
|----|-----------|
| **I1** | No dimension of the problem is hard-coded. `n_people`, `n_desks`, `n_zones`, `n_rooms`, `n_features`, `K` are all derived from config/data at runtime. |
| **I2** | The solver is a pure function of `(responses, config, seed)`. No I/O, no clock, no environment, no manual override path. |
| **I3** | Same `(responses, config, seed)` ⇒ byte-identical `results.json`. Always, on any machine, any OS, any supported Python. |
| **I4** | Every assigned desk is within that person's submitted top-K. Asserted post-solve. |
| **I5** | Every assigned desk is in a zone permitted for that person by `eligibility.json`. Asserted post-solve. |
| **I6** | Tie-break jitter is provably too small to change which assignments are optimal. Bound asserted at runtime. |
| **I7** | If **I4** cannot be satisfied for everyone, the run FAILS. It never silently degrades to a worse assignment. |
| **I8** | `n_people != n_desks` is normal, in either direction. |

`K` is `len(scoring.curves[primary])`. It is called "K=5" in the prose because
the default is 5. Nothing in the code assumes 5.

---

## 1. Repository layout

```
config/           # the only place problem-specific values live
  rooms.json
  eligibility.json
  roster.csv
  scoring.json
  floorplans/*.png
tools/
  sync_config.py       # config/ -> frontend/ConfigData.gs
  build_local_preview.py
  merge_keepers.py     # the pre-lock claim log -> roster.csv (§3.5)
  calibrate/           # standalone floor-plan calibration tool
solver/
  deskmatch/      # the Python package
  pyproject.toml
  requirements.lock
frontend/         # Google Apps Script sources + deploy instructions
tests/
docs/
examples/         # runnable synthetic end-to-end example
```

---

## 2. Config schemas

All configs are validated by `deskmatch.validate` **before anything else runs**.
Validation errors are collected and reported together, each naming the file,
the JSON path, the offending value, and what was expected. A validation failure
is `ConfigError`, never a `KeyError` / `TypeError` traceback.

### 2.1 `config/rooms.json`

```jsonc
{
  "schema_version": 1,
  "coord_space": "normalized",        // "normalized" (0..1) | "pixels"
  "zones": {                           // zone id -> metadata. Arbitrary strings.
    "candidate_side":    { "label": "Upper years side",          "color": "#3b6ea5" },
    "precandidate_side": { "label": "First and second years side","color": "#a55b3b" }
  },
  "rooms": [
    {
      "id": "main_office",
      "label": "Main Grad Office (Room 406)",
      "image": "floorplans/main_office.png",   // relative to config/
      "image_size": [1212, 706],               // px; required, used to convert coords
      "features": [                    // v2+, optional. Decoration only.
        {
          "id": "huddle_rm",
          "kind": "room",              // outline|wall|door|window|partition|
                                       // furniture|room|divider
          "label": "Huddle Room",
          "shape": { "rect": [0.477, 0.607, 0.172, 0.271] },
          "note": ""                   // optional tooltip
        }
      ],
      "desks": [
        {
          "id": "D01",                  // stable, unique across ALL rooms
          "label": "1",                 // what humans call it
          "zone": "candidate_side",
          "shape": {                     // exactly one of rect | polygon
            "rect": [0.047, 0.268, 0.055, 0.190]   // [x, y, w, h]
          },
          "notes": ""                   // optional, shown as tooltip
        }
      ]
    }
  ]
}
```

Rules enforced by the validator:

- `desk.id` unique across all rooms; `desk.zone` ∈ `zones`; `room.id` unique.
- Every zone in `zones` is referenced by ≥1 desk (warning, not error — an empty
  zone is legal but is almost always a typo).
- `shape.rect` = `[x, y, w, h]`, `shape.polygon` = `[[x,y], ...]` with ≥3 points.
- **Features** (`schema_version` ≥ 2) are never selectable, have no zone, and
  never enter the solve. They may additionally use `shape.polyline`
  (`[[x,y], ...]`, ≥2 points) for walls. A *desk* may not: a polyline has no
  interior, so the desk would be impossible to click — the validator rejects it.
  Feature ids must be unique within a room and must not collide with any desk id.
  An unrecognised `kind` is a warning; it still draws as generic structure.
  Features may overlap anything, including each other, without complaint.
- In `normalized` space all coordinates ∈ [0, 1]. In `pixels` space, ∈ image bounds.
- `image` path must exist **or** produce a warning (the solver can render on a
  blank canvas; the frontend cannot).
- Overlapping desk shapes produce a warning naming both desks.

Error message shape (required):
```
rooms.json: rooms[0].desks[13] ("D14"): references zone 'senior_side',
  which is not defined in rooms.json:zones. Defined zones are:
  candidate_side, precandidate_side.
```

### 2.2 `config/eligibility.json`

A **rule table**, evaluated top-to-bottom, first match wins. No `if year <= 2`
anywhere in the codebase.

```jsonc
{
  "schema_version": 1,
  "rules": [
    {
      "id": "precandidates_together",
      "when": { "candidacy": "precandidate" },        // predicate, see below
      "allow_zones": ["precandidate_side"],
      "reason": "Years 1-2 sit together for coursework."
    },
    {
      "id": "everyone_else",
      "when": {},                                      // {} = always matches
      "allow_zones": "*",
      "reason": "Candidates may sit anywhere."
    }
  ]
}
```

Predicate grammar for `when` — a mapping of `roster attribute -> matcher`:

| Matcher form | Example | Meaning |
|---|---|---|
| scalar | `{"candidacy": "precandidate"}` | equality (string compare is case-insensitive, trimmed) |
| list | `{"year": [1, 2]}` | membership |
| range | `{"year": {"min": 1, "max": 2}}` | inclusive numeric range; `min`/`max` each optional |
| negation | `{"candidacy": {"not": "candidate"}}` | inverts any of the above |

Multiple keys in one `when` are ANDed. `allow_zones` is `"*"` or a list of zone
ids (validated against `rooms.json`).

Enforced: the last rule MUST be a catch-all (`"when": {}`), so no person can
fall through with undefined eligibility. Attribute names in `when` must exist as
roster columns.

### 2.3 `config/roster.csv`

```csv
name,email,year,candidacy,keeps_desk,current_desk
Ada Lovelace,ada@umich.edu,4,candidate,no,
Vera Rubin,vera@umich.edu,1,precandidate,no,
Jocelyn Bell,jbell@umich.edu,5,candidate,yes,D07
```

- `email` is the primary key; lower-cased and trimmed on load; must be unique.
- `year` integer ≥ 1.
- `candidacy` free string (validated only against values used in eligibility rules;
  an unreferenced value is a warning).
- `keeps_desk` ∈ {yes,no,true,false,1,0,y,n} case-insensitive.
- If `keeps_desk` is truthy, `current_desk` is REQUIRED and must be a valid desk id.
  That person and that desk are both **removed from the pool** before solving.
- Two people keeping the same desk is an error.
- Extra columns are preserved and usable in eligibility predicates.

### 2.4 `config/scoring.json`

```jsonc
{
  "schema_version": 1,
  "curves": {
    "linear_borda": [5, 4, 3, 2, 1],
    "convex":       [16, 8, 4, 2, 1],
    "concave":      [5, 4.5, 4, 3.5, 3]
  },
  "primary_curve": "linear_borda",
  "comparison_curves": ["convex", "concave"],
  "seed_year": "auto",                 // "auto" | <int year>. Governs the seed.
  "tie_break_seed": "...",             // used ONLY when seed_year is absent
  "seed_committed_at": "2026-09-01T12:00:00-04:00",   // informational; not needed under seed_year
  "sensitivity_seeds": ["alt-seed-a", "alt-seed-b", "alt-seed-c"]
}
```

- All curves must have the same length; that length is **K**.
- Curves must be strictly decreasing (rank 1 worth strictly more than rank 2, …).
  A non-decreasing curve is an error — it would make the ranking meaningless.
- All values > 0. (A zero-valued last rank makes "got 5th choice" indistinguishable
  from "got nothing" in the objective, which breaks the K-floor's meaning.)
- Values may be decimals; they are exactly rationalised to integers internally
  (see §5.3). Non-terminating decimals are rejected at validation.
- `primary_curve` and every `comparison_curves` entry must exist in `curves`.
- **Seed resolution.** If `seed_year` is present it governs and the seed string is
  the year as text (`"2026"`); `tie_break_seed` is then dead config and the
  validator warns if both are set. `"auto"` means the calendar year, **resolved
  once at config load** and written into `results.json`.

  The clock is read at exactly one point, in `config._build_scoring`. Nothing
  downstream reads it. This is load-bearing: if the solve resolved the year
  itself, re-running the 2026 cycle in 2027 would produce a different assignment
  and every published hash would stop verifying — breaking **I3**. `verify` uses
  the seed *recorded in the results file*, never the current year, so
  verification is correct in any later year. There is a test for this.

  To re-run an old cycle deliberately, pin the year: `"seed_year": 2026`.

---

## 3. Response schema — the Component A ↔ Component B contract

This is the only coupling between the Apps Script frontend and the Python solver.
Component B must run from this CSV alone with **no Google dependency**.

### 3.1 CSV columns

| column | type | notes |
|---|---|---|
| `submission_id` | string | unique per row; Apps Script uses `Utilities.getUuid()` |
| `timestamp` | ISO-8601 with UTC offset | e.g. `2026-09-15T14:03:22-04:00` |
| `email` | string | lower-cased on ingest; joins to roster |
| `name` | string | as submitted; roster value wins on conflict |
| `candidacy` | string | as confirmed by the student; overrides roster. The **only** attribute the form collects, and therefore the only one that can override the roster |
| `year` | int | **OPTIONAL, and no longer written.** The form does not ask — candidacy alone decides zones. Read if an older file has it, recorded, never used for eligibility. An unparseable value is a warning. |
| `choice_1` … `choice_K` | desk id | exactly K columns, contiguous from 1 |
| `client_version` | string | frontend build id, for debugging |
| `auth_method` | string | `google` \| `self_select` — audit only, never affects the solve |

The number of `choice_*` columns is discovered from the header, not assumed.
If it disagrees with K from `scoring.json`, that is a `ResponseError`.

### 3.2 Row semantics

- One row per **submission**, not per person. Full history is kept.
- Re-submission is allowed; **the latest row per email wins**, ordered by
  `timestamp`, ties broken by later file position. Superseded rows are retained
  in the audit output.
- Choices within a row must be K distinct, non-empty desk ids.

### 3.3 Roster vs. submission conflicts

The roster is stale by design (the coordinator says so). Resolution:

- `candidacy`: **submission wins**, and the conflict is recorded in
  `results.json:roster_conflicts` and printed as a warning. (`year` is no longer
  collected by the form; when an older file carries it, it is recorded but never
  affects eligibility, so it cannot produce a meaningful conflict.) The coordinator sees
  every one of them and can fix the roster and re-run.
- Membership: an email not in the roster is an **error**, not a warning. Someone
  outside the department must not be able to enter the pool.
- A roster member with no submission is a **warning**; they are excluded from the
  pool (they did not participate) and listed in the report.

### 3.4 Desk pool

```
pool_desks = all desks
           - desks held by roster members with keeps_desk truthy
           - desks listed in config/rooms.json with "available": false (optional key)
pool_people = roster members with keeps_desk falsy AND a valid submission
```

A submission ranking a desk that is not in `pool_desks` is a **warning**, and
that choice is dropped (the person keeps their other choices). If dropping leaves
them with zero valid choices, that is an error naming the person.

### 3.5 The pre-lock claim log — optional, Component A only

The frontend has an optional step (Apps Script property `PRELOCK_ENABLED`,
default `false`) on which a student claims the desk they already occupy and
keeps it instead of ranking. It writes a second append-only sheet with its own
CSV shape:

| column | type | notes |
|---|---|---|
| `claim_id` | string | unique per row |
| `timestamp` | ISO-8601 with UTC offset | as §3.1 |
| `email` | string | lower-cased on ingest; joins to the roster |
| `name` | string | as submitted |
| `desk_id` | desk id | must exist in `rooms.json` |
| `keeping` | bool | `yes`/`no` vocabulary of §2.3 |
| `client_version` | string | audit only |

**Row semantics are §3.2's, unchanged**: one row per action, releasing a desk
appends `keeping=no` rather than deleting, and the latest row per email wins
(timestamp, ties by later file position).

The solver never reads this file. It is folded into `config/roster.csv` by
`tools/merge_keepers.py`, which sets `keeps_desk`/`current_desk` and so feeds the
existing §3.4 keeper mechanism — there is no second concept of "out of the pool".
The merge refuses to write anything if two people are keeping the same desk, a
claimer is not on the roster, or a desk id is unknown.

A claim is deliberately **not** checked against `eligibility.json`. The rule
table governs where a person may be *assigned*; remaining where they already sit
is not an assignment, and §3.4 has never zone-checked `current_desk` either.

---

## 4. Python package structure

```
deskmatch/
  types.py        # frozen dataclasses; the in-memory contract. No logic.
  errors.py       # ConfigError, ResponseError, InfeasibleError + friends
  validate.py     # all schema validation; collects errors, never raises raw
  config.py       # load + validate config dir -> Config
  responses.py    # load CSV/JSON -> Responses; dedup; hashing
  eligibility.py  # rule-table evaluation -> allowed zone set per person
  scoring.py      # curve -> exact integer points; jitter epsilon bound
  problem.py      # Config + Responses -> Problem (matrix, masks, index maps)
  solve.py        # Problem -> Solution | Infeasibility.  Pluggable backends.
  matching.py     # bipartite feasibility, max matching, König cover, Hall sets
  diagnostics.py  # blocking sets, K_min, round-2 export, pre-deadline check
  baselines.py    # random serial dictatorship Monte-Carlo
  synth.py        # synthetic roster + preference generator
  report.py       # matplotlib PDF (public + coordinator)
  provenance.py   # hashes, versions, seed, reproduction command
  cli.py          # argparse entry points
```

### 4.1 Key types (see `types.py` for the source of truth)

```python
Config(rooms, eligibility, roster, scoring, source_dir, hashes)
Problem(people: tuple[PersonId], desks: tuple[DeskId],
        allowed: BoolMatrix,          # (n_people, n_desks) eligibility ∧ top-K
        points: IntMatrix,            # exact integer points; 0 where not allowed
        rank_of: dict[(person, desk)] -> int)
Solution(assignment: Mapping[PersonId, DeskId], total_points: int,
         rank_histogram, seed, curve_name, provenance)
Infeasibility(max_satisfiable: int, deficiency: int,
              blocking_sets: list[BlockingSet], k_min_submitted: int | None,
              k_min_extended: int, unmatched_candidates: frozenset)
```

### 4.2 Solver backend interface

```python
class AssignmentBackend(Protocol):
    name: str
    def solve(self, points: np.ndarray, allowed: np.ndarray) -> np.ndarray | None:
        """Return array of column indices per row (or None if no perfect
        matching on rows exists). Must maximise sum of points over allowed cells."""
```

`ScipyJVBackend` is the default. `PuLPBackend` is the documented escape hatch for
side constraints. Swapping is a config/CLI flag, not a rewrite.

---

## 5. The algorithm

### 5.1 Objective

Maximise `Σ points[person, assigned_desk]` over all injective assignments of
people to desks, restricted to `allowed` cells.

`allowed[p, d]` ⟺ `d` is in `p`'s submitted top-K **and** `d`'s zone is permitted
for `p` **and** `d ∈ pool_desks`.

Because `allowed` already encodes the top-K restriction, **I4 is structural**:
there is no cell in the matrix that violates it. It is still asserted afterwards.

### 5.2 Feasibility comes first

1. Build the bipartite graph from `allowed`.
2. Compute a maximum matching (Hopcroft–Karp, `scipy.sparse.csgraph`).
3. If `|matching| < n_people` ⇒ **infeasible**. Do not solve. Go to §6.
4. Otherwise solve the assignment problem.

This ordering exists so that infeasibility is *detected*, never *approximated*.

### 5.3 Exact integer points

Curve values may be decimals. They are converted with `fractions.Fraction`,
scaled by the LCM of denominators, and asserted to be exactly integral.
Consequence: **the minimum gap between two distinct achievable totals is exactly 1.**
This is what makes the jitter bound provable rather than hand-waved.

### 5.4 Tie-breaking

Multiple assignments may achieve the same optimal total. Lexicographic-by-name is
rejected: it disadvantages the same people every year, forever.

Given `seed_string` from config:

```
seed_int = int.from_bytes(sha256(seed_string.encode("utf-8")).digest()[:8], "big")
rng      = numpy.random.default_rng(seed_int)
```

SHA-256 and NumPy's PCG64 stream are both stability-guaranteed, so this is stable
across platforms and NumPy versions.

Two independent mechanisms, both seeded:

**(a) Permutation.** Rows and columns are randomly permuted before the solve and
un-permuted after. Solver output can depend on input order; this makes that
dependence a published, seeded choice rather than an artefact of roster
alphabetisation.

**(b) Jitter.** A deterministic perturbation `J[p, d] ~ Uniform[0, ε)` is added
to the integer points.

**The epsilon bound.** With `n = n_people` assigned cells and integer points, any
two distinct achievable totals differ by ≥ 1. The total jitter on any assignment
is in `[0, n·ε)`. If `n·ε < 1`, then for assignments with totals `A > B`
(so `A ≥ B + 1`):

```
A + jitter(A) ≥ A > B + n·ε > B + jitter(B)
```

so the jittered order never disagrees with the true order — jitter can only ever
select *among* exact ties. We use

```
ε = 1 / (2 · (n_people + 1))        ⇒  n·ε ≤ n / (2n + 2) < 1/2 < 1
```

with a factor-of-2 margin. `scoring.assert_jitter_bound()` recomputes `n·ε < 1`
against the actual matrix at runtime and raises if violated. Additionally,
`solve()` asserts that the un-jittered total of the returned assignment equals
the un-jittered optimum reported by a jitter-free solve. **I6.**

**(c) Forbidden cells.** Not `inf`. `M = -(n_people · max_point + 1)`, which is
strictly worse than any complete assignment on allowed cells. After solving,
every returned cell is asserted to be `allowed`. Combined with the §5.2
feasibility pre-check, a forbidden cell can never appear in output.

### 5.5 Determinism checklist

- No `set` iteration order in any output path — sorted tuples only.
- No `dict` ordering assumptions beyond insertion order (which is guaranteed).
- No `hash()` of strings (PYTHONHASHSEED-dependent). `hashlib` only.
- No wall-clock in the solve path. Timestamps live only in provenance and are
  excluded from the canonical hash.
- `results.json` is written with `sort_keys=True`, fixed separators, `\n` endings,
  and floats formatted via `repr` at fixed precision.

---

## 6. Infeasibility handling

Triggered when `|max matching| < n_people`. The run **fails** (exit code 2) and
emits a diagnostic report. It never emits an assignment.

### 6.1 What is computed

**Max satisfiable.** `|max matching|` — the largest number of people who can
simultaneously get a top-K desk.

**Deficiency.** `n_people - |max matching|` — how many people cannot.

**Blocking sets (Hall's-condition violators).** By König's theorem: from a maximum
matching, let `Z` be all vertices reachable by alternating paths from unmatched
people. `S = people ∩ Z` satisfies `|N(S)| < |S|`.

`S` is split into **connected deficiency components**, and each component yields
*two* statements:

1. the **full** over-subscribed group (`minimal = False`), whose shortfall is
   that component's true deficiency — the number the coordinator acts on;
2. a **minimal** violator inside it (`minimal = True`), produced by greedy
   removal in a seeded order — the tightest true claim.

Both are needed, and reporting only one is a real failure mode in each direction:

- *Minimal only understates.* Nine people all ranking the same five desks yields
  "some six of you are short by one", so the coordinator asks one person to
  re-rank when four must.
- *A single global union overstates.* Two **independent** over-subscribed groups
  merged into one statement sends twice as many students back to re-rank as
  necessary.

Hence: per component, both numbers. When a component is already minimal, only one
statement is emitted. Minimalisation is a repeated sweep, not one pass — dropping
member A can become valid only after B is removed (`matching.minimal_violator`).

Output, per blocking set:
```
9 people can only reach 5 desks between them (short by 4)
  [FULL over-subscribed group -- this is the one to act on]:
    people: Ada Lovelace, Vera Rubin, ... (9)
    desks:  D01 (1), D02 (2), D03 (3), D04 (4), D05 (5)
6 people can only reach 5 desks between them (short by 1)
  [smallest group that is still over-subscribed]:
    people: ... (6)
```

**K_min (submitted).** The smallest `K' ≤ K` for which a complete assignment exists
using only each person's top-`K'`. Monotone, so if `K` fails this is `None`; it is
reported to show whether the run *would* have worked at a tighter K in easier years.

**K_min (extended)** — clearly labelled as hypothetical. To answer "how close were
we", preferences are extended past rank K using a seeded deterministic ordering of
each person's remaining *eligible* desks, and the smallest `K'` making the problem
feasible is reported. This is a diagnostic only; **it never produces an assignment**,
because the extension is invented, not chosen by the student.

### 6.2 Round-2 input file

`round2_input.json` + `round2_roster.csv` containing:

- the people in the union of the minimal blocking sets (these are the people who
  actually need to re-rank), plus anyone unmatched in the reference max matching;
- for each, the desks genuinely still available to them: eligible desks minus
  desks locked by keepers minus desks that a **forced** assignment already fixes;
- their existing ranking, so the form can pre-fill it;
- a note of how many additional ranks are needed to clear the deficiency.

Everyone else's assignment is *not* finalised (that would leak information and
create a strategic incentive); the round-2 file only scopes who needs to act.

### 6.3 Pre-deadline feasibility check

`deskmatch check --responses partial.csv` runs the same analysis on a partial
response set, treating non-responders as unconstrained (they can take any eligible
desk) and reporting whether the *current* responders are already over-subscribed.
Designed to be run daily while the form is open. Exit code 0 = fine, 3 = would
currently fail.

---

## 7. Outputs

| file | audience | notes |
|---|---|---|
| `results.json` | everyone | canonical; the reproducibility target |
| `results_public.pdf` | department | default output |
| `results_coordinator.pdf` | coordinator | requires `--full`; adds per-person preferences |
| `assignments.csv` | everyone | name, email, desk, rank received |
| `responses_anonymized.csv` | everyone | for public re-running; see §7.2 |
| `diagnostics.json` | coordinator | only on infeasible runs |
| `round2_*.{json,csv}` | coordinator | only on infeasible runs |

### 7.1 Provenance block (in `results.json` and on the last page of both PDFs)

```jsonc
{
  "seed_string": "...", "seed_int": 1234567890,
  "curve": "linear_borda", "curve_values": [5,4,3,2,1], "K": 5,
  "responses_sha256": "...", "responses_row_count": 47,
  "config_sha256": { "rooms.json": "...", "eligibility.json": "...",
                     "roster.csv": "...", "scoring.json": "..." },
  "canonical_sha256": "...",          // hash of results.json minus this field
  "deskmatch_version": "...", "python": "3.12.2",
  "numpy": "...", "scipy": "...",
  "reproduce": "deskmatch solve --config config/ --responses responses.csv --verify <hash>"
}
```

### 7.2 Privacy

- **Public** report: aggregate figures only. Per-person *rankings* are never
  attributed. Final *assignments* ARE public — people need to know where they sit.
- **Coordinator** report (`--full`): adds the full preference table, per-person
  rank received, and the roster-conflict list.
- `responses_anonymized.csv` replaces `email`/`name` with a salted pseudonym
  (`sha256(email + seed)[:8]`), preserving the rows needed to reproduce the *shape*
  of the solve. Note honestly in the README: the anonymised file reproduces the
  assignment *structure* and every aggregate figure, but pseudonym→desk mapping is
  deliberately not invertible, so the public re-run verifies the algorithm, not the
  name-level output. Name-level verification is via `assignments.csv` + the
  coordinator publishing `responses_sha256`.

---

## 8. Integrity model

What this system does and does not defend against, stated plainly because the
coordinator is also a participant.

**Defended:** post-hoc tampering with the solve. The solver has no override path;
the seed is committed publicly before collection; the response file is hashed and
the hash is printed in the report; results are a pure function of published inputs.
Anyone can re-run and get the same answer.

**Defended:** cherry-picking a favourable seed. The seed is announced on Discord
*before the form opens*, and `seed_committed_at` is recorded. Re-running with a
different seed produces a different `results.json` hash, visibly.

**Not defended, by design:** the coordinator editing the response CSV or roster
before running. This is deliberate — it is *visible in git*, which is the actual
control. The runbook requires committing the raw export before solving. If you
want more than that, have a second person hold a copy of the export and compare
hashes; the report prints the hash for exactly this purpose.

**Not defended:** the login fallback. Self-select is a convenience, not a security
boundary, per explicit instruction. `auth_method` is recorded so it is at least
visible.

---

## 9. Exit codes

| code | meaning |
|---|---|
| 0 | success |
| 1 | usage / IO error |
| 2 | infeasible at K (diagnostics written) |
| 3 | `check` says the current partial responses would fail |
| 4 | config or response validation failed |
| 5 | verification mismatch (`--verify`) |
