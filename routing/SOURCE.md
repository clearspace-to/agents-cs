# Source of the routing module

Copied (not moved: the source repo stays as it is) from the Jev routing plugin, version 2.4.0:

- Repository: https://github.com/clearspace-to/hermes-agents-mg (checkout `/Volumes/NVME/Code/hermes-agent`)
- Path: `scripts/jev/plugin/` (the plugin) and `scripts/jev/test_*.py` (its tests)
- Last commit touching `scripts/jev`: `6961fdb4844421eef00c7dcdfaf9d6658845f693`
  ("jev-routing 2.4: route each turn server-side; app only toggles Auto model / Auto effort");
  repository HEAD when copied: `4965540aaa9189fd815ce0c4ef0ab70fc212da29`, 2026-10-17.
- License: MIT. Copyright Clearspace. The permission notice follows; the upstream MIT notices the code was
  informed by are kept in `licenses/` and listed in `THIRD_PARTY.md`.

## What changed in this copy

- Became a module of `hermes-bots` (`register(ctx)` is called by the root `__init__.py` only when routing is
  configured; its manifest entries moved into the root `plugin.yaml`; `dashboard/plugin_api.py` became
  `routes.py`, composed by the root `dashboard/plugin_api.py`).
- Routes: `/api/plugins/jev-routing/routing/<op>` is now `/api/plugins/hermes-bots/routing/<op>`.
- Hard-coded home paths replaced by Hermes's home API through `../paths.py`
  (`get_hermes_home`, `get_default_hermes_root`). Data files stay where they were: `<home>/jev-routing-v2.sqlite`,
  `<home>/logs/jev-routing-events.jsonl`, `<hermes root>/logs/jev-routing-api.log`, `<home>/jev.disabled`.
- Off unless configured (`settings.py`): `plugins.entries.hermes-bots.settings.routing_mode`, or an existing store.
  (The old `plugins.entries.jev-routing.settings.mode` stopped counting on 2026-10-20.)
- The OpenRouter key is `secrets/jev-openrouter-api-key` under the profile's Hermes home, then the root's (or
  the `routing_key_file` setting). A missing key means "routing unavailable" (`classifier_unavailable`, the chat
  keeps its model; `jev_review` answers `routing_unavailable`), never an error.
- Tests moved to `../tests/test_routing_*.py` (they load the plugin as a package).

## MIT License

Copyright (c) 2026 Clearspace

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
