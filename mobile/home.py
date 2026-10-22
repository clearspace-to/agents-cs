"""Where the ``mobile`` module reads and writes, all derived from Hermes's home API (``paths.py``).

Every function is called at use time (never cached at import), so a test, or a Hermes whose home moved, is
followed. ``root()`` is the Hermes root home: the ``mobile`` module is one thing per computer, not per profile.
"""
from __future__ import annotations

import json
from pathlib import Path

try:
    from .. import paths
except ImportError:  # the connector imports this folder on its own, as ``agents_cs_shared``; it calls ``use_root``
    paths = None

_root: Path | None = None


def use_root(path) -> None:
    """Set the root home for a process outside Hermes: the connector, which runs ``scan.cycle`` for its Hermes."""
    global _root
    _root = Path(path)


def root() -> Path:
    """The Hermes root home (also when this process serves a profile)."""
    return _root if _root is not None else paths.hermes_root()


def state_dir() -> Path:
    """``<root home>/agents-cs``: the delivery database and the connector's pointer file live here."""
    return root() / "hermes-bots"


def delivery_path() -> Path:
    return state_dir() / "delivery.db"


def nudge_path() -> Path:
    """Beside the delivery database: touched after every outbox commit and when a turn finishes. The connector checks
    it every 100 ms and then scans and drains at once."""
    return delivery_path().with_suffix(".nudge")


def connector_info() -> dict:
    """``<root home>/hermes-bots/connector.json`` (``{"home": "<connector home>"}``, written by
    ``agents-cs connect``); ``{}`` when it is missing or unreadable."""
    try:
        data = json.loads((state_dir() / "connector.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def connector_home() -> Path | None:
    value = connector_info().get("home")
    return Path(value) if isinstance(value, str) and value.strip() else None


def computer_id() -> str:
    """The hub's id for this computer (``cmp_...``), from the same pointer file; ``""`` until the connector has been
    welcomed by the hub. Every alert carries it (``computer``) so the app can tell which computer it came from."""
    value = connector_info().get("computer_id")
    return value if isinstance(value, str) else ""


def sources() -> list[tuple[str, Path]]:
    """``[(profile, state.db)]``: the root home as ``default`` and every profile under ``<root>/profiles``."""
    base = root()
    found = [("default", base / "state.db")]
    for path in sorted((base / "profiles").glob("*/state.db")):
        found.append((path.parent.name, path))
    return found
