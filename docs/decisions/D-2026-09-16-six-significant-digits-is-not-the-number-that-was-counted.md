# D-2026-09-16-six-significant-digits-is-not-the-number-that-was-counted — one sample formatter, because `:g` was wrong twice and only one of them was visible

A dependency audit of this tree went looking for hand-written code a library already does better, and
found `core/metrics.py`'s Prometheus renderer with the argument against `prometheus_client` written
in its own docstring. That argument is a separate decision and is taken separately. This ADR is about
what reading the renderer found on the way: **every numeric sample it emitted went through `:g`, and
`:g` is wrong in two independent ways.**

## What was measured

Run directly, on this interpreter:

| value | `format(value, "g")` | what the text format requires |
| --- | --- | --- |
| `float("inf")` | `inf` | `+Inf` |
| `float("-inf")` | `-inf` | `-Inf` |
| `float("nan")` | `nan` | `NaN` |
| `1234567` | `1.23457e+06` | `1234567` |
| `1234567.25` | `1.23457e+06` | `1234567.25` |
| `12345678901234` | `1.23457e+13` | `12345678901234` |

**The loud half** is the first three rows. A bound gauge computing a ratio meets a zero denominator
eventually, and the sample it then emits is a token Prometheus rejects. A rejected sample does not
fail alone — it fails the *scrape*, so one unreadable ratio loses every metric this pod has, at
exactly the moment somebody is looking for them. That the histogram path already spelled `le="+Inf"`
correctly is what made this read as a convention rather than as an inconsistency inside one renderer.

**The quiet half is the one worth the ADR**, because nothing would ever have reported it. `:g` carries
six significant digits. A counter at 1,234,567 rendered as `1.23457e+06` — well-formed, accepted,
stored, graphed, and **wrong by three**. Every counter in this process was exact until its millionth
observation and silently rounded afterwards. `chemclaw_tool_calls_total` on the shipped fleet crosses
a million in days, and a histogram `_sum` of seconds crosses it in about eleven days of busy tool
calls, after which every `rate()` reads a rounded numerator against an exact denominator.

## The decision

`_sample(value)` is the one function every numeric emission in `render()` goes through: `int` rendered
exactly rather than through float at all, the three non-finite floats spelled as the format spells
them, and every other float through `repr`, which is the shortest string that round-trips.

**`le` is deliberately left on `:g`, and the asymmetry is asserted rather than commented.** `le` is a
*label*, so its rendered text is part of the series identity: re-spelling `3600` as `3600.0` would
mint a new series beside the old one on every dashboard and recording rule already reading these
histograms. Neither reason `_sample` exists reaches a bucket boundary — every value in
`_HISTOGRAM_BUCKETS` is a small human-chosen number well inside six digits, and `+Inf` is spelled
literally rather than formatted. A later sweep "finishing the job" now has to argue with a test.

## What this is not

It is not the `prometheus_client` question. The library would have made both defects impossible, and
that is an argument on the other side of a decision this ADR does not take — but it is worth
recording here that the defects are the kind a maintained exposition library does not have, and that
the refusal on record (`D-084`, and the module docstring) rests on a line count that has since moved
by 3.5x and on a "counters and gauges only, no labels" premise the module has outgrown.

## What keeps it true

- `tests/test_metrics.py::test_a_non_finite_gauge_reading_renders_as_prometheus_spells_it`
- `tests/test_metrics.py::test_a_counter_past_a_million_is_rendered_exactly`
- `tests/test_metrics.py::test_a_histogram_sum_keeps_the_precision_its_observations_had`
- `tests/test_metrics.py::test_the_bucket_boundary_label_is_left_alone_because_it_is_a_series_identity`
