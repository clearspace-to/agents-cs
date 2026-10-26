"""Auto effort (the ``routing`` module, once the jev-routing plugin): per-turn low, medium or high reasoning on the
chat's own model, switched per bot and per chat from the app. See ``effort.py`` and README.md.

Registers in every bot process; it does nothing for a chat until Auto effort is switched on for it. Imports only
the standard library."""
from pathlib import Path


def register(ctx):
    from . import effort
    from .. import paths
    home = paths.hermes_home()
    engine = effort.Engine(home)

    def scoped(callback):
        def run(**kwargs):
            # One process can serve several profiles: each engine answers for its own home only.
            if Path(paths.hermes_home()).resolve() != Path(home).resolve():
                return None
            try:
                return callback(**kwargs)
            except Exception:  # noqa: BLE001  fail open, without logging private payloads
                return None
        return run

    ctx.register_hook('pre_llm_call', scoped(engine.prepare))
    ctx.register_middleware('llm_request', scoped(engine.request))
