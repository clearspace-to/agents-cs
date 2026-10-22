import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugin_loader
plugin_loader.load_root()  # the plugin as a package; its voice module is agents_cs_dashboard.voice
"""Primary subscription wire contract. Fixtures contain no credentials."""
import unittest
import asyncio
import json
import secrets
import httpx

from agents_cs_dashboard.voice.live_voice_auth import LiveCredentials


class _Auth:
    async def resolve(self):
        return LiveCredentials(secrets.token_urlsafe(24), "fixture-account")


class _Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait(json.dumps({"type":"session.started","session":{}}))
        self.sent = []
        self.closed = False
    async def recv(self):
        return await self.incoming.get()
    async def send(self, text):
        self.sent.append(json.loads(text))
    async def close(self, **_kwargs):
        self.closed = True


class LiveVoiceTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_event_diagnostics_keep_raw_types_without_frame_payloads(self):
        from agents_cs_dashboard.voice.live_voice_provider import CodexLiveProvider
        sdp = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
        events = []
        async def handler(request):
            return httpx.Response(201, headers={"Location":"/v1/live/rtc_fixture"}, text=sdp)
        ws = _Socket()
        async def connect(url, **options):
            return ws
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = CodexLiveProvider(auth=_Auth(), http_client=client,
                                         websocket_connect=connect, on_event=events.append)
            try:
                await provider.create(sdp)
                await provider.wait_started()
                await ws.incoming.put(json.dumps({
                    "type": "unknown.provider.event", "private_text": "must not escape",
                }))
                await ws.incoming.put("not-json")
                for _ in range(100):
                    if provider.schema_mismatches >= 2:
                        break
                    await asyncio.sleep(0.001)
                self.assertEqual(provider.provider_event_types["session.started"], 1)
                self.assertEqual(provider.provider_event_types["unknown.provider.event"], 1)
                self.assertEqual(provider.schema_mismatches, 2)
                encoded = json.dumps(provider.provider_event_types)
                self.assertNotIn("must not escape", encoded)
            finally:
                await provider.close()

    async def test_real_http_serializer_attaches_sideband_once_before_returning_sdp(self):
        from agents_cs_dashboard.voice.live_voice_provider import CodexLiveProvider, CODEX_CALL_URL
        sdp = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
        requests, sockets, events = [], [], []
        async def handle(request):
            requests.append(request)
            self.assertEqual(str(request.url), CODEX_CALL_URL)
            self.assertEqual(json.loads(request.content)["session"]["model"], "gpt-live-1-codex")
            self.assertEqual(request.headers["OpenAI-Alpha"], "quicksilver=v2")
            return httpx.Response(201, headers={"Location":"/v1/live/rtc_fixture"}, text=sdp)
        ws = _Socket()
        async def connect(url, **options):
            sockets.append(url)
            self.assertEqual(url,"wss://api.openai.com/v1/live/rtc_fixture")
            self.assertEqual(options["additional_headers"]["session-id"], requests[0].headers["session-id"])
            return ws
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            provider = CodexLiveProvider(auth=_Auth(), http_client=client, websocket_connect=connect, on_event=events.append)
            try:
                self.assertEqual(await provider.create(sdp), sdp)
                await provider.wait_started()
                await provider.append_result("delegation_a", "First result")
                await provider.append_result("delegation_b", "Second result")
                self.assertEqual([x["delegation_item_id"] for x in ws.sent], ["delegation_a", "delegation_b"])
                with self.assertRaises(Exception):
                    await provider.create(sdp)
                self.assertEqual(len(requests),1)
                self.assertEqual(len(sockets),1)
            finally:
                await provider.close()
            self.assertTrue(ws.closed)
            self.assertEqual(ws.sent[-1]["type"], "session.close")

    async def test_subscription_denial_never_falls_back_or_reposts(self):
        from agents_cs_dashboard.voice.live_voice_provider import CodexLiveProvider, LiveProviderError
        called=[]
        async def handler(request):
            called.append(str(request.url))
            return httpx.Response(403, text="untrusted provider body must not escape")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider=CodexLiveProvider(auth=_Auth(),http_client=client)
            with self.assertRaises(LiveProviderError) as error:
                await provider.create("v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n")
            self.assertEqual(error.exception.code,"access_denied")
            self.assertNotIn("untrusted",str(error.exception))
            self.assertEqual(len(called),1)


class SharedHttpClientTests(unittest.IsolatedAsyncioTestCase):
    """Call setup reuses one keep-alive client per event loop unless a client is injected."""

    async def asyncSetUp(self):
        from agents_cs_dashboard.voice import live_voice_provider
        self.module = live_voice_provider
        self.saved = live_voice_provider._SHARED_HTTP
        live_voice_provider._SHARED_HTTP = None

    async def asyncTearDown(self):
        shared = self.module._SHARED_HTTP
        if shared is not None and shared[0] is asyncio.get_running_loop():
            await shared[1].aclose()
        self.module._SHARED_HTTP = self.saved

    async def test_shared_client_is_reused_per_loop_and_keeps_no_cookies(self):
        first = self.module._shared_http_client()
        self.assertIs(self.module._shared_http_client(), first)
        self.assertFalse(first.follow_redirects)
        self.assertFalse(first.trust_env)
        first.cookies.extract_cookies(httpx.Response(
            200, headers={"Set-Cookie": "tracker=1; Domain=chatgpt.com; Path=/"},
            request=httpx.Request("POST", "https://chatgpt.com/backend-api/codex/realtime/calls")))
        self.assertEqual(len(first.cookies.jar), 0)
        self.module._SHARED_HTTP = (object(), first)  # A client from another (dead) loop is replaced.
        second = self.module._shared_http_client()
        self.assertIsNot(second, first)
        await first.aclose()
        self.assertIs(self.module._shared_http_client(), second)
        await second.aclose()
        self.assertIsNot(self.module._shared_http_client(), second)  # A closed client is replaced too.

    async def test_setup_without_injected_client_reuses_and_never_closes_shared_client(self):
        from agents_cs_dashboard.voice.live_voice_provider import CodexLiveProvider
        sdp = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
        requests = []
        async def handler(request):
            requests.append(request)
            return httpx.Response(201, headers={"Location": "/v1/live/rtc_fixture"}, text=sdp)
        shared = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.module._SHARED_HTTP = (asyncio.get_running_loop(), shared)
        async def connect(url, **options):
            return _Socket()
        for _ in range(2):
            provider = CodexLiveProvider(auth=_Auth(), websocket_connect=connect)
            try:
                self.assertEqual(await provider.create(sdp), sdp)
            finally:
                await provider.close()
            self.assertFalse(shared.is_closed)
        self.assertEqual(len(requests), 2)
        self.assertIs(self.module._SHARED_HTTP[1], shared)

    async def test_injected_client_is_used_and_shared_client_untouched(self):
        from agents_cs_dashboard.voice.live_voice_provider import CodexLiveProvider
        sdp = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
        async def handler(request):
            return httpx.Response(201, headers={"Location": "/v1/live/rtc_fixture"}, text=sdp)
        async def connect(url, **options):
            return _Socket()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = CodexLiveProvider(auth=_Auth(), http_client=client, websocket_connect=connect)
            try:
                self.assertEqual(await provider.create(sdp), sdp)
            finally:
                await provider.close()
            self.assertFalse(client.is_closed)
        self.assertIsNone(self.module._SHARED_HTTP)


class LiveVoiceProviderTests(unittest.TestCase):
    def test_subscription_instructions_cover_delegation_and_continued_turns(self):
        from agents_cs_dashboard.voice.live_voice_provider import CodexLiveProvider, DEFAULT_INSTRUCTIONS

        provider = CodexLiveProvider(auth=_Auth())
        payload = provider._create_payload(
            "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n",
            DEFAULT_INSTRUCTIONS,
            "cove",
            (),
        )
        instructions = payload["session"]["instructions"].lower()
        for phrase in (
            "current information",
            "calendar",
            "reminders",
            "health questions",
            "delegate before answering",
            "do not guess",
            "keep listening for later utterances",
            "queued for the next free speaking moment",
            "does not end the call",
            "on it, give me a sec, let me check that",
            "then delegate; never guess the result",
            "what's happening?",
            "check-in",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, instructions)
        for phrase in ("greetings", "small talk", "brief clarification", "repeating a verified result"):
            with self.subTest(non_delegated=phrase):
                self.assertIn(phrase, instructions)
        self.assertEqual(payload["session"]["delegation"], {"type": "client", "ack_filler": False})

    def test_subscription_delegation_preserves_opaque_identity_and_task(self):
        from agents_cs_dashboard.voice.live_voice_provider import CodexLiveProvider
        event = CodexLiveProvider.decode_event({
            "type": "delegation.created", "item": {
                "type": "delegation", "target": "client", "id": "delegation_fixture_a",
                "content": [{"type": "input_text", "text": "Inspect the project."}],
            },
        })
        self.assertEqual(event, {"kind": "delegation", "id": "delegation_fixture_a", "text": "Inspect the project."})
        self.assertIsNone(CodexLiveProvider.decode_event({
            "type": "session.delegation.created", "id": "public-api-shape-must-not-be-adopted",
        }))

    def test_audio_interruption_is_not_task_cancellation(self):
        from agents_cs_dashboard.voice.live_voice_provider import CodexLiveProvider
        self.assertEqual(CodexLiveProvider.decode_event({"type": "output_audio_buffer.cleared"}),
                         {"kind": "audio_cleared"})

    def test_results_use_speakable_quicksilver_channel(self):
        from agents_cs_dashboard.voice.live_voice_provider import CodexLiveProvider
        frames = CodexLiveProvider.result_frames("delegation_fixture_a", "Finished: " + "🧑🏾\u200d💻" * 150)
        self.assertGreater(len(frames), 1)
        self.assertEqual("".join(x["content"][0]["text"] for x in frames), "Finished: " + "🧑🏾\u200d💻" * 150)
        for frame in frames:
            self.assertEqual(frame["type"], "delegation.context.append")
            self.assertEqual(frame["delegation_item_id"], "delegation_fixture_a")
            self.assertEqual(frame["channel"], "speakable")
            self.assertLessEqual(len(frame["content"][0]["text"].encode()), 500)
