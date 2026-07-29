# D-067 — Fail-closed startup: unauthenticated + network-exposed refuses to boot

**Context.** With `entra_required=false` every request runs as the shared dev principal and every
authorization gate is open (SEC-2). The default bind is `service_host="0.0.0.0"` (correct inside a
container behind the OpenShift Route), so the *default* combination — no auth, all interfaces —
was a network-exposed, gates-open deployment guarded only by a startup WARNING log line. A missed
log line is not a security control; the earlier sign-off ("warn and still boot") predated the F4
identity work that made `entra_required=true` the sole production posture.

**Decision.** `create_app` now *refuses to boot* (`RuntimeError` with an actionable message) when
`entra_required` is false and `service_host` is non-loopback. Two escapes, both explicit: bind a
loopback interface (the local dev flow, unchanged), or set the new `service_allow_insecure=true`
(default false), which boots and keeps the loud warning — making an exposed unauthenticated
deployment a conscious, greppable decision instead of a default. Entra-enforced deployments are
untouched.

**Consequence.** A deployment that forgets `CHEMCLAW_ENTRA_REQUIRED=true` now fails at startup
with the fix in the error message, rather than serving the network with authorization gates open.

**Result.** Tests pin all four postures (loopback+no-auth boots; exposed+no-auth refuses;
exposed+no-auth+opt-in boots with warning; exposed+enforced boots clean); the in-process test
suite uses the loopback posture via an autouse fixture. `make lint type test` green.
