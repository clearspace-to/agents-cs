"""The scan worker: a Hermes background task in ``hermes serve`` (root home only) that turns finished replies into
sealed outbox rows (``scan.cycle``) every 10 seconds, or at once when something nudges it.

* Where it runs: ``register(ctx)`` (every process that loads the plugin) starts it only when this process serves the
  root Hermes home AND is the dashboard process (``hermes_cli.web_server`` is loaded) AND an event loop is already
  running, through ``ctx.spawn_task`` (Hermes's supervised task: cancelled on unload). When no loop runs yet at
  register time, the dashboard router (``dashboard/plugin_api.py``) starts it when ``mobile/routes.py`` mounts
  (``routes.on_mount``). Either way it starts once per process. Profile homes, gateways, CLIs and Kanban workers
  never run it: they only write alert rows (``events.py``).
* The nudge: ``wake()`` in this process, or the file ``<root home>/hermes-bots/nudge`` touched by any other process
  (``events.nudge``); the worker looks at that file twice a second, which is a single ``stat``.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import threading
import time

from . import home

log = logging.getLogger("hermes_bots.mobile")

POLL_SECONDS = 10       # the fallback when nobody nudges
LOOK_SECONDS = 0.5      # how often the sleeping worker checks for a nudge
PURGE_SECONDS = 600     # finished outbox rows older than a week go every ten minutes
NAME = "hermes-bots-mobile"

_lock = threading.Lock()
_started = {"how": None}
_leases: list = []
_wake = threading.Event()
_seen = {"nudge": 0}


def wake() -> None:
    """Ask the worker in this process to scan now (a no-op where it doesn't run)."""
    _wake.set()


def _nudge_mtime() -> int:
    try:
        return os.stat(home.nudge_path()).st_mtime_ns
    except OSError:
        return 0


async def _sleep(seconds: float) -> None:
    """Sleep up to ``seconds``, waking early on ``wake()`` or when the nudge file changes."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        stamp = _nudge_mtime()
        if _wake.is_set() or stamp != _seen["nudge"]:
            _seen["nudge"] = stamp
            break
        await asyncio.sleep(LOOK_SECONDS)
    _wake.clear()


def _cycle(purge: bool) -> None:
    from . import alerts, scan
    scan.cycle()
    if purge:
        alerts.store().purge()


async def _scan(purge: bool) -> None:
    # to_thread cancellation does not stop its thread. Keep the worker task (and its OS lease)
    # alive until the writer really finishes, including repeated cancellation during unload.
    work = asyncio.create_task(asyncio.to_thread(_cycle, purge))
    try:
        await asyncio.shield(work)
    except asyncio.CancelledError:
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        with contextlib.suppress(Exception):
            work.result()
        raise


async def run() -> None:
    """The loop: scan, then sleep until the next poll or a nudge. Every failure is logged (type only) and survived."""
    _seen["nudge"] = _nudge_mtime()
    last_purge = 0.0
    while True:
        due = time.monotonic() - last_purge >= PURGE_SECONDS
        try:
            await _scan(due)
            if due:
                last_purge = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - never tokens, content or raw exceptions in logs
            log.warning("hermes-bots mobile: scan cycle failed: %s", type(error).__name__)
        await _sleep(POLL_SECONDS)


def _lease() -> bool:
    """Take the one-worker lease for this root home: an OS file lock on ``<root home>/hermes-bots/worker.lock``, held until
    the process exits (or the worker stops). It is what keeps one worker per computer even when the plugin is loaded
    twice in a process (Hermes's loader and the dashboard router each import their own copy) or two ``hermes serve``
    run. If the lock file cannot be opened, starting must fail closed."""
    home.state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = open(home.state_dir() / "worker.lock", "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _leases.append(handle)
    return True


def _release() -> None:
    """The worker stopped (its task was cancelled on unload): free the lease so a reload can start it again."""
    with _lock:
        _started["how"] = None
        while _leases:
            _leases.pop().close()


def start(ctx=None) -> str:
    """Start the worker in this process if it should run here and isn't running: ``started`` (as a supervised Hermes
    task or a thread), ``running`` (already, here or in another copy or process), or ``off`` (not the root home)."""
    with _lock:
        if _started["how"]:
            return "running"
        if not home.is_root_home():
            return "off"
        try:
            leased = _lease()
        except OSError as error:
            log.warning("hermes-bots mobile: worker lease unavailable: %s", type(error).__name__)
            return "off"
        if not leased:
            _started["how"] = "elsewhere"
            return "running"
        try:
            asyncio.get_running_loop()
            loop_running = True
        except RuntimeError:
            loop_running = False
        if loop_running and ctx is not None and hasattr(ctx, "spawn_task"):
            task = ctx.spawn_task(run(), name=NAME)
            task.add_done_callback(lambda _task: _release())
            _started["how"] = "task"
        elif loop_running:
            task = asyncio.get_running_loop().create_task(run(), name=NAME)
            task.add_done_callback(lambda _task: _release())
            _started["how"] = "task"
        else:
            threading.Thread(target=lambda: asyncio.run(run()), name=NAME, daemon=True).start()
            _started["how"] = "thread"
    log.info("hermes-bots mobile: scan worker started (%s)", _started["how"])
    return "started"


def register(ctx) -> str:
    """Called by the plugin's ``register(ctx)`` in every process. Starts the worker as a Hermes background task only in
    the dashboard process of the root home with a running loop; elsewhere it does nothing (see the module note)."""
    if "hermes_cli.web_server" not in sys.modules:
        return "off"
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return "off"  # routes.on_mount starts it from the router's import
    return start(ctx)


def reset() -> None:
    """Forget that the worker started (tests)."""
    _release()
    _wake.clear()
