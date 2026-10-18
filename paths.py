"""Where the plugin keeps and finds things.

Every location comes from Hermes's own home API (``hermes_constants.get_hermes_home`` for the profile this
process serves, ``get_default_hermes_root`` for the root home that also holds ``profiles/``). Nothing else in
the plugin spells out ``~/.hermes`` or a user's home directory (``tests/test_paths.py`` enforces it).

The only fallback is for a process where Hermes is not importable (unit tests, ``hermes plugins validate``'s
bare probe): ``HERMES_HOME`` when set, else the same platform default Hermes itself uses.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def _fallback_home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        return Path(os.path.expanduser(os.path.expandvars(configured)))
    if sys.platform == "win32":  # hermes_constants._get_platform_default_hermes_home
        local = os.environ.get("LOCALAPPDATA", "").strip()
        return (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"
    return Path.home() / ".hermes"


def hermes_home() -> Path:
    """The Hermes home this process serves: the root, or ``<root>/profiles/<name>`` inside a profile."""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001 - no usable Hermes here
        return _fallback_home()


def hermes_root() -> Path:
    """The Hermes root home, also when this process runs a profile (``<root>/profiles/<name>``)."""
    try:
        from hermes_constants import get_default_hermes_root
        return Path(get_default_hermes_root())
    except Exception:  # noqa: BLE001
        pass
    home = hermes_home()
    return home.parent.parent if home.parent.name == "profiles" else home


def profiles_dir() -> Path:
    return hermes_root() / "profiles"


def plugin_dir() -> Path:
    """This plugin's own (code) directory."""
    return Path(__file__).resolve().parent


def config() -> dict[str, Any]:
    """The serving profile's config.yaml, read-only; ``{}`` when it can't be read."""
    try:
        from hermes_cli.config import load_config_readonly
        return load_config_readonly() or {}
    except Exception:  # noqa: BLE001
        return {}


def settings(config_data: dict[str, Any] | None = None, *names: str) -> dict[str, Any]:
    """``plugins.entries.<name>.settings`` merged over ``names`` (default: this plugin); earlier names win.

    A setting still living under a retired plugin's name (``hermes-bots-cards``, ``jev-routing``) keeps
    working until it is moved under ``hermes-bots``.
    """
    data = config() if config_data is None else config_data
    entries = ((data.get("plugins") or {}).get("entries") or {})
    merged: dict[str, Any] = {}
    for name in reversed(names or ("hermes-bots",)):
        entry = entries.get(name)
        found = entry.get("settings") if isinstance(entry, dict) else None
        if isinstance(found, dict):
            merged.update(found)
    return merged


def secrets_dirs() -> list[Path]:
    """Where a secret file may live, most specific first: this profile's ``secrets/``, then the root's."""
    out: list[Path] = []
    for base in (hermes_home(), hermes_root()):
        candidate = base / "secrets"
        if candidate not in out:
            out.append(candidate)
    return out


def find_secret(filename: str) -> Path | None:
    """The first existing ``secrets/<filename>`` (profile, then root), or ``None``."""
    for directory in secrets_dirs():
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    return None
