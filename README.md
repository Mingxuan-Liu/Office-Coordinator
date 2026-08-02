# Office Coordinator — graduate desk assignment

Preference-matching desk assignment for the Department of Astronomy graduate
office, University of Michigan.

Students rank their top **K** desks (K = 5 by default). A solver finds the
assignment that maximises total satisfaction across everyone at once, subject to
a hard guarantee that **nobody is assigned a desk outside their top K**. It
replaces the previous process, in which everyone opened a Google Doc
simultaneously and raced — which rewarded fast internet rather than preference.

If you are the incoming coordinator and you want to run this without reading the
code, go straight to **[The runbook](#the-runbook)**. It is written for you.

---

## Contents

| path | what it is |
|---|---|
| `config/` | Everything problem-specific. Desks, structures, zones, roster, scoring curve, seed. |
| `solver/` | The Python package (Component B). Runs offline from a CSV. |
| `frontend/` | Google Apps Script web app (Component A) that collects the rankings. |
| `tools/` | Floor-plan calibration tool, the config→frontend sync script, and the keeper-claim merge. |
| `tests/` | Test suite, including adversarial and golden-file tests. |
| `docs/SPEC.md` | The authoritative technical contract. Read before changing code. |

---

## How it works, briefly

**Two components, on purpose.**

*Component A* is a Google Apps Script web app. It is free, needs no cloud account
or credit card, is hosted by Google, and — deployed as *Execute as: Me* /
*Who has access: Anyone within University of Michigan* — gives you the student's
authenticated UMich email for nothing. It collects rankings into a Google Sheet.

*Component B* is a local Python package. It reads the exported CSV, solves the
assignment, and produces the PDF. It has **no Google dependency** and runs
entirely offline, which is what makes it independently auditable.

They talk through a documented CSV schema ([below](#the-data-contract)). Apps
Script collects; Python decides. Apps Script cannot run `scipy`, and the
assignment is deliberately not reimplemented in JavaScript.

**The algorithm.** Each rank is worth points (default 5/4/3/2/1). Build the
matrix of person × desk points, mask out every pair that is not in that person's
top K or is outside the zones they may sit in, and maximise the total with
`scipy.optimize.linear_sum_assignment` (Jonker–Volgenant). The result is a proven
global optimum, not a greedy sequence of local choices. Because the mask is
applied before solving, the top-K guarantee is *structural* — the solver is not
capable of expressing a violation.

**When it can't work.** With ~20 people each naming 5 of ~20 desks, and strongly
correlated preferences, there may be *no* assignment where everyone gets a top-5
desk. This is expected, not exotic. When it happens the run **fails** and tells
you exactly which group of students collectively wants too few desks, by name. It
never quietly hands someone their ninth choice.

---

## Install

```bash
cd solver && python -m pip install -e .
```

Needs Python ≥ 3.11, numpy, scipy, matplotlib. `solver/requirements.lock` records
the exact versions used to produce the published PDFs.

Check it works:

```bash
python -m deskmatch validate --config config/
```

---

## The runbook

Do these in order. Each step says what to check before moving on.

### 1. Update the roster — *the week before, once you know who is in*

Edit `config/roster.csv`:

```csv
name,email,year,candidacy,keeps_desk,current_desk
Ada Lovelace,alovelace@umich.edu,5,candidate,no,
Jocelyn Bell,jbell@umich.edu,6,candidate,yes,D07
```

- `email` is the key. It must match their UMich email exactly.
- `candidacy` is the only field that affects seating. `year` is kept as
  informational metadata; the form no longer asks for it.
- `keeps_desk` = `yes` means they are staying where they are: **they and their
  desk are both removed from the pool.** `current_desk` is then required.
- `candidacy` drives which zones they may sit in. The values you use here must
  match the ones in `config/eligibility.json`.

Stale data is fine — students confirm and correct their own candidacy on the
form, and every correction is reported back to you. The form does not ask about
`year` at all, so a stale year in this file changes nothing unless one of your
own eligibility rules reads it.

```bash
python -m deskmatch validate --config config/
```

Fix anything it complains about before continuing.

### 2. Verify the desk map — *skip in most years*

Only needed if desks moved, a room was added, or you are running this for the
first time.

`config/rooms.json` is a **schematic**, not a tracing of the architect's plan.
No floor-plan image is drawn anywhere, so the rectangles *are* the map and their
spacing has to carry the layout on its own:

| gap | means |
|---|---|
| narrow | two columns facing each other across a divider |
| wide | a walking aisle |
| widest | the wall between the two sides of the main office |
| against the edge | that column faces a wall |

So in the main office, desks 1–2 and 15–16 face walls, 17–18 and 27–28 face
walls, and everything in between faces another desk.

Open `tools/calibrate/index.html` in a browser (double-click it — no server
needed), **Import** `config/rooms.json`, and switch to **Preview**. Check the
groupings match the room. Correct anything wrong, **Export** back over
`config/rooms.json`, and commit.

`config/rooms.json` is the single definition of where desks are. It drives the
student-facing map, the validator, and the heatmap in the report.

> The shipped map puts desks 29–31 on the upper-years side. **Confirm that is
> what the department intends** before your first run.

### 3. The seed — *nothing to do, but know what it is*

`config/scoring.json` ships with:

```json
"seed_year": "auto"
```

The tie-break seed is **the calendar year of the run** — `2026` for the 2026
cycle. It changes every year, and nobody picks it, so it cannot be shopped for a
favourable outcome. That is a stronger guarantee than the old "announce a string
on Discord first", and it is one less thing for you to remember.

The year is resolved **once**, when the config loads, and written into
`results.json`. It is never read from the clock during the solve. That is what
lets someone re-run the 2026 cycle in 2028 and still reproduce your published
hash — `verify` uses the seed recorded in the results file, not today's year.

To deliberately re-run an old cycle, pin it:

```json
"seed_year": 2026
```

There is deliberately **no `--seed` flag** on the solver. Changing the seed means
editing a tracked file.

### 4. Deploy the form

```bash
python tools/sync_config.py --config-dir config/ --out frontend/ConfigData.gs
```

This bakes the desks, zones and roster into the Apps Script project so there is
one source of truth in git. (No images: the map is drawn from the desk
rectangles, which keeps the generated file about 20 KB instead of a megabyte.) Then follow
[`frontend/DEPLOY.md`](frontend/DEPLOY.md) — step by step, assuming you have
never used Apps Script.

Post the web app URL and a deadline.

### 5. While the form is open — *run this every day or two*

Export the Sheet as CSV (File → Download → CSV) and:

```bash
python -m deskmatch check --config config/ --responses partial.csv --list-outstanding
```

This tells you whether the responses so far can actually be satisfied, and if
not, **which specific students are colliding on which desks**. Exit code 3 means
it would currently fail.

Then go talk to those people. They do not have to change their first choice —
they only need to rank further down. Doing this before the deadline is far easier
than after, which is the entire reason the command exists.

### 5b. Merge the desk-keepers — *only if you turned the Pre-lock step on*

The optional Pre-lock step (`PRELOCK_ENABLED`, see
[`frontend/DEPLOY.md`](frontend/DEPLOY.md#step-5b--decide-about-the-pre-lock-step))
lets students claim the desk they already sit at and keep it. Those claims land
in a `Keepers` tab and mean nothing to the solver until they are in the roster.
Export that tab as CSV, then:

```bash
python tools/merge_keepers.py --roster config/roster.csv --keepers keepers.csv --dry-run
python tools/merge_keepers.py --roster config/roster.csv --keepers keepers.csv
git add config/roster.csv && git commit -m "Desk keepers, 2026 cycle"
```

Always the `--dry-run` first: it prints the exact diff and writes nothing. The
real run refuses to write at all if two people are keeping the same desk, if a
claimer is not on the roster, or if a desk id is not in `rooms.json`.

Do this **before** step 7, or the solver will happily assign somebody else's
desk to a stranger.

### 6. Close the form and export

In Apps Script: **Deploy → Manage deployments → Archive**. Then export the Sheet
to CSV.

**Commit the raw export before you run anything.** This is the integrity step: the
report prints the file's SHA-256, and the commit is what lets anyone confirm the
published hash is the file you actually used.

```bash
git add data/responses_2026.csv && git commit -m "Raw responses, 2026 cycle"
```

### 7. Run the solver

```bash
python -m deskmatch solve --config config/ --responses data/responses_2026.csv --out out/
```

**If it succeeds**, you get:

| file | give it to |
|---|---|
| `out/results_public.pdf` | everyone |
| `out/results.json` | everyone — the canonical, hash-verified result |
| `out/assignments.csv` | everyone |
| `out/responses_anonymized.csv` | everyone — lets them re-run it themselves |

Add `--full` to also get `out/results_coordinator.pdf`, which contains everyone's
individual rankings. **That one is for you only.**

**If it fails** with exit code 2, it did not produce an assignment, and it wrote:

- `out/diagnostics.json` — who is blocking whom, with names and desk numbers
- `out/round2_roster.csv` — just the affected students and what is still
  available to them
- `out/diagnostic_report.pdf` — the version you show those students

Go back to step 5's conversation, re-open the form for that subset, and re-run. Do
not hand-edit the assignment; there is no way to do it, and adding one would break
the guarantee the whole process rests on.

### 8. Publish

```bash
python -m deskmatch publish --config config/ --results-dir out/ --out publish/
```

Post `publish/` plus the raw response file's hash. Anyone can now run:

```bash
python -m deskmatch verify --config config/ --responses <the-published-csv> --results results.json
```

and confirm they get the same answer you did.

---

## The data contract

This is the interface between Component A and Component B. Either side can be
replaced as long as this holds.

`responses.csv`, one row per **submission** (not per person — history is kept):

| column | type | notes |
|---|---|---|
| `submission_id` | string | unique per row |
| `timestamp` | ISO-8601 with UTC offset | `2026-09-15T14:03:22-04:00` |
| `email` | string | lower-cased on ingest; joins to the roster |
| `name` | string | roster value wins on conflict |
| `candidacy` | string | as confirmed by the student — **overrides the roster** |
| `choice_1` … `choice_K` | desk id | exactly K columns, contiguous from 1 |
| `client_version` | string | frontend build id, for debugging |
| `auth_method` | `google` \| `self_select` | audit only; never affects the solve |

Rules:

- **Re-submission is allowed.** The latest row per email wins, by timestamp, ties
  broken by later file position. Superseded rows stay in the file.
- There is no `year` column. The form stopped collecting it, because candidacy
  alone decides which zones a person may sit in. A file from an older cycle that
  still carries one is read without complaint and the value is recorded, but it
  never affects eligibility.
- K is discovered by counting `choice_*` columns. It must match the length of the
  scoring curve, or the run stops.
- An email not on the roster is an **error**, not a warning.
- A roster member with no submission is a warning; they are excluded from the pool.
- Ranking a desk that has left the pool drops that one choice with a warning.
  Losing *all* your choices that way is an error naming you.

The Sheet stays private to the coordinator. The anonymised CSV is what gets
published.

---

## Reproducibility, precisely

The solver is a pure function of `(responses, config, seed)`. No clock, no
environment, no override path.

- **`results.json` is byte-identical** given the same inputs, on any machine, any
  OS, any supported Python. This is the canonical artefact and what `verify`
  checks.
- **The PDF is byte-identical under the pinned environment** in
  `solver/requirements.lock`. Matplotlib embeds font subsets, so a different
  matplotlib version can produce a different — equally correct — file. Verify
  against `results.json`, not the PDF.

Determinism is treated as a correctness property: the tie-break RNG is seeded from
SHA-256 of the published seed string (never Python's salted `hash()`), sets are
sorted before iteration anywhere that reaches output, and the test suite asserts
identical output across runs.

### Why you can trust the tie-break

Multiple assignments can tie for the same optimal total. Breaking ties
lexicographically by name was rejected — it would disadvantage the same people
every year, permanently. Instead the published seed drives two things: a random
permutation of row and column order before solving, and a tiny deterministic
jitter added to the score matrix.

The jitter magnitude is provably too small to change the answer. Scores are exact
integers, so two *different* achievable totals differ by at least 1, while the
total jitter on any assignment is bounded by `n·ε < ½`. The jitter can therefore
only ever reorder assignments that are *exactly* tied. This bound is re-derived at
runtime against the actual matrix and the run aborts if it fails; the solver
separately checks that the jittered answer scores the same as a jitter-free solve.
See `docs/SPEC.md` §5.4 for the proof.

---

## What this system does and does not defend against

The coordinator runs this and also competes for a desk, so it is worth being
explicit.

**Defended.** Post-hoc tampering: the solver has no override path and results are a
pure function of published inputs. Seed shopping: the seed is committed publicly
before collection and there is no CLI flag to change it. Swapping inputs after the
fact: the response file's hash is printed on page 1 of the report.

**Not defended, deliberately.** The coordinator editing the response CSV or roster
*before* running. The control for this is that it is visible in git — which is why
step 6 says commit the raw export first. If you want more, have a second person
keep a copy of the export and compare hashes; the report prints the hash for
exactly that purpose.

**Not defended.** The login fallback. If Google identity is unavailable the form
falls back to picking your name from a dropdown, unauthenticated. This is a
convenience for ~35 people who trust each other, not a security boundary. The
`auth_method` column records which path each submission took.

---

## Running the tests

```bash
cd solver && python -m pytest ../tests -v
```

The suite covers: brute-force optimality on small instances, the K-floor and zone
constraints on every successful run, determinism under a fixed seed, outcome
variation under different seeds on tie-heavy input, a golden-file test, and
adversarial cases (everyone ranks the same five desks, more people than desks, a
zone-starved cohort, empty roster, one person, duplicate submissions, stale desk
references).

Synthetic data at any N:

```bash
python -m deskmatch.synth --n 35 --concentration 0.7 --out /tmp/dry-run
```

Doing a full dry run on synthetic data before the real cycle is a good idea and
takes about a minute.

---

## Changing things

Read `docs/SPEC.md` first — it is the contract, and it lists the invariants that
must not be broken.

| you want to | edit |
|---|---|
| add or move desks | `tools/calibrate/index.html` → `config/rooms.json` |
| add a room | same; `rooms.json` holds a list of rooms |
| add a zone | `config/rooms.json` `zones`, then reference it from `eligibility.json` |
| change who may sit where | `config/eligibility.json` — a rule table, not code |
| change the points per rank | `config/scoring.json` |
| change K | change the length of *every* curve in `config/scoring.json` |
| change the seed | `config/scoring.json`, and announce it first |
| let people keep their current desk | set `PRELOCK_ENABLED=true` in Apps Script, then `tools/merge_keepers.py` |

Nothing in that table requires touching Python. If you find yourself editing
Python to change a number, that is a bug — fix the config schema instead.
