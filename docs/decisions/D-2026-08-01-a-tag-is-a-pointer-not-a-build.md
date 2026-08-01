# D-2026-08-01-a-tag-is-a-pointer-not-a-build — A tag is a pointer, not a build

**Status:** accepted · **Date:** 2026-08-01 · **Extends:** F6-T1 (the single multi-target image),
D-117 (the stranded workflow), REV-17 (`deployment_revision` on every audit record)

## Context

The chart deployed `tag: "0.1.0"`, the Containerfile built `FROM …/python-311:latest`, there was no
`imagePullSecrets` field anywhere, and nothing scanned dependencies, produced an SBOM, or scanned
the image. The Containerfile also flagged xtb (LGPL-3.0) and crest (GPL-3.0) redistribution as an
**unmade decision**, in a comment, indefinitely.

The mutable tag is the part with teeth, and the reason is specific to this system. `helm rollback`
to a release naming `0.1.0` fetches whatever `0.1.0` means *now* — so a rollback, the operation
whose entire purpose is to return to known-good bytes, does not. And REV-17 put a build revision on
every audit record precisely so a past result ties to the version that produced it; a re-pushed tag
makes that column a claim about a name rather than about bytes.

Nine templates each interpolated `repository:tag` themselves, so this was also nine places to fix.

## Decision

**Name bytes where bytes can be named, and record them where they cannot.**

- `chemclaw.image` is the one helper every pod spec uses. `image.digest` wins when set; the tag
  remains the default so `helm install .` still works in dev. The chart is now capable of a pinned
  release, and a test asserts no template builds its own reference — because the failure mode is a
  tenth pod spec added later that quietly ignores the digest the other nine honour.
- `image.pullSecrets` exists, on every pod spec, counted per spec rather than per file.
- `deploy/Containerfile` takes `ARG BASE_IMAGE`, so a release pins the base by digest without
  editing the file.
- CI gains two blocking gates: `pip-audit` over the exported lockfile, and an SPDX SBOM from
  `syft` retained 90 days with the built image's digest. `make deps-audit` is the same command, so
  a red build is reproducible locally. The **image** scan is deferred to its own change — see below;
  it ran here, it earned its keep, and it does not yet belong in a merge gate.
- `ARG INCLUDE_CREST` makes the licence decision a build flag.

## Why not the alternatives

**Pin the base image digest in the Containerfile.** The obvious move, and wrong here. A digest
written into the file is stale within weeks, and then every developer build pulls a base missing
months of CVE fixes — a security regression introduced by a security control. The dev default
floats and a release pins, which is the same split `image.digest` makes in the chart. The honest
cost is that the bytes cannot be pinned *in advance*, which is exactly why the SBOM and the digest
record what a build actually contained: if you cannot name them before, name them after.

**Non-blocking scanners.** Rejected in the same words the ServiceMonitor row earned one layer down:
a control that produces output and reports to nobody is not a control. A finding that genuinely
cannot be fixed is an explicit `--ignore-vuln` with its reason in the diff — a decision — rather
than a red badge everyone learns to scroll past. The dependency audit was run against the current
lockfile before this shipped and is clean, so the gate starts green rather than starting ignored.

**Make the crest licence call in this repo.** Not ours to make: whether to redistribute a GPL-3.0
binary inside a product image is the product owner's decision. What *was* wrong is that taking it
required editing a `RUN` block, so it looked like writing a patch and was therefore never taken. It
is now `--build-arg INCLUDE_CREST=false`, and `calc.crest_cli` already reports unavailable rather
than failing — the image loses conformer sampling and nothing else. The decision stays open in the
backlog with an owner named instead of dissolving into a comment.

**Image signing (cosign).** Not here. Signing needs a key, a policy admission controller to verify
it, and a registry to push to — all three of which are the cluster-ownership row this chart does not
yet answer. Pinning by digest is the property signing would enforce; adding a signature nothing
verifies would be a fourth control reporting to nobody.

## What the image scan found, and why it is not merged

The `trivy` image scan was written as a third blocking gate, run eight times against real builds,
and is **not in this change**. Both halves of that sentence are the finding.

**It earned its keep immediately.** Three classes of real problem, none of them reachable by the
lockfile audit, the offline suite, `mypy`, or review, and all of them present in every image this
repo has ever built. Every fix is in `deploy/Containerfile` and stays:

1. **Base OS packages** — openssh (use-after-free in host-key re-exchange), openssl (heap UAF in
   `PKCS7_verify`), python3, python3-urllib3, nodejs — each with an errata already published and
   simply not yet in Red Hat's periodic UBI9 rebuild. `dnf -y update` at build time, which is also
   what makes the floating-base decision coherent: the base moves when Red Hat rebuilds it, the
   packages move on *every* build regardless.
2. **`setuptools` 65.5.1 in the base interpreters**, carrying CVE-2024-6345 (RCE) and
   CVE-2025-47273. Not a locked dependency — `make deps-audit` is clean and always was — so no
   lockfile change could have reached it.
3. **uv's wheel cache shipped in the runtime image.** `uv sync` extracts every downloaded wheel
   into `$HOME/.cache/uv/archive-v0/`, each with its own `dist-info`, and that build-time scratch
   was in the production artifact. `uv cache clean` removes it, which is strictly better than
   upgrading anything: the copies are not made safe, they are made absent.

**And then it said something the artifact contradicts.** After all three fixes, the scan still
reports `setuptools` 70.3.0 and `msgpack` 1.1.2, while an exhaustive `find / -xdev` in the same
build — printed into the build log — lists every versioned artifact of both and contains neither.
What it does list is setuptools 83.0.0 in all three importable environments, RPM-owned 65.5.1 and
53.0.0 shadowed under `/usr/lib`, and virtualenv seed wheels at 82.0.1 and 83.0.0. No 70.3.0
anywhere; no msgpack at all.

That is where it stops, and the reason is about honesty rather than effort. **A gate whose last
word contradicts the artifact it scanned makes every future red build ambiguous** — the next
engineer cannot tell a real finding from this one, which is the same disease as a non-blocking
scanner arrived at from the opposite direction. And the two candidate resolutions are both worse
than waiting: softening the gate is the failure this whole row exists to end, and writing an
`--ignore-vuln` whose stated reason is "I could not find it" would be a documented decision resting
on an unverified claim.

The remaining work is an environment where the image can be inspected interactively — this one has
no container runtime and no registry, so each hypothesis cost a full CI round trip. It is a backlog
row with the investigation's exact state recorded, not a lost thread.

## What the two shipped gates found

`pip-audit` over the lockfile and the SBOM both run clean today, which is the point: the dependency
gate starts green rather than starting ignored, and the SBOM records the OS packages and the two
downloaded binaries that no Python-level inventory can see. The image's *contents* are therefore
still named on every build — which is what makes the floating base defensible — even though nothing
yet fails the build over them.

## Consequences

- A release can be pinned to bytes, and a rollback returns to them.
- CI fails on a known-vulnerable dependency, on a fixable HIGH/CRITICAL in the image, and produces
  a retained SBOM naming what the build contained — including the OS packages and the two
  downloaded binaries no Python-level inventory can see.
- A private registry is reachable. Before, an operator whose registry needed authentication had no
  field to set and the pods simply failed to pull, which reads as a broken image.
- A mutation is recorded because it found a real gap: asserting the pull-secret include was *present
  in each file* passed with `deployment-connectors.yaml`'s second pod spec — the bundle's Temporal
  worker — left unable to pull. The assertion now counts includes against `_POD_SPECS`.
- `CLAUDE.md` said `make help` lists 23 targets while it listed 28. The count is gone rather than
  corrected; the same lesson as the chart's secret count, which went stale twice.

## Not in this change

Signing and a policy gate, the registry push and rollout (both need the cluster this chart does not
own), and the crest licence decision itself — now one build argument away from being takeable.
