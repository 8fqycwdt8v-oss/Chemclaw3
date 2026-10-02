"""Artefacts: the versioned working documents a session shows beside its chat.

Named `exhibit` in code because `artifact` is the calculation store's word in this tree (D-124);
every surface a chemist reads says "Artefacts". An artefact is **part of the answer, not an effect**
(`D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect`): it changes nothing in a
laboratory, the knowledge graph or another system, so writing one needs no approved plan — and a
helper may not write one, because it would reach the chemist's surface from a context the chemist
cannot see.

`models` is the shape (one spec per kind, validated for every writer), `store` the revision history,
`diff` what changed between two revisions, `export` the files a chemist keeps, and `grounding` which
figures an agent-written revision states that no tool in its session returned.
"""
