# Jev routing 2.4 (the `routing` module of agents-cs)

The plugin routes each turn itself (2.4): with Auto model and/or Auto effort on, its
`llm_request` middleware sends that turn to the chosen slot model/effort inside the
chat's provider. The app only writes the switches. See API_HANDOFF.md; the 2.3 plan/arm
endpoints remain, deprecated, for older app builds.

- Small: available Luna, low; Medium: current configured Sol, medium; Big: Sol,
  high. Slots are configurable among catalog-listed Codex Luna/Sol/Astra models;
  coding slots cannot be Luna. Exact saved IDs never silently upgrade.
- Baseline is the user's selected model, independent of the routed execution.
- Routing off bypasses Jev. Known code is Medium/Big; uncertain code uses Big.
- One 500 ms classification, no retries or queued classifier backlog. Secrets,
  attachments and uncertainty have conservative fallbacks.
- 2.4: model changes are applied per request by the plugin (`llm_request` middleware),
  same provider only, never written to the chat. Pre-2.4 apps used `config.set --once`.
- API reservations, revisions, idempotency, expiry and observed-request status are
  shared across processes in a mode-0600 per-home owned SQLite store.
- Small planned turns block non-allowlisted tools before execution. Approved
  read-only names: web_search, web_extract, read_file. Blocked coding requires an
  explicit Medium/Big continuation; it is never silently replayed.
- A matching (2.3 app) plan bypasses server routing, preventing double calls.
  Without a plan, the same v2 engine routes model and/or effort for Codex and Claude
  chats per the switches, with explicit-pin protection on unmanaged chats.
- Existing jev_review remains read-only passage/inbox/alert/transaction review.

## Boundaries

Server routes cover tui (native app, dashboard), desktop and cli turns; cron, kanban,
gateways and voice are not routed. Hermes keeps one provider client per chat, so a route
never crosses provider (Claude chat + OpenAI slots keeps its model; effort still applies).
Hermes' session.info/usage keep reporting the chat's model; GET /turns has what was served.
Legacy (2.3 app) plans: the app serializes sends and restores after its `--once` switch. Route targets (2.3): Claude subscription (`anthropic`) or OpenAI subscription (`openai-codex`) catalog models; Medium and Big refuse Luna and Haiku. Other providers stay manual picks.

Cross-model routes require a conservative full input-token bound <=180,000.
Without it the selected model is retained; coding on a selected Luna may pause.
Native context budgeting and availability validation still run after switching.

Both Jev routing/reviews use OpenRouter Decisions, typesafe/jev-1.13. Key remains
in `secrets/jev-openrouter-api-key` under the Hermes home (the profile's, then the root's; or the
`routing_key_file` setting); never in app or artifacts. No key means routing is unavailable and the
chat keeps its own model.
Ordinary excerpts go through OpenRouter to TypeSafe; redaction is not anonymization.
`<home>/jev.disabled` stops Jev calls. Routing off preserves manual reviews.

## Verification

`python3 -m unittest discover -s scripts/jev -p 'test_*.py'`.
On installed Hermes, `verify_v2_api.py plugin` checks a real isolated authenticated
API mount, ownership, plans, actual middleware observations and native pre-tool
blocking. `verify_v2_fallback_runtime.py plugin` checks v2 fallback profile isolation.
Doctor confirms plugin discovery/registration. No private messages or actual tool
side effects are needed. Native app E2E remains the app release gate.

Keep backups before replacing plugin/config. Use SQLite backup for live v2 state.
Rollback requires draining/reconciling pending --once turns before restoring the
previous plugin. Keep later selected-model changes and policy state intact.

## Separate automatic controls

Auto model and Auto effort have independent bot defaults and nullable chat overrides. Both defaults are off in production. Model-only retains manual effort; effort-only retains the chosen model. See API_HANDOFF.md for compatibility, manual selection and app requirements.
