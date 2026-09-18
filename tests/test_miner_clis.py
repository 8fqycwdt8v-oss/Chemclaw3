"""The two miner CLIs, driven — they had no test file at all.

`src/chemclaw/cli/distill.py` (151 lines) and `src/chemclaw/cli/propose_profile.py` (190) were
referenced by the `Makefile` and by one docstring list, and by nothing that runs. A mutation review
made the cost of that concrete: replacing `distiller.propose`'s store call with an undefined name
left `tests/test_distiller.py tests/test_behaviour_proposals.py tests/test_api_proposals.py` at 27
passed, and the commit message's "both miners run on demand and report zero on this corpus" was a
claim about somebody's manual run.

**What these assert is the shape of the run, not a mined result**, and the first draft got that
wrong in a way worth recording: it asserted the *empty-corpus* message, which is a claim about the
database rather than about the miner. The Postgres arm of this suite is shared — by the time these
ran in a full pass, 243 sessions written by other tests were in `turn_costs`, and the two tests that
had passed alone failed. So what is pinned is what the miner is responsible for: it completes,
exits 0, says something a person can act on, and its machine-readable form parses with the keys a
report reads. Which branch of that prose it takes is the corpus's business, not this file's.
"""

import asyncio
import json

import pytest

from chemclaw.core.config import settings


@pytest.fixture(autouse=True)
def _dry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both miners read `turn_costs`; this suite's default store has none."""
    monkeypatch.setattr(settings, "session_store", "memory")


def test_the_distiller_completes_and_says_what_it_found(capsys: pytest.CaptureFixture[str]) -> None:
    """A dry run ends cleanly and reports, whatever the corpus holds."""
    from chemclaw.cli.distill import _run

    assert asyncio.run(_run(False, False)) == 0
    printed = capsys.readouterr().out

    assert printed.strip(), "the miner finished silently"
    assert "nothing to distil" in printed or "sessions:" in printed


def test_the_distiller_s_json_form_is_parseable(capsys: pytest.CaptureFixture[str]) -> None:
    """`--json` is what a scheduled report would read; an unparseable one is silently useless."""
    from chemclaw.cli.distill import _run

    assert asyncio.run(_run(False, True)) == 0
    payload = json.loads(capsys.readouterr().out)

    assert set(payload) == {"sessions", "candidates"}
    assert isinstance(payload["candidates"], list)
    # A dry run proposes nothing, whatever it found: every entry carries what it *would* file and
    # no `state`, which is the field `--propose` writes. That is the property, not the count.
    assert all("state" not in entry for entry in payload["candidates"])


def test_the_distiller_refuses_to_file_where_nothing_can_accept(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--propose` with the personal tier off writes rows the accept route answers 503 to.

    Non-zero, because a miner that files into a queue nobody can drain has not done its job and a
    `make` target should say so.
    """
    from chemclaw.cli.distill import _run

    monkeypatch.setattr(settings, "agent_memory_enabled", False)

    assert asyncio.run(_run(True, False)) == 1
    assert "refusing to file" in capsys.readouterr().out


def test_the_profile_miner_completes_and_says_what_it_found(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same for the other miner."""
    from chemclaw.cli.propose_profile import _run

    assert asyncio.run(_run(False, False)) == 0
    assert capsys.readouterr().out.strip(), "the miner finished silently"


def test_the_profile_miner_s_json_form_is_parseable(capsys: pytest.CaptureFixture[str]) -> None:
    """As above: the machine-readable form is what a report would read."""
    from chemclaw.cli.propose_profile import _run

    assert asyncio.run(_run(False, True)) == 0
    json.loads(capsys.readouterr().out)
