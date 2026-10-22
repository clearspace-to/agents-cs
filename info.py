"""``GET /api/plugins/agents-cs/info``: what is installed and what loaded (for the connector, the app and
``agents-cs doctor``). Standard library only; the dashboard supplies the per-module status it observed."""
from __future__ import annotations

from typing import Any


def hermes_version() -> str | None:
    try:
        import hermes_cli
        return str(hermes_cli.__version__)
    except Exception:  # noqa: BLE001
        pass
    try:
        from importlib.metadata import version
        return version("hermes-agent")
    except Exception:  # noqa: BLE001
        return None


def build(version: str, modules: dict[str, str]) -> dict[str, Any]:
    return {"name": "agents-cs", "version": version, "modules": dict(modules), "hermes_version": hermes_version()}
