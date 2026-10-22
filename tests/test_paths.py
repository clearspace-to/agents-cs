"""Every location the plugin uses comes from Hermes's home API; none is hard-coded."""
import importlib
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugin_loader  # noqa: E402

ROOT = plugin_loader.ROOT
root = plugin_loader.load_root()
paths = importlib.import_module(root.__name__ + ".paths")
store = importlib.import_module(root.__name__ + ".cards.store")


def fake_hermes(home, default_root):
    module = types.ModuleType("hermes_constants")
    module.get_hermes_home = lambda: str(home)
    module.get_default_hermes_root = lambda: str(default_root)
    return {"hermes_constants": module}


class NoHardCodedPathTests(unittest.TestCase):
    PATTERNS = (r"~/\.hermes", r"Path\.home\(", r"expanduser\(['\"]~", r"/Users/", r"/home/\w+", r"\.hermes['\"/]")

    def test_no_plugin_source_spells_out_a_home(self):
        offenders = []
        for path in sorted(ROOT.rglob("*")):
            if path.suffix not in {".py", ".json", ".yaml", ".js"} or "tests" in path.parts or path.name == "paths.py" \
                    or ".venv" in path.parts or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in self.PATTERNS:
                for match in re.finditer(pattern, text):
                    line = text[:match.start()].count("\n") + 1
                    offenders.append("%s:%d %s" % (path.relative_to(ROOT), line, text.splitlines()[line - 1].strip()[:90]))
        self.assertEqual(offenders, [], "paths come from paths.py (Hermes's home API)")

    def test_paths_py_alone_has_the_no_hermes_fallback(self):
        self.assertEqual(len(re.findall(r"Path\.home\(\)", (ROOT / "paths.py").read_text())), 2)


class HomeResolutionTests(unittest.TestCase):
    def test_hermes_constants_wins(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {"HERMES_HOME": "/ignored"}), \
                patch.dict(sys.modules, fake_hermes(Path(tmp) / "profiles" / "jarvis", tmp)):
            self.assertEqual(paths.hermes_home(), Path(tmp) / "profiles" / "jarvis")
            self.assertEqual(paths.hermes_root(), Path(tmp))
            self.assertEqual(paths.profiles_dir(), Path(tmp) / "profiles")
            self.assertEqual(store.hermes_root(), Path(tmp))

    def test_without_hermes_the_env_var_then_a_profile_unwraps_to_its_root(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"hermes_constants": None}):
            profile = Path(tmp) / "profiles" / "jarvis"
            with patch.dict(os.environ, {"HERMES_HOME": str(profile)}):
                self.assertEqual(paths.hermes_home(), profile)
                self.assertEqual(paths.hermes_root(), Path(tmp))
            with patch.dict(os.environ, {"HERMES_HOME": tmp}):
                self.assertEqual(paths.hermes_home(), Path(tmp))
                self.assertEqual(paths.hermes_root(), Path(tmp))

    def test_data_files_stay_where_they_were(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, fake_hermes(tmp, tmp)):
            voice = importlib.import_module(root.__name__ + ".voice.preferences")
            cron = importlib.import_module(root.__name__ + ".cards.cron_archive")
            self.assertEqual(store.FormStore().path, Path(tmp) / "hermes-bots-cards" / "forms.db")
            self.assertEqual(cron.Decided().path, Path(tmp) / "hermes-bots-cards" / "cron-archive.db")
            self.assertEqual(voice.default_path(), Path(tmp) / "hermes-bots-voice" / "voices.json")
            routing = importlib.import_module(root.__name__ + ".routing.routing_v2")
            self.assertEqual(routing.Store(tmp).path, Path(tmp) / "jev-routing-v2.sqlite")


class SettingsAndSecretsTests(unittest.TestCase):
    CONFIG = {"plugins": {"entries": {
        "agents-cs": {"settings": {"card_guard": "on", "card_guard_key_file": "/keys/jev"}},
        "hermes-bots-cards": {"settings": {"card_guard": "shadow", "card_guard_key_file": "/legacy"}}}}}

    def test_settings_come_only_from_this_plugins_entry(self):
        self.assertEqual(paths.settings(self.CONFIG), {"card_guard": "on", "card_guard_key_file": "/keys/jev"})
        retired = {"plugins": {"entries": {"hermes-bots-cards": {"settings": {"card_guard": "on"}},
                                           "jev-routing": {"settings": {"mode": "on"}}}}}
        self.assertEqual(paths.settings(retired), {}, "the 1.x plugin names are no longer read")
        self.assertEqual(paths.settings({}), {})

    def test_secret_is_found_in_the_profile_then_the_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profiles" / "jarvis"
            (Path(tmp) / "secrets").mkdir(parents=True)
            (profile / "secrets").mkdir(parents=True)
            with patch.dict(sys.modules, fake_hermes(profile, tmp)):
                self.assertIsNone(paths.find_secret("jev-openrouter-api-key"))
                (Path(tmp) / "secrets" / "jev-openrouter-api-key").write_text("root")
                self.assertEqual(paths.find_secret("jev-openrouter-api-key"), Path(tmp) / "secrets" / "jev-openrouter-api-key")
                (profile / "secrets" / "jev-openrouter-api-key").write_text("profile")
                self.assertEqual(paths.find_secret("jev-openrouter-api-key"), profile / "secrets" / "jev-openrouter-api-key")

    def test_card_guard_reads_its_settings_and_falls_back_to_routings_key(self):
        guard = importlib.import_module(root.__name__ + ".cards.guard")
        with patch.object(paths, "config", return_value=self.CONFIG):
            self.assertEqual(guard.mode(), "on")
            self.assertEqual(str(guard._key_file()), "/keys/jev")
        with patch.object(paths, "config", return_value={"plugins": {"entries": {"hermes-bots-cards": {"settings": {"card_guard": "shadow"}}}}}):
            self.assertEqual(guard.mode(), "off")
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "secrets" / "jev-openrouter-api-key"
            key.parent.mkdir()
            with patch.dict(sys.modules, fake_hermes(tmp, tmp)), patch.object(paths, "config", return_value={}):
                self.assertIsNone(guard._key_file())
                key.write_text("k")
                self.assertEqual(guard._key_file(), key, "the Jev key under secrets/, as routing finds it")


if __name__ == "__main__":
    unittest.main()
