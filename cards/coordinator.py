"""Open Hermes's orchestrator session for a decomposed root and hand it the plugin's instruction, from
inside ``hermes serve`` (docs/TASK_EXPERIENCE_API.md, Coordination).

Until 2026-10-08 the phone did this itself in three calls (``session.create``, the comment route,
``prompt.submit``), and every gap between them was a state the app had to explain. Now the comment
route does it in one go, through the same JSON-RPC the app uses, on Hermes's own ``/api/ws`` socket
with the serve process's internal credential (what Hermes hands its embedded chat child).

Hermes reaps a session whose socket vanished mid-turn, so the socket is held open on a daemon thread
until the coordinator's turn ends (or ``KEEPALIVE_SECONDS``). Nothing here changes Hermes.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

KEEPALIVE_SECONDS = 900
END_METHODS = {"message.complete", "turn.complete", "turn.completed", "turn.error", "session.closed", "session.ended"}
# Tests swap this for a fake; production opens Hermes's socket.
connect: Callable[..., Any] | None = None


class CoordinationError(Exception):
    """The session could not be opened or prompted; the comment is stored and the app offers Resume."""


def ws_url() -> str:
    from hermes_cli.web_server_chat import _server_internal_ws_url

    url = _server_internal_ws_url("/api/ws")
    if not url:
        raise CoordinationError("Hermes's socket is not ready yet")
    return url


def orchestrator_profile() -> str:
    """Hermes's ``kanban.orchestrator_profile`` when it names an installed profile, else the active one
    (the same resolution as ``GET /api/plugins/kanban/orchestration``)."""
    explicit = ""
    try:
        from hermes_cli.config import load_config_readonly

        explicit = str(((load_config_readonly() or {}).get("kanban") or {}).get("orchestrator_profile") or "").strip()
    except Exception:
        explicit = ""
    try:
        from hermes_cli import profiles as profiles_mod

        active = profiles_mod.get_active_profile_name() or "default"
        if explicit and profiles_mod.profile_exists(explicit):
            return explicit
        return active
    except Exception:
        return explicit or "default"


class Rpc:
    """Newline-delimited JSON-RPC 2.0 over one socket. ``call`` buffers events that arrive first."""

    def __init__(self, socket: Any):
        self.socket = socket
        self.next_id = 0
        self.events: list[dict[str, Any]] = []

    def _frames(self, timeout: float) -> list[dict[str, Any]]:
        """One websocket message is one or more whole frames. Hermes sends each frame as its own message
        with no trailing newline (``tui_gateway.transport.serialize_frame``); a batch may join several with
        newlines. Waiting for a newline here never returned (live, 2026-10-08: "timed out in 8.0s")."""
        try:
            raw = self.socket.recv(timeout=timeout)
        except TimeoutError:
            raise
        except Exception as error:
            raise CoordinationError(f"Hermes's socket closed: {error}") from error
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        frames = []
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            frames.extend(parsed if isinstance(parsed, list) else [parsed])
        return frames

    def call(self, method: str, params: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
        self.next_id += 1
        rid = self.next_id
        self.socket.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CoordinationError(f"Hermes did not answer {method}")
            try:
                frames = self._frames(remaining)
            except TimeoutError as error:
                raise CoordinationError(f"Hermes did not answer {method}") from error
            for frame in frames:
                if frame.get("id") == rid:
                    if frame.get("error"):
                        error = frame["error"]
                        message = error.get("message") if isinstance(error, dict) else str(error)
                        raise CoordinationError(f"{method}: {message}")
                    result = frame.get("result")
                    return result if isinstance(result, dict) else {}
                self.events.append(frame)


def _hold(rpc: Rpc, socket: Any, session_id: str, keepalive: float,
          on_end: Callable[[], None] | None = None) -> None:
    """Keep the socket until the coordinator's turn ends, then close it and run ``on_end`` (the plugin's
    check that something in the family is moving)."""
    deadline = time.monotonic() + keepalive
    try:
        while time.monotonic() < deadline:
            try:
                frames = rpc._frames(timeout=min(30.0, max(1.0, deadline - time.monotonic())))
            except TimeoutError:
                continue
            except Exception:
                break
            for frame in frames:
                params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
                if frame.get("method") in END_METHODS and params.get("session_id") in (session_id, None):
                    return
    finally:
        try:
            socket.close()
        except Exception:
            pass
        if on_end is not None:
            try:
                on_end()
            except Exception:
                pass


def start(*, title: str, instruction: str, profile: str | None = None, keepalive: float = KEEPALIVE_SECONDS,
          on_end: Callable[[], None] | None = None) -> dict[str, Any]:
    """``session.create`` (hidden, titled after the root) then ``prompt.submit``. Returns every id
    Hermes gave the session, runtime first: the fence covers both (a hook may see either)."""
    opener = connect
    if opener is None:
        from websockets.sync.client import connect as opener  # type: ignore
    try:
        socket = opener(ws_url(), open_timeout=10, max_size=8 * 1024 * 1024)
    except CoordinationError:
        raise
    except Exception as error:
        raise CoordinationError(f"Couldn't reach Hermes's socket: {error}") from error
    rpc = Rpc(socket)
    try:
        try:
            rpc.call("client.capabilities", {"server_requests": False}, timeout=8)
        except CoordinationError:
            pass  # an older backend has no handshake
        created = rpc.call("session.create", {"profile": profile or orchestrator_profile(), "title": title,
                                              "hidden": True, "cols": 100})
        session_id = created.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise CoordinationError("Hermes did not return a session id")
        stored = created.get("stored_session_id") if isinstance(created.get("stored_session_id"), str) else None
        rpc.call("prompt.submit", {"session_id": session_id, "text": instruction})
    except Exception:
        try:
            socket.close()
        except Exception:
            pass
        raise
    threading.Thread(target=_hold, args=(rpc, socket, session_id, keepalive, on_end), name="hermes-bots-coordinator",
                     daemon=True).start()
    ids = [session_id] + ([stored] if stored and stored != session_id else [])
    return {"session_id": session_id, "stored_session_id": stored, "session_ids": ids}


def prompt(session_id: str, instruction: str) -> None:
    """A follow-up to a session that is still open (a second comment within the fence's lifetime)."""
    opener = connect
    if opener is None:
        from websockets.sync.client import connect as opener  # type: ignore
    try:
        socket = opener(ws_url(), open_timeout=10, max_size=8 * 1024 * 1024)
    except CoordinationError:
        raise
    except Exception as error:
        raise CoordinationError(f"Couldn't reach Hermes's socket: {error}") from error
    rpc = Rpc(socket)
    try:
        try:
            rpc.call("client.capabilities", {"server_requests": False}, timeout=8)
        except CoordinationError:
            pass
        rpc.call("prompt.submit", {"session_id": session_id, "text": instruction})
    except Exception:
        socket.close()
        raise
    threading.Thread(target=_hold, args=(rpc, socket, session_id, KEEPALIVE_SECONDS), name="hermes-bots-coordinator", daemon=True).start()
