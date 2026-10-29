"""relay.py: a scheduled job's result is posted to the main chat as the run wrote it, cards included, and
the relay ledger records which run it came from."""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cards import EXAMPLES, FakeContext, cards, plugin, store_module  # noqa: E402

relay = sys.modules[plugin.__name__ + ".relay"]
JOB, NAME = "ccf70a8aa892", "Prudence: daily needs-you — max two items"
NOW = datetime(2026, 10, 14, 13, 0, tzinfo=timezone.utc).timestamp()  # 9:00 AM EDT


def handoff(body, name=NAME):
    return (f'[Cronjob "{name}" output — scheduled job, not the user. Review it, act on anything that needs '
            f"action, and summarize for the chat.]\n\n{body}")


class ParseTests(unittest.TestCase):
    def test_reads_name_and_body(self):
        self.assertEqual(relay.parse(handoff("Two items")), {"name": NAME, "body": "Two items"})

    def test_anything_else_is_not_a_hand_off(self):
        for text in ("Hi", "[Cronjob broken", handoff("   "), None, ["x"]):
            self.assertIsNone(relay.parse(text), text)

    def test_label_drops_the_bot_prefix_and_names_the_time(self):
        with patch.object(relay, "datetime", wraps=datetime) as dt:
            dt.fromtimestamp.return_value = datetime(2026, 10, 14, 9, 0)
            self.assertEqual(relay.label(NAME, NOW), "Daily needs-you — max two items · 9:00 AM")
        self.assertEqual(relay.label("services check", None), "Services check")


class HomeMixin:
    def make_home(self):
        self.home = Path(self.temp.name) / "home"
        (self.home / "cron").mkdir(parents=True)
        (self.home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
            {"id": JOB, "name": NAME, "deliver": "bot-chat"}, {"id": "def456", "name": "Elsewhere", "deliver": "telegram"}]}))
        self.state = sqlite3.connect(self.home / "state.db")
        self.state.execute("CREATE TABLE sessions (id TEXT, source TEXT, started_at REAL, title TEXT)")
        self.state.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT)")
        self.runs = sqlite3.connect(self.home / "cron" / "executions.db")
        self.runs.execute("CREATE TABLE executions (id TEXT, job_id TEXT, status TEXT, delivery_outcome TEXT, started_at TEXT)")

    def make_run(self, session, start, reply, execution="exec1", opened=6, title=None):
        self.state.execute("INSERT INTO sessions VALUES (?, 'cron', ?, ?)", (session, start + opened, title))
        self.state.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'assistant', ?)", (session, reply))
        self.state.commit()
        self.runs.execute("INSERT INTO executions VALUES (?, ?, 'completed', 'delivered', ?)",
                          (execution, JOB, datetime.fromtimestamp(start, timezone.utc).isoformat()))
        self.runs.commit()


class FindRunTests(HomeMixin, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.make_home()

    def tearDown(self):
        self.state.close()
        self.runs.close()
        self.temp.cleanup()

    def test_the_run_is_the_chat_whose_last_reply_is_the_result(self):
        self.make_run(f"cron_{JOB}_20261014_080000", NOW - 3600, "Yesterday's items", execution="e0")
        self.make_run(f"cron_{JOB}_20261014_090000", NOW - 60, "Two items need you.", execution="e1")
        found = relay.find_run(self.home, NAME, "Two items need you.\n", now=NOW)
        self.assertEqual((found["job_id"], found["run_session"], found["execution_id"]),
                         (JOB, f"cron_{JOB}_20261014_090000", "e1"))
        self.assertAlmostEqual(found["started_at"], NOW - 60)

    def test_falls_back_to_the_newest_chat_of_the_job(self):
        self.make_run(f"cron_{JOB}_20261014_090000", NOW - 60, "Slightly different text")
        found = relay.find_run(self.home, NAME, "Two items", now=NOW)
        self.assertEqual(found["run_session"], f"cron_{JOB}_20261014_090000")

    def test_finds_a_run_chat_named_the_way_hermes_names_them_since_2026_10_24(self):
        """A local-<hash> chat with the job only in its title: the result's label keeps its time (Windy, 2026-10-28)."""
        self.make_run("local-yesterday", NOW - 86400, "Yesterday's items", execution="e0", opened=0.03,
                      title=f"{NAME} · Oct 13 09:00")
        self.make_run("local-today", NOW - 60, "Two items need you.", execution="e1", opened=0.03,
                      title=f"{NAME} · Oct 14 09:00")
        self.make_run("local-other", NOW - 60, "Two items need you.", execution="e2", opened=0.03,
                      title="Elsewhere · Oct 14 09:00")
        found = relay.find_run(self.home, NAME, "Two items need you.", now=NOW)
        self.assertEqual((found["run_session"], found["execution_id"]), ("local-today", "e1"))
        self.assertAlmostEqual(found["started_at"], NOW - 60)

    def test_a_run_whose_execution_hermes_dropped_is_still_found_by_its_title(self):
        self.state.execute("INSERT INTO sessions VALUES ('local-pruned', 'cron', ?, ?)", (NOW - 60, f"{NAME} · Oct 14 09:00"))
        self.state.execute("INSERT INTO messages (session_id, role, content) VALUES ('local-pruned', 'assistant', 'Two items')")
        self.state.commit()
        found = relay.find_run(self.home, NAME, "Two items", now=NOW)
        self.assertEqual((found["run_session"], found["execution_id"]), ("local-pruned", ""))

    def test_unknown_job_or_no_chat_leaves_the_run_empty(self):
        self.assertEqual(relay.find_run(self.home, "Nobody: job", "x", now=NOW)["job_id"], "")
        self.assertEqual(relay.find_run(self.home, NAME, "x", now=NOW)["run_session"], "")


class HookTests(HomeMixin, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.make_home()
        self.root = patch.object(store_module, "hermes_root", return_value=Path(self.temp.name))
        self.root.start()
        self.home_patch = patch.object(plugin, "_hermes_home", return_value=self.home)
        self.home_patch.start()
        # The hooks read the clock: hold it at the fixture's morning, or the run ages out of find_run's window.
        self.clock = patch.object(relay.time, "time", return_value=NOW)
        self.clock.start()
        plugin._PENDING.clear()
        self.ctx = FakeContext()
        with patch.object(plugin, "_attach_supported", return_value=True):
            plugin.register(self.ctx)
        self.hooks = self.ctx.hooks
        self.run_chat = f"cron_{JOB}_20261014_090000"
        self.make_run(self.run_chat, NOW - 60, "Two items need you.")

    def tearDown(self):
        self.clock.stop()
        self.home_patch.stop()
        self.root.stop()
        self.state.close()
        self.runs.close()
        self.temp.cleanup()

    def test_a_local_run_chat_for_bot_chat_keeps_its_table_card(self):
        """Since Hermes's 2026-10-24 update a run chat is local-<hash>: its cards must still reach Bot Chat, not
        turn into pipe text (Windy's 10am nudge, 2026-10-29)."""
        self.make_run("local-abc", NOW - 600, "", execution="e9", opened=0.03)
        card_id = json.loads(self.ctx.tools["hermes_card_table"][1](EXAMPLES["table"], session_id="local-abc"))["card_id"]
        reply = self.hooks["transform_llm_output"](response_text="Hiring nudge", session_id="local-abc", platform="cron")
        self.assertEqual([c["id"] for c in cards.cards_in(reply)], [card_id])
        self.runs.execute("UPDATE executions SET job_id = 'def456' WHERE id = 'e9'")
        self.runs.commit()
        self.ctx.tools["hermes_card_table"][1](EXAMPLES["table"], session_id="local-abc")
        reply = self.hooks["transform_llm_output"](response_text="Hiring nudge", session_id="local-abc", platform="cron")
        self.assertEqual(cards.cards_in(reply), [])

    def test_the_reply_is_the_run_result_whatever_the_model_wrote(self):
        context = self.hooks["pre_llm_call"](session_id="bot", user_message=handoff("Two items need you."))
        self.assertIn("exactly as written", context["context"])
        self.assertEqual(self.hooks["pre_tool_call"](tool_name="terminal", args={}, session_id="bot")["action"], "block")
        reply = self.hooks["transform_llm_output"](response_text="Summary: two things.", session_id="bot", platform="tui")
        self.assertTrue(reply.endswith("\n\nTwo items need you."), reply)
        self.assertTrue(reply.startswith("Daily needs-you — max two items · "), reply)
        self.assertIsNone(self.hooks["pre_tool_call"](tool_name="terminal", args={}, session_id="bot"),
                          "Tools come back once the relay turn is over")
        row = plugin._RELAYS.for_run(self.run_chat)
        self.assertEqual((row["chat_session"], row["execution_id"], row["job_id"]), ("bot", "exec1", JOB))
        self.assertIsNotNone(row["replied_at"])

    def test_the_next_turn_in_that_chat_is_told_where_the_run_is(self):
        self.hooks["pre_llm_call"](session_id="bot", user_message=handoff("Two items need you."))
        self.hooks["transform_llm_output"](response_text="ok", session_id="bot", platform="tui")
        context = self.hooks["pre_llm_call"](session_id="bot", user_message="What was the Air Canada one about?")
        self.assertIn(self.run_chat, context["context"])
        self.assertIn("session_search", context["context"])
        self.assertIsNone(self.hooks["pre_tool_call"](tool_name="terminal", args={}, session_id="bot"))
        self.assertIsNone(self.hooks["transform_llm_output"](response_text="Flights.", session_id="bot", platform="tui"))
        self.assertIsNone(self.hooks["pre_llm_call"](session_id="other", user_message="Hi"))

    def test_a_decisions_block_can_still_become_cards(self):
        self.hooks["pre_llm_call"](session_id="bot", user_message=handoff("Two items need you."))
        self.assertIsNone(self.hooks["pre_tool_call"](tool_name="hermes_card_selection", args={}, session_id="bot"))
        card_id = json.loads(self.ctx.tools["hermes_card_selection"][1](EXAMPLES["selection"], session_id="bot"))["card_id"]
        reply = self.hooks["transform_llm_output"](response_text="One slot to pick.", session_id="bot", platform="tui")
        self.assertIn("\n\nOne slot to pick.", reply)
        self.assertEqual([c["id"] for c in cards.cards_in(reply)], [card_id])

    def test_a_bot_chat_runs_cards_survive_and_move_to_the_main_chat(self):
        card_id = json.loads(self.ctx.tools["hermes_card_selection"][1](EXAMPLES["selection"], session_id=self.run_chat))["card_id"]
        run_reply = self.hooks["transform_llm_output"](response_text="Pick a slot.", session_id=self.run_chat, platform="cron")
        self.assertEqual([c["id"] for c in cards.cards_in(run_reply)], [card_id], "The run keeps its card")
        self.assertEqual(plugin._DECISIONS.open(profile="friday"), [], "Not open until it is in the main chat")
        self.assertEqual(plugin._DECISIONS.open_chat_decisions(), 0)
        self.state.execute("UPDATE messages SET content = ? WHERE session_id = ?", (run_reply, self.run_chat))
        self.state.commit()

        context = self.hooks["pre_llm_call"](session_id="bot", user_message=handoff(run_reply))
        self.assertIn("cards included", context["context"])
        self.assertEqual(self.hooks["pre_tool_call"](tool_name="hermes_card_selection", args={}, session_id="bot")["action"],
                         "block", "No second set of cards")
        relayed = self.hooks["transform_llm_output"](response_text="ok", session_id="bot", platform="tui")
        self.assertEqual([c["id"] for c in cards.cards_in(relayed)], [card_id])
        self.assertEqual([d["session_id"] for d in plugin._DECISIONS.open(profile="friday")], ["bot"],
                         "Review opens the main chat, where the card now is")

    def test_a_card_answered_in_the_run_chat_before_its_relay_closes_once(self):
        card_id = json.loads(self.ctx.tools["hermes_card_selection"][1](EXAMPLES["selection"], session_id=self.run_chat))["card_id"]
        run_reply = self.hooks["transform_llm_output"](response_text="Pick a slot.", session_id=self.run_chat, platform="cron")
        answer = f'Decisions:\n\n```{cards.RESPONSE_FENCE}\n{{"card_id":"{card_id}","type":"selection","values":{{"selected":["tue"]}}}}\n```'
        self.hooks["pre_llm_call"](session_id=self.run_chat, user_message=answer)
        self.hooks["pre_llm_call"](session_id="bot", user_message=handoff(run_reply))
        self.hooks["transform_llm_output"](response_text="ok", session_id="bot", platform="tui")
        self.assertEqual(plugin._DECISIONS.open(profile="friday"), [], "Answered stays answered")

    def test_a_run_delivered_elsewhere_still_gets_text(self):
        session = "cron_def456_20261014_090000"
        self.ctx.tools["hermes_card_selection"][1](EXAMPLES["selection"], session_id=session)
        reply = self.hooks["transform_llm_output"](response_text="Pick.", session_id=session, platform="cron")
        self.assertNotIn("```", reply)

    def test_a_hand_off_for_an_unknown_run_is_still_relayed(self):
        self.hooks["pre_llm_call"](session_id="bot", user_message=handoff("Nightly check done.", name="Cisco: nightly"))
        reply = self.hooks["transform_llm_output"](response_text="Done.", session_id="bot", platform="tui")
        self.assertEqual(reply, "Nightly\n\nNightly check done.")


if __name__ == "__main__":
    unittest.main()
