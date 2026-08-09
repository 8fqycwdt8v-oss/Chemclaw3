# D-2026-08-08-a-bundle-may-extend-a-closed-vocabulary — note types and relations are declared, not written into core

**Status:** accepted

## Context

The connector seam's claim is that a capability is a folder: `connector.yaml` declares the tools it
serves, the durable jobs it runs, the skills that teach them and the profiles it enables, and adding
one costs **zero** edits to this repository (D-118). The extensibility audit of 2026-08-08 tested
that claim by declaring a connector in a directory outside the repo and watching its tool appear on
the live agent surface. It held — with one hole.

`KNOWN_NOTE_TYPES` (`kg/note.py`) and `KNOWN_RELATIONS` (`kg/relations.py`) are frozensets in core,
and two of the eleven note types are minted by *bundles*:

```python
"job-result",    # connectors/qm/knowledge.py
"bo-candidate",  # connectors/bo/knowledge.py
```

Both had been written into core's set by hand. So a new bundle whose job declares
`publish_to_graph: true` and returns a note of a new type is a folder-only addition right up until
`make kg-validate` rejects the note — at which point it needs an edit to a file in `kg/`. Every
other bundle contribution needs none.

The closed vocabulary itself is not the problem and is not in question. A note type is a path
segment (`knowledge/<type>/<id>.md`) and a filter key, so a typo produces a note that validates and
is then invisible to every query keyed on its type. Checking the vocabulary at the PR-gate rather
than in the `Note` schema is deliberate (KNW-6, STO-8): the agent must be able to *propose* a
genuinely new type, and a human decides at the gate whether it joins.

What was wrong is that a closed set had no way to be extended **by declaration** — only by editing
the file that closes it.

## Decision

**A bundle declares the vocabulary it mints, in its own manifest.**

```yaml
note_types:
  - job-result
```

`chemclaw.kg.note.known_note_types()` returns core's set unioned with what the *enabled* bundles
declare, and `known_relations()` does the same for edges. `kg/validate.py` and the prose-contract
validator both read the union. `job-result` and `bo-candidate` moved out of core's frozensets and
into `connectors/qm/connector.yaml` and `connectors/bo/connector.yaml`.

Three properties are deliberately preserved:

- **The set stays closed.** A name that neither core nor any enabled manifest declares still fails
  `kg-validate`. Nothing became open; the door has a different key.
- **A human still sees a new type.** It arrives in the pull request that adds the bundle, reviewed
  by whoever reviews the capability it belongs to — which is a better reviewer for "should this be
  a note type?" than whoever happens to be reading `kg/note.py`.
- **Enabled, not discovered.** A bundle a deployment does not run contributes no vocabulary either,
  so a note of its type correctly fails validation there. The deployment's vocabulary is a property
  of what it runs.

Names are shape-checked at manifest load (`[a-z][a-z0-9-]*`). The door opened for extending a closed
set must not let through exactly what closing it prevented, and a name with a slash or a capital
would produce a note that validates and cannot be found.

**The connector registry is imported lazily**, inside `known_note_types()`. `chemclaw.kg` is layer 4
and `chemclaw.connectors` is layer 2/3, so a module-scope import would make the graph depend on the
capability layer at import time — for a set only two validators ever ask for. This is the same shape
and the same reason as `core.logging`'s lazy resolution of connector token names, and it is declared
alongside it in `tests/test_layering.py::_ALLOWED_LAZY_EDGES` rather than left implicit.

## Consequences

- Contributing a note type or a relation is now a bundle-local act, like every other connector
  contribution. Core's frozensets hold only what core itself mints.
- `kg -> connectors` exists as a second declared lazy edge. Two is still few enough to read; a third
  would be the moment to ask whether the vocabulary belongs somewhere else entirely.
- Three tests that asserted against `KNOWN_NOTE_TYPES` now assert against the union — including the
  seed-corpus coverage test, whose own docstring names `bo-candidate` as its example, so leaving it
  on core's half would have quietly retired the example it argues from.
- A rejected alternative: threading the extra vocabulary in as a parameter from a CLI caller. It
  avoids the layering exception and was dropped because `make kg-validate` runs
  `python -m chemclaw.kg.validate` directly — the module *is* the entry point, so the parameter
  would have had no one to pass it.
