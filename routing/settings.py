"""The OpenRouter key Auto effort's classifier uses (no Hermes imports at load time).

``routing_key_file`` in ``plugins.entries.agents-cs.settings`` wins; else ``secrets/jev-openrouter-api-key`` under
the profile's Hermes home, then the Hermes root's. No key means Auto effort is unavailable, never an error.
"""
from pathlib import Path

KEY_FILE = 'jev-openrouter-api-key'


def key_file(config=None, home=None):
    """The key file, or None. ``home`` asks for that Hermes home (``hermes serve`` answers for every bot);
    without it, for this process's profile."""
    from .. import paths
    if home is not None:
        try:
            import yaml
            config = yaml.safe_load((Path(home) / 'config.yaml').read_text()) or {}
        except Exception:  # noqa: BLE001
            config = {}
    configured = paths.settings(config if config is not None else paths.config()).get('routing_key_file')
    if configured:
        candidate = Path(str(configured)).expanduser()
        return candidate if candidate.is_file() else None
    if home is None:
        return paths.find_secret(KEY_FILE)
    for directory in (Path(home) / 'secrets', paths.hermes_root() / 'secrets'):
        if (directory / KEY_FILE).is_file():
            return directory / KEY_FILE
    return None


class KeyUnavailable(Exception):
    """No OpenRouter key: the classifier can't run (callers treat it as 'unavailable', never an error)."""


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
