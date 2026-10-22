"""The devices alerts are addressed to, and the key that signs them: both come from the Agents CS connector.

* ``<connector home>/devices.json``  ``{"updated": ..., "devices": [{"id", "name", "platform", "recipient_public_key"}]}``,
  rewritten by the connector on every ``devices`` frame from the hub (atomic replace). Read on every use (cheap: it is
  cached by modification time).
* ``<connector home>/sender-key.pem``  the computer's P-256 signing key (the connector creates it and reports its public
  half to the hub; the app pins that half). The plugin only ever READS it, and never creates one.

The connector home is found through ``<root Hermes home>/hermes-bots/connector.json`` (see ``home.py``). Without it there
are no devices and nothing is sent; that is logged once per process.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from . import home

log = logging.getLogger("agents_cs.mobile")

_lock = threading.Lock()
_said: set[str] = set()
_devices_cache: dict = {"key": None, "value": []}
_key_cache: dict = {"key": None, "value": None}


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    platform: str
    recipient_public_key: str


def _once(key: str, message: str) -> None:
    with _lock:
        if key in _said:
            return
        _said.add(key)
    log.warning("agents-cs mobile: %s", message)


def forget() -> None:
    """Drop the caches and the log-once memory (tests; also a connector that was just relinked)."""
    with _lock:
        _said.clear()
        _devices_cache.update(key=None, value=[])
        _key_cache.update(key=None, value=None)


def _stamp(path: Path):
    try:
        status = path.stat()
    except OSError:
        return None
    return (str(path), status.st_mtime_ns, status.st_size)


def devices() -> list[Device]:
    """The account's devices as the connector last wrote them; ``[]`` when the connector isn't linked or the file is absent."""
    folder = home.connector_home()
    if folder is None:
        _once("connector.json", "no connector home (%s/connector.json is missing): no devices, no alerts"
              % (home.state_dir(),))
        return []
    path = folder / "devices.json"
    stamp = _stamp(path)
    if stamp is None:
        _once("devices.json", "%s is missing: no devices, no alerts" % (path,))
        return []
    with _lock:
        if _devices_cache["key"] == stamp:
            return list(_devices_cache["value"])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data.get("devices") if isinstance(data, dict) else None
        found = []
        for row in rows or []:
            if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"] \
                    and isinstance(row.get("recipient_public_key"), str) and row["recipient_public_key"]:
                found.append(Device(row["id"], str(row.get("name") or ""), str(row.get("platform") or ""),
                                    row["recipient_public_key"]))
    except (OSError, ValueError):
        return list(_devices_cache["value"])  # being rewritten right now: keep what we had
    with _lock:
        _devices_cache.update(key=stamp, value=found)
    return list(found)


def sender_key():
    """The computer's signing key (an ``EllipticCurvePrivateKey``), or ``None`` when it can't be read."""
    folder = home.connector_home()
    if folder is None:
        return None
    path = folder / "sender-key.pem"
    stamp = _stamp(path)
    if stamp is None:
        _once("sender-key.pem", "%s is missing: alerts can't be signed, so none is sent" % (path,))
        return None
    with _lock:
        if _key_cache["key"] == stamp:
            return _key_cache["value"]
    try:
        from cryptography.hazmat.primitives import serialization
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except Exception as error:  # noqa: BLE001 - a bad key file is "no key", never a crash in Hermes
        _once("sender-key.pem:bad", "%s can't be read (%s): no alerts" % (path, type(error).__name__))
        return None
    with _lock:
        _key_cache.update(key=stamp, value=key)
    return key
