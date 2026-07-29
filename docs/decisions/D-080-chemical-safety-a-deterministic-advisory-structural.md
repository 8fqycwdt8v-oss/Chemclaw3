# D-080 — Chemical safety: a deterministic, advisory structural screen (never a clearance)

**Context.** The last remaining capability gap the user had parked *for a decision* rather than
deferred. Its own precondition — "decide scope before any capability phase that could propose a
hazardous route or procedure" — was already past: BO recommendations (1d.5) and development reports
(5b) publish agent-authored procedures today, and no hazard logic existed anywhere in the tree (only
prose cautions in two `SKILL.md` files). Unlike every other open capability item, this one is not
infra-gated: it can be built and proven offline.

**Decision — the minimum viable slice, deliberately advisory.**

- `safety/rules.yaml` — a committed, citation-carrying SMARTS table (organic/acyl azide, diazo,
  diazonium, peroxide, nitrate ester, polynitroaromatic, perchlorate, hydrazine, N-halamine) plus
  one pairwise incompatibility (strong oxidizer with strong reductant). **Data, not code**: a
  process-safety chemist maintains it without touching Python.
- `safety/screen.py` — `screen_structure` / `screen_reaction` returning `HazardFlag`s (rule, severity,
  explanation, citation, what matched), worst first. Deterministic, offline, no model.
- `agents/safety_tools.py::screen_hazards` — registered through the D-075 `@tool` seam, so the agent
  gained a capability with no orchestration edit. The system prompt tells the agent to screen before
  proposing chemistry; `skills/safety-screening/SKILL.md` holds the judgment for acting on a flag.
- `safety/notes.py` + `kg/validate.py` — an **agent-authored note carrying a `## Procedure`** whose
  structures raise a flag at or above `safety_gate_severity` must document it in a `## Hazards`
  section, or `kg-validate` fails the PR. The warning reaches the reviewer before the merge, in the
  gate that already runs in CI — no new enforcement path.
- `hazard_flag_recall` (`@metric`, D-009 seam) over a committed case pinning one reference molecule
  per rule, gated at `eval_hazard_recall_min` = 1.0 — because a SMARTS that stops matching fails
  *silently*: the screen simply reports nothing, which reads as "no hazard".

**The invariant: the system flags, it never certifies.** `ScreenResult.verdict` renders an empty
result as "No rule in the hazard table matched. This is not a safety assessment." The tool docstring,
the skill, and the module docstring all repeat it, and a test asserts no clearance-like phrasing can
appear. An over-trusted screen is *more* dangerous than none: it converts an absence of knowledge
into apparent assurance, and a chemist told "no hazards" three times stops reading the fourth answer.

**Explicit non-goals** (each a separate decision, none smuggled in): no GHS/SDS database (licensing),
no toxicity/ADMET prediction, no route-level safety verdict, no regulatory or transport
classification, no thermal-stability data, no scale or engineering controls. The skill names these
as the boundary and points at the SDS, EHS, and process-safety review.

**Scoping choices that keep the gate credible.** Agent-authored notes only (a human writing up their
own procedure has made their own judgment); procedure notes only (a record that merely mentions a
structure is not an instruction); high severity only by default. A gate that fires on the wrong notes
is a gate somebody switches off. `safety_gate_enabled` exists for a deployment migrating a legacy
corpus, not as a routine escape hatch.

**Rule-table discipline.** Each rule keeps its SMARTS as specific as the motif allows and is pinned
by a test with one molecule that must match and (across the benign set) molecules that must not —
nitrobenzene must not read as polynitro, acetohydrazide must not read as free hydrazine. Perchlorate
and permanganate match with `~` bonds because RDKit sanitizes them to charge-separated forms; a
double-bond pattern would never fire on a parsed molecule (found by testing, not by reading).

**Open for the user (asked in `docs/backlog-plan.md` §5, implemented under stated defaults).**
Advisory-only scope, a committed table rather than an external hazard database, and a hard-failing
`kg-validate` rule are the defaults shipped; the gate's severity and its on/off switch are config, so
reversing any of them is an env change, not a code change.
