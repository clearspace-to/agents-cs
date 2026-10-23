"""Form, decision and task routes for the Agents CS app (the ``cards`` module's router), mounted by
``hermes serve`` under ``/api/plugins/agents-cs/`` (``dashboard/plugin_api.py`` composes it) and protected
by Hermes' own dashboard authentication.

POST forms/{request_id}/submit   {"profile": "<bot>", "values": {...}} -> {state, code, message[, values]}
GET  forms/{request_id}?profile=  -> {state, title[, values]}
GET  decisions?profile=           -> {decisions: [{card_id, session_id, title, options, created_at}], closed: [card_id]}
POST decisions/close              {"profile": "<bot>", "text": "<the answer message>"} -> {closed}
POST kanban/specify/{task_id}     {"author": "<who>"} -> {ok, task_id, reason, new_title}
POST kanban/decompose/{task_id}   {"author": "<who>"} -> {ok, task_id, reason, fanout, child_ids, new_title}
GET  tasks/board, POST tasks/{id}/comments, POST actions/{id}/answers: the task experience (``tasks.py``,
docs/TASK_EXPERIENCE_API.md).

``kanban/specify`` is Hermes's own triage specifier (``hermes kanban specify``) run with the serving
profile's secret scope bound. Hermes's Kanban route ``/api/plugins/kanban/tasks/{id}/specify`` binds
none, so under a multiplexing gateway its model call fails with UnscopedSecretError (2026-10-03).

The answer is checked against the form the bot stored (``store.py``), so a malformed or second
answer is refused before the app sends the person's message.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from .. import paths
from . import creation as _creation
from . import notify as _notify
from . import tasks as _tasks
from .store import DecisionLog, FormStore

_REQUEST = re.compile(r"^[0-9a-f]{32}$")
_PROFILE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
router = APIRouter()
_store = FormStore()
_decisions = DecisionLog()
_creation_log = _creation.CreationLog()


def _checked(request_id: str, profile: str | None) -> None:
    if not _REQUEST.fullmatch(request_id):
        raise HTTPException(status_code=404, detail="Unknown form")
    if profile is not None and not _PROFILE.fullmatch(profile):
        raise HTTPException(status_code=400, detail="Bad profile")


@router.post("/forms/{request_id}/submit")
def submit(request_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    profile = body.get("profile")
    _checked(request_id, profile if isinstance(profile, str) else None)
    return _store.submit(request_id, body.get("values"), profile=profile if isinstance(profile, str) else None)


@router.get("/forms/{request_id}")
def status(request_id: str, profile: str | None = Query(default=None)) -> dict[str, Any]:
    _checked(request_id, profile)
    return _store.status(request_id, profile=profile)


@router.get("/decisions")
def open_decisions(profile: str = Query(...)) -> dict[str, Any]:
    """The bot's decisions still waiting on an answer, newest first, and the ones answered lately on any device."""
    if not _PROFILE.fullmatch(profile):
        raise HTTPException(status_code=400, detail="Bad profile")
    return {"decisions": _decisions.open(profile=profile), "closed": _decisions.closed(profile=profile)}


@router.post("/decisions/close")
def close_decisions(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """The app closes what it just answered at once, rather than waiting for the bot's next turn."""
    profile, text = body.get("profile"), body.get("text")
    if not isinstance(profile, str) or not _PROFILE.fullmatch(profile) or not isinstance(text, str):
        raise HTTPException(status_code=400, detail="Bad request")
    closed = _decisions.close_ids_from(text[:65_536], profile=profile)
    _notify.decisions_closed(profile, closed)
    return {"closed": len(closed)}


_TASK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _with_scope(run, board: str | None = None):
    """Run ``run()`` inside the serving home's secret scope (Hermes's own Kanban routes bind none), pinned to
    ``board`` the way Hermes's dashboard pins it (context-local, never the process env)."""
    from contextlib import nullcontext
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope

    pinned = nullcontext()
    if board:
        from hermes_cli import kanban_db
        pinned = kanban_db.scoped_current_board(_tasks.normalize_slug(board))
    home = paths.hermes_home()
    token = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
    try:
        with pinned:
            return run()
    finally:
        reset_secret_scope(token)


def specify_with_scope(task_id: str, author: str | None, board: str | None = None) -> dict[str, Any]:
    """Runs ``hermes_cli.kanban_specify.specify_task`` inside the serving home's secret scope."""
    from hermes_cli import kanban_specify

    outcome = _with_scope(lambda: kanban_specify.specify_task(task_id, author=author or None), board)
    return {"ok": bool(outcome.ok), "task_id": outcome.task_id, "reason": outcome.reason,
            "new_title": getattr(outcome, "new_title", None)}


def decompose_with_scope(task_id: str, author: str | None, board: str | None = None) -> dict[str, Any]:
    """Runs ``hermes_cli.kanban_decompose.decompose_task`` inside the serving home's secret scope: Hermes's
    own ``tasks/{id}/decompose`` route fails with UnscopedSecretError the same way specify does (2026-10-05)."""
    from hermes_cli import kanban_decompose

    outcome = _with_scope(lambda: kanban_decompose.decompose_task(task_id, author=author or None), board)
    return {"ok": bool(outcome.ok), "task_id": outcome.task_id, "reason": outcome.reason,
            "fanout": bool(getattr(outcome, "fanout", False)), "child_ids": list(getattr(outcome, "child_ids", None) or []),
            "new_title": getattr(outcome, "new_title", None)}


def _author(body: dict[str, Any]) -> str | None:
    author = body.get("author") if isinstance(body.get("author"), str) else None
    return author[:80] if author else None


# The task routes (tasks.py): the board, Quick Add, comments and answers. Quick Add runs the specifier
# or the decomposer with the serving scope, as the routes below do.
router.include_router(_tasks.make_router(
    _decisions,
    specify=lambda task_id, board: specify_with_scope(task_id, "app", board),
    decompose=lambda task_id, board: decompose_with_scope(task_id, "creation-planner", board),
    creation_log=_creation_log))


@router.get("/kanban/creation-settings")
def creation_settings() -> dict[str, Any]:
    return {"decompose_on_create": _creation_log.enabled()}


@router.put("/kanban/creation-settings")
def set_creation_settings(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    enabled = body.get("decompose_on_create")
    if not isinstance(enabled, bool):
        raise HTTPException(400, "decompose_on_create must be a boolean")
    if enabled and _creation.background_enabled():
        raise HTTPException(409, "Turn off background decomposition first.")
    _creation_log.set_enabled(enabled)
    return creation_settings()


@router.post("/kanban/prepare-new/{task_id}")
def prepare_new_task(task_id: str, body: dict[str, Any] = Body(...), board: str | None = Query(None)) -> dict[str, Any]:
    if not _TASK.fullmatch(task_id):
        raise HTTPException(404, "Unknown task")
    key = body.get("creation_key")
    if not isinstance(key, str) or not key or len(key) > 200:
        raise HTTPException(400, "Task creation receipt is required")
    # This operation is invoked only from creation, never from a board scan or a comment.
    if _creation.background_enabled():
        raise HTTPException(409, "Turn off background decomposition first.")
    opened = _tasks.HermesBoard(board)
    try:
        result = _creation.prepare(opened, _decisions, _creation_log, task_id, key,
                                   lambda tid, slug: decompose_with_scope(tid, _author(body), slug))
        if result.get("child_ids"):
            _decisions.record_containment(opened.slug, task_id, [c for c in result["child_ids"] if isinstance(c, str)])
        return result
    finally:
        opened.close()


@router.post("/kanban/specify/{task_id}")
def kanban_specify(task_id: str, body: dict[str, Any] = Body(default={}),
                   board: str | None = Query(default=None)) -> dict[str, Any]:
    """Quick Add's write-up. Sync, so it runs in the threadpool while the model writes (up to 120 s)."""
    if not _TASK.fullmatch(task_id):
        raise HTTPException(status_code=404, detail="Unknown task")
    return specify_with_scope(task_id, _author(body), board)


@router.post("/kanban/decompose/{task_id}")
def kanban_decompose(task_id: str, body: dict[str, Any] = Body(default={}),
                     board: str | None = Query(default=None)) -> dict[str, Any]:
    """Split a triage task into subtasks (Hermes's decomposer, scoped). Sync; up to 180 s."""
    if not _TASK.fullmatch(task_id):
        raise HTTPException(status_code=404, detail="Unknown task")
    result = decompose_with_scope(task_id, _author(body), board)
    if result.get("child_ids"):
        _decisions.record_containment(_tasks.normalize_slug(board), task_id, [c for c in result["child_ids"] if isinstance(c, str)])
    return result
