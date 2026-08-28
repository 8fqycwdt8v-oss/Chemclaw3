# Dashboards

The consumer the metrics never had. This chart declares over a hundred series; before these files
only the alerted minority had a reader of any kind and the rest had none — computed, exposed,
collected, retained, and never seen. `templates/configmap-dashboards.yaml` carries them into the cluster.

Five files, split the way the questions split, because a single sixty-panel page is one nobody opens
twice:

| File | The question it answers |
| --- | --- |
| `chemclaw-turns.json` | What are chemists asking for, how does it end, how long does it take, what does it cost |
| `chemclaw-tools-and-model.json` | Which tool is slow, which tool is failing, what is the provider seam doing |
| `chemclaw-durable.json` | Are durable jobs completing, which connector is failing, what went missing |
| `chemclaw-front-door.json` | Request rate, error ratio, per-route latency, what is refused before the handler |
| `chemclaw-data.json` | Ingest, retrieval, the calculation cache, the result outbox, the Postgres pool |

Between them every metric `chemclaw.core.metrics` declares appears on exactly one panel or in one
alert. `tests/test_deploy_chart.py::test_every_declared_metric_has_a_consumer` is what keeps that
true: a series added tomorrow with no panel and no rule fails there rather than joining the ones
that had nobody. It checks the other direction too — a panel querying a series this system does not
declare is a graph that can never draw, which looks exactly like "nothing happened".

## Two readers, two places to look

A dashboard is only a dashboard to something that reads it, and the two readers disagree:

- **The OpenShift console** reads ConfigMaps labelled `console.openshift.io/dashboard: "true"` in
  **`openshift-config-managed` and nowhere else**. Set `monitoring.dashboards.namespace` to that
  namespace (it needs cluster-admin) and they appear under Observe -> Dashboards.
- **A self-managed Grafana** with the usual sidecar reads `grafana_dashboard: "1"` in the namespace
  it watches — normally the release's own, which is the shipped default. Add that label to
  `monitoring.dashboards.labels`; both may be set at once, since different things read them.

The shipped default writes into the release namespace with the console's label, which is the
combination that installs everywhere and displays nowhere until an operator picks one. That is
deliberate: a chart whose *install* fails on a dashboard is worse than one whose dashboards need one
more `--set`, and `docs/guides/runbook.md` § "Make the monitoring stack actually collect this" is
where the choice is written down.

## Panel types

`graph` and `singlestat`, not `timeseries` and `stat`. The OpenShift console renders the classic
Grafana panel types; Grafana itself migrates them on load. Written for the reader that cannot
migrate.

## Editing one

Edit the JSON. There is no generator to re-run — the file is the artifact, and a dashboard exported
from a Grafana you have been experimenting in can be dropped in whole, provided its queries still
name series this system emits (the test above checks that direction too).
