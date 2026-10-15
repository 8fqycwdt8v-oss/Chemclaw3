"""The Postgres chart (`deploy/helm/chemclaw-postgres`): a CloudNativePG Cluster with WAL archiving.

Rendered with `helm` (skipped, named and counted, without it) and read as YAML. The claims under
test: the objects agree with each other and with the application chart's connection budget; the
three ways a cluster comes into being (empty, imported from a live database, restored from an
archive) render what their runbook procedures say; nothing is deleted with the release; and the
chart refuses a backup nobody has stated the targets of. Schema validity is
`make helm-validate`'s, against the CRD catalogue.
"""

import shutil
import subprocess
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
    assert {kind for kind, _ in documents} == {"Cluster", "ObjectStore", "ScheduledBackup"}
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
