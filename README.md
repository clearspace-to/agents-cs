# Agents CS plugin

The host side of the Agents CS app (iPhone, iPad, Mac) as one Hermes plugin. Apache-2.0 (see LICENSE and the
NOTICE files); the `routing` module is MIT (see `routing/SOURCE.md`). It changes nothing in Hermes: it uses
Hermes's documented plugin surface (tools, hooks, a skill, request middleware, a supervised background task and a dashboard router).

| Module | What it does | Where it runs |
|---|---|---|
| `cards` | Native cards and forms, decisions, the approval/question/task/turn hooks that ring the phone, the task experience (board, quick add, comments, answers), Kanban hooks, scheduled-run relay | Every bot process (hooks, tools); routes in `hermes serve` |
| `mobile` | Alerts and read state for the app (formerly the separate push bridge): reply scan, sealed alerts, the outbox the connector drains, unread/badge/inbox-filter state, API usage | Alert writing in any process; the scan worker and `/mobile/*` routes in `hermes serve` (root home) |
| `voice` | Live voice media routes (Codex subscription), per-bot voice choice | `hermes serve` only |
| `routing` | Auto effort: one classifier call per turn picks low, medium or high reasoning on the chat's own model, when the app switches it on for a bot or a chat (see `routing/README.md`) | Every bot process (`pre_llm_call` and `llm_request` middleware); routes in `hermes serve` |

Each module loads inside its own guard: one that fails is logged and reported by `GET /api/plugins/agents-cs/info`
(`{name, version, modules: {cards, mobile, voice, routing}, hermes_version}`; a module is `ok` or `error: ...`), and the
others keep working.

## Install

The Agents CS connector installs and updates it (`hermes plugins install <source> --enable` into the root home, a link
into each profile home). By hand:

```
hermes plugins install <git url of this repository>
hermes plugins enable agents-cs
```

Hermes installs `python_dependencies` (httpx, websockets, pydantic, cryptography; `pyproject.toml` lists the same four,
and Hermes reads it first). Restart `hermes serve` afterwards, and the gateway when `plugin.yaml`'s `restart: gateway`
list names a changed file (the files a bot process imports). Requires Hermes 0.21.5 or newer.

## Alerts (the `mobile` module)

There is no bridge process, port, key or APNs credential on the Hermes computer.

* **Devices and key** come from the connector: `<root home>/hermes-bots/connector.json` (`{"home": "<connector home>"}`,
  written by `agents-cs connect`) points at `<connector home>/devices.json` (`{updated, devices: [{id, name, platform,
  recipient_public_key}]}`) and `<connector home>/sender-key.pem` (the P-256 key that signs alerts; the plugin only reads it).
  Without them nothing is sent, and the log says so once.
* **Reply scan.** `mobile/scan.py` runs in the connector, not in Hermes: before every drain (every 2 s, or at once on a
  nudge) it reads the root and every profile's `state.db`, applies each device's inbox filter, and writes one sealed row
  per device to the outbox. Nothing in Hermes runs it. The scan cursor covers only the batch queued, even if a read
  or badge refresh discovers newer replies during delivery.
* **Events** (approvals, clarify questions, board tasks, the badge) come from `cards` through `mobile.events`, called
  in-process from whichever Hermes process saw them; each writes sealed rows to the same outbox, so SQLite is the only
  hand-off between processes. A finished turn nudges the connector's scan by touching `<root home>/hermes-bots/delivery.nudge`
  (the file every outbox commit touches too).
* **The outbox** is `<root home>/hermes-bots/delivery.db`, `outbox(id, device_id, created, push_type, priority, collapse_id,
  expiration, payload_json, state, attempts, last_error)`, with `mobile/outbox.py` (`Outbox.enqueue`, `claim_queued`, `mark`,
  `purge`) as the API the connector imports to drain it, send a `push` frame per row and mark it `sent`, `failed` or `dropped`.
  The same file holds `cursors`, `reads`, `baselines`, `views` and `attention`; it has no devices or keys.
* **Sealing** is `mobile/sealed.py` (ECDH P-256 + HKDF + AES-GCM, ECDSA signature; the HERMES format). Apple and the hub see a
  placeholder; the phone's notification extension opens the real text.

## Routes

Everything is under `/api/plugins/agents-cs/`, behind Hermes's own dashboard authentication:
`forms/…`, `decisions…`, `kanban/…`, `tasks/…`, `actions/…` (cards); `mobile/reads`, `mobile/replies`,
`mobile/kinds`, `mobile/view`, `mobile/usage` (mobile; the device is the hub's `X-Hermes-Bots-Device` header, the owner
Hermes's session user, else the computer's one owner); `voice/<op>` (voice); `routing/<op>` (Auto effort); `info`.
There is no device registration route.

## Settings (`plugins.entries.agents-cs.settings` in config.yaml)

- `routing_key_file`: OpenRouter key for Auto effort's classifier; default `secrets/jev-openrouter-api-key` under the
  profile's Hermes home, then the root's. No key means Auto effort is unavailable (the app hides it), never an error.

Settings under the retired plugins' names (`hermes-bots-cards`, `jev-routing`) are no longer read; the 2.x
`plugins.entries.hermes-bots.settings` is, while `agents-cs` has none (the connector copies it over when it moves a
computer to this plugin). `routing_mode`, `card_guard` and `card_guard_key_file` were removed on 2026-10-26 (Model
Routing's model switching and the card guard are gone) and are ignored where they remain.

## Where it keeps things

Every path comes from Hermes's home API (`hermes_constants.get_hermes_home` / `get_default_hermes_root`), never a
hard-coded home. Data files keep the names the earlier plugins gave them: `<root>/hermes-bots-cards/forms.db`,
`<home>/hermes-bots-voice/voices.json`, `<home>/jev-routing-v2.sqlite`.
New in 2.0, fresh (nothing migrates): `<root>/agents-cs/delivery.db` (and its `delivery.nudge`), `connector.json`.

## Tests

```
cd plugin/agents-cs && uv run pytest -q      # a uv workspace member (pyproject.toml); standalone too
python -m unittest discover -s tests           # with an environment that has fastapi, httpx, cryptography, pyyaml
```

`scripts/check-hermes-contract.sh [--all]` runs the Hermes contract tests, Hermes's own Plugin Doctor and validator (and
with `--all` the whole suite) under the Hermes Mac's Python from a throwaway copy and home; nothing is installed.
