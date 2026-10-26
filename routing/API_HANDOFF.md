# Auto effort — app API (schema 3, since 2026-10-26)

Served at `/api/plugins/agents-cs/routing/<op>` on the bot's computer (through the hub's tunnel), with the same sign-in
as every other plugin route. Responses carry `Cache-Control: no-store`. Requests accept only the listed fields; GET
takes them as the query, PUT as a JSON body. Refusals are `{"code": "…"}` with an HTTP status.

`agentId` is the bot's profile name (`default` or `friday` for the root home). `sessionId` is the chat's canonical
(stored) session id, never the WebSocket runtime id; each call checks the chat exists on that bot and, when Hermes
records a user, that it is the caller's.

## GET /capabilities?agentId=

```json
{"schemaVersion": 3, "autoEffort": true, "efforts": ["low", "medium", "high"], "classifierAvailable": true}
```

Show Auto effort only when `autoEffort` and `classifierAvailable` are both true. A 404 (`agent_not_found`, or no route:
an older or missing plugin) means none. Schema 2 (Model Routing, `serverRouting`, slots) is gone; app builds that
still look for `serverRouting` hide their routing controls.

## GET /policy?agentId= · PUT /policy {agentId, expectedRevision, autoEffortDefault}

The bot's default for chats that haven't chosen: `{"revision": 2, "autoEffortDefault": true}`. PUT is compare-and-swap
on `revision` (`409 policy_conflict`).

## GET /session?agentId=&sessionId= · PUT /session {agentId, sessionId, expectedRevision, autoEffortOverride}

The chat's own switch: `{"revision": 3, "autoEffortOverride": null, "managed": true}`; `null` follows the bot's default.
PUT is compare-and-swap (`409 selection_conflict`) and marks the chat app-managed: from then on its own manual effort
in Hermes no longer blocks the bot's default (the app writes `false` when the person picks an effort).

## GET /turns?agentId=&sessionId=

The last 20 decided turns, newest first, without content:

```json
{"turns": [{"turnId": "…", "at": 1792831072, "effort": "high", "from": "low", "reason": "classified", "waitedMs": 0}]}
```

`effort` is what the turn ran with (null: the chat's own effort answered, and `reason` says why: `uncertain`,
`deadline`, `no_answer`, `classifier_unavailable`, `classifier_error`, `thinking_off`, `no_effort_control`,
`unsupported_provider`, `no_text`, `not_a_person`, `explicit_choice`). `from` is the effort Hermes asked for. Turns
with Auto effort off are not recorded. Rows Model Routing wrote before then are not returned.

## Errors

`401 sign_in_required`, `404 agent_not_found | session_not_found | unknown_operation`, `409 policy_conflict |
selection_conflict`, `413 request_too_large`, `422 invalid_fields | invalid_identifier | invalid_json`,
`503 session_unavailable | routing_unavailable`.
