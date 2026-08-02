"""deskmatch -- preference-matching desk assignment.

Component B of the two-part system described in docs/SPEC.md. Reads collected
rankings (a CSV; no Google dependency), solves the assignment exactly under a
hard top-K guarantee, and produces the results PDF.

The three properties this package is built around:

  * Reproducible -- same (responses, config, seed) always gives the same answer,
    byte for byte, on any machine.
  * Auditable    -- the solver is a pure function with no override path, inputs
                    are hashed into the output, and anyone can re-run it.
  * Honest about failure -- if the top-K guarantee cannot be met, the run fails
                    loudly and names exactly who is blocking whom, rather than
                    quietly producing a worse answer.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
