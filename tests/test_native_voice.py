import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugin_loader
plugin_loader.load_root()  # the plugin as a package; its voice module is agents_cs_dashboard.voice
"""Native media ownership and at-most-once boundaries; no real provider calls."""
import asyncio
from dataclasses import replace
import json
import time
import unittest

from agents_cs_dashboard.voice.live_voice_provider import LiveProviderError
from agents_cs_dashboard.voice.context import NativeAPIError
from agents_cs_dashboard.voice.native_voice import ACK_TEXT, NativeVoiceHub

SDP = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"


class Provider:
    def __init__(self, **kwargs):
        self.event = kwargs["on_event"]
        self.creates = 0
        self.results = []
        self.contexts = []
        self.closed = False
        self.provider_event_types = {}
        self.schema_mismatches = 0

    async def create(self, sdp, **kwargs):
        self.creates += 1
        self.create_options = kwargs
        await self.event({"kind": "started"})
        return SDP

    async def append_result(self, identifier, text, **kwargs):
        self.results.append((identifier, text))

    async def append_context(self, text, *, quiet=True):
        self.contexts.append((text, quiet))

    async def close(self):
        self.closed = True


class NativeVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.providers = []
        def factory(**kwargs):
            provider = Provider(**kwargs)
            self.providers.append(provider)
            return provider
        self.hub = NativeVoiceHub(provider_factory=factory, lease_seconds=30)
        self.owner = "session-token-digest"
        self.fields = {"agentId": "default", "sessionId": "stored", "voiceId": "voice_fixture",
                       "provider": "codex_subscription", "voice": "cove", "sdp": SDP}

    async def asyncTearDown(self):
        await self.hub.shutdown()

    async def offer(self):
        return await self.hub.offer(self.owner, self.fields)

    async def test_a_new_offer_replaces_a_leftover_voice_sample_but_never_a_real_call(self):
        sample = {**self.fields, "sessionId": "voice-sample", "voiceId": "sample_one"}
        await self.hub.offer(self.owner, sample)
        # The app was closed mid-sample: the next sample takes over at once.
        await self.hub.offer(self.owner, {**sample, "voiceId": "sample_two"})
        self.assertTrue(self.hub.calls["sample_one"].closed)
        self.assertTrue(self.providers[0].closed)
        # A real call replaces a leftover sample too.
        await self.offer()
        self.assertTrue(self.hub.calls["sample_two"].closed)
        self.assertFalse(self.hub.calls["voice_fixture"].closed)
        # But a sample never ends a real call (another device may be on it).
        with self.assertRaises(NativeAPIError) as raised:
            await self.hub.offer(self.owner, {**sample, "voiceId": "sample_three"})
        self.assertEqual(raised.exception.code, "voice_already_active")
        self.assertFalse(self.hub.calls["voice_fixture"].closed)
        # Another sign-in's sample is left alone.
        other = {**sample, "voiceId": "sample_other"}
        await self.hub.close(self.owner, {**self.fields})
        await self.hub.offer("someone-else", other)
        await self.hub.offer(self.owner, {**sample, "voiceId": "sample_four"})
        self.assertFalse(self.hub.calls["sample_other"].closed)

    async def test_provider_is_reused_without_cloud_and_repeat_offer_never_allocates(self):
        self.assertEqual((await self.offer())["sdp"], SDP)
        with self.assertRaises(NativeAPIError):
            await self.offer()
        self.assertEqual(len(self.providers), 1)
        self.assertEqual(self.providers[0].creates, 1)
        page = self.hub.poll(self.owner, self.fields, after=0)
        self.assertEqual(page["events"][0]["event"]["kind"], "started")

    async def test_owner_profile_session_and_runtime_cannot_adopt_call(self):
        await self.offer()
        for owner, fields in [
            ("other-user-token", self.fields),
            ("replacement-session-token", self.fields),
            (self.owner, dict(self.fields, agentId="other")),
            (self.owner, dict(self.fields, sessionId="other")),
        ]:
            with self.assertRaises(NativeAPIError):
                self.hub.poll(owner, fields, after=0)
            with self.assertRaises(NativeAPIError):
                await self.hub.close(owner, fields)

    async def test_close_winning_offer_race_consumes_id_without_allocating(self):
        await self.hub.close(self.owner, self.fields)
        with self.assertRaises(NativeAPIError):
            await self.offer()
        self.assertEqual(self.providers, [])

    async def test_nonfatal_error_does_not_hang_up_and_result_cannot_replay(self):
        await self.offer()
        provider = self.providers[0]
        await provider.event({"kind": "provider_error", "fatal": False, "code": "upstream_error"})
        self.assertFalse(provider.closed)
        await provider.event({"kind": "delegation", "id": "item-one", "text": "Check tomorrow's calendar"})
        fields = dict(self.fields, delegationId="item-one", text="Hermes returned three events.")
        await self.hub.result(self.owner, fields)
        with self.assertRaises(NativeAPIError):
            await self.hub.result(self.owner, fields)
        self.assertEqual(provider.results, [("item-one", "Hermes returned three events.")])
        with self.assertRaises(NativeAPIError):
            await self.hub.result(self.owner, dict(fields, delegationId="unseen"))

    async def test_poll_reports_payload_free_provider_diagnostics_and_turn_counters(self):
        await self.offer()
        provider = self.providers[0]
        provider.provider_event_types = {
            "session.started": 1,
            "delegation.created": 1,
            "private_text": "must not escape",
            "\ud800": 99,
        }
        provider.schema_mismatches = 2
        await provider.event({"kind": "delegation", "id": "item", "text": "Check calendar"})
        await self.hub.result(self.owner, dict(self.fields, delegationId="item", text="Two events."))

        diagnostics = self.hub.poll(self.owner, self.fields, after=0)["diagnostics"]
        self.assertEqual(set(diagnostics), {
            "providerEventTypes", "schemaMismatches", "admittedDelegations", "appendedResults",
        })
        self.assertEqual(diagnostics["providerEventTypes"], {
            "session.started": 1, "delegation.created": 1,
        })
        self.assertEqual(diagnostics["schemaMismatches"], 2)
        self.assertEqual(diagnostics["admittedDelegations"], 1)
        self.assertEqual(diagnostics["appendedResults"], 1)
        encoded = json.dumps(diagnostics)
        self.assertNotIn("Check calendar", encoded)
        self.assertNotIn("Two events", encoded)
        self.assertNotIn("must not escape", encoded)

    async def test_lost_append_receipt_is_not_retried(self):
        await self.offer()
        provider = self.providers[0]
        await provider.event({"kind": "delegation", "id": "item", "text": "Hello"})
        async def uncertain(*args):
            provider.results.append(args)
            raise OSError("lost receipt")
        provider.append_result = uncertain
        fields = dict(self.fields, delegationId="item", text="Verified reply")
        with self.assertRaises(OSError):
            await self.hub.result(self.owner, fields)
        with self.assertRaises(NativeAPIError):
            await self.hub.result(self.owner, fields)
        self.assertEqual(len(provider.results), 1)

    async def test_duplicate_delegation_is_once_and_conflicting_replay_closes(self):
        await self.offer()
        provider = self.providers[0]
        event = {"kind": "delegation", "id": "one", "text": "Original request"}
        await provider.event(event)
        await provider.event(dict(event))
        page = self.hub.poll(self.owner, self.fields, after=0)
        self.assertEqual(sum(row["event"]["kind"] == "delegation" for row in page["events"]), 1)
        await provider.event(dict(event, text="Changed request"))
        self.assertTrue(provider.closed)

    async def test_acknowledged_event_gap_fails_instead_of_resubmitting(self):
        await self.offer()
        provider = self.providers[0]
        await provider.event({"kind": "delegation", "id": "one", "text": "Check calendar"})
        self.hub.poll(self.owner, self.fields, after=1)
        with self.assertRaises(NativeAPIError) as caught:
            self.hub.poll(self.owner, self.fields, after=0)
        self.assertEqual(caught.exception.code, "voice_feed_gap")

    async def test_overflow_closes_without_allocating_replacement(self):
        await self.offer()
        provider = self.providers[0]
        for _ in range(128):
            await provider.event({"kind": "transcript_delta", "role": "user", "text": "fixture"})
        self.assertTrue(provider.closed)
        self.assertEqual(len(self.providers), 1)

    async def test_close_during_pending_offer_never_returns_media_answer(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def pending(provider, sdp, **kwargs):
            entered.set()
            await release.wait()
            return SDP
        original_factory = self.hub.factory
        def factory(**kwargs):
            provider = original_factory(**kwargs)
            provider.create = lambda sdp, **options: pending(provider, sdp, **options)
            return provider
        self.hub.factory = factory
        offer = asyncio.create_task(self.offer())
        await entered.wait()
        await self.hub.close(self.owner, self.fields)
        release.set()
        with self.assertRaises(NativeAPIError) as caught:
            await offer
        self.assertEqual(caught.exception.code, "voice_closed_during_setup")
        self.assertTrue(self.providers[0].closed)

    async def test_expired_media_does_not_submit_or_cancel_a_hermes_turn(self):
        self.hub.lease_seconds = 0.01
        await self.offer()
        await asyncio.sleep(0.03)
        self.assertTrue(self.providers[0].closed)
        self.assertTrue(self.hub.poll(self.owner, self.fields, after=0)["closed"])


class NativeVoiceProgressTests(unittest.IsolatedAsyncioTestCase):
    """Fallback acknowledgement and app-fed progress notes."""

    async def asyncSetUp(self):
        self.providers = []
        def factory(**kwargs):
            provider = Provider(**kwargs)
            self.providers.append(provider)
            return provider
        self.hub = NativeVoiceHub(provider_factory=factory, lease_seconds=30, ack_seconds=0.02, ack_interval=5)
        self.owner = "session-token-digest"
        self.fields = {"agentId": "default", "sessionId": "stored", "voiceId": "voice_fixture",
                       "provider": "codex_subscription", "voice": "cove", "sdp": SDP}
        await self.hub.offer(self.owner, self.fields)
        self.provider = self.providers[0]

    async def asyncTearDown(self):
        await self.hub.shutdown()

    def note(self, text="Hermes is reading the calendar.", speak=False, **changes):
        return self.hub.note(self.owner, dict(self.fields, text=text, speak=speak, **changes))

    async def test_fallback_ack_is_spoken_when_model_stays_silent(self):
        await self.provider.event({"kind": "delegation_context_required", "id": "one", "text": "Check calendar"})
        self.assertEqual(self.provider.contexts, [])
        await asyncio.sleep(0.06)
        self.assertEqual(self.provider.contexts, [(ACK_TEXT, False)])
        self.assertIn("On it, give me a sec", ACK_TEXT)

    async def test_assistant_speech_after_or_just_before_delegation_suppresses_ack(self):
        await self.provider.event({"kind": "delegation", "id": "one", "text": "Check calendar"})
        await self.provider.event({"kind": "transcript_delta", "role": "assistant", "text": "On it"})
        await asyncio.sleep(0.06)
        self.assertEqual(self.provider.contexts, [])
        await asyncio.sleep(0.05)
        await self.provider.event({"kind": "transcript_done", "role": "assistant", "text": "On it."})
        await self.provider.event({"kind": "delegation", "id": "two", "text": "Check mail"})
        await asyncio.sleep(0.06)
        self.assertEqual(self.provider.contexts, [])

    async def test_user_speech_does_not_count_as_ack(self):
        await self.provider.event({"kind": "delegation", "id": "one", "text": "Check calendar"})
        await self.provider.event({"kind": "transcript_done", "role": "user", "text": "thanks"})
        await asyncio.sleep(0.06)
        self.assertEqual(len(self.provider.contexts), 1)

    async def test_close_delegations_share_one_ack_within_interval(self):
        for identifier in ("one", "two", "three"):
            await self.provider.event({"kind": "delegation", "id": identifier, "text": "Task " + identifier})
        await asyncio.sleep(0.06)
        await self.provider.event({"kind": "delegation", "id": "four", "text": "Task four"})
        await asyncio.sleep(0.06)
        self.assertEqual(self.provider.contexts, [(ACK_TEXT, False)])
        self.hub.ack_interval = 0
        await self.provider.event({"kind": "delegation", "id": "five", "text": "Task five"})
        await asyncio.sleep(0.06)
        self.assertEqual(len(self.provider.contexts), 2)

    async def test_close_cancels_pending_ack(self):
        self.hub.ack_seconds = 10
        await self.provider.event({"kind": "delegation", "id": "one", "text": "Check calendar"})
        task = self.hub.calls["voice_fixture"].ack
        self.assertFalse(task.done())
        await self.hub.close(self.owner, self.fields)
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())
        self.assertEqual(self.provider.contexts, [])

    async def test_ack_failure_does_not_close_call(self):
        async def failing(text, *, quiet=True):
            raise LiveProviderError("append_failed", stage="append")
        self.provider.append_context = failing
        await self.provider.event({"kind": "delegation", "id": "one", "text": "Check calendar"})
        await asyncio.sleep(0.06)
        self.assertFalse(self.provider.closed)
        self.assertFalse(self.hub.poll(self.owner, self.fields, after=0)["closed"])

    async def test_note_quiet_and_spoken_map_to_context_channel(self):
        self.assertEqual(await self.note("Reading calendar.", speak=False), {"voiceId": "voice_fixture", "noted": True})
        await self.note("Check in now.", speak=True)
        self.assertEqual(self.provider.contexts, [("Reading calendar.", True), ("Check in now.", False)])

    async def test_note_rejects_invalid_text(self):
        for text in ("", "   ", "a\x00b", "bell\x07", "c1\x85", "x" * 1601, "\u00e9" * 801):
            with self.subTest(text=text[:8]):
                with self.assertRaises(NativeAPIError) as caught:
                    await self.note(text)
                self.assertEqual(caught.exception.code, "voice_note_invalid")
        await self.note("Line one\n\tline two\r\n" + "x" * 500)
        await self.note("\u00e9" * 800)
        await self.note("x" * 1600)  # v2 "long_notes": a result from before a reconnect fits.
        self.assertEqual(len(self.provider.contexts), 3)

    async def test_note_limits_and_ownership(self):
        for _ in range(16):
            await self.note(speak=True)
        with self.assertRaises(NativeAPIError) as caught:
            await self.note(speak=True)
        self.assertEqual(caught.exception.code, "voice_note_limit")
        for _ in range(48):
            await self.note()
        with self.assertRaises(NativeAPIError) as caught:
            await self.note()
        self.assertEqual(caught.exception.code, "voice_note_limit")
        self.assertEqual(len(self.provider.contexts), 64)
        for owner, changes in (("other", {}), (self.owner, {"agentId": "other"}), (self.owner, {"sessionId": "other"})):
            with self.assertRaises(NativeAPIError) as caught:
                await self.hub.note(owner, dict(self.fields, text="x", speak=False, **changes))
            self.assertEqual(caught.exception.code, "voice_owner_changed")

    async def test_note_after_close_and_provider_failure(self):
        async def failing(text, *, quiet=True):
            raise LiveProviderError("append_failed", stage="append")
        self.provider.append_context = failing
        with self.assertRaises(NativeAPIError) as caught:
            await self.note()
        self.assertEqual(caught.exception.code, "voice_provider_append_failed")
        await self.hub.close(self.owner, self.fields)
        with self.assertRaises(NativeAPIError) as caught:
            await self.note()
        self.assertEqual(caught.exception.code, "voice_note_unavailable")


class NativeVoiceProviderFailureTests(unittest.IsolatedAsyncioTestCase):
    """Setup failures carry the provider's fixed reason, never a generic outage."""

    async def asyncSetUp(self):
        self.providers = []
        self.failure = None
        outer = self

        class Failing(Provider):
            async def create(self, sdp, **kwargs):
                self.creates += 1
                if outer.failure == "hang":
                    await asyncio.sleep(1)
                raise outer.failure

        def factory(**kwargs):
            provider = Failing(**kwargs)
            self.providers.append(provider)
            return provider
        self.hub = NativeVoiceHub(provider_factory=factory, lease_seconds=30, setup_seconds=0.05)
        self.owner = "session-token-digest"
        self.fields = {"agentId": "default", "sessionId": "stored", "voiceId": "voice_fixture",
                       "provider": "codex_subscription", "voice": "cove", "sdp": SDP}

    async def asyncTearDown(self):
        await self.hub.shutdown()

    async def test_provider_refusal_reports_its_code_and_closes_the_call(self):
        self.failure = LiveProviderError("rate_limited", stage="create", allocation_state="rejected", http_status=429)
        with self.assertLogs("agents_cs_dashboard.voice.native_voice", level="WARNING") as logs:
            with self.assertRaises(NativeAPIError) as caught:
                await self.hub.offer(self.owner, self.fields)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "voice_provider_rate_limited"))
        self.assertTrue(self.providers[0].closed)
        self.assertTrue(self.hub.calls["voice_fixture"].closed)
        self.assertIn("code=rate_limited stage=create http_status=429", logs.output[0])

    async def test_setup_timeout_is_named(self):
        self.failure = "hang"
        with self.assertRaises(NativeAPIError) as caught:
            await self.hub.offer(self.owner, self.fields)
        self.assertEqual(caught.exception.code, "voice_provider_setup_timeout")
        self.assertTrue(self.providers[0].closed)

    async def test_invalid_offer_is_rejected_before_any_allocation(self):
        with self.assertRaises(NativeAPIError) as caught:
            await self.hub.offer(self.owner, dict(self.fields, sdp="v=0\r\nm=video 9 RTP/AVP 96\r\n"))
        self.assertEqual(caught.exception.code, "voice_provider_audio_only_required")
        self.assertEqual(self.providers, [])

    async def test_unexpected_code_shape_never_leaves_the_host(self):
        self.failure = LiveProviderError("Bearer sk-secret/leak", stage="create")
        with self.assertRaises(NativeAPIError) as caught:
            await self.hub.offer(self.owner, self.fields)
        self.assertEqual(caught.exception.code, "voice_provider_failed")
        self.assertNotIn("secret", caught.exception.message)


class NativeVoiceV2Tests(unittest.IsolatedAsyncioTestCase):
    """v2 long poll, streamed results and chat context; v1 requests behave as before."""

    async def asyncSetUp(self):
        self.providers = []
        def factory(**kwargs):
            provider = Provider(**kwargs)
            self.providers.append(provider)
            return provider
        self.hub = NativeVoiceHub(provider_factory=factory, lease_seconds=30)
        self.owner = "session-token-digest"
        self.fields = {"agentId": "default", "sessionId": "stored", "voiceId": "voice_fixture",
                       "provider": "codex_subscription", "voice": "cove", "sdp": SDP}

    async def asyncTearDown(self):
        await self.hub.shutdown()

    async def started(self, **changes):
        await self.hub.offer(self.owner, dict(self.fields, **changes))
        self.provider = self.providers[0]
        self.call = self.hub.calls["voice_fixture"]

    def wait_poll(self, after=1, wait=5000, limit=4, owner=None, **changes):
        return self.hub.wait_poll(owner or self.owner, dict(self.fields, **changes), after=after, wait=wait, limit=limit)

    def result(self, text="Part", **changes):
        return self.hub.result(self.owner, dict(self.fields, delegationId="item", text=text, **changes))

    # Long poll

    async def test_long_poll_wakes_on_event(self):
        await self.started()
        task = asyncio.create_task(self.wait_poll())
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        await self.provider.event({"kind": "transcript_delta", "role": "user", "text": "hello"})
        page = await asyncio.wait_for(task, 1)
        self.assertEqual([row["event"]["text"] for row in page["events"]], ["hello"])
        self.assertEqual((page["next"], page["closed"]), (2, False))

    async def test_long_poll_wakes_on_close(self):
        await self.started()
        task = asyncio.create_task(self.wait_poll())
        await asyncio.sleep(0.02)
        await self.hub.close(self.owner, self.fields)
        page = await asyncio.wait_for(task, 1)
        self.assertTrue(page["closed"])
        self.assertEqual(page["events"], [])

    async def test_long_poll_times_out_empty(self):
        await self.started()
        started = asyncio.get_running_loop().time()
        page = await self.wait_poll(wait=50)
        elapsed = asyncio.get_running_loop().time() - started
        self.assertGreaterEqual(elapsed, 0.04)
        self.assertLess(elapsed, 1)
        self.assertEqual((page["events"], page["next"], page["closed"]), ([], 1, False))

    async def test_long_poll_answers_at_once_when_events_are_waiting(self):
        await self.started()
        page = await asyncio.wait_for(self.wait_poll(after=0, wait=10_000), 1)
        self.assertEqual(page["events"][0]["event"]["kind"], "started")

    async def test_long_poll_keeps_the_lease_and_survives_the_watchdog(self):
        self.hub.lease_seconds = 0.03
        await self.started()
        self.call.lease = 0
        task = asyncio.create_task(self.wait_poll(wait=200))
        await asyncio.sleep(0)
        self.assertGreater(self.call.lease, 0)  # Refreshed when the poll starts.
        page = await task
        self.assertFalse(page["closed"])
        self.assertFalse(self.provider.closed)
        self.assertLess(time.monotonic() - self.call.lease, 0.03)  # Refreshed again when it answers.

    async def test_long_poll_checks_ownership_and_cursor_before_waiting(self):
        await self.started()
        for owner, changes in (("other", {}), (self.owner, {"agentId": "other"}), (self.owner, {"sessionId": "other"})):
            with self.assertRaises(NativeAPIError) as caught:
                await asyncio.wait_for(self.wait_poll(owner=owner, **changes), 0.5)
            self.assertEqual(caught.exception.code, "voice_owner_changed")
        with self.assertRaises(NativeAPIError) as caught:
            await asyncio.wait_for(self.wait_poll(after=5), 0.5)
        self.assertEqual(caught.exception.code, "voice_feed_gap")
        self.assertEqual(self.call.waiters, 0)

    async def test_extra_concurrent_long_polls_answer_at_once(self):
        await self.started()
        parked = [asyncio.create_task(self.wait_poll()) for _ in range(4)]
        await asyncio.sleep(0.01)
        self.assertEqual(self.call.waiters, 4)
        page = await asyncio.wait_for(self.wait_poll(), 0.5)
        self.assertEqual(page["events"], [])
        await self.provider.event({"kind": "transcript_delta", "role": "user", "text": "hi"})
        pages = await asyncio.wait_for(asyncio.gather(*parked), 1)
        self.assertTrue(all(len(page["events"]) == 1 for page in pages))
        self.assertEqual(self.call.waiters, 0)

    async def test_poll_limit_and_legacy_default(self):
        await self.started()
        for index in range(9):
            await self.provider.event({"kind": "transcript_delta", "role": "user", "text": str(index)})
        legacy = self.hub.poll(self.owner, self.fields, after=0)
        self.assertEqual(set(legacy), {"voiceId", "closed", "next", "events", "diagnostics"})
        self.assertEqual((len(legacy["events"]), legacy["next"]), (4, 4))
        page = self.hub.poll(self.owner, self.fields, after=0, limit=32)
        self.assertEqual((len(page["events"]), page["next"]), (10, 10))
        page = await self.wait_poll(after=2, limit=3)
        self.assertEqual([row["sequence"] for row in page["events"]], [3, 4, 5])

    async def test_poll_stops_at_byte_cap_but_returns_at_least_one_row(self):
        await self.started()
        for _ in range(8):
            await self.provider.event({"kind": "transcript_delta", "role": "user", "text": "x" * 31_000})
        page = await self.wait_poll(after=1, limit=32)
        self.assertEqual(len(page["events"]), 4)  # A fifth would pass 150,000 bytes.
        size = sum(len(json.dumps(row["event"]).encode()) for row in page["events"])
        self.assertLessEqual(size, 150_000)
        self.assertEqual(page["next"], 5)
        self.assertEqual(len(self.hub.poll(self.owner, self.fields, after=8, limit=1)["events"]), 1)

    # Streamed results

    async def test_streamed_parts_in_order_and_final_closes(self):
        await self.started()
        await self.provider.event({"kind": "delegation", "id": "item", "text": "Plan my day"})
        await self.result("First,", part=0, final=False)
        await self.result("then,", part=1, final=False)
        self.assertEqual(self.hub.poll(self.owner, self.fields, after=0)["diagnostics"]["appendedResults"], 0)
        self.assertEqual(await self.result("done.", part=2, final=True), {"voiceId": "voice_fixture", "appended": True})
        self.assertEqual(self.provider.results, [("item", "First,"), ("item", "then,"), ("item", "done.")])
        self.assertEqual(self.hub.poll(self.owner, self.fields, after=0)["diagnostics"]["appendedResults"], 1)
        for changes in ({"part": 3, "final": False}, {"part": 3}, {}):
            with self.assertRaises(NativeAPIError) as caught:
                await self.result(**changes)
            self.assertEqual(caught.exception.code, "voice_result_unavailable")
        self.assertEqual(len(self.provider.results), 3)

    async def test_out_of_order_and_repeated_parts_are_rejected(self):
        await self.started()
        await self.provider.event({"kind": "delegation", "id": "item", "text": "Plan my day"})
        for changes in ({"part": 1, "final": False}, {"part": 5}):
            with self.assertRaises(NativeAPIError) as caught:
                await self.result(**changes)
            self.assertEqual(caught.exception.code, "voice_result_unavailable")
        await self.result("First", part=0, final=False)
        for changes in ({"part": 0, "final": False}, {"part": 2}, {}):  # Repeat, gap, or a v1 result.
            with self.assertRaises(NativeAPIError) as caught:
                await self.result(**changes)
            self.assertEqual(caught.exception.code, "voice_result_unavailable")
        await self.result("Last", part=1)  # final defaults to True.
        self.assertEqual(self.provider.results, [("item", "First"), ("item", "Last")])

    async def test_lost_part_receipt_is_never_replayed(self):
        await self.started()
        await self.provider.event({"kind": "delegation", "id": "item", "text": "Plan my day"})
        async def failing(identifier, text, **kwargs):
            raise LiveProviderError("append_unconfirmed", stage="append")
        self.provider.append_result = failing
        with self.assertRaises(NativeAPIError):
            await self.result(part=0, final=False)
        with self.assertRaises(NativeAPIError) as caught:
            await self.result(part=0, final=False)
        self.assertEqual(caught.exception.code, "voice_result_unavailable")

    async def test_parts_are_capped_per_delegation(self):
        await self.started()
        await self.provider.event({"kind": "delegation", "id": "item", "text": "Plan my day"})
        for part in range(64):
            await self.result(str(part), part=part, final=False)
        with self.assertRaises(NativeAPIError) as caught:
            await self.result(part=64, final=True)
        self.assertEqual(caught.exception.code, "voice_result_unavailable")
        self.assertEqual(len(self.provider.results), 64)
        self.assertEqual(self.hub.poll(self.owner, self.fields, after=0)["diagnostics"]["appendedResults"], 1)

    async def test_legacy_single_result_is_unchanged(self):
        await self.started()
        await self.provider.event({"kind": "delegation", "id": "item", "text": "Plan my day"})
        self.assertEqual(await self.result("Whole answer"), {"voiceId": "voice_fixture", "appended": True})
        with self.assertRaises(NativeAPIError):
            await self.result("Whole answer")
        with self.assertRaises(NativeAPIError):
            await self.result(part=0)
        self.assertEqual(self.provider.results, [("item", "Whole answer")])
        self.assertEqual(self.hub.poll(self.owner, self.fields, after=0)["diagnostics"]["appendedResults"], 1)

    # Chat context

    async def test_context_is_trimmed_oldest_first_and_passed(self):
        context = [{"role": "user" if index % 2 else "assistant", "text": str(index % 10) * 800} for index in range(16)]
        await self.started(context=context)
        self.assertEqual(self.provider.create_options["context"], context[-9:])  # 9 x 800 <= 7,500 bytes.
        self.assertEqual(self.provider.create_options["voice"], "cove")

    async def test_context_is_omitted_when_empty_or_absent(self):
        await self.started(context=[])
        self.assertEqual(self.provider.create_options, {"voice": "cove"})
        legacy = NativeVoiceHub(provider_factory=lambda **kwargs: Provider(**kwargs))
        call = await legacy.offer(self.owner, dict(self.fields, voiceId="legacy"))
        self.assertEqual(call["sdp"], SDP)
        self.assertEqual(legacy.calls["legacy"].provider.create_options, {"voice": "cove"})
        await legacy.shutdown()

    async def test_context_drops_entries_the_provider_would_refuse(self):
        trimmed = NativeVoiceHub._trim_context([{"role": "user", "text": "older"}, {"role": "assistant", "text": "\ud800"}])
        self.assertEqual(trimmed, [])
        multibyte = [{"role": "user", "text": "é" * 800}] * 6  # 1,600 bytes each.
        self.assertEqual(len(NativeVoiceHub._trim_context(multibyte)), 4)
        self.assertEqual(NativeVoiceHub._trim_context([{"role": "user", "text": "hi"}] * 20),
                         [{"role": "user", "text": "hi"}] * 16)
