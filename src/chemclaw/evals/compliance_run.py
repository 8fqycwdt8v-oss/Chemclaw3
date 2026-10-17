"""The run half of the compliance probe: prepare each arm, drive its turns, read back its designs.

Two arms over `compliance.PATH_PROBES`. `control` runs first with nothing on file naming the
prohibited species; `preference` then runs with `compliance.PROHIBITION` written through
`agent.preferences.PreferenceStore` — the store `StandingPreferences` reads on every model call —
and the seed is removed afterwards whatever happened. An arm that cannot be shown to be what its
name says is refused before a turn is paid for: the owner is read back from the front door's own
session row rather than assumed, and the seed must be readable back from the durable store. A
preference naming the species that this suite did not write is somebody's constraint and is never
deleted here.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict

from chemclaw.core.chem import require_standard_smiles
from chemclaw.core.config import settings
from chemclaw.evals.compliance import (
    PATH_PROBES,
    PROHIBITION,
    Arm,
    Prohibition,
    RepeatScore,
    design_slots,
    mentions,
    score,
    written_designs,
)
from chemclaw.evals.live import ProbeOutcome, open_session, run_probes
from chemclaw.evals.probe import Probe

logger = logging.getLogger(__name__)


class ArmRefused(RuntimeError):
    """An arm could not be set up as its name says, so nothing was asked."""


class ComplianceRun(BaseModel):
    """Every scored repeat of both arms, and whether the seed was still on file when its arm ended.

    `seed_held` is False when something removed the preference mid-arm (a turn may call
    `forget_preference`), so the later repeats of that arm ran as a second control.
    """

    model_config = ConfigDict(extra="forbid")

    owner: str
    records: list[RepeatScore]
    seed_held: bool


def path_probes(corpus: Sequence[Probe]) -> dict[str, Probe]:
    """The probe behind each path, from the loaded corpus.

    Raises:
        KeyError: A path's probe id is not in the corpus, rather than a run that asks two paths.
    """
    by_id = {probe.id: probe for probe in corpus}
    missing = sorted(probe_id for probe_id in PATH_PROBES.values() if probe_id not in by_id)
    if missing:
        raise KeyError(f"the compliance suite's probes {missing} are not in the corpus")
    return {path: by_id[probe_id] for path, probe_id in PATH_PROBES.items()}


async def runner_actor(client: httpx.AsyncClient) -> str:
    """The actor the front door runs this runner's turns as, read from its own session row.

    Read back, not assumed, because a seed under any other owner turns the treatment arm into a
    second control. Proves on the way that this process reads the front door's database.

    Raises:
        ArmRefused: This process keeps preferences in memory, so a seed would never reach the front
            door, or the session the front door just opened is not in (or cannot be read from)
            this process's database.
    """
    from chemclaw.agent.session_store import SessionOwnerStore

    if settings.session_store != "postgres":
        raise ArmRefused(
            f"this runner's session store is {settings.session_store!r}, so a seeded preference "
            "would live in this process and never reach the front door. Run it with "
            "CHEMCLAW_SESSION_STORE=postgres, as the lane's front door runs."
        )
    session_id = await open_session(client)
    try:
        found, owner, _profile = await SessionOwnerStore().lookup(session_id)
    except ConnectionError as exc:
        raise ArmRefused(f"this runner cannot read the front door's database: {exc}") from exc
    finally:
        with contextlib.suppress(httpx.HTTPError):
            (await client.delete(f"/sessions/{session_id}")).raise_for_status()
    if not found or not owner:
        raise ArmRefused(
            f"the front door opened session {session_id!r} but this process's database "
            f"({'found it with no owner' if found else 'has no row for it'}), so a seed written "
            "here could not be shown to reach the actor the turns run as"
        )
    return owner


def _naming(pairs: Sequence[tuple[str, str]], prohibition: Prohibition) -> list[tuple[str, str]]:
    """The `(key, value)` pairs naming the prohibited species in either half."""
    target = require_standard_smiles(prohibition.smiles)
    return [
        pair
        for pair in pairs
        if any(m.structure == target for m in mentions(f"{pair[0]} {pair[1]}"))
    ]


async def _held(owner: str) -> list[tuple[str, str]]:
    """`owner`'s preferences as the front door would read them, from a store with no memory."""
    from chemclaw.agent.preferences import PreferenceStore

    return [(p.key, p.value) for p in await PreferenceStore().recall(owner)]


async def clear_for_control(owner: str, prohibition: Prohibition = PROHIBITION) -> None:
    """Make `owner`'s preferences a null for the control arm, or refuse.

    Removes only this suite's own seed, which a run killed before its cleanup leaves behind.

    Raises:
        ArmRefused: Another preference names the species, or the seed could not be removed.
    """
    from chemclaw.agent.preferences import PreferenceStore

    seed = (prohibition.key, prohibition.value)
    naming = _naming(await _held(owner), prohibition)
    foreign = [pair for pair in naming if pair != seed]
    if foreign:
        raise ArmRefused(
            f"{owner!r} already holds preference(s) naming the prohibited species: {foreign}. The "
            "control arm would not be a null; they are not this suite's to delete."
        )
    if naming:
        logger.warning("removing a seed an earlier compliance run left behind for %s", owner)
        await PreferenceStore().forget(owner, prohibition.key)
    if _naming(await _held(owner), prohibition):
        raise ArmRefused(f"the earlier seed for {owner!r} is still on file after removing it")


async def seed(owner: str, prohibition: Prohibition = PROHIBITION) -> None:
    """Write the preference arm's seed, and refuse unless the durable store reads it back.

    Raises:
        ArmRefused: The write did not persist, or a fresh read does not return it.
    """
    from chemclaw.agent.preferences import PreferenceStore

    stored = await PreferenceStore().remember(owner, prohibition.key, prohibition.value)
    if not stored or (prohibition.key, prohibition.value) not in await _held(owner):
        raise ArmRefused(f"seeding {prohibition.key!r} for {owner!r} did not reach the store")


async def is_seeded(owner: str, prohibition: Prohibition = PROHIBITION) -> bool:
    """Whether the seed is on file for `owner` exactly as `seed` wrote it."""
    return (prohibition.key, prohibition.value) in await _held(owner)


async def unseed(owner: str, prohibition: Prohibition = PROHIBITION) -> None:
    """Remove the seed. Logged rather than raised: it runs in a `finally`, after the measurement."""
    from chemclaw.agent.preferences import PreferenceStore

    if not await PreferenceStore().forget(owner, prohibition.key):
        logger.error(
            "could not remove %r for %s — the next run refuses until it is gone",
            prohibition.key,
            owner,
        )


async def _scored(
    arm: Arm,
    path: str,
    repeat: int,
    outcome: ProbeOutcome,
    client: httpx.AsyncClient,
    seen: set[str],
) -> RepeatScore:
    """One repeat, scored with the stored revision of every design its turn wrote.

    A design an earlier repeat of the run also wrote is named in `reused`: designs are keyed by
    owner and ask, so a later repeat can revise an earlier one's document.
    """
    slots: list[str] = []
    forbidden: list[str] = []
    labels: list[str] = []
    reused: list[str] = []
    for design_id, revision in sorted(written_designs(outcome).items()):
        label = f"{design_id}@{revision}"
        try:
            response = await client.get(f"/protocols/{design_id}", params={"revision": revision})
            response.raise_for_status()
            more_slots, more_forbidden = design_slots(response.json()["design"])
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.warning("could not read %s for %s/%s#%d: %s", label, arm, path, repeat, exc)
            label += " (unread)"
        else:
            slots += more_slots
            forbidden += more_forbidden
        labels.append(label)
        if design_id in seen:
            reused.append(label)
        seen.add(design_id)
    return RepeatScore(
        arm=arm,
        path=path,
        repeat=repeat,
        probe_id=outcome.probe_id,
        session_id=outcome.session_id,
        designs=labels,
        reused=reused,
        score=score(outcome.answer, slots=slots, forbidden=forbidden),
    )


async def drive_arm(
    arm: Arm,
    probes: Mapping[str, Probe],
    repeats: int,
    *,
    client: httpx.AsyncClient,
    base_url: str | None,
    directory: Path,
    seen: set[str],
) -> list[RepeatScore]:
    """Ask every path's probe `repeats` times on one arm, each repeat an ordinary `run_probes`.

    `seen` is shared across both arms, so a design the control arm wrote and the preference arm
    revised is named rather than silently scored as the preference arm's own.
    """
    path_of = {probe.id: path for path, probe in probes.items()}
    records: list[RepeatScore] = []
    for repeat in range(1, repeats + 1):
        logger.info("compliance arm %s, repeat %d/%d", arm, repeat, repeats)
        outcomes = await run_probes(
            list(probes.values()),
            base_url=base_url,
            transcript_dir=str(directory / arm / f"repeat-{repeat}"),
        )
        for outcome in outcomes:
            records.append(
                await _scored(arm, path_of[outcome.probe_id], repeat, outcome, client, seen)
            )
    return records


async def run_arms(
    probes: Mapping[str, Probe],
    repeats: int,
    *,
    client: httpx.AsyncClient,
    base_url: str | None,
    directory: Path,
) -> ComplianceRun:
    """Both arms, control first, with the seed in force only for the preference arm.

    Raises:
        ArmRefused: Either arm could not be prepared; the seed is never left behind by a refusal.
    """
    owner = await runner_actor(client)
    await clear_for_control(owner)
    seen: set[str] = set()
    records = await drive_arm(
        "control", probes, repeats, client=client, base_url=base_url, directory=directory, seen=seen
    )
    try:
        await seed(owner)
        records += await drive_arm(
            "preference",
            probes,
            repeats,
            client=client,
            base_url=base_url,
            directory=directory,
            seen=seen,
        )
        held = await is_seeded(owner)
    finally:
        await unseed(owner)
    return ComplianceRun(owner=owner, records=records, seed_held=held)
