# D-2026-10-03-model-written-html-runs-in-an-opaque-origin-the-backend-never-serves — the `html` artefact and where it may run

**Status:** accepted · **Date:** 2026-10-03 · **Owner decision** (2026-10-03). Wave 3 of
`D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect`, against the frozen wire contract
the frontend builds to. Reverses that ADR's decline of "artefacts that run code"; that ADR is merged
and stands as the record of why it declined them then.

**Superseded-by:** [D-2026-10-03-model-written-html-runs-its-scripts-by-default](D-2026-10-03-model-written-html-runs-its-scripts-by-default.md) (in part)

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
2. **Exfiltrate** — `fetch`, an `<img src>`, a form post, a navigation, a DNS prefetch, **or a
   WebRTC peer connection** (STUN over UDP), carrying what it read or what the model put in it.
3. **Reach outside its rectangle** — write the chemist's clipboard after one click, or navigate its
   own frame elsewhere with data in the URL.
4. **Deceive** — draw a fake sign-in, or rewrite the surrounding app to make the chemist act.

**What the sandbox review measured** (`Chemclaw3_ui`, 2026-10-03): inside
`sandbox="allow-scripts"` with the full CSP below, page script **can** still send data out over
WebRTC — CSP has no directive for it, and Chromium ignores a `webrtc 'block'` policy — and can set
the clipboard after one user click. So a sandbox that runs scripts closes (1) and most of (2), and
does not close all of it.

## Decision

1. **The backend never serves an artefact as `text/html`.** It stores the page as a string in the
   spec, serves it as JSON, and exports it as `text/plain; charset=utf-8` with
   `Content-Disposition: attachment` and an `.html` filename. Nothing on the API origin can be
   navigated to and render the page, so (1) is closed at this end whatever the page says: the page
   never executes on the origin holding the session. `tests/test_exhibit_routes.py` walks every GET
   route the app serves under an artefact, with every export format, and asserts no `text/html`.
2. **It renders only in the UI's sandbox origin**, which `Chemclaw3_ui` serves on a **separate
   listener and origin** (`SANDBOX_ORIGIN`) and the app embeds as
   `<iframe sandbox="allow-scripts">` — **no** `allow-same-origin`, so the frame's origin is
   *opaque*: it has no cookies, no storage and no DOM access to the app, even if the two origins were
   the same. The separate origin is defence in depth for a browser bug in the opaque-origin rule.
   The shell (the UI's own script, not the artefact) receives the page by `postMessage` from the
   app origin only and posts back only its height. Without a separate origin configured the UI
   shows escaped source, never a rendering.
3. **Scripts are off by default.** The shell writes the page into a nested `srcdoc` frame with
   `sandbox=""` — no scripts at all — so a page renders as markup and styles, and none of (2)-(3)'s
   script channels exists. A per-view **"Run scripts"** click, behind a warning naming the residual
   risks, re-renders it with `allow-scripts`; the choice is never persisted and resets on a new
   revision or a remount, so one click authorizes one page as the chemist is looking at it, not an
   artefact for good.
4. **In scripted mode the network is closed as far as the platform allows.** The CSP is
   `default-src 'none'; connect-src 'none'; form-action 'none'; base-uri 'none'`, images and fonts
   only as `data:`/`blob:`; with no `allow-popups`, `allow-top-navigation` or `allow-forms` the frame
   cannot open, navigate the app or post; `X-DNS-Prefetch-Control: off` leaves no prefetch. A
   prelude replaces `RTCPeerConnection`, `webkitRTCPeerConnection` and `RTCDataChannel` before the
   page's own script runs — **defence in depth only**: a page can reach a pristine constructor
   through a fresh realm, so it is documented as bypassable and not counted as a control. The
   deployment documentation recommends the browser policy
   `WebRtcIPHandling=disable_non_proxied_udp`, which is the control that actually holds.
5. **The residual risk, stated.** With scripts run, a page can still: send what it holds over
   WebRTC where the browser policy is not set; write the clipboard after a click; navigate its own
   frame, bounded only by the app's `frame-src`; and deceive inside its rectangle, which no header
   bounds — only where it sits, a pane labelled as an artefact the agent wrote. What it *holds* is
   bounded too: its own HTML and what the model put there, never the session, the transcript or
   another artefact. Production here is air-gapped, so even an egress channel reaches no host
   outside the cluster; on a connected workstation that is not true, which is why scripts are off
   until a person turns them on.
6. **The kind is on by default behind `agent_html_artefacts_enabled`**; off refuses a *new* html
   artefact (a 422, or a worded refusal to the model) while the ones a session holds still list, read and
   revise. `GET /sessions/{id}/exhibits` says which as `html_enabled`. `exhibit_max_html_bytes`
   bounds the page.
7. **Grounding runs over the page's visible text** — the character data the standard library's
   parser yields outside `<script>` and `<style>`, which includes an inline SVG's `<text>` labels;
   attributes are never read, so coordinates, widths and a script's numbers are not figures to it.
   What that misses is a figure a script draws from its own data, which goes unchecked: scanning
   code would flag every loop bound and pixel width instead. A page's figures are only ever
   transcribed, never bound.
8. **Migration `117_exhibit_html_kind.sql` widens the kind `CHECK`** the way 116 did. The previous
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
- **Scripts on by default**, as the contract first froze it. Declined after the sandbox review
  measured WebRTC egress and clipboard writes from a scripted frame: a page the model wrote is
  rendered without its scripts until a chemist asks for them.
  Revisit when: browsers expose a CSP directive or a Permissions-Policy feature that blocks WebRTC
  in a sandboxed frame and Chromium honours it (`webrtc 'block'` is the candidate it ignores today)
  — `Chemclaw3_ui`'s sandbox e2e is the test that would show the channel closed.
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
- The sandbox's own headers, the app listener refusing `/sandbox/frame`, scripts off until "Run
  scripts", the RTC prelude, and the browser e2e (no `fetch`, no `document.cookie`, no top
  navigation, height the only message) are `Chemclaw3_ui`'s tests, in the same wave; nothing here
  can run them.
