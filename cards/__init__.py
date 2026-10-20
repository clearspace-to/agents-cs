# Portions adapted from bighelp-plugin (loopdy_plugin/tools.py), Copyright the Loopdy / bighelp
# contributors, Apache License 2.0. See cards/NOTICE and ../LICENSE.
"""Native cards for the Hermes Bots app: charts, metrics, tables, checklists, forms and more.

Registers one ``hermes_card_<type>`` tool per card type, ``hermes_card_form_response`` to read a
form's answer, ``hermes_task_wait`` (a worker parks for a third party), ``hermes_task_adjust`` (a coordinator
restarts, redoes, drops or reorders its main task's steps), the ``native-cards`` skill, and these hooks:

* ``transform_llm_output`` adds each card the bot rendered this turn to the end of its reply, so
  the card is part of the saved message (Hermes 0.21.5+ runs this hook before the reply is
  stored) and the bot never has to copy JSON. On messaging channels that can't draw cards
  (Telegram, Slack, …) and scheduled jobs it swaps every card for its plain-text fallback.
* ``pre_llm_call`` forgets cards left over from an interrupted turn.
* ``pre_approval_request``, ``post_approval_response``, ``post_llm_call`` and the clarify tool's
  ``pre_tool_call``/``post_tool_call`` tell the mobile module about approvals, questions and finished
  turns (``alerts.py``, formerly the ``mobile-push-approvals`` plugin).
* ``post_llm_call`` also archives a scheduled run's chat once Hermes confirms its result reached the
  main chat (``cron_archive.py``).
* A scheduled job's result reaches the main chat as the run wrote it (``relay.py``): the hand-off turn's
  reply is replaced by the run's text under a ``<job> · <time>`` label, its tools are blocked, a run for
  the bot's own Bot Chat keeps its cards, and the relay ledger (``store.RelayLog``) records which run a
  result came from, for the archiver, the mobile module and the bot itself (it is told the run chat on
  later turns).
* ``pre_tool_call`` guards Kanban work (``tasks.tool_call_block``): a worker can't finish its task
  while a card it made waits on the person, and a coordinator session stays inside its root's family.

Inside a Kanban worker (``HERMES_KANBAN_TASK`` set) a decision card is bound to that task
(``DecisionLog.bind_task``) and answered from the app's task board instead of a chat
(docs/TASK_EXPERIENCE_API.md).

With ``card_guard`` on in the profile's plugin settings, an app reply that should have been a card
but carries none is turned into cards before it is saved (``guard.py``: Jev decides text or card,
the auxiliary model builds the card, this plugin validates it).

Nothing here changes Hermes: tools, hooks, a skill and a dashboard API router are all part of
its documented plugin surface.
"""
from __future__ import annotations

import json
import re
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import alerts, cards, cron_archive, guard, tasks, creation, notify, relay
from .store import DecisionLog, FormStore, RelayLog

TOOLSET = "hermes_bots_cards"
# Where a reply is delivered as plain text: channels that can't draw a card, and cron jobs, whose
# output is handed to whatever channel the job delivers to.
TEXT_ONLY_PLATFORMS = {
    "telegram", "discord", "slack", "mattermost", "matrix", "signal", "whatsapp", "bluebubbles", "photon",
    "homeassistant", "email", "sms", "webhook", "api_server", "weixin", "wecom", "qqbot", "yuanbao",
    "dingtalk", "feishu", "cron",
}
_PENDING: dict[str, list[tuple[float, dict[str, Any]]]] = {}
_LOCK = threading.Lock()
_PENDING_TTL = 3_600

DESCRIPTIONS = {
    "summary": "a short titled note with an optional status badge",
    "metrics": "up to 12 key numbers (label, value, optional detail, trend and tone), e.g. KPIs or totals",
    "list": "a list of up to 40 items (title, optional detail and trailing value)",
    "checklist": "up to 40 items with done states; with submit_label the person can tick items and send them back",
    "table": "a table of up to 8 columns and 50 rows",
    "timeline": "up to 20 ordered steps, each done, current, upcoming or failed",
    "chart": "a bar, line or area chart (up to 6 series, 60 points each) or a pie chart (one series, up to 12 slices)",
    "form": "a form the person fills in (text, textarea, select, multi_select, toggle, integer, decimal, date fields); menus hold at most 3 options",
    "selection": "a decision: at most 3 options (the app adds Other and a details box); single sends on tap, multiple sends the ticked options",
    "message": "an email or any other message you drafted for the person, shown whole so they can Send it, edit it in place, type what to change in Other, or say don't send",
}


def _attach_supported() -> bool:
    """Hermes 0.21.5 transforms the reply before it is saved; 0.21.3 only after, so the card would
    vanish on reload. On an older Hermes the bot copies the card into its reply itself."""
    try:
        from agent import turn_finalizer
    except Exception:
        return False
    return hasattr(turn_finalizer, "apply_llm_output_transform")


def _remember(session_id: str, card: dict[str, Any]) -> None:
    now = time.time()
    with _LOCK:
        for key in [k for k, items in _PENDING.items() if all(now - at > _PENDING_TTL for at, _ in items)]:
            del _PENDING[key]
        pending = _PENDING.setdefault(session_id or "", [])
        if not any(item["id"] == card["id"] for _, item in pending):
            pending.append((now, card))


def _take(session_id: str) -> list[dict[str, Any]]:
    with _LOCK:
        return [card for _, card in _PENDING.pop(session_id or "", [])]


_DECISIONS: DecisionLog | None = None
_RELAYS: RelayLog | None = None
_STORE: FormStore | None = None
_PROFILE = "default"
_CREATION: creation.BotCreation | None = None
# The person's last message per session, as context for the card guard (bounded, in memory only).
_LAST_USER: dict[str, str] = {}


def on_pre_llm_call(session_id: str = "", user_message: Any = None, **_kwargs: Any) -> dict[str, str] | None:
    _take(session_id)
    # A scheduled job's result handed to this chat is posted as written (relay.py); on any other turn the
    # bot is told which runs the results posted here came from.
    try:
        relayed = relay.begin(session_id, user_message, home=_hermes_home(), relays=_RELAYS)
        if relayed:
            return {"context": relayed}
        runs = relay.context_for(session_id, _RELAYS)
        if runs:
            return {"context": runs}
    except Exception:  # never let bookkeeping break a turn
        pass
    if _CREATION is not None:
        _CREATION.observe_turn(session_id, str(_kwargs.get("platform") or "").lower())
    if isinstance(user_message, str):
        with _LOCK:
            if len(_LAST_USER) > 200:
                _LAST_USER.clear()
            _LAST_USER[session_id or ""] = user_message[:2000]
    # The person's answers (from the app's deck, or any client that sends the block) close decisions.
    if _DECISIONS is not None and isinstance(user_message, str) and cards.RESPONSE_FENCE in user_message:
        try:
            _DECISIONS.close_from(user_message, profile=_PROFILE)
        except Exception:  # never let bookkeeping break a turn
            pass
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    if task_id and _DECISIONS is not None:
        board = None
        try:
            board = tasks.HermesBoard(os.environ.get("HERMES_KANBAN_BOARD"))
            context = tasks.worker_decision_context(board, _DECISIONS, task_id)
            if context:
                return {"context": context}
        except Exception:
            return {"context": "Saved task decisions could not be recovered. Read the task and ancestor "
                               "comments before acting; missing context is not a reason to request approval again."}
        finally:
            if board is not None:
                board.close()
    return None


def on_transform_llm_output(response_text: str = "", session_id: str = "", platform: str = "",
                            **_kwargs: Any) -> str | None:
    """Attach this turn's cards to the reply, or turn every card into text where cards can't show."""
    # Hermes's background review (its self-improvement pass after a turn) shares the chat's session id,
    # but its text never reaches the chat: a card guarded from it was a decision nobody could see or
    # answer, stuck as "1 open decision" with Review showing nothing (Mark, 2026-10-09).
    if _background_review():
        return None
    relayed = relay.reply(session_id, response_text, relays=_RELAYS)
    if relayed is not None:
        # A scheduled job's result, posted as the run wrote it (relay.py): the run's cards come along, and
        # cards this turn drew from its decisions are attached below like any reply's.
        try:
            relay.move_cards(_DECISIONS, relayed, session_id, cards.cards_in)
        except Exception:  # bookkeeping never breaks a reply
            pass
    pending = _take(session_id)
    text = relayed if relayed is not None else (response_text or "")
    if relayed is not None and not pending:
        return relayed if relayed != response_text else None
    if (platform or "").lower() in TEXT_ONLY_PLATFORMS and not _run_for_bot_chat(platform, session_id):
        # Nobody can tap these cards, so a decision among them must not stay open on the app's bot page.
        if _DECISIONS is not None:
            try:
                _DECISIONS.discard([card["id"] for card in pending] + [card["id"] for card in cards.cards_in(text)])
            except Exception:  # bookkeeping never breaks a reply
                pass
        swapped = cards.replace_fences(text, lambda card, raw: card["fallback"] if card else raw)
        extra = [card["fallback"] for card in pending if card["id"] not in text]
        result = "\n\n".join([swapped.rstrip()] + extra) if extra else swapped
        return result if result != text else None
    if not pending and (platform or "").lower() != "cron":
        guarded = _guard(text, session_id)
        if guarded is not None:
            return guarded
    # A bot that copies its card anyway can garble it ("type": "further" on 2026-10-14): the app can't draw
    # that, yet the decision stays open, so Review showed nothing. Each copy becomes the card as recorded.
    by_id = {card["id"]: card for card in pending}
    copied: set[str] = set()

    def canonical(_card: dict[str, Any] | None, raw: str) -> str:
        found = next((card_id for card_id in by_id if card_id in raw), None)
        if found is None:
            return raw
        copied.add(found)
        return cards.fence(by_id[found])

    fixed = cards.replace_fences(text, canonical)
    missing = [card for card in by_id.values() if card["id"] not in copied]
    result = "\n\n".join([fixed.rstrip()] + [cards.fence(card) for card in missing]).lstrip() if missing else fixed
    if (platform or "").lower() == "cron" and _DECISIONS is not None:
        # The run's cards reach Mark in the main chat (relay.py): not open until they are posted there.
        try:
            _DECISIONS.hold([card["id"] for card in cards.cards_in(result)])
        except Exception:  # bookkeeping never breaks a reply
            pass
    return result if result != text else None


def _hermes_home() -> Path:
    from .. import paths
    return paths.hermes_home()


def _run_for_bot_chat(platform: str, session_id: str) -> bool:
    """A scheduled run whose result goes to the bot's own Bot Chat keeps its cards: Hermes posts the
    result there as written (relay.py), where they can be answered (2026-10-14)."""
    if (platform or "").lower() != "cron":
        return False
    match = re.match(r"cron_([0-9a-f]{6,64})_", session_id or "")
    if not match:
        return False
    try:
        data = json.loads((_hermes_home() / "cron" / "jobs.json").read_text(encoding="utf-8"))
    except Exception:
        return False
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    job = next((j for j in jobs if isinstance(j, dict) and j.get("id") == match.group(1)), None)
    targets = (job or {}).get("deliver") or ""
    targets = targets if isinstance(targets, list) else str(targets).split(",")
    own = {"bot-chat", f"bot-chat:{_PROFILE}"}
    return bool(targets) and all(str(t).strip() in own for t in targets if str(t).strip())


def _background_review() -> bool:
    """True inside Hermes's background review fork (its documented provenance flag); False when unknown."""
    try:
        from tools.skill_provenance import is_background_review
        return bool(is_background_review())
    except Exception:
        return False


def _guard(text: str, session_id: str) -> str | None:
    """Run the card guard on a card-less app reply; record its cards like the bot's own."""
    # Task decisions must pass native tool policy; never recreate a blocked ask.
    if os.environ.get("HERMES_KANBAN_TASK"):
        return None
    if not _attach_supported():
        return None
    with _LOCK:
        user_message = _LAST_USER.get(session_id or "", "")
    result = guard.run(text, parameters=parameters, user_message=user_message)
    if result is None:
        return None
    made, trimmed = result
    for card in made:
        try:
            if card["type"] == "form" and _STORE is not None:
                _STORE.create(card, profile=_PROFILE, session_id=session_id or "")
            if _DECISIONS is not None:
                card["id"] = _DECISIONS.record(card, profile=_PROFILE, session_id=session_id or "")
        except Exception:  # bookkeeping never breaks a reply
            pass
    unique = {card["id"]: card for card in made}
    return "\n\n".join([trimmed.rstrip()] + [cards.fence(card) for card in unique.values()]).lstrip()


def _render_handler(card_type: str, *, store: FormStore, profile: str, attached: bool,
                    decisions: DecisionLog | None = None):
    def handle(args: Any, **kwargs: Any) -> str:
        try:
            card = cards.render(card_type, args)
            session_id = str(kwargs.get("session_id") or "")
            if card_type == "form":
                store.create(card, profile=profile, session_id=session_id)
            if decisions is not None:
                card["id"] = decisions.record(card, profile=profile, session_id=session_id)
            result = cards.delivery(card, attached=attached)
            task_id = os.environ.get("HERMES_KANBAN_TASK")
            if task_id and decisions is not None and cards.wants_answer(card):
                # A worker's question is answered from the task board, not a chat: bind it and have the
                # worker park the task until the answer lands as a comment (Mark, 2026-10-05). Every form,
                # not only one-menu ones: an unbound form waits in a session nobody sees (2026-10-20).
                result.update(_bind(decisions, card, profile=profile, task_id=task_id))
            if attached:
                _remember(session_id, card)
            return json.dumps(result, ensure_ascii=False)
        except cards.CardError as error:
            return json.dumps({"error": error.code, "message": str(error),
                               "hint": "Fix the arguments and call the tool again, or answer in text."})
    return handle


def _bind(decisions: DecisionLog, card: dict[str, Any], *, profile: str, task_id: str) -> dict[str, Any]:
    run_id = os.environ.get("HERMES_KANBAN_RUN_ID")
    action = decisions.bind_task(card, profile=profile, board=os.environ.get("HERMES_KANBAN_BOARD") or None,
                                 task_id=task_id, run_id=int(run_id) if run_id and run_id.isdigit() else None)
    return {
        "action_id": action["action_id"], "task_id": task_id,
        "next_step": (f"This card is bound to Kanban task {task_id}; the person answers it from the task board. "
                      f"Now call kanban_block(kind=\"needs_input\", reason={json.dumps(card['title'], ensure_ascii=False)}) "
                      "and stop. The answer arrives as a comment on the task carrying a "
                      f"{cards.RESPONSE_FENCE} block with card_id {action['action_id']}; Hermes wakes you when it lands. "
                      "If you change the draft before then, show a new card: the old one is withdrawn."),
    }


def on_pre_tool_call(tool_name: str = "", args: Any = None, session_id: str = "", **_kwargs: Any) -> dict[str, Any] | None:
    """Block a Kanban tool call the task experience forbids (contract: Guard). Never raises."""
    _alert(alerts.on_tool, tool_name=tool_name, args=args, session_id=session_id, **_kwargs)
    blocked = relay.tool_block(session_id, tool_name)
    if blocked:
        return blocked
    if _DECISIONS is None:
        return None
    try:
        message = tasks.tool_call_block(tool_name, args, session_id=session_id or "", decisions=_DECISIONS,
                                        family_reader=lambda board, root: tasks.family_ids(board, root, _DECISIONS))
    except Exception:  # a broken guard must never wedge a worker
        return None
    if message:
        return {"action": "block", "message": message}
    if _CREATION is not None:
        return _CREATION.before(tool_name, args if isinstance(args, dict) else {}, session_id,
                                str(_kwargs.get("tool_call_id") or ""))
    return None


def _alert(hook: Any, **kwargs: Any) -> None:
    """The mobile alerts (alerts.py) are observers: they never raise into Hermes or the hooks around them."""
    try:
        hook(**kwargs)
    except Exception:
        pass


def on_post_tool_call(**kwargs: Any) -> None:
    _alert(alerts.on_tool_done, **kwargs)
    if _CREATION is not None:
        _CREATION.after(**kwargs)
    # A worker handed its work over for review: ring the phone (Hermes fires no lifecycle hook for review).
    if kwargs.get("tool_name") == "kanban_request_review" and not kwargs.get("error_type") \
            and kwargs.get("status") not in ("error", "blocked"):
        args = kwargs.get("args") if isinstance(kwargs.get("args"), dict) else {}
        task_id = args.get("task_id") if isinstance(args.get("task_id"), str) and args.get("task_id") else os.environ.get("HERMES_KANBAN_TASK")
        if task_id:
            _notify_in_background(args.get("board") or os.environ.get("HERMES_KANBAN_BOARD"), task_id, "review",
                                  reason=args.get("summary") if isinstance(args.get("summary"), str) else None)


# --- Kanban observers: what rings the phone (docs/TASK_EXPERIENCE_API.md, Notifications) -------------

def on_kanban_task_blocked(task_id: str = "", board: Any = None, assignee: Any = None, reason: Any = None,
                           profile_name: str = "", **_kwargs: Any) -> None:
    """A worker (or Hermes's breaker) blocked a task. A human block rings the phone: a bound card is a
    decision, anything else a question in the block reason. Other block kinds only refresh the badge."""
    if task_id:
        _notify_in_background(board, task_id, "blocked", bot=assignee or profile_name,
                              reason=reason if isinstance(reason, str) else None)


def on_kanban_task_completed(task_id: str = "", board: Any = None, **_kwargs: Any) -> None:
    if task_id:
        _notify_in_background(board, task_id, "resolved")


def on_kanban_task_claimed(task_id: str = "", board: Any = None, **_kwargs: Any) -> None:
    if task_id:
        _notify_in_background(board, task_id, "resolved")


def on_kanban_dispatch_tick(board: Any = None, **_kwargs: Any) -> None:
    """Once a minute in the dispatcher: the badge and the alerts follow changes made anywhere (the desktop
    included), without the phone having to ask."""
    _notify_in_background(board, None, "tick")


def _notify_in_background(board: Any, task_id: str | None, what: str, *, bot: Any = None, reason: str | None = None) -> None:
    if _DECISIONS is None:
        return
    decisions = _DECISIONS

    def work() -> None:
        opened = None
        try:
            opened = tasks.HermesBoard(board if isinstance(board, str) and board else None)
            if task_id and what in ("blocked", "review"):
                task = opened.task(task_id) or {}
                if what == "review" or task.get("block_kind") in tasks.HUMAN_BLOCKS:
                    kind = "review" if what == "review" else ("decision" if decisions.open_task_cards(task_id) else "blocked")
                    notify.task_event("requested", board=opened.slug, task_id=task_id, title=task.get("title"),
                                      bot=task.get("assignee") or (bot if isinstance(bot, str) else None), kind=kind, reason=reason)
            tasks.attention_after(opened, decisions, task_id if what == "resolved" else None, force=what != "tick")
        except Exception:
            pass
        finally:
            if opened is not None:
                opened.close()

    threading.Thread(target=work, name="hermes-bots-kanban-observer", daemon=True).start()


def on_transform_tool_result(**kwargs: Any) -> str | None:
    return _CREATION.transform(**kwargs) if _CREATION is not None else None


def _response_handler(*, store: FormStore, profile: str):
    def handle(args: Any, **_kwargs: Any) -> str:
        request_id = args.get("request_id") if isinstance(args, dict) else None
        if not isinstance(request_id, str) or not cards._HEX_32.fullmatch(request_id):
            return json.dumps({"error": "invalid_value", "message": "request_id is the form's 32-character id"})
        return json.dumps(store.status(request_id, profile=profile, mark_read=True), ensure_ascii=False)
    return handle


def _wait_handler(*, decisions: DecisionLog):
    def handle(args: Any, **_kwargs: Any) -> str:
        task_id = os.environ.get("HERMES_KANBAN_TASK", "")
        run_id = os.environ.get("HERMES_KANBAN_RUN_ID", "")
        reason = args.get("reason") if isinstance(args, dict) else None
        if not task_id or not run_id.isdigit() or int(run_id) <= 0:
            return json.dumps({"error": "worker_required", "message": "Only the current Kanban worker can wait."})
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            return json.dumps({"error": "invalid_reason", "message": "Name who must reply and what is pending (1–2000 characters)."})
        board = None
        try:
            board = tasks.HermesBoard(os.environ.get("HERMES_KANBAN_BOARD"))
            return json.dumps(tasks.wait_for_external_reply(board, decisions, task_id, int(run_id), reason.strip()))
        except Exception:
            return json.dumps({"error": "wait_failed", "message": "Could not park the task; read its current state before retrying."})
        finally:
            if board is not None:
                board.close()
    return handle


def _adjust_handler(*, decisions: DecisionLog):
    """``hermes_task_adjust``: only a coordinator session (a registered fence) may use it, on its own family."""
    def handle(args: Any, **kwargs: Any) -> str:
        fence = decisions.coordination_for_session(str(kwargs.get("session_id") or ""))
        if fence is None:
            return json.dumps({"error": "not_coordinating",
                               "message": "Only the session coordinating a main task can change its steps."})
        board = None
        try:
            board = tasks.HermesBoard(fence.get("board"))
            return json.dumps(tasks.adjust_step(board, decisions, fence["root_id"], args if isinstance(args, dict) else {}),
                              ensure_ascii=False)
        except Exception as error:
            return json.dumps({"error": "adjust_failed", "message": f"Hermes refused the change: {error}. Read the step before retrying."})
        finally:
            if board is not None:
                board.close()
    return handle


ADJUST_SCHEMA = {
    "name": "hermes_task_adjust",
    "description": ("Change a step of the main task you are coordinating (only inside a coordination session). "
                    "restart: get a step moving now (not started, parked in triage, blocked or scheduled), with its new instruction. "
                    "redo: reopen a finished or in-review step, with what to do differently (required). "
                    "drop: archive a step that no longer makes sense, with the reason (required). "
                    "link / unlink: the child step waits (or stops waiting) for the parent step. "
                    "add: a new step with its title and instruction (after: steps it waits for); the main task reopens "
                    "and waits for it. The main task itself can be restarted or redone. "
                    "The instruction is posted on the step as a comment before it moves."),
    "parameters": {"type": "object", "additionalProperties": False, "required": ["action"], "properties": {
        "action": {"type": "string", "enum": list(tasks.ADJUST_ACTIONS)},
        "task_id": {"type": "string", "description": "The step, for restart, redo and drop"},
        "title": {"type": "string", "minLength": 1, "maxLength": 200, "description": "add: the new step's title"},
        "after": {"type": "array", "items": {"type": "string"}, "maxItems": 20,
                  "description": "add: steps the new step waits for (default: none, it starts now)"},
        "instruction": {"type": "string", "minLength": 1, "maxLength": 8000,
                        "description": "restart/redo: what the step should now do; add: what the new step does; drop: why"},
        "parent_id": {"type": "string", "description": "link/unlink: the step that must finish first"},
        "child_id": {"type": "string", "description": "link/unlink: the step that waits"}}},
}


# --- Model-facing parameter schemas ---------------------------------------------------------------

def _text(maximum: int) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


_ID = {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,39}$"}
_TONE = {"type": "string", "enum": sorted(cards.TONES)}
_VALUE = {"type": ["string", "number"], "description": "Display value, e.g. \"$1,240\", \"98%\" or 42"}


def parameters(card_type: str) -> dict[str, Any]:
    head = {"title": _text(120), "subtitle": _text(240)}
    body: dict[str, Any]
    required: list[str]
    if card_type == "summary":
        body, required = {"body": _text(2_000), "tone": _TONE, "badge": _text(40)}, ["body"]
    elif card_type == "metrics":
        item = _obj({"label": _text(80), "value": _VALUE, "detail": _text(100),
                     "trend": {"type": "string", "enum": ["up", "down", "flat"]}, "tone": _TONE}, ["label", "value"])
        body, required = {"items": {"type": "array", "minItems": 1, "maxItems": 12, "items": item}}, ["items"]
    elif card_type == "list":
        item = _obj({"title": _text(160), "detail": _text(240), "trailing": _VALUE, "tone": _TONE}, ["title"])
        body, required = {"items": {"type": "array", "minItems": 1, "maxItems": 40, "items": item},
                          "numbered": {"type": "boolean"}}, ["items"]
    elif card_type == "checklist":
        item = _obj({"id": _ID, "label": _text(160), "detail": _text(240), "done": {"type": "boolean"}}, ["id", "label", "done"])
        body, required = {"items": {"type": "array", "minItems": 1, "maxItems": 40, "items": item},
                          "submit_label": {**_text(40), "description": "Set to let the person tick items and send them back"}}, ["items"]
    elif card_type == "table":
        column = _obj({"label": _text(60), "align": {"type": "string", "enum": ["leading", "center", "trailing"]}}, ["label"])
        cell = {"type": ["string", "number", "null"]}
        body, required = {"columns": {"type": "array", "minItems": 1, "maxItems": 8, "items": column},
                          "rows": {"type": "array", "minItems": 1, "maxItems": 50,
                                   "items": {"type": "array", "items": cell, "description": "One cell per column"}}}, ["columns", "rows"]
    elif card_type == "timeline":
        step = _obj({"title": _text(160), "detail": _text(240), "time": _text(40),
                     "status": {"type": "string", "enum": ["done", "current", "upcoming", "failed"]}}, ["title", "status"])
        body, required = {"steps": {"type": "array", "minItems": 1, "maxItems": 20, "items": step}}, ["steps"]
    elif card_type == "chart":
        point = _obj({"x": {"type": ["string", "number"]}, "y": {"type": "number"}}, ["x", "y"])
        series = _obj({"name": _text(60), "points": {"type": "array", "minItems": 1, "maxItems": 60, "items": point}}, ["name", "points"])
        body, required = {"kind": {"type": "string", "enum": ["bar", "line", "area", "pie"]},
                          "series": {"type": "array", "minItems": 1, "maxItems": 6, "items": series,
                                     "description": "Pie: exactly one series, up to 12 non-negative slices"},
                          "x_label": _text(60), "y_label": _text(60),
                          "unit": {**_text(20), "description": "Shown after values, e.g. \"$\", \"kg\", \"%\""}}, ["kind", "series"]
    elif card_type == "form":
        option = _obj({"id": _ID, "label": _text(80)}, ["id", "label"])
        field = {"type": "object", "additionalProperties": False, "required": ["id", "kind", "label"], "properties": {
            "id": _ID, "label": _text(100), "help_text": _text(240), "required": {"type": "boolean"},
            "kind": {"type": "string", "enum": ["text", "textarea", "select", "multi_select", "toggle", "integer", "decimal", "date"]},
            "placeholder": _text(100), "options": {"type": "array", "minItems": 1, "maxItems": 3, "items": option},
            "max_selected": {"type": "integer", "minimum": 1}, "min_length": {"type": "integer", "minimum": 0},
            "max_length": {"type": "integer", "minimum": 1}, "min": {"type": ["number", "string"]},
            "max": {"type": ["number", "string"]}, "step": {"type": "number"},
            "default": {"type": ["string", "number", "boolean", "array"], "items": {"type": "string"},
                        "description": "Text, option id, list of option ids, boolean, number or YYYY-MM-DD, by kind"}}}
        body, required = {"description": _text(300), "submit_label": _text(40),
                          "fields": {"type": "array", "minItems": 1, "maxItems": 12, "items": field}}, ["submit_label", "fields"]
    elif card_type == "selection":
        option = _obj({"id": _ID, "label": _text(120), "detail": _text(240),
                       "recommended": {"type": "boolean", "description": "At most one: the answer you recommend"}}, ["id", "label"])
        body, required = {"mode": {"type": "string", "enum": ["single", "multiple"]}, "description": _text(300),
                          "options": {"type": "array", "minItems": 1, "maxItems": 3, "items": option},
                          "max_selected": {"type": "integer", "minimum": 1},
                          "submit_label": {**_text(40), "description": "Multiple mode only"}}, ["mode", "options"]
    elif card_type == "message":
        recipients = {"type": "array", "minItems": 1, "maxItems": 20, "items": _text(200)}
        body, required = {
            "channel": {**_text(80), "description": "email for email; otherwise any service (upwork, teams, imessage, etc.). Omit for a generic message. All services are supported for approval."},
            "to": {**recipients, "description": "Names with addresses, as the person would recognise them: \"Morgan Goh <morgan@…>\", a person or conversation on any service"},
            "cc": recipients,
            "subject": {**_text(200), "description": "Email only"},
            "body": {**_text(6_000), "description": "The whole message, exactly as it will be sent"},
            "from": {**_text(200), "description": "The account it goes out from, when you know it"},
            "thread": {**_text(200), "description": "The conversation it joins, if needed to identify the destination"},
            "attachments": {"type": "array", "minItems": 1, "maxItems": 10, "items": _text(200), "description": "File names"},
            "description": {**_text(300), "description": "One line on why you drafted it"}}, ["to", "body"]
    else:
        raise ValueError(card_type)
    return _obj({**head, **body}, ["title", *required])


def _description(card_type: str) -> str:
    if card_type == "message":
        return ("Show an email or any other message you drafted for the person in the Hermes Bots app, so "
                "they can Send it, edit it in place, type what to change in Other, or say don't send. ALWAYS call this before sending any "
                "message that goes out under the person's name, and send nothing until their answer arrives; "
                "only selected=[send] with no values.other authorizes sending; if authorized and edited, send their wording exactly. "
                "If they typed in Other (values.other), treat comments and attached edits as notes, rewrite the draft and show it again. Title it as the question (\"Send this to "
                "Morgan?\"). One message per card. Guidance: skill_view('hermes-bots:native-cards').")
    return (f"Show {DESCRIPTIONS[card_type]} as a native card in the Hermes Bots app, instead of long text. "
            "Use a card when structure reads better than prose; keep prose for everything else. The card "
            "appears below your reply; don't repeat its contents. Other channels get plain text. "
            "Guidance: skill_view('hermes-bots:native-cards').")


def register(ctx: Any) -> None:
    profile = str(getattr(ctx, "profile_name", "default") or "default")
    global _DECISIONS, _STORE, _PROFILE, _CREATION, _RELAYS
    store = FormStore()
    _DECISIONS = DecisionLog()
    _RELAYS = RelayLog()
    _STORE, _PROFILE = store, profile
    _CREATION = creation.BotCreation(creation.CreationLog(), _DECISIONS)
    attached = _attach_supported()
    for card_type in cards.TYPES:
        name = f"hermes_card_{card_type}"
        ctx.register_tool(name=name, toolset=TOOLSET,
                          schema={"name": name, "description": _description(card_type), "parameters": parameters(card_type)},
                          handler=_render_handler(card_type, store=store, profile=profile, attached=attached,
                                                  decisions=_DECISIONS),
                          description=DESCRIPTIONS[card_type], emoji="🗂️")
    ctx.register_tool(
        name="hermes_task_wait", toolset=TOOLSET,
        schema={"name": "hermes_task_wait",
                "description": "Park your current Kanban task while waiting for another person's reply or confirmation. "
                               "Use after the approved action has been carried out and recorded with a receipt. "
                               "This is not a request for Mark's approval. Does not send messages, set a timer or poll. "
                               "Never use for a missing user decision, failed tool or unfinished work you can do now.",
                "parameters": _obj({"reason": _text(2000)}, ["reason"])},
        handler=_wait_handler(decisions=_DECISIONS), description="Wait for an external reply", emoji="🕓")
    ctx.register_tool(name="hermes_task_adjust", toolset=TOOLSET, schema=ADJUST_SCHEMA,
                      handler=_adjust_handler(decisions=_DECISIONS), description="Change a coordinated task's steps",
                      emoji="🔀")
    ctx.register_tool(
        name="hermes_card_form_response", toolset=TOOLSET,
        schema={"name": "hermes_card_form_response",
                "description": ("Read the person's answer to a form you showed with hermes_card_form. "
                                "Call it when their message ends in a hermes-card-response block for that "
                                "form. Returns state (pending, submitted, expired, not_found) and the checked values."),
                "parameters": _obj({"request_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"}}, ["request_id"])},
        handler=_response_handler(store=store, profile=profile), description="Read a form's answer", emoji="🗂️")
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("transform_llm_output", on_transform_llm_output)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("transform_tool_result", on_transform_tool_result)
    # Approvals, clarify questions and finished turns ring the phone (alerts.py; was mobile-push-approvals).
    ctx.register_hook("pre_approval_request", alerts.on_request)
    ctx.register_hook("post_approval_response", alerts.on_response)
    ctx.register_hook("post_llm_call", alerts.on_turn)
    # A scheduled run whose result reached the main chat is archived out of Scheduled (cron_archive.py).
    ctx.register_hook("post_llm_call", cron_archive.on_turn)
    # Hermes's Kanban observers (VALID_HOOKS): fired after the commit, in whichever process drove it.
    ctx.register_hook("kanban_task_blocked", on_kanban_task_blocked)
    ctx.register_hook("kanban_task_completed", on_kanban_task_completed)
    ctx.register_hook("kanban_task_claimed", on_kanban_task_claimed)
    ctx.register_hook("on_kanban_dispatch_tick", on_kanban_dispatch_tick)
    skill = Path(__file__).parent / "skills" / "native-cards" / "SKILL.md"
    if skill.exists() and hasattr(ctx, "register_skill"):
        ctx.register_skill("native-cards", skill,
                           description="When and how to answer with native cards in the Hermes Bots app.")
