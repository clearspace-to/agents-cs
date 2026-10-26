"""Auto effort (the routing module): one classification per turn, the chosen effort on the chat's own model.

What changed on 2026-10-26: Model Routing's model switching and jev_review are gone, and Auto effort actually picks an
effort. On the Mac mini it had changed nothing in 424 turns: three questions all had to be 90 % sure (398 "uncertain"),
masking ran on serialized JSON and broke its escapes ("Invalid \\escape"), and a pick equal to the chat's own effort
was dropped. These tests pin each of those down."""
import copy
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugin_loader  # noqa: E402
from test_plugin import Ctx  # noqa: E402

root = plugin_loader.load_root()
effort = importlib.import_module(root.__name__ + ".routing.effort")
settings = importlib.import_module(root.__name__ + ".routing.settings")
paths = importlib.import_module(root.__name__ + ".paths")


def answer(choice, probability=1.0, confidence=None):
    """The shape OpenRouter's decisions API returned live (2026-10-26)."""
    rest = (1 - probability) / 2
    probabilities = {x: (probability if x == choice else rest) for x in effort.EFFORTS}
    return {'model': 'typesafe/jev-1.13-20260917', 'provider': 'TypeSafe', 'usage': {'input_tokens': 300},
            'answers': {'effort': {'type': 'choice', 'choice': choice, 'probabilities': probabilities,
                                   'confidence': probability if confidence is None else confidence}}}


class PrepareTests(unittest.TestCase):
    def test_masking_never_breaks_a_json_escape(self):
        """The live 'Invalid \\escape' turns: an email after a newline was masked inside the serialized JSON."""
        state = effort.prepare('Reply to this:\nsam@example.com wrote "hi"\n+1 (416) 555-0100',
                               [{'role': 'assistant', 'content': 'path C:\\new\nbob@example.org'}])
        raw = json.dumps(state, ensure_ascii=False)
        self.assertEqual(json.loads(raw), state)
        self.assertNotIn('example.com', raw)
        self.assertNotIn('555-0100', raw)
        self.assertIn('[email]', state['current_request'])

    def test_long_messages_are_trimmed_not_refused_and_attachments_are_skipped(self):
        state = effort.prepare('x' * 20000, [
            {'role': 'user', 'content': [{'type': 'text', 'text': 'see the photo'}, {'type': 'image_url', 'image_url': {}}]},
            {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 't'}]},
            {'role': 'tool', 'content': 'secret tool output'},
            {'role': 'assistant', 'content': 'Here is the summary.'}])
        self.assertLess(len(state['current_request']), 4100)
        self.assertEqual([x['text'] for x in state['recent_exchange']], ['see the photo', 'Here is the summary.'])

    def test_multi_part_messages_use_their_text(self):
        state = effort.prepare([{'type': 'text', 'text': 'What is in this picture?'}, {'type': 'image_url'}], [])
        self.assertEqual(state['current_request'], 'What is in this picture?')

    def test_credentials_are_masked_and_the_current_message_is_not_repeated(self):
        state = effort.prepare('my api_key = sk-or-v1-' + 'a' * 40, [{'role': 'user', 'content': 'my api_key = sk-or-v1-' + 'a' * 40}])
        self.assertNotIn('aaaa', json.dumps(state))
        self.assertEqual(state['recent_exchange'], [])

    def test_turns_nobody_typed_and_explicit_choices_are_skipped(self):
        for text, reason in (('', 'no_text'), ('/reasoning high', 'not_a_person'), ('[Cronjob] daily', 'not_a_person'),
                             ('Think harder about this one', 'explicit_choice'), ('use high reasoning please', 'explicit_choice')):
            with self.subTest(text=text), self.assertRaises(effort.Skip) as skipped:
                effort.prepare(text, [])
            self.assertEqual(skipped.exception.reason, reason)


class ParseTests(unittest.TestCase):
    def test_a_clear_pick_is_taken(self):
        self.assertEqual(effort.parse(answer('high', 0.72, confidence=0.58)), ('high', 'classified'))
        self.assertEqual(effort.parse(answer('low')), ('low', 'classified'))

    def test_an_unsure_pick_keeps_the_chats_effort(self):
        self.assertEqual(effort.parse(answer('medium', 0.45)), (None, 'uncertain'))

    def test_tolerant_of_the_answers_shape(self):
        loose = {'answers': {'effort': {'choice': 'medium', 'confidence': '0.8', 'probabilities': {'medium': 0.8}}}}
        self.assertEqual(effort.parse(loose), ('medium', 'classified'), "probabilities needn't add up or list all")
        self.assertEqual(effort.parse({'answers': {'effort': {'choice': 'high', 'confidence': 0.9}}}), ('high', 'classified'))
        self.assertEqual(effort.parse({'answers': {'effort': {'choice': 'low'}}}), ('low', 'classified'))
        for broken in (None, {}, {'answers': []}, {'answers': {'effort': {'choice': 'xhigh'}}}, {'answers': {'effort': 'low'}}):
            self.assertEqual(effort.parse(broken), (None, 'no_answer'), broken)


class EngineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.calls, self.reply = [], answer('high')

        def api(state, questions, timeout):
            self.calls.append(state)
            return self.reply

        self.engine = effort.Engine(self.home, api=api, manual=lambda home, sid: False)
        self.addCleanup(self.engine.pool.shutdown)
        self.store = self.engine.store
        self.store.save_policy(True, self.store.policy()['revision'])
        self.codex = {'model': 'gpt-6.1-sol', 'reasoning': {'effort': 'low', 'summary': 'auto'},
                      'input': [{'role': 'user', 'content': 'body'}]}
        self.claude = {'model': 'claude-sonnet-5-5', 'max_tokens': 64000, 'thinking': {'type': 'adaptive'},
                       'output_config': {'effort': 'low'}, 'messages': [{'role': 'user', 'content': 'body'}]}

    def prepare(self, turn='t1', text='Plan the database migration and its rollback.', history=None, platform='tui'):
        self.engine.prepare(session_id='s', turn_id=turn, user_message=text, conversation_history=history or [],
                            platform=platform, model='gpt-6.1-sol')

    def codex_request(self, turn='t1', request=None, model='gpt-6.1-sol'):
        return self.engine.request(request=request or self.codex, session_id='s', turn_id=turn, provider='openai-codex',
                                   model=model, api_mode='codex_responses')

    def test_the_pick_goes_on_every_request_of_the_turn_and_nothing_else_changes(self):
        self.prepare()
        before = copy.deepcopy(self.codex)
        out = self.codex_request()['request']
        expected = copy.deepcopy(before)
        expected['reasoning']['effort'] = 'high'
        self.assertEqual(out, expected)
        self.assertEqual(self.codex, before, "Hermes' own request is never mutated")
        self.assertEqual(self.codex_request()['request'], out, 'the tool loop keeps the same effort')
        self.assertEqual(len(self.calls), 1, 'one classification per turn')
        (turn,) = self.store.turns('s')
        self.assertEqual((turn['effort'], turn['from'], turn['reason']), ('high', 'low', 'classified'))
        self.assertNotIn('migration', self.store.path.read_bytes().decode(errors='ignore'), 'no content is stored')

    def test_a_pick_equal_to_the_chats_own_effort_is_still_recorded(self):
        """It used to be dropped, so the app showed nothing and the logs said no effort was ever chosen."""
        self.reply = answer('low')
        self.prepare(text='Thanks!')
        self.assertIsNone(self.codex_request(), 'nothing to change')
        self.assertEqual(self.store.turns('s')[0]['effort'], 'low')

    def test_claude_gets_its_effort_in_output_config(self):
        self.reply = answer('medium')
        self.prepare()
        out = self.engine.request(request=self.claude, session_id='s', turn_id='t1', provider='anthropic',
                                  model='claude-sonnet-5-5', api_mode='anthropic_messages')['request']
        self.assertEqual(out['output_config'], {'effort': 'medium'})
        self.assertEqual((out['model'], out['thinking'], out['messages']), (self.claude['model'], self.claude['thinking'], self.claude['messages']))

    def test_thinking_off_and_unknown_providers_are_left_alone(self):
        off = dict(self.claude, thinking={'type': 'disabled'})
        self.prepare()
        self.assertIsNone(self.engine.request(request=off, session_id='s', turn_id='t1', provider='anthropic',
                                              model='claude-sonnet-5-5', api_mode='anthropic_messages'))
        self.assertEqual(self.store.turns('s')[0]['reason'], 'thinking_off')
        self.prepare('t2')
        self.assertIsNone(self.engine.request(request={'model': 'x', 'messages': []}, session_id='s', turn_id='t2',
                                              provider='openrouter', model='x', api_mode='chat_completions'))
        self.assertEqual(self.store.turns('s')[0]['reason'], 'unsupported_provider')

    def test_side_effects_never_run_on_low(self):
        self.reply = answer('low')
        self.prepare(text='Delete the old drafts')
        self.assertEqual(self.codex_request()['request']['reasoning']['effort'], 'medium')
        self.prepare('t2', text='yes do it', history=[{'role': 'assistant', 'content': 'Shall I send the email to Sam?'}])
        self.assertEqual(self.codex_request('t2')['request']['reasoning']['effort'], 'medium')

    def test_uncertain_slow_and_failing_classifiers_keep_the_chats_effort(self):
        self.reply = answer('medium', 0.4)
        self.prepare()
        self.assertIsNone(self.codex_request())
        self.assertEqual(self.store.turns('s')[0]['reason'], 'uncertain')
        release = threading.Event()
        self.engine.api = lambda *a: (release.wait(5), answer('high'))[1]
        with patch.object(effort, 'DEADLINE', 0.05):
            self.prepare('slow')
            self.assertIsNone(self.codex_request('slow'))
        release.set()
        self.assertEqual(self.store.turns('s')[0]['reason'], 'deadline')
        self.engine.api = lambda *a: (_ for _ in ()).throw(settings.KeyUnavailable('none'))
        self.prepare('nokey')
        self.assertIsNone(self.codex_request('nokey'))
        self.assertEqual(self.store.turns('s')[0]['reason'], 'classifier_unavailable')

    def test_off_by_default_and_by_the_chats_own_switch(self):
        fresh = effort.Engine(self.home / 'other', api=lambda *a: answer('high'), manual=lambda *a: False)
        self.addCleanup(fresh.pool.shutdown)
        fresh.prepare(session_id='s', turn_id='t', user_message='Plan it', conversation_history=[], platform='tui')
        self.assertIsNone(fresh.request(request=self.codex, session_id='s', turn_id='t', provider='openai-codex',
                                        model='gpt-6.1-sol', api_mode='codex_responses'))
        self.assertFalse(fresh.store.exists(), 'a bot nobody switched on gets no store')
        self.store.session('s', 'owner')
        self.store.save_session('s', 'owner', False, 1)
        self.prepare()
        self.assertIsNone(self.codex_request())
        self.assertEqual(self.calls, [], 'no classifier call when off')

    def test_a_manual_effort_wins_over_the_default_until_the_app_manages_the_chat(self):
        self.engine.manual = lambda home, sid: True
        self.prepare()
        self.assertIsNone(self.codex_request())
        self.store.session('s', 'owner')
        self.store.save_session('s', 'owner', None, 1)  # the app follows the default for this chat
        self.prepare('t2')
        self.assertEqual(self.codex_request('t2')['request']['reasoning']['effort'], 'high')

    def test_switching_off_mid_turn_restores_the_chats_effort(self):
        self.prepare()
        self.assertIsNotNone(self.codex_request())
        self.store.save_policy(False, self.store.policy()['revision'])
        self.assertIsNone(self.codex_request())

    def test_other_surfaces_and_hermes_fallback_models_are_left_alone(self):
        self.prepare('cron', platform='cron')
        self.assertIsNone(self.codex_request('cron'))
        self.prepare()
        self.assertIsNotNone(self.codex_request())
        self.assertIsNone(self.codex_request(request=dict(self.codex, model='gpt-6-astra'), model='gpt-6-astra'))

    def test_registers_two_hooks_scoped_to_its_home(self):
        ctx = Ctx()
        with patch.dict(os.environ, {'HERMES_HOME': str(self.home)}):
            importlib.import_module(root.__name__ + '.routing').register(ctx)
        self.assertIn('pre_llm_call', ctx.hooks)
        self.assertIn('llm_request', ctx.middleware)
        self.assertEqual(ctx.tools, {})


class StoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = effort.Store(tmp.name)

    def test_rows_from_model_routing_still_read(self):
        """The Mac mini's stores were written by Model Routing: the Auto effort switches carry over."""
        self.store.policy()
        with sqlite3.connect(self.store.path) as db:
            db.execute('UPDATE policy SET value=? WHERE id=1', (json.dumps({
                'schemaVersion': 2, 'revision': 5, 'autoModelDefault': False, 'autoEffortDefault': True, 'enabledDefault': False,
                'slots': {'small': {'provider': 'openai-codex', 'model': 'gpt-6-luna', 'effort': 'low'}}}),))
            db.execute('INSERT INTO sessions VALUES(?,?,?)', ('s', 'owner', json.dumps({
                'revision': 3, 'autoModelOverride': True, 'autoEffortOverride': False, 'managed': True,
                'selectedBaseline': {'provider': 'anthropic', 'model': 'claude-sonnet-5-5', 'effort': 'low'}})))
            db.execute('INSERT INTO served VALUES(?,?,?,?)', ('s', 'old', time.time(), json.dumps({
                'turnId': 'old', 'slot': None, 'reason': 'uncertain', 'served': {'model': 'x', 'effort': 'low'}})))
        self.assertEqual(self.store.policy(), {'revision': 5, 'autoEffortDefault': True})
        self.assertEqual(self.store.switches('s'), (True, False, True))
        self.assertEqual(self.store.turns('s'), [], 'old rows label nothing')
        self.assertEqual(self.store.save_policy(False, 5), {'revision': 6, 'autoEffortDefault': False})

    def test_compare_and_swap_and_owners(self):
        self.store.policy()
        with self.assertRaises(effort.Conflict):
            self.store.save_policy(True, 7)
        self.store.session('s', 'owner')
        with self.assertRaises(LookupError):
            self.store.session('s', 'someone-else')
        with self.assertRaises(effort.Conflict):
            self.store.save_session('s', 'owner', True, 9)
        self.assertEqual(self.store.save_session('s', 'owner', True, 1), {'revision': 2, 'autoEffortOverride': True, 'managed': True})


@unittest.skipUnless(importlib.util.find_spec('fastapi'), 'FastAPI is not installed (use Hermes\'s venv python)')
class RoutesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {'HERMES_HOME': tmp.name})
        env.start()
        self.addCleanup(env.stop)
        (self.root / 'profiles' / 'jarvis').mkdir(parents=True)
        with sqlite3.connect(self.root / 'profiles' / 'jarvis' / 'state.db') as db:
            db.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY, user_id TEXT, model_config TEXT)')
            db.execute("INSERT INTO sessions VALUES('chat', NULL, '{}')")
        self.routes = importlib.import_module(root.__name__ + '.routing.routes')

    def call(self, operation, method='GET', **body):
        return self.routes.dispatch(operation, method, body, 'owner', None)

    def refused(self, operation, method='GET', **body):
        with self.assertRaises(self.routes.Refused) as refused:
            self.call(operation, method, **body)
        return refused.exception.code

    def test_capabilities_say_whether_the_classifier_has_a_key(self):
        self.assertEqual(self.call('capabilities', agentId='jarvis'),
                         {'schemaVersion': 3, 'autoEffort': True, 'efforts': ['low', 'medium', 'high'], 'classifierAvailable': False})
        (self.root / 'secrets').mkdir()
        (self.root / 'secrets' / 'jev-openrouter-api-key').write_text('k')
        self.assertTrue(self.call('capabilities', agentId='jarvis')['classifierAvailable'], "the root's key serves every bot")
        self.assertEqual(self.refused('capabilities', agentId='nobody'), 'agent_not_found')

    def test_the_bots_default_and_the_chats_own_switch(self):
        policy = self.call('policy', agentId='jarvis')
        self.assertEqual(policy, {'revision': 1, 'autoEffortDefault': False})
        self.assertEqual(self.call('policy', 'PUT', agentId='jarvis', expectedRevision=1, autoEffortDefault=True),
                         {'revision': 2, 'autoEffortDefault': True})
        self.assertEqual(self.refused('policy', 'PUT', agentId='jarvis', expectedRevision=1, autoEffortDefault=False), 'policy_conflict')
        self.assertEqual(self.refused('policy', 'PUT', agentId='jarvis', expectedRevision=2, autoEffortDefault=False,
                                      autoModelDefault=False, slots={}), 'invalid_fields', 'Auto model is gone')
        session = self.call('session', agentId='jarvis', sessionId='chat')
        self.assertEqual((session['revision'], session['autoEffortOverride']), (1, None))
        saved = self.call('session', 'PUT', agentId='jarvis', sessionId='chat', expectedRevision=1, autoEffortOverride=False)
        self.assertEqual((saved['revision'], saved['autoEffortOverride']), (2, False))
        self.assertEqual(self.refused('session', 'PUT', agentId='jarvis', sessionId='chat', expectedRevision=1,
                                      autoEffortOverride=True), 'selection_conflict')
        self.assertEqual(self.refused('session', agentId='jarvis', sessionId='missing'), 'session_not_found')
        self.assertEqual(self.call('turns', agentId='jarvis', sessionId='chat'), {'turns': []})
        for gone in ('plan', 'plan/arm'):
            self.assertEqual(self.refused(gone, 'POST', agentId='jarvis', sessionId='chat'), 'unknown_operation')


if __name__ == '__main__':
    unittest.main()
