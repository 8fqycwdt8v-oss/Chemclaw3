# D-2026-10-03-model-written-html-runs-in-an-opaque-origin-the-backend-never-serves — the `html` artefact and where it may run

**Status:** accepted · **Date:** 2026-10-03 · **Owner decision** (2026-10-03). Wave 3 of
`D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect`, against the frozen wire contract
the frontend builds to. Reverses that ADR's decline of "artefacts that run code"; that ADR is merged
and stands as the record of why it declined them then.

## Context

The earlier record declined model-authored HTML for one reason, and it is still true: **a tool
result is untrusted text that can carry injected instructions, and an artefact that executes is the
channel that would carry them into a browser.** Its trigger was `Chemclaw3_ui` serving a sandbox
origin with `connect-src 'none'`. The owner chose on 2026-10-03 to build that sandbox and the kind
together, so the trigger is met by construction in the same wave; this record states the threat
model the design answers, because "the trigger fired" is not by itself an argument that the bound
holds.

## The threat

The chain is: a fleet server answers from a vendored corpus or a document a chemist uploaded → the
text reaches the model as a tool result (framed, defanged, but *read*) → an instruction in it steers
the model → the model writes an `html` artefact → a chemist's browser renders it. What the page
could then try:

1. **Read or act with the chemist's session** — call `/sessions/…` with their cookies or bearer,
   read the transcript, post messages, approve a plan.
2. **Exfiltrate** — `fetch`, an `<img src>`, a form post, a navigation, a DNS prefetch, carrying
   what it read or what the model put in it.
3. **Deceive** — draw a fake sign-in, or rewrite the surrounding app to make the chemist act.

## Decision

1. **The backend never serves an artefact as `text/html`.** It stores the page as a string in the
   spec, serves it as JSON, and exports it as `text/plain; charset=utf-8` with
   `Content-Disposition: attachment` and an `.html` filename. Nothing on the API origin can be
   navigated to and render the page, so (1) is closed at this end whatever the page says: the page
   never executes on the origin holding the session. `tests/test_exhibit_routes.py` walks every GET
   route the app serves under an artefact, with every export format, and asserts no `text/html`.
2. **It runs only in the UI's sandbox origin**, which `Chemclaw3_ui` serves on a **separate
   listener and origin** (`SANDBOX_ORIGIN`) and the app embeds as
   `<iframe sandbox="allow-scripts">` — **no** `allow-same-origin`, so the frame's origin is
   *opaque*: it has no cookies, no storage and no DOM access to the app, even if the two origins were
   the same. The separate origin is defence in depth for a browser bug in the opaque-origin rule.
   The frame receives the page by `postMessage` from the app origin only and posts back only its
   height. Without a separate origin configured the UI shows escaped source, never inline.
3. **Its CSP closes the network**: `default-src 'none'; connect-src 'none'; form-action 'none';
   base-uri 'none'`, images and fonts only as `data:`/`blob:`. With no `allow-popups`,
   `allow-top-navigation` or `allow-forms`, the frame cannot open, navigate or post anywhere —
   which is (2) — and `X-DNS-Prefetch-Control: off` with no external `src` allowed leaves no
   resolver lookup to carry data either.
4. **What is left is (3), deception inside the frame's own rectangle**, which no header bounds. It is
   bounded by where the frame sits — a pane labelled as an artefact the agent wrote, beside the
   chat — and by the deployment: production here is air-gapped, so even a channel this design
   missed reaches no host outside the cluster.
5. **On by default behind `agent_html_artefacts_enabled`**; off refuses a *new* html artefact (a
   422, or a worded refusal to the model) while the ones a session holds still list, read and
   revise. `GET /sessions/{id}/exhibits` says which as `html_enabled`. `exhibit_max_html_bytes`
   bounds the page.
6. **Grounding runs over the page's text** — the character data the standard library's parser
   yields, `<script>` included (a chart drawn from a JS array states those figures) and `<style>`
   excluded (lengths are layout). A page's figures are only ever transcribed, never bound.
7. **Migration `117_exhibit_html_kind.sql` widens the kind `CHECK`** the way 116 did. The previous
   image writes every kind it knows and cannot open an html revision — nor, with no schema change of
   its own, a revision that binds a value; the runbook's rollback table says so.

## Options considered

- **Keep code-running artefacts declined.** The owner chose to build, with the sandbox.
- **Sanitise the HTML server-side** (an allow-list of tags) and render it inline in the app.
  Declined: a sanitiser's correctness is the whole bound, it is the component with the longest
  history of bypasses, and it would remove the scripts that are the reason for the kind. The
  opaque-origin frame bounds what the page can *reach*, which holds whatever the page contains.
- **Serve the page from the backend** under its own path with a strict CSP. Declined: it puts
  model-written markup on the origin that holds the session, one mis-set header from (1).
  Revisit when: a deployment cannot run the UI's second listener and asks to render pages anyway —
  the answer is still not this origin, but the question is worth a record.
- **Allow `connect-src` to a local data endpoint** so a page could fetch artefact data. Declined:
  any network the frame has is an exfiltration channel. A page carries its data inline.
  Revisit when: a page that must read more than `exhibit_max_html_bytes` of data is asked for by a
  chemist — inline is the bound until then.

## What keeps it true

- `tests/test_exhibit_routes.py::test_no_route_answers_text_html_for_artefact_content`, and the
  export, the switch, the page cap, the grounding and the agent tool beside it.
- `tests/test_exhibit_models.py` and `tests/test_event_contract.py` — the kind in the spec union and
  in the published `exhibit` event.
- `tests/test_migrations_are_additive.py` — 117's one reviewed statement, and the runbook row.
- The sandbox's own headers, the app listener refusing `/sandbox/frame`, and the browser e2e
  (no `fetch`, no `document.cookie`, no top navigation, height the only message) are
  `Chemclaw3_ui`'s tests, in the same wave; nothing here can run them.
