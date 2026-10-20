"""Routing as a module of hermes-bots: off unless configured, the key as 'unavailable', homes from Hermes."""
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugin_loader  # noqa: E402
from test_plugin import Ctx  # noqa: E402

root = plugin_loader.load_root()
settings = importlib.import_module(root.__name__ + ".routing.settings")
paths = importlib.import_module(root.__name__ + ".paths")
rt = importlib.import_module(root.__name__ + ".routing.runtime_v2")
jev = importlib.import_module(root.__name__ + ".routing.jev")
v2 = importlib.import_module(root.__name__ + ".routing.routing_v2")


def config(**entries):
    return {"plugins": {"entries": {name: {"settings": values} for name, values in entries.items()}}}


class ConfiguredTests(unittest.TestCase):
    def test_off_unless_configured(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertIsNone(settings.configured_mode({}))
            self.assertEqual(settings.mode({}), "off")
            self.assertFalse(settings.configured({}, home))
            self.assertFalse(settings.configured(config(**{"hermes-bots": {"card_guard": "on"}}), home))
            self.assertFalse(settings.configured(config(**{"hermes-bots": {"routing_mode": "bogus"}}), home))

    def test_the_new_setting_the_old_setting_and_an_existing_store_each_configure_it(self):
        with tempfile.TemporaryDirectory() as home:
            for given in (config(**{"hermes-bots": {"routing_mode": "shadow"}}), config(**{"hermes-bots": {"routing_mode": "off"}})):
                self.assertTrue(settings.configured(given, home), given)
            self.assertFalse(settings.configured(config(**{"jev-routing": {"mode": "on"}}), home), "the 1.x name is not read")
            (Path(home) / "jev-routing-v2.sqlite").write_text("")
            self.assertTrue(settings.configured({}, home), "a profile jev-routing already set up keeps routing")

    def test_an_unquoted_yaml_on_or_off_is_understood(self):
        import yaml
        text = "plugins:\n  entries:\n    hermes-bots:\n      settings:\n        routing_mode: %s\n"
        self.assertEqual(settings.mode(yaml.safe_load(text % "on")), "on")
        self.assertEqual(settings.configured_mode(yaml.safe_load(text % "off")), "off")

    def test_only_the_hermes_bots_setting_counts(self):
        given = config(**{"hermes-bots": {"routing_mode": "shadow"}, "jev-routing": {"mode": "on"}})
        self.assertEqual(settings.mode(given), "shadow")
        self.assertEqual(settings.mode(config(**{"jev-routing": {"mode": "on"}})), "off")

    def test_the_engine_reads_the_setting(self):
        def controls(given):
            with tempfile.TemporaryDirectory() as home:  # a fresh store: the first policy() persists the default
                engine = v2.Engine(Path(home), lambda: given, lambda: {}, api=lambda *a: {})
                self.addCleanup(engine.pool.shutdown)
                return engine.controls("s")["mode"]
        self.assertEqual(controls({}), "off")
        self.assertEqual(controls(config(**{"hermes-bots": {"routing_mode": "on"}})), "on")
        self.assertEqual(controls(config(**{"hermes-bots": {"routing_mode": "shadow"}})), "shadow")
        self.assertEqual(controls(config(**{"hermes-bots": {"routing_mode": "off"}})), "off")


class RegisterTests(unittest.TestCase):
    def setUp(self):
        root.STATUS.clear()
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = patch.dict(os.environ, {"HERMES_HOME": self.home.name})
        env.start()
        self.addCleanup(env.stop)

    def test_nothing_of_routing_registers_by_default(self):
        ctx = Ctx()
        with patch.object(paths, "config", return_value={}):
            root.register(ctx)
        self.assertEqual(root.STATUS["routing"], "off")
        self.assertNotIn("jev_review", ctx.tools)
        self.assertNotIn("llm_request", ctx.middleware)
        self.assertNotIn("on_session_end", ctx.hooks)
        self.assertNotIn("jev-routing", ctx.skills)

    def test_everything_registers_once_configured(self):
        ctx = Ctx()
        with patch.object(paths, "config", return_value=config(**{"hermes-bots": {"routing_mode": "shadow"}})):
            root.register(ctx)
        self.assertEqual(root.STATUS["routing"], "ok")
        self.assertIn("jev_review", ctx.tools)
        self.assertIn("llm_request", ctx.middleware)
        self.assertTrue({"on_session_end", "api_request_error", "pre_llm_call", "pre_tool_call"} <= set(ctx.hooks))
        self.assertIn("jev-routing", ctx.skills)
        self.assertEqual(len(ctx.hooks["pre_tool_call"]), 2, "cards' guard and routing's tool guard")

    def test_a_profile_with_a_store_but_no_setting_keeps_routing(self):
        (Path(self.home.name) / "jev-routing-v2.sqlite").write_text("")
        ctx = Ctx()
        with patch.object(paths, "config", return_value={}):
            root.register(ctx)
        self.assertEqual(root.STATUS["routing"], "ok")


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = patch.dict(os.environ, {"HERMES_HOME": self.home.name})
        env.start()
        self.addCleanup(env.stop)
        self.secret = Path(self.home.name) / "secrets" / "jev-openrouter-api-key"

    def test_a_missing_key_is_unavailable_not_an_error(self):
        with patch.object(paths, "config", return_value={}):
            self.assertIsNone(settings.key_file())
            with self.assertRaises(settings.KeyUnavailable):
                settings.api_key()
            engine = v2.Engine(Path(self.home.name), lambda: {}, lambda: {})
            self.addCleanup(engine.pool.shutdown)
            self.assertEqual(engine.classify({"current_request": "hello"}), (None, "classifier_unavailable"))
            reviewed = jev.run({"source_path": "x.txt", "mode": "select", "query": "q"}, home=self.home.name, workspace=self.home.name)
            self.assertEqual((reviewed["status"], reviewed["reason"]), ("fallback", "routing_unavailable"))

    def test_the_key_lives_under_the_hermes_home_secrets(self):
        self.secret.parent.mkdir()
        self.secret.write_text("sk-test\n")
        with patch.object(paths, "config", return_value={}):
            self.assertEqual(settings.key_file(), self.secret)
            self.assertEqual(settings.api_key(), "sk-test")

    def test_an_empty_key_file_is_unavailable(self):
        self.secret.parent.mkdir()
        self.secret.write_text("  \n")
        with patch.object(paths, "config", return_value={}):
            with self.assertRaises(settings.KeyUnavailable):
                settings.api_key()

    def test_routing_key_file_setting(self):
        other = Path(self.home.name) / "elsewhere.key"
        other.write_text("sk-other")
        given = config(**{"hermes-bots": {"routing_key_file": str(other)}})
        self.assertEqual(settings.key_file(given), other)
        self.assertIsNone(settings.key_file(config(**{"hermes-bots": {"routing_key_file": str(other) + ".gone"}})))


class HomeForTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        env = patch.dict(os.environ, {"HERMES_HOME": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def install(self, home, setting=True):
        (home / "plugins" / "hermes-bots" / "routing").mkdir(parents=True)
        (home / "plugins" / "hermes-bots" / "routing" / "routing_v2.py").write_text("")
        if setting:
            (home / "config.yaml").write_text("plugins:\n  entries:\n    hermes-bots:\n      settings:\n        routing_mode: on\n")

    def test_the_root_and_a_profile_resolve_under_hermes_root(self):
        self.install(self.root)
        self.install(self.root / "profiles" / "jarvis")
        self.assertEqual(rt.home_for("friday"), self.root)
        self.assertEqual(rt.home_for("default"), self.root)
        self.assertEqual(rt.home_for("jarvis"), self.root / "profiles" / "jarvis")

    def test_from_a_profile_process_the_root_is_still_the_root(self):
        profile = self.root / "profiles" / "jarvis"
        self.install(self.root)
        self.install(profile)
        with patch.dict(os.environ, {"HERMES_HOME": str(profile)}), patch.dict(sys.modules, {"hermes_constants": None}):
            self.assertEqual(rt.root(), self.root)
            self.assertEqual(rt.home_for("friday"), self.root)

    def test_unknown_unconfigured_or_not_installed_is_agent_not_found(self):
        self.install(self.root, setting=False)  # installed, never configured
        (self.root / "profiles" / "bare").mkdir(parents=True)  # no plugin
        for agent in ("friday", "bare", "nobody"):
            with self.assertRaises(v2.RoutingError) as caught:
                rt.home_for(agent)
            self.assertEqual((caught.exception.code, caught.exception.status), ("agent_not_found", 404), agent)
        with self.assertRaises(v2.RoutingError) as caught:
            rt.home_for("../etc")
        self.assertEqual(caught.exception.code, "invalid_identifier")

    def test_the_dashboard_serves_routing_when_any_home_has_it(self):
        self.assertFalse(settings.configured_anywhere())
        self.install(self.root / "profiles" / "jarvis")
        self.assertTrue(settings.configured_anywhere())


if __name__ == "__main__":
    unittest.main()


class ConnectorAuthenticationTests(unittest.TestCase):
    def test_the_connectors_loopback_bearer_reaches_routing(self):
        import asyncio
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
        from fastapi.testclient import TestClient
        from hermes_bots_connector.hermes import HermesLink, Detection
        routes = importlib.import_module(root.__name__ + '.routing.routes')
        app = FastAPI()
        app.state.auth_required = False
        app.include_router(routes.router, prefix='/api/plugins/hermes-bots')
        # Hermes' outer mount validates either supported session-token header.
        @app.middleware('http')
        async def gate(request, call_next):
            if request.headers.get('authorization') != 'Bearer local-secret':
                return JSONResponse({}, status_code=401)
            request.state.token_authenticated = getattr(app.state, 'api_token', False)
            return await call_next(request)
        with patch.object(routes, 'dispatch', return_value={'ok': True}), TestClient(app) as client:
            link = HermesLink()
            self.addCleanup(lambda: asyncio.run(link.close()))
            link.detection = Detection(mode='loopback', token='local-secret')
            reply = client.get('/api/plugins/hermes-bots/routing/capabilities', headers=link.headers)
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertEqual(client.get('/api/plugins/hermes-bots/routing/capabilities').status_code, 401)
            app.state.auth_required = True
            self.assertEqual(client.get('/api/plugins/hermes-bots/routing/capabilities', headers=link.headers).status_code, 401)
            app.state.auth_required = False
            app.state.api_token = True
            self.assertEqual(client.get('/api/plugins/hermes-bots/routing/capabilities', headers=link.headers).status_code, 401)
