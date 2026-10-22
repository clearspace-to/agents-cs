"""The agents-cs plugin (plugin/agents-cs) against a real Hermes checkout, through Hermes' own Plugin
Doctor and plugin validator (what ``hermes plugins validate`` and a ``hermes plugins install`` run).

Skipped when Hermes isn't importable (an ordinary dev run). A plugin-compatibility job puts a
pinned Hermes checkout on PYTHONPATH and sets HERMES_COMPAT_REQUIRED=1, so a missing runtime fails.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

def _plugin_dir() -> Path:
    """plugin/agents-cs in this checkout; AGENTS_CS_PLUGIN_DIR points at a staged copy (scripts/check-hermes-contract.sh)."""
    configured = os.environ.get("AGENTS_CS_PLUGIN_DIR")
    return Path(configured) if configured else Path(__file__).resolve().parents[1]


PLUGINS = [p for p in (_plugin_dir(),) if (p / "plugin.yaml").is_file()]

try:
    import hermes_cli.plugins  # noqa: F401
    HERMES = True
except Exception:  # noqa: BLE001 - any import failure means no usable runtime here
    HERMES = False

REQUIRED = os.environ.get("HERMES_COMPAT_REQUIRED") == "1"
# Hooks only the routing module registers (declared in plugin.yaml, registered where routing is configured).
OPTIONAL_ROUTING_HOOKS = {"on_session_end", "api_request_error"}


def manifest_hooks(plugin: Path) -> list[str]:
    text = (plugin / "plugin.yaml").read_text()
    try:
        import yaml
        data = yaml.safe_load(text) or {}
        return list(data.get("hooks") or data.get("provides_hooks") or [])
    except ImportError:
        return [line.strip()[2:] for line in text.split("hooks:")[1].splitlines() if line.strip().startswith("- ")]


@unittest.skipUnless(HERMES or REQUIRED, "Hermes is not importable; CI's plugin-compatibility job runs this")
class PluginRuntimeCompatibilityTests(unittest.TestCase):
    def test_runtime_is_present(self):
        self.assertTrue(HERMES, "HERMES_COMPAT_REQUIRED=1 but hermes_cli does not import")

    def test_there_is_a_plugin_to_check(self):
        self.assertTrue(PLUGINS, "No plugin/agents-cs/plugin.yaml found (set AGENTS_CS_PLUGIN_DIR)")

    def test_plugin_doctor_loads_and_registers_every_plugin(self):
        """Hermes's own doctor runs under a throwaway home of its own, so routing (an optional module, off
        unless configured) is off here: its two hooks are declared but, correctly, not registered."""
        for plugin in PLUGINS:
            with self.subTest(plugin=plugin.name), tempfile.TemporaryDirectory(prefix="hermes-compat-") as home:
                result = subprocess.run([sys.executable, "-c", """
import json, sys
from hermes_cli.plugin_dev import doctor_plugin
report = doctor_plugin(sys.argv[1])
print(json.dumps({"ok": report.ok, "report": report.format_text(),
                  "hooks": list(report.registered_hooks), "tools": list(report.registered_tools)}))
""", str(plugin)], env={**os.environ, "HERMES_HOME": home}, capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stderr[-3000:])
                report = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertTrue(report["ok"], report["report"])
                for hook in manifest_hooks(plugin):
                    if hook in OPTIONAL_ROUTING_HOOKS:
                        continue
                    self.assertIn(hook, report["hooks"], f"{plugin.name}: {hook} did not register\n{report['report']}")

    def test_routing_registers_through_hermes_when_configured(self):
        """The routing module's tool, hooks and ``llm_request`` middleware against Hermes's real PluginContext,
        in a throwaway home whose config turns routing on."""
        for plugin in PLUGINS:
            with self.subTest(plugin=plugin.name), tempfile.TemporaryDirectory(prefix="hermes-compat-") as home:
                (Path(home) / "config.yaml").write_text(
                    "plugins:\n  entries:\n    agents-cs:\n      settings:\n        routing_mode: shadow\n")
                result = subprocess.run([sys.executable, "-c", """
import json, shutil, sys
from pathlib import Path
from hermes_cli.plugins import PluginManager
home = Path(sys.argv[2])
(home / "bundled-plugins").mkdir()
shutil.copytree(sys.argv[1], home / "plugins" / "agents-cs", ignore=shutil.ignore_patterns("__pycache__", "tests"))
manager = PluginManager()
(manifest,) = manager._scan_directory(home / "plugins", source="user")
manager._load_plugin(manifest)
loaded = manager._plugins[manifest.key or manifest.name]
print(json.dumps({"error": loaded.error, "tools": sorted(loaded.tools_registered), "hooks": list(loaded.hooks_registered)}))
""", str(plugin), home], env={**os.environ, "HERMES_HOME": home, "HERMES_BUNDLED_PLUGINS": str(Path(home) / "bundled-plugins"),
                                "HERMES_ENABLE_PROJECT_PLUGINS": "0"}, capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stderr[-3000:])
                report = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertFalse(report["error"], report)
                self.assertIn("jev_review", report["tools"])
                self.assertIn("hermes_card_chart", report["tools"])
                for hook in manifest_hooks(plugin):
                    self.assertIn(hook, report["hooks"], f"{hook} did not register: {report}")

    def test_hermes_plugin_validator_accepts_it(self):
        """What a catalog admission or ``hermes plugins install`` of the public mirror checks first."""
        for plugin in PLUGINS:
            with self.subTest(plugin=plugin.name), tempfile.TemporaryDirectory(prefix="hermes-validate-") as home:
                result = subprocess.run([sys.executable, "-c", """
import json, sys
from pathlib import Path
from hermes_cli.plugin_validate import validate_plugin_dir
report = validate_plugin_dir(Path(sys.argv[1]))
print(json.dumps({"ok": report.ok, "failures": report.failures, "warnings": report.warnings}))
""", str(plugin)], env={**os.environ, "HERMES_HOME": home}, capture_output=True, text=True, timeout=180)
                self.assertEqual(result.returncode, 0, result.stderr[-3000:])
                report = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertTrue(report["ok"], report["failures"])

    def test_declared_hooks_are_ones_hermes_fires(self):
        from hermes_cli.plugins import VALID_HOOKS
        for plugin in PLUGINS:
            for hook in manifest_hooks(plugin):
                self.assertIn(hook, VALID_HOOKS, f"{plugin.name}: Hermes has no {hook} hook")


if __name__ == "__main__":
    unittest.main()
