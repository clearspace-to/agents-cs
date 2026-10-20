"""The ``voice`` module's router, mounted by ``dashboard/plugin_api.py`` under ``/api/plugins/hermes-bots/``.
Mounted behind Hermes dashboard authentication; never expose as a standalone API.
Verified account identities bind gated media ownership across token refreshes.
Legacy loopback mode uses the dashboard-validated session-token digest.
"""
import hashlib
import json
import time
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import native_voice as native
from . import preferences

_PREFERENCES = preferences.VoicePreferences()
router = APIRouter()
MODELS = {"status": native._Status, "offer": native._Offer, "poll": native._Poll,
          "result": native._Result, "note": native._Note, "close": native._Voice,
          "voices": native._Bot, "voice_set": native._VoiceChoice}


def _owner(request: Request):
    """The verified-session / loopback grant split.

    Never treat a caller-selected legacy header as identity behind the auth gate.
    Do not mount this router outside Hermes' authenticated plugin API.
    """
    session = getattr(request.state, "session", None)
    if session is not None:
        try:
            from hermes_cli.dashboard_auth.base import Session
            if not isinstance(session, Session):
                return None
            identity = [session.provider, session.org_id, session.user_id]
            if (any(not isinstance(value, str) or len(value) > 512
                    or any(ord(char) < 32 for char in value) for value in identity)
                    or not session.provider or not session.user_id
                    or type(session.expires_at) is not int or session.expires_at <= time.time()):
                return None
            raw = json.dumps(["session", *identity], ensure_ascii=True, separators=(",", ":"))
            return hashlib.sha256(raw.encode()).hexdigest()
        except (ImportError, AttributeError, UnicodeError):
            return None
    # Explicit False is essential: an arbitrary FastAPI app has no auth grant.
    # Hermes validates this sole credential on every mounted /api/plugins route.
    # A service-token seam must not bypass that check.
    if (getattr(request.app.state, "auth_required", None) is not False
            or getattr(request.state, "token_authenticated", False)
            or not request.url.path.startswith("/api/plugins/hermes-bots/voice/")):
        return None
    token = request.headers.get("X-Hermes-Session-Token", "")
    authorization = request.headers.get("Authorization", "")
    if token and authorization:
        return None  # Never choose between competing credentials.
    if not token and authorization.startswith("Bearer "):
        token = authorization[7:]  # Also validated by Hermes' outer loopback gate.
    if not token or len(token) > 32768:
        return None
    return hashlib.sha256(("loopback:" + token).encode()).hexdigest()


@router.post("/voice/{operation}")
async def voice(operation: str, request: Request):
    headers = {"Cache-Control": "no-store"}
    def error(code, status=409):
        return JSONResponse({"code": code}, status_code=status, headers=headers)
    if operation not in MODELS:
        return error("unknown_voice_operation", 404)
    owner = _owner(request)
    if owner is None:
        return error("voice_sign_in_required", 401)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 200_000:
            return error("voice_request_too_large", 413)
    try:
        body = MODELS[operation].model_validate_json(bytes(raw))
    except (ValidationError, ValueError):
        return error("voice_invalid_request", 422)
    fields = body.model_dump()
    try:
        if operation == "status":
            value = {"available": True, "provider": body.provider, "capability": native.CAPABILITY,
                     "features": list(native.FEATURES)}
        elif operation == "voices":
            # Each bot's chosen voice, shared by every device on this host.
            value = {"agentId": body.agentId, "voices": _PREFERENCES.get(body.agentId)}
        elif operation == "voice_set":
            try:
                saved = _PREFERENCES.set(body.agentId, body.provider, body.voice)
            except ValueError as exc:
                return error(str(exc), 422)
            value = {"agentId": body.agentId, "voices": saved}
        elif operation == "poll":
            if body.wait > 0:  # v2 long poll; wait 0 is the v1 immediate poll.
                value = await native._HUB.wait_poll(owner, fields, after=body.after, wait=body.wait, limit=body.limit)
            else:
                value = native._HUB.poll(owner, fields, after=body.after, limit=body.limit)
        else:
            value = await getattr(native._HUB, operation)(owner, fields)
        return JSONResponse(value, headers=headers)
    except native.NativeAPIError as exc:
        return error(exc.code, exc.status)
    except Exception:
        # Provider failures may contain credentials or transcripts. Never echo them.
        return error("voice_service_unavailable", 503)
