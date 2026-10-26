# Auto effort (the `routing` module of agents-cs)

Auto effort picks Low, Medium or High reasoning for each message, on the chat's own model. The app switches it per bot
(the default for its chats) and per chat; the plugin does the rest on Hermes. API: `API_HANDOFF.md`. Code: `effort.py`.

## How a turn is handled

1. `pre_llm_call`: for a turn on the native app or dashboard (`tui`), `desktop` or `cli` with Auto effort on, the
   person's message and up to four recent exchanges are masked (credentials, emails, phone numbers), trimmed, and sent
   to Jev (`typesafe/jev-1.13` through OpenRouter's decisions API) in the background: one question, "how much reasoning
   does this need", answered low, medium or high.
2. `llm_request` middleware: the turn's first provider request waits for that answer, at most one second from the
   turn's start (it usually takes ~0.2 s). A pick at least 60 % likely is applied to every request of the turn; a
   Low pick for a message (or a "yes" to an offer) that sends, deletes, pays or the like becomes Medium.
3. Only the effort field changes: Codex `reasoning.effort`, Claude `output_config.effort`. The model, provider,
   messages and the chat's own settings are never touched or written. Thinking off, a provider without an effort
   field, Hermes moving the turn to a fallback model, or the switch going off mid-turn leave the request as Hermes
   built it.

The turn is then recorded without content (`GET /turns`: the effort picked, the effort it replaced, why), which the app
shows on the reply's work trail ("Auto effort · High").

## When it does nothing

Auto effort off for the chat (its own switch, else the bot's default); a chat with its own manual effort that the app
doesn't manage (a pick on the desktop or in the CLI); a `/command`, a scheduled or system turn, or "think harder" /
"use high reasoning"; an unsure, slow or failed classifier; no OpenRouter key. In every case the chat's own effort
answers. Cron, Kanban, gateways and voice are never touched.

## Key and data

The key is `secrets/jev-openrouter-api-key` under the profile's Hermes home, then the root's, or the
`routing_key_file` setting; never in the app. No key: `GET /capabilities` says `classifierAvailable: false` and the app
hides Auto effort. Message excerpts go through OpenRouter to TypeSafe; masking is not anonymization.

The switches and the turn records live in `<home>/jev-routing-v2.sqlite` (mode 0600; the name it has had since the
jev-routing plugin, whose `policy` and `sessions` rows are still read for their Auto effort switches).

## History

Until 2026-10-26 this module was Model Routing: it also switched the model per turn (Small, Medium, Big slots),
offered the read-only `jev_review` tool and the `jev-routing` skill. Those are gone; Auto model had never switched a
model on the Mac mini. Auto effort had changed nothing either (424 turns): three questions all had to be 90 % sure,
masking ran on serialized JSON and broke its escapes, and a pick equal to the chat's own effort was dropped. All three
are fixed here (`tests/test_routing_effort.py`).
