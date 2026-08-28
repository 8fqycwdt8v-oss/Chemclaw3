# Leak probe: 300 turns in 12 batches

| series | first | last | per turn | verdict |
| --- | ---: | ---: | ---: | --- |
| RSS | 471700 | 485928 | +51.74 KB | grows and slowing — first half +2244.0, second half +352.3 KB/batch/round (± 109.8) |
| gc objects | 562925 | 583290 | +74.05 objects | flat within its own noise (slope +964.3 ± 1165.1 objects/batch/round) |

## Live objects gained per turn, by type

| type | per turn | total |
| --- | ---: | ---: |
| `dict` | +20.67 | +5684 |
| `list` | +10.79 | +2967 |
| `set` | +8.52 | +2343 |
| `cell` | +4.76 | +1309 |
| `tuple` | +4.07 | +1119 |
| `function` | +2.01 | +552 |
| `AIMessage` | +2.00 | +551 |
| `FieldInfo` | +1.96 | +538 |
| `Parameter` | +1.40 | +386 |
| `method` | +1.37 | +376 |
| `HumanMessage` | +1.00 | +276 |
| `TurnSession` | +1.00 | +275 |
