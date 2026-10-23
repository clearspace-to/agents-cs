"""Tests for the task experience (tasks.py, the task half of store.py, the pre_tool_call guard). Run
from the repo root:

    python3 -m unittest discover -s plugin/agents-cs/tests

Hermes is never imported: ``FakeBoard`` stands in for ``tasks.HermesBoard`` with the same methods over
dicts in memory. The route test needs FastAPI and is skipped without it.
"""
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cards import EXAMPLES, FakeContext, load_plugin  # noqa: E402

plugin = load_plugin()
cards = sys.modules[plugin.__name__ + ".cards"]
store_module = sys.modules[plugin.__name__ + ".store"]
tasks = sys.modules[plugin.__name__ + ".tasks"]
notify = sys.modules[plugin.__name__ + ".notify"]

NOW = 1_790_000_000


def selection(title="Choose a date", options=("thu", "fri")):
    return cards.render("selection", {"title": title, "mode": "single", "description": "Both venues are free then.",
                                      "options": [{"id": o, "label": o.title()} for o in options]})


class FakeBoard:
    """The adapter surface tasks.py needs, over dicts. ``promote`` follows Hermes (todo/blocked only)
    unless ``lenient`` is set, which lets the done -> reopened row be exercised."""

    def __init__(self, tasks_=None, events=None, links=None, slug="default", lenient=False):
        self.slug = slug
        self.requested = None
        self.lenient = lenient
        self._tasks = {t["id"]: t for t in (tasks_ or [])}
        self._events = {k: list(v) for k, v in (events or {}).items()}
        self._links = list(links or [])
        self._comments = {}
        self.calls = []
        self.closed = False
        self.dispatches = 0
        self.created = []

    def dispatch(self):
        self.dispatches += 1

    def create(self, *, title, body, assignee, priority, triage, idempotency_key, created_by):
        for row in self._tasks.values():
            if idempotency_key and row.get("idempotency_key") == idempotency_key and row["status"] != "archived":
                return row["id"]
        tid = "n%d" % (len(self.created) + 1)
        self._tasks[tid] = self.task_row(tid, "triage" if triage else "ready", title, body=body, assignee=assignee,
                                         priority=priority, created_by=created_by, idempotency_key=idempotency_key,
                                         created_at=NOW)
        self._events.setdefault(tid, []).append({"id": 50 + len(self.created), "task_id": tid, "kind": "created", "payload": None, "created_at": NOW})
        self.created.append((title, body, assignee, priority, triage, idempotency_key, created_by))
        return tid

    @staticmethod
    def task_row(tid, status, title=None, **extra):
        row = {"id": tid, "title": title or tid.replace("_", " ").title(), "body": None, "assignee": None,
               "status": status, "priority": 0, "created_by": "mark", "created_at": NOW - 3600, "started_at": None,
               "completed_at": None, "result": None, "latest_summary": None, "block_kind": None}
        row.update(extra)
        return row

    def close(self):
        self.closed = True

    def tasks(self, include_archived=False):
        return [dict(t) for t in self._tasks.values() if include_archived or t["status"] != "archived"]

    def task(self, task_id):
        t = self._tasks.get(task_id)
        return dict(t) if t else None

    def events(self, task_ids=None):
        return {k: list(v) for k, v in self._events.items() if task_ids is None or k in task_ids}

    def links(self):
        return list(self._links)

    def unsatisfied_parents(self, task_id):
        return [(p, self._tasks[p]["status"]) for p, c in self._links
                if c == task_id and p in self._tasks and self._tasks[p]["status"] not in ("done", "archived")]

    def comment(self, task_id, comment_id):
        return next((c for c in self._comments.get(task_id, []) if c["id"] == comment_id), None)

    def latest_event_id(self):
        return max((e["id"] for rows in self._events.values() for e in rows), default=0)

    def add_comment(self, task_id, author, body):
        if task_id not in self._tasks:
            raise ValueError(f"unknown task {task_id}")
        rows = self._comments.setdefault(task_id, [])
        rows.append({"id": 100 + len(rows), "task_id": task_id, "author": author, "body": body, "created_at": NOW})
        self.calls.append(("add_comment", task_id, author))
        return rows[-1]["id"]

    def promote(self, task_id, actor, reason):
        self.calls.append(("promote", task_id))
        status = self._tasks[task_id]["status"]
        if status not in ("todo", "blocked") and not self.lenient:
            return False, f"task {task_id} is {status!r}; promote only applies to 'todo' or 'blocked'"
        if self.unsatisfied_parents(task_id):
            return False, "unsatisfied parent dependencies"
        self._tasks[task_id]["status"] = "ready"
        return True, None

    def unblock(self, task_id):
        self.calls.append(("unblock", task_id))
        if self._tasks[task_id]["status"] not in ("blocked", "scheduled"):
            return False
        self._tasks[task_id]["status"] = "ready"
        return True

    def reopen_review(self, task_id):
        self.calls.append(("reopen_review", task_id))
        if self._tasks[task_id]["status"] != "review":
            return False
        self._tasks[task_id]["status"] = "todo" if self.unsatisfied_parents(task_id) else "ready"  # Hermes re-gates
        return True

    def release_triage(self, task_id, author):
        self.calls.append(("release_triage", task_id, author))
        if self._tasks[task_id]["status"] != "triage":
            return False
        self._tasks[task_id]["status"] = "todo" if self.unsatisfied_parents(task_id) else "ready"
        self._events.setdefault(task_id, []).append({"id": 90, "kind": "specified", "payload": None})
        return True

    def reopen_done(self, task_id):
        self.calls.append(("reopen_done", task_id))
        if self._tasks[task_id]["status"] != "done":
            return False, "not done"
        if self.unsatisfied_parents(task_id):
            return False, "Cannot move to 'ready': blocked by parent(s) not done"
        self._tasks[task_id]["status"] = "ready"
        return True, None

    def hold(self, task_id):
        self.calls.append(("hold", task_id))
        if self._tasks[task_id]["status"] == "running":
            return False, "running"
        self._tasks[task_id]["status"] = "todo"
        return True, None

    def archive(self, task_id):
        self.calls.append(("archive", task_id))
        if self._tasks[task_id]["status"] == "archived":
            return False
        self._tasks[task_id]["status"] = "archived"
        return True

    def link(self, parent_id, child_id):
        if parent_id == child_id or (child_id, parent_id) in self._links:
            raise ValueError(f"linking {parent_id} -> {child_id} would create a cycle")
        self.calls.append(("link", parent_id, child_id))
        self._links.append((parent_id, child_id))
        if self._tasks[child_id]["status"] == "ready" and self._tasks[parent_id]["status"] not in ("done", "archived"):
            self._tasks[child_id]["status"] = "todo"

    def unlink(self, parent_id, child_id):
        if (parent_id, child_id) not in self._links:
            return False
        self.calls.append(("unlink", parent_id, child_id))
        self._links.remove((parent_id, child_id))
        if self._tasks[child_id]["status"] == "todo" and not self.unsatisfied_parents(child_id):
            self._tasks[child_id]["status"] = "ready"
        return True

    def wait_external(self, task_id, reason, run_id):
        row = self._tasks.get(task_id)
        if not row or row.get("current_run_id") != run_id or row["status"] != "running":
            return False
        row["status"] = "scheduled"
        self._events.setdefault(task_id, []).append({"id": 101, "kind": "scheduled", "payload": {
            "reason": tasks.EXTERNAL_WAIT_PREFIX + reason}})
        return True


def family_board(**overrides):
    """A decomposed root r1 with c1 (review), c2 (blocked needs_input) and c3 (todo, waits on c2); plus
    a free task t_free in todo."""
    T = FakeBoard.task_row
    rows = [T("r1", "todo", "Plan the night out"),
            T("c1", "review", "Shortlist two venues", started_at=NOW - 1800),
            T("c2", "blocked", "Choose a date", block_kind="needs_input"),
            T("c3", "todo", "Book the table"),
            T("t_free", "todo", "Unrelated")]
    events = {"r1": [{"id": 1, "task_id": "r1", "kind": "decomposed", "payload": {"child_ids": ["c1", "c2", "c3"]}, "created_at": NOW - 3000}],
              "c1": [{"id": 2, "task_id": "c1", "kind": "created", "payload": {"from_decompose_of": "r1"}, "created_at": NOW - 3000},
                     {"id": 5, "task_id": "c1", "kind": "review_requested",
                      "payload": {"summary": "Two venues shortlisted.", "reviewer": "friday", "artifacts": ["venues.md"]},
                      "created_at": NOW - 600}],
              "c2": [{"id": 3, "task_id": "c2", "kind": "created", "payload": {"from_decompose_of": "r1"}, "created_at": NOW - 3000},
                     {"id": 6, "task_id": "c2", "kind": "blocked", "payload": {"kind": "needs_input", "reason": "Choose a date"}, "created_at": NOW - 300}],
              "c3": [{"id": 4, "task_id": "c3", "kind": "created", "payload": {"from_decompose_of": "r1"}, "created_at": NOW - 3000}]}
    links = [("c2", "c3"), ("c1", "r1"), ("c2", "r1"), ("c3", "r1")]
    return FakeBoard(rows, events, links, **overrides)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "forms.db"
        self.log = store_module.DecisionLog(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def bound(self, card=None, task_id="c2", root_id="r1", board="default"):
        card = card or selection()
        self.log.record(card, profile="friday", session_id="s1", now=NOW)
        return self.log.bind_task(card, profile="friday", board=board, task_id=task_id, root_id=root_id, run_id=12)

    def test_migration_adds_missing_columns_once(self):
        with sqlite3.connect(self.db) as raw:
            raw.execute("""CREATE TABLE decisions(card_id TEXT PRIMARY KEY, profile TEXT NOT NULL, session_id TEXT NOT NULL,
                title TEXT NOT NULL, options INTEGER NOT NULL, state TEXT NOT NULL, created_at INTEGER NOT NULL, closed_at INTEGER)""")
            raw.execute("INSERT INTO decisions VALUES('a','friday','s1','Old',2,'open',1,NULL)")
        for _ in range(2):  # a second open finds every column present and adds nothing
            self.assertEqual(self.log.open(profile="friday")[0]["card_id"], "a")
        with sqlite3.connect(self.db) as raw:
            columns = {row[1] for row in raw.execute("PRAGMA table_info(decisions)")}
            tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"board", "task_id", "root_id", "run_id", "explanation", "card_json", "payload_hash",
                         "answered_by", "answer_json", "answer_comment_id", "answered_at"} <= columns)
        self.assertIn("coordinations", tables)

    def test_bind_lists_and_answers_an_action(self):
        card = selection()
        action = self.bound(card)
        self.assertEqual((action["kind"], action["task_id"], action["root_id"], action["run_id"], action["state"]),
                         ("question", "c2", "r1", 12, "open"))
        self.assertEqual(action["explanation"], "Both venues are free then.")
        self.assertEqual(action["card"], card)
        self.assertEqual(action["payload_hash"], store_module.payload_hash(card))
        listed = self.log.open_task_actions("default")
        self.assertEqual([a["action_id"] for a in listed], [card["id"]])
        self.assertEqual(self.log.open_task_actions("other"), [])
        self.assertEqual(self.log.open(profile="friday"), [], "a task-bound card is not a chat decision")
        state, answered = self.log.answer(card["id"], action["payload_hash"], {"selected": ["thu"]}, "mark", now=NOW)
        self.assertEqual((state, answered["state"], answered["answer"], answered["answered_by"], answered["answered_at"]),
                         ("answered", "answered", {"selected": ["thu"]}, "mark", NOW))
        self.assertEqual(self.log.open_task_actions("default"), [])
        self.log.set_answer_comment(card["id"], 124)
        self.assertEqual(self.log.action(card["id"])["answer_comment_id"], 124)

    def test_message_card_is_an_approval(self):
        card = cards.render("message", EXAMPLES["message"])
        self.assertEqual(self.bound(card)["kind"], "approval")

    def test_generic_task_message_preserves_destination_and_exact_approved_edits(self):
        card = cards.render("message", {"title": "Send on Upwork?", "channel": "upwork",
                                      "to": ["Alex (Upwork)"], "body": "Please refresh the proposal."})
        action = self.bound(card)
        self.assertEqual(action["kind"], "approval")
        answer = {"selected": ["send"], "edited": True, "body": "Can you please refresh the proposal?"}
        state, saved = self.log.answer(card["id"], action["payload_hash"], answer, "mark", now=NOW)
        self.assertEqual(state, "answered")
        self.assertEqual(saved["answer"], answer)
        self.assertEqual(saved["card"]["data"]["to"], ["Alex (Upwork)"])
        self.assertEqual(saved["card"]["data"]["channel"], "upwork")

    def test_answer_refusals(self):
        card = selection()
        action = self.bound(card)
        with self.assertRaises(store_module.StoreError) as stale:
            self.log.answer(card["id"], "0" * 64, {"selected": ["thu"]}, "mark")
        self.assertEqual(stale.exception.code, "stale_action")
        self.log.answer(card["id"], action["payload_hash"], {"selected": ["thu"]}, "mark")
        with self.assertRaises(store_module.StoreError) as again:
            self.log.answer(card["id"], action["payload_hash"], {"selected": ["fri"]}, "mark")
        self.assertEqual(again.exception.code, "already_answered")
        with self.assertRaises(store_module.StoreError) as missing:
            self.log.answer("f" * 32, action["payload_hash"], {}, "mark")
        self.assertEqual(missing.exception.code, "not_found")

    def test_withdraw_and_discard(self):
        card = selection()
        self.bound(card)
        self.assertEqual(self.log.discard([card["id"]]), 0, "a task-bound card is never retired as unshown")
        self.assertEqual(self.log.action(card["id"])["state"], "open")
        self.assertTrue(self.log.withdraw(card["id"]))
        self.assertFalse(self.log.withdraw(card["id"]))
        self.assertEqual(self.log.action(card["id"])["state"], "withdrawn")
        self.assertEqual(self.log.open_task_actions("default"), [])
        chat = selection("Chat question")
        self.log.record(chat, profile="friday", session_id="s9")
        self.assertEqual(self.log.discard([chat["id"]]), 1)

    def test_bind_is_idempotent_and_dedupes_a_repeated_draft(self):
        with patch.dict("os.environ", {"HERMES_KANBAN_TASK": "c2", "HERMES_KANBAN_BOARD": "default"}):
            first = cards.render("message", EXAMPLES["message"])
            first["id"] = self.log.record(first, profile="friday", session_id="s1")
            self.log.bind_task(first, profile="friday", board="default", task_id="c2", root_id="r1")
            again = cards.render("message", {**EXAMPLES["message"], "title": "Approve this email?"})
            again["id"] = self.log.record(again, profile="friday", session_id="s2")
            self.assertEqual(again["id"], first["id"])
            action = self.log.bind_task(again, profile="friday", board="default", task_id="c2", root_id="r1")
        self.assertEqual(len(self.log.open_task_actions("default")), 1, "one open action for one draft")
        self.assertEqual(action["card"]["title"], first["title"])

    def test_coordination_register_and_reuse(self):
        self.assertIsNone(self.log.coordination_for_root("default", "r1", now=NOW))
        self.log.register_coordination("sess-1", "default", "r1", now=NOW - 600)
        self.assertEqual(self.log.coordination_for_root("default", "r1", now=NOW)["session_id"], "sess-1")
        self.assertEqual(self.log.coordination_for_session("sess-1")["root_id"], "r1")
        self.assertIsNone(self.log.coordination_for_session("nope"))
        self.assertIsNone(self.log.coordination_for_root("default", "r1", now=NOW + 1800), "30 minutes, then a new session")
        self.assertIsNone(self.log.coordination_for_root("other", "r1", now=NOW))


class DerivationTests(unittest.TestCase):
    def test_containment_from_events(self):
        board = family_board()
        parent_of, children_of = tasks.containment(board.events())
        self.assertEqual(children_of, {"r1": ["c1", "c2", "c3"]})
        self.assertEqual(parent_of, {"c1": "r1", "c2": "r1", "c3": "r1"})
        only_created = {"c9": [{"id": 1, "kind": "created", "payload": {"from_decompose_of": "r2"}, "created_at": 1}]}
        self.assertEqual(tasks.containment(only_created), ({"c9": "r2"}, {"r2": ["c9"]}))
        self.assertEqual(tasks.descendants({"r": ["a"], "a": ["b"]}, "r"), ["a", "b"])

    def test_review_block_reason_and_waits_on(self):
        board = family_board()
        review = tasks.review_from_events(board.events()["c1"])
        self.assertEqual((review["summary"], review["artifacts"], review["reviewer"], review["event_id"]),
                         ("Two venues shortlisted.", ["venues.md"], "friday", 5))
        self.assertEqual(tasks.block_reason(board.events()["c2"]), "Choose a date")
        self.assertIsNone(tasks.block_reason(board.events()["c1"]))
        by_id = {t["id"]: t for t in board.tasks()}
        self.assertEqual(tasks.waits_on("c3", board.links(), by_id), ["c2"])
        self.assertEqual(tasks.waits_on("r1", board.links(), by_id), ["c1", "c2", "c3"])
        self.assertEqual(tasks.dependents("c2", board.links()), ["c3", "r1"])

    def test_attention_state_for_each_rule(self):
        T = FakeBoard.task_row
        state = tasks.attention_state
        self.assertEqual(state(T("a", "archived"), pending=3, waiting=[], child_statuses=[]), "archived")
        self.assertEqual(state(T("a", "todo"), pending=1, waiting=[], child_statuses=[]), "needs_you")
        self.assertEqual(state(T("a", "blocked", block_kind="needs_input"), pending=0, waiting=[], child_statuses=[]), "needs_you")
        self.assertEqual(state(T("a", "blocked", block_kind="capability"), pending=0, waiting=[], child_statuses=[]), "needs_you")
        stuck = T("a", "todo", diagnostics=[{"kind": "repeated_crashes"}])
        self.assertEqual(state(stuck, pending=0, waiting=[], child_statuses=[]), "stuck")
        self.assertEqual(state(T("a", "running"), pending=0, waiting=[], child_statuses=[]), "working")
        self.assertEqual(state(T("a", "todo"), pending=0, waiting=["c"], child_statuses=["done", "running"]), "working")
        self.assertEqual(state(T("a", "done"), pending=0, waiting=[], child_statuses=[]), "done")
        self.assertEqual(state(T("a", "scheduled"), pending=0, waiting=[], child_statuses=[]), "scheduled")
        self.assertEqual(state(T("a", "ready"), pending=0, waiting=[], child_statuses=[]), "queued")
        self.assertEqual(state(T("a", "todo"), pending=0, waiting=["c"], child_statuses=[]), "waiting")
        self.assertEqual(state(T("a", "todo"), pending=0, waiting=[], child_statuses=[]), "not_started")
        self.assertEqual(state(T("a", "triage"), pending=0, waiting=[], child_statuses=[]), "not_started")
        self.assertEqual(state(T("a", "blocked", block_kind="dependency"), pending=0, waiting=[], child_statuses=[]), "waiting")
        self.assertEqual(state(T("a", "blocked", block_kind="transient"), pending=0, waiting=[], child_statuses=[]), "stuck")

    def test_board_pending_is_reviews_only_and_actions_are_empty(self):
        board = family_board()
        out = tasks.compose_board(board="default", tasks=board.tasks(True), events_by_task=board.events(),
                                  links=board.links(), latest_event_id=6, now=NOW)
        by_id = {t["id"]: t for t in out["tasks"]}
        root = by_id["r1"]
        self.assertEqual((out["capability"], out["board"], out["latest_event_id"], out["now"]), (tasks.CAPABILITY, "default", 6, NOW))
        self.assertEqual(out["actions"], [], "task-bound cards were removed (2026-10-23); the key stays for shipped apps")
        self.assertEqual(root["allowed_moves"], ["triage", "scheduled", "ready"])
        self.assertFalse(root["preparing"])
        self.assertEqual(sorted(out["containment"]), [["r1", "c1"], ["r1", "c2"], ["r1", "c3"]])
        self.assertEqual((root["parent_id"], root["children"], root["waits_on"]), (None, ["c1", "c2", "c3"], ["c1", "c2", "c3"]))
        # Hermes links a decomposed root UNDER its children, so progress (per link parent) sits on c2, not r1.
        self.assertIsNone(root["progress"])
        self.assertEqual(by_id["c2"]["progress"], {"done": 0, "total": 2})
        self.assertEqual(root["link_counts"], {"parents": 3, "children": 0})
        self.assertEqual(root["attention"]["pending"], 1, "only c1's review")
        self.assertEqual(root["attention"]["state"], "needs_you")
        first = root["attention"]["first"]
        self.assertEqual((first["kind"], first["task_id"], first["title"]), ("review", "c1", "Review: Shortlist two venues"))
        c1 = by_id["c1"]
        self.assertEqual((c1["parent_id"], c1["children"], c1["attention"]["pending"], c1["attention"]["state"]), ("r1", [], 1, "needs_you"))
        self.assertEqual(c1["attention"]["first"]["summary"], "Two venues shortlisted.")
        self.assertEqual(c1["review"]["artifacts"], ["venues.md"])
        c2 = by_id["c2"]
        # A worker's question is its needs_input block: needs you, with the question as the block reason.
        self.assertEqual((c2["block_reason"], c2["attention"]), ("Choose a date", {"state": "needs_you", "pending": 0, "first": None}))
        c3 = by_id["c3"]
        self.assertEqual((c3["waits_on"], c3["attention"]["state"]), (["c2"], "waiting"))
        self.assertEqual(by_id["t_free"]["attention"], {"state": "not_started", "pending": 0, "first": None})
        self.assertNotIn("archived", {t["status"] for t in out["tasks"]})

    def test_archived_tasks_only_when_asked(self):
        board = FakeBoard([FakeBoard.task_row("a", "archived"), FakeBoard.task_row("b", "todo", assignee="friday")])
        shown = tasks.compose_board(board="default", tasks=board.tasks(True), events_by_task={}, links=[], latest_event_id=0)
        self.assertEqual([t["id"] for t in shown["tasks"]], ["b"])
        self.assertEqual(shown["assignees"], ["friday"])
        both = tasks.compose_board(board="default", tasks=board.tasks(True), events_by_task={}, links=[],
                                   latest_event_id=0, include_archived=True)
        self.assertEqual({t["id"]: t["attention"]["state"] for t in both["tasks"]}, {"a": "archived", "b": "not_started"})

    def test_follow_up_table(self):
        plan = tasks.follow_up_plan
        self.assertEqual(plan("todo", decomposed=True, unsatisfied=[]), ("coordinate", None))
        self.assertEqual(plan("todo", decomposed=False, unsatisfied=[]), ("promote", None))
        self.assertEqual(plan("triage", decomposed=False, unsatisfied=[]), ("release_triage", None))
        self.assertEqual(plan("todo", decomposed=False, unsatisfied=[("c2", "blocked")]), ("waiting", "Waits on c2 (blocked)."))
        for status in ("ready", "running", "scheduled"):
            self.assertEqual(plan(status, decomposed=False, unsatisfied=[]), ("none", None))
        self.assertEqual(plan("blocked", decomposed=False, unsatisfied=[]), ("unblock", None))
        self.assertEqual(plan("review", decomposed=False, unsatisfied=[]), ("reopen_review", None))
        self.assertEqual(plan("done", decomposed=False, unsatisfied=[]), ("reopen_done", None))

    def test_instruction_text(self):
        board = family_board()
        _p, children_of = tasks.containment(board.events())
        family = tasks.family_snapshot("r1", children_of, {t["id"]: t for t in board.tasks()}, board.links())
        self.assertEqual([m["id"] for m in family], ["r1", "c1", "c2", "c3"])
        self.assertEqual(family[2], {"id": "c2", "title": "Choose a date", "status": "blocked", "block_kind": "needs_input",
                                     "assignee": None, "waits_on": []})
        self.assertEqual(family[3]["waits_on"], ["c2"])
        text = tasks.coordination_instruction(board="default", root=board.task("r1"), comment="Make it Friday instead.",
                                              family=family)
        for needle in ("r1", "Plan the night out", "board default", "Make it Friday instead.", "c2 · Choose a date · blocked · blocked: needs_input",
                       "c3 · Book the table · todo · waits for c2", "review the root and every step together",
                       "hermes_task_adjust", "restart:", "redo:", "drop:", "link / unlink", "kanban_create", "don't ask the person first",
                       "Never answer a step's question for the person",
                       'ask in one kanban_comment on the root (r1)', "the question in words", "Don't block the root",
                       "Never send messages or spend money",
                       "at least one step must be ready or running", "saying what you changed and why",
                       "A comment alone wakes nothing"):
            self.assertIn(needle, text)
        for gone in ("hermes_card", "Open decision cards", "withdraws its card"):
            self.assertNotIn(gone, text, "no cards on Kanban tasks (2026-10-23)")

    def test_block_question_writes_the_card_out_in_words(self):
        choice = cards.render("selection", {"title": "Choose a date", "description": "Both venues are free then.", "mode": "single",
                                            "options": [{"id": "thu", "label": "Thursday 19:00", "recommended": True},
                                                        {"id": "fri", "label": "Friday 19:00", "detail": "Quieter room"}]})
        self.assertEqual(tasks.block_question(choice),
                         "Choose a date\nBoth venues are free then.\n\nPick one:\n1. Thursday 19:00 (recommended)\n"
                         "2. Friday 19:00: Quieter room\n\nReply with your choice, or tell me what you'd rather do.")
        draft = cards.render("message", EXAMPLES["message"])
        text = tasks.block_question(draft)
        for needle in ("Send this to Morgan?", "Channel: email", "From: mark@example.com", "To: Morgan Goh <morgan@example.com>",
                       "Subject: Thursday pickup", EXAMPLES["message"]["body"]):
            self.assertIn(needle, text)
        self.assertTrue(text.endswith("\n\nReply send, or tell me what to change."))
        form = cards.render("form", {"title": "Approve the hotel", "submit_label": "Approve", "fields": [
            {"id": "hotel", "kind": "select", "label": "Hotel", "required": True,
             "options": [{"id": "inn", "label": "Inn"}, {"id": "pow", "label": "Prince of Wales"}]},
            {"id": "budget", "kind": "decimal", "label": "Budget", "help_text": "Per night"}]})
        self.assertEqual(tasks.block_question(form), "Approve the hotel\n\nPlease answer:\n- Hotel (required): Inn / Prince of Wales\n"
                                                     "- Budget (Per night)\n\nReply with your answers.")
        many = cards.render("selection", {"title": "Which venues?", "mode": "multiple", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]})
        self.assertIn("Pick one or more:\n1. A\n2. B", tasks.block_question(many))


class RouteBodyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log = store_module.DecisionLog(Path(self.temp.name) / "forms.db")

    def tearDown(self):
        self.temp.cleanup()

    def comment(self, board, task_id, text="Make it shorter", **extra):
        return tasks.comment_on_task(board, self.log, task_id, {"text": text, "author": "mark", **extra})

    def test_a_worker_gets_no_saved_card_context(self):
        # The saved-decisions context restored task-bound card answers (removed 2026-10-23); a worker now reads the
        # task's comments, where every answer is.
        board = family_board()
        card = cards.render("message", EXAMPLES["message"])
        self.log.record(card, profile="friday", session_id="old-worker")
        action = self.log.bind_task(card, profile="friday", board=board.slug, task_id="r1")
        self.log.answer(card["id"], action["payload_hash"], {"selected": ["send"]}, "mark")
        with patch.dict("os.environ", {"HERMES_KANBAN_TASK": "c2"}), \
             patch.object(plugin, "_DECISIONS", self.log), patch.object(tasks, "HermesBoard", return_value=board):
            self.assertIsNone(plugin.on_pre_llm_call(session_id="new-worker", user_message="Resume task"))
        self.assertFalse(hasattr(tasks, "worker_decision_context"))

    def test_external_wait_is_not_another_request_for_review(self):
        board = FakeBoard([FakeBoard.task_row("c2", "running", current_run_id=12)])
        handler = plugin._wait_handler()
        with patch.dict("os.environ", {"HERMES_KANBAN_TASK": "c2", "HERMES_KANBAN_RUN_ID": "12"}), \
             patch.object(tasks, "HermesBoard", return_value=board):
            self.assertEqual(json.loads(handler({"reason": "Waiting for recipient confirmation; sent receipt 123"}))["status"], "waiting")
        out = tasks.board_payload(board, self.log)
        self.assertEqual(out["tasks"][0]["attention"], {"state": "waiting", "pending": 0, "first": None})
        self.assertEqual(out["actions"], [])
        self.assertEqual(board.task("c2")["status"], "scheduled")
        _, resumed = self.comment(board, "c2", "The recipient confirmed it.")
        self.assertEqual((resumed["status"], resumed["follow_up"]), ("ready", "ready"))

    def test_decomposed_root_does_not_keep_old_approval_escalation(self):
        board = family_board()
        board._events["r1"].insert(0, {"id": 0, "kind": "block_loop_detected",
                                       "payload": {"reason": "Approve the draft", "kind": "needs_input"}})
        board._tasks["c1"]["status"] = "done"
        board._tasks["c2"]["status"] = "scheduled"
        board._events["c2"].append({"id": 100, "kind": "scheduled",
                                    "payload": {"reason": tasks.EXTERNAL_WAIT_PREFIX + "Recipient confirmation"}})
        out = tasks.board_payload(board, self.log)
        root = next(t for t in out["tasks"] if t["id"] == "r1")
        # A subtask waiting on someone outside keeps its root in progress (2026-10-11), not the person's.
        self.assertEqual(root["attention"], {"state": "working", "pending": 0, "first": None})
        # A genuine new human block after decomposition must still surface.
        board._events["r1"].append({"id": 101, "kind": "block_loop_detected", "payload": {"reason": "New question"}})
        root = next(t for t in tasks.board_payload(board, self.log)["tasks"] if t["id"] == "r1")
        self.assertEqual(root["attention"]["state"], "needs_you")

    def test_ordinary_schedule_is_not_resumed_by_a_comment(self):
        board = FakeBoard([FakeBoard.task_row("c2", "scheduled")],
                          events={"c2": [{"kind": "scheduled", "payload": {"reason": "Tomorrow at noon"}}]})
        _, result = self.comment(board, "c2", "Thanks")
        self.assertEqual((result["status"], result["follow_up"]), ("scheduled", "none"))

    def test_wait_ignores_an_old_card_but_never_changes_a_newer_run(self):
        board = FakeBoard([FakeBoard.task_row("c2", "running", current_run_id=12)])
        card = selection()
        self.log.record(card, profile="friday", session_id="old")
        self.log.bind_task(card, profile="friday", board="default", task_id="c2")
        self.assertEqual(tasks.wait_for_external_reply(board, "c2", 11, "Reply")["error"], "stale_run")
        self.assertEqual(board.task("c2")["status"], "running")
        self.assertEqual(tasks.wait_for_external_reply(board, "c2", 12, "Reply")["status"], "waiting",
                         "a card bound before 2026-10-23 no longer holds the worker")
        handler = plugin._wait_handler()
        with patch.dict("os.environ", {"HERMES_KANBAN_TASK": "", "HERMES_KANBAN_RUN_ID": ""}):
            self.assertEqual(json.loads(handler({"reason": "Reply"}))["error"], "worker_required")

    def test_unknown_and_archived(self):
        board = FakeBoard([FakeBoard.task_row("a", "archived")])
        self.assertEqual(self.comment(board, "nope"), (404, {"detail": "task_not_found"}))
        self.assertEqual(self.comment(board, "../x")[0], 404)
        self.assertEqual(self.comment(board, "a"), (409, {"detail": "restore_required"}))
        self.assertEqual(board.calls, [])

    def test_leaf_follow_ups(self):
        T = FakeBoard.task_row
        board = FakeBoard([T("todo1", "todo"), T("ready1", "ready"), T("run1", "running"), T("sched1", "scheduled"),
                           T("block1", "blocked", block_kind="needs_input"), T("review1", "review"), T("tri1", "triage"),
                           T("wait1", "todo"), T("done_free", "done")],
                          links=[("block1", "wait1")])
        status, out = self.comment(board, "todo1")
        self.assertEqual((status, out["follow_up"], out["status"], out["route"], out["comment_id"]), (200, "ready", "ready", "leaf", 100))
        for tid, expected in (("ready1", "ready"), ("run1", "running"), ("sched1", "scheduled")):
            status, out = self.comment(board, tid)
            self.assertEqual((status, out["follow_up"], out["status"], out["detail"]), (200, "none", expected, None), tid)
        status, out = self.comment(board, "wait1")
        self.assertEqual((out["follow_up"], out["detail"]), ("waiting", "Waits on block1 (blocked)."))
        status, out = self.comment(board, "block1")
        self.assertEqual((out["follow_up"], out["status"]), ("ready", "ready"))
        board._tasks["block1"]["status"] = "blocked"
        card = selection()
        self.log.record(card, profile="friday", session_id="s1")
        self.log.bind_task(card, profile="friday", board="default", task_id="block1")
        status, out = self.comment(board, "block1")
        self.assertEqual((out["follow_up"], out["status"]), ("ready", "ready"))
        self.assertEqual(self.log.action(card["id"])["state"], "withdrawn", "A comment requests reconsideration, never approval")
        status, out = self.comment(board, "review1")
        self.assertEqual((out["follow_up"], out["status"]), ("reopened", "ready"))
        # Triage uses Hermes's release API rather than promote_task, which refuses triage.
        status, out = self.comment(board, "tri1")
        self.assertEqual((status, out["follow_up"], out["status"]), (200, "ready", "ready"))
        status, out = self.comment(board, "done_free")
        self.assertEqual((status, out["follow_up"], out["comment_id"]), (200, "failed", 100))
        self.assertEqual(board.comment("done_free", 100)["body"], "Make it shorter", "the comment exists even when the follow-up fails")

    def test_comment_release_reports_actual_status_including_fast_workers(self):
        for start, method in (("triage", "release_triage"), ("blocked", "unblock")):
            for landed, expected in (("ready", "ready"), ("running", "none"), ("scheduled", "none"),
                                     ("done", "none"), ("todo", "waiting"), (start, "failed")):
                with self.subTest(start=start, landed=landed):
                    board = FakeBoard([FakeBoard.task_row("t1", start)])
                    def release(*args):
                        board._tasks["t1"]["status"] = landed
                        return True
                    with patch.object(board, method, side_effect=release):
                        code, out = self.comment(board, "t1")
                    self.assertEqual((code, out["status"], out["follow_up"]), (200, landed, expected))
                    self.assertEqual(out["comment_id"], 100)
                    if expected == "failed":
                        self.assertIn("could not resume", out["detail"])

    def test_done_reopen_asks_first_and_reopens_when_confirmed(self):
        T = FakeBoard.task_row
        board = FakeBoard([T("d1", "done", "Draft the CO"), T("k1", "todo", "Send the CO")], links=[("d1", "k1")], lenient=True)
        status, out = self.comment(board, "d1")
        self.assertEqual((status, out), (409, {"detail": "reopen_affects", "affected": [{"id": "k1", "title": "Send the CO", "status": "todo"}]}))
        self.assertEqual(board.calls, [], "nothing is written before the person confirms")
        status, out = self.comment(board, "d1", confirm_reopen=True)
        self.assertEqual((status, out["follow_up"], out["status"]), (200, "reopened", "ready"))
        self.assertEqual(self.comment(board, "k1")[1]["follow_up"], "waiting")

    def test_coordination(self):
        board = family_board()
        coordinator = sys.modules[plugin.__name__ + ".coordinator"]

        def down(*_args, **_kwargs):
            raise OSError("socket refused")
        with patch.object(coordinator, "connect", down):
            status, out = self.comment(board, "r1", text="Make it Friday instead.")
        self.assertEqual((status, out["route"], out["follow_up"], out["coordination"]), (200, "coordinate", "coordination_not_started", None))
        self.assertTrue(out["detail"].startswith("Saved. Coordination not started"), out["detail"])
        self.assertEqual([c for c in board.calls if c[0] != "add_comment"], [], "a root is never promoted")
        resume_status, resumed = tasks.comment_on_task(board, self.log, "r1", {"resume_comment_id": out["comment_id"],
                                                                                "coordinator_session_id": "sess-1", "author": "mark"})
        self.assertEqual((resume_status, resumed["follow_up"], resumed["comment_id"]), (200, "coordinate", out["comment_id"]))
        coordination = resumed["coordination"]
        self.assertEqual((coordination["session_id"], coordination["root_id"]), ("sess-1", "r1"))
        self.assertEqual([m["id"] for m in coordination["family"]], ["r1", "c1", "c2", "c3"])
        self.assertIn("Make it Friday instead.", coordination["instruction"])
        self.assertEqual(len(board._comments["r1"]), 1, "resume adds no comment")
        self.assertEqual(self.log.coordination_for_session("sess-1")["root_id"], "r1")
        # Every comment gets the session the app just made; every id the app knows for it is fenced
        # (Hermes may hand the hook the runtime id or the stored id).
        status, again = tasks.comment_on_task(board, self.log, "r1", {"text": "And book for six.", "author": "mark",
                                                                      "coordinator_session_id": "sess-2",
                                                                      "coordinator_session_ids": ["sess-2", "20261005_stored"]})
        self.assertEqual((again["follow_up"], again["coordination"]["session_id"]), ("coordinate", "sess-2"))
        self.assertEqual(self.log.coordination_for_session("sess-2")["root_id"], "r1")
        self.assertEqual(self.log.coordination_for_session("20261005_stored")["root_id"], "r1")
        self.assertEqual(tasks.comment_on_task(board, self.log, "r1", {"resume_comment_id": 999, "author": "mark"})[0], 404)
        # Every step finished (and the root too): still the coordinator, which can add or redo a step. Mark's
        # "unmark complete and add a step to send the invite" went to a plain worker that could only comment.
        for child in ("c1", "c2", "c3", "r1"):
            board._tasks[child]["status"] = "done"
        status, done = tasks.comment_on_task(board, self.log, "r1", {"text": "Not done: add a step to send it.",
                                                                     "author": "mark", "coordinator_session_id": "sess-3"})
        self.assertEqual((done["route"], done["follow_up"], done["status"]), ("coordinate", "coordinate", "done"))
        self.assertNotIn(("reopen_done", "r1"), board.calls, "the coordinator decides; the comment reopens nothing")
        # Only archived steps left: the root is a leaf again.
        for child in ("c1", "c2", "c3"):
            board._tasks[child]["status"] = "archived"
        board._tasks["r1"]["status"] = "todo"
        status, leaf = self.comment(board, "r1", text="Thanks.")
        self.assertEqual((leaf["route"], leaf["follow_up"], leaf["status"]), ("leaf", "ready", "ready"))

    def test_the_answers_route_is_gone(self):
        # Old app builds still post card answers; they get 410 and nothing is recorded, posted or moved.
        board = family_board()
        card = selection()
        self.log.record(card, profile="friday", session_id="s1")
        action = self.log.bind_task(card, profile="friday", board="default", task_id="c2", root_id="r1")
        body = {"payload_hash": action["payload_hash"], "answer": {"selected": ["thu"]}, "readable": ["Thu"], "author": "mark"}
        for action_id in (card["id"], "0" * 32, "zz"):
            self.assertEqual(tasks.answer_action(lambda slug: board, self.log, action_id, body), (410, {"detail": "task_cards_removed"}))
        self.assertEqual((board.calls, board._comments), ([], {}))
        self.assertEqual(self.log.action(card["id"])["state"], "open", "nothing is answered")

    def test_an_old_open_card_is_ignored_by_the_board_and_the_badge(self):
        # Cards bound before 2026-10-23 stay in the table but never count: the blocked task needs you through its
        # block, the root is led by its review, and the alert kind is never "decision".
        board = family_board()
        card = selection()
        self.log.record(card, profile="friday", session_id="s1")
        self.log.bind_task(card, profile="friday", board="default", task_id="c2", root_id="r1")
        shown = tasks.board_payload(board, self.log)
        by_id = {t["id"]: t for t in shown["tasks"]}
        self.assertEqual(shown["actions"], [])
        self.assertEqual(by_id["c2"]["attention"], {"state": "needs_you", "pending": 0, "first": None})
        self.assertEqual((by_id["r1"]["attention"]["pending"], by_id["r1"]["attention"]["first"]["kind"]), (1, "review"))
        summary = notify.attention_summary(shown, self.log.open_chat_decisions())
        self.assertEqual([(t["id"], t["kind"]) for t in summary["tasks"]], [("r1", "review")])
        self.assertEqual(summary["decisions"], 0, "a bound card is not a chat decision either")
        # Without the review, the root is led by the subtask's block, not the card.
        board._tasks["c1"]["status"] = "done"
        root = next(t for t in tasks.board_payload(board, self.log)["tasks"] if t["id"] == "r1")
        self.assertEqual((root["attention"]["state"], root["attention"]["first"]), ("needs_you", None))
        self.assertEqual(root["block_reason"], "Subtask “Choose a date” is blocked: Choose a date")
        self.assertEqual(notify.request_kind(root), "blocked")
        # A comment on the task retires the card, never approves it.
        self.comment(board, "c2", "Thursday works.")
        self.assertEqual(self.log.action(card["id"])["state"], "withdrawn")


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log = store_module.DecisionLog(Path(self.temp.name) / "forms.db")
        self.board = family_board()
        self.reader = lambda board, root: tasks.descendants(tasks.containment(self.board.events())[1], root)

    def tearDown(self):
        self.temp.cleanup()

    def block(self, tool, args=None, session_id="", env=None):
        return tasks.tool_call_block(tool, args or {}, session_id=session_id, decisions=self.log,
                                     family_reader=self.reader, env=env if env is not None else {})

    def test_worker_finishes_despite_an_old_open_card(self):
        card = selection()
        self.log.record(card, profile="friday", session_id="s1")
        self.log.bind_task(card, profile="friday", board="default", task_id="c2")
        env = {"HERMES_KANBAN_TASK": "c2"}
        self.assertIsNone(self.block("kanban_complete", {"result": "done"}, env=env), "task-bound cards no longer hold a worker")
        self.assertIsNone(self.block("kanban_request_review", {"task_id": "c2"}, env=env))
        self.assertIsNone(self.block("kanban_block", {"kind": "needs_input", "reason": "Which date?"}, env=env))
        self.assertIsNone(self.block("terminal", {"command": "ls"}, env=env))

    def test_coordinator_stays_in_the_family(self):
        self.log.register_coordination("sess-1", "default", "r1")
        inside = {"task_id": "c2", "body": "Try Friday"}
        outside = {"task_id": "t_free", "body": "Try Friday"}
        self.assertIsNone(self.block("kanban_comment", inside, session_id="sess-1"))
        self.assertIsNone(self.block("kanban_comment", outside, session_id="sess-other"), "an unfenced session is free")
        message = self.block("kanban_comment", outside, session_id="sess-1")
        self.assertIn("t_free is outside its family (c1, c2, c3, r1)", message)
        self.assertIsNotNone(self.block("kanban_show", {"task_id": "t_free"}, session_id="sess-1"))
        self.assertIsNone(self.block("kanban_list", {}, session_id="sess-1"), "no task named, nothing to fence")
        self.assertIsNotNone(self.block("kanban_create", {"title": "New"}, session_id="sess-1"))
        self.assertIsNotNone(self.block("kanban_create", {"title": "New", "parents": ["t_free"]}, session_id="sess-1"))
        self.assertIsNone(self.block("kanban_create", {"title": "New", "parents": ["c2"]}, session_id="sess-1"))
        self.assertIsNotNone(self.block("kanban_link", {"parent_id": "c1", "child_id": "t_free"}, session_id="sess-1"))
        self.assertIsNone(self.block("kanban_link", {"parent_id": "c1", "child_id": "c3"}, session_id="sess-1"))

    def test_unreadable_board_fences_to_the_root(self):
        self.log.register_coordination("sess-1", "default", "r1")
        def boom(board, root):
            raise RuntimeError("no kanban")
        verdict = tasks.tool_call_block("kanban_comment", {"task_id": "c2"}, session_id="sess-1", decisions=self.log,
                                        family_reader=boom, env={})
        self.assertIn("c2 is outside its family (r1)", verdict)


class PluginHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        patcher = patch.object(store_module, "hermes_root", return_value=Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = FakeContext()
        with patch.object(plugin, "_attach_supported", return_value=True):
            plugin.register(self.ctx)

    def tearDown(self):
        self.temp.cleanup()

    def test_worker_selection_is_not_shown_and_asks_in_words(self):
        handler = self.ctx.tools["hermes_card_selection"][1]
        with patch.dict("os.environ", {"HERMES_KANBAN_TASK": "c2", "HERMES_KANBAN_BOARD": "default", "HERMES_KANBAN_RUN_ID": "12"}):
            result = json.loads(handler({"title": "Choose a date", "mode": "single", "description": "Both venues are free.",
                                         "options": [{"id": "thu", "label": "Thursday 19:00", "recommended": True},
                                                     {"id": "fri", "label": "Friday 19:00"}]}, session_id="w1"))
            self.assertEqual((result["shown"], result["task_id"]), (False, "c2"))
            self.assertNotIn("card_id", result)
            self.assertEqual(result["block_reason"], "Choose a date\nBoth venues are free.\n\nPick one:\n1. Thursday 19:00 (recommended)\n"
                                                     "2. Friday 19:00\n\nReply with your choice, or tell me what you'd rather do.")
            for needle in ("Cards aren't shown on Kanban tasks", 'kanban_block(kind="needs_input", reason=<block_reason, verbatim>)',
                           "and stop", "comment on the task"):
                self.assertIn(needle, result["next_step"])
            self.assertIsNone(self.ctx.hooks["transform_llm_output"](response_text="Blocked on the date.", session_id="w1",
                                                                     platform="cli"), "nothing is attached to the reply")
            plain = json.loads(self.ctx.tools["hermes_card_metrics"][1](EXAMPLES["metrics"], session_id="w1"))
            self.assertIn("card_id", plain, "display-only cards in a worker are unchanged")
            self.assertNotIn("next_step", plain)
            self.assertIsNone(self.ctx.hooks["pre_tool_call"](tool_name="kanban_complete", args={}, session_id="w1"))
            self.assertIsNone(self.ctx.hooks["pre_tool_call"](tool_name="kanban_heartbeat", args={}, session_id="w1"))
        self.assertEqual(plugin._DECISIONS.open_task_actions("default"), [], "nothing is bound")
        self.assertEqual(plugin._DECISIONS.open(profile="friday"), [], "and nothing waits in the worker's session")
        chat = json.loads(handler({"title": "Chat question", "mode": "single", "options": [{"id": "a", "label": "A"}]}, session_id="s1"))
        self.assertIn("card_id", chat, "chat cards are unchanged")
        self.assertNotIn("next_step", chat)
        self.assertEqual(len(plugin._DECISIONS.open(profile="friday")), 1)

    def test_worker_form_and_draft_ask_in_words(self):
        handler = self.ctx.tools["hermes_card_form"][1]
        form = {"title": "Approve the hotel", "description": "Inn on the Twenty, 1 night.", "submit_label": "Approve",
                "fields": [{"id": "hotel", "kind": "select", "label": "Hotel", "required": True,
                            "options": [{"id": "inn", "label": "Inn"}, {"id": "pow", "label": "Prince of Wales"}]},
                           {"id": "budget", "kind": "decimal", "label": "Budget", "required": False}]}
        with patch.dict("os.environ", {"HERMES_KANBAN_TASK": "c2", "HERMES_KANBAN_BOARD": "default", "HERMES_KANBAN_RUN_ID": "12"}):
            result = json.loads(handler(form, session_id="w1"))
            draft = json.loads(self.ctx.tools["hermes_card_message"][1](EXAMPLES["message"], session_id="w1"))
        self.assertEqual((result["shown"], result["task_id"]), (False, "c2"))
        self.assertNotIn("request_id", result, "no form is stored for a worker")
        self.assertEqual(result["block_reason"], "Approve the hotel\nInn on the Twenty, 1 night.\n\nPlease answer:\n"
                                                 "- Hotel (required): Inn / Prince of Wales\n- Budget\n\nReply with your answers.")
        self.assertIn(EXAMPLES["message"]["body"], draft["block_reason"])
        self.assertTrue(draft["block_reason"].endswith("Reply send, or tell me what to change."))
        self.assertIn("Send nothing until their comment clearly says to send it", draft["next_step"])
        self.assertEqual(plugin._DECISIONS.open_task_actions("default"), [])
        chat = json.loads(handler(form, session_id="s1"))
        self.assertIn("request_id", chat, "in a chat the form stays a form")
        self.assertNotIn("next_step", chat)

    def test_hook_never_raises(self):
        with patch.object(plugin.tasks, "tool_call_block", side_effect=RuntimeError("boom")):
            self.assertIsNone(plugin.on_pre_tool_call(tool_name="kanban_complete", args={}, session_id="x"))


class RouterTests(unittest.TestCase):
    def test_routes_against_a_fake_board(self):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("FastAPI is not installed (run with Hermes' venv python)")
        with tempfile.TemporaryDirectory() as temp:
            log = store_module.DecisionLog(Path(temp) / "forms.db")
            board = family_board()
            card = selection()
            log.record(card, profile="friday", session_id="s1")
            action = log.bind_task(card, profile="friday", board="default", task_id="c2", root_id="r1")

            def factory(slug):
                if slug == "missing":
                    raise tasks.BoardError(404, "board 'missing' does not exist")
                return board
            app = FastAPI()
            app.include_router(tasks.make_router(log, factory), prefix="/api/plugins/agents-cs")
            client = TestClient(app)
            base = "/api/plugins/agents-cs"
            shown = client.get(base + "/tasks/board", params={"board": "default"})
            self.assertEqual(shown.status_code, 200)
            self.assertEqual((shown.json()["capability"], shown.json()["actions"]), (tasks.CAPABILITY, []),
                             "an old bound card is never listed")
            self.assertEqual(client.get(base + "/tasks/board", params={"board": "missing"}).status_code, 404)
            self.assertEqual(client.post(base + "/tasks/zzz/comments", json={"text": "x"}).json(), {"detail": "task_not_found"})
            commented = client.post(base + "/tasks/c3/comments", json={"text": "Hurry", "author": "mark"})
            self.assertEqual((commented.status_code, commented.json()["follow_up"]), (200, "waiting"))
            answered = client.post(base + f"/actions/{card['id']}/answers",
                                   json={"payload_hash": action["payload_hash"], "answer": {"selected": ["fri"]},
                                         "readable": ["Friday"], "author": "mark"})
            self.assertEqual((answered.status_code, answered.json()), (410, {"detail": "task_cards_removed"}))
            self.assertNotIn("c2", board._comments, "nothing is posted")


class LiveBoardLessons(unittest.TestCase):
    """What Mark's real board taught on 2026-10-05: loop-escalated triage tasks need him, and a child of an
    archived root is a task of its own."""

    def test_block_loop_escalation_needs_you_and_keeps_its_reason(self):
        events = [{"kind": "blocked", "payload": {"kind": "needs_input", "reason": "Which date?"}},
                  {"kind": "unblocked", "payload": None}, {"kind": "claimed", "payload": None},
                  {"kind": "block_loop_detected", "payload": {"kind": "needs_input", "reason": "Which date?"}},
                  {"kind": "commented", "payload": None}]
        self.assertTrue(tasks.loop_escalated(events))
        self.assertEqual(tasks.block_reason(events), "Which date?")
        task = {"id": "t1", "status": "triage", "block_kind": "needs_input"}
        self.assertEqual(tasks.attention_state(task, pending=0, waiting=[], child_statuses=[], escalated=True), "needs_you")
        self.assertEqual(tasks.attention_state(task, pending=0, waiting=[], child_statuses=[]), "not_started")
        self.assertFalse(tasks.loop_escalated(events + [{"kind": "claimed", "payload": None}]), "a new claim clears it")

    def test_child_of_an_archived_root_stands_alone(self):
        root = {"id": "r", "status": "archived", "title": "Old root"}
        child = {"id": "c", "status": "triage", "title": "Still open"}
        events = {"r": [{"kind": "decomposed", "payload": {"child_ids": ["c"]}}],
                  "c": [{"kind": "created", "payload": {"from_decompose_of": "r"}}]}
        board = tasks.compose_board(board="default", tasks=[root, child], events_by_task=events, links=[("r", "c")],
                                    latest_event_id=1)
        rows = {t["id"]: t for t in board["tasks"]}
        self.assertEqual(list(rows), ["c"])
        self.assertIsNone(rows["c"]["parent_id"])
        with_archived = tasks.compose_board(board="default", tasks=[root, child], events_by_task=events, links=[("r", "c")],
                                            latest_event_id=1, include_archived=True)
        rows = {t["id"]: t for t in with_archived["tasks"]}
        self.assertEqual((rows["c"]["parent_id"], rows["r"]["children"]), ("r", ["c"]))


class BlockedSubtaskLessons(unittest.TestCase):
    """Mark's board, 2026-10-10 (t_77a73847, Riverdale Christmas Evite): a subtask blocked on a capability
    left its root under Not started saying "Friday is working through the subtasks". The board shows roots,
    so the root has to carry the subtask's need: Review, naming the subtask and its reason, with Reply."""

    REASON = "Chrome must be fully quit before the browser tool can attach; provider unresolved."

    def board(self, block_kind="capability", *, reason=REASON, root_status="todo", second=None):
        rows = [{"id": "r", "status": root_status, "title": "Riverdale Christmas Evite", "assignee": "friday"},
                {"id": "c_send", "status": "blocked", "block_kind": block_kind, "title": "Send the Evite"},
                {"id": "c_after", "status": "todo", "title": "Confirm the guest list"}]
        events = {"r": [{"kind": "decomposed", "payload": {"child_ids": ["c_send", "c_after"]}}],
                  "c_send": [{"kind": "created", "payload": {"from_decompose_of": "r"}},
                             {"kind": "blocked", "payload": {"kind": block_kind, "reason": reason}}],
                  "c_after": [{"kind": "created", "payload": {"from_decompose_of": "r"}}]}
        if second:
            rows.append({"id": "c_more", "title": "Order the cards", **second})
            events["r"][0]["payload"]["child_ids"].append("c_more")
            events["c_more"] = [{"kind": "created", "payload": {"from_decompose_of": "r"}}]
        # The live family's links: the sibling waits on the blocked subtask; the root sits under both.
        links = [("c_send", "c_after"), ("c_send", "r"), ("c_after", "r")]
        payload = tasks.compose_board(board="default", tasks=rows, events_by_task=events, links=links,
                                      latest_event_id=9, now=NOW)
        return payload, {t["id"]: t for t in payload["tasks"]}

    def test_human_blocked_subtask_puts_its_root_in_review(self):
        payload, rows = self.board("capability")
        root = rows["r"]
        self.assertEqual(root["attention"]["state"], "needs_you")
        self.assertEqual((root["attention"]["pending"], root["attention"]["first"]), (0, None),
                         "no new request kind: shipped app builds decode only card and review")
        self.assertEqual(root["block_reason"], f"Subtask “Send the Evite” is blocked: {self.REASON}")
        self.assertEqual(root["attention"]["subtask"]["task_id"], "c_send")
        self.assertNotIn("working through", root["block_reason"])
        # The subtask and its waiting sibling keep their own states.
        self.assertEqual(rows["c_send"]["attention"]["state"], "needs_you")
        self.assertEqual(rows["c_after"]["attention"]["state"], "waiting")
        self.assertEqual(rows["c_send"]["block_reason"], self.REASON, "the subtask's own reason is untouched")
        # Review group: the badge and the board count the root once, as a blocked request.
        summary = notify.attention_summary(payload, 0)
        self.assertEqual([(t["id"], t["kind"]) for t in summary["tasks"]], [("r", "blocked")])
        needs_input, _ = self.board("needs_input", reason="Which RSVP date?")
        self.assertEqual({t["id"]: t for t in needs_input["tasks"]}["r"]["attention"]["state"], "needs_you")

    def test_dependency_blocked_subtask_leaves_its_root_waiting(self):
        payload, rows = self.board("dependency", reason="Waits on the venue")
        root = rows["r"]
        self.assertEqual(root["attention"]["state"], "waiting")
        self.assertIsNone(root["block_reason"])
        self.assertNotIn("subtask", root["attention"])
        self.assertEqual(notify.attention_summary(payload, 0)["tasks"], [])

    def test_stuck_subtask_makes_its_root_stuck_and_extra_needs_are_counted(self):
        _, rows = self.board("transient", reason="Browser crashed")
        self.assertEqual(rows["r"]["attention"]["state"], "stuck")
        self.assertEqual(rows["r"]["block_reason"], "Subtask “Send the Evite” is stuck: Browser crashed")
        # needs_you outranks stuck, and further subtasks are counted on the line.
        _, rows = self.board("transient", reason="Browser crashed",
                             second={"status": "blocked", "block_kind": "needs_input"})
        self.assertEqual(rows["r"]["attention"]["state"], "needs_you")
        self.assertEqual(rows["r"]["block_reason"],
                         "Subtask “Order the cards” is blocked and needs your input. 1 more subtask also needs you.")

    def test_finished_subtasks_and_running_roots(self):
        _, rows = self.board("capability", second={"status": "done", "diagnostics": [{"kind": "repeated_crashes"}]})
        self.assertEqual(rows["r"]["attention"]["subtask"]["task_id"], "c_send", "a done subtask never needs you")
        _, rows = self.board("capability", root_status="running")
        self.assertEqual(rows["r"]["attention"]["state"], "needs_you", "a working root still surfaces the block")


if __name__ == "__main__":
    unittest.main()


class OutsideWaitLessons(unittest.TestCase):
    """Mark's board, 2026-10-11 (t_bc49a266, the Upwork designer): the offer step was parked waiting for the
    designer's acceptance, Hermes flagged its re-checks as a block/unblock cycle, and the root read "Needs you"
    though Friday's own note said nothing was needed from Mark. A wait on someone outside is not the person's."""

    def board(self, *, diagnostics=(), reason=tasks.EXTERNAL_WAIT_PREFIX + "Offer sent; waiting for the designer to accept."):
        rows = [{"id": "r", "status": "todo", "title": "Hire an Upwork designer", "assignee": "friday"},
                {"id": "c_done", "status": "done", "title": "Shortlist designers"},
                {"id": "c_offer", "status": "scheduled", "title": "Send the offer", "diagnostics": list(diagnostics)}]
        events = {"r": [{"kind": "decomposed", "payload": {"child_ids": ["c_done", "c_offer"]}}],
                  "c_done": [{"kind": "created", "payload": {"from_decompose_of": "r"}}],
                  "c_offer": [{"kind": "created", "payload": {"from_decompose_of": "r"}},
                              {"kind": "scheduled", "payload": {"reason": reason}}]}
        payload = tasks.compose_board(board="default", tasks=rows, events_by_task=events,
                                      links=[("c_done", "c_offer"), ("c_offer", "r")], latest_event_id=9, now=NOW)
        return payload, {t["id"]: t for t in payload["tasks"]}

    def test_cycling_rechecks_of_an_outside_wait_are_not_stuck(self):
        payload, rows = self.board(diagnostics=[{"kind": "block_unblock_cycling", "severity": "warning"}])
        self.assertEqual(rows["c_offer"]["attention"]["state"], "waiting")
        self.assertEqual(rows["r"]["attention"]["state"], "working", "the root is in progress, not the person's")
        self.assertNotIn("subtask", rows["r"]["attention"])
        self.assertTrue(rows["c_offer"]["block_reason"].startswith(tasks.EXTERNAL_WAIT_PREFIX), "the app reads the marker")
        self.assertEqual(notify.attention_summary(payload, 0)["tasks"], [])

    def test_an_ordinary_schedule_still_reads_as_scheduled_and_a_real_stuck_diagnostic_still_counts(self):
        _, rows = self.board(reason="Try again at 9am")
        self.assertEqual(rows["c_offer"]["attention"]["state"], "scheduled")
        self.assertEqual(rows["r"]["attention"]["state"], "waiting")
        _, rows = self.board(diagnostics=[{"kind": "repeated_crashes"}], reason="Try again at 9am")
        self.assertEqual(rows["r"]["attention"]["state"], "stuck")


class CoordinatorStepLessons(unittest.TestCase):
    """Mark's board, 2026-10-11: he told a main task to switch to comparing providers. The coordinator rewrote the
    plan but the research step sat in triage ("kanban_unblock cannot resume it") and nothing moved. Now the
    coordinator restarts, redoes, drops and reorders steps itself, and a family it leaves stalled is put in
    front of Mark."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log = store_module.DecisionLog(Path(self.temp.name) / "forms.db")
        T = FakeBoard.task_row
        rows = [T("r", "todo", "Send the invitations"),
                T("research", "triage", "Research the provider"),
                T("copy", "done", "Write the copy"),
                T("final", "todo", "Finalize and send"),
                T("other", "triage", "Unrelated")]
        events = {"r": [{"id": 1, "kind": "decomposed", "payload": {"child_ids": ["research", "copy", "final"]}}]}
        links = [("research", "final"), ("copy", "final"), ("final", "r"), ("research", "r"), ("copy", "r")]
        self.board = FakeBoard(rows, events, links)

    def tearDown(self):
        self.temp.cleanup()

    def adjust(self, **args):
        return tasks.adjust_step(self.board, self.log, "r", args)

    def test_restart_releases_a_parked_step_and_posts_the_instruction_first(self):
        out = self.adjust(action="restart", task_id="research", instruction="Compare the major free providers with RSVP.")
        self.assertEqual(out, {"status": "ready", "task_id": "research"})
        self.assertEqual(self.board.calls[:2], [("add_comment", "research", "coordinator"), ("release_triage", "research", "coordinator")])
        self.assertEqual(self.board.dispatches, 1)

    def test_restart_behind_an_unfinished_step_says_what_it_waits_for(self):
        out = self.adjust(action="restart", task_id="final")
        self.assertEqual(out["status"], "todo")
        self.assertIn("Waits for research (triage)", out["detail"])

    def test_redo_reopens_a_finished_step_and_needs_an_instruction(self):
        self.assertEqual(self.adjust(action="redo", task_id="copy")["error"], "instruction_required")
        self.assertEqual(self.adjust(action="restart", task_id="copy")["error"], "finished")
        out = self.adjust(action="redo", task_id="copy", instruction="Keep the copy but name the new provider.")
        self.assertEqual(out["status"], "ready")
        self.assertIn(("reopen_done", "copy"), self.board.calls)
        self.board._tasks["final"]["status"] = "review"
        self.assertEqual(self.adjust(action="redo", task_id="final", instruction="Wait for the comparison.")["status"], "todo")

    def test_drop_archives_a_step_with_its_reason_but_never_the_root(self):
        self.assertEqual(self.adjust(action="drop", task_id="research")["error"], "reason_required")
        self.assertEqual(self.adjust(action="drop", task_id="r", instruction="x")["error"], "root")
        self.assertEqual(self.adjust(action="drop", task_id="research", instruction="We pick a new provider instead."),
                         {"status": "dropped", "task_id": "research"})
        self.assertEqual(self.board._comments["research"][0]["body"], "Dropped: We pick a new provider instead.")
        self.assertEqual(self.adjust(action="restart", task_id="research")["error"], "dropped")

    def test_link_and_unlink_reorder_steps_inside_the_family_only(self):
        self.assertEqual(self.adjust(action="unlink", parent_id="research", child_id="final")["status"], "unlinked")
        self.assertEqual(self.adjust(action="unlink", parent_id="research", child_id="final")["error"], "no_link")
        self.assertEqual(self.adjust(action="link", parent_id="final", child_id="research")["status"], "linked")
        self.assertEqual(self.adjust(action="link", parent_id="research", child_id="final")["error"], "refused")
        for args in ({"action": "restart", "task_id": "other"}, {"action": "link", "parent_id": "other", "child_id": "final"},
                     {"action": "drop", "task_id": "nope", "instruction": "x"}):
            self.assertEqual(self.adjust(**args)["error"], "outside_family")
        self.assertEqual(self.adjust(action="explode")["error"], "invalid_action")
        self.assertEqual(self.board._tasks["other"]["status"], "triage")

    def test_add_puts_a_new_step_in_the_family_and_reopens_a_finished_main_task(self):
        # Mark's invite task, 2026-10-11: every step done, "add a step to send the invite".
        for tid in ("r", "research", "final"):
            self.board._tasks[tid]["status"] = "done"
        self.assertEqual(self.adjust(action="add", title="Send it")["error"], "title_and_instruction_required")
        self.assertEqual(self.adjust(action="add", title="Send it", instruction="x", after=["other"])["error"], "outside_family")
        out = self.adjust(action="add", title="Send the invitation", instruction="Send once Mark approves the payload.")
        self.assertEqual(out, {"status": "ready", "task_id": "n1", "root_status": "todo"})
        self.assertEqual(self.board.created[0][:5], ("Send the invitation", "Send once Mark approves the payload.", None, 0, True))
        self.assertIn(("n1", "r"), self.board.links())
        self.assertEqual(self.log.containment_pairs("default"), [("r", "n1")])
        _p, children_of = tasks.containment(self.board.events(), self.log.containment_pairs("default"))
        self.assertIn("n1", tasks.descendants(children_of, "r"))
        # A second step after the first waits for it; the main task (already Todo) is left alone.
        self.board.calls.clear()
        out = self.adjust(action="add", title="Check RSVPs", instruction="Read back the RSVPs.", after=["n1"])
        self.assertEqual((out["status"], out["detail"]), ("todo", "Waits for n1 (ready)."))
        self.assertNotIn(("hold", "r"), self.board.calls)
        self.board._tasks["r"]["status"] = "running"
        self.assertEqual(self.adjust(action="add", title="More", instruction="x")["error"], "root_running")

    def test_a_stalled_family_is_put_in_front_of_mark_until_something_moves(self):
        line = tasks.after_coordination(None, "r", self.log, board_factory=lambda ref: self.board)
        self.assertEqual(line, "Nothing in this task is moving: “Research the provider” hasn't started (parked in triage). "
                               "Reply with how to continue.")
        self.assertTrue(self.board.closed)
        payload = tasks.board_payload(self.board, self.log)
        root = next(t for t in payload["tasks"] if t["id"] == "r")
        self.assertEqual(root["attention"]["state"], "needs_you")
        self.assertEqual(root["block_reason"], line)
        self.assertEqual(root["attention"]["subtask"]["task_id"], "research")
        # The research step starts: the root is in progress again without anyone clearing the note.
        self.adjust(action="restart", task_id="research")
        root = next(t for t in tasks.board_payload(self.board, self.log)["tasks"] if t["id"] == "r")
        self.assertEqual(root["attention"]["state"], "working")
        self.assertIsNone(tasks.after_coordination(None, "r", self.log, board_factory=lambda ref: self.board))
        self.assertEqual(self.log.stalls("default"), set())

    def test_no_stall_while_a_step_moves_or_mark_already_has_something(self):
        family = ["research", "copy", "final"]
        by_id = {t["id"]: t for t in self.board.tasks()}
        kwargs = dict(tasks_by_id=by_id, links=self.board.links(), events_by_task={})
        self.assertIsNotNone(tasks.stall_line("r", family, **kwargs))
        self.assertIsNone(tasks.stall_line("r", family, **{**kwargs, "events_by_task": {"research": [{"kind": "block_loop_detected"}]}}))
        for status in ("ready", "running", "review", "blocked", "scheduled"):
            by_id["final"] = {**by_id["final"], "status": status}
            self.assertIsNone(tasks.stall_line("r", family, **kwargs), status)
        # Not stalled-by-note: a root nobody coordinated stays as it was.
        by_id["final"]["status"] = "todo"
        root = next(t for t in tasks.board_payload(self.board, self.log)["tasks"] if t["id"] == "r")
        self.assertNotEqual(root["attention"]["state"], "needs_you")

    def test_a_new_coordination_clears_the_old_stall(self):
        self.log.record_stall("default", "r")
        coordinator = sys.modules[plugin.__name__ + ".coordinator"]
        with patch.object(coordinator, "start", side_effect=coordinator.CoordinationError("down")):
            tasks.comment_on_task(self.board, self.log, "r", {"text": "Pick a new provider.", "author": "mark"})
        self.assertEqual(self.log.stalls("default"), set())

    def test_the_check_runs_when_the_coordinators_turn_ends(self):
        from test_one_call import FakeSocket
        coordinator = sys.modules[plugin.__name__ + ".coordinator"]
        self.board.reopened = lambda: self.board
        socket = FakeSocket()
        with patch.object(coordinator, "connect", lambda *a, **k: socket), patch.object(coordinator, "ws_url", return_value="ws://x"), \
                patch.object(notify, "attention") as rang:
            status, out = tasks.comment_on_task(self.board, self.log, "r", {"text": "Pick a new provider.", "author": "mark"})
            self.assertEqual((status, out["follow_up"]), (200, "coordinate"))
            deadline = time.time() + 2
            while "r" not in self.log.stalls("default") and time.time() < deadline:
                time.sleep(0.02)
            time.sleep(0.05)
        self.assertEqual(self.log.stalls("default"), {"r"}, "the coordinator left the research step parked")
        self.assertTrue(rang.called, "a new stall reaches the phone")

    def test_the_tool_works_only_in_a_coordinator_session_on_its_own_family(self):
        root = Path(self.temp.name) / "home"
        with patch.object(store_module, "hermes_root", return_value=root):
            ctx = FakeContext()
            with patch.object(plugin, "_attach_supported", return_value=True):
                plugin.register(ctx)
            handler = ctx.tools["hermes_task_adjust"][1]
            self.assertEqual(json.loads(handler({"action": "restart", "task_id": "research"}, session_id="chat"))["error"],
                             "not_coordinating")
            plugin._DECISIONS.register_coordination("coord-1", "default", "r")
            with patch.object(tasks, "HermesBoard", lambda board: self.board):
                out = json.loads(handler({"action": "restart", "task_id": "research", "instruction": "Go."}, session_id="coord-1"))
            self.assertEqual(out["status"], "ready")
            self.assertTrue(self.board.closed)
