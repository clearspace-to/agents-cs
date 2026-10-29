"""A window of a chat's transcript for ``GET /mobile/history``, in Hermes's own ``session.history`` row shape.

``load`` builds the rows with Hermes's own history builder (``hermes_state.SessionDB`` read-only, then
``tui_gateway.server._history_to_messages``), so they are in ``session.history``'s shape, plus the rows a compaction
archived (Hermes's own display read, ``include_compacted``); ``window``
cuts the part the app asked for. The app resumes with ``omit_messages: true`` and reads the transcript here, so a long
chat costs its window, not the whole history (Merlin's Bot Chat: 12 MB per open, 2026-10-22).

No imports from the rest of the plugin: ``qa/FakeComputer.py`` loads this file on its own to serve the same contract.
"""
from __future__ import annotations

from pathlib import Path

TAIL_DEFAULT, TAIL_MAX = 30, 200
TURN_REACH = 60        # how far back a window is extended to reach the person's message that opens its first turn
TEXT_CAP = 64 * 1024   # bytes of UTF-8 a message's text keeps


class HistoryUnavailable(Exception):
    """This Hermes lacks the builder (renamed or re-shaped): the app falls back to ``session.history``."""


def _hermes_messages(path, session, profile_home):
    """The wire rows for a chat, built as Hermes's ``session.history`` builds them. Imported lazily: inside ``hermes
    serve`` these modules are already loaded, and a test patches this function instead of installing Hermes."""
    try:
        import hermes_state
        from tui_gateway import server as tui_server
        SessionDB = hermes_state.SessionDB
        build = tui_server._history_to_messages
    except (ImportError, AttributeError) as error:
        raise HistoryUnavailable(str(error)) from error
    db = SessionDB(db_path=path, read_only=True)
    try:
        # Hermes compacts a long chat in place: the summarised rows go inactive and session.history stops listing
        # them. include_compacted is Hermes's display read (deduped, never fed to the model), so a compacted chat
        # keeps its whole history on the phone (Friday's Bot Chat, 2026-10-29: 99 rows listed of 15,267).
        try:
            history = db.get_messages_as_conversation(session, include_ancestors=True, include_row_ids=True,
                                                      include_compacted=True)
        except TypeError:  # a Hermes from before display history (2026-09)
            history = db.get_messages_as_conversation(session, include_ancestors=True, include_row_ids=True)
    finally:
        db.close()
    return build(history, profile_home=Path(profile_home))


def load(profile, path, session):
    """Every message of a chat in the wire shape (user and assistant rows carry ``row_id``; tool rows don't). The
    profile home is the folder the ``state.db`` sits in (``<root>`` for ``default``, ``<root>/profiles/<name>``)."""
    return list(_hermes_messages(Path(path), session, Path(path).parent))


def window(messages, tail=TAIL_DEFAULT, since=None, before=None, after=None):
    """The part of ``messages`` (oldest first) the app asked for: ``tail`` newest, extended back to the turn start;
    plus everything after ``since``; or the ``tail`` before ``before`` (Load earlier); or only what follows ``after``
    (a refresh). A named row that is no longer in the chat answers the plain tail with ``reset``. Tool rows have no
    row id: "after row X" is everything after X's position."""
    tail = max(1, min(TAIL_MAX, int(tail)))
    total = len(messages)
    position = {m.get("row_id"): i for i, m in enumerate(messages) if m.get("row_id") is not None}
    reset = False
    if after is not None:
        if after in position:
            return _answer(messages, position[after] + 1, total, False)
        reset = True
    elif before is not None:
        if before in position:
            end = position[before]
            return _answer(messages, _turn_start(messages, max(0, end - tail)), end, False)
        reset = True
    start = max(0, total - tail)
    if since is not None and not reset:
        if since in position:
            start = min(start, position[since] + 1)
        else:
            reset = True
    return _answer(messages, _turn_start(messages, start), total, reset)


def _turn_start(messages, start):
    """``start`` moved back to the nearest person's message, when one sits within ``TURN_REACH`` rows."""
    for index in range(start, max(-1, start - TURN_REACH - 1), -1):
        if index < len(messages) and messages[index].get("role") == "user":
            return index
    return start


def _answer(messages, start, end, reset):
    rows = [_capped(m) for m in messages[start:end]]
    ids = [m["row_id"] for m in rows if m.get("row_id") is not None]
    # The chat's oldest row with text (the app drops empty rows): a refresh tells the app whether rows it never held sit
    # before its oldest one, so a chat saved short (before a compacted chat kept its history) offers Load earlier again.
    oldest = next((m["row_id"] for m in messages if m.get("row_id") is not None and str(m.get("text") or "").strip()), None)
    return {"messages": rows, "count": len(messages), "has_more": start > 0, "reset": reset,
            "first_row": min(ids) if ids else None, "last_row": max(ids) if ids else None, "chat_first_row": oldest}


def _capped(message):
    """A message whose text runs over ``TEXT_CAP`` keeps its first 64 KiB (cut on a character) and says so."""
    text = message.get("text")
    if not isinstance(text, str):
        return message
    data = text.encode("utf-8")
    if len(data) <= TEXT_CAP:
        return message
    size = f"{len(data) / 1048576:.1f} MB" if len(data) >= 1048576 else f"{len(data) // 1024} KB"
    kept = data[:TEXT_CAP].decode("utf-8", "ignore")
    return {**message, "text": kept + f"\n\n… (message shortened: the full text is {size})",
            "text_truncated": True, "text_length": len(text)}
