---
id: hazard-rule-recall
metrics: [hazard_flag_recall]
output:
  screened_smiles:
    - "CCCN=[N+]=[N-]"
    - "CC(=O)N=[N+]=[N-]"
    - "CC(=[N+]=[N-])C(=O)OC"
    - "c1ccccc1[N+]#N"
    - "CC(C)(C)OOC(C)(C)C"
    - "CCO[N+](=O)[O-]"
    - "O=[N+]([O-])c1ccccc1[N+](=O)[O-]"
    - "OCl(=O)(=O)=O"
    - "NN"
    - "ClN1C(=O)CCC1=O"
reference:
  expected_rule_ids:
    - organic-azide
    - acyl-azide
    - diazo
    - diazonium
    - peroxide
    - nitrate-ester
    - polynitro-aromatic
    - perchlorate
    - hydrazine
    - n-halamine
---
Rule-table regression case for the structural hazard screen (D-080): one reference
molecule per rule in `safety/rules.yaml`, each a textbook example of the motif its
rule names (1-azidopropane, acetyl azide, methyl diazoacetate, benzenediazonium,
di-*tert*-butyl peroxide, ethyl nitrate, 1,2-dinitrobenzene, perchloric acid,
hydrazine, N-chlorosuccinimide).

Why this case exists: the rule table is *data* a process-safety chemist edits, and a
SMARTS that stops matching fails silently — the screen simply reports nothing, which a
reader takes as "no hazard found". Scoring recall over pinned molecules turns that
silent failure into a red `make eval`. `eval_hazard_recall_min` is 1.0: with a table
this small, one broken pattern is one whole class of hazard going unflagged.

Recall only, deliberately. Precision would need a labelled set of molecules that must
*not* flag, which is a much larger corpus to curate honestly; the false-positive side
is covered instead by unit tests in `tests/test_safety.py` pinning benign molecules
(ethanol, aspirin, ethyl acetate, nitrobenzene) that must raise nothing.
