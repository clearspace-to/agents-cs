from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mobile_support import home, mobile  # noqa: E402

usage = mobile("usage")
report, window = usage.report, usage.window

TZ = ZoneInfo('America/Toronto')
NOW = datetime(2026, 9, 23, 12, tzinfo=TZ).timestamp()  # Wednesday
START = datetime(2026, 9, 23, tzinfo=TZ).timestamp()


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.db = self.home / 'state.db'
        with sqlite3.connect(self.db) as c:
            c.executescript('''CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,started_at REAL,last_activity_at REAL,
                api_call_count INT, estimated_cost_usd REAL);
                CREATE TABLE session_model_usage(session_id TEXT,model TEXT,billing_provider TEXT,task TEXT,
                input_tokens INT, output_tokens INT, cache_read_tokens INT, cache_write_tokens INT, reasoning_tokens INT,
                api_call_count INT, estimated_cost_usd REAL, cost_status TEXT, first_seen REAL,last_seen REAL);''')
    def tearDown(self):
        self.temp.cleanup()
    def add(self, sid='s1', model='model-a', task='', source='desktop', cost=2, status='estimated', first=START+1, last=NOW-1, provider='provider'):
        with sqlite3.connect(self.db) as c:
            c.execute('INSERT OR IGNORE INTO sessions VALUES(?,?,?,?,?,?)', (sid,source,first,last,1,999))
            c.execute('INSERT INTO session_model_usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (sid,model,provider,task,100,50,200,20,30,1,cost,status,first,last))
    def get(self, period='today', now=NOW):
        return report(self.home, period, now, lambda p: 'Friday' if p == 'default' else p.title())
    def test_disjoint_tokens_and_no_session_double_count(self):
        self.add(); self.add(task='compression', cost=.5)
        r=self.get(); b=r['bots'][0]
        self.assertEqual(r['cost'],2.5)  # not session's 999
        self.assertEqual(b['totals']['input'],200)
        self.assertEqual(b['totals']['reasoning'],60)
        self.assertEqual(sum(b['totals'][k] for k in ['input','output','cache_read','cache_write']),740)
        self.assertEqual(sum(w['totals']['cost'] for w in b['work']),2.5)
        self.assertEqual(b['models'][0]['totals']['cost'],2.5)
    def test_boundary_record_included_with_elapsed_time_allocation(self):
        self.add(cost=8,first=START-100)
        self.add(sid='fresh',cost=2)
        r=self.get(); share=(NOW-1-START)/(NOW-1-(START-100))
        self.assertAlmostEqual(r['cost'],2+8*share)
        self.assertAlmostEqual(r['allocated_cost'],8*share)
        self.assertEqual(r['unallocated_cost'],0)
        self.assertTrue(r['partial'])
        self.assertEqual(self.get('week')['cost'],10)

    def test_missing_timestamps_assigned_to_current_day_and_future_excluded(self):
        self.add(first=None,last=None,cost=4);self.add(sid='future',first=NOW+100,last=NOW+200,cost=5)
        r=self.get();self.assertEqual(r['unallocated_cost'],0);self.assertEqual(r['cost'],4)
        self.assertEqual(r['allocated_cost'],4)

    def test_activity_allocation_reconciles_and_revises(self):
        read_bot = usage.read_bot
        self.add(cost=12,first=START-86400,last=NOW-1)
        with sqlite3.connect(self.db) as c:
            c.execute('CREATE TABLE messages(session_id TEXT, role TEXT, timestamp REAL)')
            c.executemany('INSERT INTO messages VALUES(?,?,?)',
                          [('s1','assistant',START-100),('s1','assistant',START),('s1','assistant',START+100)])
        r=self.get();self.assertEqual(r['cost'],8)
        previous=read_bot('default','Friday',self.db,START-86400,START-0.001)
        self.assertEqual(previous['totals']['cost']+r['cost'],12)
        for key in ['input','output','cache_read','cache_write','reasoning','calls']:
            self.assertEqual(previous['totals'][key]+r['bots'][0]['totals'][key],
                             self.get('week')['bots'][0]['totals'][key])
        with sqlite3.connect(self.db) as c:
            c.execute('UPDATE session_model_usage SET estimated_cost_usd=18')
        self.assertAlmostEqual(self.get()['cost'],12)  # source correction replaces the old estimate
        self.assertAlmostEqual(sum(m['totals']['cost'] for m in self.get()['bots'][0]['models']),12)

    def test_actual_cost_supersedes_estimated_cost(self):
        self.add(cost=8)
        with sqlite3.connect(self.db) as c:
            c.execute('ALTER TABLE session_model_usage ADD COLUMN actual_cost_usd REAL')
            c.execute('UPDATE session_model_usage SET actual_cost_usd=6')
        self.assertEqual(self.get()['cost'],6)

    def test_default_zero_actual_does_not_erase_estimate(self):
        self.add(cost=8)
        with sqlite3.connect(self.db) as c:
            c.execute('ALTER TABLE session_model_usage ADD COLUMN actual_cost_usd REAL DEFAULT 0')
        self.assertEqual(self.get()['cost'],8)

    def test_session_dates_recover_missing_ledger_timestamps(self):
        self.add(first=None,last=None,cost=4)
        with sqlite3.connect(self.db) as c:
            c.execute('UPDATE sessions SET started_at=?,last_activity_at=?',(START-86400,START-1))
        self.assertEqual(self.get()['cost'],0)
        self.assertEqual(self.get('week')['cost'],4)

    def test_sunday_week_and_last7_are_different_on_wednesday(self):
        self.assertEqual(datetime.fromtimestamp(window('week',NOW)[0],TZ).date().isoformat(),'2026-09-20')
        self.assertEqual(datetime.fromtimestamp(window('last7',NOW)[0],TZ).date().isoformat(),'2026-09-17')
        self.assertEqual(datetime.fromtimestamp(window('month',NOW)[0],TZ).day,1)
        sunday=datetime(2026,9,27,0,1,tzinfo=TZ).timestamp()
        self.assertEqual(datetime.fromtimestamp(window('week',sunday)[0],TZ).day,27)
    def test_calendar_window_across_dst(self):
        end=datetime(2026,3,10,12,tzinfo=TZ).timestamp()
        start,_=window('last7',end)
        local=datetime.fromtimestamp(start,TZ)
        self.assertEqual((local.day,local.hour),(4,0))
        self.assertEqual(local.utcoffset().total_seconds(),-18000)
    def test_unknown_price_keeps_tokens_and_does_not_claim_zero(self):
        self.add(cost=0,status='unknown')
        b=self.get()['bots'][0]
        self.assertIsNone(b['totals']['cost']);self.assertEqual(b['totals']['input'],100)
        self.assertIsNone(b['models'][0]['totals']['cost']);self.assertTrue(b['warnings'])
    def test_known_and_unknown_cost_is_partial_subtotal(self):
        self.add();self.add(sid='missing',cost=None)
        r=self.get();self.assertEqual(r['cost'],2);self.assertTrue(r['partial'])
        self.assertEqual(r['bots'][0]['totals']['unknown_costs'],1)
    def test_unpriced_legacy_zero_is_not_a_known_free_call(self):
        self.add(cost=0,status=None)
        self.assertIsNone(self.get()['cost'])

    def test_subscription_usage_not_missing(self):
        self.add(cost=0,status='included')
        r=self.get();self.assertFalse(r['partial']);self.assertEqual(r['cost'],0)
        self.assertEqual(r['bots'][0]['totals']['included_calls'],1)
    def test_cron_and_background_partition(self):
        self.add(source='cron');self.add(source='cron',task='approval',cost=.1)
        b=self.get()['bots'][0]
        self.assertEqual({x['id'] for x in b['work']},{'scheduled','task:approval'})
        self.assertAlmostEqual(sum(x['totals']['cost'] for x in b['work']),b['totals']['cost'])
    def test_same_model_different_providers_kept_separate(self):
        self.add();self.add(sid='s2',provider='other')
        self.assertEqual(len(self.get()['bots'][0]['models']),2)
    def test_missing_database_unknown_empty_database_zero(self):
        (self.home/'profiles'/'missing').mkdir(parents=True)
        r=self.get();self.assertEqual(r['bots'][0]['totals']['cost'],0)
        self.assertIsNone(r['bots'][1]['totals']['cost']);self.assertFalse(r['bots'][1]['available'])
        self.assertFalse((self.home/'profiles'/'missing'/'state.db').exists())
    def test_duplicate_database_not_added_twice(self):
        self.add();folder=self.home/'profiles'/'duplicate';folder.mkdir(parents=True)
        (folder/'state.db').symlink_to(self.db)
        r=self.get();self.assertEqual(r['cost'],2);self.assertTrue(r['partial'])
    def test_missing_main_usage_detected_even_with_auxiliary_row(self):
        self.add(task='vision')
        self.assertIn('main-loop',self.get()['bots'][0]['warnings'][0])
    def test_source_unchanged_and_no_private_fields(self):
        self.add();before=self.db.read_bytes();r=self.get()
        self.assertEqual(before,self.db.read_bytes())
        self.assertNotIn('session_id',json.dumps(r));self.assertNotIn('billing_base_url',json.dumps(r))
    def test_unsupported_schema_is_unknown_even_when_empty(self):
        with sqlite3.connect(self.db) as c:
            c.execute('DROP TABLE session_model_usage');c.execute('CREATE TABLE session_model_usage(session_id TEXT,last_seen REAL)')
        self.assertFalse(self.get()['bots'][0]['available'])
    def test_the_route_serves_the_homes_report_and_validates_the_period(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        routes = mobile("routes")
        app = FastAPI()
        app.include_router(routes.router, prefix="/api/plugins/agents-cs")
        client = TestClient(app)
        base = "/api/plugins/agents-cs/mobile/usage"
        with patch.object(home, "root", return_value=self.home):
            self.assertEqual(client.get(base + "?period=invalid").status_code, 422)
            r = client.get(base + "?period=last7")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["period"], "last7")
            self.assertEqual(client.get("/api/plugins/agents-cs/usage").status_code, 404)  # no unprefixed duplicate

if __name__=='__main__': unittest.main()
