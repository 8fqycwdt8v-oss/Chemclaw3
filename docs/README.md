# Documentation

Which of these are true today, and which are not, is the point of the layout.

| Directory | Maintained? | What is in it |
| --- | --- | --- |
| `decisions/` | **yes** — append-only | One file per architecture decision (`D-YYYY-MM-DD-<slug>.md`; the frozen `D-NNN-<slug>.md` files keep their names). `CURRENT.md` is the one-page index of what is in force, `README.md` the ledger of every ADR, `TEMPLATE.md` the starting point for a new one. A merged ADR is never edited except to add a `**Superseded-by:**` line. |
| `planning/` | **yes** | `BACKLOG.md`: the open queue, including deferred items with the reason they are not now and the trigger that would revisit them. A row is deleted when it closes. |
| `guides/` | **yes** | Operational how-to. Start with `deployment.md` (install the family end to end), `operations.md` (routine operation) and `troubleshooting.md`; `runbook.md` is the full reference. Also `model-text-evaluation.md` (shipping edits to what a model reads), workflow versioning, the xTB catalogues, the harness concept, warehouse ELN and file-share attachment, and `feeder-pipelines/`. |
| `reference/` | partly | `architektur.md` is the original four-layer design: right about the layers, historical in its details (its HPC/DFT prose describes a retracted design). `user-story-capability-map.md` and `bo-capability-map.md` are maintained. |
| `archive/` | **no** | Point-in-time documents — audits, reviews, load tests, finished plans. Accurate as of their date and never updated. |

`planning/` holds documents edited because the world changed; anything finished, dated or
describing a past state goes to `archive/`. For the code directories, see `ARCHITECTURE.md` at the
repository root.

Older ADRs cite `DECISIONS.md`, root-level `BACKLOG.md`/`DEFERRED.md` or `services/chemclaw/…`;
those references are left as written, because the record is append-only.
