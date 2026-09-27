# D-2026-09-14-the-phantom-packages-were-pips-vendored-manifest — the image scan goes on, because the packages it could not address are two lines of `pip/_vendor/vendor.txt`

## Status

Accepted. Closes the `BACKLOG.md` row "the image vulnerability scan is not merged as a gate".

## Context

The image scan has been held out of `.github/workflows/image.yml` since
`D-2026-08-01-a-tag-is-a-pointer-not-a-build`, for one stated reason: the candidate scan reported
`setuptools` 70.3.0 and `msgpack` 1.1.2 while an exhaustive `find / -xdev` **in the same build**
listed neither, and a gate whose last word contradicts the artifact it scanned makes every future
red build ambiguous. The row said to re-check that against a current trivy before merging.

Re-checked, 2026-09-14, against **trivy 0.58.2** on an image built from `deploy/Containerfile` at
this revision — which means built *with* the `uv cache clean` that a later comment recorded as the
fix:

```
chemclaw:ci (redhat 9.8)      Total: 0 (HIGH: 0, CRITICAL: 0)
Python (python-pkg)           Total: 2 (HIGH: 2, CRITICAL: 0)
  msgpack     GHSA-6v7p-g79w-8964  HIGH  fixed  1.1.2   -> 1.2.1
  setuptools  CVE-2025-47273       HIGH  fixed  70.3.0  -> 78.1.1
```

Both reproduce. So the recorded diagnosis — *"they were cached copies of build dependencies, and
the fix is to not ship the cache"* — is **false**, and had been false in the tree, in the present
tense, since it was written.

Where they actually are, found by grepping the image rather than by reasoning about it:

```
/opt/app-root/lib/python3.11/site-packages/pip/_vendor/vendor.txt
  msgpack==1.1.2
  setuptools==70.3.0
```

pip's own vendored manifest, with the code beside it (`pip/_vendor/msgpack`,
`pip/_vendor/pkg_resources`), in pip **26.2.1** — the current release. No pip upgrade moves them.
No lockfile change reaches them: `uv.lock` pins setuptools 83.0.0 and contains no msgpack at all.
The Containerfile's "upgrade it wherever it lives" loop cannot touch them either, because
`pip show setuptools` reports the *installed* 84.0.0.

**And the contradiction was never a contradiction.** The build's `find` searched for
`setuptools-[0-9]*` and `msgpack-[0-9]*` — a `dist-info`/wheel naming convention. A line in a text
file has no such name. The search was narrower than the scan; the scanner was right for three
rounds while the build's own diagnostic answered "nothing here".

## Decision

1. **Merge the scan as a blocking gate**, on pull requests as well as `main` (the image is already
   built locally on every PR, so it needs no registry): `trivy image --scanners vuln --severity
   HIGH,CRITICAL --ignore-unfixed --ignorefile .trivyignore.yaml --exit-code 1`. The installer is
   fetched to a file and checksummed before it runs, as `syft`'s already is.
2. **`.trivyignore.yaml`** carries the two findings with the measurement above and an
   `expired_at` — a suppression with no expiry is the defect W29.2 opened against the sibling
   fleet's eight permanent `--ignore-vuln` flags. It also records what it cannot do: trivy reports
   these against the bare target `Python` with no `PkgPath`, so a `paths:` filter matches nothing
   (driven — the gate still exited 1 with one in place) and the id alone suppresses the advisory
   image-wide. `make deps-audit` bounds the lockfile half of that hole independently.
3. **Correct the three places that carried the false diagnosis**: `deploy/Containerfile`'s
   `uv cache clean` block and its remediation loop, and `docs/guides/runbook.md` §(xiv), which had
   said something false about this scan in *both* directions — first that `trivy` ran when it did
   not, then that there was no scan because the scanner contradicted the artifact.
4. **Widen the build's own diagnostic** so it can see what it is asked about: it now lists every
   `vendor.txt` and the setuptools/msgpack lines inside it, beside the versioned-artifact search.

## Consequences

A base image regressing on a fixable HIGH/CRITICAL now fails the build on the PR that would ship
it, which is the class `make deps-audit` is structurally blind to: measured, the first run of this
scan found fixable advisories in the base's openssh, openssl, python3 and urllib3, every one
already fixed upstream and simply not yet in the UBI9 rebuild.

The two ignored ids will keep needing a person: when pip's vendored manifest moves, or by
2027-03-14, whichever is first.

## What keeps it true

- `tests/test_deploy_chart.py::test_every_supply_chain_gate_the_runbook_names_actually_runs` —
  every tool the runbook's §(xiv) names, in the table **or** in the prose beside it, is invoked by
  `image.yml`, and no step invoking one may carry `continue-on-error`. Four mutations, all applied
  and all verified: `continue-on-error: true` on the trivy step fails it; replacing `trivy image`
  with `echo image` fails it; replacing `syft chemclaw:ci` with `echo chemclaw:ci` fails it;
  renaming the runbook's row to a tool nothing runs fails it.
- Two of those mutations found real holes in the check as it stood and closed them, which is why
  they are listed: it matched a *substring*, so a workflow that downloaded the scanner and never
  ran it passed; and it read only a table row's first cell, so the SBOM row's claim that it "only
  fails if `syft` cannot run" was unchecked.
