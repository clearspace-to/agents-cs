"""A scheduled job's result reaches the main chat as written (Mark, 2026-10-14: relay, don't redo).

Hermes hands a job's result to the bot's Bot Chat as a turn (``cron/scheduler_delivery.py``): a message
starting ``[Cronjob "<name>" output — …]`` followed by the run's final reply. That turn used to review
and rewrite the result, re-checking things the run had already checked, so the chat and the run could
disagree (Windy's 10:00 nudge on 2026-10-14). Hermes itself is never changed; this module does it all:

* ``begin`` (``pre_llm_call``) spots the hand-off, finds the run it came from in Hermes's own records
  (``cron/jobs.json`` for the job, ``state.db`` for the run's chat, ``cron/executions.db`` for the run)
  and writes a relay row (``store.RelayLog``): run chat → main chat. It returns context telling the
  model to post the result as given.
* ``tool_block`` (``pre_tool_call``) stops the turn using tools. The exception is this plugin's cards:
  a result that carries no cards may still have its decisions shown as cards (a run that delivers
  elsewhere turned its cards into text); a result that already carries cards may not add more.
* ``reply`` (``transform_llm_output``) replaces whatever the model wrote with the result under a
  ``<job> · <time>`` label, so it is saved and shown exactly, and marks the relay replied. A turn that
  drew cards keeps the model's text instead, under the same label.
* ``context_for`` (``pre_llm_call``, any other turn in that chat) names the results relayed here in the
  last day and their run chats, so the bot reads the run when Mark asks about one.

The relay ledger is what the archiver (``cron_archive.py``) and the mobile module read to know a run's
result reached the main chat: no receipts, no matching by job name and time.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .store import RelayLog
except ImportError:  # pragma: no cover
    from store import RelayLog  # type: ignore

HANDOFF = re.compile(r'\A\[Cronjob "(?P<name>[^"\n]{1,200})" output[^\]]*\]\n\n(?P<body>.*)\Z', re.DOTALL)
CARD_FENCE = "```hermes-card"
CARD_TOOLS = "hermes_card_"
RECENT = 24 * 3600
BLOCK = ("This turn posts a scheduled job's result as written; tools are off for it. "
         "Reply with the result exactly as given.")
BLOCK_CARDS = ("This turn posts a scheduled job's result as written. Its cards are already in it, so no "
               "card tools; reply with the result exactly as given.")

_lock = threading.Lock()
_turns: dict[str, dict[str, Any]] = {}


def parse(message: Any) -> dict[str, str] | None:
    """The hand-off's job name and result; None for anything else."""
    if not isinstance(message, str):
        return None
    match = HANDOFF.match(message)
    if not match or not match["body"].strip():
        return None
    return {"name": match["name"], "body": match["body"]}


def label(name: str, when: float | None) -> str:
    """'Prudence: daily needs-you — max two items', 09:00 -> 'Daily needs-you — max two items · 9:00 AM'."""
    short = re.sub(r"\A[^:]{1,30}:\s+", "", name).strip() or name.strip()
    short = short[:1].upper() + short[1:]
    if when is None:
        return short
    clock = datetime.fromtimestamp(when).astimezone().strftime("%I:%M %p").lstrip("0")
    return f"{short} · {clock}"


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def find_run(home: Path, name: str, body: str, *, now: float | None = None) -> dict[str, Any]:
    """Which run produced this result: the job (by name), its chat (the job's cron chat of the last six hours
    whose last reply is the result, else its newest) and its execution (``cron_runs.run_of``). Missing pieces
    are left empty; the relay still happens."""
    from ..mobile import cron_runs  # stdlib only, but mobile's code loads only where it's used
    now = time.time() if now is None else now
    found: dict[str, Any] = {"job_id": "", "run_session": "", "execution_id": "", "started_at": None}
    known = cron_runs.jobs(home)
    found["job_id"] = next((job for job, job_name in known.items() if job_name == name), "")
    if not found["job_id"] or not (home / "state.db").is_file():
        return found
    executions = cron_runs.runs(home / "cron" / "executions.db")
    wanted = body.strip()
    try:
        with closing(_read_only(home / "state.db")) as state:
            chats = []
            for chat, started, title in state.execute(
                    "SELECT id, started_at, title FROM sessions WHERE source = 'cron' AND started_at BETWEEN ? AND ? "
                    "ORDER BY started_at DESC", (now - 6 * 3600, now)).fetchall():
                if cron_runs.job_of(chat, started, title, executions, known) == found["job_id"]:
                    chats.append((chat, started, cron_runs.run_of(chat, started, title, executions, known)))
            chats = chats[:10]
            pick = next((entry for entry in chats if _last_reply(state, entry[0]) == wanted), chats[0] if chats else None)
    except sqlite3.Error:
        return found
    if pick:
        chat, started, run = pick
        found["run_session"], found["started_at"] = chat, cron_runs.epoch(started)
        if run:
            found["started_at"], found["execution_id"] = run[0], run[1]
    return found


def _last_reply(state: sqlite3.Connection, chat: str) -> str | None:
    row = state.execute("SELECT content FROM messages WHERE session_id = ? AND role = 'assistant' AND content IS NOT NULL "
                        "AND trim(content) != '' ORDER BY id DESC LIMIT 1", (chat,)).fetchone()
    return str(row[0]).strip() if row else None


def begin(session_id: str, user_message: Any, *, home: Path | None = None, relays: RelayLog | None = None,
          now: float | None = None) -> str | None:
    """Start a relay turn for this chat (or end one): returns the context for the model on a hand-off."""
    handoff = parse(user_message)
    with _lock:
        if handoff is None:
            _turns.pop(session_id or "", None)
            return None
    run = find_run(home, handoff["name"], handoff["body"], now=now) if home else \
        {"job_id": "", "run_session": "", "execution_id": "", "started_at": None}
    text = f"{label(handoff['name'], run['started_at'])}\n\n{handoff['body'].strip()}"
    has_cards = CARD_FENCE in handoff["body"]
    with _lock:
        if len(_turns) > 200:
            _turns.clear()
        _turns[session_id or ""] = {"text": text, "label": label(handoff["name"], run["started_at"]),
                                    "carded": False, "has_cards": has_cards, "run_session": run["run_session"]}
    if relays is not None and run["run_session"]:
        try:
            relays.record(run["run_session"], home=str(home), job_id=run["job_id"], job_name=handoff["name"],
                          execution_id=run["execution_id"], chat_session=session_id or "", label=_turns[session_id or ""]["label"],
                          now=now)
        except Exception:  # bookkeeping never breaks a turn
            pass
    if has_cards:
        return ("This is a scheduled job's result, not a message from Mark. Reply with it exactly as written, "
                "cards included. Do not re-check anything, use any tool, add cards or add words.")
    return ("This is a scheduled job's result, not a message from Mark. Reply with it exactly as written: do not "
            "re-check anything, use tools or add words. The one exception: if it lists decisions for Mark, you may "
            "show each as a card with a hermes_card_* tool (one card per decision, the options it gives), then "
            "reply with the result's text.")


def active(session_id: str) -> bool:
    with _lock:
        return (session_id or "") in _turns


def tool_block(session_id: str, tool_name: str = "") -> dict[str, str] | None:
    with _lock:
        turn = _turns.get(session_id or "")
        if turn is None:
            return None
        if (tool_name or "").startswith(CARD_TOOLS):
            if turn["has_cards"]:
                return {"action": "block", "message": BLOCK_CARDS}
            turn["carded"] = True
            return None
    return {"action": "block", "message": BLOCK}


def reply(session_id: str, response_text: str = "", *, relays: RelayLog | None = None,
          now: float | None = None) -> str | None:
    """The relayed text for this chat's turn (once); None outside a relay turn."""
    with _lock:
        turn = _turns.pop(session_id or "", None)
    if turn is None:
        return None
    if relays is not None and turn["run_session"]:
        try:
            relays.replied(turn["run_session"], now=now)
        except Exception:
            pass
    if not turn["carded"]:
        return turn["text"]
    text = (response_text or "").strip()
    return text if text.startswith(turn["label"]) else f"{turn['label']}\n\n{text}".rstrip()


def context_for(session_id: str, relays: RelayLog | None, *, now: float | None = None) -> str | None:
    """For a turn in a chat that had results relayed into it in the last day: which runs they came from."""
    if relays is None or not session_id:
        return None
    try:
        recent = relays.for_chat(session_id, since=(time.time() if now is None else now) - RECENT)
    except Exception:
        return None
    if not recent:
        return None
    lines = [f"- {row['label']} (job \"{row['job_name']}\"): run chat {row['run_session']}" for row in recent[:5]]
    return ("Scheduled-job results posted in this chat in the last day, and the run chats behind them:\n"
            + "\n".join(lines)
            + "\nIf Mark's message is about one of them, read that run first — "
              "session_search(session_id=\"<run chat>\") — so you answer from what the run checked.")


def move_cards(decisions: Any, text: str, session_id: str, cards_in) -> None:
    """Point the run's open decision cards at the chat that shows them now."""
    if decisions is None or not session_id:
        return
    ids = [card["id"] for card in cards_in(text) if card and card.get("id")]
    if ids:
        decisions.move(ids, session_id=session_id)
