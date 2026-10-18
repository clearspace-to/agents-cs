"""Tell the ``mobile`` module about approvals, questions and finished turns.

Folded in from the retired ``mobile-push-approvals`` plugin (1.3.0) unchanged in behaviour; it posts
through ``notify.post`` like the task alerts, which hands the event to ``mobile.events`` in-process (no bridge, no HTTP).

Observer only: it never answers, delays or blocks an approval, a tool or a turn (the tool hooks
return nothing, so they never veto or rewrite a call). A clarify tool call is a bot asking the
person a question: its start alerts and its return clears the alert. A finished turn
(``post_llm_call``, fired once Hermes has saved the reply) sends only the profile name, no content:
it nudges the mobile module's scan worker, which reads the saved reply and alerts by its own rules. Each event is
handed over from a daemon thread; any failure is dropped silently. The command is redacted with Hermes' own
redactor before it leaves this module.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

try:  # the plugin lives in two homes (a profile's and the root's); tests import it bare
    from . import notify
except ImportError:  # pragma: no cover
    import notify  # type: ignore


def _profile() -> str:
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except Exception:
        return "default"
    return home.name if home.parent.name == "profiles" else "default"


def _redact(text) -> str:
    text = str(text or "")
    try:
        from agent.redact import redact_sensitive_text
        text = redact_sensitive_text(text)
    except Exception:
        pass
    return text[:600]


def _post(body: dict, path: str = "approvals") -> None:
    notify.post(path, body)


def _event(kind: str, kwargs: dict, allow_session: bool = False) -> None:
    _post({
        "event": kind,
        "profile": _profile(),
        "session_id": str(kwargs.get("session_id") or ""),
        "session_key": str(kwargs.get("session_key") or ""),
        "command": _redact(kwargs.get("command")) if kind == "requested" else "",
        "description": _redact(kwargs.get("description")) if kind == "requested" else "",
        "surface": str(kwargs.get("surface") or ""),
        # Whether Hermes offers "Allow for this chat" for this request, so the alert can too.
        "allow_session": allow_session,
    })


# "smart" is Hermes' guard model deciding on its own. Only an escalation reaches a person, and that
# fires again from the surface that waits for the answer, so a smart check is never an alert.
_SILENT = {"cli", "smart"}

# The hook payload doesn't say whether Hermes offers "session" for a request. Hermes offers it for
# every prompt except an owner override of the guard model's DENY (tools/approval.py: allow_session =
# not smart_denied), and that DENY is announced just before, as post_approval_response surface="smart"
# choice="smart_deny" for the same session and pattern. Remember those for a short while.
_SMART_DENIED: dict = {}
_SMART_DENY_WINDOW = 300.0
_lock = threading.Lock()


def _approval_key(kwargs: dict) -> tuple:
    return (str(kwargs.get("session_key") or ""), str(kwargs.get("pattern_key") or ""))


def _was_smart_denied(kwargs: dict) -> bool:
    now = time.monotonic()
    with _lock:
        for key, at in list(_SMART_DENIED.items()):
            if now - at > _SMART_DENY_WINDOW:
                _SMART_DENIED.pop(key, None)
        return _SMART_DENIED.pop(_approval_key(kwargs), None) is not None


def on_request(**kwargs) -> None:
    if kwargs.get("surface") in _SILENT or kwargs.get("coalesced"):
        return  # someone is at the terminal, the guard is deciding, or it is already on the phone
    _event("requested", kwargs, allow_session=not _was_smart_denied(kwargs))


def on_response(**kwargs) -> None:
    if kwargs.get("surface") == "smart" and kwargs.get("choice") == "smart_deny":
        with _lock:
            _SMART_DENIED[_approval_key(kwargs)] = time.monotonic()
    if kwargs.get("surface") in _SILENT:
        return
    _event("resolved", kwargs)


def _question(args) -> tuple:
    """The first question the clarify tool asks (redacted, short), and how many it asks."""
    args = args if isinstance(args, dict) else {}
    batch = args.get("questions")
    if isinstance(batch, list) and batch:
        first = batch[0]
        text = first.get("question") if isinstance(first, dict) else first
        return _redact(text)[:300], len(batch)
    return _redact(args.get("question"))[:300], 1


def on_tool(**kwargs) -> None:
    """clarify is the tool that waits for the person's answer: alert when it starts, clear when it
    returns. Hermes has no hook for its sudo or secret prompts, so those never alert."""
    if kwargs.get("tool_name") != "clarify":
        return
    question, count = _question(kwargs.get("args"))
    _post({"event": "requested", "profile": _profile(), "session_id": str(kwargs.get("session_id") or ""),
           "question": question, "count": count}, "questions")


def on_tool_done(**kwargs) -> None:
    if kwargs.get("tool_name") != "clarify":
        return
    _post({"event": "resolved", "profile": _profile(), "session_id": str(kwargs.get("session_id") or "")},
          "questions")


def on_turn(**kwargs) -> None:
    if not str(kwargs.get("assistant_response") or "").strip():
        return  # nothing was said, so nothing can alert
    _post({"profile": _profile()}, "nudge")
