# mcp-calc — correctness · verification pass (lens: reachability + consequence)

Scope note: the findings file contains **one** finding marked critical or high — the
`predict_site_reactivity` truncation. The other five are marked medium (1) and low (4) and were
not examined, per scope.

Both repos' working trees were clean for every file cited (`git status --porcelain` empty for
`servers/calc/src/chemclaw_mcp_calc/tools.py`, `engine/xtb_props.py`,
`src/chemclaw/connectors/calc/server/tools.py`, `src/chemclaw/science/calc/models.py`), so no
diff against the pristine copy was needed.

---

## `predict_site_reactivity` truncates the ranking here, so the payload the caller re-ranks is missing atoms

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (as filed — and the consequence is worse than the finding
  states, see below)
- **What I did**:

  1. Confirmed the two defaults are both 15 and independent of each other —
     `/workspace/chemclaw3-mcp/servers/calc/src/chemclaw_mcp_calc/engine/config.py:77` and
     `/home/user/Chemclaw3/src/chemclaw/core/config/calculators.py:65`.

  2. Reproduced the server-side truncation in-process (`uv run --project servers/calc python`,
     tblite backend, no `xtb` binary — the shipped image's configuration):

     ```
     default top_n setting: 15
     total_atoms: 33 full sites: 33
     served total_atoms: 33 served sites: 15
     TRUE nucleophilic top6:
       #1 idx=11 O f_plus=0.1393
       #2 idx=12 O f_plus=0.0694
       #3 idx=4 C f_plus=0.0678
       #4 idx=10 C f_plus=0.0674
       #5 idx=32 H f_plus=0.0621
     SERVED re-ranked nucleophilic top6:
       #1 idx=11 O f_plus=0.1393
       #2 idx=12 O f_plus=0.0694
       #3 idx=4 C f_plus=0.0678
       #4 idx=32 H f_plus=0.0621
       #5 idx=24 H f_plus=0.0618
     missing from served, in true top6: [10]
     top_n=100 -> sites: 33 total_atoms: 33
     ```

     `idx=10` identified independently: `ibuprofen idx10: C [(8,'C','SINGLE'), (11,'O','DOUBLE'),
     (12,'O','SINGLE')]` — the carboxyl carbon, as the finding says.

  3. Then ran the **caller's own post-cache logic** against the exact wire payload the server
     produced (dumped to JSON from `tools.predict_site_reactivity(smiles=...)`, re-validated with
     the backend's `SiteReactivityResult` and put through `.ranked_for("nucleophilic")` plus the
     `top_n` slice at `connectors/calc/server/tools.py:795-797`):

     ```
     backend default xtb_fukui_top_n: 15
     top_n=0:   returned sites=15 total_atoms=33  carboxyl C (idx 10) present? False
     top_n=100: returned sites=15 total_atoms=33  carboxyl C (idx 10) present? False
     ```

     Both of the finding's downstream claims hold verbatim: the re-rank operates on a pre-truncated
     list, and `top_n=100` — documented on the caller side as "pass a larger number to see the whole
     molecule" — returns 15 while `total_atoms` reports 33.

  4. Swept six further molecules for how often the truncation changes the answer (server-side
     electrophilic top-15, then re-ranked as the caller does):

     ```
     ibuprofen    atoms= 33 nucleophilic true[11,12,4,10,32] served[11,12,4,32,24]  DIFFERS
     paracetamol  atoms= 20 nucleophilic true[2,18,19,1,16]  served[2,18,19,16,13]  DIFFERS
     aspirin      atoms= 21 nucleophilic same
     caffeine     atoms= 24 nucleophilic same
     nicotine     atoms= 26 nucleophilic same
     benzaldehyde atoms= 14 / toluene atoms=15  same (at or below the limit)
     radical mode: same on all seven
     ```

     Paracetamol's dropped atom is `idx=1`, `C [(0,'C','SINGLE'), (2,'O','DOUBLE'),
     (3,'N','SINGLE')]` — the amide carbonyl carbon.

- **Why**:

  **Reachability: nothing stands in the way, at any layer.** I traced from the defect outward and
  found no validator, gate or caller-side constraint that prevents it:

  - The backend caller (`src/chemclaw/connectors/calc/server/tools.py:784-797`) sends literally
    `{"smiles": smiles}` — no `mode`, no `top_n` — so the server always applies its own
    `settings.xtb_fukui_top_n` default and always sorts by the *electrophilic* index.
  - `predict_site_reactivity` is a live agent-facing tool: it is listed under `tools:` in
    `src/chemclaw/connectors/calc/connector.yaml:35`, and five root-level skills plus the
    connector's own `calculation-selection` skill route to it.
    `skills/reactivity-descriptors/SKILL.md:24` maps the plain question *"Where does a nucleophile
    add?"* to this tool with `mode: nucleophilic`. That is the trigger, produced by a chemist asking
    an ordinary question about an ordinary drug molecule.
  - `SiteReactivityResult` in the backend has no invariant tying `len(sites)` to `total_atoms` —
    `model_validate` accepted the 15-site/33-atom payload without complaint (step 3 above).
  - The cache is not required for the bug, which makes the finding's "the cache makes it permanent"
    an understatement rather than the mechanism: on a **miss** the wire call also omits `mode`, so
    the freshly computed payload is already the electrophilic top-15. Hit and miss are both wrong.
    `_site_reactivity` in `engine/identity.py:157-163` confirms neither `mode` nor `top_n` enters
    the key, so no variant of the call can produce a complete row.
  - The threshold is 15 atoms *including hydrogens*, so it is not a large-molecule edge case —
    paracetamol (20 atoms) already trips it.

  **Consequence: as stated, and materially worse than the finding's framing.** The finding says the
  ranking "is wrong at position 4". What a chemist is actually shown for "where does a nucleophile
  add to ibuprofen" is a list topped by the two carboxyl **oxygens** and a ring carbon and then
  hydrogens — with the carbonyl **carbon**, the only chemically meaningful nucleophilic-addition
  site on the molecule and the exact case the tool's own docstring names ("addition to a
  carbonyl"), absent from the payload entirely. The same holds for paracetamol's amide carbonyl
  carbon. This is a systematic bias, not a coin flip: a carbonyl carbon is electron-poor, so it
  ranks low on `f_minus` and is precisely the kind of atom the electrophilic sort demotes out of the
  top 15 — while being the atom `f_plus` exists to find. The failure mode is "the right answer is
  the one that gets cut."

  Nothing raises and nothing hints. The result carries `mode="nucleophilic"`, `ranked_by="f_plus"`,
  `total_atoms=33`, 15 sites, and no caveat field; the tool docstring the model reads describes
  `total_atoms` as "the total number of atoms the ranking was drawn from", which is false on this
  path — the ranking was drawn from the electrophilic top 15. The caller's inline comment asserting
  "the row holds every atom" is simply untrue of the row the server sends.

  Two narrowings I checked and that do **not** rescue the finding but are worth stating precisely,
  since they are the only things keeping this below critical: (a) the **default** `electrophilic`
  mode is correct — server sort and client sort agree, so the top-15 by `f_minus` is a true top-15,
  merely incomplete; (b) `radical` mode happened to be unchanged on all seven molecules I tried
  (`f_zero` is half `f_minus`, so its order correlates strongly with the sort actually applied) —
  but that is an empirical observation on seven molecules, not a guarantee. The defect bites
  `nucleophilic` reliably.

  This is not a safety or impurity-limit answer, and the tool is honestly framed as producing a
  hypothesis rather than a yield prediction, which is why I do not raise it to critical. It is a
  confidently wrong scientific answer delivered to a chemist with no signal of its wrongness, on a
  path any drug-sized molecule reaches by default — high is right.

  One correction to the finding's **Fix**: its fallback option ("if the parameter must stay, send
  `top_n` through `calculation_key`'s `accepts` as a *keyed* argument") does not fix anything, and
  would make the cache worse — it would let two rows exist for one calculation while still storing
  a truncated payload under each. The finding's primary fix (delete the truncation and the
  parameter from the server; the caller already owns presentation and re-slices at
  `connectors/calc/server/tools.py:795-797`) is the only one that works.
