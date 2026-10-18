"""Each bot's live-voice choice, kept on the Hermes host so every device signed in to it agrees.

One small JSON file outside the plugin directory (reinstalling the plugin replaces that directory).
Choices are per bot (keyed by the one provider, codex_subscription), not per device or sign-in. Not secret: any
authenticated caller of this plugin may read and change them.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

PROVIDERS = ("codex_subscription",)
_VOICE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_BOT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_BOTS = 256


def default_path() -> Path:
    """``<hermes home>/hermes-bots-voice/voices.json``: where the 1.x plugin kept it, so nothing migrates."""
    from .. import paths
    return paths.hermes_home() / "hermes-bots-voice" / "voices.json"


class VoicePreferences:
    def __init__(self, path: Path | None = None):
        self.path = path or default_path()
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        # Keep only well-formed rows; a hand-edited or damaged file never breaks a call.
        return {bot: {p: v for p, v in row.items() if p in PROVIDERS and isinstance(v, str) and _VOICE.match(v)}
                for bot, row in data.items() if isinstance(bot, str) and _BOT.match(bot) and isinstance(row, dict)}

    def get(self, bot: str) -> dict[str, str]:
        with self._lock:
            return dict(self._read().get(bot, {}))

    def set(self, bot: str, provider: str, voice: str) -> dict[str, str]:
        if not _BOT.match(bot) or provider not in PROVIDERS or not _VOICE.match(voice):
            raise ValueError("invalid_voice_choice")
        with self._lock:
            data = self._read()
            if bot not in data and len(data) >= MAX_BOTS:
                raise ValueError("voice_choice_limit")
            data.setdefault(bot, {})[provider] = voice
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, sort_keys=True, indent=1), encoding="utf-8")
            os.replace(temporary, self.path)
            return dict(data[bot])
