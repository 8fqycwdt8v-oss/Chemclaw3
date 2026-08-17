# ui-app — security: reproduction verdicts (round 1)

Lens: **does it actually reproduce?** Scope: critical/high only. The findings file contains one
high finding and three at medium/low; only the high one is verified here.

Repo state: `/workspace/chemclaw3_ui` at `1a1f6f0`, working tree clean apart from another agent's
`tests/audit_verify/`. Nothing I ran mutated the tree (my test file was removed after the run).

---

## Agent-authored markdown can forge the "this figure came from a tool" provenance mark

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

I did not use the reporter's `/tmp/rmtest/figure.mjs`, and I did not copy `FigureMark` or the `a`
component out of the file. I rendered the **real `src/components/Markdown.tsx`** through the repo's
own vitest + happy-dom + `@testing-library/react` harness, importing the actual component:

```tsx
// tests/zz_repro_audit.test.tsx (mine; deleted after the run)
import { Markdown } from '../src/components/Markdown.tsx';
const html = (md, figures) => render(<Markdown figures={figures}>{md}</Markdown>).container.innerHTML;
```

`npx vitest run tests/zz_repro_audit.test.tsx` printed:

**A — a turn that called no tool (`figures` left at its default, i.e. `NO_FIGURES`):**

```
<div class="prose-answer"><p>The measured yield was <span class="border-b border-ok/60 bg-ok-soft/60"
title="This figure matches a value a tool returned this turn.">98.4</span>% and the barrier is
<span class="border-b border-ok/60 bg-ok-soft/60" title="This figure matches a value a tool returned
this turn.">12.7</span> kcal/mol.</p></div>
```

**B — a turn that *did* return `4.76`, with the grounding plugin therefore active** (input:
`Real tool value: 4.76. Forged: [98.4](#figure/grounded)%. Discredited: [4.76](#figure/unmatched) again.`,
`figures=[4.76]`):

```
Real tool value: <span class="border-b border-ok/60 bg-ok-soft/60" title="This figure matches a value
a tool returned this turn.">4.76</span>. Forged: <span class="border-b border-ok/60 bg-ok-soft/60"
title="This figure matches a value a tool returned this turn.">98.4</span>%. Discredited:
<span class="rounded-sm border-b border-warn/70 bg-warn-soft px-0.5 text-warn-ink" title="Not among
the values this turn's tools returned. …">4.76</span> again.
```

**D — the genuine mark for comparison** (`The value is 4.76 kcal/mol.`, `figures=[4.76]`) produced
exactly `<span class="border-b border-ok/60 bg-ok-soft/60" title="This figure matches a value a tool
returned this turn.">4.76</span>`.

**E — grounding vocabulary:** `[9.9](#figure/GROUNDED)`, `[8.8](#figure/)` and
`[7.7](#figure/grounded/x)` all rendered the **amber** unmatched mark. The affirmative branch is an
exact `=== 'grounded'`; everything else falls to the accusation.

**C — citation chip:** `[ignored-label](#cite/note/compound-verified-lot-42)` rendered
`<button type="button" title="Open compound-verified-lot-42" …>compound-verified-lot-42</button>`,
i.e. a real `CitationChip` for an id no retrieval produced, and the anchor's own label was discarded.

Line numbers and symbols, checked against the current file: `Markdown.tsx:95-113` is the `a`
component, `:96-104` the `#cite/` branch, `:105-107` the `FIGURE_HREF` branch, `:60-85` `FigureMark`;
`provenance.ts:238` is `export const FIGURE_HREF = '#figure/'`, `:254` the
`if (returned.length === 0) return;` early exit, `:260` the `parent.type === 'link'` skip. All exact.

Chain to model output, checked independently of the finding:
- `MessageList.tsx:73` — `const body = message.finalText ?? message.streamedText;`, passed straight
  to `<Markdown figures={figures}>` at `:112`.
- `chatStore.ts:590` sets `finalText: event.text` from the `answer` event.
- Backend `src/chemclaw/api/runner_answer.py:50` — `AnswerEvent(text=answer, …)`; the model's answer
  string is copied verbatim, with no markdown transform anywhere on the path.
- The UI BFF (`server/proxy.ts`) is a byte proxy: `http.request` piped through, the only injection
  being `: hb\n\n` SSE comments at frame boundaries. No content rewriting.

### Why

Every claim in the finding reproduces on the real code path, on the arguments stated, and the
consequence is exactly as described: a fabricated figure renders with the affirmative styling and
the tooltip *"This figure matches a value a tool returned this turn."* — the single structured
provenance claim this UI makes.

Two things I found that the reporter did not, and that make it worse rather than better:

1. **The forged mark is byte-identical to the genuine one, in the same paragraph.** Case B is the
   realistic case (a turn that *did* call tools), and there the forged `98.4` and the honestly-marked
   `4.76` differ in not one character of class or `title`. There is no visual tell, no hover tell
   (the `#figure/` branch returns a `<span>`, never an anchor, so there is no href to inspect), and
   the plugin being switched on provides no protection whatever — it skips text under a `link`
   parent (`provenance.ts:260`), so the model's own link node passes through untouched. The
   chemist sees the same quiet green underline the design reserves for tool-returned values.
2. **The discredit direction needs no knowledge of the vocabulary.** `FigureMark` affirms only on an
   exact `'grounded'` and paints amber on *everything else* (case E). So one arbitrary suffix —
   `[4.76](#figure/x)` — is enough to stamp a genuinely tool-returned value with "Not among the
   values this turn's tools returned", which is the failure mode `provenance.ts:14-21` says the whole
   under-flag rule exists to prevent.

The one thing that keeps this at high rather than critical, and the finding does not overclaim it:
the model is not taught this syntax anywhere. `grep -rn "#figure/\|#cite/"` over
`/home/user/Chemclaw3/src` and `/home/user/Chemclaw3/skills` returns **zero** hits, so spontaneous
emission is unlikely and the realistic vector is injected instruction text — which this system takes
into model context by design (KG notes, ELN warehouse rows, mounted SMB documents, MCP tool output).
That is a precondition, not a mitigation: it is one line of text in one untrusted document, there is
no server-side or client-side filter between it and `FigureMark`, and the payload is trivially
short. High is the right severity.

The proposed fix is sound in both variants. I would add one constraint the finding leaves implicit:
whichever variant is taken, the `#cite/` branch needs the same treatment in the same change —
`CitationChip` currently accepts an arbitrary `kind` and `id` off the href with no check that
`remarkCitations` produced it, and the chip's `PALETTE` lookup means a forged `#cite/job/…` also
picks up the green `ok` tone.

---

## Out of scope

The remaining three findings are marked **medium**, **medium** and **low** by the reporter and were
not verified under this lens.
