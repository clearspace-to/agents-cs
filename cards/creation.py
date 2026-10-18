"""Creation-triggered decomposition; never scans triage or retries old work.

Hermes owns task mutations and decomposition. This plugin owns only the preference and
an at-most-once attempt receipt. The native tool still runs through every fleet guard.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from .store import FormStore, resolve_board
from . import tasks

APP_PLATFORMS = {"api", "desktop", "tui", "cli"}


class CreationLog(FormStore):
    def _connect(self):
        c = super()._connect()
        c.execute("CREATE TABLE IF NOT EXISTS creation_settings (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        c.execute("""CREATE TABLE IF NOT EXISTS creation_attempts (
            board TEXT NOT NULL, task_id TEXT NOT NULL, started_at INTEGER NOT NULL,
            result_json TEXT, PRIMARY KEY(board,task_id))""")
        return c

    def enabled(self) -> bool:
        with closing(self._connect()) as c:
            row = c.execute("SELECT value FROM creation_settings WHERE key='decompose'").fetchone()
            return bool(row and row[0])

    def set_enabled(self, enabled: bool) -> None:
        with closing(self._connect()) as c:
            c.execute("INSERT OR REPLACE INTO creation_settings VALUES('decompose',?)", (int(enabled),))

    def result(self, board: str, task_id: str) -> dict | None:
        with closing(self._connect()) as c:
            row = c.execute("SELECT result_json FROM creation_attempts WHERE board=? AND task_id=?", (board, task_id)).fetchone()
        if row is None:
            return None
        return json.loads(row[0]) if row[0] else {"ok": False, "pending": True, "reason": "Creation planning already started; it will not be repeated."}

    def reserve(self, board: str, task_id: str) -> bool:
        with closing(self._connect()) as c:
            return c.execute("INSERT OR IGNORE INTO creation_attempts VALUES(?,?,?,NULL)",
                             (board, task_id, int(time.time()))).rowcount == 1

    def finish(self, board: str, task_id: str, result: dict) -> None:
        with closing(self._connect()) as c:
            c.execute("UPDATE creation_attempts SET result_json=? WHERE board=? AND task_id=?",
                      (json.dumps(result), board, task_id))

    def start(self, board: str, task_id: str) -> bool:
        with closing(self._connect()) as c:
            return c.execute("UPDATE creation_attempts SET result_json=? WHERE board=? AND task_id=? AND result_json IS NULL",
                             (json.dumps({"ok": False, "pending": True, "reason": "Creation planning is already running; it will not be repeated."}),
                              board, task_id)).rowcount == 1


def rejection(board: Any, decisions: Any, task_id: str, creation_key: str) -> str | None:
    task = board.task(task_id)
    if not task or task.get("status") != "triage":
        return "Only a newly created task still in triage can be split."
    if not creation_key or task.get("idempotency_key") != creation_key:
        return "The task creation receipt does not match."
    if task.get("started_at") or task.get("current_run_id") or task.get("block_kind"):
        return "This task has already started or waited for input."
    events = board.events()
    parent_of, children_of = tasks.containment(events)
    if task_id in parent_of or children_of.get(task_id):
        return "Subtasks and previously decomposed tasks cannot be split on creation."
    if any(e.get("kind") != "created" for e in events.get(task_id, [])):
        return "This task has already progressed beyond creation."
    # Include withdrawn and answered decisions, not just open ones.
    with closing(decisions._connect()) as c:
        if c.execute("SELECT 1 FROM decisions WHERE board=? AND task_id=? LIMIT 1", (board.slug, task_id)).fetchone():
            return "This task already has decision history."
    return None


def prepare(board: Any, decisions: Any, log: CreationLog, task_id: str, creation_key: str,
            decompose, *, reserved: bool = False) -> dict:
    previous = log.result(board.slug, task_id)
    if previous is not None and not reserved:
        return previous
    if reserved and not log.start(board.slug, task_id):
        return previous or {"ok": False, "reason": "No creation attempt was reserved."}
    reason = rejection(board, decisions, task_id, creation_key)
    if reason:
        result = {"ok": False, "reason": reason}
        if reserved:
            log.finish(board.slug, task_id, result)
        return result
    if not reserved and not log.reserve(board.slug, task_id):
        return log.result(board.slug, task_id)
    if not reserved and not log.start(board.slug, task_id):
        return log.result(board.slug, task_id)
    try:
        result = decompose(task_id, board.slug)
    except Exception:
        result = {"ok": False, "reason": "Creation planning failed. Check the task before taking further action; it was not retried."}
    log.finish(board.slug, task_id, result)
    if not result.get("ok"):
        board.add_comment(task_id, "creation-planner", result.get("reason") or "Creation planning failed; no retry was scheduled.")
    return result


def background_enabled() -> bool:
    from hermes_cli.config import load_config_readonly
    return bool((load_config_readonly().get("kanban") or {}).get("auto_decompose", True))


def run_reserved(board_slug: str, task_id: str, creation_key: str) -> None:
    from .store import DecisionLog
    from .dashboard.plugin_api import decompose_with_scope
    log = CreationLog()
    board = tasks.HermesBoard(board_slug)
    try:
        if background_enabled():
            result = {"ok": False, "reason": "Turn off background task decomposition before using creation-only planning."}
            log.finish(board_slug, task_id, result)
            board.add_comment(task_id, "creation-planner", result["reason"])
            return
        prepare(board, DecisionLog(), log, task_id, creation_key,
                lambda tid, slug: decompose_with_scope(tid, "creation-planner", slug), reserved=True)
    finally:
        board.close()


def launch(board_slug: str, task_id: str, creation_key: str) -> None:
    # A separate process outlives the chat/tool hook. No retries after an uncertain exit.
    script = ("import importlib.util,sys; from pathlib import Path; p=Path(sys.argv[1]); "
              "s=importlib.util.spec_from_file_location('creation_plugin',p/'__init__.py',submodule_search_locations=[str(p)]); "
              "m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m); "
              "m.creation.run_reserved(*sys.argv[2:])")
    proc = subprocess.Popen([sys.executable, "-c", script, str(Path(__file__).parent), board_slug, task_id, creation_key],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    def reap():
        if proc.wait() == 0:
            return
        log = CreationLog()
        if (log.result(board_slug, task_id) or {}).get("pending"):
            result = {"ok": False, "reason": "Creation planning stopped before confirming an outcome. Check the task; it will not be retried automatically."}
            log.finish(board_slug, task_id, result)
            board = tasks.HermesBoard(board_slug)
            try:
                board.add_comment(task_id, "creation-planner", result["reason"])
            finally:
                board.close()
    threading.Thread(target=reap, daemon=True).start()


class BotCreation:
    def __init__(self, log: CreationLog, decisions: Any):
        self.log, self.decisions = log, decisions
        self.sessions: dict[str, bool] = {}
        self.calls: dict[tuple[str, str], int] = {}
        self.lock = threading.Lock()

    def observe_turn(self, session_id: str, platform: str) -> None:
        with self.lock:
            if len(self.sessions) > 2000:
                self.sessions.clear()
            self.sessions[session_id] = bool(session_id and platform in APP_PLATFORMS)

    def before(self, tool_name: str, args: dict, session_id: str, tool_call_id: str) -> dict | None:
        if tool_name != "kanban_create" or not tool_call_id:
            return None
        if (not self.sessions.get(session_id) or os.environ.get("HERMES_KANBAN_TASK") or
                os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT") or args.get("parents") or
                args.get("initial_status") == "blocked" or not self.log.enabled()):
            return None
        if background_enabled():
            return {"action": "block", "message": "Turn off background decomposition before creating with creation-only planning."}
        with self.lock:
            self.calls[(session_id, tool_call_id)] = int(time.time())
        changes = {"triage": True}
        if not args.get("idempotency_key"):
            changes["idempotency_key"] = "creation:" + uuid.uuid4().hex
        return {"action": "modify", "args": changes}

    def after(self, tool_name: str, args: dict, result: Any, session_id: str, tool_call_id: str, **kwargs) -> None:
        key = (session_id, tool_call_id)
        with self.lock:
            if key not in self.calls:
                return
            began = self.calls.pop(key)
        if kwargs.get("error_type") or kwargs.get("status") in ("error", "blocked"):
            return
        data = json.loads(result) if isinstance(result, str) else result
        task_id = data.get("task_id") if isinstance(data, dict) else None
        if not task_id or data.get("error"):
            return
        board = tasks.HermesBoard(args.get("board"))
        try:
            task = board.task(task_id) or {}
            if (task.get("created_at") or 0) < began:
                return  # idempotency returned an older task, not a new creation
            receipt = task.get("idempotency_key") or ""
            reason = rejection(board, self.decisions, task_id, receipt)
            if reason or not self.log.reserve(board.slug, task_id):
                return
            try:
                launch(board.slug, task_id, receipt)
            except Exception:
                result = {"ok": False, "reason": "Could not start creation planning. No retry was scheduled."}
                self.log.finish(board.slug, task_id, result)
                board.add_comment(task_id, "creation-planner", result["reason"])
        finally:
            board.close()

    def transform(self, tool_name: str, args: dict, result: Any, **kwargs) -> str | None:
        if tool_name != "kanban_create":
            return None
        data = json.loads(result) if isinstance(result, str) else result
        if not isinstance(data, dict) or not data.get("task_id"):
            return None
        outcome = self.log.result(resolve_board(args.get("board")), data["task_id"])
        if outcome is None:
            return None
        return json.dumps({**data, "creation_planning": outcome,
                           "next_step": "Creation planning has already been requested once. Do not specify, decompose, "
                                        "promote, or recreate this task. Read its current state for the outcome."})
