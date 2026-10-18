"""Turning an event into sealed outbox rows: one row per device, each sealed for that device and signed with the
computer's sender key (``sealed.py``), written to ``delivery.db`` for the connector to send.

Used by ``scan.py`` (finished replies) and ``events.py`` (approvals, questions, board tasks, badges). Nothing here
sends anything or knows a push token: it addresses device ids from the connector's ``devices.json``. With no devices,
or no readable sender key, nothing is written (and ``devices.py`` says why once).
"""
from __future__ import annotations

import logging
import re
import time
import uuid

from . import devices, home, outbox, replies, sealed, state

log = logging.getLogger("hermes_bots.mobile")

# What Apple sees on a sealed alert. The extension replaces it, and names the bot itself even when it
# can't open one, so a failure shows "<Bot>: New message", never the reply.
REPLY_PLACEHOLDER = {"title": "Hermes Bots", "body": "New message"}
APPROVAL_PLACEHOLDER = {"title": "Approval needed", "body": "Open Hermes Bots to review the request."}
QUESTION_PLACEHOLDER = {"title": "Question", "body": "Open Hermes Bots to answer."}
TASK_PLACEHOLDER = {"title": "A task needs you", "body": "Open Hermes Bots to review it."}

REPLY_EXPIRY = 86400  # a reply alert is useless after a day
EVENT_EXPIRY = 3600   # approvals, questions, tasks and badge updates after an hour


def named(payload: dict) -> dict:
    """The payload with ``computer: <cmp id>`` added (top level, outside the sealed part) when this computer's id is
    known: the app places an alert, and a silent push most of all, by it."""
    computer = home.computer_id()
    return {**payload, "computer": computer} if computer else payload


def store() -> outbox.Outbox:
    return outbox.Outbox(home.delivery_path())


def recipients() -> list[devices.Device]:
    """The devices to alert, or ``[]`` when alerts can't be signed (no sender key)."""
    found = devices.devices()
    if found and devices.sender_key() is None:
        return []
    return found


def seal_for(device: devices.Device, payload: dict, content: dict, event_id: str, placeholder: dict) -> dict:
    return sealed.seal_payload(payload, content, device_id=device.id, device_key=device.recipient_public_key,
                               sender=devices.sender_key, event_id=event_id, placeholder=placeholder, log=log.warning)


def push_all(con, aps, extra, push_type="alert", priority=10, collapse=None, content=None,
             placeholder=APPROVAL_PLACEHOLDER, now=None) -> int:
    """Queue ``{"aps": aps, **extra}`` for every device (sealed when ``content`` is given, as is for a silent push).
    Returns how many rows were written."""
    now = time.time() if now is None else now
    count = 0
    for device in recipients():
        payload = named({"aps": aps, **extra})
        if content:
            payload = seal_for(device, payload, content, str(uuid.uuid4()), placeholder)
        if store().enqueue(device.id, push_type, payload, priority=priority, collapse_id=collapse,
                           expiration=now + EVENT_EXPIRY, con=con, now=now):
            count += 1
    return count


def push_to_owner(con, owner, exclude, changed, unread=None, now=None) -> int:
    """Badge update and a silent note to the owner's other devices, so each clears the alerts for chats read
    elsewhere. Best effort: iOS may hold a silent push back; the app catches up on open."""
    now = time.time() if now is None else now
    unread = state.unread_for(con, owner) if unread is None else unread
    count = 0
    for device in recipients():
        badge = state.badge_for(con, owner, device.id, unread)
        for push_type, body in (("alert", named({"aps": {"badge": badge}})),
                                ("background", named({"aps": {"content-available": 1},
                                                      "hermes": {"reads": changed, "badge": badge}}))):
            if push_type == "background" and device.id == (exclude or ""):
                continue  # The reader still publishes its badge, but needs no read echo.
            if store().enqueue(device.id, push_type, body, priority=5, expiration=now + EVENT_EXPIRY, con=con, now=now):
                count += 1
    return count


def reply_identity(device_id: str, profile: str, row_id) -> str:
    """The id of one reply's alert for one device: the outbox row id, APNs collapse id and sealing event id, so the
    same reply is never alerted twice to the same device."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, ":".join((device_id, profile, str(row_id)))))


def hold_reply(con, device: devices.Device, profile: str, chat: str, row_id, group: str, now=None) -> None:
    """A scheduled run's alert waits as a ``held`` row (no content yet); ``scan.release_held`` decides later."""
    store().enqueue(device.id, "alert", {"hold": {"profile": profile, "chat": chat, "row_id": row_id, "group": group}},
                    id=reply_identity(device.id, profile, row_id), state=outbox.HELD, con=con, now=now)


def queue_reply(con, device: devices.Device, profile: str, chat: str, row_id, text, badge: int, *, stuck: bool = False,
                created=None, replace_held: bool = False) -> bool:
    """Seal and queue one finished reply for one device (the banner: the bot's name and a preview)."""
    created = time.time() if created is None else created
    identity = reply_identity(device.id, profile, row_id)
    bot = replies.display_name(profile)
    body = (replies.STUCK_NOTE if stuck else "") + replies.preview(text)
    # category "reply": the app's long-press actions (Reply, Mark as Read), as in Messages.
    aps = {"alert": {"title": bot, "body": body}, "thread-id": profile + ":" + chat, "mutable-content": 1,
           "category": "reply", "badge": badge, "sound": "default"}
    payload = sealed.with_full_text(named({"aps": aps, "bot": profile, "chat": chat, "reply": str(row_id)}), text)
    content = {"type": "reply", "title": bot, "body": body}
    if text and text.strip():
        content["text"] = text
    payload = seal_for(device, payload, content, identity, REPLY_PLACEHOLDER)
    ob = store()
    if replace_held:
        con.execute("DELETE FROM outbox WHERE id=? AND state=?", (identity, outbox.HELD))
    return bool(ob.enqueue(device.id, "alert", payload, priority=10, collapse_id=identity,
                           expiration=created + REPLY_EXPIRY, id=identity, con=con, now=created))


_PROFILE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def valid_profile(value) -> bool:
    return isinstance(value, str) and bool(_PROFILE.fullmatch(value))
