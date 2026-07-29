# D-094 — CI's `kg-validate` step needs a real (even empty) `knowledge` directory

Found immediately after D-093's fix cleared the fan-out hang: `make kg-validate` then failed
fast (`notes directory does not exist: knowledge`, exit 1) on the very next CI run. `knowledge` is
a git-tracked symlink (mode 120000) to `/home/runner/workspace/services/chemclaw-notes-repo/knowledge`
— an absolute path specific to the Replit workspace layout, deliberately kept as one of the "six
Replit-only additions" (D-091). It does not resolve on a GitHub Actions runner, or on any other
checkout; `Path.exists()` on a symlink follows it to the target, so `kg.validate.main()` correctly
reports the directory as missing and refuses to validate.

Not touched: the committed symlink itself — it is a real, deliberate deployment decision for
Replit (D-091), and rewriting or removing it here would be an unrelated, out-of-scope change to
that target. Instead, `.github/workflows/ci.yml` gained one step before `Validate knowledge graph`
that replaces the broken symlink with a real empty directory *in that checkout only*: `kg-validate`
against zero notes is a legitimate, already-documented state (BACKLOG.md: "the corpus holds no
procedure notes yet"), not a special case to work around. Verified locally by reproducing the exact
CI condition (removing the tracked symlink, recreating an empty directory, running
`python -m kg.validate`) — exits 0, "OK: knowledge is a valid knowledge graph" — then restored the
symlink in the working tree before committing, since only the CI step changes, not the tracked path.
