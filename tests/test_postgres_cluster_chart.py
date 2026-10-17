"""The Postgres chart (`deploy/helm/chemclaw-postgres`): a CloudNativePG Cluster with WAL archiving.

Rendered with `helm` (skipped, named and counted, without it) and read as YAML. The claims under
test: the objects agree with each other and with the application chart's connection budget; the
three ways a cluster comes into being (empty, imported from a live database, restored from an
archive) render what their runbook procedures say; nothing is deleted with the release; the chart
refuses a backup nobody has stated the targets of; and the backup alerts read only their own
cluster, take every threshold from a value and fire on the failure they name, evaluated by
`promtool`. Schema validity is `make helm-validate`'s, against the CRD catalogue.
"""

import re
import shutil
import subprocess
import tempfile
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_CHART = _ROOT / "deploy" / "helm" / "chemclaw-postgres"
_VALUES: dict[str, Any] = yaml.safe_load((_CHART / "values.yaml").read_text(encoding="utf-8"))
_APP_VALUES: dict[str, Any] = yaml.safe_load(
    (_ROOT / "deploy" / "helm" / "chemclaw" / "values.yaml").read_text(encoding="utf-8")
)

_HELM = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")

_REQUIRED = (
    "imageName=registry.example/pgvector:16",
    "backup.destinationPath=s3://chemclaw-backups/prod",
    "targets.rpoSeconds=900",
    "targets.rtoSeconds=14400",
)
_IMPORT = (
    "import.enabled=true",
    "import.source.host=old-pg.example",
    "import.source.user=chemclaw",
    "import.source.passwordSecret.name=old-pg",
)
_RECOVERY = (
    "name=chemclaw-pg-r1",
    "recovery.enabled=true",
    "recovery.sourceClusterName=chemclaw-pg",
    "recovery.targetTime=2026-10-14 09:41:00+00",
)


@cache
def _render(*sets: str) -> subprocess.CompletedProcess[str]:
    """`helm template chemclaw-pg` with `sets` (a `--set` each)."""
    command = ["helm", "template", "chemclaw-pg", str(_CHART)]
    for value in sets:
        command += ["--set", value]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _documents(*sets: str) -> dict[tuple[str, str], dict[str, Any]]:
    """The rendered objects keyed by (kind, name)."""
    result = _render(*_REQUIRED, *sets)
    assert result.returncode == 0, result.stderr
    return {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in yaml.safe_load_all(result.stdout)
        if doc
    }


def test_nothing_only_the_site_knows_has_a_default() -> None:
    """The image, the bucket and both recovery targets are stated, never inherited."""
    assert _VALUES["imageName"] == ""
    assert _VALUES["backup"]["destinationPath"] == ""
    assert _VALUES["targets"] == {"rpoSeconds": 0, "rtoSeconds": 0}
    assert _VALUES["import"]["enabled"] is False
    assert _VALUES["recovery"]["enabled"] is False
    assert _VALUES["temporalDatabases"]["enabled"] is False


def test_the_connection_budget_is_the_application_charts() -> None:
    """The server must serve what the application declares it opens (`postgres.maxConnections`)."""
    assert _VALUES["connections"]["fleet"] == _APP_VALUES["postgres"]["maxConnections"]


@_HELM
def test_the_objects_agree_with_each_other_and_with_the_values_that_size_them() -> None:
    documents = _documents()
    assert {kind for kind, _ in documents} == {
        "Cluster",
        "ObjectStore",
        "ScheduledBackup",
        "PodMonitor",
        "PrometheusRule",
    }
    cluster = documents[("Cluster", "chemclaw-pg")]["spec"]
    store = documents[("ObjectStore", "chemclaw-pg-backup")]["spec"]
    scheduled = documents[("ScheduledBackup", "chemclaw-pg-base")]["spec"]

    (plugin,) = cluster["plugins"]
    assert plugin["isWALArchiver"] is True
    assert plugin["parameters"]["barmanObjectName"] == "chemclaw-pg-backup"
    assert scheduled["cluster"]["name"] == "chemclaw-pg"
    assert scheduled["method"] == "plugin"
    assert scheduled["pluginConfiguration"]["name"] == plugin["name"]

    parameters = cluster["postgresql"]["parameters"]
    budget = _VALUES["connections"]
    assert parameters["max_connections"] == str(budget["fleet"] + budget["headroom"])
    assert parameters["archive_timeout"] == f"{_VALUES['archiveTimeoutSeconds']}s"
    assert cluster["bootstrap"]["initdb"]["postInitApplicationSQL"] == [
        "CREATE EXTENSION IF NOT EXISTS vector"
    ]
    assert cluster["walStorage"]["size"] == _VALUES["walStorage"]["size"]
    assert cluster["inheritedMetadata"]["labels"] == {"chemclaw/postgres": "true"}

    assert store["retentionPolicy"] == _VALUES["backup"]["retentionPolicy"]
    assert store["configuration"]["wal"]["compression"] == "gzip"
    assert store["configuration"]["s3Credentials"] == {"inheritFromIAMRole": True}
    annotations = documents[("Cluster", "chemclaw-pg")]["metadata"]["annotations"]
    assert annotations["chemclaw/rpo-seconds"] == "900"
    assert annotations["chemclaw/rto-seconds"] == "14400"


@_HELM
def test_nothing_this_chart_renders_is_deleted_with_the_release() -> None:
    """Uninstall, rollback and an upgrade that stops rendering an object spare the database."""
    documents = _documents(
        "temporalDatabases.enabled=true", "temporalDatabases.passwordSecret=temporal-db"
    )
    assert {kind for kind, _ in documents} == {
        "Cluster",
        "ObjectStore",
        "ScheduledBackup",
        "Database",
        "PodMonitor",
        "PrometheusRule",
    }
    for key, document in documents.items():
        policy = document["metadata"]["annotations"].get("helm.sh/resource-policy")
        assert policy == "keep", f"{key} would be deleted with the release"


@_HELM
def test_credentials_encryption_tls_and_an_operator_override_reach_the_objects() -> None:
    documents = _documents(
        "backup.credentialsSecret=backup-keys",
        "backup.encryption=AES256",
        "backup.endpointURL=https://objects.example:9000",
        "tls.serverCASecret=pg-ca",
        "tls.serverTLSSecret=pg-tls",
        "parameters.max_connections=500",
    )
    configuration = documents[("ObjectStore", "chemclaw-pg-backup")]["spec"]["configuration"]
    assert configuration["s3Credentials"]["accessKeyId"]["name"] == "backup-keys"
    assert configuration["wal"]["encryption"] == configuration["data"]["encryption"] == "AES256"
    assert configuration["endpointURL"] == "https://objects.example:9000"
    cluster = documents[("Cluster", "chemclaw-pg")]["spec"]
    assert cluster["postgresql"]["parameters"]["max_connections"] == "500"
    assert cluster["certificates"] == {"serverCASecret": "pg-ca", "serverTLSSecret": "pg-tls"}


@_HELM
def test_temporals_databases_ride_the_same_cluster_under_one_role() -> None:
    documents = _documents(
        "temporalDatabases.enabled=true", "temporalDatabases.passwordSecret=temporal-db"
    )
    databases = {n: d["spec"] for (k, n), d in documents.items() if k == "Database"}
    assert {d["name"] for d in databases.values()} == {"temporal", "temporal_visibility"}
    assert {d["cluster"]["name"] for d in databases.values()} == {"chemclaw-pg"}
    (role,) = documents[("Cluster", "chemclaw-pg")]["spec"]["managed"]["roles"]
    assert {d["owner"] for d in databases.values()} == {role["name"]}
    assert role["passwordSecret"] == {"name": "temporal-db"}


@_HELM
def test_an_existing_database_is_carried_over_by_import_and_not_initialised_empty() -> None:
    """The path for a release already running on another Postgres: copy it, do not start empty."""
    documents = _documents(*_IMPORT)
    cluster = documents[("Cluster", "chemclaw-pg")]["spec"]
    initdb = cluster["bootstrap"]["initdb"]
    assert initdb["import"]["type"] == "microservice"
    assert initdb["import"]["databases"] == [_VALUES["database"]]
    assert initdb["import"]["source"] == {"externalCluster": "source-database"}
    assert "postInitApplicationSQL" not in initdb
    (source,) = cluster["externalClusters"]
    assert source["name"] == "source-database"
    assert source["connectionParameters"]["host"] == "old-pg.example"
    assert source["password"] == {"name": "old-pg", "key": "password"}


@_HELM
def test_a_monolith_import_names_every_database_and_role_to_carry() -> None:
    documents = _documents(
        *_IMPORT,
        "import.type=monolith",
        "import.databases={chemclaw,temporal,temporal_visibility}",
        "import.roles={temporal}",
    )
    imported = documents[("Cluster", "chemclaw-pg")]["spec"]["bootstrap"]["initdb"]["import"]
    assert imported["type"] == "monolith"
    assert imported["databases"] == ["chemclaw", "temporal", "temporal_visibility"]
    assert imported["roles"] == ["temporal"]
    # The operator's admission webhook refuses `postImportApplicationSQL` for a monolith import
    # (pg_restore creates the extension), and a monolith connects through the `postgres` database.
    assert "postImportApplicationSQL" not in imported
    source = documents[("Cluster", "chemclaw-pg")]["spec"]["externalClusters"][0]
    assert source["connectionParameters"]["dbname"] == "postgres"


@_HELM
def test_a_microservice_import_names_the_source_database_it_copies() -> None:
    """`import.source.database` is the database copied (the destination keeps `database`'s name)."""
    documents = _documents(*_IMPORT, "import.source.database=legacy_chemclaw")
    cluster = documents[("Cluster", "chemclaw-pg")]["spec"]
    imported = cluster["bootstrap"]["initdb"]["import"]
    assert imported["databases"] == ["legacy_chemclaw"]
    assert cluster["bootstrap"]["initdb"]["database"] == _VALUES["database"]
    assert cluster["externalClusters"][0]["connectionParameters"]["dbname"] == "legacy_chemclaw"
    assert imported["postImportApplicationSQL"] == ["CREATE EXTENSION IF NOT EXISTS vector"]


@_HELM
def test_a_pull_secret_reaches_the_cluster() -> None:
    cluster = _documents("imagePullSecrets={registry-login}")[("Cluster", "chemclaw-pg")]["spec"]
    assert cluster["imagePullSecrets"] == [{"name": "registry-login"}]


@_HELM
def test_a_restore_is_a_new_cluster_that_archives_under_its_own_name() -> None:
    """The cutover: a release with a new name reads the old archive and writes its own."""
    documents = _documents(*_RECOVERY)
    cluster = documents[("Cluster", "chemclaw-pg-r1")]["spec"]
    recovery = cluster["bootstrap"]["recovery"]
    assert "initdb" not in cluster["bootstrap"]
    assert recovery["database"] == _VALUES["database"] and recovery["owner"] == _VALUES["owner"]
    assert recovery["recoveryTarget"] == {"targetTime": "2026-10-14 09:41:00+00"}
    (origin,) = cluster["externalClusters"]
    assert origin["plugin"]["parameters"]["serverName"] == "chemclaw-pg"
    # Its own archive and its own base backups, so the restored cluster is recoverable too.
    (plugin,) = cluster["plugins"]
    assert plugin["parameters"]["barmanObjectName"] == "chemclaw-pg-r1-backup"
    assert "serverName" not in plugin["parameters"]
    assert documents[("ScheduledBackup", "chemclaw-pg-r1-base")]["spec"]["immediate"] is True
    # The same connection budget as the original, so the server starts as it was backed up.
    original = _documents()[("Cluster", "chemclaw-pg")]["spec"]["postgresql"]["parameters"]
    assert cluster["postgresql"]["parameters"]["max_connections"] == original["max_connections"]
    # The label the application's NetworkPolicy peer names does not change with the cluster.
    assert (
        cluster["inheritedMetadata"]
        == _documents()[("Cluster", "chemclaw-pg")]["spec"]["inheritedMetadata"]
    )


@_HELM
def test_a_rehearsal_restores_and_writes_nothing_into_the_bucket() -> None:
    """The scratch-namespace restore reads the archive and neither archives nor takes backups."""
    documents = _documents(
        "name=pg-rehearsal",
        "recovery.enabled=true",
        "recovery.rehearsal=true",
        "recovery.sourceClusterName=chemclaw-pg",
        "instances=1",
    )
    assert {kind for kind, _ in documents} == {"Cluster", "ObjectStore"}
    cluster = documents[("Cluster", "pg-rehearsal")]["spec"]
    assert "plugins" not in cluster
    assert cluster["bootstrap"]["recovery"]["source"] == "origin"
    assert cluster["externalClusters"][0]["plugin"]["parameters"]["serverName"] == "chemclaw-pg"


@_HELM
@pytest.mark.parametrize(
    ("what", "sets", "refusal"),
    [
        ("image", ["imageName="], "carry no pgvector"),
        ("bucket", ["backup.destinationPath=gs://bucket/x"], "must be an s3:// URL"),
        ("rpo", ["targets.rpoSeconds=0"], "ships no default"),
        ("rto", ["targets.rtoSeconds=0"], "ships no default"),
        ("rpo below the interval", ["targets.rpoSeconds=60"], "cannot be met"),
        (
            "archive_timeout set beside the interval",
            ["parameters.archive_timeout=1h", "targets.rpoSeconds=1000"],
            "parameters.archive_timeout is refused",
        ),
        ("retention words", ["backup.retentionPolicy=forever"], "is not a Barman retention"),
        ("retention zero", ["backup.retentionPolicy=0d"], "is not a Barman retention"),
        ("retention leading zero", ["backup.retentionPolicy=07d"], "is not a Barman retention"),
        ("encryption", ["backup.encryption=rot13"], "AES256 or aws:kms"),
        ("instances", ["instances=0"], "at least 1"),
        ("temporal role", ["temporalDatabases.enabled=true"], "passwordSecret is empty"),
        ("import without a source", ["import.enabled=true"], "import.source.host"),
        (
            "import and recovery",
            [*_IMPORT, *_RECOVERY],
            "not both",
        ),
        (
            "monolith without databases",
            [*_IMPORT, "import.type=monolith"],
            "needs import.databases",
        ),
        (
            "roles with a microservice import",
            [*_IMPORT, "import.roles={temporal}"],
            "import.roles is refused",
        ),
        (
            "two databases in a microservice import",
            [*_IMPORT, "import.databases={chemclaw,temporal}"],
            "more than one database",
        ),
        (
            "a server certificate without its CA",
            ["tls.serverTLSSecret=cert"],
            "needs tls.serverCASecret",
        ),
        ("a zero archive interval", ["archiveTimeoutSeconds=0"], "at least 1"),
        ("a negative archive interval", ["archiveTimeoutSeconds=-5"], "at least 1"),
        ("a non-numeric archive interval", ["archiveTimeoutSeconds=abc"], "at least 1"),
        ("recovery without a source", ["recovery.enabled=true"], "recovery.sourceClusterName"),
        ("rehearsal without recovery", ["recovery.rehearsal=true"], "does not archive"),
        (
            "a zero alert wait",
            ["monitoring.alerts.archiveFailingSeconds=0"],
            "monitoring.alerts.archiveFailingSeconds 0 must be a whole number of seconds",
        ),
        (
            "a fractional backup age",
            ["monitoring.alerts.baseBackupMaxAgeSeconds=1.5"],
            "monitoring.alerts.baseBackupMaxAgeSeconds 1.5 must be a whole number of seconds",
        ),
        (
            "restore over itself",
            ["name=chemclaw-pg", "recovery.enabled=true", "recovery.sourceClusterName=chemclaw-pg"],
            "own name",
        ),
    ],
)
def test_the_chart_refuses_a_backup_it_cannot_make_sense_of(
    what: str, sets: list[str], refusal: str
) -> None:
    # `_REQUIRED` first so a later value in `sets` overrides it (`--set` is last-wins).
    result = _render(*_REQUIRED, *sets)
    assert result.returncode != 0, f"{what}: rendered"
    assert refusal in " ".join(result.stderr.split())


# --- watching the backup -------------------------------------------------------------------------

_PROMTOOL = pytest.mark.skipif(
    shutil.which("helm") is None or shutil.which("promtool") is None,
    reason="helm is not installed (or promtool is): both render and evaluate the rule",
)

#: What the PodMonitor stamps on every sample of the release's first instance (`helm template` with
#: no `--namespace` renders into `default`).
_PRIMARY = 'namespace="default",pod="chemclaw-pg-1"'

_ARCHIVED = f"cnpg_pg_stat_archiver_last_archived_time{{{_PRIMARY}}}"
_FAILED = f"cnpg_pg_stat_archiver_last_failed_time{{{_PRIMARY}}}"
_READY = f'cnpg_collector_pg_wal_archive_status{{{_PRIMARY},value="ready"}}'
_UP = f"cnpg_collector_up{{{_PRIMARY}}}"
_BACKUP = "barman_cloud_cloudnative_pg_io_"
_AVAILABLE = f"{_BACKUP}last_available_backup_timestamp{{{_PRIMARY}}}"
_BACKUP_FAILED = f"{_BACKUP}last_failed_backup_timestamp{{{_PRIMARY}}}"
_RECOVERABLE = f"{_BACKUP}first_recoverability_point{{{_PRIMARY}}}"


def _rules(*sets: str) -> dict[str, dict[str, Any]]:
    """The rendered backup alerts by name, from the one PrometheusRule the chart renders."""
    (rule,) = [doc for (kind, _), doc in _documents(*sets).items() if kind == "PrometheusRule"]
    return {r["alert"]: r for group in rule["spec"]["groups"] for r in group["rules"]}


@_HELM
@pytest.mark.parametrize(("name", "sets"), [("chemclaw-pg", ()), ("chemclaw-pg-r1", _RECOVERY)])
def test_the_backup_is_scraped_and_alerted_on_under_the_clusters_own_name(
    name: str, sets: tuple[str, ...]
) -> None:
    """Each cluster is collected by its own label and every alert reads only its own instances.

    A restore runs beside the original until it is decommissioned, so a selector that also matched
    the other cluster's pods would let one cluster's healthy archive hide the other's stopped one.
    """
    documents = _documents(*sets)
    monitor = documents[("PodMonitor", name)]["spec"]
    assert monitor["selector"] == {"matchLabels": {"cnpg.io/cluster": name}}
    assert [endpoint["port"] for endpoint in monitor["podMetricsEndpoints"]] == ["metrics"]
    rules = _rules(*sets)
    assert rules, "the chart renders no backup alert"
    for alert, rule in rules.items():
        selectors = re.findall(r"\{([^}]*)\}", rule["expr"])
        assert selectors, f"{alert} reads no series"
        for selector in selectors:
            assert f'namespace="default",pod=~"{name}-[0-9]+"' in selector, (
                f"{alert} reads a series outside {name}'s instances: {{{selector}}}"
            )
        assert rule["labels"]["cnpg_cluster"] == name
    # The selector is anchored, so the original's pattern cannot match a restored cluster's pods.
    assert not re.fullmatch("chemclaw-pg-[0-9]+", "chemclaw-pg-r1-1")


@_HELM
def test_every_backup_threshold_is_the_value_that_states_it() -> None:
    """Moving a value moves the alert: nothing in the rules is a literal of its own."""
    rules = _rules(
        "targets.rpoSeconds=1234",
        "monitoring.alerts.archiveFailingSeconds=101",
        "monitoring.alerts.archiveBacklogSeconds=102",
        "monitoring.alerts.baseBackupMaxAgeSeconds=103",
        "monitoring.alerts.firstBackupGraceSeconds=104",
        "monitoring.alerts.metricsAbsentSeconds=105",
    )
    assert rules["ChemclawWalArchivingFailing"]["for"] == "101s"
    assert rules["ChemclawRecoveryPointObjectiveMissed"]["for"] == "102s"
    assert "> 1234" in rules["ChemclawRecoveryPointObjectiveMissed"]["expr"]
    assert "> 103" in rules["ChemclawBaseBackupTooOld"]["expr"]
    assert rules["ChemclawNoRecoveryPoint"]["for"] == "104s"
    assert rules["ChemclawBackupMonitoringBlind"]["for"] == "105s"


def test_the_default_backup_age_outlasts_the_default_schedule() -> None:
    """The age a backup may reach is longer than one period of the schedule it is checked against.

    Read off the default schedule rather than restated: six fields, seconds first, and a fixed time
    with every day, month and weekday is one backup a day.
    """
    fields = _VALUES["backup"]["schedule"].split()
    assert len(fields) == 6 and all(f.isdigit() for f in fields[:3]) and fields[3:] == ["*"] * 3, (
        f"the default schedule {fields} is no longer once a day; rederive the period here"
    )
    period = 24 * 60 * 60
    assert _VALUES["monitoring"]["alerts"]["baseBackupMaxAgeSeconds"] > period


@_HELM
def test_a_rehearsal_and_a_site_that_scrapes_itself_render_what_they_need() -> None:
    """A rehearsal archives nothing, so it renders no alert; the PodMonitor is the site's choice."""
    rehearsal = _documents(
        "name=pg-rehearsal",
        "recovery.enabled=true",
        "recovery.rehearsal=true",
        "recovery.sourceClusterName=chemclaw-pg",
    )
    assert not {kind for kind, _ in rehearsal} & {"PodMonitor", "PrometheusRule"}
    scraped_elsewhere = {kind for kind, _ in _documents("monitoring.podMonitor.enabled=false")}
    assert "PrometheusRule" in scraped_elsewhere and "PodMonitor" not in scraped_elsewhere


def _evaluate(cases: list[dict[str, Any]]) -> subprocess.CompletedProcess[str]:
    """`promtool test rules` over the rendered backup alerts.

    Rules are rebuilt with only `alert`, `expr` and `for`, since a unit test matches labels and
    annotations exactly; each case's expected labels are therefore the series' own.
    """
    rules = [{key: rule[key] for key in ("alert", "expr", "for")} for rule in _rules().values()]
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        (work / "rules.yaml").write_text(
            yaml.safe_dump({"groups": [{"name": "unit", "rules": rules}]})
        )
        (work / "test.yaml").write_text(
            yaml.safe_dump(
                {"rule_files": ["rules.yaml"], "evaluation_interval": "1m", "tests": cases}
            )
        )
        return subprocess.run(
            ["promtool", "test", "rules", "test.yaml"],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
        )


def _case(series: dict[str, str], alert: str, expected: dict[str, list[dict[str, str]]]) -> Any:
    """One promtool case: `series` as given, `alert` checked against its labels at each time."""
    return {
        "interval": "1m",
        "input_series": [{"series": name, "values": values} for name, values in series.items()],
        "alert_rule_test": [
            {
                "eval_time": at,
                "alertname": alert,
                "exp_alerts": [{"exp_labels": labels} for labels in fired],
            }
            for at, fired in expected.items()
        ],
    }


@_PROMTOOL
def test_each_backup_alert_fires_on_its_failure_and_not_on_a_healthy_or_quiet_cluster() -> None:
    """Each alert against its failure, a healthy cluster and, where they differ, a quiet one.

    Evaluated with the shipped defaults (RPO 900 s from `_REQUIRED`, the waits in `values.yaml`), so
    the times below are those values: 600 s failing, 300 s over the RPO, 36 h without a backup, 6 h
    without a recoverability point, 15 min without the series. `time()` starts at 0, so a timestamp
    of 0 is "at the start of the test".
    """
    primary = {"namespace": "default", "pod": "chemclaw-pg-1"}
    cluster = {"namespace": "default"}
    cases = [
        # The newest attempt failed (200 after 100): pending, then firing past its wait.
        _case(
            {_ARCHIVED: "100x30", _FAILED: "200x30"},
            "ChemclawWalArchivingFailing",
            {"5m": [], "15m": [primary]},
        ),
        # A failure the archiver then retried past is not an alert.
        _case(
            {_ARCHIVED: "300x30", _FAILED: "200x30"},
            "ChemclawWalArchivingFailing",
            {"15m": []},
        ),
        # WAL waiting and nothing archived since time 0: past 900 s, then past the 300 s wait.
        _case(
            {_ARCHIVED: "0x40", _READY: "3x40"},
            "ChemclawRecoveryPointObjectiveMissed",
            {"12m": [], "25m": [primary]},
        ),
        # The null control: a quiet database ages its last archival with nothing at risk.
        _case(
            {_ARCHIVED: "0x40", _READY: "0x40"},
            "ChemclawRecoveryPointObjectiveMissed",
            {"25m": []},
        ),
        # A healthy archiver ships every minute while segments come and go.
        _case(
            {_ARCHIVED: "0+60x40", _READY: "1x40"},
            "ChemclawRecoveryPointObjectiveMissed",
            {"25m": []},
        ),
        _case(
            {_AVAILABLE: "100x5", _BACKUP_FAILED: "200x5"},
            "ChemclawBaseBackupFailed",
            {"1m": [cluster]},
        ),
        _case(
            {_AVAILABLE: "100x5", _BACKUP_FAILED: "0x5"},
            "ChemclawBaseBackupFailed",
            {"1m": []},
        ),
        # A backup completed at 100 s ages past 129,600 s a little after minute 2161.
        _case(
            {_AVAILABLE: "100x2200"},
            "ChemclawBaseBackupTooOld",
            {"2150m": [], "2170m": [cluster]},
        ),
        # Never completed is the alert below, not this one.
        _case({_AVAILABLE: "0x2200"}, "ChemclawBaseBackupTooOld", {"2170m": []}),
        _case({_RECOVERABLE: "0x400"}, "ChemclawNoRecoveryPoint", {"300m": [], "370m": [cluster]}),
        _case({_RECOVERABLE: "100x400"}, "ChemclawNoRecoveryPoint", {"370m": []}),
        # The instance is scraped but the archiver and plugin series are not.
        _case({_UP: "1x30"}, "ChemclawBackupMonitoringBlind", {"10m": [], "20m": [cluster]}),
        _case(
            {_UP: "1x30", _ARCHIVED: "0+60x30", _RECOVERABLE: "100x30"},
            "ChemclawBackupMonitoringBlind",
            {"20m": []},
        ),
    ]
    evaluated = _evaluate(cases)
    assert evaluated.returncode == 0, f"{evaluated.stdout}{evaluated.stderr}"
