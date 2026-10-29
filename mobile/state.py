"""Read state, inbox filters and badges: the parts of ``delivery.db`` the app's own requests change.

``reads`` and ``baselines`` are keyed by owner. A computer belongs to one account, and Hermes in the loopback mode the
connector requires has no per-person login, so every request and the reply scan use ``OWNER`` unless Hermes's
session names a user (``routes.owner_of``). ``views`` is keyed by the hub's device id (``X-Hermes-Bots-Device``).
"""
from __future__ import annotations

import json
import sqlite3
import time

from . import replies
from .replies import DEFAULT_VIEW, in_view

OWNER = "local"


def view_for(con, device_id):
    """(groups, bots) the device lists; bots empty means every bot."""
    row = con.execute("SELECT groups, bots FROM views WHERE device_id=?", (device_id or "",)).fetchone()
    if row is None:
        return DEFAULT_VIEW
    try:
        return frozenset(json.loads(row[0])), frozenset(json.loads(row[1]))
    except (TypeError, ValueError):
        return DEFAULT_VIEW  # a damaged row must never stop alerts


def set_view(con, device_id, groups, bots):
    con.execute("INSERT OR REPLACE INTO views(device_id, groups, bots, updated) VALUES(?,?,?,?)",
                (device_id, json.dumps(sorted(groups)), json.dumps(sorted(bots)), time.time()))


def attention_count(con):
    """What needs the person beside unread chats (Mark, 2026-10-08): the bots' open decisions and the
    board tasks waiting on him, as the cards module last reported them (``events.attention``)."""
    try:
        row = con.execute("SELECT COALESCE(SUM(count), 0) AS n FROM attention").fetchone()
    except sqlite3.Error:
        return 0
    return int(row["n"] if row else 0)


def attention_state(con):
    """The cards module's last attention report, for the app to reconcile its own count against."""
    try:
        rows = con.execute("SELECT board, count, decisions, tasks FROM attention").fetchall()
    except sqlite3.Error:
        return {"count": 0, "decisions": 0, "tasks": []}
    tasks = []
    for row in rows:
        try:
            tasks.extend(json.loads(row["tasks"]))
        except (TypeError, ValueError):
            pass
    return {"count": sum(r["count"] for r in rows), "decisions": sum(r["decisions"] for r in rows), "tasks": tasks}


def store_attention(con, report):
    """Keep the report (``{board, count, decisions, tasks}``); return ``(changed, resolved)``: whether anything the
    badge or the alerts care about moved, and the task ids that stopped needing the person since the last report."""
    before, previous = set(), None
    board = report.get("board") or ""
    row = con.execute("SELECT count, decisions, tasks FROM attention WHERE board=?", (board,)).fetchone()
    if row:
        try:
            before = {t["id"] for t in json.loads(row["tasks"])}
            previous = (row["count"], row["decisions"], json.loads(row["tasks"]))
        except (TypeError, ValueError, KeyError):
            before = set()
    tasks = report["tasks"]
    con.execute("INSERT OR REPLACE INTO attention(board,count,decisions,tasks,updated) VALUES(?,?,?,?,?)",
                (board, report["count"], report["decisions"], json.dumps(tasks), time.time()))
    con.commit()
    changed = previous != (report["count"], report["decisions"], tasks)
    return changed, sorted(before - {t["id"] for t in tasks})


def unread_for(con, owner):
    """[(profile, chat, group)] unread for this owner, in every group but archived. A scheduled run is never
    unread (Mark, 2026-10-29): Scheduled shows that a job ran, its result reaches the main chat. The first look
    at a profile sets a baseline, so history from before sync existed is not flagged."""
    result = []
    for profile, path in replies.sources():
        try:
            latest = replies.visible_replies(profile, path)
        except sqlite3.Error:
            continue  # one bot's unreadable database never hides the others' unread chats
        index = replies.index_for(profile, path)
        base = con.execute("SELECT row_id FROM baselines WHERE owner=? AND profile=?", (owner, profile)).fetchone()
        if base is None:
            base = (replies.newest_id(path),)
            con.execute("INSERT INTO baselines VALUES (?,?,?)", (owner, profile, base[0]))
            con.commit()
        reads = {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT chat, read_id, unread FROM reads WHERE owner=? AND profile=?", (owner, profile))}
        for chat, rid in latest.items():
            read_id, manual = reads.get(chat, (0, 0))
            if (manual or rid > max(base[0], read_id or 0)) and (group := index.group(chat)) != "scheduled":
                result.append((profile, chat, group))
    return result


def badge_for(con, owner, device_id, unread=None):
    """The number the app icon shows: unread chats this device's inbox lists, plus every decision and
    board task that needs the person (the inbox's Review rows), so the badge and the rows agree."""
    view = view_for(con, device_id)
    unread = unread_for(con, owner) if unread is None else unread
    return sum(1 for profile, chat, group in unread if in_view(view, profile, group)) + attention_count(con)


def read_marks(con, owner):
    """Where each chat was last read, by any device: the newer of its read report and the profile's
    baseline. The app opens an unread chat at the first reply after it ("Since you last read")."""
    bases = {r[0]: r[1] for r in con.execute("SELECT profile, row_id FROM baselines WHERE owner=?", (owner,))}
    marks = {(r[0], r[1]): r[2] or 0 for r in con.execute("SELECT profile, chat, read_id FROM reads WHERE owner=?", (owner,))}
    return lambda profile, chat: max(marks.get((profile, chat), 0), bases.get(profile) or 0)


def record_read(con, owner, profile, chat, unread, through):
    """A device read a chat (``unread`` False: record the profile's message id it showed, so every reply up to there
    counts as seen) or marked it unread (``unread`` True)."""
    if unread:
        con.execute("""INSERT INTO reads(owner,profile,chat,read_id,unread,updated) VALUES(?,?,?,0,1,?)
            ON CONFLICT(owner,profile,chat) DO UPDATE SET unread=1, updated=excluded.updated""",
                    (owner, profile, chat, time.time()))
    else:
        con.execute("""INSERT INTO reads(owner,profile,chat,read_id,unread,updated) VALUES(?,?,?,?,0,?)
            ON CONFLICT(owner,profile,chat) DO UPDATE SET read_id=MAX(read_id, excluded.read_id), unread=0,
            updated=excluded.updated""", (owner, profile, chat, through, time.time()))
    con.commit()
