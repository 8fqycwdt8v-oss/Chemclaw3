# Vendored benchmark data — attribution

`reizman_suzuki_case_1.csv` is vendored unmodified from the **Summit** project
(https://github.com/sustainable-processes/summit), which is MIT-licensed. It is
included here because Summit itself does not support Python 3.11; only the data is
reused (the surrogate and BO loop are ours).

The data originates from:

> Reizman, B. J.; Wang, Y.-M.; Buchwald, S. L.; Jensen, K. F.
> "Suzuki–Miyaura cross-coupling optimization enabled by automated feedback."
> *Reactions Chemistry & Engineering*, 2016, 1, 658–666.
> https://doi.org/10.1039/C6RE00153J

Columns: `catalyst` (ligand/pre-catalyst, categorical), `t_res` (residence time, s),
`temperature` (°C), `catalyst_loading` (mol %), `ton` (turnover number),
`yld` (yield, %). The `TYPE` row is Summit's metadata header and is dropped on load.
