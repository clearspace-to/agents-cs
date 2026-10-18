"""The delivery database (``<root Hermes home>/hermes-bots/delivery.db``) and its outbox: the hand-off between the
plugin's ``mobile`` module (which writes sealed alerts) and the Hermes Bots connector (which sends them to the hub).

Standard library only and no relative imports: the connector (and the Claude Code host inside it) import this
file on its own, so it must work without the rest of the plugin.

Producers (the plugin: ``hermes serve``, gateways, workers, Kanban workers, any process) call ``enqueue``.
The one consumer (the connector) calls ``claim_queued``, sends a ``push`` frame per row and reports back with
``mark``; ``purge`` drops finished rows after a week. Every method opens its own short-lived connection, so it is
safe from any thread or process (SQLite WAL, 10 s busy timeout).

An outbox row is ALREADY SEALED for exactly one device (``payload_json`` is the whole APNs body, ``alert`` replaced
by a placeholder and the real text inside ``sealed``); nothing here ever sees or needs a push token.

    outbox(id, device_id, created, push_type, priority, collapse_id, expiration, payload_json, state,
           attempts, last_error)

* ``id``           unique row id (a deterministic uuid for an event that must not be sent twice)
* ``device_id``    the hub's device id (``dev_...``), the address of the alert
* ``created``      unix seconds (float)
* ``push_type``    APNs ``apns-push-type``: ``alert`` or ``background``
* ``priority``     APNs ``apns-priority``: 10 or 5
* ``collapse_id``  APNs ``apns-collapse-id`` (a later push with the same id replaces an earlier one), or NULL
* ``expiration``   APNs ``apns-expiration`` as unix seconds (int), or NULL
* ``payload_json`` the APNs body as JSON text
* ``state``        ``queued`` (ready to send) | ``sent`` | ``failed`` (will never work) | ``dropped`` (device gone)
                   | ``held`` (the plugin is holding it back, e.g. a scheduled run's alert; never claimed)
* ``attempts``     how many times ``mark`` recorded an error for it
* ``last_error``   the last error text, or NULL

The same database also holds the mobile module's own tables (``cursors``, ``reads``, ``baselines``, ``views``,
``attention``); there is no ``devices`` table (devices live in the connector's ``devices.json``) and no
``content_keys`` (a device's key is in that file too).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

QUEUED, HELD, SENT, FAILED, DROPPED = "queued", "held", "sent", "failed", "dropped"
STATES = (QUEUED, HELD, SENT, FAILED, DROPPED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  created REAL NOT NULL,
  push_type TEXT NOT NULL,
  priority INTEGER NOT NULL,
  collapse_id TEXT,
  expiration INTEGER,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS outbox_state ON outbox(state, created);
CREATE TABLE IF NOT EXISTS cursors (profile TEXT PRIMARY KEY, row_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS reads (owner TEXT, profile TEXT, chat TEXT, read_id INTEGER DEFAULT 0,
  unread INTEGER DEFAULT 0, updated REAL, PRIMARY KEY(owner, profile, chat));
CREATE TABLE IF NOT EXISTS baselines (owner TEXT, profile TEXT, row_id INTEGER NOT NULL,
  PRIMARY KEY(owner, profile));
CREATE TABLE IF NOT EXISTS views (device_id TEXT PRIMARY KEY, groups TEXT NOT NULL, bots TEXT NOT NULL, updated REAL);
CREATE TABLE IF NOT EXISTS attention (board TEXT PRIMARY KEY, count INTEGER NOT NULL, decisions INTEGER NOT NULL,
  tasks TEXT NOT NULL, updated REAL);
"""

COLUMNS = ("id", "device_id", "created", "push_type", "priority", "collapse_id", "expiration", "payload_json",
           "state", "attempts", "last_error")


def connect(path) -> sqlite3.Connection:
    """The delivery database, created (owner-only) with every table on first use. The caller closes it
    (``with closing(connect(path)) as con, con:`` commits and closes)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fresh = not path.exists()
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass  # another process is switching it right now; WAL is sticky, so it is set already
    con.executescript(SCHEMA)
    if fresh:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return con


class Outbox:
    """The outbox of one delivery database. ``Outbox(path)`` is cheap; nothing is opened until a method runs."""

    def __init__(self, path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        return connect(self.path)

    def enqueue(self, device_id: str, push_type: str, payload: dict, *, priority: int = 10, collapse_id: str | None = None,
                expiration: int | None = None, id: str | None = None, state: str = QUEUED, con: sqlite3.Connection | None = None,
                now: float | None = None) -> str | None:
        """Add one sealed push for one device. Returns the row id, or ``None`` when a row with that ``id`` already
        exists (``INSERT OR IGNORE``: an event enqueued twice is sent once). Pass ``con`` to join the caller's
        transaction (the caller commits); otherwise it commits at once."""
        if state not in (QUEUED, HELD):
            raise ValueError("a new row is queued or held")
        row_id = id or str(uuid.uuid4())
        values = (row_id, device_id, time.time() if now is None else now, push_type, int(priority), collapse_id,
                  None if expiration is None else int(expiration), json.dumps(payload, separators=(",", ":")), state)
        sql = ("INSERT OR IGNORE INTO outbox(id, device_id, created, push_type, priority, collapse_id, expiration, "
               "payload_json, state) VALUES(?,?,?,?,?,?,?,?,?)")
        if con is not None:
            return row_id if con.execute(sql, values).rowcount else None
        with closing(self.connect()) as own, own:
            return row_id if own.execute(sql, values).rowcount else None

    def claim_queued(self, limit: int = 50, *, per_device: bool = False,
                     skip_devices=(), skip_ids=()) -> list[dict]:
        """The oldest rows in state ``queued`` (at most ``limit``), as dicts with every column plus ``payload`` (the
        body already parsed). A read: the rows stay ``queued`` until ``mark`` says otherwise, so the sender decides
        its own in-flight and backoff rules (the connector keeps one in flight per device)."""
        with closing(self.connect()) as con:
            # Rank before applying the batch limit: one device's backlog must not hide other devices.
            sql = ("SELECT * FROM (SELECT *, rowid AS ordering, ROW_NUMBER() OVER "
                   "(PARTITION BY device_id ORDER BY created, rowid) AS rank FROM outbox WHERE state=?) "
                   "WHERE rank=1 ORDER BY created, ordering LIMIT ?") if per_device else (
                   "SELECT * FROM outbox WHERE state=? ORDER BY created, rowid LIMIT ?")
            params = [QUEUED]
            if per_device:
                filters = ""
                for column, values in (("device_id", skip_devices), ("id", skip_ids)):
                    if values:
                        filters += f" AND {column} NOT IN ({','.join('?' for _ in values)})"
                        params.extend(values)
                sql = sql.replace("WHERE rank=1", "WHERE rank=1" + filters)
            rows = con.execute(sql, (*params, int(limit))).fetchall()
        out = []
        for row in rows:
            item = {key: row[key] for key in COLUMNS}
            try:
                item["payload"] = json.loads(row["payload_json"])
            except ValueError:
                item["payload"] = None  # a damaged row: the sender marks it failed
            out.append(item)
        return out

    def mark(self, id: str, state: str, error: str | None = None) -> None:
        """Record the outcome of sending a row: ``sent``, ``failed`` (permanent), ``dropped`` (the hub says the device
        isn't the account's: ``not_your_device``) or back to ``queued`` to retry. A non-empty ``error`` is stored in
        ``last_error`` and counts as an attempt."""
        if state not in STATES:
            raise ValueError("unknown outbox state %r" % (state,))
        with closing(self.connect()) as con, con:
            if error:
                con.execute("UPDATE outbox SET state=?, last_error=?, attempts=attempts+1 WHERE id=?", (state, str(error)[:500], id))
            else:
                con.execute("UPDATE outbox SET state=? WHERE id=?", (state, id))

    def purge(self, older_than_days: float = 7) -> int:
        """Delete rows created more than ``older_than_days`` ago, whatever their state. Returns how many."""
        cutoff = time.time() - older_than_days * 86400
        with closing(self.connect()) as con, con:
            return con.execute("DELETE FROM outbox WHERE created<?", (cutoff,)).rowcount
