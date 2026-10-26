"""The approval, question and finished-turn alerts (alerts.py, formerly the mobile-push-approvals plugin),
without Hermes: what they send, and what they stay quiet about."""
import importlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugin_loader  # noqa: E402
from test_cards import plugin  # noqa: E402

alerts = sys.modules[plugin.__name__ + ".alerts"]
notify = sys.modules[plugin.__name__ + ".notify"]


class Recorder:
    def __init__(self):
        self.sent = []

    def __call__(self, path, body):
        self.sent.append((path, body))


class AlertEventTests(unittest.TestCase):
    def setUp(self):
        self.recorder = Recorder()
        self.sent = self.recorder.sent
        notify.transport = self.recorder
        alerts._SMART_DENIED.clear()

    def tearDown(self):
        notify.transport = None

    def test_registered_through_the_cards_plugin(self):
        hooks = {}

        class Context:
            profile_name = "friday"

            def register_tool(self, **_kwargs):
                pass

            def register_hook(self, name, callback):
                hooks.setdefault(name, []).append(callback)

            def register_skill(self, *_args, **_kwargs):
                pass

        with tempfile.TemporaryDirectory() as home, \
                patch.object(sys.modules[plugin.__name__ + ".store"], "hermes_root", return_value=Path(home)):
            plugin.register(Context())
        self.assertIn(alerts.on_request, hooks["pre_approval_request"])
        self.assertIn(alerts.on_response, hooks["post_approval_response"])
        self.assertIn(alerts.on_turn, hooks["post_llm_call"])
        manifest = (Path(plugin.__file__).parent.parent / "plugin.yaml").read_text()
        block = manifest.split("provides_hooks:")[1].split("provides_middleware:")[0]
        declared = {line.strip()[2:] for line in block.splitlines() if line.strip().startswith("- ")}
        # Every hook the plugin declares is one the cards module registers (routing's pre_llm_call is shared with it).
        self.assertEqual(set(hooks), declared)

    def test_request_sends_redacted_bounded_command_and_session_identity(self):
        alerts.on_request(session_id="s1", session_key="k1", command="x" * 900, description="Do it", surface="gateway")
        (path, body), = self.sent
        self.assertEqual(path, "approval")
        self.assertEqual(body["event"], "requested")
        self.assertEqual((body["session_id"], body["session_key"], body["surface"]), ("s1", "k1", "gateway"))
        self.assertEqual(len(body["command"]), 600, "Commands are truncated before they leave Hermes")
        self.assertEqual(body["description"], "Do it")
        self.assertEqual(body["profile"], "default", "Without Hermes the profile falls back to default")
        self.assertTrue(body["allow_session"])

    def test_allow_for_this_chat_follows_hermes(self):
        """Hermes offers "session" unless the guard model denied and the owner may override once."""
        request = dict(session_key="k", pattern_key="rm", command="rm -rf build", description="Delete", surface="gateway")
        alerts.on_request(**request)
        alerts.on_response(session_key="k", pattern_key="rm", surface="smart", choice="smart_deny")
        alerts.on_request(**request)
        alerts.on_request(**request)  # the override was one request; the next is ordinary again
        self.assertEqual([body["allow_session"] for _path, body in self.sent], [True, False, True],
                         "the smart verdict itself never alerts")

    def test_terminal_guard_model_and_coalesced_requests_never_alert(self):
        alerts.on_request(surface="cli", command="ls")
        alerts.on_request(surface="smart", command="ls")
        alerts.on_request(surface="gateway", command="ls", coalesced=True)
        alerts.on_response(surface="cli", choice="once")
        alerts.on_response(surface="smart", choice="smart_approve")
        self.assertEqual(self.sent, [])

    def test_response_sends_no_command_text(self):
        alerts.on_response(session_id="s1", surface="gateway", command="secret", description="secret", choice="once")
        (_path, body), = self.sent
        self.assertEqual(body["event"], "resolved")
        self.assertEqual((body["command"], body["description"]), ("", ""))

    def test_clarify_alerts_through_the_plugins_tool_hooks(self):
        plugin.on_pre_tool_call(tool_name="clarify", args={"questions": [{"question": "Which day?"}, "Where?"]}, session_id="s1")
        plugin.on_post_tool_call(tool_name="clarify", session_id="s1")
        plugin.on_pre_tool_call(tool_name="terminal", args={}, session_id="s1")
        plugin.on_post_tool_call(tool_name="terminal", args={}, session_id="s1")
        plugin.on_pre_tool_call(tool_name="clarify", args={"question": "Ship it?", "choices": ["Yes", "No"]}, session_id="s1")
        self.assertEqual(self.sent, [
            ("question", {"event": "requested", "profile": "default", "session_id": "s1", "question": "Which day?", "count": 2}),
            ("question", {"event": "resolved", "profile": "default", "session_id": "s1"}),
            ("question", {"event": "requested", "profile": "default", "session_id": "s1", "question": "Ship it?", "count": 1}),
        ])

    def test_finished_turn_nudges_with_the_profile_only(self):
        alerts.on_turn(assistant_response="  ")
        self.assertEqual(self.sent, [], "Nothing said, nothing to alert")
        alerts.on_turn(assistant_response="Done.", user_message="private", session_id="s1")
        self.assertEqual(self.sent, [("nudge", {"profile": "default"})])


class AlertTransportTests(unittest.TestCase):
    """Without a recorder, an event goes to the mobile module in this process (no HTTP, no key)."""

    def setUp(self):
        self.events = importlib.import_module(plugin_loader.PACKAGE + ".mobile.events")

    def test_hands_the_event_to_the_mobile_module_in_process(self):
        done, calls = threading.Event(), []

        def nudge(body):
            calls.append(body)
            done.set()

        with patch.object(self.events, "nudge", side_effect=nudge):
            alerts.on_turn(assistant_response="Done.")
            self.assertTrue(done.wait(5))
        self.assertEqual(calls, [{"profile": "default"}])

    def test_each_kind_is_the_mobile_function_of_that_name(self):
        for kind in ("approval", "question", "task", "attention", "nudge"):
            with patch.object(self.events, kind) as handler:
                notify.post(kind, {"event": "resolved"}, wait=True)
            handler.assert_called_once_with({"event": "resolved"})

    def test_a_failure_is_dropped_silently(self):
        with patch.object(self.events, "task", side_effect=RuntimeError("outbox locked")):
            notify.post("task", {"event": "resolved", "task_id": "t"}, wait=True)  # does not raise
        notify.post("approvals", {}, wait=True)  # nor does a kind that doesn't exist
        notify.post("approval", "not a dict", wait=True)  # nor a malformed event

    def test_the_alert_path_talks_to_no_bridge(self):
        here = Path(plugin.__file__).parent
        source = "".join((here / name).read_text() for name in ("alerts.py", "notify.py"))
        for gone in ("9131", "internal.key", "urllib.request", "X-Hermes-Internal-Key"):
            self.assertNotIn(gone, source)


if __name__ == "__main__":
    unittest.main()
