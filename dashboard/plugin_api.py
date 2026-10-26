"""The one dashboard router of the ``agents-cs`` plugin, mounted by ``hermes serve`` under
``/api/plugins/agents-cs/`` behind Hermes's own dashboard authentication. It composes the modules' routers
and keeps every subpath of the plugins it replaced; only the prefix changed:

    /api/plugins/hermes-bots-cards/<x>        ->  /api/plugins/agents-cs/<x>          (cards)
    (new in 2.0)                              ->  /api/plugins/agents-cs/mobile/<op>  (mobile: reads, replies, usage, kinds, view)
    /api/plugins/hermes-bots-voice/voice/<op> ->  /api/plugins/agents-cs/voice/<op>   (voice)
    /api/plugins/jev-routing/routing/<op>     ->  /api/plugins/agents-cs/routing/<op> (routing: Auto effort)
    GET /api/plugins/agents-cs/info                                                   (name, version, modules)

Each module's router is imported inside its own guard: one that fails is left out and reported by ``/info``,
the rest are served. ``hermes serve`` is the only process that imports this file, so voice's ``httpx``,
``websockets`` and ``pydantic`` load here and never in a bot worker.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path

from fastapi import APIRouter

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE = "agents_cs_dashboard"
_log = logging.getLogger("agents_cs.dashboard")


def _root_package():
    """The plugin as a package (relative imports intact), loaded once under a private name."""
    if _PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(_PACKAGE, _ROOT / "__init__.py",
                                                      submodule_search_locations=[str(_ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[_PACKAGE] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(_PACKAGE, None)
            raise
    return sys.modules[_PACKAGE]


_root = _root_package()
router = APIRouter()
MODULES: dict[str, str] = {}


def _mount(name: str) -> None:
    try:
        module = importlib.import_module("%s.%s.routes" % (_PACKAGE, name))
        router.include_router(module.router)
        MODULES[name] = "ok"
    except Exception as exc:  # noqa: BLE001 - a module that can't load is reported, not fatal
        _log.warning("agents-cs: %s routes not mounted: %s", name, exc, exc_info=True)
        MODULES[name] = _root._error(exc)


for _name in _root.MODULES:
    _mount(_name)


@router.get("/info")
def info() -> dict:
    from importlib import import_module
    return import_module("%s.info" % _PACKAGE).build(_root.VERSION, MODULES)
