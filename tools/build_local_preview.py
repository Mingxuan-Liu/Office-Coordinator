#!/usr/bin/env python3
"""Expand the Apps Script frontend into one standalone HTML file you can open
locally with file:// — no Google account, no deployment, no build step.

Why this exists
---------------
`frontend/Index.html` cannot be opened directly in a browser: it contains
Apps Script scriptlets (`<?!= include('JsCore') ?>`) that only the HTML Service
expands, server-side. Without them the page is an empty shell.

The obvious workaround is to keep a hand-written standalone copy of the page for
local testing. That copy then drifts from the real one within a week and starts
lying to you. So instead this script performs exactly the expansion Apps Script
performs, from the real sources, every time you run it. There is one frontend,
and this is a view of it.

It also injects `mock_server.html`, which stands in for `google.script.run` so
the whole flow (login → explainer → select → confirm) is clickable offline.

Usage
-----
    python tools/build_local_preview.py
    open frontend/_preview.html          # macOS
    xdg-open frontend/_preview.html      # Linux

The output is a build artefact. It is git-ignored; never edit it, and never
paste it into Apps Script — edit the real partials instead.

This is also a cheap correctness check: Apps Script concatenates every partial
into ONE document, so a top-level `const APP` declared in two partials is a hard
runtime error that shows up as a blank page with a console error nobody sees.
`--check` catches that here instead.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FRONTEND = REPO / "frontend"

#: Apps Script's include() call, exactly as it appears in the sources.
INCLUDE_RE = re.compile(r"<\?!=\s*include\(\s*'([^']+)'\s*\)\s*\?>")
#: Any other scriptlet. Server-side templating we cannot evaluate here.
SCRIPTLET_RE = re.compile(r"<\?!?=?[^?]*?\?>")
SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
TOP_DECL_RE = re.compile(r"^(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)", re.M)


def expand(entry: Path, *, seen: tuple[str, ...] = ()) -> tuple[str, list[str]]:
    """Recursively expand include() scriptlets. Returns (html, partial names)."""
    html = entry.read_text(encoding="utf-8")
    used: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in seen:
            raise SystemExit(
                f"include cycle: {' -> '.join([*seen, name])}\n"
                f"  Apps Script would also loop here."
            )
        path = FRONTEND / f"{name}.html"
        if not path.exists():
            raise SystemExit(
                f"{entry.name} includes '{name}', but {path.relative_to(REPO)} "
                f"does not exist.\n"
                f"  Apps Script would render an empty string and the page would "
                f"break with no error. Create the file or fix the include."
            )
        inner, inner_used = expand(path, seen=(*seen, name))
        used.append(name)
        used.extend(inner_used)
        return inner

    return INCLUDE_RE.sub(replace, html), used


def combined_script(html: str) -> str:
    """All inline JS, concatenated the way the browser will see it."""
    stripped = SCRIPTLET_RE.sub("null", html)
    stripped = COMMENT_RE.sub("", stripped)
    return "\n;\n".join(SCRIPT_RE.findall(stripped))


def check_duplicate_globals(js: str) -> list[str]:
    """Top-level names declared more than once across the concatenated partials.

    A duplicated `const`/`let`/`class` is a fatal SyntaxError for the whole
    document; a duplicated `function`/`var` silently shadows, which is worse
    because the page half-works. Both are reported.
    """
    counts: dict[str, int] = {}
    for match in TOP_DECL_RE.finditer(js):
        counts[match.group(1)] = counts.get(match.group(1), 0) + 1
    return sorted(name for name, n in counts.items() if n > 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--entry", default=str(FRONTEND / "Index.html"))
    parser.add_argument("--out", default=str(FRONTEND / "_preview.html"))
    parser.add_argument(
        "--no-mock", action="store_true",
        help="omit mock_server.html (the page will need a real Apps Script backend)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="validate only; write nothing. Exit 1 on a problem.",
    )
    args = parser.parse_args(argv)

    entry = Path(args.entry)
    if not entry.exists():
        print(f"no such entry file: {entry}", file=sys.stderr)
        return 1

    html, used = expand(entry)
    js = combined_script(html)

    print(f"expanded {len(used)} partial(s): {', '.join(used)}")
    print(f"combined inline JS: {len(js):,} characters")

    problems: list[str] = []
    dups = check_duplicate_globals(js)
    if dups:
        problems.append(
            "top-level names declared in more than one partial: "
            + ", ".join(dups)
            + "\n  Apps Script concatenates every partial into one document, so a "
              "duplicated\n  const/let/class is a fatal SyntaxError and the page "
              "renders blank."
        )

    leftover = SCRIPTLET_RE.findall(html)
    if leftover:
        # Not fatal: some scriptlets are legitimately server-side only.
        print(
            f"note: {len(leftover)} non-include scriptlet(s) left unevaluated "
            f"(they only resolve inside Apps Script)"
        )

    if problems:
        for problem in problems:
            print(f"\nPROBLEM: {problem}", file=sys.stderr)
        return 1
    print("no duplicate top-level declarations")

    if args.check:
        print("check passed; nothing written")
        return 0

    if not args.no_mock:
        mock = FRONTEND / "mock_server.html"
        if mock.exists():
            html = html.replace("</body>", mock.read_text(encoding="utf-8") + "\n</body>")
            print("injected mock_server.html (offline stand-in for google.script.run)")
        else:
            print("warning: mock_server.html not found; the page will not have a "
                  "backend", file=sys.stderr)

    banner = (
        "<!-- GENERATED by tools/build_local_preview.py from the real Apps Script\n"
        "     partials. Do not edit, and do not paste into Apps Script.\n"
        "     Regenerate with: python tools/build_local_preview.py -->\n"
    )
    out = Path(args.out)
    out.write_text(banner + html, encoding="utf-8")
    print(f"\nwrote {out}")
    print(f"open it with:  open {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
