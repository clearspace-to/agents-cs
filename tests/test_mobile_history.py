"""GET /mobile/history (3.2.0): the transcript window (history.window), the route, the per-chat kinds cache behind
/mobile/kinds and /mobile/history, and the reply index's 2 s sessions re-read window."""
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mobile_support import MobileCase, mobile, replies  # noqa: E402

history = mobile("history")
REAL_LOAD = history.load


def turns(count, first=1):
    """``count`` turns of user, assistant commentary, a tool row (no row id) and the reply; row ids from ``first``."""
    messages, rid = [], first
    for turn in range(count):
        messages += [{"role": "user", "text": f"Q{turn}", "row_id": rid, "timestamp": rid},
                     {"role": "assistant", "text": f"Looking {turn}", "row_id": rid + 1, "timestamp": rid + 1},
                     {"role": "tool", "name": "read_file", "context": "notes.md", "tool_call_id": "c", "timestamp": rid + 1},
                     {"role": "assistant", "text": f"A{turn}", "row_id": rid + 2, "timestamp": rid + 2}]
        rid += 3
    return messages


def ids(answer):
    return [m.get("row_id") for m in answer["messages"]]


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.messages = turns(10)  # 40 messages, row ids 1..30; turn n is rows 3n+1..3n+3

    def test_tail_extends_back_to_the_turn_start(self):
        answer = history.window(self.messages, tail=3)
        self.assertEqual(ids(answer), [28, 29, None, 30])
        self.assertEqual((answer["count"], answer["has_more"], answer["reset"]), (40, True, False))
        self.assertEqual((answer["first_row"], answer["last_row"]), (28, 30))
        self.assertEqual(ids(history.window(self.messages, tail=1)), [28, 29, None, 30])

    def test_tail_covering_the_whole_chat_has_nothing_more(self):
        answer = history.window(self.messages, tail=200)
        self.assertEqual(len(answer["messages"]), 40)
        self.assertFalse(answer["has_more"])
        self.assertEqual(history.window([], tail=30), {"messages": [], "count": 0, "has_more": False, "reset": False,
                                                        "first_row": None, "last_row": None})

    def test_tail_is_clamped(self):
        self.assertEqual(ids(history.window(self.messages, tail=0)), [28, 29, None, 30])
        self.assertEqual(len(history.window(self.messages, tail=999)["messages"]), 40)

    def test_since_adds_everything_after_the_read_mark(self):
        answer = history.window(self.messages, tail=3, since=21)  # read through turn 6's reply
        self.assertEqual(answer["first_row"], 22)  # turn 7 opens the window: nothing to extend
        self.assertEqual(len(answer["messages"]), 12)  # turns 7 to 9
        self.assertTrue(answer["has_more"])
        inside = history.window(self.messages, tail=3, since=29)  # newer than the tail: the tail wins
        self.assertEqual(ids(inside), [28, 29, None, 30])

    def test_before_is_the_page_before_a_row(self):
        answer = history.window(self.messages, tail=3, before=13)  # Load earlier from turn 4's user row
        self.assertEqual(ids(answer), [10, 11, None, 12])
        self.assertTrue(answer["has_more"])
        top = history.window(self.messages, tail=3, before=1)
        self.assertEqual((top["messages"], top["has_more"], top["first_row"], top["last_row"]), ([], False, None, None))
        self.assertEqual(ids(history.window(self.messages, tail=5, before=4)), [1, 2, None, 3])

    def test_after_is_only_what_follows_a_row(self):
        answer = history.window(self.messages, tail=3, after=27)
        self.assertEqual(ids(answer), [28, 29, None, 30])
        self.assertTrue(answer["has_more"])
        self.assertFalse(answer["reset"])
        self.assertEqual(ids(history.window(self.messages, after=29)), [None, 30])  # the tool row after 29
        latest = history.window(self.messages, after=30)
        self.assertEqual((latest["messages"], latest["has_more"], latest["first_row"]), ([], True, None))

    def test_a_row_no_longer_in_the_chat_answers_the_tail_with_reset(self):
        for name in ("after", "before", "since"):
            with self.subTest(name):
                answer = history.window(self.messages, tail=3, **{name: 9999})
                self.assertEqual(ids(answer), [28, 29, None, 30])
                self.assertTrue(answer["reset"])

    def test_turn_start_is_sought_at_most_sixty_rows_back(self):
        long = [{"role": "user", "text": "go", "row_id": 1}]
        long += [{"role": "assistant", "text": f"step {i}", "row_id": 2 + i} for i in range(70)]  # rows 2..71
        self.assertEqual(history.window(long, tail=5)["first_row"], 67)  # the user row is 65 back: not reached
        self.assertEqual(history.window(long, tail=11)["first_row"], 1)  # 60 back: reached

    def test_long_text_is_shortened_on_a_character_boundary(self):
        big = "é" * 70000  # 140 000 bytes
        wide = "あ" * 30000  # 90 000 bytes, 65536 is not a multiple of 3
        messages = [{"role": "user", "text": big, "row_id": 1}, {"role": "assistant", "text": wide, "row_id": 2},
                    {"role": "assistant", "text": "short", "row_id": 3}]
        answer = history.window(messages)
        first, second, third = answer["messages"]
        self.assertTrue(first["text"].startswith("é" * 32768 + "\n\n… (message shortened: the full text is 136 KB)"))
        self.assertEqual((first["text_truncated"], first["text_length"]), (True, 70000))
        self.assertTrue(second["text"].startswith("あ" * 21845 + "\n\n…"))
        self.assertNotIn("text_truncated", third)
        self.assertEqual(len(messages[0]["text"]), 70000, "the source list is never changed")
        huge = history.window([{"role": "user", "text": "x" * 1_200_000, "row_id": 1}])["messages"][0]
        self.assertIn("the full text is 1.1 MB", huge["text"])


def fake_load(profile, path, session):
    """What Hermes's builder would answer for the test schema: user and assistant rows of the lineage, in order."""
    with sqlite3.connect(path) as c:
        lineage = sorted(replies.lineage(c, session))
        rows = c.execute("SELECT id, role, content FROM messages WHERE session_id IN (%s) ORDER BY id"
                         % ",".join("?" * len(lineage)), lineage).fetchall()
    return [{"role": role, "text": text, "row_id": rid, "timestamp": rid} for rid, role, text in rows]


class Recording:
    """A sqlite3 connection that logs every statement with its parameters, so a test sees which rows a call asked for."""

    def __init__(self, con, log):
        object.__setattr__(self, "_con", con)
        object.__setattr__(self, "_log", log)

    def execute(self, sql, params=()):
        self._log.append((" ".join(sql.split()), tuple(params)))
        return self._con.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._con, name)

    def __setattr__(self, name, value):
        setattr(self._con, name, value)

    def __enter__(self):
        return self._con.__enter__()

    def __exit__(self, *args):
        return self._con.__exit__(*args)


class RecordingCase(MobileCase):
    def recording(self):
        """Patch sqlite3.connect for the test; returns the log of (statement, parameters)."""
        log, connect = [], sqlite3.connect
        patcher = patch.object(sqlite3, "connect", side_effect=lambda *a, **k: Recording(connect(*a, **k), log))
        patcher.start()
        self.addCleanup(patcher.stop)
        return log

    @staticmethod
    def reads(log, table):
        return [params for sql, params in log if sql.startswith("SELECT") and f"FROM {table}" in sql]


class HistoryRouteTests(MobileCase):
    def setUp(self):
        super().setUp()
        patcher = patch.object(history, "load", side_effect=fake_load)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_answers_a_window_with_the_chats_kinds(self):
        self.reply(2)
        self.reply(4)
        self.script("""INSERT INTO messages VALUES(6,'chat','user','[Cronjob "x" output]',NULL,NULL,NULL,1);
                       INSERT INTO messages VALUES(7,'chat','assistant','Ran fine.',NULL,'stop',NULL,1);""")
        response = self.call("GET", "/history", params={"profile": "jarvis", "session": "chat", "tail": 2})
        self.assertEqual(response.status_code, 200)
        answer = response.json()
        self.assertEqual(sorted(answer), ["count", "first_row", "has_more", "kinds", "kinds_version", "last_row",
                                          "messages", "reset"])
        self.assertEqual([m["row_id"] for m in answer["messages"]], [6, 7])
        self.assertEqual(answer["messages"][0], {"role": "user", "text": '[Cronjob "x" output]', "row_id": 6, "timestamp": 6})
        self.assertEqual((answer["count"], answer["has_more"], answer["reset"]), (7, True, False))
        self.assertEqual((answer["first_row"], answer["last_row"]), (6, 7))
        self.assertEqual(answer["kinds"], {"6": "cron", "7": "summary"})
        self.assertEqual(answer["kinds_version"], replies.KINDS_VERSION)
        newer = self.call("GET", "/history", params={"profile": "jarvis", "session": "chat", "after": 5}).json()
        self.assertEqual([m["row_id"] for m in newer["messages"]], [6, 7])
        gone = self.call("GET", "/history", params={"profile": "jarvis", "session": "chat", "after": 99}).json()
        self.assertTrue(gone["reset"])

    def test_unknown_profile_or_session_is_404_and_bad_ranges_422(self):
        self.assertEqual(self.call("GET", "/history", params={"profile": "nobody", "session": "chat"}).status_code, 404)
        self.assertEqual(self.call("GET", "/history", params={"profile": "jarvis", "session": "ghost"}).status_code, 404)
        for bad in ({"tail": 0}, {"tail": 201}, {"since": -1}, {"after": "x"}):
            self.assertEqual(self.call("GET", "/history", params={"profile": "jarvis", "session": "chat", **bad}).status_code,
                             422, bad)

    def test_a_hermes_without_the_builder_is_501(self):
        with patch.object(history, "load", side_effect=history.HistoryUnavailable("no tui_gateway.server")):
            response = self.call("GET", "/history", params={"profile": "jarvis", "session": "chat"})
        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.json(), {"error": "history_unavailable"})

    def test_a_busy_database_is_503(self):
        with patch.object(history, "load", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(self.call("GET", "/history", params={"profile": "jarvis", "session": "chat"}).status_code, 503)

    def test_the_real_loader_needs_hermes(self):
        with patch.object(history, "load", side_effect=REAL_LOAD):
            response = self.call("GET", "/history", params={"profile": "jarvis", "session": "chat"})
        self.assertEqual(response.status_code, 501, "no Hermes on this Mac: the builder can't be imported")


class CompactedHistoryTests(unittest.TestCase):
    """Hermes compacts a long chat in place; the loader asks for the archived rows too (Friday's Bot Chat, 2026-10-29)."""

    def load_with(self, accepts_compacted):
        import types
        calls = []

        class SessionDB:
            def __init__(self, db_path, read_only):
                pass

            def get_messages_as_conversation(self, session, include_ancestors=False, include_row_ids=False, **extra):
                if extra and not accepts_compacted:
                    raise TypeError("unexpected keyword argument 'include_compacted'")
                calls.append(extra)
                return [{"role": "user", "content": "before the compaction", "_row_id": 1}]

            def close(self):
                pass

        server = types.ModuleType("tui_gateway.server")
        server._history_to_messages = lambda rows, profile_home: [{"role": r["role"], "text": r["content"]} for r in rows]
        package = types.ModuleType("tui_gateway")
        package.server = server
        fakes = {"hermes_state": types.SimpleNamespace(SessionDB=SessionDB), "tui_gateway": package,
                 "tui_gateway.server": server}
        with patch.dict(sys.modules, fakes):
            messages = REAL_LOAD("jarvis", "/tmp/state.db", "chat")
        return messages, calls

    def test_the_loader_reads_compacted_rows(self):
        messages, calls = self.load_with(accepts_compacted=True)
        self.assertEqual(calls, [{"include_compacted": True}])
        self.assertEqual(messages, [{"role": "user", "text": "before the compaction"}])

    def test_an_older_hermes_without_display_history_still_loads(self):
        messages, calls = self.load_with(accepts_compacted=False)
        self.assertEqual(calls, [{}])
        self.assertEqual(len(messages), 1)


class KindsCacheTests(RecordingCase):
    ROWS = [(10, "user", "[System: model switched]", None, None), (11, "user", "Hello?", None, None),
            (12, "assistant", "[SILENT]", None, None), (13, "user", "x", "process_complete", None),
            (14, "assistant", "Done.", None, None), (15, "user", "Message from \U0001F916 Jarvis (@jarvis): hi", None, None),
            (16, "assistant", "Checking…", None, '[{"id":"1"}]'), (17, "assistant", "Sent.", None, None),
            (18, "user", '[Cronjob "Friday: clean" output — scheduled job, not the user.]', None, None),
            (19, "assistant", "Inbox cleaned.", None, None), (20, "user", "Thanks", None, None), (21, "assistant", "Any time.", None, None)]
    LABELS = {"10": "notes", "12": "silent", "13": "background", "14": "background", "15": "bots", "16": "bots",
              "17": "bots", "18": "cron", "19": "summary"}

    def setUp(self):
        super().setUp()
        with sqlite3.connect(self.source) as c:
            c.execute("INSERT INTO sessions VALUES('root','desktop',0,0,'Plan',NULL)")
            c.execute("INSERT INTO sessions VALUES('tip','desktop',0,0,'Plan','root')")
            for i, (rid, role, text, kind, tools) in enumerate(self.ROWS):
                c.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,1)", (rid, "root" if i < 6 else "tip", role, text, tools, "stop", kind))

    def kinds(self, session="tip"):
        response = self.call("GET", "/kinds", params={"profile": "jarvis", "session": session})
        self.assertEqual(response.status_code, 200)
        return response.json()["kinds"]

    def test_rows_are_labelled_once_and_the_state_carries_forward(self):
        log = self.recording()
        self.assertEqual(self.kinds(), self.LABELS)
        self.assertEqual(self.reads(log, "messages")[-1][-1], 0, "the first call reads every row")
        del log[:]
        self.assertEqual(self.kinds(), self.LABELS)
        self.assertEqual(self.reads(log, "messages"), [(("root", "tip", 21))], "only rows newer than the last labelled")
        self.sql("INSERT INTO messages VALUES(22,'tip','user','[Cronjob \"x\" output]',NULL,NULL,NULL,1)")
        self.assertEqual(self.kinds()["22"], "cron")
        self.sql("INSERT INTO messages VALUES(23,'tip','assistant','Ran.',NULL,'stop',NULL,1)")
        del log[:]
        self.assertEqual(self.kinds()["23"], "summary", "the answer to a prompt labelled by an earlier call")
        self.assertEqual(self.reads(log, "messages"), [("root", "tip", 22)])

    def test_a_lineage_that_gained_a_continuation_starts_over(self):
        self.kinds()
        log = self.recording()
        self.session("tip-2", parent="tip")
        self.sql("INSERT INTO messages VALUES(30,'tip-2','assistant','Any time.',NULL,'stop',NULL,1)")
        self.assertEqual(self.kinds(), self.LABELS)  # row 30 answers the person: no kind
        self.assertEqual(self.reads(log, "messages"), [("root", "tip", "tip-2", 0)], "rebuilt from the first row")

    def test_the_cache_is_bounded(self):
        with patch.object(replies, "KINDS_CACHE_SIZE", 2):
            for session in ("chat", "root", "tip"):
                self.kinds(session)
            self.assertEqual(list(replies._kinds), [("jarvis", "root"), ("jarvis", "tip")])
            self.kinds("root")  # used again: stays
            self.kinds("chat")
            self.assertEqual(list(replies._kinds), [("jarvis", "root"), ("jarvis", "chat")])


class IndexSessionsWindowTests(RecordingCase):
    def test_sessions_are_re_read_at_most_every_two_seconds(self):
        clock = [100.0]
        index = replies.Index(self.source, clock=lambda: clock[0])
        with patch.object(replies, "SESSIONS_TTL", 2.0):
            log = self.recording()
            index.update()
            index.update()
            self.assertEqual(len(self.reads(log, "sessions")), 1)
            self.assertEqual(len(self.reads(log, "messages")), 2, "new rows are read on every call")
            self.sql("UPDATE sessions SET archived=1")
            clock[0] += 1.9
            index.update()
            self.assertEqual(index.group("chat"), "chats", "a change inside the window waits")
            clock[0] += 0.1
            index.update()
            self.assertEqual(len(self.reads(log, "sessions")), 2)
            self.assertEqual(index.group("chat"), "archived")

    def test_a_new_chats_first_rows_read_sessions_at_once(self):
        clock = [100.0]
        index = replies.Index(self.source, clock=lambda: clock[0])
        with patch.object(replies, "SESSIONS_TTL", 2.0):
            index.update()
            log = self.recording()
            self.session("fresh", source="cron")
            self.reply(2, chat="fresh")
            index.update()
            self.assertEqual(len(self.reads(log, "sessions")), 1)
            self.assertEqual(index.group("fresh"), "scheduled")


if __name__ == "__main__":
    unittest.main()
