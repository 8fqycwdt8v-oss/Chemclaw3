# Verdict roll-up — generated, do not hand-edit

Regenerate rather than amend. The rule applied: a finding survives only if **neither**
refuter lens refuted it, and its agreed severity is the **lower** of the two — one sceptic
can de-escalate, neither can escalate. UNDECIDED means only one lens has reported; those are
**not** survivors. The generating script is session tooling and lives outside the repo,
because `src/` is all the code (`tests/test_repo_map.py`).

```
verdict files read: 68
findings with >=1 verdict: 57

=== SURVIVORS (53) — neither lens refuted; severity is the lower of the two
  [critical] `standardize` silently erases sp3 stereochemistry — enantiomers collapse to one compound id, on
             reach=CONFIRMED repro=CONFIRMED
  [high    ] LLM failover makes every agent build crash — the AG-12 fallback is unusable
             reach=CONFIRMED repro=CONFIRMED
  [high    ] A failed session-title write bricks the conversation with 409 for 605 seconds
             reach=CONFIRMED repro=CONFIRMED
  [high    ] A one-shot plan approval is not spent on two reachable turn endings, so one human approval auth
             reach=CONFIRMED repro=CONFIRMED
  [high    ] An atomic number of 0 terminates the server process — no exception, no error response, exit cod
             reach=CONFIRMED repro=CONFIRMED
  [high    ] One tool call can burn unbounded CPU: no size cap, no atom cap, no timeout, no concurrency limi
             reach=CONFIRMED repro=CONFIRMED
  [high    ] `manifests/` is the discovery directory *and* contains the one bundle that must not be discover
             reach=CONFIRMED repro=CONFIRMED
  [high    ] Eight `render_structure` calls with a 4 KB SMILES take the pod out of service, and the caller h
             reach=CONFIRMED repro=CONFIRMED
  [high    ] A multi-fragment SMILES silently defeats every pair rule and over-fires every counted rule
             reach=CONFIRMED repro=CONFIRMED
  [high    ] `MAX_COMPONENTS` bounds the component count, not the response — 6 KB in, 29.6 MB out
             reach=CONFIRMED repro=CONFIRMED
  [high    ] Any authenticated principal can read every other principal's durable job — inputs, results, mol
             reach=CONFIRMED repro=OVERSTATED
  [high    ] The embedding cache is a plain dict mutated from several worker threads, and a concurrent trim 
             reach=CONFIRMED repro=CONFIRMED
  [high    ] 1. The fleet connection guard counts one pool per process; a front-door process opens two
             reach=CONFIRMED repro=CONFIRMED
  [high    ] 1. A profile file's `harness_autonomy` is an unvalidated string, so a typo silently turns the p
             reach=CONFIRMED repro=CONFIRMED
  [high    ] An evidence source that fails is reported to the model as "nothing on file"
             reach=CONFIRMED repro=CONFIRMED
  [high    ] A transient git outage is filed as per-entry bad data, and the ELN cursor advances past the los
             reach=CONFIRMED repro=CONFIRMED
  [high    ] A process opens three Postgres pools; every bound and every gauge counts one
             reach=CONFIRMED repro=CONFIRMED
  [high    ] A split-principal deployment cannot take a single turn: the runtime role has no CREATE on schem
             reach=CONFIRMED repro=CONFIRMED
  [high    ] The banner's Retry control does nothing after a failed turn
             reach=CONFIRMED repro=CONFIRMED
  [high    ] Agent-authored markdown can forge the "this figure came from a tool" provenance mark
             reach=CONFIRMED repro=CONFIRMED
  [high    ] A lost upstream connection leaves the browser's response open forever
             reach=CONFIRMED repro=CONFIRMED
  [high    ] 128 concurrent SSE streams wedge every other request through the BFF, forever
             reach=CONFIRMED repro=CONFIRMED
  [high    ] Unauthenticated slow-body requests exhaust the 128-socket upstream pool and wedge the whole /ap
             reach=CONFIRMED repro=CONFIRMED
  [medium  ] The dry-run gate and the plan gate both read the raw `file_path`, but the tool writes the *norm
             reach=CONFIRMED repro=OVERSTATED
  [medium  ] An operator gate spelled with an empty role list opens the tool to everyone, including under th
             reach=OVERSTATED repro=OVERSTATED
  [medium  ] The plan-approval gate is inert wherever the ambient session id is unset — the CLI is one such 
             reach=OVERSTATED repro=CONFIRMED
  [medium  ] A loop-capped turn hands the chemist a raw tool result as the answer
             reach=OVERSTATED repro=OVERSTATED
  [medium  ] 1. An unauthenticated caller drives one outbound request to the tenant IdP per HTTP request, an
             reach=OVERSTATED repro=OVERSTATED
  [medium  ] Any authenticated user can enumerate and read every durable job's full result
             reach=OVERSTATED repro=CONFIRMED
  [medium  ] Half of `runner_trace.py` is dead: the streamed-reassembly machinery has no production caller
             reach=OVERSTATED repro=CONFIRMED
  [medium  ] `substructure_pattern` skips the whole-string gate `require_molecule` exists to be — a query wi
             reach=OVERSTATED repro=CONFIRMED
  [medium  ] Three dead Entra settings, one of them set to a real-looking tenant URL by the shipped chart
             reach=OVERSTATED repro=OVERSTATED
  [medium  ] A skipped beam gives a model gapped ranks, and the aggregator divides by the rank — flipping th
             reach=OVERSTATED repro=CONFIRMED
  [medium  ] Condition sets that every model agrees on are counted as separate one-vote candidates
             reach=OVERSTATED repro=OVERSTATED
  [medium  ] A 20,000-character SMILES argument kills the server process (SIGSEGV)
             reach=OVERSTATED repro=CONFIRMED
  [medium  ] `rxnpredict`'s two synchronous tools run RDKit on the event loop — one call stalls every other 
             reach=CONFIRMED repro=CONFIRMED
  [medium  ] `top_k` is unbounded on every served prediction tool; the `le=50` bound lives on schemas nothin
             reach=OVERSTATED repro=CONFIRMED
  [medium  ] `ich_impurity_limit("CO")` returns cobalt's PDE — `CO` is methanol, and the fleet's own props s
             reach=OVERSTATED repro=CONFIRMED
  [medium  ] `props` and `safety` disagree on triethylamine's ICH Q3C class and limit (5000 ppm vs 640 ppm)
             reach=CONFIRMED repro=OVERSTATED
  [medium  ] `ich_impurity_limit` runs unbounded RDKit canonicalization on the event loop
             reach=OVERSTATED repro=CONFIRMED
  [medium  ] Unauthenticated `/_mock/reset` recycles workflow ids, so a live handle starts serving a differe
             reach=CONFIRMED repro=CONFIRMED
  [medium  ] `entra_expensive_actions` is inert: an operator-gated connector job launches for any authentica
             reach=CONFIRMED repro=CONFIRMED
  [medium  ] 2. `connectors.<name>.enabled: false` removes the pods and leaves the bundle loaded
             reach=OVERSTATED repro=CONFIRMED
  [medium  ] `erase_actor` deletes other people's tool results: the content-addressed blob cascade is not id
             reach=CONFIRMED repro=CONFIRMED
  [medium  ] The plan-approval hash: no test binds anything past the first todo line
             reach=CONFIRMED repro=CONFIRMED
  [medium  ] `POST /sessions/{id}/plan/decision` is untested past its first guard
             reach=CONFIRMED repro=CONFIRMED
  [medium  ] The durable `PlanApprovalStore` never runs in the suite, and diverges from the twin it claims t
             reach=OVERSTATED repro=OVERSTATED
  [medium  ] The job push-back stream reconnects in an unthrottled tight loop when the response body ends
             reach=OVERSTATED repro=OVERSTATED
  [low     ] The explanatory prose has decayed: eight symbol references in this slice resolve to nothing
             reach=OVERSTATED repro=OVERSTATED
  [low     ] A typo'd `MOCK_HPC_ENFORCE_AUTH` value silently turns off all HPC authentication
             reach=OVERSTATED repro=OVERSTATED
  [low     ] The ELN control surface has no authentication and deletes JSON files it did not create
             reach=OVERSTATED repro=OVERSTATED
  [low     ] Every configured credential is printed to stderr when config validation fails
             reach=OVERSTATED repro=OVERSTATED
  [low     ] `langsmith` — the egress control imports a package nothing declares, and pyproject asserts the 
             reach=OVERSTATED repro=OVERSTATED

=== KILLED (1) — at least one lens refuted
  [low     ] `props.compare_solvents` takes an unbounded list — 700 KB in, 81.6 MB out, 15 s of frozen serve  (reach=REFUTED repro=CONFIRMED)

=== UNDECIDED (3) — only one lens has reported; NOT survivors yet
  [high    ] `predict_site_reactivity` truncates the ranking here, so the payload the caller re-ranks is mis  (repro=CONFIRMED)
  [high    ] `engine` is in every xTB key but the single point, the properties and the Fukui paths never dis  (repro=CONFIRMED)
  [high    ] The frozen-atom fallback keeps `engine="xtb"`, so every `scan_point` on a binary pod is keyed a  (repro=CONFIRMED)

=== FILED crit/high WITH NO VERDICT (32) — the coverage gap
  [critical] The digest dedupe key omits the session, so two chemists watching the same query silently lose 
  [critical] One row with a NULL (or unparseable) created_at column kills the whole warehouse sync, permanen
  [critical] A "full factorial" over mixed categorical + continuous factors is not the Cartesian product — f
  [high    ] `memory_store()` publishes the store before its migrations have run — concurrent first turns ge
  [high    ] A profile's `harness_autonomy` value is unvalidated, so a typo silently turns the plan gate off
  [high    ] `predict_site_reactivity` re-ranks a set the server already truncated by the *other* mode
  [high    ] `parse_qm_output` silently truncates a scientific-notation energy instead of raising
  [high    ] One private-constant import drags LangGraph and layer 1 into every `calc` and `bo` process
  [high    ] A calc-server outage reaches the model as "an internal error occurred"
  [high    ] The four connectors this repo actually runs serve their whole MCP surface unauthenticated
  [high    ] `expensive: true` on CREST is bypassed by `level: "thorough"` on three ungated jobs
  [high    ] The durable-launch idempotency key omits the calculator/pipeline version, so a completed pre-up
  [high    ] A connector *server* process loads the agent's authorization module and the whole LangGraph/Tem
  [high    ] An endpoint with no `tools:` gives the server an unlimited tool surface
  [high    ] A connector tool name silently replaces a core tool of the same name
  [high    ] The embedding cache is mutated from multiple threads with no lock, so `embed_texts` raises `Key
  [high    ] `llm_fallback_api_key` is a live credential outside the redaction inventory
  [high    ] A truncated ELN chunk advances the cursor past entries it never fetched, whenever any entry in 
  [high    ] A Unicode minus or en dash before a temperature flips its sign: “−78 °C” is ingested as **+78 °
  [high    ] Rows tied on the watermark beyond `fetch_limit` are stranded forever, and nothing reports it
  [high    ] The cursor advances on `max(created, modified)` while the fetch pages on `COALESCE(modified, cr
  [high    ] The warehouse ELN retrieve half has no authorization gate, and its binding cannot declare one
  [high    ] 5,760 of the 9,987 seeded ORD records — the entire Suzuki-Miyaura dataset — are rejected wholes
  [high    ] `POST /workflow/launch` accepts a body carrying no chemistry and returns a converged energy
  [high    ] The vendor MCP server does not start: `mcp>=1.2` resolves to mcp 2.0.0, which has no `mcp.serve
  [high    ] F0-3 — The migration-immutability guard is vacuous on a shallow clone, and its sibling's failur
  [high    ] An excluded pairing in the run history makes the optimizer declare a finite space exhausted whi
  [high    ] A NaN `threshold` escapes `find_matches`' clamp and turns a populated index into a "genuine neg
  [high    ] `campaign_progress` enumerates 2^k cells on the connector's event loop — a 2.8 KB tool call wed
  [high    ] `generate_screening_design` builds a 2^k-row design with no factor cap — memory exhaustion from
  [high    ] Five request models in `models.py` have no reference anywhere in the repository
  [high    ] `uncertainty.structural_domain` is dead code kept alive by its own test
```
