"""The unified plugin as a whole: manifest, guarded module loading, /info, lazy imports.

Hermes is never imported (``test_plugin_runtime.py`` and Hermes's own validator check the real runtime)."""
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugin_loader  # noqa: E402

ROOT = plugin_loader.ROOT
root = plugin_loader.load_root()


def manifest() -> dict:
    import yaml
    return yaml.safe_load((ROOT / "plugin.yaml").read_text())


class Ctx:
    """The slice of Hermes's PluginContext the modules use."""
    profile_name = "friday"

    def __init__(self):
        self.tools, self.hooks, self.middleware, self.skills = {}, {}, {}, {}

    def register_tool(self, name, **kwargs):
        self.tools[name] = kwargs

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)

    def register_middleware(self, kind, callback):
        self.middleware[kind] = callback

    def register_skill(self, name, path, description=""):
        assert Path(path).exists(), path
        self.skills[name] = path


class ManifestTests(unittest.TestCase):
    def test_identity_and_version(self):
        data = manifest()
        self.assertEqual((data["name"], data["version"], data["license"]), ("hermes-bots", "2.0.0", "Apache-2.0"))
        self.assertEqual(root.VERSION, data["version"])
        self.assertEqual(root.NAME, data["name"])
        dashboard = json.loads((ROOT / "dashboard" / "manifest.json").read_text())
        self.assertEqual((dashboard["name"], dashboard["api"], dashboard["version"]), ("hermes-bots", "plugin_api.py", "2.0.0"))
        self.assertTrue((ROOT / "dashboard" / dashboard["entry"]).is_file())

    def test_python_dependencies_match_requirements_txt(self):
        required = [line.strip() for line in (ROOT / "requirements.txt").read_text().splitlines() if line.strip()]
        self.assertEqual(sorted(manifest()["python_dependencies"]), sorted(required))

    def test_pyproject_lists_the_same_runtime_dependencies_and_not_fastapi(self):
        """Hermes installs pyproject's [project].dependencies in preference to plugin.yaml's list."""
        try:
            import tomllib
        except ImportError:  # Python 3.10
            self.skipTest("tomllib needs Python 3.11")
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(sorted(project["dependencies"]), sorted(manifest()["python_dependencies"]))
        self.assertNotIn("fastapi", " ".join(project["dependencies"]), "fastapi is Hermes's own")
        self.assertEqual(project["name"], "hermes-bots-plugin")

    def test_restart_gateway_names_real_files_including_every_hook_file(self):
        listed = manifest()["restart"]["gateway"]
        for name in listed:
            self.assertTrue((ROOT / name).is_file(), name)
        self.assertEqual(len(listed), len(set(listed)))
        for must in ("__init__.py", "cards/__init__.py", "cards/alerts.py", "cards/notify.py", "mobile/events.py", "mobile/outbox.py",
                     "mobile/sealed.py", "routing/runtime_v2.py"):
            self.assertIn(must, listed)
        # The files only the dashboard (hermes serve) imports never need the gateway restarted.
        for serve_only in ("mobile/routes.py", "mobile/scan.py", "mobile/usage.py", "voice/routes.py", "dashboard/plugin_api.py"):
            self.assertNotIn(serve_only, listed)

    def test_provides_the_union_of_the_modules(self):
        data = manifest()
        self.assertTrue({"hermes_card_form", "hermes_task_wait", "jev_review"} <= set(data["provides_tools"]))
        self.assertTrue({"transform_llm_output", "on_session_end", "api_request_error"} <= set(data["provides_hooks"]))
        self.assertEqual(data["provides_middleware"], ["llm_request"])
        self.assertEqual(set(data["provides_skills"]), {"native-cards", "jev-routing"})

    def test_attribution_is_kept(self):
        self.assertIn("Apache License", (ROOT / "LICENSE").read_text())
        self.assertIn("bighelp", (ROOT / "cards" / "NOTICE").read_text())
        self.assertIn("34f2a16938ba69185a7f5ed9fa4a963f9a9fe1b4", (ROOT / "voice" / "NOTICE").read_text())
        self.assertIn("6961fdb4844421eef00c7dcdfaf9d6658845f693", (ROOT / "routing" / "SOURCE.md").read_text())
        self.assertIn("MIT License", (ROOT / "routing" / "SOURCE.md").read_text())


class GuardedLoadingTests(unittest.TestCase):
    def setUp(self):
        root.STATUS.clear()
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = patch.dict(os.environ, {"HERMES_HOME": self.home.name})
        env.start()
        self.addCleanup(env.stop)

    def test_every_module_registers(self):
        ctx = Ctx()
        root.register(ctx)
        self.assertEqual(root.STATUS, {"cards": "ok", "mobile": "ok", "voice": "ok", "routing": "off"})
        self.assertIn("hermes_card_chart", ctx.tools)
        self.assertIn("native-cards", ctx.skills)
        self.assertIn("transform_llm_output", ctx.hooks)

    def test_a_module_that_fails_to_import_is_reported_and_the_rest_load(self):
        real = importlib.import_module

        def fake(name, package=None):
            if name.endswith(".cards"):
                raise ImportError("cards is broken")
            return real(name, package)

        ctx = Ctx()
        with patch.object(root.importlib, "import_module", side_effect=fake):
            root.register(ctx)
        self.assertEqual(root.STATUS["cards"], "error: ImportError: cards is broken")
        self.assertEqual((root.STATUS["mobile"], root.STATUS["voice"], root.STATUS["routing"]), ("ok", "ok", "off"))
        self.assertEqual(ctx.tools, {})  # nothing of the failed module is half-registered

    def test_a_module_that_fails_to_register_does_not_stop_the_next(self):
        voice = importlib.import_module(root.__name__ + ".voice")
        with patch.object(voice, "register", side_effect=RuntimeError("no voice today")):
            ctx = Ctx()
            with patch.object(root, "module_enabled", return_value=True) as enabled, \
                    patch.object(importlib.import_module(root.__name__ + ".routing"), "register") as routing:
                root.register(ctx)
        self.assertEqual(root.STATUS["voice"], "error: RuntimeError: no voice today")
        self.assertEqual((root.STATUS["cards"], root.STATUS["mobile"], root.STATUS["routing"]), ("ok", "ok", "ok"))
        routing.assert_called_once_with(ctx)
        self.assertIn("hermes_card_chart", ctx.tools)
        enabled.assert_called()

    def test_register_never_raises(self):
        with patch.object(root, "module_enabled", side_effect=RuntimeError("config unreadable")):
            root.register(Ctx())
        self.assertTrue(all(v.startswith("error") for v in root.STATUS.values()))


def plugin_api(label, *, patched_import=None):
    if patched_import is None:
        return plugin_loader.load_plugin_api(label)
    with patch.object(importlib, "import_module", side_effect=patched_import):
        return plugin_loader.load_plugin_api(label)


@unittest.skipUnless(importlib.util.find_spec("fastapi"), "FastAPI is not installed (use Hermes's venv python)")
class InfoAndRoutesTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = patch.dict(os.environ, {"HERMES_HOME": self.home.name})
        env.start()
        self.addCleanup(env.stop)

    def client(self, api):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(api.router, prefix="/api/plugins/hermes-bots")
        return TestClient(app)

    def paths(self, api):
        """The served subpaths (OpenAPI spelling: ``{operation}`` for ``{operation:path}``)."""
        prefix = "/api/plugins/hermes-bots"
        return {path[len(prefix):] for path in self.client(api).app.openapi()["paths"]}

    def test_info(self):
        info = self.client(plugin_api("info")).get("/api/plugins/hermes-bots/info").json()
        self.assertEqual((info["name"], info["version"]), ("hermes-bots", "2.0.0"))
        self.assertEqual(info["modules"], {"cards": "ok", "mobile": "ok", "voice": "ok", "routing": "off"})
        self.assertIn("hermes_version", info)

    def test_every_old_subpath_is_kept_under_the_one_prefix(self):
        paths = self.paths(plugin_api("paths"))
        for subpath in ("/forms/{request_id}/submit", "/forms/{request_id}", "/decisions", "/decisions/close",
                        "/kanban/specify/{task_id}", "/kanban/decompose/{task_id}", "/kanban/prepare-new/{task_id}",
                        "/kanban/creation-settings", "/tasks/board", "/ssh/host-key", "/voice/{operation}", "/info"):
            self.assertIn(subpath, paths)
        self.assertNotIn("/routing/{operation}", paths, "routing is off unless configured")

    def test_the_mobile_routes_live_under_mobile_and_nothing_else_of_the_bridge_survives(self):
        paths = self.paths(plugin_api("mobile"))
        for subpath in ("/mobile/reads", "/mobile/replies", "/mobile/usage", "/mobile/kinds", "/mobile/view"):
            self.assertIn(subpath, paths)
        for gone in ("/mobile/devices", "/devices", "/health", "/internal/nudge", "/internal/approvals", "/usage", "/reads"):
            self.assertNotIn(gone, paths)
        self.assertFalse([p for p in paths if p.startswith("/internal")])

    def test_routing_routes_and_status_appear_once_configured(self):
        (Path(self.home.name) / "config.yaml").write_text("plugins:\n  entries:\n    hermes-bots:\n      settings:\n        routing_mode: shadow\n")
        api = plugin_api("routing")
        self.assertIn("/routing/{operation}", self.paths(api))
        self.assertEqual(self.client(api).get("/api/plugins/hermes-bots/info").json()["modules"]["routing"], "ok")

    def test_a_module_whose_routes_fail_is_reported_and_the_others_are_served(self):
        real = importlib.import_module

        def fake(name, package=None):
            if name.endswith(".voice.routes"):
                raise ImportError("voice needs httpx")
            return real(name, package)

        api = plugin_api("broken", patched_import=fake)
        client = self.client(api)
        modules = client.get("/api/plugins/hermes-bots/info").json()["modules"]
        self.assertEqual(modules["voice"], "error: ImportError: voice needs httpx")
        self.assertEqual(modules["cards"], "ok")
        self.assertNotIn("/voice/{operation}", self.paths(api))
        self.assertIn("/forms/{request_id}", self.paths(api))

    def test_voice_identity_checks_the_new_prefix(self):
        voice = importlib.import_module(root.__name__ + ".voice.routes")
        source = (ROOT / "voice" / "routes.py").read_text()
        self.assertIn('"/api/plugins/hermes-bots/voice/"', source)
        self.assertNotIn("hermes-bots-voice/", source)
        self.assertTrue(hasattr(voice, "router"))


PROBE = r"""
import importlib.util, json, sys, os
root_dir = sys.argv[1]
# Where Hermes is installed its own imports (config, constants, the turn finalizer cards probes for) pull in some
# of these: only what register() adds on top counts.
for name in ("hermes_constants", "hermes_cli.config", "agent.turn_finalizer", "tools.skill_provenance"):
    try:
        __import__(name)
    except Exception:
        pass
spec = importlib.util.spec_from_file_location("hermes_bots", os.path.join(root_dir, "__init__.py"),
                                              submodule_search_locations=[root_dir])
module = importlib.util.module_from_spec(spec); sys.modules["hermes_bots"] = module; spec.loader.exec_module(module)
if os.environ.get("PROBE_ROUTING") == "1":
    import types
    config = types.ModuleType("hermes_cli.config")
    config.load_config_readonly = lambda: {"plugins": {"entries": {"hermes-bots": {"settings": {"routing_mode": "on"}}}}}
    sys.modules["hermes_cli"] = types.ModuleType("hermes_cli"); sys.modules["hermes_cli.config"] = config
class Ctx:
    profile_name = "friday"
    def register_tool(self, *a, **k): pass
    def register_hook(self, *a, **k): pass
    def register_middleware(self, *a, **k): pass
    def register_skill(self, *a, **k): pass
heavy = ["httpx", "websockets", "pydantic", "fastapi", "starlette", "yaml"]
before = {m for m in heavy if m in sys.modules}
module.register(Ctx())
print(json.dumps({"status": module.STATUS, "loaded": sorted(m for m in heavy if m in sys.modules and m not in before),
                  "voice": [m for m in sys.modules if m.startswith("hermes_bots.voice.")],
                  "mobile": sorted(m for m in sys.modules if m.startswith("hermes_bots.mobile.") and not m.endswith((".worker", ".home")))}))
"""


class LazyImportTests(unittest.TestCase):
    """A bot worker calls register() and must not pull in the dashboard's dependencies."""

    def probe(self, **env):
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run([sys.executable, "-c", PROBE, str(ROOT)], capture_output=True, text=True, timeout=60,
                                    env={**os.environ, "HERMES_HOME": home, **env})
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_register_imports_none_of_the_heavy_modules(self):
        out = self.probe()
        self.assertEqual(out["status"], {"cards": "ok", "mobile": "ok", "voice": "ok", "routing": "off"})
        self.assertEqual(out["loaded"], [])
        self.assertEqual(out["voice"], [], "voice's code loads only with its routes")
        self.assertEqual(out["mobile"], [], "mobile's code loads only where an alert is written or a route is served")

    def test_register_with_routing_on_still_imports_none_of_them(self):
        out = self.probe(PROBE_ROUTING="1")
        self.assertEqual(out["status"]["routing"], "ok", out)
        self.assertEqual([m for m in out["loaded"] if m != "yaml"], [])  # routing may read config.yaml with yaml


if __name__ == "__main__":
    unittest.main()
