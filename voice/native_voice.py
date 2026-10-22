"""Native authenticated media only. Hermes owns every delegated chat turn.

This bounded, disposable feed replaces Link's voice control channel. It does not
execute prompts, store transcripts, own jobs, or implement a second scheduler.
The native app submits work through ordinary Hermes prompt.submit and appends
the resulting answer here. Provider credentials never leave this process.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
import re
import time
from typing import Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .context import NativeAPIError
NativeContext = str
from .live_voice_provider import (
    MAX_PROVIDER_EVENT_NAME_BYTES, MAX_PROVIDER_EVENT_TYPES,
    LiveProviderError, _instructions, make_live_provider, validate_audio_sdp,
)

CAPABILITY = "native-voice-v2"
# Optional v2 request fields an app may send only after status lists them. Every
# field defaults to v1 behaviour, so an app that never reads this list is unaffected.
FEATURES = ("long_poll", "streamed_results", "context", "long_notes")
logger = logging.getLogger(__name__)
_FIXED_NAME = re.compile(r"[a-z_]{1,48}")
ACK_TEXT = ("Hermes has the request. Say one very short acknowledgement now, such as "
            "\"On it, give me a sec, let me check that.\" Do not guess or state the answer.")
MAX_NOTES, MAX_SPOKEN_NOTES, MAX_NOTE_BYTES = 64, 16, 1600
MAX_POLL_WAIT_MS, MAX_POLL_ROWS, MAX_POLL_BYTES = 10_000, 32, 150_000  # App rejects replies over 200 KB.
MAX_POLL_WAITERS = 4  # Further concurrent long polls on one call answer at once instead of parking.
MAX_RESULT_PARTS = 64
MAX_CONTEXT_ENTRIES, MAX_CONTEXT_BYTES = 16, 7_500  # Provider budget is 8,000 bytes.


def _reject(code="voice_unavailable", status=409):
    return NativeAPIError(status, code, "This native voice operation is unavailable. Reconnect explicitly when ready.")


def _provider_rejection(error: LiveProviderError) -> NativeAPIError:
    """Report the provider's fixed, payload-free reason (for example
    ``voice_provider_rate_limited``) instead of a generic outage, so the app and
    the host log can say what went wrong. Unknown shapes collapse to ``failed``."""
    code = error.code if isinstance(error.code, str) and _FIXED_NAME.fullmatch(error.code) else "failed"
    stage = error.stage if isinstance(error.stage, str) and _FIXED_NAME.fullmatch(error.stage) else "unknown"
    status = error.http_status if type(error.http_status) is int else None
    logger.warning("Agents CS native voice provider failed: code=%s stage=%s http_status=%s", code, stage, status)
    return NativeAPIError(409, "voice_provider_" + code, "The live voice provider could not complete this call.")


@dataclass
class _Call:
    owner: NativeContext
    profile: str
    session: str
    provider: object = None
    closed: bool = False
    sequence: int = 0
    events: list = field(default_factory=list)
    delegations: dict = field(default_factory=dict)
    appended: set = field(default_factory=set)  # Delegations closed to further results.
    parts: dict = field(default_factory=dict)  # Streamed delegation -> next expected part.
    wake: asyncio.Event = field(default_factory=asyncio.Event)  # Set on each new event and on close.
    waiters: int = 0
    admitted_delegations: int = 0
    appended_results: int = 0
    provider_event_types: dict[str, int] = field(default_factory=dict)
    schema_mismatches: int = 0
    lease: float = 0
    watch: asyncio.Task | None = None
    ack: asyncio.Task | None = None
    last_ack: float = float("-inf")
    last_assistant: float = float("-inf")
    notes: int = 0
    spoken_notes: int = 0


SAMPLE_SESSION = "voice-sample"


class NativeVoiceHub:
    def __init__(self, *, provider_factory=make_live_provider, lease_seconds=60, setup_seconds=40,
                 ack_seconds=1.2, ack_interval=5):
        self.factory = provider_factory
        self.lease_seconds = lease_seconds
        self.setup_seconds = setup_seconds
        self.ack_seconds = ack_seconds
        self.ack_interval = ack_interval
        self.calls: dict[str, _Call] = {}

    def _claim(self, owner, fields):
        identifier = fields["voiceId"]
        if identifier in self.calls or len(self.calls) >= 1024:
            raise _reject("voice_id_consumed")
        call = _Call(owner, fields["agentId"], fields["sessionId"], lease=time.monotonic())
        self.calls[identifier] = call  # Before any suspension/provider allocation.
        return call

    def _owned(self, owner, fields):
        call = self.calls.get(fields["voiceId"])
        if call is None or (call.owner, call.profile, call.session) != (owner, fields["agentId"], fields["sessionId"]):
            raise _reject("voice_owner_changed")
        return call

    async def offer(self, owner, fields):
        try:
            validate_audio_sdp(fields["sdp"])
        except LiveProviderError as error:
            raise _provider_rejection(error) from None
        # A voice sample (the app's settings, session "voice-sample") is a few seconds of throwaway audio:
        # a new offer from the same sign-in replaces it rather than waiting out its lease, e.g. after the
        # app was closed mid-sample. Real calls are never replaced.
        for stale in [c for c in self.calls.values() if not c.closed and c.owner == owner and c.session == SAMPLE_SESSION]:
            await self._close(stale)
        if sum(not call.closed for call in self.calls.values()) >= 4:
            raise _reject("voice_capacity", 503)
        if any(not call.closed and call.owner == owner for call in self.calls.values()):
            raise _reject("voice_already_active")
        context = self._trim_context(fields.get("context") or ())
        call = self._claim(owner, fields)
        try:
            kwargs = {"mode": fields["provider"], "on_event": lambda event: self._event(call, event)}
            call.provider = self.factory(**kwargs)
            call.watch = asyncio.create_task(self._watch(call))
            try:
                async with asyncio.timeout(self.setup_seconds):
                    options = {"voice": fields["voice"]}
                    if context:  # v1 offers never pass context, exactly as before.
                        options["context"] = context
                    answer = await call.provider.create(fields["sdp"], **options)
            except TimeoutError:
                raise LiveProviderError("setup_timeout") from None
            if call.closed:
                raise _reject("voice_closed_during_setup")
            validate_audio_sdp(answer)
            if len(answer.encode("utf-8")) > 180_000:
                raise _reject("voice_answer_too_large")
            return {"voiceId": fields["voiceId"], "sdp": answer}
        except LiveProviderError as error:
            await self._close(call)
            raise _provider_rejection(error) from None
        except BaseException:
            await self._close(call)
            raise

    async def _event(self, call, event):
        if call.closed or not isinstance(event, dict):
            return
        self._refresh_diagnostics(call)
        kind = event.get("kind")
        if kind not in {"started", "sideband_attached", "transcript_delta", "transcript_done", "delegation",
                        "delegation_context_required", "provider_error", "transport_closed", "session_closed", "audio_cleared"}:
            return
        encoded = json.dumps(event, allow_nan=False).encode("utf-8")
        if len(encoded) > 32_768 or len(call.events) >= 128 or sum(len(row[2]) for row in call.events) + len(encoded) > 524_288:
            await self._close(call)
            return
        if kind in {"transcript_delta", "transcript_done"} and event.get("role") == "assistant":
            call.last_assistant = time.monotonic()
        if kind in {"delegation", "delegation_context_required"}:
            identifier = event.get("id")
            if not isinstance(identifier, str) or not identifier or len(identifier.encode("utf-8")) > 180:
                await self._close(call)
                return
            previous = call.delegations.get(identifier)
            if previous is not None:
                if previous != event:
                    await self._close(call)
                return  # A provider retransmission never submits another turn.
            if len(call.delegations) >= 1024:
                await self._close(call)
                return
            call.delegations[identifier] = dict(event)
            call.admitted_delegations = min(call.admitted_delegations + 1, 2147483647)
            self._schedule_ack(call)
        call.sequence += 1
        call.events.append((call.sequence, event, encoded))
        call.wake.set()
        if kind in {"transport_closed", "session_closed"} or (kind == "provider_error" and event.get("fatal") is not False):
            await self._close(call)

    @staticmethod
    def _check_cursor(call, after):
        if after > call.sequence or (call.events and after < call.events[0][0] - 1):
            raise _reject("voice_feed_gap")

    def poll(self, owner, fields, *, after, limit=4):
        call = self._owned(owner, fields)
        self._check_cursor(call, after)
        call.events = [row for row in call.events if row[0] > after]
        call.lease = time.monotonic()
        self._refresh_diagnostics(call)
        rows, size = [], 0
        for row in call.events[:max(1, min(limit, MAX_POLL_ROWS))]:
            # Events are each at most 32 KiB, so v1's four rows always fit.
            if rows and size + len(row[2]) > MAX_POLL_BYTES:
                break
            rows.append(row)
            size += len(row[2])
        return {"voiceId": fields["voiceId"], "closed": call.closed,
                "next": rows[-1][0] if rows else after,
                "events": [{"sequence": sequence, "event": event} for sequence, event, _ in rows],
                "diagnostics": self._diagnostics(call)}

    async def wait_poll(self, owner, fields, *, after, wait, limit):
        """Long poll: answer as soon as an event arrives or the call closes, else after ``wait`` ms.

        Ownership and the cursor are checked before parking and again by ``poll``
        when it answers. A parked poll keeps the lease alive, so it never lets the
        lease watchdog retire a call that is still being listened to.
        """
        call = self._owned(owner, fields)
        self._check_cursor(call, after)
        call.lease = time.monotonic()
        if call.waiters < MAX_POLL_WAITERS:
            call.waiters += 1
            try:
                deadline = time.monotonic() + max(0, min(wait, MAX_POLL_WAIT_MS)) / 1000
                while True:
                    call.wake.clear()  # Clear, then recheck: a wakeup between the two is never lost.
                    remaining = deadline - time.monotonic()
                    if call.closed or remaining <= 0 or any(row[0] > after for row in call.events):
                        break
                    try:
                        async with asyncio.timeout(remaining):
                            await call.wake.wait()
                    except TimeoutError:
                        break
            finally:
                call.waiters -= 1
        return self.poll(owner, fields, after=after, limit=limit)

    async def result(self, owner, fields):
        call = self._owned(owner, fields)
        identifier = fields["delegationId"]
        part = fields.get("part")
        if call.closed or identifier not in call.delegations or identifier in call.appended:
            raise _reject("voice_result_unavailable")
        if part is None:
            if identifier in call.parts:  # A streamed result never ends with a v1 one.
                raise _reject("voice_result_unavailable")
            final = True
        else:
            # Parts arrive in order from 0; each is consumed once, sent or not.
            if part != call.parts.get(identifier, 0) or part >= MAX_RESULT_PARTS:
                raise _reject("voice_result_unavailable")
            call.parts[identifier] = part + 1
            final = fields.get("final", True) is True or part + 1 >= MAX_RESULT_PARTS
        if final:
            call.appended.add(identifier)  # A lost provider receipt never permits replay.
        try:
            await call.provider.append_result(identifier, fields["text"])
        except LiveProviderError as error:
            raise _provider_rejection(error) from None
        if final:  # Counts finished delegations, comparable with admittedDelegations.
            call.appended_results = min(call.appended_results + 1, 2147483647)
        self._refresh_diagnostics(call)
        return {"voiceId": fields["voiceId"], "appended": True}

    async def note(self, owner, fields):
        call = self._owned(owner, fields)
        if call.closed:
            raise _reject("voice_note_unavailable")
        text = fields["text"]
        if (not isinstance(text, str) or not text.strip() or len(text.encode("utf-8", "replace")) > MAX_NOTE_BYTES
                or any((ord(c) < 32 and c not in "\t\n\r") or 127 <= ord(c) < 160 or 0xD800 <= ord(c) < 0xE000
                       for c in text)):
            raise _reject("voice_note_invalid", 422)
        speak = fields["speak"] is True
        if call.notes >= MAX_NOTES or (speak and call.spoken_notes >= MAX_SPOKEN_NOTES):
            raise _reject("voice_note_limit")
        call.notes += 1  # Counted before sending: a lost receipt still spends the budget.
        call.spoken_notes += speak
        try:
            await call.provider.append_context(text, quiet=not speak)
        except LiveProviderError as error:
            raise _provider_rejection(error) from None
        return {"voiceId": fields["voiceId"], "noted": True}

    @staticmethod
    def _trim_context(entries):
        """Recent chat for the voice model: drop the oldest entries, never reject the call."""
        history = [{"role": entry["role"], "text": entry["text"]} for entry in entries][-MAX_CONTEXT_ENTRIES:]
        while history:
            try:
                fits = sum(len(entry["text"].encode("utf-8")) for entry in history) <= MAX_CONTEXT_BYTES
                if fits:
                    _instructions("context", history)  # The provider's own limits, applied at create.
                    return history
            except (LiveProviderError, UnicodeError):
                pass
            history.pop(0)
        return history

    def _schedule_ack(self, call):
        # Delegations close together share one pending or recent acknowledgement.
        if (call.ack and not call.ack.done()) or time.monotonic() - call.last_ack < self.ack_interval:
            return
        call.ack = asyncio.create_task(self._fallback_ack(call, time.monotonic()))

    async def _fallback_ack(self, call, admitted):
        await asyncio.sleep(self.ack_seconds)
        # Speech just before admission counts too: the model often acknowledges, then delegates.
        if (call.closed or call.provider is None or call.last_assistant >= admitted - self.ack_seconds
                or time.monotonic() - call.last_ack < self.ack_interval):
            return
        call.last_ack = time.monotonic()
        try:
            await call.provider.append_context(ACK_TEXT, quiet=False)
        except Exception as error:
            # A missed filler is cosmetic; never let it end the call.
            logger.info("Agents CS native voice fallback acknowledgement skipped: %s", type(error).__name__)

    @staticmethod
    def _copy_event_types(value):
        if not isinstance(value, dict):
            return {}
        result = {}
        for name, count in value.items():
            if len(result) >= MAX_PROVIDER_EVENT_TYPES:
                break
            if not isinstance(name, str) or not name:
                continue
            try:
                name_bytes = len(name.encode("utf-8"))
            except UnicodeEncodeError:
                continue
            if (name_bytes > MAX_PROVIDER_EVENT_NAME_BYTES
                    or any(ord(character) < 32 or ord(character) == 127 for character in name)
                    or type(count) is not int or count < 0):
                continue
            result[name] = min(count, 2147483647)
        return dict(sorted(result.items()))

    def _refresh_diagnostics(self, call):
        provider = call.provider
        if provider is None:
            return
        call.provider_event_types = self._copy_event_types(
            getattr(provider, "provider_event_types", {}))
        mismatches = getattr(provider, "schema_mismatches", 0)
        if type(mismatches) is int and mismatches >= 0:
            call.schema_mismatches = min(mismatches, 2147483647)

    @staticmethod
    def _diagnostics(call):
        return {
            "providerEventTypes": dict(call.provider_event_types),
            "schemaMismatches": call.schema_mismatches,
            "admittedDelegations": call.admitted_delegations,
            "appendedResults": call.appended_results,
        }

    async def close(self, owner, fields):
        call = self._owned(owner, fields) if fields["voiceId"] in self.calls else self._claim(owner, fields)
        await self._close(call)
        return {"voiceId": fields["voiceId"], "closed": True}

    async def _close(self, call):
        if call.closed:
            return
        self._refresh_diagnostics(call)
        call.closed = True
        call.events.clear()
        call.delegations.clear()
        call.appended.clear()
        call.parts.clear()
        call.wake.set()  # Parked long polls answer "closed" at once.
        for task in (call.watch, call.ack):
            if task and task is not asyncio.current_task():
                task.cancel()
        provider, call.provider = call.provider, None
        if provider:
            try:
                async with asyncio.timeout(5):
                    await provider.close()
            except Exception:
                pass  # Media is locally retired; never create a replacement.

    async def _watch(self, call):
        while not call.closed:
            await asyncio.sleep(self.lease_seconds)
            if not call.waiters and time.monotonic() - call.lease >= self.lease_seconds:
                await self._close(call)

    async def shutdown(self):
        for call in self.calls.values():
            await self._close(call)


_HUB = NativeVoiceHub()


class _Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    agentId: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    sessionId: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,179}$")


class _Status(_Scope):
    provider: Literal["codex_subscription"]


class _Voice(_Scope):
    voiceId: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,179}$")


class _ContextEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=800)


class _Offer(_Voice):
    provider: Literal["codex_subscription"]
    voice: str = Field(min_length=1, max_length=32)
    sdp: str = Field(min_length=1, max_length=180_000)
    context: list[_ContextEntry] = Field(default_factory=list, max_length=MAX_CONTEXT_ENTRIES)  # v2 "context"


class _Poll(_Voice):
    after: StrictInt = Field(ge=0, le=9_007_199_254_740_991)
    wait: StrictInt = Field(default=0, ge=0, le=MAX_POLL_WAIT_MS)  # v2 "long_poll"
    limit: StrictInt = Field(default=4, ge=1, le=MAX_POLL_ROWS)


class _Result(_Voice):
    delegationId: str = Field(min_length=1, max_length=180)
    text: str = Field(min_length=1, max_length=1500)
    part: StrictInt | None = Field(default=None, ge=0, le=255)  # v2 "streamed_results"
    final: StrictBool = True


class _Bot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    agentId: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")


class _VoiceChoice(_Bot):
    provider: Literal["codex_subscription"]
    voice: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")


class _Note(_Voice):
    text: str = Field(min_length=1, max_length=MAX_NOTE_BYTES)
    speak: StrictBool
