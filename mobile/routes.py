"""The ``mobile`` module's router, mounted by ``dashboard/plugin_api.py`` under ``/api/plugins/hermes-bots/`` behind
Hermes's own dashboard authentication (never expose it as a standalone API):

    GET  mobile/reads?all=       the owner's unread chats, this device's badge and the attention summary
    PUT  mobile/reads            {profile, chat, unread, through?}: a device read a chat, or marked it unread
    GET  mobile/replies?profile= the newest person-facing reply id per chat (identities only, no text)
    GET  mobile/kinds?profile=&session=   which Show in Chats switch each message of a chat belongs to
    PUT  mobile/view             {groups, bots}: this device's inbox filter (its badge counts only those)
    GET  mobile/usage?period=    API usage reports (usage.py)

The device is the hub's device id in ``X-Hermes-Bots-Device`` (the hub sets it from the access token and strips any
client-sent copy). The owner is Hermes's session user when the dashboard has one (``request.state.session``), else the
computer's one owner (``state.OWNER``): in the loopback mode the connector requires, there is no per-person login.
There is no device registration route: devices and their keys come from the connector (``devices.py``).
"""
import re
import sqlite3
import time
from contextlib import closing
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import alerts, home, replies, state, usage

router = APIRouter(prefix="/mobile")

DEVICE_HEADER = "x-hermes-bots-device"
_DEVICE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def device_of(request: Request):
    """The hub's device id for this request, or ``None`` (a request that didn't come through the hub)."""
    value = request.headers.get(DEVICE_HEADER, "")
    return value if _DEVICE.fullmatch(value) else None


def owner_of(request: Request) -> str:
    """Hermes's session user when there is one, else the computer's one owner."""
    session = getattr(request.state, "session", None)
    user = getattr(session, "user_id", None)
    return user if isinstance(user, str) and user and len(user) <= 512 else state.OWNER


def _connect():
    return closing(alerts.store().connect())


@router.get("/usage")
def api_usage(period: Literal["today", "week", "month", "last7"] = "today"):
    return usage.report(home.root(), period, time.time(), replies.display_name)


@router.get("/replies")
def snapshot(profile: str):
    path = dict(replies.sources()).get(profile)
    if path is None or not path.exists():
        raise HTTPException(404, "Profile not found")
    # Durable identities only, never content, by the same definition as alerts and unread.
    try:
        found = replies.visible_replies(profile, path)
    except sqlite3.Error:
        raise HTTPException(503, "The bot's database can't be read right now")
    return {"replies": [{"session_id": chat, "id": rid} for chat, rid in found.items()]}


@router.get("/reads")
def reads(request: Request, all: bool = False):
    """The owner's unread chats and this device's badge. Builds before the filter sheet count the
    list for their badge, so without ``all=1`` it holds only the chats the device's view lists."""
    owner, device = owner_of(request), device_of(request)
    try:
        with _connect() as con, con:
            unread = state.unread_for(con, owner)
            view = state.view_for(con, device)
            listed = [u for u in unread if replies.in_view(view, u[0], u[2])]
            attention = state.attention_state(con)
            through = state.read_marks(con, owner)
    except sqlite3.Error:
        raise HTTPException(503, "Read state is unavailable right now")
    # "through": where reading stopped, so the app opens the chat at "Since you last read" (2026-10-11).
    return {"unread": [{"profile": p, "chat": c, "group": g, "through": through(p, c)}
                       for p, c, g in (unread if all else listed)],
            "count": len(listed) + attention["count"], "attention": attention}


class ReadChange(BaseModel):
    profile: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    chat: str = Field(min_length=1, max_length=200)
    unread: bool
    through: Optional[int] = Field(default=None, ge=0, le=2**62)  # newest message the device showed


@router.put("/reads")
def set_read(change: ReadChange, request: Request):
    """A device read a chat, or marked it unread. Reading records the profile's newest message id,
    so every reply up to now counts as seen. Other devices are told at once."""
    owner, device = owner_of(request), device_of(request)
    path = dict(replies.sources()).get(change.profile)
    if path is None:
        raise HTTPException(404, "Profile not found")
    try:
        through = change.through
        if through is None:  # older apps send nothing: then the chat's newest reply
            through = replies.visible_replies(change.profile, path).get(change.chat, 0)
        with _connect() as con, con:
            state.record_read(con, owner, change.profile, change.chat, change.unread, through)
            unread = state.unread_for(con, owner)
            alerts.push_to_owner(con, owner, device, [{"profile": change.profile, "chat": change.chat, "unread": change.unread}],
                                 unread)
            count = state.badge_for(con, owner, device, unread)
    except sqlite3.Error:
        raise HTTPException(503, "Read state is unavailable right now")
    return {"ok": True, "count": count}


@router.get("/kinds")
def kinds(profile: str, session: str):
    """Which Show in Chats switch each message of a chat belongs to: {row_id: kind}. Rows the person
    wrote, and the bot's replies to them, are absent. No message text leaves in the response."""
    path = dict(replies.sources()).get(profile)
    if path is None or not path.exists():
        raise HTTPException(404, "Profile not found")
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as source:
            source.row_factory = sqlite3.Row
            ids = sorted(replies.lineage(source, session))
            rows = source.execute(
                "SELECT id, role, content, display_kind, tool_calls FROM messages WHERE session_id IN (%s) "
                "AND role IN ('user','assistant') ORDER BY id" % ",".join("?" * len(ids)), ids).fetchall()
            origin = source.execute("SELECT source FROM sessions WHERE id=?", (session,)).fetchone()
    except sqlite3.Error:
        raise HTTPException(503, "The bot's database can't be read right now")
    autonomous = bool(origin) and (origin[0] or "") in replies.AUTONOMOUS_SOURCES
    return {"version": replies.KINDS_VERSION,
            "kinds": {str(rid): kind for rid, kind in replies.message_kinds(rows, autonomous).items()}}


class View(BaseModel):
    groups: list[str] = Field(max_length=len(replies.GROUPS) + 1)
    bots: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]] = Field(default_factory=list, max_length=64)


@router.put("/view")
def set_view(view: View, request: Request):
    """This device's inbox filter: which chat groups and bots it lists. Its badge counts only those."""
    owner, device = owner_of(request), device_of(request)
    if device is None:
        raise HTTPException(400, "Missing X-Hermes-Bots-Device")
    groups = set(view.groups) & replies.GROUPS
    try:
        with _connect() as con, con:
            state.set_view(con, device, groups, set(view.bots))
            count = state.badge_for(con, owner, device)
            alerts.store().enqueue(device, "alert", alerts.named({"aps": {"badge": count}}), priority=5,
                                   expiration=time.time() + alerts.EVENT_EXPIRY, con=con)
            con.commit()
    except sqlite3.Error:
        raise HTTPException(503, "Read state is unavailable right now")
    return {"ok": True, "count": count}


def on_mount() -> None:
    """Called by ``dashboard/plugin_api.py`` once these routes are mounted in ``hermes serve``: start the scan worker
    here when ``register(ctx)`` couldn't (no event loop yet). Does nothing outside the root home."""
    from . import worker
    worker.start()
