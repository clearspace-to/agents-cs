"""Archive a scheduled run's chat once its result has reached the bot's main chat (Mark, 2026-10-13).

A job that delivers to Bot Chat shows twice: as a run in Scheduled and as a message in the main chat.
Mark reads it in the main chat, so the run is archived once that is certain. Two proofs, nothing else:

- the run's result was relayed into a main chat and the reply saved (``store.RelayLog``, written by
  ``relay.py`` on the hand-off turn; ``replied_at`` set when the reply was stored); or
- the run was silent: ``executions.delivery_outcome = suppressed`` for a completed execution, and the
  job's one chat of that run (``cron_<job>_<local time>``, started between the run's start and the
  job's next run).

Either way the chat must exist, be unarchived and not open with ``[CRON_FAILURE]``. Anything else
(failed, still pending, delivered elsewhere, no or several chats) stays in Scheduled. Each run is
decided once (``cron-archive.db``), so a run Mark unarchives is never archived again.

Triggered by ``post_llm_call`` in whichever profile finished a turn: the main chat's turn is what
writes the relay. The sweep runs on daemon timers seconds later and looks back two days, so a missed
sweep is caught by the next turn. It never raises into Hermes. Uses Hermes's own archive call
(``SessionDB.set_session_archived``, the same one ``PATCH /api/sessions/{id}`` makes); Hermes itself
is not changed.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

try:
    from .store import RelayLog, hermes_root
except ImportError:  # pragma: no cover
    from store import RelayLog, hermes_root  # type: ignore

logger = logging.getLogger(__name__)

LOOKBACK = 2 * 86400
# Soon after the main chat's reply, then fallbacks. The mobile module holds a run's own alert and drops it
# once the run is archived.
DELAYS = (3.0, 15.0, 60.0, 240.0)
_timers: set = set()
_lock = threading.Lock()


def on_turn(**_kwargs) -> None:
    try:
        from hermes_constants import get_hermes_home
        home = str(Path(get_hermes_home()).resolve())
    except Exception:
        return
    for delay in DELAYS:
        _later(home, delay)


def _later(home: str, delay: float) -> None:
    key = (home, delay)
    with _lock:
        if key in _timers:
            return
        _timers.add(key)

    def run():
        with _lock:
            _timers.discard(key)
        try:
            sweep(home)
        except Exception:
            logger.debug("cron run archive sweep failed for %s", home, exc_info=True)

    timer = threading.Timer(delay, run)
    timer.daemon = True
    timer.start()


def _archive(home: Path, session_id: str) -> bool:
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    try:
        return bool(db.set_session_archived(session_id, True))
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            close()


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def _epoch(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


class Decided:
    """Runs already decided, so an unarchived run stays unarchived. Shared by every process and profile."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else hermes_root() / "hermes-bots-cards" / "cron-archive.db"

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        con.execute("CREATE TABLE IF NOT EXISTS archived (home TEXT, execution_id TEXT, session_id TEXT, "
                    "at REAL, PRIMARY KEY (home, execution_id))")
        return con

    def seen(self, home: str) -> set[str]:
        with closing(self._connect()) as con:
            return {row[0] for row in con.execute("SELECT execution_id FROM archived WHERE home = ?", (home,))}

    def claim(self, home: str, execution_id: str, session_id: str) -> bool:
        with closing(self._connect()) as con:
            return con.execute("INSERT OR IGNORE INTO archived VALUES (?, ?, ?, ?)",
                               (home, execution_id, session_id, time.time())).rowcount == 1


def _reported_failure(state: sqlite3.Connection, session_id: str) -> bool:
    row = state.execute("SELECT content FROM messages WHERE session_id = ? AND role = 'assistant' "
                        "AND content IS NOT NULL AND content != '' ORDER BY id DESC LIMIT 1",
                        (session_id,)).fetchone()
    return bool(row) and str(row[0]).lstrip().startswith("[CRON_FAILURE]")


def sweep(home, *, now: float | None = None, archive=_archive, decided: Decided | None = None,
          relays: RelayLog | None = None) -> list[str]:
    """Archive this home's delivered and silent runs from the last two days; returns the chats archived."""
    home = Path(home).resolve()
    executions_db, state_db = home / "cron" / "executions.db", home / "state.db"
    if not executions_db.is_file() or not state_db.is_file():
        return []
    now = time.time() if now is None else now
    since = now - LOOKBACK
    decided = decided or Decided()
    relays = relays or RelayLog()
    seen = decided.seen(str(home))

    with closing(_read_only(executions_db)) as con:
        rows = con.execute("SELECT id, job_id, status, delivery_outcome, started_at FROM executions "
                           "WHERE started_at IS NOT NULL").fetchall()
    runs: dict[str, list] = {}
    for execution_id, job_id, status, outcome, started in rows:
        start = _epoch(started)
        if start is not None and start >= since - 86400:
            runs.setdefault(job_id, []).append((start, execution_id, status, outcome or ""))
    by_execution = {execution_id: status for _, execution_id, status, _ in
                    (run for job_runs in runs.values() for run in job_runs)}

    archived = []
    with closing(_read_only(state_db)) as state:
        chats = {row[0]: row for row in state.execute(
            "SELECT id, started_at, ended_at, archived FROM sessions WHERE source = 'cron' AND started_at >= ?",
            (since - 86400,)).fetchall()}

        def archive_chat(session_id: str, execution_id: str) -> None:
            chat = chats.get(session_id)
            if chat is None or chat[3] or execution_id in seen or _reported_failure(state, session_id):
                return
            if decided.claim(str(home), execution_id, session_id) and archive(home, session_id):
                archived.append(session_id)

        # Relayed: the main chat posted the result, so the run is read there.
        for relay in relays.replied_runs(str(home), since=since):
            execution_id = relay.get("execution_id") or f"relay:{relay['run_session']}"
            if relay.get("execution_id") and by_execution.get(relay["execution_id"]) not in (None, "completed"):
                continue
            archive_chat(relay["run_session"], execution_id)

        # Silent: nothing was sent anywhere, and exactly one chat of that job belongs to the run.
        for job_id, job_runs in runs.items():
            job_runs.sort()
            prefix = f"cron_{job_id}_"
            for index, (start, execution_id, status, outcome) in enumerate(job_runs):
                if start < since or status != "completed" or outcome != "suppressed" or execution_id in seen:
                    continue
                end = job_runs[index + 1][0] if index + 1 < len(job_runs) else float("inf")
                matches = [c for c in chats.values() if c[0].startswith(prefix) and start - 5 <= (c[1] or 0) < end]
                if len(matches) == 1:
                    archive_chat(matches[0][0], execution_id)
    return archived
