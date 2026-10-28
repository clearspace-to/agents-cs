# Agents CS plugin: cards + voice + routing in one install. See README.md and NOTICE.
"""Agents CS, the app's host side, as one Hermes plugin made of modules:

* ``cards``   native cards, forms, decisions, alerts, tasks, coordinator, creation, cron archive, relay
* ``mobile``  alerts and read state for the app (was the push bridge): the reply scan (run by the connector), the outbox it drains
              and ``/mobile/*`` routes
* ``voice``   live voice media routes (dashboard only: nothing to register in a bot worker)
* ``routing`` Auto effort (was the ``jev-routing`` plugin): per-turn reasoning effort, switched on per bot or chat

``register(ctx)`` registers each module inside its own guard: a module that fails to import or register is
logged and reported (``/api/plugins/agents-cs/info``), and the others still load.

Loading is lazy and process-aware. This file imports only the standard library. A bot worker (gateway,
CLI, Kanban worker) calls ``register`` and gets the cards hooks and tools and routing's two hooks; ``httpx``, ``websockets``, ``pydantic`` and ``fastapi`` load only where a
module needs them (the routes, in ``hermes serve``'s ``dashboard/plugin_api.py``).
"""
from __future__ import annotations

import importlib
import logging
from typing import Any

VERSION = "3.7.2"  # keep equal to plugin.yaml (tests/test_plugin.py checks)
NAME = "agents-cs"
MODULES = ("cards", "mobile", "voice", "routing")

logger = logging.getLogger(__name__)

# What this process's last register() did, per module: "ok" or "error: <what>".
STATUS: dict[str, str] = {}


def _error(exc: BaseException) -> str:
    return ("error: %s: %s" % (type(exc).__name__, exc))[:300]


def _load(ctx: Any, name: str) -> str:
    try:
        module = importlib.import_module("%s.%s" % (__name__, name))
        module.register(ctx)
        return "ok"
    except Exception as exc:  # noqa: BLE001 - one module never takes the others down
        logger.warning("agents-cs: module %s failed to load: %s", name, exc, exc_info=True)
        return _error(exc)


def register(ctx: Any) -> None:
    for name in MODULES:
        STATUS[name] = _load(ctx, name)
    bad = {name: status for name, status in STATUS.items() if status.startswith("error")}
    if bad:
        logger.warning("agents-cs %s: %s", VERSION, bad)
    else:
        logger.info("agents-cs %s: %s", VERSION, STATUS)
