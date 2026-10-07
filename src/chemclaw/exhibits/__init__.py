"""Artefacts: the versioned working documents a session shows beside its chat.

Named `exhibit` in code because `artifact` is the calculation store's word; every surface a chemist
reads says "Artefacts". An artefact is part of the answer, not an effect
(`D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect`): it changes nothing outside the
session, so writing one needs no approved plan, but a helper may not write one, since it would reach
the chemist from a context the chemist cannot see.

`models` is the shape (one spec per kind), `store` the revision history, `diff` what changed between
revisions, `export` the files a chemist keeps, and `grounding` which figures an agent revision
states that no tool in its session returned.
"""
