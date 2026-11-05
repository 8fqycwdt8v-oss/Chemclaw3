# The Teams room bridge — setup and verification

A Teams channel (or chat) as a surface of a shared ChemClaw session. Each message is posted to the
session **as the person who typed it**, on a token Entra issued for that person, so their roles,
gates and spend apply. The bridge is a separate process, `python -m chemclaw.rooms`, **off until
`CHEMCLAW_TEAMS_BRIDGE_ENABLED=true`**. The decision is
`docs/decisions/D-2026-10-24-a-chat-room-reaches-a-shared-session-only-as-its-senders.md`; the code
map is `src/chemclaw/rooms/README.md`.

Nothing here has been run against a real Microsoft tenant: the tests use a fake Teams and Entra
(`tests/fakes_teams.py`). Section 7 is the first live check, and the one step to watch is the silent
sign-in (step 3 and 7c).

## 1. Prerequisites

- ChemClaw's front door runs with `CHEMCLAW_ENTRA_REQUIRED=true`. The bridge probes this at start
  and refuses to run against a front door that accepts any token.
- Its API app registration already exists (the audience `CHEMCLAW_ENTRA_AUDIENCE` names). It issues
  v2.0 tokens: in its manifest, `requestedAccessTokenVersion` is `2`.
- That app exposes a delegated scope, for example `access_as_user`; its full form
  (`api://<chemclaw app id>/access_as_user`) is the value of `CHEMCLAW_TEAMS_API_SCOPE`.

## 2. The bot's Entra app registration

1. New registration, **single tenant**. Note its Application (client) id: `CHEMCLAW_TEAMS_BOT_APP_ID`.
   Note the Directory (tenant) id: `CHEMCLAW_TEAMS_TENANT_ID`.
2. **Certificates & secrets**: create a client secret. Put its value in the secret store and expose
   it to the bridge pod as the environment variable `CHEMCLAW_TEAMS_BOT_CREDENTIAL` (the name is the
   setting `CHEMCLAW_TEAMS_BOT_CREDENTIAL_TOKEN_ENV`; the value never goes in a file or a setting).
3. **Expose an API**: Application ID URI `api://botid-<bot app id>` (this is
   `CHEMCLAW_TEAMS_SSO_RESOURCE_URI`), add a scope `access_as_user`, and add the two Teams clients as
   authorised client applications of that scope: `1fec8e78-bce4-4aaf-ab1b-5451cc387264` (desktop and
   mobile) and `5e3ce6c0-2b1f-4285-8d4b-75ee78787346` (web).
4. **API permissions** (the on-behalf-of consent): add a *delegated* permission to ChemClaw's API
   scope, then **grant admin consent for the tenant**. Silent sign-in cannot ask a person for consent,
   so an unconsented tenant answers "needs an administrator's consent". Add **no** application
   permission or app role on ChemClaw's API: the bot must have nothing that mints a ChemClaw token
   without a person.
5. **Authentication**: add a Web redirect URI `https://token.botframework.com/.auth/callback`.

## 3. The Azure Bot resource

1. Create an Azure Bot, type **Single Tenant**, using the app registration above.
2. **Settings → Configuration → Messaging endpoint**: `https://<bridge host>/api/messages`.
3. **Channels**: enable Microsoft Teams.
4. **Configuration → OAuth Connection Settings → Add**: service provider *Azure Active Directory v2*;
   name `chemclaw-sso` (this is `CHEMCLAW_TEAMS_SSO_CONNECTION_NAME`); the bot's client id, secret
   and tenant id; scopes = ChemClaw's API scope; **Token Exchange URL** =
   `api://botid-<bot app id>`. This connection is what makes Teams offer the bot a silent token; the
   bridge exchanges that token itself and never asks the Token Service for one.
5. Teams app manifest (upload as a custom app, or publish to the org catalogue): `bots[0].botId` =
   the bot app id with scopes `personal`, `team`, `groupChat`; `webApplicationInfo` =
   `{ "id": "<bot app id>", "resource": "api://botid-<bot app id>" }`; `validDomains` includes
   `token.botframework.com`.

## 4. Configuration

Set on the bridge process (all in `.env.example`; none has a default that is a secret):

| Key | Value |
| --- | --- |
| `CHEMCLAW_TEAMS_BRIDGE_ENABLED` | `true` |
| `CHEMCLAW_TEAMS_BOT_APP_ID` | the bot's client id |
| `CHEMCLAW_TEAMS_BOT_CREDENTIAL_TOKEN_ENV` | name of the variable holding the secret (default `CHEMCLAW_TEAMS_BOT_CREDENTIAL`) |
| `CHEMCLAW_TEAMS_TENANT_ID` | the one tenant whose people may use it |
| `CHEMCLAW_TEAMS_API_SCOPE` | ChemClaw's delegated scope, full form |
| `CHEMCLAW_TEAMS_SSO_CONNECTION_NAME` | the OAuth connection's name |
| `CHEMCLAW_TEAMS_SSO_RESOURCE_URI` | `api://botid-<bot app id>` |
| `CHEMCLAW_TEAMS_API_BASE_URL` | the front door's URL inside the cluster |
| `CHEMCLAW_TEAMS_BINDINGS_FILE` | a path on a mounted volume (room -> session bindings) |

The rest (`CHEMCLAW_TEAMS_AUTHORITY_URL`, `..._OPENID_METADATA_URL`, `..._CHANNEL_TOKEN_ISSUER`,
`..._CONNECTOR_SCOPE`, `..._SERVICE_URL_HOSTS`, timeouts, the token cache bound, the activity size
cap) are defaults for public Microsoft; change them only for a sovereign cloud.

## 5. Deployment

Run the ChemClaw image with the command `python -m chemclaw.rooms`, **one replica**, the bindings
volume mounted read-write, the secret in the environment, port `CHEMCLAW_TEAMS_PORT` (3978)
published behind a TLS ingress. The chart has no role for it yet; this is a plain Deployment.

## 6. Egress (documented, not applied)

The bridge's own guard allows exactly these once the bridge is enabled; the cluster's
NetworkPolicy is yours to write.

| Direction | Destination | Why |
| --- | --- | --- |
| out | `login.microsoftonline.com:443` | the on-behalf-of exchange and the bot's reply token |
| out | `login.botframework.com:443` | Bot Framework's OpenID metadata and signing keys |
| out | `smba.trafficmanager.net:443` (and regional subdomains) | replies into Teams |
| out | the ChemClaw front door | posting messages |
| in | Bot Framework's service tag (`AzureBotService`) to 443 | activities |

Nothing else; in particular the bridge never dials a model gateway.

## 7. Verify

a. Start it: with the bridge off it exits naming the setting; pointed at a front door that is not
   enforcing tokens it exits saying so. `GET /healthz` answers `ok`.
b. In Teams, add the bot to a channel. `@ChemClaw !help` from a person who has not signed in shows a
   sign-in card and posts nothing to ChemClaw.
c. **The unrehearsed step:** within a second or two Teams should complete the silent sign-in and the
   bot should say "Signed in as <name>. Send your message again." If it says it needs consent, step
   2.4 was not granted; if nothing happens, the OAuth connection's token exchange URL (3.4) or the
   authorised clients (2.3) are wrong. The bridge's log says `on-behalf-of exchange refused: <error>`.
d. `@ChemClaw !new` creates a session owned by the person and binds the room. The owner adds the
   colleagues in ChemClaw (the conversation's People panel takes an Entra object id).
e. Two people write in the room: each answer in the thread is the turn run as the asker. In the
   audit trail the two messages carry two different actors.
f. A third person who was not added writes: the room is told they are not a member; nothing runs.
g. While one turn runs, a second person writes: the room is told their place in the line and when
   it starts.
h. `!status`, `!unbind` (binder only) behave as `!help` says. A restart keeps the binding.

## 8. Limits

- The silent sign-in only; there is no interactive consent fallback.
- Another member's browser turn is not mirrored into the room (that needs a member's token held
  with nobody present). Plan approvals stay in ChemClaw.
- One replica; a token is held in memory, so a restart costs each person one silent sign-in.
- Remove the bot from Teams, rotate the client secret and delete the bindings file to take it down.
