# Jev routing 2.4 — implemented app API

Plugin source: `plugin/agents-cs/routing` (the `routing` module of the `agents-cs` plugin; was the `jev-routing` plugin in scripts/jev/plugin of clearspace-to/hermes-agents-mg, see SOURCE.md). No Hermes core edits.

**Prefix change (agents-cs 2.0):** every route below is now served at `/api/plugins/agents-cs/routing/<op>` (it was `/api/plugins/agents-cs/routing/<op>`); subpaths and bodies are unchanged. Routing is off unless configured: `plugins.entries.agents-cs.settings.routing_mode` (`off`|`shadow`|`on`), the old `plugins.entries.jev-routing.settings.mode`, or an existing `<home>/jev-routing-v2.sqlite`; a bot that isn't configured answers `404 agent_not_found`. `GET /capabilities` also carries `classifierAvailable` (false when the host has no OpenRouter key: routing keeps the chat's own model).

## 2.4: the plugin routes every turn itself (current contract)

Since 2.4 the app no longer plans, switches or restores models. When a chat's effective **Auto model** and/or **Auto effort** is on, the plugin picks the slot, model and effort on the turn's first provider request and applies them to every provider request of that turn, through Hermes' supported `llm_request` middleware (documented for "adaptive routing"; the request is rewritten, never Hermes). The chat's own model, provider, effort and session config are never written, so there is nothing to restore, nothing to recover after a relaunch, and no window where another writer can see a routed model.

**What the app calls now:** `GET /capabilities` (gate on `serverRouting:true`), `GET/PUT /policy` (bot defaults and slots), `GET/PUT /session` (the chat's Auto model / Auto effort overrides and its acknowledged manual pick), and optionally `GET /turns` for labels. Then it sends with the ordinary `prompt.submit`. Nothing else.

`GET /turns?agentId=friday&sessionId=CANONICAL_ID` returns the last 20 server-routed turns, newest first, content-free:

```json
{"turns":[{"turnId":"…","at":1790866000,"autoModel":true,"autoEffort":true,"slot":"small","reason":"classified","coding":false,
  "baseline":{"provider":"openai-codex","model":"gpt-6.1-sol","effort":"medium"},
  "served":{"provider":"openai-codex","model":"gpt-6-luna","effort":"low"},"switched":true,"applied":true,"classifierMs":180}]}
```

`served` is what the provider was asked for (Hermes' own `session.info`/usage keep reporting the chat's model, and cost estimates use the chat model's prices). `applied:false` means shadow mode. A turn with no entry was not routed (both switches off, a manual pin on an unmanaged chat, a `/command` or explicit "use … model/reasoning" request, or a non-interactive surface).

Rules the plugin applies (same classifier, slots and coding floor as before):

- Surfaces: `tui` (the native app and dashboard chats), `desktop`, `cli`. Cron, kanban, gateways and voice are never routed.
- App-managed chats (any PUT /session) follow their switches even with a manual pick; the app turns Auto model off when the person picks a model, as before. Unmanaged chats keep a manual pick unless that switch was explicitly turned on for them.
- **Same provider only.** Hermes holds one provider client per chat, so a route can only change the model inside the provider the chat already uses. A chat on Claude with OpenAI slots (or the reverse) keeps its own model (`reason:cross_provider_unavailable`); Auto effort still applies. Slots already share one provider (2.3).
- Token bound: a different model needs the whole request ≤180,000 tokens by a conservative bytes/3 + 8,000 estimate, checked on every Codex request of the turn (`context_unverified_or_too_large`, `context_grew`).
- Codex: a routed request carries none of the chat model's sealed reasoning and asks for none of its own, so neither model is handed the other's encrypted reasoning; a Fast (`service_tier`) or natively compacted request keeps the chat model (`fast_mode`, `native_compaction`).
- Claude: model, max output, thinking/effort and sampling fields are rebuilt with Hermes' own model tables; prior-turn thinking signed by a different model is dropped. Manual-thinking (budget) models and Fast on a model without Fast keep the chat model.
- Small turns may only run web_search, web_extract and read_file. On Codex a blocked tool moves the rest of the turn to Medium and the model is told to call it again; on Claude the model stops and the chat's next turn starts at Medium. Luna/Haiku coding delegates stay blocked.
- A routed request the provider refuses (400/401/403/404/422) sends the rest of the turn on the chat's own model and skips that model for 15 minutes (`route_failed`, `target_failed_recently`). Classifier timeout/uncertainty keeps the chat's model and effort; known code goes Big.
- Late changes win: switches are re-read on every request, so turning a switch off mid-turn returns the next request to the chat's own model and effort.

**Deprecated (kept for 2.3 TestFlight builds):** `POST /plan`, `GET /plan`, `POST /plan/arm`, `POST /plan/cancel`, the `rpc` one-turn switch and everything under "POST plan", "Arm, cancel and status" and "Exact native RPC order" below. `execution` stays `app_one_turn_switch` so those builds keep working; an armed plan that matches the turn still owns it (the plugin does not route it a second time). Remove these once no 2.3 build is in use.

Capabilities 2.4 adds `serverRouting:true`, `serverRoutingSurfaces`, `appPlanDeprecated:true`; `atomicDispatch` is now true, `crossProviderRoutes` false (server routes), `requiresAppIntegration` false.

## Transport and identity

Use the SAME Hermes base URL and authenticated URLSession as the app already uses. Base path: `/api/plugins/agents-cs/routing`. API responses use `Cache-Control: no-store`. Native authenticated sessions use existing Hermes sign-in/cookies; never add a second Jev login. Legacy loopback mode uses the existing `X-Hermes-Session-Token` issued by Hermes, only when that mode is enabled. Never put the OpenRouter key in the app. Unauthenticated or service-token-only callers receive 401.

`agentId` is `friday` (alias `default`) or actual profile name `jarvis`; later profiles must have v2 installed. `sessionId` on these REST calls is the canonical persisted session key/ID, NOT the short WebSocket runtime ID. Use the canonical ID returned by the app's existing session open/resume flow. Each REST request verifies that the canonical session belongs to that bot's database and, when owner-stamped, the authenticated `provider:user`. A plugin session is also bound to the authenticated account. Legacy ownerless sessions have the same dashboard-management boundary as existing Hermes; this is not a new multi-tenant permissions system.

Existing JSON-RPC calls below use `session_id` = the live WebSocket runtime ID. Never interchange the two IDs. Reacquire the canonical ID after a compression/session lineage transition; initialize the new REST session and preserve the user's selected baseline. The app's selected model and the actual routed model are separate properties.

All examples use illustrative session IDs. Requests accept only listed fields. PUT is compare-and-swap; read back revisions, never fabricate them.

## GET capabilities

`GET /capabilities?agentId=friday`

```json
{
  "schemaVersion": 2,
  "pluginVersion": "2.2.0",
  "agentId": "friday",
  "execution": "app_one_turn_switch",
  "atomicDispatch": false,
  "independentRoutingControls": true,
  "supportedProviders": ["openai-codex"],
  "models": [{"provider":"openai-codex","model":"gpt-6-luna","efforts":["low","medium","high"]}],
  "catalogSource":"hermes_provider_catalog",
  "entitlementCheckedOnSwitch":true,
  "deadlineMs":500,
  "codingFloor":"medium",
  "uncoveredSurfaces":["direct_cli","cron","third_party_senders","realtime_voice"],
  "requiresAppIntegration":true
}
```

Actual models come from Hermes' catalog, cached five minutes, limited to supported Codex Luna/Sol/Astra families. The catalog may include fallback entries; the native model switch is the final availability/credential check. v2.3: routes may target `anthropic` (Claude subscription) or `openai-codex` models from Hermes' catalog (`supportedProviders`, `crossProviderRoutes: true`); Medium/Big refuse Luna and Haiku. Manual selections can use another provider as the baseline; routing off preserves it. Selected baseline effort accepts existing supported labels including none/xhigh/max/ultra; routing slots currently use low/medium/high.

## GET/PUT profile policy

`GET /policy?agentId=friday` returns a persisted document:

```json
{
 "schemaVersion":2,"revision":1,"autoModelDefault":false,"autoEffortDefault":false,
 "slots":{
  "small":{"provider":"openai-codex","model":"gpt-6-luna","effort":"low"},
  "medium":{"provider":"openai-codex","model":"gpt-6.1-sol","effort":"medium"},
  "big":{"provider":"openai-codex","model":"gpt-6.1-sol","effort":"high"}
 },
 "codingFloor":"medium","complexCodeSlot":"big","onUnknown":"selected","deadlineMs":500
}
```

Initial Medium/Big prefer the profile's currently selected Sol; they do not change `model.default`. Missing catalog choices initialize as null. Saved exact model IDs do not auto-upgrade. First migration preserves the previously authorized on/off setting; execution still requires the app sequence. Enabling Auto model requires all three valid slots; effort-only does not. Medium/Big cannot be Luna. Big may be configured to available Astra.

`PUT /policy` body (do not send the GET response wholesale):

```json
{
 "agentId":"friday","expectedRevision":1,"autoModelDefault":false,"autoEffortDefault":false,
 "slots":{
  "small":{"provider":"openai-codex","model":"gpt-6-luna","effort":"low"},
  "medium":{"provider":"openai-codex","model":"gpt-6.1-sol","effort":"medium"},
  "big":{"provider":"openai-codex","model":"gpt-6-astra","effort":"high"}
 }
}
```

Returns the policy with `revision:2`. Same calls with `agentId:jarvis` configure Jarvis independently. Set `autoModelDefault` and `autoEffortDefault` independently. Both are deployed false. Model off preserves the selected model; effort off preserves selected effort. Both off bypass classification. Existing explicit session overrides still win. Reviews remain available. Do not imply a profile toggle changes every explicit session override.

## GET/PUT session selection

`GET /session?agentId=friday&sessionId=CANONICAL_ID`

```json
{"revision":1,"autoModelOverride":null,"autoEffortOverride":null,"selectedBaseline":{"provider":"openai-codex","model":"gpt-6.1-sol","effort":"low"},"managed":false}
```

On first read the backend derives a baseline from that session's persisted model/config, falling back to the profile; merely reading does not change routing. Compare against the actual native `model.options`/`config.get` state before the first plan. Explicitly PUT any newer user selection. Do not overwrite the baseline from routed `session.info` events.

`PUT /session`:

```json
{"agentId":"friday","sessionId":"CANONICAL_ID","expectedRevision":1,"autoModelOverride":false,"autoEffortOverride":true,"selectedBaseline":{"provider":"openai-codex","model":"gpt-6.1-sol","effort":"low"}}
```

Returns revision 2 and `managed:true`. Each of `autoModelOverride` and `autoEffortOverride` independently accepts null (inherit profile), true (on), or false (off). A session becomes app-managed after a PUT or its first plan. A matching armed plan skips the built-in effort fallback, preventing duplicate classification. Without a matching plan, v2 can still adjust effort on the selected model, even if this session previously used the app API. PUT records the selected baseline; it does NOT execute a native model switch. For a manual choice, finish the existing native session switch first, then PUT its acknowledged selection. If either fails, re-read and reconcile; never display an optimistic successful save.

## POST plan (deprecated in 2.4)

Only request a plan when this session is idle and this prompt is at the head of the app's serialized queue. Every app send path shares the same coordinator. Queue plaintext in existing app storage, never route every queued item at enqueue time.

`POST /plan`:

```json
{
 "agentId":"friday","sessionId":"CANONICAL_ID",
 "idempotencyKey":"client-turn-uuid",
 "expectedPolicyRevision":2,"expectedSelectionRevision":2,
 "text":"Fix the bounded parser bug.",
 "history":[{"role":"assistant","content":"We isolated the failing parser function."}],
 "taskKind":"code","hasAttachments":false,"inputTokens":42000
}
```

Required: agentId/sessionId/idempotencyKey/expectedPolicyRevision/expectedSelectionRevision/text. Optional: history (max four ordinary messages, 4,000 chars each; no tools/system), taskKind general|code, hasAttachments boolean, inputTokens integer. text ≤12,000 chars, body ≤40,000 bytes. Mark explicit coding/Auto-code entry points `taskKind:code`; use general for ordinary chat. Jev separately classifies code. Known code with ambiguous complexity/error routes Big. Bounded ordinary code routes Medium; simple tasks cannot put code on Small. The plugin never grants permission to execute tools.

`inputTokens` means a conservative upper bound for the FULL next request (system, schemas, history, attachments, new message), not just prompt length or cumulative session usage. If unavailable, omit it. Cross-model switching is refused/falls back to selected at unknown or >180,000 input tokens in v2.2; a coding turn on a selected Luna may instead receive a 409 requiring a validated larger model. The cap is intentionally conservative; Hermes still performs final model-specific budgeting. Images/attachments bypass Jev and preserve selected unless explicit coding requires Big. No silent truncation to force Luna.

Example response:

```json
{
 "routeId":"server-route-uuid","status":"ready","expiresAt":1790866000,
 "policyRevision":2,"selectionRevision":2,"slot":"medium",
 "baseline":{"provider":"openai-codex","model":"gpt-6.1-sol","effort":"low"},
 "target":{"provider":"openai-codex","model":"gpt-6.1-sol","effort":"medium"},
 "reason":"classified","coding":true,"applied":false,"classifierMs":165,
 "promptDigest":"sha256-of-exact-text","requiresSwitch":true,
 "execution":"app_one_turn_switch",
 "rpc":{"method":"config.set","params":{"key":"model","value":"gpt-6.1-sol --provider openai-codex --once --reasoning medium"}}
}
```

Off/error/unknown generally returns `slot:null`, the selected target, `requiresSwitch:false`, `rpc:null`. Reasons also include routing_off, kill_switch, uncertain, deadline, api_error, router_busy, code_uncertain, sensitive_input, attachment_context, explicit_choice, context_unverified_or_too_large. Explicit known code uses Big on classification failure. Duplicate identical POST returns the same stored plan, without another Jev call. Same key/different payload is 409. A different outstanding plan for the session is 409. Reservations expire after 120 seconds. Persist only plan metadata, never prompt/history/key in plugin state. A repeated expired plan remains expired: do not submit it; re-read settings and create a new turn key.

## Arm, cancel and status (deprecated in 2.4)

`POST /plan/arm` and `POST /plan/cancel` body:

```json
{"agentId":"friday","sessionId":"CANONICAL_ID","routeId":"server-route-uuid"}
```

Arm verifies idle state, expiry and current policy/selection revisions. It returns the same plan with `status:armed`. Arm before the native one-turn switch; recheck it immediately before submission. Cancelling marks the reservation cancelled; it does NOT undo a native switch or stop an already-running turn. An already started/observed plan cannot be cancelled through this endpoint.

`GET /plan?agentId=friday&sessionId=CANONICAL_ID&routeId=server-route-uuid`

Returns ready → armed → started → observed, or cancelled. `started` means matching exact text reached the pre-turn hook. `observed` means the provider request was seen; it adds `turnId`, `actualModel`, `actualProvider`, `actualEffort`, and sets applied only when all three match target. It does NOT prove inference succeeded or the task completed. Use normal Hermes completion/error events for that. The same record stays observed after completion; there is no completion endpoint to call.

Small-turn tools are restricted to known read-only web_search/web_extract/read_file. Other tools return a block message instructing continuation through Medium/Big; no blocked tool is run. Explicit Luna delegates from app-managed sessions are blocked too. The app should offer “Continue with Medium/Big” as a NEW turn carrying the blocked task, never replay completed side effects. A conceptual code explanation may be Small, but unexpected code execution cannot proceed there.

## Exact native RPC order (deprecated in 2.4)

These are EXISTING Hermes JSON-RPC methods over the app's existing authenticated WebSocket `/api/ws`; all use the live runtime ID. Keep the existing client framing, response and event handling.

1. Resolve/open the chat using the existing flow. Get `model.options` with `{"session_id":"RUNTIME_ID"}`; get `config.get` with `{"session_id":"RUNTIME_ID","key":"reasoning"}`. Retain these as the user-selected baseline, not the last routed answer. Fetch capabilities, policy and plugin session. PUT session if the user selection differs.
2. Wait for existing app/backend idle and drain only one queued prompt. POST plan, then POST arm. If cancelled/expired/conflict, do not switch or submit.
3. If requiresSwitch, use returned rpc method/params, adding session_id. Example full frame:

```json
{"jsonrpc":"2.0","id":41,"method":"config.set","params":{"session_id":"RUNTIME_ID","key":"model","value":"gpt-6-luna --provider openai-codex --once --reasoning low"}}
```

Honor `confirm_required`/`confirm_message`: show the existing cost confirmation and only resend with `confirm_expensive_model:true` after approval. Never auto-confirm. Honor `deferred:true`: WAIT for idle and acknowledgement, revalidate plan/selection, never submit on the old model. For no switch, verify native state still equals the baseline. Do not alter Fast automatically; if an existing Fast/service tier is not supported by the target, pause routing and keep baseline until the user resolves it.

4. Read `model.options` and `config.get reasoning` again; require their actual provider/model/effort to equal target. POST arm again to validate policy/selection/expiry, and verify queue ownership/idle. Then send the same exact text via existing prompt.submit:

```json
{"jsonrpc":"2.0","id":42,"method":"prompt.submit","params":{"session_id":"RUNTIME_ID","text":"Explain a token."}}
```

The text is the same text used in POST plan; the two different examples above illustrate different tasks and MUST NOT be mixed. Attachments use the app's existing attach flow before planning; never drop them. Do not put routeId in the user's text or invent prompt.submit metadata fields. No routing approval automatically submits user content.

5. GET plan status to confirm observed actual target, then continue normal message/turn events. Hermes' --once lifecycle restores the prior selected model after the turn. Read model.options/config.get after terminal completion and verify baseline; if not restored, correct through existing session-scoped config.set only if the user's selection revision is unchanged. Never replace a newer manual pick. Persist an app pending-turn journal for disconnect/restart; reconcile status and existing turn events before any retry. Never blindly re-submit after a timeout, lost ACK, partial output or tool execution.
6. If submission fails BEFORE acceptance but AFTER switch, cancel route and restore baseline using `config.set` value `<baseline.model> --provider <baseline.provider> --session --reasoning <baseline.effort>`, provided the selected revision is unchanged and session idle. If acceptance is uncertain, reconcile first; do not send again. Re-read profile/session revisions before the next prompt.

### Explicit integration boundary

`atomicDispatch:false` is intentional. Hermes has no supported atomic switch+submit call. Plugin reservations coordinate callers using this API but cannot fence an old app, CLI or another arbitrary Hermes client. The new app must serialize all its writers per canonical session and must not enable automatic model switching on a session concurrently controlled by an unfenced external client. Multi-device app sends share the reservation. A paused/crashed holder expires; it must never resume sending from an expired plan. Routing is unsuitable for a Main Chat with uncontrollable concurrent writers until they use this sequence too. Leave manual selection intact and show routing unavailable there. Do not represent GET/POST plan as backend-owned inference or claim the coding floor covers unrelated CLI/cron sessions.

## Error handling

All errors: `{"code":"machine_readable_code"}`; no raw provider exception or credential is returned.
401 sign_in_required → reconnect/sign in.
404 agent_not_found/session_not_found/route_not_found → refresh identities, never retry on another bot.
409 policy_conflict/selection_conflict/revision_conflict → fetch current revisions, preserve draft, ask/reconcile; no blind overwrite.
409 session_busy/session_reserved/plan_in_progress → wait for owning turn; do not switch.
409 idempotency_conflict → programming error: a key was reused for different content.
409 route_expired_or_used/turn_already_started → reconcile existing turn; do not resubmit.
409 route_slot_unconfigured/coding_context_requires_validated_larger_model → choose a valid model/slot and replan.
422 invalid_fields/invalid_identifier/invalid_enabled/invalid_override/invalid_text/invalid_history/invalid_task_kind/invalid_attachments/invalid_input_tokens/unsupported_effort/model_unavailable/configure_all_slots/coding_slot_requires_sol_or_stronger → show actionable field error, retain draft.
413 request_too_large → reduce the classifier excerpt; retain original user content in the normal composer.
503 session_unavailable/idle_check_unavailable/routing_unavailable → keep selected model, disable automatic send until safe state can be verified. A failed REST request is not permission to bypass a pending/armed switch.

## App files and release gates

Adapt ModelSheet.swift, BotSettingsView.swift, AppModel.swift send/queue/reconnect flow, ChatState.swift selected-vs-actual state. Add RoutingClient, Codable policy/session/plan types and fixture handlers. Retain native controls and platform parity. GET capability negotiation gates the feature; do not assume proposed endpoints from the earlier brief still apply.

Installed runtime evidence: unit suite plus real plugin hooks, authenticated API mount, ownership rejection, observed request and real pre-tool blocking in isolated fixtures. This is not a native app end-to-end test. Before app release, prove one-turn restore, deferred switches, expensive-model confirmation, disconnect after switch/before submit, partial stream, same-session two-device reservation and external-writer exclusion. The installed venv has no pytest; upstream model-switch pytest tests were not run. Existing app scripts/test.sh --critical and iOS/macOS QA remain required. Do not publish TestFlight merely because this backend is installed.

Plugin state is per-home `jev-routing-v2.sqlite` (owned plugin database, not Hermes state.db). Back it up using SQLite backup, not a live plain copy. Per-home jev.disabled stops Jev calls; Routing off preserves reviews. Roll back only after active turns drain, retaining selected baseline and any new policy state for diagnosis. Restore prior plugin from deployment backup and reload affected profiles/serve as needed; removing v2 does not replace the native app's pending-turn recovery logic.

## V2 built-in selected-model fallback

Plugin 2.2 removes router.py and all v1 router wiring. One Engine and one complexity/task/tool classifier now serve app route plans and native effort fallback. In the official Hermes desktop app, eligible Codex turns retain the exact selected model/provider and change only low/medium/high effort. Uncertain/error/timeout keeps the original effort. Explicit manual pins are preserved; explicitly enabling autoEffortOverride authorizes automatic effort even with a manual pin. Unknown pin metadata conservatively skips adjustment. (2.2–2.3: no valid app plan meant no automatic model switch. Since 2.4 the same Engine also routes the model server-side when Auto model is on, for Codex and Claude chats, and app-managed chats follow their switches even with a manual pick.)

GET capabilities additionally returns `nativeFallback:"selected_model_effort"` and `fallbackBuiltIn:true`. Model-routing contracts are unchanged. Native fallback metrics are content-free at logs/jev-routing-events.jsonl. Routing off skips classification; per-home jev.disabled stops Jev calls. Reviews remain available.

## Independent controls (2.2): required iOS implementation

Use capability `independentRoutingControls:true`. Do not infer this from schemaVersion (still 2). Hide/disable independent controls on an older backend; do not translate independent intent into a legacy unified write.

| Auto model | Auto effort | Result |
|---|---|---|
| Off | Off | Selected model and selected effort; no classification |
| Off | On | Selected model; Jev chooses low/medium/high effort |
| On | Off | Jev chooses Small/Medium/Big model; selected effort is retained |
| On | On | Jev chooses model and effort, with one classifier call |

Show two native SwiftUI Toggles: **Auto model** (“Choose a model for each message”) and **Auto effort** (“Adjust reasoning on the selected model”). Separate “This chat” from “Bot defaults”. Each chat control needs a Use bot default reset; show inherited effective values without silently writing an override. Both bot defaults are currently off. Model slots configure model routing. Effort off ignores slot effort and keeps selectedBaseline.effort. Native model switching validates whether that effort is supported; surface rejection, never silently substitute effort.

Manual model selection: complete native selection, then PUT the acknowledged selectedBaseline and autoModelOverride=false while preserving autoEffortOverride. Manual effort selection: complete native selection, then PUT autoEffortOverride=false while preserving autoModelOverride. Turning Auto effort on explicitly sets autoEffortOverride=true and allows it to supersede a previous manual effort pin. Always retain the manual baseline for restore/fallback; never replace it with a routed response's actual model or effort.

Since 2.4, when either effective switch is on the app just submits; the plugin routes the turn (see the 2.4 section). Pre-2.4 builds used the plan → arm → acknowledged native one-turn config.set → verify → arm → prompt.submit sequence in this document. This also handles effort-only plans, whose target.model equals baseline.model. When both off, submit through the normal native path without planning. Do not call a second effort classifier. A matching armed plan suppresses built-in fallback. Record route.autoModel and route.autoEffort for display, but report applied only after actual-request observation. With only effort enabled, uncertain code preserves baseline; Medium/Big coding floors and Small tool guards apply to automatic MODEL routes. Explicitly pinned Small remains the user's choice when Auto model is off.

PUT policy fields: agentId, expectedRevision, autoModelDefault, autoEffortDefault, slots. PUT session fields: agentId, sessionId, expectedRevision, autoModelOverride, autoEffortOverride, selectedBaseline. Send full listed bodies, not PATCH, and preserve the sibling toggle on each change. Null must encode as JSON null (not omitted). GET also returns deprecated enabledDefault/routingOverride aliases representing Auto model; ignore aliases in new clients. Legacy write bodies with enabledDefault or routingOverride still set BOTH controls for old-client compatibility. Mixed legacy/new write bodies are rejected with 422. Warn against concurrently using an older settings client that may reset both controls.

Existing unified values are interpreted as both values on read without losing state; saving canonical fields persists them. Deployment explicitly materializes both false at Friday/Jarvis with revision bump. No session overrides existed during deployment. Read fresh revisions after upgrade; stale plans/settings receive 409. For compression lineage, copy both user overrides to the new canonical session after verifying actual baseline; do not mistake a runtime ID for the persisted ID.

Acceptance tests: all four combinations; independent null inheritance and per-chat reset; manual model pick leaving effort automatic; manual effort pick disabling only effort; strict Boolean/null encoding; stale revision conflict; switch change during classifier/stream; no duplicate Jev request; unknown/deadline fallback; coding floor only when Auto model is on; off switch prevents classification; supported-effort rejection; relaunch/reconnect/two-device sync; selected versus actual response metadata; one-turn restoration and failure recovery. Do not enable either control automatically during migration. Do not ship TestFlight until app-side integration tests pass.
