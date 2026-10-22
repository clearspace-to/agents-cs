"""The scan: new person-facing replies in the root home's and every profile's ``state.db`` become sealed outbox rows,
one per device whose inbox filter lists the chat.

Runs in the Agents CS connector (outside Hermes; ``home.use_root`` names the root home) every 2 s and at once on a
nudge, just before it drains the outbox. The first look at a profile only records
where it is (a cursor), so starting never floods old replies. A scheduled run's alert is first ``held``: the run's
result rings once, from the main chat, so the run's own alert waits while Hermes hands the result over and is dropped
once it got there (``replies.run_handoff``); it rings only when the hand-off didn't happen.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import closing

from . import alerts, outbox, replies, state

log = logging.getLogger("agents_cs.mobile")

RECENT = 86400  # a held alert older than this is stale


def scan(con, profile, path, now=None) -> int:
    """Queue an alert per device for each new person-facing reply of one profile. Returns how many rows were written."""
    now = time.time() if now is None else now
    index = replies.index_for(profile, path)
    with index.lock:
        index.update()
        new = index.take()
        through = index.last  # Badge/read refreshes may advance the shared index while this batch queues.
    cursor = con.execute("SELECT row_id FROM cursors WHERE profile=?", (profile,)).fetchone()
    if cursor is None:
        con.execute("INSERT INTO cursors VALUES (?,?)", (profile, through))
        return 0
    written = 0
    recipients = alerts.recipients()
    views = {device.id: state.view_for(con, device.id) for device in recipients}
    unread = None
    for rid, chat, group in new:
        if rid <= cursor[0]:
            continue
        text = None
        for device in recipients:
            if not replies.in_view(views[device.id], profile, group):
                continue  # filtered out on that device: no alert there, as no dot or badge
            if group == "scheduled":
                alerts.hold_reply(con, device, profile, chat, rid, group, now=now)
                written += 1
                continue
            if text is None:
                text = replies.reply_text(profile, rid)
            if unread is None:
                unread = state.unread_for(con, state.OWNER)
            written += alerts.queue_reply(con, device, profile, chat, rid, text,
                                          state.badge_for(con, state.OWNER, device.id, unread), created=now)
    if through > cursor[0]:
        con.execute("UPDATE cursors SET row_id=? WHERE profile=?", (through, profile))
    return written


def release_held(con, now=None) -> int:
    """Decide the held scheduled-run alerts: queue the ones that should ring, drop the rest. Returns how many were queued."""
    now = time.time() if now is None else now
    rows = con.execute("SELECT id, device_id, created, payload_json FROM outbox WHERE state=? ORDER BY created",
                       (outbox.HELD,)).fetchall()
    if not rows:
        return 0
    known = {device.id: device for device in alerts.recipients()}
    if not known:
        return 0  # no devices or no sender key right now: keep them held (the 24 h limit below still applies on return)
    released, unread = 0, None
    for row in rows:
        try:
            hold = json.loads(row["payload_json"])["hold"]
            profile, chat, rid = hold["profile"], hold["chat"], hold["row_id"]
        except (ValueError, KeyError, TypeError):
            con.execute("DELETE FROM outbox WHERE id=?", (row["id"],))
            continue
        device, age = known.get(row["device_id"]), now - row["created"]
        if device is None or age > RECENT:
            con.execute("DELETE FROM outbox WHERE id=?", (row["id"],))  # its device is gone, or the alert is stale
            continue
        if age < replies.SCHEDULED_HOLD:
            continue
        index = replies.known_index(profile)
        group = index.group(chat) if index is not None else None
        if group == "archived":
            con.execute("DELETE FROM outbox WHERE id=?", (row["id"],))  # an archived chat never alerts: read elsewhere
            continue
        stuck = False
        if group == "scheduled":
            handoff = replies.run_handoff(index.path, chat)
            if handoff == "delivered":
                con.execute("DELETE FROM outbox WHERE id=?", (row["id"],))  # the main chat rang for it
                continue
            if handoff == "waiting" and age < replies.SCHEDULED_MAX_WAIT:
                continue  # looked at again on the next scan
            stuck = handoff == "waiting"
        text = replies.reply_text(profile, rid)
        if unread is None:
            unread = state.unread_for(con, state.OWNER)
        if alerts.queue_reply(con, device, profile, chat, rid, text, state.badge_for(con, state.OWNER, device.id, unread),
                              stuck=stuck, created=row["created"], replace_held=True):
            released += 1
    return released


def cycle(now=None) -> None:
    """One pass: scan every source, then settle the held alerts. Each source is isolated: one unreadable
    database never stops the others."""
    ob = alerts.store()
    try:
        with closing(ob.connect()) as con, con:
            for profile, path in replies.sources():
                try:
                    scan(con, profile, path, now=now)
                except sqlite3.Error as error:
                    replies.invalidate_index(profile)
                    log.warning("agents-cs mobile: scan %s failed: %s: %s", profile, type(error).__name__, error)
            con.commit()
            release_held(con, now=now)
    except Exception:
        # A failed commit rolls back the cursor and rows together; in-memory candidates must roll back too.
        replies.invalidate_index()
        raise
