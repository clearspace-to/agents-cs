"""Shared bounded input/transport helpers for the v2 engine (no routing policy)."""
import json
from pathlib import Path
import re
import sqlite3
import urllib.request
from . import jev
from . import settings

EXPLICIT = re.compile(r'(?i)(^/(?:model|reasoning|effort)\b|\b(?:use|set|keep|switch|choose)\b.{0,65}\b(?:model|reasoning|effort|gpt[- ]|claude|opus|sonnet)\b)')
RISK = re.compile(r'(?i)\b(send|delete|remove|purchase|pay|transfer|deploy|credentials?|medical|legal|financial|approve|archive|unsubscribe)\b')


def prepare_state(message, history):
    """Bound only ordinary conversation text, never tool results/system/reasoning."""
    if not isinstance(message, str) or not message.strip() or len(message) > 12000:
        raise ValueError('unsupported_input')
    if message.lstrip().startswith(('/', '[Cronjob', '[System:', '[Heartbeat')):
        raise ValueError('non_user_turn')
    if EXPLICIT.search(message) or re.search(r'(?i)\b(think harder|think deeply|reason deeply|use (low|medium|high) reasoning)\b', message):
        raise ValueError('explicit_choice')
    recent = []
    for item in (history or [])[-20:]:
        if not isinstance(item, dict) or item.get('role') not in ('user', 'assistant') or item.get('tool_calls'):
            continue
        content = item.get('content')
        if not isinstance(content, str):
            raise ValueError('attachment_context')
        if content == message or content.startswith(('[Cronjob', '[System:', '[Heartbeat')):
            continue
        if jev.SECRET.search(content):
            raise ValueError('sensitive_input')
        recent.append({'role': item['role'], 'text': content[-1000:]})
    if jev.SECRET.search(message):
        raise ValueError('sensitive_input')
    # Recent assistant text usually states the active task; no extra summarizer call.
    state = {'current_request': message, 'recent_exchange': recent[-4:]}
    raw = json.dumps(state, ensure_ascii=False)
    raw = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[email]', raw)
    raw = re.sub(r'(?<!\w)\+?\d[\d ()-]{8,}\d(?!\w)', '[phone]', raw)
    return json.loads(raw)


def call_api(state, questions, timeout):
    body = json.dumps({'model': jev.MODEL, 'state': state, 'questions': questions}).encode()
    req = urllib.request.Request(jev.ENDPOINT, data=body, headers={
        'Authorization': 'Bearer ' + settings.api_key(), 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError('oversized_response')
        return json.loads(raw)


def session_pinned(home, session_id):
    """Conservative: ambiguous provenance is not permission to override an explicit pick."""
    try:
        with sqlite3.connect(f'file:{Path(home)/"state.db"}?mode=ro', uri=True, timeout=.02) as db:
            row = db.execute('SELECT model_config FROM sessions WHERE id=?', (session_id,)).fetchone()
        if not row:
            return True
        cfg = json.loads(row[0] or '{}')
        return bool(cfg.get('composer_override_profile') or cfg.get('reasoning_config'))
    except Exception:
        return True
