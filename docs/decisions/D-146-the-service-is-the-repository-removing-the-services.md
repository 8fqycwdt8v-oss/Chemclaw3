# D-146 — The service is the repository: removing the `services/` tier

The Python service lived at `services/chemclaw/`. Nothing needed it there. The tier is the last
remnant of the Replit-era monorepo whose other half — a TypeScript client, 174 tracked files — D-138's
pass already deleted as unreferenced; the real client lives in `Chemclaw3_ui`, the mock in
`Chemclaw3_mock`, and this repository has hosted exactly one deployable thing since.

**What the empty tier cost.** It is not merely cosmetic. GitHub Actions reads workflows only from the
repository root, so every workflow carried a `defaults.run.working-directory: services/chemclaw`, and
the two that did not — the real CI gate and the image build — sat *inside* the wrapper where nothing
ever executed them. That is D-117's whole story, and it cost this repository months of `main` with no
CI at all. A layout whose failure mode is "the gate silently does not run" is a defect in the layout.

It also put `services/` (the wrapper) one character from `service/` (the FastAPI front door), which
is a genuine reading hazard in a tree that already carries `calc/` beside `connectors/calc/`.

**The move.** Every entry of `services/chemclaw/` is now the repository root; `services/` is gone.
The two `.gitignore` files merged into one (the service's Python template was a superset for Python;
the root's IDE and system sections were appended, so nothing that was ignored stopped being ignored).
`.gitattributes` repoints the Git-LFS rule at `.bin/temporal`. Both workflows dropped their
`working-directory` block.

**What was deliberately not rewritten.** `DECISIONS.md`, `BACKLOG.md`, `DEFERRED.md` and the
narrative comments in `pyproject.toml`, `deploy/Containerfile` and `deploy/README.md` still say
`services/chemclaw/`. Every one of them is past tense, describing where something *was* when it broke.
This log is append-only and the others are records of what was true at the time; editing them to match
today's tree would make the record of D-117 unreadable — it is a story about a path that no longer
exists, and it needs to name that path.

**The map.** The reason the tree read as unnavigable was never only the wrapper: eighteen flat
top-level packages with no stated grouping, several of them near-homonyms of each other. A root
`ARCHITECTURE.md` now names every directory and ties it to one of the four layers, and is the file to
update when a directory is added. D-147 and D-148 are the other two halves of the same cleanup.
