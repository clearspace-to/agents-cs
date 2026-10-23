# Portions adapted from bighelp-plugin (loopdy_plugin/store.py form requests), Copyright the
# Loopdy / bighelp contributors, Apache License 2.0. See cards/NOTICE and ../LICENSE.
"""Form requests and their answers, shared by every bot and the dashboard API.

One SQLite file in the Hermes root (``<hermes root>/hermes-bots-cards/forms.db``): a bot's tool call
writes the pending form from that bot's own process, the app's submission arrives through the
dashboard router in ``hermes serve``, and the bot reads the answer back with
``hermes_card_form_response``. A form accepts one answer; it stays readable after that.

The same file holds every decision card (``DecisionLog``); ``coordinations`` fences an orchestrator session
to one root task. Until 2026-10-23 a decision made inside a Kanban worker was bound to its task (``bind_task``)
and answered from the task board; task-bound cards are gone now (a worker asks in words with ``kanban_block``),
and the rows already bound stay in the table, ignored by the board, the guard and the alerts
(docs/TASK_EXPERIENCE_API.md).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

try:  # the plugin lives in two homes (a profile's and the root's); tests import it bare
    from . import cards
except ImportError:  # pragma: no cover - loaded as a top-level module by the dashboard router
    import cards  # type: ignore

# Added to ``decisions`` for task-bound cards (2026-10-05); every column nullable so an old file
# migrates with ALTER TABLE and a chat card keeps them empty.
TASK_COLUMNS = ("board TEXT", "task_id TEXT", "root_id TEXT", "run_id INTEGER", "explanation TEXT",
                "card_json TEXT", "payload_hash TEXT", "answered_by TEXT", "answer_json TEXT",
                "answer_comment_id INTEGER", "answered_at INTEGER")
COORDINATION_MAX_AGE = 1_800  # an orchestrator session is reused for 30 minutes (contract)


class StoreError(Exception):
    """A refused action: ``code`` is one the app switches on (not_found, already_answered, stale_action)."""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


def hermes_root() -> Path:
    """The Hermes root home, also when this process runs a profile (``<root>/profiles/<name>``)."""
    from .. import paths
    return paths.hermes_root()


def resolve_board(board: str | None = None) -> str:
    """The Kanban board slug a worker's card belongs to: the one asked for, else the worker's
    ``HERMES_KANBAN_BOARD``, else Hermes's current board, else ``default``."""
    slug = (board or os.environ.get("HERMES_KANBAN_BOARD") or "").strip().lower()
    if slug:
        return slug
    try:
        from hermes_cli import kanban_db
        return str(kanban_db.get_current_board() or "default")
    except Exception:
        return "default"


def payload_hash(card: dict[str, Any]) -> str:
    """What the app echoes back with an answer, so a changed draft can't be answered with a stale one."""
    return hashlib.sha256(cards.canonical_json(card["data"]).encode()).hexdigest()


class FormStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else hermes_root() / "hermes-bots-cards" / "forms.db"

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("""CREATE TABLE IF NOT EXISTS decisions(
            card_id TEXT PRIMARY KEY, profile TEXT NOT NULL, session_id TEXT NOT NULL,
            title TEXT NOT NULL, options INTEGER NOT NULL, state TEXT NOT NULL,
            created_at INTEGER NOT NULL, closed_at INTEGER)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS forms(
            request_id TEXT PRIMARY KEY, profile TEXT NOT NULL, session_id TEXT NOT NULL,
            title TEXT NOT NULL, form_json TEXT NOT NULL, state TEXT NOT NULL,
            values_json TEXT, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
            submitted_at INTEGER, read_at INTEGER)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS decision_drafts(
            profile TEXT NOT NULL, scope TEXT NOT NULL, digest TEXT NOT NULL,
            card_id TEXT NOT NULL, PRIMARY KEY(profile,scope,digest))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS coordinations(
            session_id TEXT PRIMARY KEY, board TEXT, root_id TEXT, created_at INTEGER)""")
        # What the plugin owns beside the cards (2026-10-08): a decomposed root's children as recorded
        # when we split it (so the board no longer depends on Hermes's event payloads for families it
        # has seen), the receipts that make a comment and a Quick Add safe to retry, and the write-ups
        # still in flight.
        connection.execute("""CREATE TABLE IF NOT EXISTS containment(
            board TEXT NOT NULL, root_id TEXT NOT NULL, child_id TEXT NOT NULL, created_at INTEGER,
            PRIMARY KEY(board, child_id))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS comment_keys(
            client_key TEXT PRIMARY KEY, board TEXT, task_id TEXT, comment_id INTEGER, result_json TEXT,
            created_at INTEGER)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS quick_adds(
            client_key TEXT PRIMARY KEY, board TEXT, task_id TEXT, state TEXT NOT NULL, detail TEXT,
            created_at INTEGER, finished_at INTEGER)""")
        # A root the coordinator left with nothing moving (2026-10-11); the board shows it only while the
        # family is still stalled, and the next coordination clears it.
        connection.execute("""CREATE TABLE IF NOT EXISTS stalls(
            board TEXT NOT NULL, root_id TEXT NOT NULL, created_at INTEGER, PRIMARY KEY(board, root_id))""")
        # A scheduled run's result relayed into a main chat (2026-10-14, relay.py): the one record of which run
        # a Bot Chat result came from, read by the archiver and the mobile module.
        connection.execute("""CREATE TABLE IF NOT EXISTS relays(
            run_session TEXT PRIMARY KEY, home TEXT NOT NULL, job_id TEXT NOT NULL, job_name TEXT NOT NULL,
            execution_id TEXT, chat_session TEXT NOT NULL, label TEXT NOT NULL,
            handed_at INTEGER NOT NULL, replied_at INTEGER)""")
        _migrate_decisions(connection)
        return connection

    def create(self, card: dict[str, Any], *, profile: str, session_id: str, now: float | None = None) -> None:
        current = int(now if now is not None else time.time())
        expires = int(cards._timestamp(card["expires_at"]).timestamp())
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM forms WHERE expires_at < ?", (current - 30 * 86_400,))
            cursor = connection.execute(
                "INSERT OR IGNORE INTO forms(request_id,profile,session_id,title,form_json,state,created_at,expires_at) "
                "VALUES(?,?,?,?,?,'pending',?,?)",
                (card["id"], profile or "default", session_id or "", card["title"],
                 cards.canonical_json(card["data"]), current, expires))
            if cursor.rowcount != 1:
                raise cards.CardError("internal_error", "Form request id collision")

    def submit(self, request_id: str, values: Any, *, profile: str | None = None,
               now: float | None = None) -> dict[str, Any]:
        """Record a person's answer. Returns ``{state, code, message}``; ``state`` is accepted or error."""
        current = int(now if now is not None else time.time())
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM forms WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                return _answer("error", "request_not_found", "This form isn't known to the bot any more.")
            if profile and profile != row["profile"]:
                return _answer("error", "owner_mismatch", "This form belongs to another bot.")
            if row["state"] != "pending":
                return _answer("error", "already_submitted", "This form was already answered.")
            if row["expires_at"] <= current:
                connection.execute("UPDATE forms SET state='expired' WHERE request_id=?", (request_id,))
                return _answer("error", "request_expired", "This form has expired. Ask the bot for a new one.")
            try:
                checked = cards.validate_submission(json.loads(row["form_json"]), values)
            except cards.CardError as error:
                return _answer("error", "invalid_value", str(error))
            connection.execute("UPDATE forms SET state='submitted', values_json=?, submitted_at=? WHERE request_id=?",
                               (cards.canonical_json(checked), current, request_id))
            return {**_answer("accepted", "accepted", "Sent."), "values": checked}

    def status(self, request_id: str, *, profile: str | None = None, mark_read: bool = False,
               now: float | None = None) -> dict[str, Any]:
        current = int(now if now is not None else time.time())
        with closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT * FROM forms WHERE request_id=?", (request_id,)).fetchone()
            if row is None or (profile and profile != row["profile"]):
                return {"request_id": request_id, "state": "not_found"}
            state = row["state"]
            if state == "pending" and row["expires_at"] <= current:
                state = "expired"
            result: dict[str, Any] = {"request_id": request_id, "state": state, "title": row["title"]}
            if row["values_json"]:
                result["values"] = json.loads(row["values_json"])
                result["submitted_at"] = row["submitted_at"]
                if mark_read and row["read_at"] is None:
                    connection.execute("UPDATE forms SET read_at=? WHERE request_id=?", (current, request_id))
            return result


class DecisionLog(FormStore):
    """Every decision card a bot sends (``cards.is_decision``) and what became of it: open until the
    person answers it, then answered or skipped (withdrawn when the bot replaced it). The app's bot
    page lists the open chat ones; the task board lists the task-bound ones. Same file as the forms."""

    def record(self, card: dict[str, Any], *, profile: str, session_id: str, now: float | None = None) -> str:
        """Returns the card id to use: for a ``message`` card that repeats an open draft in the same
        scope (one Kanban task across its retries, or one session) it is the open card's id, so the
        person is asked once however often the bot re-shows the same words."""
        # Retries of one task retain their scope even when Hermes starts a new session.
        # Match the exact outbound payload, never fuzzy titles, recipients or wording.
        task = os.environ.get("HERMES_KANBAN_TASK", "")
        # A worker's card is answered from its task, whatever its shape (``cards.wants_answer``).
        if not (cards.wants_answer(card) if task else cards.is_decision(card)):
            return card["id"]
        data = card["data"]
        count = len(data.get("options") or next((f.get("options") or [] for f in data.get("fields", []) if f.get("kind") == "select"), []))
        scope = ("task:" + resolve_board() + ":" + task if task else "session:" + session_id if session_id else "")
        payload = {k: v for k, v in data.items() if k != "description"}
        digest = hashlib.sha256(cards.canonical_json(payload).encode()).hexdigest()
        dedupe = card["type"] == "message" and bool(scope)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if dedupe:
                existing = connection.execute(
                    "SELECT d.card_id FROM decision_drafts k JOIN decisions d ON d.card_id=k.card_id "
                    "WHERE k.profile=? AND k.scope=? AND k.digest=? AND d.state='open'",
                    (profile or "default", scope, digest)).fetchone()
                if existing:
                    return existing["card_id"]
            connection.execute(
                "INSERT OR IGNORE INTO decisions(card_id,profile,session_id,title,options,state,created_at) "
                "VALUES(?,?,?,?,?,'open',?)",
                (card["id"], profile or "default", session_id or "", card["title"], count,
                 int(now if now is not None else time.time())))
            if dedupe:
                connection.execute("INSERT OR REPLACE INTO decision_drafts VALUES(?,?,?,?)",
                                   (profile or "default", scope, digest, card["id"]))
        return card["id"]

    def close_from(self, text: str, *, profile: str | None = None, now: float | None = None) -> int:
        """Close the decisions a person's message answers. Returns how many closed."""
        return len(self.close_ids_from(text, profile=profile, now=now))

    def close_ids_from(self, text: str, *, profile: str | None = None, now: float | None = None) -> list[str]:
        """``close_from``, returning the card ids it closed (so every other device can be told)."""
        blocks = cards.parse_response_blocks(text)
        if not blocks:
            return []
        current = int(now if now is not None else time.time())
        closed: list[str] = []
        with closing(self._connect()) as connection, connection:
            for block in blocks:
                values = block.get("values") if isinstance(block.get("values"), dict) else {}
                state = "skipped" if values.get("skipped") is True else "answered"
                query = "UPDATE decisions SET state=?, closed_at=? WHERE card_id=? AND state IN ('open','relaying')"
                params: tuple[Any, ...] = (state, current, block["card_id"])
                if profile:
                    query += " AND profile=?"
                    params += (profile,)
                if connection.execute(query, params).rowcount:
                    closed.append(block["card_id"])
        return closed

    def discard(self, card_ids: list[str], *, profile: str | None = None, now: float | None = None) -> int:
        """Retire decisions whose card never reached a screen (a text-only channel or a scheduled job
        swapped it for its fallback text), so the app's open list never shows a card nobody can answer.
        A task-bound card has no chat to show in and is answered from the task board, so it is never
        retired here (2026-10-05)."""
        if not card_ids:
            return 0
        current = int(now if now is not None else time.time())
        with closing(self._connect()) as connection, connection:
            query = ("UPDATE decisions SET state='unshown', closed_at=? WHERE card_id=? AND state='open' "
                     "AND task_id IS NULL")
            return sum(connection.execute(query + (" AND profile=?" if profile else ""),
                                          (current, card_id) + ((profile,) if profile else ())).rowcount
                       for card_id in card_ids)

    def hold(self, card_ids: list[str]) -> int:
        """A scheduled run's cards are on their way to the main chat (relay.py): until they are posted
        there they are ``relaying``, not open, so the app's badge and Review don't send Mark to the run
        (2026-10-14: a card answered in the run chat seconds before its relay was processed twice).
        Answering one in the run chat still closes it."""
        if not card_ids:
            return 0
        with closing(self._connect()) as connection, connection:
            return sum(connection.execute(
                "UPDATE decisions SET state='relaying' WHERE card_id=? AND state='open' AND task_id IS NULL",
                (card_id,)).rowcount for card_id in card_ids)

    def move(self, card_ids: list[str], *, session_id: str) -> int:
        """Point chat decisions at the chat that shows their card now, open again: a scheduled run's cards
        are posted to the main chat as written (relay.py), so Review opens there, not in the archived run."""
        if not card_ids or not session_id:
            return 0
        with closing(self._connect()) as connection, connection:
            return sum(connection.execute(
                "UPDATE decisions SET session_id=?, state='open' WHERE card_id=? AND state IN ('open','relaying') "
                "AND task_id IS NULL", (session_id, card_id)).rowcount for card_id in card_ids)

    def open(self, *, profile: str, limit: int = 100) -> list[dict[str, Any]]:
        """The bot's open chat decisions, newest first. Task-bound cards live on the task board."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT card_id,session_id,title,options,created_at FROM decisions "
                "WHERE profile=? AND state='open' AND task_id IS NULL ORDER BY created_at DESC LIMIT ?",
                (profile, limit)).fetchall()
        return [dict(row) for row in rows]

    def closed(self, *, profile: str, days: int = 14, limit: int = 500, now: float | None = None) -> list[str]:
        """The bot's chat decisions the person answered or skipped lately, on any device: another device whose
        transcript hasn't caught up yet hides these rather than asking again (2026-10-23)."""
        since = int(now if now is not None else time.time()) - days * 86_400
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT card_id FROM decisions WHERE profile=? AND state IN ('answered','skipped') AND task_id IS NULL "
                "AND closed_at>=? ORDER BY closed_at DESC LIMIT ?", (profile, since, limit)).fetchall()
        return [row["card_id"] for row in rows]

    def open_chat_decisions(self) -> int:
        """Every bot's open chat decisions: with the tasks that need the person, the app icon's badge."""
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM decisions WHERE state='open' AND task_id IS NULL").fetchone()
        return int(row["n"] if row else 0)

    # --- Containment, receipts and write-ups (2026-10-08) ------------------------------------------

    def record_containment(self, board: str | None, root_id: str, child_ids: list[str], *, now: float | None = None) -> int:
        """Remember a root's children (from our decompose, or derived once from Hermes's events)."""
        current = int(now if now is not None else time.time())
        slug = resolve_board(board)
        with closing(self._connect()) as connection, connection:
            return sum(connection.execute(
                "INSERT OR IGNORE INTO containment(board,root_id,child_id,created_at) VALUES(?,?,?,?)",
                (slug, root_id, child, current)).rowcount for child in child_ids if child and child != root_id)

    def containment_pairs(self, board: str | None) -> list[tuple[str, str]]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT root_id, child_id FROM containment WHERE board=? ORDER BY created_at, rowid",
                                      (resolve_board(board),)).fetchall()
        return [(row["root_id"], row["child_id"]) for row in rows]

    def recall_comment(self, client_key: str) -> dict[str, Any] | None:
        """The answer a comment with this receipt already got, so a retried send never stores it twice."""
        if not client_key:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT result_json FROM comment_keys WHERE client_key=?", (client_key,)).fetchone()
        return json.loads(row["result_json"]) if row and row["result_json"] else None

    def remember_comment(self, client_key: str, *, board: str | None, task_id: str, comment_id: int | None,
                         result: dict[str, Any], now: float | None = None) -> None:
        if not client_key:
            return
        current = int(now if now is not None else time.time())
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM comment_keys WHERE created_at < ?", (current - 7 * 86_400,))
            connection.execute(
                "INSERT OR REPLACE INTO comment_keys(client_key,board,task_id,comment_id,result_json,created_at) VALUES(?,?,?,?,?,?)",
                (client_key[:200], resolve_board(board), task_id, comment_id, cards.canonical_json(result), current))

    def quick_add(self, client_key: str) -> dict[str, Any] | None:
        if not client_key:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM quick_adds WHERE client_key=?", (client_key,)).fetchone()
        return dict(row) if row else None

    def quick_add_begin(self, client_key: str, *, board: str | None, task_id: str, state: str, now: float | None = None) -> None:
        current = int(now if now is not None else time.time())
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM quick_adds WHERE created_at < ?", (current - 7 * 86_400,))
            connection.execute(
                "INSERT OR REPLACE INTO quick_adds(client_key,board,task_id,state,detail,created_at,finished_at) VALUES(?,?,?,?,NULL,?,NULL)",
                (client_key[:200], resolve_board(board), task_id, state, current))

    def quick_add_finish(self, client_key: str, *, state: str, detail: str | None, now: float | None = None) -> None:
        current = int(now if now is not None else time.time())
        with closing(self._connect()) as connection, connection:
            connection.execute("UPDATE quick_adds SET state=?, detail=?, finished_at=? WHERE client_key=?",
                               (state, (detail or None), current, client_key[:200]))

    def preparing_tasks(self, board: str | None, *, max_age: int = 900, now: float | None = None) -> set[str]:
        """Tasks whose write-up or planning is still running (a crashed one stops counting after ``max_age``)."""
        current = int(now if now is not None else time.time())
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT task_id FROM quick_adds WHERE board=? AND state='writing' AND created_at > ?",
                (resolve_board(board), current - max_age)).fetchall()
        return {row["task_id"] for row in rows}

    # --- Task-bound cards (actions): legacy since 2026-10-23, nothing new is bound ------------------

    def bind_task(self, card: dict[str, Any], *, profile: str, board: str | None, task_id: str,
                  root_id: str | None = None, run_id: int | None = None) -> dict[str, Any]:
        """Tie a recorded decision to the Kanban task whose worker made it. Idempotent: a card
        ``record`` deduplicated onto an open draft keeps that draft's binding, so a re-shown identical
        draft never becomes a second action."""
        if not cards.wants_answer(card):
            raise StoreError("not_a_decision", "Only a card the person answers can be bound to a task")
        data = card.get("data") or {}
        explanation = data.get("description") if isinstance(data.get("description"), str) else None
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM decisions WHERE card_id=?", (card["id"],)).fetchone()
            if row is None:
                raise StoreError("not_found", "Record the card before binding it")
            if row["task_id"] is None:
                connection.execute(
                    "UPDATE decisions SET board=?, task_id=?, root_id=?, run_id=?, explanation=?, card_json=?, "
                    "payload_hash=? WHERE card_id=?",
                    (resolve_board(board), task_id, root_id, run_id, explanation, cards.canonical_json(card),
                     payload_hash(card), card["id"]))
            elif row["root_id"] is None and root_id:
                connection.execute("UPDATE decisions SET root_id=? WHERE card_id=?", (root_id, card["id"]))
            row = connection.execute("SELECT * FROM decisions WHERE card_id=?", (card["id"],)).fetchone()
        return action_from_row(row)

    def open_task_actions(self, board: str | None = None) -> list[dict[str, Any]]:
        """Every open task-bound card on a board, oldest first (the order the person works through)."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM decisions WHERE state='open' AND task_id IS NOT NULL AND board=? "
                "ORDER BY created_at ASC, card_id ASC", (resolve_board(board),)).fetchall()
        return [action_from_row(row) for row in rows]

    def answered_task_actions(self, board: str, task_ids: list[str]) -> list[dict[str, Any]]:
        """Recover exact decisions across worker restarts and containment handoffs."""
        if not task_ids:
            return []
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM decisions WHERE board=? AND state='answered' AND answer_json IS NOT NULL "
                "AND task_id IN (" + ",".join("?" for _ in task_ids) + ") "
                "ORDER BY answered_at DESC, card_id DESC LIMIT 20", (board, *task_ids)).fetchall()
        return [action_from_row(row) for row in reversed(rows)]

    def open_task_cards(self, task_id: str) -> list[dict[str, Any]]:
        """Open task-bound cards of one task, whatever the board (the worker guard's question)."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM decisions WHERE state='open' AND task_id=? ORDER BY created_at ASC, card_id ASC",
                (task_id,)).fetchall()
        return [action_from_row(row) for row in rows]

    def action(self, action_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM decisions WHERE card_id=? AND task_id IS NOT NULL",
                                     (action_id,)).fetchone()
        return action_from_row(row) if row else None

    def answer(self, action_id: str, payload_hash_given: str | None, answer: Any, author: str,
               *, now: float | None = None) -> tuple[str, dict[str, Any]]:
        """Record the person's answer in one transaction. Returns ``("answered", action)``; raises
        ``StoreError`` not_found / already_answered / stale_action."""
        current = int(now if now is not None else time.time())
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM decisions WHERE card_id=? AND task_id IS NOT NULL",
                                     (action_id,)).fetchone()
            if row is None:
                raise StoreError("not_found", "Unknown action")
            if row["state"] != "open":
                raise StoreError("already_answered", "This decision was already answered")
            if (payload_hash_given or "") != (row["payload_hash"] or ""):
                raise StoreError("stale_action", "The card changed since it was shown; reload the task")
            connection.execute(
                "UPDATE decisions SET state='answered', answer_json=?, answered_by=?, answered_at=?, closed_at=? "
                "WHERE card_id=?",
                (cards.canonical_json(answer if answer is not None else {}), (author or "")[:80] or None,
                 current, current, action_id))
            row = connection.execute("SELECT * FROM decisions WHERE card_id=?", (action_id,)).fetchone()
        return "answered", action_from_row(row)

    def set_answer_comment(self, action_id: str, comment_id: int | None) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("UPDATE decisions SET answer_comment_id=? WHERE card_id=?", (comment_id, action_id))

    def withdraw(self, card_id: str, *, now: float | None = None) -> bool:
        """The worker replaced the card (a changed draft): the old one stops being asked."""
        current = int(now if now is not None else time.time())
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE decisions SET state='withdrawn', closed_at=? WHERE card_id=? AND state='open'",
                (current, card_id))
            return cursor.rowcount == 1

    # --- Coordinator fences -----------------------------------------------------------------------

    def register_coordination(self, session_id: str, board: str | None, root_id: str,
                              *, now: float | None = None) -> dict[str, Any]:
        current = int(now if now is not None else time.time())
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO coordinations(session_id,board,root_id,created_at) VALUES(?,?,?,?)",
                (session_id, resolve_board(board), root_id, current))
        return {"session_id": session_id, "board": resolve_board(board), "root_id": root_id, "created_at": current}

    def coordination_for_session(self, session_id: str) -> dict[str, Any] | None:
        if not session_id:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM coordinations WHERE session_id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def coordination_for_root(self, board: str | None, root_id: str, *, max_age: int = COORDINATION_MAX_AGE,
                              now: float | None = None) -> dict[str, Any] | None:
        """The newest orchestrator session still fresh for this root, so a second comment goes to it."""
        current = int(now if now is not None else time.time())
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM coordinations WHERE board=? AND root_id=? AND created_at>? "
                "ORDER BY created_at DESC LIMIT 1", (resolve_board(board), root_id, current - max_age)).fetchone()
        return dict(row) if row else None


    # --- Stalls ------------------------------------------------------------------------------------

    def record_stall(self, board: str | None, root_id: str, *, now: float | None = None) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("INSERT OR REPLACE INTO stalls(board,root_id,created_at) VALUES(?,?,?)",
                               (resolve_board(board), root_id, int(now if now is not None else time.time())))

    def clear_stall(self, board: str | None, root_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM stalls WHERE board=? AND root_id=?", (resolve_board(board), root_id))

    def stalls(self, board: str | None) -> set[str]:
        with closing(self._connect()) as connection:
            return {row["root_id"] for row in connection.execute(
                "SELECT root_id FROM stalls WHERE board=?", (resolve_board(board),))}


def action_from_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """The contract's Action: what the app renders on the task board (docs/TASK_EXPERIENCE_API.md)."""
    card = json.loads(row["card_json"]) if row["card_json"] else None
    answer = json.loads(row["answer_json"]) if row["answer_json"] else None
    return {
        "action_id": row["card_id"], "board": row["board"], "task_id": row["task_id"], "root_id": row["root_id"],
        "profile": row["profile"], "run_id": row["run_id"],
        "kind": "approval" if (card or {}).get("type") == "message" else "question",
        "title": row["title"], "explanation": row["explanation"], "card": card,
        "payload_hash": row["payload_hash"], "state": row["state"], "created_at": row["created_at"],
        "answered_at": row["answered_at"], "answer": answer, "answered_by": row["answered_by"],
        "answer_comment_id": row["answer_comment_id"], "session_id": row["session_id"],
    }


def _migrate_decisions(connection: sqlite3.Connection) -> None:
    """Add the task columns an older ``forms.db`` lacks. Idempotent: only missing columns are added."""
    present = {row["name"] for row in connection.execute("PRAGMA table_info(decisions)")}
    for column in TASK_COLUMNS:
        if column.split()[0] not in present:
            connection.execute(f"ALTER TABLE decisions ADD COLUMN {column}")


def _answer(state: str, code: str, message: str) -> dict[str, Any]:
    return {"state": state, "code": code, "message": message}


class RelayLog(FormStore):
    """Which run each relayed result came from, and whether its reply was saved (``replied_at``)."""

    def record(self, run_session: str, *, home: str, job_id: str, job_name: str, execution_id: str,
               chat_session: str, label: str, now: float | None = None) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO relays(run_session,home,job_id,job_name,execution_id,chat_session,label,"
                "handed_at,replied_at) VALUES(?,?,?,?,?,?,?,?,NULL)",
                (run_session, home, job_id, job_name, execution_id or None, chat_session, label,
                 int(now if now is not None else time.time())))

    def replied(self, run_session: str, *, now: float | None = None) -> bool:
        with closing(self._connect()) as connection, connection:
            return connection.execute("UPDATE relays SET replied_at=? WHERE run_session=? AND replied_at IS NULL",
                                      (int(now if now is not None else time.time()), run_session)).rowcount == 1

    def for_run(self, run_session: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM relays WHERE run_session=?", (run_session,)).fetchone()
        return dict(row) if row else None

    def for_chat(self, chat_session: str, *, since: float = 0) -> list[dict[str, Any]]:
        """Relayed results shown in this chat since ``since``, newest first."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM relays WHERE chat_session=? AND replied_at IS NOT NULL AND replied_at>=? "
                "ORDER BY replied_at DESC", (chat_session, int(since))).fetchall()
        return [dict(row) for row in rows]

    def replied_runs(self, home: str, *, since: float = 0) -> list[dict[str, Any]]:
        """This home's runs whose result was posted to a main chat since ``since``."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM relays WHERE home=? AND replied_at IS NOT NULL AND replied_at>=?", (home, int(since))).fetchall()
        return [dict(row) for row in rows]

