"""No live tasks or LLM calls: creation events are the only planning trigger."""
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from test_tasks import plugin, store_module, FakeBoard, family_board, selection

creation = plugin.creation


class CreationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'forms.db'
        self.log = creation.CreationLog(self.path)
        self.log.set_enabled(True)
        self.decisions = store_module.DecisionLog(self.path)
        self.board = FakeBoard([FakeBoard.task_row('fresh', 'triage', created_at=int(time.time()), idempotency_key='receipt')])
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def decompose(self, tid, board):
        self.calls.append((tid, board))
        return {'ok': True, 'fanout': True, 'child_ids': ['a', 'b']}

    def test_prepare_once_even_after_restart_and_retriage(self):
        result = creation.prepare(self.board, self.decisions, self.log, 'fresh', 'receipt', self.decompose)
        self.assertTrue(result['ok'])
        reloaded = creation.CreationLog(self.path)
        for status in ('running', 'blocked', 'triage'):
            self.board._tasks['fresh']['status'] = status
            self.assertEqual(creation.prepare(self.board, self.decisions, reloaded, 'fresh', 'receipt', self.decompose), result)
        self.assertEqual(self.calls, [('fresh', 'default')])

    def test_concurrent_reservations_have_one_owner(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.log.reserve('default', 'fresh'), range(8)))
        self.assertEqual(sum(results), 1)
        self.assertTrue(creation.CreationLog(self.path).result('default', 'fresh')['pending'])
        self.assertTrue(self.log.reserve('another-board', 'fresh'))

    def test_failed_attempt_is_not_repeated(self):
        def fail(*_):
            self.calls.append('failed')
            raise TimeoutError()
        a = creation.prepare(self.board, self.decisions, self.log, 'fresh', 'receipt', fail)
        b = creation.prepare(self.board, self.decisions, creation.CreationLog(self.path), 'fresh', 'receipt', fail)
        self.assertFalse(a['ok'])
        self.assertEqual(a, b)
        self.assertEqual(self.calls, ['failed'])
        self.assertEqual(len(self.board._comments['fresh']), 1)

    def test_duplicate_reserved_runners_execute_only_once(self):
        self.assertTrue(self.log.reserve('default', 'fresh'))
        def run(_):
            return creation.prepare(self.board, self.decisions, creation.CreationLog(self.path),
                                    'fresh', 'receipt', self.decompose, reserved=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(run, range(8)))
        self.assertEqual(self.calls, [('fresh', 'default')])
        self.assertTrue(self.log.result('default', 'fresh')['ok'])

    def test_old_or_changed_work_never_decomposes(self):
        variants = [{'status': s} for s in ('ready', 'running', 'blocked', 'scheduled', 'review', 'done')]
        variants += [{'started_at': 5}, {'current_run_id': 2}, {'block_kind': 'needs_input'}, {'idempotency_key': 'different'}]
        for fields in variants:
            board = FakeBoard([FakeBoard.task_row('fresh', 'triage', idempotency_key='receipt')])
            board._tasks['fresh'].update(fields)
            result = creation.prepare(board, self.decisions, self.log, 'fresh', 'receipt', self.decompose)
            self.assertFalse(result['ok'], fields)
        for event in ('claimed', 'blocked', 'block_loop_detected', 'specified', 'review_requested', 'scheduled'):
            self.board._events['fresh'] = [{'kind': event}]
            self.assertIsNotNone(creation.rejection(self.board, self.decisions, 'fresh', 'receipt'))
        self.assertEqual(self.calls, [])

    def test_subtasks_and_approval_history_excluded(self):
        board = family_board()
        for tid in ('r1', 'c2'):
            board._tasks[tid].update(status='triage', idempotency_key='receipt', block_kind=None)
            self.assertIsNotNone(creation.rejection(board, self.decisions, tid, 'receipt'))
        card = selection()
        self.decisions.record(card, profile='friday', session_id='s')
        a = self.decisions.bind_task(card, profile='friday', board='default', task_id='fresh')
        self.assertIsNotNone(creation.rejection(self.board, self.decisions, 'fresh', 'receipt'))
        self.decisions.answer(card['id'], a['payload_hash'], {'selected': ['thu']}, 'mark')
        self.assertIsNotNone(creation.rejection(self.board, self.decisions, 'fresh', 'receipt'))

    def test_bot_app_creation_uses_native_tool_then_one_background_attempt(self):
        bot = creation.BotCreation(self.log, self.decisions)
        bot.observe_turn('s', 'api')
        args = {'title': 'Plan dinner', 'assignee': 'default', 'idempotency_key': 'receipt'}
        with patch.object(creation, 'background_enabled', return_value=False), \
             patch.object(creation.tasks, 'HermesBoard', return_value=self.board), patch.object(creation, 'launch') as launch:
            self.assertEqual(bot.before('kanban_create', args, 's', 'call'), {'action': 'modify', 'args': {'triage': True}})
            result = json.dumps({'ok': True, 'task_id': 'fresh', 'status': 'triage'})
            bot.after('kanban_create', args, result, 's', 'call')
            bot.after('kanban_create', args, result, 's', 'call')
            launch.assert_called_once_with('default', 'fresh', 'receipt')
            transformed = json.loads(bot.transform('kanban_create', args, result))
            self.assertTrue(transformed['creation_planning']['pending'])
            self.assertIn('Do not specify', transformed['next_step'])

    def test_comments_restarts_cron_workers_and_children_cannot_trigger(self):
        bot = creation.BotCreation(self.log, self.decisions)
        bot.observe_turn('app', 'api')
        bot.observe_turn('cron', 'cron')
        bot.observe_turn('teams', 'teams')
        with patch.object(creation, 'background_enabled', return_value=False):
            for name in ('kanban_comment', 'kanban_unblock', 'kanban_request_review', 'kanban_edit'):
                self.assertIsNone(bot.before(name, {}, 'app', 'call'))
            for session in ('cron', 'teams', 'unknown'):
                self.assertIsNone(bot.before('kanban_create', {}, session, 'call'))
            self.assertIsNone(bot.before('kanban_create', {'parents': ['parent']}, 'app', 'call'))
            self.assertIsNone(bot.before('kanban_create', {'initial_status': 'blocked'}, 'app', 'call'))
            with patch.dict('os.environ', {'HERMES_KANBAN_TASK': 'worker'}):
                self.assertIsNone(bot.before('kanban_create', {}, 'app', 'call'))
            self.log.set_enabled(False)
            self.assertIsNone(bot.before('kanban_create', {}, 'app', 'call'))

    def test_native_idempotency_returning_old_task_does_not_trigger(self):
        bot = creation.BotCreation(self.log, self.decisions)
        bot.observe_turn('app', 'api')
        self.board._tasks['fresh']['created_at'] = 1
        with patch.object(creation, 'background_enabled', return_value=False), \
             patch.object(creation.tasks, 'HermesBoard', return_value=self.board), patch.object(creation, 'launch') as launch:
            bot.before('kanban_create', {}, 'app', 'call')
            bot.after('kanban_create', {}, {'task_id': 'fresh'}, 'app', 'call')
            launch.assert_not_called()

    def test_routes_share_setting_and_replay_creation_receipt(self):
        import importlib.util
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import plugin_loader
        api = plugin_loader.load_fresh('cards.routes', 'creation_route_fixture')
        api._creation_log, api._decisions = self.log, self.decisions
        app = FastAPI()
        app.include_router(api.router)
        with patch.object(api._creation, 'background_enabled', return_value=False), \
             patch.object(api._tasks, 'HermesBoard', return_value=self.board), \
             patch.object(api, 'decompose_with_scope', side_effect=lambda tid, author, slug: self.decompose(tid, slug)):
            client = TestClient(app)
            self.assertTrue(client.get('/kanban/creation-settings').json()['decompose_on_create'])
            self.assertEqual(client.put('/kanban/creation-settings', json={'decompose_on_create': 'yes'}).status_code, 400)
            self.assertFalse(client.put('/kanban/creation-settings', json={'decompose_on_create': False}).json()['decompose_on_create'])
            body = {'creation_key': 'receipt', 'author': 'mark'}
            a = client.post('/kanban/prepare-new/fresh', json=body)
            b = client.post('/kanban/prepare-new/fresh', json=body)
            self.assertEqual(a.json(), b.json())
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(client.post('/kanban/prepare-new/fresh', json={}).status_code, 400)
        with patch.object(api._creation, 'background_enabled', return_value=True):
            self.assertEqual(client.put('/kanban/creation-settings', json={'decompose_on_create': True}).status_code, 409)
