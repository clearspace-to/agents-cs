"""Which scheduled run a cron chat belongs to. Hermes keeps no link from a run's chat to its job or execution,
so it is read from the chat itself (Mark, 2026-10-28: results stopped reaching the main chat):

* until Hermes's 2026-10-24 update a run's chat was named ``cron_<job>_<YYYYmmdd_HHMMSS>``: the job is in the
  name, and the run is that job's last execution started by the chat's start (give or take five seconds);
* since then it is a ``local-<hash>`` chat (source ``cron``) that the run's owner opens a few hundredths of a
  second after the execution starts. The run is the execution started just before the chat (within two
  seconds). When the run ends Hermes titles the chat ``<job name> · <Mon DD HH:MM>`` (``cron/scheduler.py``,
  ``_finalize_cron_session``: whitespace squeezed, cut to 60 characters, `` #2`` on a clash, ``cron <job>``
  when it can't), and a title that names a job settles which of two runs started together it is.

Read-only and standard library only: the connector ships this file (``agents_cs_shared``) beside ``replies.py``.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

_NAMED = re.compile(r"^cron_([0-9a-f]+)_")
_STAMP = re.compile(r" · [A-Z][a-z]{2} \d{2} \d{2}:\d{2}(?: #\d+)?$")
_FALLBACK = re.compile(r"^cron ([0-9a-f]+)(?:\s|$)")
OPENS_WITHIN = 2.0  # seconds between an execution's start and its chat's (0.02-0.03 s on Mark's Mac mini)


def epoch(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def jobs(home) -> dict[str, str]:
    """The home's jobs, id -> name (``cron/jobs.json``); {} when it can't be read."""
    try:
        data = json.loads((Path(home) / "cron" / "jobs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    listed = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(listed, list):
        return {}
    return {str(job["id"]): str(job.get("name") or "") for job in listed if isinstance(job, dict) and job.get("id")}


def runs(executions_db) -> list[tuple[float, str, str, str, str]]:
    """Every started execution as (start, execution id, job id, status, delivery outcome); [] when unreadable.
    Hermes keeps the last thousand or so."""
    path = Path(executions_db)
    if not path.is_file():
        return []
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as con:
            rows = con.execute("SELECT id, job_id, status, delivery_outcome, started_at FROM executions "
                               "WHERE started_at IS NOT NULL").fetchall()
    except sqlite3.Error:
        return []
    return [(start, str(eid), str(job), status or "", outcome or "")
            for eid, job, status, outcome, started in rows if (start := epoch(started)) is not None]


def titled_job(title, known: dict[str, str]) -> str:
    """The job a finished run's title names; "" for no title, a renamed job or two jobs of one name."""
    title = (title or "").strip()
    fallback = _FALLBACK.match(title)
    if fallback:
        return fallback.group(1) if fallback.group(1) in known else ""
    base = _STAMP.sub("", title)
    if not base or base == title:
        return ""
    named = [job for job, name in known.items() if " ".join(name.split())[:60].strip() == base]
    return named[0] if len(named) == 1 else ""


def run_of(chat_id, chat_start, title, executions, known: dict[str, str]):
    """The execution (a ``runs`` row) this cron chat belongs to; None when it can't be told."""
    start = epoch(chat_start)
    if start is None:
        return None
    named = _NAMED.match(chat_id or "")
    if named:
        before = [run for run in executions if run[2] == named.group(1) and run[0] <= start + 5]
        return max(before) if before else None
    job = titled_job(title, known)
    near = [run for run in executions
            if start - OPENS_WITHIN <= run[0] <= start + 0.5 and (not job or run[2] == job)]
    return min(near, key=lambda run: abs(start - run[0])) if near else None


def job_of(chat_id, chat_start, title, executions, known: dict[str, str]) -> str:
    """The job id this cron chat belongs to (its run's, else the one its title names, as when Hermes has already
    dropped the execution); "" when it can't be told."""
    named = _NAMED.match(chat_id or "")
    if named:
        return named.group(1)
    run = run_of(chat_id, chat_start, title, executions, known)
    return run[2] if run else titled_job(title, known)
