# D-2026-09-14-property-value-is-the-shallow-table — how many rows a real corpus produces

**Status**: accepted

## Context

`D-2026-08-25` named a volume risk and nobody measured it: `cached_compute` publishes on every miss,
and one calculation becomes many rows in the result store. `BACKLOG.md` carried the ask —
count rows-per-calculation per `calc_type` — and made the open question explicit: *"whether
`property_value` needs partitioning, and on what — a partition key chosen before the row count is
known would be a guess."*

## What was measured

Every shape the shipped projectors handle, through `publish.project.project`, over the same payload
fixtures `tests/test_publish_projection.py` parametrises. **19 shapes, 184 rows, 9.7 rows per
calculation** at the fixtures' own sizes — but the fixtures are small, and the fixture count is not
the answer. The answer is the **slope**.

Re-projected with 47 items (a conformer search's own number, and about the heavy-atom count of an
ordinary drug-like molecule):

| shape | 47 items | `property_value` | `calculation_site_value` | `calculation_point_value` | `conformer` |
| --- | --- | ---: | ---: | ---: | ---: |
| `xtb.fukui` | 47 reactive sites | 8 | **329** | 0 | 0 |
| `xtb.scan` | 47 scan points | 4 | 0 | **94** | 0 |
| `xtb.conformers` | 47 conformers | 6 | 0 | 0 | **47** |

So the per-item laws are **7 rows per reactive site**, **2 per scan point**, **1 per conformer**.
The steepest is not the one the risk was filed about: a conformer search is 1 row per conformer, and
a site-reactivity panel is seven — a 100-atom molecule projects **710** rows from one calculation.

## The open question answers itself

**`property_value` does not grow with a calculation's size at all.** A 1-site and a 47-site Fukui
panel write the same number of `property_value` rows; the 321-row difference is entirely
`calculation_site_value`. `property_value` is per *result* — 1 to 19 rows, whatever the calculation.

So the partition key that was going to be chosen for `property_value` would have been chosen for the
shallow table. The tables that grow are `calculation_site_value`, `calculation_point_value` and
`conformer`, all three keyed by the calculation rather than by the property — which means the
partition key, if one is ever needed, is the **calculation** (or its time), and it is the same key
for all three.

**Partitioning is not recommended now**, and that is a measurement rather than caution: the growth
is per calculation, not per corpus-wide dimension, so an ordinary index on `calculation_id` serves
every query these tables exist for. The number that would change it is the calculation count, which
no deployment has yet.

## Consequences

- The projector's per-item slope is now a test, not a property of the fixture sizes. A projector
  that starts emitting one more fact per item multiplies the store by the item count — a change
  that looks like one line and is not.
- `publishing` stays off by default (`CHEMCLAW_RESULT_SINKS`), unchanged by this.

## What keeps it true

- `tests/test_publish_projection.py::test_a_result_projects_a_fixed_number_of_rows_per_item` — the
  three slopes, asserted at 1 item against 47. Driven: emitting one extra `SiteFact` per site
  reddens it.
- `tests/test_publish_projection.py::test_property_value_does_not_grow_with_the_size_of_a_calculation`
  — the claim the partitioning decision rests on, in the other direction.
