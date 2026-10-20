"""Load the plugin the way Hermes does: a package from its folder, relative imports intact.

Every test module shares one copy, registered as ``hermes_bots_dashboard``: the name ``dashboard/plugin_api.py``
looks for first, so the router tests and the module tests see the same modules.
"""
import importlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # plugin/hermes-bots
PACKAGE = "hermes_bots_dashboard"


def load_root():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


def load_module(name):
    """A module of the plugin (``cards``, ``voice``, ``routing``), imported as a subpackage."""
    load_root()
    return importlib.import_module("%s.%s" % (PACKAGE, name))


def load_plugin_api(label="fresh"):
    """A fresh copy of ``dashboard/plugin_api.py`` (what ``hermes serve`` imports)."""
    load_root()
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_test_" + label, ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_fresh(dotted, label="fresh"):
    """A fresh copy of a plugin submodule file (e.g. ``cards.routes``) inside the shared package."""
    load_root()
    path = ROOT.joinpath(*dotted.split(".")).with_suffix(".py")
    spec = importlib.util.spec_from_file_location("%s.%s_%s" % (PACKAGE, dotted, label), path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
