# Live corpus-fidelity pass

Ground truth: the published factor tables · Postgres `user=chemclaw dbname=chemclaw host=localhost port=5432`
· 6.7s

| dataset | published | seeded | mapped | refused |
| --- | ---: | ---: | ---: | ---: |
| bh_amination_hte | 3955 | 3955 | 3955 | 0 |
| suzuki_miyaura_flow_hte | 5760 | 5760 | 0 | 5760 |
| santanilla_amidation_screen | 96 | 96 | 96 | 0 |
| santanilla_sulfonamidation_screen | 96 | 96 | 96 | 0 |
| nielsen_deoxyfluorination | 80 | 80 | 80 | 0 |

| check | result | observed |
| --- | --- | --- |
| seeding faithful · bh_amination_hte.csv | PASS | 3955 published, 3955 seeded, 0 missing, 0 unpublished |
| seeding faithful · suzuki_miyaura_flow_hte.csv | PASS | 5760 published, 5760 seeded, 0 missing, 0 unpublished |
| seeding faithful · santanilla_amidation_screen.csv | PASS | 96 published, 96 seeded, 0 missing, 0 unpublished |
| seeding faithful · santanilla_sulfonamidation_screen.csv | PASS | 96 published, 96 seeded, 0 missing, 0 unpublished |
| seeding faithful · nielsen_deoxyfluorination.csv | PASS | 80 published, 80 seeded, 0 missing, 0 unpublished |
| zero yields survive seeding | PASS | 644 published at exactly 0.00%, 644 seeded |
| adapter matches declaration · bh_amination_hte.csv | PASS | 3955 mapped, 0 refused |
| adapter matches declaration · suzuki_miyaura_flow_hte.csv | PASS | 0 mapped, 5760 refused (declared unreachable: the source spreadsheet (Perera, Science 2018, 359, 429) publishes the second coupling partner only as its own shorthand (`2a, Boronic Acid`), so no structure exists to map. `ord_adapter._smiles` refuses it rather than inventing one, and `test_ord_compound_with_no_resolvable_identifier_is_still_refused` pins that refusal) |
| adapter matches declaration · santanilla_amidation_screen.csv | PASS | 96 mapped, 0 refused |
| adapter matches declaration · santanilla_sulfonamidation_screen.csv | PASS | 96 mapped, 0 refused |
| adapter matches declaration · nielsen_deoxyfluorination.csv | PASS | 80 mapped, 0 refused |
| adapter preserves values · bh_amination_hte.csv | PASS | 3955/3955 reactions carry their published factors and yield |
| adapter preserves values · santanilla_amidation_screen.csv | PASS | 96/96 reactions carry their published factors and yield |
| adapter preserves values · santanilla_sulfonamidation_screen.csv | PASS | 96/96 reactions carry their published factors and yield |
| adapter preserves values · nielsen_deoxyfluorination.csv | PASS | 80/80 reactions carry their published factors and yield |
| note carries the number | PASS | bh-amination-btmg-0018 states 0%: True · bh-amination-btmg-0000 states 14.02%: True |
| prose reaches the steps, and never the setpoint | **FAIL** | 0/12 procedures state a temperature and a time in prose, carry both on a step, and invent no setpoint · first miss: uspto-suzuki-biphenyl-1: headline setpoint (82.0, 4.0) was derived from prose, which D-2026-08-26 forbids |

**16/17 checks passed.**
