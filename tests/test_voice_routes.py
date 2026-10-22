"""Our dashboard adapter's bounded input and ownership contract, without Hermes or credentials."""
import importlib.util
import sys
import unittest
import time
from dataclasses import dataclass
from types import ModuleType
from unittest.mock import patch
from pathlib import Path
import httpx
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugin_loader  # noqa: E402

api = plugin_loader.load_module('voice.routes')

class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()
        self.app.state.auth_required = False
        self.app.include_router(api.router, prefix='/api/plugins/agents-cs')
        @self.app.middleware('http')
        async def fixture_auth(request, call_next):
            if (request.headers.get('X-Hermes-Session-Token') not in {'fixture', 'owner', 'other'}
                    and request.headers.get('Authorization') != 'Bearer fixture'):
                return api.JSONResponse({'code': 'fixture_auth_required'}, status_code=401,
                                        headers={'Cache-Control': 'no-store'})
            return await call_next(request)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://fixture')
        self.fields = {'agentId':'friday', 'sessionId':'saved', 'provider':'codex_subscription'}
    async def asyncTearDown(self):
        await self.client.aclose()
    async def test_missing_session_header_rejected(self):
        response = await self.client.post('/api/plugins/agents-cs/voice/status', json=self.fields)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers['cache-control'], 'no-store')
    async def test_status_does_not_allocate_or_resolve_credentials(self):
        before = len(api.native._HUB.calls)
        response = await self.client.post('/api/plugins/agents-cs/voice/status', json=self.fields, headers={'X-Hermes-Session-Token':'fixture'})
        self.assertTrue(response.json()['available'])
        self.assertEqual(len(api.native._HUB.calls), before)
    async def test_connector_local_session_reaches_voice(self):
        from agents_cs_connector.hermes import HermesLink, Detection
        link = HermesLink()
        link.detection = Detection(mode='loopback', token='fixture')
        try:
            response = await self.client.post('/api/plugins/agents-cs/voice/status',
                                             json=self.fields, headers=link.headers)
            self.assertEqual(response.status_code, 200, response.text)
        finally:
            await link.close()
    async def test_invalid_input_has_no_reflected_secret(self):
        response = await self.client.post('/api/plugins/agents-cs/voice/status', json={**self.fields, 'secret':'private-fixture'}, headers={'X-Hermes-Session-Token':'fixture'})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('private-fixture', response.text)
    async def test_body_is_bounded(self):
        response = await self.client.post('/api/plugins/agents-cs/voice/offer', content=b'x'*200001, headers={'X-Hermes-Session-Token':'fixture'})
        self.assertEqual(response.status_code, 413)
    async def test_call_cannot_be_adopted_by_another_session(self):
        fields = {'agentId':'friday', 'sessionId':'saved', 'voiceId':'close-first-route-test'}
        response = await self.client.post('/api/plugins/agents-cs/voice/close', json=fields, headers={'X-Hermes-Session-Token':'owner'})
        self.assertEqual(response.status_code, 200)
        response = await self.client.post('/api/plugins/agents-cs/voice/poll', json={**fields, 'after':0}, headers={'X-Hermes-Session-Token':'other'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'voice_owner_changed')


@dataclass
class FixtureSession:
    provider: str = 'password'
    user_id: str = 'mark'
    org_id: str = ''
    expires_at: int = 4_000_000_000
    access_token: str = 'fixture-access'


class GatedRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()
        self.app.state.auth_required = True
        self.app.include_router(api.router, prefix='/api/plugins/agents-cs')
        self.session = FixtureSession()
        self.service_authenticated = False
        @self.app.middleware('http')
        async def verified_session_fixture(request, call_next):
            # Only this middleware represents host-verified identity. Headers do not.
            request.state.session = self.session
            request.state.token_authenticated = self.service_authenticated
            return await call_next(request)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://fixture')
        module = ModuleType('hermes_cli.dashboard_auth.base')
        module.Session = FixtureSession
        self.modules = patch.dict(sys.modules, {'hermes_cli.dashboard_auth.base': module})
        self.modules.start()
        self.original_hub = api.native._HUB
        api.native._HUB = api.native.NativeVoiceHub()
        self.fields = {'agentId': 'friday', 'sessionId': 'saved', 'voiceId': 'gated-fixture'}

    async def asyncTearDown(self):
        await api.native._HUB.shutdown()
        api.native._HUB = self.original_hub
        self.modules.stop()
        await self.client.aclose()

    async def post(self, operation, **kwargs):
        return await self.client.post('/api/plugins/agents-cs/voice/' + operation, **kwargs)

    async def test_verified_cookie_session_needs_no_legacy_header_and_survives_refresh(self):
        response = await self.post('close', json=self.fields)
        self.assertEqual(response.status_code, 200)
        self.session.access_token = 'rotated-fixture-access'
        response = await self.post('poll', json={**self.fields, 'after': 0})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['closed'])

    async def test_unverified_header_cannot_replace_missing_or_expired_gated_session(self):
        for session in (None, object(), FixtureSession(expires_at=int(time.time()) - 1)):
            self.session = session
            response = await self.post('close', json=self.fields,
                                       headers={'X-Hermes-Session-Token': 'caller-chosen'})
            self.assertEqual(response.status_code, 401)

    async def test_different_verified_account_cannot_adopt_call_with_same_header(self):
        headers = {'X-Hermes-Session-Token': 'shared-caller-chosen'}
        self.assertEqual((await self.post('close', json=self.fields, headers=headers)).status_code, 200)
        for changes in ({'user_id': 'other'}, {'provider': 'other'}, {'org_id': 'other'}):
            self.session = FixtureSession(**changes)
            response = await self.post('poll', json={**self.fields, 'after': 0}, headers=headers)
            self.assertEqual(response.status_code, 409)

    async def test_standalone_missing_auth_configuration_fails_closed(self):
        self.session = None
        del self.app.state.auth_required
        response = await self.post('close', json=self.fields,
                                   headers={'X-Hermes-Session-Token': 'caller-chosen'})
        self.assertEqual(response.status_code, 401)

    async def test_note_route_is_wired_validated_and_owned(self):
        contexts = []
        class Fixture:
            def __init__(self, **kwargs):
                self.provider_event_types, self.schema_mismatches = {}, 0
            async def create(self, sdp, **kwargs):
                return sdp
            async def append_context(self, text, *, quiet=True):
                contexts.append((text, quiet))
            async def close(self):
                pass
        api.native._HUB = api.native.NativeVoiceHub(provider_factory=Fixture)
        sdp = 'v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n'
        offer = {**self.fields, 'provider': 'codex_subscription', 'voice': 'cove', 'sdp': sdp}
        self.assertEqual((await self.post('offer', json=offer)).status_code, 200)
        response = await self.post('note', json={**self.fields, 'text': 'Reading mail.', 'speak': True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'voiceId': 'gated-fixture', 'noted': True})
        self.assertEqual(contexts, [('Reading mail.', False)])
        for body in ({**self.fields, 'text': 'x', 'speak': 'yes'}, {**self.fields, 'text': 'x'},
                     {**self.fields, 'text': 'x' * 1601, 'speak': False}):
            self.assertEqual((await self.post('note', json=body)).status_code, 422)
        response = await self.post('note', json={**self.fields, 'text': 'bad\x00', 'speak': False})
        self.assertEqual((response.status_code, response.json()['code']), (422, 'voice_note_invalid'))
        self.session = FixtureSession(user_id='other')
        response = await self.post('note', json={**self.fields, 'text': 'x', 'speak': False})
        self.assertEqual((response.status_code, response.json()['code']), (409, 'voice_owner_changed'))
        self.assertEqual(len(contexts), 1)

    async def test_status_advertises_v2_features(self):
        response = await self.post('status', json={'agentId': 'friday', 'sessionId': 'saved', 'provider': 'codex_subscription'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['capability'], 'native-voice-v2')
        self.assertEqual(response.json()['features'], ['long_poll', 'streamed_results', 'context', 'long_notes'])

    def v2_fixture(self):
        created, results = [], []
        class Fixture:
            def __init__(self, **kwargs):
                self.provider_event_types, self.schema_mismatches = {}, 0
                self.on_event = kwargs['on_event']
            async def create(self, sdp, **kwargs):
                created.append(kwargs)
                await self.on_event({'kind': 'delegation', 'id': 'item', 'text': 'Plan my day'})
                return sdp
            async def append_result(self, identifier, text, **kwargs):
                results.append((identifier, text))
            async def append_context(self, text, *, quiet=True):
                pass
            async def close(self):
                pass
        api.native._HUB = api.native.NativeVoiceHub(provider_factory=Fixture)
        sdp = 'v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n'
        return {**self.fields, 'provider': 'codex_subscription', 'voice': 'cove', 'sdp': sdp}, created, results

    async def test_offer_context_is_validated_and_trimmed(self):
        offer, created, _ = self.v2_fixture()
        entry = {'role': 'user', 'text': 'Book the dentist'}
        for context in ([{**entry, 'role': 'system'}], [{**entry, 'text': 'x' * 801}], [{**entry, 'text': ''}],
                        [{**entry, 'extra': 1}], [entry] * 17, 'not a list', [{'role': 'user'}]):
            response = await self.post('offer', json={**offer, 'context': context})
            self.assertEqual((response.status_code, response.json()['code']), (422, 'voice_invalid_request'))
        self.assertEqual(created, [])
        context = [{'role': 'assistant', 'text': 'y' * 800}] * 10 + [entry]
        self.assertEqual((await self.post('offer', json={**offer, 'context': context})).status_code, 200)
        self.assertEqual(created, [{'voice': 'cove', 'context': context[-10:]}])  # 9 x 800 + 16 bytes.

    async def test_offer_without_context_passes_none(self):
        offer, created, _ = self.v2_fixture()
        self.assertEqual((await self.post('offer', json=offer)).status_code, 200)
        self.assertEqual(created, [{'voice': 'cove'}])

    async def test_long_poll_route_and_bounds(self):
        offer, _, _ = self.v2_fixture()
        self.assertEqual((await self.post('offer', json=offer)).status_code, 200)
        for changes in ({'wait': 10_001}, {'wait': -1}, {'wait': 1.5}, {'limit': 0}, {'limit': 33}, {'limit': '4'}):
            response = await self.post('poll', json={**self.fields, 'after': 0, **changes})
            self.assertEqual(response.status_code, 422, changes)
        page = (await self.post('poll', json={**self.fields, 'after': 0, 'wait': 5000, 'limit': 32})).json()
        self.assertEqual(page['events'][0]['event']['kind'], 'delegation')
        page = (await self.post('poll', json={**self.fields, 'after': 1, 'wait': 30})).json()
        self.assertEqual((page['events'], page['next']), ([], 1))
        legacy = (await self.post('poll', json={**self.fields, 'after': 1})).json()
        self.assertEqual(set(legacy), {'voiceId', 'closed', 'next', 'events', 'diagnostics'})

    async def test_streamed_result_route(self):
        offer, _, results = self.v2_fixture()
        self.assertEqual((await self.post('offer', json=offer)).status_code, 200)
        body = {**self.fields, 'delegationId': 'item', 'text': 'Part'}
        for changes in ({'part': 256}, {'part': -1}, {'part': '0'}, {'part': 0, 'final': 'yes'}):
            self.assertEqual((await self.post('result', json={**body, **changes})).status_code, 422, changes)
        self.assertEqual((await self.post('result', json={**body, 'part': 0, 'final': False})).status_code, 200)
        response = await self.post('result', json={**body, 'part': 0, 'final': True})
        self.assertEqual((response.status_code, response.json()['code']), (409, 'voice_result_unavailable'))
        self.assertEqual((await self.post('result', json={**body, 'part': 1, 'final': True})).status_code, 200)
        self.assertEqual(results, [('item', 'Part'), ('item', 'Part')])

    async def test_long_note_route(self):
        offer, _, _ = self.v2_fixture()
        self.assertEqual((await self.post('offer', json=offer)).status_code, 200)
        response = await self.post('note', json={**self.fields, 'text': 'x' * 1600, 'speak': True})
        self.assertEqual(response.status_code, 200)

    async def test_legacy_mode_refuses_service_seam_or_competing_bearer(self):
        self.session = None
        self.app.state.auth_required = False
        response = await self.post('close', json=self.fields, headers={
            'X-Hermes-Session-Token': 'caller-chosen', 'Authorization': 'Bearer independently-valid'})
        self.assertEqual(response.status_code, 401)
        self.service_authenticated = True
        response = await self.post('close', json=self.fields,
                                   headers={'X-Hermes-Session-Token': 'caller-chosen'})
        self.assertEqual(response.status_code, 401)


class VoiceChoiceRouteTests(unittest.IsolatedAsyncioTestCase):
    """A bot's voice is kept on the host, so every device signed in to it agrees."""
    async def asyncSetUp(self):
        import tempfile
        self.directory = tempfile.TemporaryDirectory()
        self.saved = api._PREFERENCES
        api._PREFERENCES = api.preferences.VoicePreferences(Path(self.directory.name) / 'voices.json')
        self.app = FastAPI()
        self.app.state.auth_required = False
        self.app.include_router(api.router, prefix='/api/plugins/agents-cs')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://fixture')
    async def asyncTearDown(self):
        await self.client.aclose()
        api._PREFERENCES = self.saved
        self.directory.cleanup()
    async def post(self, operation, body, token='owner'):
        return await self.client.post('/api/plugins/agents-cs/voice/' + operation, json=body,
                                      headers={'X-Hermes-Session-Token': token})
    async def test_choice_is_shared_across_sign_ins(self):
        empty = await self.post('voices', {'agentId': 'friday'})
        self.assertEqual(empty.json(), {'agentId': 'friday', 'voices': {}})
        saved = await self.post('voice_set', {'agentId': 'friday', 'provider': 'codex_subscription', 'voice': 'ember'})
        self.assertEqual(saved.json()['voices'], {'codex_subscription': 'ember'})
        await self.post('voice_set', {'agentId': 'friday', 'provider': 'codex_subscription', 'voice': 'sol'}, token='other')
        read = await self.post('voices', {'agentId': 'friday'}, token='other')
        self.assertEqual(read.json()['voices'], {'codex_subscription': 'sol'})
        other_bot = await self.post('voices', {'agentId': 'jarvis'})
        self.assertEqual(other_bot.json()['voices'], {})
    async def test_invalid_choices_are_rejected(self):
        for body in [{'agentId': 'friday', 'provider': 'other', 'voice': 'ember'},
                     {'agentId': 'friday', 'provider': 'api_key', 'voice': 'ember'},
                     {'agentId': 'friday', 'provider': 'codex_subscription', 'voice': 'Ember!'},
                     {'agentId': '../x', 'provider': 'codex_subscription', 'voice': 'ember'},
                     {'agentId': 'friday', 'provider': 'codex_subscription', 'voice': 'ember', 'extra': 1}]:
            response = await self.post('voice_set', body)
            self.assertEqual(response.status_code, 422, body)
        unauthenticated = await self.client.post('/api/plugins/agents-cs/voice/voices', json={'agentId': 'friday'})
        self.assertEqual(unauthenticated.status_code, 401)
    async def test_damaged_file_reads_as_no_choices(self):
        api._PREFERENCES.path.write_text('{"friday": {"api_key": 5, "codex_subscription": "ember"}, "BAD": {}}')
        read = await self.post('voices', {'agentId': 'friday'})
        self.assertEqual(read.json()['voices'], {'codex_subscription': 'ember'})
