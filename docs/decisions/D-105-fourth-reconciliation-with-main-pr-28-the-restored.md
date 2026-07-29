# D-105 — Fourth reconciliation with `main` (PR #28): the restored tree meets the xTB layer

**Context.** `main` landed the restore of the tree the Replit move rewound (D-091) while this
branch was building the xTB capability layer. The branch was based on the *rewound* tree, so the
merge is not two feature sets meeting — it is a feature set meeting ~38 modules it had never seen.
Five files conflicted. Two were mechanical; three were not, and each of the three was a place where
the two designs disagreed about the same thing rather than merely touching the same lines.

**The ADR numbers collided again, exactly as D-088 describes.** Both sides independently allocated
D-082…D-091. `main`'s allocation keeps the numbers — it is the trunk, it merged first, and its ids
are already cited from `BACKLOG.md`, `DEFERRED.md` and several modules. This branch's ten xTB ADRs
renumber to **D-095…D-104**, and every citation moved with them: `BACKLOG.md` (the X-entries only —
`main`'s DA-5/DA-10/TOOL-6 rows keep theirs), `tasks/todo.md`, `tasks/lessons.md`,
`calc/xtb_spec.py`, `agents/calc_tools.py`, `workflows/README.md`, `workers/README.md`, and the
three xTB design docs. `tests/test_decision_log.py`, which `main` added *as the fix for the last
collision*, is what makes this checkable rather than reviewable — and it passes.

### `_log_prediction` follows the calculators it hooks

`main` added a prediction ledger (`calc/calibration.py`, D-090's gap IDEA-2) and hooked it into
`predict_pka` and `predict_solubility` in `agents/calc_tools.py` — deliberately at the *tool* layer,
"the boundary where a prediction becomes advice a chemist acts on". X8 (D-103) had moved both of
those calculators to `mcp_servers/calc`. So the hook's stated principle and its location had come
apart.

Resolved by moving the hook, not by weakening either side: the MCP server's tool functions *are*
the tool layer now, so `_log_prediction` lives there and hooks the same two calculators at the same
boundary. It needs no ambient identity — the ledger is keyed on the canonical SMILES, not on who
asked — so it crosses the D-103 line cleanly, which is the test that boundary was written to pass.
`report_measurement` and `calculator_trust` stay in-process: they record and score, they do not
compute, and nothing about them is a calculator.

`default_store()` keeps X8's home in `calc/postgres_store.py` rather than `agents/calc_tools.py`,
because the MCP server needs it too and a tool module is the wrong place for the one naming of the
production backend.

### The registry absorbed four workflows rather than being replaced by them

`workers/background_worker.py` was the sharpest conflict: this branch reads what it serves from the
registry (D-099), `main` restored the hand-maintained lists and *added four modules to them* —
`audit_verify`, `digest`, `note_index`, `retention`. Taking the registry naively would have dropped
four workflows and six activities on the floor, silently, which is the exact failure mode the
registry exists to prevent.

So the four modules were decorated at their definition sites, which is what D-099 says adding a
durable capability means. Then the resolution was *verified rather than asserted*: the registry's
served sets were diffed against `main`'s explicit lists, and they are equal — fourteen workflows and
twenty-four activities, nothing missing and nothing extra. A merge that claims to preserve a
capability list should prove it against the list it replaced.

**One thing the merge caught that the branch had missed.** `mcp_servers/calc/server.py`'s
`predict_pka` docstring still described the tool as O-H/S-H only — stale since D-104 added
aromatic-nitrogen bases and the aliphatic refusal. The agent reads that docstring, so it was the
one place the X11 result had not actually shipped. Corrected here.
