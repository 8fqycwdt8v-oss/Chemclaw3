"""Knowledge-graph layer: the note schema, parser, and NetworkX indexer.

Interlinked Markdown notes in Git: YAML frontmatter plus a body whose [[wikilinks]] encode
relations. Retrieval is graph traversal, not top-k vector similarity. The notes live in the
configured `knowledge/` directory; agent-authored notes are written there by `kg/record.py`
carrying `created_by: agent`.
"""
