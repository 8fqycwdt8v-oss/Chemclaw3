# D-2026-08-26-a-transcription-may-not-infer-a-setpoint — the ELN transcription tier records what the entry states, and leaves the rest absent

`D-2026-08-25-an-eln-transcription-is-data-not-a-claim` removed the PR-gate from ELN ingestion, on
a premise stated in that ADR, in `ingest/eln/records.py`, in `ingest/eln/record.py` and in
`infra/sql/052_reaction_records.sql`: *"`record_from_ord_reaction` is a pure deterministic mapping
with no model in it, so the reviewer was approving a rendering of data a chemist had already signed
off on upstream."*

That is true of `record.py`. It was false of the `OrdReaction` it is handed.

## What was actually happening

`json_adapter._condition` filled `temperature_c` and `time_h` from the **first regex match in the
whole procedure** whenever the structured field was absent. Those two values land in
`reaction_records.conditions` — the typed columns a chemist compares runs on — with nothing beside
them saying they were derived.

Measured against the real `JsonExportAdapter.map_to_ord` + `record_from_ord_reaction`, on an
entirely ordinary procedure:

> *"1. Charge the vessel and cool to 0 °C. 2. Add the acid chloride dropwise over 0.5 h. 3. Warm to
> 80 °C and stir for 12 h. 4. Quench, extract and dry over MgSO4."*

```
structured temperature_c in payload: False
record.conditions = {'temperature_c': 0.0, 'time_h': 0.5, ...}
body conditions   = ['- temperature: 0.0 °C', '- time: 0.5 h', ...]
```

A reaction run at 80 °C for 12 h, stored as a run at 0 °C for 0.5 h. It is not a parsing failure —
the regex read the prose correctly. The *addition* temperature and the *addition* time are simply
the first numbers a procedure states, because a procedure starts by charging a vessel.

Deterministic is not the same as inference-free. A chemist signed off on the prose; nobody signed
off on the number extracted from it. And since D-2026-08-25 there is no reviewer in front of the
row, so the number is queryable the moment it is written — by `condense_protocols`, by a
`since`/`until` + conditions comparison, and by whatever campaign note cites the record.

## Decision

**A transcription records what its source states.** `temperature_c` and `time_h` on `OrdReaction`
come from the structured field or are absent. The prose fallback is removed — not flagged, not
made configurable.

Nothing is lost that the entry actually carried:

- `procedure_text` keeps the prose verbatim;
- `ReactionStep.temperature_c` / `.duration_h` keep the regex result **per segment**, which is the
  scope those numbers genuinely have — "0 °C" belongs to the charging step and says so;
- a site whose ELN records the setpoint in a field maps that field, and warehouse bindings already
  do exactly this with no inference anywhere on the path.

The alternative considered and rejected was a provenance flag beside each value, rendering
`temperature: 0.0 °C (read from the procedure text)`. It keeps a number whose *value* is wrong far
more often than not — it is the addition temperature — so the flag would decorate a wrong answer
rather than withdraw it. **A missing number is a smaller harm than a wrong one stored as fact**: a
chemist reading the record sees the procedure and can read 80 °C out of it, where a stored 0 °C is
what a query returns and looks like a measurement.

## What this does not change

D-2026-08-25 stands. Its conclusion — a deterministic transcription is data and is not gated — is
correct; the fix belongs in what made the transcription non-deterministic, not in the gate. This
ADR narrows the *input* to that rule rather than reopening it, and it is the reason the rule can
keep being stated in the present tense.

The regex vocabulary stays exactly as it is (`_TEMPERATURE`, `_TIME_HOURS`, the eight-dash minus
family `D-2026-08-01` added), because `_segment_steps` still uses it. What changed is only which
field a match may be written into.

## Consequence

Four prose sites that still described a PR-gate in front of this path in the present tense are
corrected in the same commit: `json_adapter`'s two, `warehouse/adapter.py`'s module docstring, and
the `eln-databricks` manifest's `description:` and `modified_at:` comment. A stale claim that a
human reviews these rows is exactly what made this defect survive its own ADR.
