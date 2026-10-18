"""V2 selected-model fallback: same classifier, no legacy router dependency."""
import copy
import json
import sqlite3
import threading
import time
import unittest
import test_routing_v2 as fixtures
reply, r = fixtures.reply, fixtures.r

class NativeFallbackTests(unittest.TestCase):
 body = fixtures.V2Tests.body
 plan = fixtures.V2Tests.plan
 def setUp(self):
  fixtures.V2Tests.setUp(self);self.e.pinned=lambda *a:False
  self.wire={'model':'gpt-6-astra','reasoning':{'effort':'low','summary':'auto'},'tools':[{'name':'example'}],'input':[{'role':'user','content':'body'}]}
 def prepare(self,turn='turn',text='Explain a token.',history=None):
  self.e.prepare_native(session_id='s',turn_id=turn,user_message=text,conversation_history=history or [],platform='tui',model=self.wire['model'])
 def request(self,turn='turn',**kw):
  args=dict(request=self.wire,session_id='s',turn_id=turn,provider='openai-codex',model=self.wire['model'],api_mode='codex_responses');args.update(kw);return self.e.native_request(**args)
 def test_selected_model_preserved_only_effort_changes(self):
  self.e.put_policy({'expectedRevision':1,'autoModelDefault':False,'autoEffortDefault':True,'slots':self.policy['slots']})
  self.answer=reply('complex');self.prepare();before=copy.deepcopy(self.wire)
  result=self.request()['request'];expected=copy.deepcopy(before);expected['reasoning']['effort']='high'
  self.assertEqual(result,expected);self.assertEqual(self.wire,before);self.assertEqual(result,self.request()['request']);self.assertEqual(self.calls,[1])
 def test_fallback_for_managed_session_without_current_plan(self):
  self.e.store.save_session('s','owner',{'routingOverride':True,'selectedBaseline':self.baseline},1)
  self.e.pinned=lambda *a:True;self.answer=reply('ordinary');self.prepare();self.assertEqual(self.request()['request']['reasoning']['effort'],'medium')
 def test_matching_plan_prevents_duplicate_classifier(self):
  p=self.plan();self.e.transition('s','owner',p['routeId'],'arm');self.wire['model']='gpt-6-luna'
  self.prepare(text='Summarize this paragraph.');self.assertIsNone(self.request());self.assertEqual(self.calls,[1])
 def test_profile_off_and_session_override(self):
  body={'expectedRevision':1,'enabledDefault':False,'slots':self.policy['slots']};self.e.put_policy(body)
  self.prepare();self.assertIsNone(self.request());self.assertEqual(self.calls,[])
  self.e.store.save_session('s','owner',{'routingOverride':True,'selectedBaseline':self.baseline},1)
  self.prepare('next');self.request('next');self.assertEqual(self.calls,[1])
 def test_uncertainty_preserves_model_and_effort(self):
  self.answer={};self.prepare();self.assertIsNone(self.request());self.assertIsNone(self.request());self.assertEqual(self.calls,[1])
 def test_manual_pins_and_explicit_text_do_not_call(self):
  self.e.pinned=lambda *a:True;self.prepare();self.assertIsNone(self.request());self.assertEqual(self.calls,[])
  self.e.pinned=lambda *a:False;self.prepare('explicit','Use high reasoning for this');self.assertIsNone(self.request('explicit'));self.assertEqual(self.calls,[])
 def test_shadow_cannot_apply_after_enable(self):
  self.cfg['plugins']['entries']['jev-routing']['settings']['mode']='shadow';self.answer=reply('complex');self.prepare();self.assertIsNone(self.request());self.assertEqual(self.calls,[1])
  self.cfg['plugins']['entries']['jev-routing']['settings']['mode']='on';self.assertIsNone(self.request())
 def test_code_and_actions_not_low(self):
  for n,response in enumerate([reply('simple','code'),reply('simple','general','action')]):
   self.answer=response;self.prepare(str(n));self.assertEqual(self.request(str(n))['request']['reasoning']['effort'],'medium')
 def test_excluded_context_and_secret(self):
  self.prepare(text='access_token=abcdefghijklmnop');self.assertIsNone(self.request());self.assertEqual(self.calls,[])
  states=[]
  def api(state,*a):states.append(state);return reply()
  self.e.api=api;self.prepare('next',history=[{'role':'system','content':'system secret'},{'role':'tool','content':'tool secret'},{'role':'assistant','content':'Reply to user@example.com'}]);self.request('next')
  text=json.dumps(states);self.assertNotIn('system secret',text);self.assertNotIn('tool secret',text);self.assertNotIn('user@example.com',text)
 def test_kill_late_result_and_cleanup(self):
  self.answer=reply('complex');self.prepare();(self.home/'jev.disabled').touch();self.assertIsNone(self.request());self.assertEqual(self.calls,[])
  self.e.end_native(session_id='s');self.assertEqual(len(self.e.turns),0)
 def test_native_deadline_and_no_queue(self):
  done=threading.Event();self.e.api=lambda *a:(done.wait(2),reply())[1]
  try:
   self.prepare();start=time.monotonic();self.assertIsNone(self.request());self.assertLess(time.monotonic()-start,.8)
   self.prepare('next');start=time.monotonic();self.assertIsNone(self.request('next'));self.assertLess(time.monotonic()-start,.1)
  finally:done.set()
 def test_session_pin_helper_readonly(self):
  from hermes_bots_dashboard.routing.classifier_io import session_pinned
  with sqlite3.connect(self.home/'state.db') as db:
   db.execute('create table sessions(id text,model_config text)');db.execute('insert into sessions values(?,?)',('manual','{"reasoning_config":{"effort":"low"}}'));db.execute('insert into sessions values(?,?)',('normal','{}'))
  self.assertTrue(session_pinned(self.home,'manual'));self.assertFalse(session_pinned(self.home,'normal'));self.assertTrue(session_pinned(self.home,'missing'))
