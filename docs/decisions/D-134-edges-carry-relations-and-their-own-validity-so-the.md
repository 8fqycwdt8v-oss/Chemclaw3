# D-134 — Edges carry relations and their own validity, so the graph stops being a citation network

**Context.** `kg/graph.py:150` was `graph.add_edge(note.id, target)`. No attributes at all. Nothing
could say *precursor-of*, *contradicts*, *measured-by* or *computed-from*, so every graph query was
structurally blind to what a connection meant, and the retrieval layer treated the graph as what it
was: a citation network. Separately, `valid_from`/`valid_to` existed on nodes, so a *fact* that
stopped being true was expressible while a *relation* that stopped being true was not.

**Decision.** Two syntaxes, because they serve different authors:

- **Body:** `[[rel:target]]`. The syntax was free to take — `_SLUG` excludes `:`, so
  `[[precursor-of:x]]` previously parsed as one dangling id and failed `kg-validate`, meaning no
  corpus could be relying on it.
- **Frontmatter:** `relations: [{rel, to, confidence?, valid_from?, valid_to?}]` — the structured
  form, and the only place per-edge metadata can live.

`cited_ids` and `outgoing_links` keep returning bare targets, so `kg.validate`'s dangling-link
check and the answer verifier work unchanged through one code path rather than two. A bare
`[[link]]` still means exactly what it always meant (`cites`), which is asserted against the
shipped corpus rather than assumed.

`kg/relations.py` holds `KNOWN_RELATIONS`, **adopted from RXNO / CHMO / CHEMINF / OntoRXN** rather
than invented, so it maps to a standard later instead of being one more thing to reconcile.
Enforced by `kg.validate`, not by the schema — exactly as `KNOWN_NOTE_TYPES` is, and for the same
reason: the agent must be able to propose a genuinely new relation, and the PR-gate is where a
human decides.

**Kept on `nx.DiGraph`, with a tuple of relations per edge.** A `MultiDiGraph` models parallel
edges properly and would change the meaning of `graph[a][b]` for every existing reader —
`neighborhood`, `kg.analytics`, the retrievers — to solve a case that barely arises. The cost is
that an edge holds a *set* of relations rather than one, which is why the attribute is plural and
why a compound that is both precursor and product of one reaction is tested.

**A bug this created, and fixed.** `report/retrievers.py:_excerpt` did `WIKILINK.sub(r"\1", ...)`,
which would have rendered `[[precursor-of:x]]` into a report a person reads as
`precursor-of:x`. It now strips to the target through the same shared splitter the indexer uses.

**Downstream in the same decision.**

- **Conflict signalling (KM-8).** Retrieval used to return two contradictory notes with no marker,
  which reads as corroboration — worse than returning neither. `kg/conflicts.py` reports a
  `declared` conflict (a `contradicts`/`supersedes` edge, now expressible) and a `suspected` one
  (same type, same compound, overlapping validity, materially different confidence). There is
  deliberately **no property extractor**: parsing "the yield was 82%" out of prose and comparing it
  across notes is a natural-language problem this layer would get subtly wrong, and a false
  conflict is as damaging as a missed one. A conflict is a **flag** on the evidence
  (`EvidenceChunk.conflicts_with`), never a filter — dropping one side would be retrieval deciding
  which of two curated notes is right, and it has no basis for that.

- **Negative feedback (KM-12).** `failure-mode` sat in `KNOWN_NOTE_TYPES` with nothing minting one.
  `memory/failure.py` builds it, carrying a `contradicts` relation to what it refutes — which is
  what makes the feedback actually feed back, since before typed edges a correction could only be
  prose and `find_conflicts` could not see it. It goes through the PR-gate like everything else: a
  machine-written note asserting that curated knowledge is wrong needs *more* human sign-off, not
  less. The refuted note is never edited or deleted.

**Verification.** `tests/test_relations.py` pins backward compatibility against the real shipped
fixture corpus (every pre-existing edge is still exactly `cites`), the new syntax, both forms
producing one edge, an unknown relation failing validation, every known relation passing, and an
edge whose validity has lapsed dropping out of a time-scoped query while both its notes stay
current. `tests/test_conflicts.py` covers the detector, including the cases it must *not* report.

---
