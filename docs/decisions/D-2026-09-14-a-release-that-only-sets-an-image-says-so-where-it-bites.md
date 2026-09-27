# D-2026-09-14-a-release-that-only-sets-an-image-says-so-where-it-bites — a chartless component cannot be created by a release, and the release now says so where an operator meets it

## Status

Accepted.

## Context

`docs/planning/BACKLOG.md` carried a row reading "two of the four deployables have no chart, so a
release changes their bytes and nothing else". W29.6 asked for it to be closed from this
repository. It cannot be, and the measurement is what says so rather than the row's own hedge.

The two deployables are `Chemclaw3_ui` and every `Chemclaw3-mcp` server. Neither describes itself
deployably: the fleet has seven `Containerfile`s and a per-server `networkpolicy.yaml`, the UI has a
`Dockerfile` and a compose file. **Both live in other repositories**, and a chart written from here
would be inventing somebody else's Service, Route, probes and limits — the same objection
`deploy/jenkins/environments/README.md` already makes about shipping a placeholder values file.

What *is* in this repository is the release path those components are deployed by:
`deploy/jenkins/targets/openshift.sh`, whose `apply_deployment` runs `oc set image` against a
Deployment an operator created by hand. Driven with an `oc` that refuses the way a real one does
(`NAMESPACE=probe-ns DRY_RUN=false bash deploy/jenkins/targets/openshift.sh <descriptor>`), the
shipped behaviour was:

```
[openshift] chemclaw-ui: setting chemclaw-ui=reg/ui@sha256:abc
Error from server (NotFound): deployments.apps "chemclaw-ui" not found
SCRIPT rc=1
```

The refusal is loud — the release fails, which is the important half and it already worked. What an
operator is left with is `oc`'s sentence, and `oc`'s sentence names a symptom that reads as a
cluster somebody broke. It is not: it is the *shape* of this kind of component. A `deployment`-kind
component cannot be created by a release at all, and cannot have a port, a probe, a resource limit
or an env var moved by one. That fact was stated in three documents and in a comment at the top of
the very script, and appeared nowhere on the one path where an operator meets it.

## Decision

1. **The chart for the other two repositories is deferred, not open here.** The row moves from
   `BACKLOG.md` to `DEFERRED.md`'s "gated on infrastructure this environment does not have"
   section, beside the rollout row it shares a trigger with: one real namespace to write against,
   and the repository that owns the component writing it.
2. **The release says what it could not do, at the point it could not do it.**
   `apply_deployment` now branches on `oc set image` failing and prints `chartless_failure`, which
   names the two things a chartless component's release cannot do and the two reasons this call
   fails. The exit code is unchanged.

Deliberately *not* done: creating the Deployment from the release. A release that conjures a
workload it has no description of is the failure this split exists to avoid — it would put a
default port, a default probe and no limits into a namespace, and then own them.

## Consequences

A release against a namespace missing a chartless component now fails with a sentence an operator
can act on. Nothing else about the release path changes, and no claim is made anywhere that the UI
or the fleet is deployed by anything more than `oc set image`.

## What keeps it true

- `tests/test_jenkins_delivery.py::test_a_chartless_component_says_what_a_release_could_not_do` —
  drives the real script with a refusing `oc` on PATH and asserts both the non-zero exit and the
  sentence. Mutation: deleting the `chartless_failure` call fails it.
- `tests/test_jenkins_delivery.py::test_the_cluster_target_deploys_bytes_rather_than_a_pointer` —
  unchanged, still holds the digest-not-tag half of the same path.
- `tests/test_deferred_register.py` — holds the shape of the row this moves.
