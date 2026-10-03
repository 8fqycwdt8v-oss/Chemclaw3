---
name: exhibits
description: >-
  Before making or revising an artefact beside the chat: when to, and what may go in one.
tools:
  - create_exhibit
  - revise_exhibit
  - read_exhibit
---

# Artefacts beside the chat

An artefact is a working document the chemist sees in a pane beside the conversation, can edit,
export and hand back to you. It is part of your answer, not a record: nothing in it is knowledge
until somebody writes a note or a protocol from it.

## Make one, or answer in prose

Make one with `create_exhibit` when the chemist will **reread, edit or export** the thing:

- a study plan, an investigation write-up, a report draft — the `document` kind, in Markdown;
- a table of four or more rows (a solvent ranking, a screening result, a comparison);
- a set of structures (a series of analogues, the species of a mechanism);
- a series to plot (a temperature profile, a campaign's best-so-far);
- one 3D structure — the `geometry` kind: cite the calculation that produced it as `source`
  (`calc_key`, the calculation key, and the `name` of the stored by-product) rather than
  retyping its coordinates; give an inline `xyz` block only for a structure no stored
  calculation holds.

Answer in prose for a single value, a yes or no, a short list, or an explanation. An artefact for a
two-row table is a pane the chemist has to open for something the answer could have said. When you
make one, keep the answer short and say what it holds — the chemist reads the artefact, not a copy
of it in the chat.

## One artefact per deliverable, revised rather than re-created

- When the chemist asks for a change, **revise** the artefact (`revise_exhibit`) rather than
  creating a second one. Two versions of one table beside each other is the confusion the revision
  history exists to prevent.
- For a `document`, revise with `edits` — exact replacements, each `old` quoted from the current
  text and occurring once. Resend the whole `spec` only when most of it changes.
- A refusal saying the artefact is at a later revision means **the chemist edited it**. Call
  `read_exhibit`, keep their change, and revise on top of it. Their edit is the most useful thing
  they can tell you about your draft; never write over it.
- When the turn's note says the chemist changed an artefact, read the change before you touch it.

## Never put in a number no tool returned — bind it instead of copying it

- Every figure in an artefact must come from a tool result in this conversation — a yield, a pKa,
  a limit, a point on a chart.
- **Bind a value rather than retyping it.** Every tool result ends with a line `⟨r:3fa2b1c0d9e8⟩`:
  that is its handle. Wherever a cell, a property, a SMILES or a chart's `x`/`y` goes, write
  `{"$bind": {"result": "r:3fa2b1c0d9e8", "pointer": "/rows/0/yield"}}` — the `pointer` is a JSON
  Pointer into that result (`/key/0/key`; `~1` is a `/` inside a key, `~0` a `~`). The chemist sees
  the value with its source beside it, it is never flagged unchecked, and a transcription error is
  impossible. A chart axis binds to a whole array (`"/temps"`), not one point at a time.
- **A whole table from one result:** give `rows_from` instead of `rows` —
  `{"result": "r:…", "pointer": "/solvents", "columns": {"name": "/name", "yield": "/yield_pct"}}`,
  one row per element, each column a pointer *inside* the element (missing → empty cell).
- A binding is refused, with the reason, when the handle is not a result of this conversation,
  the pointer does not reach anything, or the value is the wrong type (a chart's `y` takes numbers).
  Fix the pointer; do not fall back to typing the number in.
- Bind only into a result that is JSON. When a value is arithmetic over tool values (a ratio, a
  difference) write it as a literal and say in the note how it was derived.
- A figure written as a literal that no tool returned is shown as **unchecked** beside the
  artefact. That is a flag a chemist reads before trusting the table, so do not create it by
  writing numbers from background knowledge into a table; say those in prose, marked as such.
- A chart whose series are bound is shown as taken from the results; a literal series is shown as
  transcribed by you.
- When you revise an artefact that has bindings, start from its `raw_spec` (`read_exhibit` returns
  it) and keep each `$bind` you are not changing — sending back the filled-in `spec` would replace
  every binding with a typed copy.

## An html page

The `html` kind is a self-contained page — `{"kind": "html", "html": "<!doctype html>…",
"height": 480}` — for what the other kinds cannot show: an interactive comparison, a small custom
plot. It runs in a sandbox with **no network at all**: no CDN, no `fetch`, no external font or
image; inline everything (data as a JS literal, images as `data:` URIs). Prefer a table, chart or
structures artefact whenever one fits — they are editable by the chemist and their values can be
bound; a page's figures are only ever transcribed, and are checked like any other.

## What you do not do

- You do not pin a tool result as an artefact: the chemist pins results themselves from the result
  block in the chat. Show the values as a table if they need arranging.
- An artefact is not a knowledge note or a protocol. If the chemist wants one kept, write the note
  or draft the protocol with the tools that do that, citing what the artefact summarised.
