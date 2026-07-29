# D-084 — F11 waves 3–4: operating the system; the knowledge model reasoning about itself

**Phase F11 waves 3–4: operating the system, and the knowledge model reasoning about itself.**

**Context.** D-083 closed the deployment and reachability gaps. This completes the phase: the
operational surfaces the system had no way to expose, and the knowledge-model capabilities it had no
way to ask for.

**Decision (and the reasoning that shaped each).**

1. **Metrics without a dependency, and only what a scrape needs.** `service/metrics.py` renders the
   Prometheus text format directly rather than adding `prometheus_client` — ~80 lines of stable
   protocol against another package to install, scan and pin. Counters and gauges only: latency
   distribution already rides the OTel trace pipeline, and duplicating it would create a second
   source of truth. Gauges are *callables over live structures*, so they cannot drift from what
   they describe. The route carries no labels at all, which is what lets it stay unauthenticated
   (like `/healthz`) without leaking a session id or user.

2. **Schedule health reads Temporal, not a mirror.** Temporal already knows when a Schedule fired
   and how often; a second table could only ever disagree with it. A *planned* Schedule missing from
   Temporal is reported rather than omitted — "the job was never applied" is exactly the failure the
   surface exists to show, and silence makes it indistinguishable from a healthy quiet job.

3. **Two refusals, again, are the substance.** Retention refuses `audit_events` (deleting from a
   hash chain is indistinguishable from the tampering it detects) and `calculation_results` (age is
   the wrong axis for a cache; D-011 makes eviction a silent recomputation). The pattern repeats in
   `screen_hazards` reporting `unresolved` as prominently as findings: **a capability that cannot
   cover something must say so, or its silence reads as a clearance it has not earned.**

4. **Mid-turn resume is defined by its failure modes.** Opt-in (holding a turn open holds an
   admission permit), bounded below the front door's deadline, non-recursive (else one turn could
   hold a permit indefinitely by launching a job from each continuation), and degrading to the
   *previous* behavior — result on the next turn — rather than to an error.

5. **Dry-run is ambient, never a tool argument.** As an argument the model could clear it, turning a
   chemist's requested dry run into a real HPC submission, or set it, silently no-op'ing real work.
   The same reasoning already governs the ambient session and identity.

6. **The knowledge model can now be asked about itself.** `kg/analytics.py` answers "what don't we
   know" — the complement of outward traversal, and the question that actually steers experimental
   design. `KNOWN_NOTE_TYPES` is enforced by `kg-validate` rather than by the schema, so the agent
   may still *propose* a new type and a human judges it at the PR-gate. `outcome_class` gives
   negative results somewhere to live, and the filter keeping failures out of playbook distillation
   is the load-bearing half — without it a repeated failure distils into a recommendation.

7. **One identity table, three consumers.** `chemclaw.reagents` (W2) now backs the hazard screen,
   the compound notes (KNW-7) and the conditions vocabulary (KNW-4). That is the Rule of Three
   satisfied by real callers rather than anticipated ones, and it is why `DMF`,
   `N,N-dimethylformamide` and `CN(C)C=O` can no longer split one campaign into two.

8. **Preferences are deliberately not graph notes.** Routing "Anna prefers 2-MeTHF" through the
   PR-gate would ask a reviewer to sign off on personal trivia — which is how a gate stops being
   taken seriously. The graph holds what the organisation knows; `user_preferences` holds how one
   person works.

**Two findings closed as not-gaps after assessment**, recorded so they are not re-opened blindly:
**TOOL-7** (units are carried in field names throughout, including every model added in this phase;
a `Quantity` type would be an abstraction with no second caller) and **AGT-6** (the W1 tools take
typed pydantic arguments, so MAF already forces a validated payload at the machine-consumed call
site whose absence was the original reason to defer structured outputs).

**Consequences.** Five items remain open and are listed in `BACKLOG.md` with the reason each is not
built. Three are blocked on a decision or a prerequisite rather than on effort (TOOL-6 needs a
literature-source decision; AGT-3 needs a first real document format; IDEA-6 depends on AGT-3), and
two are genuinely sizeable and warrant their own design note (IDEA-2 predicted-vs-actual
calibration, IDEA-1 standing queries). Stopping on those boundaries rather than half-building them
is the deliberate call.
