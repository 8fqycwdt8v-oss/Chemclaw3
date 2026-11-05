"""The local compose Postgres is sized for the release it runs, not for the stock default.

`deploy/README.md` says a stock `max_connections=100` refuses this release at full scale. The live
lane (connectors, workers, front door), a second front door for an evaluation and Temporal's pools
hold ~90 idle connections between them, so a compose file left at the default turns the next
process into "too many clients" — which read, in an evaluation, as a model failure.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from tests.siblings import REPO_ROOT

_COMPOSE = REPO_ROOT / "infra" / "docker-compose.yml"
_CHART_VALUES = REPO_ROOT / "deploy" / "helm" / "chemclaw" / "values.yaml"

#: Postgres's own default, which the compose file must not be left at.
_STOCK = 100


def _compose_max_connections(compose: Path) -> int | None:
    """The `max_connections` the compose Postgres is started with, or `None` when it sets none."""
    service = yaml.safe_load(compose.read_text(encoding="utf-8"))["services"]["postgres"]
    command = " ".join(str(part) for part in service.get("command", []))
    match = re.search(r"max_connections=(\d+)", command)
    return int(match.group(1)) if match else None


def test_the_compose_postgres_holds_at_least_what_the_release_is_sized_for() -> None:
    """Never below the application chart's own `postgres.maxConnections`."""
    chart = yaml.safe_load(_CHART_VALUES.read_text(encoding="utf-8"))["postgres"]["maxConnections"]
    assert chart > _STOCK, "the chart no longer sizes past the stock default; this guard is stale"
    configured = _compose_max_connections(_COMPOSE)
    assert configured is not None, "the compose Postgres runs at the stock max_connections"
    assert configured >= chart, (
        f"the compose Postgres allows {configured} connections but the release is sized for {chart}"
    )


def test_a_compose_file_that_sets_nothing_is_read_as_the_stock_default(tmp_path: Path) -> None:
    """The reader reports `None` for a service with no `command`, so the guard above can fail."""
    bare = tmp_path / "docker-compose.yml"
    bare.write_text("services:\n  postgres:\n    image: pgvector/pgvector:pg16\n", encoding="utf-8")
    assert _compose_max_connections(bare) is None
