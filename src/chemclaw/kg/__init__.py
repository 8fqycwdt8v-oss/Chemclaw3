"""Knowledge-graph layer: the note schema, parser, and NetworkX indexer.

"What do we know" (D-004): interlinked Markdown notes in Git. Each note is YAML
frontmatter (structured, queryable) plus a Markdown body whose [[wikilinks]]
encode relations. Retrieval is graph traversal, not top-k vector similarity. This
package is code only; the notes themselves live in the configured `knowledge/`
directory (data), and agent-authored notes are written straight into it by `kg/record.py`,
carrying `created_by: agent` and readable at once
(`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`).
"""
