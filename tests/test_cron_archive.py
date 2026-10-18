"""cron_archive.py without Hermes: a scheduled run's chat is archived only when its result was relayed
into the main chat (the relay ledger) or the run was silent, and every doubt leaves it in Scheduled."""
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cards import plugin  # noqa: E402

cron_archive = sys.modules[plugin.__name__ + ".cron_archive"]
store = sys.modules[plugin.__name__ + ".store"]

JOB = "b53527d462af"
NOW = datetime(2026, 10, 13, 20, 0, tzinfo=timezone.utc).timestamp()


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve() / "home"
        (self.home / "cron").mkdir(parents=True)
        self.executions = sqlite3.connect(self.home / "cron" / "executions.db")
        self.executions.execute("CREATE TABLE executions (id TEXT, job_id TEXT, status TEXT, "
                                "delivery_outcome TEXT, started_at TEXT)")
        self.state = sqlite3.connect(self.home / "state.db")
        self.state.execute("CREATE TABLE sessions (id TEXT, source TEXT, started_at REAL, ended_at REAL, "
                           "archived INTEGER DEFAULT 0, title TEXT)")
        self.state.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
                           "timestamp REAL, finish_reason TEXT)")
        self.relays = store.RelayLog(Path(self.tmp.name) / "forms.db")
        self.archived = []
        self.decided = cron_archive.Decided(Path(self.tmp.name) / "decided.db")

    def tearDown(self):
        self.executions.close()
        self.state.close()
        self.tmp.cleanup()

    def run_(self, execution_id, start, *, status="completed", outcome="delivered", chat_after=6.0,
             reply="DECISIONS FOR MARK", relayed=True, replied=True, job=JOB):
        self.executions.execute("INSERT INTO executions VALUES (?, ?, ?, ?, ?)",
                                (execution_id, job, status, outcome, iso(start)))
        session_id = None
        if chat_after is not None:
            session_id = f"cron_{job}_{execution_id}"
            self.state.execute("INSERT INTO sessions (id, source, started_at, ended_at, archived) VALUES (?, 'cron', ?, ?, 0)",
                               (session_id, start + chat_after, start + 30))
            self.state.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'assistant', ?)",
                               (session_id, reply))
            if relayed:
                self.relays.record(session_id, home=str(self.home), job_id=job, job_name="Friday: weekly brief",
                                   execution_id=execution_id, chat_session="bot", label="Weekly brief · 9:00 AM",
                                   now=start + 40)
                if replied:
                    self.relays.replied(session_id, now=start + 50)
        self.executions.commit()
        self.state.commit()
        return session_id

    def sweep(self):
        return cron_archive.sweep(self.home, now=NOW, decided=self.decided, relays=self.relays,
                                  archive=lambda home, sid: self.archived.append(sid) or True)

    def test_a_run_relayed_to_the_main_chat_is_archived(self):
        chat = self.run_("e1", NOW - 3600)
        self.assertEqual(self.sweep(), [chat])

    def test_a_silent_run_is_archived(self):
        chat = self.run_("e1", NOW - 3600, outcome="suppressed", relayed=False, reply="[SILENT]")
        self.assertEqual(self.sweep(), [chat])

    def test_every_doubt_leaves_the_run_in_scheduled(self):
        self.run_("failed", NOW - 9000, status="failed", relayed=False)
        self.run_("running", NOW - 8000, status="running", relayed=False)
        self.run_("handed-not-replied", NOW - 7000, replied=False)
        self.run_("elsewhere", NOW - 6000, outcome="delivered", relayed=False)
        self.run_("delivery-failed", NOW - 5000, outcome="failed", relayed=False)
        self.run_("marker", NOW - 4000, reply="[CRON_FAILURE]\nchild failed")
        self.run_("relayed-but-run-failed", NOW - 3000, status="failed")
        self.assertEqual(self.sweep(), [])

    def test_a_silent_run_without_exactly_one_chat_is_left_alone(self):
        self.run_("no-chat", NOW - 7200, outcome="suppressed", chat_after=None)
        self.run_("two-chats", NOW - 3600, outcome="suppressed", relayed=False)
        self.state.execute("INSERT INTO sessions (id, source, started_at, ended_at, archived) VALUES (?, 'cron', ?, ?, 0)",
                           (f"cron_{JOB}_extra", NOW - 3500, NOW - 3400))
        self.state.commit()
        self.assertEqual(self.sweep(), [])

    def test_a_run_is_decided_once_so_unarchiving_sticks(self):
        chat = self.run_("e1", NOW - 3600)
        self.assertEqual(self.sweep(), [chat])
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.archived, [chat])

    def test_a_reply_that_lands_later_is_picked_up_by_a_later_sweep(self):
        chat = self.run_("e1", NOW - 3600, replied=False)
        self.assertEqual(self.sweep(), [])
        self.relays.replied(chat, now=NOW - 10)
        self.assertEqual(self.sweep(), [chat])

    def test_runs_older_than_two_days_are_left_alone(self):
        self.run_("old", NOW - 3 * 86400)
        self.assertEqual(self.sweep(), [])

    def test_already_archived_chats_are_skipped(self):
        chat = self.run_("e1", NOW - 3600)
        self.state.execute("UPDATE sessions SET archived = 1 WHERE id = ?", (chat,))
        self.state.commit()
        self.assertEqual(self.sweep(), [])

    def test_a_relay_without_an_execution_id_still_archives_on_its_chat(self):
        session_id = f"cron_{JOB}_e9"
        self.state.execute("INSERT INTO sessions (id, source, started_at, ended_at, archived) VALUES (?, 'cron', ?, ?, 0)",
                           (session_id, NOW - 3600, NOW - 3500))
        self.state.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'assistant', 'Two items')", (session_id,))
        self.state.commit()
        self.relays.record(session_id, home=str(self.home), job_id=JOB, job_name="x", execution_id="",
                           chat_session="bot", label="X · 9:00 AM", now=NOW - 100)
        self.relays.replied(session_id, now=NOW - 90)
        self.assertEqual(self.sweep(), [session_id])

    def test_registered_on_finished_turns(self):
        hooks = []

        class Context:
            profile_name = "friday"

            def register_tool(self, **_):
                pass

            def register_hook(self, name, callback):
                hooks.append((name, callback))

            def register_skill(self, *_, **__):
                pass

        with patch.object(store, "hermes_root", return_value=Path(self.tmp.name)):
            plugin.register(Context())
        self.assertIn(("post_llm_call", cron_archive.on_turn), hooks)


if __name__ == "__main__":
    unittest.main()
