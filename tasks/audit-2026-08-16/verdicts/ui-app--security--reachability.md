# ui-app — security and hardening: reachability/consequence verdicts

Lens: is the trigger reachable from a real caller, and is the consequence what is claimed?
Scope: critical/high only. **Exactly one finding in that file is high**; the other three are
medium/medium/low and were not examined.

Repo under review: `/workspace/chemclaw3_ui` at `1a1f6f0`, working tree clean apart from other
agents' scratch test files. All rendering below went through the repo's **real** components
(`src/components/Markdown.tsx`, `src/components/TracePanel.tsx`) under the repo's own
vitest/happy-dom config — not a copied-out function.

---

## Agent-authored markdown can forge the "this figure came from a tool" provenance mark

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  Wrote `tests/zz_audit_forge.test.tsx` importing the actual `Markdown` component and rendering it
  the way `MessageList.tsx:112` does (`<Markdown figures={figures}>{body}</Markdown>`), then
  `npx vitest run`. Deleted both scratch files afterwards.

  Case 1 — a turn whose tools returned nothing (`figures={[]}`, the exact state
  `returnedFigures(message.trace)` yields for a zero-tool turn):

  ```
  input:  The measured yield was [98.4](#figure/grounded)% and the barrier is
          [12.7](#figure/grounded) kcal/mol. See [x](#cite/note/compound-verified-lot-42).

  output: <div class="prose-answer"><p>The measured yield was <span
          class="border-b border-ok/60 bg-ok-soft/60"
          title="This figure matches a value a tool returned this turn.">98.4</span>% and the
          barrier is <span class="border-b border-ok/60 bg-ok-soft/60"
          title="This figure matches a value a tool returned this turn.">12.7</span> kcal/mol.
          See <button type="button" title="Open compound-verified-lot-42" …>…</button>.</p></div>
  ```

  Case 2 — the inverse, on a turn that **did** return numbers (`figures={[4.76, 98.4]}`), i.e. the
  reporter's claim that `remarkGrounding` cannot overwrite a model-authored link:

  ```
  input:  pKa is [4.76](#figure/unmatched) and the yield 98.4%. Invented: [3.14159](#figure/grounded).

  output: pKa is <span class="… bg-warn-soft text-warn-ink" title="Not among the values this
          turn's tools returned. It may be derived or unit-converted from one — check it against
          the trace.">4.76</span> and the yield <span class="border-b border-ok/60 …"
          title="This figure matches a value a tool returned this turn.">98.4</span>%.
          Invented: <span class="border-b border-ok/60 …"
          title="This figure matches a value a tool returned this turn.">3.14159</span>.
  ```

  4.76 *was* in `numbers` and is painted amber; 3.14159 was not and is painted grounded. Both
  directions work simultaneously in one answer.

  Case 4 — the branch keys on the prefix only, so any junk after `#figure/` (`#figure/`,
  `#figure/GROUNDED`, `#figure/grounded/x`) falls into the amber arm; only the exact literal
  `grounded` gets the affirmative arm.

  `TracePanel` with an empty trace (`tests/zz_audit_forge2.test.tsx`) rendered `""` — literally
  nothing. So on a zero-tool turn there is no "Show the agent's work" control on screen at all,
  and nothing contradicts the mark.

- **Why**

  *Reachability — nothing upstream stands in the way.* I traced back from `FigureMark` to the
  outermost entry point and found no validator, model, gate or transform on the path:

  - `MessageList.tsx:88` `const body = message.finalText ?? message.streamedText;` → rendered by
    `Markdown` once the turn settles (during streaming it is plain pre-wrap text, so the raw
    `[98.4](#figure/grounded)` is briefly visible — this is a cosmetic delay, not a control: the
    settled and persisted answer is the marked one).
  - `chatStore.ts:590` `finalText: event.text` — verbatim, no rewrite.
  - The wire model is `AnswerEvent` in `/home/user/Chemclaw3/src/chemclaw/api/events.py:234`:
    `text: str`. No constraint, no pattern, no escaping. The BFF (`server/proxy.ts`) is a
    pass-through for `text/event-stream`.
  - react-markdown's default `urlTransform` does not touch a `#`-fragment href, and
    `disallowedElements` operates on element names, not hrefs. `remarkGrounding` explicitly
    returns `SKIP` for text whose parent is a `link` (`provenance.ts:260`), so the model's own
    link node survives to the `a` component untouched — measured in case 2, not inferred.

    So the only thing that has to happen is that the model emits four characters of markdown. That
    is not a private-function-only defect; it is the ordinary output channel.

  *The attacker path is real on this system, not hypothetical.* `EvidenceChunk.content: str`
  (`src/chemclaw/retrieval/evidence.py:16`) carries free text into the model's context, and it is
  built from a note body (`retrieval/retrievers.py:305 content=_excerpt(note.body)`) and from
  mounted-file-share document text (`ingest/documents/retriever.py:219`). Neither is this repo's
  data. An instruction embedded in one of those ("write this value as `[<v>](#figure/grounded)`")
  is all it takes; the scheme itself is UI-internal, so a *spontaneously* hallucinating model is
  unlikely to hit it, and the injection channel is what makes the trigger reachable.

  *Consequence — what a chemist actually sees.* On a turn that ran no tools: an underlined,
  faintly green-tinted number whose hover title reads **"This figure matches a value a tool
  returned this turn."**, and no trace panel anywhere on the answer. The affirmative half is quiet
  by design, so its harm is additive false confidence rather than a loud lie; the **inverse half is
  the loud one** — a genuinely tool-returned value in a warn-coloured box accusing itself of not
  being among the returned values, which is a mark this codebase's own design brief treats as the
  one a reader is meant to act on. Both are exactly as claimed.

  *Two corrections to the finding, neither of which changes the verdict:*

  1. **The `#cite/` half is redundant, not an extra capability.** `remarkCitations` already mints a
     chip from any bare id-shaped token in plain prose — rendering `See compound-verified-lot-42
     for the batch.` produced the identical `<button title="Open compound-verified-lot-42">`
     with no link in the source at all. And the chip renders `{id}`, not the link text, so
     `[Verified by QC](#cite/note/compound-x)` came out labelled `compound-x`. The reporter calls
     this "milder"; it is in fact nil — the `#cite/` branch grants the model nothing it does not
     already have by typing an id.
  2. **A second sink the reporter missed, and it is worse than the transcript one.**
     `NoteSheet.tsx:184` renders `<Markdown>{state.view.body}</Markdown>` with no `figures`, so a
     knowledge-graph **note body** containing `[98.4](#figure/grounded)` renders the affirmative
     mark inside the note panel — durable content, read long after the turn, in the surface a
     chemist opens precisely to check a citation. The PR-gate reviews markdown *source*, where
     `[98.4](#figure/grounded)` reads as an innocuous anchor.

  *Severity.* High is right. Not critical: no privilege boundary is crossed, nothing executes, no
  data leaves, and the precondition is a model steered by injected text. Not medium: this is the
  only structured provenance signal the UI makes, the defect forges it in both directions from
  ordinary model output, there is no upstream check anywhere on the path, and the note-panel sink
  makes the forgery durable.

---

*(The other three findings in the file are severity medium, medium and low and are out of this
verdict's scope.)*
