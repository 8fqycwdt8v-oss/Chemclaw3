# D-113 — Stages D and E: profiles select an agent, templates fix a procedure

The connector seam (D-110) made *capability* one thing to add. These two stages do the same for the
two ways an "agentic workflow" is configured, and the decision worth recording is that they are two
things and not one.

**A profile (Stage D) configures an agent; the model still chooses the order.** It is a YAML file
under `profiles/` — or inside a connector bundle, when it is about that one capability — naming
instructions, a narrowed tool set, and the harness settings. A session picks one with
`POST /sessions {"profile": ...}`; an unknown name is a 400, not a silent fallback to the default,
because a caller that asked for a narrowed agent and quietly got the full one is the failure mode
worth being loud about. Agents are built once per profile and cached on the app, so the profile is a
key rather than a per-turn cost.

*The filename is the name.* `profiles/property-lookup.yaml` is `property-lookup`, and a `name:` key
in the body is refused rather than merged. Two sources of truth for one identity is drift waiting to
happen, and this is the same rule `skills/` already follows.

**A template (Stage E) fixes the procedure; the model only fills the gaps.** Also a YAML file, also
discovered, also enabled by one config token — but it runs as a Temporal workflow with an ordered
step list. Three kinds: `tool` (call anything on the agent's surface), `job` (run a connector's
durable job and *await* it), `agent` (one model turn under an optional profile). The last is what
keeps a template agentic rather than a script: the sequence does not vary, the reasoning inside a
step does.

**Why both, when the user's ask was "configure an agentic workflow easily".** A profile cannot
express "these five steps, in this order, every time" — the model may reorder or skip, which for a
safety screen preceding a written brief is precisely the judgment nobody wants delegated. A template
cannot express open-ended research. The shipped pair demonstrates the split: `property-lookup`
narrows to four calculators and lets the model work; `hazard-briefing` screens, then searches
precedent, then writes — in that order, durably, or not at all.

**Substitution is deliberately not a template language.** `${inputs.x}` and `${steps.id.result}`,
nothing else — no conditionals, loops or expressions. Those are how a config format becomes a
programming language with no debugger, and a procedure that needs them wants an agent step or real
code in a connector, not more YAML. Two rules inside that small surface earn their complexity: a
whole-string reference substitutes the *value* with its type (so a tool wanting `list[str]` does not
receive the repr of one), while an embedded reference interpolates JSON text (so a prompt reads);
and an unresolvable reference raises rather than yielding `None`, refused at *validation* time so a
broken template cannot start rather than dying on step four having already spent the compute.

**The resolved template travels in the workflow input, not its name.** Editing
`templates/<name>.yaml` therefore cannot change a run already in flight. That is the versioning
story — no migration, an edit affects only later runs — and simultaneously a hard replay
requirement: a workflow re-reading a file on replay would diverge from its own history and Temporal
would reject it.

**Identity travels too, and is re-stamped per step.** A workflow has no request context, so the
actor and roles ride in each activity's input and are set ambient before the work happens. The part
that matters is `run_tool_step` applying the audit and authz middleware *by hand*: MAF applies an
agent's middleware inside its own tool-calling loop, which a template does not go through, so a
direct `tool.invoke(...)` would run ungoverned. A template must not become a way to run a tool the
requester could not run directly, and that line is enforced there.

**Two omissions the gate caught, worth naming because both would have shipped silently.** The image
never `COPY`d `templates/` or `profiles/`: a discovered-from-disk seam that is missing simply
advertises less, so the container would have started clean and offered fewer capabilities.
`test_image_ships_every_first_party_package` catches the first by discovery; the second it structurally
cannot (no `__init__.py`), which is the argument for the explicit `COPY` and the comment above it.
`connectors/` and `templates/` were also both absent from `make type`'s package list — checked
transitively, never directly.

**Deviation from the staged plan.** Stage E was gated on "a second real use case a profile provably
cannot express". The user overrode that gate and asked for it built; `hazard-briefing` is the one
worked case, not two. The gate existed to prevent building a step engine nobody needed, so the risk
it was guarding — a second caller failing to materialize — remains open and is noted here rather
than presented as retired.
