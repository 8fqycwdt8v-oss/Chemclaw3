"""Publish an archived probe run into Phoenix, and print the URL that shows it (AG-13).

The command half of `evals/phoenix.py`. It owns the endpoint — the module takes a client so the
tests can drive it against a recorder — and it prints a URL rather than a summary, because the
whole point of the row this closes is that the comparison happens in a surface a person opens.

    make phoenix-up
    uv run python -m chemclaw.cli.phoenix_publish tasks/live-test/transcripts --name haiku-2026-08

Nothing here calls a model. The transcripts are the record; this reads them.
"""

import argparse
from pathlib import Path

from phoenix.client import Client

from chemclaw.core.config import settings
from chemclaw.evals.phoenix import publish_run


def _publish(args: argparse.Namespace) -> int:
    """Publish one directory and report what landed, or say why it could not.

    Returns non-zero on a failure a caller should notice — an absent directory, an ungraded corpus
    mismatch, a Phoenix that is not running — because this is invoked from `make` and a publish
    that quietly did nothing is the failure mode the eval lane already has enough of.
    """
    base_url = args.base_url or settings.phoenix_base_url
    client = Client(base_url=base_url)
    directory = Path(args.directory)
    name = args.name or directory.name

    try:
        published = publish_run(
            directory,
            experiment_name=name,
            client=client,
            dataset_name=args.dataset,
            probe_dir=args.probe_dir,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"publish failed: {exc}")
        return 1

    covered = f"{published.runs}/{published.examples}"
    print(
        f"published {name}: {covered} corpus probes, {published.evaluations} evaluations\n"
        f"  dataset  {published.dataset_id} (version {published.dataset_version_id})\n"
        # Absolute already — the client builds it from the base URL it was constructed with, so
        # prepending `base_url` here printed it twice.
        f"  compare  {client.experiments.get_dataset_experiments_url(published.dataset_id)}"
    )
    if published.runs < published.examples:
        print(
            f"  note: this run covered {covered} probes in the corpus — "
            "the experiment records the coverage, the dataset keeps every question"
        )
    return 0


def main() -> int:
    """Parse arguments and publish."""
    parser = argparse.ArgumentParser(
        prog="phoenix-publish",
        description="Publish an archived probe run to Phoenix as an experiment over the corpus.",
    )
    parser.add_argument("directory", help="a transcript directory written by live_probes")
    parser.add_argument(
        "--name", default=None, help="experiment name; defaults to the directory name"
    )
    parser.add_argument("--dataset", default=None, help="override the configured dataset name")
    parser.add_argument("--probe-dir", default=None, help="override the configured probe corpus")
    parser.add_argument("--base-url", default=None, help="Phoenix base URL")
    return _publish(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
