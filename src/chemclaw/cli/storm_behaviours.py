"""The storm's behaviour catalogue — what the mock model does, per scenario family.

Kept beside the mock rather than inside it because these are the *test's* content and the mock is
its mechanism: adding a scenario should not mean editing a server. Each behaviour is named, and the
storm selects one by putting `[[name]]` in the turn's message, so a scenario and the behaviour it
asserts against cannot drift apart.

Nine families, and the split is the point — "it held up under load", "it held up under a model
behaving badly" and "every tool it advertises can actually be called" are different claims, and
only the first was ever testable with a real model:

* **A volume** — cheap, realistic turns, used to find where admission control bends.
* **C shapes** — the same call delivered whole, fragmented, and in parallel.
* **D durable** — real connector jobs, including deliberate idempotency collisions.
* **F adversarial** — what a real model will not reliably do: malformed arguments, an unknown tool,
  an empty function name (the STREAM-1 shape), a 100 KB argument document, forty parallel calls,
  a turn with no prose, and an unbounded tool loop.
* **H edges** — pathological chemistry, semantically impossible arguments, and unicode driven
  through the real tools and the real database.
* **T the tool surface** — one behaviour per bundle, so that every tool the agent advertises is
  reached by a real call with arguments the tool would actually accept.

Families B (tool-path truth) and G (limits) need no behaviour of their own: B is a cross-check over
`audit_events` after the others, and G attacks the front door's own limits rather than the model.
**E (chaos) borrows** — it kills processes around `a-cheap`, `f-slow` and a directly-launched
durable job rather than asking the model for anything new.

**T is here because the other eight named five tools out of ninety-nine.** Every one of them was a
`find_notes`, a `gather_evidence`, an `expand_note`, a `find_past_jobs` or a
`compute_reaction_energy` — a storm reporting "the tool path is genuinely exercised" over 5% of the
surface, which is LOAD-1's own shape one level up: the harness measuring something narrower than
the thing it named.
`tests/test_storm_behaviour_coverage.py` is what stops that recurring, and it is modelled on
`tests/test_probe_coverage.py` because the corpus there went stale in exactly the same direction.

Every behaviour here is reached by some check in `cli/live_storm.py`, and
`tests/test_live_storm.py::test_every_declared_behaviour_is_reached_by_some_check` is what makes
that a fact rather than a hope. It was written because the sentence above it was **false when it
was first written**: six behaviours — `a-retrieval`, `d-status`, `f-slow`, `h-bad-smiles`,
`h-injection`, `h-unicode` — were declared and asserted by nothing while the run reported "17/17
checks passed", and after four of them were wired the same claim was made again with two still
dead. Confident prose about coverage is what this repository has learned not to trust, including
its own; a test is the only form of it that stays true.
"""

from __future__ import annotations

import time

from chemclaw.cli.mock_llm import Behaviour, ToolCall

# The reaction the durable family launches. The workflow id is a hash of the payload
# (`connectors.jobs.job_workflow_id`), so many sessions launching *this* simultaneously is the
# idempotency collision the D-011 guarantee is about — and the only honest way to check it is to
# count what the database did, not to read a summary.
#
# **The temperature varies per run, and that is load-bearing rather than decorative.** With a fixed
# payload the *second* storm against one database finds the answer already cached: it launches
# nothing, computes nothing, and satisfies every "at most one run" bound with zero. Measured — a
# storm on 2026-08-04 reported "0 distinct workflow id(s) across 12 turns; calculation_results
# 113 → 113" as a pass. `cli/live_jobs.py` documents this failure at length and designs
# `_RUN_TEMPERATURE_K` against it; the storm inherited the hazard without the fix.
#
# A real physical input rather than a nonce, for the same reason: any temperature in this range is
# a question a chemist could ask, and the answer genuinely changes with it. Constant within the
# process, so all twelve simultaneous launches still derive the identical id — which is the whole
# point of the family.
#
# **The period must outlast the longest run that will use it, and the first version's did not.**
# `% 719` gives 719 distinct temperatures on a one-second grid, so a value recurs every ~12 minutes
# — invisible in a single storm and unmissable in a soak: 6 of 81 rounds failed this family with
# "0 job_records row(s) written", spaced 12 rounds apart at a ~58 s round. Nothing was broken. The
# payload had been computed in an earlier round, `ALLOW_DUPLICATE_FAILED_ONLY` correctly rejoined
# the completed run rather than recomputing it, and no new record was written — D-011 working, read
# as a failure. 100,000 values on a 10-µK grid puts the period at 27.8 hours, past any soak that
# fits in this container, and keeps every value a temperature a chemist could ask about. The same
# modulus is now in all three copies (it had landed in this one only) and `tests/test_run_jitter.py`
# evaluates each expression across a 24-hour window so a fourth copy cannot get a smaller one.
#
# **The base temperature differs per harness and must.** Each grid spans base + [0, 1) K, so two
# copies sharing a base share the whole set — which is what happened when `cli/live_jobs.py` took
# this modulus and kept 298.15, giving two independent harnesses byte-identical payloads and one
# workflow id. This one keeps 298.15; `live_jobs` is 301.15 and `live_storm` 300.0, and
# `tests/test_run_jitter.py` asserts the union is disjoint rather than trusting the arithmetic.
_COLLISION_TEMPERATURE_K = 298.15 + (int(time.time()) % 100_000) / 100_000.0
_COLLISION_PAYLOAD: dict[str, object] = {
    "kind": "reaction",
    "reactants": ["N#N", "[H][H]", "[H][H]", "[H][H]"],
    "products": ["N", "N"],
    "level": "quick",
    "temperature_k": _COLLISION_TEMPERATURE_K,
    "symmetry_numbers": {"N#N": 2, "[H][H]": 2, "N": 3},
}

# The molecule the T family asks most of its questions about. Paracetamol, because it is small
# enough that a Hessian is seconds rather than minutes, and *chemically* interesting enough that
# every enumeration below returns more than one entry — a probe compound whose tautomer, microstate
# and torsion sets are all singletons would exercise the tools and prove nothing about what came
# back.
_PROBE_SMILES = "CC(=O)Nc1ccc(O)cc1"

# One real optimization problem, shared by the inline `bo` tools. Written once because those tools
# take the *same* problem, and disagreeing copies would make a failure in one of them read as a
# difference between the tools rather than between the payloads.
_BO_PROBLEM: dict[str, object] = {
    "parameters": [
        {"kind": "continuous", "name": "temperature_c", "lower": 20.0, "upper": 80.0},
        {"kind": "continuous", "name": "base_equivalents", "lower": 1.0, "upper": 3.0},
        {"kind": "categorical", "name": "base", "categories": ["K2CO3", "Cs2CO3", "K3PO4"]},
    ],
    "objectives": [{"name": "yield_percent", "direction": "maximize"}],
}
_BO_OBSERVATIONS: list[dict[str, object]] = [
    {"params": {"temperature_c": 40.0, "base_equivalents": 1.5, "base": "K2CO3"}, "value": 41.0},
    {"params": {"temperature_c": 60.0, "base_equivalents": 2.0, "base": "Cs2CO3"}, "value": 68.0},
    {"params": {"temperature_c": 75.0, "base_equivalents": 2.5, "base": "K3PO4"}, "value": 57.0},
]

# A torsion and a bond cleavage, copied from what `chem`'s own engines return rather than assembled
# by hand. `profile_rotation` and `survey_bond_strengths` both say in their descriptions that the
# entry is passed through unchanged from `enumerate_torsions` / `enumerate_bond_cleavages`, and a
# hand-written atom index answers about a different bond — the well-formed-and-wrong shape
# `h-impossible-args` exists to cover. Read off `enumerate_torsion_candidates("CC(=O)Nc1ccccc1")`
# and `enumerate_cleavages("Cc1ccccc1")` in the `chem` server, not recalled.
_ACETANILIDE_AMIDE_TORSION: dict[str, object] = {
    "torsion_id": "tor_d139107cd84f9333",
    "atoms": [2, 1, 3, 4],
    "bond": [1, 3],
    "label": "the amide C1-N3 bond",
    "symmetry_order": 1,
    "period_degrees": 360.0,
}
_TOLUENE_BENZYLIC_CLEAVAGE: dict[str, object] = {
    "atoms": [0, 1],
    "bond": "C-C",
    "fragments": ["[H][C]([H])[H]", "[H]c1[c]c([H])c([H])c([H])c1[H]"],
}

BEHAVIOURS: list[Behaviour] = [
    # ---------------------------------------------------------------- A · volume
    Behaviour(
        name="a-cheap",
        calls=[ToolCall(tool="find_notes", arguments={"text": "suzuki coupling"})],
        text="Two notes cover this coupling; both are cited above.",
        think_seconds=0.4,
    ),
    Behaviour(
        name="a-retrieval",
        calls=[
            ToolCall(tool="find_notes", arguments={"text": "amide coupling"}),
            ToolCall(tool="gather_evidence", arguments={"query": "amide coupling additive"}),
            ToolCall(
                tool="expand_note",
                arguments={"note_id": "failure-dcm-amide-coupling", "hops": 1},
            ),
        ],
        text="The record covers the additive choice and the DCM failure mode.",
        think_seconds=0.4,
    ),
    # ---------------------------------------------------------------- C · streaming shapes
    Behaviour(
        name="c-whole",
        calls=[ToolCall(tool="find_notes", arguments={"text": "buchwald amination"}, fragments=1)],
        text="One call, arguments delivered whole.",
    ),
    Behaviour(
        name="c-fragmented",
        # The hypothesis under test. `ToolCallTrace.feed` treats "name and arguments" as a complete
        # call, and the Responses client puts the name on *every* fragment — so this should either
        # reassemble into one event, or expose N events each carrying a partial document.
        calls=[ToolCall(tool="find_notes", arguments={"text": "buchwald amination"}, fragments=8)],
        text="One call, arguments delivered in eight fragments.",
    ),
    Behaviour(
        name="c-parallel",
        calls=[
            ToolCall(tool="find_notes", arguments={"text": f"probe {i}"}, fragments=3)
            for i in range(6)
        ],
        text="Six interleaved calls, each fragmented.",
    ),
    # ---------------------------------------------------------------- D · durable
    Behaviour(
        name="d-collide",
        calls=[
            ToolCall(
                tool="compute_reaction_energy",
                arguments={
                    "params": _COLLISION_PAYLOAD,
                    "rationale": "storm: many sessions asking the identical question at once",
                },
            )
        ],
        text="Launched the reaction-energy job.",
        think_seconds=0.2,
    ),
    Behaviour(
        name="d-status",
        calls=[
            ToolCall(tool="find_past_jobs", arguments={"text": "reaction", "connector": "calc"})
        ],
        text="Here is what has run.",
    ),
    # ---------------------------------------------------------------- F · adversarial
    Behaviour(
        name="f-malformed-json",
        # **JSON-shaped and broken in a way partial parsing cannot close** — which is what actually
        # reaches `AIMessage.invalid_tool_calls` and therefore the repair middleware.
        #
        # This used to send `'{"text": "unterminated'`, and the check over it is named "a truncated
        # argument document is reported, not swallowed". Measured 2026-08-28 against
        # `AIMessageChunk`, that document does not reach the repair path at all: LangChain runs a
        # streamed call's fragments through `parse_partial_json`, which closes the unterminated
        # string, and it arrives as a **valid** call `{'text': 'unterminated'}` with
        # `invalid_tool_calls` empty. `agent/model_calls.py`'s own docstring records the same
        # measurement. So the check was asserting the opposite of a documented fact about the
        # module it tests, and had been failing for that reason rather than for a defect.
        #
        # Truncation is not lost, it is *renamed*: `f-truncated-arguments` below pins what really
        # happens to it, because that is the more interesting half.
        calls=[ToolCall(tool="find_notes", arguments={}, raw_arguments='{"text": ]}')],
        text="",
        adversarial=True,
    ),
    Behaviour(
        name="f-truncated-arguments",
        # A cut argument document, and the point is that nothing rejects it.
        #
        # `parse_partial_json` closes the string, so `find_notes` runs with `text="unterminated"` —
        # a search for a word the model never finished writing, answered with "no matches" and no
        # indication that the question was truncated in transit. That is
        # `D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` with the failure one layer
        # further out than this repository has previously met it: not a call that vanished, but a
        # call that ran on an argument the model did not finish.
        #
        # It is not obviously fixable here — the completion happens inside LangChain's streaming
        # assembler, above anything `src/` owns — so this behaviour exists to make the property
        # *checked* rather than rediscovered. If a future upstream release starts rejecting the
        # document instead, the check over this goes red and says so.
        calls=[ToolCall(tool="find_notes", arguments={}, raw_arguments='{"text": "unterminated')],
        text="",
        adversarial=True,
    ),
    Behaviour(
        name="f-wrong-argument",
        # LOAD-1 itself, reproduced deliberately: `find_notes` takes `text`, not `query`. Every
        # measurement in the 2026-07 load test died here without anyone noticing, so the storm
        # asserts it is *visible* now rather than trusting that it would be.
        calls=[ToolCall(tool="find_notes", arguments={}, raw_arguments='{"query": "benzene"}')],
        text="",
        adversarial=True,
    ),
    Behaviour(
        name="f-unknown-tool",
        calls=[ToolCall(tool="tool_that_does_not_exist", arguments={"x": 1})],
        text="",
        adversarial=True,
    ),
    Behaviour(
        name="f-empty-name",
        # The STREAM-1 shape: `String should have at least 1 character` on a tool_use name, which
        # failed 30 of 150 live turns in July and was closed by D-123's `AgentPool`. Nothing has
        # re-exercised it since, because a real model does not emit it on request.
        calls=[ToolCall(tool="", arguments={"text": "x"})],
        text="",
        adversarial=True,
    ),
    Behaviour(
        name="f-huge-arguments",
        calls=[
            ToolCall(
                tool="find_notes",
                arguments={},
                raw_arguments='{"text": "' + ("x" * 100_000) + '"}',
            )
        ],
        text="",
        adversarial=True,
    ),
    Behaviour(
        name="f-call-flood",
        calls=[ToolCall(tool="find_notes", arguments={"text": f"flood {i}"}) for i in range(40)],
        text="Forty calls in one turn.",
        adversarial=True,
    ),
    Behaviour(
        name="f-no-text",
        # The `empty_answer` guard added earlier today: tools ran, nothing was written. Before that
        # guard this turn produced an empty answer and no error at all.
        calls=[ToolCall(tool="find_notes", arguments={"text": "silent"})],
        text="",
    ),
    Behaviour(
        name="f-http-500",
        calls=[],
        text="",
        http_status=500,
        adversarial=True,
    ),
    Behaviour(
        name="f-slow",
        calls=[ToolCall(tool="find_notes", arguments={"text": "slow turn"})],
        text="A deliberately slow turn.",
        think_seconds=8.0,
    ),
    # ---------------------------------------------------------------- H · data edges
    Behaviour(
        name="h-bad-smiles",
        calls=[
            ToolCall(
                tool="gather_evidence",
                arguments={"query": "C1CC", "reaction_smiles": "not>>a>>reaction"},
            )
        ],
        text="",
    ),
    Behaviour(
        name="h-unicode",
        calls=[ToolCall(tool="find_notes", arguments={"text": "咖啡因 · Ω · 🧪 · ünïcødé"})],
        text="Unicode survived the round trip.",
    ),
    Behaviour(
        name="h-impossible-args",
        # Valid JSON, valid types, and impossible to answer: the symmetry-number map names species
        # the equation does not contain. `_checked_symmetry_numbers` refuses exactly this, and it
        # was found the only way such things are found — a chaos payload in `cli/live_jobs.py`
        # inherited the wrong map, the job rejected it correctly, and the lane read as a system
        # fault until someone looked. The missing negative in this family was the shape that is
        # *well-formed and wrong*: a schema check passes it, so the only thing standing between it
        # and a plausible answer is the tool's own domain validation.
        calls=[
            ToolCall(
                tool="compute_reaction_energy",
                arguments={
                    "params": {
                        "kind": "reaction",
                        # Balanced on purpose, so the symmetry map is the *only* thing wrong with
                        # it. An unbalanced equation would also be refused, and the check would
                        # then pass for a reason other than the one it names.
                        "reactants": ["N#N", "[H][H]", "[H][H]", "[H][H]"],
                        "products": ["N", "N"],
                        "level": "quick",
                        "symmetry_numbers": {"c1ccccc1": 12, "CCO": 1},
                    },
                    "rationale": "storm: arguments that parse and cannot be true",
                },
            )
        ],
        text="",
        adversarial=True,
    ),
    Behaviour(
        name="h-injection",
        calls=[
            ToolCall(
                tool="find_notes",
                arguments={
                    "text": "'; DROP TABLE audit_events; -- <script>alert(1)</script> {{7*7}}"
                },
            )
        ],
        text="Treated as a search string, which is what it is.",
    ),
    # ------------------------------------------------ T · the tool surface, one bundle at a time
    #
    # **Why this family exists.** Every behaviour above this line drives one of five tools, out of
    # ninety-nine when these were written. Everything else — every `chem`, `safety`, `molfp`,
    # `rxnfp` and `bo` tool, the calibration ledger, the memory verbs, every template — was the
    # part a storm could say nothing about, not because anybody removed its coverage but because
    # the catalogue was written against the surface of the day and nothing re-derived it
    # afterwards. `tests/test_storm_behaviour_coverage.py` re-derives it now, in both directions;
    # these are the entries that satisfy it.
    #
    # Three groups, and the split is what keeps the family cheap. The first runs for real, because
    # a tool whose body never executes proves nothing about its arguments — that is LOAD-1. The
    # second is the lookups whose honest answer is "nothing on file". The third is driven on a
    # **dry-run** turn: those calls start durable work or push a knowledge branch, and a lane that
    # has to finish cannot launch every durable job and every template run in the deployment to
    # find out that their arguments parse. `family_t_tool_surface` in `cli/live_storm.py` says
    # which behaviour is in which group and what each one asserts.
    Behaviour(
        name="t-chem-identity",
        calls=[
            # An abbreviation the reagent corpus actually carries. That corpus is a *reagent and
            # solvent* table, so an API name — `paracetamol`, the compound the rest of this family
            # asks about — resolves to `None`, which is a real answer and the wrong path to
            # exercise here: the point is that the lookup found something.
            ToolCall(tool="resolve_compound", arguments={"name": "DIPEA"}),
            ToolCall(tool="render_structure", arguments={"smiles": _PROBE_SMILES}),
            ToolCall(tool="describe_topology", arguments={"smiles": _PROBE_SMILES}),
            ToolCall(tool="describe_sites", arguments={"smiles": _PROBE_SMILES}),
        ],
        text="Resolved the compound and described its ring positions.",
    ),
    Behaviour(
        name="t-chem-species",
        calls=[
            ToolCall(tool="enumerate_tautomers", arguments={"smiles": _PROBE_SMILES}),
            ToolCall(tool="enumerate_protonation_states", arguments={"smiles": _PROBE_SMILES}),
            # A defined stereocentre rather than the probe compound, which has none: an enumeration
            # over a molecule with nothing to enumerate returns one entry and would pass while
            # proving that the tool ran on nothing.
            ToolCall(tool="enumerate_stereoisomers", arguments={"smiles": "C[C@H](N)C(=O)O"}),
            ToolCall(tool="enumerate_torsions", arguments={"smiles": _PROBE_SMILES}),
        ],
        text="Enumerated the species set the ranking jobs run over.",
    ),
    Behaviour(
        name="t-chem-degradation",
        calls=[
            ToolCall(
                tool="enumerate_bond_cleavages",
                arguments={"smiles": "Cc1ccccc1", "mode": "homolytic"},
            ),
            ToolCall(tool="enumerate_degradants", arguments={"smiles": _PROBE_SMILES}),
        ],
        text="Listed the breakable bonds and the degradation hypotheses.",
    ),
    Behaviour(
        name="t-chem-batch",
        calls=[
            # SMILES for the charged species and a name for the solvent: the tool resolves either,
            # and `water` is the one whose density is certainly on file. A solvent it cannot
            # resolve is an error rather than a dropped row, so a name it does not know would
            # exercise the refusal instead of the table.
            ToolCall(
                tool="stoichiometry_table",
                arguments={
                    "basis": "Nc1ccc(O)cc1",
                    "basis_mass_g": 250.0,
                    "reagents": ["CC(=O)OC(C)=O"],
                    "equivalents": [1.2],
                    "solvents": ["water"],
                    "volumes": [10.0],
                },
            ),
            ToolCall(
                tool="green_metrics",
                arguments={"input_masses_g": [250.0, 280.0, 2500.0], "product_mass_g": 310.0},
            ),
        ],
        text="Charge table and the E-factor for the batch.",
    ),
    Behaviour(
        name="t-safety-screen",
        calls=[
            ToolCall(
                tool="screen_hazards",
                arguments={"smiles": [_PROBE_SMILES, "O=[N+]([O-])c1ccccc1"]},
            ),
            # A sulfonate ester — the textbook DNA-reactive alert, so the screen is asked something
            # it must find rather than something it will pass.
            ToolCall(tool="screen_genotoxic_alerts", arguments={"smiles": ["CS(=O)(=O)OC"]}),
            ToolCall(tool="ich_impurity_limit", arguments={"substance": "acetonitrile"}),
        ],
        text="Structural alerts and the Q3C limit, both cited.",
    ),
    Behaviour(
        name="t-calc-properties",
        calls=[
            ToolCall(tool="compute_xtb_energy", arguments={"smiles": "CCO", "charge": 0}),
            ToolCall(tool="predict_pka", arguments={"smiles": "CC(=O)O"}),
            ToolCall(tool="predict_solubility", arguments={"smiles": _PROBE_SMILES}),
            ToolCall(tool="predict_logd", arguments={"smiles": _PROBE_SMILES, "ph": 7.4}),
            ToolCall(tool="predict_developability_profile", arguments={"smiles": _PROBE_SMILES}),
        ],
        text="The cached property panel for this compound.",
        think_seconds=0.2,
    ),
    Behaviour(
        name="t-calc-electronic",
        calls=[
            ToolCall(
                tool="compute_electronic_properties",
                arguments={"smiles": "c1ccccc1", "solvent": "acetonitrile"},
            ),
            # Bromobenzene for both descriptor tools, because a sigma-hole is what they are asked
            # about and a molecule with no heavy halogen has none to find.
            ToolCall(tool="compute_atomic_descriptors", arguments={"smiles": "Brc1ccccc1"}),
            ToolCall(tool="compute_surface_potential", arguments={"smiles": "Brc1ccccc1"}),
            ToolCall(
                tool="predict_site_reactivity",
                arguments={"smiles": "Cc1ccccc1", "mode": "electrophilic", "top_n": 3},
            ),
        ],
        text="Frontier orbitals, the potential extrema and the Fukui ranking.",
        think_seconds=0.2,
    ),
    Behaviour(
        name="t-calc-geometry",
        calls=[
            ToolCall(tool="optimize_geometry", arguments={"smiles": "CCO", "solvent": "water"}),
            ToolCall(
                tool="compute_thermochemistry",
                arguments={
                    "smiles": "CCO",
                    "solvent": "water",
                    "symmetry_number": 1,
                    "temperature_k": 298.15,
                },
            ),
        ],
        text="Relaxed the structure and took its Hessian.",
        think_seconds=0.2,
    ),
    Behaviour(
        name="t-calc-ledger",
        calls=[
            ToolCall(tool="calculator_trust", arguments={"property_name": "pka"}),
            ToolCall(
                tool="calculator_outliers",
                # `matching` is a **SMARTS/SMILES fragment**, not a class name — the tool's own
                # docstring gives `C(=O)O` for carboxylic acids. This said `"amide"` and the call
                # was refused live with `unparseable substructure query: 'amide'`, which `_validate`
                # cannot catch: it checks that an argument *name* is one the tool takes and that a
                # generated `params` payload validates, and neither reaches the domain of a
                # free-form string. The refusal is the product behaving correctly; the catalogue
                # was wrong, and a behaviour that is refused measures the refusal rather than the
                # tool body it was written to exercise.
                arguments={"property_name": "solubility", "matching": "C(=O)N", "limit": 5},
            ),
            # A real measured value (acetic acid, pKa 4.76) rather than a nonce: this appends to
            # the calibration ledger the two reads above consult, and a fabricated residual would
            # be a wrong number in the one store this system keeps to say how wrong it is.
            ToolCall(
                tool="report_measurement",
                arguments={"property_name": "pka", "smiles": "CC(=O)O", "measured_value": 4.76},
            ),
        ],
        text="How far the calculators have been off, and one new measurement.",
    ),
    Behaviour(
        name="t-calc-record",
        calls=[
            ToolCall(
                tool="find_calculations",
                arguments={"smiles": "CCO", "calc_type": "compute_xtb_energy", "limit": 5},
            )
        ],
        text="What has already been computed for this molecule.",
    ),
    Behaviour(
        name="t-molfp-search",
        calls=[
            ToolCall(
                tool="similar_molecules",
                arguments={"smiles": _PROBE_SMILES, "top_k": 5, "threshold": 0.3},
            ),
            ToolCall(tool="substructure_matches", arguments={"query": "c1ccccc1C(=O)N"}),
        ],
        text="Nearest neighbours by ECFP4 and the substructure hits.",
    ),
    Behaviour(
        name="t-rxnfp-similar",
        calls=[
            ToolCall(
                tool="similar_reactions",
                arguments={"reaction_smiles": "CC(=O)O.NCC>>CC(=O)NCC", "top_k": 5},
            ),
            ToolCall(
                tool="conditions_for_similar_reaction",
                arguments={"reaction_smiles": "CC(=O)O.NCC>>CC(=O)NCC", "top_k": 5},
            ),
            ToolCall(
                tool="conditions_for_similar_product",
                arguments={"product_smiles": "CC(=O)NCC", "top_k": 5},
            ),
            ToolCall(
                tool="substrate_precedent",
                arguments={"smiles": "Brc1ccccc1", "role": "reactant", "top_k": 5},
            ),
        ],
        text="Precedent for this amide bond, by reaction and by product.",
    ),
    Behaviour(
        name="t-rxnfp-precedent",
        calls=[
            ToolCall(
                tool="reagent_frequency",
                arguments={"named_reaction": "Suzuki coupling", "roles": ["catalyst"], "top_k": 5},
            ),
            ToolCall(
                tool="reactions_making_substructure",
                arguments={"smarts": "c1ccccc1-c1ccccc1", "top_k": 5},
            ),
            ToolCall(tool="workup_precedent", arguments={"reagent_smiles": "ClCCl", "top_k": 5}),
        ],
        text="Aggregate condition statistics over the corpus.",
    ),
    Behaviour(
        name="t-bo-inline",
        calls=[
            ToolCall(
                tool="suggest_next_experiment",
                arguments={
                    "problem": _BO_PROBLEM,
                    "observations": _BO_OBSERVATIONS,
                    "count": 2,
                    "assay_noise": 3.0,
                },
            ),
            ToolCall(
                tool="generate_screening_design",
                arguments={"problem": _BO_PROBLEM, "n_center": 2},
            ),
            ToolCall(
                tool="predict_outcome",
                arguments={
                    "problem": _BO_PROBLEM,
                    "observations": _BO_OBSERVATIONS,
                    "points": [{"temperature_c": 65.0, "base_equivalents": 2.2, "base": "Cs2CO3"}],
                },
            ),
            ToolCall(
                tool="campaign_progress",
                arguments={
                    "problem": _BO_PROBLEM,
                    "observations": _BO_OBSERVATIONS,
                    "assay_noise": 3.0,
                },
            ),
        ],
        text="The next two points, the screening design, and where the campaign stands.",
        think_seconds=0.2,
    ),
    Behaviour(
        name="t-memory",
        calls=[
            # `remember` and `forget` of the same key are emitted in one batch, so their order is
            # not decided here. That is deliberate rather than sloppy: what this proves is that
            # each verb's body ran with arguments it accepts, and whichever lands second leaves the
            # store in a state the next storm can repeat.
            ToolCall(
                tool="remember_preference",
                arguments={
                    "key": "preferred-amide-coupling-reagent",
                    "value": "T3P in ethyl acetate",
                },
            ),
            ToolCall(tool="recall_preferences", arguments={}),
            ToolCall(
                tool="forget_preference", arguments={"key": "preferred-amide-coupling-reagent"}
            ),
            ToolCall(tool="recall_observations", arguments={"limit": 5}),
        ],
        text="Preferences written, read back and cleared.",
    ),
    Behaviour(
        name="t-watches",
        calls=[
            ToolCall(
                tool="watch_for",
                arguments={"query": "protodeboronation", "note_type": "failure-mode"},
            ),
            ToolCall(tool="list_watches", arguments={}),
            ToolCall(tool="stop_watching", arguments={"query": "protodeboronation"}),
        ],
        text="Subscribed, listed and unsubscribed.",
    ),
    Behaviour(
        name="t-knowledge-read",
        calls=[
            ToolCall(tool="find_knowledge_gaps", arguments={}),
            # Note ids that exist in `knowledge/reaction/`, so the comparison is built from real
            # protocol text rather than from two references the tool has to report as unread.
            ToolCall(
                tool="condense_protocols",
                arguments={"protocol_refs": ["rxn-amide-edc", "rxn-buchwald-amination"]},
            ),
        ],
        text="What the record is missing, and the two protocols side by side.",
    ),
    Behaviour(
        name="t-scratchpad",
        calls=[
            # `/scratch/` because `agent/scratchpad.py` allows writes under exactly two roots and
            # denies everywhere else; a path outside them is refused by the backend, which would
            # exercise the refusal rather than the verb.
            ToolCall(
                tool="write_file",
                arguments={
                    "file_path": "/scratch/solvent-screen.md",
                    "content": "toluene 68%\n2-MeTHF 71%\n",
                },
            ),
            ToolCall(tool="ls", arguments={"path": "/scratch/"}),
            ToolCall(tool="glob", arguments={"pattern": "*.md", "path": "/scratch/"}),
            ToolCall(tool="grep", arguments={"pattern": "toluene", "path": "/scratch/"}),
        ],
        text="Wrote the screen to the scratchpad and searched it.",
    ),
    Behaviour(
        name="t-attachments",
        calls=[ToolCall(tool="list_attachments", arguments={})],
        text="What this session has attached.",
    ),
    # --- the lookups whose honest answer is "nothing on file" ------------------------------------
    #
    # Every reference here is deliberately unresolvable, because none of these tools can be given a
    # real one from a static catalogue: a job id, a campaign id and an artifact ref are all minted
    # by a run. So what the family asserts about this behaviour is not that the calls succeeded but
    # that the **turn survived** them — a tool that dies rather than reporting "not found" costs a
    # chemist the turn, which is the class D-2026-08-04 named.
    Behaviour(
        name="t-unknown-reference",
        calls=[
            ToolCall(tool="get_durable_job_status", arguments={"job_id": "job-storm-not-on-file"}),
            ToolCall(tool="resume_campaign", arguments={"campaign_id": "campaign-storm-unknown"}),
            ToolCall(tool="list_artifacts", arguments={"calc_ref": "calc-storm-not-on-file"}),
            ToolCall(
                tool="fetch_artifact",
                arguments={"artifact_ref": "artifact-storm-not-on-file", "max_chars": 2000},
            ),
            ToolCall(tool="read_attachment", arguments={"name": "not-attached.csv"}),
        ],
        text="None of those references is on file.",
    ),
    Behaviour(
        name="t-scratchpad-edit",
        # The other two filesystem verbs, split from `t-scratchpad` rather than appended to it.
        # One assistant message's calls are dispatched together, so a read placed beside its own
        # write resolves in whichever order the tool node happened to run them — and the scratchpad
        # is a `StateBackend`, per turn rather than per session, so a fresh turn's is empty either
        # way. "No such file" is therefore the honest expectation here, which is why this sits in
        # the group above rather than in the strict one.
        calls=[
            ToolCall(tool="read_file", arguments={"file_path": "/scratch/solvent-screen.md"}),
            ToolCall(
                tool="edit_file",
                arguments={
                    "file_path": "/scratch/solvent-screen.md",
                    "old_string": "toluene 68%",
                    "new_string": "toluene 68% (repeat)",
                    "replace_all": False,
                },
            ),
        ],
        text="",
    ),
    Behaviour(
        name="t-clarify",
        # Its own behaviour because it is the one tool whose *success* ends the turn differently:
        # it asks rather than answers, so folding it into a panel would make every other call in
        # that panel depend on how the dialogue tool terminates.
        calls=[
            ToolCall(
                tool="ask_clarifying_question",
                arguments={
                    "question": "Which solvent should the screen hold fixed?",
                    "options": ["toluene", "2-MeTHF", "ethyl acetate"],
                },
            )
        ],
        text="",
    ),
    # --- driven on a dry-run turn: refused before anything is started ----------------------------
    #
    # `agent/tool_authz.dry_run_refusal` refuses every side-effecting call on a turn the caller
    # marked `dry_run`, so each of these reaches the gate with its arguments decoded and starts
    # nothing. That is weaker than the group above — it proves the call was well-formed enough to
    # be refused, not that the tool body accepted it — and it is the price of not launching eleven
    # CREST searches and nine template runs in a lane that has to finish. It also buys something
    # nothing else measures: IDEA-4's dry-run gate swept across the whole expensive surface at once.
    Behaviour(
        name="t-job-calc-screens",
        calls=[
            ToolCall(
                tool="compare_solvents",
                arguments={
                    "params": {
                        "kind": "solvents",
                        "reactants": ["CC(=O)O", "NCC"],
                        "products": ["CC(=O)NCC", "O"],
                        "solvents": ["toluene", "acetonitrile", "dmso"],
                        "level": "quick",
                        # Every species, not just the interesting one: a species left out of this
                        # map has its entropy computed at sigma=1 and the job then reports no ΔG
                        # at all, naming it — so a partial map is a payload that runs and answers
                        # a narrower question than the one it looks like it asked.
                        "symmetry_numbers": {
                            "CC(=O)O": 1,
                            "NCC": 1,
                            "CC(=O)NCC": 1,
                            "O": 2,
                        },
                    },
                    "rationale": "storm: narrow the solvent screen before bench time",
                },
            ),
            ToolCall(
                tool="rank_species",
                arguments={
                    "params": {
                        "kind": "species_ranking",
                        "species": ["Oc1ccncc1", "O=c1cc[nH]cc1"],
                        "labels": ["hydroxypyridine", "pyridone"],
                        "ranking": "tautomers",
                        "solvent": "water",
                        "level": "quick",
                    },
                    "rationale": "storm: which tautomer every other number is about",
                },
            ),
            ToolCall(
                tool="rank_species_across_solvents",
                arguments={
                    "params": {
                        "kind": "species_solvents",
                        "species": ["Oc1ccncc1", "O=c1cc[nH]cc1"],
                        "ranking": "tautomers",
                        "solvents": ["water", "toluene"],
                        "level": "quick",
                    },
                    "rationale": "storm: does the major form change between media",
                },
            ),
        ],
        text="",
    ),
    Behaviour(
        name="t-job-calc-conformers",
        calls=[
            ToolCall(
                tool="sample_conformers",
                arguments={
                    "params": {
                        "kind": "ensemble",
                        "smiles": _PROBE_SMILES,
                        "search": "conformers",
                        "solvent": "water",
                        "effort": "quick",
                    },
                    "rationale": "storm: which shape the molecule is actually in",
                },
            ),
            ToolCall(
                tool="refine_ensemble",
                arguments={
                    "params": {
                        "kind": "refined_ensemble",
                        "smiles": _PROBE_SMILES,
                        "solvent": "water",
                        "top_n": 5,
                    },
                    "rationale": "storm: re-weight the ensemble by free energy",
                },
            ),
            ToolCall(
                tool="compute_ensemble_property",
                arguments={
                    "params": {
                        "kind": "ensemble_property",
                        "smiles": _PROBE_SMILES,
                        "prop": "dipole_debye",
                        "solvent": "water",
                    },
                    "rationale": "storm: a dipole averaged over the populated conformers",
                },
            ),
        ],
        text="",
    ),
    Behaviour(
        name="t-job-calc-coordinates",
        calls=[
            ToolCall(
                tool="scan_coordinate",
                arguments={
                    "params": {
                        "kind": "scan",
                        "smiles": "CCCC",
                        "atoms": [0, 1, 2, 3],
                        "values": [0.0, 60.0, 120.0, 180.0],
                        "solvent": "toluene",
                    },
                    "rationale": "storm: the butane torsional profile, which is textbook",
                },
            ),
            ToolCall(
                tool="profile_rotation",
                arguments={
                    "params": {
                        "kind": "rotation",
                        "smiles": "CC(=O)Nc1ccccc1",
                        "torsion": _ACETANILIDE_AMIDE_TORSION,
                        "solvent": "toluene",
                        "level": "quick",
                    },
                    "rationale": "storm: can the two amide rotamers be separated",
                },
            ),
        ],
        text="",
    ),
    Behaviour(
        name="t-job-calc-association",
        calls=[
            ToolCall(
                tool="predict_pka_ensemble",
                arguments={
                    "params": {
                        "kind": "microstate_pka",
                        "smiles": "OC(=O)c1ccccc1O",
                        "branch": "acid",
                        "solvent": "water",
                        "effort": "quick",
                    },
                    "rationale": "storm: which proton leaves first on a polyfunctional acid",
                },
            ),
            ToolCall(
                tool="compute_interaction_energy",
                arguments={
                    "params": {
                        "kind": "complex",
                        "smiles_a": "CC(=O)O",
                        "smiles_b": "CCN",
                        "solvent": "toluene",
                        "effort": "quick",
                    },
                    "rationale": "storm: does this acid/base pair associate",
                },
            ),
        ],
        text="",
    ),
    Behaviour(
        name="t-job-calc-bonds",
        calls=[
            ToolCall(
                tool="survey_bond_strengths",
                arguments={
                    "params": {
                        "kind": "bond_survey",
                        "smiles": "Cc1ccccc1",
                        "cleavages": [_TOLUENE_BENZYLIC_CLEAVAGE],
                        "level": "quick",
                    },
                    "rationale": "storm: which bond breaks first under forced degradation",
                },
            )
        ],
        text="",
    ),
    Behaviour(
        name="t-job-bo-campaign",
        calls=[
            ToolCall(
                tool="start_optimization_campaign",
                arguments={
                    "params": {
                        # `solubility_max` is one of the two names `science/bo/objectives` actually
                        # registers, over the molecule parameter that registry's own problem
                        # builder uses. A campaign naming an unregistered objective is refused at
                        # launch, which would measure the precondition rather than the launcher.
                        "problem": {
                            "parameters": [
                                {
                                    "kind": "categorical",
                                    "name": "molecule",
                                    "categories": ["CCO", _PROBE_SMILES, "c1ccccc1"],
                                }
                            ],
                            "objectives": [{"name": "log_s", "direction": "maximize"}],
                        },
                        "objective_name": "solubility_max",
                        "n_initial": 2,
                        "n_rounds": 2,
                        "batch": 1,
                    },
                    "rationale": "storm: a durable campaign over a small molecule library",
                },
            )
        ],
        text="",
    ),
    Behaviour(
        name="t-job-results",
        calls=[
            ToolCall(
                tool="republish_calculations",
                arguments={
                    "params": {"requeue_failed": True, "batch": 100},
                    "rationale": "storm: re-queue what a newly attached results store never saw",
                },
            )
        ],
        text="",
    ),
    Behaviour(
        name="t-job-report",
        calls=[
            ToolCall(
                tool="request_development_report",
                arguments={
                    "title": "Amide coupling solvent screen",
                    "sections": [
                        {
                            "heading": "Precedent",
                            "query": "amide coupling in 2-MeTHF",
                            "memory_layer": "evidence",
                        },
                        {
                            "heading": "What we tried",
                            "query": "amide coupling campaign",
                            "memory_layer": "episodic",
                        },
                    ],
                },
            )
        ],
        text="",
    ),
    Behaviour(
        name="t-knowledge-write",
        calls=[
            ToolCall(
                tool="propose_knowledge_note",
                arguments={
                    "id": "storm-t-surface-probe",
                    "type": "failure-mode",
                    "body": "Storm probe: the tool-surface family reached this write path.",
                    "compound_smiles": _PROBE_SMILES,
                    "tags": ["storm"],
                    "confidence": 0.5,
                },
            ),
            ToolCall(
                tool="record_confirmed_answer",
                arguments={
                    "interaction_id": "storm-t-surface-interaction",
                    "question": "Which solvent held the best yield?",
                    "answer": "2-MeTHF, at 71%.",
                    "evidence_note_ids": ["rxn-amide-edc"],
                },
            ),
            ToolCall(
                tool="record_failure",
                arguments={
                    "refutes": "failure-dcm-amide-coupling",
                    "what_happened": "Storm probe: the negative-result path was reached.",
                    "compound_smiles": _PROBE_SMILES,
                    "confidence": 0.5,
                },
            ),
        ],
        text="",
    ),
    Behaviour(
        name="t-memory-synthesis",
        calls=[ToolCall(tool="synthesize_memory", arguments={"kind": "playbook", "fresh": False})],
        text="",
    ),
    Behaviour(
        name="t-template-species",
        calls=[
            ToolCall(
                tool="run_tautomer_resolution",
                arguments={"params": {"smiles": "Oc1ccncc1", "solvent": "water"}},
            ),
            ToolCall(
                tool="run_microspecies_profile",
                arguments={"params": {"smiles": _PROBE_SMILES, "solvent": "water"}},
            ),
            ToolCall(
                tool="run_stereoisomer_ranking",
                arguments={"params": {"smiles": "C[C@H](N)C(=O)O", "solvent": "water"}},
            ),
        ],
        text="",
    ),
    Behaviour(
        name="t-template-conformers",
        calls=[
            ToolCall(
                tool="run_conformer_refinement",
                arguments={"params": {"smiles": _PROBE_SMILES, "solvent": "water"}},
            ),
            ToolCall(
                tool="run_ensemble_free_energy",
                arguments={"params": {"smiles": _PROBE_SMILES, "solvent": "water"}},
            ),
            ToolCall(
                tool="run_regioselectivity_in_conformer",
                arguments={"params": {"smiles": "Cc1ccccc1", "solvent": "acetonitrile"}},
            ),
        ],
        text="",
    ),
    Behaviour(
        name="t-template-safety",
        calls=[
            ToolCall(
                tool="run_hazard_briefing",
                arguments={"params": {"smiles": "O=[N+]([O-])c1ccccc1"}},
            ),
            ToolCall(
                tool="run_degradant_triage",
                arguments={"params": {"smiles": _PROBE_SMILES, "solvent": "water"}},
            ),
        ],
        text="",
    ),
    Behaviour(
        name="t-template-bonds",
        calls=[
            ToolCall(
                tool="run_bond_strength_survey",
                arguments={"params": {"smiles": "Cc1ccccc1", "solvent": "toluene"}},
            )
        ],
        text="",
    ),
]
