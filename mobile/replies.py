"""What counts as a reply the person sees, which chat group it falls in, and the bots' databases it is read from.

Read-only over each bot's ``state.db`` (root home as ``default`` plus every profile), plus the scheduled-run
hand-off check (Hermes's ``cron/executions.db`` and the cards module's relay ledger). Nothing here writes, sends
or touches the delivery database: ``state.py`` (read state and badges), ``scan.py`` (new replies become alerts) and
``routes.py`` (the app's questions) build on it. Moved from the push bridge's ``server.py`` (until 2.0
``scripts/mobile-push``); the rules are unchanged.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import threading
import time

from . import cron_runs, home
from .sealed import PREVIEW_LIMIT, preview  # noqa: F401  the banner preview, shared with the Claude Code host


def sources():
    """``[(profile, state.db)]`` for the root home (``default``) and every profile."""
    return home.sources()


# --- What counts as a reply the person sees (the one definition) --------------------------------
# A finished, active assistant turn with text, not hidden, that Show in Chats gives no kind
# (assistant_kind: not silent, not a tool step, not an answer to a machine message). The same
# function labels messages for the app (GET /mobile/kinds) and decides unread, the badge and alerts,
# so what the app shows as a reply and what notifies can't drift apart.
#
# Each chat also falls in one group, the app's inbox filter (ChatKind), checked in this order:
# archived, never (worker sessions), hidden, then by source. Unread covers every group but archived
# and never; the badge and alerts both follow the groups and bots each device lists (PUT /mobile/view),
# so a filtered-out chat neither rings nor counts. Bot Chats always do.

# --- Message kinds: the one place the app's Show in Chats switches are decided ---------------------
# Hermes writes several kinds of message into the person's seat. It labels some (display_kind); the
# rest are recognised by the fixed words they start with. GET /mobile/kinds hands the result to the
# app, so these rules exist only here. Kind names are the app's MessageKind raw values.
DISPLAY_KINDS = {"hidden": "notes", "model_switch": "notes", "auto_continue": "notes",
                 "personality_switch": "notes", "internal_notification": "notes",
                 "async_delegation_complete": "background", "process_complete": "background"}
PROMPT_OPENINGS = [
    # gateway/kanban_watchers_notifier.py posts these status pings in the user's seat.
    # Require its icon, optional board/assignee, task ID and event wording; ordinary
    # questions about Kanban must stay visible. Answers inherit the same machine kind.
    (re.compile(r"^\s*[✔⏸⛔✖⏱🔄👀🛑]\ufe0f?\s+"
                r"(?:\[[^\]\r\n]+\]\s+)?(?:@[^\s]+\s+)?Kanban t_[A-Za-z0-9_-]+\s+"
                r"(?:done —|blocked(?=:|\s|$)|is now blocked:|— its worker stopped unexpectedly;|"
                r"ran past |→|ready for review —|review requested changes/BLOCK:|routed to TRIAGE —)"),
     "notes"),
    (re.compile(r"^\s*\[(?:System|SYSTEM)(?: note| NOTE)?:"), "notes"),
    # Hermes' own bookkeeping after context compaction and across a preserved task list.
    (re.compile(r"^\s*\[(?:CONTEXT COMPACTION|STILL IN PROGRESS|Your active task list was preserved)"), "notes"),
    (re.compile(r"^\s*Message from \U0001F916 "), "bots"),
    (re.compile(r"^\s*\[Cronjob \""), "cron"),
    # What a scheduled job hands the bot in its own run's chat (cron/scheduler_prompt.py).
    (re.compile(r"^\s*\[IMPORTANT: You are running as a scheduled cron job"), "cron"),
    (re.compile(r"^\s*(?:\[IMPORTANT: Background process |\[ASYNC [^\]]{0,40}COMPLETE|"
                r"A background fan-out of \d+ subagent|A background subagent you dispatched earlier has finished\.)"),
     "background"),
]
KINDS_VERSION = 1
NEVER_SOURCES = {"kanban", "subagent", "tool", "delegate", "integration", "api"}
MESSAGING_SOURCES = {"telegram", "discord", "slack", "mattermost", "matrix", "signal", "whatsapp", "bluebubbles",
                     "photon", "homeassistant", "email", "sms", "webhook", "api_server", "weixin", "wecom", "qqbot",
                     "yuanbao", "dingtalk", "feishu"}
GROUPS = {"chats", "hidden", "messaging", "scheduled", "oneshot", "archived"}
# Each bot's permanent Bot Chat is born hidden (Hermes keeps it out of the desktop's lists), but the app
# always lists it as the bot's avatar: its own group, alerted and badged whatever the device's view.
BOT_CHAT = "botchat"
BOT_CHAT_TITLE = "Bot Chat"
UNREAD_GROUPS = (GROUPS | {BOT_CHAT}) - {"archived"}
# A device that never sent its filter: the app's default, Chats and Hidden on Desktop (plus Bot Chats).
DEFAULT_VIEW = (frozenset({"chats", "hidden", BOT_CHAT}), frozenset())


def in_view(view, profile, group):
    """Whether a device lists this chat: its filter's groups and bots. Bot Chats always. Badges and
    alerts both follow it, so a device is never pinged for a chat its inbox doesn't show."""
    groups, bots = view
    return group == BOT_CHAT or (group in groups and (not bots or profile in bots))


def chat_group(source, hidden, archived, bot_chat=False, delegate=False):
    """The inbox filter group a session belongs to; None for worker sessions nobody reads."""
    source = source or ""
    if archived:
        return "archived"
    # A background helper a chat dispatched (Hermes marks it ``_delegate_from``; its source is the parent's, so
    # Hermes's own lists hide it by the marker). Its answers go to the bot that sent it, which tells the person
    # in its own chat. Counted as unread it became an extra inbox row that opened as the Bot Chat (2026-10-23).
    if delegate:
        return None
    if bot_chat:
        return BOT_CHAT
    if source in NEVER_SOURCES:
        return None
    if hidden:
        # The app lists hidden chats of the person's own kinds only; a hidden scheduled, one-shot or
        # messaging chat is in none of its lists, so it can't be unread anywhere either.
        return None if source in ("cron", "oneshot") or source in MESSAGING_SOURCES else "hidden"
    if source == "cron":
        return "scheduled"
    if source == "oneshot":
        return "oneshot"
    if source in MESSAGING_SOURCES:
        return "messaging"
    return "chats"
SILENCE = {"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY", "[静默]", "静默", "[沉默]", "沉默"}
_EDGE = re.compile(r"^[^\w\[\]]+|[^\w\[\]]+$")


_NOTE_OPENING = re.compile(r"\[(?:System|SYSTEM)(?: note| NOTE)?:")


def person_text(content):
    """What the person wrote in a row that opens with Hermes notes, or None when it's only notes.

    When Hermes rebuilds a chat's history (after context compaction) it merges consecutive rows in
    the person's seat, so a request typed after a model switch arrives as "[System: …]" notes then
    the request. Notes are matched by balanced brackets ("[Coding]" may sit inside one); what follows
    the last leading note, after a blank line, is the person's."""
    text = (content or "").lstrip()
    found = False
    while _NOTE_OPENING.match(text):
        depth = 0
        for index, character in enumerate(text):
            depth += character == "["
            depth -= character == "]"
            if depth == 0:
                break
        else:
            return None  # An unclosed note: all of it is Hermes'.
        rest = text[index + 1:]
        if rest.strip() and not re.match(r"[ \t]*\n[ \t]*\n", rest):
            return None  # Text running on from the note is part of it.
        text, found = rest.lstrip(), True
    return text if found and text else None


def prompt_kind(content, display_kind):
    """The Show in Chats kind of a message in the person's seat; None when the person wrote it."""
    rest = person_text(content)
    if rest:
        # Merged rows: what follows the notes decides (usually the person; possibly a job report).
        return prompt_kind(rest, None)
    if display_kind in DISPLAY_KINDS:
        return DISPLAY_KINDS[display_kind]
    for pattern, kind in PROMPT_OPENINGS:
        if pattern.match(content or ""):
            return kind
    return None


def answers(content, display_kind):
    """The kind a bot's answer to this prompt inherits. A crash-recovery note (auto_continue) carries
    the person's own interrupted request, so its answer is a reply to the person, not a Hermes note."""
    return None if display_kind == "auto_continue" else prompt_kind(content, display_kind)


def has_tool_calls(value):
    return value not in (None, "", "[]", "null")


def calls_card(value):
    """Whether a row's tool calls include one of our card tools (``hermes_card_*``)."""
    if not has_tool_calls(value):
        return False
    try:
        calls = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return False
    for call in calls if isinstance(calls, list) else []:
        function = call.get("function") if isinstance(call, dict) else None
        name = (function or {}).get("name") if isinstance(function, dict) else (call or {}).get("name") if isinstance(call, dict) else None
        if isinstance(name, str) and name.startswith("hermes_card_"):
            return True
    return False


def assistant_kind(row, answering, autonomous=False):
    """The Show in Chats kind of a bot message, given the kind of the prompt it answers (None when
    the person wrote it); None for a reply to the person. A silence marker is silent (Hermes' looser
    rule in scheduled and webhook chats). An answer to a machine message, tool notes included, takes
    that message's kind; otherwise a message beside a tool call is a tool step, except text beside a card
    call, which is the answer. A job report's answer
    is a job summary, and its tool notes stay tool steps."""
    if (is_autonomous_silence if autonomous else is_silence)(row["content"]):
        return "silent"
    if answering and answering != "cron":
        return answering
    if has_tool_calls(row["tool_calls"]):
        # Text written just before a card is the answer the card goes with, not a note about a tool:
        # show it even when the bot ignored "put the whole answer in your final message" (Mark, 2026-10-18).
        if calls_card(row["tool_calls"]) and (row["content"] or "").strip():
            return None
        return "tools"
    if answering == "cron":
        return "summary"
    return None


# Kinds that still count as a reply to the person: a job's result is what the job exists for, so it
# rings, dots and badges like one (Mark, 2026-09-26). It is always shown, never behind a switch.
REPLY_KINDS = {None, "summary"}
AUTONOMOUS_SOURCES = {"cron", "webhook"}


# Hermes's verify-on-stop gate (agent/turn_stop_gates.py) stores an answer as provisional, nudges the bot to check
# its work, and lets whatever it says next stand as the reply, a silence marker included: Friday's answer to "do
# these dates work" was dropped for a [SILENT] (26 answers in a month, 2026-10-27). The next reply supersedes a
# provisional answer, as Hermes and the live app treat it; silence, or no reply before the next prompt, keeps it.
PROVISIONAL = {"verification_required", "verify_hook_continue"}
PROVISIONAL_REACH = 1000  # rows of a bot's database after which an open turn's answer stops holding the alert cursor


def finish(row):
    """A row's finish_reason; None for a row read without it."""
    return row["finish_reason"] if "finish_reason" in row.keys() else None


def provisional_answer(row):
    """A provisional answer with text, not a silence marker or a note beside a tool call."""
    return (row["role"] == "assistant" and finish(row) in PROVISIONAL
            and bool((row["content"] or "").strip()) and not has_tool_calls(row["tool_calls"])
            and not is_silence(row["content"]))


def message_kinds(rows, autonomous=False):
    """{row_id: kind} for one chat's rows in order. The person's own messages and the bot's replies
    to them get no kind."""
    return {rid: kind for rid, kind in label_rows(rows, autonomous)[0].items() if kind}


def label_rows(rows, autonomous=False, answering=None, provisional=None):
    """``message_kinds`` that can carry on: starts from the kind the last prompt before ``rows`` passed on
    (``answering``) and the provisional answer still waiting for its turn's end, and returns ``(kinds, answering,
    provisional)`` after the last row, so a cache labels each row once. A provisional answer is a tool step until
    the turn ends in silence; then ``kinds`` maps it to None, a reply again, which a cache must apply."""
    kinds = {}
    for row in rows:
        if row["role"] == "user":
            if provisional is not None:
                kinds[provisional] = None
            provisional = None
            kind = prompt_kind(row["content"], row["display_kind"])
            answering = answers(row["content"], row["display_kind"])
        elif row["role"] == "assistant":
            kind = assistant_kind(row, answering, autonomous)
            if kind is None and provisional_answer(row):
                kind, provisional = "tools", row["id"]
            elif provisional is not None and finish(row) == "stop":
                if kind == "silent":
                    kinds[provisional] = None
                provisional = None
        else:
            continue
        if kind:
            kinds[row["id"]] = kind
    return kinds, answering, provisional


def lineage(source, session):
    """The session plus its compression ancestors and continuations: the rows its history shows."""
    ids, frontier = {session}, [session]
    parent = source.execute("SELECT parent_session_id FROM sessions WHERE id=?", (session,)).fetchone()
    while parent and parent[0] and parent[0] not in ids:
        ids.add(parent[0])
        parent = source.execute("SELECT parent_session_id FROM sessions WHERE id=?", (parent[0],)).fetchone()
    while frontier:
        children = [r[0] for r in source.execute(
            "SELECT id FROM sessions WHERE parent_session_id IN (%s)" % ",".join("?" * len(frontier)), frontier)]
        frontier = [c for c in children if c not in ids]
        ids.update(frontier)
    return ids


# GET /mobile/kinds labels a chat once (3.2.0): each call reads only rows newer than the last labelled, carrying the
# answering state forward. The app asks on every chat open and every history window, for every row of the lineage.
KINDS_CACHE_SIZE = 64
_kinds = OrderedDict()   # (profile, session) -> {ids, last, answering, autonomous, kinds}; oldest first
_kinds_lock = threading.Lock()


def kinds_for(profile, source, session):
    """{row_id: kind} for a chat, across its compression lineage, from the cache plus the rows since. A lineage that
    changed (a continuation appeared) starts over, since the carried state would be wrong for the new segment."""
    ids = lineage(source, session)
    key = (profile, session)
    with _kinds_lock:
        entry = _kinds.get(key)
        if entry is None or entry["ids"] != ids:
            origin = source.execute("SELECT source FROM sessions WHERE id=?", (session,)).fetchone()
            entry = {"ids": ids, "last": 0, "answering": None, "provisional": None, "kinds": {},
                     "autonomous": bool(origin) and (origin[0] or "") in AUTONOMOUS_SOURCES}
        ordered = sorted(ids)
        rows = source.execute(
            "SELECT id, role, content, display_kind, tool_calls, finish_reason FROM messages WHERE session_id IN (%s) "
            "AND role IN ('user','assistant') AND id>? ORDER BY id" % ",".join("?" * len(ordered)),
            [*ordered, entry["last"]]).fetchall()
        if rows:
            new, entry["answering"], entry["provisional"] = label_rows(
                rows, entry["autonomous"], entry["answering"], entry["provisional"])
            for rid, kind in new.items():
                if kind:
                    entry["kinds"][rid] = kind
                else:
                    entry["kinds"].pop(rid, None)
            entry["last"] = rows[-1]["id"]
        _kinds[key] = entry
        _kinds.move_to_end(key)
        while len(_kinds) > KINDS_CACHE_SIZE:
            _kinds.popitem(last=False)
        return dict(entry["kinds"])


def forget_kinds():
    with _kinds_lock:
        _kinds.clear()


def is_silence(text):
    """Mirrors Hermes' is_intentional_silence_response, as the app does."""
    stripped = (text or "").strip()
    if not stripped or len(stripped) > 64:
        return False
    forms = {stripped, _EDGE.sub("", stripped).strip()}
    return any(" ".join(f.split()).upper() in SILENCE for f in forms)


def is_autonomous_silence(text):
    """Mirrors Hermes' is_autonomous_silence_response (cron and webhook lanes): also a marker on its
    own first or last line, or a bracketed marker opening the reply ("[SILENT] No changes detected")."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    lines = [line for line in stripped.splitlines() if line.strip()]
    bracketed = tuple(m for m in SILENCE if m.startswith("["))
    return stripped.upper().startswith(bracketed) or any(is_silence(c) for c in (stripped, lines[0], lines[-1]))


def is_reply(row, answering=None, autonomous=False):
    """A reply to the person: a finished, active bot message with text, not hidden, that Show in Chats
    always shows (no kind, or a job summary). `answering` is the kind its prompt passes on."""
    return (row["role"] == "assistant" and row["finish_reason"] == "stop"
            and bool((row["content"] or "").strip())
            and (row["display_kind"] or "") != "hidden" and (row["active"] if row["active"] is not None else 1) == 1
            and assistant_kind(row, answering, autonomous) in REPLY_KINDS)


SESSION_COLUMNS = ("id", "source", "hidden", "archived", "title", "parent_session_id", "end_reason", "ended_at",
                   "started_at", "model_config")


def compression_tips(sessions):
    """{session_id: its compression chain's live tip}, the way Hermes lists a compressed chat: a
    continuation is a child of a session that ended for compression, not a branch, delegate or tool
    child; a live child wins over a closed one, then the newest."""
    by_id = {s["id"]: s for s in sessions}
    children = {}
    for s in sessions:
        parent = by_id.get(s.get("parent_session_id"))
        config = s.get("model_config") or ""
        if (parent and parent.get("end_reason") == "compression" and (s.get("source") or "") != "tool"
                and "_branched_from" not in config and "_delegate_from" not in config):
            children.setdefault(parent["id"], []).append(s)
    def next_of(sid):
        options = children.get(sid) or []
        return max(options, key=lambda c: (c.get("ended_at") is None, c.get("started_at") or 0))["id"] if options else None
    tips = {}
    for s in sessions:
        current, seen = s["id"], {s["id"]}
        while (child := next_of(current)) and child not in seen:
            seen.add(child)
            current = child
        if current != s["id"]:
            tips[s["id"]] = current
    return tips


SESSIONS_TTL = 2.0  # seconds between re-reads of the sessions table (3.2.0): the scan asks every 2 s, every device every 5 s


class Index:
    """Per bot database, read once and incrementally: the newest person-facing reply per chat, and
    each new one as it appears. Rebuilt from the start after a restart; alerts use the cursor."""

    def __init__(self, path, clock=time.monotonic):
        self.lock = threading.RLock()
        self.path, self.last, self.answering, self.latest, self.groups = path, 0, {}, {}, {}
        self.tips, self.sources, self.stamps = {}, {}, {}
        self.provisional = {}  # chat -> its turn's provisional answer row, until the turn ends (PROVISIONAL)
        self.found = []  # person-facing replies read but not yet taken by the alert scan
        self.clock, self.sessions_read, self.stamp = clock, None, None  # when the sessions table was last read

    def update(self):
        with self.lock:
            self._update()

    def _update(self):
        if not self.path.exists():
            return
        now = self.clock()
        fresh = self.sessions_read is not None and now - self.sessions_read < SESSIONS_TTL
        with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as source:
            source.row_factory = sqlite3.Row
            if not fresh:
                self.stamp = "timestamp" if any(r[1] == "timestamp" for r in source.execute("PRAGMA table_info(messages)")) else "NULL"
            rows = source.execute(f"""SELECT id, session_id, role, content, display_kind, finish_reason, tool_calls, active,
                {self.stamp} AS stamp FROM messages WHERE id>? AND role IN ('user','assistant') ORDER BY id""", (self.last,)).fetchall()
            # The sessions table (every chat's group, title and lineage) changes rarely and is read at most every
            # SESSIONS_TTL, unless a new row belongs to a chat it hasn't listed yet; the messages query runs every call.
            if fresh and all(row["session_id"] in self.sources for row in rows):
                self._index(rows)
                return
            have = {r[1] for r in source.execute("PRAGMA table_info(sessions)")}
            wanted = [c for c in SESSION_COLUMNS if c in have]
            sessions = [dict(r) for r in source.execute("SELECT %s FROM sessions" % ", ".join(wanted)).fetchall()]
        self.sessions_read = now
        self.tips = compression_tips(sessions)
        titles = {r["id"]: r.get("title") or "" for r in sessions}
        self.sources = {r["id"]: r.get("source") or "" for r in sessions}
        # A chat is known by its compression tip, as the app lists it; the tip carries the title. A
        # continuation of the Bot Chat is still the Bot Chat.
        self.groups = {r["id"]: chat_group(r.get("source"), r.get("hidden"), r.get("archived"),
                                           BOT_CHAT_TITLE in (titles.get(r["id"]), titles.get(r.get("parent_session_id"))),
                                           "_delegate_from" in (r.get("model_config") or ""))
                       for r in sessions}
        self._index(rows)

    def _index(self, rows):
        for row in rows:
            self.last = row["id"]
            chat = self.tips.get(row["session_id"], row["session_id"])
            autonomous = self.sources.get(chat) in AUTONOMOUS_SOURCES
            if row["role"] == "user":
                kept = self.provisional.pop(chat, None)
                if kept is not None:
                    self._found(kept, chat)
                self.answering[chat] = answers(row["content"], row["display_kind"])
            elif (provisional_answer(row) and assistant_kind(row, self.answering.get(chat), autonomous) is None
                  and is_reply({**dict(row), "finish_reason": "stop"}, self.answering.get(chat), autonomous)):
                self.provisional[chat] = row
            elif is_reply(row, self.answering.get(chat), autonomous):
                self.provisional.pop(chat, None)
                self._found(row, chat)
            elif row["role"] == "assistant" and row["finish_reason"] == "stop" and chat in self.provisional:
                # The turn ended in silence: its provisional answer is the reply (PROVISIONAL).
                kept = self.provisional.pop(chat)
                if (is_autonomous_silence if autonomous else is_silence)(row["content"]):
                    self._found(kept, chat)

    def settled(self):
        """The row the alert cursor may move to: before any provisional answer whose turn is still open, so the answer
        alerts when silence keeps it. A turn left open further back than PROVISIONAL_REACH rows holds it no longer."""
        open_turns = [row["id"] - 1 for row in self.provisional.values() if self.last - row["id"] < PROVISIONAL_REACH]
        return min([self.last, *open_turns])

    def _found(self, row, chat):
        # Compacting a chat in place (Bot Chat, 2026-10-15) retires its rows and writes the kept ones
        # again under new ids with their original timestamps: a reply no newer than one already
        # seen there is such a copy, not news. It alerted a day of old replies at once.
        if row["stamp"] is not None and row["stamp"] <= self.stamps.get(chat, float("-inf")):
            return
        if row["stamp"] is not None:
            self.stamps[chat] = row["stamp"]
        self.latest[chat] = row["id"]
        self.found.append((row["id"], chat))

    def group(self, chat):
        return self.groups.get(chat, "chats")

    def tip(self, chat):
        """A chat compressed since it was read is known by its new tip."""
        return self.tips.get(chat, chat)

    def take(self):
        """New person-facing replies since the last take: [(row_id, chat, group)], in order. Which
        devices hear about each is their filter's business (scan)."""
        with self.lock:
            found, self.found = self.found, []
            found = [(rid, self.tip(chat)) for rid, chat in found]
            return [(rid, chat, self.group(chat)) for rid, chat in found if self.group(chat) in UNREAD_GROUPS]

    def replies(self, groups=UNREAD_GROUPS):
        """Newest person-facing reply per chat in these groups, by each chat's current tip."""
        with self.lock:
            newest = {}
            for chat, rid in self.latest.items():
                tip = self.tip(chat)
                newest[tip] = max(rid, newest.get(tip, 0))
            return {chat: rid for chat, rid in newest.items() if self.group(chat) in groups}


_indexes = {}
_index_lock = threading.Lock()


def invalidate_index(profile=None):
    """After a failed delivery transaction, rebuild candidates from the durable cursor on the next scan."""
    with _index_lock:
        if profile is None:
            _indexes.clear()
        else:
            _indexes.pop(profile, None)


def index_for(profile, path):
    with _index_lock:
        index = _indexes.get(profile)
        if index is None or index.path != path:
            index = _indexes[profile] = Index(path)
        return index


def known_index(profile):
    """The index of a profile the scan has already read (``None`` before its first scan)."""
    return _indexes.get(profile)


def visible_replies(profile, path, groups=UNREAD_GROUPS):
    index = index_for(profile, path)
    index.update()
    return index.replies(groups)


def newest_id(path):
    if not path.exists():
        return 0
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as source:
        return source.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]


def reply_text(profile, row_id):
    """The finished reply's text, read from the bot's database at delivery time."""
    path = dict(sources()).get(profile)
    if path is None or not path.exists():
        return None
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as source:
        row = source.execute("SELECT content FROM messages WHERE id=?", (row_id,)).fetchone()
    return row[0] if row else None


def display_name(profile):
    """The profile's display name from its profile.yaml, else "Hermes" for the root home and the capitalized folder
    name for a profile."""
    base = home.root() if profile == "default" else home.root() / "profiles" / profile
    try:
        for line in (base / "profile.yaml").read_text().splitlines():
            if line.startswith("display_name:"):
                value = line.split(":", 1)[1].strip().strip("'\"")
                if value:
                    return value
    except OSError:
        pass
    return "Hermes" if profile == "default" else profile.capitalize()


# A scheduled run whose result goes to the bot's main chat rings once, from the main chat (Mark,
# 2026-10-13). The run's own alert waits while Hermes is still handing the result over (the main chat
# may be busy with another turn) and is dropped once it got there; the cards plugin (cron_archive.py)
# archives the run. It rings only when the hand-off didn't happen: the run failed, the delivery failed,
# or the job delivers somewhere else. Read from Hermes' own records and the cards plugin's relay ledger, read-only (run_handoff).

SCHEDULED_HOLD = 10        # the run's bookkeeping lands just after its reply
SCHEDULED_MAX_WAIT = 15 * 60  # a hand-off stuck this long rings anyway, saying so (Mark, 2026-10-13)
STUCK_NOTE = "Main chat stuck 15+ min, so this run's result is here instead. "


def cards_db(state_db):
    """The cards plugin's store beside Hermes's root home (``<root>/hermes-bots-cards/forms.db``), for a
    profile's state.db (``<root>/profiles/<name>/state.db``) or the root's."""
    folder = Path(state_db).resolve().parent
    base = folder.parent.parent if folder.parent.name == "profiles" else folder
    return base / "hermes-bots-cards" / "forms.db"


def relayed(state_db, chat):
    """Whether the cards plugin posted this run's result into the bot's main chat (relay.py's ledger)."""
    path = cards_db(state_db)
    if not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as con:
            row = con.execute("SELECT replied_at FROM relays WHERE run_session=?", (chat,)).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0])


def run_handoff(state_db, chat):
    """Where a scheduled run's result stands: "delivered" (the cards plugin relayed it into the main chat,
    or the run was silent), "waiting" (the run is still going, or its result is on its way to the main
    chat) or "released" (it isn't going to the main chat).

    Which execution a run chat belongs to is ``cron_runs.run_of`` (Hermes's ``cron/executions.db``). The relay
    ledger (``hermes-bots-cards/forms.db``) is the proof of delivery."""
    if relayed(state_db, chat):
        return "delivered"
    folder = Path(state_db).parent.resolve()
    executions = folder / "cron" / "executions.db"
    if not executions.is_file() or not Path(state_db).is_file():
        return "released"
    try:
        with closing(sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)) as state:
            row = state.execute("SELECT started_at, title FROM sessions WHERE id=? AND source='cron'", (chat,)).fetchone()
    except sqlite3.Error:
        return "released"
    if not row or row[0] is None:
        return "released"
    run = cron_runs.run_of(chat, row[0], row[1], cron_runs.runs(executions), cron_runs.jobs(folder))
    if run is None:
        return "released"
    _, _execution, _job, status, outcome = run
    if status in ("claimed", "running"):
        return "waiting"
    if status != "completed":
        return "released"
    if outcome == "suppressed":
        return "delivered"
    # Handed to the main chat (queued to its live owner, or delivered by Hermes's own Bot Chat turn) but the
    # relay isn't recorded yet: the main chat is busy or its reply hasn't landed.
    return "waiting" if outcome in ("queued", "delivered") else "released"


def session_source(profile, chat):
    """The session's source in its bot's database ('' when unknown), read-only."""
    for name, path in sources():
        if name != profile or not path.exists():
            continue
        try:
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as source:
                row = source.execute("SELECT source FROM sessions WHERE id=?", (chat,)).fetchone()
        except sqlite3.Error:
            return ""
        return (row[0] or "") if row else ""
    return ""

