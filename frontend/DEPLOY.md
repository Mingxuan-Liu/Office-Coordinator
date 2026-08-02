# Deploying the form (Component A)

For someone who has never used Google Apps Script. Follow it top to bottom; it
takes about twenty minutes the first time and five minutes in later years.

You need: a UMich Google account. Nothing else — no cloud project, no credit
card, no command line beyond one Python script.

---

## What you are building

A web page, hosted by Google, that shows students the floor plan and records
their ranked desk choices into a Google Sheet you own. That Sheet is the only
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
| `ConfigData.gs` | Desks, zones, roster, floor plan image | **no — generated** |
| `appsscript.json` | Project manifest (timezone, scopes, deployment) | rarely |
| `Index.html` | Page shell; pulls in everything else | yes |
| `Style.html` | All CSS | yes |
| `JsCore.html` | App state machine, server bridge | yes |
| `ViewLogin/Explainer/Select/Confirm.html` | The four screens | yes |
| `JsMap.html` | Floor plan renderer | yes |
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

If it warns that the floor plan image is missing, fix that first: put the plan
PNG at the path `config/rooms.json` names (see `config/floorplans/README.md`) and
re-run. The solver survives a missing image; students being asked to rank desks
with no plan to look at do not.

Commit the result.

## Step 2 — Preview it locally before touching Google

```bash
python tools/build_local_preview.py
open frontend/_preview.html
```

This expands the Apps Script includes exactly the way Google does and wires in a
fake server, so you can click the whole flow — login, explainer, desk selection,
confirmation — with no deployment. Do this first every year. It catches most
mistakes in thirty seconds.

## Step 3 — Create the Sheet

1. Go to <https://sheets.new>. Name it something like `Desk selection 2026`.
2. Rename the first tab to `Responses`.
3. Leave it empty. The script writes the header row itself, with the right
   number of `choice_N` columns for whatever K your scoring curve uses.

Do **not** share this Sheet with students. It is the raw response log.

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

- your name and year appear, and correcting the year updates the zones you are
  offered;
- desks kept by other people are hatched and unclickable;
- if you are a pre-candidate, upper-years desks are visibly not available to you;
- submitting adds exactly one row to the `Responses` tab;
- submitting a second time adds a **second** row and does not overwrite the
  first — history is append-only, and the solver takes the latest.

---

## While the form is open

Download the Sheet as CSV (**File → Download → CSV**) and run the pre-deadline
check every day or two:

```bash
python -m deskmatch check --config config/ --responses partial.csv --list-outstanding
```

Exit code 3 means the responses so far already cannot all be satisfied, and it
names who is colliding on what. Talk to those people *before* the deadline.

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

**The page is fine but the map is blank with a warning.** The floor plan image is
missing from `ConfigData.gs`. See step 1.
