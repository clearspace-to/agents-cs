"""Credential boundary regression tests; only synthetic, unsigned fixture tokens."""
import asyncio
import base64
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager
from contextvars import ContextVar
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugin_loader
plugin_loader.load_root()  # the plugin as a package; its voice module is hermes_bots_dashboard.voice
from hermes_bots_dashboard.voice.live_voice_auth import (
    CODEX_BASE_URL, CodexLiveAuth, LiveAuthError, validate_codex_runtime,
)


def runtime():
    payload = base64.urlsafe_b64encode(json.dumps({
        "https://api.openai.com/auth": {"chatgpt_account_id": "fixture-account"},
    }).encode()).decode().rstrip("=")
    return dict(provider="openai-codex", api_mode="codex_responses", base_url=CODEX_BASE_URL,
                api_key="fixture." + payload + ".unsigned", source="hermes-auth-store")


class CredentialTests(unittest.IsolatedAsyncioTestCase):
    def test_selected_account_and_secret_repr(self):
        value = runtime()
        result = validate_codex_runtime(value, expected_account_id="fixture-account")
        self.assertEqual(result.account_id, "fixture-account")
        self.assertNotIn(value["api_key"], repr(result))
        with self.assertRaises(LiveAuthError):
            validate_codex_runtime(value, expected_account_id="other-account")

    def test_untrusted_provenance_and_conflicting_identity_fail_closed(self):
        for changes in (
            {"base_url": "https://untrusted.invalid/responses"},
            {"provider": "openai"}, {"api_mode": "chat_completions"},
            {"auth_type": "api_key"}, {"auth_mode": "api_key"},
            {"source": "custom"}, {"source": "api_key"},
            {"account_id": "other-account"}, {"chatgpt_account_id": "other-account"},
            {"credential_id": "unverified"}, {"api_key": "fixture\r\nInjected: header"},
        ):
            with self.subTest(changes=changes), self.assertRaises(LiveAuthError):
                validate_codex_runtime({**runtime(), **changes})

    def test_pool_entry_must_uniquely_match_without_advancing_cursor(self):
        value = runtime()
        entry = SimpleNamespace(runtime_api_key=value["api_key"], provider="openai-codex",
                                auth_type="oauth", source=value["source"], id="selected", base_url=CODEX_BASE_URL)
        pool = SimpleNamespace(entries=lambda: [entry])
        value["credential_pool"] = pool
        self.assertEqual(validate_codex_runtime(value).credential_id, "selected")
        pool.entries = lambda: [entry, entry]
        with self.assertRaises(LiveAuthError):
            validate_codex_runtime(value)

    async def test_resolver_called_once_with_explicit_subscription_and_safe_errors(self):
        calls = []
        async def resolver(**kwargs):
            calls.append(kwargs)
            raise RuntimeError("private-resolver-fixture")
        with self.assertRaises(LiveAuthError) as caught:
            await CodexLiveAuth(resolver).resolve()
        self.assertEqual(calls, [{"requested": "openai-codex", "target_model": "gpt-live-1-codex"}])
        self.assertNotIn("private-resolver-fixture", str(caught.exception))

    async def test_cancelled_resolver_does_not_return_credentials(self):
        entered = asyncio.Event()
        async def resolver(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(CodexLiveAuth(resolver).resolve())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class HostScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_auth_scopes_worker_and_pool_then_restores_caller(self):
        active = ContextVar("fixture_voice_scope", default="chat-profile")
        scopes = []
        @contextmanager
        def launch_scope(home):
            scopes.append(home)
            token = active.set(home)
            try:
                yield
            finally:
                active.reset(token)
        value = runtime()
        class Entry:
            provider, auth_type, source = "openai-codex", "oauth", value["source"]
            id, base_url = "selected", CODEX_BASE_URL
            @property
            def runtime_api_key(entry):
                self.assertEqual(active.get(), "pinned-launch-home")
                return value["api_key"]
        def resolver(**kwargs):
            # Runs on the worker thread; an unscoped dashboard read must fail.
            self.assertEqual(active.get(), "pinned-launch-home")
            return {**value, "credential_pool": SimpleNamespace(entries=lambda: [Entry()])}
        modules = {
            "hermes_constants": SimpleNamespace(get_routing_process_hermes_home=lambda: "pinned-launch-home"),
            "tui_gateway.launch_profile_policy": SimpleNamespace(launch_profile_runtime_scope=launch_scope),
            "hermes_cli.runtime_provider": SimpleNamespace(resolve_runtime_provider=resolver),
            "agent.codex_headers": SimpleNamespace(codex_cloudflare_headers=lambda *a, **k: {
                "ChatGPT-Account-ID": "fixture-account", "User-Agent": "fixture", "originator": "hermes-agent"}),
        }
        with patch.dict(sys.modules, modules):
            credentials = await CodexLiveAuth().resolve()
            self.assertEqual(credentials.account_id, "fixture-account")
            self.assertEqual(active.get(), "chat-profile")
            self.assertEqual(scopes, ["pinned-launch-home"])
            def failure(**kwargs):
                self.assertEqual(active.get(), "pinned-launch-home")
                raise RuntimeError("private secret must not escape")
            modules["hermes_cli.runtime_provider"].resolve_runtime_provider = failure
            with self.assertRaises(LiveAuthError) as error:
                await CodexLiveAuth().resolve()
            self.assertNotIn("private secret", str(error.exception))
            self.assertEqual(active.get(), "chat-profile")
