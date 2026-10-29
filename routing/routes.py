"""The ``routing`` module's router, mounted by ``dashboard/plugin_api.py`` under ``/api/plugins/agents-cs/``
(``/routing/<op>``): Auto effort's switches and the record of decided turns. Contract in API_HANDOFF.md."""
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import effort
from . import settings as routing_settings

router = APIRouter()


class Refused(Exception):
    def __init__(self, code, status=422):
        super().__init__(code)
        self.code, self.status = code, status


def identity(request):
    """(owner, Hermes user) of the caller. Dashboard sessions are their own owner; the connector's token is one."""
    session = getattr(request.state, 'session', None)
    if session is not None:
        from hermes_cli.dashboard_auth.base import Session
        if not isinstance(session, Session) or type(session.expires_at) != int or session.expires_at <= time.time():
            raise Refused('sign_in_required', 401)
        parts = [session.provider, session.org_id, session.user_id]
        if (any(not isinstance(x, str) or len(x) > 512 or any(ord(c) < 32 for c in x) for x in parts)
                or not session.provider or not session.user_id):
            raise Refused('sign_in_required', 401)
        return hashlib.sha256(json.dumps(parts).encode()).hexdigest(), f'{session.provider}:{session.user_id}'
    if (getattr(request.app.state, 'auth_required', None) is not False or getattr(request.state, 'token_authenticated', False)
            or not request.url.path.startswith('/api/plugins/agents-cs/routing/')):
        raise Refused('sign_in_required', 401)
    token = request.headers.get('X-Hermes-Session-Token', '')
    authorization = request.headers.get('Authorization', '')
    if token and authorization:
        raise Refused('sign_in_required', 401)
    if not token and authorization.startswith('Bearer '):
        token = authorization[7:]
    if not token or len(token) > 32768:
        raise Refused('sign_in_required', 401)
    # Hermes' outer mount validates both session-token headers, including the connector's Bearer. Stable identity
    # across local token rotation.
    return 'legacy-dashboard', None


IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,190}')


def identifier(value):
    if not isinstance(value, str) or not IDENT.fullmatch(value) or '..' in value:
        raise Refused('invalid_identifier')
    return value


def home_for(agent):
    """The bot's Hermes home: the root for the default bot, else its profile."""
    from .. import paths
    if '/' in identifier(agent):
        raise Refused('agent_not_found', 404)
    root = paths.hermes_root()
    home = root if agent in ('default', 'friday') else root / 'profiles' / agent
    if not home.is_dir():
        raise Refused('agent_not_found', 404)
    return home.resolve()


def check_session(home, sid, auth_user):
    """The chat exists on this bot and, for a dashboard user, is theirs (Hermes's state, read-only). The connector's
    token (`auth_user` None) is the computer's owner, whose chats carry whatever user Hermes recorded: none for the
    app's, "basic:…" for chats begun in 1.x, "uid:…" for Kanban, "cron-owner" for scheduled runs. Checking those
    against None refused Auto Effort in every such chat as "not found" (2026-10-29)."""
    identifier(sid)
    try:
        with sqlite3.connect(f'file:{Path(home) / "state.db"}?mode=ro', uri=True, timeout=.1) as db:
            row = db.execute('SELECT user_id FROM sessions WHERE id=?', (sid,)).fetchone()
    except sqlite3.Error:
        raise Refused('session_unavailable', 503)
    if not row or (row[0] and auth_user is not None and row[0] != auth_user):
        raise Refused('session_not_found', 404)


def fields(body, required):
    if not isinstance(body, dict) or set(body) != set(required):
        raise Refused('invalid_fields')


def revision(value):
    if type(value) != int:
        raise Refused('invalid_fields')
    return value


def dispatch(operation, method, body, owner, auth_user):
    home = home_for(body.get('agentId'))
    store = effort.Store(home)
    if operation == 'capabilities' and method == 'GET':
        fields(body, ('agentId',))
        return {'schemaVersion': 3, 'autoEffort': True, 'efforts': list(effort.EFFORTS),
                'classifierAvailable': routing_settings.key_file(home=home) is not None}
    if operation == 'policy':
        if method == 'GET':
            fields(body, ('agentId',))
            return store.policy()
        if method == 'PUT':
            fields(body, ('agentId', 'expectedRevision', 'autoEffortDefault'))
            if type(body['autoEffortDefault']) != bool:
                raise Refused('invalid_fields')
            store.policy()
            try:
                return store.save_policy(body['autoEffortDefault'], revision(body['expectedRevision']))
            except effort.Conflict:
                raise Refused('policy_conflict', 409)
    sid = body.get('sessionId')
    check_session(home, sid, auth_user)
    try:
        if operation == 'session':
            if method == 'GET':
                fields(body, ('agentId', 'sessionId'))
                return store.session(sid, owner)
            if method == 'PUT':
                fields(body, ('agentId', 'sessionId', 'expectedRevision', 'autoEffortOverride'))
                override = body['autoEffortOverride']
                if override is not None and type(override) != bool:
                    raise Refused('invalid_fields')
                store.session(sid, owner)
                try:
                    return store.save_session(sid, owner, override, revision(body['expectedRevision']))
                except effort.Conflict:
                    raise Refused('selection_conflict', 409)
        if operation == 'turns' and method == 'GET':
            fields(body, ('agentId', 'sessionId'))
            store.session(sid, owner)
            return {'turns': store.turns(sid)}
    except LookupError:
        raise Refused('session_not_found', 404)
    raise Refused('unknown_operation', 404)


@router.api_route('/routing/{operation:path}', methods=['GET', 'PUT', 'POST'])
async def routing(operation: str, request: Request):
    headers = {'Cache-Control': 'no-store'}
    try:
        owner, auth_user = identity(request)
        if request.method == 'GET':
            if len(request.url.query) > 2000:
                raise Refused('request_too_large', 413)
            body = dict(request.query_params)
        else:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 40000:
                    raise Refused('request_too_large', 413)
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                raise Refused('invalid_json')
            if not isinstance(body, dict):
                raise Refused('invalid_fields')
        result = await run_in_threadpool(dispatch, operation, request.method, body, owner, auth_user)
        return JSONResponse(result, headers=headers)
    except Refused as exc:
        if exc.status >= 500 or exc.status == 401:
            trace(operation, request.method, exc.status, exc.code)
        return JSONResponse({'code': exc.code}, status_code=exc.status, headers=headers)
    except Exception as exc:  # noqa: BLE001
        trace(operation, request.method, 503, type(exc).__name__)
        return JSONResponse({'code': 'routing_unavailable'}, status_code=503, headers=headers)


def trace(operation, method, status, code):
    """One log line per refused or failed app call: operation, status and error class only, never bodies."""
    import logging
    logging.getLogger('agents_cs.routing').warning('auto effort %s %s -> %s %s', method, operation[:40], status, str(code)[:80])
