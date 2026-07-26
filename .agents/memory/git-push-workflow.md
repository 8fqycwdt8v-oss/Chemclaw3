---
name: Git push workflow
description: How the three Chemclaw3 repos are pushed to GitHub and the user's preference for direct commits.
---

# Git push workflow

**Rule:** Every batch of changes to the three Chemclaw3 repos is committed and pushed directly to
`main`. Do not open pull requests unless the user explicitly asks.

**Why:** The user asked for direct commits to `main` for all changes already done and all future
changes. They want to keep the upstream repos in sync with the Replit workspace without review
overhead.

**How to apply:**
1. The three repos live in these workspace directories with these remotes:
   - `services/chemclaw/` (workspace root) → `https://github.com/8fqycwdt8v-oss/Chemclaw3.git`
   - `services/chemclaw-ui/` → `https://github.com/8fqycwdt8v-oss/Chemclaw3_ui.git`
   - `services/chemclaw-mock/` → `https://github.com/8fqycwdt8v-oss/Chemclaw3_mock.git`
2. Use the `GITHUB_PAT` secret to push: `git push "https://x-access-token:${GITHUB_PAT}@github.com/..."`.
3. For the backend repo, the local branch is `Replit`; push with `HEAD:main` so the local branch
   name does not matter.
4. Before committing a large batch on the backend repo, make sure `origin/main` is fetched. If
   `issues_replit.md` was pushed via the GitHub API in the same session, `git reset --soft
   origin/main` lets you build the new commit on top of the remote HEAD without conflicts.
5. Commit messages should group Replit-specific tweaks (artifact config, preview framing) with the
   product fixes (runner.py, Composer.tsx) so a single commit describes the whole change.

**Caveats:**
- The backend repo (`/home/runner/workspace`) is the same as `services/chemclaw/`. Files under
  `services/chemclaw/` appear at `services/chemclaw/` in the GitHub tree, but `issues_replit.md`
  lives at the repo root on GitHub (moved there after being pushed via the API).
- Replit-specific files (`.replit`, `artifacts/*/artifact.toml`) are part of the backend repo and
  are pushed to `main` along with code changes, because the user asked for all changes to be pushed.
