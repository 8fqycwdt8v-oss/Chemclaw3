# D-2026-10-03-model-written-html-runs-its-scripts-by-default — html artefacts render with scripts on, behind a kill switch

**Status:** accepted · **Date:** 2026-10-03 · **Owner decision** (2026-10-03). Supersedes decision 3
("Scripts are off by default") and the "Scripts on by default" decline of
`D-2026-10-03-model-written-html-runs-in-an-opaque-origin-the-backend-never-serves`, which is merged
and stands as the record of why scripts were off; every other decision of that record holds.

## Context

The sandbox review measured two channels a scripted page keeps inside
`sandbox="allow-scripts"` with the full CSP: **WebRTC egress** (STUN over UDP — CSP has no directive
for it and Chromium ignores `webrtc 'block'`) and a **clipboard write** after one click. On that
measurement the earlier record rendered pages without scripts until a per-view "Run scripts" click.
In use, the pages the agent writes are interactive more often than not — a sortable table, a chart
with a tooltip — so the default rendered most of them broken until clicked, and the click was
becoming a reflex rather than a decision. The owner chose on 2026-10-03, in the final review of the
hardening contract, to render with scripts by default and to keep the channels bounded by the
controls that do hold rather than by the click.

## Decision

1. **Scripts run by default.** `Chemclaw3_ui`'s `HTML_SCRIPTS_DEFAULT=on|off` (default **on**),
   published in `/config.js` as `htmlScriptsDefault`. With `on` the page renders in the nested frame
   with `allow-scripts` and the RTC prelude immediately; the per-view control becomes **Disable
   scripts**, never persisted. With `off`, the earlier record's "Run scripts" behaviour, unchanged.
   The backend's `agent_html_artefacts_enabled` is unchanged: a create-time switch for the kind.
2. **The mitigations kept, unchanged:** the page never executes on the API origin (the backend
   never answers `text/html` for an artefact); it renders only on the UI's separate sandbox origin,
   in an opaque-origin frame with no `allow-same-origin`, `allow-popups`, `allow-top-navigation`,
   `allow-forms` or `allow-modals`; the CSP with `connect-src 'none'`, `form-action 'none'` and
   `data:`/`blob:`-only images and fonts; the app's `frame-src` naming the sandbox origin alone; the
   `ready` handshake (contract item 3) so the app sends a page only to its own armed shell; the RTC
   prelude as defence in depth.
3. **The browser policy is the control for WebRTC, and the runbook makes it a deployment step**:
   `WebRtcIPHandling=disable_non_proxied_udp` on Chrome and Edge, `media.peerconnection.enabled=false`
   on Firefox. Production here is air-gapped as well, so even an open channel reaches no host
   outside the cluster.
4. **The residual risk, stated.** Where the browser policy is not applied, a page the model wrote
   can send what it holds — its own HTML and what the model put in it, never the session, the
   transcript or another artefact — over WebRTC, without anybody clicking; it can write the
   clipboard after a click; and it can navigate its own frame, bounded by `frame-src`. What it can
   reach is unchanged; what changed is that no click precedes it.
5. **The kill switch is `HTML_SCRIPTS_DEFAULT=off` on the UI**, a configuration change with no
   backend release, which restores scripts-off rendering for every viewer. Turning the kind off
   altogether stays `CHEMCLAW_AGENT_HTML_ARTEFACTS_ENABLED=false`.

## Options considered

- **Keep scripts off by default** (the superseded decision). Declined by the owner: it rendered
  most pages broken and turned the click into a reflex, which bounds nothing.
- **Scripts on with no kill switch.** Declined: a browser regression or a deployment that cannot
  apply the policy needs a way back that is not a release.
- **Ship a stricter prelude and count it as the WebRTC control.** Declined: a page reaches a
  pristine constructor through a fresh realm, so it is bypassable by construction.

Revisit when: a deployment cannot apply the WebRTC browser policy to the browsers its chemists use
and is not air-gapped — set `HTML_SCRIPTS_DEFAULT=off` there and reopen this record — or when
Chromium honours a CSP or Permissions-Policy control that blocks WebRTC in a sandboxed frame, which
`Chemclaw3_ui`'s sandbox e2e would show and which would retire the browser-policy step.

## What keeps it true

- Backend: `tests/test_exhibit_routes.py::test_no_route_answers_text_html_for_artefact_content` and
  the html switch tests — unchanged, and still the reason a page cannot reach the session.
- `Chemclaw3_ui`: the scripts-default setting, the `Disable scripts` control, the handshake and the
  sandbox e2e are that repository's tests; nothing here can run them.
- `docs/guides/runbook.md`, the artefacts section — the browser policy and the kill switch as
  deployment steps.
