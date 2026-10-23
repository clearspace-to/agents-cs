"""The 2026-10-08 simplification: one call per intent (Quick Add, a comment with its coordinator), the
rules the plugin now owns for the app (allowed moves, stored containment), and what rings the phone.
Hermes is never imported; ``FakeBoard`` stands in for the adapter and a recorder for the mobile alerts."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tasks import FakeBoard, family_board, plugin, selection, store_module, tasks, NOW  # noqa: E402

notify = sys.modules[plugin.__name__ + ".notify"]
coordinator = sys.modules[plugin.__name__ + ".coordinator"]
creation = sys.modules[plugin.__name__ + ".creation"]


class Recorder:
    def __init__(self):
        self.posts = []

    def __call__(self, path, body):
        self.posts.append((path, body))

    def events(self, kind=None):
        return [b for p, b in self.posts if p == "task" and (kind is None or b["event"] == kind)]

    def attention(self):
        return [b for p, b in self.posts if p == "attention"]


class Recorded(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log = store_module.DecisionLog(Path(self.temp.name) / "forms.db")
        self.recorder = Recorder()
        notify.transport = self.recorder
        notify.reset()

    def tearDown(self):
        notify.transport = None
        self.temp.cleanup()


class RulesTests(unittest.TestCase):
    def test_allowed_moves_are_the_table_the_app_used_to_carry(self):
        self.assertEqual(tasks.allowed_moves("ready"), ["triage", "todo", "scheduled", "blocked", "review", "done"])
        self.assertEqual(tasks.allowed_moves("review"), ["triage", "todo", "ready", "done"])
        self.assertEqual(tasks.allowed_moves("archived"), [])
        self.assertEqual(tasks.allowed_moves(None), [])
        for status, targets in tasks.ALLOWED_MOVES.items():
            self.assertNotIn("running", targets, status)
            self.assertNotIn(status, targets, status)

    def test_quick_title_cuts_at_a_word(self):
        self.assertEqual(tasks.quick_title("Short one\nmore"), "Short one")
        self.assertEqual(tasks.quick_title("\n\n  Second line first  \nx"), "Second line first")
        title = tasks.quick_title("word " * 30)
        self.assertLessEqual(len(title), 80)
        self.assertTrue(title.endswith("word…"))

    def test_slug_rules(self):
        self.assertIsNone(tasks.normalize_slug(None))
        self.assertIsNone(tasks.normalize_slug("  "))
        self.assertEqual(tasks.normalize_slug(" My-Board "), "my-board")
        with self.assertRaises(tasks.BoardError) as refused:
            tasks.normalize_slug("no spaces")
        self.assertEqual(refused.exception.status, 400)

    def test_stored_containment_outranks_and_fills_in_for_events(self):
        parent_of, children_of = tasks.containment({}, [("r1", "c1"), ("r1", "c2")])
        self.assertEqual((parent_of, children_of), ({"c1": "r1", "c2": "r1"}, {"r1": ["c1", "c2"]}))
        events = {"r9": [{"kind": "decomposed", "payload": {"child_ids": ["c3"]}}]}
        _parents, merged = tasks.containment(events, [("r1", "c1")])
        self.assertEqual(merged, {"r1": ["c1"], "r9": ["c3"]})


class BoardPayloadTests(Recorded):
    def test_board_payload_marks_preparing_and_remembers_families(self):
        board = family_board()
        self.log.quick_add_begin("k1", board="default", task_id="t_free", state="writing")
        shown = tasks.board_payload(board, self.log)
        by_id = {t["id"]: t for t in shown["tasks"]}
        self.assertTrue(by_id["t_free"]["preparing"])
        self.assertFalse(by_id["r1"]["preparing"])
        self.assertEqual(self.log.containment_pairs("default"), [("r1", "c1"), ("r1", "c2"), ("r1", "c3")], "derived once, kept")
        # With the events gone (an older Hermes GC'd them) the family still reads from what was stored.
        bare = family_board()
        bare._events = {}
        again = tasks.board_payload(bare, self.log)
        self.assertEqual({t["id"]: t["parent_id"] for t in again["tasks"]}["c1"], "r1")
        self.assertEqual(self.recorder.posts, [], "reading the board posts nothing by itself")

    def test_attention_summary_counts_roots_and_chat_decisions(self):
        board = family_board()
        chat_card = selection("Chat question")
        self.log.record(chat_card, profile="friday", session_id="chat-1")
        payload = tasks.board_payload(board, self.log)
        summary = notify.attention_summary(payload, self.log.open_chat_decisions())
        self.assertEqual([row["id"] for row in summary["tasks"]], ["r1"], "subtasks count through their root")
        self.assertEqual(summary["tasks"][0]["kind"], "review")
        self.assertEqual((summary["decisions"], summary["count"]), (1, 2))
        self.assertTrue(notify.attention(summary, now=1000.0))
        self.assertFalse(notify.attention(summary, now=1005.0), "an unchanged summary is not repeated within 20 s")
        self.assertTrue(notify.attention(summary, now=1030.0))
        self.assertTrue(notify.attention(summary, now=1031.0, force=True))

    def test_attention_after_clears_a_task_that_stopped_needing_the_person(self):
        board = family_board()
        board._tasks["c1"]["status"] = "done"
        board._tasks["c2"]["status"] = "ready"
        summary = tasks.attention_after(board, self.log, "r1")
        self.assertEqual(summary["tasks"], [])
        self.assertEqual([e["task_id"] for e in self.recorder.events("resolved")], ["r1"])
        self.assertEqual(len(self.recorder.attention()), 1)


class QuickAddTests(Recorded):
    def setUp(self):
        super().setUp()
        self.board = FakeBoard([])
        self.specified = []
        self.decomposed = []
        self.jobs = []
        self.creation_log = creation.CreationLog(Path(self.temp.name) / "forms.db")

    def specify(self, task_id, slug):
        self.specified.append((task_id, slug))
        row = self.board._tasks[task_id]
        row["title"], row["body"], row["status"] = "Written up: " + row["title"], "**Goal** — " + (row["body"] or row["title"]), "todo"
        return {"ok": True, "task_id": task_id, "new_title": row["title"]}

    def decompose(self, task_id, slug):
        self.decomposed.append((task_id, slug))
        for child in ("a", "b"):
            self.board._tasks[child] = FakeBoard.task_row(child, "ready", child.upper())
        self.board._tasks[task_id]["status"] = "todo"
        return {"ok": True, "fanout": True, "child_ids": ["a", "b"]}

    def add(self, body, run_background=True):
        status, out = tasks.quick_add(self.board, self.log, {"author": "mark", "client_key": "k1", **body},
                                      specify=self.specify, decompose=self.decompose, creation_log=self.creation_log,
                                      background=self.jobs.append, board_factory=lambda _ref: self.board)
        if run_background:
            for job in self.jobs:
                job()
            self.jobs.clear()
        return status, out

    def test_write_up_then_start_in_one_call(self):
        status, out = self.add({"text": "  get three chair quotes\nHerman Miller too ", "assignee": "friday", "priority": 1}, run_background=False)
        self.assertEqual(status, 200)
        task = self.board.task(out["task_id"])
        self.assertEqual((task["status"], task["title"], task["body"], task["assignee"], task["priority"]),
                         ("triage", "get three chair quotes", "get three chair quotes\nHerman Miller too", "friday", 1))
        self.assertTrue(out["preparing"])
        self.assertIn(out["task_id"], self.log.preparing_tasks("default"))
        self.assertEqual(len(self.jobs), 1, "the write-up runs off the request")
        self.jobs.pop()()
        task = self.board.task(out["task_id"])
        self.assertEqual((task["status"], task["title"]), ("ready", "Written up: get three chair quotes"))
        self.assertEqual(self.specified, [(out["task_id"], "default")])
        self.assertEqual(self.board.dispatches, 1, "the dispatcher is nudged once it is Ready")
        self.assertEqual(self.log.preparing_tasks("default"), set())
        self.assertEqual(self.log.quick_add("k1")["state"], "done")
        # The same receipt returns the same task instead of a second one.
        again_status, again = self.add({"text": "get three chair quotes"})
        self.assertEqual((again_status, again["task_id"], again.get("replayed")), (200, out["task_id"], True))
        self.assertEqual(len(self.board.created), 1)
        self.assertEqual(len(self.recorder.attention()), 1, "the finished write-up refreshes the badge")

    def test_held_stays_in_triage_and_as_typed_starts_at_once(self):
        status, held = self.add({"text": "Call the landlord", "write_up": False, "start": False})
        self.assertEqual((status, held["status"], held["preparing"]), (200, "triage", False))
        self.assertEqual(self.specified, [])
        self.log = store_module.DecisionLog(Path(self.temp.name) / "forms2.db")
        status, typed = tasks.quick_add(self.board, self.log, {"text": "Reconcile receipts", "write_up": False, "start": True, "client_key": "k2"},
                                        specify=self.specify, decompose=self.decompose, creation_log=self.creation_log,
                                        background=self.jobs.append)
        self.assertEqual((status, typed["status"], typed["preparing"]), (200, "ready", False))
        self.assertEqual(self.board.dispatches, 1)
        self.assertEqual(self.jobs, [])

    def test_a_failed_write_up_keeps_the_words_and_still_starts(self):
        def failing(task_id, slug):
            raise TimeoutError("model timed out")
        status, out = tasks.quick_add(self.board, self.log, {"text": "Plan the trip", "client_key": "k3"},
                                      specify=failing, decompose=self.decompose, creation_log=self.creation_log,
                                      background=self.jobs.append, board_factory=lambda _ref: self.board)
        self.jobs[0]()
        task = self.board.task(out["task_id"])
        self.assertEqual((task["status"], task["title"]), ("ready", "Plan the trip"))
        self.assertEqual(self.log.quick_add("k3")["state"], "failed")
        self.assertIn("model timed out", self.log.quick_add("k3")["detail"])
        self.assertIn(("release_triage", out["task_id"], "app"), self.board.calls)

    def test_creation_decomposition_plans_once_and_never_promotes_the_root(self):
        self.creation_log.set_enabled(True)
        with patch.object(tasks, "_background_enabled", return_value=False):
            status, out = self.add({"text": "Plan the team night out", "assignee": "friday"})
        root = self.board.task(out["task_id"])
        self.assertEqual((status, out["preparing"], root["status"]), (200, True, "todo"))
        self.assertEqual(self.decomposed, [(out["task_id"], "default")])
        self.assertEqual(self.specified, [])
        self.assertEqual(self.log.containment_pairs("default"), [(out["task_id"], "a"), (out["task_id"], "b")])
        self.assertNotIn(("promote", out["task_id"]), self.board.calls)
        self.assertEqual(self.board.dispatches, 0)

    def test_text_is_required(self):
        self.assertEqual(self.add({"text": "   "})[0], 400)


class CommentReceiptTests(Recorded):
    def test_a_repeated_send_is_answered_from_the_receipt(self):
        board = family_board()
        body = {"text": "Hold at 5%", "author": "mark", "client_key": "c-1"}
        status, first = tasks.comment_on_task(board, self.log, "t_free", body)
        status2, second = tasks.comment_on_task(board, self.log, "t_free", body)
        self.assertEqual((status, status2), (200, 200))
        self.assertEqual(second, {**first, "replayed": True})
        self.assertEqual(len(board._comments["t_free"]), 1, "stored once")
        self.assertEqual(board.dispatches, 1, "promoted to Ready: the dispatcher is nudged")
        self.assertEqual([e["task_id"] for e in self.recorder.events("resolved")], ["t_free"])

    def test_a_root_comment_opens_the_coordinator_here(self):
        board = family_board()
        socket = FakeSocket()
        with patch.object(coordinator, "connect", lambda *a, **k: socket), patch.object(coordinator, "ws_url", return_value="ws://x"), \
             patch.object(coordinator, "orchestrator_profile", return_value="default"):
            status, out = tasks.comment_on_task(board, self.log, "r1", {"text": "Make it Friday.", "author": "mark"})
        self.assertEqual((status, out["route"], out["follow_up"]), (200, "coordinate", "coordinate"))
        self.assertEqual(out["coordination"]["session_id"], "rt-1")
        self.assertTrue(out["coordination"]["submitted"])
        methods = [f["method"] for f in socket.sent]
        self.assertEqual(methods, ["client.capabilities", "session.create", "prompt.submit"])
        created = socket.sent[1]["params"]
        self.assertEqual((created["profile"], created["hidden"], created["title"]), ("default", True, "Coordinating: Plan the night out"))
        self.assertIn("Make it Friday.", socket.sent[2]["params"]["text"])
        self.assertEqual(self.log.coordination_for_session("rt-1")["root_id"], "r1")
        self.assertEqual(self.log.coordination_for_session("stored-1")["root_id"], "r1", "both ids are fenced")
        socket.finished.wait(2)
        self.assertTrue(socket.closed, "the socket is held until the turn ends, then closed")

    def test_an_older_app_still_gets_the_instruction_to_submit_itself(self):
        board = family_board()
        status, out = tasks.comment_on_task(board, self.log, "r1", {"text": "Go", "author": "mark", "coordinator_session_id": "app-1"})
        self.assertEqual((status, out["follow_up"], out["coordination"]["session_id"], out["coordination"]["submitted"]), (200, "coordinate", "app-1", False))


class FakeSocket:
    """Hermes's /api/ws as the coordinator sees it: answers each call, then ends the turn."""

    def __init__(self):
        self.sent = []
        self.closed = False
        self.finished = threading.Event()
        self._queue = []

    def send(self, text):
        frame = json.loads(text)
        self.sent.append(frame)
        if frame["method"] == "session.create":
            self._queue.append({"jsonrpc": "2.0", "id": frame["id"], "result": {"session_id": "rt-1", "stored_session_id": "stored-1"}})
        elif frame["method"] == "prompt.submit":
            self._queue.append({"jsonrpc": "2.0", "id": frame["id"], "result": {"ok": True}})
            self._queue.append({"jsonrpc": "2.0", "method": "message.complete", "params": {"session_id": "rt-1"}})
        else:
            self._queue.append({"jsonrpc": "2.0", "id": frame["id"], "result": {}})

    def recv(self, timeout=None):
        if not self._queue:
            raise TimeoutError()
        # As Hermes sends them: one frame per message, no trailing newline (tui_gateway.transport).
        return json.dumps(self._queue.pop(0))

    def close(self):
        self.closed = True
        self.finished.set()


class CoordinatorTests(unittest.TestCase):
    def test_frames_without_a_newline_and_batched_frames_both_parse(self):
        class Batched(FakeSocket):
            def recv(self, timeout=None):
                if not self._queue:
                    raise TimeoutError()
                frames, self._queue = self._queue, []
                return "\n".join(json.dumps(f) for f in frames)
        rpc = coordinator.Rpc(FakeSocket())
        self.assertEqual(rpc.call("session.create", {}), {"session_id": "rt-1", "stored_session_id": "stored-1"})
        rpc = coordinator.Rpc(Batched())
        self.assertEqual(rpc.call("client.capabilities", {}), {})

    def test_a_silent_hermes_is_a_coordination_error_not_a_crash(self):
        class Silent(FakeSocket):
            def send(self, text):
                self.sent.append(json.loads(text))
        with self.assertRaises(coordinator.CoordinationError):
            coordinator.Rpc(Silent()).call("client.capabilities", {}, timeout=0.05)

    def test_rpc_errors_and_event_buffering(self):
        class Erroring(FakeSocket):
            def send(self, text):
                frame = json.loads(text)
                self.sent.append(frame)
                self._queue.append({"jsonrpc": "2.0", "method": "session.event", "params": {}})
                self._queue.append({"jsonrpc": "2.0", "id": frame["id"], "error": {"code": 1, "message": "no such profile"}})
        rpc = coordinator.Rpc(Erroring())
        with self.assertRaises(coordinator.CoordinationError) as refused:
            rpc.call("session.create", {})
        self.assertIn("no such profile", str(refused.exception))
        self.assertEqual(len(rpc.events), 1, "events ahead of the answer are kept, not lost")

    def test_the_end_of_the_coordinator_turn_runs_the_check(self):
        socket, ended = FakeSocket(), threading.Event()
        with patch.object(coordinator, "connect", lambda *a, **k: socket), patch.object(coordinator, "ws_url", return_value="ws://x"):
            coordinator.start(title="t", instruction="i", profile="default", on_end=ended.set)
        self.assertTrue(ended.wait(2), "the check runs once the turn is over")
        self.assertTrue(socket.closed)

    def test_start_closes_the_socket_when_hermes_refuses(self):
        class NoSession(FakeSocket):
            def send(self, text):
                frame = json.loads(text)
                self.sent.append(frame)
                self._queue.append({"jsonrpc": "2.0", "id": frame["id"], "result": {}})
        socket = NoSession()
        with patch.object(coordinator, "connect", lambda *a, **k: socket), patch.object(coordinator, "ws_url", return_value="ws://x"):
            with self.assertRaises(coordinator.CoordinationError):
                coordinator.start(title="t", instruction="i", profile="default")
        self.assertTrue(socket.closed)


class ObserverTests(Recorded):
    """The hooks Hermes fires in the worker and the dispatcher, through the plugin's registered callbacks."""

    def setUp(self):
        super().setUp()
        self.board = family_board()
        plugin._DECISIONS = self.log

    def tearDown(self):
        plugin._DECISIONS = None
        super().tearDown()

    def run_hook(self, callback, **kwargs):
        threads_before = set(threading.enumerate())
        with patch.object(tasks, "HermesBoard", return_value=self.board):
            callback(**kwargs)
            for thread in set(threading.enumerate()) - threads_before:
                thread.join(5)

    def test_a_human_block_rings_the_phone_as_blocked_even_with_an_old_card(self):
        # Task-bound cards were removed (2026-10-23): a card bound before then never makes the alert a "decision".
        card = selection()
        self.log.record(card, profile="friday", session_id="w1")
        self.log.bind_task(card, profile="friday", board="default", task_id="c2", root_id="r1")
        self.run_hook(plugin.on_kanban_task_blocked, task_id="c2", board="default", assignee="friday", reason="Choose a date", run_id=8, profile_name="friday")
        requested = self.recorder.events("requested")
        self.assertEqual(len(requested), 1)
        self.assertEqual((requested[0]["task_id"], requested[0]["kind"], requested[0]["bot"], requested[0]["title"], requested[0]["reason"]),
                         ("c2", "blocked", "friday", "Choose a date", "Choose a date"))
        self.assertEqual(len(self.recorder.attention()), 1)

    def test_a_transient_block_only_refreshes_the_badge(self):
        self.board._tasks["c2"]["block_kind"] = "transient"
        self.run_hook(plugin.on_kanban_task_blocked, task_id="c2", board="default", assignee="friday", reason="flaky", run_id=8)
        self.assertEqual(self.recorder.events("requested"), [])
        self.assertEqual(len(self.recorder.attention()), 1)

    def test_a_review_request_rings_and_completion_clears(self):
        self.run_hook(plugin.on_post_tool_call, tool_name="kanban_request_review", args={"task_id": "c1", "summary": "Two venues."}, result="{}", session_id="w1", tool_call_id="t1")
        self.assertEqual([(e["task_id"], e["kind"]) for e in self.recorder.events("requested")], [("c1", "review")])
        self.board._tasks["c1"]["status"] = "done"
        self.board._tasks["c2"]["status"] = "done"
        self.run_hook(plugin.on_kanban_task_completed, task_id="c1", board="default")
        self.assertEqual([e["task_id"] for e in self.recorder.events("resolved")], ["c1"])

    def test_the_dispatch_tick_posts_the_summary_without_ringing(self):
        self.run_hook(plugin.on_kanban_dispatch_tick, board="default", outcome="ok")
        self.assertEqual(self.recorder.events(), [])
        self.assertEqual(len(self.recorder.attention()), 1)
        self.run_hook(plugin.on_kanban_dispatch_tick, board="default", outcome="ok")
        self.assertEqual(len(self.recorder.attention()), 1, "an unchanged tick is quiet")


if __name__ == "__main__":
    unittest.main()
