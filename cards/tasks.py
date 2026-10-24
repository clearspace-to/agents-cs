"""The task experience for the Agents CS app (docs/TASK_EXPERIENCE_API.md, v2).

Four dashboard routes, mounted by ``cards/routes.py`` under ``/api/plugins/agents-cs/``:

GET  tasks/board?board=&include_archived=     the board with containment, attention and allowed moves
POST tasks/quick-add?board=                   one call: create the task, write it up or plan it, start it
POST tasks/{id}/comments?board=               a comment plus the right follow-up, coordinator included
POST tasks/{id}/seen?board=                   {review_key}: the person opened a root on Review (off the badge)
POST actions/{action_id}/answers?board=       410 task_cards_removed (old app builds still call it)

Task-bound cards were removed on 2026-10-23 (Mark): a worker that needs the person blocks its task with the
question in words (``kanban_block(kind="needs_input", reason=…)``, see ``block_question``) and the person answers
with an ordinary task comment. Cards bound before then are ignored everywhere: they never count as attention,
never appear in ``actions`` and never hold a worker back.

the ``pre_tool_call`` guard the plugin registers (``tool_call_block``), and the observers that tell the
mobile module when a task needs the person (``notify``).

Hermes stays the authority for tasks. ``HermesBoard`` is the one place that touches
``hermes_cli.kanban_db`` (what Hermes's own dashboard plugin uses) and read-only SQL on its tables, and
writes only through Hermes's public mutators (``create_task``, ``add_comment``, ``promote_task``,
``unblock_task``, ``specify_triage_task``, ``reopen_review_task``, ``schedule_task``, ``archive_task``,
``link_tasks``, ``unlink_tasks``; a finished task reopens through its dashboard's PATCH). Every derivation
(containment, attention, allowed moves, pending requests, the follow-up table, the coordinator's
instruction) is plain Python over dicts, so the tests run it against an in-memory board and nothing here
needs Hermes to import. FastAPI is imported only inside ``make_router``: a worker process loads this
module for the guard without it.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import threading
import time
from dataclasses import asdict, is_dataclass
from typing import Any, Callable

try:  # the plugin lives in two homes (a profile's and the root's); tests import it bare
    from . import coordinator, notify
    from .store import DecisionLog, resolve_board
except ImportError:  # pragma: no cover - loaded as a top-level module by the dashboard router
    import coordinator  # type: ignore
    import notify  # type: ignore
    from store import DecisionLog, resolve_board  # type: ignore

CAPABILITY = 2
# The moves Hermes's dashboard accepts by hand, from each column (its PATCH verbs, mapped against the live
# plugin 2026-10-02 and kept here, the one place, since 2026-10-08). Running is a worker's state: never
# offered as a target; from it, Hermes takes every move (a dashboard action overrides a live claim).
ALLOWED_MOVES: dict[str, tuple[str, ...]] = {
    "triage": ("todo", "ready"),
    "todo": ("triage", "scheduled", "ready"),
    "scheduled": ("triage", "todo", "ready"),
    "ready": ("triage", "todo", "scheduled", "blocked", "review", "done"),
    "running": ("triage", "todo", "scheduled", "ready", "blocked", "review", "done"),
    "blocked": ("triage", "todo", "scheduled", "ready", "done"),
    "review": ("triage", "todo", "ready", "done"),
    "done": ("triage", "todo", "ready"),
    "archived": (),
}
QUICK_TITLE_LIMIT = 80
TERMINAL = {"done", "archived"}
# Hermes diagnostics that mean a human has to look (contract, attention rule 4).
STUCK_KINDS = {"repeated_crashes", "repeated_failures", "stuck_in_blocked", "block_unblock_cycling",
               "review_dependency_deadlock", "stranded_in_ready"}
HUMAN_BLOCKS = {"needs_input", "capability"}
# The events the board needs: containment, the review hand-off and the latest block reason.
# Also the events that mark a block-loop escalation and what clears it (loop_escalated).
EVENT_KINDS = ("decomposed", "created", "review_requested", "blocked", "scheduled", "block_loop_detected",
               "claimed", "unblocked", "promoted", "completed", "status", "unarchived", "restored", "specified")
EXTERNAL_WAIT_PREFIX = "[Waiting for reply] "
_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class BoardError(Exception):
    """A board the request can't use: ``status`` 400 (malformed slug) or 404 (unknown board)."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def normalize_slug(board: str | None) -> str | None:
    """Hermes's board slug rules (lowercase, ``[a-z0-9_-]``, 64 chars), checked here rather than through
    Hermes's private helper. None and the empty string mean the current board."""
    if board is None:
        return None
    slug = board.strip().lower()
    if not slug:
        return None
    if not _SLUG.fullmatch(slug):
        raise BoardError(400, f"board {board!r} is not a valid board slug")
    return slug


def allowed_moves(status: str | None) -> list[str]:
    """Where a task in ``status`` may be moved by hand (the contract's ``allowed_moves``)."""
    return list(ALLOWED_MOVES.get(status or "", ()))


def quick_title(text: str) -> str:
    """Quick Add's words as a title: the first line, cut to Hermes's 80 characters at a word where it can be
    (the same rule the app and the share extension used to apply themselves)."""
    line = next((part for part in text.splitlines() if part.strip()), text).strip()
    if len(line) <= QUICK_TITLE_LIMIT:
        return line
    cut = line[:QUICK_TITLE_LIMIT - 1]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip() + "…"


# --- Derivation (pure) ----------------------------------------------------------------------------

def containment(events_by_task: dict[str, list[dict[str, Any]]],
                stored: list[tuple[str, str]] | tuple = ()) -> tuple[dict[str, str], dict[str, list[str]]]:
    """``(parent_of, children_of)`` from what the plugin recorded when it split a task (``stored`` pairs,
    root first) and, for families it never saw split, Hermes's decomposition events: a root's
    ``decomposed`` (``payload.child_ids``) and a child's ``created`` (``payload.from_decompose_of``).
    Dependency links are not containment."""
    parent_of: dict[str, str] = {}
    children_of: dict[str, list[str]] = {}

    def adopt(root: str, child: str) -> None:
        if child == root:
            return
        parent_of.setdefault(child, root)
        siblings = children_of.setdefault(root, [])
        if child not in siblings:
            siblings.append(child)

    for root, child in stored:
        adopt(root, child)
    for task_id, events in events_by_task.items():
        for event in events:
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event.get("kind") == "decomposed":
                for child in payload.get("child_ids") or []:
                    if isinstance(child, str):
                        adopt(task_id, child)
            elif event.get("kind") == "created" and isinstance(payload.get("from_decompose_of"), str):
                adopt(payload["from_decompose_of"], task_id)
    return parent_of, children_of


def descendants(children_of: dict[str, list[str]], root: str) -> list[str]:
    """Every contained task under ``root``, breadth first, without the root."""
    found: list[str] = []
    queue = list(children_of.get(root, []))
    while queue:
        child = queue.pop(0)
        if child in found or child == root:
            continue
        found.append(child)
        queue.extend(children_of.get(child, []))
    return found


def review_from_events(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The latest ``review_requested`` hand-off: summary, artifacts, reviewer, event id and time."""
    for event in reversed(events):
        if event.get("kind") != "review_requested":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        artifacts = payload.get("artifacts") or metadata.get("artifacts") or []
        return {"summary": payload.get("summary"), "reviewer": payload.get("reviewer"),
                "artifacts": [str(a) for a in artifacts if a] if isinstance(artifacts, list) else [],
                "event_id": event.get("id"), "created_at": event.get("created_at")}
    return None


def loop_escalated(events: list[dict[str, Any]]) -> bool:
    """Hermes routed the task to triage after it kept re-blocking for the same human reason
    (``block_loop_detected``): a person has to step in, so it counts as needing you (Mark's board,
    2026-10-05: four such tasks sat under In progress as "Not started"). Later comments don't clear it;
    a claim, unblock, promotion or completion does."""
    clearing = {"claimed", "unblocked", "promoted", "completed", "status", "review_requested", "unarchived",
                "restored", "specified", "decomposed", "scheduled"}
    for event in reversed(events):
        kind = event.get("kind")
        if kind == "block_loop_detected":
            return True
        if kind in clearing:
            return False
    return False


def block_reason(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("kind") in ("blocked", "scheduled", "block_loop_detected"):
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            reason = payload.get("reason")
            return reason if isinstance(reason, str) and reason.strip() else None
    return None


def waits_on(task_id: str, links: list[tuple[str, str]], tasks_by_id: dict[str, dict[str, Any]]) -> list[str]:
    """Unfinished dependency parents (Hermes ``task_links``), in link order."""
    return [parent for parent, child in links
            if child == task_id and parent in tasks_by_id and tasks_by_id[parent].get("status") not in TERMINAL]


def dependents(task_id: str, links: list[tuple[str, str]]) -> list[str]:
    return [child for parent, child in links if parent == task_id]


def pending_requests(task: dict[str, Any], family: list[str], *, tasks_by_id: dict[str, dict[str, Any]],
                     reviews_by_task: dict[str, dict[str, Any] | None]) -> list[dict[str, Any]]:
    """What waits on the person for this task and its contained descendants, oldest first: one entry per
    task in review. Task-bound cards are gone (2026-10-23), so no request is ever of kind ``card``; a worker's
    question is a ``needs_input`` block, which ``attention_state`` reads from the task itself."""
    requests: list[dict[str, Any]] = []
    for member_id in [task["id"], *family]:
        member = tasks_by_id.get(member_id)
        if member is None or (member_id != task["id"] and member.get("status") == "archived"):
            continue
        if member.get("status") == "review":
            review = reviews_by_task.get(member_id) or {}
            requests.append({"kind": "review", "task_id": member_id, "task_title": member.get("title"),
                             "title": f"Review: {member.get('title')}",
                             "summary": review.get("summary") or member.get("latest_summary"),
                             "created_at": review.get("created_at") or member.get("started_at") or member.get("created_at") or 0})
    requests.sort(key=lambda r: (r["created_at"], r["task_id"]))
    return requests


def review_key(state: str, requests: list[dict[str, Any]], members: list[str],
               events_by_task: dict[str, list[dict[str, Any]]], *, stalled: bool = False) -> str:
    """What a root on Review is waiting for: its state, its open reviews, and the latest block on it and on each
    subtask that needs the person. A new review, a new block or question, or a new stall changes it, so a root the
    person already opened counts again (Mark, 2026-10-23/24: anything on Review read on the phone stays there
    without the app icon badge). The name says review because 3.4.0 apps send it back as ``review_key``."""
    blocks = []
    for member in members:
        ids = [e.get("id") or 0 for e in events_by_task.get(member, []) if e.get("kind") in ("blocked", "block_loop_detected")]
        blocks.append(f"{member}#{max(ids, default=0)}")
    reviews = [f"{r['task_id']}@{r['created_at']}" for r in requests]
    return "|".join([state, *reviews, *blocks, *(["stalled"] if stalled else [])])


def attention_state(task: dict[str, Any], *, pending: int, waiting: list[str],
                    child_statuses: list[str], escalated: bool = False, external_wait: bool = False,
                    family_state: str | None = None, family_external: bool = False) -> str:
    """The contract's rules, in order. A block Hermes classed as a dependency reads as waiting and
    any other non-human block as stuck (the contract names neither; 2026-10-05). ``escalated``: Hermes
    sent it to triage after a block loop, which is a human's turn too. ``family_state``: the most urgent
    of the contained descendants' own needs_you/stuck (``family_attention``), since the board shows
    only the root (Mark's board, 2026-10-10: a capability-blocked subtask left its root under Not started).
    A task parked for a third party's reply (``external_wait``) is waiting even when Hermes flags its
    re-checks as a block/unblock cycle, and a root whose subtask is so parked is in progress: neither is the
    person's (Mark, 2026-10-11: an Upwork offer awaiting the designer read "Needs you")."""
    status = task.get("status")
    if status == "archived":
        return "archived"
    if pending > 0:
        return "needs_you"
    if status == "blocked" and task.get("block_kind") in HUMAN_BLOCKS:
        return "needs_you"
    if escalated and status in ("triage", "todo", "blocked"):
        return "needs_you"
    if family_state == "needs_you":
        return "needs_you"
    if family_state == "stuck":
        return "stuck"
    if status == "scheduled" and external_wait:
        return "waiting"
    if any(isinstance(d, dict) and d.get("kind") in STUCK_KINDS for d in task.get("diagnostics") or []):
        return "stuck"
    if family_external:
        return "working"
    if status == "running":
        return "working"
    if any(s in ("running", "ready", "review") for s in child_statuses):
        return "working"
    if status == "done":
        return "done"
    if status == "scheduled":
        return "waiting" if external_wait else "scheduled"
    if status == "ready":
        return "queued"
    if status == "todo" and waiting:
        return "waiting"
    if status in ("triage", "todo"):
        return "not_started"
    if status == "blocked":
        return "waiting" if task.get("block_kind") == "dependency" else "stuck"
    return "not_started"


def family_attention(family: list[str], *, tasks_by_id: dict[str, dict[str, Any]],
                     events_by_task: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Contained descendants that need a person on their own account (a human block, a block-loop
    escalation, a stuck diagnostic or a non-dependency block), needs_you before stuck, then tree order.
    Reviews are not here: ``pending_requests`` already counts those."""
    found: list[dict[str, Any]] = []
    for member_id in family:
        member = tasks_by_id.get(member_id)
        if member is None or member.get("status") in TERMINAL:
            continue
        events = events_by_task.get(member_id, [])
        state = attention_state(member, pending=0, waiting=[],
                                child_statuses=[], escalated=loop_escalated(events),
                                external_wait=waits_for_reply(member, events))
        if state in ("needs_you", "stuck"):
            found.append({"task_id": member_id, "title": member.get("title"), "state": state,
                          "block_kind": member.get("block_kind"), "reason": block_reason(events)})
    found.sort(key=lambda m: m["state"] != "needs_you")
    return found


def waits_for_reply(task: dict[str, Any], events: list[dict[str, Any]]) -> bool:
    """Parked by ``hermes_task_wait`` until a third party replies (``wait_external``)."""
    return task.get("status") == "scheduled" and (block_reason(events) or "").startswith(EXTERNAL_WAIT_PREFIX)


def family_block_line(flagged: list[dict[str, Any]]) -> str:
    """What the root's top card says when a subtask, not the root, needs the person. The app shows it as
    the needs-you body under a Reply button, so it names the subtask and why."""
    first = flagged[0]
    title = first.get("title") or first["task_id"]
    word = "is stuck" if first["state"] == "stuck" else "is blocked"
    reason = (first.get("reason") or "").strip()
    line = f"Subtask “{title}” {word}: {reason}" if reason else (
        f"Subtask “{title}” {word} and needs your input." if first["state"] == "needs_you"
        else f"Subtask “{title}” {word}.")
    more = len(flagged) - 1
    if more:
        line += f" {more} more subtask{'s' if more > 1 else ''} also need{'' if more > 1 else 's'} you."
    return line


def compose_board(*, board: str, tasks: list[dict[str, Any]], events_by_task: dict[str, list[dict[str, Any]]],
                  links: list[tuple[str, str]], latest_event_id: int,
                  now: int | None = None, include_archived: bool = False,
                  stored_containment: list[tuple[str, str]] | tuple = (), preparing: set[str] | frozenset = frozenset(),
                  stalls: set[str] | frozenset = frozenset(), seen: dict[str, str] | None = None) -> dict[str, Any]:
    """The ``GET tasks/board`` payload from raw board data. ``tasks`` are Hermes task dicts (every field
    Hermes's own board puts on them); this adds containment, dependencies, review, block reason,
    attention, the allowed moves and whether a write-up is still running (``preparing``), and recomputes
    ``link_counts`` and ``progress`` the way Hermes's board does. A root in ``stalls`` (the coordinator left
    nothing moving) needs the person for as long as its family is still stalled. A task that needs the person
    carries ``attention.review_key`` (``review_key``), and ``attention.seen`` once the person opened it at that key
    (``seen``, root id → key): it stays on Review but leaves the badge and the alerts. ``actions`` is always ``[]``:
    task-bound cards were removed on 2026-10-23, and shipped app builds still decode the key."""
    tasks_by_id = {t["id"]: t for t in tasks}
    parent_of, children_of = containment(events_by_task, stored_containment)
    link_counts: dict[str, dict[str, int]] = {}
    progress: dict[str, dict[str, int]] = {}
    for parent, child in links:
        link_counts.setdefault(parent, {"parents": 0, "children": 0})["children"] += 1
        link_counts.setdefault(child, {"parents": 0, "children": 0})["parents"] += 1
        if child in tasks_by_id:
            bucket = progress.setdefault(parent, {"done": 0, "total": 0})
            bucket["total"] += 1
            bucket["done"] += tasks_by_id[child].get("status") == "done"
    reviews_by_task = {tid: review_from_events(events_by_task.get(tid, [])) for tid in tasks_by_id}

    # Containment only among tasks the board lists: a child whose root was archived stands alone
    # (two of Mark's tasks vanished from the board under archived parents, 2026-10-05).
    visible = {t["id"] for t in tasks if include_archived or t.get("status") != "archived"}
    out: list[dict[str, Any]] = []
    for task in tasks:
        if task.get("status") == "archived" and not include_archived:
            continue
        row = dict(task)
        events = events_by_task.get(task["id"], [])
        family = descendants(children_of, task["id"])
        waiting = waits_on(task["id"], links, tasks_by_id)
        requests = pending_requests(task, family, tasks_by_id=tasks_by_id, reviews_by_task=reviews_by_task)
        row["link_counts"] = link_counts.get(task["id"], {"parents": 0, "children": 0})
        row["progress"] = progress.get(task["id"])
        parent = parent_of.get(task["id"])
        row["parent_id"] = parent if parent in visible else None
        row["children"] = [c for c in children_of.get(task["id"], []) if c in visible]
        row["waits_on"] = waiting
        row["block_reason"] = block_reason(events)
        row["review"] = reviews_by_task.get(task["id"])
        row["allowed_moves"] = allowed_moves(task.get("status"))
        row["preparing"] = task["id"] in preparing
        flagged = family_attention(family, tasks_by_id=tasks_by_id, events_by_task=events_by_task)
        inputs = dict(pending=len(requests), waiting=waiting, escalated=loop_escalated(events),
                      external_wait=(row["block_reason"] or "").startswith(EXTERNAL_WAIT_PREFIX),
                      child_statuses=[tasks_by_id[c].get("status") for c in family if c in tasks_by_id],
                      family_external=any(waits_for_reply(tasks_by_id[c], events_by_task.get(c, []))
                                          for c in family if c in tasks_by_id))
        own_state = attention_state(task, **inputs)
        state = attention_state(task, **inputs, family_state=flagged[0]["state"] if flagged else None)
        row["attention"] = {"state": state, "pending": len(requests), "first": requests[0] if requests else None}
        if state != own_state:
            # A subtask moved the root to Review. Shipped app builds decode only card/review requests, so the
            # root's top card reads its block_reason ("needs your input" + this line, with Reply); `subtask`
            # carries the same facts structured, for builds that want to link to it.
            row["block_reason"] = family_block_line(flagged)
            row["attention"]["subtask"] = flagged[0]
        elif task["id"] in stalls and state != "needs_you":
            stalled = stall_line(task["id"], family, tasks_by_id=tasks_by_id, links=links, events_by_task=events_by_task)
            if stalled:
                row["block_reason"] = stalled[0]
                row["attention"] = {"state": "needs_you", "pending": len(requests), "first": requests[0] if requests else None,
                                    "subtask": stalled[1]}
        if row["parent_id"] is None and row["attention"]["state"] in notify.NEEDS_PERSON:
            key = review_key(row["attention"]["state"], requests, [task["id"], *(m["task_id"] for m in flagged)],
                             events_by_task, stalled=task["id"] in stalls)
            row["attention"]["review_key"] = key
            if (seen or {}).get(task["id"]) == key:
                row["attention"]["seen"] = True
        out.append(row)
    assignees = sorted({t.get("assignee") for t in tasks if t.get("assignee") and t.get("status") != "archived"})
    return {"capability": CAPABILITY, "board": board, "latest_event_id": int(latest_event_id or 0),
            "now": int(now if now is not None else time.time()), "assignees": assignees, "tasks": out,
            "actions": [],
            "containment": [[root, child] for root, kids in children_of.items() for child in kids]}


def follow_up_plan(status: str | None, *, decomposed: bool, unsatisfied: list[tuple[str, str]]) -> tuple[str, str | None]:
    """The contract's follow-up table: ``(step, detail)`` where step is coordinate, promote, waiting,
    none, reopen_review or reopen_done. A decomposed root is coordinated whatever its own status, even
    once every step is finished: "add a step" or "redo that step" needs the coordinator (Mark's invite
    task, 2026-10-11, went to a plain worker that could only comment)."""
    if decomposed:
        return "coordinate", None
    if status in ("triage", "todo", "blocked"):
        if unsatisfied:
            names = ", ".join(f"{tid} ({s})" for tid, s in unsatisfied)
            return "waiting", f"Waits on {names}."
        return "release_triage" if status == "triage" else "unblock" if status == "blocked" else "promote", None
    if status in ("ready", "running", "scheduled"):
        return "none", None
    if status == "review":
        return "reopen_review", None
    if status == "done":
        return "reopen_done", None
    return "none", None


def family_snapshot(root_id: str, children_of: dict[str, list[str]], tasks_by_id: dict[str, dict[str, Any]],
                    links: list[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    """Root first, then its contained descendants: what the coordinator may touch. With ``links``, each
    row also names the unfinished steps it waits for."""
    rows = []
    for tid in [root_id, *descendants(children_of, root_id)]:
        task = tasks_by_id.get(tid)
        if task is not None:
            row = {"id": tid, "title": task.get("title"), "status": task.get("status"),
                   "block_kind": task.get("block_kind"), "assignee": task.get("assignee")}
            if links is not None:
                row["waits_on"] = waits_on(tid, links, tasks_by_id)
            rows.append(row)
    return rows


def coordination_instruction(*, board: str, root: dict[str, Any], comment: str, family: list[dict[str, Any]]) -> str:
    """The bounded prompt the app submits to the orchestrator session (contract, Coordination)."""
    members = "\n".join(
        f"- {m['id']} · {m.get('title') or '(untitled)'} · {m.get('status')}"
        + (f" · blocked: {m['block_kind']}" if m.get("block_kind") and m.get("status") == "blocked" else "")
        + (f" · {m['assignee']}" if m.get("assignee") else "")
        + (f" · waits for {', '.join(m['waits_on'])}" if m.get("waits_on") else "")
        for m in family) or "- (none)"
    return (
        f"You are coordinating Kanban task {root['id']} \"{root.get('title') or ''}\" on board {board}.\n\n"
        f"The person commented on the root task:\n<<<\n{comment.strip()}\n>>>\n\n"
        f"Family (the root and its contained tasks):\n{members}\n\n"
        "Your job: review the root and every step together against the person's comment, then change the plan so\n"
        "the work keeps moving toward what they now want. Read each step first (kanban_show: brief, comments, results).\n\n"
        "Rules:\n"
        "1. Act only on these tasks. Never touch, create or link a task outside this family.\n"
        "2. Change the steps with hermes_task_adjust; do it yourself, don't ask the person first:\n"
        "   - restart: a step that should run now (not started, parked in triage, blocked, scheduled), with its new instruction.\n"
        "   - redo: a finished or in-review step whose result no longer fits, with what to do differently.\n"
        "   - drop: a step that no longer makes sense, with the reason (it is archived; the person can restore it).\n"
        "   - link / unlink: change the order (the child step waits for the parent step).\n"
        "   - add: a missing step, with its title and instruction (after: the steps it waits for). The main task\n"
        "     reopens and waits for it, so it is not finished until the new step is. Never use kanban_create.\n"
        "   The main task itself can be restarted or redone like a step. If the person says the task is not\n"
        "   finished, or asks for more work, add or redo the step that does it; a comment never reopens anything.\n"
        "   A comment alone wakes nothing: every step you change gets restart or redo, never only kanban_comment.\n"
        "3. Never answer a step's question for the person, and never approve a draft on their behalf.\n"
        f"4. When the request is ambiguous, ask in one kanban_comment on the root ({root['id']}): the question in "
        "words, any options written out, and stop. Don't block the root (Hermes only blocks a running or ready task). "
        "No cards: the person answers with a comment.\n"
        "5. Never send messages or spend money.\n"
        "6. Before you finish, at least one step must be ready or running, unless only the person can unblock it.\n"
        "7. Finish by posting one short kanban_comment on the root saying what you changed and why."
    )


ASK_SEND = "Reply send, or tell me what to change."


def block_question(card: dict[str, Any]) -> str:
    """The question a card would have asked, in words, for ``kanban_block(kind="needs_input", reason=…)``: a worker
    no longer shows cards (2026-10-23). The app shows the block reason as the task's question and the person answers
    with a comment, so everything they need to answer is written out: the options, the form's fields, or the
    whole drafted message (ending with ``ASK_SEND``)."""
    data, kind = card.get("data") or {}, card.get("type")
    lines = [card["title"]] + ([card["subtitle"]] if card.get("subtitle") else [])
    if isinstance(data.get("description"), str):
        lines.append(data["description"])
    if kind == "selection":
        lines += ["", "Pick one or more:" if data.get("mode") == "multiple" else "Pick one:"]
        for number, option in enumerate(data.get("options") or [], 1):
            label = option["label"] + (" (recommended)" if option.get("recommended") else "")
            lines.append(f"{number}. {label}" + (f": {option['detail']}" if option.get("detail") else ""))
        lines += ["", "Reply with your choice, or tell me what you'd rather do."]
    elif kind == "message":
        lines.append("")
        lines.append("Channel: " + str(data.get("channel") or "message"))
        for key, label in (("from", "From"), ("to", "To"), ("cc", "Cc"), ("thread", "Thread"), ("subject", "Subject"),
                           ("attachments", "Attachments")):
            value = data.get(key)
            if value:
                lines.append(f"{label}: " + (", ".join(value) if isinstance(value, list) else str(value)))
        lines += ["", str(data.get("body") or ""), "", ASK_SEND]
    elif kind == "form":
        lines += ["", "Please answer:"]
        for field in data.get("fields") or []:
            line = "- " + field["label"] + (" (required)" if field.get("required") else "")
            if field.get("options"):
                line += ": " + " / ".join(option["label"] for option in field["options"])
            if field.get("help_text"):
                line += f" ({field['help_text']})"
            lines.append(line)
        lines += ["", "Reply with your answers."]
    elif kind == "checklist":
        lines.append("")
        lines += [f"[{'x' if item.get('done') else ' '}] {item['label']}" + (f": {item['detail']}" if item.get("detail") else "")
                  for item in data.get("items") or []]
        lines += ["", "Reply with what to tick or change."]
    return "\n".join(lines).strip()


def ask_in_words(card: dict[str, Any], task_id: str) -> dict[str, Any]:
    """What a card tool returns inside a Kanban worker for a card the person would answer: nothing is shown or
    recorded; the worker blocks its task with the question in words and stops."""
    reason = block_question(card)
    step = ("Cards aren't shown on Kanban tasks. Ask in words instead: call "
            f"kanban_block(kind=\"needs_input\", reason=<block_reason, verbatim>) on task {task_id} and stop. "
            "Don't call another card tool for this question. The person answers with a comment on the task, "
            "and Hermes wakes you when it lands: read the latest comments first and treat that comment as the answer.")
    if card.get("type") == "message":
        step += (" Send nothing until their comment clearly says to send it; if it asks for changes, rewrite the draft "
                 "and ask again the same way. If they edited the wording, send their wording exactly.")
    return {"shown": False, "task_id": task_id, "block_reason": reason, "next_step": step}


# --- Guard (pre_tool_call) ------------------------------------------------------------------------

def tool_call_block(tool_name: str, args: Any, *, session_id: str, decisions: DecisionLog,
                    family_reader: Callable[[str | None, str], list[str] | None] | None = None,
                    env: Any = None) -> str | None:
    """The contract's Guard. Returns the block message, or None to let the call through.

    Coordinator (the session is a registered fence): every kanban tool stays inside the root's family;
    ``kanban_create`` must hang off a family member. A worker is no longer held back by an open task-bound
    card (those were removed on 2026-10-23); ``env`` stays a parameter for the tests and later rules."""
    env = os.environ if env is None else env
    args = args if isinstance(args, dict) else {}
    if not isinstance(tool_name, str) or not tool_name.startswith("kanban_"):
        return None
    fence = decisions.coordination_for_session(session_id or "")
    if fence is None:
        return None
    root = fence["root_id"]
    family = {root}
    if family_reader is not None:
        try:
            family.update(family_reader(fence.get("board"), root) or [])
        except Exception:  # a board we can't read right now fences to the root alone
            pass
    named = ", ".join(sorted(family))
    if tool_name == "kanban_create":
        parents = [p for p in (args.get("parents") or []) if isinstance(p, str)] if isinstance(args.get("parents"), list) else []
        if not parents or not any(p in family for p in parents):
            return (f"This session coordinates task {root}: a new task must list the root or a family member in "
                    f"parents ({named}).")
        outsiders = [p for p in parents if p not in family]
        if outsiders:
            return f"This session coordinates task {root}: {', '.join(outsiders)} is outside its family ({named})."
        return None
    keys = ("parent_id", "child_id") if tool_name == "kanban_link" else ("task_id",)
    outsiders = [args[k] for k in keys if isinstance(args.get(k), str) and args[k] and args[k] not in family]
    if outsiders:
        return f"This session coordinates task {root}: {', '.join(outsiders)} is outside its family ({named})."
    return None


# --- Hermes adapter -------------------------------------------------------------------------------

def _hermes_helpers():
    """Hermes's own board helpers (``_task_dict``, diagnostics), when its dashboard plugin imports."""
    try:
        return importlib.import_module("plugins.kanban.dashboard.plugin_api")
    except Exception:
        return None


class HermesBoard:
    """One Kanban board through ``hermes_cli.kanban_db`` inside the ``hermes serve`` process, the way
    ``plugins/kanban/dashboard/plugin_api.py`` opens it. Reads use Hermes's functions and read-only SQL;
    writes use Hermes's public mutators. Close it after the request."""

    def __init__(self, board: str | None = None):
        from hermes_cli import kanban_db
        from hermes_cli import kanban_db_connect as kbc
        self.kanban_db = kanban_db
        slug = normalize_slug(board)
        if slug and slug != kanban_db.DEFAULT_BOARD and not kanban_db.board_exists(slug):
            raise BoardError(404, f"board {slug!r} does not exist")
        try:
            kanban_db.init_db(board=slug)  # idempotent; the same self-heal Hermes's board route does
        except Exception:
            pass
        self.conn = kbc.connect(board=slug)
        self.requested = slug
        self.slug = slug or resolve_board()
        self._helpers = _hermes_helpers()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def reopened(self) -> "HermesBoard":
        """The same board on a fresh connection, for work that outlives the request (the stall check)."""
        return HermesBoard(self.requested)

    def _task_dict(self, task: Any, latest_summary: str | None) -> dict[str, Any]:
        if self._helpers is not None and hasattr(self._helpers, "_task_dict"):
            try:
                return self._helpers._task_dict(task, latest_summary=latest_summary)
            except Exception:
                pass
        row = asdict(task) if is_dataclass(task) else dict(task)
        row["latest_summary"] = latest_summary
        return row

    def tasks(self, include_archived: bool = False) -> list[dict[str, Any]]:
        tasks = self.kanban_db.list_tasks(self.conn, include_archived=include_archived)
        summaries = self.kanban_db.latest_summaries(self.conn, [t.id for t in tasks])
        counts = {r["task_id"]: r["n"] for r in self.conn.execute(
            "SELECT task_id, COUNT(*) AS n FROM task_comments GROUP BY task_id")}
        diagnostics: dict[str, list[dict]] = {}
        if self._helpers is not None and hasattr(self._helpers, "_compute_task_diagnostics"):
            try:
                diagnostics = self._helpers._compute_task_diagnostics(self.conn, task_ids=None) or {}
            except Exception:
                diagnostics = {}
        rows = []
        for task in tasks:
            row = self._task_dict(task, summaries.get(task.id))
            row["comment_count"] = counts.get(task.id, 0)
            diags = diagnostics.get(task.id)
            if diags:
                if self._helpers is not None and hasattr(self._helpers, "_attach_diagnostics"):
                    self._helpers._attach_diagnostics(row, diags)
                else:
                    row["diagnostics"] = diags
            rows.append(row)
        return rows

    def task(self, task_id: str) -> dict[str, Any] | None:
        task = self.kanban_db.get_task(self.conn, task_id)
        if task is None:
            return None
        return self._task_dict(task, self.kanban_db.latest_summaries(self.conn, [task_id]).get(task_id))

    def events(self, task_ids: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
        """The board's relevant events by task, oldest first (one read-only query)."""
        query = f"SELECT id, task_id, kind, payload, created_at, run_id FROM task_events WHERE kind IN ({','.join('?' * len(EVENT_KINDS))})"
        params: list[Any] = list(EVENT_KINDS)
        if task_ids is not None:
            if not task_ids:
                return {}
            query += f" AND task_id IN ({','.join('?' * len(task_ids))})"
            params.extend(task_ids)
        out: dict[str, list[dict[str, Any]]] = {}
        for row in self.conn.execute(query + " ORDER BY created_at ASC, id ASC", params):
            try:
                payload = json.loads(row["payload"]) if row["payload"] else None
            except (TypeError, ValueError):
                payload = None
            out.setdefault(row["task_id"], []).append({"id": row["id"], "task_id": row["task_id"], "kind": row["kind"],
                                                       "payload": payload, "created_at": row["created_at"],
                                                       "run_id": row["run_id"]})
        return out

    def links(self) -> list[tuple[str, str]]:
        return [(r["parent_id"], r["child_id"]) for r in self.conn.execute(
            "SELECT parent_id, child_id FROM task_links ORDER BY rowid")]

    def unsatisfied_parents(self, task_id: str) -> list[tuple[str, str]]:
        return [(pid, status) for pid, status in self.kanban_db.unsatisfied_parents(self.conn, task_id)]

    def comment(self, task_id: str, comment_id: int) -> dict[str, Any] | None:
        for row in self.kanban_db.list_comments(self.conn, task_id):
            if row.id == comment_id:
                return asdict(row)
        return None

    def latest_event_id(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM task_events").fetchone()
        return int(row["m"] if row else 0)

    # writes: Hermes's public mutators only
    def create(self, *, title: str, body: str | None, assignee: str | None, priority: int, triage: bool,
               idempotency_key: str | None, created_by: str) -> str:
        """``kanban_db.create_task`` as Hermes's dashboard calls it: Ready unless ``triage``."""
        return str(self.kanban_db.create_task(self.conn, title=title, body=body or None, assignee=assignee or None,
                                              created_by=created_by, priority=int(priority), triage=bool(triage),
                                              idempotency_key=idempotency_key or None, board=self.requested))

    def dispatch(self) -> None:
        """Nudge Hermes's dispatcher so a Ready task starts now rather than on its next minute tick (what
        the desktop board does after a change). Best-effort: a failure here never fails the change."""
        try:
            from hermes_cli import kanban_db_dispatch as kbd
            kbd.dispatch_once(self.conn, dry_run=False, max_spawn=8, board=self.requested)
        except Exception:
            pass

    def add_comment(self, task_id: str, author: str, body: str) -> int:
        return int(self.kanban_db.add_comment(self.conn, task_id, author, body))

    def promote(self, task_id: str, actor: str, reason: str | None) -> tuple[bool, str | None]:
        ok, why = self.kanban_db.promote_task(self.conn, task_id, actor=actor, reason=reason)
        return bool(ok), why

    def unblock(self, task_id: str) -> bool:
        return bool(self.kanban_db.unblock_task(self.conn, task_id))

    def release_triage(self, task_id: str, author: str) -> bool:
        # No title/body/assignee: preserve the existing brief. Hermes moves triage -> todo,
        # then recomputes readiness with its dependency gates. No LLM specification runs.
        return bool(self.kanban_db.specify_triage_task(self.conn, task_id, author=author))

    def reopen_review(self, task_id: str) -> bool:
        return bool(self.kanban_db.reopen_review_task(self.conn, task_id))

    def wait_external(self, task_id: str, reason: str, run_id: int) -> bool:
        return bool(self.kanban_db.schedule_task(self.conn, task_id, reason=EXTERNAL_WAIT_PREFIX + reason,
                                               expected_run_id=run_id))

    def reopen_done(self, task_id: str) -> tuple[bool, str | None]:
        """Done -> Ready through Hermes's own ``PATCH /tasks/{id}`` handler (called in-process): Hermes has
        no public reopen mutator, and its handler also resets the steps that waited on this one."""
        helpers = self._helpers
        if helpers is None or not hasattr(helpers, "update_task") or not hasattr(helpers, "UpdateTaskBody"):
            return False, "Hermes's board API is not available here"
        try:
            helpers.update_task(task_id, helpers.UpdateTaskBody(status="ready"), board=self.requested)
        except Exception as error:
            return False, str(getattr(error, "detail", None) or error)
        return True, None

    def hold(self, task_id: str) -> tuple[bool, str | None]:
        """Any state but running -> Todo, so the task waits for its parents, through Hermes's own
        ``PATCH /tasks/{id}`` handler (done reopens, review re-gates, triage and blocked move directly)."""
        helpers = self._helpers
        if helpers is None or not hasattr(helpers, "update_task") or not hasattr(helpers, "UpdateTaskBody"):
            return False, "Hermes's board API is not available here"
        try:
            helpers.update_task(task_id, helpers.UpdateTaskBody(status="todo"), board=self.requested)
        except Exception as error:
            return False, str(getattr(error, "detail", None) or error)
        return True, None

    def archive(self, task_id: str) -> bool:
        return bool(self.kanban_db.archive_task(self.conn, task_id))

    def link(self, parent_id: str, child_id: str) -> None:
        self.kanban_db.link_tasks(self.conn, parent_id, child_id)  # ValueError: cycle, running child

    def unlink(self, parent_id: str, child_id: str) -> bool:
        return bool(self.kanban_db.unlink_tasks(self.conn, parent_id, child_id))


def wait_for_external_reply(board: Any, task_id: str, run_id: int, reason: str) -> dict[str, Any]:
    """Park only the current worker run (``schedule_task`` fenced to ``run_id``)."""
    if not board.wait_external(task_id, reason, run_id):
        return {"error": "stale_run", "message": "This run no longer owns the task; no task changed."}
    return {"status": "waiting", "task_id": task_id,
            "instruction": "Stop. Resume only when the reply arrives or the person changes the instruction. "
                           "No timer or automatic inbox polling was created. Do not ask for approval again."}


def family_ids(board: str | None, root_id: str, decisions: DecisionLog | None = None) -> list[str] | None:
    """Contained descendants of ``root_id`` for the coordinator guard; None when Hermes can't be read."""
    try:
        reader = HermesBoard(board)
    except Exception:
        return None
    try:
        stored = decisions.containment_pairs(reader.slug) if decisions is not None else ()
        _parent_of, children_of = containment(reader.events(), stored)
        return descendants(children_of, root_id)
    except Exception:
        return None
    finally:
        reader.close()


# --- The coordinator's step tool and stall check (Mark, 2026-10-11) -------------------------------
# A comment on a main task opens a coordinator that reviews the whole family. Hermes's kanban tools let it
# comment, add steps and wake a blocked step, but not release a parked (triage) step, reopen a finished one,
# drop one or remove an ordering link: a provider-research step sat in triage while the coordinator
# reported "kanban_unblock cannot resume it". ``hermes_task_adjust`` gives it those moves, fenced to the
# root it coordinates.

ADJUST_ACTIONS = ("restart", "redo", "drop", "link", "unlink", "add")
COORDINATOR_AUTHOR = "coordinator"


def adjust_step(board: Any, decisions: DecisionLog, root_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """One change to the coordinated family. A refusal comes back as ``error`` + ``message`` (never raised)."""
    action = args.get("action")
    if action not in ADJUST_ACTIONS:
        return {"error": "invalid_action", "message": f"action is one of {', '.join(ADJUST_ACTIONS)}."}
    _parent_of, children_of = containment(board.events(), decisions.containment_pairs(board.slug))
    family = [root_id, *descendants(children_of, root_id)]
    outside = {"error": "outside_family", "message": f"Only this task's steps can change: {', '.join(family)}."}

    def member(key: str) -> str | None:
        value = args.get(key)
        return value if isinstance(value, str) and value in family else None

    instruction = args.get("instruction").strip()[:8_000] if isinstance(args.get("instruction"), str) else ""
    if action == "add":
        return _add_step(board, decisions, root_id, args, instruction, family)
    if action in ("link", "unlink"):
        parent, child = member("parent_id"), member("child_id")
        if not parent or not child:
            return outside
        if action == "link":
            try:
                board.link(parent, child)
            except ValueError as error:
                return {"error": "refused", "message": str(error)}
        elif not board.unlink(parent, child):
            return {"error": "no_link", "message": f"{child} does not wait for {parent}."}
        board.dispatch()
        return {"status": f"{action}ed", "parent_id": parent, "child_id": child,
                "child_status": (board.task(child) or {}).get("status")}
    task_id = member("task_id")
    if not task_id:
        return outside
    task = board.task(task_id)
    if task is None:
        return {"error": "not_found", "message": f"{task_id} is not on the board."}
    status = task.get("status")
    if status == "archived":
        return {"error": "dropped", "message": "This step was dropped; add a new step instead."}
    if action == "drop":
        if task_id == root_id:
            return {"error": "root", "message": "The main task can't be dropped; drop or change its steps."}
        if not instruction:
            return {"error": "reason_required", "message": "Say why the step no longer makes sense (instruction)."}
        board.add_comment(task_id, COORDINATOR_AUTHOR, f"Dropped: {instruction}")
        if not board.archive(task_id):
            return {"error": "refused", "message": "Hermes did not archive the step."}
        board.dispatch()  # an archived step no longer holds up the steps after it
        return {"status": "dropped", "task_id": task_id}
    if action == "redo" and not instruction:
        return {"error": "instruction_required", "message": "Say what to do differently (instruction)."}
    if action == "restart" and status == "done":
        return {"error": "finished", "message": "This step is finished; use redo with what to change."}
    if instruction:
        board.add_comment(task_id, COORDINATOR_AUTHOR, instruction)
    ok, why = _wake(board, task_id, status)
    if not ok:
        return {"error": "refused", "message": why or "Hermes did not move the step.", "task_id": task_id}
    landed = (board.task(task_id) or {}).get("status")
    result: dict[str, Any] = {"status": landed, "task_id": task_id}
    if landed == "todo":
        waiting = ", ".join(f"{pid} ({s})" for pid, s in board.unsatisfied_parents(task_id))
        result["detail"] = (f"Waits for {waiting}: restart those, or unlink them if this step should not wait."
                            if waiting else "Queued; Hermes starts it on its next pass.")
    elif landed == "running":
        result["detail"] = "Already running; your instruction is on the step for its next turn."
    board.dispatch()
    return result


def _add_step(board: Any, decisions: DecisionLog, root_id: str, args: dict[str, Any], instruction: str,
              family: list[str]) -> dict[str, Any]:
    """A new step inside the family: created parked, recorded as contained (so the app lists it under the
    main task), ordered after ``after``, and the main task waits for it, reopened to Todo if it had finished,
    been parked or blocked. Then the step is released, so it starts unless it waits for something."""
    title = args.get("title").strip()[:200] if isinstance(args.get("title"), str) else ""
    if not title or not instruction:
        return {"error": "title_and_instruction_required", "message": "Give the new step a title and an instruction."}
    after = args.get("after") if isinstance(args.get("after"), list) else []
    if any(not isinstance(a, str) or a not in family for a in after):
        return {"error": "outside_family", "message": f"after names steps of this task only: {', '.join(family)}."}
    root = board.task(root_id) or {}
    if root.get("status") == "running":
        return {"error": "root_running", "message": "The main task is running; add the step after its turn ends."}
    step_id = board.create(title=title, body=instruction, assignee=root.get("assignee"),
                           priority=int(root.get("priority") or 0), triage=True, idempotency_key=None,
                           created_by=COORDINATOR_AUTHOR)
    decisions.record_containment(board.slug, root_id, [step_id])
    for parent in dict.fromkeys(after):
        board.link(parent, step_id)
    board.link(step_id, root_id)
    if (board.task(root_id) or {}).get("status") != "todo":
        ok, why = board.hold(root_id)
        if not ok:
            return {"error": "refused", "message": f"Added {step_id}, but the main task did not reopen: {why}",
                    "task_id": step_id}
    board.release_triage(step_id, COORDINATOR_AUTHOR)
    board.dispatch()
    result: dict[str, Any] = {"status": (board.task(step_id) or {}).get("status"), "task_id": step_id,
                              "root_status": (board.task(root_id) or {}).get("status")}
    if result["status"] == "todo":
        result["detail"] = "Waits for " + ", ".join(f"{p} ({s})" for p, s in board.unsatisfied_parents(step_id)) + "."
    return result


def _wake(board: Any, task_id: str, status: str | None) -> tuple[bool, str | None]:
    """Move one step toward Ready by the route Hermes allows from where it is."""
    if status in ("ready", "running"):
        return True, None
    if status == "triage":
        if not board.release_triage(task_id, COORDINATOR_AUTHOR):
            return False, "Hermes did not release the step from triage."
        status = (board.task(task_id) or {}).get("status")
    if status == "todo":
        if board.unsatisfied_parents(task_id):
            return True, None  # it stays queued behind its parents; the result says which
        return board.promote(task_id, COORDINATOR_AUTHOR, "coordinator: the plan changed")
    if status in ("blocked", "scheduled"):
        return board.unblock(task_id), None
    if status == "review":
        return board.reopen_review(task_id), None
    if status == "done":
        return board.reopen_done(task_id)
    return True, None


def stall_line(root_id: str, family: list[str], *, tasks_by_id: dict[str, dict[str, Any]],
               links: list[tuple[str, str]], events_by_task: dict[str, list[dict[str, Any]]]) -> tuple[str, dict[str, Any]] | None:
    """``(line, step)`` when nothing in the family can move and nothing already asks the person: every
    unfinished member sits in triage or todo, none escalated. ``step`` is the one that should be running
    (waits for nothing unfinished), else the first stuck one. None otherwise."""
    members = [tasks_by_id[t] for t in [root_id, *family] if t in tasks_by_id]
    unfinished = [m for m in members if m.get("status") not in TERMINAL]
    if not unfinished:
        return None
    if any(m.get("status") not in ("triage", "todo") for m in unfinished):
        return None
    if any(loop_escalated(events_by_task.get(m["id"], [])) for m in unfinished):
        return None
    steps = [m for m in unfinished if m["id"] != root_id] or unfinished
    head = next((m for m in steps if not waits_on(m["id"], links, tasks_by_id)), None)
    step = head or steps[0]
    title = step.get("title") or step["id"]
    if head is not None:
        why = f"“{title}” hasn't started" + (" (parked in triage)" if step.get("status") == "triage" else "")
    else:
        names = ", ".join(f"“{tasks_by_id[p].get('title') or p}”" for p in waits_on(step["id"], links, tasks_by_id))
        why = f"“{title}” waits for {names}"
    line = f"Nothing in this task is moving: {why}. Reply with how to continue."
    return line, {"task_id": step["id"], "title": step.get("title"), "state": "needs_you",
                  "block_kind": step.get("block_kind"), "reason": line}


def after_coordination(board_ref: Any, root_id: str, decisions: DecisionLog, *,
                       board_factory: Callable[[Any], Any] | None = None) -> str | None:
    """When the coordinator's turn ends: if nothing in the family moves, remember the stall so the board
    puts the main task in front of the person; otherwise forget any earlier one. Returns the line."""
    board = (board_factory or HermesBoard)(board_ref)
    try:
        events = board.events()
        _parent_of, children_of = containment(events, decisions.containment_pairs(board.slug))
        tasks_by_id = {t["id"]: t for t in board.tasks(include_archived=True)}
        found = stall_line(root_id, descendants(children_of, root_id), tasks_by_id=tasks_by_id, links=board.links(),
                           events_by_task=events)
        was = root_id in decisions.stalls(board.slug)
        if found and not was:
            decisions.record_stall(board.slug, root_id)
        elif was and not found:
            decisions.clear_stall(board.slug, root_id)
        if bool(found) != was:  # only a change rings the phone or clears its alert
            try:
                attention_after(board, decisions, None, force=True)
            except Exception:
                pass
        return found[0] if found else None
    finally:
        board.close()


# --- Route bodies (framework-free; the router below and the QA fixture both call them) -----------

def board_payload(board: Any, decisions: DecisionLog, *, include_archived: bool = False) -> dict[str, Any]:
    tasks = board.tasks(include_archived=True)  # containment and dependencies see archived members too
    payload = compose_board(board=board.slug, tasks=tasks, events_by_task=board.events(), links=board.links(),
                            latest_event_id=board.latest_event_id(),
                            include_archived=include_archived, stored_containment=decisions.containment_pairs(board.slug),
                            preparing=decisions.preparing_tasks(board.slug), stalls=decisions.stalls(board.slug),
                            seen=decisions.reviews_seen(board.slug))
    # A family derived from Hermes's events is remembered, so the board no longer depends on those payloads.
    derived: dict[str, list[str]] = {}
    for root, child in payload.get("containment", []):
        derived.setdefault(root, []).append(child)
    for root, children in derived.items():
        decisions.record_containment(board.slug, root, children)
    return payload


def attention_after(board: Any, decisions: DecisionLog, task_id: str | None = None, *, force: bool = True) -> dict[str, Any]:
    """Tell the mobile module what needs the person now (and that ``task_id`` no longer does, when so).
    Called after every change a route makes; the mobile module diffs the list to clear stale alerts."""
    payload = board_payload(board, decisions)
    summary = notify.attention_summary(payload, decisions.open_chat_decisions())
    notify.attention(summary, force=force)
    if task_id and task_id not in {row["id"] for row in summary["tasks"]}:
        task = next((t for t in payload["tasks"] if t["id"] == task_id), None)
        notify.task_event("resolved", board=board.slug, task_id=task_id, title=(task or {}).get("title"),
                          bot=(task or {}).get("assignee"), kind="resolved")
    return summary


# --- Quick Add (one call, 2026-10-08) ---------------------------------------------------------------

def quick_add(board: Any, decisions: DecisionLog, body: dict[str, Any], *, specify: Callable[[str, str], dict[str, Any]],
              decompose: Callable[[str, str], dict[str, Any]], creation_log: Any,
              background: Callable[[Callable[[], None]], None] | None = None,
              board_factory: Callable[[Any], Any] | None = None) -> tuple[int, dict[str, Any]]:
    """``POST tasks/quick-add``: the person's words become a task in one call. With ``write_up`` the task
    goes in as Triage and Hermes's specifier writes the title and brief in the background; ``start`` then
    promotes it to Ready. With creation-only decomposition on (Board Settings) and ``start``, the
    planner splits it instead and owns its gating. Without a write-up it goes in as typed: Ready, or
    held in Triage. ``client_key`` makes a retry return the same task.

    Returns ``(status, {task_id, status, preparing, title})``; the board lists the task at once with
    ``preparing`` while the write-up runs."""
    text = body.get("text") if isinstance(body.get("text"), str) else ""
    text = text.strip()[:65_536]
    if not text:
        return 400, {"detail": "text is required"}
    key = body.get("client_key") if isinstance(body.get("client_key"), str) and body.get("client_key") else None
    if key:
        previous = decisions.quick_add(key)
        if previous is not None:
            task = board.task(previous["task_id"])
            return 200, {"task_id": previous["task_id"], "status": (task or {}).get("status"),
                         "preparing": previous["state"] == "writing", "title": (task or {}).get("title"), "replayed": True}
    author = _author(body)
    assignee = body.get("assignee") if isinstance(body.get("assignee"), str) and body.get("assignee") else None
    try:
        priority = int(body.get("priority", 0))
    except (TypeError, ValueError):
        priority = 0
    write_up = body.get("write_up") is not False
    start = body.get("start") is not False
    decompose_on = bool(start and creation_log is not None and creation_log.enabled() and not _background_enabled())
    title = quick_title(text)
    triage = write_up or decompose_on or not start
    try:
        task_id = board.create(title=title, body=text if text != title else None, assignee=assignee, priority=priority,
                               triage=triage, idempotency_key=key, created_by=author)
    except Exception as error:
        return 400, {"detail": f"Hermes refused the task: {error}"}
    preparing = write_up or decompose_on
    if key:
        decisions.quick_add_begin(key, board=board.slug, task_id=task_id, state="writing" if preparing else "done")
    if preparing:
        slug, requested = board.slug, getattr(board, "requested", None)

        def finish() -> None:
            _finish_quick_add(requested or slug, decisions, key, task_id, author=author, start=start, decompose_on=decompose_on,
                              specify=specify, decompose=decompose, creation_log=creation_log, board_factory=board_factory)

        (background or _spawn)(finish)
    elif start:
        board.dispatch()
    task = board.task(task_id) or {}
    return 200, {"task_id": task_id, "status": task.get("status"), "preparing": preparing, "title": task.get("title") or title}


def _spawn(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="agents-cs-quick-add", daemon=True).start()


def _background_enabled() -> bool:
    try:
        from . import creation
    except ImportError:  # pragma: no cover
        import creation  # type: ignore
    try:
        return creation.background_enabled()
    except Exception:
        return False


def _finish_quick_add(board_ref: Any, decisions: DecisionLog, key: str | None, task_id: str, *, author: str, start: bool,
                      decompose_on: bool, specify: Callable[[str, str], dict[str, Any]],
                      decompose: Callable[[str, str], dict[str, Any]], creation_log: Any,
                      board_factory: Callable[[Any], Any] | None = None) -> dict[str, Any]:
    """The write-up or planning after the task exists; runs off the request. Never retries, never
    promotes a decomposed root (the planner owns its gating); a failed write-up keeps the person's words
    and still starts the task when asked."""
    try:
        from . import creation
    except ImportError:  # pragma: no cover
        import creation  # type: ignore
    board = (board_factory or HermesBoard)(board_ref)
    result: dict[str, Any]
    try:
        if decompose_on:
            receipt = (board.task(task_id) or {}).get("idempotency_key") or key or ""
            result = creation.prepare(board, decisions, creation_log, task_id, receipt,
                                      lambda tid, slug: decompose(tid, slug))
            if result.get("child_ids"):
                decisions.record_containment(board.slug, task_id, [c for c in result["child_ids"] if isinstance(c, str)])
        else:
            try:
                result = specify(task_id, board.slug) or {}
            except Exception as error:
                result = {"ok": False, "reason": str(error)}
            task = board.task(task_id)
            if task is None:
                result = {"ok": False, "reason": "The task was removed before the write-up finished."}
            elif start and task.get("status") in ("triage", "todo"):
                if task.get("status") == "triage":
                    board.release_triage(task_id, author)
                    task = board.task(task_id) or task
                if task.get("status") == "todo":
                    board.promote(task_id, author, "Quick Add: start right away")
                board.dispatch()
    except Exception as error:
        result = {"ok": False, "reason": f"The write-up stopped before confirming an outcome: {error}"}
    finally:
        if key:
            decisions.quick_add_finish(key, state="done" if result.get("ok") else "failed", detail=result.get("reason"))
        try:
            attention_after(board, decisions, None, force=True)
        except Exception:
            pass
        board.close()
    return result


def see_review(board: Any, decisions: DecisionLog, task_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """``POST tasks/{id}/seen``: the person opened a root on Review (a review, a question, a block or a stall). Recorded at the ``review_key`` the
    app saw, only while it is still the board's, so a review requested since counts again. The root stays on Review;
    it leaves the badge, and its alert clears on every device."""
    if not _TASK_ID.fullmatch(task_id or ""):
        return 404, {"detail": "task_not_found"}
    key = body.get("review_key")
    row = next((t for t in board_payload(board, decisions)["tasks"] if t["id"] == task_id), None)
    if row is None:
        return 404, {"detail": "task_not_found"}
    current = (row.get("attention") or {}).get("review_key")
    if not isinstance(key, str) or current != key:
        return 200, {"seen": False}
    if not (row.get("attention") or {}).get("seen"):
        decisions.record_review_seen(board.slug, task_id, key)
        try:
            attention_after(board, decisions, task_id)
        except Exception:
            pass
    return 200, {"seen": True}


def _author(body: dict[str, Any]) -> str:
    author = body.get("author")
    return (author.strip()[:80] if isinstance(author, str) and author.strip() else "app")


def comment_on_task(board: Any, decisions: DecisionLog, task_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """``POST tasks/{id}/comments``: ``(status_code, payload)``. Each step is persisted before the next,
    so once the comment exists every later failure is reported as ``follow_up: failed``. A ``client_key``
    the app already sent answers with the stored result, so a retried send never posts twice."""
    if not _TASK_ID.fullmatch(task_id or ""):
        return 404, {"detail": "task_not_found"}
    key = body.get("client_key") if isinstance(body.get("client_key"), str) and body.get("client_key") else None
    if key:
        replay = decisions.recall_comment(key)
        if replay is not None:
            return 200, {**replay, "replayed": True}
    status_code, payload = _comment_on_task(board, decisions, task_id, body)
    if key and status_code == 200 and payload.get("comment_id") is not None:
        decisions.remember_comment(key, board=board.slug, task_id=task_id, comment_id=payload.get("comment_id"), result=payload)
    if status_code == 200 and payload.get("comment_id") is not None:
        if payload.get("follow_up") in ("ready", "reopened"):
            board.dispatch()
        try:
            attention_after(board, decisions, task_id)
        except Exception:
            pass
    return status_code, payload


def _comment_on_task(board: Any, decisions: DecisionLog, task_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    task = board.task(task_id)
    if task is None:
        return 404, {"detail": "task_not_found"}
    if task.get("status") == "archived":
        return 409, {"detail": "restore_required"}
    author = _author(body)
    text = body.get("text") if isinstance(body.get("text"), str) else ""
    text = text.strip()[:65_536]
    # Hermes hands hooks a session id that may be the runtime id (per socket) or the stored id; the app sends
    # every id it has and each one is fenced to the root (2026-10-05, live gate).
    session_ids = [body.get("coordinator_session_id")] if isinstance(body.get("coordinator_session_id"), str) else []
    extra = body.get("coordinator_session_ids")
    if isinstance(extra, list):
        session_ids += [x for x in extra if isinstance(x, str)]
    session_ids = list(dict.fromkeys(x for x in session_ids if x))
    resume_id = body.get("resume_comment_id")
    resume_id = int(resume_id) if isinstance(resume_id, int) and not isinstance(resume_id, bool) else None

    events = board.events()
    _parent_of, children_of = containment(events, decisions.containment_pairs(board.slug))
    tasks_by_id = {t["id"]: t for t in board.tasks(include_archived=True)}
    family = descendants(children_of, task_id)
    decomposed = any(c in tasks_by_id and tasks_by_id[c].get("status") != "archived" for c in family)

    if resume_id is not None and not text:
        # Resume: the comment exists; only the fence and the instruction are wanted.
        stored = board.comment(task_id, resume_id)
        if stored is None:
            return 404, {"detail": "comment_not_found"}
        if not decomposed:
            return 200, {"comment_id": resume_id, "status": task.get("status"), "route": "leaf", "follow_up": "none",
                         "detail": "Nothing to coordinate: the task has no steps.", "coordination": None}
        return 200, _coordinate(board, decisions, task, stored["body"], resume_id, session_ids, children_of, tasks_by_id)
    if not text:
        return 400, {"detail": "text is required"}

    if task.get("status") == "done":
        affected = [{"id": d, "title": tasks_by_id[d].get("title"), "status": tasks_by_id[d].get("status")}
                    for d in dependents(task_id, board.links()) if d in tasks_by_id]
        if affected and body.get("confirm_reopen") is not True:
            return 409, {"detail": "reopen_affects", "affected": affected}

    try:
        comment_id = board.add_comment(task_id, author, text)
    except Exception as error:
        return 500, {"detail": f"Hermes refused the comment: {error}"}

    # Follow-up by the status after the comment.
    try:
        task = board.task(task_id) or task
        status = task.get("status")
        # A card bound before 2026-10-23 is retired, never approved: the comment is the answer now.
        for action in decisions.open_task_cards(task_id):
            decisions.withdraw(action["action_id"])
        step, detail = follow_up_plan(status, decomposed=decomposed,
                                      unsatisfied=board.unsatisfied_parents(task_id) if status in ("triage", "todo", "blocked") else [])
        if (not decomposed and status == "scheduled"
                and (block_reason(events.get(task_id, [])) or "").startswith(EXTERNAL_WAIT_PREFIX)):
            # A new human reply is the awaited event; ordinary time schedules stay intact.
            step, detail = "unblock", None
        if step == "coordinate":
            return 200, _coordinate(board, decisions, task, text, comment_id, session_ids, children_of, tasks_by_id)
        route = "leaf"
        if step == "promote" or step == "reopen_done":
            ok, why = board.promote(task_id, author, "comment from the Agents CS app")
            follow_up, detail = ("ready" if step == "promote" else "reopened", None) if ok else ("failed", why)
        elif step in ("unblock", "release_triage"):
            ok = board.unblock(task_id) if step == "unblock" else board.release_triage(task_id, author)
            landed = (board.task(task_id) or task).get("status")
            if ok and landed in ("ready", "running", "scheduled", "review", "done"):
                follow_up, detail = ("ready" if landed == "ready" else "none"), None
            elif ok and landed == "todo":
                follow_up, detail = "waiting", "Waiting for dependencies."
            else:
                follow_up, detail = "failed", "Hermes could not resume the task."
        elif step == "reopen_review":
            ok = board.reopen_review(task_id)
            follow_up, detail = ("reopened", None) if ok else ("failed", "Hermes could not reopen the review.")
        else:
            follow_up = step  # waiting | none
        status = (board.task(task_id) or task).get("status")
        return 200, {"comment_id": comment_id, "status": status, "route": route, "follow_up": follow_up,
                     "detail": detail, "coordination": None}
    except Exception as error:
        return 200, {"comment_id": comment_id, "status": task.get("status"), "route": "leaf", "follow_up": "failed",
                     "detail": str(error), "coordination": None}


def _coordinate(board: Any, decisions: DecisionLog, root: dict[str, Any], comment: str, comment_id: int,
                session_ids: list[str], children_of: dict[str, list[str]],
                tasks_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Open the orchestrator session here (``coordinator.start``), fence every id Hermes gave it and hand
    it the instruction. An app that still opens the session itself (before 2026-10-08) sends the ids and
    gets the instruction back to submit. Each parent comment gets its own session: a runtime session id
    only lives as long as the socket that made it (live gate, 2026-10-05)."""
    family = family_snapshot(root["id"], children_of, tasks_by_id, board.links())
    decisions.clear_stall(board.slug, root["id"])
    instruction = coordination_instruction(board=board.slug, root=root, comment=comment, family=family)
    base = {"comment_id": comment_id, "status": root.get("status"), "route": "coordinate"}
    if session_ids:
        for sid in session_ids:
            decisions.register_coordination(sid, board.slug, root["id"])
        return {**base, "follow_up": "coordinate", "detail": None,
                "coordination": {"session_id": session_ids[0], "root_id": root["id"], "instruction": instruction,
                                 "family": family, "submitted": False}}
    try:
        root_id, reopen = root["id"], getattr(board, "reopened", None)
        on_end = (lambda: after_coordination(None, root_id, decisions, board_factory=lambda _ref: reopen())) if callable(reopen) else None
        opened = coordinator.start(title=f"Coordinating: {root.get('title') or root['id']}", instruction=instruction,
                                   profile=root.get("assignee") if _orchestrator_is_assignee() else None, on_end=on_end)
    except Exception as error:
        return {**base, "follow_up": "coordination_not_started", "coordination": None,
                "detail": f"Saved. Coordination not started: {error}"}
    for sid in opened["session_ids"]:
        decisions.register_coordination(sid, board.slug, root["id"])
    return {**base, "follow_up": "coordinate", "detail": None,
            "coordination": {"session_id": opened["session_id"], "root_id": root["id"], "instruction": instruction,
                             "family": family, "submitted": True}}


def _orchestrator_is_assignee() -> bool:
    """Hermes's orchestration setting resolves the orchestrator profile itself; the root's assignee is
    never substituted (kept as a seam for a future per-board rule)."""
    return False


def answer_action(board_factory: Callable[[str | None], Any], decisions: DecisionLog, action_id: str,
                  body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """``POST actions/{action_id}/answers``: gone since task-bound cards were removed (2026-10-23). App builds from
    before then may still call it; they get 410 and nothing is recorded or posted. The person answers a worker's
    question with an ordinary comment (``POST tasks/{id}/comments``)."""
    return 410, {"detail": "task_cards_removed"}


# --- Router ---------------------------------------------------------------------------------------

def make_router(decisions: DecisionLog, board_factory: Callable[[str | None], Any] | None = None, *,
                specify: Callable[[str, str], dict[str, Any]] | None = None,
                decompose: Callable[[str, str], dict[str, Any]] | None = None, creation_log: Any = None):
    """The four routes on a FastAPI router. ``board_factory(board_slug)`` opens a board (``HermesBoard``
    in production, a fake in tests); the sync handlers run in FastAPI's threadpool like Hermes's own.
    ``specify``/``decompose`` run Hermes's specifier and decomposer with the serving scope (the dashboard
    module supplies them); ``creation_log`` holds the creation-only decomposition preference."""
    from fastapi import APIRouter, Body, Query
    from fastapi.responses import JSONResponse

    router = APIRouter()
    factory = board_factory or HermesBoard

    def opened(board: str | None):
        try:
            return factory(board or None), None
        except BoardError as error:
            return None, JSONResponse({"detail": error.detail}, status_code=error.status)
        except Exception as error:
            return None, JSONResponse({"detail": f"Kanban is not available: {error}"}, status_code=503)

    @router.get("/tasks/board")
    def tasks_board(board: str | None = Query(default=None), include_archived: bool = Query(default=False)):
        opened_board, failure = opened(board)
        if failure is not None:
            return failure
        try:
            return board_payload(opened_board, decisions, include_archived=include_archived)
        finally:
            opened_board.close()

    @router.post("/tasks/quick-add")
    def task_quick_add(body: dict[str, Any] = Body(...), board: str | None = Query(default=None)):
        opened_board, failure = opened(board)
        if failure is not None:
            return failure
        try:
            status, payload = quick_add(opened_board, decisions, body if isinstance(body, dict) else {},
                                        specify=specify or (lambda tid, slug: {"ok": False, "reason": "No specifier is configured."}),
                                        decompose=decompose or (lambda tid, slug: {"ok": False, "reason": "No decomposer is configured."}),
                                        creation_log=creation_log)
        finally:
            opened_board.close()
        return JSONResponse(payload, status_code=status)

    @router.post("/tasks/{task_id}/comments")
    def task_comment(task_id: str, body: dict[str, Any] = Body(...), board: str | None = Query(default=None)):
        opened_board, failure = opened(board)
        if failure is not None:
            return failure
        try:
            status, payload = comment_on_task(opened_board, decisions, task_id, body if isinstance(body, dict) else {})
        finally:
            opened_board.close()
        return JSONResponse(payload, status_code=status)

    @router.post("/tasks/{task_id}/seen")
    def task_seen(task_id: str, body: dict[str, Any] = Body(...), board: str | None = Query(default=None)):
        opened_board, failure = opened(board)
        if failure is not None:
            return failure
        try:
            status, payload = see_review(opened_board, decisions, task_id, body if isinstance(body, dict) else {})
        finally:
            opened_board.close()
        return JSONResponse(payload, status_code=status)

    @router.post("/actions/{action_id}/answers")
    def action_answer(action_id: str, body: dict[str, Any] = Body(...), board: str | None = Query(default=None)):
        status, payload = answer_action(lambda slug: factory(slug or board or None), decisions, action_id,
                                        body if isinstance(body, dict) else {})
        return JSONResponse(payload, status_code=status)

    return router
