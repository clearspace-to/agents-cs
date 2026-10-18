"""Model Routing's switches and key, read from Hermes's config and home (no Hermes imports at load time).

Routing is an optional module of the ``hermes-bots`` plugin and is **off unless configured**: a profile has it
when ``plugins.entries.hermes-bots.settings.routing_mode`` is set (``off`` | ``shadow`` | ``on``; ``off`` still
lets the app switch Auto model / Auto effort on per bot), when the retired ``jev-routing`` plugin's
``plugins.entries.jev-routing.settings.mode`` is set, or when the profile already has a routing store
(``<home>/jev-routing-v2.sqlite``, written by jev-routing and kept where it is, so nothing migrates).
"""
from pathlib import Path

MODES = ('off', 'shadow', 'on')
STORE_FILE = 'jev-routing-v2.sqlite'
KEY_FILE = 'jev-openrouter-api-key'


def _entry(config, name):
    entries = ((config or {}).get('plugins') or {}).get('entries') or {}
    entry = entries.get(name)
    settings = entry.get('settings') if isinstance(entry, dict) else None
    return settings if isinstance(settings, dict) else {}


def configured_mode(config):
    """The mode set in config (hermes-bots wins over the retired jev-routing entry), or None when unset."""
    for value in (_entry(config, 'hermes-bots').get('routing_mode'), _entry(config, 'jev-routing').get('mode')):
        if isinstance(value, bool):  # YAML 1.1 reads an unquoted on/off as true/false
            return 'on' if value else 'off'
        if isinstance(value, str) and value.strip().lower() in MODES:
            return value.strip().lower()
    return None


def mode(config):
    """The effective default mode: configured, else ``off``."""
    return configured_mode(config) or 'off'


def configured(config, home):
    """True when routing is set up for the profile at ``home`` (see the module docstring)."""
    return configured_mode(config) is not None or (Path(home) / STORE_FILE).exists()


def configured_anywhere():
    """True when the Hermes root or any of its profiles has routing configured (what ``hermes serve`` serves)."""
    from .. import paths
    homes = [paths.hermes_root()]
    try:
        homes += sorted(p for p in paths.profiles_dir().iterdir() if p.is_dir())
    except OSError:
        pass
    for home in homes:
        try:
            import yaml
            config = yaml.safe_load((home / 'config.yaml').read_text()) or {}
        except Exception:
            config = {}
        if configured(config, home):
            return True
    return False


def key_file(config=None):
    """The OpenRouter key file Jev's classifier uses, or None when there isn't one (routing unavailable).

    ``routing_key_file`` in the settings wins; else ``secrets/jev-openrouter-api-key`` under this profile's
    Hermes home, then the Hermes root's."""
    from .. import paths
    configured_path = _entry(config if config is not None else paths.config(), 'hermes-bots').get('routing_key_file')
    if configured_path:
        candidate = Path(str(configured_path)).expanduser()
        return candidate if candidate.is_file() else None
    return paths.find_secret(KEY_FILE)


class KeyUnavailable(Exception):
    """No OpenRouter key: routing's classifier can't run (callers treat it as 'unavailable', never an error)."""


def api_key(config=None):
    path = key_file(config)
    if path is None:
        raise KeyUnavailable('openrouter_key_unavailable')
    try:
        value = path.read_text().strip()
    except OSError as exc:
        raise KeyUnavailable('openrouter_key_unavailable') from exc
    if not value:
        raise KeyUnavailable('openrouter_key_unavailable')
    return value
