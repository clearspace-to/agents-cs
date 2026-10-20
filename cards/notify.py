"""Tell the ``mobile`` module about tasks that need the person (docs/TASK_EXPERIENCE_API.md, Notifications).
Observer only, like the approval alerts (``alerts.py``): every post runs on a daemon thread and any failure is
dropped, so a slow or absent alert path never slows Hermes.

Since 2.0 there is no bridge process and no HTTP: ``post(kind, body)`` calls ``mobile.events.<kind>(body)`` in-process,
which seals the alert for each device and writes it to the outbox (``<root home>/hermes-bots/delivery.db``) that the
connector drains. That works from any Hermes process (a worker or a profile's gateway included), because SQLite is the
hand-off. The kinds are ``approval``, ``question`` and ``nudge`` (``alerts.py``) and these two:

* ``task``      ``{event: requested|resolved, board, task_id, title, bot, kind, reason}`` rings the phone
                ("Friday needs your decision on …") or clears that alert everywhere.
* ``attention`` ``{board, count, tasks: [{id, title, bot, kind}], decisions}``: what the app icon badge counts beside
                unread chats, and the list diffed to clear alerts for tasks that stopped needing the person on the
                desktop.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any

NEEDS_PERSON = ("needs_you", "stuck")
# Repeating an unchanged attention summary within this window is noise (the board is polled every 8 s).
ATTENTION_REPEAT_SECONDS = 20.0

_lock = threading.Lock()
_last_attention: dict[str, tuple[str, float]] = {}
# Tests swap this for a recorder; production hands the event to the mobile module in-process.
transport = None
log = logging.getLogger("hermes_bots.mobile")


def _deliver(kind: str, body: dict[str, Any]) -> None:
    try:
        from ..mobile import events
        getattr(events, kind)(body)
    except Exception as error:  # noqa: BLE001 - an observer: never into Hermes
        log.warning("hermes-bots mobile: %s event failed: %s", kind, type(error).__name__)


def post(kind: str, body: dict[str, Any], *, wait: bool = False) -> None:
    """Hand an event to ``mobile.events.<kind>`` on a daemon thread (``wait`` runs it inline, for tests)."""
    if transport is not None:
        transport(kind, body)
        return
    if wait:
        _deliver(kind, body)
    else:
        threading.Thread(target=_deliver, args=(kind, body), name="hermes-bots-notify", daemon=True).start()


def task_event(event: str, *, board: str | None, task_id: str, title: str | None, bot: str | None,
               kind: str, reason: str | None = None) -> None:
    post("task", {"event": event, "board": board or "", "task_id": task_id, "title": (title or "")[:200],
                   "bot": bot or "", "kind": kind, "reason": (reason or "")[:400]})


def request_kind(row: dict[str, Any]) -> str:
    """Why a root needs the person, for the alert's words: decision, review, blocked or stuck."""
    first = (row.get("attention") or {}).get("first") or {}
    if first.get("kind") == "card":
        return "decision"
    if first.get("kind") == "review" or row.get("status") == "review":
        return "review"
    if (row.get("attention") or {}).get("state") == "stuck":
        return "stuck"
    return "blocked"


def attention_summary(payload: dict[str, Any], decisions_open: int) -> dict[str, Any]:
    """What the badge counts for one board: roots (and standalone tasks) that need the person, plus
    the bots' open chat decisions. Subtasks count through their root, as the app counts them."""
    rows = []
    for task in payload.get("tasks", []):
        attention = task.get("attention") or {}
        if task.get("parent_id") is None and attention.get("state") in NEEDS_PERSON:
            rows.append({"id": task["id"], "title": task.get("title"), "bot": task.get("assignee") or "",
                         "kind": request_kind(task)})
    return {"board": payload.get("board") or "", "tasks": rows, "decisions": int(decisions_open),
            "count": len(rows) + int(decisions_open)}


def attention(summary: dict[str, Any], *, force: bool = False, now: float | None = None) -> bool:
    """Post the summary unless the same one went recently. Returns whether it was sent."""
    digest = hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest()
    current = time.time() if now is None else now
    with _lock:
        previous = _last_attention.get(summary.get("board") or "")
        if not force and previous and previous[0] == digest and current - previous[1] < ATTENTION_REPEAT_SECONDS:
            return False
        _last_attention[summary.get("board") or ""] = (digest, current)
    post("attention", summary)
    return True


def reset() -> None:
    with _lock:
        _last_attention.clear()
