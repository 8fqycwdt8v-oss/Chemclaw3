"""The optional CloudNativePG Cluster the chart can render (`postgres.cluster`).

Rendered with `helm` (skipped, named and counted, without it) and read as YAML. The claim under test
is that the three objects agree with each other and with the values that size them, and that the
chart refuses to render a backup nobody has stated the targets of; schema validity is
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
_CHART = _ROOT / "deploy" / "helm" / "chemclaw"
_VALUES: dict[str, Any] = yaml.safe_load((_CHART / "values.yaml").read_text(encoding="utf-8"))
_CLUSTER_VALUES: dict[str, Any] = _VALUES["postgres"]["cluster"]

_HELM = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")

_ON = (
    "postgres.cluster.enabled=true",
    "postgres.cluster.imageName=registry.example/pgvector:16",
    "postgres.cluster.backup.destinationPath=s3://chemclaw-backups/prod",
    "postgres.cluster.targets.rpoSeconds=900",
    "postgres.cluster.targets.rtoSeconds=14400",
)
_KINDS = {"Cluster", "ObjectStore", "ScheduledBackup", "Database"}


@cache
def _render(*sets: str) -> subprocess.CompletedProcess[str]:
    """`helm template` with the three postures every render of this chart states, plus `sets`."""
    command = ["helm", "template", "chemclaw", str(_CHART)]
    for value in (
        "networkPolicy.allowAnyDestination=true",
        "retention.unboundedGrowthAccepted=true",
        "temporal.namespace=chemclaw",
        *sets,
    ):
        command += ["--set", value]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _documents(*sets: str) -> dict[tuple[str, str], dict[str, Any]]:
    """The rendered objects of the four kinds this feature owns, keyed by (kind, name)."""
    result = _render(*sets)
    assert result.returncode == 0, result.stderr
    return {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in yaml.safe_load_all(result.stdout)
        if doc and doc["kind"] in _KINDS
    }


def test_the_cluster_ships_off_and_without_a_default_for_what_only_the_site_knows() -> None:
    """A release that turns it on must state the image, the bucket and both recovery targets."""
    assert _CLUSTER_VALUES["enabled"] is False
    assert _CLUSTER_VALUES["imageName"] == ""
    assert _CLUSTER_VALUES["backup"]["destinationPath"] == ""
    assert _CLUSTER_VALUES["targets"] == {"rpoSeconds": 0, "rtoSeconds": 0}
    assert _CLUSTER_VALUES["temporalDatabases"]["enabled"] is False


@_HELM
def test_a_default_render_contains_none_of_it() -> None:
    assert _documents() == {}


@_HELM
def test_the_objects_agree_with_each_other_and_with_the_values_that_size_them() -> None:
    documents = _documents(*_ON)
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
    ceiling = _VALUES["postgres"]["maxConnections"] + _CLUSTER_VALUES["connectionHeadroom"]
    assert parameters["max_connections"] == str(ceiling)
    assert parameters["archive_timeout"] == f"{_CLUSTER_VALUES['archiveTimeoutSeconds']}s"
    assert cluster["bootstrap"]["initdb"]["postInitApplicationSQL"] == [
        "CREATE EXTENSION IF NOT EXISTS vector"
    ]
    assert cluster["walStorage"]["size"] == _CLUSTER_VALUES["walStorage"]["size"]

    assert store["retentionPolicy"] == _CLUSTER_VALUES["backup"]["retentionPolicy"]
    assert store["configuration"]["wal"]["compression"] == "gzip"
    assert "encryption" not in store["configuration"]["wal"]
    assert store["configuration"]["s3Credentials"] == {"inheritFromIAMRole": True}

    annotations = documents[("Cluster", "chemclaw-pg")]["metadata"]["annotations"]
    assert annotations == {"chemclaw/rpo-seconds": "900", "chemclaw/rto-seconds": "14400"}


@_HELM
def test_a_ceiling_the_fleet_does_not_declare_leaves_max_connections_to_the_operator() -> None:
    documents = _documents(*_ON, "postgres.maxConnections=0")
    parameters = documents[("Cluster", "chemclaw-pg")]["spec"]["postgresql"]["parameters"]
    assert "max_connections" not in parameters


@_HELM
def test_credentials_encryption_and_an_operator_override_reach_the_objects() -> None:
    documents = _documents(
        *_ON,
        "postgres.cluster.backup.credentialsSecret=backup-keys",
        "postgres.cluster.backup.encryption=AES256",
        "postgres.cluster.backup.endpointURL=https://objects.example:9000",
        "postgres.cluster.parameters.max_connections=500",
    )
    configuration = documents[("ObjectStore", "chemclaw-pg-backup")]["spec"]["configuration"]
    assert configuration["s3Credentials"]["accessKeyId"]["name"] == "backup-keys"
    assert configuration["wal"]["encryption"] == configuration["data"]["encryption"] == "AES256"
    assert configuration["endpointURL"] == "https://objects.example:9000"
    cluster = documents[("Cluster", "chemclaw-pg")]["spec"]
    assert cluster["postgresql"]["parameters"]["max_connections"] == "500"


@_HELM
def test_temporals_databases_ride_the_same_cluster_under_one_role() -> None:
    documents = _documents(
        *_ON,
        "postgres.cluster.temporalDatabases.enabled=true",
        "postgres.cluster.temporalDatabases.passwordSecret=temporal-db",
    )
    databases = {n: d["spec"] for (k, n), d in documents.items() if k == "Database"}
    assert {d["name"] for d in databases.values()} == {"temporal", "temporal_visibility"}
    assert {d["cluster"]["name"] for d in databases.values()} == {"chemclaw-pg"}
    (role,) = documents[("Cluster", "chemclaw-pg")]["spec"]["managed"]["roles"]
    assert {d["owner"] for d in databases.values()} == {role["name"]}
    assert role["passwordSecret"] == {"name": "temporal-db"}


@_HELM
def test_a_cluster_name_override_reaches_every_reference() -> None:
    documents = _documents(*_ON, "postgres.cluster.name=lab-pg")
    assert set(documents) == {
        ("Cluster", "lab-pg"),
        ("ObjectStore", "lab-pg-backup"),
        ("ScheduledBackup", "lab-pg-base"),
    }
    assert documents[("ScheduledBackup", "lab-pg-base")]["spec"]["cluster"]["name"] == "lab-pg"
    plugin = documents[("Cluster", "lab-pg")]["spec"]["plugins"][0]
    assert plugin["parameters"]["barmanObjectName"] == "lab-pg-backup"


@_HELM
@pytest.mark.parametrize(
    ("drop", "extra", "refusal"),
    [
        ("imageName", ["postgres.cluster.imageName="], "carry no pgvector"),
        (
            "bucket",
            ["postgres.cluster.backup.destinationPath=gs://bucket/x"],
            "must be an s3:// URL",
        ),
        ("rpo", ["postgres.cluster.targets.rpoSeconds=0"], "ships no default"),
        ("rto", ["postgres.cluster.targets.rtoSeconds=0"], "ships no default"),
        (
            "rpo below the shipping interval",
            ["postgres.cluster.targets.rpoSeconds=60"],
            "cannot be met",
        ),
        (
            "retention",
            ["postgres.cluster.backup.retentionPolicy=forever"],
            "is not a Barman retention",
        ),
        ("encryption", ["postgres.cluster.backup.encryption=rot13"], "AES256 or aws:kms"),
        ("instances", ["postgres.cluster.instances=0"], "at least 1"),
        (
            "temporal role secret",
            ["postgres.cluster.temporalDatabases.enabled=true"],
            "passwordSecret is empty",
        ),
    ],
)
def test_the_chart_refuses_a_backup_it_cannot_make_sense_of(
    drop: str, extra: list[str], refusal: str
) -> None:
    result = _render(*_ON, *extra)
    assert result.returncode != 0, f"{drop}: rendered"
    assert refusal in " ".join(result.stderr.split())
