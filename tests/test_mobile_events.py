"""Approvals, questions, board tasks, the attention badge and nudges: what ``cards`` hands to ``mobile.events`` (in the
same process or from another), and the sealed outbox rows that come out. Replaces the bridge's ``/internal/*`` tests."""
import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mobile_support import IPAD, PHONE, MobileCase, alerts, events, home, replies, state  # noqa: E402

TESTS = Path(__file__).resolve().parent


class EventTests(MobileCase):
    def only(self, device=PHONE):
        (payload,) = self.payloads(device)
        return payload

    def test_approval_alert_names_the_bot_carries_the_command_sealed_and_collapses_per_chat(self):
        body = {"event": "requested", "profile": "jarvis", "session_id": "chat", "command": "rm -rf build", "description": "Delete build output"}
        events.approval(body)
        (row,) = self.queued()
        payload = json.loads(row["payload_json"])
        aps = payload["aps"]
        self.assertEqual((row["push_type"], row["priority"]), ("alert", 10))
        self.assertEqual(aps["alert"], alerts.APPROVAL_PLACEHOLDER)
        self.assertEqual((aps["category"], aps["interruption-level"], aps["thread-id"]), ("approval", "time-sensitive", "jarvis:chat"))
        self.assertEqual((payload["bot"], payload["chat"], payload["approval"]), ("jarvis", "chat", True))
        opened = self.opened(payload)
        self.assertEqual(opened["title"], "Jarvis needs approval")
        self.assertIn("rm -rf build", opened["body"])
        self.assertIn("Delete build output", opened["body"])
        self.assertTrue(row["collapse_id"])
        self.assertLessEqual(row["expiration"], row["created"] + 3601)
        events.approval(body)
        self.assertEqual({r["collapse_id"] for r in self.queued()}, {row["collapse_id"]}, "a repeat replaces, not stacks")

    def test_approval_resolved_clears_it_everywhere_with_a_silent_push(self):
        events.approval({"event": "resolved", "profile": "jarvis", "session_id": "chat"})
        (row,) = self.queued()
        self.assertEqual((row["push_type"], row["priority"]), ("background", 5))
        self.assertEqual(json.loads(row["payload_json"]),
                         {"aps": {"content-available": 1}, "hermes": {"approvalResolved": {"profile": "jarvis", "chat": "chat"}}})

    def test_decisions_closed_clears_their_cards_everywhere_with_a_silent_push(self):
        events.decisions_closed({"profile": "jarvis", "cards": ["a" * 32, "../bad", 7]})
        (row,) = self.queued()
        self.assertEqual((row["push_type"], row["priority"]), ("background", 5))
        self.assertEqual(json.loads(row["payload_json"]),
                         {"aps": {"content-available": 1}, "hermes": {"decisionsClosed": {"profile": "jarvis", "cards": ["a" * 32]}}})
        self.clear()
        for bad in ({"profile": "jarvis", "cards": []}, {"profile": "../etc", "cards": ["a" * 32]}, {"cards": ["a" * 32]}):
            events.decisions_closed(bad)
        self.assertEqual(self.rows(), [])

    def test_approval_alert_offers_allow_for_this_chat_only_when_hermes_does(self):
        body = {"event": "requested", "profile": "jarvis", "session_id": "chat", "command": "rm -rf build", "description": "Delete"}
        for extra, category in (({}, "approval"), ({"allow_session": True}, "approval-session"), ({"allow_session": False}, "approval")):
            self.clear()
            events.approval({**body, **extra})
            self.assertEqual(self.only()["aps"]["category"], category)

    def test_approval_falls_back_to_the_session_key_and_ignores_malformed_events(self):
        events.approval({"event": "requested", "profile": "jarvis", "session_key": "k1", "command": "ls"})
        self.assertEqual(self.only()["chat"], "k1")
        self.clear()
        for bad in ({"event": "requested", "profile": "jarvis"}, {"event": "bogus", "profile": "jarvis", "session_id": "c"},
                    {"event": "requested", "profile": "../etc", "session_id": "c"}, {"event": "requested", "session_id": "c"}):
            events.approval(bad)
        self.assertEqual(self.rows(), [])

    def test_question_alert_opens_the_chat_and_clears_when_answered(self):
        body = {"event": "requested", "profile": "jarvis", "session_id": "chat", "question": "Which venue?", "count": 3}
        self.assertTrue(events.question(body))
        payload = self.only()
        aps = payload["aps"]
        self.assertEqual(aps["alert"], alerts.QUESTION_PLACEHOLDER)  # sealed devices see no text
        self.assertEqual((aps["category"], aps["interruption-level"]), ("question", "time-sensitive"))
        self.assertEqual({k: payload[k] for k in ("bot", "chat", "question")}, {"bot": "jarvis", "chat": "chat", "question": True})
        opened = self.opened(payload)
        self.assertEqual((opened["title"], opened["body"]), ("Jarvis has a question", "Which venue?\n(+2 more)"))
        self.clear()
        self.assertFalse(events.question({"event": "resolved", "profile": "jarvis", "session_id": "chat"}))
        (row,) = self.queued()
        self.assertEqual(row["push_type"], "background")
        self.assertEqual(json.loads(row["payload_json"])["hermes"]["questionResolved"], {"profile": "jarvis", "chat": "chat"})

    def test_question_in_a_messaging_chat_never_rings_the_phone(self):
        self.session("tg", "telegram")
        self.assertFalse(events.question({"event": "requested", "profile": "jarvis", "session_id": "tg", "question": "Hm?"}))
        self.assertEqual(self.rows(), [])

    def test_task_alert_names_the_bot_the_kind_and_the_task(self):
        body = {"event": "requested", "board": "default", "task_id": "t_1", "title": "Choose a date", "bot": "jarvis", "kind": "decision"}
        events.task(body)
        payload = self.only()
        self.assertEqual(payload["aps"]["alert"], alerts.TASK_PLACEHOLDER)
        self.assertEqual((payload["aps"]["category"], payload["aps"]["thread-id"], payload["aps"]["interruption-level"]),
                         ("task", "task:t_1", "time-sensitive"))
        self.assertEqual({k: payload[k] for k in ("bot", "task", "board", "kind", "taskTitle")},
                         {"bot": "jarvis", "task": "t_1", "board": "default", "kind": "decision", "taskTitle": "Choose a date"})
        self.assertEqual(self.opened(payload)["title"], "Jarvis needs your decision")
        self.assertEqual(self.opened(payload)["body"], "Choose a date")
        self.clear()
        events.task({**body, "kind": "blocked", "reason": "Which discount?"})
        opened = self.opened(self.only())
        self.assertEqual((opened["title"], opened["body"]), ("Jarvis needs your input", "Choose a date\nWhich discount?"))
        self.clear()
        events.task({**body, "kind": "review"})
        self.assertEqual(self.opened(self.only())["title"], "Jarvis finished: review the work")
        self.clear()
        events.task({"event": "resolved", "task_id": "t_1", "board": "default"})
        (row,) = self.queued()
        self.assertEqual(row["push_type"], "background")
        self.assertEqual(json.loads(row["payload_json"])["hermes"]["taskResolved"], {"task": "t_1", "board": "default"})

    def test_every_device_gets_its_own_sealed_copy(self):
        self.link([self.phone, self.ipad])
        events.approval({"event": "requested", "profile": "jarvis", "session_id": "chat", "command": "ls"})
        self.assertEqual({r["device_id"] for r in self.queued()}, {PHONE, IPAD})
        for who in (self.phone, self.ipad):
            self.assertIn("ls", self.opened(self.only(who.id), who)["body"])
        with self.assertRaises(Exception):
            self.opened(self.only(IPAD), self.phone)

    def test_decisions_count_in_the_badge_tasks_dont_and_resolved_tasks_clear(self):
        report = {"board": "default", "count": 3, "decisions": 1,
                  "tasks": [{"id": "t_1", "title": "Choose a date", "bot": "jarvis", "kind": "decision"},
                            {"id": "t_2", "title": "Review the CO", "bot": "jarvis", "kind": "review"}]}
        self.assertEqual(events.attention(report), [])
        self.assertEqual(state.badge_for(self.con, state.OWNER, PHONE), 1, "the decision counts; board tasks badge only the Kanban tab")
        first = [json.loads(r["payload_json"]) for r in self.queued()]
        self.assertEqual({tuple(p["aps"]) for p in first}, {("badge",), ("content-available",)}, "every device's badge follows at once")
        reads = self.call("GET", "/reads").json()
        self.assertEqual((reads["count"], reads["attention"]["count"]), (1, 3))
        self.assertEqual([t["id"] for t in reads["attention"]["tasks"]], ["t_1", "t_2"])
        self.clear()
        events.attention(report)
        self.assertEqual(self.rows(), [], "an unchanged report (every dispatcher tick) pushes nothing")
        self.assertEqual(events.attention({**report, "count": 1, "decisions": 0, "tasks": report["tasks"][1:]}), ["t_1"])
        resolved = [json.loads(r["payload_json"]) for r in self.queued() if r["push_type"] == "background"
                    and "taskResolved" in json.loads(r["payload_json"]).get("hermes", {})]
        self.assertEqual(resolved[0]["hermes"]["taskResolved"], {"task": "t_1", "board": "default"})
        self.assertEqual(state.badge_for(self.con, state.OWNER, PHONE), 0, "a task still waiting never counts on the icon")

    def test_attention_is_kept_even_with_no_devices(self):
        self.link([])
        events.attention({"board": "b", "count": 2, "decisions": 0, "tasks": [{"id": "t_9", "kind": "review"}]})
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.call("GET", "/reads").json()["attention"]["count"], 2)

    def test_attention_reports_are_clamped(self):
        events.attention({"board": "b" * 200, "count": 10_001, "tasks": []})
        events.attention({"board": "x", "count": "3", "tasks": []})
        events.attention({"board": "x", "count": 1, "tasks": [{"id": "bad id!"}, {"id": "ok", "title": "t" * 500, "bot": "b" * 100, "kind": "k" * 50}]})
        (task,) = self.call("GET", "/reads").json()["attention"]["tasks"]
        self.assertEqual((len(task["title"]), len(task["bot"]), len(task["kind"])), (200, 64, 20))



class NudgeAndHandOffTests(MobileCase):
    def test_a_nudge_touches_the_file_the_connector_watches(self):
        nudge = home.nudge_path()
        self.assertEqual(nudge, home.delivery_path().with_suffix(".nudge"), "the file every outbox commit touches too")
        self.assertFalse(nudge.exists())
        events.nudge({"profile": "jarvis"})
        self.assertTrue(nudge.exists())

    def test_an_event_from_a_profile_process_lands_in_the_roots_outbox(self):
        """cards runs in a profile's gateway or a Kanban worker, the scan and the drain in the connector: the outbox database
        is the whole hand-off (no HTTP, no key, no socket)."""
        profile_home = self.root / "profiles" / "jarvis"
        script = textwrap.dedent("""
            import importlib, sys
            sys.path.insert(0, %r)
            import plugin_loader
            plugin_loader.load_root()
            events = importlib.import_module(plugin_loader.PACKAGE + ".mobile.events")
            events.approval({"event": "requested", "profile": "jarvis", "session_id": "chat", "command": "ls -la"})
            events.nudge({"profile": "jarvis"})
            """ % str(TESTS))
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60,
                                env={**os.environ, "HERMES_HOME": str(profile_home)})
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        (payload,) = self.payloads()
        self.assertEqual(self.opened(payload)["title"], "Jarvis needs approval")
        self.assertTrue(home.nudge_path().exists(), "the nudge file is beside the delivery database, in the root's home")
        self.assertEqual(home.nudge_path().parent, self.root / "hermes-bots")


if __name__ == "__main__":
    unittest.main()
