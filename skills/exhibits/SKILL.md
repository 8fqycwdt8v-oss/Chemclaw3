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
- a series to plot (a temperature profile, a campaign's best-so-far).

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

## Never put in a number no tool returned

- Every figure in an artefact must come from a tool result in this conversation — a yield, a pKa,
  a limit, a point on a chart. Copy it at the precision the tool gave or rounded from it, never
  re-derived from memory.
- A figure no tool returned is shown to the chemist as **unchecked** beside the artefact. That is
  a flag a chemist reads before trusting the table, so do not create it by writing numbers from
  background knowledge into a table; say those in prose, marked as such.
- A chart's points are always shown as transcribed by you. Keep charts to series you can point at
  in a tool result.

## What you do not do

- You do not pin a tool result as an artefact: the chemist pins results themselves from the result
  block in the chat. Show the values as a table if they need arranging.
- An artefact is not a knowledge note or a protocol. If the chemist wants one kept, write the note
  or draft the protocol with the tools that do that, citing what the artefact summarised.
