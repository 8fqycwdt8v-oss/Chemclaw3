# D-091 — Restoring the tree the Replit restructure rewound

`49cd44c` ("deploy: Replit dev deployment and runtime fixes") moved the Python service to
`services/chemclaw/`, but populated the new location from an **older snapshot** in the same
commit that deleted the top-level tree. The move itself is right; the content it moved was not.

**How this was established, rather than assumed.** `services/chemclaw/service/app.py`'s blob is
byte-identical to that file at `16b63c2` (the PR #23 merge). Comparing every common path against
that commit: **338 of 352 identical, 14 differing**. So the import is a clean snapshot of
`16b63c2`, and everything merged in `16b63c2..2fc903a` — 21 commits, including PRs #24 and #26 —
was silently dropped: 38 Python files, 20 test modules, 8 HTTP routes (`/approvals` ×3,
`/metrics`, `/schedules`, `POST /sessions/{id}/attachments`, `/events/knowledge-merged`), 4 of the
5 incompatible-pair safety rules, and 462 lines of this log.

Restored by overlaying `2fc903a`'s tree onto `services/chemclaw/`, which is content-only and
therefore keeps the new layout. Three things were deliberately **not** reverted:

1. **The six Replit-only additions** — `start.sh`, `start-temporal.sh`,
   `start-background-worker.sh`, `.bin/temporal`, `agents/job_events.py`, and the `knowledge`
   symlink into `services/chemclaw-notes-repo`. The overlay cannot touch what it does not contain,
   except the symlink, which `tar` replaced with a real directory and which was put back by hand.
2. **The `service/runner.py` disconnect fix (ISSUE-B-10).** This is the one genuinely *new* piece
   of work in the 14 differing files, and it is worth keeping: a client vanishing mid-tool-call
   left a `tool_use` block with no matching `tool_result`, which every later turn replayed until
   the model rejected the whole thread — one dropped connection permanently bricking a
   conversation. `runner.py` had moved on by +110/−16 lines since the snapshot, so the fix was
   hand-merged rather than patched, and is now **pinned by a test** that was confirmed to fail
   when the rollback is removed. The original arrived without one.
3. **The Replit deployment surface outside `services/chemclaw/`** — untouched.

### CI was collateral damage, and is restored at the root.

GitHub Actions only reads workflows from the repository root. The restructure moved `ci.yml` to
`services/chemclaw/.github/workflows/`, where nothing runs it, so `main` has had **no CI at all**
since — the green checks on PR #28 came from the PR branch's own root workflow, not from `main`'s.
A root `ci.yml` now runs the same gate with `working-directory: services/chemclaw`. It drops the
Helm/kubeconform steps (the restructure's Makefile removed the target, and the chart is not part
of the Replit deployment) and `make eval`, whose case-set has three gated failures that predate
all of this — a gate that is red on arrival trains people to ignore it, and those cases deserve
their own fix rather than a permanently-failing check.
