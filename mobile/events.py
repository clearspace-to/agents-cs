"""What the cards module tells the mobile module, in-process: approvals, bot questions, board tasks, the attention
summary and "a turn just finished". Replaces the push bridge's loopback ``/internal/*`` routes (no HTTP, no shared
key, no socket).

Callable from ANY Hermes process that loads the plugin (``hermes serve``, a gateway, a Kanban worker, a profile's
process): each function seals the alert for every device and writes the rows to ``<root home>/hermes-bots/delivery.db``,
which the connector drains; SQLite is the hand-off. ``nudge`` asks the connector, which scans for finished replies
(``scan.py``), to look now by touching the file it watches (otherwise its 2 s cycle finds the reply anyway).

``cards/notify.py`` calls these by name (``approval``, ``question``, ``task``, ``attention``, ``nudge``) on a daemon
thread; a malformed event is ignored, and a failure is logged there, never raised into Hermes.
"""
from __future__ import annotations

import logging
import re
import uuid
from contextlib import closing

from . import alerts, home, replies, state

log = logging.getLogger("agents_cs.mobile")

TASK_TITLES = {"decision": "{bot} needs your decision", "review": "{bot} finished: review the work",
               "blocked": "{bot} needs your input", "stuck": "{bot} is stuck on a task"}
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _text(value, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _connect():
    return closing(alerts.store().connect())


def nudge(_event: dict | None = None) -> None:
    """A bot just saved a finished turn (the event names only its profile): have the connector scan now."""
    try:
        folder = home.state_dir()
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        home.nudge_path().touch()
    except OSError:
        pass  # the connector's 2 s cycle is the fallback


def approval(event: dict) -> None:
    """A bot asks for approval (``requested``) or it was answered anywhere (``resolved``)."""
    kind, profile = event.get("event"), event.get("profile")
    chat = _text(event.get("session_id"), 200) or _text(event.get("session_key"), 200)
    if kind not in ("requested", "resolved") or not alerts.valid_profile(profile) or not chat:
        return
    thread = profile + ":" + chat
    collapse = str(uuid.uuid5(uuid.NAMESPACE_URL, "approval:" + thread))
    log.info("agents-cs mobile: approval %s %s %s surface=%s", kind, profile, chat[:22], event.get("surface") or "-")
    with _connect() as con, con:
        if kind == "requested":
            body = _text(event.get("description"), 600).strip() or "wants to run a command."
            command = _text(event.get("command"), 600).strip()
            if command:
                body = (body + "\n" + command)[:400]
            # "approval-session" adds Allow for this chat; Always allow is never offered from the Lock Screen.
            aps = {"alert": {"title": replies.display_name(profile) + " needs approval", "body": body},
                   "thread-id": thread, "category": "approval-session" if event.get("allow_session") is True else "approval",
                   "sound": "default", "interruption-level": "time-sensitive", "mutable-content": 1}
            alerts.push_all(con, aps, {"bot": profile, "chat": chat, "approval": True}, collapse=collapse,
                            content={"type": "approval", "title": aps["alert"]["title"], "body": body})
        else:
            alerts.push_all(con, {"content-available": 1},
                            {"hermes": {"approvalResolved": {"profile": profile, "chat": chat}}},
                            push_type="background", priority=5)


def question(event: dict) -> bool:
    """A bot called its clarify tool (``requested``) or the tool returned (``resolved``). Returns whether an alert was
    queued (a question in a messaging chat is answered there, so it never rings the phone)."""
    kind, profile, chat = event.get("event"), event.get("profile"), _text(event.get("session_id"), 200)
    if kind not in ("requested", "resolved") or not alerts.valid_profile(profile) or not chat:
        return False
    thread = profile + ":" + chat
    collapse = str(uuid.uuid5(uuid.NAMESPACE_URL, "question:" + thread))
    source = replies.session_source(profile, chat)
    log.info("agents-cs mobile: question %s %s %s source=%s", kind, profile, chat[:22], source or "-")
    if source in replies.MESSAGING_SOURCES:
        return False
    with _connect() as con, con:
        if kind == "requested":
            count = event.get("count") if isinstance(event.get("count"), int) and 1 <= event["count"] <= 20 else 1
            body = _text(event.get("question"), 2000).strip() or "wants to ask you something."
            if count > 1:
                body = f"{body}\n(+{count - 1} more)"
            body = body[:400]
            aps = {"alert": {"title": replies.display_name(profile) + " has a question", "body": body},
                   "thread-id": thread, "category": "question", "sound": "default",
                   "interruption-level": "time-sensitive", "mutable-content": 1}
            alerts.push_all(con, aps, {"bot": profile, "chat": chat, "question": True}, collapse=collapse,
                            content={"type": "question", "title": aps["alert"]["title"], "body": body},
                            placeholder=alerts.QUESTION_PLACEHOLDER)
        else:
            alerts.push_all(con, {"content-available": 1},
                            {"hermes": {"questionResolved": {"profile": profile, "chat": chat}}},
                            push_type="background", priority=5)
    return kind == "requested"


def task(event: dict) -> None:
    """A board task needs the person (``requested``: "Friday needs your decision on …") or no longer does (``resolved``)."""
    kind, task_id = event.get("event"), event.get("task_id")
    if kind not in ("requested", "resolved") or not isinstance(task_id, str) or not _ID.fullmatch(task_id):
        return
    board, bot, kind_of = _text(event.get("board"), 64), _text(event.get("bot"), 64), _text(event.get("kind"), 20) or "blocked"
    collapse = str(uuid.uuid5(uuid.NAMESPACE_URL, "task:" + task_id))
    log.info("agents-cs mobile: task %s %s %s bot=%s", kind, kind_of, task_id, bot or "-")
    with _connect() as con, con:
        if kind == "requested":
            who = replies.display_name(bot) if bot else "A bot"
            title = TASK_TITLES.get(kind_of, TASK_TITLES["blocked"]).format(bot=who)
            task_title = _text(event.get("title"), 200)
            body = task_title.strip() or "Open the task to see what it needs."
            reason = _text(event.get("reason"), 400).strip()
            if kind_of in ("blocked", "stuck") and reason:
                body = (body + "\n" + reason)[:400]
            aps = {"alert": {"title": title, "body": body}, "thread-id": "task:" + task_id, "category": "task",
                   "sound": "default", "interruption-level": "time-sensitive", "mutable-content": 1}
            alerts.push_all(con, aps, {"bot": bot, "task": task_id, "board": board, "kind": kind_of, "taskTitle": task_title},
                            collapse=collapse, content={"type": "task", "title": title, "body": body},
                            placeholder=alerts.TASK_PLACEHOLDER)
        else:
            alerts.push_all(con, {"content-available": 1}, {"hermes": {"taskResolved": {"task": task_id, "board": board}}},
                            push_type="background", priority=5)


def _report(event: dict):
    """The attention report, clamped: ``{board, count, decisions, tasks: [{id, title, bot, kind}]}``."""
    def number(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000 else None
    count, decisions = number(event.get("count")), number(event.get("decisions", 0))
    if count is None or decisions is None:
        return None
    tasks = []
    for item in (event.get("tasks") or [])[:500]:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and _ID.fullmatch(item["id"]):
            title = item.get("title")
            tasks.append({"id": item["id"], "title": title[:200] if isinstance(title, str) else None,
                          "bot": _text(item.get("bot"), 64), "kind": _text(item.get("kind"), 20) or "blocked"})
    return {"board": _text(event.get("board"), 64), "count": count, "decisions": decisions, "tasks": tasks}


def attention(event: dict) -> list[str]:
    """Everything on the board that needs the person right now. The badge follows at once on every device; tasks that
    left the list have their alerts cleared. Returns the resolved task ids."""
    report = _report(event)
    if report is None:
        return []
    with _connect() as con, con:
        changed, resolved = state.store_attention(con, report)
        # The plugin reports on every dispatcher tick; an unchanged report moves no badge and sends nothing.
        if changed:
            alerts.push_to_owner(con, state.OWNER, None, [])
        for task_id in resolved:
            alerts.push_all(con, {"content-available": 1},
                            {"hermes": {"taskResolved": {"task": task_id, "board": report["board"]}}},
                            push_type="background", priority=5)
    log.info("agents-cs mobile: attention %s count=%s tasks=%s decisions=%s resolved=%s", report["board"] or "default",
             report["count"], len(report["tasks"]), report["decisions"], len(resolved))
    return resolved

