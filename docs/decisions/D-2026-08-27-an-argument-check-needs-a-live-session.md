# D-2026-08-27-an-argument-check-needs-a-live-session — the arguments of a bundle we do not run are checked on the live lane, not in `ci`

**Status:** accepted
**Context:** the `BACKLOG` row "a pinned template's arguments go unchecked once its bundle stops being ours"

## Context

A step template is a *pinned* procedure, so an argument its tool does not take is not a typo — it
is a run that launches, spends compute, and fails on step four inside an activity. That is why
`make template-validate` checks argument *keys* as well as tool names, and why the 2026-08-08
review called the name-only check "half a reference".

The check reads a tool's parameters out of this tree: the in-process `@tool` registry, and each
bundle's own `connectors/<name>/server/tools.py`. `D-2026-08-15-capability-moves-judgment-and-declaration-stay`
moved `chem` and `safety` to `Chemclaw3-mcp`, leaving the manifests here and the servers there — so
those bundles have no local module and no signature to read. The gate skips them, and reports the
skip by name (`unchecked_arguments`), which is what made this a backlog row rather than a defect.

Measured on this checkout, that is **seven shipped tool steps across six templates**: `chem`'s five
enumerations and `safety`'s two `screen_hazards` calls.

The gap is real, not theoretical. Copying `data/templates/` to a scratch directory and misspelling
`hazard-briefing`'s `smiles` as `smilez` with `nonexistent_arg: 42` beside it:

```
$ CHEMCLAW_TEMPLATES_DIR=/tmp/tpl make template-validate
note: template 'hazard-briefing' names ['screen_hazards'], whose bundle is declared but not run
      here — name-checked, arguments unchecked
template validation passed.                                   # exit 0
```

## Decision

**The check belongs on the live lane, because the only authority for these tools is a running
server.** `make live-template-args`
(`chemclaw.cli.validate_template_args_live`) opens real connector sessions through
`connectors.registry.open_connector_specs` — the same function every turn uses — and checks the
same argument rule against the `args_schema` each server advertised. The same scratch tree, against
the fleet's real `safety` server:

```
$ CHEMCLAW_TEMPLATES_DIR=/tmp/tpl make live-template-args
live template argument validation failed:
  - template 'hazard-briefing' step 'hazards' passes argument(s) ['nonexistent_arg', 'smilez']
    that 'screen_hazards' does not take; it accepts: ['smiles']
  - template 'hazard-briefing' step 'hazards' omits required argument(s) ['smiles'] of
    'screen_hazards'                                          # exit 1
```

and on the shipped templates with `chem`, `safety`, `molfp` and `calc` all up: `9 step(s) checked`,
exit 0.

Three things this decision fixes in place, each of which was a way to get a green line for free:

1. **The row's own proposed location is wrong, and it is worth saying why.** It named
   `make connector-validate`. That target has no live session either — it imports the bundle's
   *local* module and therefore returns `[]` for exactly the bundles in question — and it runs
   inside `ci`, which must stay offline. A gate that needs a network inside `ci` goes red for
   reasons that are not about the diff, and a gate that answers `[]` where it cannot see is the
   vacuous pass this whole area keeps producing.

2. **`make template-validate` does not move, and keeps its note.** The live lane is not run on a
   diff, so what the offline gate did not check is still something its reader has to be told. The
   note now points at the target that does check it.

3. **One rule, two authorities.** `ToolArguments` and `argument_problems` are extracted in
   `cli/validate_templates.py` and imported by the live module, so the two lanes give the same
   verdict in the same words and differ only in where they read a tool's arguments from — an
   `inspect.Signature` offline, a `tool_call_schema` live. `tests/test_templates.py` pins the two
   constructors against one function and its served form, because a disagreement there would be a
   real defect and a silent one.

**An unreached connector is reported, never counted as checked.** `LiveReport` has three parts —
`checked`, `problems`, `unreached` — and `main` gives "could not reach something" its own exit code
(3, distinct from 1 for a mismatch and from `argparse`'s 2) and withholds the green line entirely.
This is `D-2026-08-17-a-harness-that-starts-two-of-five-servers-is-a-harness-that-tests-two`
applied as a return value rather than as a lesson: that harness started two of five servers and
printed one green line. Observed here on the first run, with only the fleet's two servers up:

```
checked degradant-triage/hazards -> screen_hazards (safety)          # ...7 of these
UNREACHED: connector 'calc' did not come up — 1 template step(s) were NOT checked:
  - conformer-refinement/refine -> compute_thermochemistry
UNREACHED: connector 'molfp' did not come up — 1 template step(s) were NOT checked:
  - hazard-briefing/precedent -> similar_molecules
live template argument validation INCOMPLETE: 7 step(s) checked, 2 unreached   # exit 3
```

A run that checked nothing at all takes the same exit for the same reason.

## Consequences

- The seven steps the offline note names are checked by something. All seven pass today, which is
  a fact nobody had; the value of the target is the next edit to one of those templates, or the
  next change to a tool's signature on the other side of the seam.
- The live check sees one thing no offline gate can: a tool a manifest **declares** and the running
  server does not serve. Both `connector.yaml` files say in prose that their tool list exists in two
  repositories with nothing forcing agreement, and that only a running server settles it. That is
  now a reported problem rather than a sentence.
- It only opens the connectors some template step actually names. Opening the whole enabled set
  would pay a connect timeout per connector this check has no question for and, worse, would report
  those as `unreached` — turning an honest coverage signal into noise nobody reads.
- A credential mismatch surfaces as `unreached`, not as a pass: the session never opens, so the
  connector contributes no tools and every check against it would otherwise have been vacuous. That
  is defect 2 of D-2026-08-17 arriving in the one place that can distinguish it from a clean run.

## Alternatives considered

**Vendor the fleet's `connector.yaml` tool schemas into this tree.** It would make the check
offline, and it would be a third copy of a declaration that already exists in two repositories with
nothing forcing agreement — a copy that goes stale exactly when the thing it describes changes,
which is the only moment the check matters.

**Have `make template-validate` fall back to a live session when it can reach one.** Rejected: a
gate whose coverage depends on what happens to be listening is a gate whose green line means
different things on different machines, and `ci` would silently check less than a developer's box.
Two targets that each say plainly what they checked is the smaller cost.

**Fail `make template-validate` on an unresolvable tool.** Rejected when the note was introduced and
still wrong: the template is correct, nothing here can prove it, and failing would force deleting a
good template to make a validator pass.
