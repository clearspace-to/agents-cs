# Agents CS live voice

Media-only module of the `agents-cs` Hermes plugin (was the `hermes-bots-voice` plugin) (see NOTICE and LICENSE).
No new port, chat executor, agent tools or changes to Hermes core. The app submits
voice delegations through the same chat connection used for typed messages.

## Install on the Hermes host

The voice module ships inside the `agents-cs` plugin (`plugin/agents-cs`, see its README). The Agents CS
connector installs the plugin into every Hermes home (`hermes plugins install`) and restarts the dashboard
(`hermes serve`), which is the only process that imports these routes (and so `httpx`, `websockets`
and `pydantic`); a bot worker never loads this module's code.

The dashboard must apply its normal authentication middleware to `/api/plugins/…`.
Do not mount this router as a standalone public API. The app shares its existing
authenticated cookie session. Gated requests use Hermes' verified
`request.state.session`: a SHA-256 digest of provider, organization and user ID binds
calls to that account, profile and stored chat across token refreshes. Caller-selected
legacy headers never override this identity. In legacy loopback mode only, the app
sends `X-Hermes-Session-Token`, validated by Hermes' plugin-route middleware and then
hashed for ownership. Unconfigured standalone routers, service-token grants and
competing loopback Authorization headers are rejected. No provider tokens are
returned to the phone.

The native client requires an already opened, stored chat. Calls use the Codex
provider and the host's Hermes credential resolver (the host's Codex subscription). Actual
account access is checked on call setup; status alone does not prove eligibility. The
API-key provider was removed in 1.3.0 (Mark, 2026-10-12); `provider` must be `codex_subscription`. The subscription endpoint is an upstream
integration, not an availability guarantee established by public API documentation.

## Contract

POST `/api/plugins/agents-cs/voice/{status,offer,poll,result,note,close,voices,voice_set}`
(was `/api/plugins/hermes-bots-voice/voice/…`; same subpaths and bodies).
All bodies contain `agentId` and `sessionId` (the stored chat ID).

- `status`: `provider` (`codex_subscription`). No provider call.
- `offer`: one-use `voiceId`, `provider`, `voice`, and audio-only `sdp`; returns answer SDP.
- `poll`: `voiceId`, sequence cursor `after`; up to four events, `next`, `closed`.
- `result`: `voiceId`, known `delegationId`, `text` (up to 1,500 characters); never replayed.
- `note`: `voiceId`, `text` (up to 1,600 UTF-8 bytes, no control characters except tab and
  newlines), `speak` (bool); returns `noted`. `speak: false` is silent background progress the
  model can use to answer "what's happening?"; `speak: true` asks for a short spoken check-in.
  At most 64 notes and 16 spoken notes per call (`voice_note_limit`).
- `close`: `voiceId`; retires media, never cancels an accepted Hermes turn.

When a delegation is admitted the model is told to say a short acknowledgement ("On it, give
me a sec, let me check that.") before handing off. If no assistant speech arrives within about
1.2 s of the handoff, the host nudges it once on the speakable channel; at most one nudge per
5 s per call. A failed nudge never ends the call.

Input is capped at 200 KB, events are bounded, replies are no-store, and call
leases expire if polling stops. Errors contain fixed codes, never provider payloads.
Closed IDs remain consumed for the process lifetime (upstream's 1,024-call cap);
a dashboard restart resets the disposable hub. Four active calls maximum.

Audio flows between the phone and selected provider. The host receives captions
and delegation events over a sideband connection. Captions and media state remain
in memory; delegated messages/results live in ordinary Hermes chat history.
Changing chat, backgrounding, losing the host connection, or closing the panel
ends the phone call. Already submitted work can finish independently. Unsent
queued delegations are discarded. Approvals stay visible in the transcript.

## Protocol v2 (plugin 1.1.0, capability `native-voice-v2`)

`status` now also returns `features: ["long_poll", "streamed_results", "context", "long_notes"]`.
Every new request field is optional and defaults to v1 behaviour, so apps that never send them
(every app before v2) are unaffected. A v1 plugin rejects unknown fields with 422, so an app
sends them only when `status` lists the feature.

- `long_poll` — `poll` takes `wait` (ms, 0–10,000, default 0) and `limit` (1–32, default 4).
  With `wait > 0` the reply comes as soon as an event arrives or the call closes, otherwise
  after `wait` with no events. A waiting poll keeps the call's lease alive. Replies stop adding
  events past 150,000 bytes (always at least one). Up to four polls per call wait at once; more
  answer immediately. Same `voice_owner_changed` / `voice_feed_gap` errors as v1.
- `streamed_results` — `result` takes `part` (0–255, default absent) and `final` (bool, default
  true). Without `part` it is the v1 single, final result. With it, parts for one delegation
  must arrive in order from 0, each once; after a `final: true` part (or the 64th part) the
  delegation takes no more. A wrong, repeated or late part is `voice_result_unavailable` (409).
  A part that failed to send is still spent: parts are never replayed. Each part is up to 1,500
  characters. `appendedResults` in poll diagnostics counts delegations finished (a v1 result or
  a final part), so it compares with `admittedDelegations`.
- `context` — `offer` takes `context`: up to 16 `{"role": "user"|"assistant", "text"}` entries
  (1–800 characters each), oldest first, from the chat the call starts in. The host drops the
  oldest entries until the rest total 7,500 UTF-8 bytes or less and pass the provider's own
  checks, then gives them to the voice model as background data. Malformed entries are 422.
- `long_notes` — `note` text up to 1,600 bytes and 16 spoken notes per call, so the app can
  hand the model a result that finished before a reconnect.

Call setup also reuses one keep-alive HTTPS client per dashboard event loop (no cookies, no
redirects, no proxy settings), so a new call skips a fresh TLS handshake.

## Verification

Run `scripts/test.sh --python` from the repository root. Tests use injected provider
fixtures only. For real acceptance: start a call on a device, request work, answer
an approval, interrupt/mute, change audio route, end during a tool run, background,
and disconnect/reconnect. Confirm one durable chat submission per delegation and
no automatic provider fallback. Physical audio and subscription access require
an actual configured host and device and are not established by unit tests.

## Voice choices

`voices` (`{"agentId"}`) returns each provider's chosen voice for that bot; `voice_set`
(`{"agentId", "provider", "voice"}`) saves one. They're stored in
`$HERMES_HOME/hermes-bots-voice/voices.json` (the Hermes home's own API; the folder name is unchanged), outside the plugin directory so reinstalling keeps
them, and shared by every device signed in to this host.

## Voice samples (plugin 1.2.0)

The app's settings play a voice sample as a short call with `sessionId: "voice-sample"`. A new `offer`
from the same sign-in first closes any open sample call of that sign-in (one left behind when the app
was closed mid-sample), so it never waits out the lease. A real call is never closed this way: a sample
offer while a call is active is still `voice_already_active`.
