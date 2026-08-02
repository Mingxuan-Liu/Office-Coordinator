# Deploying the form (Component A)

For someone who has never used Google Apps Script. Follow it top to bottom; it
takes about twenty minutes the first time and five minutes in later years.

You need: a UMich Google account. Nothing else — no cloud project, no credit
card, no command line beyond one Python script.

---

## What you are building

A web page, hosted by Google, that shows students a plan of the desks and
records their ranked choices into a Google Sheet you own. That Sheet is the only
thing the Python solver needs.

Two properties make this work without any infrastructure:

- **"Execute as: Me"** means the script runs with *your* permissions, so it can
  write to your Sheet without students needing access to it.
- **"Anyone within University of Michigan"** means Google authenticates the
  student for you, and `Session.getActiveUser().getEmail()` returns their real
  UMich address. That is where the identity comes from — you are not building a
  login.

---

## Files

| file | what it is | hand-edit? |
|---|---|---|
| `Code.gs` | Server: serves the page, validates, writes to the Sheet | yes |
| `ConfigData.gs` | Desks, zones, roster | **no — generated** |
| `appsscript.json` | Project manifest (timezone, scopes, deployment) | rarely |
| `Index.html` | Page shell; pulls in everything else | yes |
| `Style.html` | All CSS | yes |
| `JsCore.html` | App state machine, server bridge | yes |
| `ViewLogin/Explainer/Prelock/Select/Confirm.html` | The five screens | yes |
| `JsMap.html` | Floor plan renderer (used by both Pre-lock and Choose) | yes |
| `JsExplainer.html` | The interactive explainer figures | yes |
| `mock_server.html` | Offline stand-in for the server. **Dev only** | yes |
| `_preview.html` | Build artefact. Never edit, never paste into Apps Script | no |

`ConfigData.gs` is generated from `config/` so that desk geometry has exactly one
definition, living in git. Never edit it by hand — your changes will be silently
overwritten the next time anyone runs the sync script.

---

## Step 1 — Generate `ConfigData.gs`

From the repo root:

```bash
python tools/sync_config.py --config-dir config/ --out frontend/ConfigData.gs
```

No floor-plan bitmap is involved. `config/rooms.json` is a schematic — the desk
rectangles are the map, and their spacing carries the layout (narrow gap = two
columns facing each other, wide gap = an aisle, widest = the wall between the two
sides). That is why the generated file is about 20 KB rather than a megabyte.

If the script warns that a room *declares* an `image`, that is a leftover key:
the form cannot draw one. Remove the key, or leave it if you want it for the
validator's sake — either way nothing is embedded.

Commit the result.

## Step 2 — Preview it locally before touching Google

```bash
python tools/build_local_preview.py
open frontend/_preview.html
```

This expands the Apps Script includes exactly the way Google does and wires in a
fake server, so you can click the whole flow — login, explainer, pre-lock, desk
selection, confirmation — with no deployment. Do this first every year. It
catches most mistakes in thirty seconds.

The preview's fake server takes switches on the URL. The ones worth knowing:

```
?mockPrelock=on          turn the Pre-lock step on (it is off by default,
                         same as the real script property)
?mockClaims=3            pretend three other people already claimed a desk
?mockPerson=none         see the self-select login instead of Google
?mockDeadline=past       see what a closed form looks like
```

Open the console once — the mock prints the full list.

## Step 3 — Create the Sheet

1. Go to <https://sheets.new>. Name it something like `Desk selection 2026`.
2. Rename the first tab to `Responses`.
3. Leave it empty. The script writes the header row itself, with the right
   number of `choice_N` columns for whatever K your scoring curve uses.

You do not need to create the `Keepers` tab. If you turn the Pre-lock step on
(step 5b), the script adds it the first time somebody claims a desk.

Do **not** share this Sheet with students. It is the raw response log.

> **Re-using last year's spreadsheet?** The response header changed: `year` is
> gone, because the form no longer asks for it. The script refuses to append to
> a tab whose header does not match, which is deliberate — it will not quietly
> write rows the solver would misread. Start a new tab (or clear the old one)
> rather than editing the header by hand.

## Step 4 — Create the script project

From the Sheet: **Extensions → Apps Script**. This creates a script bound to the
Sheet, which is what you want.

Then, in the editor:

1. Click the gear (**Project Settings**) and tick
   **"Show `appsscript.json` manifest file in editor"**. You need this.
2. Go back to **Editor**. For each file in the table above (except
   `_preview.html`), create it and paste the contents:
   - `.gs` files: **＋ → Script**, name it *without* the extension (`Code`,
     `ConfigData`).
   - `.html` files: **＋ → HTML**, name it *without* the extension (`Index`,
     `Style`, `JsCore`, `ViewLogin`, …).

   The names must match exactly — `Index.html` includes the others by name, and
   a typo produces a blank page with no error.
3. Delete the default `Code.gs` stub content before pasting.
4. Open `appsscript.json` and replace it with the one from this folder.

> **Faster alternative — `clasp`.** If you would rather not paste eleven files:
> ```bash
> npm install -g @google/clasp
> clasp login
> cd frontend && clasp clone <SCRIPT_ID>   # id is in Project Settings
> clasp push
> ```
> `clasp push` will refuse to upload `appsscript.json` unless you ticked the
> manifest box in step 1. Add `_preview.html` to `.claspignore`.

## Step 5 — Point the script at the Sheet

**Project Settings → Script Properties → Add script property**:

| property | value |
|---|---|
| `RESPONSE_SHEET_NAME` | `Responses` |

If you skip this the script fails with a message telling you to set it, rather
than writing to the wrong tab.

Set a deadline the same way if you want the form to close itself:

| property | value |
|---|---|
| `DEADLINE_ISO` | `2026-09-22T17:00:00-04:00` |

## Step 5b — Decide about the Pre-lock step

**Optional, and off unless you turn it on.**

The Pre-lock step sits between "How it works" and "Choose". On it, a student who
is happy where they are can claim the desk they already sit at and keep it,
instead of ranking anything. Claiming takes **both** that person and that desk
out of the draw, which is the same thing `keeps_desk` in the roster has always
meant — the difference is that the students tell you, rather than you asking
them one at a time.

| property | value | effect |
|---|---|---|
| `PRELOCK_ENABLED` | `true` | the step is live |
| `PRELOCK_ENABLED` | `false`, or not set | **default.** The chip is greyed out and struck through, cannot be clicked, and has a tooltip saying it is not in use this year. The flow goes straight from the explainer to Choose. Nothing else changes. |
| `PRELOCK_DEADLINE_ISO` | `2026-09-12T17:00:00-04:00` | optional, and usually a few days *before* `DEADLINE_ISO`: you want the keepers settled before everyone else ranks. After it passes, claims and releases are refused. `DEADLINE_ISO` still applies as the outer bound. |
| `KEEPERS_SHEET_NAME` | `Keepers` | optional. The tab claims are written to. Defaults to `Keepers`. |

What a student sees when it is on:

- the same floor plan as the Choose step, in single-select mode;
- they tap **one** desk — the one they are sitting at;
- the consequences appear next to the desk, naming it: they keep it, they leave
  the ranking, and the desk leaves the pool for everyone else;
- confirming needs a ticked box *and* a button that names the desk;
- desks other people have already claimed are hatched and unavailable, exactly
  as roster keepers' desks are;
- if they have already claimed one, they see it and can **release** it, which
  puts them and the desk straight back into the draw.

Zones are deliberately **not** enforced here. The eligibility rules say where
somebody may be *assigned*; staying at the desk you already occupy is not an
assignment. A student keeping a desk outside their zone gets a note saying so,
and you see the claim in the tab.

### The `Keepers` tab

Created on the first claim. Append-only, like `Responses` — releasing writes a
new row with `keeping=no` rather than deleting anything, and the **latest row
per email wins**, same rule as responses.

```
claim_id, timestamp, email, name, desk_id, keeping, client_version
```

Nothing reads this tab automatically. Claims become real when you merge them
into the roster, which is one command — see
[Merging the claims](#merging-the-claims-into-the-roster) below.

**If you turn `PRELOCK_ENABLED` back off** after claims exist, the step
disappears and nobody can make a *new* claim — but claims already made are still
honoured. Those desks stay shown as taken, and anyone holding one still cannot
submit a ranking. The switch means "students may still claim a desk", not
"claims exist".

That is deliberate. The alternative — releasing the claims when the step is
switched off — fails silently and lands on exactly the wrong person: someone who
did what the form told them, was shown "there is nothing else for you to do",
and would then lose their desk without ever being asked.

Merge the claims into the roster anyway, before you close the form. After
merging, `keeps_desk` in `roster.csv` is what holds the desk, which is the
version the solver reads and the version that is visible in git.

## Step 5c — The private note box

Nothing to switch on; it is always there. The confirm page ends with an optional
free-text box — a private note to you — for the things a ranked list cannot say:
accessibility, health, caring responsibilities, or needing distance from a
particular person in order to work.

Notes go to their own tab, never into the response row.

| property | value |
|---|---|
| `ACCOMMODATIONS_SHEET_NAME` | `Accommodations` (optional; this is the default) |

Columns: `note_id, timestamp, email, name, note, client_version`. Append-only,
created the first time somebody writes one, latest row per person wins, and a
later empty note means they deleted theirs.

**Treat this tab as confidential.** It will contain health, caring
responsibilities and conflict between people who share an office, written on the
understanding that only you would read it. Do not paste it into Slack, do not
commit it, and export it separately from the responses. The solver keeps it out
of every published file, but it cannot help you with a copy you made yourself.

At solve time, pass it with `--accommodations`; see the main
[runbook](../README.md#the-runbook) step 7.

## Step 6 — Deploy

**Deploy → New deployment → ⚙ → Web app**, then:

| setting | value | why |
|---|---|---|
| Description | `2026 cycle` | so you can tell deployments apart later |
| Execute as | **Me** | the script writes to *your* Sheet; students never touch it |
| Who has access | **Anyone within University of Michigan** | Google authenticates them, which is where `Session.getActiveUser().getEmail()` comes from |

Getting "Who has access" wrong is the one setting that actually matters:

- **Anyone** → identity comes back empty for everyone, and every student falls
  back to picking their name from a dropdown. Not a disaster, but you lose
  authentication for no reason.
- **Only myself** → students get "You need access". They will all email you.

Click **Deploy**. Google shows an authorization prompt the first time: choose
your UMich account, click **Advanced → Go to (project name)**, then **Allow**.
The "unverified app" warning is expected — it is your own script, running only
for you.

Copy the **Web app URL**. That is what you post.

## Step 7 — Test it as a student

Open the URL in an incognito window, or ask one student to try it early. Check:

- your name and candidacy appear, and changing the candidacy updates the zones
  you are offered (the year is not asked for — candidacy alone decides seating);
- desks kept by other people are hatched and unclickable;
- if you are a pre-candidate, upper-years desks are visibly not available to you;
- submitting adds exactly one row to the `Responses` tab, with the columns in
  [the schema below](#response-schema);
- submitting a second time adds a **second** row and does not overwrite the
  first — history is append-only, and the solver takes the latest.

If you turned the Pre-lock step on, also check:

- the Pre-lock chip is clickable and the map appears on it;
- claiming a desk writes one row to `Keepers` and the student is told plainly
  that they are out of the ranking;
- releasing writes a **second** row with `keeping=no` and puts them back;
- from a different account, that desk now shows as taken.

And if you left it off, check the opposite: the Pre-lock chip is grey, struck
through, does nothing when clicked, and the explainer's "I'm ready" button goes
straight to Choose.

### Response schema

The `Responses` tab, written from K (SPEC §3.1). `year` is **not** a column —
the form stopped asking, because candidacy alone decides which zones a person
may sit in.

| column | what it is |
|---|---|
| `submission_id` | one per row, `Utilities.getUuid()` |
| `timestamp` | ISO-8601 with a UTC offset |
| `email` | lower-cased; joins to the roster |
| `name` | as submitted |
| `candidacy` | as confirmed by the student; overrides the roster |
| `choice_1` … `choice_K` | desk ids, best first. K columns, from your scoring curve |
| `client_version` | frontend build id + config fingerprint |
| `auth_method` | `google` or `self_select`; audit only |

---

## While the form is open

Download the Sheet as CSV (**File → Download → CSV**) and run the pre-deadline
check every day or two:

```bash
python -m deskmatch check --config config/ --responses partial.csv --list-outstanding
```

Exit code 3 means the responses so far already cannot all be satisfied, and it
names who is colliding on what. Talk to those people *before* the deadline.

## Merging the claims into the roster

Only if you turned the Pre-lock step on. Do this **once, after the pre-lock
deadline and before you solve** — the solver reads `keeps_desk` and
`current_desk` from `config/roster.csv`, and until you merge, it does not know
anybody claimed anything.

1. Open the `Keepers` tab, **File → Download → Comma Separated Values**.
2. See what it would do. Always this first:

   ```bash
   python tools/merge_keepers.py --roster config/roster.csv \
                                 --keepers ~/Downloads/keepers.csv --dry-run
   ```

   It prints one line per changed field and writes nothing:

   ```
   Changes:
     Ada Lovelace <alovelace@umich.edu>
         keeps_desk: no -> yes
         current_desk: '' -> D09
     Jocelyn Bell <jbell@umich.edu>
         keeps_desk: yes -> no
         current_desk: D07 -> ''
   ```

3. If that is right, run it again without `--dry-run`. Then commit the roster.

It resolves the latest row per email exactly as the solver resolves responses
(newest timestamp wins, ties broken by later row), preserves every other column
and the row order, and only writes if something actually changed.

It **refuses to write anything at all** — reporting every problem at once — if
two people are keeping the same desk, if somebody claiming is not on the roster,
or if a desk id is not in `rooms.json`. Each of those means the roster you would
get is wrong, and a half-applied merge is worse than none. Fix the cause (add
the person, correct the id, get one of the two to release) and re-export.

People who are not in the keepers file are left alone, so a roster entry you set
by hand survives the merge.

### Watch for out-of-zone keeps

`merge_keepers.py` prints a **warning**, not an error, when somebody keeps a desk
the eligibility rules would never have *assigned* them — a pre-candidate staying
at an upper-years desk, say:

```
merge_keepers: warning: Vera Rubin <vrubin@umich.edu> is keeping D01, which is in
'Upper years side'. The eligibility rules would only ever ASSIGN them to: First
and second years side. ...
```

Keeping the desk you already sit at is not an assignment, so this is allowed and
the merge proceeds. But the zone exists because that cohort is meant to sit
together, and pre-lock is the change that stops you hearing about each of these
in person — so this line is the only place it surfaces. Read it, then decide.

## Closing the form

**Deploy → Manage deployments → ⋮ → Archive.** Students then get a page saying
the deployment is not available.

Then **File → Download → CSV**, commit the raw file, and continue from step 6 of
the main [runbook](../README.md#the-runbook).

---

## Troubleshooting

**Blank page.** Almost always a missing or misnamed HTML file. Open the browser
console (⌥⌘I / F12). `include` failures show as an error naming the file. Check
the names in step 4 match exactly, with no `.html` in the Apps Script filename.

**"Script function not found: doGet".** The `Code.gs` content did not paste, or
it went into an HTML file. `doGet` must be in a `.gs` file.

**Identity comes back empty.** Either the deployment is set to "Anyone" instead
of the UMich domain, or the student is signed into a personal Google account in
that browser. The form degrades to the name dropdown, records
`auth_method=self_select`, and everything still works — this is a convenience
feature, not a security boundary, so do not spend time on it.

**"You do not have permission to call appendRow".** The deployment is set to
*Execute as: User accessing the web app* instead of *Me*. Redeploy.

**Changes do not appear.** Apps Script serves the last *deployed* version, not
your editor. **Deploy → Manage deployments → ✏️ → Version: New version → Deploy.**
This catches everyone at least once.

**Desks are wrong / missing.** You edited `config/rooms.json` but did not re-run
`tools/sync_config.py`, so `ConfigData.gs` is stale. The test suite catches this
too (`tests/test_frontend_parity.py`).

**"The response sheet header does not match the current config."** Either your
scoring curve changed length mid-round (do not do that), or the tab is from a
cycle that still had a `year` column. Start a fresh tab.

**The Pre-lock chip is grey and I want it live.** `PRELOCK_ENABLED` must be the
string `true`, lower case, and script properties only take effect on the next
page load — no redeploy needed, but do reload.

**A claim did not appear in the roster.** Merging is a manual step, on purpose.
Run `tools/merge_keepers.py` (above). Nothing reads the `Keepers` tab
automatically.

**The map looks like plain rectangles.** That is what it is. There is no floor
plan drawing: the desk shapes and the gaps between them are the map. Desks 1-2,
15-16, 17-18 and 27-28 sit against walls, and a wide gap means an aisle.

**A desk is in the wrong place.** Fix it in `tools/calibrate/index.html`
(Import `config/rooms.json`, correct it, Export), commit, then re-run
`tools/sync_config.py` and redeploy. Opening the tool and exporting without
changing anything produces no diff, so `git diff` shows exactly what you moved.
