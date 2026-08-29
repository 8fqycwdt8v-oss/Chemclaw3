# Leak probe: 1300 turns in 14 batches

| series | first | last | per turn | verdict |
| --- | ---: | ---: | ---: | --- |
| RSS | 478884 | 504668 | +20.63 KB | grows and slowing — first half +3072.0, second half +364.9 KB/batch/round (± 149.7) |
| gc objects | 563513 | 597426 | +27.13 objects | grows and steady — first half +2115.9, second half +2724.3 objects/batch/round (± 2646.1) |

## Live objects gained per turn, by type

| type | per turn | total |
| --- | ---: | ---: |
| `dict` | +8.19 | +10241 |
| `list` | +4.97 | +6214 |
| `set` | +4.03 | +5033 |
| `AIMessage` | +1.52 | +1900 |
| `cell` | +0.84 | +1052 |
| `tuple` | +0.79 | +987 |
| `TurnSession` | +0.76 | +950 |
| `ToolMessage` | +0.76 | +950 |
| `LiveSession` | +0.76 | +950 |
| `HumanMessage` | +0.76 | +950 |
| `FieldInfo` | +0.43 | +538 |
| `function` | +0.35 | +438 |
